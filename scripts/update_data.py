from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PLUGIN_ROOT / "arknights_skins_dict.json"
SKIN_PAGE_URL = (
    "https://wiki.biligame.com/arknights/"
    "%E6%98%8E%E6%97%A5%E6%96%B9%E8%88%9F:%E6%B8%B8%E6%88%8F%E7%AB%8B%E7%BB%98"
)
OPERATOR_PAGE_URL = (
    "https://wiki.biligame.com/arknights/%E5%B9%B2%E5%91%98%E6%95%B0%E6%8D%AE%E8%A1%A8"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36 "
    "astrbot-plugin-mrfzccl-data-updater/1.0"
)
SKIN_ALT_PATTERN = re.compile(
    r"^Pack(?:\s|_)+(?P<name>.+?)(?:\s|_)+skin(?:\s|_)+.+\.[^.]+$",
    re.IGNORECASE,
)


def _attributes(items: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in items}


def _has_class(attributes: dict[str, str], class_name: str) -> bool:
    return class_name in attributes.get("class", "").split()


def _normalize_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _split_multiple_values(value: str) -> str | list[str]:
    normalized = value.strip()
    items = normalized.split()
    return items if len(items) > 1 else normalized


def _extract_skin_name(alt_text: str) -> str | None:
    match = SKIN_ALT_PATTERN.match(alt_text.strip())
    if not match:
        return None
    name = match.group("name").strip()
    return name or None


def _original_image_url(thumbnail_url: str) -> str:
    parsed = urlsplit(thumbnail_url)
    path = parsed.path.replace("/thumb/", "/", 1)
    path = re.sub(r"/\d+px-[^/]+$", "", path)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class _SkinPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str, str, str]] = []
        self._image_link: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = _attributes(attrs)
        if tag == "a":
            self._image_link = (
                attributes.get("href") if _has_class(attributes, "image") else None
            )
            return
        if tag != "img" or not self._image_link:
            return

        alt_text = attributes.get("alt", "")
        thumbnail_url = urljoin(SKIN_PAGE_URL, attributes.get("src", ""))
        name = _extract_skin_name(alt_text)
        if name and thumbnail_url:
            self.items.append((name, thumbnail_url, alt_text, self._image_link))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._image_link = None


class _OperatorTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = _attributes(attrs)
        if tag == "table":
            if self._table_depth:
                self._table_depth += 1
            elif attributes.get("id") == "CardSelectTr" or _has_class(
                attributes, "CardSelect"
            ):
                self._table_depth = 1
            return
        if not self._table_depth:
            return
        if tag == "tr" and _has_class(attributes, "divsort"):
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell_parts = []
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell_parts is not None:
            self._row.append(_normalize_text(self._cell_parts))
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell_parts = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
        elif self._cell_parts is not None and tag not in {"img", "input"}:
            self._cell_parts.append(" ")


def parse_skin_page(html_content: str) -> dict[str, dict[str, Any]]:
    parser = _SkinPageParser()
    parser.feed(html_content)
    skin_data: dict[str, dict[str, Any]] = {}
    for name, thumbnail_url, alt_text, source_link in parser.items:
        original_url = _original_image_url(thumbnail_url)
        if name not in skin_data:
            skin_data[name] = {
                "original_url": [original_url],
                "thumbnail_url": thumbnail_url,
                "alt_text": alt_text,
                "source_link": source_link,
            }
        elif original_url not in skin_data[name]["original_url"]:
            skin_data[name]["original_url"].append(original_url)
    return skin_data


def _operator_from_cells(cells: list[str]) -> tuple[str, dict[str, Any]] | None:
    if len(cells) < 15:
        return None
    name = cells[0].strip()
    if not name:
        return None

    profession_text = cells[2].strip()
    profession_parts = re.split(r"\s+[-–—]\s+", profession_text, maxsplit=1)
    profession = profession_parts[0]
    branch = profession_parts[1] if len(profession_parts) == 2 else ""
    data: dict[str, Any] = {
        "星级": cells[1],
        "职业分支": profession_text,
        "性别": cells[3],
        "阵营": cells[4],
        "获取途径": _split_multiple_values(cells[5]),
        "标签": _split_multiple_values(cells[6]),
        "初始生命": cells[7],
        "初始攻击": cells[8],
        "初始防御": cells[9],
        "初始法抗": cells[10],
        "再部署": cells[11],
        "部署费用": cells[12],
        "阻挡数": cells[13],
        "攻击间隔": cells[14],
        "是否感染": cells[15] if len(cells) > 15 else "",
        "职业": profession,
        "分支": branch,
    }
    return name, data


def parse_operator_page(html_content: str) -> dict[str, dict[str, Any]]:
    parser = _OperatorTableParser()
    parser.feed(html_content)
    operators: dict[str, dict[str, Any]] = {}
    for cells in parser.rows:
        parsed = _operator_from_cells(cells)
        if parsed is not None:
            name, data = parsed
            operators[name] = data
    return operators


def build_updated_data(
    existing: dict[str, Any],
    skins: dict[str, dict[str, Any]],
    operators: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    updated: dict[str, dict[str, Any]] = {
        name: dict(data)
        for name, data in existing.items()
        if isinstance(name, str) and isinstance(data, dict)
    }
    for name, skin_data in skins.items():
        entry = updated.setdefault(name, {})
        existing_urls = entry.get("original_url", [])
        if not isinstance(existing_urls, list):
            existing_urls = []
        merged_urls = list(
            dict.fromkeys([*existing_urls, *skin_data.get("original_url", [])])
        )
        entry.update(skin_data)
        entry["original_url"] = merged_urls
    for name, operator_data in operators.items():
        if name in updated:
            updated[name].update(operator_data)
    return updated


def _read_existing_data(output_path: Path) -> dict[str, Any]:
    if not output_path.exists():
        return {}
    with output_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"现有数据文件不是 JSON 对象: {output_path}")
    return data


def _write_json_atomically(output_path: Path, data: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if os.linesep != "\n":
        content = content.replace("\n", os.linesep)
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(content)
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def update_from_html(
    *,
    skin_html: str,
    operator_html: str,
    output_path: Path,
    min_skins: int = 100,
    min_operators: int = 100,
    write: bool = True,
) -> dict[str, int]:
    skins = parse_skin_page(skin_html)
    operators = parse_operator_page(operator_html)
    if len(skins) < min_skins:
        raise ValueError(
            f"立绘数据只有 {len(skins)} 条，低于安全阈值 {min_skins}，拒绝覆盖"
        )
    if len(operators) < min_operators:
        raise ValueError(
            f"干员数据只有 {len(operators)} 条，低于安全阈值 {min_operators}，拒绝覆盖"
        )

    existing = _read_existing_data(output_path)
    new_characters = sum(name not in existing for name in skins)
    new_images = 0
    for name, skin_data in skins.items():
        existing_entry = existing.get(name, {})
        existing_urls = (
            existing_entry.get("original_url", [])
            if isinstance(existing_entry, dict)
            else []
        )
        if not isinstance(existing_urls, list):
            existing_urls = []
        new_images += sum(
            url not in existing_urls for url in skin_data.get("original_url", [])
        )
    updated = build_updated_data(existing, skins, operators)
    if write:
        _write_json_atomically(output_path, updated)
    return {
        "skins": len(skins),
        "operators": len(operators),
        "before": len(existing),
        "after": len(updated),
        "new_characters": new_characters,
        "new_images": new_images,
    }


def fetch_page(url: str, *, timeout: float, user_agent: str) -> str:
    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _load_html(path: Path | None, url: str, args: argparse.Namespace) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    return fetch_page(url, timeout=args.timeout, user_agent=args.user_agent)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 bilibili 明日方舟 Wiki 更新插件自带的立绘与干员数据。"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--skin-html", type=Path, help="使用本地立绘页 HTML")
    parser.add_argument("--operator-html", type=Path, help="使用本地干员表 HTML")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--min-skins", type=int, default=100)
    parser.add_argument("--min-operators", type=int, default=100)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--dry-run", action="store_true", help="仅抓取和校验，不写入数据文件"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        skin_html = _load_html(args.skin_html, SKIN_PAGE_URL, args)
        operator_html = _load_html(args.operator_html, OPERATOR_PAGE_URL, args)
        counts = update_from_html(
            skin_html=skin_html,
            operator_html=operator_html,
            output_path=args.output.resolve(),
            min_skins=max(0, args.min_skins),
            min_operators=max(0, args.min_operators),
            write=not args.dry_run,
        )
    except (OSError, URLError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"更新失败: {exc}", file=sys.stderr)
        return 1

    action = "校验完成，未写入" if args.dry_run else "更新完成"
    print(
        f"{action}: 立绘 {counts['skins']} 名，干员资料 {counts['operators']} 名，"
        f"题库 {counts['before']} -> {counts['after']} 名，"
        f"新增角色 {counts['new_characters']} 名，新增皮肤 {counts['new_images']} 张"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

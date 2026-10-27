"""把文献元数据排成常见参考文献格式。

元数据存在文档 info 的 citation 字段里，结构：
    {"authors": "张三, 李四", "year": "2024", "title": "……",
     "source": "期刊/会议名", "volume": "12", "issue": "3",
     "pages": "45-52", "doi": "", "type": "article"}

缺字段时用「[作者]」这类占位符顶上，并告诉前端还差什么，
用户去「文献信息」里补齐就能生成完整条目。
"""

from __future__ import annotations

import re
from typing import Dict, List

STYLES = [
    {"id": "gbt7714", "name": "GB/T 7714", "hint": "国内论文、期刊投稿常用"},
    {"id": "apa", "name": "APA 7", "hint": "社科、英文期刊常用"},
    {"id": "mla", "name": "MLA 9", "hint": "人文学科常用"},
    {"id": "bibtex", "name": "BibTeX", "hint": "LaTeX 用户使用"},
]

# 文献类型：article 期刊论文 / conference 会议论文 / thesis 学位论文 /
#          book 图书 / web 网页，默认按期刊论文处理
_TYPES = {
    "article", "conference", "thesis", "book", "web",
}

_FIELD_LABELS = {
    "authors": "作者",
    "year": "年份",
    "source": "期刊 / 会议 / 出版社",
    "volume": "卷",
    "issue": "期",
    "pages": "页码",
}


def _clean(value) -> str:
    return " ".join(str(value or "").split())


_INITIALS = re.compile(r"^(?:[A-Z]\.)+$")
_FILE_EXT = re.compile(r"\.(?:pdf|docx?|md|markdown|txt)$", re.IGNORECASE)


def fields_of(info: Dict) -> Dict[str, str]:
    """从文档 info 里取出引用需要的字段，并做基本的缺省处理。"""
    raw = info.get("citation") if isinstance(info.get("citation"), dict) else {}
    filename = _clean(info.get("original_filename") or info.get("filename") or "")
    display = _clean(info.get("display_name"))
    stem = re.sub(r"\.[^.]+$", "", filename)
    title = _clean(raw.get("title")) or display or stem or "未命名文献"
    # 显示名往往直接沿用了文件名（带 .pdf / .md 后缀），引用里不该出现后缀
    title = _FILE_EXT.sub("", title).strip() or title

    year = _clean(raw.get("year"))
    if not year:
        match = re.search(r"(19|20)\d{2}", filename)
        year = match.group(0) if match else ""

    kind = _clean(raw.get("type")).lower()
    if kind not in _TYPES:
        kind = "article"

    return {
        "title": title,
        "authors": _clean(raw.get("authors")),
        "year": year,
        "source": _clean(raw.get("source")),
        "volume": _clean(raw.get("volume")),
        "issue": _clean(raw.get("issue")),
        "pages": _clean(raw.get("pages")),
        "doi": _clean(raw.get("doi")),
        "type": kind,
    }


def missing(fields: Dict[str, str]) -> List[str]:
    """返回还没填的关键字段（中文名），用于提示用户补录。"""
    need = ["authors", "year", "source"]
    if fields.get("type") in {"article", "conference"}:
        need.append("pages")
    return [_FIELD_LABELS[key] for key in need if not fields.get(key)]


def _split_authors(authors: str) -> List[str]:
    """把作者串拆成一个个名字。

    同时认这几种写法：
      "张三, 李四"                      → 两个中文作者
      "Vaswani, A., Shazeer, N."        → 姓 + 缩写，两两合并
      "张三；李四；王五"                → 分号分隔
    """
    if not authors:
        return []
    result: List[str] = []
    for chunk in re.split(r"[;；、]|\s+and\s+|\s*&\s*", authors):
        parts = [p.strip() for p in chunk.split(",") if p.strip()]
        i = 0
        while i < len(parts):
            name = parts[i]
            # 姓后面紧跟 "A." / "S. B." 这种缩写时，合成一个作者
            if i + 1 < len(parts) and _INITIALS.match(parts[i + 1].replace(" ", "")):
                name = name + ", " + parts[i + 1]
                i += 2
            else:
                i += 1
            if name and name not in {"等", "et al", "et al."}:
                result.append(name)
    return result


def _gbt_authors(authors: str) -> str:
    names = _split_authors(authors)
    if not names:
        return "[作者]"
    if len(names) > 3:
        return ", ".join(names[:3]) + ", 等"
    return ", ".join(names)


def _mla_authors(authors: str) -> str:
    names = _split_authors(authors)
    if not names:
        return "[Author]"
    if len(names) >= 3:
        return names[0] + ", et al"
    return ", and ".join(names)


def _apa_authors(authors: str) -> str:
    names = _split_authors(authors)
    if not names:
        return "[Author]"
    if len(names) == 1:
        return names[0]
    if len(names) <= 20:
        return ", ".join(names[:-1]) + ", & " + names[-1]
    return ", ".join(names[:19]) + ", ... " + names[-1]


def _place(value: str, placeholder: str) -> str:
    return value or placeholder


def _pages_gbt(pages: str) -> str:
    if not pages:
        return ""
    return ": " + pages.replace("-", "-").replace("—", "-")


def gbt7714(f: Dict[str, str]) -> str:
    # 作者串常以「.」结尾（如 Vaswani, A.），后面又要跟句点，先把尾点去掉免得出现「..」
    authors = _gbt_authors(f["authors"]).rstrip(".")
    year = _place(f["year"], "[年份]")
    title = f["title"]
    source = _place(f["source"], "[期刊或会议名称]")
    volume = f["volume"]
    issue = f["issue"]
    pages = f["pages"]
    kind = f["type"]

    if kind == "thesis":
        return f"{authors}. {title}[D]. {source}, {year}."
    if kind == "book":
        return f"{authors}. {title}[M]. {source}, {year}."
    if kind == "web":
        return f"{authors}. {title}[EB/OL]. ({year})[引用日期]. {source}."
    if kind == "conference":
        return (f"{authors}. {title}[C]//{source}. {year}"
                + (f": {pages}." if pages else "."))

    # 默认期刊论文
    vol_part = volume
    if issue:
        vol_part = f"{volume}({issue})" if volume else f"({issue})"
    tail = f", {year}"
    if vol_part:
        tail += f", {vol_part}"
    tail += (_pages_gbt(pages) or "") + "."
    prefix = f"{authors}. {title}[J]. {source}"
    return prefix + tail


def apa(f: Dict[str, str]) -> str:
    authors = _apa_authors(f["authors"])
    year = _place(f["year"], "n.d.")
    title = f["title"]
    source = f["source"]
    volume = f["volume"]
    issue = f["issue"]
    pages = f["pages"]
    doi = f["doi"]
    kind = f["type"]

    if kind == "book":
        text = f"{authors} ({year}). {title}."
        if source:
            text += f" {source}."
    elif kind == "thesis":
        text = f"{authors} ({year}). {title} [Doctoral dissertation / Master's thesis]."
        if source:
            text += f" {source}."
    elif kind == "conference":
        text = f"{authors} ({year}). {title}."
        if source:
            text += f" In {source}"
            if pages:
                text += f" (pp. {pages})"
            text += "."
    else:
        text = f"{authors} ({year}). {title}."
        if source:
            text += f" {source}"
            if volume:
                text += f", {volume}"
            if issue:
                text += f"({issue})"
            if pages:
                text += f", {pages}"
            text += "."
    if doi:
        text += f" https://doi.org/{doi.lstrip('https://doi.org/').lstrip('/')}"
    return re.sub(r"\s+", " ", text).strip()


def mla(f: Dict[str, str]) -> str:
    authors = _mla_authors(f["authors"])
    year = _place(f["year"], "n.d.")
    title = f["title"]
    source = _place(f["source"], "[Source]")
    volume = f["volume"]
    issue = f["issue"]
    pages = f["pages"]

    text = f'{authors}. "{title}." {source}'
    if volume:
        text += f", vol. {volume}"
    if issue:
        text += f", no. {issue}"
    text += f", {year}"
    if pages:
        text += f", pp. {pages}"
    return text if text.endswith(".") else text + "."


def bibtex(f: Dict[str, str]) -> str:
    kind = {"thesis": "phdthesis", "book": "book",
            "conference": "inproceedings", "web": "misc"}.get(f["type"], "article")
    source_key = {"thesis": "school", "book": "publisher",
                  "conference": "booktitle", "web": "howpublished"}.get(
        f["type"], "journal")
    key_base = re.sub(r"[^A-Za-z0-9]+", "", (f["authors"].split(",")[0] if f["authors"] else "ref"))
    # 用标题里的字母数字做后缀，保证同一个进程内外生成的 key 都一样
    slug = re.sub(r"[^A-Za-z0-9]+", "", f["title"])[:12]
    if not slug:  # 纯中文标题，用字符和生成稳定的短标识
        slug = str(sum(ord(ch) for ch in f["title"]) % 1000)
    key = (key_base or "ref") + _place(f["year"], "") + slug
    lines = [
        "@" + kind + "{" + key + ",",
        "  author = {" + _place(f["authors"], "作者") + "},",
        "  title = {" + f["title"] + "},",
        "  " + source_key + " = {" + _place(f["source"], "来源") + "},",
        "  year = {" + _place(f["year"], "年份") + "},",
    ]
    if f["volume"]:
        lines.append("  volume = {" + f["volume"] + "},")
    if f["issue"]:
        lines.append("  number = {" + f["issue"] + "},")
    if f["pages"]:
        lines.append("  pages = {" + f["pages"] + "},")
    if f["doi"]:
        lines.append("  doi = {" + f["doi"] + "},")
    lines.append("}")
    return "\n".join(lines)


_RENDERERS = {
    "gbt7714": gbt7714,
    "apa": apa,
    "mla": mla,
    "bibtex": bibtex,
}


def render(style: str, fields: Dict[str, str]) -> str:
    renderer = _RENDERERS.get((style or "gbt7714").lower(), gbt7714)
    return renderer(fields)

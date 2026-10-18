# -*- coding: utf-8 -*-
"""把当前摘要、问答记录导出为 Word 或 Markdown。

导出内容由前端直接提交，不再调用大模型；这里只做格式转换。
[[p.3]] 这类出处标记会转成「（第 3 页）」，打印和二次编辑时更直观。
"""
import io
import re
from datetime import datetime
from typing import Dict, List

_CITE_RE = re.compile(r"\[\[\s*p\.?\s*(\d+)\s*\]\]")
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")


def _citation_text(text: str, unit: str = "页") -> str:
    return _CITE_RE.sub(lambda m: f"（第 {m.group(1)} {unit}）", text or "")


def _summary_sections(text: str) -> List[Dict]:
    """按模型输出的 ## 小标题切分；没有小标题就整体当一节。"""
    sections: List[Dict] = []
    current = {"title": "", "lines": []}
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        match = _HEADING_RE.match(line)
        if match:
            if current["title"] or any(item.strip() for item in current["lines"]):
                sections.append(current)
            current = {"title": match.group(1).strip(), "lines": []}
        else:
            current["lines"].append(raw.rstrip())
    if current["title"] or any(item.strip() for item in current["lines"]):
        sections.append(current)
    for section in sections:
        lines = section["lines"]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
    return [section for section in sections
            if section["title"] or any(item.strip() for item in section["lines"])]


def _body_lines(text: str, unit: str) -> List[str]:
    """正文按非空行拆段，保留模型给出的分行。"""
    return [line.strip() for line in _citation_text(text, unit).split("\n")
            if line.strip()]


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _weekday_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ------------------------------------------------------------------ Word 工具
def _set_east_asian(run, name: str) -> None:
    from docx.oxml.ns import qn

    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)


def _style_heading(paragraph) -> None:
    from docx.shared import Pt, RGBColor

    for run in paragraph.runs:
        _set_east_asian(run, "黑体")
        run.font.color.rgb = RGBColor(0, 0, 0)
        if paragraph.style.name == "Heading 1":
            run.font.size = Pt(16)
        else:
            run.font.size = Pt(13)


def _add_page_footer(document) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    paragraph = document.sections[0].footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    prefix = paragraph.add_run("智读小站 · 第 ")
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(separate)
    run._r.append(end)
    suffix = paragraph.add_run(" 页")
    for item in (prefix, run, suffix):
        _set_east_asian(item, "宋体")
        item.font.size = Pt(9)
        item.font.color.rgb = RGBColor(0x80, 0x80, 0x80)


def _new_document(title: str):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(10.5)
    try:
        style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    except Exception:  # noqa: BLE001  字体设置失败不影响导出
        pass
    document.core_properties.title = title
    document.core_properties.author = "智读小站"
    return document


def _document_title(info: Dict) -> str:
    name = info.get("display_name") or info.get("original_filename") or "文献"
    return f"《{name}》"


# ------------------------------------------------------------------ 摘要
def summary_docx(info: Dict, text: str, model: str = "") -> bytes:
    unit = info.get("unit", "页")
    document = _new_document(f"{_document_title(info)}文献摘要")
    title = document.add_heading(f"{_document_title(info)}文献摘要", level=1)
    _style_heading(title)
    meta = f"导出时间：{_stamp()}"
    if model:
        meta += f"　生成模型：{model}"
    document.add_paragraph(meta)

    sections = _summary_sections(text)
    if not sections:
        document.add_paragraph(_citation_text(text, unit))
    for section in sections:
        if section["title"]:
            heading = document.add_heading(_citation_text(section["title"], unit), level=2)
            _style_heading(heading)
        for line in _body_lines("\n".join(section["lines"]), unit):
            document.add_paragraph(line)

    _add_page_footer(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def summary_markdown(info: Dict, text: str, model: str = "") -> bytes:
    unit = info.get("unit", "页")
    lines = [f"# {_document_title(info)}文献摘要", ""]
    meta = f"> 导出时间：{_stamp()}"
    if model:
        meta += f" · 生成模型：{model}"
    lines.extend([meta, ""])
    sections = _summary_sections(text)
    if not sections:
        lines.append(_citation_text(text, unit))
        lines.append("")
    for section in sections:
        if section["title"]:
            lines.extend([f"## {_citation_text(section['title'], unit)}", ""])
        body = _citation_text("\n".join(section["lines"]), unit).strip()
        if body:
            lines.extend([body, ""])
    return "\n".join(lines).encode("utf-8")


# ------------------------------------------------------------------ 问答
def _turn_citations(turn: Dict, unit: str) -> str:
    pages = []
    for value in turn.get("citations") or []:
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page not in pages:
            pages.append(page)
    if not pages:
        return ""
    return "、".join(f"第 {page} {unit}" for page in pages)


def _turn_passages(turn: Dict) -> List[Dict]:
    result = []
    for passage in (turn.get("passages") or [])[:4]:
        if not isinstance(passage, dict):
            continue
        try:
            page = int(passage.get("page"))
        except (TypeError, ValueError):
            continue
        text = str(passage.get("text") or "").strip()
        if text:
            result.append({"page": page,
                           "section": str(passage.get("section") or "").strip(),
                           "text": text[:240]})
    return result


def qa_docx(info: Dict, turns: List[Dict]) -> bytes:
    from docx.shared import Pt

    unit = info.get("unit", "页")
    document = _new_document(f"{_document_title(info)}问答记录")
    title = document.add_heading(f"{_document_title(info)}问答记录", level=1)
    _style_heading(title)
    document.add_paragraph(f"导出时间：{_stamp()}　共 {len(turns)} 轮问答")

    for index, turn in enumerate(turns, 1):
        question = str(turn.get("q") or "").strip()
        answer = str(turn.get("a") or "").strip()
        heading = document.add_heading(f"第 {index} 问：{question}", level=2)
        _style_heading(heading)
        for line in _body_lines(answer, unit):
            document.add_paragraph(line)

        citations = _turn_citations(turn, unit)
        if citations:
            paragraph = document.add_paragraph(f"参考出处：{citations}")
            for run in paragraph.runs:
                run.font.size = Pt(9)

        passages = _turn_passages(turn)
        if passages:
            label = document.add_paragraph("参考原文：")
            for run in label.runs:
                run.font.size = Pt(9)
            for passage in passages:
                text = passage["text"]
                if passage["section"]:
                    text = f"［{passage['section']}］{text}"
                paragraph = document.add_paragraph(f"[第 {passage['page']} {unit}] {text}")
                paragraph.paragraph_format.left_indent = Pt(12)
                for run in paragraph.runs:
                    run.font.size = Pt(9)

    _add_page_footer(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def qa_markdown(info: Dict, turns: List[Dict]) -> bytes:
    unit = info.get("unit", "页")
    lines = [f"# {_document_title(info)}问答记录", "",
             f"> 导出时间：{_stamp()} · 共 {len(turns)} 轮问答", ""]
    for index, turn in enumerate(turns, 1):
        question = str(turn.get("q") or "").strip()
        answer = _citation_text(str(turn.get("a") or "").strip(), unit)
        lines.extend([f"## 第 {index} 问：{question}", "", answer, ""])
        citations = _turn_citations(turn, unit)
        if citations:
            lines.extend([f"> 参考出处：{citations}", ""])
        passages = _turn_passages(turn)
        if passages:
            lines.append("> 参考原文：")
            for passage in passages:
                section = f"［{passage['section']}］" if passage["section"] else ""
                lines.append(f"> - [第 {passage['page']} {unit}] {section}{passage['text']}")
            lines.append("")
        if index != len(turns):
            lines.extend(["---", ""])
    return "\n".join(lines).encode("utf-8")


# ------------------------------------------------------------------ 入口
def build_docx(info: Dict, kind: str, payload: Dict) -> bytes:
    if kind == "summary":
        return summary_docx(info, payload.get("text", ""), payload.get("model", ""))
    return qa_docx(info, payload.get("turns") or [])


def build_markdown(info: Dict, kind: str, payload: Dict) -> bytes:
    if kind == "summary":
        return summary_markdown(info, payload.get("text", ""), payload.get("model", ""))
    return qa_markdown(info, payload.get("turns") or [])


def export_filename(info: Dict, kind: str, fmt: str) -> str:
    stem = str(info.get("display_name") or info.get("original_filename") or "文献")
    stem = re.sub(r"\.[^.]+$", "", stem) or "文献"
    stem = re.sub(r'[\\/:*?"<>|]+', "_", stem)
    suffix = "摘要" if kind == "summary" else "问答记录"
    return f"{stem}_{suffix}_{_weekday_stamp()}.{fmt}"

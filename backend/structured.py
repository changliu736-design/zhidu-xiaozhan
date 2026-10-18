# -*- coding: utf-8 -*-
"""结构化提取：表格、公式、图注。

PDF 文字层里的表格通常是一堆按位置散落的文字，公式和图注也混在正文里。
这里在解析阶段做三件事：

1. 表格：优先识别有完整线框的表格（PyMuPDF lines 策略）；只有横线
   （booktabs 风格）时，先用横线定位表格区域，再在该区域内用 text
   策略还原行列；最后才尝试整页 text 策略，并用严格的面积/填充率
   过滤掉"把整页正文当成一张表"的误判。
2. 图注 / 表注：按「图 1」「Table 2.」这类题注格式识别并加标记。
3. 公式：按数学符号、等号模式和行长度识别，短公式行加标记。

提取结果以【表格】【图注】【表注】【公式】标记写回正文，表格另有
结构化行数据，由分块阶段生成独立的表格块。图片型 PDF 的表格/公式
只有 OCR 文字，不做版面还原，也不把公式转成 LaTeX。
"""
import re
from typing import Dict, List, Optional, Sequence, Tuple

import config

try:  # PyMuPDF 新版包名
    import pymupdf as _fitz
except ImportError:  # pragma: no cover
    import fitz as _fitz


# ------------------------------------------------------------------ 识别规则
_CAPTION_RE = re.compile(
    r"^(?P<label>图|表|图注|表注|Figure|Fig\.?|Table|Tab\.?|Algorithm|Alg\.?|"
    r"Equation|Eq\.?|公式|式)\s*"
    r"(?P<number>[0-9一二三四五六七八九十]+(?:[.\-－—:：][0-9]+)*)"
    r"(?=$|[\s:：.．、，,（(【\[]|[一-鿿])",
    re.IGNORECASE,
)

_MATH_SYMBOLS = set("=∑∫√≈≠≤≥±×÷∂∇∞∈∉⊂⊃∪∩→←↔⇒⇔≡∝⊥∠′″")
_MATH_TEX = re.compile(r"(\\(?:frac|sum|int|alpha|beta|gamma|lambda|sigma|theta)"
                       r"|\$\$|\\\[|\\\()")
_ASSIGN_RE = re.compile(r"[A-Za-z0-9α-ωΑ-Ω)\]}]\s*=\s*[-+A-Za-z0-9(α-ωΑ-Ω]")
_CJK_RE = re.compile(r"[一-鿿]")
_SENTENCE_END = ("。", "！", "？")


def _caption_kind(label: str) -> str:
    label = label.lower()
    if label.startswith(("fig", "图")):
        return "图注"
    if label.startswith(("tab", "表")):
        return "表注"
    if label.startswith(("equation", "eq", "公式", "式")):
        return "公式注"
    if label.startswith(("algorithm", "alg")):
        return "算法注"
    return "图注"


def match_caption(line: str) -> Optional[Tuple[str, str]]:
    """返回 (标记类型, 原文)，不像题注则返回 None。"""
    text = line.strip()
    if not text or len(text) > 220:
        return None
    match = _CAPTION_RE.match(text)
    if not match:
        return None
    return _caption_kind(match.group("label")), text


def is_formula_line(line: str) -> bool:
    """判断一行是否公式。宁可少标，也不要把正文整段标成公式。"""
    text = line.strip()
    if not (3 <= len(text) <= 160):
        return False
    if match_caption(text):
        return False
    if text.endswith(_SENTENCE_END):
        return False

    symbols = sum(1 for ch in text if ch in _MATH_SYMBOLS)
    cjk = len(_CJK_RE.findall(text))

    if _MATH_TEX.search(text):
        return True
    if symbols >= 1:
        if cjk == 0:
            return True
        # 含中文时要求符号更集中，避免把「从 A→B 的映射关系」这类正文当公式
        return len(text) <= 60 and (symbols >= 2 or "=" in text or "≈" in text)
    if cjk <= 24 and _ASSIGN_RE.search(text):
        return True
    return False


def _annotate_line(line: str) -> Tuple[str, Optional[str], Optional[str]]:
    """给单行加标记，返回 (标注后文本, 题注类型, 公式原文)。"""
    text = line.strip()
    if not text:
        return "", None, None
    caption = match_caption(text)
    if caption:
        kind, original = caption
        return f"【{kind}】{original}", kind, None
    if is_formula_line(text):
        return f"【公式】{text}", None, text
    return text, None, None


# ------------------------------------------------------------------ 文本块
def _text_blocks(page) -> List[Dict]:
    try:
        raw = page.get_text("blocks", sort=True)
    except Exception:  # noqa: BLE001
        try:
            raw = page.get_text("blocks")
        except Exception:  # noqa: BLE001
            return []
    blocks: List[Dict] = []
    for item in raw:
        if len(item) < 5:
            continue
        if len(item) >= 7 and item[6] != 0:  # block_type 1 = 图片
            continue
        text = str(item[4] or "").strip()
        if not text:
            continue
        blocks.append({
            "bbox": (float(item[0]), float(item[1]), float(item[2]), float(item[3])),
            "text": text,
        })
    return blocks


def _overlaps(block: Sequence[float], table: Sequence[float]) -> bool:
    bx0, by0, bx1, by1 = block
    tx0, ty0, tx1, ty1 = table
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    if tx0 <= cx <= tx1 and ty0 <= cy <= ty1:
        return True
    width = min(bx1, tx1) - max(bx0, tx0)
    height = min(by1, ty1) - max(by0, ty0)
    if width <= 0 or height <= 0:
        return False
    area = max((bx1 - bx0) * (by1 - by0), 1.0)
    return width * height / area > 0.5


def _caption_entries(blocks: Sequence[Dict]) -> List[Dict]:
    entries: List[Dict] = []
    seen = set()
    for block in blocks:
        for line in block["text"].splitlines():
            caption = match_caption(line)
            if not caption:
                continue
            kind, text = caption
            key = (kind, text)
            if key in seen:
                continue
            seen.add(key)
            entries.append({"kind": kind, "text": text, "bbox": block["bbox"]})
    return entries


# ------------------------------------------------------------------ 表格识别
def _clean_cell(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace(" ", " ")).strip()


def _table_rows(table) -> Tuple[List[List[str]], List[Tuple[float, ...]]]:
    """清洗表格行：去掉空行、行尾空列，并保留每行的 bbox 用于裁掉表外正文。"""
    try:
        raw_rows = table.extract() or []
    except Exception:  # noqa: BLE001
        return [], []
    boxes: List[Tuple[float, ...]] = []
    try:
        boxes = [tuple(float(v) for v in row.bbox) for row in table.rows]
    except Exception:  # noqa: BLE001
        boxes = []

    cleaned: List[List[str]] = []
    kept_boxes: List[Tuple[float, ...]] = []
    for index, raw in enumerate(raw_rows):
        cells = [_clean_cell(cell) for cell in raw]
        while cells and not cells[-1]:
            cells.pop()
        if not any(cells):
            continue
        cleaned.append(cells)
        kept_boxes.append(boxes[index] if index < len(boxes) else ())
    return cleaned, kept_boxes


def _trim_trailing_prose(rows: List[List[str]],
                         boxes: Sequence[Tuple[float, ...]]) -> Tuple[List[List[str]], List[Tuple[float, ...]]]:
    """text 策略有时会把表后的一段正文并进来；遇到明显空档就截断。"""
    if len(rows) < 3 or len(boxes) != len(rows):
        return rows, list(boxes)
    heights = [box[3] - box[1] for box in boxes if len(box) == 4 and box[3] > box[1]]
    heights.sort()
    median = heights[len(heights) // 2] if heights else 0
    limit = max(24.0, median * 2.2)
    keep = len(rows)
    for index in range(1, len(rows)):
        prev, current = boxes[index - 1], boxes[index]
        if len(prev) != 4 or len(current) != 4:
            continue
        gap = current[1] - prev[3]
        joined = " ".join(cell for cell in rows[index] if cell)
        if gap > limit and len(joined) > 60:
            keep = index
            break
    return rows[:keep], list(boxes[:keep])


def _table_stats(rows: Sequence[Sequence[str]]) -> Tuple[int, int, float]:
    n_rows = len(rows)
    n_cols = max((len(row) for row in rows), default=0)
    filled = sum(1 for row in rows for cell in row if cell)
    return n_rows, n_cols, filled / max(1, n_rows * n_cols)


def _table_payload(table, strategy: str, page_height: float,
                   clipped: bool = False) -> Optional[Dict]:
    rows, boxes = _table_rows(table)
    if not rows:
        return None
    if strategy == "text" and not clipped:
        rows, boxes = _trim_trailing_prose(rows, boxes)
    n_rows, n_cols, fill = _table_stats(rows)
    if n_rows < 2 or n_cols < 2:
        return None
    if sum(1 for row in rows if sum(1 for cell in row if cell) >= 2) < 2:
        return None

    bbox = tuple(float(v) for v in table.bbox)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if width < 100 or height < 20:
        return None

    if strategy == "text" and not clipped:
        # 整页正文被误判成表格时，bbox 几乎覆盖整页；这里直接拒绝。
        if height > page_height * 0.55 or width < 120 or height < 35:
            return None
        if fill < 0.55 or n_rows < 3:
            return None
    elif strategy == "text" and clipped:
        if fill < 0.35:
            return None

    padded = [list(row) + [""] * (n_cols - len(row)) for row in rows]
    return {
        "caption": "",
        "caption_kind": "",
        "rows": padded,
        "n_rows": n_rows,
        "n_cols": n_cols,
        "bbox": bbox,
        "strategy": strategy,
    }


def _horizontal_rule_clips(page, page_height: float) -> List[Tuple[float, float, float, float]]:
    """找出由多条横线围成的区域（booktabs 表格只有横线，没有竖线）。"""
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return []

    rules: List[Tuple[float, float, float]] = []
    for drawing in drawings:
        for item in drawing.get("items", []):
            if not item:
                continue
            if item[0] == "l" and len(item) >= 3:
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) <= 1.5 and abs(p1.x - p2.x) >= 80:
                    rules.append((min(p1.x, p2.x), max(p1.x, p2.x),
                                  (p1.y + p2.y) / 2))
            elif item[0] == "re" and len(item) >= 2:
                rect = item[1]
                if rect.width >= 80 and rect.height <= 2.5:
                    rules.append((rect.x0, rect.x1, (rect.y0 + rect.y1) / 2))
    if len(rules) < 2:
        return []

    rules.sort(key=lambda rule: rule[2])
    merged: List[List[float]] = []
    for x0, x1, y in rules:
        if merged and abs(y - merged[-1][2]) <= 2:
            merged[-1] = [min(merged[-1][0], x0), max(merged[-1][1], x1),
                          (merged[-1][2] + y) / 2]
        else:
            merged.append([x0, x1, y])

    runs: List[List[List[float]]] = []
    current: List[List[float]] = []
    for rule in merged:
        if not current:
            current = [rule]
            continue
        previous = current[-1]
        gap = rule[2] - previous[2]
        overlap = min(previous[1], rule[1]) - max(previous[0], rule[0])
        if gap <= 260 and overlap >= 60:
            current.append(rule)
        else:
            if len(current) >= 2:
                runs.append(current)
            current = [rule]
    if len(current) >= 2:
        runs.append(current)

    clips: List[Tuple[float, float, float, float]] = []
    for run in runs:
        x0 = min(rule[0] for rule in run)
        x1 = max(rule[1] for rule in run)
        y0, y1 = run[0][2] - 3, run[-1][2] + 3
        if 20 <= y1 - y0 <= page_height * 0.72:
            clips.append((x0 - 3, y0, x1 + 3, y1))
    return clips


def _group_words(words: Sequence[Sequence]) -> List[Dict]:
    """把 words 按纵向位置聚成行。"""
    lines: List[Dict] = []
    for word in sorted(words, key=lambda item: (round(float(item[1]), 1), float(item[0]))):
        y0, y1 = float(word[1]), float(word[3])
        if lines and abs(y0 - lines[-1]["y0"]) <= 3:
            lines[-1]["words"].append(word)
            lines[-1]["y1"] = max(lines[-1]["y1"], y1)
        else:
            lines.append({"y0": y0, "y1": y1, "words": [word]})
    for line in lines:
        line["words"].sort(key=lambda word: float(word[0]))
    return lines


def _line_cells(line: Dict, gap: float = 16.0) -> List[str]:
    """同一行里被明显空白隔开的部分算作不同单元格。"""
    cells: List[str] = []
    current: List[str] = []
    previous_x1: Optional[float] = None
    for word in line["words"]:
        x0, x1, text = float(word[0]), float(word[2]), str(word[4])
        if previous_x1 is not None and x0 - previous_x1 >= gap and current:
            cells.append(" ".join(current).strip())
            current = []
        current.append(text)
        previous_x1 = x1
    if current:
        cells.append(" ".join(current).strip())
    return [cell for cell in cells if cell]


def _caption_tables(page, captions: Sequence[Dict], page_height: float) -> List[Dict]:
    """识别没有线框、只靠空白对齐的表格：从「表 N」题注向下按列对齐还原。"""
    tables: List[Dict] = []
    page_width = float(page.rect.width)
    for caption in captions:
        if caption.get("kind") != "表注" or not caption.get("bbox"):
            continue
        caption_box = caption["bbox"]
        clip = (0, caption_box[3] + 1, page_width,
                min(page_height, caption_box[3] + 420))
        try:
            words = page.get_text("words", clip=clip)
        except Exception:  # noqa: BLE001
            continue
        lines = [line for line in _group_words(words)
                 if line["y0"] >= caption_box[3] - 1]
        row_cells: List[List[str]] = []
        row_boxes: List[Tuple[float, ...]] = []
        heights = [line["y1"] - line["y0"] for line in lines]
        heights.sort()
        line_height = heights[len(heights) // 2] if heights else 12.0
        started = False
        for line in lines:
            cells = _line_cells(line)
            if len(cells) < 2:
                if started:
                    break
                continue
            if not started:
                if line["y0"] - caption_box[3] > 60:
                    break
                started = True
            elif row_boxes and line["y0"] - row_boxes[-1][3] > max(28, line_height * 2.5):
                break
            row_cells.append(cells)
            row_boxes.append(tuple(float(v) for v in line["words"][0][:4])[:2] +
                             (float(line["words"][-1][2]), line["y1"]))
        if len(row_cells) < 2:
            continue
        data_rows = row_cells[1:]
        if not any(any(_CJK_RE.search(cell) or re.search(r"\d", cell) for cell in row)
                   for row in data_rows):
            continue
        if not any(re.search(r"\d", cell) for row in data_rows for cell in row):
            continue
        n_cols = max(len(row) for row in row_cells)
        if n_cols < 2:
            continue
        padded = [row + [""] * (n_cols - len(row)) for row in row_cells]
        bbox = (
            min(box[0] for box in row_boxes),
            row_boxes[0][1],
            max(box[2] for box in row_boxes),
            row_boxes[-1][3],
        )
        if bbox[2] - bbox[0] < 100 or bbox[3] - bbox[1] < 20:
            continue
        tables.append({
            "caption": caption["text"],
            "caption_kind": caption["kind"],
            "rows": padded,
            "n_rows": len(padded),
            "n_cols": n_cols,
            "bbox": bbox,
            "strategy": "whitespace",
        })
    return tables


def _extract_tables(page, page_height: float, text_chars: int = 0,
                    captions: Sequence[Dict] = ()) -> List[Dict]:
    tables: List[Dict] = []

    try:
        for table in page.find_tables(strategy="lines").tables:
            payload = _table_payload(table, "lines", page_height)
            if payload:
                tables.append(payload)
    except Exception:  # noqa: BLE001 表格识别失败不影响正文
        pass
    if tables:
        return tables

    # 只有横线的表格：先用横线定位区域，再在区域内做 text 策略识别，
    # 避免把表格下方的正文也吸进来。
    for clip in _horizontal_rule_clips(page, page_height):
        try:
            for table in page.find_tables(strategy="text", clip=clip).tables:
                payload = _table_payload(table, "text", page_height, clipped=True)
                if payload:
                    tables.append(payload)
        except Exception:  # noqa: BLE001
            continue
    if tables:
        return tables

    # 没有任何线条、只靠空白对齐的表格：从「表 N」题注向下按列对齐还原。
    if captions:
        tables = _caption_tables(page, captions, page_height)
        if tables:
            return tables

    # 文字很密的页面不做整页 text 策略兜底：既慢，也最容易把正文误判成表格。
    # 有完整线框或横线的表格在前两步已经识别过了。
    if text_chars > 4000:
        return tables

    # 无线表格的最后兜底：整页 text 策略，面积/填充率限制很严格。
    try:
        for table in page.find_tables(strategy="text").tables:
            payload = _table_payload(table, "text", page_height)
            if payload:
                tables.append(payload)
    except Exception:  # noqa: BLE001
        pass
    return tables


def _associate_caption(table_bbox: Sequence[float],
                       captions: Sequence[Dict]) -> Optional[Dict]:
    best: Optional[Tuple[float, Dict]] = None
    for caption in captions:
        bbox = caption["bbox"]
        overlap = min(table_bbox[2], bbox[2]) - max(table_bbox[0], bbox[0])
        if overlap < 20:
            continue
        if bbox[3] <= table_bbox[1]:
            gap = table_bbox[1] - bbox[3]
            if gap > 110:
                continue
            score = gap + (0 if caption["kind"] == "表注" else 20)
        elif bbox[1] >= table_bbox[3]:
            gap = bbox[1] - table_bbox[3]
            if gap > 90:
                continue
            score = gap + (15 if caption["kind"] == "表注" else 30)
        else:
            continue
        if best is None or score < best[0]:
            best = (score, caption)
    return best[1] if best else None


# ------------------------------------------------------------------ 对外接口
def annotate_text(text: str) -> Dict:
    """给纯文本（OCR 结果、纯文本文件）加图注/公式标记。"""
    lines: List[str] = []
    captions: List[Dict] = []
    formulas: List[str] = []
    for raw in (text or "").splitlines():
        annotated, kind, formula = _annotate_line(raw)
        if not annotated:
            continue
        lines.append(annotated)
        if kind:
            captions.append({"kind": kind, "text": raw.strip(), "bbox": None})
        if formula:
            formulas.append(formula)
    return {"text": "\n".join(lines), "captions": captions, "formulas": formulas}


def _annotate_plain(text: str) -> str:
    return annotate_text(text)["text"]


def extract_page(page, fallback_text: str) -> Dict:
    """提取一页的结构：正文（带标记）、表格、题注、公式。"""
    empty = {"text": fallback_text or "", "tables": [], "captions": [], "formulas": []}
    if not config.STRUCTURED_ENABLED:
        return empty

    blocks = _text_blocks(page)
    captions = _caption_entries(blocks)
    tables = _extract_tables(page, float(page.rect.height),
                             len((fallback_text or "").strip()), captions)

    formulas: List[str] = []
    for block in blocks:
        for line in block["text"].splitlines():
            _, _, formula = _annotate_line(line)
            if formula:
                formulas.append(formula)

    if not tables:
        text = _annotate_plain(fallback_text or "")
    else:
        kept: List[str] = []
        expected = 0
        table_boxes = [table["bbox"] for table in tables]
        for block in blocks:
            if any(_overlaps(block["bbox"], box) for box in table_boxes):
                continue
            expected += len(block["text"])
            for line in block["text"].splitlines():
                annotated, _, _ = _annotate_line(line)
                if annotated:
                    kept.append(annotated)
        text = "\n".join(kept)

        # 块级抽取丢字太多时退回整页文本（表格文字会重复，但不丢正文）；
        # 这里只和"表格区域之外"的文本量比较，避免把表格文字当成丢失内容。
        if not expected or len(text) < expected * 0.6:
            text = _annotate_plain(fallback_text or "")

        for table in tables:
            caption = _associate_caption(table["bbox"], captions)
            if caption:
                table["caption"] = caption["text"]
                table["caption_kind"] = caption["kind"]
                if caption["text"] not in text:
                    text = f"【{caption['kind']}】{caption['text']}\n{text}"

    if not text.strip() and fallback_text:
        text = fallback_text

    return {
        "text": text,
        "tables": tables,
        "captions": captions,
        "formulas": formulas,
    }

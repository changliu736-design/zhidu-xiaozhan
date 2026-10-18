# -*- coding: utf-8 -*-
"""检索层：按页解析 PDF → 带页码/章节的分块 → 混合检索（向量 + BM25）→ 词面重排。

相对原始实现的关键改进：
  1. 原实现用 pdfplumber 抽纯文本，页码信息在抽取时就丢了，所以答案永远无法给出出处。
     这里改用本机已有的 PyMuPDF，逐页提取，每个分块都带页码。
  2. 原实现只做向量检索（且用英文模型）。这里加了一路 BM25 关键词检索，
     对"专有名词、公式符号、人名"这类向量模型不敏感的查询命中率明显更高。
  3. 原实现按 500 字符硬切，会把句子拦腰截断。这里按句子边界切，并带一句重叠。
"""
import math
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

import config
import ocr
import structured
from embeddings import EmbeddingUnavailable, service

# ------------------------------------------------------------------ PDF 解析
try:  # PyMuPDF 新版包名
    import pymupdf as _fitz
except ImportError:  # pragma: no cover
    import fitz as _fitz


class PdfExtractError(RuntimeError):
    pass


def _page_needs_ocr(page, text: str) -> bool:
    """文字过少且页面上有图片，才认为这页是扫描图。"""
    if len(text.strip()) >= config.OCR_MIN_PAGE_CHARS:
        return False
    try:
        return bool(page.get_images(full=False))
    except Exception:  # noqa: BLE001
        return True


def _render_page_for_ocr(page):
    """把 PDF 页渲染成 RGB numpy 数组，交给 RapidOCR。"""
    pix = page.get_pixmap(dpi=max(120, config.OCR_DPI))
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if pix.n == 1:
        image = np.repeat(image, 3, axis=2)
    elif pix.n == 2:
        image = np.repeat(image[:, :, :1], 3, axis=2)
    elif pix.n >= 4:
        image = image[:, :, :3]
    return image


def extract_pages(pdf_path: str, run_ocr: bool = False,
                  progress_cb: Optional[Callable[[int, int], None]] = None) -> List[Dict]:
    """逐页提取文本，返回 [{'page': 1, 'text': '...'}, ...]（页码从 1 开始）。

    run_ocr=False 时只做快速提取，并把文字过少的页标记为 needs_ocr：
    上传接口据此判断要不要把 OCR 放到后台，避免请求卡住。
    run_ocr=True 时对标记页做 OCR；单页识别失败会继续处理其它页。
    """
    pages: List[Dict] = []
    errors: List[str] = []
    try:
        with _fitz.open(pdf_path) as doc:
            total = doc.page_count
            for number, page in enumerate(doc, start=1):
                text = page.get_text("text") or ""
                structure = (structured.extract_page(page, text) if text.strip()
                             else {"text": "", "tables": [], "captions": [], "formulas": []})
                item = {"page": number, "text": structure["text"]}
                for key in ("tables", "captions", "formulas"):
                    if structure[key]:
                        item[key] = structure[key]
                if _page_needs_ocr(page, text):
                    item["needs_ocr"] = True
                    if run_ocr:
                        if config.OCR_MAX_PAGES and number > config.OCR_MAX_PAGES:
                            item["ocr_skipped"] = True
                        else:
                            try:
                                recognized = ocr.recognize(_render_page_for_ocr(page))
                            except ocr.OCRUnavailable:
                                raise
                            except Exception as exc:  # noqa: BLE001 单页失败不拖垮整篇
                                errors.append(f"第 {number} 页：{exc}")
                            else:
                                item["ocr"] = True
                                if len(recognized.strip()) > len(text.strip()):
                                    annotated = structured.annotate_text(recognized)
                                    item["text"] = annotated["text"]
                                    if annotated["captions"]:
                                        item["captions"] = annotated["captions"]
                                    if annotated["formulas"]:
                                        item["formulas"] = annotated["formulas"]
                if progress_cb:
                    progress_cb(number, total)
                pages.append(item)
    except ocr.OCRUnavailable as exc:
        raise PdfExtractError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise PdfExtractError(f"PDF 解析失败：{exc}") from exc

    if not pages:
        raise PdfExtractError("PDF 中没有页面")
    total_chars = sum(len(p["text"].strip()) for p in pages)
    if total_chars < 40:
        if run_ocr:
            detail = f"（{'；'.join(errors[:3])}）" if errors else ""
            raise PdfExtractError("OCR 完成，但仍没有识别到有效文字。" + detail)
        if not any(p.get("needs_ocr") for p in pages):
            raise PdfExtractError(
                "这份 PDF 提取不到有效文字，可能是扫描件（图片型 PDF）。"
                "请先用 OCR 转成可复制文字的 PDF。"
            )
    return pages


# ------------------------------------------------------------------ 其它格式
class UnsupportedType(RuntimeError):
    pass


def detect_type(name_or_path: str) -> str:
    """返回 'pdf' / 'text' / 'docx'。

    优先看扩展名；如果后缀丢了（历史数据里可能出现没有后缀的文件），
    再靠文件头内容兜底判断。
    """
    text = str(name_or_path)
    suffix = text.rsplit(".", 1)[-1].lower() if "." in text.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] else ""
    if suffix == "pdf":
        return "pdf"
    if suffix in config.DOCX_EXTENSIONS:
        return "docx"
    if suffix in config.TEXT_EXTENSIONS:
        return "text"

    path = Path(text)
    if path.is_file():
        try:
            with open(path, "rb") as handle:
                head = handle.read(8)
        except OSError:
            head = b""
        if head.startswith(b"%PDF"):
            return "pdf"
        if head.startswith(b"PK\x03\x04"):  # docx 本质是个 zip
            return "docx"
        if head:
            return "text"

    raise UnsupportedType(f"识别不出文件格式：{Path(text).name}")


def read_text_file(path: str) -> str:
    """读纯文本，自动猜编码：UTF-8 / UTF-16 / GB18030（兼容 GBK、GB2312）。"""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _items_from_text(text: str, is_markdown: bool) -> List[tuple]:
    """拆成 [('heading'|'body', 文本)]，空行分段；Markdown 的 # 标题单独识别。"""
    items: List[tuple] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            if is_markdown and re.match(r"^#{1,6}\s+\S", line):
                items.append(("heading", re.sub(r"^#{1,6}\s+", "", line)[:60]))
            elif not is_markdown and _detect_heading(line):
                items.append(("heading", line[:60]))
            else:
                items.append(("body", line))
    return items


def _items_from_docx(path: str) -> List[tuple]:
    """用 python-docx 读段落，能拿到标题样式，章节识别比纯文本更准。"""
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedType("读取 .docx 需要 python-docx（本机已安装）") from exc

    document = Document(path)
    items: List[tuple] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = ""
        try:
            style = (paragraph.style.name or "") if paragraph.style is not None else ""
        except Exception:  # noqa: BLE001
            style = ""
        is_heading = style.lower().startswith("heading") or style.startswith("标题") \
            or bool(_detect_heading(text))
        items.append(("heading" if is_heading else "body", text[:60] if is_heading else text))
    # 表格里的文字也别丢；按行保留，让摘要/问答能看出表格结构
    for table in document.tables:
        rows = []
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            items.append(("body", "【表格】\n" + "\n".join(rows)))
    return items


def _build_units(items: Sequence[tuple], target: int) -> List[Dict]:
    """把段落合成编号单元，每个单元带一个"单元号"和它开头的所属章节。"""
    units: List[Dict] = []
    buffer: List[str] = []
    size = 0
    section = ""
    buffer_section = ""
    has_body = False

    def flush() -> None:
        nonlocal buffer, size, buffer_section, has_body
        text = "\n".join(buffer).strip()
        if text:
            units.append({"page": len(units) + 1, "section": buffer_section, "text": text})
        buffer, size, buffer_section, has_body = [], 0, "", False

    for kind, text in items:
        if kind == "heading":
            # 只有已经攒到正文才切段：连续标题（比如文档大标题 + 一级标题）
            # 如果各自单独成段就会变成超短块，容易被当成噪声丢掉。
            if has_body:
                flush()
            section = text
            if not has_body:
                buffer_section = section
            buffer.append(text)
            size += len(text)
            continue

        if size + len(text) > target and buffer:
            flush()
            buffer_section = section
        elif not buffer:
            buffer_section = section
        buffer.append(text)
        size += len(text)
        has_body = True

    flush()
    return units


def extract_units(path: str, run_ocr: bool = False,
                  progress_cb: Optional[Callable[[int, int], None]] = None) -> tuple:
    """统一入口：返回 (单元列表, 出处的单位名)。

    PDF 返回真实页码、单位名"页"；txt/md/docx 没有页的概念，
    按段落合成编号单元、单位名"段"，这样"点出处跳原文"依然成立。
    run_ocr=True 时，扫描页会先 OCR；默认只标记 needs_ocr。
    """
    kind = detect_type(path)
    if kind == "pdf":
        return extract_pages(path, run_ocr=run_ocr,
                             progress_cb=progress_cb), "页", "pdf"

    if kind == "docx":
        items = _items_from_docx(path)
    else:
        suffix = Path(path).suffix.lower()
        content = read_text_file(path)
        # 后缀丢了但内容明显是 Markdown 的，也按 Markdown 处理（标题能识别出来）
        is_markdown = suffix in {".md", ".markdown"} or bool(re.match(r"\s*#{1,6}\s+\S", content))
        items = _items_from_text(content, is_markdown)

    if not items:
        raise PdfExtractError("这份文件里没有可提取的文字")

    units = _build_units(items, config.TEXT_UNIT_CHARS)
    if not units:
        raise PdfExtractError("这份文件里没有可用的段落")
    return units, "段", kind


# ------------------------------------------------------------------ 章节识别
_HEADING_PATTERNS = [
    r"^第\s*[一二三四五六七八九十百零\d]+\s*[章节篇部]",
    # 章节号必须是 1~3 位数字，且后面要跟空白或顿点。
    # 原来写成 ^\d+(?:\.\d+){0,3}\s*[、.．]?\s*\S 太宽松，
    # 会把「15295908323 hongsheng@…」这种手机号行误判成章节标题。
    r"^\d{1,3}(?:\.\d{1,3}){0,3}\s*[、.．]?\s+\S",
    r"^(?:abstract|introduction|related work|background|method|methods|methodology|"
    r"experiment|experiments|results?|discussion|conclusions?|references|acknowledg\w*)\b",
    r"^(?:摘要|关键词|引言|绪论|相关工作|研究背景|研究方法|方法|实验|结果|讨论|结论|参考文献|致谢)\s*[:：]?\s*$",
]
_HEADING_RE = [re.compile(p, re.IGNORECASE) for p in _HEADING_PATTERNS]
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+(?=[A-Z])|\n+")


def _detect_heading(line: str) -> Optional[str]:
    s = line.strip()
    if not s or len(s) > 60:
        return None
    for pattern in _HEADING_RE:
        if pattern.match(s):
            return s[:60]
    return None


def _split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
    return parts


def _hard_split(sentence: str, limit: int) -> List[str]:
    return [sentence[i:i + limit] for i in range(0, len(sentence), limit)]


def chunk_pages(pages: Sequence[Dict]) -> List[Dict]:
    """把每页文本切成 700 字左右的块，保留页码与所属章节。"""
    chunks: List[Dict] = []
    for page in pages:
        page_no = page["page"]
        text = page["text"]
        if not text.strip():
            continue

        # txt/md/docx 的单元在合成时已经知道所属章节，直接用；
        # PDF 没有这个信息，才回退到按标题正则猜。
        section: Optional[str] = page.get("section") or None
        if not section:
            for line in text.split("\n"):
                heading = _detect_heading(line)
                if heading:
                    section = heading
                    break

        sentences: List[str] = []
        for sentence in _split_sentences(text):
            if len(sentence) > config.CHUNK_MAX_CHARS:
                sentences.extend(_hard_split(sentence, config.CHUNK_TARGET_CHARS))
            else:
                sentences.append(sentence)

        buffer: List[str] = []
        size = 0
        only_overlap = False

        def cut() -> None:
            """切一块出去，并保留一句重叠，避免答案正好被切在边界上。"""
            nonlocal buffer, size, only_overlap
            chunks.append(_make_chunk(len(chunks), page_no, section, buffer))
            overlap = buffer[-config.CHUNK_OVERLAP_SENTENCES:] if config.CHUNK_OVERLAP_SENTENCES else []
            buffer = list(overlap)
            size = sum(len(s) for s in buffer)
            only_overlap = True

        for sentence in sentences:
            if size + len(sentence) > config.CHUNK_MAX_CHARS and buffer and not only_overlap:
                cut()
            buffer.append(sentence)
            size += len(sentence)
            only_overlap = False
            if size >= config.CHUNK_TARGET_CHARS:
                cut()

        # 结尾剩余：只有重叠句就丢弃（内容已在上一块里）；否则再短也要留下，别丢内容。
        # 原来这里按"至少 60 字"过滤，会把 docx 的小标题、表格这类短内容整段丢掉。
        if buffer and not only_overlap:
            chunks.append(_make_chunk(len(chunks), page_no, section, buffer))

        for table in page.get("tables") or []:
            chunks.extend(_table_chunks(len(chunks), page_no, section, table))

    return chunks


def _make_chunk(index: int, page: int, section: Optional[str], buffer: Sequence[str],
                kind: str = "text", caption: str = "") -> Dict:
    chunk = {
        "i": index,
        "page": page,
        "section": section or "",
        "text": " ".join(buffer).strip(),
    }
    if kind != "text":
        chunk["kind"] = kind
    if caption:
        chunk["caption"] = caption
    return chunk


def _table_chunks(start_index: int, page: int, section: Optional[str],
                  table: Dict) -> List[Dict]:
    """把一张表按行写成独立块；超长时换块但不在行中间切开。"""
    rows = table.get("rows") or []
    if not rows:
        return []
    caption = (table.get("caption") or "").strip()
    n_cols = int(table.get("n_cols") or max(len(row) for row in rows))
    header = ("【表格】" + caption) if caption else "【表格】"
    header += f"（{len(rows)} 行 {n_cols} 列）"

    chunks: List[Dict] = []
    lines: List[str] = []
    size = len(header)

    def flush() -> None:
        nonlocal lines, size
        if not lines:
            return
        text = header + "\n" + "\n".join(lines)
        chunks.append(_make_chunk(start_index + len(chunks), page, section,
                                  [text], kind="table", caption=caption))
        lines, size = [], len(header)

    for row in rows:
        line = " | ".join(row).strip()
        if not line:
            continue
        if len(line) > config.CHUNK_MAX_CHARS:
            flush()
            for part in _hard_split(line, config.CHUNK_TARGET_CHARS):
                lines.append(part)
                flush()
            continue
        if size + len(line) > config.CHUNK_MAX_CHARS and lines:
            flush()
        lines.append(line)
        size += len(line) + 1
    flush()
    return chunks


# ------------------------------------------------------------------ 中文分词（纯 Python，无需 jieba）
_ASCII_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> List[str]:
    """英文按单词、中文按二元字组切分。中文学术检索里 bigram 效果接近分词。"""
    lowered = text.lower()
    tokens = _ASCII_TOKEN.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class BM25:
    """Okapi BM25，纯 Python 实现，不引入新依赖。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.doc_freqs: List[Counter] = []
        self.doc_len: np.ndarray = np.zeros(0)
        self.avg_len = 0.0
        self.idf: Dict[str, float] = {}

    def fit(self, docs: Sequence[str]) -> None:
        self.doc_freqs = [Counter(tokenize(d)) for d in docs]
        self.doc_len = np.array([sum(c.values()) for c in self.doc_freqs], dtype=np.float64)
        self.avg_len = float(self.doc_len.mean()) if len(self.doc_len) else 0.0
        df: Counter = Counter()
        for counter in self.doc_freqs:
            df.update(counter.keys())
        n = len(self.doc_freqs)
        self.idf = {
            term: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def scores(self, query: str) -> np.ndarray:
        result = np.zeros(len(self.doc_freqs), dtype=np.float64)
        if not len(self.doc_freqs) or not self.avg_len:
            return result
        for term in set(tokenize(query)):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for idx, counter in enumerate(self.doc_freqs):
                tf = counter.get(term, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_len[idx] / self.avg_len)
                result[idx] += idf * tf * (self.k1 + 1) / denom
        return result


# ------------------------------------------------------------------ 混合索引
def _minmax(values: np.ndarray) -> np.ndarray:
    if not len(values):
        return values
    lo, hi = float(values.min()), float(values.max())
    if hi - lo < 1e-9:
        return np.zeros_like(values)
    return (values - lo) / (hi - lo)


class IndexIncompatible(RuntimeError):
    pass


# faiss 的 C++ 层用窄字符 API 打开文件，Windows 上会把 UTF-8 路径按 ANSI 解码，
# 非 ASCII 路径（如 E:\大创\...）直接打不开。读写时先经过 ASCII 临时目录中转。
def _write_faiss(index, target: Path) -> None:
    import faiss

    target = Path(target)
    if str(target).isascii():
        faiss.write_index(index, str(target))
        return
    handle, temp_name = tempfile.mkstemp(suffix=".faiss")
    os.close(handle)
    try:
        faiss.write_index(index, temp_name)
        shutil.copyfile(temp_name, str(target))
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def _read_faiss(path: Path):
    import faiss

    path = Path(path)
    if str(path).isascii():
        return faiss.read_index(str(path))
    handle, temp_name = tempfile.mkstemp(suffix=".faiss")
    os.close(handle)
    try:
        shutil.copyfile(str(path), temp_name)
        return faiss.read_index(temp_name)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


class DocIndex:
    """一份文档的检索索引：faiss 向量索引 + BM25 关键词索引。"""

    def __init__(self, chunks: List[Dict], faiss_index=None, dim: Optional[int] = None):
        self.chunks = chunks
        self.faiss_index = faiss_index
        self.dim = dim
        self.bm25 = BM25()
        self.bm25.fit([c["text"] for c in chunks])

    # -------------------------------------------------- 构建
    @classmethod
    def build(
        cls,
        chunks: List[Dict],
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> "DocIndex":
        service.ensure_loaded()
        import faiss

        vectors = service.encode_documents([c["text"] for c in chunks], progress_cb=progress_cb)
        index = faiss.IndexFlatIP(int(vectors.shape[1]))  # 向量已归一化，内积 = 余弦相似度
        index.add(vectors)
        return cls(chunks, index, int(vectors.shape[1]))

    # -------------------------------------------------- 持久化
    def save(self, directory, doc_id: str) -> None:
        if self.faiss_index is not None:
            _write_faiss(self.faiss_index, Path(directory) / f"{doc_id}_index.faiss")

    @classmethod
    def load(cls, directory, doc_id: str, chunks: List[Dict]) -> "DocIndex":
        path = Path(directory) / f"{doc_id}_index.faiss"
        if not path.exists():
            raise IndexIncompatible("索引文件不存在")
        index = _read_faiss(path)
        expected = service.dim or index.d
        if index.d != expected:
            raise IndexIncompatible(
                f"索引维度 {index.d} 与当前模型维度 {expected} 不一致，需要重建"
            )
        return cls(chunks, index, index.d)

    @classmethod
    def load_or_build(cls, directory, doc_id: str, chunks: List[Dict],
                      progress_cb: Optional[Callable[[int, int], None]] = None) -> "DocIndex":
        try:
            return cls.load(directory, doc_id, chunks)
        except (IndexIncompatible, Exception) as exc:  # noqa: BLE001
            print(f"[索引] {doc_id} 重建索引：{exc}")
            index = cls.build(chunks, progress_cb=progress_cb)
            index.save(directory, doc_id)
            return index

    # -------------------------------------------------- 检索
    def search(self, query: str, top_k: int = None, candidates: int = None) -> List[Dict]:
        top_k = top_k or config.RETRIEVE_TOP_K
        candidates = candidates or config.RETRIEVE_CANDIDATES
        if not self.chunks:
            return []

        vector_scores = np.zeros(len(self.chunks), dtype=np.float64)
        if self.faiss_index is not None:
            query_vector = service.encode_query(query).astype(np.float32)
            _, indices = self.faiss_index.search(query_vector, min(candidates, len(self.chunks)))
            for rank, idx in enumerate(indices[0]):
                if idx >= 0:
                    vector_scores[idx] = 1.0 / (rank + 1)  # 排名倒数，避免余弦值区间过窄

        keyword_scores = self.bm25.scores(query)

        fused = (
            config.HYBRID_VECTOR_WEIGHT * _minmax(vector_scores)
            + config.HYBRID_BM25_WEIGHT * _minmax(keyword_scores)
        )

        pool = int(min(len(self.chunks), max(candidates, top_k * 3)))
        order = np.argsort(-fused)[:pool]

        # 词面重排：查询词在候选块里覆盖得越全，越可能是真正答案所在
        query_terms = set(tokenize(query))
        reranked: List[Tuple[float, int]] = []
        for idx in order:
            coverage = 0.0
            if query_terms:
                present = query_terms & set(tokenize(self.chunks[idx]["text"]))
                coverage = len(present) / len(query_terms)
            score = (1 - config.RERANK_WEIGHT) * float(fused[idx]) + config.RERANK_WEIGHT * coverage
            reranked.append((score, int(idx)))

        reranked.sort(key=lambda item: -item[0])

        results: List[Dict] = []
        seen: List[str] = []
        for score, idx in reranked:
            chunk = self.chunks[idx]
            if any(_similar(chunk["text"], s) for s in seen):
                continue
            seen.append(chunk["text"])
            results.append({
                "i": chunk["i"],
                "page": chunk["page"],
                "section": chunk.get("section", ""),
                "kind": chunk.get("kind", "text"),
                "caption": chunk.get("caption", ""),
                "text": chunk["text"],
                "score": round(score, 4),
            })
            if len(results) >= top_k:
                break
        return results


def page_references(text: str, unit: str = "页") -> List[int]:
    """从问题里解析明确的页码/段号，如「第244页」「244页」「pages 244-246」。

    PDF 的 unit 是「页」，txt/md/docx 是「段」。只把与当前文献单位一致的
    写法当作出处定位，避免把「第3段」误当成 PDF 的第 3 页。
    最多返回 12 个编号，防止「第1-257页」这类输入把整本书塞进上下文。
    """
    text = text or ""
    units = r"页|面" if unit == "页" else r"段"
    found = set()

    def add_range(start: int, end: int) -> None:
        if start > end:
            start, end = end, start
        for number in range(start, min(end, start + 19) + 1):
            found.add(number)

    for match in re.finditer(
            rf"(?:第\s*)?(\d{{1,4}})\s*(?:{units})?\s*[-–—~～至到]\s*"
            rf"(?:第\s*)?(\d{{1,4}})\s*(?:{units})", text):
        add_range(int(match.group(1)), int(match.group(2)))
    for match in re.finditer(rf"(?:第\s*)?(\d{{1,4}})\s*(?:{units})", text):
        found.add(int(match.group(1)))
    for match in re.finditer(
            r"(?:pages?|pp?\.)\s*(\d{1,4})\s*(?:[-–—~～至到]|to)\s*(\d{1,4})",
            text, re.IGNORECASE):
        add_range(int(match.group(1)), int(match.group(2)))
    for match in re.finditer(r"(?:pages?|pp?\.)\s*(\d{1,4})",
                             text, re.IGNORECASE):
        found.add(int(match.group(1)))

    return sorted(number for number in found if number >= 1)[:12]


def page_passages(chunks: Sequence[Dict], pages: Sequence[int]) -> List[Dict]:
    """取出指定页码/段号的全部片段，保持原文顺序。"""
    wanted = {int(page) for page in pages}
    results: List[Dict] = []
    for chunk in chunks:
        try:
            page = int(chunk.get("page", 0))
        except (TypeError, ValueError):
            continue
        if page not in wanted:
            continue
        results.append({
            "i": chunk.get("i", 0),
            "page": page,
            "section": chunk.get("section", ""),
            "kind": chunk.get("kind", "text"),
            "caption": chunk.get("caption", ""),
            "text": chunk.get("text", ""),
            # 页码定位是精确命中，不需要再和语义检索的分数比较
            "score": 1.0,
        })
    return results


def _similar(a: str, b: str) -> bool:
    """粗糙去重：前 120 字高度重合就认为重复。"""
    head_a, head_b = a[:120], b[:120]
    if not head_a or not head_b:
        return False
    shorter, longer = (head_a, head_b) if len(head_a) <= len(head_b) else (head_b, head_a)
    return longer.startswith(shorter[: max(20, len(shorter) // 2)])


def build_context(passages: Sequence[Dict], max_chars: int = 6000, unit: str = "页") -> str:
    """把检索结果拼成带出处标记的上下文，供大模型引用。

    unit 是出处的单位名：PDF 是"页"，txt/md/docx 是"段"。

    这里刻意**不写片段序号**：以前每条开头是「片段1 | 第1页」，
    同时出现两个数字，文档只有一页时页码全是 1，模型就会把片段序号当页码，
    引用出 p.1 / p.2 / p.3 这种不存在的出处。
    """
    blocks: List[str] = []
    used = 0
    for p in passages:
        header = f"【出处：第{p['page']}{unit}"
        if p.get("section"):
            header += f" | {p['section']}"
        if p.get("kind") == "table":
            header += " | 表格"
        header += "】"
        block = f"{header}\n{p['text']}"
        if used + len(block) > max_chars and blocks:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


_CITE_TOKEN_RE = re.compile(r"\[\[\s*p\.?\s*(\d+)\s*\]\]")
_SOURCE_TOKEN_RE = re.compile(r"\[\[\s*s\.?\s*(\d+)\s*\]\]")


def citation_pages(text: str) -> List[int]:
    """从模型输出里抽出 [[p.N]] 标记，用于前端渲染跳转按钮。"""
    return sorted({int(m) for m in _CITE_TOKEN_RE.findall(text)})


def allowed_pages(passages: Sequence[Dict]) -> List[int]:
    """本次检索真正提供的出处编号，也是模型唯一允许引用的编号。"""
    pages = set()
    for passage in passages:
        try:
            pages.add(int(passage["page"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(pages)


def sanitize_citations(text: str, allowed: Sequence[int]) -> Tuple[str, List[int]]:
    """删掉不在检索片段里的出处标记，返回（清理后正文, 被删编号）。

    提示词里已经给了白名单，但模型仍可能编造页码；这里做最后一道校验，
    保证前端能跳转的每个编号都真实存在于本次检索结果。
    """
    allowed_set = {int(p) for p in allowed}
    removed: List[int] = []

    def replace(match: re.Match) -> str:
        number = int(match.group(1))
        if number in allowed_set:
            return match.group(0)
        removed.append(number)
        return ""

    cleaned = _CITE_TOKEN_RE.sub(replace, text or "")
    if removed:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([，。；：！？、）】」』])", r"\1", cleaned)
        cleaned = re.sub(r"（[ \t]*）", "", cleaned)
        cleaned = re.sub(r"[ \t]+$", "", cleaned, flags=re.M)
    return cleaned, sorted(set(removed))


def source_citations(text: str) -> List[int]:
    """跨文献回答里的来源编号 [[s.N]]。"""
    return sorted({int(m) for m in _SOURCE_TOKEN_RE.findall(text)})


def sanitize_source_citations(text: str,
                              allowed: Sequence[int]) -> Tuple[str, List[int]]:
    """跨文献引用校验：来源编号必须在本次材料里，单页标记 p.N 也一并清掉。

    跨文献场景下一个页码可能对应多篇文献，p.N 本身有歧义，所以只接受 [[s.N]]。
    """
    allowed_set = {int(n) for n in allowed}
    removed: List[int] = []

    def replace_source(match: re.Match) -> str:
        number = int(match.group(1))
        if number in allowed_set:
            return match.group(0)
        removed.append(number)
        return ""

    def replace_page(match: re.Match) -> str:
        removed.append(int(match.group(1)))
        return ""

    cleaned = _SOURCE_TOKEN_RE.sub(replace_source, text or "")
    cleaned = _CITE_TOKEN_RE.sub(replace_page, cleaned)
    if removed:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([，。；：！？、）】」』])", r"\1", cleaned)
        cleaned = re.sub(r"（[ \t]*）", "", cleaned)
        cleaned = re.sub(r"[ \t]+$", "", cleaned, flags=re.M)
    return cleaned, sorted(set(removed))

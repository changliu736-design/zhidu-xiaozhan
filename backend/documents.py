# -*- coding: utf-8 -*-
"""文档存储 + 后台索引任务管理。

为什么要后台建索引：Qwen3-Embedding 在纯 CPU 上大约每块 6~19 秒，
一篇 100 多块的论文要十几分钟。同步等待会让浏览器请求超时、界面假死。
所以上传立刻返回，索引在后台线程里跑，前端轮询进度显示进度条。
"""
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import config
from retrieval import (DocIndex, PdfExtractError, chunk_pages, extract_pages,
                       extract_units)

SCHEMA_VERSION = 2

_jobs: Dict[str, Dict] = {}
_jobs_lock = threading.Lock()
# 已删除的文档：索引线程可能在删除后才跑完，不能让它把 _info.json 又写回来
_deleted_ids: set = set()
# 同一时刻只跑一个索引任务：CPU 推理本来就慢，并发只会互相抢核心、全都变慢
_index_slot = threading.Lock()

_SUMMARY_SECTION = re.compile(
    r"摘要|abstract|引言|绪论|introduction|背景|background|结论|conclusion|"
    r"讨论|discussion|结果|result|总结",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ 路径
def user_dir(user_id: str) -> Path:
    path = config.UPLOAD_ROOT / user_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def info_path(directory: Path, doc_id: str) -> Path:
    return directory / f"{doc_id}_info.json"


def index_path(directory: Path, doc_id: str) -> Path:
    return directory / f"{doc_id}_index.faiss"


def embeddings_path(directory: Path, doc_id: str) -> Path:
    return directory / f"{doc_id}_embeddings.npy"


# ------------------------------------------------------------------ 读写
def load_info(directory: Path, doc_id: str) -> Optional[Dict]:
    path = info_path(directory, doc_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[文档] 读取 {doc_id} 信息失败：{exc}")
        return None


def save_info(directory: Path, doc_id: str, info: Dict) -> None:
    path = info_path(directory, doc_id)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(info, handle, ensure_ascii=False)
    tmp.replace(path)


def list_documents(directory: Path) -> List[Dict]:
    if not directory.is_dir():
        return []
    documents: List[Dict] = []
    for path in sorted(directory.glob("*_info.json")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                info = json.load(handle)
        except (json.JSONDecodeError, OSError):
            continue
        doc_id = info.get("id") or path.name[: -len("_info.json")]
        info["id"] = doc_id
        documents.append(summary_of(info))
    documents.sort(key=lambda d: d.get("upload_time", ""), reverse=True)
    return documents


def summary_of(info: Dict) -> Dict:
    chunks = info.get("chunks") or info.get("text_chunks") or []
    filename = info.get("original_filename") or info.get("filename", "未命名")
    return {
        "id": info.get("id", ""),
        "filename": filename,
        "display_name": info.get("display_name") or filename,
        "category": info.get("category", ""),
        "tags": info.get("tags") or [],
        "progress": info.get("progress") or {},
        "last_opened_at": info.get("last_opened_at", ""),
        "chunk_count": info.get("chunk_count", len(chunks)),
        "page_count": info.get("page_count", 0),
        "upload_time": info.get("upload_time", ""),
        "index_status": index_status_of(info),
        "embedding_model": info.get("embedding_model", ""),
        "schema_version": info.get("schema_version", 1),
        "source_type": info.get("source_type", "pdf"),
        "unit": info.get("unit", "页"),
        "ocr_pages": info.get("ocr_pages", 0),
    }


def index_status_of(info: Dict) -> str:
    if info.get("index_status"):
        return info["index_status"]
    return "ready" if info.get("has_index") else "pending"


def source_path_of(directory: Path, info: Dict) -> Optional[Path]:
    """找出这篇文档对应的原始文件（pdf / txt / md / docx），兼容新旧两种记录方式。"""
    stored = info.get("stored_filename")
    if stored:
        candidate = directory / stored
        if candidate.exists():
            return candidate

    # 老记录里的 filepath 可能还指向升级前的旧位置，只取文件名到用户目录里找
    legacy = str(info.get("filepath") or "").replace("\\", "/")
    if legacy:
        base = Path(legacy).name
        if base:
            candidate = directory / base
            if candidate.exists():
                return candidate
        candidate = Path(config.BASE_DIR) / legacy
        if candidate.exists():
            return candidate

    doc_id = info.get("id", "")
    if doc_id:
        for suffix in sorted(config.ALLOWED_EXTENSIONS):
            matches = sorted(directory.glob(f"{doc_id}_*.{suffix}"))
            if matches:
                return matches[0]

    # 兜底：老实现是用 secure_filename(原文件名) 存盘的，还原一下
    original = info.get("original_filename") or ""
    if original:
        from werkzeug.utils import secure_filename

        secured = secure_filename(original)
        if secured:
            candidate = directory / secured
            if candidate.exists():
                return candidate
        stem = Path(original).stem
        if stem:
            for suffix in sorted(config.ALLOWED_EXTENSIONS):
                for candidate in sorted(directory.glob(f"*.{suffix}")):
                    if stem in candidate.name or candidate.name.endswith(stem):
                        return candidate
    return None


# ------------------------------------------------------------------ 索引任务
def job_state(doc_id: str) -> Dict:
    with _jobs_lock:
        return dict(_jobs.get(doc_id, {"status": "idle", "done": 0, "total": 0,
                                       "error": None, "stage": None}))


def _set(doc_id: str, **fields) -> None:
    with _jobs_lock:
        state = _jobs.setdefault(doc_id, {"status": "idle", "done": 0, "total": 0,
                                          "error": None, "stage": None})
        state.update(fields)


def is_running(doc_id: str) -> bool:
    return job_state(doc_id)["status"] == "running"


def mark_deleted(doc_id: str) -> None:
    with _jobs_lock:
        _deleted_ids.add(doc_id)


def is_deleted(doc_id: str) -> bool:
    with _jobs_lock:
        return doc_id in _deleted_ids


def start_index_job(directory: Path, doc_id: str, chunks: List[Dict]) -> None:
    """启动后台线程构建索引；重复调用会被忽略。"""
    if is_deleted(doc_id) or is_running(doc_id):
        return
    _set(doc_id, status="running", stage="index", done=0, total=len(chunks),
         error=None, started=time.time())
    thread = threading.Thread(
        target=_run_index_job, args=(directory, doc_id, chunks), daemon=True,
        name=f"index-{doc_id}",
    )
    thread.start()


def _fail_job(directory: Path, doc_id: str, exc: Exception) -> None:
    if is_deleted(doc_id):
        return
    message = f"{type(exc).__name__}: {exc}"
    _set(doc_id, status="error", stage=None, error=message, finished=time.time())
    info = load_info(directory, doc_id) or {}
    info["index_status"] = "error"
    info["index_error"] = message
    save_info(directory, doc_id, info)
    print(f"[索引] {doc_id} 失败：{message}")


def _build_and_save_index(directory: Path, doc_id: str,
                          chunks: List[Dict], started: float) -> None:
    """构建并保存索引；调用方必须已持有 _index_slot。"""

    def progress(done: int, total: int) -> None:
        _set(doc_id, done=done, total=total, stage="index")

    index = DocIndex.build(chunks, progress_cb=progress)
    if is_deleted(doc_id):
        return
    index.save(directory, doc_id)
    if is_deleted(doc_id):
        index_path(directory, doc_id).unlink(missing_ok=True)
        embeddings_path(directory, doc_id).unlink(missing_ok=True)
        return
    info = load_info(directory, doc_id) or {}
    info["has_index"] = True
    info["index_status"] = "ready"
    info["index_error"] = None
    info["ocr_required"] = False
    save_info(directory, doc_id, info)
    _set(doc_id, status="done", stage=None, error=None, finished=time.time())
    print(f"[索引] {doc_id} 完成，共 {len(chunks)} 块，用时 {time.time() - started:.1f}s")


def _run_index_job(directory: Path, doc_id: str, chunks: List[Dict]) -> None:
    started = time.time()
    with _index_slot:  # 排队执行，避免多个任务同时抢 CPU
        try:
            _build_and_save_index(directory, doc_id, chunks, started)
        except Exception as exc:  # noqa: BLE001
            _fail_job(directory, doc_id, exc)


def start_ocr_index_job(directory: Path, doc_id: str,
                        source: Path, page_count: int) -> None:
    """扫描件专用：先逐页 OCR，再分块建索引。"""
    if is_deleted(doc_id) or is_running(doc_id):
        return
    total = max(1, int(page_count or 0))
    _set(doc_id, status="running", stage="ocr", done=0, total=total,
         error=None, started=time.time())
    thread = threading.Thread(
        target=_run_ocr_index_job, args=(directory, doc_id, source),
        daemon=True, name=f"ocr-{doc_id}",
    )
    thread.start()


def _run_ocr_index_job(directory: Path, doc_id: str, source: Path) -> None:
    started = time.time()

    def progress(done: int, total: int) -> None:
        _set(doc_id, done=done, total=max(total, 1), stage="ocr")

    with _index_slot:
        try:
            pages = extract_pages(str(source), run_ocr=True, progress_cb=progress)
            if is_deleted(doc_id):
                return
            chunks = chunk_pages(pages)
            if not chunks:
                raise PdfExtractError("OCR 完成，但仍没有得到可索引的文字")

            info = load_info(directory, doc_id) or {}
            info.update({
                "chunks": chunks,
                "chunk_count": len(chunks),
                "page_count": len(pages),
                "source_type": "pdf",
                "unit": "页",
                "ocr_required": False,
                "ocr_pages": sum(1 for page in pages if page.get("ocr")),
                "ocr_skipped": sum(1 for page in pages if page.get("ocr_skipped")),
                "index_status": "pending",
                "has_index": False,
            })
            save_info(directory, doc_id, info)
            _set(doc_id, stage="index", done=0, total=len(chunks))
            print(f"[OCR] {doc_id} 识别完成：{info['ocr_pages']} 页，"
                  f"共 {len(chunks)} 块，开始建索引")
            _build_and_save_index(directory, doc_id, chunks, started)
        except Exception as exc:  # noqa: BLE001
            _fail_job(directory, doc_id, exc)


# ------------------------------------------------------------------ 旧版本升级
def rebuild_from_source(directory: Path, doc_id: str, info: Dict) -> Dict:
    """重新解析原始文件，生成带出处编号的新格式。

    老版本只有纯文本块、没有出处信息，这里统一按新格式重建。
    """
    source = source_path_of(directory, info)
    if source is None:
        raise FileNotFoundError("找不到原始文件，无法重建索引")
    units, unit, source_type = extract_units(str(source))
    chunks = chunk_pages(units)
    needs_ocr = source_type == "pdf" and any(p.get("needs_ocr") for p in units)
    info.update({
        "schema_version": SCHEMA_VERSION,
        "chunks": chunks,
        "chunk_count": len(chunks),
        "page_count": len(units),
        "source_type": source_type,
        "unit": unit,
        "embedding_model": config.EMBEDDING_MODEL_NAME,
        "stored_filename": source.name,
        "index_status": "pending",
        "has_index": False,
        "ocr_required": needs_ocr,
    })
    info.pop("text_chunks", None)
    save_info(directory, doc_id, info)
    return info


def needs_rebuild(info: Dict) -> bool:
    # 扫描件在后台 OCR 完成前 chunks 为空，这是正常状态，不能当成"需要重建"
    if info.get("ocr_required") and not info.get("chunks"):
        return False
    return (
        info.get("schema_version", 1) < SCHEMA_VERSION
        or not info.get("chunks")
        or (info.get("embedding_model") and info.get("embedding_model") != config.EMBEDDING_MODEL_NAME)
    )


def ensure_index_started(directory: Path, doc_id: str, info: Dict) -> Dict:
    """确保索引存在或在建：需要重建就先重建分块，再启动后台索引。"""
    if needs_rebuild(info):
        if is_running(doc_id):
            return info
        try:
            info = rebuild_from_source(directory, doc_id, info)
        except Exception as exc:  # noqa: BLE001
            info["index_status"] = "error"
            info["index_error"] = str(exc)
            save_info(directory, doc_id, info)
            return info

    if info.get("ocr_required") and not info.get("chunks"):
        if not is_running(doc_id):
            source = source_path_of(directory, info)
            if source is None:
                info["index_status"] = "error"
                info["index_error"] = "找不到原始文件，无法进行 OCR"
                save_info(directory, doc_id, info)
                return info
            start_ocr_index_job(directory, doc_id, source,
                                int(info.get("page_count") or 0))
        return info

    if index_status_of(info) == "ready" and index_path(directory, doc_id).exists():
        return info
    if not is_running(doc_id):
        start_index_job(directory, doc_id, info.get("chunks", []))
    return info


# ------------------------------------------------------------------ 摘要取材
def select_summary_passages(chunks: List[Dict], max_chars: int = 7000) -> List[Dict]:
    """给"生成摘要"挑片段：优先摘要/引言/结论章节，再补开头和结尾。

    摘要不是检索任务，用向量检索反而不合适 —— 应该按章节重要性取。
    """
    if not chunks:
        return []

    def priority(index: int) -> int:
        chunk = chunks[index]
        if _SUMMARY_SECTION.search(chunk.get("section", "")):
            return 0
        # 表格块通常篇幅短、信息密度高，排在摘要章节之后优先纳入
        if chunk.get("kind") == "table":
            return 1
        if index < 3:
            return 2
        if index >= len(chunks) - 3:
            return 2
        return 3

    ordered = sorted(range(len(chunks)), key=lambda i: (priority(i), i))
    picked: List[int] = []
    used = 0
    for index in ordered:
        size = len(chunks[index]["text"])
        if used + size > max_chars and picked:
            break
        picked.append(index)
        used += size

    return [
        {
            "i": chunks[i]["i"],
            "page": chunks[i]["page"],
            "section": chunks[i].get("section", ""),
            "kind": chunks[i].get("kind", "text"),
            "caption": chunks[i].get("caption", ""),
            "text": chunks[i]["text"],
            "score": 1.0,
        }
        for i in sorted(picked)
    ]

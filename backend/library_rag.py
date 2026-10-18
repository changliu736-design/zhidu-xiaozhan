# -*- coding: utf-8 -*-
"""跨文献检索：在用户整个文献库里召回、全局重排，并为对比/综述挑选材料。

和单篇问答的区别：
  1. 每篇文献先各自召回少量片段，再把这些片段放在一起用 cross-encoder 重排；
  2. 每个片段带文献标题、页码/段号，提示词里统一编号成「来源 N」；
  3. 综述选材要保证覆盖面，不能几篇文献把名额占满，所以按文献分配名额。
"""
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import config
import documents as docs
from reranker import get_reranker
from retrieval import DocIndex

_lock = threading.Lock()
_index_cache: Dict[tuple, DocIndex] = {}
_CACHE_LIMIT = 16

MAX_DOCS = 30            # 一次问答最多检索多少篇文献
PER_DOC_HITS = 4         # 每篇先召回几条候选
MAX_CANDIDATES = 40      # 送进 cross-encoder 的候选上限
MAX_DOCS_IN_REVIEW = 12  # 一次综述最多覆盖多少篇
MAX_SOURCES_IN_REVIEW = 24  # 综述最多给模型多少条材料


def _load_index(directory: Path, doc_id: str, info: Dict) -> DocIndex:
    path = docs.index_path(directory, doc_id)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    key = (str(directory), doc_id, mtime)
    with _lock:
        cached = _index_cache.get(key)
    if cached is not None:
        return cached

    index = DocIndex.load(directory, doc_id, info.get("chunks", []))
    with _lock:
        _index_cache[key] = index
        while len(_index_cache) > _CACHE_LIMIT:
            _index_cache.pop(next(iter(_index_cache)))
    return index


def ready_documents(directory: Path,
                    doc_ids: Optional[Sequence[str]] = None) -> List[tuple]:
    """用户库里已完成索引的文献 [(摘要, info)]，可按 id 限定范围。"""
    selected = set(doc_ids) if doc_ids else None
    items: List[tuple] = []
    for item in docs.list_documents(directory):
        if selected is not None and item["id"] not in selected:
            continue
        info = docs.load_info(directory, item["id"])
        if info is None or docs.index_status_of(info) != "ready":
            continue
        if not docs.index_path(directory, item["id"]).exists():
            continue
        items.append((item, info))
    return items


def _hit_payload(item: Dict, info: Dict, hit: Dict) -> Dict:
    return {
        "doc_id": item["id"],
        "filename": item.get("display_name") or item.get("filename", "未命名"),
        "category": item.get("category", ""),
        "tags": item.get("tags") or [],
        "source_type": item.get("source_type", "pdf"),
        "unit": item.get("unit", "页"),
        "page": int(hit.get("page", 1)),
        "section": hit.get("section", ""),
        "text": hit.get("text", ""),
        "score": float(hit.get("score", 0)),
    }


def documents_used(sources: Sequence[Dict]) -> List[Dict]:
    seen = {}
    for source in sources:
        seen.setdefault(source["doc_id"], {
            "id": source["doc_id"],
            "title": source["filename"],
            "unit": source["unit"],
            "category": source.get("category", ""),
            "tags": source.get("tags") or [],
        })
    return list(seen.values())


def search_library(directory: Path, query: str,
                   doc_ids: Optional[Sequence[str]] = None,
                   top_k: int = 8) -> Dict:
    """全库问答：每篇先召回，再全局神经重排，最后按文献限流保证覆盖面。"""
    papers = ready_documents(directory, doc_ids)[:MAX_DOCS]
    candidates: List[Dict] = []
    for item, info in papers:
        try:
            index = _load_index(directory, item["id"], info)
            hits = index.search(query, top_k=PER_DOC_HITS)
        except Exception as exc:  # noqa: BLE001  单篇索引坏了不影响其它文献
            print(f"[跨文献] {item['id']} 检索失败：{exc}")
            continue
        for hit in hits:
            candidates.append(_hit_payload(item, info, hit))

    candidates.sort(key=lambda p: -p["score"])
    candidates = candidates[:MAX_CANDIDATES]
    if not candidates:
        return {"sources": [], "documents": [], "searched": len(papers)}

    reranked = get_reranker().rerank(query, candidates, len(candidates))
    sources = _limit_per_doc(reranked, top_k, per_doc=2)
    for number, source in enumerate(sources, 1):
        source["n"] = number
    return {"sources": sources,
            "documents": documents_used(sources),
            "searched": len(papers)}


def _limit_per_doc(passages: Sequence[Dict], top_k: int, per_doc: int) -> List[Dict]:
    picked: List[Dict] = []
    overflow: List[Dict] = []
    counts: Dict[str, int] = {}
    for passage in passages:
        doc_id = passage["doc_id"]
        if counts.get(doc_id, 0) >= per_doc:
            overflow.append(passage)
            continue
        counts[doc_id] = counts.get(doc_id, 0) + 1
        picked.append(passage)
        if len(picked) >= top_k:
            return picked
    for passage in overflow:
        if len(picked) >= top_k:
            break
        picked.append(passage)
    return picked


def review_materials(directory: Path, topic: str = "",
                     doc_ids: Optional[Sequence[str]] = None) -> Dict:
    """对比/综述选材：按文献分配名额，保证每篇都有代表片段。"""
    papers = ready_documents(directory, doc_ids)
    if not papers:
        return {"sources": [], "documents": [], "searched": 0}

    topic = (topic or "").strip()
    ranked: List[tuple] = []
    if topic:
        for item, info in papers:
            try:
                index = _load_index(directory, item["id"], info)
                hits = index.search(topic, top_k=PER_DOC_HITS)
            except Exception as exc:  # noqa: BLE001
                print(f"[综述] {item['id']} 检索失败：{exc}")
                continue
            if hits:
                ranked.append((max(h["score"] for h in hits), item, info, hits))
        ranked.sort(key=lambda entry: -entry[0])
        ranked = ranked[:MAX_DOCS_IN_REVIEW]
    else:
        ranked = [(0.0, item, info, []) for item, info in papers[:MAX_DOCS_IN_REVIEW]]

    if not ranked:
        return {"sources": [], "documents": [], "searched": len(papers)}

    per_doc = max(1, min(3, MAX_SOURCES_IN_REVIEW // len(ranked)))
    sources: List[Dict] = []
    for _, item, info, hits in ranked:
        if not hits:
            chunks = docs.select_summary_passages(info.get("chunks", []),
                                                  max_chars=2400)
            hits = [{"page": c["page"], "section": c.get("section", ""),
                     "text": c["text"], "score": c.get("score", 0)}
                    for c in chunks]
        for hit in hits[:per_doc]:
            sources.append(_hit_payload(item, info, hit))
        if len(sources) >= MAX_SOURCES_IN_REVIEW:
            break

    sources = sources[:MAX_SOURCES_IN_REVIEW]
    for number, source in enumerate(sources, 1):
        source["n"] = number
    return {"sources": sources,
            "documents": documents_used(sources),
            "searched": len(papers)}

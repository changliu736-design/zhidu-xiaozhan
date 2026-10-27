# -*- coding: utf-8 -*-
"""智读小站后端服务。

本次升级的三件事：
  1. 检索换成本机已缓存的 Qwen3-Embedding-0.6B，并把模型加载失败从"静默降级"改成显式报错；
  2. 可溯源：分块保留页码/章节，答案强制标注 [[p.页码]]，支持跳转原文；
  3. 登录鉴权 + 多用户文档隔离：每个用户的文献存在 uploads/<用户ID>/ 下，互不可见。
"""
import hashlib
import io
import json
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory, stream_with_context)
from flask_cors import CORS
from werkzeug.utils import secure_filename

import admin
import auth
import cite
import config
import documents as docs
import exports as export_store
import library_rag
import marks as mark_store
import ocr
import quota
from embeddings import EmbeddingUnavailable, service as embedding_service
from llm_integration import LLMUnavailable, get_llm
from reranker import get_reranker
from retrieval import (DocIndex, PdfExtractError, allowed_pages, citation_pages,
                       chunk_pages, extract_units, page_passages,
                       page_references, sanitize_citations,
                       sanitize_source_citations, source_citations)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
config.ensure_dirs()
CORS(app)

PASSAGE_LIMIT = 4


# ==================================================================== 工具
def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def stream_response(generator) -> Response:
    return Response(
        stream_with_context(generator),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


def passages_payload(passages) -> list:
    return [
        {"page": p["page"], "section": p.get("section", ""),
         "text": p["text"][:400], "score": p.get("score", 0)}
        for p in passages[:PASSAGE_LIMIT]
    ]


def user_scope():
    """登录用户 + 他的文档目录。"""
    return request.user, docs.user_dir(request.user["id"])  # type: ignore[attr-defined]


def _quota_guard(directory: Path, kind: str):
    """占一次每日额度；用完了返回 429 响应，否则返回 None 继续执行。"""
    allowed, used, limit = quota.consume(directory, kind)
    if allowed:
        return None
    return jsonify({
        "error": quota.message(kind, limit),
        "quota": {"kind": kind, "used": used, "limit": limit},
    }), 429


@app.route("/api/quota", methods=["GET"])
def quota_status():
    """前端在页头显示「今天还剩几次」，也方便用户自查。"""
    user, directory = user_scope()
    return jsonify({"quota": quota.snapshot(directory)}), 200


# ==================================================================== 页面
@app.route("/")
@app.route("/index.html")
def index():
    return send_from_directory(str(config.ROOT_DIR), "index.html")


@app.route("/admin")
@app.route("/admin.html")
def admin_page():
    return send_from_directory(str(config.ROOT_DIR), "admin.html")


@app.route("/vendor/<path:filename>")
def vendor(filename):
    return send_from_directory(str(config.ROOT_DIR / "vendor"), filename)


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "embedding": embedding_service.status(),
        "reranker": get_reranker().status(),
        "ocr": ocr.status(),
        "llm": get_llm().status(),
        "users": auth.count_users(),
    }), 200


# ==================================================================== 鉴权
@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    is_first = auth.count_users() == 0
    try:
        user = auth.create_user(data.get("username", ""), data.get("password", ""))
    except auth.AuthError as exc:
        return jsonify({"error": str(exc)}), 400

    claimed = _claim_legacy_documents(user) if is_first else 0
    return _issue(user, {"claimed_legacy": claimed})


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    user = auth.authenticate(data.get("username", ""), data.get("password", ""))
    if not user:
        return jsonify({"error": "用户名或密码不正确"}), 401
    if auth.is_banned(user):
        return jsonify({"error": "账号已被封禁，请联系管理员", "code": "banned"}), 403
    return _issue(user)


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    response = jsonify({"success": True})
    response.delete_cookie(config.SESSION_COOKIE)
    return response


@app.route("/api/auth/me")
def me():
    user = auth.current_user()
    if not user:
        return jsonify({"error": "未登录", "code": "unauthorized"}), 401
    if auth.is_banned(user):
        return jsonify({"error": "账号已被封禁", "code": "banned"}), 403
    return jsonify({"user": auth.public_user(user)}), 200


@app.route("/api/auth/password", methods=["POST"])
def change_password():
    """修改密码。会作废此前签发的所有令牌，并给当前设备换发一个新令牌。"""
    user = request.user  # type: ignore[attr-defined]  before_request 已校验登录
    data = request.get_json(silent=True) or {}
    try:
        record = auth.change_password(user, data.get("old_password", ""),
                                      data.get("new_password", ""))
    except auth.AuthError as exc:
        return jsonify({"error": str(exc)}), 400
    print(f"[账号] {user['username']} 修改了密码，旧令牌已全部失效")
    return _issue(record, {"message": "密码已修改，其它设备上的登录状态已失效"})


def _issue(user, extra=None):
    token = auth.issue_token(user)
    payload = {"user": auth.public_user(user), "token": token}
    if extra:
        payload.update(extra)
    return auth.attach_session(jsonify(payload), token), 200


def _claim_legacy_documents(user) -> int:
    """第一个注册的用户继承 uploads/ 根目录下的老文档（升级前上传的）。"""
    directory = docs.user_dir(user["id"])
    moved = 0
    for path in list(config.UPLOAD_ROOT.glob("*")):
        if path.is_dir() and path.name != user["id"]:
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            continue
        target = directory / path.name
        if target.exists():
            continue
        try:
            path.replace(target)
            moved += 1
        except OSError as exc:
            print(f"[迁移] {path.name} 移动失败：{exc}")

    # 旧索引是用 384 维英文模型建的，和 1024 维的 Qwen3 不兼容，删掉以便重建
    for path in directory.glob("*_index.faiss"):
        path.unlink(missing_ok=True)
    for path in directory.glob("*_info.json"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                info = json.load(handle)
            # 老记录里存的是升级前的相对路径，文件已经搬进用户目录了，
            # 这里把新的文件名记下来，否则之后重建索引会找不到原始 PDF。
            legacy = str(info.get("filepath") or "").replace("\\", "/")
            base = os.path.basename(legacy)
            if base and (directory / base).exists():
                info["stored_filename"] = base
            info["has_index"] = False
            info["index_status"] = "pending"
            docs.save_info(directory, info.get("id", path.name.split("_")[0]), info)
        except (json.JSONDecodeError, OSError):
            continue
    if moved:
        print(f"[迁移] 已把 {moved} 个老文档交给用户 {user['username']}")
    return moved


# ==================================================================== 文档列表
@app.route("/api/documents", methods=["GET"])
def list_documents():
    user, directory = user_scope()
    items = docs.list_documents(directory)
    for item in items:
        info = docs.load_info(directory, item["id"]) or {}
        # 已明确失败的任务不要每次轮询都重启：确定性错误（比如文件损坏）
        # 会变成无限重试，CPU 一直空转。
        if docs.index_status_of(info) != "error" and (
                docs.needs_rebuild(info) or docs.index_status_of(info) != "ready"):
            docs.ensure_index_started(directory, item["id"], info)
        state = docs.job_state(item["id"])
        if state["status"] == "running":
            item["index_status"] = "running"
            item["index_done"] = state["done"]
            item["index_total"] = state["total"]
            item["index_stage"] = state.get("stage")
        else:
            item["index_status"] = docs.index_status_of(info)
        if info.get("index_error"):
            item["index_error"] = info["index_error"]
    return jsonify(items), 200


@app.route("/api/library/search", methods=["GET"])
def library_search():
    """跨文献检索：在用户所有已建好索引的文献里搜同一句话。"""
    user, directory = user_scope()
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "没有收到检索词"}), 400

    results = []
    searched = 0
    for item in docs.list_documents(directory):
        info = docs.load_info(directory, item["id"])
        if info is None or docs.index_status_of(info) != "ready":
            continue
        if not docs.index_path(directory, item["id"]).exists():
            continue
        try:
            index = DocIndex.load(directory, item["id"], info.get("chunks", []))
            hits = index.search(query, top_k=3)
        except Exception as exc:  # noqa: BLE001  单篇索引坏了不影响其它文献
            print(f"[跨文献] {item['id']} 检索失败：{exc}")
            continue
        searched += 1
        for hit in hits:
            results.append({
                "doc_id": item["id"],
                "filename": item["filename"],
                "source_type": item.get("source_type", "pdf"),
                "unit": item.get("unit", "页"),
                "page": hit["page"],
                "section": hit.get("section", ""),
                "text": hit["text"][:300],
                "score": hit.get("score", 0),
            })

    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return jsonify({"query": query, "searched": searched,
                    "results": results[:20]}), 200


# ---------------------------------------------------------------- 跨文献问答 / 综述
def _clean_doc_ids(raw):
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        return None
    ids = []
    for value in raw:
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                ids.append(text[:64])
    return ids[:50] or None


def library_sources_payload(sources):
    return [
        {"n": s["n"], "doc_id": s["doc_id"], "filename": s["filename"],
         "page": s["page"], "unit": s.get("unit", "页"),
         "source_type": s.get("source_type", "pdf"),
         "section": s.get("section", ""), "text": s["text"][:300],
         "score": s.get("score", 0)}
        for s in sources
    ]


def _library_request():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
    else:
        data = {"question": request.args.get("q"),
                "topic": request.args.get("topic"),
                "history": request.args.get("history"),
                "doc_ids": request.args.get("doc_ids"),
                "mode": request.args.get("mode")}
    return data


@app.route("/api/library/ask/stream", methods=["GET", "POST"])
def library_ask_stream():
    """全库问答：检索用户所有文献，综合回答并标注来源。"""
    user, directory = user_scope()
    data = _library_request()
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "没有收到问题"}), 400
    if len(question) > 1000:
        return jsonify({"error": "问题过长，请控制在 1000 字以内"}), 400
    history = _clean_history(data.get("history"))
    doc_ids = _clean_doc_ids(data.get("doc_ids"))
    blocked = _quota_guard(directory, "library")
    if blocked:
        return blocked

    def generate():
        yield sse({"type": "status", "message": "正在改写检索问题…"})
        search_query = _search_query(question, history)
        yield sse({"type": "status", "message": "正在检索整个文献库并重排…"})
        try:
            found = library_rag.search_library(directory, search_query, doc_ids)
        except EmbeddingUnavailable as exc:
            yield sse({"type": "error", "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            yield sse({"type": "error", "error": f"跨文献检索失败：{exc}"})
            return

        if not found["searched"]:
            yield sse({"type": "error",
                       "error": "文献库里还没有完成索引的文献，先上传并等索引建好"})
            return
        sources = found["sources"]
        if not sources:
            yield sse({"type": "error",
                       "error": "文献库中没有检索到与这个问题相关的内容"})
            return

        allowed = [s["n"] for s in sources]
        yield sse({"type": "status",
                   "message": f"已从 {len(found['documents'])} 篇文献找到 "
                              f"{len(sources)} 条证据，正在综合回答…"})
        llm = get_llm()
        pieces = []
        try:
            for piece in llm.stream_library_answer(question, sources,
                                                   allowed=allowed, history=history):
                pieces.append(piece)
                yield sse({"type": "delta", "text": piece})
        except LLMUnavailable as exc:
            yield sse({"type": "error", "error": str(exc)})
            return

        text, invalid = sanitize_source_citations("".join(pieces), allowed)
        yield sse({"type": "done", "text": text,
                   "citations": source_citations(text),
                   "invalid_citations": invalid,
                   "sources": library_sources_payload(sources),
                   "documents": found["documents"],
                   "searched": found["searched"],
                   "rewritten_query": search_query if search_query != question else None,
                   "source": "openai", "model": llm.model, "mode": "library"})

    return stream_response(generate())


@app.route("/api/library/ask", methods=["POST"])
def library_ask_json():
    """全库问答非流式版本，方便脚本调用。"""
    user, directory = user_scope()
    data = request.get_json(silent=True) or {}
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "没有收到问题"}), 400
    history = _clean_history(data.get("history"))
    doc_ids = _clean_doc_ids(data.get("doc_ids"))
    blocked = _quota_guard(directory, "library")
    if blocked:
        return blocked
    try:
        search_query = _search_query(question, history)
        found = library_rag.search_library(directory, search_query, doc_ids)
        if not found["sources"]:
            reason = ("文献库里还没有完成索引的文献"
                      if not found["searched"] else "没有检索到相关内容")
            return jsonify({"error": reason}), 400
        allowed = [s["n"] for s in found["sources"]]
        result = get_llm().answer_library_question(question, found["sources"],
                                                   allowed=allowed, history=history)
    except LLMUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"跨文献问答失败：{exc}"}), 400

    result["text"], result["invalid_citations"] = sanitize_source_citations(
        result.get("text", ""), allowed)
    result["citations"] = source_citations(result["text"])
    result["sources"] = library_sources_payload(found["sources"])
    result["documents"] = found["documents"]
    result["searched"] = found["searched"]
    result["mode"] = "library"
    if search_query != question:
        result["rewritten_query"] = search_query
    return jsonify({"question": question, "answer": result}), 200


@app.route("/api/library/review/stream", methods=["GET", "POST"])
def library_review_stream():
    """多篇文献对比 / 综述。topic 留空表示综述范围内的全部文献。"""
    user, directory = user_scope()
    data = _library_request()
    topic = str(data.get("topic") or "").strip()
    if len(topic) > 300:
        return jsonify({"error": "主题过长，请控制在 300 字以内"}), 400
    mode = data.get("mode") if data.get("mode") in ("compare", "review") else "review"
    doc_ids = _clean_doc_ids(data.get("doc_ids"))
    blocked = _quota_guard(directory, "library")
    if blocked:
        return blocked

    def generate():
        yield sse({"type": "status", "message": "正在从文献库挑选对比/综述材料…"})
        try:
            found = library_rag.review_materials(directory, topic, doc_ids)
        except Exception as exc:  # noqa: BLE001
            yield sse({"type": "error", "error": f"选材失败：{exc}"})
            return
        if not found["searched"]:
            yield sse({"type": "error",
                       "error": "文献库里还没有完成索引的文献，先上传并等索引建好"})
            return
        sources = found["sources"]
        if not sources:
            yield sse({"type": "error",
                       "error": "没有找到可用于综述的材料，换个主题或先给文献建好索引"})
            return

        allowed = [s["n"] for s in sources]
        kind = "对比分析" if mode == "compare" else "综述"
        yield sse({"type": "status",
                   "message": f"已选取 {len(found['documents'])} 篇文献的 "
                              f"{len(sources)} 条材料，正在生成{kind}…"})
        llm = get_llm()
        pieces = []
        try:
            for piece in llm.stream_review(topic, sources, mode, allowed=allowed):
                pieces.append(piece)
                yield sse({"type": "delta", "text": piece})
        except LLMUnavailable as exc:
            yield sse({"type": "error", "error": str(exc)})
            return

        text, invalid = sanitize_source_citations("".join(pieces), allowed)
        yield sse({"type": "done", "text": text,
                   "citations": source_citations(text),
                   "invalid_citations": invalid,
                   "sources": library_sources_payload(sources),
                   "documents": found["documents"],
                   "searched": found["searched"],
                   "source": "openai", "model": llm.model, "mode": mode})

    return stream_response(generate())


@app.route("/api/documents/<doc_id>/status", methods=["GET"])
def document_status(doc_id):
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    state = docs.job_state(doc_id)
    return jsonify({
        "id": doc_id,
        "index_status": docs.index_status_of(info) if state["status"] != "running" else "running",
        "index_done": state["done"],
        "index_total": state["total"] or info.get("chunk_count", 0),
        "index_stage": state.get("stage"),
        "index_error": state["error"] or info.get("index_error"),
        "chunk_count": info.get("chunk_count", 0),
        "page_count": info.get("page_count", 0),
        "source_type": info.get("source_type", "pdf"),
        "unit": info.get("unit", "页"),
        "ocr_pages": info.get("ocr_pages", 0),
    }), 200


@app.route("/api/documents/<doc_id>", methods=["PATCH"])
def update_document(doc_id):
    """更新标题、分类、标签或阅读进度。只改元数据，不动原文件。"""
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求格式错误"}), 400

    changed = False
    if "display_name" in data:
        name = " ".join(str(data.get("display_name") or "").split())[:120]
        if name:
            info["display_name"] = name
        else:
            info.pop("display_name", None)
        changed = True

    if "category" in data:
        category = " ".join(str(data.get("category") or "").split())[:40]
        if category:
            info["category"] = category
        else:
            info.pop("category", None)
        changed = True

    if "tags" in data:
        raw = data.get("tags")
        if isinstance(raw, str):
            raw = re.split(r"[,，、;；\s]+", raw)
        if not isinstance(raw, list):
            raw = []
        tags = []
        for value in raw:
            tag = " ".join(str(value or "").split())[:24]
            if tag and tag not in tags:
                tags.append(tag)
        info["tags"] = tags[:10]
        changed = True

    if "progress" in data:
        progress = data.get("progress")
        if not isinstance(progress, dict):
            return jsonify({"error": "阅读进度格式错误"}), 400
        try:
            page = int(progress.get("page") or 1)
        except (TypeError, ValueError):
            return jsonify({"error": "页码必须是整数"}), 400
        total = int(info.get("page_count") or 0)
        page = max(1, page)
        if total:
            page = min(page, total)
        percent = round(page * 100 / total) if total else 0
        now = datetime.now().isoformat(timespec="seconds")
        info["progress"] = {"page": page, "percent": percent, "updated_at": now}
        info["last_opened_at"] = now
        changed = True

    if "starred" in data:
        info["starred"] = bool(data.get("starred"))
        changed = True

    if "citation" in data:
        raw = data.get("citation")
        if not isinstance(raw, dict):
            return jsonify({"error": "引用信息格式错误"}), 400
        fields = {}
        for key in ("authors", "year", "source", "volume",
                    "issue", "pages", "doi", "title"):
            value = " ".join(str(raw.get(key) or "").split())[:200]
            if value:
                fields[key] = value
        kind = str(raw.get("type") or "").strip().lower()
        if kind in {"article", "conference", "thesis", "book", "web"}:
            fields["type"] = kind
        if fields:
            info["citation"] = fields
        else:
            info.pop("citation", None)
        changed = True

    if not changed:
        return jsonify({"error": "没有要更新的字段"}), 400

    docs.save_info(directory, doc_id, info)
    return jsonify({"success": True, "document": docs.summary_of(info)}), 200


@app.route("/api/documents/batch-update", methods=["POST"])
def batch_update_documents():
    """批量改分类、加标签、标星。整理文献库时不用一篇篇点。"""
    user, directory = user_scope()
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "没有选中文献"}), 400
    if len(ids) > 200:
        return jsonify({"error": "一次最多批量处理 200 篇"}), 400

    category = data.get("category")
    starred = data.get("starred")
    add_tags = data.get("add_tags")
    if isinstance(add_tags, str):
        add_tags = re.split(r"[,，、;；\s]+", add_tags)
    if not isinstance(add_tags, list):
        add_tags = []
    clean_tags = []
    for value in add_tags:
        tag = " ".join(str(value or "").split())[:24]
        if tag and tag not in clean_tags:
            clean_tags.append(tag)

    if category is None and starred is None and not clean_tags:
        return jsonify({"error": "没有要修改的内容"}), 400

    updated = 0
    for doc_id in ids:
        info = docs.load_info(directory, str(doc_id))
        if info is None:
            continue
        if category is not None:
            name = " ".join(str(category).split())[:40]
            if name:
                info["category"] = name
            else:
                info.pop("category", None)
        if clean_tags:
            tags = list(info.get("tags") or [])
            for tag in clean_tags:
                if tag not in tags:
                    tags.append(tag)
            info["tags"] = tags[:10]
        if starred is not None:
            info["starred"] = bool(starred)
        docs.save_info(directory, doc_id, info)
        updated += 1
    return jsonify({"success": True, "updated": updated}), 200


@app.route("/api/documents/<doc_id>/citation", methods=["GET"])
def document_citation(doc_id):
    """生成参考文献条目：GB/T 7714、APA、MLA、BibTeX 四种格式。"""
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    style = (request.args.get("style") or "gbt7714").strip().lower()
    fields = cite.fields_of(info)
    return jsonify({
        "style": style,
        "text": cite.render(style, fields),
        "fields": fields,
        "styles": cite.STYLES,
        "missing": cite.missing(fields),
    }), 200


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / max(1, len(text))


@app.route("/api/documents/<doc_id>/selection", methods=["POST"])
def selection_action(doc_id):
    """划词解读：对选中的一段文字提问或翻译。

    只把这段文字和相关段落发给模型，不改文档、不写进问答历史，
    用户随手圈一句话就能问，不会污染正文问答的上下文。
    """
    user, directory = user_scope()
    data = request.get_json(silent=True) or {}
    text = " ".join(str(data.get("text") or "").split())
    if not text:
        return jsonify({"error": "没有收到选中的文字"}), 400
    if len(text) > 3000:
        return jsonify({"error": "选中的文字太长，请控制在 3000 字以内"}), 400
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    blocked = _quota_guard(directory, "selection")
    if blocked:
        return blocked

    action = str(data.get("action") or "explain").strip().lower()
    unit = info.get("unit", "页")
    try:
        page = max(0, int(data.get("page") or 0))
    except (TypeError, ValueError):
        page = 0

    if action == "translate":
        target = str(data.get("target_lang") or "zh")
        source = str(data.get("source_lang") or "")
        if not source:
            source = "zh" if _cjk_ratio(text) > 0.15 else "en"
        if source == target:
            target = "en" if source == "zh" else "zh"
        try:
            result = get_llm().translate_text(text, source, target)
        except LLMUnavailable as exc:
            quota.refund(directory, "selection")
            return jsonify({"error": str(exc)}), 503
        return jsonify({
            "action": "translate",
            "original": text,
            "text": result.get("translated") or "",
            "source_lang": source,
            "target_lang": target,
            "model": result.get("model", ""),
            "quota": quota.snapshot(directory),
        }), 200

    question = " ".join(str(data.get("question") or "").split())[:300]
    if not question:
        question = ("这段话是什么意思？请解释它讲的内容、涉及的关键概念，"
                    "以及它在全文里的作用。")

    passages = [{
        "page": page or 1,
        "section": "",
        "text": "【用户选中的原文】" + text,
        "score": 1.0,
    }]
    # 选中片段一定保留；再补几条语义相关段落，帮模型结合上下文解释
    try:
        extra = _retrieve(directory, doc_id, info, question, question)
    except Exception as exc:  # noqa: BLE001  索引没建好也能只靠选中文字回答
        print(f"[划词] 补充检索失败：{type(exc).__name__}: {exc}")
        extra = []
    seen = {text}
    for item in extra:
        body = str(item.get("text") or "")
        if not body or body in seen:
            continue
        seen.add(body)
        passages.append(item)
        if len(passages) >= PASSAGE_LIMIT:
            break

    try:
        result = get_llm().answer_question(question, passages, unit)
    except LLMUnavailable as exc:
        quota.refund(directory, "selection")
        return jsonify({"error": str(exc)}), 503

    answer, invalid = sanitize_citations(result.get("text", ""),
                                         allowed_pages(passages))
    return jsonify({
        "action": "explain",
        "original": text,
        "question": question,
        "text": answer,
        "invalid_citations": invalid,
        "passages": passages_payload(passages),
        "quota": quota.snapshot(directory),
    }), 200


# ==================================================================== 上传
@app.route("/api/upload", methods=["POST"])
def upload():
    user, directory = user_scope()

    if embedding_service.load_error:
        return jsonify({"error": f"向量服务不可用，无法建立索引：{embedding_service.load_error}"}), 503
    if "file" not in request.files:
        return jsonify({"error": "没有收到文件"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "没有选择文件"}), 400
    if Path(file.filename).suffix.lower().lstrip(".") not in config.ALLOWED_EXTENSIONS:
        allowed = "、".join("." + e for e in sorted(config.ALLOWED_EXTENSIONS))
        return jsonify({"error": f"不支持的格式，目前支持：{allowed}"}), 400

    # 注意：secure_filename 会把纯中文文件名连同后缀一起吃掉
    #（"分割研究备忘.txt" → "txt"，没有点），存盘后就判断不出格式了，
    # 所以这里用"清洗过的文件名主体 + 原始后缀"重新拼。
    suffix = Path(file.filename).suffix.lower()
    stem = secure_filename(Path(file.filename).stem) or "document"
    filename = f"{stem}{suffix}" if suffix else stem
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    doc_id = hashlib.md5(f"{filename}_{stamp}".encode()).hexdigest()[:16]
    stored = f"{doc_id}_{filename}"
    path = directory / stored
    file.save(path)

    try:
        units, unit, source_type = extract_units(str(path))
        scanned_pages = sum(1 for page in units if page.get("needs_ocr"))
        ocr_needed = source_type == "pdf" and scanned_pages > 0
        chunks = chunk_pages(units)
    except PdfExtractError as exc:
        path.unlink(missing_ok=True)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        return jsonify({"error": f"处理文件时出错：{exc}"}), 500

    ocr_blocked = None
    if ocr_needed:
        ok, reason = ocr.available()
        if not ok:
            ocr_blocked = reason
            if not chunks:
                path.unlink(missing_ok=True)
                return jsonify({"error": f"这份 PDF 是扫描件，需要 OCR，但{reason}"}), 400
            # 混合型文档：OCR 不可用时先索引已有文字页，不让整篇上传失败
            ocr_needed = False

    if not chunks and not ocr_needed:
        path.unlink(missing_ok=True)
        return jsonify({"error": "这份文件没有可用的文字内容"}), 400

    info = {
        "schema_version": docs.SCHEMA_VERSION,
        "id": doc_id,
        "user_id": user["id"],
        "original_filename": file.filename,
        "stored_filename": stored,
        "upload_time": datetime.now().isoformat(timespec="seconds"),
        "page_count": len(units),
        "chunk_count": 0 if ocr_needed else len(chunks),
        "source_type": source_type,
        "unit": unit,
        "chunks": [] if ocr_needed else chunks,
        "embedding_model": config.EMBEDDING_MODEL_NAME,
        "has_index": False,
        "index_status": "pending",
        "ocr_required": ocr_needed,
        "ocr_pages": 0,
    }
    if ocr_blocked:
        info["ocr_error"] = ocr_blocked
    docs.save_info(directory, doc_id, info)

    # 上传的 PDF 本身带高亮时直接转成标记，之后就能一起导出 Word
    imported = 0
    if source_type == "pdf":
        try:
            imported = mark_store.import_highlights(directory, info, path)["imported"]
        except Exception as exc:  # noqa: BLE001  导入失败不影响正常上传
            print(f"[上传] 导入手动高亮失败：{type(exc).__name__}: {exc}")

    if ocr_needed:
        docs.start_ocr_index_job(directory, doc_id, path, len(units))
        print(f"[上传] {user['username']} / {file.filename} → "
              f"{len(units)} 页，其中 {scanned_pages} 页需要 OCR，"
              f"导入手动高亮 {imported} 条，开始后台 OCR 与索引")
        message = f"上传成功，检测到 {scanned_pages} 页图片文字，正在后台 OCR 识别并建立索引"
    else:
        docs.start_index_job(directory, doc_id, chunks)
        print(f"[上传] {user['username']} / {file.filename} → "
              f"{len(units)} {unit} / {len(chunks)} 块（{source_type}），"
              f"导入手动高亮 {imported} 条，开始后台索引")
        message = "上传成功，正在后台建立语义索引"
    if imported:
        message = f"上传成功，已导入手动高亮 {imported} 条，" + message.split("，", 1)[-1]
    return jsonify({
        "success": True,
        "message": message,
        "imported_marks": imported,
        "document": docs.summary_of(info),
    }), 200


# ==================================================================== 原文文件
@app.route("/api/documents/<doc_id>/file", methods=["GET"])
def document_file(doc_id):
    """给前端 iframe 用的原始 PDF。支持 ?token= 以便跨源嵌入。"""
    user = auth.current_user() or auth.user_from_token(request.args.get("token"))
    if not user:
        return jsonify({"error": "请先登录"}), 401
    if auth.is_banned(user):
        return jsonify({"error": "账号已被封禁", "code": "banned"}), 403
    directory = docs.user_dir(user["id"])
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    path = docs.source_path_of(directory, info)
    if path is None:
        return jsonify({"error": "原始文件已丢失"}), 404
    # 有红色高亮或改字时，优先返回生成的副本；原文件始终保留
    edited = mark_store.ensure_edited_pdf(directory, doc_id, path)
    if edited is not None:
        path = edited
    response = send_from_directory(str(path.parent), path.name, conditional=True)
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/documents/<doc_id>/blocks", methods=["GET"])
def document_blocks(doc_id):
    """给前端正文预览用：返回带出处编号的文本块。

    PDF 走 iframe 看原文；txt/md/docx 没有页可跳，就用这个接口把正文渲染出来，
    点出处时滚动定位到对应段落并高亮。
    """
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    chunks = info.get("chunks", [])
    return jsonify({
        "id": doc_id,
        "filename": info.get("original_filename", ""),
        "source_type": info.get("source_type", "pdf"),
        "unit": info.get("unit", "页"),
        "blocks": [
            {"i": c.get("i", n), "page": c.get("page", n + 1),
             "section": c.get("section", ""), "text": c.get("text", "")}
            for n, c in enumerate(chunks)
        ],
    }), 200


# ==================================================================== 标记
@app.route("/api/documents/<doc_id>/marks", methods=["GET", "POST"])
def document_marks(doc_id):
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404

    if request.method == "GET":
        return jsonify({
            "marks": mark_store.load_marks(directory, doc_id),
            "edits": mark_store.load_edits(directory, doc_id),
            "version": mark_store.version_of(directory, doc_id),
        }), 200

    data = request.get_json(silent=True) or {}
    try:
        page = int(data.get("page") or 0)
        paragraph = int(data.get("paragraph") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "页码或段落编号不是数字"}), 400
    if page < 1 or paragraph < 1:
        return jsonify({"error": "页码和段落编号必须从 1 开始"}), 400

    source = docs.source_path_of(directory, info)
    try:
        mark = mark_store.add_mark(directory, info, source, page, paragraph,
                                   str(data.get("note") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "mark": mark,
        "marks": mark_store.load_marks(directory, doc_id),
        "edits": mark_store.load_edits(directory, doc_id),
        "version": mark_store.version_of(directory, doc_id),
    }), 200


@app.route("/api/documents/<doc_id>/marks/<mark_id>", methods=["DELETE"])
def remove_mark(doc_id, mark_id):
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    if not mark_store.delete_mark(directory, doc_id, mark_id):
        return jsonify({"error": "标记不存在"}), 404
    return jsonify({
        "success": True,
        "marks": mark_store.load_marks(directory, doc_id),
        "edits": mark_store.load_edits(directory, doc_id),
        "version": mark_store.version_of(directory, doc_id),
    }), 200


@app.route("/api/documents/<doc_id>/edits", methods=["GET", "POST"])
def document_edits(doc_id):
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404

    if request.method == "GET":
        return jsonify({
            "edits": mark_store.load_edits(directory, doc_id),
            "version": mark_store.version_of(directory, doc_id),
        }), 200

    data = request.get_json(silent=True) or {}
    source = docs.source_path_of(directory, info)
    try:
        edit = mark_store.add_edit(directory, info, source, data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"修改失败：{exc}"}), 500
    return jsonify({
        "edit": edit,
        "edits": mark_store.load_edits(directory, doc_id),
        "version": mark_store.version_of(directory, doc_id),
    }), 200


@app.route("/api/documents/<doc_id>/edits/<edit_id>", methods=["DELETE"])
def remove_edit(doc_id, edit_id):
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    if not mark_store.delete_edit(directory, doc_id, edit_id):
        return jsonify({"error": "修改记录不存在"}), 404
    return jsonify({
        "success": True,
        "edits": mark_store.load_edits(directory, doc_id),
        "version": mark_store.version_of(directory, doc_id),
    }), 200


@app.route("/api/documents/<doc_id>/command", methods=["POST"])
def parse_command(doc_id):
    """用大模型把自然语言指令解析成结构化动作。

    只把指令本身和页码等上下文发给模型，不发送文献正文。
    前端优先走本地规则，只有本地没听懂时才调用这里。
    """
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "没有收到指令"}), 400

    unit = info.get("unit", "页")
    page_count = int(info.get("page_count") or 0)

    def recent(items, limit=3):
        ordered = sorted(items, key=lambda item: item.get("created_at", ""))
        return [{"page": item.get("page"), "paragraph": item.get("paragraph", 1),
                 "type": item.get("type", "mark"),
                 "text": (item.get("text") or "")[:30]}
                for item in ordered[-limit:]]

    try:
        command = get_llm().interpret_command(text, {
            "source_type": info.get("source_type", "pdf"),
            "unit": unit,
            "page_count": page_count,
            "recent_marks": recent(mark_store.load_marks(directory, doc_id)),
            "recent_edits": recent(mark_store.load_edits(directory, doc_id)),
        })
    except LLMUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"指令解析失败：{exc}"}), 400

    allowed = {"mark", "unmark", "edit", "unedit", "jump",
               "list", "export", "qa", "unsupported"}
    action = command.get("action")
    if action not in allowed:
        action = "unsupported"

    try:
        page = int(command.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    try:
        paragraph = int(command.get("paragraph") or 1)
    except (TypeError, ValueError):
        paragraph = 1
    if paragraph < 1:
        paragraph = 1

    if action in {"mark", "unmark", "edit", "unedit", "jump"}:
        if page < 1:
            action = "unsupported"
            command["reply"] = "没有听出页码，请说清楚是第几页第几段。"
        elif page_count and page > page_count:
            action = "unsupported"
            command["reply"] = f"这份文献只有 {page_count} {unit}，没有第 {page} {unit}。"

    command.update({"action": action, "page": page, "paragraph": paragraph})
    return jsonify({"command": command}), 200


@app.route("/api/documents/<doc_id>/marks/import", methods=["POST"])
def import_marks(doc_id):
    """导入手动高亮：接收带注释的 PDF，把高亮/下划线转成标记。"""
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    if info.get("source_type") != "pdf":
        return jsonify({"error": "只有 PDF 支持导入手动高亮"}), 400
    if "file" not in request.files:
        return jsonify({"error": "没有收到文件"}), 400
    file = request.files["file"]
    if Path(file.filename or "").suffix.lower() != ".pdf":
        return jsonify({"error": "请上传 PDF 文件"}), 400

    tmp = directory / ".import_highlights.tmp.pdf"
    file.save(tmp)
    try:
        result = mark_store.import_highlights(directory, info, tmp)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"导入手动高亮失败：{exc}"}), 500
    finally:
        tmp.unlink(missing_ok=True)

    return jsonify({
        "success": True,
        "imported": result["imported"],
        "skipped": result["skipped"],
        "annotations": result["annotations"],
        "marks": result["marks"],
        "edits": mark_store.load_edits(directory, doc_id),
        "version": mark_store.version_of(directory, doc_id),
    }), 200


@app.route("/api/documents/<doc_id>/marks/export", methods=["GET"])
def export_marks(doc_id):
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    marks = mark_store.load_marks(directory, doc_id)
    if not marks:
        return jsonify({"error": "还没有任何标记，先说一句「标记第 3 页第 1 段」"}), 400
    data = mark_store.build_docx(info, marks)
    stem = Path(info.get("display_name") or info.get("original_filename") or "文献").stem
    return send_file(
        io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=f"{stem}_标记.docx",
    )


@app.route("/api/documents/<doc_id>/export", methods=["POST"])
def export_content(doc_id):
    """把前端当前的摘要 / 问答记录转成 Word 或 Markdown，不调用大模型。"""
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404

    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    fmt = str(data.get("format") or "docx").lower()
    if kind not in ("summary", "qa"):
        return jsonify({"error": "kind 只能是 summary 或 qa"}), 400
    if fmt not in ("docx", "md"):
        return jsonify({"error": "format 只能是 docx 或 md"}), 400

    if kind == "summary":
        summary = data.get("summary") or {}
        text = str(summary.get("text") or "").strip()
        if not text:
            return jsonify({"error": "还没有摘要，先生成一次再导出"}), 400
        if len(text) > 60000:
            return jsonify({"error": "摘要内容过长"}), 400
        payload = {"text": text, "model": str(summary.get("model") or "")[:80]}
    else:
        turns = data.get("turns")
        if not isinstance(turns, list) or not turns:
            return jsonify({"error": "还没有问答记录"}), 400
        clean = []
        for turn in turns[-50:]:
            if not isinstance(turn, dict):
                continue
            question = str(turn.get("q") or "").strip()[:1000]
            answer = str(turn.get("a") or "").strip()[:20000]
            if not question and not answer:
                continue
            citations = []
            for value in (turn.get("citations") or [])[:20]:
                try:
                    citations.append(int(value))
                except (TypeError, ValueError):
                    continue
            passages = []
            for passage in (turn.get("passages") or [])[:4]:
                if not isinstance(passage, dict):
                    continue
                try:
                    page = int(passage.get("page"))
                except (TypeError, ValueError):
                    continue
                passages.append({
                    "page": page,
                    "section": str(passage.get("section") or "")[:120],
                    "text": str(passage.get("text") or "")[:600],
                })
            clean.append({"q": question, "a": answer,
                          "citations": citations, "passages": passages})
        if not clean:
            return jsonify({"error": "问答记录为空"}), 400
        if sum(len(turn["a"]) for turn in clean) > 200000:
            return jsonify({"error": "问答内容过长，请分批导出"}), 400
        payload = {"turns": clean}

    try:
        if fmt == "docx":
            output = export_store.build_docx(info, kind, payload)
            mimetype = ("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document")
        else:
            output = export_store.build_markdown(info, kind, payload)
            mimetype = "text/markdown; charset=utf-8"
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"导出失败：{exc}"}), 500

    return send_file(
        io.BytesIO(output),
        mimetype=mimetype,
        as_attachment=True,
        download_name=export_store.export_filename(info, kind, fmt),
    )


# ==================================================================== 摘要
@app.route("/api/documents/<doc_id>/summary/stream", methods=["GET"])
def summary_stream(doc_id):
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    blocked = _quota_guard(directory, "summary")
    if blocked:
        return blocked

    def generate():
        yield sse({"type": "status", "message": "正在挑选摘要章节…"})
        passages = docs.select_summary_passages(info.get("chunks", []))
        if not passages:
            yield sse({"type": "error", "error": "这份文档没有可用的文本片段"})
            return
        yield sse({"type": "status", "message": f"已选取 {len(passages)} 个片段，正在生成摘要…"})
        llm = get_llm()
        allowed = allowed_pages(passages)
        pieces = []
        try:
            for piece in llm.stream_summary(passages, info.get("original_filename", ""),
                                            info.get("unit", "页"), allowed=allowed):
                pieces.append(piece)
                yield sse({"type": "delta", "text": piece})
        except LLMUnavailable as exc:
            yield sse({"type": "error", "error": str(exc)})
            return
        text, invalid = sanitize_citations("".join(pieces), allowed)
        yield sse({"type": "done", "text": text,
                   "citations": citation_pages(text),
                   "invalid_citations": invalid,
                   "source": "openai", "model": llm.model,
                   "passages": passages_payload(passages)})

    return stream_response(generate())


@app.route("/api/documents/<doc_id>/summary", methods=["GET"])
def summary_json(doc_id):
    """非流式版本，方便脚本调用。"""
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    passages = docs.select_summary_passages(info.get("chunks", []))
    if not passages:
        return jsonify({"error": "这份文档没有可用的文本片段"}), 400
    blocked = _quota_guard(directory, "summary")
    if blocked:
        return blocked
    try:
        allowed = allowed_pages(passages)
        result = get_llm().generate_summary(passages, info.get("original_filename", ""),
                                            info.get("unit", "页"), allowed=allowed)
    except LLMUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    result["text"], result["invalid_citations"] = sanitize_citations(
        result.get("text", ""), allowed)
    result["citations"] = citation_pages(result["text"])
    result["passages"] = passages_payload(passages)
    return jsonify({"document_id": doc_id, "summary": result}), 200


# ==================================================================== 问答
_FOLLOWUP_RE = re.compile(
    r"(还有|再(具体|详细|说|多|举|展开)|继续|接着|然后呢|它|这个|那个|"
    r"上述|上面|前面|刚才|其(余|中)|呢[？?]?$)"
)


def _clean_history(raw, limit: int = 8):
    """最近几轮对话：只保留问答摘要和引用页码，避免请求过大。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = None
    if not isinstance(raw, list):
        return []
    history = []
    for item in raw[-limit:]:
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or "").strip()[:300]
        a = str(item.get("a") or "").strip()[:600]
        if not q and not a:
            continue
        pages = []
        for value in (item.get("pages") or [])[:8]:
            try:
                pages.append(int(value))
            except (TypeError, ValueError):
                continue
        history.append({"q": q, "a": a, "pages": pages})
    return history


def _is_followup(question: str) -> bool:
    text = (question or "").strip()
    if not text:
        return False
    return len(text) <= 12 or bool(_FOLLOWUP_RE.search(text))


def _search_query(question: str, history, unit: str = "页") -> str:
    """所有提问都先改写检索词；追问会结合上文补全指代。"""
    try:
        rewritten = get_llm().rewrite_query(question, history, unit)
        rewritten = (rewritten or "").strip()
        if 2 <= len(rewritten) <= 200:
            return rewritten
    except Exception as exc:  # noqa: BLE001  改写失败就退回原问题/上一问
        print(f"[检索] 改写查询失败：{type(exc).__name__}: {exc}")
    if history and _is_followup(question):
        last = history[-1]
        return " ".join(part for part in (
            str(last.get("q") or "").strip(), question) if part)
    return question


def _retrieve(directory: Path, doc_id: str, info: dict, question: str,
              original_question: str = ""):
    chunks = info.get("chunks", [])
    if not chunks:
        raise RuntimeError("这份文档还没有完成索引，请稍候")
    unit = info.get("unit", "页")

    # 问「第244页」这类明确出处时，直接读取该页/段的全部片段：
    # 语义检索对纯页码问题没有可靠的召回能力，也容易漏掉用户指定的那一页。
    pages = page_references(original_question or question, unit)
    if pages:
        page_count = int(info.get("page_count") or 0)
        if page_count and max(pages) > page_count:
            raise RuntimeError(
                f"这篇文献共 {page_count} {unit}，没有第 {max(pages)} {unit}")
        targeted = page_passages(chunks, pages)
        if targeted:
            return targeted
        raise RuntimeError(
            "第 " + "、".join(str(page) for page in pages)
            + f" {unit}没有可用的文字内容（可能是空白页或纯图片页）")

    if docs.needs_rebuild(info) or not docs.index_path(directory, doc_id).exists():
        docs.ensure_index_started(directory, doc_id, info)
        raise RuntimeError("这份文档正在建立索引，请稍候再问")
    index = DocIndex.load(directory, doc_id, chunks)
    # 先多召回一些候选，再让 cross-encoder 精排；模型不可用时自动退回原顺序
    candidates = index.search(question, top_k=config.RERANK_CANDIDATES)
    return get_reranker().rerank(question, candidates, config.RETRIEVE_TOP_K)


@app.route("/api/documents/<doc_id>/ask/stream", methods=["GET", "POST"])
def ask_stream(doc_id):
    user, directory = user_scope()
    data = request.get_json(silent=True) or {} if request.method == "POST" else {}
    question = ((data.get("question") if request.method == "POST" else request.args.get("q")) or "").strip()
    if not question:
        return jsonify({"error": "没有收到问题"}), 400
    history = _clean_history(data.get("history") if request.method == "POST"
                             else request.args.get("history"))
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404

    unit = info.get("unit", "页")
    pages = page_references(question, unit)
    blocked = _quota_guard(directory, "ask")
    if blocked:
        return blocked

    def generate():
        if pages:
            yield sse({"type": "status",
                       "message": "正在读取第 " + "、".join(str(page) for page in pages)
                       + f" {unit}…"})
            search_query = question
        else:
            yield sse({"type": "status",
                       "message": "正在结合上文理解追问…" if history and _is_followup(question)
                       else "正在改写检索问题…"})
            search_query = _search_query(question, history, unit)
            yield sse({"type": "status", "message": "正在检索并重排相关片段…"})
        try:
            passages = _retrieve(directory, doc_id, info, search_query, question)
        except (EmbeddingUnavailable, RuntimeError) as exc:
            yield sse({"type": "error", "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            yield sse({"type": "error", "error": f"检索失败：{exc}"})
            return

        if not passages:
            yield sse({"type": "error", "error": "没有检索到相关片段"})
            return
        yield sse({"type": "status",
                   "message": f"已检索 {len(passages)} 个片段，正在组织回答…"})
        llm = get_llm()
        allowed = allowed_pages(passages)
        pieces = []
        try:
            for piece in llm.stream_answer(question, passages,
                                           info.get("unit", "页"), history=history,
                                           allowed=allowed):
                pieces.append(piece)
                yield sse({"type": "delta", "text": piece})
        except LLMUnavailable as exc:
            yield sse({"type": "error", "error": str(exc)})
            return
        text, invalid = sanitize_citations("".join(pieces), allowed)
        citations = citation_pages(text)
        yield sse({"type": "done", "text": text, "citations": citations,
                   "invalid_citations": invalid,
                   "source": "openai", "model": llm.model,
                   "mode": "document" if citations else "general",
                   "rewritten_query": search_query if search_query != question else None,
                   # 通用知识回答没有出处，就别再挂一串无关的"参考原文"
                   "passages": passages_payload(passages) if citations else []})

    return stream_response(generate())


@app.route("/api/documents/<doc_id>/ask", methods=["POST"])
def ask_json(doc_id):
    user, directory = user_scope()
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "没有收到问题"}), 400
    history = _clean_history(data.get("history"))
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    unit = info.get("unit", "页")
    pages = page_references(question, unit)
    blocked = _quota_guard(directory, "ask")
    if blocked:
        return blocked
    try:
        search_query = question if pages else _search_query(question, history, unit)
        passages = _retrieve(directory, doc_id, info, search_query, question)
        allowed = allowed_pages(passages)
        result = get_llm().answer_question(question, passages,
                                           unit, history=history,
                                           allowed=allowed)
        result["text"], result["invalid_citations"] = sanitize_citations(
            result.get("text", ""), allowed)
        result["citations"] = citation_pages(result["text"])
        if search_query != question:
            result["rewritten_query"] = search_query
    except LLMUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    result["mode"] = "document" if result.get("citations") else "general"
    result["passages"] = passages_payload(passages) if result.get("citations") else []
    return jsonify({"document_id": doc_id, "question": question, "answer": result}), 200


# ==================================================================== 翻译
@app.route("/api/translate/stream", methods=["GET"])
def translate_stream():
    text = (request.args.get("text") or "").strip()
    source_lang = request.args.get("source_lang", "en")
    target_lang = request.args.get("target_lang", "zh")
    if not text:
        return jsonify({"error": "没有要翻译的文本"}), 400
    if source_lang == target_lang:
        return jsonify({"error": "源语言和目标语言相同"}), 400
    user, directory = user_scope()
    blocked = _quota_guard(directory, "translate")
    if blocked:
        return blocked

    def generate():
        llm = get_llm()
        pieces = []
        try:
            for piece in llm.stream_translate(text, source_lang, target_lang):
                pieces.append(piece)
                yield sse({"type": "delta", "text": piece})
        except LLMUnavailable as exc:
            yield sse({"type": "error", "error": str(exc)})
            return
        yield sse({"type": "done", "source_lang": source_lang,
                   "target_lang": target_lang, "model": llm.model})

    return stream_response(generate())


@app.route("/api/translate", methods=["POST"])
def translate_json():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "没有要翻译的文本"}), 400
    user, directory = user_scope()
    blocked = _quota_guard(directory, "translate")
    if blocked:
        return blocked
    try:
        result = get_llm().translate_text(
            text, data.get("source_lang", "en"), data.get("target_lang", "zh")
        )
    except LLMUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(result), 200


# ==================================================================== 删除
def _remove_document_files(directory: Path, doc_id: str, info: dict) -> None:
    """删除文献及其索引、标记。先打墓碑，避免后台索引线程把元数据写回来。"""
    docs.mark_deleted(doc_id)
    source = docs.source_path_of(directory, info)
    if source is not None:
        source.unlink(missing_ok=True)
    for path in (docs.info_path(directory, doc_id),
                 docs.index_path(directory, doc_id),
                 docs.embeddings_path(directory, doc_id),
                 mark_store.marks_path(directory, doc_id),
                 mark_store.edits_path(directory, doc_id),
                 mark_store.edited_pdf_path(directory, doc_id)):
        path.unlink(missing_ok=True)


@app.route("/api/documents/<doc_id>/delete", methods=["DELETE"])
def delete_document(doc_id):
    user, directory = user_scope()
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文档不存在"}), 404
    _remove_document_files(directory, doc_id, info)
    return jsonify({"success": True, "message": "文档已删除"}), 200


@app.route("/api/documents/batch-delete", methods=["POST"])
def batch_delete_documents():
    user, directory = user_scope()
    data = request.get_json(silent=True) or {}
    raw = data.get("ids")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "没有选择文献"}), 400

    ids: list = []
    for value in raw[:200]:
        doc_id = str(value or "").strip()
        if doc_id and doc_id not in ids:
            ids.append(doc_id)

    deleted, missing = 0, []
    for doc_id in ids:
        info = docs.load_info(directory, doc_id)
        if info is None:
            missing.append(doc_id)
            continue
        _remove_document_files(directory, doc_id, info)
        deleted += 1
    return jsonify({"success": True, "deleted": deleted, "missing": missing}), 200


# ==================================================================== 管理后台
_ADMIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _admin_id(value) -> str:
    """校验用户ID / 文档ID，挡住 ../ 之类的路径穿越。"""
    value = str(value or "").strip()
    return value if _ADMIN_ID_RE.match(value) else ""


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _admin_target(user_id: str):
    target = auth.get_user(_admin_id(user_id))
    if target is None:
        return None
    return target


@app.route("/api/admin/stats", methods=["GET"])
@admin.admin_required
def admin_stats():
    users = auth.all_users()
    storage = sum(admin.user_stats(u.get("id", ""))["storage_bytes"] for u in users)
    _, audit_total = admin.read_audit(1, 0)
    return jsonify({
        "users": len(users),
        "admins": sum(1 for u in users if auth.is_admin(u)),
        "banned": sum(1 for u in users if auth.is_banned(u)),
        "documents": len(admin.scan_documents()),
        "storage_bytes": storage,
        "audit_entries": audit_total,
    }), 200


@app.route("/api/admin/users", methods=["GET"])
@admin.admin_required
def admin_users():
    query = (request.args.get("q") or "").strip().lower()
    limit = _int_arg("limit", 50, 1, 200)
    offset = _int_arg("offset", 0, 0, 100000)
    records = []
    for user in auth.all_users():
        record = admin.sanitize_user(
            user, admin.user_stats(user.get("id", "")))
        if query and (query not in record["username"].lower()
                      and query not in record["id"].lower()):
            continue
        records.append(record)
    records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return jsonify({"users": records[offset:offset + limit],
                    "total": len(records)}), 200


@app.route("/api/admin/users/<user_id>/ban", methods=["POST"])
@admin.admin_required
def admin_ban_user(user_id):
    actor = request.user  # type: ignore[attr-defined]
    target = _admin_target(user_id)
    if target is None:
        return jsonify({"error": "用户不存在"}), 404
    if target.get("id") == actor.get("id"):
        return jsonify({"error": "不能封禁自己"}), 400
    if auth.is_admin(target):
        return jsonify({"error": "不能封禁其他管理员，请先在命令行调整角色"}), 400

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if len(reason) < 2:
        return jsonify({"error": "请填写封禁原因（至少 2 个字）"}), 400

    record = auth.set_banned(target["id"], True, reason, actor.get("username", ""))
    admin.audit("admin.user.ban", actor, target_type="user",
                target_id=record["id"], target_name=record["username"],
                reason=reason)
    return jsonify({"success": True, "message": "账号已封禁",
                    "user": admin.sanitize_user(
                        record, admin.user_stats(record["id"]))}), 200


@app.route("/api/admin/users/<user_id>/unban", methods=["POST"])
@admin.admin_required
def admin_unban_user(user_id):
    actor = request.user  # type: ignore[attr-defined]
    target = _admin_target(user_id)
    if target is None:
        return jsonify({"error": "用户不存在"}), 404

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    record = auth.set_banned(target["id"], False, reason, actor.get("username", ""))
    admin.audit("admin.user.unban", actor, target_type="user",
                target_id=record["id"], target_name=record["username"],
                reason=reason)
    return jsonify({"success": True, "message": "账号已解封",
                    "user": admin.sanitize_user(
                        record, admin.user_stats(record["id"]))}), 200


@app.route("/api/admin/documents", methods=["GET"])
@admin.admin_required
def admin_documents():
    """默认只返回文献元数据，绝不返回正文、分块或文件路径。"""
    owner_id = _admin_id(request.args.get("user_id", ""))
    query = (request.args.get("q") or "").strip().lower()
    limit = _int_arg("limit", 50, 1, 200)
    offset = _int_arg("offset", 0, 0, 100000)
    items = admin.scan_documents()
    if owner_id:
        items = [item for item in items if item.get("owner_id") == owner_id]
    if query:
        items = [
            item for item in items
            if query in str(item.get("display_name", "")).lower()
            or query in str(item.get("filename", "")).lower()
            or query in str(item.get("owner", "")).lower()
            or query in str(item.get("category", "")).lower()
            or any(query in str(tag).lower() for tag in item.get("tags") or [])
        ]
    return jsonify({"documents": items[offset:offset + limit],
                    "total": len(items)}), 200


@app.route("/api/admin/documents/<user_id>/<doc_id>", methods=["DELETE"])
@admin.admin_required
def admin_delete_document(user_id, doc_id):
    actor = request.user  # type: ignore[attr-defined]
    target = _admin_target(user_id)
    doc_id = _admin_id(doc_id)
    if target is None or not doc_id:
        return jsonify({"error": "文献不存在"}), 404
    directory = docs.user_dir(target["id"])
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文献不存在"}), 404

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if len(reason) < 2:
        return jsonify({"error": "请填写删除原因（至少 2 个字）"}), 400

    meta = admin.document_metadata(target, directory, info)
    _remove_document_files(directory, doc_id, info)
    admin.audit("admin.document.delete", actor, target_type="document",
                target_id=doc_id, target_name=meta["display_name"], reason=reason,
                detail={"owner_id": target["id"],
                        "owner": target.get("username", ""),
                        "filename": meta["filename"]})
    return jsonify({"success": True, "message": "文献已删除"}), 200


@app.route("/api/admin/documents/<user_id>/<doc_id>/view-grant",
           methods=["POST"])
@admin.admin_required
def admin_view_grant(user_id, doc_id):
    """处理举报时，按单篇签发短期查看授权；签发本身写入审计日志。"""
    actor = request.user  # type: ignore[attr-defined]
    target = _admin_target(user_id)
    doc_id = _admin_id(doc_id)
    if target is None or not doc_id:
        return jsonify({"error": "文献不存在"}), 404
    directory = docs.user_dir(target["id"])
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文献不存在"}), 404

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    report_ref = (data.get("report_ref") or "").strip()
    if len(reason) < 2:
        return jsonify({"error": "请填写核查事由（至少 2 个字）"}), 400

    meta = admin.document_metadata(target, directory, info)
    token = admin.issue_view_grant(actor, target, doc_id, reason, report_ref,
                                   target_name=meta["display_name"])
    return jsonify({
        "success": True,
        "token": token,
        "url": f"/api/admin/view?token={token}",
        "expires_in": config.ADMIN_VIEW_MAX_AGE,
        "document": meta,
    }), 200


@app.route("/api/admin/view", methods=["GET"])
def admin_view():
    """凭短期授权读取单一原始文件。每次实际访问都写审计。"""
    grant = admin.load_view_grant(request.args.get("token", ""))
    if not grant:
        return jsonify({"error": "查看授权无效或已过期", "code": "grant_invalid"}), 403

    actor = auth.get_user(_admin_id(grant.get("actor", "")))
    if actor is None or not auth.is_admin(actor) or auth.is_banned(actor):
        return jsonify({"error": "管理员身份已失效", "code": "grant_invalid"}), 403

    owner_id = _admin_id(grant.get("uid", ""))
    doc_id = _admin_id(grant.get("doc_id", ""))
    owner = auth.get_user(owner_id)
    if owner is None or not doc_id:
        return jsonify({"error": "文献不存在"}), 404
    directory = docs.user_dir(owner_id)
    info = docs.load_info(directory, doc_id)
    if info is None:
        return jsonify({"error": "文献不存在"}), 404
    path = docs.source_path_of(directory, info)
    if path is None:
        return jsonify({"error": "原始文件已丢失"}), 404

    admin.audit("admin.document.view", actor, target_type="document",
                target_id=doc_id,
                target_name=info.get("display_name")
                or info.get("original_filename", ""),
                reason=grant.get("reason", ""),
                detail={"owner_id": owner_id,
                        "owner": owner.get("username", ""),
                        "report_ref": grant.get("report_ref", ""),
                        "grant_iat": grant.get("iat", 0)})
    response = send_from_directory(str(path.parent), path.name, conditional=True)
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.route("/api/admin/audit", methods=["GET"])
@admin.admin_required
def admin_audit():
    limit = _int_arg("limit", 100, 1, 500)
    offset = _int_arg("offset", 0, 0, 100000)
    action = (request.args.get("action") or "").strip()[:64]
    entries, total = admin.read_audit(limit, offset, action)
    return jsonify({"entries": entries, "total": total}), 200


# ==================================================================== 启动
@app.before_request
def _block_unauthenticated_api():
    """除鉴权和健康检查外，所有 /api/ 都要求登录。"""
    if not request.path.startswith("/api/"):
        return None
    open_paths = ("/api/health", "/api/auth/login", "/api/auth/register",
                  "/api/auth/logout", "/api/auth/me", "/api/admin/view")
    if request.path in open_paths:
        return None
    if request.path.startswith("/api/documents/") and request.path.endswith("/file"):
        return None  # 该接口自己处理 ?token= 鉴权
    user = auth.current_user()
    if not user:
        return jsonify({"error": "请先登录", "code": "unauthorized"}), 401
    if auth.is_banned(user):
        return jsonify({"error": "账号已被封禁", "code": "banned"}), 403
    request.user = user
    return None


def warmup():
    print("=" * 60)
    print("智读小站后端服务")
    print(f"向量模型：{config.EMBEDDING_MODEL_NAME}")
    print(f"推理设备：{config.EMBEDDING_DEVICE}（auto = 有显卡就用显卡）")
    llm_status = get_llm().status()
    print(f"大模型：{llm_status['model']} — {'可用' if llm_status['available'] else '不可用：' + str(llm_status['error'])}")
    print("=" * 60)
    embedding_service.load()
    print(f"[模型] 就绪={embedding_service.ready} 设备={embedding_service.device} 错误={embedding_service.load_error}")
    reranker = get_reranker()
    print(f"[重排] 就绪={reranker.load()} 设备={reranker.device} 错误={reranker.error}")
    ocr_ready = ocr.load()
    print(f"[OCR] 就绪={ocr_ready} 引擎=RapidOCR 错误={ocr.status()['error']}")


if __name__ == "__main__":
    threading.Thread(target=warmup, daemon=True).start()
    app.run(debug=False, host="127.0.0.1", port=5000, threaded=True)

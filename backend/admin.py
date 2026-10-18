# -*- coding: utf-8 -*-
"""管理后台支撑：审计日志、管理员鉴权、举报核查授权。

隐私模型（项目硬性要求）：
- 管理员默认只能读文献元数据（标题、页数、大小、索引状态等），
  管理接口不返回正文、分块或文件路径。
- 只有针对单篇文献、填写举报编号/事由后签发的短期令牌才能读取原始文件；
  每次签发授权和每次实际访问都写入审计日志。
"""
import json
import os
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import auth
import config
import documents as docs

_audit_lock = threading.Lock()


# ------------------------------------------------------------------ 审计
def _rotate_audit() -> None:
    try:
        if (config.AUDIT_LOG_FILE.exists()
                and config.AUDIT_LOG_FILE.stat().st_size > config.AUDIT_MAX_BYTES):
            backup = config.AUDIT_LOG_FILE.with_suffix(".log.1")
            os.replace(config.AUDIT_LOG_FILE, backup)
    except OSError:
        pass


def _request_meta() -> Tuple[str, str]:
    try:
        return request.remote_addr or "", (request.headers.get("User-Agent") or "")[:200]
    except RuntimeError:  # 不在请求上下文里（命令行等）
        return "", ""


def audit(action: str, actor, *, target_type: str = "", target_id: str = "",
          target_name: str = "", reason: str = "",
          detail: Optional[Dict] = None) -> None:
    """追加一条审计记录。调用方不要传密码、正文或文件内容。"""
    if isinstance(actor, dict):
        actor_id, actor_name = actor.get("id", ""), actor.get("username", "")
    else:
        actor_id, actor_name = "", str(actor or "")
    ip, user_agent = _request_meta()
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "actor_id": actor_id,
        "actor": actor_name,
        "target_type": target_type,
        "target_id": target_id,
        "target": target_name,
        "reason": (reason or "")[:500],
        "detail": detail or {},
        "ip": ip,
        "ua": user_agent,
    }
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _audit_lock:
        _rotate_audit()
        try:
            with open(config.AUDIT_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            print(f"[审计] 写入失败：{exc}")


def read_audit(limit: int = 100, offset: int = 0,
               action: str = "") -> Tuple[List[Dict], int]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    if not config.AUDIT_LOG_FILE.exists():
        return [], 0
    entries: List[Dict] = []
    try:
        with open(config.AUDIT_LOG_FILE, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return [], 0
    prefix = (action or "").strip()
    if prefix:
        entries = [e for e in entries
                   if str(e.get("action", "")).startswith(prefix)]
    total = len(entries)
    entries.reverse()  # 最新的在前
    return entries[offset:offset + limit], total


# ------------------------------------------------------------------ 鉴权
def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = getattr(request, "user", None) or auth.current_user()
        if not user:
            return jsonify({"error": "请先登录", "code": "unauthorized"}), 401
        if auth.is_banned(user):
            return jsonify({"error": "账号已被封禁", "code": "banned"}), 403
        if not auth.is_admin(user):
            audit("admin.access.denied", user, target_type="endpoint",
                  target_id=request.path, target_name=request.path,
                  reason="非管理员访问管理接口")
            return jsonify({"error": "需要管理员权限", "code": "forbidden"}), 403
        request.user = user  # type: ignore[attr-defined]
        return view(*args, **kwargs)

    return wrapper


# ------------------------------------------------------------------ 举报核查授权
def _grant_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(auth.secret_key(), salt=config.ADMIN_VIEW_SALT)


def issue_view_grant(actor: Dict, target_user: Dict, doc_id: str,
                     reason: str, report_ref: str = "",
                     target_name: str = "") -> str:
    token = _grant_serializer().dumps({
        "uid": target_user.get("id", ""),
        "doc_id": doc_id,
        "actor": actor.get("id", ""),
        "reason": (reason or "")[:300],
        "report_ref": (report_ref or "")[:120],
        "iat": int(time.time()),
    })
    audit("admin.document.view_grant", actor, target_type="document",
          target_id=doc_id, target_name=target_name, reason=reason,
          detail={"owner_id": target_user.get("id", ""),
                  "owner": target_user.get("username", ""),
                  "report_ref": report_ref})
    return token


def load_view_grant(token: Optional[str]) -> Optional[Dict]:
    if not token:
        return None
    try:
        return _grant_serializer().loads(token, max_age=config.ADMIN_VIEW_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


# ------------------------------------------------------------------ 元数据
def user_stats(user_id: str) -> Dict:
    directory = config.UPLOAD_ROOT / user_id
    count = 0
    storage = 0
    latest = ""
    if directory.is_dir():
        for path in directory.iterdir():
            if not path.is_file():
                continue
            try:
                storage += path.stat().st_size
            except OSError:
                pass
            if path.name.endswith("_info.json"):
                count += 1
                try:
                    info = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                latest = max(latest, info.get("upload_time", ""))
    return {"document_count": count, "storage_bytes": storage, "last_upload": latest}


def sanitize_user(user: Dict, stats: Optional[Dict] = None) -> Dict:
    data = {
        "id": user.get("id", ""),
        "username": user.get("username", ""),
        "role": user.get("role", "user"),
        "banned": bool(user.get("banned")),
        "created_at": user.get("created_at", ""),
        "banned_at": user.get("banned_at", ""),
        "ban_reason": user.get("ban_reason", ""),
        "banned_by": user.get("banned_by", ""),
    }
    if stats:
        data.update(stats)
    return data


def document_metadata(owner: Dict, directory: Path, info: Dict) -> Dict:
    source = docs.source_path_of(directory, info)
    size = 0
    if source is not None:
        try:
            size = source.stat().st_size
        except OSError:
            pass
    filename = info.get("original_filename") or info.get("filename", "未命名")
    return {
        "id": info.get("id", ""),
        "filename": filename,
        "display_name": info.get("display_name") or filename,
        "category": info.get("category", ""),
        "tags": info.get("tags") or [],
        "source_type": info.get("source_type", "pdf"),
        "unit": info.get("unit", "页"),
        "page_count": info.get("page_count", 0),
        "chunk_count": info.get("chunk_count", 0),
        "ocr_pages": info.get("ocr_pages", 0),
        "index_status": docs.index_status_of(info),
        "upload_time": info.get("upload_time", ""),
        "size_bytes": size,
        "owner_id": owner.get("id", ""),
        "owner": owner.get("username", ""),
    }


def scan_documents() -> List[Dict]:
    """扫描所有用户目录，只返回元数据，不读正文。"""
    users = {user.get("id", ""): user for user in auth.all_users()}
    items: List[Dict] = []
    if not config.UPLOAD_ROOT.is_dir():
        return items
    for directory in config.UPLOAD_ROOT.iterdir():
        if not directory.is_dir():
            continue
        owner = users.get(directory.name)
        if owner is None:
            continue
        for info_file in directory.glob("*_info.json"):
            doc_id = info_file.name[:-len("_info.json")]
            info = docs.load_info(directory, doc_id)
            if info is None:
                continue
            items.append(document_metadata(owner, directory, info))
    items.sort(key=lambda item: item.get("upload_time", ""), reverse=True)
    return items

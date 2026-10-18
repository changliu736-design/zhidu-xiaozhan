# -*- coding: utf-8 -*-
"""账号与会话：bcrypt 存密码 + itsdangerous 签名令牌。

依赖全部是本机已装好的（bcrypt / itsdangerous 随 Flask 一起来），不新增任何包。

会话双通道：
  - HttpOnly Cookie（页面由后端同一个源提供时最稳）
  - Authorization: Bearer <token>（直接从文件打开 index.html 时也能用）
"""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime
from functools import wraps
from typing import Dict, Optional

import bcrypt
from flask import jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config

_lock = threading.RLock()
# 用户名只能是字母和数字，2~32 位（不允许中文、空格、下划线、点、横线等任何其它字符）。
# 注意：这里只约束"注册"；登录不校验格式，所以历史上用中文注册的老账号仍可正常登录。
_USERNAME_RE = re.compile(r"^[A-Za-z0-9]{2,32}$")


# ------------------------------------------------------------------ 存储
def _normalize(user: Dict) -> Dict:
    """老账号没有 role/banned 字段，按普通未封禁用户处理。"""
    user.setdefault("role", "user")
    user.setdefault("banned", False)
    return user


def _load_users() -> Dict[str, Dict]:
    if not config.USERS_FILE.exists():
        return {}
    try:
        with open(config.USERS_FILE, "r", encoding="utf-8") as handle:
            users = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}
    return {name: _normalize(user) for name, user in users.items()}


def _save_users(users: Dict[str, Dict]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.USERS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(users, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, config.USERS_FILE)


def _secret_key() -> bytes:
    if config.SECRET_FILE.exists():
        return config.SECRET_FILE.read_bytes()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(48)
    config.SECRET_FILE.write_bytes(key)
    try:
        os.chmod(config.SECRET_FILE, 0o600)
    except OSError:
        pass
    return key


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt=config.SESSION_SALT)


def secret_key() -> bytes:
    """给管理后台的短期查看令牌复用同一份服务端密钥。"""
    return _secret_key()


# ------------------------------------------------------------------ 密码
def _prehash(password: str) -> bytes:
    """bcrypt 只取前 72 字节，先做一次 SHA-256 再 base64，长密码也不会被截断。"""
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------ 用户
class AuthError(ValueError):
    pass


def count_users() -> int:
    return len(_load_users())


def get_user(user_id: str) -> Optional[Dict]:
    for user in _load_users().values():
        if user.get("id") == user_id:
            return user
    return None


def create_user(username: str, password: str, role: str = "user") -> Dict:
    if role not in ("user", "admin"):
        raise AuthError("未知账号角色")
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        if re.search(r"[\u4e00-\u9fff]", username):
            raise AuthError("用户名不能使用中文，只能由字母和数字组成")
        raise AuthError("用户名只能由字母和数字组成，长度 2~32 位")
    if len(password or "") < config.MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {config.MIN_PASSWORD_LEN} 位")

    with _lock:
        users = _load_users()
        if username.lower() in {name.lower() for name in users}:
            raise AuthError("该用户名已被注册")
        user = {
            "id": "u_" + uuid.uuid4().hex[:12],
            "username": username,
            "password": hash_password(password),
            "role": role,
            "banned": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        users[username] = user
        _save_users(users)
        return user


def create_admin(username: str, password: str) -> Dict:
    """命令行用：不存在则创建管理员，已存在则提升为管理员。"""
    username = (username or "").strip()
    for name, record in _load_users().items():
        if name.lower() == username.lower():
            with _lock:
                users = _load_users()
                current = next((u for u in users.values()
                                if u.get("id") == record.get("id")), None)
                if current is None:
                    raise AuthError("账号不存在")
                current["role"] = "admin"
                _save_users(users)
                return current
    return create_user(username, password, role="admin")


def all_users() -> list:
    return list(_load_users().values())


def is_admin(user: Optional[Dict]) -> bool:
    return bool(user and user.get("role") == "admin")


def is_banned(user: Optional[Dict]) -> bool:
    return bool(user and user.get("banned"))


def set_banned(user_id: str, banned: bool, reason: str = "",
               actor: str = "") -> Dict:
    with _lock:
        users = _load_users()
        record = next((u for u in users.values() if u.get("id") == user_id), None)
        if record is None:
            raise AuthError("用户不存在")
        record["banned"] = bool(banned)
        if banned:
            record["banned_at"] = datetime.now().isoformat(timespec="seconds")
            record["ban_reason"] = (reason or "")[:500]
            record["banned_by"] = actor
        else:
            record.pop("banned_at", None)
            record.pop("ban_reason", None)
            record.pop("banned_by", None)
        _save_users(users)
        return record


def authenticate(username: str, password: str) -> Optional[Dict]:
    user = _load_users().get((username or "").strip())
    if not user:
        # 用户不存在时也走一次哈希校验，避免通过响应时间探测用户名是否存在
        bcrypt.checkpw(b"x" * 32, bcrypt.hashpw(b"y" * 32, bcrypt.gensalt()))
        return None
    if not verify_password(password or "", user["password"]):
        return None
    return user


def change_password(user: Dict, old_password: str, new_password: str) -> Dict:
    """修改密码。改完会把之前签发的所有令牌作废（见 user_from_token）。"""
    if not verify_password(old_password or "", user["password"]):
        raise AuthError("原密码不正确")
    if len(new_password or "") < config.MIN_PASSWORD_LEN:
        raise AuthError(f"新密码至少 {config.MIN_PASSWORD_LEN} 位")
    if new_password == (old_password or ""):
        raise AuthError("新密码不能和原密码相同")

    with _lock:
        users = _load_users()
        record = users.get(user["username"])
        if record is None:
            raise AuthError("账号不存在")
        record["password"] = hash_password(new_password)
        # 记下改密时间：早于这个时间签发的令牌一律失效
        record["pwd_changed_at"] = int(time.time())
        _save_users(users)
        return record


# ------------------------------------------------------------------ 令牌
def issue_token(user: Dict) -> str:
    return _serializer().dumps({
        "uid": user["id"],
        "username": user["username"],
        "iat": int(time.time()),   # 签发时间，用于判断是否早于改密时间
    })


def user_from_token(token: Optional[str]) -> Optional[Dict]:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = get_user(data.get("uid", ""))
    if user is None:
        return None
    # 改过密码之后，之前签发的令牌全部失效（改密的那台设备会拿到新令牌，不受影响）
    changed_at = user.get("pwd_changed_at")
    if changed_at and data.get("iat", 0) < changed_at:
        return None
    return user


def current_user() -> Optional[Dict]:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        user = user_from_token(header[7:].strip())
        if user:
            return user
    return user_from_token(request.cookies.get(config.SESSION_COOKIE))


def attach_session(response, token: str):
    response.set_cookie(
        config.SESSION_COOKIE,
        token,
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="Lax",
    )
    return response


def clear_session(response):
    response.delete_cookie(config.SESSION_COOKIE)
    return response


def public_user(user: Dict) -> Dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user.get("role", "user"),
        "created_at": user.get("created_at", ""),
    }


# ------------------------------------------------------------------ 装饰器
def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "请先登录", "code": "unauthorized"}), 401
        if is_banned(user):
            return jsonify({"error": "账号已被封禁", "code": "banned"}), 403
        request.user = user  # type: ignore[attr-defined]
        return view(*args, **kwargs)

    return wrapper

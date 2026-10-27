"""按用户、按自然日统计 AI 调用次数。

记录写在各用户目录下的 quota.json，形如：
    {"date": "2026-10-27", "counts": {"ask": 3, "translate": 1}}

日期一变就自动清零，不需要额外的定时任务。次数只在真正要花云端
API 额度时才记（本地跳页、标记这类指令不计数）。
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Dict, Tuple

import config

# kind -> config 里对应的额度配置项
_LIMITS = {
    "ask": "DAILY_ASK_LIMIT",
    "translate": "DAILY_TRANSLATE_LIMIT",
    "summary": "DAILY_SUMMARY_LIMIT",
    "library": "DAILY_LIBRARY_LIMIT",
    "selection": "DAILY_SELECTION_LIMIT",
}

_NAMES = {
    "ask": "文献问答",
    "translate": "翻译",
    "summary": "摘要",
    "library": "全库问答",
    "selection": "划词解读",
}

_LOCK = threading.Lock()


def _path(directory: Path) -> Path:
    return Path(directory) / "quota.json"


def _today() -> str:
    return date.today().isoformat()


def limit_of(kind: str) -> int:
    """返回该功能的每日上限；0 或负数表示不限制。"""
    attr = _LIMITS.get(kind)
    if not attr:
        return 0
    try:
        return max(0, int(getattr(config, attr, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _read(directory: Path) -> Dict[str, int]:
    try:
        data = json.loads(_path(directory).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("date") != _today():
        return {}
    counts = data.get("counts")
    if not isinstance(counts, dict):
        return {}
    clean: Dict[str, int] = {}
    for key, value in counts.items():
        try:
            clean[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return clean


def _write(directory: Path, counts: Dict[str, int]) -> None:
    payload = {"date": _today(), "counts": counts}
    try:
        _path(directory).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
        )
    except OSError as exc:  # 额度记不下来也不能挡住用户使用
        print(f"[额度] 写入失败：{exc}")


def used(directory: Path, kind: str) -> int:
    return int(_read(directory).get(kind, 0))


def consume(directory: Path, kind: str, amount: int = 1) -> Tuple[bool, int, int]:
    """占用一次额度。返回 (是否放行, 已用次数, 上限)。"""
    limit = limit_of(kind)
    if limit <= 0:
        return True, used(directory, kind), 0
    with _LOCK:
        counts = _read(directory)
        current = int(counts.get(kind, 0))
        if current + amount > limit:
            return False, current, limit
        counts[kind] = current + amount
        _write(directory, counts)
        return True, counts[kind], limit


def refund(directory: Path, kind: str, amount: int = 1) -> None:
    """调用彻底失败（比如模型报错）时把次数退回去，别让用户白扣。"""
    if limit_of(kind) <= 0:
        return
    with _LOCK:
        counts = _read(directory)
        current = int(counts.get(kind, 0))
        if current <= 0:
            return
        counts[kind] = max(0, current - amount)
        _write(directory, counts)


def snapshot(directory: Path) -> Dict[str, Dict]:
    """给前端展示用的额度概况。"""
    result: Dict[str, Dict] = {}
    for kind in _LIMITS:
        limit = limit_of(kind)
        current = used(directory, kind)
        result[kind] = {
            "name": _NAMES.get(kind, kind),
            "used": current,
            "limit": limit,
            "remaining": max(0, limit - current) if limit > 0 else -1,
        }
    return result


def message(kind: str, limit: int) -> str:
    name = _NAMES.get(kind, "AI 调用")
    return (
        f"今天的{name}次数已经用完了（每天 {limit} 次），明天 0 点自动恢复。"
        "如果确实需要更多，可以联系管理员调整额度。"
    )

# -*- coding: utf-8 -*-
"""图片型 PDF 的 OCR 支持：RapidOCR（ONNX Runtime，CPU）延迟加载。

只在页面文字过少且含图片时调用，普通文字 PDF 完全不受影响。
模型随 rapidocr 包一起安装，运行时不需要联网下载。
"""
import importlib.util
import logging
import threading
from typing import List, Optional

import config

_lock = threading.Lock()
_infer_lock = threading.Lock()  # ONNX 会话不适合并发推理，串行化
_engine = None
_checked = False
_error: Optional[str] = None


class OCRUnavailable(RuntimeError):
    pass


def available() -> tuple:
    """快速判断能否使用 OCR，不加载模型。返回 (bool, 原因)。"""
    if not config.OCR_ENABLED:
        return False, "OCR 已关闭（OCR_ENABLED=0）"
    if importlib.util.find_spec("rapidocr") is None:
        return False, "未安装 rapidocr，请先在 backend 目录执行 pip install -r requirements.txt"
    return True, None


def load() -> bool:
    """加载 RapidOCR；失败后不反复重试，错误通过 status() 暴露。"""
    global _engine, _checked, _error
    with _lock:
        if _engine is not None:
            return True
        if _checked:
            return False
        _checked = True
        ok, reason = available()
        if not ok:
            _error = reason
            return False
        try:
            from rapidocr import RapidOCR

            # 默认 INFO 日志会刷屏，交给本项目自己的进度输出
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            _engine = RapidOCR(params={"Global.log_level": "warning"})
            _error = None
            return True
        except Exception as exc:  # noqa: BLE001
            _error = f"{type(exc).__name__}: {exc}"
            return False


def status() -> dict:
    return {
        "enabled": config.OCR_ENABLED,
        "installed": importlib.util.find_spec("rapidocr") is not None,
        "loaded": _engine is not None,
        "engine": "RapidOCR / ONNX Runtime",
        "device": "cpu",
        "error": _error,
    }


def _lines_from_result(result) -> List[str]:
    """兼容 RapidOCR 新旧两种返回结构，按行返回识别文本。"""
    txts = scores = None

    if isinstance(result, tuple):  # 旧版： ([[box, text, score], ...], elapse)
        rows = result[0] or []
        pairs = []
        for row in rows:
            try:
                pairs.append((str(row[1]), float(row[2])))
            except (IndexError, TypeError, ValueError):
                continue
        txts = [text for text, _ in pairs]
        scores = [score for _, score in pairs]
    else:
        txts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)

    if not txts:
        return []

    lines: List[str] = []
    for index, text in enumerate(txts):
        text = str(text or "").strip()
        if not text:
            continue
        if scores is not None and index < len(scores):
            try:
                if float(scores[index]) < config.OCR_SCORE_THRESHOLD:
                    continue
            except (TypeError, ValueError):
                pass
        lines.append(text)
    return lines


def recognize(image) -> str:
    """识别一张图片（numpy RGB 数组或图片字节），返回按行拼接的文本。"""
    if not load():
        raise OCRUnavailable(_error or "OCR 不可用")
    with _infer_lock:
        result = _engine(image)
    return "\n".join(_lines_from_result(result))

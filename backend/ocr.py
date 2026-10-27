# -*- coding: utf-8 -*-
"""图片型 PDF 的 OCR：调用云端多模态模型（阿里云百炼 qwen-vl-ocr）。

只在页面文字过少且含图片时调用，普通文字 PDF 完全不受影响。
不装本地推理框架（不需要 rapidocr / onnxruntime），2核2G 服务器也能跑；
扫描件那一页会被渲染成 PNG、以 base64 传给模型，识别结果按行返回。

隐私提示：启用 OCR 后，被判定为扫描页的那几页图片会上传到百炼。
纯文字 PDF 不会上传任何图片。
"""
import base64
import re
import threading
from typing import Optional

import config

_lock = threading.Lock()
_infer_lock = threading.Lock()  # 串行调用，避免一页里并发打接口
_client = None
_checked = False
_error: Optional[str] = None

# 只让模型干「认字」这一件事：不要它解释、不要 markdown、不要补内容
OCR_PROMPT = (
    "请把这张图片里的文字按自然阅读顺序原样输出。\n"
    "要求：\n"
    "1. 只输出图片里真实存在的文字，不要解释、不要总结、不要加任何前后缀；\n"
    "2. 不要用 markdown 代码块包裹，不要加「以下是识别结果」这类话；\n"
    "3. 表格按行输出，用竖线 | 分隔单元格；\n"
    "4. 公式保留原样（可以用 $...$ 包裹）；\n"
    "5. 如果这一页确实没有文字，只输出：NO_TEXT"
)


class OCRUnavailable(RuntimeError):
    pass


def _api_key() -> str:
    return (getattr(config, "OCR_API_KEY", "") or "").strip()


def available() -> tuple:
    """快速判断能否使用 OCR，不发网络请求。返回 (bool, 原因)。"""
    if not config.OCR_ENABLED:
        return False, "OCR 已关闭（OCR_ENABLED=0）"
    if not _api_key():
        return False, "没有配置 OCR_API_KEY（可直接复用百炼的 key），扫描件无法识别"
    return True, None


def load() -> bool:
    """建立云端客户端（不联网）；失败后不反复重试，错误通过 status() 暴露。"""
    global _client, _checked, _error
    with _lock:
        if _client is not None:
            return True
        if _checked:
            return False
        _checked = True
        ok, reason = available()
        if not ok:
            _error = reason
            return False
        try:
            from openai import OpenAI

            _client = OpenAI(
                api_key=_api_key(),
                base_url=config.OCR_API_BASE_URL,
                timeout=config.OCR_API_TIMEOUT,
                max_retries=0,  # 重试自己控制，方便打印真实错误
            )
            _error = None
            return True
        except Exception as exc:  # noqa: BLE001
            _error = f"{type(exc).__name__}: {exc}"
            return False


def status() -> dict:
    return {
        "enabled": config.OCR_ENABLED,
        "provider": "api",
        "mode": "api",
        "engine": f"云端多模态 {config.OCR_API_MODEL}",
        "model": config.OCR_API_MODEL,
        "endpoint": config.OCR_API_BASE_URL,
        "installed": bool(_api_key()),  # 有 key 就算「装好了」，不再依赖本地包
        "loaded": _client is not None,
        "device": "cloud",
        "error": _error,
    }


def _to_png_bytes(image) -> bytes:
    """把调用方给的东西统一成 PNG 字节。"""
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    if isinstance(image, str):
        with open(image, "rb") as fh:
            return fh.read()
    tobytes = getattr(image, "tobytes", None)
    if callable(tobytes):  # PIL.Image
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    raise TypeError("OCR 只接受 PNG/JPEG 字节、图片路径或 PIL 图片")


def _clean(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    if text.upper().startswith("NO_TEXT") or text in {"无文字", "（无文字）", "图片中没有文字"}:
        return ""
    # 模型偶尔会自己补一行 markdown 表格分隔线（| --- | --- |），对阅读没用，去掉
    lines = [line for line in text.splitlines()
             if not re.fullmatch(r"[\s|:\-]+", line)]
    return "\n".join(lines).strip()


def _call_once(png: bytes) -> str:
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    response = _client.chat.completions.create(
        model=config.OCR_API_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": uri}},
            ],
        }],
        temperature=0.01,
    )
    if not response.choices:
        return ""
    return _clean(response.choices[0].message.content or "")


def recognize(image) -> str:
    """识别一页图片（PNG/JPEG 字节、路径或 PIL 图片），返回纯文本。"""
    if not load():
        raise OCRUnavailable(_error or "OCR 不可用")

    png = _to_png_bytes(image)
    if not png:
        return ""

    attempts = max(1, int(config.OCR_API_RETRIES))
    last_error: Optional[BaseException] = None
    with _infer_lock:
        for attempt in range(attempts):
            try:
                return _call_once(png)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt + 1 < attempts:
                    import time

                    time.sleep(min(2.0 ** attempt, 6.0))
    raise OCRUnavailable(
        f"云端 OCR 调用失败（已重试 {attempts} 次）：{type(last_error).__name__}: {last_error}"
    )

# -*- coding: utf-8 -*-
"""一键自检：确认 .env 里配的模型接口都能通。

用法（在 backend 目录下）：
    python check_api.py

依次测四个接口：
    1. 生成模型 —— 回答问题时用（DeepSeek 等）
    2. 向量模型 —— 上传文献建索引时用（阿里云百炼 text-embedding-v3）
    3. 重排模型 —— 检索后精排（阿里云百炼 gte-rerank-v2，可选）
    4. 扫描件识别 —— 图片型 PDF 才用到（阿里云百炼 qwen-vl-ocr，可选）

每项几厘钱，跑完会打印 ✓ / ✗ 和耗时。
哪项没过，按它的提示改 backend/.env，再跑一次即可。
"""
import sys
import time
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent.parent


def _ok(message: str) -> None:
    print(f"  [通过] {message}")


def _bad(message: str) -> None:
    print(f"  [失败] {message}")


# ------------------------------------------------------------------ 1. 生成模型
def check_llm() -> bool:
    print("\n[1/4] 生成模型（回答问题用）")
    print(f"  接口：{config.OPENAI_BASE_URL}")
    print(f"  模型：{config.OPENAI_MODEL}")
    if not config.OPENAI_API_KEY:
        _bad("没有配置 OPENAI_API_KEY")
        return False

    from openai import OpenAI
    client = OpenAI(api_key=config.OPENAI_API_KEY,
                    base_url=config.OPENAI_BASE_URL, timeout=30)
    started = time.time()
    try:
        resp = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "user", "content": "只回复两个字：收到"}],
            max_tokens=16,
        )
    except Exception as exc:  # noqa: BLE001
        _bad(f"{type(exc).__name__}: {exc}")
        return False

    reply = (resp.choices[0].message.content or "").strip()
    _ok(f"用时 {time.time() - started:.1f}s，回复：{reply[:30]}")
    return True


# ------------------------------------------------------------------ 2. 向量模型
def check_embedding() -> bool:
    print("\n[2/4] 向量模型（上传文献建索引用）")
    print(f"  模式：{config.EMBEDDING_MODE}")
    print(f"  模型：{config.EMBEDDING_MODEL_NAME}")
    if config.EMBEDDING_MODE == "api":
        print(f"  接口：{config.EMBEDDING_API_BASE_URL}")

    import embeddings
    started = time.time()
    try:
        vector = embeddings.service.encode_query("人工智能在教育中的应用")
    except Exception as exc:  # noqa: BLE001
        _bad(f"{type(exc).__name__}: {exc}")
        return False

    elapsed = time.time() - started
    flat = vector.reshape(-1)
    _ok(f"用时 {elapsed:.1f}s，维度 {flat.shape[0]}，"
        f"前三个数 [{flat[0]:.3f}, {flat[1]:.3f}, {flat[2]:.3f}]")

    # 归一化检查：faiss 用内积当余弦相似度，向量必须是单位长度
    import numpy as np
    norm = float(np.linalg.norm(flat))
    if abs(norm - 1.0) > 0.01:
        _bad(f"向量长度是 {norm:.3f}，不是 1 —— 归一化没生效，检索会不准")
        return False
    return True


# ------------------------------------------------------------------ 3. 重排模型
def check_rerank() -> bool:
    print("\n[3/4] 重排模型（检索后精排，可选）")
    import reranker
    service = reranker.get_reranker()
    service.load()
    status = service.status()
    print(f"  模式：{status['mode']}")
    print(f"  模型：{status['name']}")

    if not status["enabled"] or status["mode"] == "off":
        print(f"  [跳过] 未启用（{status.get('error') or '配置关闭'}）"
              f" —— 不影响问答，只是排序会弱一点")
        return True

    # 两条候选：一条相关、一条无关，看它能不能把相关的排前面
    passages = [
        {"text": "深度学习在图像识别领域取得了显著进展。", "score": 0.50},
        {"text": "今天天气不错，适合出去散步。", "score": 0.70},
    ]
    started = time.time()
    try:
        ranked = service.rerank("深度学习在图像识别上的进展", passages, 2)
    except Exception as exc:  # noqa: BLE001
        _bad(f"{type(exc).__name__}: {exc}")
        return False

    after = service.status()
    if after.get("error"):
        _bad(after["error"])
        return False

    top = ranked[0]["text"] if ranked else ""
    elapsed = time.time() - started
    if "深度学习" in top:
        _ok(f"用时 {elapsed:.1f}s，相关片段排在第 1 位")
        return True
    _bad(f"用时 {elapsed:.1f}s，但无关片段排在前面 —— 检查 RERANKER_API_MODEL 是否写对")
    return False


# ------------------------------------------------------------------ 4. 扫描件识别
def check_ocr() -> bool:
    print("\n[4/4] 扫描件识别（图片型 PDF 才用到，可选）")
    import ocr

    status = ocr.status()
    print(f"  引擎：{status['engine']}")
    ok, reason = ocr.available()
    if not ok:
        print(f"  [跳过] {reason} —— 有文字层的 PDF 不受影响")
        return True

    sample = ROOT / "samples" / "lightrag_demo.pdf"
    if not sample.exists():
        print(f"  [跳过] 没找到示例文件 {sample}，没法实测")
        return True

    # 示例论文的第 8 页是纯图片扫描页，正好拿来试
    import fitz

    with fitz.open(sample) as doc:
        index = min(7, doc.page_count - 1)
        png = doc.load_page(index).get_pixmap(dpi=config.OCR_DPI).tobytes("png")
    print(f"  测试图片：{sample.name} 第 {index + 1} 页（{len(png) // 1024} KB）")

    started = time.time()
    try:
        text = ocr.recognize(png)
    except Exception as exc:  # noqa: BLE001
        _bad(f"{type(exc).__name__}: {exc}")
        return False

    elapsed = time.time() - started
    if not text.strip():
        _bad(f"用时 {elapsed:.1f}s，接口通了但没识别出文字 —— 检查 OCR_API_MODEL 是否是视觉模型")
        return False
    preview = " / ".join(text.splitlines()[:2])[:60]
    _ok(f"用时 {elapsed:.1f}s，识别出 {len(text)} 字：{preview}")
    return True


def main() -> int:
    print("=" * 58)
    print("智读小站 —— 模型接口自检")
    print("=" * 58)

    results = [check_llm(), check_embedding(), check_rerank(), check_ocr()]

    print("\n" + "=" * 58)
    if all(results):
        print("全部通过，可以启动服务了：python app.py")
        return 0
    print("有项目没通过，按上面的提示改 backend/.env，再跑一次这个脚本。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

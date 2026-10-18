# -*- coding: utf-8 -*-
"""向量模型服务：全项目共用同一个 Qwen3-Embedding-0.6B 实例。

修复的原始问题：
  - app.py 与 ai_functions.py 各自 `SentenceTransformer(...)`，模型被加载两份；
  - 加载失败只 print 一行，然后静默退化成"取前 5 段"，用户完全无感。

现在：单例 + 显式状态，加载失败会带明确错误信息抛出。
"""
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

import config

_lock = threading.Lock()


class EmbeddingUnavailable(RuntimeError):
    """模型不可用时抛出，由上层转成明确的错误响应，而不是悄悄降级。"""


class EmbeddingService:
    def __init__(self) -> None:
        self.model = None
        self.model_path: Optional[str] = None
        self.device: str = "cpu"
        self.dim: Optional[int] = None
        self.load_error: Optional[str] = None
        self.load_seconds: float = 0.0

    # ------------------------------------------------------------ 加载
    def load(self) -> None:
        if self.model is not None or self.load_error is not None:
            return
        with _lock:
            if self.model is not None or self.load_error is not None:
                return
            started = time.time()
            try:
                import torch  # noqa: F401  (必须在 sentence_transformers 之前导入)
                from sentence_transformers import SentenceTransformer

                # CPU 推理时把线程数收敛到物理核附近，避免超线程互相抢资源
                try:
                    import os as _os
                    logical = _os.cpu_count() or 4
                    torch.set_num_threads(max(2, min(16, logical // 2)))
                except Exception:
                    pass

                device = config.EMBEDDING_DEVICE
                if device == "auto":
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                self.device = device

                self.model_path = config.resolve_local_model()
                print(f"[模型] 正在加载 {config.EMBEDDING_MODEL_NAME}")
                print(f"[模型] 本地路径 {self.model_path}")
                print(f"[模型] 推理设备 {device}")

                model = SentenceTransformer(self.model_path, device=device)
                model.max_seq_length = config.EMBEDDING_MAX_SEQ_LENGTH
                # 新版 sentence-transformers 改了这个方法名，兼容两种写法
                getter = (getattr(model, "get_embedding_dimension", None)
                          or model.get_sentence_embedding_dimension)
                self.dim = getter()
                self.model = model
                self.load_seconds = time.time() - started
                print(f"[模型] 加载成功，维度 {self.dim}，耗时 {self.load_seconds:.1f}s")
            except Exception as exc:  # noqa: BLE001
                self.load_error = f"{type(exc).__name__}: {exc}"
                print(f"[模型] 加载失败：{self.load_error}", file=sys.stderr)

    def ensure_loaded(self) -> None:
        self.load()
        if self.model is None:
            raise EmbeddingUnavailable(
                f"语义模型未能加载（{self.load_error}）。"
                f"请确认本机 HuggingFace 缓存中存在 {config.EMBEDDING_MODEL_NAME}。"
            )

    # ------------------------------------------------------------ 编码
    @property
    def ready(self) -> bool:
        return self.model is not None

    def encode_documents(
        self,
        texts: List[str],
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> np.ndarray:
        """文档向量（不加指令前缀，符合 Qwen3-Embedding 官方用法）。"""
        return self._encode(texts, progress_cb=progress_cb)

    def encode_query(self, text: str) -> np.ndarray:
        """查询向量，使用模型自带的 query 指令前缀，跨语言检索效果明显更好。"""
        return self._encode([text], is_query=True)

    def _encode(
        self,
        texts: List[str],
        is_query: bool = False,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> np.ndarray:
        self.ensure_loaded()
        assert self.model is not None

        kwargs = {"normalize_embeddings": True, "show_progress_bar": False}
        if is_query:
            try:
                return self.model.encode(
                    texts, prompt_name="query",
                    batch_size=config.EMBEDDING_BATCH_SIZE, **kwargs
                )
            except Exception:
                pass  # 模型没有配 prompt，退回手动前缀

        # 手动分批，便于上报进度（上传大文献时前端能看到进度）
        out: List[np.ndarray] = []
        total = len(texts)
        bs = config.EMBEDDING_BATCH_SIZE
        for start in range(0, total, bs):
            batch = texts[start:start + bs]
            out.append(self._encode_batch(batch, bs, kwargs))
            if progress_cb:
                progress_cb(min(start + bs, total), total)
        if not out:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        return np.vstack(out).astype(np.float32)

    def _encode_batch(self, batch: List[str], batch_size: int, kwargs: dict) -> np.ndarray:
        """单批编码；显存不足时自动减半重试，避免 8GB 显卡被顶爆后整个任务失败。"""
        size = batch_size
        while True:
            try:
                assert self.model is not None
                return self.model.encode(batch, batch_size=size, **kwargs)
            except Exception as exc:  # noqa: BLE001
                message = str(exc).lower()
                if "out of memory" in message and size > 1:
                    size = max(1, size // 2)
                    print(f"[模型] 显存不足，批次降到 {size} 重试")
                    try:
                        import torch
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                    continue
                raise

    # ------------------------------------------------------------ 状态
    def status(self) -> Dict:
        return {
            "name": config.EMBEDDING_MODEL_NAME,
            "loaded": self.ready,
            "dim": self.dim,
            "device": self.device,
            "max_seq_length": config.EMBEDDING_MAX_SEQ_LENGTH,
            "load_seconds": round(self.load_seconds, 1),
            "error": self.load_error,
        }


service = EmbeddingService()

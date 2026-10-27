# -*- coding: utf-8 -*-
"""向量服务：文档切块 → 向量，供 faiss 建立检索索引。

两种运行方式（由 config.EMBEDDING_PROVIDER 决定）：

  api   —— 调云端 embedding 接口（默认阿里云百炼 compatible-mode，走 OpenAI 兼容协议）。
           2核2G 服务器必须用这个：本地模型光加载就要 1.5G+ 内存，机器直接扛不住，
           而且 2 核 CPU 给一篇论文建索引要十几分钟。
  local —— 本地 sentence-transformers 加载 Qwen3-Embedding-0.6B（需要 torch）。

历史背景（保留，避免后人踩回去）：
  - app.py 与 ai_functions.py 曾各自 `SentenceTransformer(...)`，模型被加载两份；
  - 加载失败只 print 一行，然后静默退化成"取前 5 段"，用户完全无感。

现在：单例 + 显式状态，加载失败会带明确错误信息抛出（不允许静默降级）。
云端模式下网络抖动会在内部重试，重试耗尽才报错。
"""
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

import config

_lock = threading.Lock()


class EmbeddingUnavailable(RuntimeError):
    """模型/接口不可用时抛出，由上层转成明确的错误响应，而不是悄悄降级。"""


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化，让 faiss 的内积等价于余弦相似度。

    本地模型内部已经做了归一化（normalize_embeddings=True），云端接口返回的是
    原始向量，必须手动归一化。否则两种模式算出来的相似度尺度不一致，检索排序会变形，
    而且 faiss 的 IndexFlatIP 只有在向量归一化之后才等价于余弦相似度。
    """
    if not matrix.size:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    return (matrix / norms).astype(np.float32)


class EmbeddingService:
    def __init__(self) -> None:
        self.model = None            # local 模式：SentenceTransformer 实例
        self._client = None          # api 模式：OpenAI 客户端
        self.mode: str = config.EMBEDDING_MODE
        self.model_path: Optional[str] = None
        self.device: str = "cpu"
        self.dim: Optional[int] = None
        self.load_error: Optional[str] = None
        self.load_seconds: float = 0.0

    # ------------------------------------------------------------ 加载
    def load(self) -> None:
        if self.ready or self.load_error is not None:
            return
        with _lock:
            if self.ready or self.load_error is not None:
                return
            started = time.time()
            try:
                if self.mode == "api":
                    self._load_api()
                else:
                    self._load_local()
                self.load_seconds = time.time() - started
                print(f"[向量] 就绪（{self.mode} 模式），耗时 {self.load_seconds:.1f}s")
            except Exception as exc:  # noqa: BLE001
                # 自己抛的 EmbeddingUnavailable 已经是写给人看的话，不要再套一层类名
                if isinstance(exc, EmbeddingUnavailable):
                    self.load_error = str(exc)
                else:
                    self.load_error = f"{type(exc).__name__}: {exc}"
                print(f"[向量] 初始化失败：{self.load_error}", file=sys.stderr)

    def _load_api(self) -> None:
        """初始化云端客户端。这里刻意不发网络请求，避免启动时被网络卡住。"""
        if not config.EMBEDDING_API_KEY:
            raise EmbeddingUnavailable(
                "向量服务设为 api，但没有配置 EMBEDDING_API_KEY（或 OPENAI_API_KEY）")
        from openai import OpenAI  # 延迟导入：local 模式不需要这个包

        self._client = OpenAI(
            api_key=config.EMBEDDING_API_KEY,
            base_url=config.EMBEDDING_API_BASE_URL,
            timeout=config.EMBEDDING_API_TIMEOUT,
            max_retries=0,   # 重试逻辑自己控制，便于打日志
        )
        self.dim = config.EMBEDDING_API_DIMENSIONS or None
        self.model_path = config.EMBEDDING_API_BASE_URL
        self.device = "cloud"
        print(f"[向量] 使用云端接口 {config.EMBEDDING_API_BASE_URL}")
        print(f"[向量] 模型 {config.EMBEDDING_API_MODEL}，"
              f"维度 {self.dim or '由服务端决定'}")

    def _load_local(self) -> None:
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
        print(f"[向量] 正在加载 {config.LOCAL_EMBEDDING_MODEL_NAME}")
        print(f"[向量] 本地路径 {self.model_path}")
        print(f"[向量] 推理设备 {device}")

        model = SentenceTransformer(self.model_path, device=device)
        model.max_seq_length = config.EMBEDDING_MAX_SEQ_LENGTH
        # 新版 sentence-transformers 改了这个方法名，兼容两种写法
        getter = (getattr(model, "get_embedding_dimension", None)
                  or model.get_sentence_embedding_dimension)
        self.dim = getter()
        self.model = model
        print(f"[向量] 加载成功，维度 {self.dim}")

    def ensure_loaded(self) -> None:
        self.load()
        if not self.ready:
            if self.mode == "api":
                raise EmbeddingUnavailable(
                    f"云端向量接口不可用（{self.load_error}）。"
                    f"请检查 backend/.env 里的 EMBEDDING_API_KEY 以及网络能否访问 "
                    f"{config.EMBEDDING_API_BASE_URL}。")
            raise EmbeddingUnavailable(
                f"语义模型未能加载（{self.load_error}）。"
                f"请确认本机 HuggingFace 缓存中存在 "
                f"{config.LOCAL_EMBEDDING_MODEL_NAME}，"
                f"或把 EMBEDDING_PROVIDER 改成 api。")

    # ------------------------------------------------------------ 状态
    @property
    def ready(self) -> bool:
        return self.model is not None or self._client is not None

    # ------------------------------------------------------------ 编码
    def encode_documents(
        self,
        texts: List[str],
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> np.ndarray:
        """文档向量（不加指令前缀，文档侧不需要）。"""
        return self._encode(texts, is_query=False, progress_cb=progress_cb)

    def encode_query(self, text: str) -> np.ndarray:
        """查询向量，返回形状 (1, dim)。

        local 模式下用模型自带的 query 指令前缀（Qwen3 官方用法，跨语言检索更好）；
        api 模式下由服务端模型自己处理，不再拼前缀。
        """
        return self._encode([text], is_query=True)

    def _encode(
        self,
        texts: List[str],
        is_query: bool = False,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> np.ndarray:
        self.ensure_loaded()
        if self.mode == "api":
            return self._encode_api(texts, progress_cb=progress_cb)
        return self._encode_local(texts, is_query=is_query, progress_cb=progress_cb)

    # ------------------------------------------------ 云端实现
    def _encode_api(
        self,
        texts: List[str],
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> np.ndarray:
        out: List[np.ndarray] = []
        total = len(texts)
        bs = max(1, config.EMBEDDING_API_BATCH_SIZE)
        for start in range(0, total, bs):
            batch = texts[start:start + bs]
            out.append(self._api_embed_batch(batch))
            if progress_cb:
                progress_cb(min(start + bs, total), total)
        if not out:
            return np.zeros((0, self.dim or 0), dtype=np.float32)

        matrix = np.vstack(out)
        if self.dim and matrix.shape[1] != self.dim:
            # 服务端返回维度与配置不一致时以返回值为准，避免后面索引维度错位
            print(f"[向量] 注意：返回维度 {matrix.shape[1]} 与配置 {self.dim} "
                  f"不一致，以返回值为准")
            self.dim = int(matrix.shape[1])
        return _l2_normalize(matrix)

    def _api_embed_batch(self, batch: List[str]) -> np.ndarray:
        """单批调用 + 重试；重试耗尽才抛错（不静默降级）。"""
        assert self._client is not None
        attempts = max(1, config.EMBEDDING_API_RETRIES)
        last_error: Optional[Exception] = None
        use_dimensions = bool(config.EMBEDDING_API_DIMENSIONS)

        for attempt in range(1, attempts + 1):
            kwargs = {"model": config.EMBEDDING_API_MODEL, "input": batch}
            if use_dimensions:
                kwargs["dimensions"] = config.EMBEDDING_API_DIMENSIONS
            try:
                resp = self._client.embeddings.create(**kwargs)
                return np.asarray([d.embedding for d in resp.data], dtype=np.float32)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                message = str(exc).lower()
                # 个别兼容服务不认 dimensions 参数：去掉它再试，而不是直接失败
                if use_dimensions and "dimension" in message:
                    use_dimensions = False
                    print("[向量] 服务端不接受 dimensions 参数，去掉后重试")
                    continue
                if attempt < attempts:
                    wait = min(2.0 ** attempt * 0.5, 8.0)
                    print(f"[向量] 第 {attempt} 次调用失败，{wait:.1f}s 后重试：{exc}")
                    time.sleep(wait)

        raise EmbeddingUnavailable(
            f"云端向量接口调用失败（已重试 {attempts} 次）：{last_error}")

    # ------------------------------------------------ 本地实现
    def _encode_local(
        self,
        texts: List[str],
        is_query: bool = False,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> np.ndarray:
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
                    print(f"[向量] 显存不足，批次降到 {size} 重试")
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
            "provider": config.EMBEDDING_PROVIDER,
            "mode": self.mode,
            "loaded": self.ready,
            "dim": self.dim,
            "device": self.device,
            "endpoint": (config.EMBEDDING_API_BASE_URL if self.mode == "api"
                         else self.model_path),
            "max_seq_length": config.EMBEDDING_MAX_SEQ_LENGTH,
            "load_seconds": round(self.load_seconds, 1),
            "error": self.load_error,
        }


service = EmbeddingService()

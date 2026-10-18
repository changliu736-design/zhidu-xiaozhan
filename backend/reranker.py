# -*- coding: utf-8 -*-
"""神经重排：cross-encoder 判断「候选片段是否真的回答了这个问题」。

混合检索负责"召回"（向量 + BM25 先捞一批），重排负责"精排"。
cross-encoder 把问题和片段拼在一起过一遍模型，比分别取向量更准，
代价是只能对少量候选打分，所以流程是：召回 18 条 → 重排取前 6 条。

模型缺失、显存不足或推理报错时自动退回混合检索的原顺序，
不影响问答主流程，只在健康检查里暴露状态。
"""
import threading
from typing import Dict, List

import numpy as np

import config

_lock = threading.Lock()


def _minmax(values: np.ndarray) -> np.ndarray:
    if not len(values):
        return values
    lo, hi = float(values.min()), float(values.max())
    if hi - lo < 1e-9:
        return np.zeros_like(values)
    return (values - lo) / (hi - lo)


class RerankerService:
    def __init__(self) -> None:
        self.model = None
        self.path = ""
        self.device = "cpu"
        self.error = None
        self.checked = False
        self._predict_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.model is not None

    def load(self) -> bool:
        """首次使用时加载；失败只记录错误，后续直接走降级，不反复卡顿。"""
        if self.checked:
            return self.ready
        with _lock:
            if self.checked:
                return self.ready
            self.checked = True
            if not config.RERANKER_ENABLED:
                self.error = "已在配置中关闭"
                return False
            self.path = config.resolve_reranker_path()
            if not self.path:
                self.error = f"未找到本地模型 {config.RERANKER_MODEL_NAME}"
                print(f"[重排] {self.error}，使用词面重排")
                return False
            try:
                import torch
                from sentence_transformers import CrossEncoder

                device = config.RERANKER_DEVICE
                if device == "auto":
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                self.device = device
                print(f"[重排] 正在加载 {self.path}（{device}）")
                self.model = CrossEncoder(
                    self.path, device=device,
                    max_length=config.RERANK_MAX_LENGTH,
                )
                print("[重排] 加载成功")
            except Exception as exc:  # noqa: BLE001
                self.model = None
                self.error = f"{type(exc).__name__}: {exc}"
                print(f"[重排] 加载失败，使用词面重排：{self.error}")
        return self.ready

    def rerank(self, query: str, passages: List[Dict], top_k: int) -> List[Dict]:
        """按问题给候选片段打分排序；不可用时原样截断返回。"""
        if not passages:
            return []
        if not self.load() or not query.strip():
            return passages[:top_k]

        pairs = [(query, str(p.get("text") or "")) for p in passages]
        try:
            # CrossEncoder 共享一个 GPU 模型，多请求并发时串行推理，避免显存互相踩踏
            with self._predict_lock:
                scores = np.asarray(
                    self.model.predict(
                        pairs,
                        batch_size=config.RERANK_BATCH_SIZE,
                        show_progress_bar=False,
                    ),
                    dtype=np.float64,
                )
        except Exception as exc:  # noqa: BLE001
            # 典型场景是显存不足：卸掉模型，之后本次进程内都用词面重排
            self.model = None
            self.error = f"推理失败：{type(exc).__name__}: {exc}"
            print(f"[重排] {self.error}，改用词面重排")
            return passages[:top_k]

        if scores.ndim > 1:
            scores = scores.reshape(scores.shape[0], -1)[:, 0]
        neural = _minmax(scores)
        base = _minmax(np.array([float(p.get("score", 0)) for p in passages],
                                dtype=np.float64))
        final = (config.RERANK_NEURAL_WEIGHT * neural
                 + (1 - config.RERANK_NEURAL_WEIGHT) * base)

        results: List[Dict] = []
        for idx in np.argsort(-final)[:top_k]:
            item = dict(passages[int(idx)])
            item["score"] = round(float(final[idx]), 4)
            item["neural_score"] = round(float(neural[idx]), 4)
            results.append(item)
        return results

    def status(self) -> Dict:
        return {
            "name": config.RERANKER_MODEL_NAME,
            "enabled": config.RERANKER_ENABLED,
            "installed": bool(config.resolve_reranker_path()),
            "loaded": self.ready,
            "device": self.device,
            "error": self.error,
        }


service = RerankerService()


def get_reranker() -> RerankerService:
    return service

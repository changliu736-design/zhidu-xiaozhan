# -*- coding: utf-8 -*-
"""神经重排：判断「候选片段是否真的回答了这个问题」。

混合检索负责"召回"（向量 + BM25 先捞一批），重排负责"精排"。
重排把问题和片段拼在一起打分，比分别取向量更准，
代价是只能对少量候选打分，所以流程是：召回 18 条 → 重排取前 6 条。

三种运行方式（config.RERANKER_PROVIDER）：
  api   —— 调云端 rerank 接口（默认阿里云百炼 text-rerank，国内可直连）
  local —— 本地 cross-encoder 加载 bge-reranker-base（需要 torch）
  off   —— 直接关掉，只保留词面重排
  auto  —— 有 API Key 走 api；否则本机有模型用 local；都没有就 off

任何环节失败都退回混合检索的原顺序，不影响问答主流程，
只在健康检查里暴露状态 —— 这是刻意的：重排是锦上添花，
不能因为它挂掉把整个问答拖死。
"""
import json
import threading
import urllib.error
import urllib.request
from typing import Dict, List, Optional

import numpy as np

import config

_lock = threading.Lock()

# 连续失败到这个次数就停用云端重排，避免每个请求都白等一次超时
_API_FAILURE_LIMIT = 3


def _minmax(values: np.ndarray) -> np.ndarray:
    if not len(values):
        return values
    lo, hi = float(values.min()), float(values.max())
    if hi - lo < 1e-9:
        return np.zeros_like(values)
    return (values - lo) / (hi - lo)


class RerankerService:
    def __init__(self) -> None:
        self.mode = "off"
        self.model = None
        self.path = ""
        self.device = "cpu"
        self.error = None
        self.checked = False
        self._predict_lock = threading.Lock()
        self._api_failures = 0

    @property
    def ready(self) -> bool:
        if self.mode == "api":
            return self._api_failures < _API_FAILURE_LIMIT
        return self.model is not None

    def _decide_mode(self) -> str:
        """按配置决定这次进程里用哪种重排。"""
        provider = config.RERANKER_PROVIDER
        if not config.RERANKER_ENABLED or provider == "off":
            return "off"
        if provider == "api":
            return "api"
        if provider == "local":
            return "local"
        # auto：优先云端（省内存），其次本地，都没有就关掉
        if config.RERANKER_API_KEY:
            return "api"
        if config.resolve_reranker_path():
            return "local"
        return "off"

    def load(self) -> bool:
        """首次使用时初始化；失败只记录错误，后续直接走降级，不反复卡顿。"""
        if self.checked:
            return self.ready
        with _lock:
            if self.checked:
                return self.ready
            self.checked = True
            self.mode = self._decide_mode()

            if self.mode == "off":
                if not config.RERANKER_ENABLED or config.RERANKER_PROVIDER == "off":
                    self.error = "已在配置中关闭"
                elif not config.RERANKER_API_KEY:
                    self.error = ("未配置 RERANKER_API_KEY，且本机没有本地重排模型 "
                                  f"{config.LOCAL_RERANKER_MODEL_NAME}")
                else:
                    self.error = "不可用"
                print(f"[重排] {self.error}，使用词面重排")
                return False

            if self.mode == "api":
                self._load_api()
            else:
                self._load_local()
        return self.ready

    # ------------------------------------------------------------ 初始化
    def _load_api(self) -> None:
        """云端模式：只记配置，不发请求（启动不该被网络卡住）。"""
        if not config.RERANKER_API_KEY:
            self.error = "未配置 RERANKER_API_KEY"
            self.mode = "off"
            print(f"[重排] {self.error}，使用词面重排")
            return
        self.device = "cloud"
        self.path = config.RERANKER_API_BASE_URL
        print(f"[重排] 使用云端接口 {config.RERANKER_API_BASE_URL}")
        print(f"[重排] 模型 {config.RERANKER_API_MODEL}")

    def _load_local(self) -> None:
        """本地模式：cross-encoder。找不到模型或加载失败就降级。"""
        self.path = config.resolve_reranker_path()
        if not self.path:
            self.error = f"未找到本地模型 {config.LOCAL_RERANKER_MODEL_NAME}"
            self.mode = "off"
            print(f"[重排] {self.error}，使用词面重排")
            return
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
            self.mode = "off"
            self.error = f"{type(exc).__name__}: {exc}"
            print(f"[重排] 加载失败，使用词面重排：{self.error}")

    # ------------------------------------------------------------ 打分
    def rerank(self, query: str, passages: List[Dict], top_k: int) -> List[Dict]:
        """按问题给候选片段打分排序；不可用时原样截断返回。"""
        if not passages:
            return []
        if not self.load() or not (query or "").strip():
            return passages[:top_k]

        if self.mode == "api":
            scores = self._api_scores(query, passages)
        else:
            scores = self._local_scores(query, passages)

        if scores is None:
            return passages[:top_k]
        return self._fuse(scores, passages, top_k)

    def _local_scores(self, query: str,
                      passages: List[Dict]) -> Optional[np.ndarray]:
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
            self.mode = "off"
            self.error = f"推理失败：{type(exc).__name__}: {exc}"
            print(f"[重排] {self.error}，改用词面重排")
            return None

        if scores.ndim > 1:
            scores = scores.reshape(scores.shape[0], -1)[:, 0]
        return scores

    def _record_failure(self, message: str) -> None:
        """记一次失败；连续失败到上限就让本次进程不再尝试云端。"""
        self._api_failures += 1
        self.error = message
        print(f"[重排] {message}（连续 {self._api_failures} 次），改用词面重排")
        if self._api_failures >= _API_FAILURE_LIMIT:
            print("[重排] 连续失败达到上限，本次进程内停用云端重排")

    def _api_scores(self, query: str,
                    passages: List[Dict]) -> Optional[np.ndarray]:
        """调百炼原生 text-rerank 接口。

        用标准库 urllib 而不是 requests：少一个依赖，2核2G 服务器上能省就省。
        """
        documents = [str(p.get("text") or "")[:4000] for p in passages]
        payload = {
            "model": config.RERANKER_API_MODEL,
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": False, "top_n": len(documents)},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            config.RERANKER_API_BASE_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {config.RERANKER_API_KEY}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(
                    request, timeout=config.RERANKER_API_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 把服务端返回的 JSON 错误体带出来，比 "HTTP Error 401" 好排查得多
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace").strip()[:300]
            except Exception:  # noqa: BLE001
                pass
            self._record_failure(f"接口返回 HTTP {exc.code}：{detail or exc.reason}")
            return None
        except Exception as exc:  # noqa: BLE001
            self._record_failure(f"调用失败：{type(exc).__name__}: {exc}")
            return None

        results = ((data or {}).get("output") or {}).get("results") or []
        if not results:
            self._record_failure("接口返回为空")
            return None

        # 按 index 回填，保证与 passages 顺序一一对应
        scores = np.zeros(len(passages), dtype=np.float64)
        for item in results:
            try:
                idx = int(item["index"])
                score = float(item["relevance_score"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(scores):
                scores[idx] = score

        self._api_failures = 0
        self.error = None
        return scores

    # ------------------------------------------------------------ 融合
    @staticmethod
    def _fuse(scores: np.ndarray, passages: List[Dict], top_k: int) -> List[Dict]:
        """神经分与混合检索分加权融合，输出前 top_k 条。"""
        neural = _minmax(np.asarray(scores, dtype=np.float64))
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

    # ------------------------------------------------------------ 状态
    def status(self) -> Dict:
        return {
            "name": (config.RERANKER_API_MODEL if self.mode == "api"
                     else config.RERANKER_MODEL_NAME),
            "provider": config.RERANKER_PROVIDER,
            "mode": self.mode,
            "enabled": config.RERANKER_ENABLED,
            "installed": bool(config.resolve_reranker_path()),
            "loaded": self.ready,
            "device": self.device,
            "error": self.error,
        }


service = RerankerService()


def get_reranker() -> RerankerService:
    return service

"""重排序封装：默认 qwen3.7-text-rerank，SDK 不支持时自动走 HTTP，失败降级向量顺序。"""

from __future__ import annotations

import logging
from functools import lru_cache

import dashscope
import requests
from dashscope import TextReRank

from app.config import Settings, settings

logger = logging.getLogger(__name__)

PROBE_QUERY = "应收账款期末余额"
PROBE_DOCS = ["应收账款 期末余额 12,851,000.00", "存货 期末余额 3,000.00"]


def _configure_base_url(base_url: str) -> None:
    """SDK 调用走 /api/v1，与 OpenAI 兼容模式路径不同，需按站点自动映射。"""
    if "dashscope-intl" in base_url:
        dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
    else:
        dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"


def _http_base(base_url: str) -> str:
    """把 OpenAI 兼容 base_url 换算成原生 API 根（用于 /rerank 端点）。"""
    base = base_url.rstrip("/")
    if base.endswith("/compatible-mode/v1"):
        base = base[: -len("/compatible-mode/v1")]
    return f"{base}/api/v1"


def _field(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class Reranker:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        _configure_base_url(self.cfg.dashscope_base_url)
        self._probe: dict | None = None

    # ------------------------------------------------------------------ 能力探测

    def probe(self, force: bool = False) -> dict:
        """确认当前 rerank_model 走哪条调用路径可用，并给出分数范围（缓存结果）。"""
        if self._probe is not None and not force:
            return self._probe

        result = {"model": self.cfg.rerank_model, "path": None, "ok": False, "score_range": [None, None], "error": ""}
        for path, call in (("sdk", self._call_sdk), ("http", self._call_http)):
            try:
                ranked = call(PROBE_QUERY, PROBE_DOCS, len(PROBE_DOCS))
                scores = [score for _, score in ranked if score is not None]
                result.update(
                    {
                        "path": path,
                        "ok": bool(ranked),
                        "score_range": [min(scores), max(scores)] if scores else [None, None],
                    }
                )
                if ranked:
                    self._probe = result
                    logger.info("rerank 探测通过：model=%s path=%s", self.cfg.rerank_model, path)
                    return result
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"{path}: {str(exc)[:200]}"
                logger.warning("rerank 探测失败（%s）：%s", path, str(exc)[:200])

        result["path"] = "none"
        self._probe = result
        return result

    def reset(self) -> None:
        self._probe = None

    # ------------------------------------------------------------------ 调用实现

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float | None]]:
        """返回 (原始下标, 相关性分数) 列表，按相关性从高到低。"""
        if not documents:
            return []
        top_n = max(1, min(top_n, len(documents)))

        path = (self.probe().get("path") or "sdk")
        for candidate in [path, "http" if path == "sdk" else "sdk"]:
            try:
                ranked = self._call_sdk(query, documents, top_n) if candidate == "sdk" else self._call_http(
                    query, documents, top_n
                )
                if ranked:
                    return ranked
            except Exception as exc:  # noqa: BLE001 - 降级保证问答链路可用
                logger.warning("重排序失败（%s），尝试降级：%s", candidate, str(exc)[:200])
        logger.warning("重排序全部失败，降级为召回顺序")
        return [(i, None) for i in range(top_n)]

    def _call_sdk(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float | None]]:
        resp = TextReRank.call(
            model=self.cfg.rerank_model,
            query=query,
            documents=documents,
            top_n=top_n,
            return_documents=False,
            api_key=self.cfg.require_api_key(),
        )
        status = _field(resp, "status_code", 200)
        if status != 200:
            raise RuntimeError(f"status={status} message={_field(resp, 'message')}")
        results = _field(_field(resp, "output", {}), "results", []) or []
        return self._parse_results(results)

    def _call_http(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float | None]]:
        url = f"{_http_base(self.cfg.dashscope_base_url)}/services/rerank/text-rerank/text-rerank"
        payload = {
            "model": self.cfg.rerank_model,
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": False, "top_n": top_n},
        }
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.cfg.require_api_key()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        results = _field(_field(data, "output", {}), "results", []) or []
        return self._parse_results(results)

    @staticmethod
    def _parse_results(results) -> list[tuple[int, float | None]]:
        ranked: list[tuple[int, float | None]] = []
        for item in results:
            index = _field(item, "index")
            score = _field(item, "relevance_score")
            if index is None:
                continue
            ranked.append((int(index), float(score) if score is not None else None))
        if not ranked:
            raise RuntimeError("重排序返回结果为空")
        ranked.sort(key=lambda pair: (pair[1] is None, -(pair[1] or 0.0)))
        return ranked


@lru_cache(maxsize=1)
def get_reranker() -> Reranker:
    return Reranker()

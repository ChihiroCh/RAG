"""答案置信度（0-1）：检索信号即时打分 + 大模型一致性校准后融合。

两段式：
1. `from_retrieval(hits)`：只用已召回的候选打分，零网络调用，供首字前展示"预估"。
2. `finalize(question, answer, hits, base)`：答案生成后调用一次轻量模型，评估答案与
   引用资料的一致性，与检索分加权融合；任何异常都降级为检索分，不影响回答。
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.clients.llm import LLMClient, get_llm_client
from app.config import Settings, settings

logger = logging.getLogger(__name__)

LEVEL_LABELS = {"high": "高", "medium": "中", "low": "低"}
LOW_HINT = "资料可能不足，请核实引用原文"

# 答案中出现这些措辞时，说明模型判定资料不足，置信度封顶
REFUSAL_MARKERS = (
    "资料不足",
    "不足以回答",
    "无法回答",
    "无法确定",
    "未检索到",
    "没有检索到",
    "无相关",
    "知识库中没有",
)

JUDGE_SYSTEM_PROMPT = """你是检索增强问答的质量审核员。给定【参考资料】与【答案】，判断答案是否被资料支撑，并给出 0-1 的一致性分数：
- 1.0：事实、数值、结论均能在资料中找到依据，引用编号与内容对应正确
- 0.6~0.9：主体有依据，存在少量合理概括或推断
- 0.3~0.5：部分内容超出资料范围，或引用编号与内容不匹配
- 0~0.2：答案与资料矛盾，或资料明显不足以回答该问题

只输出一行 JSON，不要解释、不要代码块：{"consistency": 0.0, "reason": "不超过40字的中文理由"}"""


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _norm(value: float, low: float, high: float) -> float:
    if high <= low:
        return _clamp(value)
    return _clamp((value - low) / (high - low))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ConfidenceEstimator:
    """置信度计算器，评分策略集中在此，便于按数据特点调参。"""

    def __init__(self, llm: LLMClient | None = None, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_llm_client()
        return self._llm

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.confidence_enabled)

    # ------------------------------------------------------------------ 第一段

    def from_retrieval(self, hits: list[dict[str, Any]]) -> dict[str, Any] | None:
        """检索完成后的即时评分（无网络调用）。"""
        if not self.enabled:
            return None
        if not hits:
            return self._empty(reason="未检索到相关资料")

        rerank_scores = [_to_float(hit.get("score")) for hit in hits if hit.get("score") is not None]
        degraded = not rerank_scores
        # 每条候选的实际相关度：精排可用时用精排分，否则退回向量分
        relevance = [
            _to_float(hit.get("score")) if hit.get("score") is not None else _to_float(hit.get("vector_score"))
            for hit in hits
        ]
        top_value = relevance[0]
        if degraded:
            rel_top = _norm(top_value, self.cfg.confidence_vector_low, self.cfg.confidence_vector_high)
        else:
            rel_top = _norm(top_value, self.cfg.confidence_rerank_low, self.cfg.confidence_rerank_high)

        margin_raw = self._margin(relevance)
        margin = _clamp(margin_raw / 0.35)
        # 覆盖度只统计"有效支撑"（明显弱于 top1 的尾部结果不计入，避免虚高）
        floor = max(0.20, 0.35 * top_value)
        support = [hit for hit, value in zip(hits, relevance) if value >= floor]
        support_docs = len({str(hit.get("doc_id")) for hit in support})
        coverage = 0.6 * min(len(support) / 3, 1.0) + 0.4 * min(support_docs / 2, 1.0)

        score = 0.60 * rel_top + 0.22 * margin + 0.18 * coverage
        components = {
            "rerank_top": None if degraded else round(top_value, 4),
            "vector_top": round(_to_float(hits[0].get("vector_score")), 4),
            "rerank_norm": round(rel_top, 4),
            "margin": round(margin, 4),
            "coverage": round(coverage, 4),
            "retrieval": round(_clamp(score), 4),
            "consistency": None,
            "citation_hit": None,
        }
        reason = (
            f"{'仅向量分（精排不可用）' if degraded else '精排'}最高 {top_value:.2f}，"
            f"有效支撑 {len(support)} 段 / {support_docs} 个来源"
        )
        return self._result(
            score=score,
            stage="retrieval",
            method="retrieval",
            degraded=degraded,
            components=components,
            reason=reason,
        )

    # ------------------------------------------------------------------ 第二段

    def finalize(
        self,
        question: str,
        answer: str,
        hits: list[dict[str, Any]],
        base: dict[str, Any] | None,
        require_citations: bool = True,
    ) -> dict[str, Any] | None:
        """生成结束后的终值：一致性校准 + 融合 + 低分封顶。"""
        if not self.enabled:
            return None
        if not hits or not base:
            return self._empty(reason="未检索到相关资料")
        if base.get("stage") == "final":
            return base

        answer = (answer or "").strip()
        components = dict(base.get("components") or {})
        consistency: float | None = None
        reason: str | None = None
        refusal = any(marker in answer for marker in REFUSAL_MARKERS)

        # 答案本身已声明"资料不足"时无需再校准，直接走低分封顶（省一次调用）
        if self.cfg.confidence_llm_enabled and answer and not refusal:
            judged = self._judge(question, answer, hits)
            if judged:
                consistency, reason = judged

        retrieval = _clamp(_to_float(components.get("retrieval"), base.get("score", 0.0)))
        if consistency is None:
            fused = retrieval
            method = "retrieval"
        else:
            weight = _clamp(_to_float(self.cfg.confidence_retrieval_weight, 0.55))
            fused = weight * retrieval + (1 - weight) * consistency
            method = "hybrid"

        citation_hit = self._citation_hit(answer, hits) if require_citations else 1.0
        score = fused * (0.85 + 0.15 * citation_hit)

        capped = False
        if refusal:
            score = min(score, 0.30)
            capped = True
            reason = reason or "答案表明资料不足以回答"
        elif citation_hit == 0 and answer:
            reason = reason or "答案未标注引用编号"

        components.update(
            {
                "consistency": None if consistency is None else round(consistency, 4),
                "citation_hit": round(citation_hit, 4),
            }
        )
        logger.info(
            "置信度终值 %.2f（检索 %.2f / 一致性 %s / 引用命中 %.2f%s）",
            score,
            retrieval,
            "降级" if consistency is None else f"{consistency:.2f}",
            citation_hit,
            "，资料不足封顶" if capped else "",
        )
        return self._result(
            score=score,
            stage="final",
            method=method,
            degraded=bool(base.get("degraded")),
            components=components,
            reason=reason,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _margin(ranked: list[float]) -> float:
        """top1 与 top2 的差距，用来衡量"是否只有一个来源勉强命中"。"""
        if len(ranked) < 2:
            return 0.0
        return max(0.0, ranked[0] - ranked[1])

    @staticmethod
    def _citation_hit(answer: str, hits: list[dict[str, Any]]) -> float:
        markers = [int(item) for item in re.findall(r"\[(\d{1,2})\]", answer or "")]
        if not markers:
            return 0.0
        valid = {int(hit.get("index") or 0) for hit in hits}
        hit_ratio = len([m for m in markers if m in valid]) / len(markers)
        return _clamp(hit_ratio)

    def _judge(
        self, question: str, answer: str, hits: list[dict[str, Any]]
    ) -> tuple[float, str] | None:
        """一次轻量调用评估答案与资料的一致性；失败返回 None。"""
        references = []
        for hit in hits:
            content = " ".join(str(hit.get("content") or "").split())
            references.append(
                f"[{hit.get('index')}] {hit.get('doc_id')}｜{hit.get('file_name')} "
                f"第{hit.get('page')}页：{content}"
            )
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"问题：{question[:500]}\n\n"
                    f"答案：{answer[:2000]}\n\n"
                    "参考资料：\n" + "\n".join(references)
                ),
            },
        ]

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.llm.complete, messages, None, 0.0, 200)
        try:
            raw = future.result(timeout=max(2.0, float(self.cfg.confidence_timeout)))
        except Exception as exc:  # noqa: BLE001 - 校准失败一律降级
            logger.info("一致性校准失败，降级为检索分：%s", str(exc)[:160])
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        payload = self._parse_json(raw)
        if not payload:
            logger.info("一致性校准返回非 JSON，降级为检索分：%s", (raw or "")[:120])
            return None
        score = payload.get("consistency", payload.get("score"))
        if score is None:
            return None
        reason = str(payload.get("reason") or "").strip()[:60]
        return _clamp(_to_float(score)), reason

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | None:
        if not raw:
            return None
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip()
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _empty(self, reason: str) -> dict[str, Any]:
        return self._result(
            score=0.0,
            stage="final",
            method="retrieval",
            degraded=False,
            components={
                "rerank_top": None,
                "vector_top": None,
                "rerank_norm": 0.0,
                "margin": 0.0,
                "coverage": 0.0,
                "retrieval": 0.0,
                "consistency": None,
                "citation_hit": 0.0,
            },
            reason=reason,
        )

    def _result(
        self,
        score: float,
        stage: str,
        method: str,
        degraded: bool,
        components: dict[str, Any],
        reason: str | None,
    ) -> dict[str, Any]:
        score = round(_clamp(float(score)), 2)
        level = self._level(score)
        return {
            "enabled": True,
            "score": score,
            "percent": int(round(score * 100)),
            "level": level,
            "level_label": LEVEL_LABELS[level],
            "stage": stage,
            "method": method,
            "degraded": degraded,
            "hint": LOW_HINT if level == "low" else None,
            "components": components,
            "reason": reason,
        }

    def _level(self, score: float) -> str:
        if score >= float(self.cfg.confidence_high):
            return "high"
        if score < float(self.cfg.confidence_low):
            return "low"
        return "medium"

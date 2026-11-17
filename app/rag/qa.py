"""问答编排：检索 -> 组装提示词 -> 流式生成 -> 带引用返回。"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Iterator

from app.clients.llm import LLMClient, friendly_error, get_llm_client
from app.config import Settings, settings
from app.rag.confidence import ConfidenceEstimator
from app.rag.retriever import Retriever, build_context
from app.store.faiss_store import FaissStore

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「多模态知识库」的问答助手，服务对象需要可信、可溯源的答案。请严格遵守：

1. 只依据「参考资料」作答，资料中没有的信息不要编造；如果资料不足以回答，直接说明"当前知识库资料不足以回答该问题"，并指出还缺什么信息。
2. 每条关键结论后用 [编号] 标注来源，例如 [1]、[2][3]，编号必须对应参考资料中给出的编号。
3. 提到具体文件时，带上来源标识（如 001、077），方便用户溯源。
4. 表格数据要准确：保留原始数值、单位与行列关系，必要时用 Markdown 表格或列表复述关键行列。
5. 回答使用简体中文，结构清晰（先给结论，再分点说明），不要复述无关的参考资料原文。
6. 如果多个来源说法冲突，指出差异并分别标注来源。"""

NO_RESULT = "我在当前知识库中没有检索到与该问题相关的内容。可以换个说法，或在左侧确认相关文件是否已入库。"

# 数值/数组/结构题：强约束输出，禁止解释性文字与引用标注
STRUCTURED_SYSTEM_PROMPT = """你是表格数据取值器，只依据「参考资料」中的表格输出最终答案。规则：
1. 只输出最终答案本身，不写"根据表格可知""答案是"等说明性文字，不解释过程、不加引用标注；
2. 结果是单个数值时：只输出数字（去掉千分位逗号，不带单位，如 12851000.00）；
3. 结果是一行/一列/一个区域时：输出 JSON 数组，元素顺序与题目列举项一致，表格中为空的项填 ""；
4. 结果是表格结构时：输出 {"row_count":整表逻辑行数,"col_count":整表逻辑列数,"cells":[{"text","row","col","rowspan","colspan"}]}，行列从 0 开始，合并单元格只输出左上角；
5. 多个来源数值冲突时，取与题目字段/期间最匹配的那个；资料不足时输出空字符串。"""

FORMAT_HINT = {
    "string": "只输出题目要求的单元格文本、名称或最终结论，不复述问题、不写解释、不加引用。",
    "number": "最终答案是单个数值，只输出数字本身。",
    "json_array": "最终答案是 JSON 字符串数组，元素与题目列举项一一对应，空值用 \"\"。",
    "json": "最终答案是结构 JSON（row_count/col_count/cells）。",
}


class RAGChat:
    def __init__(
        self,
        cfg: Settings | None = None,
        store: FaissStore | None = None,
        llm: LLMClient | None = None,
        retriever: Retriever | None = None,
    ) -> None:
        self.cfg = cfg or settings
        self._store = store
        self._retriever = retriever
        self._confidence: ConfidenceEstimator | None = None
        self.llm = llm or get_llm_client()

    # ------------------------------------------------------------------ 索引

    def get_store(self) -> FaissStore:
        if self._store is None:
            store = FaissStore(cfg=self.cfg)
            store.load()
            self._store = store
        return self._store

    def get_retriever(self) -> Retriever:
        if self._retriever is None:
            self._retriever = Retriever(self.get_store(), cfg=self.cfg, llm=self.llm)
        return self._retriever

    def get_confidence(self) -> ConfidenceEstimator | None:
        """置信度关闭时返回 None，调用方据此跳过相关事件。"""
        if not self.cfg.confidence_enabled:
            return None
        if self._confidence is None:
            self._confidence = ConfidenceEstimator(cfg=self.cfg, llm=self.llm)
        return self._confidence

    def reset(self) -> None:
        """索引重建后调用，强制重新加载。"""
        self._store = None
        self._retriever = None
        self._confidence = None

    @property
    def ready(self) -> bool:
        try:
            return len(self.get_store().chunks) > 0
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ 问答

    def ask_stream(
        self,
        question: str,
        history: list[dict[str, Any]] | None = None,
        doc_ids: Iterable[str] | None = None,
        answer_format: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """事件流：sources -> confidence(预估) -> delta... -> confidence(终值) -> done。"""
        question = (question or "").strip()
        if not question:
            yield {"type": "error", "message": "问题不能为空"}
            return

        try:
            store = self.get_store()
        except Exception as exc:  # noqa: BLE001
            logger.exception("加载索引失败")
            yield {"type": "error", "message": f"加载索引失败：{exc}"}
            return

        if not store.chunks:
            yield {"type": "error", "message": "索引为空，请先执行入库（Web 端点击「重建索引」或运行 python -m app.cli ingest）"}
            return

        try:
            hits = self.get_retriever().retrieve(question, doc_ids=doc_ids)
        except Exception as exc:  # noqa: BLE001
            logger.exception("检索失败")
            yield {"type": "error", "message": f"检索失败：{exc}"}
            return

        yield {"type": "sources", "sources": [self._public_hit(hit) for hit in hits]}

        estimator = self.get_confidence()
        base_confidence = estimator.from_retrieval(hits) if estimator else None

        if not hits:
            if base_confidence:
                yield {"type": "confidence", "confidence": base_confidence}
            yield {"type": "delta", "text": NO_RESULT}
            yield {"type": "done", "confidence": base_confidence}
            return

        if base_confidence:
            yield {"type": "confidence", "confidence": base_confidence}

        context = build_context(hits, self.cfg.max_context_chars)
        structured = bool(answer_format) and self.cfg.numeric_answer_enabled
        messages = (
            self._build_structured_messages(question, context, str(answer_format))
            if structured
            else self._build_messages(question, context, history)
        )

        answer_parts: list[str] = []
        try:
            for piece in self.llm.chat_stream(messages):
                answer_parts.append(piece)
                yield {"type": "delta", "text": piece}
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成失败")
            yield {"type": "error", "message": f"生成失败：{friendly_error(exc)}"}
            return

        final_confidence = None
        if estimator and base_confidence:
            final_confidence = estimator.finalize(
                question, "".join(answer_parts), hits, base_confidence, require_citations=not structured
            )
            if final_confidence:
                yield {"type": "confidence", "confidence": final_confidence}
        yield {"type": "done", "confidence": final_confidence}

    def ask(
        self,
        question: str,
        history: list[dict[str, Any]] | None = None,
        doc_ids: Iterable[str] | None = None,
        answer_format: str | None = None,
    ) -> dict[str, Any]:
        """非流式封装，便于 CLI 使用。"""
        answer_parts: list[str] = []
        sources: list[dict[str, Any]] = []
        confidence: dict[str, Any] | None = None
        error: str | None = None
        for event in self.ask_stream(
            question, history=history, doc_ids=doc_ids, answer_format=answer_format
        ):
            if event["type"] == "delta":
                answer_parts.append(event["text"])
            elif event["type"] == "sources":
                sources = event["sources"]
            elif event["type"] == "confidence":
                confidence = event["confidence"]
            elif event["type"] == "error":
                error = event["message"]
        return {
            "answer": "".join(answer_parts).strip(),
            "sources": sources,
            "confidence": confidence,
            "error": error,
        }

    # ------------------------------------------------------------------ 组装

    def _build_messages(
        self,
        question: str,
        context: str,
        history: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self._clean_history(history))
        messages.append(
            {
                "role": "user",
                "content": (
                    f"参考资料（每段开头是该片段的编号与来源标识）：\n{context}\n\n"
                    f"用户问题：{question}\n\n"
                    "请依据参考资料作答，并在结论后标注引用编号。"
                ),
            }
        )
        return messages

    def _build_structured_messages(
        self,
        question: str,
        context: str,
        answer_format: str,
    ) -> list[dict[str, Any]]:
        """数值/数组/结构题：强约束输出，便于下游直接写入提交文件。"""
        hint = FORMAT_HINT.get(answer_format, "只输出最终答案本身。")
        return [
            {"role": "system", "content": STRUCTURED_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"参考资料（每段开头是编号与来源标识）：\n{context}\n\n"
                    f"问题：{question}\n\n输出要求：{hint}"
                ),
            },
        ]

    @staticmethod
    def _clean_history(history: list[dict[str, Any]] | None, max_messages: int = 6) -> list[dict[str, str]]:
        if not history:
            return []
        cleaned: list[dict[str, str]] = []
        for item in history[-max_messages:]:
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                cleaned.append({"role": role, "content": content[:2000]})
        return cleaned

    @staticmethod
    def _public_hit(hit: dict[str, Any]) -> dict[str, Any]:
        data = {key: value for key, value in hit.items() if key not in ("content", "rerank_order")}
        data["content"] = (hit.get("content") or "")[:1500]
        return data

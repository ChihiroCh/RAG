"""检索链路：查询改写 -> 向量 + BM25 混合召回 -> RRF 融合 -> gte/qwen 精排 -> 父子扩展。"""

from __future__ import annotations

import logging
from typing import Any, Iterable
from urllib.parse import quote

from app.clients.llm import LLMClient, get_llm_client
from app.clients.rerank import Reranker, get_reranker
from app.config import Settings, settings
from app.rag.bm25 import BM25Cache, reciprocal_rank_fusion
from app.rag.query_rewrite import rewrite_query
from app.store.faiss_store import FaissStore

logger = logging.getLogger(__name__)

TYPE_LABEL = {"text": "文本", "table": "表格", "image": "图片", "table_row": "表格行"}
DEFAULT_MAX_TABLE_PER_DOC = 3


def preview_url(chunk: dict[str, Any]) -> str | None:
    """引用卡片的预览地址：原图走 /api/files，扫描页走 /api/page。"""
    if chunk.get("type") not in ("image", "table_row"):
        return None
    if chunk.get("image_path"):
        return f"/api/files/{quote(str(chunk['image_path']))}"
    if chunk.get("file_name") and chunk.get("page"):
        return f"/api/page/{quote(str(chunk['file_name']))}/{int(chunk['page'])}"
    return None


class Retriever:
    def __init__(
        self,
        store: FaissStore,
        cfg: Settings | None = None,
        llm: LLMClient | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.cfg = cfg or settings
        self.store = store
        self.llm = llm or get_llm_client()
        self.reranker = reranker or get_reranker()
        self._bm25 = BM25Cache()
        self._chunks_by_id: dict[str, dict] | None = None

    # ------------------------------------------------------------------ 工具

    def _parent_of(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        parent_id = chunk.get("parent_id")
        if not parent_id:
            return None
        if self._chunks_by_id is None:
            self._chunks_by_id = {item["chunk_id"]: item for item in self.store.chunks if item.get("chunk_id")}
        return self._chunks_by_id.get(str(parent_id))

    def reset(self) -> None:
        self._bm25.reset()
        self._chunks_by_id = None

    # ------------------------------------------------------------------ 主流程

    def retrieve(
        self,
        query: str,
        doc_ids: Iterable[str] | None = None,
        top_k_vector: int | None = None,
        top_k_rerank: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        top_k_vector = top_k_vector or self.cfg.hybrid_vector_top_k
        top_k_rerank = top_k_rerank or self.cfg.top_k_rerank

        rewrite = {"query": query, "variants": []}
        if self.cfg.query_rewrite_enabled:
            try:
                rewrite = rewrite_query(
                    query,
                    llm=self.llm if self.cfg.query_rewrite_llm else None,
                    cfg=self.cfg,
                )
            except Exception as exc:  # noqa: BLE001 - 改写失败不影响主链路
                logger.warning("查询改写失败，使用原问题：%s", str(exc)[:200])
        search_query = rewrite.get("query") or query
        variants = [item for item in rewrite.get("variants", []) if item][:2]

        # 问题里出现来源标识（如 "003 ..."）而调用方没显式限定文档时，自动按该标识过滤
        effective_doc_ids = doc_ids or ([rewrite["doc_id"]] if rewrite.get("doc_id") else None)
        doc_filter = {str(item) for item in effective_doc_ids} if effective_doc_ids else None

        chunks = self.store.chunks
        vector_rows: list[int] = []
        vector_scores: dict[int, float] = {}
        all_queries = list(dict.fromkeys([query, search_query, *variants]))
        per_query_top = max(10, top_k_vector // max(1, len(all_queries)))
        for text in all_queries:
            try:
                query_vector = self.llm.embed_texts([text])[0]
                candidates = self.store.search(query_vector, per_query_top, doc_ids=effective_doc_ids)
            except Exception as exc:  # noqa: BLE001
                logger.warning("向量召回失败：%s", str(exc)[:200])
                continue
            for item in candidates:
                row = int(item["row"])
                if row in vector_scores:
                    continue
                vector_rows.append(row)
                vector_scores[row] = float(item["score"])

        bm25_rows: list[int] = []
        if self.cfg.bm25_enabled:
            index = self._bm25.get(chunks)
            if index is not None:
                for text in all_queries:
                    for row, _score in index.search(text, self.cfg.hybrid_bm25_top_k, doc_ids=doc_filter):
                        if row >= len(chunks):
                            continue
                        if doc_filter and str(chunks[row].get("doc_id")) not in doc_filter:
                            continue
                        if row not in bm25_rows:
                            bm25_rows.append(row)

        if not vector_rows and not bm25_rows:
            return []

        fused = reciprocal_rank_fusion(
            [(vector_rows, 1.0), (bm25_rows, self.cfg.bm25_weight)],
            k=self.cfg.rrf_k,
        )
        fused = fused[: max(self.cfg.hybrid_fuse_top_k, top_k_rerank * 2)]
        candidates: list[dict[str, Any]] = []
        for row, score in fused:
            if 0 <= row < len(chunks):
                candidates.append({"chunk": chunks[row], "score": score, "row": row})
        if not candidates:
            return []

        documents = [item["chunk"].get("text", "") for item in candidates]
        ranked = (
            self.reranker.rerank(search_query, documents, min(len(candidates), max(top_k_rerank * 4, 10)))
            if len(candidates) > 1
            else [(0, None)]
        )

        max_tables = int(getattr(self.cfg, "retrieval_max_table_per_doc", DEFAULT_MAX_TABLE_PER_DOC) or 3)
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        per_doc_table: dict[str, int] = {}
        per_doc_row: dict[str, int] = {}
        for order, (index, score) in enumerate(ranked, start=1):
            if len(hits) >= top_k_rerank:
                break
            if index < 0 or index >= len(candidates):
                continue
            candidate = candidates[index]
            chunk = candidate["chunk"]
            chunk_type = chunk.get("type", "text")
            key = (
                chunk.get("chunk_id")
                if chunk_type in ("table", "table_row")
                else f"{chunk.get('doc_id')}_{chunk.get('page')}_{chunk_type}"
            )
            if key in seen:
                continue
            doc_id = str(chunk.get("doc_id"))
            if chunk_type == "table":
                if per_doc_table.get(doc_id, 0) >= max_tables:
                    continue
                per_doc_table[doc_id] = per_doc_table.get(doc_id, 0) + 1
            elif chunk_type == "table_row":
                if per_doc_table.get(doc_id, 0) >= max_tables and per_doc_row.get(doc_id, 0) >= 2:
                    continue
                per_doc_row[doc_id] = per_doc_row.get(doc_id, 0) + 1
            seen.add(key)
            hits.append(self._to_hit(chunk, score, vector_scores.get(candidate["row"]), order))

        hits = self._expand_parents(hits, cap=top_k_rerank + 2)
        logger.info(
            "检索完成：向量 %d / BM25 %d -> 融合 %d -> 精排保留 %d",
            len(vector_rows),
            len(bm25_rows),
            len(candidates),
            len(hits),
        )
        return hits

    # ------------------------------------------------------------------ 组装

    def _to_hit(
        self,
        chunk: dict[str, Any],
        score: float | None,
        vector_score: float | None,
        order: int,
    ) -> dict[str, Any]:
        chunk_type = chunk.get("type", "text")
        return {
            "index": order,
            "chunk_id": chunk.get("chunk_id"),
            "parent_id": chunk.get("parent_id"),
            "doc_id": chunk.get("doc_id"),
            "file_name": chunk.get("file_name"),
            "file_type": chunk.get("file_type"),
            "page": chunk.get("page"),
            "type": chunk_type,
            "type_label": TYPE_LABEL.get(chunk_type, "文本"),
            "image_path": chunk.get("image_path"),
            "preview_url": preview_url(chunk),
            "content": chunk.get("content", ""),
            "snippet": self._snippet(chunk.get("content", "")),
            "score": round(float(score), 4) if score is not None else None,
            "vector_score": round(float(vector_score or 0.0), 4),
            "rerank_order": order,
        }

    def _expand_parents(self, hits: list[dict[str, Any]], cap: int = 8) -> list[dict[str, Any]]:
        """命中表格行块时，把父表块插到它前面，保证上下文有完整表格与列头。"""
        if not getattr(self.cfg, "parent_expand_enabled", True):
            return hits
        expanded: list[dict[str, Any]] = []
        existing = {hit.get("chunk_id") for hit in hits}
        for hit in hits:
            if hit.get("type") == "table_row" and hit.get("parent_id"):
                parent = self._parent_of({"parent_id": hit["parent_id"]})
                if parent and parent.get("chunk_id") not in existing and len(expanded) < cap:
                    score = (hit.get("score") or 0.0) * 0.98
                    parent_hit = self._to_hit(parent, score, None, 0)
                    expanded.append(parent_hit)
                    existing.add(parent_hit.get("chunk_id"))
            if len(expanded) < cap:
                expanded.append(hit)
        for position, hit in enumerate(expanded, start=1):
            hit["index"] = position
        return expanded

    @staticmethod
    def _snippet(content: str, limit: int = 320) -> str:
        text = " ".join((content or "").split())
        return text if len(text) <= limit else text[:limit] + "…"


def build_context(hits: list[dict[str, Any]], max_chars: int = 8000) -> str:
    """拼接带引用编号与来源标识的上下文。"""
    blocks: list[str] = []
    used = 0
    for hit in hits:
        header = (
            f"[{hit['index']}] 来源标识：{hit['doc_id']}｜{hit['file_name']} "
            f"第{hit['page']}页｜{hit['type_label']}"
        )
        content = hit.get("content", "")
        block = f"{header}\n{content}"
        if used + len(block) > max_chars:
            remaining = max_chars - used - len(header) - 8
            if remaining <= 80:
                break
            block = f"{header}\n{content[:remaining]}…（已截断）"
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)

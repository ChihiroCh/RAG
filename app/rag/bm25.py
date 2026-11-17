"""BM25 词法召回：打分直接用 rank_bm25.BM25Okapi，分词用 jieba + 正则。

为什么需要：表格数值题依赖"科目名 + 数字"的精确匹配，纯向量召回对
`12,851,000.00`、`期末余额`、`注释36` 这类 token 区分度很低。
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

import jieba
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

jieda = jieba  # 兼容别名（避免旧代码 from app.rag.bm25 import jieda 失败）

# 英文词、数字（含千分位与小数、百分比）
WORD_RE = re.compile(r"[A-Za-z]+|\d[\d,，]*\.?\d*%?")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
STOPWORDS = set("的 了 和 与 及 或 是 在 有 为 对 从 中 该 其 这 那 个 元 年 月 日 号 第 页 表")


def tokenize(text: str, with_numeric_variants: bool = True) -> list[str]:
    """统一分词：英文/数字走正则（数字额外给出去千分位的归一形），中文走 jieba。"""
    raw = str(text or "")
    if not raw.strip():
        return []

    tokens: list[str] = []
    for match in WORD_RE.finditer(raw):
        token = match.group(0).lower()
        tokens.append(token)
        if with_numeric_variants and ("," in token or "，" in token):
            tokens.append(token.replace(",", "").replace("，", ""))

    for word in jieba.lcut(raw):
        word = word.strip()
        if not word or not CJK_RE.search(word):
            continue
        if len(word) == 1 and word in STOPWORDS:
            continue
        tokens.append(word)
    return tokens


class BM25Retriever:
    """对全量分块构建 BM25 索引（惰性、内存驻留，索引重建后需重新构造）。"""

    def __init__(self, chunks: list[dict], k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self._tokenized: list[list[str]] = [tokenize(chunk.get("text", "")) for chunk in chunks]
        self._index = BM25Okapi(self._tokenized, k1=k1, b=b) if self._tokenized else None
        logger.info("BM25 索引构建完成：%d 个块", len(self._tokenized))

    def search(self, query: str, top_k: int = 30, doc_ids: Iterable[str] | None = None) -> list[tuple[int, float]]:
        """返回 [(chunk 下标, 分数)]，按分数降序，分数为 0 的丢弃。"""
        if self._index is None or top_k <= 0:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._index.get_scores(tokens)
        allow = set(doc_ids) if doc_ids else None
        ranked = sorted(
            ((index, float(score)) for index, score in enumerate(scores) if score > 0
             and (allow is None or self.chunks[index].get("doc_id") in allow)),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[:top_k]


class BM25Cache:
    """按分块列表的"身份"缓存索引，索引重建后自动失效。"""

    def __init__(self) -> None:
        self._signature: tuple[int, int] | None = None
        self._retriever: BM25Retriever | None = None

    def get(self, chunks: list[dict]) -> BM25Retriever | None:
        if not chunks:
            return None
        signature = (id(chunks), len(chunks))
        if self._signature != signature:
            self._retriever = BM25Retriever(chunks)
            self._signature = signature
        return self._retriever

    def reset(self) -> None:
        self._signature = None
        self._retriever = None


def reciprocal_rank_fusion(
    rankings: Iterable[tuple[Iterable[int], float]],
    k: int = 60,
) -> list[tuple[int, float]]:
    """RRF 融合多路召回：score = Σ weight / (k + rank)。"""
    scores: dict[int, float] = {}
    for ranking, weight in rankings:
        for rank, index in enumerate(ranking, start=1):
            scores[index] = scores.get(index, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)

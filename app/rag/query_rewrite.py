"""查询改写：规则清洗（零成本、始终执行）+ LLM 改写（可选、带超时/缓存/降级）。

测试集问题常带口语噪声（"恢复一下""分别是多少"）与指代（"上期""本期末"），
直接拿去向量化/BM25 会稀释语义信号，先改写再检索能显著提升命中率。
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.config import Settings, settings

logger = logging.getLogger(__name__)

# 口语/题目指令类噪声：去掉不影响检索语义
NOISE_PATTERNS = (
    r"请问",
    r"帮我(看看|查一下|找找|分析)",
    r"恢复一下",
    r"恢复",
    r"表格结构",
    r"的表格",
    r"表格中",
    r"分别是多少(呢|啊)?",
    r"分别是(什么|哪些)(呢|啊)?",
    r"是多少(呢|啊)?",
    r"有多少个",
    r"有哪些",
    r"怎么样",
    r"如何",
    r"请给出",
    r"列出",
)

# 期间/口径同义映射（财务报表高频）
SYNONYMS = {
    "本期末": "期末余额",
    "期末数": "期末余额",
    "上期末": "期初余额",
    "期初数": "期初余额",
    "年初数": "期初余额",
    "本期数": "本期金额",
    "上期数": "上期金额",
    "本年": "本期",
    "上年": "上期",
}

DOC_ID_RE = re.compile(r"(?<!\d)(\d{3})(?!\d)")
CACHE_LIMIT = 512
_cache: dict[str, dict[str, Any]] = {}

REWRITE_SYSTEM_PROMPT = """你是检索式改写助手。把用户问题改写成两条中文检索式，帮助在财务报表/表格中定位数据：
1. 关键词式：只保留实体与字段名，用空格分隔（如"应收账款 期末余额 坏账准备"）；
2. 字段式：补充同义字段与口径（如"期末余额 本期金额 账面价值"）。
要求：不要输出解释，不要编造题目中没有的数值；严格输出 JSON 数组，例如 ["关键词式","字段式"]。"""


def rule_rewrite(question: str) -> dict[str, Any]:
    """规则层改写：去噪声、同义归一、抽取文档标识与表格提示。"""
    original = str(question or "").strip()
    text = original
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, " ", text)
    for source, target in SYNONYMS.items():
        if source in text and target not in text:
            text = text.replace(source, target)
    text = re.sub(r"[（(]\s*[)）]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ，。、：；?？!！")
    doc_match = DOC_ID_RE.search(original)
    return {
        "query": text or original,
        "original": original,
        "doc_id": doc_match.group(1) if doc_match else None,
        "variants": [],
    }


def _llm_rewrite(question: str, llm: Any, cfg: Settings) -> list[str]:
    messages = [
        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: llm.complete(messages, temperature=0.0, max_tokens=120))
        text = future.result(timeout=max(3.0, float(cfg.query_rewrite_timeout)))
    match = re.search(r"\[[\s\S]*\]", text or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [" ".join(str(item).split())[:120] for item in data if str(item).strip()][:2]


def rewrite_query(question: str, llm: Any | None = None, cfg: Settings | None = None) -> dict[str, Any]:
    """对外入口：规则改写 + （可选）LLM 改写，结果按问题文本缓存。"""
    cfg = cfg or settings
    key = str(question or "").strip()
    cached = _cache.get(key)
    if cached is not None:
        return cached

    result = rule_rewrite(key)
    if llm is not None and cfg.query_rewrite_llm:
        try:
            variants = _llm_rewrite(result["query"] or key, llm, cfg)
            if variants:
                result["variants"] = variants
        except Exception as exc:  # noqa: BLE001 - 超时/失败静默降级
            logger.debug("LLM 查询改写降级：%s", str(exc)[:160])

    if len(_cache) >= CACHE_LIMIT:
        _cache.clear()
    _cache[key] = result
    return result

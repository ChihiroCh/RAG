"""分层抽样评测：按题型抽样跑一遍 ask()，产出人工核对报告与量化指标。

指标说明（不依赖标准答案，属于弱监督/可用性指标）：
- 平均置信度、空答案率：衡量整体可用性；
- 引用命中率：答案里的 [n] 是否真的对应返回的引用；
- table_hint 命中率：题目的表格提示词是否出现在命中块的表名/表头/正文里（检索侧指标）；
- 精排分数分布：用于给置信度阈值重新校准。
"""

from __future__ import annotations

import json
import random
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings

TYPES = ("structure", "extract", "thinking")


def _tests_path() -> Path:
    return Path.cwd() / "tests.xlsx"


def load_tests(path: Path | None = None) -> pd.DataFrame:
    target = path or _tests_path()
    if not target.exists():
        raise RuntimeError(f"未找到测试集：{target}")
    frame = pd.read_excel(target)
    frame["id"] = frame["id"].astype(str)
    return frame


def stratified_sample(frame: pd.DataFrame, sample: int, seed: int = 42) -> pd.DataFrame:
    """按 question_type 分层抽样，尽量按比例分配。"""
    rng = random.Random(seed)
    total = len(frame)
    picked: list[pd.DataFrame] = []
    for qtype in TYPES:
        subset = frame[frame["question_type"] == qtype]
        if subset.empty:
            continue
        quota = max(1, round(sample * len(subset) / total))
        quota = min(quota, len(subset))
        picked.append(subset.sample(n=quota, random_state=rng.randint(0, 10**6)))
    merged = pd.concat(picked).head(sample) if picked else frame.head(sample)
    return merged.reset_index(drop=True)


def _hint_hit(hint: Any, hits: list[dict[str, Any]]) -> bool:
    if not isinstance(hint, str) or not hint.strip():
        return False
    keywords = [token for token in hint.replace("，", " ").replace("、", " ").split() if len(token) >= 2]
    if not keywords:
        keywords = [hint.strip()[:6]]
    # "应收账款表"这类提示在文本里常写作"应收账款"，去掉尾部"表"再试一次
    relaxed = [token[:-1] if token.endswith("表") and len(token) > 2 else token for token in keywords]
    haystack = " ".join(
        f"{hit.get('content', '')} {hit.get('snippet', '')} {hit.get('file_name', '')}" for hit in hits
    )
    return any(keyword in haystack for keyword in keywords + relaxed)


def _citation_hit(answer: str, hits: list[dict[str, Any]]) -> float | None:
    """答案含 [n] 引用标注时检查标注是否对应真实引用；结构化答案（无标注）返回 None。"""
    import re

    markers = [int(item) for item in re.findall(r"\[(\d{1,2})\]", answer or "")]
    if not markers:
        return None
    valid = {int(hit.get("index") or 0) for hit in hits}
    return len([m for m in markers if m in valid]) / len(markers)


def run_eval(
    sample: int = 30,
    out: str | None = None,
    seed: int = 42,
    tests_path: str | None = None,
) -> dict[str, Any]:
    from app.rag.qa import RAGChat
    from app.rag.table_answer import answer_test, table_store

    frame = stratified_sample(load_tests(Path(tests_path) if tests_path else None), sample, seed)
    chat = RAGChat()
    if not chat.ready:
        raise RuntimeError("索引为空，请先运行 python -m app.cli ingest --rebuild")
    from app.ingest.pipeline import PARSER_VERSION
    from app.ingest.chunker import CHUNKER_VERSION
    manifest = table_store(chat).manifest
    if manifest.get("complete") is False or any(not d.get("chunks") for d in manifest.get("docs", {}).values()):
        raise RuntimeError("索引不完整，请恢复视觉模型额度后重新入库")
    if manifest.get("parser_version") != PARSER_VERSION or manifest.get("chunker_version") != CHUNKER_VERSION:
        raise RuntimeError("索引版本过旧，请先运行 python3 -m app.cli ingest --rebuild")

    rows: list[dict[str, Any]] = []
    for _, item in frame.iterrows():
        doc_id = str(item["file_name"]).rsplit(".", 1)[0]
        answer_format = str(item.get("answer_format") or "string")
        result = answer_test(chat, item)
        hits = result.get("sources") or []
        confidence = (result.get("confidence") or {}).get("score")
        rows.append(
            {
                "id": str(item["id"]),
                "type": str(item.get("question_type")),
                "format": answer_format,
                "doc_id": doc_id,
                "question": str(item["question"])[:160],
                "answer": (result.get("answer") or "")[:400],
                "confidence": confidence,
                "empty": not (result.get("answer") or "").strip(),
                "citation": _citation_hit(result.get("answer") or "", hits),
                "method": result.get("method"),
                "status": result.get("status"),
                "error": result.get("error"),
                "table_id": result.get("table_id"),
                "evidence": result.get("evidence"),
                "hint_hit": _hint_hit(item.get("table_hint"), hits),
                "rerank_top": (hits[0].get("score") if hits else None),
                "sources": [f"{hit.get('doc_id')}#{hit.get('type')}" for hit in hits[:3]],
            }
        )

    rerank_scores = [row["rerank_top"] for row in rows if row["rerank_top"] is not None]
    confidences = [row["confidence"] for row in rows if row["confidence"] is not None]
    citations = [row["citation"] for row in rows if row["citation"] is not None]
    metrics = {
        "sample": len(rows),
        "avg_confidence": round(statistics.mean(confidences), 3) if confidences else None,
        "empty_rate": round(sum(1 for row in rows if row["empty"]) / len(rows), 3) if rows else None,
        "marker_rate": round(len(citations) / len(rows), 3) if rows else None,
        "citation_hit_rate": round(statistics.mean(citations), 3) if citations else None,
        "hint_hit_rate": round(sum(1 for row in rows if row["hint_hit"]) / len(rows), 3) if rows else None,
        "rerank_p50": round(statistics.median(rerank_scores), 3) if rerank_scores else None,
        "rerank_max": round(max(rerank_scores), 3) if rerank_scores else None,
        "rerank_min": round(min(rerank_scores), 3) if rerank_scores else None,
    }

    report = _render_report(rows, metrics, seed)
    out_path = Path(out) if out else Path("reports") / f"eval-{datetime.now():%Y%m%d-%H%M%S}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    json_path = out_path.with_suffix(".json")
    json_path.write_text(json.dumps({"metrics": metrics, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"metrics": metrics, "report": str(out_path), "rows": rows}


def _render_report(rows: list[dict[str, Any]], metrics: dict[str, Any], seed: int) -> str:
    lines = [
        "# RAG 抽样评测报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 抽样：{metrics['sample']} 题（seed={seed}，按 question_type 分层）",
        "",
        "## 指标",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 平均置信度 | {metrics['avg_confidence']} |",
        f"| 空答案率 | {metrics['empty_rate']} |",
        f"| 引用标注覆盖率 / 命中率 | {metrics['marker_rate']} / {metrics['citation_hit_rate']} |",
        f"| table_hint 命中率 | {metrics['hint_hit_rate']} |",
        f"| 精排分 p50 / min / max | {metrics['rerank_p50']} / {metrics['rerank_min']} / {metrics['rerank_max']} |",
        "",
        "## 逐题明细（请人工核对 answer 列）",
        "",
        "| id | 题型 | 格式 | 置信度 | 精排 | hint命中 | 问题 | 答案 | 来源 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        answer = str(row["answer"]).replace("|", "／").replace("\n", " ")[:120]
        question = str(row["question"]).replace("|", "／")[:60]
        sources = "、".join(row["sources"])
        lines.append(
            f"| {row['id']} | {row['type']} | {row['format']} | {row['confidence']} | {row['rerank_top']} | "
            f"{'是' if row['hint_hit'] else '否'} | {question} | {answer} | {sources} |"
        )
    return "\n".join(lines) + "\n"

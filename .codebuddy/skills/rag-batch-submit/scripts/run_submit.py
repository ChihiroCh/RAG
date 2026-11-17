"""批量答题脚本：读取 tests.xlsx，逐题调用知识库生成答案，按 answer_format 输出到提交文件。

用法（在项目根目录执行）：
    python3 .codebuddy/skills/rag-batch-submit/scripts/run_submit.py --limit 3          # 试跑 3 题
    python3 .codebuddy/skills/rag-batch-submit/scripts/run_submit.py --ids 1,2,25      # 指定题目调试
    python3 .codebuddy/skills/rag-batch-submit/scripts/run_submit.py                    # 全量 908 题
    # 中断后重跑会自动跳过已填答案的题目（断点续跑）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from app.rag.qa import RAGChat  # noqa: E402
from app.rag.table_answer import answer_test, PIPELINE_VERSION, number_text, table_store, format_answer

TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<t[dh]([^>]*)>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
NUM_RE = re.compile(r"-?\d[\d,，]*(?:\.\d+)?")
STRIP_LINES = ("置信度", "依据：", "依据:", "引用来源", "（无引用）", "✅", "❌")
# 官方要求：答案中不得出现"根据表格可知""答案是"等说明性文字
EXPLANATORY_RE = re.compile(
    r"^(?:根据(?:上述|以上)?(?:表格|资料|表)(?:可知|显示|可看出)?|由(?:上|上述)?表可知|"
    r"从(?:上述)?(?:表格|表中)(?:中)?(?:可知|可看出)?|综上(?:所述)?|答案是|答案为|答)[：:，,\s]*"
)


def doc_stem(file_name: str) -> str:
    return str(file_name).rsplit(".", 1)[0]


def clean_answer(text: str) -> str:
    """去掉引用标注、置信度说明与说明性文字，只留答案本体。"""
    kept: list[str] = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(STRIP_LINES):
            continue
        s = re.sub(r"\[\d{1,2}\]", "", s)
        s = re.sub(r"[#*`>]+", "", s)
        s = EXPLANATORY_RE.sub("", s)
        s = re.sub(r"(?:答案是|答案为)[：:，,]?", "", s)
        s = re.sub(r"\s+([，。；：、！？])", r"\1", s)
        s = s.strip()
        if s:
            kept.append(s)
    return " ".join(kept).strip()


def format_number(text: str) -> str | None:
    """从答案中提取数值：优先带单位/千分位的第一个数字。"""
    try:
        return number_text(text)
    except ValueError:
        return None


def format_json_array(text: str) -> list[str] | None:
    """提取答案中的 JSON 数组，全部转为字符串（None/空 -> ""）。"""
    match = re.search(r"\[[\s\S]*\]", str(text or ""))
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return ["" if item is None else str(item).strip() for item in data]
        except json.JSONDecodeError:
            pass
    return None


ARRAY_SYSTEM_PROMPT = (
    "你是答案格式化器。把给定答案整理成 JSON 数组，官方规则："
    "元素与问题中列举的项目/行/列/区域按顺序一一对应，元素直接填具体值；"
    "表格中为空的项用空字符串 \"\" 占位；数值去掉千分位逗号（125000 而非 125,000）；"
    "日期、金额、百分比和单位按题目要求填写；不要写\"根据表格可知\"\"答案是\"等说明性文字；"
    "只输出 JSON 数组本身，不要任何解释。"
)


def normalize_json_array(chat: RAGChat, question: str, raw: str) -> list[str] | None:
    """答案不含可直接解析的 JSON 数组时，用一次轻量调用强制归一化。"""
    messages = [
        {"role": "system", "content": ARRAY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"问题：{question}\n\n答案：\n{clean_answer(raw)[:2500]}\n\n只输出 JSON 数组。",
        },
    ]
    try:
        text = chat.llm.complete(messages, temperature=0.0, max_tokens=400)
    except Exception:  # noqa: BLE001
        return None
    return format_json_array(text)


def _cell_attr(attrs: str, name: str) -> int:
    match = re.search(name + r'\s*=\s*["\']?(\d+)', attrs, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def parse_html_table(html: str) -> list[list[tuple[str, int, int]]]:
    """解析表格 HTML，返回 [(文本, rowspan, colspan), ...] 的行列表。"""
    rows: list[list[tuple[str, int, int]]] = []
    for tr in TR_RE.findall(str(html or "")):
        cells = []
        for attrs, inner in CELL_RE.findall(tr):
            text = TAG_RE.sub("", inner)
            text = text.replace("&nbsp;", " ").replace("&amp;", "&")
            text = " ".join(text.split())
            cells.append((text, _cell_attr(attrs, "rowspan"), _cell_attr(attrs, "colspan")))
        if any(cell[0] for cell in cells):
            rows.append(cells)
    return rows


def parse_region(question: str) -> tuple[set[int], set[int]]:
    """结构题区域：返回要整行/整列包含的行号与列号集合（0-based，首行/首列并集语义）。"""
    q = str(question)
    rows: set[int] = set()
    cols: set[int] = set()
    match = re.search(r"前\s*(\d+)\s*行", q)
    if match:
        rows |= set(range(int(match.group(1))))
    if "第一行" in q or "首行" in q:
        rows.add(0)
    match = re.search(r"前\s*(\d+)\s*列", q)
    if match:
        cols |= set(range(int(match.group(1))))
    if "第一列" in q or "首列" in q:
        cols.add(0)
    if not rows and not cols:
        rows, cols = {0}, {0}
    return rows, cols


def compact_grid(grid: list[list[tuple[str, int, int]]]) -> list[list[tuple[str, int, int]]]:
    """收缩整列为空的填充列（VLM 常为合并表头补空列），并对齐行宽。

    行内空单元格保留占位（保证列下标不错位），输出时再统一跳过。
    """
    if not grid:
        return grid
    width = max(len(r) for r in grid)
    keep_cols = [j for j in range(width) if any(j < len(r) and r[j][0] for r in grid)]
    if not keep_cols:
        return []
    compacted = [
        [r[j] if j < len(r) else ("", 1, 1) for j in keep_cols]
        for r in grid
        if any(cell[0] for cell in r)
    ]
    return compacted


def structure_answer(chat: RAGChat, row: pd.Series) -> str | None:
    result = answer_test(chat, row)
    return result.get("answer") or None


def answer_one(chat: RAGChat, row: pd.Series) -> dict:
    return answer_test(chat, row)


def load_existing(out_path: Path) -> dict[str, str]:
    if not out_path.exists():
        return {}
    ws = load_workbook(out_path).active
    existing: dict[str, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and row[0] is not None and row[1] not in (None, ""):
            existing[str(row[0])] = str(row[1])
    return existing


def main() -> int:
    parser = argparse.ArgumentParser(description="批量答题并生成提交文件")
    parser.add_argument("--tests", default="tests.xlsx")
    parser.add_argument("--template", default="submit-template.xlsx")
    parser.add_argument("--out", default=None, help="输出文件，默认 submit-<时间戳>.xlsx")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（试跑用）")
    parser.add_argument("--ids", default=None, help="只跑指定 id，逗号分隔")
    parser.add_argument("--workers", type=int, default=4, help="并发数（过高易触发限流）")
    args = parser.parse_args()

    df = pd.read_excel(args.tests)
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        df = df[df["id"].astype(str).isin(wanted)]
    if args.limit:
        df = df.head(args.limit)

    out_path = Path(args.out) if args.out else Path(f"submit-{time.strftime('%Y%m%d-%H%M%S')}.xlsx")
    chat = RAGChat()
    if not chat.ready:
        print("索引为空，请先运行 python3 -m app.cli ingest", file=sys.stderr)
        return 1
    from app.ingest.pipeline import PARSER_VERSION
    from app.ingest.chunker import CHUNKER_VERSION
    manifest = table_store(chat).manifest
    if manifest.get("complete") is False or any(not d.get("chunks") for d in manifest.get("docs", {}).values()):
        print("索引不完整，请恢复视觉模型额度后重新入库", file=sys.stderr)
        return 1
    if manifest.get("parser_version") != PARSER_VERSION or manifest.get("chunker_version") != CHUNKER_VERSION:
        print("索引版本过旧，请先运行 python3 -m app.cli ingest --rebuild", file=sys.stderr)
        return 1
    run_fingerprint = hashlib.sha256(Path(args.tests).read_bytes() + str(manifest.get("built_at", "")).encode()).hexdigest()
    checkpoint = out_path.with_suffix(".jsonl")
    answers = {}
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("pipeline_version") == PIPELINE_VERSION and record.get("run_fingerprint") == run_fingerprint and record.get("status") == "success" and record.get("answer"):
                answers[str(record["id"])] = record["answer"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tasks = [row for _, row in df.iterrows() if str(row["id"]) not in answers]
    print(f"题目总数 {len(df)}，已完成 {len(answers)}，本次待答 {len(tasks)} -> {out_path}")

    started = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(answer_one, chat, row): row for row in tasks}
        for future in as_completed(futures):
            record = future.result()
            record["run_fingerprint"] = run_fingerprint
            question_id, answer = record["id"], record["answer"]
            with checkpoint.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
            answers[question_id] = answer
            done += 1
            if done % 10 == 0 or done == len(tasks):
                print(f"  进度 {done}/{len(tasks)}，耗时 {time.time() - started:.0f}s", flush=True)

    template = load_workbook(args.template)
    formats = {str(item["id"]): str(item["answer_format"]) for _, item in df.iterrows()}
    ws = template.active
    filled = 0
    for row in ws.iter_rows(min_row=2):
        key = str(row[0].value) if row[0].value is not None else None
        if key and key in answers:
            if answers[key] and formats.get(key) in ("number", "json_array"):
                answers[key] = format_answer(answers[key], formats[key])
            row[1].value = answers[key] or None
            if answers[key]:
                filled += 1
    template.save(out_path)

    stats = df["answer_format"].value_counts().to_dict()
    print(f"\n完成：{filled}/{len(df)} 题已写入 {out_path}，总耗时 {time.time() - started:.0f}s")
    print(f"格式分布：{stats}")
    if filled < len(df):
        missing = [str(row['id']) for _, row in df.iterrows() if not answers.get(str(row['id']))]
        print(f"未答出 {len(missing)} 题：{','.join(missing[:20])}{'…' if len(missing) > 20 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

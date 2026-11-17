"""竞赛答题：表级定位、带坐标的取值计划、确定性计算和严格输出。"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ingest.table_norm import normalize_table, column_headers

PIPELINE_VERSION = "table-v2-ordered-fallback"
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


def table_store(chat: Any) -> Any:
    """表级答题读取独立目录，无需重新调用向量模型。"""
    from types import SimpleNamespace
    root = chat.cfg.index_dir
    from pathlib import Path
    if not isinstance(root, Path):
        return chat.get_store()
    manifest_path = root / "table-catalog-manifest.json"
    chunks_path = root / "table-catalog.jsonl"
    if manifest_path.exists() and chunks_path.exists():
        if not getattr(chat, "_table_catalog", None):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            chat._table_catalog = SimpleNamespace(manifest=manifest, chunks=chunks)
        return chat._table_catalog
    return chat.get_store()


def parse_json(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    return json.loads(text)


def number_text(raw: str) -> str:
    text = raw.strip().replace("，", ",")
    # 只接受合法数字或合法千分位，绝不从公式/解释里猜第一个数字。
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    if not NUMBER.fullmatch(text):
        raise ValueError("答案不是单个数值")
    return text


def format_answer(raw: str, fmt: str) -> str:
    text = raw.strip()
    if not text or "资料不足" in text or "无法确定" in text:
        raise ValueError("缺少答案证据")
    if fmt == "number":
        return number_text(text)
    if fmt == "json_array":
        value = parse_json(text)
        if not isinstance(value, list) or any(isinstance(v, (dict, list, bool)) for v in value):
            raise ValueError("答案必须是一维数组")
        values = ["" if v is None else str(v).strip().replace("，", ",") for v in value]
        values = [v.replace(",", "") if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", v) else v for v in values]
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if fmt == "string":
        if text.startswith('"'):
            value = parse_json(text)
            if not isinstance(value, str):
                raise ValueError("答案必须是字符串")
            if not value.strip():
                raise ValueError("答案为空，需复核")
            return value
        if re.search(r"根据.*(?:可知|资料)|依据[：:]|答案[是为]|\[\d+\]", text):
            raise ValueError("字符串答案含解释或引用")
        return text
    raise ValueError(f"不支持格式 {fmt}")


def region(question: str) -> tuple[set[int], set[int]] | None:
    if "完整" in question or "整张" in question or "全部" in question or "整个表格" in question:
        return None
    rows, cols = set(), set()
    if "表头行" in question:
        rows.add(0)
    for noun, selected in (("行", rows), ("列", cols)):
        for match in re.finditer(r"前\s*(\d+)\s*" + noun, question):
            selected.update(range(int(match.group(1))))
        for match in re.finditer(r"第\s*(\d+)\s*" + noun, question):
            selected.add(int(match.group(1)) - 1)
        if "第一" + noun in question or "首" + noun in question:
            selected.add(0)
    if not rows and not cols:
        raise ValueError("未识别结构区域，需明确行列或完整表")
    return rows, cols


def structure_payload(table: dict, question: str) -> dict:
    selection = region(question)
    cells = table["cells"]
    if selection is not None:
        rows, cols = selection
        if "表头行" in question:
            depth = max((c["rowspan"] for c in cells if c["row"] == 0), default=1)
            cursor = 0
            while cursor < depth:
                if any(c["row"] == cursor and c["colspan"] > 1 for c in cells):
                    depth = max(depth, cursor + 2)
                cursor += 1
                if depth >= table["row_count"]:
                    depth = min(depth, table["row_count"])
                    break
            rows.update(range(depth))
        cells = [c for c in cells if any(r in rows for r in range(c["row"], c["row"] + c["rowspan"]))
                 or any(k in cols for k in range(c["col"], c["col"] + c["colspan"]))]
    return {"row_count": table["row_count"], "col_count": table["col_count"], "cells": cells}


def execute_plan(plan: dict, table: dict, fmt: str) -> tuple[str, list[dict]]:
    index = {(c["row"], c["col"]): c for c in table["cells"]}
    coordinates = plan.get("cells")
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError("没有证据单元格")
    evidence = []
    for pos in coordinates:
        if not isinstance(pos, list) or len(pos) != 2 or any(type(v) is not int for v in pos):
            raise ValueError("无效坐标")
        if tuple(pos) not in index:
            raise ValueError("坐标不是实际单元格")
        evidence.append(index[tuple(pos)])
    operation = plan.get("operation")
    if operation != "extract" and len({tuple(pos) for pos in coordinates}) != len(coordinates):
        raise ValueError("计算计划重复选择单元格")
    values = [c["text"] for c in evidence]
    if operation == "extract":
        if fmt != "json_array" and len(values) != 1:
            raise ValueError("单值题只能选一个单元格")
        raw = json.dumps(values, ensure_ascii=False) if fmt == "json_array" else values[0]
    elif operation in {"sum", "subtract", "ratio", "mean", "min", "max", "count"}:
        if fmt != "number":
            raise ValueError("计算计划需输出数值")
        if operation == "count":
            result = Decimal(len(values))
        else:
            numeric = []
            for v in values:
                s = v.strip()
                percent = s.endswith("%")
                n = Decimal(number_text(s[:-1] if percent else s))
                numeric.append(n / 100 if percent else n)
            if operation in {"subtract", "ratio"} and len(numeric) != 2:
                raise ValueError("减法/比例必须两个操作数")
            if operation == "sum": result = sum(numeric, Decimal(0))
            elif operation == "subtract": result = numeric[0] - numeric[1]
            elif operation == "ratio": result = numeric[0] / numeric[1]
            elif operation == "mean": result = sum(numeric, Decimal(0)) / len(numeric)
            elif operation == "min": result = min(numeric)
            else: result = max(numeric)
        scale = plan.get("scale", 1)
        if scale not in (1, 100, 1000, 10000, 1000000):
            raise ValueError("不支持的换算倍数")
        result *= Decimal(str(scale))
        precision = plan.get("decimal_places")
        if precision is not None:
            if type(precision) is not int or not 0 <= precision <= 10:
                raise ValueError("无效精度")
            from decimal import ROUND_HALF_UP
            result = result.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
        raw = format(result, "f")
    else:
        raise ValueError("不支持的操作")
    return format_answer(raw, fmt), evidence


def select_table(chat: Any, doc_id: str, question: str, hint: str, include_related: bool = False) -> tuple[dict, dict, float | None]:
    tables = [c for c in table_store(chat).chunks if c.get("doc_id") == doc_id and c.get("type") == "table"]
    # 新索引每表只有一个父块；旧索引仍可用于诊断，但不能修复已丢失的结构。
    if not tables:
        raise ValueError("文档没有表格")
    query = f"目标表：{hint}\n问题：{question}" if hint else question
    ranked = chat.get_retriever().reranker.rerank(query, [t["text"] for t in tables], min(3, len(tables))) if len(tables) > 1 else [(0, None)]
    if len(tables) > 1 and not any(score is not None for _, score in ranked):
        from app.rag.bm25 import BM25Retriever
        lexical = BM25Retriever(tables).search(query, min(3, len(tables)))
        if lexical:
            ranked = [(i, None) for i, _ in lexical]
    if not ranked:
        raise ValueError("没有目标表候选")
    if len(ranked) > 1:
        summaries = [{"candidate": i, "page": tables[i].get("page"), "title": tables[i].get("table_name", ""),
                      "content": tables[i]["text"][:6000]} for i, _ in ranked]
        raw = chat.llm.complete([
            {"role": "system", "content": "选择与目标表、字段、期间最匹配的表。只输出 JSON {\"candidate\":候选编号}。无法定位输出 {\"candidate\":null}。"},
            {"role": "user", "content": f"目标表：{hint}\n问题：{question}\n候选：{json.dumps(summaries, ensure_ascii=False)}"}
        ], temperature=0.0, max_tokens=1500)
        chosen = parse_json(raw).get("candidate")
        if type(chosen) is not int or chosen not in {i for i, _ in ranked}:
            raise ValueError("目标表不确定")
    else:
        chosen = ranked[0][0]
    chunk = tables[chosen]
    table = normalize_table(chunk.get("table_cells") or chunk["content"], chunk.get("table_name", ""))
    if include_related and re.search(r"期末.*期初|期初.*期末|本期.*上期|上期.*本期", question):
        cells, offset, width = [], 0, 0
        for i in dict.fromkeys([chosen, *[i for i, _ in ranked]]):
            part = normalize_table(tables[i].get("table_cells") or tables[i]["content"], tables[i].get("table_name", ""))
            cells.append({"text": tables[i].get("table_name") or tables[i]["text"][:100], "row": offset, "col": 0, "rowspan": 1, "colspan": part["col_count"]})
            offset += 1
            cells.extend({**c, "row": c["row"] + offset, "source_row": c["row"],
                          "source_table_id": tables[i].get("table_id", tables[i]["chunk_id"])} for c in part["cells"])
            offset += part["row_count"]
            width = max(width, part["col_count"])
        table = {"cells": cells, "row_count": offset, "col_count": width}
    return chunk, table, next(score for i, score in ranked if i == chosen)


PLAN_PROMPT = """只依据给出的表格和问题，输出 JSON 取值/计算计划，不输出最终答案。
格式：{"operation":"extract|sum|subtract|ratio|mean|min|max|count","cells":[[row,col]],"scale":1,"decimal_places":null}
坐标从0开始，只选实际单元格。extract按题目所需顺序选格，空单元格也要选。subtract为第一格减第二格，ratio为第一格除第二格。
计算只选操作数，不重复计入小计和明细。count选所有满足条件的格，每项一次。百分数计算按小数处理，题目要求百分数结果时scale=100。
scale仅用于题目明确要求的单位换算，decimal_places仅用于明确要求的精度。期间、币种、单位必须一致。
不能用支持的操作表达、缺乏证据、需要视觉信息时输出 {"operation":"unsupported","cells":[]}。"""


def answer_test(chat: Any, row: Any) -> dict:
    question, fmt = str(row["question"]), str(row.get("answer_format") or "string")
    hint_value = row.get("table_hint")
    hint = "" if hint_value is None or str(hint_value) == "nan" else str(hint_value)
    doc_id = str(row["file_name"]).rsplit(".", 1)[0]
    if doc_id.isdigit():
        doc_id = f"{int(doc_id):03d}"
    record = {"id": str(row["id"]), "pipeline_version": PIPELINE_VERSION, "answer": "", "status": "failed", "sources": [], "confidence": None}
    chat.llm.reset_call_trace()
    try:
        manifest = table_store(chat).manifest
        if isinstance(manifest, dict) and manifest:
            if manifest.get("complete") is False or any(not d.get("chunks") for d in manifest.get("docs", {}).values()):
                raise RuntimeError("索引不完整，恢复模型额度后请重新入库")
            from app.ingest.pipeline import PARSER_VERSION
            from app.ingest.chunker import CHUNKER_VERSION
            if manifest.get("parser_version") != PARSER_VERSION or manifest.get("chunker_version") != CHUNKER_VERSION:
                raise RuntimeError("索引版本过旧，请先运行 python3 -m app.cli ingest --rebuild")
        if re.search(r"紫色|黄色|绿色|红色|蓝色|颜色|可见|图片基本为空", question) and fmt != "json":
            return visual_answer(chat, row, record)
        chunk, table, score = select_table(chat, doc_id, question, hint, include_related=fmt != "json")
        record.update(table_id=chunk.get("table_id", chunk["chunk_id"]), rerank_top=score,
                      sources=[{"index": 1, "doc_id": doc_id, "type": "table", "page": chunk.get("page"), "score": score, "content": chunk["content"]}])
        if fmt == "json":
            record["answer"] = json.dumps(structure_payload(table, question), ensure_ascii=False, separators=(",", ":"))
            record["method"] = "structure"
        else:
            context = {"source": chunk.get("text", "")[:1000], "caption": chunk.get("caption", ""),
                       "headers": column_headers(table["cells"], table["row_count"], table["col_count"]), "cells": table["cells"]}
            raw = chat.llm.complete([
                {"role": "system", "content": PLAN_PROMPT},
                {"role": "user", "content": f"问题：{question}\n答案格式：{fmt}\n表名：{hint}\n表格：{json.dumps(context, ensure_ascii=False)}"}
            ], temperature=0.0, max_tokens=1500)
            record["raw_plan"] = raw
            try:
                plan = parse_json(raw)
                record["answer"], record["evidence"] = execute_plan(plan, table, fmt)
                record["method"] = "cell_plan"
            except (ValueError, TypeError, KeyError, InvalidOperation, ZeroDivisionError):
                # 复杂文本/条件题仍用同一目标表，严格输出，避免换表。
                raw = chat.llm.complete(chat._build_structured_messages(question, json.dumps(context, ensure_ascii=False), fmt), temperature=0.0, max_tokens=2048)
                record["raw_answer"] = raw
                record["answer"] = format_answer(raw, fmt)
                record["method"] = "table_generation"
        record["status"] = "success"
    except Exception as exc:
        record["error"] = str(exc)[:500]
        if not isinstance(exc, RuntimeError) and fmt != "json" and str(row["file_name"]).lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            try:
                return visual_answer(chat, row, record)
            except Exception as visual_error:
                record["visual_error"] = str(visual_error)[:500]
    finally:
        record["model_calls"] = chat.llm.call_trace()
    return record


def visual_answer(chat: Any, row: Any, record: dict) -> dict:
    from pathlib import Path
    from app.ingest.image_parser import prepare_image_bytes
    from app.ingest.pdf_parser import render_page_jpeg
    path = (chat.cfg.files_path / str(row["file_name"])).resolve()
    if not path.exists() and path.stem.isdigit():
        path = (chat.cfg.files_path / (f"{int(path.stem):03d}" + path.suffix)).resolve()
    if path.parent != chat.cfg.files_path.resolve():
        raise ValueError("来源文件路径无效")
    page = 1
    if path.suffix.lower() == ".pdf":
        sources = record.get("sources") or chat.get_retriever().retrieve(str(row["question"]), doc_ids=[path.stem])
        if not sources:
            raise ValueError("无法定位原图页码")
        page = int(sources[0].get("page") or 1)
        data = render_page_jpeg(path, page, scale=3.0, quality=95)
    else:
        data = prepare_image_bytes(path, max_side=3200)
    fmt = str(row["answer_format"])
    question = str(row["question"])
    if re.search(r"颜色|紫色|黄色|绿色|红色|蓝色|位置|布局|最左|最右|表头|结构|Day\s*\d|上午|下午|晚上", question, re.I):
        recognized = chat.llm.vision_describe(data, prompt=f"识别与问题有关的原图文字、表格、颜色及位置，保留编号和数值，不计算。问题：{question}", max_tokens=4096)
    else:
        recognized = chat.llm.ocr_read(data, prompt=f"只识别原图中与问题相关的文字和数值，保留原文，不猜测缺失值。问题：{question}")
    raw = chat.llm.complete(chat._build_structured_messages(str(row["question"]), recognized, fmt), temperature=0.0, max_tokens=2048)
    record.update(answer=format_answer(raw, fmt), status="success", method="visual", raw_answer=raw, visual_page=page)
    record.pop("error", None)
    return record

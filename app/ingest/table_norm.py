"""表格规范化：HTML/二维行 -> 逻辑网格 -> 规范 HTML / Markdown / 行检索文本。

视觉模型常为合并表头补出大量空填充单元格（13 物理列实为 3 逻辑列），
本模块用"占用矩阵 + 真实单元格"还原逻辑网格：
- 合并单元格只保留左上角（带 rowspan/colspan），被覆盖位置不输出；
- 整列/整行从未被任何真实单元格覆盖时视为填充列/行，直接丢弃；
- 丢弃后重算行列下标与整表逻辑行列数。
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, Iterable

TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<t([dh])([^>]*)>(.*?)</t\1>", re.IGNORECASE | re.DOTALL)
TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
FENCE_TABLE_RE = re.compile(
    r"```(?:html)?\s*((?:\s*<table\b.*?</table>)+)\s*```",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------- 基础工具


def _attr(attrs: str, name: str) -> int:
    match = re.search(name + r'\s*=\s*["\']?(\d+)', attrs, re.IGNORECASE)
    if not match:
        return 1
    return max(1, int(match.group(1)))


def clean_cell_text(raw: str) -> str:
    text = TAG_RE.sub(" ", raw or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return " ".join(text.split())


def _looks_like_html_table(text: str) -> bool:
    return "<table" in (text or "").lower()


# --------------------------------------------------------------------- HTML 解析


def parse_html_cells(html_text: str) -> list[dict[str, Any]]:
    """解析 HTML 表格为真实单元格列表（未被合并覆盖的位置不产生单元格）。"""
    cells: list[dict[str, Any]] = []
    occupied: dict[tuple[int, int], int] = {}
    for row_index, tr in enumerate(TR_RE.findall(html_text or "")):
        col_cursor = 0
        for tag, attrs, inner in CELL_RE.findall(tr):
            text = clean_cell_text(inner)
            rowspan = _attr(attrs, "rowspan")
            colspan = _attr(attrs, "colspan")
            while (row_index, col_cursor) in occupied:
                col_cursor += 1
            cell_id = len(cells)
            cells.append(
                {
                    "text": text,
                    "row": row_index,
                    "col": col_cursor,
                    "rowspan": rowspan,
                    "colspan": colspan,
                }
            )
            for dr in range(rowspan):
                for dc in range(colspan):
                    occupied.setdefault((row_index + dr, col_cursor + dc), cell_id)
            col_cursor += colspan
    return cells


def _rows_to_cells(rows: Iterable[Iterable[str]]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            text = " ".join(str(value if value is not None else "").split())
            cells.append(
                {"text": text, "row": row_index, "col": col_index, "rowspan": 1, "colspan": 1}
            )
    return cells


# --------------------------------------------------------------------- 网格收缩


def compact_cells(cells: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """丢弃从未被真实单元格覆盖的填充行列，返回 (cells, 逻辑行数, 逻辑列数)。"""
    if not cells:
        return [], 0, 0

    used_rows: set[int] = set()
    used_cols: set[int] = set()
    for cell in cells:
        for dr in range(cell["rowspan"]):
            used_rows.add(cell["row"] + dr)
        for dc in range(cell["colspan"]):
            used_cols.add(cell["col"] + dc)

    row_map = {old: new for new, old in enumerate(sorted(used_rows))}
    col_map = {old: new for new, old in enumerate(sorted(used_cols))}

    compacted: list[dict[str, Any]] = []
    for cell in cells:
        if cell["row"] not in row_map or cell["col"] not in col_map:
            continue
        compacted.append(
            {
                "text": cell["text"],
                "row": row_map[cell["row"]],
                "col": col_map[cell["col"]],
                "rowspan": cell["rowspan"],
                "colspan": cell["colspan"],
            }
        )
    compacted.sort(key=lambda item: (item["row"], item["col"]))
    return compacted, len(row_map), len(col_map)


# --------------------------------------------------------------------- 输出格式


def cells_to_html(cells: list[dict[str, Any]], row_count: int, col_count: int) -> str:
    """规范 HTML：只含真实单元格，合并信息用 rowspan/colspan 表达。"""
    grid: list[list[dict[str, Any] | None]] = [[None] * col_count for _ in range(row_count)]
    for cell in cells:
        for dr in range(cell["rowspan"]):
            for dc in range(cell["colspan"]):
                r, c = cell["row"] + dr, cell["col"] + dc
                if 0 <= r < row_count and 0 <= c < col_count:
                    grid[r][c] = cell if (dr == 0 and dc == 0) else {"skip": True}

    lines = ["<table>"]
    for row in grid:
        pieces = ["<tr>"]
        for item in row:
            if item is None:
                pieces.append("<td></td>")
                continue
            if item.get("skip"):
                continue
            attrs = ""
            if item["rowspan"] > 1:
                attrs += f' rowspan="{item["rowspan"]}"'
            if item["colspan"] > 1:
                attrs += f' colspan="{item["colspan"]}"'
            pieces.append(f"<td{attrs}>{html_lib.escape(item['text'], quote=False)}</td>")
        pieces.append("</tr>")
        lines.append("".join(pieces))
    lines.append("</table>")
    return "".join(lines)


def cells_to_matrix(cells: list[dict[str, Any]], row_count: int, col_count: int) -> list[list[str]]:
    matrix = [["" for _ in range(col_count)] for _ in range(row_count)]
    for cell in cells:
        r, c = cell["row"], cell["col"]
        if 0 <= r < row_count and 0 <= c < col_count:
            matrix[r][c] = cell["text"]
    return matrix


def cells_to_markdown(cells: list[dict[str, Any]], row_count: int, col_count: int) -> str:
    matrix = cells_to_matrix(cells, row_count, col_count)
    if not matrix:
        return ""
    header = matrix[0]
    lines = ["| " + " | ".join(cell or " " for cell in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in matrix[1:]:
        lines.append("| " + " | ".join(cell or " " for cell in row) + " |")
    return "\n".join(lines)


def cells_to_row_texts(
    cells: list[dict[str, Any]],
    row_count: int,
    col_count: int,
    table_name: str = "",
) -> list[str]:
    """每行生成检索文本：`表名 列名 值 列名 值 …`（跳过空值）。"""
    matrix = cells_to_matrix(cells, row_count, col_count)
    if not matrix:
        return []
    header = column_headers(cells, row_count, col_count)
    prefix = f"{table_name} " if table_name else ""
    texts: list[str] = []
    for row in matrix:
        parts: list[str] = []
        for index, value in enumerate(row):
            if not value:
                continue
            column = header[index] if index < len(header) else ""
            parts.append(f"{column} {value}".strip() if column and value != column else value)
        if parts:
            texts.append(f"{prefix}{' '.join(parts)}".strip())
    return texts


def column_headers(cells: list[dict[str, Any]], row_count: int, col_count: int) -> list[str]:
    """按列取表头：第一行优先；仅当第一行该列为合并表头时，才用第二行叶子表头细化。"""
    paths = [[] for _ in range(col_count)]
    active = set(range(col_count))
    for row in range(row_count):
        next_active = set()
        for cell in cells:
            if cell["row"] != row:
                continue
            for col in range(cell["col"], cell["col"] + cell["colspan"]):
                if col not in active or col >= col_count:
                    continue
                if cell["text"] and (not paths[col] or paths[col][-1] != cell["text"]):
                    paths[col].append(cell["text"])
                if cell["colspan"] > 1:
                    next_active.add(col)
        active = next_active
        if not active:
            break
    return ["/".join(parts) for parts in paths]


# --------------------------------------------------------------------- 主入口


def normalize_cells(cells: list[dict[str, Any]], table_name: str = "") -> dict[str, Any]:
    compacted, row_count, col_count = compact_cells(cells)
    return {
        "cells": compacted,
        "row_count": row_count,
        "col_count": col_count,
        "html": cells_to_html(compacted, row_count, col_count),
        "markdown": cells_to_markdown(compacted, row_count, col_count),
        "matrix": cells_to_matrix(compacted, row_count, col_count),
        "row_texts": cells_to_row_texts(compacted, row_count, col_count, table_name),
    }



def normalize_table(source: str | list[list[str]] | list[dict[str, Any]], table_name: str = "") -> dict[str, Any]:
    """把 HTML 表格或二维行数据规范化为逻辑网格。"""
    if isinstance(source, list):
        if source and isinstance(source[0], dict):
            return normalize_cells(source, table_name)  # type: ignore[arg-type]
        return normalize_cells(_rows_to_cells(source), table_name)  # type: ignore[arg-type]
    return normalize_cells(parse_html_cells(str(source or "")), table_name)


# --------------------------------------------------------------------- 从 VLM 输出抽取表格


def extract_tables(markdown: str) -> tuple[list[str], str]:
    """从 VLM 输出中抽出所有 <table> HTML，返回 (表格 HTML 列表, 去掉表格后的正文)。"""
    text = FENCE_TABLE_RE.sub(lambda match: match.group(1), str(markdown or ""))
    text = re.sub(r"```(?:html)?\s*(?=<table\b)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(</table>)\s*```", r"\1", text, flags=re.IGNORECASE)
    tables = [match.group(0).strip() for match in TABLE_RE.finditer(text)]
    prose = TABLE_RE.sub("\n", text)
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    return tables, prose


# 强表名特征：出现这些词且行内无冒号（排除"法定代表人：xxx"这类表单行）
TITLE_HINTS = (
    "利润表", "资产负债表", "现金流量表", "明细表", "余额表", "变动表", "减值", "折旧",
    "应收账款", "应付账款", "其他应收", "存货", "固定资产", "在建工程", "营业收入",
    "营业成本", "期间费用", "管理费用", "销售费用", "财务费用", "研发费用", "股本",
    "资本公积", "未分配利润", "长期借款", "短期借款", "职工薪酬", "应交税费", "附注",
)
SKIP_TITLE_RE = re.compile(
    r"^(页码|第\s*\d+\s*页|注[:：]|单位[:：]|数据来源|资料来源|\d+[、.．])|[:：]"
    r"|^[（(]|后附|组成部分|以下|如上|如下|签字|盖章|法定代表人|主管会计"
)
CAPTION_RE = re.compile(r"<caption[^>]*>(.*?)</caption>", re.IGNORECASE | re.DOTALL)


def guess_table_name(prose: str, matrix: list[list[str]], fallback: str = "") -> str:  # noqa: D401
    """猜表名：优先强特征表题行（无冒号、含财报表名/科目词），否则退回表头首行。"""
    lines = [item.strip() for item in str(prose or "").splitlines() if item.strip()]
    best = ""
    for line in lines:
        candidate = re.sub(r"^[#>*\-•\d\.、\s]+", "", line).strip("：: ")
        if not (2 <= len(candidate) <= 25) or candidate.startswith("<"):
            continue
        if SKIP_TITLE_RE.search(candidate):
            continue
        if any(hint in candidate for hint in TITLE_HINTS):
            best = candidate  # 取最后一个命中的标题行（离表格最近）
    if best:
        return best
    if matrix and matrix[0]:
        head = " ".join(cell for cell in matrix[0][:3] if cell).strip()
        if head:
            return head[:40]
    return fallback


def caption_of(html_text: str) -> str:
    match = CAPTION_RE.search(html_text or "")
    if not match:
        return ""
    return " ".join(clean_cell_text(match.group(1)).split())[:40]


# --------------------------------------------------------------------- 解析块组装


def blocks_from_markdown(
    markdown: str,
    doc_id: str,
    page: int,
    image_path: str | None = None,
    scan: bool = False,
) -> list[Any]:
    """把 VLM 输出的整页 Markdown 拆成：规范化表格块 + 正文块（保持可检索粒度）。"""
    from app.ingest.models import ParsedBlock

    tables, prose = extract_tables(markdown)
    blocks: list[Any] = []
    for index, table_html in enumerate(tables, start=1):
        norm = normalize_table(table_html)
        if norm["row_count"] < 2 or norm["col_count"] < 2:
            continue
        name = caption_of(table_html) or guess_table_name(prose, norm["matrix"], fallback=f"表格{index}")
        blocks.append(
            ParsedBlock(
                type="table",
                page=page,
                rows=norm["matrix"],
                html=norm["html"],
                name=name,
                scan=scan,
                caption=f"{doc_id} 第{page}页 {name}（{norm['row_count']}行{norm['col_count']}列）",
            )
        )

    if prose.strip():
        blocks.append(
            ParsedBlock(
                type="image",
                page=page,
                text=prose.strip(),
                image_path=image_path,
                scan=scan,
                caption=f"{doc_id} 第{page}页 页面描述",
            )
        )
    return blocks

import unittest
from unittest.mock import Mock

from app.ingest.table_norm import normalize_table, column_headers
from app.ingest.chunker import chunk_document
from app.ingest.models import ParsedDoc, ParsedBlock
from app.rag.table_answer import execute_plan, number_text, format_answer, structure_payload, answer_test
from app.rag.bm25 import BM25Retriever


class TableAccuracyTests(unittest.TestCase):
    def test_empty_cells_roundtrip_and_zero(self):
        table = normalize_table([["项目", "2024", "2023", "空列"], ["A", "", "100", ""], ["B", 0, "80", ""]])
        again = normalize_table(table["html"])
        self.assertEqual(again["matrix"], table["matrix"])
        self.assertEqual(again["matrix"][1], ["A", "", "100", ""])
        self.assertEqual(again["matrix"][2][1], "0")

    def test_merged_header_paths_and_coordinates(self):
        html = '<table><tr><td rowspan="2">类别</td><td colspan="2">期末余额</td><td colspan="2">期初余额</td></tr><tr><td>金额</td><td>比例</td><td>金额</td><td>比例</td></tr><tr><td>A</td><td>100</td><td>10</td><td>80</td><td>8</td></tr></table>'
        table = normalize_table(html)
        self.assertEqual(column_headers(table["cells"], 3, 5), ["类别", "期末余额/金额", "期末余额/比例", "期初余额/金额", "期初余额/比例"])
        self.assertEqual(normalize_table(table["html"])["cells"], table["cells"])
        payload = structure_payload(table, "恢复第一行和前1列")
        self.assertEqual(payload["col_count"], 5)
        self.assertEqual([(c["row"], c["col"]) for c in payload["cells"]], [(0, 0), (0, 1), (0, 3), (2, 0)])

    def test_three_level_headers(self):
        table = normalize_table('<table><tr><td colspan="2">2024</td></tr><tr><td colspan="2">收入</td></tr><tr><td>国内</td><td>海外</td></tr><tr><td>1</td><td>2</td></tr></table>')
        self.assertEqual(column_headers(table["cells"], 4, 2), ["2024/收入/国内", "2024/收入/海外"])

    def test_blank_column_survives(self):
        table = normalize_table([["A", "", "C"], ["1", "", "3"]])
        self.assertEqual(table["col_count"], 3)
        self.assertEqual(normalize_table(table["html"])["matrix"], table["matrix"])

    def test_strict_number(self):
        self.assertEqual(number_text("-1,234.50"), "-1234.50")
        for value in ["4+11+18=33", "2024年金额33", "12,34", "33元"]:
            with self.assertRaises(ValueError): number_text(value)

    def test_decimal_calculation_and_array_order(self):
        table = normalize_table([["项目", "期末", "期初"], ["A", "0.3", "0.1"], ["B", "", "100"]])
        result, evidence = execute_plan({"operation": "subtract", "cells": [[1, 1], [1, 2]]}, table, "number")
        self.assertEqual(result, "0.2")
        self.assertEqual(len(evidence), 2)
        result, _ = execute_plan({"operation": "extract", "cells": [[2, 2], [2, 1]]}, table, "json_array")
        self.assertEqual(result, '["100",""]')
        with self.assertRaises(ValueError): execute_plan({"operation": "sum", "cells": [[99, 1]]}, table, "number")

    def test_percent_precision(self):
        table = normalize_table([["A", "B"], ["1", "3"]])
        result, _ = execute_plan({"operation": "ratio", "cells": [[1, 0], [1, 1]], "scale": 100, "decimal_places": 2}, table, "number")
        self.assertEqual(result, "33.33")

    def test_bm25_filters_before_top_k(self):
        index = BM25Retriever([{"text": "unique target", "doc_id": "other"}, {"text": "unique", "doc_id": "target"}, {"text": "unrelated", "doc_id": "third"}])
        self.assertEqual(index.search("unique", 1, doc_ids=["target"])[0][0], 1)

    def test_long_table_preserves_full_parent(self):
        rows = [["项目", "值"]] + [[str(i), "long-value" * 10] for i in range(100)]
        chunks = chunk_document(ParsedDoc("001", "001.pdf", "pdf", [ParsedBlock("table", 1, rows=rows)]))
        parents = [c for c in chunks if c["type"] == "table"]
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]["row_count"], 101)
        self.assertEqual(len([c for c in chunks if c["type"] == "table_row"]), 100)

    def test_answer_entry_plan(self):
        chat = Mock()
        chat.get_store.return_value.chunks = [{"doc_id": "001", "type": "table", "chunk_id": "t1", "content": '<table><tr><td>A</td><td>B</td></tr><tr><td>x</td><td>33</td></tr></table>', "text": "A B x 33", "page": 1}]
        chat.llm.complete.return_value = '{"operation":"extract","cells":[[1,1]]}'
        result = answer_test(chat, {"id": 1, "file_name": "001.pdf", "question": "x对应的B", "answer_format": "number", "table_hint": "A B"})
        self.assertEqual(result["answer"], "33")
        self.assertEqual(result["method"], "cell_plan")
        self.assertEqual(result["evidence"][0]["col"], 1)

    def test_unknown_region_and_nested_array_rejected(self):
        with self.assertRaises(ValueError): structure_payload(normalize_table([["A", "B"]]), "恢复特定区域")
        with self.assertRaises(ValueError): format_answer('[[1,2]]', "json_array")


if __name__ == "__main__":
    unittest.main()

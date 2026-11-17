# RAG 抽样评测报告

- 生成时间：2026-11-16 11:27:52
- 抽样：30 题（seed=42，按 question_type 分层）

## 指标

| 指标 | 数值 |
| --- | --- |
| 平均置信度 | 0.579 |
| 空答案率 | 0.0 |
| 引用命中率 | 0.467 |
| table_hint 命中率 | 0.4 |
| 精排分 p50 / min / max | 0.851 / 0.353 / 1.0 |

## 逐题明细（请人工核对 answer 列）

| id | 题型 | 格式 | 置信度 | 精排 | hint命中 | 问题 | 答案 | 来源 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 25 | structure | json | 0.42 | 0.719 | 否 | 应收账款表，恢复一下 第一行 和 前1列的表格结构 | {   "row_count": 6,   "col_count": 5,   "cells": [     {"text": "类别", "row": 0, "col": 0, "rowspan": 1, "colspan": 1},   | 006#table、006#table_row、006#table |
| 439 | extract | string | 0.42 | 0.8754 | 否 | 接送地点是哪里？ | 接送地点是：**上海工业园区88号** [2]。   该信息明确出现在表格的倒数第二行，对应列标题为“Pick-Up Locations 接送地点”，其右侧单元格内容为“上海工业园区88号” [2]。 | 046#image、046#table、046#table_row |
| 397 | extract | number | 0.73 | 0.8921 | 否 | 请提取“Women case count”对应的值。 | 363 | 040#table、040#table_row、040#table_row |
| 99 | extract | string | 0.69 | 0.5783 | 是 | 该文件中主表或主区域的标题是什么？ | 该文件中主表或主区域的标题是“课程基本信息（Course Information）”[2]。    依据参考资料[2]，其所在表格上方明确标注了该表格的标题为“课程基本信息（Course Information）”，且该表格完整呈现了课程代 | 011#image、011#table、011#table_row |
| 260 | extract | string | 0.56 | 0.3534 | 是 | 该表主要使用哪种语言或语言组合？ | 该表主要使用**简体中文与英文组合**的语言形式。  具体表现为： - 表格标题、行分类标题（如“文档解析”“文本识别”“公式识别”“表格识别”“信息抽取”）均为**简体中文** [5]； - 各测试基准名称（如“OmniDocBench  | 027#table、027#table_row、027#table_row |
| 140 | extract | string | 0.36 | None | 是 | 该表主要使用哪种语言或语言组合？ | 该表主要使用**英语**，辅以少量数字、单位和括号格式的符号化表达，无其他自然语言混用 [1]。  具体依据如下：   - 所有字段标题（如 “No. Of Turns”、“Turns Material”、“Shape”、“Material | 015#image |
| 151 | extract | number | 0.5 | 0.4014 | 是 | 请给出该表可见主表的列数；如果图片基本为空则填写 0。 | 2 | 016#table、016#table_row、016#table_row |
| 763 | extract | number | 0.75 | 0.9334 | 是 | 请从 79.png 的“Amazon consolidated statements of cash flows”中提取 | -27819 | 079#table_row、079#table、079#table_row |
| 531 | extract | number | 0.37 | 0.369 | 否 | 请给出该表可见主表的列数；如果图片基本为空则填写 0。 | 6 | 056#image |
| 752 | extract | number | 0.73 | 0.999 | 是 | 请从 78.png 的“Amazon consolidated statements of operations”中提取 | 107006 | 078#table、078#table_row、078#table_row |
| 412 | extract | string | 0.82 | 0.8367 | 否 | 该表最左侧的表头或字段名是什么？ | 该表最左侧的表头或字段名是「分类」[1]。    依据参考资料[1]中提供的表格结构，第一列的表头明确为「分类」，其下方内容为各能力维度（如“产品能力”“应用能力”“生态能力”等），且所有后续行数据均以「分类」作为最左侧字段进行组织 [1] | 043#table、043#table_row、043#table_row |
| 163 | extract | string | 0.8 | 0.7843 | 是 | 该表最右侧的表头或字段名是什么？ | 该表最右侧的表头或字段名是 **CC?** [1]。    依据参考资料 [1] 中提供的完整表格，其表头行为：  `<tr><td>Ranking</td><td>Pts</td><td>School</td><td>District</ | 017#table、017#table_row、017#table_row |
| 682 | extract | string | 0.3 | 0.6731 | 否 | 请从 71.pdf 的“表1 工业纯钛、α型和近α型钛合金牌号和化学成分”中提取：TA6 的 Al 范围。 | 当前知识库资料不足以回答该问题。  理由：参考资料 [1]（来源标识：071｜071.pdf 第1页｜表格）虽标明来自“071.pdf 第1页”，并标注为表格，但所提供的 HTML 表格内容中： - 表头文字为乱码或不可识别符号（如 `!$ | 071#table、071#table_row、071#table_row |
| 644 | extract | number | 0.74 | 0.9995 | 否 | 请从 67.pdf 的“财务预测与估值”中提取：2026E 负债合计。 | 22388 | 067#table_row、067#table、067#table_row |
| 332 | extract | string | 0.52 | 0.8505 | 否 | 该表最左侧的表头或字段名是什么？ | 该表最左侧的表头或字段名是 **“项目”**。  依据参考资料 [4] 中的「四、任职信息」部分，其表格结构为：  ／ 项目 ／ 内容 ／ ／------／------／ ／ 工号 ／ 1 ／ ／ 入职公司 ／ 重庆因瀚信息技术有限公司 ／ | 034#table_row、034#table_row、034#table |
| 282 | extract | string | 0.89 | 0.8851 | 否 | 该表最左侧的表头或字段名是什么？ | 该表最左侧的表头（字段名）是 **“Fag”**，意为“科目”[1][2]。    这一结论在多处资料中得到一致确认：   - [1] 明确指出：“第一列：‘Fag’（科目），含课程名称及编号（如 Matematikk R1）”；   -  | 029#image、029#table、029#table_row |
| 583 | extract | number | 0.31 | 0.9192 | 是 | 请从 61.pdf 的“Table 12 中文功能写作评估”中提取：Everyday Writing 中 Reflect | 68 | 061#text |
| 373 | extract | string | 0.81 | 0.8122 | 否 | 该表最右侧的表头或字段名是什么？ | 该表最右侧的表头或字段名是 **“Levels +1”** [2]。  依据来源标识 038｜038.png 第1页的表格结构 [2]，其表头（即第一行）从左到右依次为：   `Factor #`、`Factor\nComplete nam | 038#table_row、038#table、038#table_row |
| 705 | extract | string | 0.3 | 0.5133 | 否 | 请从 73.pdf 的““表2（续）”中Mo元素含量大于 0.5 的有多少个? | 当前知识库资料不足以回答该问题。  理由如下：  - 用户所提问题涉及「73.pdf 的“表2（续）”中 Mo 元素含量大于 0.5 的个数」，但所提供的参考资料 [1] 和 [2] 均未明确标注为「表2（续）」，也未说明其对应的是哪张表（ | 073#table、073#table、073#table_row |
| 770 | extract | number | 0.31 | 0.9818 | 否 | 请从 80.png 的“Historical Results financial statements”中提取：2013 | 181210 | 080#image |
| 532 | extract | string | 0.78 | 0.8348 | 否 | 该表最左侧的表头或字段名是什么？ | 该表最左侧的表头或字段名是 **“时间段”** [1]。 | 056#image |
| 36 | extract | json_array | 0.74 | 0.9977 | 否 | 坏账表中，期初余额中单项计提坏账准备的金额和计提比例是分别是多少？ | [4881245.42, 100.00] | 006#table、006#table_row、006#table |
| 833 | extract | string | 0.87 | 0.9825 | 是 | 请从 86.png 的“entry field value table”中提取：entry_id 为 5 的 count | 从参考资料中提取：entry_id 为 5 的 `country` 字段值为 **India**。  依据如下： - 在 [1] 中明确指出：`id 50 entry_id 5 field_id fld_3009006 slug count | 086#table_row、086#table、086#image |
| 866 | thinking | number | 0.38 | 0.9039 | 否 | 请计算紫色区域内黄色编号 9 和 10 的和。 | 19 | 089#image、089#table、089#table_row |
| 228 | thinking | json_array | 0.43 | 0.9879 | 是 | 请按表中顺序列出以下字段的答案：Total Malaria visit non-user、Total Non-malar | ["2175", "3418", "34.7", 21] | 023#table、023#table_row、023#table_row |
| 101 | thinking | number | 0.44 | 0.4367 | 是 | 请给出该表可见主表的列数；如果图片基本为空则填写 0。 | 6 | 011#table、011#table_row、011#table_row |
| 40 | thinking | number | 0.33 | 0.8489 | 否 | 坏账表中，单项计提坏账准备的金额期末余额与期初余额的差值是多少？ | 8069762.03 | 006#table_row、006#table、006#table |
| 47 | thinking | string | 0.81 | 0.7431 | 否 | 单项计提坏账准备表，坏账准备最高的是哪家公司? | 根据参考资料，单项计提坏账准备的应收账款中，**坏账准备金额最高的是新疆蓝海新能源有限公司**，其坏账准备为 **5,264,881.02 元**。  依据如下：  - 参考资料 [3] 的表格明确列示了单项计提坏账准备的各公司明细：    | 006#text、006#table_row、006#table |
| 768 | thinking | number | 0.78 | 0.9459 | 是 | 请计算 2017 Sales and maturities of marketable securities 减 Pur | -3789 | 079#table、079#table_row、079#table_row |
| 867 | thinking | number | 0.47 | 0.9442 | 否 | 请计算所有绿色块编号 4、11、18 的和。 | 4+11+18=33 | 089#image、089#table、089#table_row |

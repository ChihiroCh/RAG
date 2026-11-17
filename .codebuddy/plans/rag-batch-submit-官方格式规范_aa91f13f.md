---
name: rag-batch-submit-官方格式规范
overview: 把官方提交规范（三类 question_type 的定义与输出格式要求）写入 rag-batch-submit Skill，并同步调整 run_submit.py 的答案后处理逻辑使其符合官方要求。
todos:
  - id: rewrite-format-md
    content: 按官方规范重写 references/format.md（题型定义、structure JSON 规则与示例、通用答案规范）
    status: completed
  - id: update-skill-md
    content: 更新 SKILL.md：三类 question_type 定义与官方口径的格式要点、校验检查点
    status: completed
    dependencies:
      - rewrite-format-md
  - id: align-run-submit
    content: 调整 run_submit.py：clean_answer 剥离说明性文字、structure_answer 过滤占位格并收缩空填充列、归一化提示词对齐官方规则
    status: completed
    dependencies:
      - rewrite-format-md
  - id: verify-package
    content: 用 --ids 复测四类格式输出，quick_validate 校验并重新打包 dist/rag-batch-submit.zip
    status: completed
    dependencies:
      - align-run-submit
---

## Product Overview

将官方提交规范（三类题型定义与输出格式要求）写入 `rag-batch-submit` Skill，使 Skill 文档与脚本后处理逻辑完全对齐官方要求。

## Core Features

- **题型定义写入 SKILL.md**：`structure`（表格结构恢复：还原行列结构、单元格文本、位置与合并关系）、`extract`（内容提取：抽取指定单元格/行/列/区域）、`thinking`（内容推理：定位数据后做加总、求差、比例、筛选、排序）
- **结构恢复题格式**：answer 为合法 JSON（`row_count`/`col_count`/`cells` 必填；`text`/`row`/`col`/`rowspan`/`colspan` 均必填）；行列从 0 开始；每个真实单元格只输出一次，合并单元格只输出左上角，不输出被覆盖位置的占位格；局部恢复时 `row_count`/`col_count` 仍填完整表格逻辑行列数，cells 只含要求区域；含官方示例（完整 + 局部）
- **提取/推理题格式**：单值直接填最终值；一行/列/区域填 JSON 数组；数组空值填 `""`
- **通用答案规范**：id 与题目清单一致；文本去多余空格换行；数字去千分位逗号（125000 而非 125,000）；不写"根据表格可知""答案是"等说明性文字；日期金额百分比单位按题目要求

## 修改范围

- `SKILL.md`：补充三类题型定义、按官方规范修订"答案格式规则"要点
- `references/format.md`：以官方原文规则重写（替换现存的"从参考答案反推"语义）
- `scripts/run_submit.py`：后处理逻辑对齐官方规范

## Tech Stack

- 沿用现有 Skill 结构（SKILL.md + references/ + scripts/），无新增依赖
- 脚本继续使用 pandas + openpyxl + 项目内 `app.rag.qa.RAGChat`

## Implementation Approach

### 文档层

1. **`references/format.md` 重写**：以官方规范为准绳，替换现有"从模板参考答案反推"的描述：

- 题型定义表（structure/extract/thinking 三类任务说明）
- structure JSON 完整规则 + 官方两个示例（4×3 表完整恢复与局部恢复），字段说明表
- extract/thinking 格式规则与通用答案规范清单（逐条照录官方要求）

2. **`SKILL.md` 更新**：背景段落加入三类 `question_type` 定义；"答案格式规则"要点改为官方口径（structure JSON 字段、合并单元格左上角、无占位格）；"校验输出"步骤补充官方检查点（无说明性文字、数字无千分位、空值用 `""`）

### 脚本层（run_submit.py）

1. **`clean_answer`**：剥离说明性前缀——在 `STRIP_LINES` 与行内正则中增加"根据表格可知/由表可知/从表格可知/答案是/答案为/综上"等模式的去除
2. **`structure_answer`**：

- 过滤空文本占位单元格（VLM HTML 中大量 `""` 填充格），只保留真实单元格（合并单元格天然只出现在左上角）
- 输出前对网格做"全空填充列收缩"：整列均为空文本的列不计入 `col_count`（降低物理布局与逻辑列数的偏差，如 13 物理列 → 3 逻辑列的场景）；行同理处理全空行
- `rowspan`/`colspan` 仍从 HTML 属性透传

3. **`normalize_json_array` / `ARRAY_SYSTEM_PROMPT`**：提示词加入官方规则（空值 `""`、数值无千分位、禁止说明性文字）
4. **`format_number`**：已实现去千分位，保持不变；百分比/单位按题目要求保留现有口径

### 验证

- 用 `--ids 1,25,32`（structure）与 id 2（string）复测：结构题输出无空占位格、`col_count` 为收缩后逻辑列数、JSON 合法
- 用 `quick_validate.py` 校验 Skill 结构，重新打包 `dist/rag-batch-submit.zip`（排除 `__pycache__`）

## Performance

- 结构题的空列收缩为 O(rows×cols) 网格遍历，开销可忽略
- 不新增 LLM 调用（仅提示词文本调整）

## Blast Radius

- 仅改 Skill 三个文件；`app/` 主代码不动（此前修复的 faiss_store 过滤逻辑保持）
- `clean_answer` 前缀剥离采用保守正则，避免误伤以"答案"开头的正常文本（仅在行首匹配固定短语）
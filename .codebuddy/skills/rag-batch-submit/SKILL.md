---
name: rag-batch-submit
description: 批量回答天池竞赛测试集 tests.xlsx 中的 908 道题，并将答案按 answer_format 规范写入 submit-template.xlsx 的副本生成提交文件。当用户要求"生成提交文件"、"跑测试集批量答题"、"按模板填写答案"时使用此技能。
---

# 批量答题生成提交文件

将测试集问题交给本项目的多模态 RAG 知识库（向量召回 + rerank + qwen 生成）批量作答，按竞赛格式要求输出 Excel 提交文件。

## 背景

- 项目根目录有 `tests.xlsx`（908 题：extract 652 / thinking 216 / structure 39）与 `submit-template.xlsx`（id|answer 两列模板）。
- 知识库入口是 `app.rag.qa.RAGChat`（索引在 `data/index/`，需已执行过 `python3 -m app.cli ingest`）。
- 每题通过 `doc_ids=[file_name 主干]` 只检索对应来源文件，避免跨文档污染。

`question_type` 三类题型：

| 题型 | question_type | 任务说明 |
| --- | --- | --- |
| 表格结构恢复 | `structure` | 还原表格行列结构、单元格文本、单元格位置和合并关系 |
| 表格内容提取 | `extract` | 从目标表格中抽取指定单元格、行、列或区域 |
| 表格内容推理 | `thinking` | 先定位相关数据，再完成加总、求差、比例、筛选或排序等简单处理 |

## 工作流

1. **核对输入**：确认 `tests.xlsx` 可用 pandas 读取、`submit-template.xlsx` 是合法 xlsx（`load_workbook` 不报错）。若模板损坏（文件只有几十字节、报 `BadZipFile`），从 `/Users/chenyun/Desktop/天池/multimodal_table_recognition/submit-template.xlsx` 恢复后重试；不要手写重建模板。
2. **试跑**：先执行 `--limit 3 --out /tmp/submit-test.xlsx` 覆盖三种题型各若干，人工检查输出格式（见 `references/format.md`）。
3. **全量跑**：执行脚本（默认并发 4，约需 30~60 分钟，成本主要是 LLM 调用）：

   ```bash
   python3 .codebuddy/skills/rag-batch-submit/scripts/run_submit.py
   ```

   参数：`--limit N` 试跑、`--ids 1,2,3` 指定题目、`--workers N` 并发、`--out 路径` 指定输出。中断后**直接重跑同一 --out 文件即可断点续跑**（已填答案的题目自动跳过）。
4. **校验输出**（官方口径）：
   - 行数 = 909（含表头），`id` 与题目清单完全一致；
   - `structure` 题 answer 是**合法 JSON**，含 `row_count`/`col_count`/`cells`，每个 cell 含 `text`/`row`/`col`/`rowspan`/`colspan`，`row`/`col` 从 0 开始，被合并覆盖的位置不出现占位格；
   - `extract`/`thinking` 题：单值题 answer 只有最终值；数组题是合法 JSON 数组、空值用 `""`；
   - 数字答案无千分位逗号（`125000` 而非 `125,000`），答案中无"根据表格可知""答案是"等说明性文字；
   - 统计未答出（空答案）的题目数，对空答案集中的题目用 `--ids` 重跑或换问法。
5. **抽查质量**：随机抽 5~10 题，用 `python3 -m app.cli ask "问题" --doc <标识>` 单独验证答案与引用是否可靠，尤其扫描件（002-005.pdf）相关的数值题。

## 答案格式规则

完整规范与官方示例见 `references/format.md`。要点：

- `structure` → `json`：answer 必须是合法 JSON，含 `row_count`（完整表格逻辑行数）、`col_count`（逻辑列数）、`cells`；每个真实单元格只输出一次，**合并单元格只输出左上角**并用 `rowspan`/`colspan` 表示合并范围，被覆盖位置不输出占位格；`row`/`col` 从 0 开始；局部恢复时 `row_count`/`col_count` 仍填整表逻辑行列数，cells 只含要求区域。
- `extract`/`thinking` → `string`：单个单元格文本，直接填最终值，去掉引用标注 `[n]`、置信度说明与说明性文字；
- `extract`/`thinking` → `number`：单个数值，无单位、无千分位逗号；
- `extract`/`thinking` → `json_array`：一行/一列/一个区域的答案填 JSON 数组，元素直接填值，表格中为空的项填 `""`；多个结果也用数组。

## 注意事项

- 输出永远是模板副本，**不得改动模板本身**。本次处理的失败题写空值，避免将旧模板答案混入新结果；未选中的题仍保留模板内容。
- 评测和提交统一使用 `app.rag.table_answer.answer_test`：表级定位、单元格计划、Decimal 计算、严格输出。优先修此模块，不使用“取第一个数字”的兜底。
- 2026-11-16 新流程要求 parser v4 / chunker v6，先运行 `python3 -m app.cli ingest --rebuild`。旧缓存已经丢失的空格位置无法由新代码可靠补回，因此必须从源文件重新解析。
- 每题立即写入输出文件同名 `.jsonl`，记录目标表、证据、方法和错误；恢复仅信任同一流水线版本、测试集与索引版本的成功记录，失败题会重试。Excel 在本批处理结束后保存，中断后重跑可由 JSONL 恢复。
- 颜色/可见布局题直接查看原图；图片题在表级答题失败时使用视觉复核。复杂操作暂时回退到同一目标表的强约束生成，并记录 method=table_generation。
- 2026-11-16 检索层升级后：混合检索（向量+BM25）、查询改写、rerank 换用 `qwen3.7-text-rerank`（分数 0.3~1.0，区分度远高于旧 gte 的 ~0.18），表格解析已规范化（无空占位格、含表名），问答层支持 `answer_format` 强约束输出（run_submit 已接入）。structure 题仍由脚本本地选表（全文档表格分块 + rerank），已知限制：合并表头的物理/逻辑布局偏差需人工复核。
- tests.xlsx 第 903 行附近有一行脏数据（question_type 列错位），按 answer_format 处理即可。
- 908 题全量跑会产生较多 LLM 调用（约 900+ 次生成 + 107 次 json_array 归一化）；如需控制成本，可先 `--limit 50` 估算正确率再决定。

## 免费额度降级与质量验证（2026-11-17）

- 文本问答（含流式）按 `.env` 配置的顺序尝试：`qwen4-max → qwen-max → qwen-plus → qwen-flash`。顺序是当前候选模型的质量优先配置，不代表已用本测试集验证排名。
- 仅额度、权限、模型不存在等模型级错误触发下一档；网络错误在当前模型重试。不可用状态缓存 600 秒后重新尝试。
- `qwen-vl-ocr-2025-11-20` 仅用于原始文字和数字识别。布局、颜色、Day/时段对齐、表格结构任务使用通用视觉模型；不可用时保留失败记录，不能由 OCR 猜测替代。
- 每题 JSONL 的 `model_calls` 记录请求模型、选中模型、上游返回模型和是否降级。流水线版本已更新，禁止复用旧版本答案。
- 保留用户反馈准确率 64% 的原始 `submit-template.xlsx`。格式检查通过不代表正确率提高；降级后先用人工核对源文件的小样本比较内容和格式，不直接全量重写提交文件。

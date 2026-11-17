---
name: RAG正确率优化-解析检索问答三层
overview: 全量重解析（VLM 提示词与表格规范化）+ 检索层混合召回与查询改写 + 数值题结构化取值，配合抽样人工评测与置信度统计验证正确率提升。
todos:
  - id: parse-table-normalize
    content: 升级 VLM 表格/整页提示词，并新增 table_norm 规范化模块与解析缓存版本化
    status: completed
  - id: chunk-row-level
    content: 重构分块：表格行级块、表名与表头线性化检索文本、chunk_size 调整、扫描页渲染提分辨率
    status: completed
    dependencies:
      - parse-table-normalize
  - id: hybrid-retrieval
    content: 实现 BM25 混合召回 + RRF 融合 + 表格父子扩展，并清洗 rerank 输入与配置化多样性上限
    status: completed
    dependencies:
      - chunk-row-level
  - id: query-rewrite
    content: 新增 query 改写模块（规则 + LLM 带缓存降级）并接入 Retriever
    status: completed
  - id: structured-answer
    content: 问答层支持 answer_format 结构化取值，接入 CLI/API 与 rag-batch-submit 脚本
    status: completed
    dependencies:
      - hybrid-retrieval
  - id: eval-harness
    content: 新增 eval 分层抽样评测与指标报告，建立优化前基线
    status: completed
    dependencies:
      - chunk-row-level
  - id: rebuild-verify
    content: 全量重解析重建索引，跑前后对比与抽检，更新 README 与 .env.example
    status: completed
    dependencies:
      - hybrid-retrieval
      - query-rewrite
      - structured-answer
      - eval-harness
---

## 用户需求

优化多模态 RAG 问答的正确率，从四个方向排查与改进：图片解析、表格解析、PDF 解析的数据质量，检索层的准确性，以及测试集问题的 query 改写。允许全量重解析（接受约 1-2 小时与视觉模型调用成本），评测采用人工抽检 + 置信度统计，不使用模板预填答案作为标签。

补充要求：

1. **BM25 直接使用 `rank_bm25` 库（`BM25Okapi`），不自己实现打分逻辑**
2. **rerank 模型改用 `qwen3.7-text-rerank`**（替换当前 `gte-rerank-v2`）

## 现状诊断（已实测）

- 索引仅 397 块 / 92 文档（text 100、table 61、image 236），30 个文档只有 2 块，检索粒度极粗
- 61 个表格块中 30 个空单元格占比过半：视觉模型对合并表头补出大量空填充列（如 13 物理列实为 3 逻辑列）
- 扫描件整页只产出一段约 1000 字自由描述，表格数值靠自然语言复述，易漏易错
- 检索为纯向量 top-20 + rerank top-5，rerank 分密集在 0.18 附近几乎无区分度，选表经常出错
- 无关键词/数字混合召回、无 query 改写；表格块以 HTML 噪声参与向量化
- 现有 rerank 走 `dashscope.TextReRank` SDK（`model=self.cfg.rerank_model`，读取 `output.results[].relevance_score`），失败降级为向量顺序

## 核心改进目标

1. 解析产物可检索、可还原：表格保留合并关系且不含空占位格，扫描页表格数值准确
2. 检索从"纯语义"升级为"语义 + 词法（数字/表名/字段名）"混合，并支持表格行级命中回溯整表
3. 测试集问题先改写再检索，去掉"恢复一下""分别是多少"等噪声并抽取表名、期间字段
4. 数值型问题改为结构化取值输出，减少自由生成造成的错值
5. 建立可复现的抽样评测与指标对比（置信度均值、引用命中率、空答案率、抽检正确率）

## 技术栈

沿用现有 FastAPI + FAISS + 百炼 OpenAI 兼容客户端（qwen-plus / qwen3-vl-plus / text-embedding-v4）。

**模型变更**：rerank 由 `gte-rerank-v2` 改为 `qwen3.7-text-rerank`（`config.rerank_model` 默认值 + `.env` 可选覆盖）；仍需走 `dashscope.TextReRank`，若该模型不在 TextReRank 支持列表或返回结构不同，则改用 OpenAI 兼容 `/rerank` HTTP 端点适配（在 `app/clients/rerank.py` 内做一次能力探测 + 缓存，失败降级向量顺序不变）。

继续使用 pdfplumber + pypdfium2 + openpyxl。

**新增依赖（写入 `requirements.txt`）**：

- `rank_bm25`：BM25Okapi 打分，直接复用，不自研
- `jieba`：中文分词（BM25 与数字/术语抽取共用）；英文与数字走正则 token

## 实现方案

### 一、解析层（数据质量是正确率上限）

1. **视觉提示词拆分**（`app/clients/llm.py`）

- 新增 `TABLE_HTML_PROMPT`：仅输出 HTML `<table>`；合并单元格必须用 `rowspan`/`colspan`，**禁止输出被合并覆盖的空占位单元格**；数字原样、不换算单位；不输出解释文字
- 新增 `PAGE_STRUCT_PROMPT`：先输出该页所有表格的 HTML（含表名/表题），再输出正文要点，要求保留表名、单位、期间字样（"期末余额/期初余额"）——表名对检索至关重要
- 扫描页渲染分辨率由 `scale=2.0` 提升至 `2.6`（≈190DPI）、JPEG quality 90，改善小字与千分位识别

2. **表格规范化模块**（新增 `app/ingest/table_norm.py`）

- `parse_html_to_grid()`：解析 HTML（含 rowspan/colspan）→ 占用矩阵 → 丢弃被覆盖的占位格与全空行/全空列 → 还原逻辑网格
- `grid_to_html()`（规范 HTML，仅真实单元格）、`grid_to_markdown()`（供 LLM 上下文，省 token 且更准）、`grid_to_rowtexts()`（每行生成检索文本：`表名 列名 值 列名 值 …`）
- `table_name_from_caption()`：从 caption/上下文提取表名（如"应收账款""坏账准备"），写入块 meta 与检索文本

3. **解析缓存版本化**（`app/ingest/pipeline.py`）

- 新增 `PARSER_VERSION`（示例 `v2`），缓存文件名改为 `{doc_id}_{sha1}_{PARSER_VERSION}.json`，manifest 记录 `parser_version`；版本不符自动全量重解析（否则改提示词后仍命中旧缓存）

4. **分块重构**（`app/ingest/chunker.py`，`CHUNKER_VERSION` 升级）

- 表格：新增 `table_row` 行级块（`type="table_row"`，`parent_id=表块 chunk_id`，`content` 为该行 markdown），表块本身保留完整 HTML + 表名 + 表头
- 表格块检索文本改为"表名 + 表头 + 行文本"（去 HTML 噪声），HTML 仅留在 `content`
- `chunk_size` 500 → 800、`chunk_overlap` 80 → 100，`TABLE_MAX_CHARS` 2000 → 4000（避免宽表被切碎）
- image 块按"小标题/表名"边界切分（而非固定 1600 字符），保留每段表名

### 二、检索层

1. **rerank 模型切换**（`app/clients/rerank.py` + `app/config.py` + `.env.example`）

- `rerank_model` 默认改为 `qwen3.7-text-rerank`
- 增加**能力探测**：启动/首次调用时用一段固定 query+documents 试调用，缓存结果（`_probe_supported: bool`）；若 `TextReRank` 报"模型不支持/参数错误"，自动切到 HTTP 兼容调用（`POST {dashscope_base_url}/rerank`，解析 `output.results[].relevance_score`），仍失败则降级向量顺序
- **分数分布校准**：新模型的 `relevance_score` 量纲可能与 gte 不同（0-1 vs logit），先跑 20 条真实问题采集分布，再据实测重设 `confidence_rerank_low/high` 与 confidence 归一化区间；未知量纲时统一做 min-max 归一化后再进置信度计算
- 文档与 `.env.example` 同步说明可切换回 `gte-rerank-v2`

2. **混合召回**（新增 `app/rag/bm25.py` + 改造 `app/rag/retriever.py`）

- `app/rag/bm25.py` 封装 `rank_bm25.BM25Okapi`：
- 建索引：对每个块的 `content_text`（表名+表头+行文本 / 正文）分词后 `BM25Okapi(corpus)`；索引随 `FaissStore` 加载时惰性构建并缓存在内存，索引重建后失效重建
- 查询：同一分词器切 query → `bm25.get_scores(tokens)` → 取 top_k
- **分词器**（`tokenize()` 单函数，BM25 与 query 抽取共用）：`jieba.lcut` 切中文词 + 正则抽取英文/数字 token；数字保留原形并额外产出"去千分位归一形"（`12,851,000.00` → `12851000.00`），提高数值题命中率
- **RRF 融合**（k=60）：`vector_top_k=30` 与 `bm25_top_k=30` 融合后取 20 送 rerank，权重可配置（默认向量 1.0 / BM25 0.6）

3. **表格父子扩展**

- 命中 `table_row` 时，将父表块以 0.98×分数插入结果（去重），保证 LLM 拿到完整表格与列头
- `MAX_TABLE_PER_DOC` 2 → 3 且可配置（`retrieval_max_table_per_doc`），避免同文档多表时误伤

4. **rerank 输入清洗**：送精排的文本统一用 `content_text`（表名+表头+行文本），不再送 HTML；同时补充"精排输入长度上限"（超长表格只送表名+表头+前若干行）

5. **配置项新增**（`app/config.py` + `.env.example`）：`rerank_model`（默认 `qwen3.7-text-rerank`）、`bm25_enabled`、`hybrid_vector_top_k`、`hybrid_bm25_top_k`、`rrf_k`、`bm25_weight`、`parent_expand_enabled`、`retrieval_max_table_per_doc`、`numeric_answer_enabled`

### 三、查询改写（新增 `app/rag/query_rewrite.py`）

1. **规则层（零成本，始终执行）**：去噪声词（"恢复一下""分别是多少""请问""帮我看看"）；括号/单位提权（万元、%）；同义映射（本期末→期末余额、上一期→期初余额、金额→余额）；抽取文档标识（001）与表格提示
2. **LLM 层（可获得良好效果时启用）**：一次调用产出 2 条检索式（关键词式 + 字段式），带 8s 超时与失败降级；结果按 `hash(question)` 进程内 LRU 缓存
3. **接入点**：`Retriever.retrieve()` 内部先改写再做混合召回；`RAGChat.ask()` 记录改写结果到 sources 调试字段（不影响前端展示）

### 四、回答层（数值题结构化）

- `RAGChat.ask(question, answer_format=None)` 透传格式；当 `answer_format in {number, json_array, json}` 且 `numeric_answer_enabled` 时，使用新增 `STRUCTURED_SYSTEM_PROMPT`：只依据上下文表格（markdown 行号列表）输出最终值/JSON 数组/结构 JSON，禁止解释性文字
- Web 对话保持现有 `SYSTEM_PROMPT` 不变（体验优先）；CLI 增加 `--format` 参数；`.codebuddy/skills/rag-batch-submit/scripts/run_submit.py` 改为向 `ask()` 传 `answer_format`，保留现有正则/归一化作为兜底

### 五、评测与验证（人工抽检为主）

1. 新增 `app/eval.py` + CLI 子命令 `python -m app.cli eval --sample 30 --out reports/eval-<ts>.md`

- 按 `question_type` 分层抽样（structure/extract/thinking 各若干），跑 `ask()`，输出问题、答案、引用、置信度，便于人工核对
- 统计：平均置信度、空答案率、引用命中率、"table_hint 命中率"（hint 词是否出现在命中表的名/表头中——弱监督检索指标）、**rerank 分数分布（min/p50/p90/max，用于新模型校准）**

2. 优化前后各跑一次同 seed 抽样，形成对比表
3. 全量重解析 + 重建索引，报告：块数变化、表格空列率（目标 <10%）、table_row 覆盖率、平均置信度提升
4. 更新 `README.md`（新配置与评测命令）与 `.env.example`

## 性能与风险

- 重解析耗时集中在 VLM（92 文件 / 约 236 张页面图，并发 4，约 40-70 分钟）；表格规范化与 BM25 为纯 CPU，对 1-2k 块可忽略
- `rank_bm25` 索引构建为 O(块数×词数)，1-2k 块毫秒级；索引常驻内存（数 MB），随索引重建刷新；首次检索惰性构建，避免启动阻塞
- jieba 首次加载词典约 0.5-1s（`jieba.lcut` 初始化），可接受；如需更快可开 `jieba.enable_parallel` 或预加载
- **rerank 换模型风险**：`qwen3.7-text-rerank` 的名称/可用性需以实际 API 返回为准（若 404/不支持则走 HTTP 兼容路径或回退 `gte-rerank-v2`），且分数分布变化会影响置信度阈值——已列入评测项与校准步骤
- 改写 LLM 调用每次问答 +1 次（可关闭），失败自动降级不影响主流程
- 兼容性：`ParsedDoc/ParsedBlock` 结构不变，仅新增 `meta` 字段（`from_dict` 已容错）；`table_row` 为新增类型，前端 `TYPE_LABEL` 需补映射并默认按"表格"样式展示；旧索引因 `PARSER_VERSION`/`CHUNKER_VERSION` 变化自动全量重建
- 不触碰 `.codebuddy/skills` 结构与前端交互形态（置信度徽章复用）

## 架构与目录

```
app/
├── clients/
│   ├── llm.py                # [MODIFY] 新增 TABLE_HTML_PROMPT / PAGE_STRUCT_PROMPT，vision_describe 支持指定 prompt
│   └── rerank.py             # [MODIFY] rerank_model 切换 + 能力探测 + HTTP 兼容兜底 + 分数归一化
├── ingest/
│   ├── table_norm.py         # [NEW] HTML→逻辑网格→规范 HTML/markdown/行文本/表名
│   ├── pdf_parser.py         # [MODIFY] 表格用 table_norm 规范化、扫描页渲染分辨率、表格 caption 带表名
│   ├── image_parser.py       # [MODIFY] 使用 PAGE_STRUCT_PROMPT、表格规范化、块按表名切分准备
│   ├── chunker.py            # [MODIFY] table_row 块、表名+表头+行文本检索文本、chunk_size/TABLE_MAX_CHARS 调整
│   └── pipeline.py           # [MODIFY] PARSER_VERSION 缓存版本化与 manifest 记录
├── rag/
│   ├── bm25.py               # [NEW] 封装 rank_bm25.BM25Okapi + jieba/正则分词（不实现打分）
│   ├── query_rewrite.py      # [NEW] 规则改写 + LLM 改写（带超时/缓存/降级）
│   ├── retriever.py          # [MODIFY] 混合召回 + RRF + 父子扩展 + rerank 文本清洗 + 配置化多样性上限
│   └── qa.py                 # [MODIFY] ask(answer_format) 与 STRUCTURED_SYSTEM_PROMPT
├── rag/confidence.py         # [MODIFY] rerank 分数归一化改为自适应（min-max / 模型量纲）
├── eval.py                   # [NEW] 分层抽样评测与报告生成
├── cli.py                    # [MODIFY] 新增 eval 子命令、ask --format、rerank 探测命令
├── config.py                 # [MODIFY] rerank_model 默认值、混合检索/改写/结构化回答配置项
app/web/app.js                # [MODIFY] TYPE_LABEL 支持 table_row（按表格展示）
requirements.txt              # [MODIFY] 新增 rank_bm25、jieba
.codebuddy/skills/rag-batch-submit/scripts/run_submit.py  # [MODIFY] 传 answer_format 给 ask()，保留兜底
README.md / .env.example      # [MODIFY] 新配置项、模型切换与评测命令说明
```

## 关键接口

```python
# app/clients/rerank.py
class Reranker:
    def probe(self) -> dict            # {"model": str, "path": "sdk"|"http", "ok": bool, "score_range": [min, max]}
    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float | None]]

# app/ingest/table_norm.py
def normalize_table(html_or_rows) -> dict:  # {"html": str, "markdown": str, "rows": list[list[str]], "row_texts": list[str]}

# app/rag/bm25.py
def tokenize(text: str, with_numeric_variants: bool = True) -> list[str]
class BM25Retriever:
    def __init__(self, chunks: list[dict], k1: float = 1.5, b: float = 0.75)  # 内部 rank_bm25.BM25Okapi
    def search(self, query: str, top_k: int) -> list[tuple[int, float]]  # (chunk 下标, 分数)

# app/rag/query_rewrite.py
def rewrite(question: str, llm=None) -> dict:  # {"query": str, "variants": list[str], "tokens": list[str], "doc_id": str|None}

# app/rag/qa.py
def ask(self, question: str, history=None, doc_ids=None, answer_format: str | None = None) -> dict
```

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose: 执行阶段快速定位解析、检索、分块相关调用点与前端 type 映射，避免遗漏改动面
- Expected outcome: 输出受影响的文件清单与调用链，确认 `table_row` 新增类型、`ask()` 新参数、`Reranker.probe()` 在 API/前端/CLI/批量脚本中的所有使用点均已覆盖
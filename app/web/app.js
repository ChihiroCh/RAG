/* 多模态知识库前端：SSE 流式问答、引用卡片、索引状态与重建。 */
(function () {
  "use strict";

  const state = {
    status: null,
    docFilter: new Set(),
    streaming: false,
    controller: null,
    history: [],
    docKeyword: "",
    ingestTimer: null,
    statusTimer: null,
  };

  const el = (id) => document.getElementById(id);

  /* ------------------------------------------------------------ 基础工具 */

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot");
  }

  function fmtNumber(value) {
    const num = Number(value || 0);
    return num >= 10000 ? (num / 1000).toFixed(1) + "k" : String(num);
  }

  function toast(message, type) {
    const node = document.createElement("div");
    node.className = "toast" + (type === "error" ? " error" : "");
    node.textContent = message;
    el("toast-stack").appendChild(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transform = "translateX(14px)";
      node.style.transition = "all .3s ease";
      setTimeout(() => node.remove(), 320);
    }, type === "error" ? 6000 : 3600);
  }

  function scrollToBottom(smooth) {
    const box = el("chat-scroll");
    box.scrollTo({ top: box.scrollHeight, behavior: smooth === false ? "auto" : "smooth" });
  }

  function hideWelcome() {
    const welcome = el("welcome");
    if (welcome) welcome.classList.add("hidden");
  }

  /* ------------------------------------------------------------ Markdown */

  function renderMarkdown(text) {
    if (!text) return "";
    const stash = [];
    const hold = (html) => {
      stash.push(html);
      return "\u0000" + (stash.length - 1) + "\u0000";
    };

    let src = String(text);
    // 视觉模型输出的表格常包在 ```html 围栏里，先还原为表格再走普通围栏
    src = src.replace(/```(?:html)?\s*(<table[\s\S]*?<\/table>)\s*```/gi, (_, table) => table);
    src = src.replace(/```[a-zA-Z0-9+#-]*\n([\s\S]*?)```/g, (_, code) =>
      hold('<pre class="code-block"><code>' + escapeHtml(code) + "</code></pre>")
    );
    src = src.replace(/<table[\s\S]*?<\/table>/gi, (m) => hold('<div class="table-wrap">' + m + "</div>"));
    src = escapeHtml(src);

    src = src.replace(/`([^`\n]+)`/g, (_, code) => "<code>" + code + "</code>");
    src = src.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    src = src.replace(/(^|[\s（(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    src = src.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    src = src.replace(/\[(\d{1,2})\]/g, '<span class="cite-ref" data-cite="$1">[$1]</span>');

    const lines = src.split("\n");
    const out = [];
    let para = [];
    let listType = null;

    const flushPara = () => {
      if (para.length) {
        out.push("<p>" + para.join("<br/>") + "</p>");
        para = [];
      }
    };
    const closeList = () => {
      if (listType) {
        out.push("</" + listType + ">");
        listType = null;
      }
    };
    const openList = (type) => {
      if (listType !== type) {
        closeList();
        out.push("<" + type + ">");
        listType = type;
      }
    };
    const splitRow = (line) =>
      line
        .replace(/^\s*\|/, "")
        .replace(/\|\s*$/, "")
        .split("|")
        .map((cell) => cell.trim());

    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];

      if (/^\u0000\d+\u0000\s*$/.test(line.trim())) {
        flushPara();
        closeList();
        out.push(line.trim());
        continue;
      }
      if (!line.trim()) {
        flushPara();
        closeList();
        continue;
      }

      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushPara();
        closeList();
        const level = heading[1].length;
        out.push("<h" + level + ">" + heading[2] + "</h" + level + ">");
        continue;
      }

      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        flushPara();
        closeList();
        out.push("<hr/>");
        continue;
      }

      if (
        line.indexOf("|") >= 0 &&
        i + 1 < lines.length &&
        /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1]) &&
        lines[i + 1].indexOf("-") >= 0
      ) {
        flushPara();
        closeList();
        const header = splitRow(line);
        const rows = [];
        let cursor = i + 2;
        while (cursor < lines.length && lines[cursor].trim() && lines[cursor].indexOf("|") >= 0) {
          rows.push(splitRow(lines[cursor]));
          cursor += 1;
        }
        const html =
          '<div class="table-wrap"><table><thead><tr>' +
          header.map((cell) => "<th>" + cell + "</th>").join("") +
          "</tr></thead><tbody>" +
          rows
            .map((row) => "<tr>" + row.map((cell) => "<td>" + cell + "</td>").join("") + "</tr>")
            .join("") +
          "</tbody></table></div>";
        out.push(html);
        i = cursor - 1;
        continue;
      }

      const bullet = line.match(/^\s*[-*•·]\s+(.*)$/);
      if (bullet) {
        flushPara();
        openList("ul");
        out.push("<li>" + bullet[1] + "</li>");
        continue;
      }

      const ordered = line.match(/^\s*\d+[.、)]\s+(.*)$/);
      if (ordered) {
        flushPara();
        openList("ol");
        out.push("<li>" + ordered[1] + "</li>");
        continue;
      }

      const quote = line.match(/^\s*>\s?(.*)$/);
      if (quote) {
        flushPara();
        closeList();
        out.push("<blockquote>" + quote[1] + "</blockquote>");
        continue;
      }

      closeList();
      para.push(line);
    }
    flushPara();
    closeList();

    let html = out.join("\n");
    html = html.replace(/\u0000(\d+)\u0000/g, (_, index) => stash[Number(index)] || "");
    return html;
  }

  /* ------------------------------------------------------------ 状态渲染 */

  function setText(id, value) {
    const node = el(id);
    if (node) node.textContent = value;
  }

  function renderStatus() {
    const status = state.status;
    if (!status) return;

    setText("stat-docs", fmtNumber(status.documents));
    setText("stat-chunks", fmtNumber(status.chunks));
    setText("stat-dim", status.models.embed_dim);

    const chunksBadge = el("badge-chunks").querySelector("span");
    if (chunksBadge) chunksBadge.textContent = "分块 " + fmtNumber(status.chunks);
    const docsBadge = el("badge-docs").querySelector("span");
    if (docsBadge) docsBadge.textContent = "文件 " + fmtNumber(status.documents);
    setText("badge-model", status.models.llm);

    const manifest = status.manifest || {};
    setText(
      "built-at",
      manifest.built_at ? "构建于 " + String(manifest.built_at).replace("T", " ") : "尚未构建"
    );

    const byType = status.by_type || {};
    const total = Math.max(1, (byType.text || 0) + (byType.table || 0) + (byType.image || 0));
    const rows = el("type-bars").querySelectorAll(".type-row");
    ["text", "table", "image"].forEach((key, index) => {
      const row = rows[index];
      if (!row) return;
      const count = byType[key] || 0;
      row.querySelector("b").textContent = fmtNumber(count);
      row.querySelector(".bar i").style.width = Math.round((count / total) * 100) + "%";
    });

    renderDocs();
    renderIngest(status.ingest);
  }

  function renderDocs() {
    const status = state.status;
    const list = el("doc-list");
    if (!status || !status.documents_detail || !status.documents_detail.length) {
      list.innerHTML =
        '<p class="empty-hint">索引尚未构建，点击右上角「重建索引」开始解析 files/ 目录。</p>';
      setText("doc-count", "0 个");
      return;
    }

    const keyword = state.docKeyword.trim().toLowerCase();
    const docs = status.documents_detail.filter((doc) => {
      if (!keyword) return true;
      return (
        String(doc.doc_id).toLowerCase().includes(keyword) ||
        String(doc.file_name).toLowerCase().includes(keyword)
      );
    });

    setText("doc-count", docs.length + " 个");
    if (!docs.length) {
      list.innerHTML = '<p class="empty-hint">没有匹配的文件标识。</p>';
      return;
    }

    list.innerHTML = docs
      .map((doc) => {
        const active = state.docFilter.has(doc.doc_id) ? " active" : "";
        const types = (doc.types || [])
          .map((type) => '<span class="tag-mini">' + typeLabel(type) + "</span>")
          .join(" ");
        return (
          '<button class="doc-item' +
          active +
          '" data-doc="' +
          escapeHtml(doc.doc_id) +
          '" title="点击仅检索该文件">' +
          '<span class="doc-chip">' +
          escapeHtml(doc.doc_id) +
          "</span>" +
          '<span class="doc-name">' +
          escapeHtml(doc.file_name) +
          "</span>" +
          '<span class="doc-meta">' +
          doc.chunks +
          " 块</span>" +
          "</button>"
        );
      })
      .join("");

    list.querySelectorAll(".doc-item").forEach((node) => {
      node.addEventListener("click", () => toggleDoc(node.dataset.doc));
    });
  }

  function typeLabel(type) {
    return { text: "文本", table: "表格", image: "图片" }[type] || type;
  }

  function toggleDoc(docId) {
    if (state.docFilter.has(docId)) state.docFilter.delete(docId);
    else state.docFilter.add(docId);
    renderFilterChips();
    renderDocs();
    document.querySelectorAll(".doc-item").forEach((node) => {
      node.classList.toggle("active", state.docFilter.has(node.dataset.doc));
    });
  }

  function renderFilterChips() {
    const box = el("filter-chips");
    if (!state.docFilter.size) {
      box.innerHTML = "";
      return;
    }
    const chips = Array.from(state.docFilter)
      .map(
        (docId) =>
          '<span class="filter-chip">仅检索 <b>' +
          escapeHtml(docId) +
          '</b><button data-remove="' +
          escapeHtml(docId) +
          '" title="移除">×</button></span>'
      )
      .join("");
    box.innerHTML =
      chips + '<button class="btn ghost" id="clear-filter" style="height:25px;padding:0 10px;font-size:11px">清空筛选</button>';
    box.querySelectorAll("[data-remove]").forEach((node) => {
      node.addEventListener("click", () => toggleDoc(node.dataset.remove));
    });
    const clear = el("clear-filter");
    if (clear)
      clear.addEventListener("click", () => {
        state.docFilter.clear();
        renderFilterChips();
        renderDocs();
      });
  }

  const STAGE_TEXT = {
    prepare: "扫描源文件",
    parse: "解析文件",
    parse_error: "部分文件解析失败",
    chunk: "文本分块",
    embed: "向量化",
    save: "写入索引",
    done: "完成",
    error: "失败",
    idle: "空闲",
  };

  function renderIngest(ingest) {
    if (!ingest) return;
    const panel = el("ingest-panel");
    if (!ingest.running && ingest.stage !== "error" && ingest.stage !== "done") {
      panel.classList.add("hidden");
      return;
    }
    if (!ingest.running && ingest.stage === "idle") {
      panel.classList.add("hidden");
      return;
    }
    panel.classList.remove("hidden");
    setText("ingest-stage", STAGE_TEXT[ingest.stage] || ingest.stage || "处理中");

    const total = Number(ingest.total || 0);
    const done = Number(ingest.done || 0);
    let percent = total > 0 ? Math.round((done / total) * 100) : 0;
    if (ingest.stage === "save") percent = Math.max(percent, 92);
    if (ingest.stage === "done") percent = 100;
    if (ingest.stage === "error") percent = Math.min(percent, 99);
    el("ingest-bar").style.width = percent + "%";

    let text = "";
    if (ingest.stage === "parse") text = "解析 " + done + " / " + total + "：" + (ingest.file || "");
    else if (ingest.stage === "embed") text = "向量化 " + done + " / " + total + " 个分块";
    else if (ingest.stage === "save") text = "写入 FAISS 索引与元数据…";
    else if (ingest.stage === "done") text = ingest.message || "入库完成";
    else if (ingest.stage === "error") text = "失败：" + (ingest.message || "未知错误");
    else text = ingest.message || "正在准备…";
    setText("ingest-text", text);
  }

  async function loadStatus() {
    try {
      const resp = await fetch("/api/status");
      if (!resp.ok) throw new Error("状态接口异常");
      state.status = await resp.json();
      renderStatus();
      if (state.status.ingest && state.status.ingest.running) startPollingIngest();
    } catch (error) {
      console.warn(error);
    }
  }

  /* ------------------------------------------------------------ 消息 */

  function appendMessage(role, text) {
    const wrapper = document.createElement("article");
    wrapper.className = "message " + role;

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "我" : "AI";

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    const body = document.createElement("div");
    body.className = "markdown";
    body.innerHTML = text ? renderMarkdown(text) : "";
    bubble.appendChild(body);

    const confidence = document.createElement("div");
    confidence.className = "confidence hidden";
    bubble.appendChild(confidence);

    const citations = document.createElement("div");
    citations.className = "citations";
    bubble.appendChild(citations);

    if (role === "assistant") bubble.classList.add("typing");

    wrapper.appendChild(avatar);
    wrapper.appendChild(bubble);
    el("messages").appendChild(wrapper);
    scrollToBottom();
    return {
      article: wrapper,
      bubble: bubble,
      body: body,
      confidence: confidence,
      citations: citations,
      raw: text || "",
    };
  }

  /* ------------------------------------------------------------ 置信度 */

  function confidenceRow(label, value) {
    const num = value == null ? null : Number(value);
    const width = num == null ? 0 : Math.max(0, Math.min(100, Math.round(num * 100)));
    return (
      '<div class="confidence-row"><span class="confidence-row-label">' +
      escapeHtml(label) +
      '</span><span class="confidence-row-bar"><i style="width:' +
      width +
      '%"></i></span><b>' +
      (num == null ? "—" : num.toFixed(2)) +
      "</b></div>"
    );
  }

  function updateConfidence(message, conf) {
    const host = message.confidence;
    if (!host || !conf || conf.score == null) return;

    const stage = conf.stage === "final" ? "final" : "retrieval";
    const level = ["high", "medium", "low"].includes(conf.level) ? conf.level : "medium";
    const score = Number(conf.score) || 0;
    const percent = conf.percent != null ? conf.percent : Math.round(score * 100);
    const components = conf.components || {};

    host.classList.remove("hidden", "high", "medium", "low", "pending", "final", "aborted");
    host.classList.add(level, stage);

    const label = conf.level_label || { high: "高", medium: "中", low: "低" }[level];
    const note =
      stage === "retrieval"
        ? "生成中 · 结束后校准"
        : conf.method === "hybrid"
        ? "检索 + 一致性校准"
        : "仅检索信号";

    const rows = [
      confidenceRow("检索分", components.retrieval),
      confidenceRow(
        conf.degraded ? "向量最高分" : "精排最高分",
        components.rerank_top != null ? components.rerank_top : components.vector_top
      ),
      confidenceRow("分数间隔", components.margin),
      confidenceRow("来源覆盖", components.coverage),
      confidenceRow("一致性", components.consistency),
      confidenceRow("引用命中", components.citation_hit),
    ].join("");

    host.innerHTML =
      '<div class="confidence-line">' +
      '<button class="confidence-pill" type="button" aria-expanded="false" title="查看置信度构成">' +
      '<span class="confidence-dot"></span>' +
      '<span class="confidence-score">置信度 ' +
      score.toFixed(2) +
      " · " +
      escapeHtml(label) +
      "</span>" +
      (stage === "retrieval" ? '<span class="confidence-stage">预估</span>' : "") +
      '<span class="confidence-meter"><i style="width:' +
      percent +
      '%"></i></span>' +
      "</button>" +
      '<span class="confidence-note">' +
      escapeHtml(note) +
      "</span>" +
      "</div>" +
      '<div class="confidence-panel">' +
      '<p class="confidence-panel-title">置信度构成（0-1）</p>' +
      rows +
      (conf.reason ? '<p class="confidence-reason">' + escapeHtml(conf.reason) + "</p>" : "") +
      (conf.degraded ? '<p class="confidence-reason">精排不可用，已按向量分估算</p>' : "") +
      "</div>" +
      (conf.hint ? '<p class="confidence-hint">' + escapeHtml(conf.hint) + "</p>" : "");

    const pill = host.querySelector(".confidence-pill");
    if (pill) {
      pill.addEventListener("click", () => {
        const expanded = host.classList.toggle("expanded");
        pill.setAttribute("aria-expanded", expanded ? "true" : "false");
      });
    }
  }

  function markConfidenceAborted(message) {
    const host = message.confidence;
    if (!host || host.classList.contains("hidden")) return;
    host.classList.add("aborted");
    const note = host.querySelector(".confidence-note");
    if (note) note.textContent = "已停止生成 · 保留预估值";
  }

  function renderCitations(container, sources) {
    if (!sources || !sources.length) {
      container.innerHTML = "";
      return;
    }
    const head = document.createElement("p");
    head.className = "citations-title";
    head.textContent = "引用来源 · " + sources.length + " 条";
    container.appendChild(head);

    sources.forEach((source, index) => {
      const card = document.createElement("div");
      card.className = "citation";
      card.dataset.cite = String(source.index);
      card.style.animationDelay = index * 60 + "ms";

      const meta = "第" + source.page + "页 · " + source.type_label;
      const score = source.score != null ? "精排 " + source.score : "向量 " + source.vector_score;
      card.innerHTML =
        '<div class="citation-head">' +
        '<span class="doc-chip">' + escapeHtml(source.doc_id) + "</span>" +
        '<span class="citation-name">' + escapeHtml(source.file_name) + "</span>" +
        '<span class="citation-meta">' + escapeHtml(meta) + "</span>" +
        "</div>" +
        '<p class="citation-snippet">' + escapeHtml(source.snippet || "") + "</p>" +
        '<div class="citation-foot"><span class="score">' + escapeHtml(score) + "</span>" +
        '<button class="link-btn" type="button">展开原文</button></div>';

      if (source.preview_url) {
        const thumb = document.createElement("img");
        thumb.className = "citation-thumb";
        thumb.loading = "lazy";
        thumb.src = source.preview_url;
        thumb.alt = source.file_name;
        thumb.onerror = function () {
          this.remove();
        };
        thumb.addEventListener("click", () => openLightbox(source.preview_url));
        card.appendChild(thumb);
      }

      const toggle = card.querySelector(".link-btn");
      toggle.addEventListener("click", () => {
        card.classList.toggle("expanded");
        toggle.textContent = card.classList.contains("expanded") ? "收起" : "展开原文";
        if (card.classList.contains("expanded") && source.content) {
          card.querySelector(".citation-snippet").textContent = source.content;
        } else {
          card.querySelector(".citation-snippet").textContent = source.snippet || "";
        }
      });

      container.appendChild(card);
    });
  }

  function focusCitation(messageNode, index) {
    const card = messageNode.querySelector('.citation[data-cite="' + index + '"]');
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.remove("flash");
    void card.offsetWidth;
    card.classList.add("flash");
  }

  /* ------------------------------------------------------------ SSE */

  async function readSSE(body, onEvent) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        raw.split("\n").forEach((line) => {
          if (!line.startsWith("data:")) return;
          const payload = line.slice(5).trim();
          if (!payload) return;
          try {
            onEvent(JSON.parse(payload));
          } catch (error) {
            console.warn("SSE 解析失败", error, payload);
          }
        });
      }
    }
  }

  function setStreaming(active) {
    state.streaming = active;
    el("btn-send").classList.toggle("hidden", active);
    el("btn-stop").classList.toggle("hidden", !active);
  }

  async function sendMessage(text) {
    const question = (text || "").trim();
    if (!question || state.streaming) return;
    if (!state.status || !state.status.has_api_key) {
      toast("未配置 DASHSCOPE_API_KEY，请在 .env 中填写后重启服务", "error");
      return;
    }

    hideWelcome();
    appendMessage("user", question);
    state.history.push({ role: "user", content: question });

    const message = appendMessage("assistant", "");
    let answer = "";
    let failed = null;
    setStreaming(true);
    state.controller = new AbortController();

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: question,
          history: state.history.slice(-6, -1),
          doc_ids: Array.from(state.docFilter),
        }),
        signal: state.controller.signal,
      });
      if (!resp.ok || !resp.body) throw new Error("请求失败（HTTP " + resp.status + "）");

      await readSSE(resp.body, (event) => {
        if (event.type === "sources") {
          renderCitations(message.citations, event.sources);
        } else if (event.type === "confidence") {
          updateConfidence(message, event.confidence);
        } else if (event.type === "delta") {
          answer += event.text;
          message.body.innerHTML = renderMarkdown(answer);
          scrollToBottom(false);
        } else if (event.type === "done") {
          if (event.confidence) updateConfidence(message, event.confidence);
        } else if (event.type === "error") {
          failed = event.message;
        }
      });

      if (failed) {
        message.article.classList.add("error");
        message.body.innerHTML = "<p>" + escapeHtml(failed) + "</p>";
        message.confidence.classList.add("hidden");
        toast(failed, "error");
      } else {
        message.body.innerHTML = renderMarkdown(answer);
        state.history.push({ role: "assistant", content: answer });
        if (state.history.length > 8) state.history = state.history.slice(-8);
      }
    } catch (error) {
      if (error.name === "AbortError") {
        message.body.innerHTML =
          renderMarkdown(answer) + '<p style="color:var(--text-mute);font-size:12px">（已停止生成）</p>';
        markConfidenceAborted(message);
        if (answer) state.history.push({ role: "assistant", content: answer });
      } else {
        message.article.classList.add("error");
        message.body.innerHTML = "<p>" + escapeHtml(error.message) + "</p>";
        message.confidence.classList.add("hidden");
        toast(error.message, "error");
      }
    } finally {
      message.bubble.classList.remove("typing");
      setStreaming(false);
      state.controller = null;
      scrollToBottom(false);
    }
  }

  /* ------------------------------------------------------------ 入库 */

  function setIngestButtons(disabled) {
    el("btn-rebuild").disabled = disabled;
    el("btn-incremental").disabled = disabled;
  }

  async function startIngest(rebuild) {
    setIngestButtons(true);
    try {
      const resp = await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rebuild: rebuild }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || "启动入库失败（HTTP " + resp.status + "）");
      el("ingest-panel").classList.remove("hidden");
      toast(rebuild ? "开始全量重建索引，解析结果会缓存以避免重复调用模型" : "开始增量入库");
      startPollingIngest();
    } catch (error) {
      setIngestButtons(false);
      toast(error.message, "error");
    }
  }

  function startPollingIngest() {
    if (state.ingestTimer) return;
    state.ingestTimer = setInterval(async () => {
      try {
        const resp = await fetch("/api/status");
        const status = await resp.json();
        state.status = status;
        renderStatus();
        const ingest = status.ingest || {};
        if (!ingest.running) {
          clearInterval(state.ingestTimer);
          state.ingestTimer = null;
          setIngestButtons(false);
          if (ingest.stage === "done") {
            toast(ingest.message || "入库完成");
            await loadStatus();
          } else if (ingest.stage === "error") {
            toast(ingest.message || "入库失败", "error");
          }
        }
      } catch (error) {
        console.warn(error);
      }
    }, 1600);
  }

  /* ------------------------------------------------------------ 灯箱 */

  function openLightbox(url) {
    el("lightbox-img").src = url;
    el("lightbox").classList.remove("hidden");
  }

  function closeLightbox() {
    el("lightbox").classList.add("hidden");
    el("lightbox-img").src = "";
  }

  /* ------------------------------------------------------------ 事件绑定 */

  function bindEvents() {
    const input = el("input");

    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 148) + "px";
    });

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        const text = input.value;
        input.value = "";
        input.style.height = "auto";
        sendMessage(text);
      }
    });

    el("btn-send").addEventListener("click", () => {
      const text = input.value;
      input.value = "";
      input.style.height = "auto";
      sendMessage(text);
    });

    el("btn-stop").addEventListener("click", () => {
      if (state.controller) state.controller.abort();
    });

    el("btn-rebuild").addEventListener("click", () => startIngest(true));
    el("btn-incremental").addEventListener("click", () => startIngest(false));

    el("doc-search").addEventListener("input", (event) => {
      state.docKeyword = event.target.value;
      renderDocs();
    });

    document.querySelectorAll(".example").forEach((node) => {
      node.addEventListener("click", () => sendMessage(node.dataset.q));
    });

    el("messages").addEventListener("click", (event) => {
      const cite = event.target.closest(".cite-ref");
      if (!cite) return;
      const messageNode = cite.closest(".message");
      if (!messageNode) return;
      focusCitation(messageNode, cite.dataset.cite);
    });

    el("lightbox").addEventListener("click", (event) => {
      if (event.target !== el("lightbox-img")) closeLightbox();
    });
    el("lightbox-close").addEventListener("click", closeLightbox);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !el("lightbox").classList.contains("hidden")) closeLightbox();
    });
  }

  async function init() {
    bindEvents();
    await loadStatus();
    state.statusTimer = setInterval(() => {
      if (!state.streaming && !state.ingestTimer) loadStatus();
    }, 30000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();

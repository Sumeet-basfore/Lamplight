let mode = "nsfw", passcode = sessionStorage.getItem("pb_pass") || "";
let aborter = null, confirmCb = null;
let activeSessions = {
  nsfw: localStorage.getItem("lamplight_session_nsfw") || null,
  friend: localStorage.getItem("lamplight_session_friend") || null
};
let stagedAttachments = [];
let mediaRecorder = null, audioChunks = [], recTimerInterval = null, recStartTime = 0;
let allSkills = [], activeSkill = null, slashSelectedIndex = -1, filteredSkills = [];
const $ = id => document.getElementById(id);
const savedTheme = localStorage.getItem("lamplight_theme") || "lamplight";
document.body.dataset.theme = savedTheme;

/* ---------- tiny helpers ---------- */
function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : ""); t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .4s"; setTimeout(() => t.remove(), 400); }, 2600);
}
function askConfirm(text) {
  return new Promise(res => {
    $("confirmText").textContent = text;
    $("confirmVeil").classList.remove("hidden");
    confirmCb = res;
  });
}
$("confirmYes").onclick = () => { $("confirmVeil").classList.add("hidden"); confirmCb && confirmCb(true); };
$("confirmNo").onclick = () => { $("confirmVeil").classList.add("hidden"); confirmCb && confirmCb(false); };

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts,
    headers: { "Content-Type": "application/json", "X-Passcode": passcode, ...(opts.headers || {}) } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detailMsg = Array.isArray(j.detail)
      ? j.detail.map(d => d.msg || JSON.stringify(d)).join(", ")
      : (typeof j.detail === "string" ? j.detail : (r.statusText || "Request failed"));
    throw new Error(detailMsg);
  }
  return j;
}
function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function unesc(s) {
  const d = document.createElement("textarea");
  d.innerHTML = s;
  return d.value;
}
// Mermaid diagram initialization for inline chat rendering
if (window.mermaid) {
  try {
    mermaid.initialize({
      startOnLoad: false,
      theme: 'dark',
      themeVariables: {
        darkMode: true,
        background: '#0d111a',
        primaryColor: '#1e293b',
        primaryTextColor: '#f8fafc',
        primaryBorderColor: 'rgba(255,255,255,0.15)',
        lineColor: '#00e5ff',
        secondaryColor: '#161e2e',
        tertiaryColor: '#0f172a'
      }
    });
  } catch (e) {}
}

async function renderChatMermaids(container = document) {
  if (!window.mermaid) return;
  const nodes = container.querySelectorAll(".mermaid-raw:not(.rendered)");
  nodes.forEach(async (el) => {
    el.classList.add("rendered");
    const code = el.getAttribute("data-code") || el.textContent;
    const id = "mmd-" + Math.random().toString(36).substring(2, 9);
    try {
      const res = await mermaid.render(id, code);
      el.innerHTML = res.svg;
    } catch (e) {
      el.innerHTML = `<div class="mermaid-syntax-note" style="color:#f43f5e; font-size:11px; padding:8px;">Diagram Syntax Note: ${esc(e.message || String(e))}</div>`;
    }
  });
}

window.insertPrompt = function(text) {
  const m = $("msg");
  if (!m) return;
  m.value = text;
  autosize();
  m.focus();
  toast("Prompt loaded into composer.");
};

function extractSocraticQuestions(text) {
  if (!text) return { cleanText: text, questions: [] };
  const lines = text.split("\n");
  const questions = [];
  let splitIdx = lines.length;
  let inQ = false;

  for (let i = lines.length - 1; i >= 0; i--) {
    const trimmed = lines[i].trim();
    if (!trimmed) continue;
    const match = trimmed.match(/^(?:[-*•]|\d+[\.)]|\?)\s+(.+\?)\s*$/);
    if (match) {
      questions.unshift(match[1].trim());
      inQ = true;
      splitIdx = i;
    } else if (inQ && (trimmed.toLowerCase().includes("explore") || trimmed.toLowerCase().includes("direction") || trimmed.toLowerCase().includes("question") || trimmed.toLowerCase().includes("choice") || trimmed.toLowerCase().includes("next") || trimmed.endsWith(":"))) {
      splitIdx = i;
      break;
    } else {
      if (inQ) break;
    }
  }

  if (questions.length >= 2 && questions.length <= 5) {
    const clean = lines.slice(0, splitIdx).join("\n").trimEnd();
    return { cleanText: clean, questions };
  }
  return { cleanText: text, questions: [] };
}

function parseCallouts(text) {
  return text.replace(/(?:^|<br>)\s*&gt;[ \t]*\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION|LORE|PLOT TWIST|ARCHITECT)\][ \t]*(?:<br>)?([\s\S]*?)(?=(?:<br>\s*<br>)|$)/gi, (match, type, content) => {
    const t = type.toUpperCase();
    const icons = {
      "NOTE": "ℹ️",
      "TIP": "💡",
      "WARNING": "⚠️",
      "IMPORTANT": "❗",
      "CAUTION": "🛑",
      "LORE": "📖",
      "PLOT TWIST": "⚡",
      "ARCHITECT": "📐"
    };
    const icon = icons[t] || "📌";
    const cleanContent = content.split(/<br>/i).map(l => l.replace(/^&gt;\s?/, "")).join("<br>");
    return `<div class="glass-callout callout-${t.toLowerCase().replace(/\s+/g, '-')}"><div class="callout-header"><span class="callout-icon">${icon}</span> <strong class="callout-title">${t}</strong></div><div class="callout-body">${cleanContent}</div></div>`;
  });
}

function parseSceneBreaks(text) {
  return text.replace(/(?:^|<br>)\s*[-*_]{3,}\s*(?:<br>|$)/g, '<div class="scene-break-divider"><span class="sb-line"></span><span class="sb-glyph">✦ ✦ ✦</span><span class="sb-line"></span></div>');
}

function highlightSyntax(code, lang) {
  if (!code) return "";
  const l = (lang || "").toLowerCase().trim();
  const escH = (s) => (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  if (["html", "xml", "svg", "htm"].includes(l)) {
    return code.replace(/(?<comm><!--[\s\S]*?-->)|(?<doctype><!DOCTYPE[^>]*>)|(?<tag><\/?[a-zA-Z0-9:-]+)|(?<attr>\s+[a-zA-Z0-9:-]+(?=\=))|(?<str>"(?:\\.|[^"\\])*"|'[^']*')|(?<close>\/?>)/gi,
      (m, ...args) => {
        const g = args[args.length - 1];
        if (g && g.comm) return `<span class="tok-comm">${escH(g.comm)}</span>`;
        if (g && g.doctype) return `<span class="tok-type">${escH(g.doctype)}</span>`;
        if (g && g.tag) return `<span class="tok-tag">${escH(g.tag)}</span>`;
        if (g && g.attr) return `<span class="tok-attr">${escH(g.attr)}</span>`;
        if (g && g.str) return `<span class="tok-str">${escH(g.str)}</span>`;
        if (g && g.close) return `<span class="tok-tag">${escH(g.close)}</span>`;
        return escH(m);
      }
    );
  }

  const isPy = ["python", "py"].includes(l);
  const isBash = ["bash", "sh", "zsh", "shell"].includes(l);
  const isSql = ["sql"].includes(l);
  const isRust = ["rust", "rs"].includes(l);

  const kwSrc = isPy ? "\\b(?:def|class|return|if|elif|else|for|while|break|continue|pass|import|from|as|try|except|finally|raise|with|yield|lambda|global|nonlocal|assert|del|and|or|not|is|in|async|await)\\b"
    : isBash ? "\\b(?:if|then|else|elif|fi|case|esac|for|while|until|do|done|in|function|select|time|return|exit|echo|set|export|source|alias|sudo|chmod|chown|mkdir|rm|cp|mv|cat|grep)\\b"
    : isSql ? "\\b(?:SELECT|FROM|WHERE|INSERT|INTO|UPDATE|DELETE|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|ON|GROUP\\s+BY|ORDER\\s+BY|HAVING|LIMIT|OFFSET|UNION|ALL|DISTINCT|AS|AND|OR|NOT|IN|IS|NULL|LIKE|BETWEEN|CREATE|TABLE|ALTER|DROP|INDEX|VIEW|TRIGGER|PRIMARY\\s+KEY|FOREIGN\\s+KEY|REFERENCES|CASCADE|DEFAULT|CHECK|VALUES|SET|COUNT|SUM|AVG|MIN|MAX|select|from|where|insert|into|update|delete|join|left|right|inner|outer|on|limit|offset|as|and|or|not|in|is|null)\\b"
    : isRust ? "\\b(?:fn|let|mut|pub|use|mod|struct|enum|impl|trait|match|where|crate|self|type|async|await|const|static|extern|unsafe|loop|for|while|if|else|return|break|continue)\\b"
    : "\\b(?:const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|default|import|export|from|as|class|extends|new|this|super|async|await|yield|try|catch|finally|throw|typeof|instanceof|void|delete|in|of|interface|type|enum|implements|package|private|protected|public|static|readonly|func|defer|go|struct)\\b";

  const commPattern = (isPy || isBash) ? "#[^\\n]*" : "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/";
  const strPattern = isPy ? "\"\"\"[\\s\\S]*?\"\"\"|'''[\\s\\S]*?'''|\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'"
    : "\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`";

  const typePattern = isPy ? "@[a-zA-Z_]\\w*(?:\\.\\w+)*|\\b(?:int|str|float|bool|bytes|list|dict|set|tuple|object|Any|Optional|Union|List|Dict|Tuple|Set|self|cls)\\b"
    : "@[a-zA-Z_]\\w*|\\b(?:String|Number|Boolean|Array|Object|Promise|Map|Set|Symbol|Function|Error|RegExp|Date|void|int|float|double|char|bool|any|unknown|never)\\b";

  const combined = new RegExp(
    `(?<comm>${commPattern})|(?<str>${strPattern})|(?<kw>${kwSrc})|(?<bool>\\b(?:true|false|null|undefined|True|False|None|nil)\\b)|(?<typ>${typePattern})|(?<fn>\\b[a-zA-Z_$][a-zA-Z0-9_$]*(?=\\s*\\())|(?<num>\\b(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][+-]?\\d+)?\\b|0x[0-9a-fA-F]+\\b)|(?<op>[=><!~?:&|+\\-*\\/\\^%]+)`,
    "g"
  );

  let result = "";
  let lastIndex = 0;
  let match;

  while ((match = combined.exec(code)) !== null) {
    if (match.index > lastIndex) {
      result += escH(code.slice(lastIndex, match.index));
    }
    const g = match.groups;
    if (g && g.comm) result += `<span class="tok-comm">${escH(g.comm)}</span>`;
    else if (g && g.str) result += `<span class="tok-str">${escH(g.str)}</span>`;
    else if (g && g.kw) result += `<span class="tok-kw">${escH(g.kw)}</span>`;
    else if (g && g.bool) result += `<span class="tok-bool">${escH(g.bool)}</span>`;
    else if (g && g.typ) result += `<span class="tok-type">${escH(g.typ)}</span>`;
    else if (g && g.fn) result += `<span class="tok-fn">${escH(g.fn)}</span>`;
    else if (g && g.num) result += `<span class="tok-num">${escH(g.num)}</span>`;
    else if (g && g.op) result += `<span class="tok-op">${escH(g.op)}</span>`;
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < code.length) {
    result += escH(code.slice(lastIndex));
  }
  return result;
}

function md(src) {
  if (!src) return "";

  // Check for embedded <think> in historical or completed text
  let historicalThinking = "";
  if (src.includes("<think>")) {
    const endIdx = src.indexOf("</think>");
    if (endIdx !== -1) {
      historicalThinking = src.substring(src.indexOf("<think>") + 7, endIdx).trim();
      src = src.substring(endIdx + 8).trimStart();
    }
  }

  // Extract Socratic questions before inline formatting
  const { cleanText, questions } = extractSocraticQuestions(src);
  src = cleanText;

  const blocks = [];
  let h = esc(src).replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const l = (lang || "").trim().toLowerCase();
    if (l === "mermaid") {
      const codeClean = unesc(code.trim());
      return `\u0000${blocks.push(`
        <div class="mermaid-chat-card">
          <div class="code-header">
            <span class="code-lang"><span class="lang-dot mmd-dot"></span> ARCHITECTURE DIAGRAM</span>
            <div class="code-header-actions">
              <button class="code-header-btn code-canvas-btn" type="button" onclick="openSnippetInCanvas(this)" title="Open in Interactive Canvas">✨ Canvas</button>
              <button class="code-header-btn code-copy-btn" type="button" onclick="copySnippet(this)">Copy</button>
            </div>
          </div>
          <div class="mermaid-raw" data-code="${esc(codeClean)}">
            <div class="mermaid">${esc(codeClean)}</div>
          </div>
        </div>
      `) - 1}\u0000`;
    }

    const langDisplay = l || "code";
    const highlighted = highlightSyntax(unesc(code), langDisplay);
    return `\u0000${blocks.push(`
      <div class="code-wrap">
        <div class="code-header">
          <span class="code-lang"><span class="lang-dot"></span> ${esc(langDisplay.toUpperCase())}</span>
          <div class="code-header-actions">
            <button class="code-header-btn code-canvas-btn" type="button" onclick="openSnippetInCanvas(this)" title="Open in Interactive Canvas">✨ Canvas</button>
            <button class="code-header-btn code-copy-btn" type="button" onclick="copySnippet(this)">Copy</button>
          </div>
        </div>
        <pre><code data-lang="${esc(langDisplay)}">${highlighted}</code></pre>
      </div>
    `) - 1}\u0000`;
  });

  // Inline formatting
  h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>")
       .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
       .replace(/(^|\W)\*([^*\n]+)\*/g, "$1<i>$2</i>")
       .replace(/\n/g, "<br>");

  // Callouts & scene breaks
  h = parseCallouts(h);
  h = parseSceneBreaks(h);

  // Citations
  h = h.replace(/\[Doc:\s*([^,\]]+),\s*p\.\s*(\d+)\]/gi, (_, docName, pageNum) => {
    return `<span class="doc-citation-pill" data-doc="${esc(docName)}" data-page="${pageNum}" onclick="handleCitationClick(this)">📄 ${esc(docName)}: p.${pageNum}</span>`;
  });
  h = h.replace(/\[Page\s*(\d+)\]/gi, (_, pageNum) => {
    return `<span class="doc-citation-pill" data-page="${pageNum}" onclick="handleCitationClick(this)">📄 Page ${pageNum}</span>`;
  });

  // Append Socratic choice pills if found
  if (questions && questions.length > 0) {
    const pillsHtml = `
      <div class="socratic-pills-row">
        ${questions.map(q => `<button type="button" class="socratic-glass-pill" onclick="insertPrompt(${JSON.stringify(unesc(q))})"><span>✦</span> ${q}</button>`).join("")}
      </div>
    `;
    h += pillsHtml;
  }

  let finalHtml = h.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);

  if (historicalThinking) {
    const tokenEst = Math.round(historicalThinking.split(/\s+/).filter(Boolean).length * 1.25);
    const hudHtml = `
      <details class="thinking-hud-accordion">
        <summary class="thinking-hud-summary">
          <span class="hud-chip">
            <span class="hud-chip-dot"></span>
            <span class="hud-chip-title">REASONING SCRATCHPAD</span>
          </span>
          <span class="hud-chip-meta">
            <span class="hud-token-count">${tokenEst} TOKENS</span> · <span class="hud-toggle-label">EXPAND ▾</span>
          </span>
        </summary>
        <div class="thinking-hud-body">${esc(historicalThinking)}</div>
      </details>
    `;
    finalHtml = hudHtml + finalHtml;
  }

  return finalHtml;
}

// Global listener to update EXPAND/COLLAPSE label on any thinking accordion toggle
document.addEventListener("toggle", (e) => {
  if (e.target && e.target.classList && e.target.classList.contains("thinking-hud-accordion")) {
    const lbl = e.target.querySelector(".hud-toggle-label");
    if (lbl) lbl.textContent = e.target.open ? "COLLAPSE ▴" : "EXPAND ▾";
  }
}, true);

function renderLiveReply(el, rawContent, rawThinking, isDone = false) {
  let content = rawContent || "";
  let thinking = rawThinking || "";

  if (content.includes("<think>")) {
    const endIdx = content.indexOf("</think>");
    if (endIdx !== -1) {
      if (!thinking) {
        thinking = content.substring(content.indexOf("<think>") + 7, endIdx).trim();
      }
      content = content.substring(endIdx + 8).trimStart();
    } else {
      if (!thinking) {
        thinking = content.substring(content.indexOf("<think>") + 7).trimStart();
      }
      content = "";
    }
  }

  el.dataset.raw = content || thinking;

  // Manage Thinking HUD Accordion with in-place updates
  let accordion = el.querySelector(".thinking-hud-accordion");
  if (thinking) {
    const tokenEst = Math.round(thinking.trim().split(/\s+/).filter(Boolean).length * 1.25);
    const isThinkingActive = !content && !isDone;

    if (!accordion) {
      accordion = document.createElement("details");
      accordion.className = "thinking-hud-accordion";
      accordion.open = isThinkingActive;
      accordion.innerHTML = `
        <summary class="thinking-hud-summary">
          <span class="hud-chip">
            <span class="hud-chip-dot ${isThinkingActive ? "pulse" : ""}"></span>
            <span class="hud-chip-title">${isThinkingActive ? "REASONING IN PROGRESS…" : "REASONING SCRATCHPAD"}</span>
          </span>
          <span class="hud-chip-meta">
            <span class="hud-token-count">${tokenEst} TOKENS</span> · <span class="hud-toggle-label">${accordion.open ? "COLLAPSE ▴" : "EXPAND ▾"}</span>
          </span>
        </summary>
        <div class="thinking-hud-body">${esc(thinking.trim())}</div>
      `;
      // User manual click stops any automatic collapse
      accordion.querySelector(".thinking-hud-summary").addEventListener("click", () => {
        accordion.dataset.userInteracted = "1";
      });
      accordion.addEventListener("toggle", () => {
        const lbl = accordion.querySelector(".hud-toggle-label");
        if (lbl) lbl.textContent = accordion.open ? "COLLAPSE ▴" : "EXPAND ▾";
      });

      // Remove any initial placeholder pulse dot before prepending
      const pulseDot = el.querySelector(".thinking-pulse-dot");
      if (pulseDot && pulseDot.parentNode === el) el.innerHTML = "";
      el.prepend(accordion);
    } else {
      // In-place update of existing DOM elements to avoid disrupting open state
      const dot = accordion.querySelector(".hud-chip-dot");
      if (dot) dot.className = `hud-chip-dot ${isThinkingActive ? "pulse" : ""}`;

      const title = accordion.querySelector(".hud-chip-title");
      if (title) title.textContent = isThinkingActive ? "REASONING IN PROGRESS…" : "REASONING SCRATCHPAD";

      const tokCount = accordion.querySelector(".hud-token-count");
      if (tokCount) tokCount.textContent = `${tokenEst} TOKENS`;

      const bodyEl = accordion.querySelector(".thinking-hud-body");
      if (bodyEl && bodyEl.textContent !== thinking.trim()) {
        bodyEl.textContent = thinking.trim();
      }

      // If transitioning from thinking to content and user hasn't explicitly interacted, collapse it once
      if (content && !accordion.dataset.userInteracted && accordion.open && !isDone) {
        accordion.open = false;
        const lbl = accordion.querySelector(".hud-toggle-label");
        if (lbl) lbl.textContent = "EXPAND ▾";
      }
    }
  } else if (accordion) {
    accordion.remove();
  }

  // Manage Message Content Body
  if (content) {
    let contentBody = el.querySelector(".msg-content-body");
    if (!contentBody) {
      contentBody = document.createElement("div");
      contentBody.className = "msg-content-body";
      el.appendChild(contentBody);
    }
    contentBody.innerHTML = md(content);
    renderChatMermaids(contentBody);

    const pDot = el.querySelector(".thinking-pulse-dot");
    if (pDot) {
      const pLbl = el.querySelector(".thinking-label");
      pDot.remove();
      if (pLbl) pLbl.remove();
    }
  } else if (!thinking) {
    if (!el.querySelector(".thinking-pulse-dot")) {
      el.innerHTML = `<span class="thinking-pulse-dot"></span> <span class="thinking-label">Thinking…</span>`;
    }
  }
}

window.handleCitationClick = function(el) {
  const doc = el.dataset.doc || "";
  const page = el.dataset.page || "";
  toast(`Citation: ${doc ? doc + " · " : ""}Page ${page}`, false);
};

function timeAgo(dateStr) {
  if (!dateStr) return "";
  const iso = dateStr.includes("T") ? dateStr : dateStr.replace(" ", "T") + "Z";
  const d = new Date(iso);
  const diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (isNaN(diff)) return "";
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

window.copySnippet = function(btn) {
  const wrap = btn.closest(".code-wrap");
  const code = wrap.querySelector("code").innerText;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = "Copied! ✓";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = "Copy";
      btn.classList.remove("copied");
    }, 2000);
  }).catch(() => toast("Failed to copy", true));
};

window.openSnippetInCanvas = function(btn) {
  if (typeof labOn !== "undefined" && labOn) {
    setMainView("chat");
  }
  const wrap = btn.closest(".code-wrap");
  const codeEl = wrap.querySelector("code");
  const rawCode = codeEl ? codeEl.innerText : "";
  const lang = (codeEl ? codeEl.dataset.lang : "") || wrap.querySelector(".code-lang")?.innerText?.toLowerCase() || "code";

  canvasState.code = rawCode;
  canvasState.lang = lang;
  canvasState.title = lang === "html" ? "Interactive Web App" :
                      lang === "svg" ? "Interactive SVG" :
                      lang === "mermaid" ? "Mermaid Diagram" :
                      lang === "json" ? "JSON Tree Explorer" :
                      lang === "javascript" || lang === "js" ? "JS Component" :
                      lang === "python" || lang === "py" ? "Python Script" :
                      lang === "markdown" || lang === "md" ? "Document" :
                      `${lang.toUpperCase()} Artifact`;

  if (["html", "htm", "svg", "markdown", "md", "javascript", "js", "mermaid", "json"].includes(lang.toLowerCase())) {
    canvasState.tab = "preview";
  } else {
    canvasState.tab = "code";
  }

  toggleCanvas(true);
};

/* ---------- lock screen & boot ---------- */
function wireLockScreen() {
  const codeInput = $("unlockCode");
  const lockCard = document.querySelector(".lock-card");
  const errorMsg = $("lockErrorMsg");
  const eyeBtn = $("btnTogglePassVisibility");
  const eyeSvg = $("eyeSvg");
  const dots = document.querySelectorAll("#lockPinDots .pin-dot");

  function updatePinDots() {
    const val = codeInput ? codeInput.value : "";
    dots.forEach((dot, idx) => {
      dot.classList.toggle("filled", idx < val.length);
    });
    if (errorMsg) errorMsg.classList.add("hidden");
    if (lockCard) lockCard.classList.remove("shake-error");
  }

  if (codeInput && !codeInput.dataset.wired) {
    codeInput.dataset.wired = "1";
    codeInput.addEventListener("input", updatePinDots);
    codeInput.addEventListener("keydown", e => {
      if (e.key === "Enter") unlock();
    });
  }

  // Keypad button interactions
  document.querySelectorAll(".keypad-btn").forEach(btn => {
    btn.onclick = (e) => {
      e.preventDefault();
      if (!codeInput) return;
      const key = btn.dataset.key;
      if (key === "clear") {
        codeInput.value = "";
      } else if (key === "backspace") {
        codeInput.value = codeInput.value.slice(0, -1);
      } else if (key) {
        if (codeInput.value.length < 32) {
          codeInput.value += key;
        }
      }
      updatePinDots();
      codeInput.focus();
    };
  });

  // Show/Hide password toggle
  if (eyeBtn && codeInput && !eyeBtn.dataset.wired) {
    eyeBtn.dataset.wired = "1";
    eyeBtn.onclick = (e) => {
      e.preventDefault();
      const isPass = codeInput.type === "password";
      codeInput.type = isPass ? "text" : "password";
      if (eyeSvg) {
        eyeSvg.innerHTML = isPass
          ? `<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line>`
          : `<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle>`;
      }
      codeInput.focus();
    };
  }

  if ($("btnUnlock")) $("btnUnlock").onclick = unlock;
}

async function unlock() {
  const codeInput = $("unlockCode");
  const lockCard = document.querySelector(".lock-card");
  const errorMsg = $("lockErrorMsg");
  const btnUnlock = $("btnUnlock");
  const code = codeInput ? codeInput.value.trim() : "";

  if (!code) {
    if (lockCard) {
      lockCard.classList.remove("shake-error");
      void lockCard.offsetWidth; // trigger reflow
      lockCard.classList.add("shake-error");
    }
    if (errorMsg) {
      errorMsg.textContent = "Please enter your passcode";
      errorMsg.classList.remove("hidden");
    }
    if (codeInput) codeInput.focus();
    return;
  }

  try {
    if (btnUnlock) {
      btnUnlock.disabled = true;
      const span = btnUnlock.querySelector("span");
      if (span) span.textContent = "Decrypting…";
    }
    const j = await api("/api/passcode/unlock", { method: "POST", body: JSON.stringify({ code }) });
    passcode = j.token;
    sessionStorage.setItem("pb_pass", passcode);

    if (lockCard) {
      lockCard.classList.add("unlock-success");
      const statusText = lockCard.querySelector(".lock-status-badge span:last-child");
      if (statusText) statusText.textContent = "CHASSIS UNLOCKED";
    }

    setTimeout(() => {
      $("lock").classList.add("hidden");
      showApp();
    }, 280);
  } catch (e) {
    if (btnUnlock) {
      btnUnlock.disabled = false;
      const span = btnUnlock.querySelector("span");
      if (span) span.textContent = "Decrypt & Unlock";
    }
    if (lockCard) {
      lockCard.classList.remove("shake-error");
      void lockCard.offsetWidth;
      lockCard.classList.add("shake-error");
    }
    if (errorMsg) {
      errorMsg.textContent = "Access Denied: Invalid Passcode";
      errorMsg.classList.remove("hidden");
    }
    if (codeInput) {
      codeInput.value = "";
      document.querySelectorAll("#lockPinDots .pin-dot").forEach(d => d.classList.remove("filled"));
      codeInput.focus();
    }
  }
}

async function boot() {
  wireLockScreen();
  const g = await api("/api/gate").catch(() => null);
  if (!g) { toast("Server unreachable — is the service running?", true); return; }
  if (g.need18) $("gate").classList.remove("hidden");
  else if (g.hasPasscode && !passcode) {
    $("lock").classList.remove("hidden");
    setTimeout(() => $("unlockCode")?.focus(), 100);
  }
  else showApp();
}
$("btn18").onclick = async () => { await api("/api/confirm-18", { method: "POST" }); $("gate").classList.add("hidden"); boot(); };
function showApp() {
  document.body.dataset.mode = mode;
  $("app").classList.remove("hidden");
  $("sessionDrawerHeading").textContent = mode === "muse" ? "Muse Stories & Brainstorms" : (mode === "architect" ? "Architect Chats" : (mode === "nsfw" ? "After Dark Chats" : "Friend Chats"));
  $("msg").placeholder = mode === "muse" ? "Ask Muse: brainstorm plots, interrogate scenes, flesh out characters, craft dialogue…" : (mode === "architect" ? "Ask Architect: code, architecture, refactoring, systems…" : (mode === "friend" ? "Message friend…" : "Message After Dark… (local only)"));
  $("btnMemory").classList.toggle("hidden", mode !== "friend");
  if ($("btnArchitectStack")) $("btnArchitectStack").classList.toggle("hidden", mode !== "architect");
  if ($("btnMuseBible")) $("btnMuseBible").classList.toggle("hidden", mode !== "muse");
  wireTabs(); moveGlow(); loadPicker(); loadHistory(); loadSessions(mode); loadVramStatus(); wireThemes(); loadSkills(); wireSkills(); wireTts(); loadTtsSettings(); wireCanvas(); wireLab(); wireArena(); wireProviderForm(); wireDocHub(); wireObservatory(); wirePrompts(); wireWorkbenchMenu(); wireArchitect(); wireMuse(); wireThinkingLevels(); wireModelCartridge();
}

/* ---------- Interactive Canvas Workspace ---------- */
let canvasState = {
  open: false,
  title: "Interactive Canvas",
  lang: "html",
  code: `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 24px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f121a; color: #e2e8f0; text-align: center;
    }
    .badge {
      display: inline-block; padding: 4px 12px; background: rgba(240, 168, 50, 0.15);
      border: 1px solid rgba(240, 168, 50, 0.3); color: #f0a832; border-radius: 20px;
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
      margin-bottom: 16px;
    }
    h2 { margin: 0 0 10px; font-size: 22px; color: #fff; }
    p { color: #94a3b8; font-size: 14px; max-width: 440px; margin: 0 auto 24px; line-height: 1.6; }
    .demo-btn {
      background: linear-gradient(135deg, #f0a832, #d48b1d); color: #0b0802;
      border: 0; padding: 12px 24px; font-weight: 600; font-size: 14px;
      border-radius: 12px; cursor: pointer; transition: transform 0.18s, filter 0.18s;
      box-shadow: 0 4px 20px rgba(240, 168, 50, 0.25);
    }
    .demo-btn:hover { transform: translateY(-2px); filter: brightness(1.1); }
    .demo-btn:active { transform: translateY(0); }
    .counter { margin-top: 20px; font-size: 15px; color: #38bdf8; font-weight: 600; }
  </style>
</head>
<body>
  <div class="badge">Canvas Sandbox Ready</div>
  <h2>Interactive Workspace</h2>
  <p>Run web apps, SVG graphics, JavaScript utilities, or write HTML components alongside your chats.</p>
  <button class="demo-btn" onclick="inc()">Click to Interact</button>
  <div id="count" class="counter">Taps: 0</div>
  <script>
    let n = 0;
    function inc() {
      n++;
      document.getElementById('count').textContent = 'Taps: ' + n + ' ⚡ (Sandbox is live!)';
    }
  </script>
</body>
</html>`,
  tab: "preview"
};

function toggleCanvas(force = null) {
  canvasState.open = force !== null ? force : !canvasState.open;
  const panel = $("canvasPanel");
  const mainApp = $("app");
  const btn = $("btnCanvas");

  if (!panel || !mainApp) return;

  panel.classList.toggle("hidden", !canvasState.open);
  mainApp.classList.toggle("canvas-open", canvasState.open);
  if (btn) btn.classList.toggle("on", canvasState.open);

  if (canvasState.open) {
    renderCanvas();
  }
}

let canvasSyntaxEnabled = true;

function updateCanvasEditorHighlight() {
  const codeArea = $("canvasCodeArea");
  const highlightInner = $("canvasCodeHighlightInner");
  const highlightLayer = $("canvasCodeHighlight");
  if (!codeArea || !highlightInner) return;

  if (!canvasSyntaxEnabled) {
    codeArea.classList.add("plain-mode");
    if (highlightLayer) highlightLayer.style.display = "none";
    return;
  }

  codeArea.classList.remove("plain-mode");
  if (highlightLayer) highlightLayer.style.display = "block";
  const code = codeArea.value || "";
  highlightInner.innerHTML = highlightSyntax(code, canvasState.lang || "code");
  if (highlightLayer) {
    highlightLayer.scrollTop = codeArea.scrollTop;
    highlightLayer.scrollLeft = codeArea.scrollLeft;
  }
}

function renderCanvas() {
  if ($("canvasTitle")) $("canvasTitle").value = canvasState.title;
  if ($("canvasTypeBadge")) $("canvasTypeBadge").textContent = (canvasState.lang || "HTML").toUpperCase();
  if ($("canvasCodeArea")) $("canvasCodeArea").value = canvasState.code;
  updateCanvasLineNumbers();
  updateCanvasEditorHighlight();
  setCanvasTab(canvasState.tab || "preview");
}

function updateCanvasLineNumbers() {
  const code = $("canvasCodeArea") ? $("canvasCodeArea").value : (canvasState.code || "");
  const lines = code.split("\n").length;
  if ($("canvasLineNumbers")) {
    $("canvasLineNumbers").textContent = Array.from({ length: Math.max(lines, 1) }, (_, i) => i + 1).join("\n");
  }
}

function setCanvasTab(tab) {
  canvasState.tab = tab;
  if ($("btnCanvasPreview")) $("btnCanvasPreview").classList.toggle("on", tab === "preview");
  if ($("btnCanvasCode")) $("btnCanvasCode").classList.toggle("on", tab === "code");
  if ($("canvasPreviewView")) $("canvasPreviewView").classList.toggle("hidden", tab !== "preview");
  if ($("canvasCodeView")) $("canvasCodeView").classList.toggle("hidden", tab !== "code");

  if (tab === "preview") {
    runCanvasSandbox();
  }
}

const CONSOLE_INTERCEPTOR_TAG = `<script>
(function() {
  const _log = console.log, _warn = console.warn, _error = console.error, _info = console.info;
  function relay(lvl, args) {
    try {
      const msg = args.map(a => {
        if (typeof a === "object") {
          try { return JSON.stringify(a, null, 2); } catch(e) { return String(a); }
        }
        return String(a);
      }).join(" ");
      window.parent.postMessage({ type: "canvas_console", level: lvl, message: msg, time: new Date().toLocaleTimeString() }, "*");
    } catch(e) {}
  }
  console.log = function(...args) { relay("info", args); if (_log) _log.apply(console, args); };
  console.warn = function(...args) { relay("warn", args); if (_warn) _warn.apply(console, args); };
  console.error = function(...args) { relay("error", args); if (_error) _error.apply(console, args); };
  console.info = function(...args) { relay("info", args); if (_info) _info.apply(console, args); };
  window.addEventListener("error", function(e) {
    relay("error", [e.message + (e.filename ? " (" + e.filename + ":" + e.lineno + ")" : "")]);
  });
})();
<\/script>`;

function buildJsonTreeHtml(jsonString) {
  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 20px;
      background: #080b12;
      background-image: radial-gradient(ellipse 90% 45% at 50% 0%, rgba(56, 189, 248, 0.08), transparent 75%);
      color: #e2e8f0;
      font-family: "JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px; line-height: 1.6;
    }
    .json-toolbar {
      display: flex; gap: 8px; margin-bottom: 16px; align-items: center;
      background: rgba(13, 17, 28, 0.9); padding: 8px 12px; border-radius: 8px;
      border: 1px solid rgba(56, 189, 248, 0.2); box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    }
    .json-btn {
      background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
      color: #94a3b8; border-radius: 6px; padding: 5px 12px; font-size: 11px; cursor: pointer;
      font-family: inherit; font-weight: 500; transition: all 0.2s;
    }
    .json-btn:hover { background: rgba(56, 189, 248, 0.18); border-color: rgba(56, 189, 248, 0.4); color: #fff; }
    .node { margin-left: 18px; position: relative; }
    .toggle {
      cursor: pointer; user-select: none; color: #94a3b8; display: inline-block;
      width: 14px; text-align: center; margin-right: 4px; font-size: 10px;
    }
    .toggle:hover { color: #38bdf8; }
    .key { color: #38bdf8; font-weight: 600; }
    .string { color: #34d399; }
    .number { color: #fbbf24; }
    .boolean { color: #c084fc; font-weight: 600; }
    .null { color: #f43f5e; font-style: italic; }
    .badge { font-size: 11px; color: #64748b; margin-left: 6px; }
    .collapsed > .children { display: none; }
    .collapsed > .toggle::before { content: "▶"; }
    .expanded > .toggle::before { content: "▼"; }
    .error-banner {
      background: rgba(244, 63, 94, 0.15); border: 1px solid #f43f5e; color: #fca5a5;
      padding: 12px 16px; border-radius: 8px; font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="json-toolbar">
    <button class="json-btn" onclick="expandAll()">Expand All</button>
    <button class="json-btn" onclick="collapseAll()">Collapse All</button>
    <button class="json-btn" onclick="copyRaw()">Copy JSON</button>
  </div>
  <div id="treeContainer"></div>
  <script>
    const rawData = ${JSON.stringify(jsonString)};
    let parsed;
    try {
      parsed = JSON.parse(rawData);
    } catch(e) {
      document.getElementById('treeContainer').innerHTML = '<div class="error-banner"><strong>Invalid JSON:</strong> ' + e.message + '</div>';
    }

    function createTree(val, key = null) {
      const wrap = document.createElement('div');
      wrap.className = 'node expanded';

      const line = document.createElement('div');
      line.className = 'line';

      if (val !== null && typeof val === 'object') {
        const isArr = Array.isArray(val);
        const toggle = document.createElement('span');
        toggle.className = 'toggle';
        toggle.onclick = (e) => {
          e.stopPropagation();
          wrap.classList.toggle('collapsed');
          wrap.classList.toggle('expanded');
        };
        line.appendChild(toggle);

        if (key !== null) {
          const kSpan = document.createElement('span');
          kSpan.className = 'key';
          kSpan.textContent = '"' + key + '": ';
          line.appendChild(kSpan);
        }

        const openBracket = document.createTextNode(isArr ? '[' : '{');
        line.appendChild(openBracket);

        const keys = Object.keys(val);
        const countSpan = document.createElement('span');
        countSpan.className = 'badge';
        countSpan.textContent = keys.length + (isArr ? ' items' : ' keys');
        line.appendChild(countSpan);
        wrap.appendChild(line);

        const children = document.createElement('div');
        children.className = 'children';
        keys.forEach(k => {
          children.appendChild(createTree(val[k], isArr ? null : k));
        });
        wrap.appendChild(children);

        const closeLine = document.createElement('div');
        closeLine.style.marginLeft = '18px';
        closeLine.textContent = isArr ? ']' : '}';
        wrap.appendChild(closeLine);
      } else {
        if (key !== null) {
          const kSpan = document.createElement('span');
          kSpan.className = 'key';
          kSpan.textContent = '"' + key + '": ';
          line.appendChild(kSpan);
        }
        const vSpan = document.createElement('span');
        if (typeof val === 'string') {
          vSpan.className = 'string';
          vSpan.textContent = '"' + val + '"';
        } else if (typeof val === 'number') {
          vSpan.className = 'number';
          vSpan.textContent = String(val);
        } else if (typeof val === 'boolean') {
          vSpan.className = 'boolean';
          vSpan.textContent = String(val);
        } else if (val === null) {
          vSpan.className = 'null';
          vSpan.textContent = 'null';
        }
        line.appendChild(vSpan);
        wrap.appendChild(line);
      }
      return wrap;
    }

    if (parsed !== undefined) {
      document.getElementById('treeContainer').appendChild(createTree(parsed));
    }

    function expandAll() {
      document.querySelectorAll('.node').forEach(n => { n.classList.remove('collapsed'); n.classList.add('expanded'); });
    }
    function collapseAll() {
      document.querySelectorAll('.node').forEach(n => { n.classList.add('collapsed'); n.classList.remove('expanded'); });
    }
    function copyRaw() {
      navigator.clipboard.writeText(rawData);
    }
  <\/script>
</body>
</html>`;
}

function buildMermaidHtml(mermaidCode) {
  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <script src="/static/mermaid.min.js"><\/script>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 24px;
      background: #0f121a; color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
      min-height: 100vh;
    }
    .mermaid-card {
      background: #141824;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px;
      padding: 24px;
      max-width: 100%;
      overflow: auto;
      box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    }
    .mermaid svg {
      max-width: 100% !important;
      height: auto !important;
    }
    #error-box {
      display: none;
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid #ef4444;
      color: #fca5a5;
      padding: 12px 16px;
      border-radius: 8px;
      margin-top: 16px;
      font-family: monospace;
      font-size: 13px;
      max-width: 600px;
      white-space: pre-wrap;
    }
  </style>
</head>
<body>
  <div class="mermaid-card">
    <div class="mermaid">
${mermaidCode}
    </div>
  </div>
  <div id="error-box"></div>
  <script>
    try {
      mermaid.initialize({
        startOnLoad: true,
        theme: 'dark',
        themeVariables: {
          darkMode: true,
          background: '#141824',
          primaryColor: '#f0a832',
          primaryTextColor: '#fff',
          primaryBorderColor: '#d97706',
          lineColor: '#94a3b8',
          secondaryColor: '#38bdf8',
          tertiaryColor: '#1e293b'
        }
      });
    } catch(err) {
      const eb = document.getElementById('error-box');
      eb.style.display = 'block';
      eb.textContent = 'Mermaid Render Error: ' + err.message;
    }
  <\/script>
</body>
</html>`;
}

function runCanvasSandbox() {
  const frame = $("canvasSandboxFrame");
  if (!frame) return;

  const rawCode = $("canvasCodeArea") ? $("canvasCodeArea").value : canvasState.code;
  canvasState.code = rawCode;
  const lang = (canvasState.lang || "html").toLowerCase();

  let htmlContent = "";

  if (lang === "mermaid") {
    htmlContent = buildMermaidHtml(rawCode);
  } else if (lang === "json") {
    htmlContent = buildJsonTreeHtml(rawCode);
  } else if (lang === "html" || lang === "htm") {
    if (rawCode.includes("<!DOCTYPE") || rawCode.includes("<html") || rawCode.includes("<body")) {
      htmlContent = rawCode.includes("<head>")
        ? rawCode.replace("<head>", "<head>" + CONSOLE_INTERCEPTOR_TAG)
        : CONSOLE_INTERCEPTOR_TAG + rawCode;
    } else {
      htmlContent = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  ${CONSOLE_INTERCEPTOR_TAG}
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 20px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f121a; color: #e2e8f0; line-height: 1.6;
    }
  </style>
</head>
<body>
  ${rawCode}
</body>
</html>`;
    }
  } else if (lang === "svg") {
    htmlContent = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {
      margin: 0; display: flex; align-items: center; justify-content: center;
      min-height: 100vh; background: #0f121a;
      background-image: radial-gradient(rgba(255,255,255,0.06) 1px, transparent 1px);
      background-size: 20px 20px; padding: 24px; box-sizing: border-box;
    }
    svg { max-width: 100%; max-height: 90vh; filter: drop-shadow(0 8px 24px rgba(0,0,0,0.5)); }
  </style>
</head>
<body>
  ${rawCode}
</body>
</html>`;
  } else if (lang === "javascript" || lang === "js") {
    htmlContent = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  ${CONSOLE_INTERCEPTOR_TAG}
  <style>
    body {
      margin: 0; padding: 18px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      background: #0a0d14; color: #7dd3fc; font-size: 13px; line-height: 1.5;
    }
    .log-line { border-bottom: 1px solid rgba(255,255,255,0.06); padding: 5px 0; }
    .log-error { color: #f87171; }
    .log-warn { color: #fbbf24; }
    .header-bar { color: #94a3b8; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 10px; }
  </style>
</head>
<body>
  <div class="header-bar">Console Output</div>
  <div id="output"></div>
  <script>
    const out = document.getElementById('output');
    function log(msg, cls = '') {
      const d = document.createElement('div');
      d.className = 'log-line ' + cls;
      d.textContent = (typeof msg === 'object') ? JSON.stringify(msg, null, 2) : String(msg);
      out.appendChild(d);
    }
    console.log = (...args) => log(args.join(' '));
    console.error = (...args) => log(args.join(' '), 'log-error');
    console.warn = (...args) => log(args.join(' '), 'log-warn');
    try {
      ${rawCode}
    } catch(e) {
      console.error(e.name + ': ' + e.message);
    }
  <\/script>
</body>
</html>`;
  } else if (lang === "markdown" || lang === "md") {
    htmlContent = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 28px 36px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
      background: #080b12; color: #e2e8f0; line-height: 1.7;
      max-width: 900px; margin: 0 auto;
    }
    h1, h2, h3, h4 { color: #f8fafc; font-weight: 700; letter-spacing: -0.01em; }
    h1 { font-size: 1.8rem; border-bottom: 2px solid; border-image: linear-gradient(90deg, #38bdf8, #fbbf24, transparent) 1; padding-bottom: 8px; margin-top: 0; }
    h2 { font-size: 1.35rem; color: #38bdf8; border-bottom: 1px solid rgba(56, 189, 248, 0.2); padding-bottom: 6px; margin-top: 24px; }
    h3 { font-size: 1.15rem; color: #fbbf24; margin-top: 20px; }
    p { margin: 12px 0; color: #cbd5e1; }
    a { color: #38bdf8; text-decoration: none; border-bottom: 1px dashed rgba(56, 189, 248, 0.4); }
    a:hover { border-bottom-style: solid; }
    code { background: rgba(56, 189, 248, 0.1); color: #7dd3fc; border: 1px solid rgba(56, 189, 248, 0.2); padding: 2px 7px; border-radius: 5px; font-family: "JetBrains Mono", ui-monospace, Menlo, monospace; font-size: 0.9em; }
    pre { background: #0c101a; border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 10px; padding: 14px 18px; overflow-x: auto; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
    pre code { background: transparent; border: 0; padding: 0; color: #e2e8f0; }
    blockquote { border-left: 3px solid #38bdf8; margin: 16px 0; padding: 8px 18px; background: rgba(56, 189, 248, 0.05); border-radius: 0 8px 8px 0; color: #94a3b8; font-style: italic; }
    table { width: 100%; border-collapse: collapse; margin: 16px 0; background: rgba(15, 20, 32, 0.6); border-radius: 8px; overflow: hidden; border: 1px solid rgba(255, 255, 255, 0.08); }
    th { background: rgba(56, 189, 248, 0.12); color: #38bdf8; padding: 10px 14px; text-align: left; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid rgba(56, 189, 248, 0.2); }
    td { padding: 10px 14px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); color: #cbd5e1; font-size: 13px; }
    tr:nth-child(even) td { background: rgba(255, 255, 255, 0.02); }
    ul, ol { padding-left: 22px; color: #cbd5e1; }
    li { margin: 6px 0; }
  </style>
</head>
<body>
  ${md(rawCode)}
</body>
</html>`;
  } else {
    const lines = (rawCode || "").split("\n");
    const gutterHtml = Array.from({ length: Math.max(lines.length, 1) }, (_, i) => i + 1).join("\n");
    const highlightedCode = highlightSyntax(rawCode, lang);
    htmlContent = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 16px;
      background: #080b12;
      background-image: radial-gradient(ellipse 90% 45% at 50% 0%, rgba(56, 189, 248, 0.08), transparent 75%);
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      min-height: 100vh;
      display: flex; flex-direction: column;
    }
    .studio-card {
      flex: 1;
      display: flex; flex-direction: column;
      background: rgba(13, 17, 28, 0.9);
      border: 1px solid rgba(56, 189, 248, 0.22);
      border-radius: 14px;
      box-shadow: 0 16px 48px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.06);
      overflow: hidden;
    }
    .studio-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 16px;
      background: rgba(255,255,255,0.03);
      border-bottom: 1px solid rgba(255,255,255,0.08);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11px;
    }
    .studio-badge {
      display: inline-flex; align-items: center; gap: 7px;
      padding: 3px 10px; border-radius: 6px;
      background: rgba(56, 189, 248, 0.15);
      color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35);
      font-weight: 700; letter-spacing: 0.08em;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .studio-dot { width: 6px; height: 6px; border-radius: 50%; background: #38bdf8; box-shadow: 0 0 8px #38bdf8; }
    .studio-meta { color: #64748b; font-size: 11px; }
    .studio-actions button {
      background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
      color: #cbd5e1; border-radius: 6px; padding: 4px 12px; font-size: 11px;
      cursor: pointer; transition: all 0.2s; font-family: inherit; font-weight: 500;
    }
    .studio-actions button:hover {
      background: rgba(56, 189, 248, 0.2); border-color: rgba(56, 189, 248, 0.45); color: #fff;
    }
    .studio-body {
      flex: 1; display: flex; overflow: auto;
      background: #090d16;
      font-family: "JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px; line-height: 22px;
    }
    .studio-gutter {
      padding: 16px 12px 16px 14px;
      background: rgba(4, 6, 10, 0.75);
      border-right: 1px solid rgba(56, 189, 248, 0.15);
      color: #475569; user-select: none; text-align: right; min-width: 44px;
      white-space: pre; font-family: inherit; font-size: 13px; line-height: 22px;
    }
    .studio-code {
      flex: 1; padding: 16px 18px; margin: 0; white-space: pre; overflow-x: auto;
      color: #e2e8f0; tab-size: 2; font-family: inherit; font-size: 13px; line-height: 22px;
      background: transparent; border: 0;
    }
    .studio-code code {
      font-family: inherit; font-size: inherit; line-height: inherit;
      background: transparent; border: 0; padding: 0;
    }
    /* Syntax Tokens */
    .tok-kw { color: #38bdf8 !important; font-weight: 600; }
    .tok-str { color: #34d399 !important; }
    .tok-fn { color: #fbbf24 !important; font-weight: 500; }
    .tok-type { color: #a78bfa !important; }
    .tok-num { color: #f43f5e !important; }
    .tok-bool { color: #fb7185 !important; font-weight: 600; }
    .tok-comm { color: #64748b !important; font-style: italic; }
    .tok-tag { color: #38bdf8 !important; font-weight: 600; }
    .tok-attr { color: #c084fc !important; }
    .tok-op { color: #93c5fd !important; }
    .tok-prop { color: #2dd4bf !important; }
  </style>
</head>
<body>
  <div class="studio-card">
    <div class="studio-header">
      <div class="studio-badge"><span class="studio-dot"></span> ${esc(lang.toUpperCase())} ARTIFACT</div>
      <div class="studio-meta">${lines.length} lines · ${rawCode.length} chars</div>
      <div class="studio-actions">
        <button id="copyBtn" onclick="navigator.clipboard.writeText(document.getElementById('rawSource').textContent); this.textContent='Copied!'; setTimeout(()=>this.textContent='Copy Code', 1500)">Copy Code</button>
      </div>
    </div>
    <div class="studio-body">
      <div class="studio-gutter">${gutterHtml}</div>
      <pre class="studio-code"><code>${highlightedCode}</code></pre>
    </div>
  </div>
  <div id="rawSource" style="display:none">${esc(rawCode)}</div>
</body>
</html>`;
  }

  frame.srcdoc = htmlContent;
  if ($("canvasStatus")) $("canvasStatus").textContent = "Live";
}

function downloadCanvasFile() {
  const code = $("canvasCodeArea") ? $("canvasCodeArea").value : canvasState.code;
  const lang = (canvasState.lang || "txt").toLowerCase();
  const extMap = {
    html: "html", htm: "html", javascript: "js", js: "js",
    css: "css", python: "py", py: "py", svg: "svg",
    markdown: "md", md: "md", json: "json", sql: "sql",
    rust: "rs", rs: "rs", go: "go", c: "c", cpp: "cpp", mermaid: "mmd"
  };
  const ext = extMap[lang] || "txt";
  const filename = (canvasState.title || "artifact").toLowerCase().replace(/[^a-z0-9_-]/g, "_") + "." + ext;

  const blob = new Blob([code], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
  toast(`Downloaded ${filename}`);
}

/* ---------- Canvas Viewport, Console, Snapshots & ZIP ---------- */
function setCanvasViewport(vp) {
  const wrap = $("canvasViewportWrap");
  if (!wrap) return;
  wrap.classList.remove("viewport-desktop", "viewport-tablet", "viewport-mobile");
  wrap.classList.add("viewport-" + vp);
  if ($("btnViewportDesktop")) $("btnViewportDesktop").classList.toggle("on", vp === "desktop");
  if ($("btnViewportTablet")) $("btnViewportTablet").classList.toggle("on", vp === "tablet");
  if ($("btnViewportMobile")) $("btnViewportMobile").classList.toggle("on", vp === "mobile");
}

let canvasConsoleLogs = [];
function addCanvasConsoleEntry(level, msg, time) {
  canvasConsoleLogs.push({ level, msg, time });
  if (canvasConsoleLogs.length > 200) canvasConsoleLogs.shift();
  renderCanvasConsole();
}
function renderCanvasConsole() {
  const container = $("canvasConsoleLogs");
  const badge = $("canvasConsoleCount");
  if (badge) badge.textContent = `${canvasConsoleLogs.length} logs`;
  if (!container) return;
  if (!canvasConsoleLogs.length) {
    container.innerHTML = '<div class="console-empty">No console messages yet.</div>';
    return;
  }
  container.innerHTML = canvasConsoleLogs.map(l => `
    <div class="console-log-entry ${esc(l.level)}">
      <span class="console-time">${esc(l.time || "")}</span>
      <span class="console-level">[${esc(l.level.toUpperCase())}]</span>
      <span class="console-msg">${esc(l.msg)}</span>
    </div>
  `).join("");
  container.scrollTop = container.scrollHeight;
}
function clearCanvasConsole() {
  canvasConsoleLogs = [];
  renderCanvasConsole();
}
function toggleCanvasConsole(force = null) {
  const drawer = $("canvasConsoleDrawer");
  if (!drawer) return;
  const isHidden = drawer.classList.contains("hidden");
  const show = force !== null ? force : isHidden;
  drawer.classList.toggle("hidden", !show);
  if ($("btnCanvasConsole")) $("btnCanvasConsole").classList.toggle("on", show);
}

window.addEventListener("message", (ev) => {
  if (ev.data && ev.data.type === "canvas_console") {
    addCanvasConsoleEntry(ev.data.level || "info", ev.data.message || "", ev.data.time || "");
  }
});

async function saveCanvasSnapshot(customDesc = null) {
  const code = $("canvasCodeArea") ? $("canvasCodeArea").value : canvasState.code;
  const title = canvasState.title || "Interactive Canvas";
  const lang = canvasState.lang || "html";
  const desc = customDesc !== null ? customDesc : prompt("Enter snapshot note (e.g. 'Added responsive styling'):", `Checkpoint at ${new Date().toLocaleTimeString()}`);
  if (desc === null) return;

  try {
    const res = await api("/api/canvas/snapshots", {
      method: "POST",
      body: JSON.stringify({
        title,
        lang,
        code,
        description: desc || `Snapshot ${new Date().toLocaleTimeString()}`
      })
    });
    toast(`Saved snapshot #${res.snapshot_id}! 📜`);
    if ($("canvasSnapshotDrawer") && !$("canvasSnapshotDrawer").classList.contains("hidden")) {
      loadCanvasSnapshots();
    }
  } catch (err) {
    toast("Failed to save snapshot: " + err.message, true);
  }
}

async function loadCanvasSnapshots() {
  const listEl = $("canvasSnapshotList");
  if (!listEl) return;
  listEl.innerHTML = '<div class="muted" style="padding:16px;text-align:center;">Loading snapshots…</div>';

  try {
    const snapshots = await api("/api/canvas/snapshots");
    if (!snapshots || snapshots.length === 0) {
      listEl.innerHTML = '<div class="snapshot-empty">No snapshots saved yet. Click "+ Save Checkpoint" to record artifact history.</div>';
      return;
    }

    listEl.innerHTML = snapshots.map(s => `
      <div class="snapshot-item" data-id="${s.id}">
        <div class="snapshot-item-header">
          <div class="snapshot-item-title">${esc(s.title || "Untitled")}</div>
          <span class="snapshot-lang-tag">${esc(s.lang || "TXT")}</span>
        </div>
        <div class="snapshot-desc">${esc(s.description || "No note")}</div>
        <div class="snapshot-meta-row">
          <span class="snapshot-meta">${timeAgo(s.created_at)} · ${(s.code || "").length} chars</span>
          <div class="snapshot-btn-group">
            <button class="ghost-xs" onclick="restoreSnapshot(${s.id})" title="Restore this version">Restore ↺</button>
            <button class="ghost-xs danger" onclick="deleteSnapshot(${s.id})" title="Delete checkpoint">🗑️</button>
          </div>
        </div>
      </div>
    `).join("");
  } catch (err) {
    listEl.innerHTML = `<div class="error-banner" style="padding:12px;">Failed to load snapshots: ${esc(err.message)}</div>`;
  }
}

async function restoreSnapshot(snapId) {
  try {
    const snapshots = await api("/api/canvas/snapshots");
    const snap = snapshots.find(s => s.id === snapId);
    if (!snap) {
      toast("Snapshot not found", true);
      return;
    }
    canvasState.title = snap.title;
    canvasState.lang = snap.lang;
    canvasState.code = snap.code;
    renderCanvas();
    toggleCanvasSnapshots(false);
    toast(`Restored snapshot: "${snap.description || snap.title}"`);
  } catch (err) {
    toast("Failed to restore snapshot: " + err.message, true);
  }
}

async function deleteSnapshot(snapId) {
  try {
    await api(`/api/canvas/snapshots/${snapId}`, { method: "DELETE" });
    toast("Snapshot deleted");
    loadCanvasSnapshots();
  } catch (err) {
    toast("Failed to delete snapshot: " + err.message, true);
  }
}

function toggleCanvasSnapshots(force = null) {
  const drawer = $("canvasSnapshotDrawer");
  if (!drawer) return;
  const isHidden = drawer.classList.contains("hidden");
  const show = force !== null ? force : isHidden;
  drawer.classList.toggle("hidden", !show);
  if ($("btnCanvasHistory")) $("btnCanvasHistory").classList.toggle("on", show);
  if (show) loadCanvasSnapshots();
}

async function exportCanvasZip() {
  const code = $("canvasCodeArea") ? $("canvasCodeArea").value : canvasState.code;
  const title = (canvasState.title || "artifact").toLowerCase().replace(/[^a-z0-9_-]/g, "_");
  const lang = (canvasState.lang || "html").toLowerCase();

  let primaryName = "index.html";
  if (lang === "svg") primaryName = "graphic.svg";
  else if (lang === "javascript" || lang === "js") primaryName = "app.js";
  else if (lang === "python" || lang === "py") primaryName = "main.py";
  else if (lang === "markdown" || lang === "md") primaryName = "README.md";
  else if (lang === "json") primaryName = "data.json";
  else if (lang === "mermaid") primaryName = "diagram.mmd";

  const files = [
    { name: primaryName, content: code },
    {
      name: "README.md",
      content: `# ${canvasState.title}\n\nGenerated with Lamplight AI Workbench Interactive Canvas.\nDate: ${new Date().toISOString()}\nLanguage: ${lang}\n`
    }
  ];

  try {
    toast("Preparing ZIP export…");
    const resp = await fetch("/api/canvas/export_zip", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Passcode": passcode
      },
      body: JSON.stringify({
        filename: `${title}.zip`,
        files
      })
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "ZIP export failed");
    }

    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${title}.zip`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast(`Exported ${title}.zip! 📦`);
  } catch (err) {
    toast("Failed to export ZIP: " + err.message, true);
  }
}

function wireCanvas() {
  if ($("btnCanvas")) $("btnCanvas").onclick = () => toggleCanvas();
  if ($("btnCanvasClose")) $("btnCanvasClose").onclick = () => toggleCanvas(false);
  if ($("btnCanvasFullscreen")) {
    $("btnCanvasFullscreen").onclick = () => {
      const app = $("app");
      const isMax = app.classList.toggle("canvas-maximized");
      $("btnCanvasFullscreen").classList.toggle("on", isMax);
      toast(isMax ? "Canvas maximized (100% width)" : "Split view restored");
    };
  }
  if ($("btnCanvasPreview")) $("btnCanvasPreview").onclick = () => setCanvasTab("preview");
  if ($("btnCanvasCode")) $("btnCanvasCode").onclick = () => setCanvasTab("code");
  if ($("btnCanvasRun")) $("btnCanvasRun").onclick = () => runCanvasSandbox();
  if ($("btnCanvasCopy")) $("btnCanvasCopy").onclick = () => {
    const code = $("canvasCodeArea") ? $("canvasCodeArea").value : canvasState.code;
    navigator.clipboard.writeText(code).then(() => toast("Artifact code copied!"));
  };
  if ($("btnCanvasSyntax")) {
    $("btnCanvasSyntax").onclick = () => {
      canvasSyntaxEnabled = !canvasSyntaxEnabled;
      $("btnCanvasSyntax").classList.toggle("on", canvasSyntaxEnabled);
      updateCanvasEditorHighlight();
      toast(canvasSyntaxEnabled ? "Syntax Highlighting: ON" : "Plain Text Mode");
    };
  }
  if ($("btnCanvasDownload")) $("btnCanvasDownload").onclick = () => downloadCanvasFile();
  if ($("btnCanvasExportZip")) $("btnCanvasExportZip").onclick = () => exportCanvasZip();

  // Viewport switchers
  if ($("btnViewportDesktop")) $("btnViewportDesktop").onclick = () => setCanvasViewport("desktop");
  if ($("btnViewportTablet")) $("btnViewportTablet").onclick = () => setCanvasViewport("tablet");
  if ($("btnViewportMobile")) $("btnViewportMobile").onclick = () => setCanvasViewport("mobile");

  // Snapshots / Checkpoints
  if ($("btnCanvasSaveVersion")) $("btnCanvasSaveVersion").onclick = () => saveCanvasSnapshot();
  if ($("btnCanvasHistory")) $("btnCanvasHistory").onclick = () => toggleCanvasSnapshots();
  if ($("btnCloseSnapshotDrawer")) $("btnCloseSnapshotDrawer").onclick = () => toggleCanvasSnapshots(false);
  if ($("btnCreateSnapshotManual")) $("btnCreateSnapshotManual").onclick = () => saveCanvasSnapshot();

  // Console Drawer
  if ($("btnCanvasConsole")) $("btnCanvasConsole").onclick = () => toggleCanvasConsole();
  if ($("btnCloseConsoleDrawer")) $("btnCloseConsoleDrawer").onclick = () => toggleCanvasConsole(false);
  if ($("btnCanvasClearConsole")) $("btnCanvasClearConsole").onclick = () => clearCanvasConsole();

  const codeArea = $("canvasCodeArea");
  if (codeArea) {
    codeArea.addEventListener("input", () => {
      canvasState.code = codeArea.value;
      updateCanvasLineNumbers();
      updateCanvasEditorHighlight();
    });
    codeArea.addEventListener("scroll", () => {
      if ($("canvasLineNumbers")) {
        $("canvasLineNumbers").scrollTop = codeArea.scrollTop;
      }
      if ($("canvasCodeHighlight")) {
        $("canvasCodeHighlight").scrollTop = codeArea.scrollTop;
        $("canvasCodeHighlight").scrollLeft = codeArea.scrollLeft;
      }
    });
    codeArea.addEventListener("keydown", e => {
      if (e.key === "Tab") {
        e.preventDefault();
        const start = codeArea.selectionStart;
        const end = codeArea.selectionEnd;
        codeArea.value = codeArea.value.substring(0, start) + "  " + codeArea.value.substring(end);
        codeArea.selectionStart = codeArea.selectionEnd = start + 2;
        canvasState.code = codeArea.value;
        updateCanvasLineNumbers();
        updateCanvasEditorHighlight();
      }
    });
  }

  if ($("canvasTitle")) {
    $("canvasTitle").addEventListener("input", () => {
      canvasState.title = $("canvasTitle").value;
    });
  }
}

/* ---------- Cyber-Glass Thinking Level & Telemetry ---------- */
const DEFAULT_THINKING_LEVELS = { nsfw: "off", friend: "balanced", architect: "deep", muse: "balanced" };
let cachedModelsData = null;
let modelCapsMap = {};

function getThinkingLevel(m = mode) {
  const saved = localStorage.getItem(`lamplight_thinking_level_${m}`);
  if (saved && ["off", "low", "balanced", "deep"].includes(saved)) return saved;
  return DEFAULT_THINKING_LEVELS[m] || "balanced";
}

function setThinkingLevel(lvl, m = mode) {
  if (!["off", "low", "balanced", "deep"].includes(lvl)) return;
  localStorage.setItem(`lamplight_thinking_level_${m}`, lvl);
  api("/api/thinking-level", { method: "POST", body: JSON.stringify({ mode: m, level: lvl }) }).catch(() => {});
  updateThinkingLevelUI(m);
}

function getEffectiveModelForMode(m = mode) {
  const custom = $("msgModel")?.value;
  if (custom) return custom;
  if (cachedModelsData?.per_mode?.[m]) {
    return cachedModelsData.per_mode[m];
  }
  return (cachedModelsData?.models?.[0]?.name) || "Qwen3.5-uncensored:latest";
}

function getModelCaps(modelName) {
  if (!modelName) {
    return {
      supports_thinking: true,
      supported_levels: ["off", "low", "balanced", "deep"],
      summary: "Reasoning ready"
    };
  }
  const clean = modelName.split("::")[0].trim();
  if (modelCapsMap[clean]) return modelCapsMap[clean];
  if (modelCapsMap[modelName]) return modelCapsMap[modelName];

  // Heuristic while models data is being fetched
  const lower = clean.toLowerCase();
  const isReasoning = lower.includes("qwen3") || lower.includes("deepseek") || lower.includes("r1") || lower.includes("qwq") || lower.includes("o1") || lower.includes("o3") || lower.includes("jj");
  return {
    supports_thinking: isReasoning,
    supported_levels: isReasoning ? ["off", "low", "balanced", "deep"] : ["off"],
    summary: isReasoning ? "Reasoning model" : "Direct output only"
  };
}

function updateThinkingLevelUI(m = mode) {
  const effectiveModel = getEffectiveModelForMode(m);
  const caps = getModelCaps(effectiveModel);
  const currentSavedLevel = getThinkingLevel(m);

  const container = $("thinkingLevelGroup");
  const wrapper = $("thinkSelectorWrapper") || container?.closest(".think-level-selector");
  const badge = $("thinkSupportBadge");
  const lcdThinking = $("lcdThinking");
  const lcdModel = $("lcdModel");

  if (lcdModel) {
    lcdModel.textContent = effectiveModel.toUpperCase();
  }

  const buttons = document.querySelectorAll("#thinkingLevelGroup .think-toggle-btn");

  if (!caps.supports_thinking) {
    if (wrapper) wrapper.classList.add("unsupported");
    if (container) container.classList.add("unsupported");
    if (badge) {
      badge.textContent = "DIRECT ONLY";
      badge.className = "think-support-badge unsupported";
      badge.title = `Model "${effectiveModel}" does not support internal reasoning scratchpads. Output will be generated directly.`;
    }

    buttons.forEach(btn => {
      const lvl = btn.dataset.level;
      if (lvl === "off") {
        btn.disabled = false;
        btn.classList.add("active");
        btn.title = `Direct output active (${effectiveModel} does not support internal reasoning tokens)`;
      } else {
        btn.disabled = true;
        btn.classList.remove("active");
        btn.title = `Thinking level "${lvl}" is unsupported by model "${effectiveModel}"`;
      }
    });

    if (lcdThinking) {
      lcdThinking.textContent = "OFF (DIRECT)";
      lcdThinking.classList.add("dimmed");
      lcdThinking.title = `Model "${effectiveModel}" does not support reasoning scratchpads.`;
    }
  } else {
    if (wrapper) wrapper.classList.remove("unsupported");
    if (container) container.classList.remove("unsupported");

    const supportedLevels = caps.supported_levels || ["off", "low", "balanced", "deep"];
    if (badge) {
      badge.textContent = `REASONING (${supportedLevels.length} LVL)`;
      badge.className = "think-support-badge supported";
      badge.title = `Model "${effectiveModel}" supports reasoning scratchpad control (${supportedLevels.join(", ")}).`;
    }

    let activeLevel = currentSavedLevel;
    if (!supportedLevels.includes(activeLevel)) {
      activeLevel = supportedLevels.includes("balanced") ? "balanced" : supportedLevels[0];
    }

    buttons.forEach(btn => {
      const lvl = btn.dataset.level;
      const isSupported = supportedLevels.includes(lvl);
      btn.disabled = !isSupported;
      btn.classList.toggle("active", lvl === activeLevel);
      if (isSupported) {
        btn.title = lvl === "off" ? "Direct output without thinking tokens (Lowest latency)"
                  : lvl === "low" ? "Concise scratchpad reasoning (Under 120 words)"
                  : lvl === "balanced" ? "Standard balanced reasoning"
                  : "Deep multi-step reasoning, questioning assumptions & verifying logic";
      } else {
        btn.title = `Thinking level "${lvl}" is unsupported by ${effectiveModel}`;
      }
    });

    if (lcdThinking) {
      lcdThinking.textContent = activeLevel.toUpperCase();
      lcdThinking.classList.remove("dimmed");
      lcdThinking.title = `Reasoning depth: ${activeLevel.toUpperCase()} for model "${effectiveModel}".`;
    }
  }
}

function wireThinkingLevels() {
  document.querySelectorAll("#thinkingLevelGroup .think-toggle-btn").forEach(btn => {
    btn.onclick = () => {
      if (btn.disabled) return;
      const effectiveModel = getEffectiveModelForMode(mode);
      const caps = getModelCaps(effectiveModel);
      if (!caps.supports_thinking && btn.dataset.level !== "off") {
        toast(`Model ${effectiveModel} does not support internal reasoning scratchpads.`, true);
        return;
      }
      setThinkingLevel(btn.dataset.level, mode);
    };
  });
  updateThinkingLevelUI(mode);
  refreshTelemetryVram();
}

async function refreshTelemetryVram() {
  try {
    const j = await api("/api/vram");
    if (j) {
      const vramGb = (j.total_vram_gb !== undefined ? j.total_vram_gb : (j.summary && j.summary.total_vram_gb)) || 0;
      const gpuTotal = j.gpu_total_gb || 5.8;
      const vramStr = `${vramGb}G / ${gpuTotal}G`;
      if ($("lcdVram")) $("lcdVram").textContent = vramStr;
      if ($("hudVramVal")) $("hudVramVal").textContent = `${vramGb}GB VRAM`;
    }
  } catch (e) {}
}

/* ---------- tabs + glow ---------- */
function moveGlow() {
  const on = document.querySelector("#tabs button.on");
  const g = $("tabGlow");
  if (!on) return;
  g.style.left = on.offsetLeft + "px"; g.style.width = on.offsetWidth + "px";
}
window.addEventListener("resize", moveGlow);
function wireTabs() {
  document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => {
    if (b.classList.contains("on")) return;
    stopCurrentTTS();
    document.querySelectorAll("#tabs button").forEach(x => x.classList.remove("on"));
    b.classList.add("on"); moveGlow(); mode = b.dataset.mode;
    document.body.dataset.mode = mode;
    $("msg").placeholder = mode === "muse" ? "Ask Muse: brainstorm plots, interrogate scenes, flesh out characters, craft dialogue…" : (mode === "architect" ? "Ask Architect: code, architecture, refactoring, systems…" : (mode === "friend" ? "Message friend…" : "Message After Dark… (local only)"));
    $("btnMemory").classList.toggle("hidden", mode !== "friend");
    if ($("btnArchitectStack")) $("btnArchitectStack").classList.toggle("hidden", mode !== "architect");
    if ($("btnMuseBible")) $("btnMuseBible").classList.toggle("hidden", mode !== "muse");
    $("sessionDrawerHeading").textContent = mode === "muse" ? "Muse Stories & Brainstorms" : (mode === "architect" ? "Architect Chats" : (mode === "nsfw" ? "After Dark Chats" : "Friend Chats"));
    if ($("msgModel") && cachedModelsData && cachedModelsData.per_mode) {
      const defOpt = $("msgModel").options[0];
      if (defOpt && defOpt.value === "") {
        defOpt.textContent = `Default (${cachedModelsData.per_mode[mode] || "Auto"})`;
      }
    }
    updateThinkingLevelUI(mode);
    renderCartridgeTrigger();
    refreshTelemetryVram();
    closeDrawers();
    loadHistory();
    loadSessions(mode);
  });
  setTimeout(moveGlow, 50);
}

/* ---------- drawers ---------- */
function openDrawer(id) {
  $("scrim").classList.remove("hidden");
  $(id).classList.remove("hidden");
  requestAnimationFrame(() => {
    $("scrim").classList.add("open");
    $(id).classList.add("open");
  });
}
function closeDrawers() {
  $("scrim").classList.remove("open");
  ["modelsPanel", "friendPanel", "architectPanel", "musePanel", "sessionsPanel", "themesPanel", "skillsPanel", "docHubPanel", "observatoryPanel", "promptsPanel"].forEach(i => {
    const el = $(i);
    if (el) el.classList.remove("open");
  });
  if (typeof stopObservatoryPolling === "function") stopObservatoryPolling();
  setTimeout(() => {
    if (!document.querySelector(".drawer.open")) {
      $("scrim").classList.add("hidden");
      ["modelsPanel", "friendPanel", "architectPanel", "musePanel", "sessionsPanel", "themesPanel", "skillsPanel", "docHubPanel", "observatoryPanel", "promptsPanel"].forEach(i => {
        const el = $(i);
        if (el && (i !== "sessionsPanel" || window.innerWidth < 900)) el.classList.add("hidden");
      });
    }
  }, 400);
}
$("scrim").onclick = closeDrawers;
document.querySelectorAll(".closeBtn").forEach(b => b.onclick = closeDrawers);
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawers(); });
$("btnMemory").onclick = () => { openDrawer("friendPanel"); loadProfile(); loadPromptInto("friend", "friendPrompt"); wireTemp("friend", "friendTemp", "friendTempVal"); wireTools("friend", "friendTools"); };
$("btnSkills").onclick = () => { openDrawer("skillsPanel"); loadSkills().then(() => renderSkillsList()); };

/* ---------- 25 curated themes system ---------- */
const THEMES = [
  { id: "lamplight", name: "Lamplight Amber", category: "classic", bg: "#07090e", panel: "#10141f", accent: "#f0a832", text: "#f0ecdf", desc: "Signature golden obsidian glass" },
  { id: "cyberpunk", name: "Cyberpunk Neon", category: "neon", bg: "#080912", panel: "#0f1124", accent: "#00f0ff", text: "#eefbff", desc: "Electric cyan & hot magenta synth" },
  { id: "catppuccin", name: "Catppuccin Mocha", category: "nature", bg: "#11111b", panel: "#181825", accent: "#cba6f7", text: "#cdd6f4", desc: "Soothing lavender & mauve slate" },
  { id: "dracula", name: "Dracula Castle", category: "classic", bg: "#181923", panel: "#21222d", accent: "#bd93f9", text: "#f8f8f2", desc: "Gothic vampire purple & pink" },
  { id: "tokyo-night", name: "Tokyo Night", category: "neon", bg: "#16161e", panel: "#1a1b26", accent: "#7aa2f7", text: "#c0caf5", desc: "Twilight neon blue & indigo" },
  { id: "nordic-frost", name: "Nordic Frost", category: "classic", bg: "#1a1d24", panel: "#222630", accent: "#88c0d0", text: "#eceff4", desc: "Chilled arctic cyan & glacial frost" },
  { id: "rose-pine", name: "Rose Pine", category: "nature", bg: "#191724", panel: "#1f1d2e", accent: "#ebbcba", text: "#e0def4", desc: "Moody blush rose, gold & pine" },
  { id: "matrix", name: "Matrix Terminal", category: "neon", bg: "#050a06", panel: "#09140b", accent: "#00ff66", text: "#d1ffd9", desc: "Phosphor cyber emerald on deep black" },
  { id: "oled-void", name: "OLED Void", category: "minimal", bg: "#000000", panel: "#0a0a0a", accent: "#e5e5e5", text: "#f5f5f5", desc: "True #000000 pitch black & titanium" },
  { id: "monokai", name: "Monokai Pro", category: "classic", bg: "#1d1e1f", panel: "#252627", accent: "#ffd866", text: "#fcfcfa", desc: "Tangerine & gold on charcoal" },
  { id: "gruvbox", name: "Gruvbox Dark", category: "nature", bg: "#1d2021", panel: "#282828", accent: "#fe8019", text: "#ebdbb2", desc: "Retro earthy terracotta & orange" },
  { id: "deep-abyss", name: "Deep Abyss", category: "neon", bg: "#040d1a", panel: "#091629", accent: "#38bdf8", text: "#e0f2fe", desc: "Bioluminescent ocean cyan & navy" },
  { id: "emerald-synth", name: "Emerald Synth", category: "nature", bg: "#060f0d", panel: "#0c1d18", accent: "#34d399", text: "#ecfdf5", desc: "High-tech jade & emerald glass" },
  { id: "amethyst-haze", name: "Amethyst Haze", category: "neon", bg: "#0d0718", panel: "#160d26", accent: "#a855f7", text: "#faf5ff", desc: "Royal crystal violet & orchid" },
  { id: "sunset-boulevard", name: "Sunset Boulevard", category: "nature", bg: "#14090c", panel: "#221016", accent: "#fb7185", text: "#fff1f2", desc: "Hot coral, vibrant peach & crimson" },
  { id: "solarized", name: "Solarized Dark", category: "classic", bg: "#00212b", panel: "#072b35", accent: "#2aa198", text: "#eee8d5", desc: "Precision cyan & teal on deep sea" },
  { id: "crimson-moon", name: "Crimson Moonlight", category: "classic", bg: "#0e0608", panel: "#180b0e", accent: "#f43f5e", text: "#ffe4e6", desc: "Blood ruby & velvet scarlet" },
  { id: "vaporwave", name: "Vaporwave 84", category: "neon", bg: "#120924", panel: "#1c1038", accent: "#f472b6", text: "#fdf2f8", desc: "Pastel hot pink & retro VHS purple" },
  { id: "coffee-roast", name: "Coffee Roast", category: "nature", bg: "#130e0a", panel: "#1f1610", accent: "#d97706", text: "#fef3c7", desc: "Roasted mocha & espresso crema" },
  { id: "ghost-shell", name: "Ghost Shell", category: "neon", bg: "#0d100d", panel: "#151b15", accent: "#a3e635", text: "#f7fee7", desc: "Tactical cyber lime & industrial slate" },
  { id: "arcane-gold", name: "Arcane Gold", category: "classic", bg: "#0a0c14", panel: "#121624", accent: "#eab308", text: "#fefce8", desc: "Royal champagne gold & midnight navy" },
  { id: "glacier-blue", name: "Glacier Blue", category: "neon", bg: "#050c14", panel: "#0b1726", accent: "#0ea5e9", text: "#f0f9ff", desc: "Crystalline ice & frozen diamond" },
  { id: "sakura-night", name: "Sakura Night", category: "nature", bg: "#120a12", panel: "#1c111c", accent: "#f472b6", text: "#fdf2f8", desc: "Cherry blossom petals & plum mist" },
  { id: "dark-titanium", name: "Dark Titanium", category: "minimal", bg: "#0c0e12", panel: "#14171d", accent: "#94a3b8", text: "#f1f5f9", desc: "Aerospace space-grey & stealth carbon" },
  { id: "challenger-deep", name: "Challenger Deep", category: "minimal", bg: "#02070d", panel: "#05111e", accent: "#10b981", text: "#ecfdf5", desc: "Abyssal depth & bioluminescent glow" }
];

let activeThemeFilter = "all";

function applyTheme(themeId) {
  document.body.dataset.theme = themeId;
  localStorage.setItem("lamplight_theme", themeId);
  document.querySelectorAll(".theme-card").forEach(c => {
    c.classList.toggle("active", c.dataset.themeId === themeId);
  });
}

function renderThemeGrid() {
  const grid = $("themeGrid");
  if (!grid) return;
  const current = localStorage.getItem("lamplight_theme") || "lamplight";
  const list = activeThemeFilter === "all" ? THEMES : THEMES.filter(t => t.category === activeThemeFilter);
  grid.innerHTML = list.map(t => `
    <div class="theme-card ${t.id === current ? 'active' : ''}" data-theme-id="${t.id}" title="${t.desc}">
      <span class="theme-check">✓</span>
      <div class="theme-card-swatches">
        <span class="theme-swatch" style="background:${t.bg}"></span>
        <span class="theme-swatch" style="background:${t.panel}"></span>
        <span class="theme-swatch accent" style="background:${t.accent};color:${t.accent}"></span>
        <span class="theme-swatch" style="background:${t.text}"></span>
      </div>
      <div class="theme-card-info">
        <span class="theme-card-title">${t.name}</span>
        <span class="theme-card-cat">${t.category}</span>
      </div>
    </div>
  `).join("");

  grid.querySelectorAll(".theme-card").forEach(card => {
    card.onclick = () => {
      applyTheme(card.dataset.themeId);
    };
  });
}

function wireThemes() {
  const saved = localStorage.getItem("lamplight_theme") || "lamplight";
  applyTheme(saved);
  $("btnThemes").onclick = () => {
    openDrawer("themesPanel");
    renderThemeGrid();
  };
  document.querySelectorAll(".theme-filter-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(".theme-filter-btn").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      activeThemeFilter = b.dataset.filter;
      renderThemeGrid();
    };
  });
}

/* ---------- sessions sidebar ---------- */
$("btnSessions").onclick = () => {
  $("sessionDrawerHeading").textContent = mode === "muse" ? "MUSE // ARCHIVE" : (mode === "architect" ? "ARCHITECT // ARCHIVE" : (mode === "nsfw" ? "AFTERDARK // ARCHIVE" : "FRIEND // ARCHIVE"));
  loadSessions(mode);
  if (window.innerWidth >= 900) {
    document.body.classList.toggle("sessions-collapsed");
  } else {
    openDrawer("sessionsPanel");
  }
};

$("btnNewSession").onclick = async () => {
  try {
    const s = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ mode })
    });
    activeSessions[mode] = s.id;
    localStorage.setItem(`lamplight_session_${mode}`, s.id);
    $("chat").innerHTML = "";
    updateHero(0);
    toast("New chat started.");
    await loadSessions(mode);
    if (window.innerWidth < 768) closeDrawers();
    $("msg").focus();
  } catch (e) {
    toast(e.message, true);
  }
};

let allCachedSessions = [];
let sessionSearchWired = false;

function renderSessionItems(sessions, container, modeKey, currentActive) {
  container.innerHTML = "";
  if (!sessions || sessions.length === 0) {
    container.innerHTML = `<div class="session-empty" style="padding: 24px 16px; text-align: center; color: var(--muted); font-size: 13px;">No conversations found.</div>`;
    return;
  }
  sessions.forEach(s => {
    const item = document.createElement("div");
    item.className = "session-item" + (s.id === currentActive ? " active" : "");
    item.dataset.id = s.id;
    const countText = s.msg_count === 1 ? "1 message" : `${s.msg_count} messages`;
    const relTime = timeAgo(s.updated_at || s.created_at);
    item.innerHTML = `
      <div class="session-main">
        <div class="session-title" title="${esc(s.title)}">${esc(s.title)}</div>
        <div class="session-meta">
          <span>${countText}</span>
          ${relTime ? `<span>•</span><span>${relTime}</span>` : ""}
        </div>
      </div>
      <div class="session-actions">
        <button class="session-btn-icon btn-rename" title="Rename conversation">✎</button>
        <button class="session-btn-icon btn-delete" title="Delete conversation">🗑</button>
      </div>
    `;
    item.querySelector(".session-main").onclick = () => {
      if (activeSessions[modeKey] === s.id) {
        if (window.innerWidth < 768) closeDrawers();
        return;
      }
      activeSessions[modeKey] = s.id;
      localStorage.setItem(`lamplight_session_${modeKey}`, s.id);
      loadHistory(s.id);
      loadSessions(modeKey);
      if (window.innerWidth < 768) closeDrawers();
    };
    item.querySelector(".btn-rename").onclick = async (ev) => {
      ev.stopPropagation();
      const newTitle = prompt("Rename conversation:", s.title);
      if (!newTitle || newTitle.trim() === "" || newTitle.trim() === s.title) return;
      try {
        await api(`/api/sessions/${encodeURIComponent(s.id)}`, {
          method: "PATCH",
          body: JSON.stringify({ title: newTitle.trim() })
        });
        toast("Conversation renamed.");
        loadSessions(modeKey);
      } catch (err) { toast(err.message, true); }
    };
    item.querySelector(".btn-delete").onclick = async (ev) => {
      ev.stopPropagation();
      const ok = await askConfirm(`Delete "${s.title}" and its messages?`);
      if (!ok) return;
      try {
        await api(`/api/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
        toast("Conversation deleted.");
        if (activeSessions[modeKey] === s.id) {
          activeSessions[modeKey] = null;
          localStorage.removeItem(`lamplight_session_${modeKey}`);
          loadHistory();
        } else {
          loadSessions(modeKey);
        }
      } catch (err) { toast(err.message, true); }
    };
    container.appendChild(item);
  });
}

async function loadSessions(modeKey = mode) {
  const container = $("sessionList");
  if (!container) return;
  const j = await api(`/api/sessions?mode=${modeKey}`).catch(() => null);
  if (!j) return;
  const currentActive = activeSessions[modeKey];
  allCachedSessions = j.sessions || [];

  const searchInput = $("sessionSearchInput");
  if (searchInput && !sessionSearchWired) {
    sessionSearchWired = true;
    searchInput.oninput = () => {
      const q = (searchInput.value || "").toLowerCase().trim();
      const filtered = q
        ? allCachedSessions.filter(s => (s.title || "").toLowerCase().includes(q))
        : allCachedSessions;
      renderSessionItems(filtered, container, modeKey, activeSessions[modeKey]);
    };
  }

  const curQ = (searchInput?.value || "").toLowerCase().trim();
  const listToRender = curQ
    ? allCachedSessions.filter(s => (s.title || "").toLowerCase().includes(curQ))
    : allCachedSessions;
  renderSessionItems(listToRender, container, modeKey, currentActive);
}

/* ---------- chat render ---------- */
function nearBottom() { const c = $("chat"); return c.scrollHeight - c.scrollTop - c.clientHeight < 120; }
function scrollEnd(smooth = true) { $("chat").scrollTo({ top: 1e9, behavior: smooth ? "smooth" : "auto" }); }
$("chat").addEventListener("scroll", () => {
  const c = $("chat");
  $("btnDown").classList.toggle("hidden", c.scrollHeight - c.scrollTop - c.clientHeight < 200);
});
$("btnDown").onclick = () => scrollEnd();
function updateHero(count) {
  const h = $("hero");
  if (h) h.classList.add("hidden");
}
let currentTtsAudio = null;
let activeTtsBtn = null;
let ttsSettings = {
  voice_friend: "af_heart",
  voice_nsfw: "af_bella",
  voice_architect: "af_sky",
  voice_muse: "af_nicole",
  auto_speak: false,
  speed: 1.0
};
let ttsWired = false;

function stopCurrentTTS() {
  if (currentTtsAudio) {
    try {
      currentTtsAudio.pause();
      currentTtsAudio.currentTime = 0;
    } catch (_) {}
    currentTtsAudio = null;
  }
  if (activeTtsBtn) {
    activeTtsBtn.classList.remove("playing", "loading");
    const icon = activeTtsBtn.querySelector(".tts-icon");
    const label = activeTtsBtn.querySelector(".tts-label");
    const waves = activeTtsBtn.querySelector(".tts-waves");
    if (icon) icon.textContent = "🔊";
    if (label) label.textContent = "Listen";
    if (waves) waves.classList.add("hidden");
    activeTtsBtn = null;
  }
}

async function playBubbleTTS(msgEl, btn) {
  if (activeTtsBtn === btn && currentTtsAudio && !currentTtsAudio.paused) {
    stopCurrentTTS();
    return;
  }
  stopCurrentTTS();

  const rawText = msgEl ? (msgEl.dataset.raw || msgEl.innerText) : "";
  if (!rawText || !rawText.trim()) return;

  activeTtsBtn = btn;
  btn.classList.add("playing", "loading");
  const icon = btn.querySelector(".tts-icon");
  const label = btn.querySelector(".tts-label");
  const waves = btn.querySelector(".tts-waves");
  if (icon) icon.textContent = "⏳";
  if (label) label.textContent = "Synthesizing…";

  try {
    const selectedVoice = mode === "muse" ? (ttsSettings.voice_muse || "af_nicole") : (mode === "architect" ? (ttsSettings.voice_architect || "af_sky") : (mode === "nsfw" ? ttsSettings.voice_nsfw : ttsSettings.voice_friend));
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Passcode": passcode
      },
      body: JSON.stringify({
        text: rawText,
        mode: mode,
        voice: selectedVoice,
        speed: ttsSettings.speed || 1.0
      })
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "TTS generation failed");
    }

    const blob = await res.blob();
    const audioUrl = URL.createObjectURL(blob);
    const audio = new Audio(audioUrl);
    currentTtsAudio = audio;

    btn.classList.remove("loading");
    if (icon) icon.textContent = "⏹";
    if (label) label.textContent = "Stop";
    if (waves) waves.classList.remove("hidden");

    audio.onended = () => {
      URL.revokeObjectURL(audioUrl);
      stopCurrentTTS();
    };
    audio.onerror = () => {
      URL.revokeObjectURL(audioUrl);
      stopCurrentTTS();
      toast("Audio playback error", true);
    };

    await audio.play();
  } catch (err) {
    stopCurrentTTS();
    toast(err.message || "Failed to play voice", true);
  }
}

async function previewVoice(voiceId, sampleText, btn) {
  stopCurrentTTS();
  const origText = btn.textContent;
  btn.textContent = "⏳ …";
  btn.disabled = true;

  try {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Passcode": passcode
      },
      body: JSON.stringify({
        text: sampleText,
        mode: mode,
        voice: voiceId,
        speed: ttsSettings.speed || 1.0
      })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "TTS failed");
    }
    const blob = await res.blob();
    const audioUrl = URL.createObjectURL(blob);
    const audio = new Audio(audioUrl);
    currentTtsAudio = audio;
    btn.textContent = "⏹ Stop";
    btn.disabled = false;

    const cleanup = () => {
      URL.revokeObjectURL(audioUrl);
      btn.textContent = origText;
      currentTtsAudio = null;
      btn.onclick = () => previewVoice(voiceId, sampleText, btn);
    };

    audio.onended = cleanup;
    audio.onerror = () => {
      cleanup();
      toast("Audio playback error", true);
    };
    btn.onclick = () => {
      if (currentTtsAudio) {
        currentTtsAudio.pause();
        currentTtsAudio = null;
      }
      cleanup();
    };
    await audio.play();
  } catch (err) {
    btn.textContent = origText;
    btn.disabled = false;
    toast(err.message || "Voice preview failed", true);
  }
}

async function loadTtsSettings() {
  try {
    const [voicesData, settingsData] = await Promise.all([
      api("/api/tts/voices"),
      api("/api/tts/settings")
    ]);

    if (settingsData) {
      ttsSettings = { ...ttsSettings, ...settingsData };
      if ($("autoSpeakCheck")) $("autoSpeakCheck").checked = !!ttsSettings.auto_speak;
      if ($("btnAutoSpeak")) {
        $("btnAutoSpeak").classList.toggle("active", !!ttsSettings.auto_speak);
        $("btnAutoSpeak").title = ttsSettings.auto_speak ? "Auto-speak bot replies (On)" : "Auto-speak bot replies (Off)";
      }
      if ($("voiceSpeed")) {
        $("voiceSpeed").value = ttsSettings.speed;
        $("voiceSpeedVal").textContent = ttsSettings.speed.toFixed(2) + "x";
      }
    }

    if (voicesData && voicesData.voices) {
      const friendSel = $("voiceFriendSelect");
      const nsfwSel = $("voiceNsfwSelect");
      const archSel = $("voiceArchitectSelect");
      const museSel = $("voiceMuseSelect");
      const renderOpts = (cur) => voicesData.voices.map(v => 
        `<option value="${esc(v.id)}" ${v.id === cur ? "selected" : ""}>${esc(v.name)} (${esc(v.accent)})</option>`
      ).join("");

      if (friendSel) friendSel.innerHTML = renderOpts(ttsSettings.voice_friend || "af_heart");
      if (nsfwSel) nsfwSel.innerHTML = renderOpts(ttsSettings.voice_nsfw || "af_bella");
      if (archSel) archSel.innerHTML = renderOpts(ttsSettings.voice_architect || "af_sky");
      if (museSel) museSel.innerHTML = renderOpts(ttsSettings.voice_muse || "af_nicole");
    }
  } catch (err) {
    console.warn("TTS init:", err);
  }
}

async function saveTtsSettings(patch) {
  ttsSettings = { ...ttsSettings, ...patch };
  if (patch.auto_speak !== undefined) {
    if ($("autoSpeakCheck")) $("autoSpeakCheck").checked = patch.auto_speak;
    if ($("btnAutoSpeak")) {
      $("btnAutoSpeak").classList.toggle("active", patch.auto_speak);
      $("btnAutoSpeak").title = patch.auto_speak ? "Auto-speak bot replies (On)" : "Auto-speak bot replies (Off)";
    }
  }
  await api("/api/tts/settings", {
    method: "POST",
    body: JSON.stringify(patch)
  }).catch(e => toast(e.message, true));
}

function wireTts() {
  if (ttsWired) return;
  ttsWired = true;

  const btnAuto = $("btnAutoSpeak");
  if (btnAuto) {
    btnAuto.onclick = () => {
      saveTtsSettings({ auto_speak: !ttsSettings.auto_speak });
    };
  }

  const autoChk = $("autoSpeakCheck");
  if (autoChk) {
    autoChk.onchange = () => {
      saveTtsSettings({ auto_speak: autoChk.checked });
    };
  }

  const speedSlider = $("voiceSpeed");
  const speedVal = $("voiceSpeedVal");
  if (speedSlider) {
    speedSlider.oninput = () => {
      const spd = parseFloat(speedSlider.value);
      if (speedVal) speedVal.textContent = spd.toFixed(2) + "x";
    };
    speedSlider.onchange = () => {
      const spd = parseFloat(speedSlider.value);
      saveTtsSettings({ speed: spd });
    };
  }

  const friendSel = $("voiceFriendSelect");
  if (friendSel) {
    friendSel.onchange = () => {
      saveTtsSettings({ voice_friend: friendSel.value });
    };
  }

  const nsfwSel = $("voiceNsfwSelect");
  if (nsfwSel) {
    nsfwSel.onchange = () => {
      saveTtsSettings({ voice_nsfw: nsfwSel.value });
    };
  }

  const archSel = $("voiceArchitectSelect");
  if (archSel) {
    archSel.onchange = () => {
      saveTtsSettings({ voice_architect: archSel.value });
    };
  }

  const museSel = $("voiceMuseSelect");
  if (museSel) {
    museSel.onchange = () => {
      saveTtsSettings({ voice_muse: museSel.value });
    };
  }

  const btnPrevFriend = $("btnPreviewFriendVoice");
  if (btnPrevFriend) {
    btnPrevFriend.onclick = () => {
      const v = friendSel ? friendSel.value : ttsSettings.voice_friend;
      previewVoice(v, "Hello! I am ready to converse, write, and explore ideas with you.", btnPrevFriend);
    };
  }

  const btnPrevNsfw = $("btnPreviewNsfwVoice");
  if (btnPrevNsfw) {
    btnPrevNsfw.onclick = () => {
      const v = nsfwSel ? nsfwSel.value : ttsSettings.voice_nsfw;
      previewVoice(v, "Hello. Dim the lights and tell me what you're thinking tonight.", btnPrevNsfw);
    };
  }

  const btnPrevArch = $("btnPreviewArchitectVoice");
  if (btnPrevArch) {
    btnPrevArch.onclick = () => {
      const v = archSel ? archSel.value : (ttsSettings.voice_architect || "af_sky");
      previewVoice(v, "Architect system online. Ready to build robust, scalable architectures.", btnPrevArch);
    };
  }

  const btnPrevMuse = $("btnPreviewMuseVoice");
  if (btnPrevMuse) {
    btnPrevMuse.onclick = () => {
      const v = museSel ? museSel.value : (ttsSettings.voice_muse || "af_nicole");
      previewVoice(v, "The muse awakens. What world, character, or mystery shall we bring to life today?", btnPrevMuse);
    };
  }
}

function copyBtn(el) {
  const existing = el.querySelector(".msg-actions");
  if (existing) existing.remove();

  const actions = document.createElement("div");
  actions.className = "msg-actions";

  const copyBtnEl = document.createElement("button");
  copyBtnEl.type = "button";
  copyBtnEl.className = "msg-action-btn copy-btn";
  copyBtnEl.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg> <span>Copy</span>`;
  copyBtnEl.onclick = ev => {
    ev.stopPropagation();
    navigator.clipboard.writeText(el.dataset.raw || "")
      .then(() => {
        copyBtnEl.classList.add("copied");
        copyBtnEl.querySelector("span").textContent = "Copied ✓";
        setTimeout(() => {
          copyBtnEl.classList.remove("copied");
          copyBtnEl.querySelector("span").textContent = "Copy";
        }, 1400);
      });
  };
  actions.appendChild(copyBtnEl);

  const ttsBtnEl = document.createElement("button");
  ttsBtnEl.type = "button";
  ttsBtnEl.className = "msg-action-btn tts-btn";
  ttsBtnEl.innerHTML = `<span class="tts-icon">🔊</span> <span class="tts-label">Listen</span><span class="tts-waves hidden"><span class="tts-wave-bar"></span><span class="tts-wave-bar"></span><span class="tts-wave-bar"></span><span class="tts-wave-bar"></span></span>`;
  ttsBtnEl.onclick = ev => {
    ev.stopPropagation();
    playBubbleTTS(el, ttsBtnEl);
  };
  actions.appendChild(ttsBtnEl);

  if (mode === "muse") {
    const bibleBtn = document.createElement("button");
    bibleBtn.type = "button";
    bibleBtn.className = "msg-action-btn bible-btn";
    bibleBtn.innerHTML = `<span>📖</span> <span>Field Bible</span>`;
    bibleBtn.title = "Open Story Bible & Lore Drawer";
    bibleBtn.onclick = ev => {
      ev.stopPropagation();
      openDrawer("musePanel");
      toast("Opened Field Story Bible.");
    };
    actions.appendChild(bibleBtn);
  } else if (mode === "architect") {
    const stackBtn = document.createElement("button");
    stackBtn.type = "button";
    stackBtn.className = "msg-action-btn stack-btn";
    stackBtn.innerHTML = `<span>📐</span> <span>Project Stack</span>`;
    stackBtn.title = "Open Project Stack & Rules Drawer";
    stackBtn.onclick = ev => {
      ev.stopPropagation();
      openDrawer("architectPanel");
      toast("Opened Project Stack & Rules.");
    };
    actions.appendChild(stackBtn);
  }

  el.appendChild(actions);
}
function fmtTime(ts) {
  if (!ts) { const d = new Date(); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
  const m = ts.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  return m ? `${m[4]}:${m[5]}` : "";
}
function stamp(el, ts, modelName, skillCmd = null) {
  const s = document.createElement("span");
  s.className = "ts";
  let t = fmtTime(ts);
  if (modelName) t += (t ? " · " : "") + modelName;

  if (el.classList.contains("bot")) {
    const raw = el.dataset.raw || "";
    const words = raw.trim().split(/\s+/).filter(Boolean).length;
    if (words > 15) {
      const readMins = Math.max(1, Math.round(words / 200));
      t += ` · ${words} words (~${readMins}m read)`;
    }
  }

  if (skillCmd) {
    const sk = allSkills.find(x => x.command === skillCmd);
    const icon = sk ? sk.icon : "⚡";
    const b = document.createElement("span");
    b.className = "msg-skill-badge";
    b.textContent = `${icon} /${skillCmd}`;
    s.appendChild(b);
  }
  const textNode = document.createTextNode(t);
  s.appendChild(textNode);
  el.appendChild(s);
}
function fmtSize(b) {
  if (!b) return "0 B";
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
  return (b / (1024 * 1024)).toFixed(1) + " MB";
}

function getFileIcon(ext) {
  ext = (ext || "").toLowerCase();
  if (ext === ".pdf") return "📕";
  if ([".docx", ".doc"].includes(ext)) return "📘";
  if ([".py", ".js", ".ts", ".html", ".css", ".json", ".sql", ".c", ".cpp", ".rs"].includes(ext)) return "💻";
  if ([".md", ".txt", ".csv", ".tsv"].includes(ext)) return "📝";
  if ([".png", ".jpg", ".jpeg", ".webp", ".gif"].includes(ext)) return "🖼️";
  return "📎";
}

function renderAttachmentTray() {
  const tray = $("attachmentTray");
  if (!tray) return;
  tray.innerHTML = "";
  if (!stagedAttachments.length) {
    tray.classList.add("hidden");
    return;
  }
  tray.classList.remove("hidden");
  stagedAttachments.forEach((att, idx) => {
    const pill = document.createElement("div");
    pill.className = "attachment-pill";
    if (att.is_image) {
      pill.innerHTML = `
        <img class="att-thumb" src="${esc(att.preview_url)}" alt="">
        <div class="att-info">
          <span class="att-name" title="${esc(att.filename)}">${esc(att.filename)}</span>
          <span class="att-size">${fmtSize(att.size)}</span>
        </div>
        <button class="att-del" type="button" title="Remove attachment">✕</button>
      `;
    } else {
      pill.innerHTML = `
        <span class="att-icon">${getFileIcon(att.ext)}</span>
        <div class="att-info">
          <span class="att-name" title="${esc(att.filename)}">${esc(att.filename)}</span>
          <span class="att-size">${fmtSize(att.size)}</span>
        </div>
        <button class="att-del" type="button" title="Remove attachment">✕</button>
      `;
    }
    pill.querySelector(".att-del").onclick = () => {
      stagedAttachments.splice(idx, 1);
      renderAttachmentTray();
    };
    tray.appendChild(pill);
  });
}

async function uploadFiles(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  let count = 0;
  for (const f of files) {
    if (f.size > 30 * 1024 * 1024) {
      toast(`"${f.name}" exceeds 30MB limit.`, true);
      continue;
    }
    fd.append("files", f);
    count++;
  }
  if (!count) return;
  toast(`Uploading & parsing ${count} file(s)…`);
  try {
    const r = await fetch("/api/upload", {
      method: "POST",
      headers: { "X-Passcode": passcode },
      body: fd
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "Upload failed");
    if (j.files && j.files.length) {
      stagedAttachments.push(...j.files);
      renderAttachmentTray();
      toast(`Attached ${j.files.length} file(s) ✓`);
    }
  } catch (err) {
    toast(err.message, true);
  }
}

/* Attach button & file input */
$("btnAttach").onclick = () => $("fileInput").click();
$("fileInput").onchange = () => {
  uploadFiles($("fileInput").files);
  $("fileInput").value = "";
};

/* Drag and drop overlay */
let dragCounter = 0;
window.addEventListener("dragenter", e => {
  e.preventDefault();
  dragCounter++;
  $("dropOverlay").classList.remove("hidden");
});
window.addEventListener("dragover", e => {
  e.preventDefault();
  $("dropOverlay").classList.remove("hidden");
});
window.addEventListener("dragleave", e => {
  e.preventDefault();
  dragCounter--;
  if (dragCounter <= 0) {
    dragCounter = 0;
    $("dropOverlay").classList.add("hidden");
  }
});
window.addEventListener("drop", e => {
  e.preventDefault();
  dragCounter = 0;
  $("dropOverlay").classList.add("hidden");
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
    uploadFiles(e.dataTransfer.files);
  }
});

/* Clipboard paste */
window.addEventListener("paste", e => {
  if (e.clipboardData && e.clipboardData.files && e.clipboardData.files.length) {
    e.preventDefault();
    uploadFiles(e.clipboardData.files);
  }
});

/* ==========================================================================
   MICROPHONE STUDIO & ROBUST SPEECH-TO-TEXT
   ========================================================================== */
let micAudioCtx = null, micAnalyser = null, micAnimFrame = null;
let testAudioStream = null, testAudioCtx = null, testAnalyser = null, testAnimFrame = null;

function getSupportedAudioMime() {
  if (typeof MediaRecorder === "undefined") return { mimeType: "", ext: "webm" };
  const candidates = [
    { mime: "audio/webm;codecs=opus", ext: "webm" },
    { mime: "audio/webm", ext: "webm" },
    { mime: "audio/ogg;codecs=opus", ext: "ogg" },
    { mime: "audio/mp4", ext: "mp4" },
    { mime: "audio/wav", ext: "wav" }
  ];
  for (const c of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(c.mime)) {
        return { mimeType: c.mime, ext: c.ext };
      }
    } catch (e) {}
  }
  return { mimeType: "", ext: "webm" };
}

function setupAudioVu(stream, targetBarId, targetDbId = null) {
  try {
    if (!micAudioCtx) {
      micAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (micAudioCtx.state === "suspended") {
      micAudioCtx.resume();
    }
    const source = micAudioCtx.createMediaStreamSource(stream);
    micAnalyser = micAudioCtx.createAnalyser();
    micAnalyser.fftSize = 128;
    micAnalyser.smoothingTimeConstant = 0.4;
    source.connect(micAnalyser);
    const dataArray = new Uint8Array(micAnalyser.frequencyBinCount);

    function loop() {
      if (!micAnalyser) return;
      micAnalyser.getByteFrequencyData(dataArray);
      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
      const avg = sum / dataArray.length;
      const pct = Math.min(100, Math.round((avg / 128) * 100));
      const el = $(targetBarId);
      if (el) el.style.width = Math.max(4, pct * 1.5) + "%";
      if (targetDbId && $(targetDbId)) {
        $(targetDbId).textContent = pct + "%";
      }
      micAnimFrame = requestAnimationFrame(loop);
    }
    loop();
  } catch (err) {
    console.warn("VU meter init warning:", err);
  }
}

function stopAudioVu(targetBarId = null) {
  if (micAnimFrame) {
    cancelAnimationFrame(micAnimFrame);
    micAnimFrame = null;
  }
  if (targetBarId && $(targetBarId)) {
    $(targetBarId).style.width = "0%";
  }
  micAnalyser = null;
}

async function startRecording() {
  // Check Secure Context
  const isLocal = ["localhost", "127.0.0.1", "::1"].includes(location.hostname);
  if (!window.isSecureContext && !isLocal) {
    openMicModal({
      name: "SecurityError",
      message: "Browsers block microphone access over unencrypted HTTP unless accessed via http://localhost:8123."
    });
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    openMicModal({
      name: "Unsupported",
      message: "Microphone access (navigator.mediaDevices.getUserMedia) is not supported by your browser."
    });
    return;
  }

  const selectedDeviceId = localStorage.getItem("lamplight_mic_device") || undefined;
  const constraints = {
    audio: selectedDeviceId ? { deviceId: { exact: selectedDeviceId } } : true
  };

  try {
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    audioChunks = [];
    const mimeConfig = getSupportedAudioMime();
    const recorderOptions = mimeConfig.mimeType ? { mimeType: mimeConfig.mimeType } : undefined;
    
    mediaRecorder = recorderOptions ? new MediaRecorder(stream, recorderOptions) : new MediaRecorder(stream);
    setupAudioVu(stream, "recVuBar");

    mediaRecorder.ondataavailable = e => {
      if (e.data && e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      stopAudioVu("recVuBar");
      clearInterval(recTimerInterval);
      $("recordingBar").classList.add("hidden");
      $("btnMic").classList.remove("recording");

      if (!audioChunks.length) {
        toast("No audio data recorded.");
        return;
      }
      const audioBlob = new Blob(audioChunks, { type: mimeConfig.mimeType || "audio/webm" });
      if (audioBlob.size < 400) {
        toast("Voice recording was too brief or empty.");
        return;
      }

      toast("Transcribing voice via local Whisper…");
      const fd = new FormData();
      fd.append("file", audioBlob, `speech.${mimeConfig.ext}`);
      try {
        const r = await fetch("/api/transcribe", {
          method: "POST",
          headers: { "X-Passcode": passcode },
          body: fd
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || "Transcription failed");
        if (j.text && j.text.trim()) {
          const cur = $("msg").value;
          $("msg").value = cur ? (cur.trim() + " " + j.text.trim()) : j.text.trim();
          autosize();
          $("msg").focus();
          toast("Transcribed ✓");
        } else {
          toast("No speech detected in audio.");
        }
      } catch (err) {
        toast("Speech error: " + err.message, true);
      }
    };

    // Use 250ms timeslice to ensure continuous chunk gathering
    mediaRecorder.start(250);
    recStartTime = Date.now();
    $("recordingBar").classList.remove("hidden");
    $("btnMic").classList.add("recording");
    $("recTimer").textContent = "0:00";
    recTimerInterval = setInterval(() => {
      const sec = Math.floor((Date.now() - recStartTime) / 1000);
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      $("recTimer").textContent = `${m}:${String(s).padStart(2, "0")}`;
    }, 1000);
  } catch (err) {
    console.error("Microphone access error:", err);
    $("recordingBar").classList.add("hidden");
    $("btnMic").classList.remove("recording");
    stopAudioVu("recVuBar");
    openMicModal(err);
  }
}

function stopRecording(cancel = false) {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    if (cancel) audioChunks = [];
    mediaRecorder.stop();
  }
}

/* ---------- microphone modal & diagnostics ---------- */
async function openMicModal(err = null) {
  const modal = $("micModal");
  if (!modal) return;
  modal.classList.remove("hidden");

  // Context status
  const isLocal = ["localhost", "127.0.0.1", "::1"].includes(location.hostname);
  const isSecure = window.isSecureContext || isLocal;
  const ctxEl = $("micContextStatus");
  if (ctxEl) {
    ctxEl.textContent = isSecure ? "Secure (OK)" : "Insecure (HTTP Blocked)";
    ctxEl.className = "mic-stat-val " + (isSecure ? "val-green" : "val-red");
  }

  // Permission query
  const permEl = $("micPermStatus");
  if (navigator.permissions && navigator.permissions.query) {
    try {
      const p = await navigator.permissions.query({ name: "microphone" });
      if (permEl) {
        if (p.state === "granted") {
          permEl.textContent = "Granted ✓";
          permEl.className = "mic-stat-val val-green";
        } else if (p.state === "denied") {
          permEl.textContent = "Blocked / Denied";
          permEl.className = "mic-stat-val val-red";
        } else {
          permEl.textContent = "Prompt Required";
          permEl.className = "mic-stat-val val-amber";
        }
      }
    } catch (e) {
      if (permEl) {
        permEl.textContent = err ? "Access Error" : "Ready";
        permEl.className = "mic-stat-val " + (err ? "val-red" : "val-green");
      }
    }
  }

  // Populate Device Select
  const devSelect = $("micDeviceSelect");
  if (devSelect && navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const audioInputs = devices.filter(d => d.kind === "audioinput");
      const saved = localStorage.getItem("lamplight_mic_device") || "";
      devSelect.innerHTML = `<option value="">Default System Microphone</option>`;
      audioInputs.forEach((d, idx) => {
        const label = d.label || `Microphone ${idx + 1}`;
        const opt = document.createElement("option");
        opt.value = d.deviceId;
        opt.textContent = label;
        if (d.deviceId === saved) opt.selected = true;
        devSelect.appendChild(opt);
      });
      devSelect.onchange = () => {
        localStorage.setItem("lamplight_mic_device", devSelect.value);
        toast("Microphone input set to: " + (devSelect.selectedOptions[0]?.textContent || "Default"));
      };
    } catch (e) {
      console.warn("Could not list audio devices:", e);
    }
  }

  // Error troubleshooting card
  const tbCard = $("micTroubleshootCard");
  if (err && tbCard) {
    tbCard.classList.remove("hidden");
    const titleEl = $("micErrorTitle");
    const descEl = $("micErrorDesc");
    const step1 = $("micBrowserStep1");

    if (err.name === "NotAllowedError" || err.name === "PermissionDeniedError") {
      if (titleEl) titleEl.textContent = "Microphone Permission Blocked";
      if (descEl) descEl.textContent = "Firefox or your browser denied permission to record audio for this page.";
      if (step1) step1.innerHTML = "In your browser address bar (next to <code>localhost:8123</code>), click the crossed-out microphone or permissions icon.";
    } else if (err.name === "NotFoundError" || err.name === "DevicesNotFoundError") {
      if (titleEl) titleEl.textContent = "No Microphone Hardware Found";
      if (descEl) descEl.textContent = "No audio input hardware was detected by the browser. Ensure your microphone is plugged in and recognized in system sound settings.";
    } else if (err.name === "NotReadableError") {
      if (titleEl) titleEl.textContent = "Microphone Busy or In Use";
      if (descEl) descEl.textContent = "Another application (or PipeWire/PulseAudio stream) has exclusive access to the microphone.";
    } else if (err.name === "SecurityError") {
      if (titleEl) titleEl.textContent = "Insecure Browser Origin";
      if (descEl) descEl.textContent = "Microphones are restricted to HTTPS or localhost. If you are accessing via LAN IP (e.g. 192.168.x.x), open http://localhost:8123 instead.";
    } else {
      if (titleEl) titleEl.textContent = "Microphone Access Error: " + (err.name || "Error");
      if (descEl) descEl.textContent = err.message || "An unexpected error occurred while accessing the microphone.";
    }
  } else if (tbCard) {
    tbCard.classList.add("hidden");
  }
}

function closeMicModal() {
  const modal = $("micModal");
  if (modal) modal.classList.add("hidden");
  stopTestMic();
}

function stopTestMic() {
  if (testAudioStream) {
    testAudioStream.getTracks().forEach(t => t.stop());
    testAudioStream = null;
  }
  if (testAnimFrame) {
    cancelAnimationFrame(testAnimFrame);
    testAnimFrame = null;
  }
  if ($("micTestVuBar")) $("micTestVuBar").style.width = "0%";
  if ($("micVuDb")) $("micVuDb").textContent = "0%";
  if ($("btnTestMicText")) $("btnTestMicText").textContent = "Start Live Signal Test";
  if ($("micTestStatus")) $("micTestStatus").textContent = "Speak to verify audio capture…";
}

async function toggleTestMic() {
  if (testAudioStream) {
    stopTestMic();
    return;
  }
  const selectedDeviceId = localStorage.getItem("lamplight_mic_device") || undefined;
  const constraints = {
    audio: selectedDeviceId ? { deviceId: { exact: selectedDeviceId } } : true
  };
  try {
    testAudioStream = await navigator.mediaDevices.getUserMedia(constraints);
    if (!testAudioCtx) {
      testAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (testAudioCtx.state === "suspended") testAudioCtx.resume();
    const source = testAudioCtx.createMediaStreamSource(testAudioStream);
    testAnalyser = testAudioCtx.createAnalyser();
    testAnalyser.fftSize = 128;
    source.connect(testAnalyser);
    const dataArray = new Uint8Array(testAnalyser.frequencyBinCount);

    if ($("btnTestMicText")) $("btnTestMicText").textContent = "Stop Signal Test";
    if ($("micTestStatus")) $("micTestStatus").textContent = "Signal Active: speak now!";

    function testLoop() {
      if (!testAnalyser) return;
      testAnalyser.getByteFrequencyData(dataArray);
      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
      const avg = sum / dataArray.length;
      const pct = Math.min(100, Math.round((avg / 128) * 100));
      if ($("micTestVuBar")) $("micTestVuBar").style.width = Math.max(3, pct * 1.6) + "%";
      if ($("micVuDb")) $("micVuDb").textContent = pct + "% Signal";
      if (pct > 8 && $("micTestStatus")) {
        $("micTestStatus").textContent = "Voice detected ✓ Hardware working!";
        $("micTestStatus").style.color = "#34d399";
      }
      testAnimFrame = requestAnimationFrame(testLoop);
    }
    testLoop();
  } catch (err) {
    toast("Could not start mic test: " + err.message, true);
    stopTestMic();
  }
}

// Wire up events
if ($("btnMic")) {
  $("btnMic").onclick = () => {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      stopRecording(false);
    } else {
      startRecording();
    }
  };
}
if ($("btnCancelRec")) $("btnCancelRec").onclick = () => stopRecording(true);
if ($("btnMicTroubleshoot")) $("btnMicTroubleshoot").onclick = () => openMicModal();
if ($("btnCloseMicModal")) $("btnCloseMicModal").onclick = closeMicModal;
if ($("btnDismissMicModal")) $("btnDismissMicModal").onclick = closeMicModal;
if ($("btnTestMic")) $("btnTestMic").onclick = toggleTestMic;
if ($("btnRequestMicPerm")) {
  $("btnRequestMicPerm").onclick = async () => {
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      s.getTracks().forEach(t => t.stop());
      toast("Microphone access granted ✓");
      openMicModal();
    } catch (e) {
      openMicModal(e);
    }
  };
}

function bubble(who, text, stagger = 0, time = null, modelName = null, attachments = null, skillCmd = null) {
  const d = document.createElement("div");
  d.className = `msg ${who}`;
  if (stagger) { d.classList.add("stagger"); d.style.animationDelay = Math.min(stagger * 30, 400) + "ms"; }

  if (attachments && attachments.length) {
    const wrap = document.createElement("div");
    wrap.className = "msg-attachments";
    attachments.forEach(att => {
      if (att.is_image) {
        const a = document.createElement("a");
        a.href = att.preview_url;
        a.target = "_blank";
        a.innerHTML = `<img class="msg-attachment-img" src="${esc(att.preview_url)}" alt="${esc(att.filename)}" />`;
        wrap.appendChild(a);
      } else {
        const a = document.createElement("a");
        a.className = "msg-attachment-chip";
        a.href = att.preview_url;
        a.target = "_blank";
        a.download = att.filename;
        a.innerHTML = `<span>${getFileIcon(att.ext)}</span> <span>${esc(att.filename)}</span> <small>(${fmtSize(att.size)})</small>`;
        wrap.appendChild(a);
      }
    });
    d.appendChild(wrap);
  }

  if (who === "bot") {
    d.dataset.raw = text;
    const bodyEl = document.createElement("div");
    bodyEl.innerHTML = md(text);
    d.appendChild(bodyEl);
    renderChatMermaids(d);
    stamp(d, time, modelName, skillCmd);
    copyBtn(d);
  } else {
    if (text) {
      const textEl = document.createElement("div");
      textEl.textContent = text;
      d.appendChild(textEl);
    }
    stamp(d, time, null, skillCmd);
  }
  $("chat").appendChild(d);
  return d;
}

/* per-message model picker: memory is per-bot, shared across every model */
async function loadPicker() {
  const j = await api("/api/models").catch(() => null);
  if (!j) return;
  cachedModelsData = j;
  modelCapsMap = {};
  if (j.models && Array.isArray(j.models)) {
    j.models.forEach(m => {
      modelCapsMap[m.name] = m;
      if (m.full_id) modelCapsMap[m.full_id] = m;
    });
  }
  const cur = $("msgModel") ? $("msgModel").value : "";
  const defModel = j.per_mode?.[mode] || (j.models[0] ? j.models[0].name : "Auto");
  if ($("msgModel")) {
    $("msgModel").innerHTML = `<option value="">Default (${esc(defModel)})</option>` +
      j.models.map(m => {
        const thinkBadge = m.supports_thinking ? " [Reasoning]" : " [Direct]";
        return `<option value="${esc(m.name)}">${esc(m.name)}${thinkBadge}</option>`;
      }).join("");
    if ([...$("msgModel").options].some(o => o.value === cur)) $("msgModel").value = cur;
  }
  
  populateModelPopover(j.models, j.per_mode);
  renderCartridgeTrigger();

  const activeName = getEffectiveModelForMode(mode);
  if ($("lcdModel")) $("lcdModel").textContent = activeName.toUpperCase();
  if ($("msgModel")) {
    $("msgModel").onchange = () => {
      const eff = getEffectiveModelForMode(mode);
      if ($("lcdModel")) $("lcdModel").textContent = eff.toUpperCase();
      renderCartridgeTrigger();
      updateThinkingLevelUI(mode);
    };
  }
  updateThinkingLevelUI(mode);
}

function wireModelCartridge() {
  const btn = $("cartridgeTriggerBtn");
  if (btn) {
    btn.onclick = (e) => {
      e.stopPropagation();
      toggleModelPopover();
    };
  }

  document.addEventListener("click", e => {
    const wrap = $("modelPickerWrap");
    if (wrap && !wrap.contains(e.target)) {
      closeModelPopover();
    }
  });

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      closeModelPopover();
    }
  });
}

function populateModelPopover(models = [], perMode = {}) {
  const list = $("cartridgeOptionsScroll");
  const countTag = $("cartridgeCountTag");
  if (!list) return;

  if (countTag) {
    countTag.textContent = `${models.length} MODEL${models.length === 1 ? '' : 'S'}`;
  }

  const defModel = perMode?.[mode] || (models[0] ? models[0].name : "Auto");
  const curVal = $("msgModel") ? $("msgModel").value : "";

  let html = `
    <div class="cartridge-option-card ${curVal === "" ? "active" : ""}" data-model="" role="option" tabindex="0">
      <div class="cartridge-option-left">
        <div class="cartridge-option-pip"></div>
        <div class="cartridge-option-meta">
          <div class="cartridge-option-name">Default Model</div>
          <div class="cartridge-option-sub">Inherit mode target (${esc(defModel.split(":")[0])})</div>
        </div>
      </div>
      <div class="cartridge-option-right">
        <span class="cartridge-tag tag-default">DEFAULT</span>
      </div>
    </div>
  `;

  models.forEach(m => {
    const isAct = curVal === m.name;
    const gb = m.size ? `${(m.size / (1024 * 1024 * 1024)).toFixed(1)}GB` : "Neural Core";
    const tagClass = m.supports_thinking ? "tag-reasoning" : "tag-direct";
    const tagLabel = m.supports_thinking ? "REASONING" : "DIRECT";
    
    html += `
      <div class="cartridge-option-card ${isAct ? "active" : ""}" data-model="${esc(m.name)}" role="option" tabindex="0">
        <div class="cartridge-option-left">
          <div class="cartridge-option-pip"></div>
          <div class="cartridge-option-meta">
            <div class="cartridge-option-name">${esc(m.name)}</div>
            <div class="cartridge-option-sub">${esc(gb)} • ${esc(m.provider_name || 'Local')}</div>
          </div>
        </div>
        <div class="cartridge-option-right">
          <span class="cartridge-tag ${tagClass}">${tagLabel}</span>
        </div>
      </div>
    `;
  });

  list.innerHTML = html;

  list.querySelectorAll(".cartridge-option-card").forEach(card => {
    card.onclick = (e) => {
      e.stopPropagation();
      selectModelCartridge(card.dataset.model || "");
    };
    card.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectModelCartridge(card.dataset.model || "");
      }
    };
  });
}

function selectModelCartridge(modelVal) {
  const select = $("msgModel");
  if (!select) return;
  select.value = modelVal;

  // Haptic pulse animation on trigger pill
  const btn = $("cartridgeTriggerBtn");
  if (btn) {
    btn.classList.remove("cartridge-locked");
    void btn.offsetWidth;
    btn.classList.add("cartridge-locked");
  }

  // Holographic glitch animation on LCD model
  const lcd = $("lcdModel");
  if (lcd) {
    lcd.classList.remove("lcd-glitch");
    void lcd.offsetWidth;
    lcd.classList.add("lcd-glitch");
  }

  closeModelPopover();
  renderCartridgeTrigger();
  if (select.onchange) select.onchange();
}

function renderCartridgeTrigger() {
  const select = $("msgModel");
  const nameEl = $("cartridgeName");
  const badgeEl = $("cartridgeBadge");
  const pipEl = $("cartridgePip");
  if (!select || !nameEl) return;

  const curVal = select.value;
  const effective = getEffectiveModelForMode(mode);
  const caps = getModelCaps(effective);

  if (!curVal) {
    nameEl.textContent = `Default (${effective.split(":")[0]})`;
    if (badgeEl) {
      badgeEl.textContent = caps.supports_thinking ? "REASONING" : "DIRECT";
      badgeEl.className = `cartridge-spec-badge ${caps.supports_thinking ? "badge-reasoning" : "badge-direct"}`;
    }
  } else {
    nameEl.textContent = curVal.split(":")[0];
    if (badgeEl) {
      badgeEl.textContent = caps.supports_thinking ? "REASONING" : "DIRECT";
      badgeEl.className = `cartridge-spec-badge ${caps.supports_thinking ? "badge-reasoning" : "badge-direct"}`;
    }
  }

  if (pipEl) {
    pipEl.className = `cartridge-status-pip ${caps.supports_thinking ? "pip-reasoning" : "pip-direct"}`;
  }

  // Sync active classes in popover
  document.querySelectorAll("#cartridgeOptionsScroll .cartridge-option-card").forEach(card => {
    const isAct = (card.dataset.model || "") === curVal;
    card.classList.toggle("active", isAct);
    card.setAttribute("aria-selected", isAct ? "true" : "false");
  });
}

function toggleModelPopover() {
  const popover = $("modelPopover");
  const btn = $("cartridgeTriggerBtn");
  if (!popover || !btn) return;
  const isOpen = popover.classList.contains("open");
  if (isOpen) {
    closeModelPopover();
  } else {
    openModelPopover();
  }
}

function openModelPopover() {
  const popover = $("modelPopover");
  const btn = $("cartridgeTriggerBtn");
  if (!popover || !btn) return;
  renderCartridgeTrigger();
  popover.classList.remove("hidden");
  requestAnimationFrame(() => {
    popover.classList.add("open");
    btn.classList.add("active");
    btn.setAttribute("aria-expanded", "true");
  });
}

function closeModelPopover() {
  const popover = $("modelPopover");
  const btn = $("cartridgeTriggerBtn");
  if (!popover || !btn) return;
  popover.classList.remove("open");
  btn.classList.remove("active");
  btn.setAttribute("aria-expanded", "false");
  setTimeout(() => {
    if (!popover.classList.contains("open")) {
      popover.classList.add("hidden");
    }
  }, 200);
}
async function loadHistory(sessionId = null) {
  stopCurrentTTS();
  if (sessionId) {
    activeSessions[mode] = sessionId;
    localStorage.setItem(`lamplight_session_${mode}`, sessionId);
  }
  const sid = activeSessions[mode] || "";
  const stick = nearBottom() || $("chat").children.length === 0;
  const j = await api(`/api/history?mode=${mode}&session_id=${encodeURIComponent(sid)}&limit=100`).catch(e => { toast(e.message, true); return null; });
  if (!j) return;
  if (j.session_id) {
    activeSessions[mode] = j.session_id;
    localStorage.setItem(`lamplight_session_${mode}`, j.session_id);
  }
  $("chat").innerHTML = "";
  j.messages.forEach((m, i) => bubble(
    m.role === "user" ? "user" : "bot",
    m.content,
    i,
    m.created_at,
    m.role === "bot" ? (m.model || null) : null,
    m.attachments || null,
    m.skill || null
  ));
  updateHero(j.messages.length);
  renderChatMermaids($("chat"));
  if (stick) scrollEnd(false);
  loadSessions(mode);
}

/* ---------- send + stream ---------- */
function setStreaming(on) { $("btnStop").classList.toggle("hidden", !on); $("btnSend").disabled = on; }
$("btnStop").onclick = () => aborter && aborter.abort();
function autosize() {
  const m = $("msg");
  m.style.height = "auto";
  m.style.height = Math.min(Math.max(m.scrollHeight, 26), 180) + "px";
}
$("msg").addEventListener("input", () => {
  autosize();
  checkSlashTrigger();
});
$("msg").addEventListener("keydown", e => {
  if (!$("slashMenu").classList.contains("hidden") && filteredSkills.length > 0) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      slashSelectedIndex = (slashSelectedIndex + 1) % filteredSkills.length;
      renderSlashSelection();
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      slashSelectedIndex = (slashSelectedIndex - 1 + filteredSkills.length) % filteredSkills.length;
      renderSlashSelection();
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      if (slashSelectedIndex >= 0 && filteredSkills[slashSelectedIndex]) {
        e.preventDefault();
        selectSkill(filteredSkills[slashSelectedIndex]);
        return;
      }
    }
    if (e.key === "Escape") {
      e.preventDefault();
      hideSlashMenu();
      return;
    }
  }
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("btnSend").onclick = send;
async function send() {
  const v = $("msg").value.trim();
  const attsToSend = [...stagedAttachments];
  const currentSkill = activeSkill;
  if ((!v && !attsToSend.length) || $("btnSend").disabled) return;
  $("msg").value = ""; autosize();
  hideSlashMenu();
  stagedAttachments = []; renderAttachmentTray();
  activeSkill = null;
  $("activeSkillBar").classList.add("hidden");

  bubble("user", v, 0, null, null, attsToSend, currentSkill ? currentSkill.command : null);
  const el = bubble("bot", ""); el.innerHTML = "…";
  scrollEnd();
  try { await sendStream(v, el, attsToSend, currentSkill ? currentSkill.command : undefined); }
  catch (e) {
    try {
      const j = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          mode,
          message: v,
          model: $("msgModel").value || undefined,
          session_id: activeSessions[mode] || undefined,
          attachments: attsToSend,
          skill: currentSkill ? currentSkill.command : undefined,
          thinking_level: getThinkingLevel(mode)
        })
      });
      el.classList.remove("streaming"); el.dataset.raw = j.reply; el.innerHTML = md(j.reply); stamp(el, null, j.model, j.skill || (currentSkill ? currentSkill.command : null)); copyBtn(el);
      if (ttsSettings.auto_speak) {
        const tBtn = el.querySelector(".tts-btn");
        if (tBtn) playBubbleTTS(el, tBtn);
      }
      if (j.session_id) {
        activeSessions[mode] = j.session_id;
        localStorage.setItem(`lamplight_session_${mode}`, j.session_id);
      }
      loadSessions(mode);
    } catch (e2) { el.classList.remove("streaming"); el.textContent = "Error: " + e2.message; }
    setStreaming(false);
  }
  scrollEnd();
}
async function sendStream(v, el, attachments = [], skillCmd = undefined) {
  aborter = new AbortController();
  setStreaming(true);
  el.classList.add("streaming");
  let raw = "";
  let rawThinking = "";
  const tStream0 = performance.now();
  let tokenCount = 0;
  const warmTimer = setTimeout(() => {
    if (!raw && !rawThinking) el.innerHTML = "<i>Warming up the model… first reply after a switch can take ~40s on this laptop.</i>";
  }, 8000);
  const stick = () => { if (nearBottom()) scrollEnd(); };
  try {
    const r = await fetch("/api/chat-stream", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Passcode": passcode },
      body: JSON.stringify({
        mode,
        message: v,
        model: $("msgModel").value || undefined,
        session_id: activeSessions[mode] || undefined,
        attachments: attachments,
        skill: skillCmd,
        thinking_level: getThinkingLevel(mode)
      }), signal: aborter.signal });
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || r.statusText); }
    clearTimeout(warmTimer);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const j = JSON.parse(line);
        if (j.error) throw new Error(j.error);
        if (j.context_refreshed) {
          showContextRefreshNotice(j.notice);
        }
        if (j.thinking) {
          rawThinking += j.thinking;
          renderLiveReply(el, raw, rawThinking);
          stick();
        }
        if (j.t) {
          raw += j.t;
          tokenCount += j.t.split(/\s+/).length || 1;
          renderLiveReply(el, raw, rawThinking);
          stick();
        }
        if (j.done) {
          el.classList.remove("streaming");
          renderLiveReply(el, raw, rawThinking, true);
          stamp(el, null, j.model || null, j.skill || skillCmd || null);
          copyBtn(el);
          setStreaming(false);
          updateHero(1);
          const durSec = (performance.now() - tStream0) / 1000;
          if (durSec > 0.4 && tokenCount > 0) {
            const tps = Math.round((tokenCount * 1.3) / durSec);
            if ($("lcdSpeed")) $("lcdSpeed").textContent = `${tps} T/S`;
            if ($("hudTokVal")) $("hudTokVal").textContent = `${tps} tok/s`;
          }
          refreshTelemetryVram();
          if (j.session_id) {
            activeSessions[mode] = j.session_id;
            localStorage.setItem(`lamplight_session_${mode}`, j.session_id);
          }
          loadSessions(mode);
          if (ttsSettings.auto_speak) {
            const tBtn = el.querySelector(".tts-btn");
            if (tBtn) playBubbleTTS(el, tBtn);
          }
          return;
        }
      }
    }
    el.classList.remove("streaming");
    const stoppedRaw = raw || rawThinking;
    renderLiveReply(el, stoppedRaw, rawThinking, true);
    el.innerHTML += "<br><i>(stopped — not saved)</i>";
  } catch (e) {
    clearTimeout(warmTimer);
    el.classList.remove("streaming");
    if (e.name === "AbortError") {
      const stoppedRaw = raw || rawThinking;
      renderLiveReply(el, stoppedRaw, rawThinking, true);
      el.innerHTML += "<br><i>(stopped — not saved)</i>";
    }
    else { el.innerHTML = ""; throw e; }
  }
  setStreaming(false);
}

/* ---------- erase ---------- */
$("btnErase").onclick = async () => {
  if (!await askConfirm(`Erase all ${mode} history on this machine?`)) return;
  await api("/api/clear", { method: "POST", body: JSON.stringify({ mode }) });
  activeSessions[mode] = null;
  localStorage.removeItem(`lamplight_session_${mode}`);
  loadHistory();
  loadSessions(mode);
  toast("Erased.");
};

/* ---------- backup + export ---------- */
function dl(path, fallbackName) {
  fetch(path, { headers: { "X-Passcode": passcode } }).then(r => {
    if (!r.ok) throw new Error("locked?");
    return r.blob();
  }).then(b => {
    const u = URL.createObjectURL(b);
    const a = document.createElement("a");
    a.href = u; a.download = fallbackName;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(u); a.remove(); }, 500);
    toast("Downloading — keep it somewhere safe.");
  }).catch(e => toast(e.message, true));
}
$("btnBackup").onclick = () => dl("/api/backup", "lamplight-backup.db");
$("btnExportNsfw").onclick = () => {
  const sid = activeSessions["nsfw"] ? `&session_id=${encodeURIComponent(activeSessions["nsfw"])}` : "";
  dl(`/api/export?mode=nsfw${sid}`, "after-dark-chat.md");
};
$("btnExportFriend").onclick = () => {
  const sid = activeSessions["friend"] ? `&session_id=${encodeURIComponent(activeSessions["friend"])}` : "";
  dl(`/api/export?mode=friend${sid}`, "friend-chat.md");
};

/* ---------- regenerate ---------- */
$("btnRegen").onclick = async () => {
  const bots = document.querySelectorAll("#chat .msg.bot");
  if (!bots.length) { toast("Nothing to regenerate yet."); return; }
  if ($("btnSend").disabled) return;
  bots[bots.length - 1].remove();
  const el = bubble("bot", ""); el.innerHTML = "…";
  scrollEnd();
  const warmTimer2 = setTimeout(() => {
    if (el.innerHTML === "…") el.innerHTML = "<i>Warming up the model…</i>";
  }, 8000);
  aborter = new AbortController();
  setStreaming(true); el.classList.add("streaming");
  let raw = "";
  let rawThinking = "";
  try {
    const r = await fetch("/api/chat-stream", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Passcode": passcode },
      body: JSON.stringify({
        mode,
        message: "",
        regenerate: true,
        model: $("msgModel").value || undefined,
        session_id: activeSessions[mode] || undefined
      }), signal: aborter.signal });
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || r.statusText); }
    clearTimeout(warmTimer2);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const j = JSON.parse(line);
        if (j.error) throw new Error(j.error);
        if (j.context_refreshed) {
          showContextRefreshNotice(j.notice);
        }
        if (j.thinking) {
          rawThinking += j.thinking;
          renderLiveReply(el, raw, rawThinking);
          if (nearBottom()) scrollEnd();
        }
        if (j.t) {
          raw += j.t;
          renderLiveReply(el, raw, rawThinking);
          if (nearBottom()) scrollEnd();
        }
        if (j.done) {
          el.classList.remove("streaming");
          renderLiveReply(el, raw, rawThinking, true);
          stamp(el, null, j.model || null);
          copyBtn(el);
          setStreaming(false);
          loadSessions(mode);
          if (ttsSettings.auto_speak) {
            const tBtn = el.querySelector(".tts-btn");
            if (tBtn) playBubbleTTS(el, tBtn);
          }
          return;
        }
      }
    }
    clearTimeout(warmTimer2);
    el.classList.remove("streaming");
    const stoppedRaw = raw || rawThinking;
    renderLiveReply(el, stoppedRaw, rawThinking, true);
    el.innerHTML += "<br><i>(stopped — not saved)</i>";
  } catch (e) {
    clearTimeout(warmTimer2);
    el.classList.remove("streaming");
    if (e.name === "AbortError") {
      const stoppedRaw = raw || rawThinking;
      renderLiveReply(el, stoppedRaw, rawThinking, true);
      el.innerHTML += "<br><i>(stopped — not saved)</i>";
    }
    else el.textContent = "Error: " + e.message;
  }
  setStreaming(false);
};

/* ---------- wildness sliders + tool toggles ---------- */
function wireTools(modeKey, checkId) {
  api(`/api/tools?mode=${modeKey}`).then(j => { $(checkId).checked = !!j.enabled; }).catch(() => {});
  $(checkId).addEventListener("change", () => {
    api("/api/tools", { method: "POST", body: JSON.stringify({ mode: modeKey, enabled: $(checkId).checked }) })
      .then(() => toast($(checkId).checked ? "Tools on." : "Tools off."))
      .catch(e => toast(e.message, true));
  });
}
function wireTemp(modeKey, sliderId, valId) {
  api(`/api/temp?mode=${modeKey}`).then(j => { $(sliderId).value = j.temp; $(valId).textContent = (+j.temp).toFixed(1); }).catch(() => {});
  let t = null;
  $(sliderId).addEventListener("input", () => {
    $(valId).textContent = (+$(sliderId).value).toFixed(1);
    clearTimeout(t);
    t = setTimeout(() => api("/api/temp", { method: "POST", body: JSON.stringify({ mode: modeKey, temp: +$(sliderId).value }) }).catch(e => toast(e.message, true)), 400);
  });
}

/* ---------- vram monitor ---------- */
async function loadVramStatus() {
  const v = await api("/api/vram").catch(() => null);
  const textEl = $("vramStatusText");
  const cardEl = $("vramCard");
  const fillEl = $("vramBarFill");
  const pctEl = $("vramPctText");
  const subEl = $("vramSubtext");
  if (!v || !textEl) return;
  const pct = Math.min(100, Math.round(((v.total_vram_gb || 0) / (v.gpu_total_gb || 6)) * 100));
  if (fillEl) {
    fillEl.style.width = pct + "%";
    fillEl.classList.toggle("warn", pct > 75);
  }
  if (pctEl) pctEl.textContent = pct + "% used";

  const headerBadge = $("vramHeaderBadge");
  if (v.loaded && v.loaded.length > 0) {
    const names = v.loaded.map(m => `${esc(m.name)} (${m.size_vram_gb} GB)`).join(", ");
    textEl.innerHTML = `<strong>VRAM:</strong> ${v.total_vram_gb} / ${v.gpu_total_gb} GB`;
    if (subEl) subEl.innerHTML = `<span>Active: <strong>${names}</strong></span><span>${pct}%</span>`;
    if (cardEl) cardEl.classList.add("active");
    if (headerBadge) {
      headerBadge.textContent = `${v.total_vram_gb} GB`;
      headerBadge.style.background = pct > 80 ? "rgba(239, 68, 68, 0.2)" : "rgba(56, 189, 248, 0.15)";
      headerBadge.style.color = pct > 80 ? "#fca5a5" : "#7dd3fc";
    }
  } else {
    textEl.innerHTML = `<strong>GPU Idle:</strong> 0 / ${v.gpu_total_gb} GB`;
    if (subEl) subEl.innerHTML = `<span>GPU Memory Available</span><span>0%</span>`;
    if (cardEl) cardEl.classList.remove("active");
    if (headerBadge) {
      headerBadge.textContent = "Idle";
      headerBadge.style.background = "rgba(255, 255, 255, 0.06)";
      headerBadge.style.color = "#94a3b8";
    }
  }
}
$("btnEjectVram").onclick = async () => {
  try {
    const res = await api("/api/vram/eject", { method: "POST" });
    toast(res.message || "VRAM cleared");
    await loadVramStatus();
  } catch (e) {
    toast("Eject failed: " + e.message, true);
  }
};

/* ---------- models drawer ---------- */
$("btnModels").onclick = async () => {
  openDrawer("modelsPanel");
  loadVramStatus();
  loadTtsSettings();
  loadProviders();
  wireTemp("nsfw", "nsfwTemp", "nsfwTempVal");
  wireTools("nsfw", "nsfwTools");
  loadPromptInto("nsfw", "nsfwPrompt");
  const j = await api("/api/models").catch(e => { toast(e.message, true); return null; });
  if (!j) return;
  const per = j.per_mode || {};
  // Match active user modes (nsfw, friend, architect, and muse)
  const usersOf = name => Object.entries(per)
    .filter(([m, cur]) => (m === "nsfw" || m === "friend" || m === "architect" || m === "muse") && cur === name)
    .map(([m]) => m);

  $("modelList").innerHTML = j.models.map(m => {
    const users = usersOf(m.name);
    return `<div class="model-card ${users.length ? 'in-use' : ''}">
      <div class="model-top">
        <div class="model-title-wrap">
          <svg class="model-brain-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2a4 4 0 0 0-4 4v1H6a3 3 0 0 0-3 3v2a3 3 0 0 0 3 3h2v1a4 4 0 0 0 4 4 4 4 0 0 0 4-4v-1h2a3 3 0 0 0 3-3v-2a3 3 0 0 0-3-3h-2V6a4 4 0 0 0-4-4Z"></path>
          </svg>
          <span class="model-name">${esc(m.name)}</span>
        </div>
        <span class="size-badge">${(m.size / 1e9).toFixed(1)} GB</span>
      </div>
      ${users.length ? `<div class="use-chips">${users.map(u => `
        <span class="chip ${u}">
          <span class="chip-dot"></span>
          ${u === "nsfw" ? "🌙 After Dark Active" : (u === "architect" ? "📐 Architect Active" : (u === "muse" ? "✒️ Muse Active" : "✨ Friend Active"))}
        </span>`).join("")}</div>` : ''}
    </div>`;
  }).join("") || "<i>No models detected</i>";

  $("perMode").innerHTML = [
    { mode: "nsfw", label: "After Dark Bot", icon: "🌙", cur: per.nsfw },
    { mode: "friend", label: "Friend Companion", icon: "✨", cur: per.friend },
    { mode: "architect", label: "Architect Systems Bot", icon: "📐", cur: per.architect },
    { mode: "muse", label: "Muse Storyteller", icon: "✒️", cur: per.muse }
  ].map(b => `
    <div class="assign-card">
      <div class="assign-label">
        <span class="assign-icon">${b.icon}</span>
        <span class="assign-title">${b.label}</span>
      </div>
      <div class="assign-select-wrap">
        <select data-m="${b.mode}">
          ${j.models.map(x => `<option value="${esc(x.name)}" ${x.name === b.cur ? "selected" : ""}>${esc(x.name)} (${(x.size / 1e9).toFixed(1)}GB)</option>`).join("")}
        </select>
        <span class="select-chevron">▾</span>
      </div>
    </div>
  `).join("");

  document.querySelectorAll("#perMode select").forEach(s => s.onchange = () => {
    api("/api/mode-model", { method: "POST", body: JSON.stringify({ mode: s.dataset.m, model: s.value }) })
      .then(() => {
        const botName = s.dataset.m === 'nsfw' ? 'After Dark' : (s.dataset.m === 'architect' ? 'Architect' : (s.dataset.m === 'muse' ? 'Muse' : 'Friend'));
        toast(`Assigned ${botName} → ${s.value}`);
        $("btnModels").onclick(); // Refresh list to update active badges
      }).catch(e => toast(e.message, true));
  });
};
$("btnImport").onclick = async () => {
  if (!await askConfirm("Import this GGUF into Ollama? It may take a few minutes.")) return;
  $("importMsg").textContent = "Importing…";
  try {
    const j = await api("/api/import", { method: "POST", body: JSON.stringify({ name: $("ggufName").value, gguf_path: $("ggufPath").value }) });
    $("importMsg").textContent = "Imported: " + j.name; toast("Model imported.");
  } catch (e) { $("importMsg").textContent = "Failed: " + e.message; toast(e.message, true); }
};
$("btnSetCode").onclick = async () => {
  try { await api("/api/passcode/set", { method: "POST", body: JSON.stringify({ code: $("newCode").value }) }); toast("Lock set. Reload to test."); }
  catch (e) { toast(e.message, true); }
};

/* ---------- personalities ---------- */
async function loadPromptInto(modeKey, areaId) {
  const j = await api("/api/prompts").catch(() => null);
  if (j) $(areaId).value = j.prompts[modeKey].custom || "";
  $(areaId).placeholder = "Default:\n" + ((j && j.prompts[modeKey].effective) || "").slice(0, 160) + "…";
}
async function savePrompt(modeKey, areaId) {
  try {
    await api("/api/prompts", { method: "POST", body: JSON.stringify({ mode: modeKey, prompt: $(areaId).value }) });
    toast("Personality saved — applies to new replies.");
  } catch (e) { toast("Save failed: " + e.message, true); }
}
async function resetPrompt(modeKey, areaId) {
  if (!await askConfirm("Reset to the default personality?")) return;
  const j = await api("/api/prompts/reset", { method: "POST", body: JSON.stringify({ mode: modeKey }) });
  $(areaId).value = ""; $(areaId).placeholder = "Default:\n" + (j.effective || "").slice(0, 160) + "…";
  toast("Reset to default.");
}
$("btnSaveFriendPrompt").onclick = () => savePrompt("friend", "friendPrompt");
$("btnResetFriendPrompt").onclick = () => resetPrompt("friend", "friendPrompt");
$("btnSaveNsfwPrompt").onclick = () => savePrompt("nsfw", "nsfwPrompt");
$("btnResetNsfwPrompt").onclick = () => resetPrompt("nsfw", "nsfwPrompt");

/* ---------- friend memory ---------- */
async function loadProfile() {
  const j = await api("/api/friend/profile").catch(e => { toast(e.message, true); return null; });
  if (!j) return;
  $("friendName").value = j.profile.name || "";
  $("friendAbout").value = j.profile.about || "";
  $("friendSummary").textContent = j.summary ? "Remembers: " + j.summary : "No long-term summary yet.";
  const f = await api("/api/friend/facts").catch(() => null);
  if (!f) return;
  $("factList").innerHTML = f.facts.map(x =>
    `<div><span>• ${esc(x.fact)}</span><button data-fid="${x.id}" class="delFact">✕</button></div>`).join("")
    || "<i class='muted'>Nothing learned yet — chat and it picks things up.</i>";
  document.querySelectorAll(".delFact").forEach(b => b.onclick = async () => {
    await api("/api/friend/facts/delete", { method: "POST", body: JSON.stringify({ id: +b.dataset.fid }) });
    loadProfile();
  });
}
$("btnSaveProfile").onclick = async () => {
  try {
    await api("/api/friend/profile", { method: "POST", body: JSON.stringify({ name: $("friendName").value, about: $("friendAbout").value }) });
    await loadProfile(); updateHero(0); toast("Memory saved.");
  } catch (e) { toast("Save failed: " + e.message, true); }
};
$("btnAddFact").onclick = async () => {
  const v = $("newFact").value.trim(); if (!v) return;
  try { await api("/api/friend/facts", { method: "POST", body: JSON.stringify({ fact: v }) }); $("newFact").value = ""; loadProfile(); }
  catch (e) { toast("Couldn't save: " + e.message, true); }
};
$("btnForget").onclick = async () => {
  if (!await askConfirm("Forget profile, learned facts, summary and all friend chats?")) return;
  await api("/api/friend/forget", { method: "POST" });
  loadProfile(); loadHistory(); toast("Forgotten.");
};

/* ---------- rolling context refresh notice ---------- */
function showContextRefreshNotice(msg = "Context window refreshed & compacted into rolling memory.") {
  const existing = document.querySelector(".context-refresh-pill");
  if (existing) existing.remove();
  const pill = document.createElement("div");
  pill.className = "context-refresh-pill";
  pill.innerHTML = `
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
    </svg>
    <span>${msg}</span>
  `;
  document.body.appendChild(pill);
  setTimeout(() => {
    pill.style.transition = "opacity 0.5s ease, transform 0.5s ease";
    pill.style.opacity = "0";
    pill.style.transform = "translateY(10px)";
    setTimeout(() => pill.remove(), 500);
  }, 4500);
}

/* ---------- architect project stack & settings ---------- */
function wireArchitect() {
  const btn = $("btnArchitectStack");
  if (btn) {
    btn.onclick = () => {
      openDrawer("architectPanel");
      loadArchitectSettings();
    };
  }

  const btnSavePrompt = $("btnSaveArchitectPrompt");
  if (btnSavePrompt) btnSavePrompt.onclick = () => savePrompt("architect", "architectPrompt");

  const btnResetPrompt = $("btnResetArchitectPrompt");
  if (btnResetPrompt) btnResetPrompt.onclick = () => resetPrompt("architect", "architectPrompt");

  // 1-Click Stack Presets
  document.querySelectorAll(".stack-presets button[data-stack]").forEach(b => {
    b.onclick = () => {
      const val = b.getAttribute("data-stack");
      const ta = $("architectStack");
      if (ta) {
        ta.value = val;
        ta.focus();
        toast("Stack preset loaded: " + b.textContent.trim());
      }
    };
  });

  // 1-Click Rule Presets
  document.querySelectorAll(".rule-presets button[data-rule]").forEach(b => {
    b.onclick = () => {
      const val = b.getAttribute("data-rule");
      const ta = $("architectRules");
      if (ta) {
        if (ta.value.trim() && !ta.value.includes(val)) {
          ta.value = ta.value.trim() + "\n- " + val;
        } else {
          ta.value = val;
        }
        ta.focus();
        toast("Rule directive added: " + b.textContent.trim());
      }
    };
  });

  const btnSaveStack = $("btnSaveArchitectStack");
  if (btnSaveStack) {
    btnSaveStack.onclick = async () => {
      try {
        const stack = $("architectStack") ? $("architectStack").value : "";
        const rules = $("architectRules") ? $("architectRules").value : "";
        await api("/api/architect/stack", {
          method: "POST",
          body: JSON.stringify({ stack, rules })
        });
        toast("Architect project stack & rules saved.");
      } catch (err) {
        toast("Save failed: " + err.message, true);
      }
    };
  }

  const btnClearStack = $("btnClearArchitectStack");
  if (btnClearStack) {
    btnClearStack.onclick = async () => {
      if (!await askConfirm("Clear all Architect stack and rules?")) return;
      try {
        await api("/api/architect/clear", { method: "POST" });
        if ($("architectStack")) $("architectStack").value = "";
        if ($("architectRules")) $("architectRules").value = "";
        toast("Architect project stack cleared.");
      } catch (err) {
        toast("Clear failed: " + err.message, true);
      }
    };
  }
}

async function loadArchitectSettings() {
  loadPromptInto("architect", "architectPrompt");
  wireTemp("architect", "architectTemp", "architectTempVal");
  wireTools("architect", "architectTools");

  try {
    const data = await api("/api/architect/stack");
    if (data) {
      if ($("architectStack")) $("architectStack").value = data.stack || "";
      if ($("architectRules")) $("architectRules").value = data.rules || "";
    }
  } catch (err) {
    console.warn("Could not load architect stack:", err);
  }
}

/* ---------- muse story bible & settings ---------- */
function wireMuse() {
  const btn = $("btnMuseBible");
  if (btn) {
    btn.onclick = () => {
      openDrawer("musePanel");
      loadMuseSettings();
    };
  }

  const btnSavePrompt = $("btnSaveMusePrompt");
  if (btnSavePrompt) btnSavePrompt.onclick = () => savePrompt("muse", "musePrompt");

  const btnResetPrompt = $("btnResetMusePrompt");
  if (btnResetPrompt) btnResetPrompt.onclick = () => resetPrompt("muse", "musePrompt");

  // 1-Click Muse Genre Presets
  document.querySelectorAll(".muse-presets button[data-muse-bible]").forEach(b => {
    b.onclick = () => {
      const val = b.getAttribute("data-muse-bible");
      const ta = $("museBible");
      if (ta) {
        ta.value = val;
        ta.focus();
        toast("Lore setting loaded: " + b.textContent.trim());
      }
    };
  });

  // 1-Click Muse Rule Presets
  document.querySelectorAll(".muse-rule-presets button[data-muse-rule]").forEach(b => {
    b.onclick = () => {
      const val = b.getAttribute("data-muse-rule");
      const ta = $("museRules");
      if (ta) {
        if (ta.value.trim() && !ta.value.includes(val)) {
          ta.value = ta.value.trim() + "\n- " + val;
        } else {
          ta.value = val;
        }
        ta.focus();
        toast("Prose directive added: " + b.textContent.trim());
      }
    };
  });

  const btnSaveBible = $("btnSaveMuseBible");
  if (btnSaveBible) {
    btnSaveBible.onclick = async () => {
      try {
        const bible = $("museBible") ? $("museBible").value : "";
        const characters = $("museCharacters") ? $("museCharacters").value : "";
        const rules = $("museRules") ? $("museRules").value : "";
        await api("/api/muse/bible", {
          method: "POST",
          body: JSON.stringify({ bible, characters, rules })
        });
        toast("Story & World Bible saved.");
      } catch (err) {
        toast("Save failed: " + err.message, true);
      }
    };
  }

  const btnClearBible = $("btnClearMuseBible");
  if (btnClearBible) {
    btnClearBible.onclick = async () => {
      if (!await askConfirm("Clear all Story Bible lore, characters, and rules?")) return;
      try {
        await api("/api/muse/clear", { method: "POST" });
        if ($("museBible")) $("museBible").value = "";
        if ($("museCharacters")) $("museCharacters").value = "";
        if ($("museRules")) $("museRules").value = "";
        toast("Story Bible cleared.");
      } catch (err) {
        toast("Clear failed: " + err.message, true);
      }
    };
  }
}

async function loadMuseSettings() {
  loadPromptInto("muse", "musePrompt");
  wireTemp("muse", "museTemp", "museTempVal");
  wireTools("muse", "museTools");

  try {
    const data = await api("/api/muse/bible");
    if (data) {
      if ($("museBible")) $("museBible").value = data.bible || "";
      if ($("museCharacters")) $("museCharacters").value = data.characters || "";
      if ($("museRules")) $("museRules").value = data.rules || "";
    }
  } catch (err) {
    console.warn("Could not load muse bible:", err);
  }
}

/* ---------- chess arena ---------- */
let chessOn = false, chessAuto = false, chessMode = "watch";
let currentChessState = null, scrubStep = -1;
let selSq = null, dotSq = [], hintSq = [];
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const PIECE_VALS = { p: 1, n: 3, b: 3, r: 5, q: 9, k: 0 };
const START_PIECES = { p: 8, n: 2, b: 2, r: 2, q: 1 };

let audioCtx = null;
function playMoveSound(isCapture = false) {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const now = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    if (isCapture) {
      osc.type = "triangle";
      osc.frequency.setValueAtTime(340, now);
      osc.frequency.exponentialRampToValueAtTime(140, now + 0.12);
      gain.gain.setValueAtTime(0.2, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.12);
      osc.start(now);
      osc.stop(now + 0.12);
    } else {
      osc.type = "sine";
      osc.frequency.setValueAtTime(260, now);
      osc.frequency.exponentialRampToValueAtTime(180, now + 0.08);
      gain.gain.setValueAtTime(0.12, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.08);
      osc.start(now);
      osc.stop(now + 0.08);
    }
  } catch (e) {}
}

function updateCapturedTrays(fen) {
  const boardPart = (fen || "").split(" ")[0];
  const currentWhite = { p: 0, n: 0, b: 0, r: 0, q: 0 };
  const currentBlack = { p: 0, n: 0, b: 0, r: 0, q: 0 };
  for (const ch of boardPart) {
    const low = ch.toLowerCase();
    if (PIECE_VALS[low] !== undefined) {
      if (ch === ch.toUpperCase()) currentWhite[low] = (currentWhite[low] || 0) + 1;
      else currentBlack[low] = (currentBlack[low] || 0) + 1;
    }
  }
  const capByWhite = [];
  let wScore = 0;
  for (const [p, startCount] of Object.entries(START_PIECES)) {
    const lost = Math.max(0, startCount - (currentBlack[p] || 0));
    for (let i = 0; i < lost; i++) capByWhite.push(p);
    wScore += (currentWhite[p] || 0) * PIECE_VALS[p];
  }
  const capByBlack = [];
  let bScore = 0;
  for (const [p, startCount] of Object.entries(START_PIECES)) {
    const lost = Math.max(0, startCount - (currentWhite[p] || 0));
    for (let i = 0; i < lost; i++) capByBlack.push(p);
    bScore += (currentBlack[p] || 0) * PIECE_VALS[p];
  }
  const diff = wScore - bScore;

  const wTray = $("trayWhite");
  const bTray = $("trayBlack");
  if (wTray) {
    wTray.innerHTML = capByWhite.map(p => `<span class="pb">${GLYPH[p]}</span>`).join("")
      + (diff > 0 ? `<span class="mat-diff">+${diff}</span>` : "");
  }
  if (bTray) {
    bTray.innerHTML = capByBlack.map(p => `<span class="pw">${GLYPH[p]}</span>`).join("")
      + (diff < 0 ? `<span class="mat-diff">+${-diff}</span>` : "");
  }
}

$("btnChess").onclick = () => {
  setMainView(chessOn ? "chat" : "chess");
};
async function chessInit() {
  const j = await api("/api/models").catch(() => null);
  if (j) {
    const opts = j.models.map(m => `<option>${esc(m.name)}</option>`).join("");
    $("chessW").innerHTML = opts; $("chessB").innerHTML = opts;
  }
  const s = await api("/api/chess/state").catch(e => { toast(e.message, true); return null; });
  if (!s) return;
  currentChessState = s;
  scrubStep = -1;
  if (j && j.models.length) {
    if ([...$("chessW").options].some(o => o.value === s.white)) $("chessW").value = s.white;
    if ([...$("chessB").options].some(o => o.value === s.black)) $("chessB").value = s.black;
  }
  setFeedTab(feedTab || "banter");
  renderChess(s, null);
}
function renderChess(s, moved) {
  currentChessState = s;
  if (moved) scrubStep = -1;
  $("chessWhite").textContent = s.white;
  $("chessBlack").textContent = s.black;
  const sc = s.score || {};
  $("chessScore").textContent = Object.keys(sc).length
    ? Object.entries(sc).map(([m, r]) => `${m.split(":")[0]} ${r.w}W·${r.d}D·${r.l}L`).join("  |  ") : "fresh rivalry";

  const fw = $("fighterWhite");
  const fb = $("fighterBlack");
  if (fw && fb) {
    fw.classList.toggle("turn-active", !s.over && s.turn === "white");
    fb.classList.toggle("turn-active", !s.over && s.turn === "black");
  }
  updateCapturedTrays(s.fen);

  if (s.over) {
    $("chessStatus").innerHTML = `🏁 ${esc(s.why || s.result || "")}`;
    chessAuto = false; $("btnChessAuto").textContent = "Auto ▶";
  } else if (s.busy) $("chessStatus").innerHTML = `<span class="thinking">●</span> ${s.turn === "white" ? esc(s.white) : esc(s.black)} is thinking…`;
  else $("chessStatus").textContent = `${s.turn === "white" ? "White" : "Black"} to move${s.in_book ? "  📖" : ""}`;
  
  // eval bar (White perspective)
  const ev = s.eval || {};
  let pct = 50, label = "—";
  if (ev.mate !== null && ev.mate !== undefined) {
    pct = ev.mate > 0 ? 100 : 0;
    label = ev.mate > 0 ? `mate in ${ev.mate} for White` : `mate in ${-ev.mate} for Black`;
  } else if (ev.cp !== null && ev.cp !== undefined) {
    pct = 50 + 50 * (2 / (1 + Math.exp(-0.004 * ev.cp)) - 1);
    label = (ev.cp >= 0 ? "+" : "") + (ev.cp / 100).toFixed(1);
  }
  if (s.eval_state && s.eval_state.advantage) {
    label = s.eval_state.advantage;
    if (s.eval_state.win_pct !== undefined) pct = s.eval_state.win_pct;
  }
  $("evalWhite").style.width = pct.toFixed(1) + "%";
  $("evalText").textContent = label;
  if (s.reason) $("chessReason").textContent = `💭 ${s.reason}`;

  // feed tabs & badges
  const banterList = s.banter || [];
  const movesList = s.history || [];
  if ($("badgeBanter")) $("badgeBanter").textContent = banterList.length;
  if ($("badgeMoves")) $("badgeMoves").textContent = movesList.length;
  if ($("feedStatusSub")) {
    $("feedStatusSub").textContent = s.over ? (s.why || "Game over")
      : (s.busy ? "Thinking…" : (s.turn === "white" ? "White to move" : "Black to move"));
  }

  // ringside banter
  const rs = $("ringside");
  if (!banterList.length) {
    rs.innerHTML = `<div class="empty-state"><span class="empty-icon">🥊</span><p>Quiet… too quiet. Start a game or make a move to unleash the banter.</p></div>`;
  } else {
    rs.innerHTML = banterList.map(b => {
      const cls = b.side === "human" ? "human" : (b.side === "white" ? "w" : "b");
      const who = b.side === "human" ? "You" : `${b.side === "white" ? "White" : "Black"} · ${esc((b.model || "").split(":")[0])}`;
      return `<div class="jab ${cls}"><small>${who}</small>${esc(b.text)}</div>`;
    }).join("");
  }
  rs.scrollTop = 1e9;

  // human-mode chrome
  const humanTurn = !s.over && s.human && s.human === s.turn;
  $("btnChessHint").classList.toggle("hidden", !humanTurn);
  $("tauntRow").classList.toggle("hidden", !(s.human && !s.over));

  if (scrubStep === -1) {
    const rows = s.fen.split(" ")[0].split("/");
    const bd = $("board"); bd.innerHTML = "";
    const last = s.last_move ? [s.last_move.slice(0, 2), s.last_move.slice(2, 4)] : [];
    rows.forEach((row, ri) => {
      let fi = 0;
      for (const ch of row) {
        if (/\d/.test(ch)) { for (let k = 0; k < +ch; k++) bd.appendChild(mkSq(fi++, ri, null, last)); }
        else { bd.appendChild(mkSq(fi, ri, ch, last)); fi++; }
      }
    });
    $("btnScrubEnd")?.classList.add("on");
    const scrubStatus = $("scrubStatusText");
    if (scrubStatus) scrubStatus.textContent = "Live Position";
  }

  // moves list
  const movesEl = $("chessMoves");
  if (!movesList.length) {
    movesEl.innerHTML = `<div class="empty-state"><span class="empty-icon">♟️</span><p>No moves yet. White moves first.</p></div>`;
  } else {
    movesEl.innerHTML = `<div class="moves-table">` + movesList.map((m, idx) => {
      const isLatest = idx === movesList.length - 1;
      const isWhiteActive = scrubStep === -1 ? (isLatest && !m.b) : (scrubStep === idx * 2);
      const isBlackActive = scrubStep === -1 ? (isLatest && !!m.b) : (scrubStep === idx * 2 + 1);
      return `<div class="moves-row">
        <span class="num">${m.n}.</span>
        <span class="san-w ${isWhiteActive ? 'active' : ''}" data-step="${idx * 2}" title="Scrub to ${esc(m.w)}">${esc(m.w)}</span>
        <span class="san-b ${isBlackActive ? 'active' : ''}" ${m.b ? `data-step="${idx * 2 + 1}" title="Scrub to ${esc(m.b)}"` : ''}>${esc(m.b || '—')}</span>
      </div>`;
    }).join("") + `</div>`;
  }
  if (scrubStep === -1) {
    movesEl.scrollTop = 1e9;
  }

  const f = s.fallbacks || {}, r = s.retries || {};
  const c = (s.coach && s.coach.vetoes) || {};
  $("chessStats").textContent = `Illegal retries — w: ${r.white || 0} (random ×${f.white || 0}) · b: ${r.black || 0} (×${f.black || 0}) — coach vetoes w:${c.white || 0} b:${c.black || 0}`;
  const coachBtn = $("btnChessCoach");
  if (s.coach) coachBtn.textContent = s.coach.on ? "Coach 🛡️ on" : "Coach off";

  if (moved) {
    playMoveSound((moved.san || "").includes("x"));
    if (moved.fallback) toast(`${moved.model} played random (3 illegal tries)`, true);
    else if (moved.book) toast(`📖 book theory: ${moved.san}`);
    else if (moved.vetoes) toast(`🛡️ coach vetoed ×${moved.vetoes}, final: ${moved.san}`);
    else toast(`${moved.side === "white" ? "White" : "Black"}: ${moved.san}`);
  }
}

// Feed tab switching
let feedTab = "banter";
function setFeedTab(t) {
  feedTab = t;
  $("tabFeedBanter").classList.toggle("on", t === "banter");
  $("tabFeedMoves").classList.toggle("on", t === "moves");
  $("feedBodyBanter").classList.toggle("hidden", t !== "banter");
  $("feedBodyMoves").classList.toggle("hidden", t !== "moves");
}
$("tabFeedBanter").onclick = () => setFeedTab("banter");
$("tabFeedMoves").onclick = () => setFeedTab("moves");

$("btnModeWatch").onclick = () => setChessMode("watch");
$("btnModePlay").onclick = () => setChessMode("play");
function setChessMode(m) {
  chessMode = m;
  chessAuto = false; $("btnChessAuto").textContent = "Auto ▶";
  selSq = null; dotSq = []; hintSq = [];
  $("btnModeWatch").classList.toggle("on", m === "watch");
  $("btnModePlay").classList.toggle("on", m === "play");
  $("chessSide").classList.toggle("hidden", m !== "play");
  $("btnChessAuto").classList.toggle("hidden", m !== "watch");
  $("tauntRow").classList.toggle("hidden", true);
  if (m === "play") toast("Pick a side, New game, tap your pieces.");
}
function mkSq(fi, ri, piece, last) {
  const d = document.createElement("div");
  const file = "abcdefgh"[fi];
  const rank = 8 - ri;
  const sq = file + rank;
  d.dataset.sq = sq;
  d.className = "sq " + ((fi + ri) % 2 ? "dark" : "light");
  if (last.includes(sq)) d.classList.add("last");
  if (sq === selSq) d.classList.add("sel");
  if (dotSq.includes(sq)) d.classList.add(piece ? "cap" : "dot");
  if (hintSq.includes(sq)) d.classList.add("hintSq");

  if (fi === 0) {
    const rSpan = document.createElement("span");
    rSpan.className = "coord rank";
    rSpan.textContent = rank;
    d.appendChild(rSpan);
  }
  if (ri === 7) {
    const fSpan = document.createElement("span");
    fSpan.className = "coord file";
    fSpan.textContent = file;
    d.appendChild(fSpan);
  }

  if (piece) {
    const white = piece === piece.toUpperCase();
    const pSpan = document.createElement("span");
    pSpan.className = white ? "pw" : "pb";
    pSpan.textContent = GLYPH[piece.toLowerCase()];
    d.appendChild(pSpan);
  }
  return d;
}
$("board").addEventListener("click", async ev => {
  const sqEl = ev.target.closest(".sq");
  if (!sqEl || chessMode !== "play") return;
  const sq = sqEl.dataset.sq;
  const st = await api("/api/chess/state").catch(() => null);
  if (!st || st.over) return;
  if (st.human !== st.turn) { toast("Wait for the model…", true); return; }
  if (dotSq.includes(sq) && selSq) {
    await humanMove(selSq + sq);
    return;
  }
  const j = await api(`/api/chess/legal?square=${sq}`).catch(() => null);
  selSq = (j && j.targets.length) ? sq : null;
  dotSq = (j && j.targets) || [];
  hintSq = [];
  renderDotsOnly();
});
function renderDotsOnly() {
  document.querySelectorAll("#board .sq").forEach(d => {
    const sq = d.dataset.sq;
    d.classList.toggle("sel", sq === selSq);
    d.classList.toggle("dot", dotSq.includes(sq) && !d.querySelector("span"));
    d.classList.toggle("cap", dotSq.includes(sq) && !!d.querySelector("span"));
    if (!hintSq.includes(sq)) d.classList.remove("hintSq");
  });
}
async function humanMove(uci) {
  const taunt = $("taunt").value.trim();
  selSq = null; dotSq = []; hintSq = [];
  try {
    const s = await api("/api/chess/human", { method: "POST", body: JSON.stringify({ uci, taunt }) });
    $("taunt").value = "";
    renderChess(s, s.moved);
    if (!s.over) setTimeout(() => chessStep(), 1200); // model answers
  } catch (e) { toast(e.message, true); chessInit(); }
}
$("btnChessHint").onclick = async () => {
  try {
    const h = await api("/api/chess/hint");
    hintSq = [h.from, h.to];
    renderDotsOnly();
    toast(`Coach suggests ${h.san}`);
  } catch (e) { toast(e.message, true); }
};
$("btnChessNew").onclick = async () => {
  chessAuto = false; $("btnChessAuto").textContent = "Auto ▶";
  selSq = null; dotSq = []; hintSq = [];
  const payload = { white: $("chessW").value, black: $("chessB").value };
  if (chessMode === "play") payload.human = $("chessSide").value;
  try {
    const s = await api("/api/chess/new", { method: "POST", body: JSON.stringify(payload) });
    renderChess(s, null);
    toast(chessMode === "play" ? `You are ${payload.human}. ${payload.human === "white" ? "Your move!" : "Model moves first…"}` : "New game — White moves first.");
    if (chessMode === "play" && payload.human === "black" && !s.over) setTimeout(() => chessStep(), 1200);
  } catch (e) { toast(e.message, true); }
};
$("btnChessStep").onclick = () => chessStep();
$("btnChessCoach").onclick = async () => {
  const s = await api("/api/chess/state").catch(() => null);
  const on = !(s && s.coach && s.coach.on);
  try {
    await api("/api/chess/coach", { method: "POST", body: JSON.stringify({ enabled: on }) });
    toast(on ? "Coach on — blunders get vetoed." : "Coach off — raw bot chaos.");
    chessInit();
  } catch (e) { toast(e.message, true); }
};
async function chessStep() {
  selSq = null; dotSq = []; hintSq = [];
  $("chessStatus").innerHTML = `<span class="thinking">●</span> thinking…`;
  try {
    const s = await api("/api/chess/step", { method: "POST" });
    renderChess(s, s.moved);
  } catch (e) { toast(e.message, true); chessInit(); }
}
$("btnChessAuto").onclick = async () => {
  chessAuto = !chessAuto;
  $("btnChessAuto").textContent = chessAuto ? "Stop ■" : "Auto ▶";
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  while (chessAuto && chessOn) {
    const s = await api("/api/chess/state").catch(() => null);
    if (!s || s.over || s.busy) { if (s && s.over) renderChess(s, null); chessAuto = false; $("btnChessAuto").textContent = "Auto ▶"; break; }
    await chessStep();
    if (chessAuto) await sleep(2500); // let spectators read the reason
  }
};

/* ---------- AI Arena: Scrubber, Council, Debate & History ---------- */

async function scrubToMove(stepIndex) {
  if (!currentChessState) return;
  const hist = currentChessState.hist_details || [];
  if (stepIndex < 0 || stepIndex >= hist.length) return;
  
  if (chessAuto) {
    chessAuto = false;
    const ab = $("btnChessAuto");
    if (ab) ab.textContent = "Auto ▶";
  }
  
  scrubStep = stepIndex;
  const stepData = hist[stepIndex];
  
  $("btnScrubEnd")?.classList.remove("on");
  const scrubStatus = $("scrubStatusText");
  if (scrubStatus) {
    scrubStatus.textContent = `Move ${stepIndex + 1}/${hist.length} (${stepData.san})`;
  }
  
  // Render historical board
  const rows = stepData.fen.split(" ")[0].split("/");
  const bd = $("board");
  if (bd) {
    bd.innerHTML = "";
    const last = stepData.uci ? [stepData.uci.slice(0, 2), stepData.uci.slice(2, 4)] : [];
    rows.forEach((row, ri) => {
      let fi = 0;
      for (const ch of row) {
        if (/\d/.test(ch)) { for (let k = 0; k < +ch; k++) bd.appendChild(mkSq(fi++, ri, null, last)); }
        else { bd.appendChild(mkSq(fi, ri, ch, last)); fi++; }
      }
    });
  }
  updateCapturedTrays(stepData.fen);
  
  // Highlight active move in moves table
  document.querySelectorAll("#chessMoves [data-step]").forEach(el => {
    el.classList.toggle("active", parseInt(el.dataset.step, 10) === stepIndex);
  });
}

function scrubToLive() {
  scrubStep = -1;
  $("btnScrubEnd")?.classList.add("on");
  const scrubStatus = $("scrubStatusText");
  if (scrubStatus) scrubStatus.textContent = "Live Position";
  if (currentChessState) {
    renderChess(currentChessState, null);
  }
}

/* --- AI Council --- */
let councilJurors = [];
let councilExecMode = "sequential";

function renderCouncilJurorPickers(models) {
  const container = $("councilJurorsPickers");
  if (!container) return;
  container.innerHTML = "";
  
  councilJurors.forEach((jurorModel, idx) => {
    const item = document.createElement("div");
    item.className = "council-juror-item";
    
    const num = document.createElement("span");
    num.className = "juror-index";
    num.textContent = `#${idx + 1}`;
    
    const sel = document.createElement("select");
    sel.className = "council-juror-select";
    models.forEach(m => {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = m.name;
      if (m.name === jurorModel) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.onchange = () => { councilJurors[idx] = sel.value; };
    
    item.appendChild(num);
    item.appendChild(sel);
    
    if (councilJurors.length > 2) {
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "council-remove-juror-btn";
      delBtn.textContent = "✕";
      delBtn.title = "Remove juror";
      delBtn.onclick = () => {
        councilJurors.splice(idx, 1);
        renderCouncilJurorPickers(models);
      };
      item.appendChild(delBtn);
    }
    
    container.appendChild(item);
  });
  
  if (councilJurors.length < 4) {
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "council-add-juror-btn";
    addBtn.innerHTML = `<span>+ Add Juror</span>`;
    addBtn.onclick = () => {
      const nextModel = models[councilJurors.length % models.length]?.name || models[0]?.name || "default";
      councilJurors.push(nextModel);
      renderCouncilJurorPickers(models);
    };
    container.appendChild(addBtn);
  }
}

async function initCouncil() {
  const j = await api("/api/models").catch(() => null);
  const models = (j && j.models) ? j.models : [];
  if (!models.length) return;
  
  if (councilJurors.length === 0) {
    const distinct = models.map(m => m.name);
    councilJurors = [
      distinct[0],
      distinct[1] || distinct[0],
      distinct[2] || distinct[0]
    ];
  }
  
  const judgeSel = $("councilJudgeSelect");
  if (judgeSel && judgeSel.options.length === 0) {
    judgeSel.innerHTML = models.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
    judgeSel.value = models[0].name;
  }
  
  renderCouncilJurorPickers(models);
}

async function runCouncil() {
  const topic = $("councilTopic")?.value.trim();
  if (!topic) {
    toast("Please enter a topic or dilemma for the Council", true);
    return;
  }
  if (councilJurors.length < 2) {
    toast("Please select at least 2 jurors", true);
    return;
  }
  const judge = $("councilJudgeSelect")?.value;
  if (!judge) {
    toast("Please select a Chief Synthesizer", true);
    return;
  }
  
  const runBtn = $("btnCouncilRun");
  const statusNotice = $("councilStatusNotice");
  const statusText = $("councilStatusText");
  const resultsArea = $("councilResultsArea");
  
  if (runBtn) runBtn.disabled = true;
  if (statusNotice) statusNotice.classList.remove("hidden");
  if (resultsArea) resultsArea.classList.add("hidden");
  
  let councilTimer = null;
  let elapsed = 0;
  if (statusText) statusText.innerHTML = `<strong>Deliberating:</strong> Jurors analyzing topic independently (${councilExecMode})… <span class="muted" style="font-size:0.75rem;">(0s)</span>`;
  councilTimer = setInterval(() => {
    elapsed += 1;
    if (statusText) {
      const phase = elapsed < 15 ? "Jurors reviewing prompt & evidence..." : (elapsed < 35 ? "Chief Synthesizer cross-examining arguments..." : "Formulating consensus report & executive summary...");
      statusText.innerHTML = `<strong>Deliberating:</strong> ${phase} <span class="muted" style="font-size:0.75rem;">(${elapsed}s)</span>`;
    }
  }, 1000);
  
  try {
    const res = await api("/api/arena/council/run", {
      method: "POST",
      body: JSON.stringify({
        topic,
        jurors: councilJurors,
        judge,
        mode: councilExecMode
      })
    });
    renderCouncilSession(res);
    toast("Council deliberation complete! 🏛️");
  } catch (e) {
    toast(`Council error: ${e.message}`, true);
  } finally {
    if (councilTimer) clearInterval(councilTimer);
    if (runBtn) runBtn.disabled = false;
    if (statusNotice) statusNotice.classList.add("hidden");
  }
}

function renderCouncilSession(session) {
  if (session && session.session) session = session.session;
  const resultsArea = $("councilResultsArea");
  if (!resultsArea) return;
  resultsArea.classList.remove("hidden");
  
  const jurorsList = session.jurors || session.rounds || [];
  const grid = $("councilJurorsGrid");
  if (grid) {
    grid.innerHTML = jurorsList.map(j => {
      const modelName = j.model || j.model_name || "Juror";
      const durSec = j.duration_ms ? (j.duration_ms / 1000).toFixed(1) + "s" : "";
      const tps = j.tokens_per_sec ? `${j.tokens_per_sec.toFixed(1)} t/s` : "";
      const text = j.perspective || j.response_text || "";
      return `<div class="council-juror-card">
        <div class="juror-card-header">
          <div class="juror-title">
            <span class="botdot leaf"></span>
            <strong>${esc(modelName)}</strong>
          </div>
          <div class="juror-meta-chips">
            ${durSec ? `<span class="lab-metric-chip">${durSec}</span>` : ''}
            ${tps ? `<span class="lab-metric-chip highlight">${tps}</span>` : ''}
          </div>
        </div>
        <div class="juror-perspective-body markdown-body">
          ${j.error ? `<div class="text-danger" style="color:var(--rose);">⚠️ ${esc(j.error)}</div>` : md(text)}
        </div>
      </div>`;
    }).join("");
  }
  
  const synthCard = $("councilSynthesizerCard");
  if (synthCard) {
    const s = session.synthesis || session.verdict_json || {};
    const durSec = s.duration_ms ? (s.duration_ms / 1000).toFixed(1) + "s" : "";
    const tps = s.tokens_per_sec ? `${s.tokens_per_sec.toFixed(1)} t/s` : "";
    const rawSynthesis = session.verdict_text || s.response_text || s.raw || (s.verdict ? `### Consensus\n${s.consensus}\n\n### Divergence\n${s.divergence}\n\n### Executive Verdict\n${s.verdict}` : "");
    const judgeName = s.judge || session.judge_model || session.judge || s.model_name || "Chief Synthesizer";
    
    synthCard.innerHTML = `
      <div class="synth-card-header">
        <div class="synth-title">
          <span class="botdot amber"></span>
          <strong>Chief Synthesizer: ${esc(judgeName)}</strong>
        </div>
        <div class="juror-meta-chips">
          ${durSec ? `<span class="lab-metric-chip">${durSec}</span>` : ''}
          ${tps ? `<span class="lab-metric-chip highlight">${tps}</span>` : ''}
          <button class="synth-copy-btn" type="button" onclick="copyCouncilReport(this)">Copy Synthesis 📋</button>
        </div>
      </div>
      <div class="synth-body markdown-body">
        ${s.consensus ? `<div class="synth-section"><h5>🤝 Common Consensus</h5><div>${md(s.consensus)}</div></div>` : ''}
        ${s.divergence ? `<div class="synth-section"><h5>⚡ Points of Divergence</h5><div>${md(s.divergence)}</div></div>` : ''}
        ${s.verdict ? `<div class="synth-section verdict-section"><h5>⚖️ Executive Verdict & Recommendation</h5><div>${md(s.verdict)}</div></div>` : ''}
        ${(!s.consensus && !s.divergence && !s.verdict) ? md(rawSynthesis) : ''}
      </div>
    `;
    synthCard.dataset.raw = rawSynthesis;
  }
  
  resultsArea.scrollIntoView({ behavior: "smooth", block: "start" });
}

window.copyCouncilReport = function(btn) {
  const card = btn.closest(".council-synthesizer-card");
  const raw = card ? card.dataset.raw : "";
  if (raw) {
    navigator.clipboard.writeText(raw).then(() => {
      btn.textContent = "Copied! ✓";
      setTimeout(() => { btn.textContent = "Copy Synthesis 📋"; }, 2000);
    });
  }
};

/* --- AI Debate --- */

async function initDebate() {
  const j = await api("/api/models").catch(() => null);
  const models = (j && j.models) ? j.models : [];
  if (!models.length) return;
  
  const opts = models.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
  
  const selA = $("debateModelASelect");
  const selB = $("debateModelBSelect");
  const selJudge = $("debateJudgeSelect");
  
  if (selA && selA.options.length === 0) {
    selA.innerHTML = opts;
    selA.value = models[0].name;
  }
  if (selB && selB.options.length === 0) {
    selB.innerHTML = opts;
    selB.value = (models[1] || models[0]).name;
  }
  if (selJudge && selJudge.options.length === 0) {
    selJudge.innerHTML = opts;
    selJudge.value = (models[2] || models[0]).name;
  }
}

async function runDebate() {
  const topic = $("debateTopic")?.value.trim();
  if (!topic) {
    toast("Please enter a debate proposition or motion", true);
    return;
  }
  const modelA = $("debateModelASelect")?.value;
  const modelB = $("debateModelBSelect")?.value;
  const judge = $("debateJudgeSelect")?.value;
  const rounds = parseInt($("debateRoundsSelect")?.value || "2", 10);
  
  if (!modelA || !modelB || !judge) {
    toast("Please select both debating models and an arbiter", true);
    return;
  }
  
  const runBtn = $("btnDebateRun");
  const statusNotice = $("debateStatusNotice");
  const statusText = $("debateStatusText");
  const resultsArea = $("debateResultsArea");
  
  if (runBtn) runBtn.disabled = true;
  if (statusNotice) statusNotice.classList.remove("hidden");
  if (resultsArea) resultsArea.classList.add("hidden");
  
  let debateTimer = null;
  let elapsed = 0;
  if (statusText) statusText.innerHTML = `<strong>Debate underway:</strong> ${rounds} round(s) with cross-examination… <span class="muted" style="font-size:0.75rem;">(0s)</span>`;
  debateTimer = setInterval(() => {
    elapsed += 1;
    if (statusText) {
      const phase = elapsed < 12 ? `${modelA} delivering opening argument…` : (elapsed < 25 ? `${modelB} formulating counter-rebuttal…` : "Arbiter judge evaluating rhetorical strength & scoring…");
      statusText.innerHTML = `<strong>Debate underway:</strong> ${phase} <span class="muted" style="font-size:0.75rem;">(${elapsed}s)</span>`;
    }
  }, 1000);
  
  try {
    const res = await api("/api/arena/debate/run", {
      method: "POST",
      body: JSON.stringify({
        topic,
        model_a: modelA,
        model_b: modelB,
        judge,
        rounds
      })
    });
    renderDebateSession(res);
    toast("Adversarial debate concluded! 🥊");
  } catch (e) {
    toast(`Debate error: ${e.message}`, true);
  } finally {
    if (debateTimer) clearInterval(debateTimer);
    if (runBtn) runBtn.disabled = false;
    if (statusNotice) statusNotice.classList.add("hidden");
  }
}

function renderDebateSession(session) {
  if (session && session.session) session = session.session;
  const resultsArea = $("debateResultsArea");
  if (!resultsArea) return;
  resultsArea.classList.remove("hidden");
  
  const modelAName = session.models?.[0] || session.model_a || "Affirmative";
  const modelBName = session.models?.[1] || session.model_b || "Negative";
  const judgeName = session.judge_model || session.judge || "Grand Arbiter";

  const timeline = $("debateRoundsTimeline");
  if (timeline) {
    timeline.innerHTML = (session.rounds || []).map(r => {
      let speechesList = r.speeches;
      if (!speechesList) {
        speechesList = [];
        if (r.affirmative) {
          speechesList.push({
            speaker: "affirmative",
            model: modelAName,
            text: r.affirmative.response_text || "",
            duration_ms: r.affirmative.duration_ms,
            tokens_per_sec: r.affirmative.tokens_per_sec
          });
        }
        if (r.negative) {
          speechesList.push({
            speaker: "negative",
            model: modelBName,
            text: r.negative.response_text || "",
            duration_ms: r.negative.duration_ms,
            tokens_per_sec: r.negative.tokens_per_sec
          });
        }
      }

      const speechesHtml = speechesList.map(sp => {
        const isAff = sp.speaker === "affirmative";
        const sideCls = isAff ? "speech-aff" : "speech-neg";
        const sideBadge = isAff ? `<span class="debate-side-badge aff">Affirmative (Pro)</span>` : `<span class="debate-side-badge neg">Negative (Con)</span>`;
        const durSec = sp.duration_ms ? (sp.duration_ms / 1000).toFixed(1) + "s" : "";
        const tps = sp.tokens_per_sec ? `${sp.tokens_per_sec.toFixed(1)} t/s` : "";
        return `
          <div class="debate-speech-box ${sideCls}">
            <div class="speech-header">
              <div class="speech-author">
                <span class="botdot ${isAff ? 'leaf' : 'rose'}"></span>
                <strong>${esc(sp.model)}</strong>
                ${sideBadge}
              </div>
              <div class="speech-metrics">
                ${durSec ? `<span class="lab-metric-chip">${durSec}</span>` : ''}
                ${tps ? `<span class="lab-metric-chip highlight">${tps}</span>` : ''}
              </div>
            </div>
            <div class="speech-body markdown-body">
              ${md(sp.text || "")}
            </div>
          </div>
        `;
      }).join("");
      
      return `
        <div class="debate-round-block">
          <div class="round-title-divider">
            <span>Round ${r.round_num || r.round}: ${esc(r.round_name || r.name || 'Debate Round')}</span>
          </div>
          <div class="round-speeches-grid">
            ${speechesHtml}
          </div>
        </div>
      `;
    }).join("");
  }
  
  const verdictCard = $("debateVerdictCard");
  if (verdictCard) {
    const v = session.verdict || session.verdict_json || {};
    const rawVerdict = session.verdict_text || v.response_text || v.analysis || v.raw || "";
    const winner = v.winner || (rawVerdict.toLowerCase().includes("winner: affirmative") ? "affirmative" : (rawVerdict.toLowerCase().includes("winner: negative") ? "negative" : "draw"));
    const winnerTitle = winner === "affirmative" ? `🏆 Affirmative Victory: ${esc(modelAName)}`
      : (winner === "negative" ? `🏆 Negative Victory: ${esc(modelBName)}` : `⚖️ Arbiter Official Verdict`);
    const sc = v.scorecard || {};
    const scAff = sc.affirmative || {};
    const scNeg = sc.negative || {};
    
    verdictCard.innerHTML = `
      <div class="verdict-banner ${winner}">
        <span class="verdict-banner-title">${winnerTitle}</span>
        <button class="synth-copy-btn" type="button" onclick="copyDebateVerdict(this)">Copy Verdict 📋</button>
      </div>
      
      ${(scAff.total !== undefined || scNeg.total !== undefined) ? `
      <div class="debate-scorecard-grid">
        <div class="scorecard-column aff">
          <div class="scorecard-head">Affirmative (${esc(modelAName)})</div>
          <div class="scorecard-row"><span>Opening:</span> <strong>${scAff.opening ?? '—'}</strong></div>
          <div class="scorecard-row"><span>Rebuttals:</span> <strong>${scAff.rebuttals ?? '—'}</strong></div>
          <div class="scorecard-row"><span>Evidence:</span> <strong>${scAff.evidence ?? '—'}</strong></div>
          <div class="scorecard-row"><span>Persuasion:</span> <strong>${scAff.persuasion ?? '—'}</strong></div>
          <div class="scorecard-row total"><span>Total Score:</span> <strong>${scAff.total ?? '—'}/100</strong></div>
        </div>
        <div class="scorecard-column neg">
          <div class="scorecard-head">Negative (${esc(modelBName)})</div>
          <div class="scorecard-row"><span>Opening:</span> <strong>${scNeg.opening ?? '—'}</strong></div>
          <div class="scorecard-row"><span>Rebuttals:</span> <strong>${scNeg.rebuttals ?? '—'}</strong></div>
          <div class="scorecard-row"><span>Evidence:</span> <strong>${scNeg.evidence ?? '—'}</strong></div>
          <div class="scorecard-row"><span>Persuasion:</span> <strong>${scNeg.persuasion ?? '—'}</strong></div>
          <div class="scorecard-row total"><span>Total Score:</span> <strong>${scNeg.total ?? '—'}/100</strong></div>
        </div>
      </div>
      ` : ''}
      
      <div class="verdict-analysis-body markdown-body">
        <h5>Arbiter Analysis &amp; Breakdown (${esc(judgeName)})</h5>
        <div>${md(rawVerdict)}</div>
      </div>
    `;
    verdictCard.dataset.raw = rawVerdict;
  }
  
  resultsArea.scrollIntoView({ behavior: "smooth", block: "start" });
}

window.copyDebateVerdict = function(btn) {
  const card = btn.closest(".debate-verdict-card");
  const raw = card ? card.dataset.raw : "";
  if (raw) {
    navigator.clipboard.writeText(raw).then(() => {
      btn.textContent = "Copied! ✓";
      setTimeout(() => { btn.textContent = "Copy Verdict 📋"; }, 2000);
    });
  }
};

/* --- Arena History Drawer --- */
let arenaHistoryFilter = "all";

function openArenaHistory() {
  const drawer = $("arenaHistoryDrawer");
  if (!drawer) return;
  drawer.classList.remove("hidden");
  loadArenaHistory(arenaHistoryFilter);
}

async function loadArenaHistory(mode = "all") {
  const list = $("arenaHistoryList");
  if (!list) return;
  list.innerHTML = `<div class="muted" style="padding:16px;text-align:center;">Loading arena history…</div>`;
  try {
    const res = await api(`/api/arena/sessions?mode=${mode}`);
    const sessions = res.sessions || [];
    if (!sessions.length) {
      list.innerHTML = `<div class="muted" style="padding:24px;text-align:center;">No past ${mode === "all" ? "Arena" : mode} sessions found.</div>`;
      return;
    }
    list.innerHTML = sessions.map(s => {
      const isCouncil = s.session_type === "council";
      const icon = isCouncil ? "🏛️ Council" : "🥊 Debate";
      const badgeCls = isCouncil ? "leaf" : "rose";
      const created = timeAgo(s.created_at);
      const title = s.topic || "Untitled Session";
      return `
        <div class="lab-history-item" data-id="${esc(s.id)}" data-type="${esc(s.session_type)}">
          <div class="lab-history-item-top">
            <span class="lab-history-suite-badge ${badgeCls}">${icon}</span>
            <span class="lab-history-date">${created}</span>
            <button class="lab-history-delete-btn" title="Delete session" onclick="deleteArenaSession(event, '${esc(s.id)}')">✕</button>
          </div>
          <div class="lab-history-prompt">${esc(title)}</div>
        </div>
      `;
    }).join("");
    
    list.querySelectorAll(".lab-history-item").forEach(item => {
      item.onclick = async (e) => {
        if (e.target.closest(".lab-history-delete-btn")) return;
        const sid = item.dataset.id;
        const stype = item.dataset.type;
        try {
          const session = await api(`/api/arena/sessions/${sid}`);
          $("arenaHistoryDrawer").classList.add("hidden");
          if (stype === "council") {
            setArenaTab("council");
            if ($("councilTopic")) $("councilTopic").value = session.topic || "";
            renderCouncilSession(session);
          } else if (stype === "debate") {
            setArenaTab("debate");
            if ($("debateTopic")) $("debateTopic").value = session.topic || "";
            renderDebateSession(session);
          }
        } catch (err) {
          toast(`Failed to load session: ${err.message}`, true);
        }
      };
    });
  } catch (e) {
    list.innerHTML = `<div class="text-danger" style="padding:16px;">Failed to load history: ${esc(e.message)}</div>`;
  }
}

window.deleteArenaSession = async function(event, sid) {
  event.stopPropagation();
  if (!confirm("Delete this arena session?")) return;
  try {
    await api(`/api/arena/sessions/${sid}`, { method: "DELETE" });
    toast("Session deleted");
    loadArenaHistory(arenaHistoryFilter);
  } catch (e) {
    toast(`Delete failed: ${e.message}`, true);
  }
};

/* --- Arena Tab Switching & Event Wiring --- */
let arenaActiveTab = "chess";

function setArenaTab(tab) {
  arenaActiveTab = tab;
  $("btnArenaTabChess")?.classList.toggle("on", tab === "chess");
  $("btnArenaTabCouncil")?.classList.toggle("on", tab === "council");
  $("btnArenaTabDebate")?.classList.toggle("on", tab === "debate");
  
  $("arenaPaneChess")?.classList.toggle("hidden", tab !== "chess");
  $("arenaPaneCouncil")?.classList.toggle("hidden", tab !== "council");
  $("arenaPaneDebate")?.classList.toggle("hidden", tab !== "debate");
  
  if (tab === "chess") {
    chessInit();
  } else if (tab === "council") {
    initCouncil();
  } else if (tab === "debate") {
    initDebate();
  }
}

function wireArena() {
  $("btnArenaTabChess")?.addEventListener("click", () => setArenaTab("chess"));
  $("btnArenaTabCouncil")?.addEventListener("click", () => setArenaTab("council"));
  $("btnArenaTabDebate")?.addEventListener("click", () => setArenaTab("debate"));
  $("btnArenaClose")?.addEventListener("click", () => setMainView("chat"));
  $("btnArenaHistory")?.addEventListener("click", () => openArenaHistory());
  $("btnCloseArenaHistory")?.addEventListener("click", () => $("arenaHistoryDrawer")?.classList.add("hidden"));
  
  // Chess Scrubber buttons
  $("btnScrubStart")?.addEventListener("click", () => scrubToMove(0));
  $("btnScrubPrev")?.addEventListener("click", () => {
    const total = (currentChessState?.hist_details || []).length;
    if (total === 0) return;
    const target = scrubStep === -1 ? total - 2 : scrubStep - 1;
    if (target >= 0) scrubToMove(target);
  });
  $("btnScrubNext")?.addEventListener("click", () => {
    const total = (currentChessState?.hist_details || []).length;
    if (total === 0 || scrubStep === -1) return;
    if (scrubStep + 1 >= total) scrubToLive();
    else scrubToMove(scrubStep + 1);
  });
  $("btnScrubEnd")?.addEventListener("click", () => scrubToLive());
  
  // Click on move in moves table
  $("chessMoves")?.addEventListener("click", (e) => {
    const span = e.target.closest("[data-step]");
    if (span && span.dataset.step !== undefined) {
      scrubToMove(parseInt(span.dataset.step, 10));
    }
  });
  
  // Stockfish deep analysis card
  $("btnStockfishDeep")?.addEventListener("click", async () => {
    const card = $("stockfishDeepCard");
    if (!card) return;
    card.classList.remove("hidden");
    $("deepPvLine").textContent = "Running Stockfish 17 analysis at depth 16…";
    $("deepBestMove").textContent = "Best: …";
    try {
      const res = await api("/api/chess/analyze?depth=16");
      $("deepDepthBadge").textContent = `Depth ${res.depth || 16}`;
      $("deepWinPct").textContent = `Win (White): ${res.win_pct_white !== undefined ? res.win_pct_white : 50}%`;
      let evalStr = "Eval: ";
      if (res.mate !== null && res.mate !== undefined) {
        evalStr += res.mate > 0 ? `Mate in ${res.mate}` : `Mate in ${-res.mate} (Black)`;
      } else if (res.cp !== null && res.cp !== undefined) {
        evalStr += (res.cp >= 0 ? "+" : "") + (res.cp / 100).toFixed(2);
      } else {
        evalStr += "0.00";
      }
      const bm = (typeof res.best_move === "object" && res.best_move !== null) ? (res.best_move.san || res.best_move.uci) : (res.best_move || "—");
      const pvLine = Array.isArray(res.pv) ? res.pv.join(" ") : (res.pv || "—");
      $("deepBestMove").textContent = `Best: ${bm}`;
      $("deepPvLine").textContent = `Principal line: ${pvLine}`;
    } catch (e) {
      $("deepPvLine").textContent = `Analysis error: ${e.message}`;
    }
  });
  
  $("btnCloseDeepCard")?.addEventListener("click", () => {
    $("stockfishDeepCard")?.classList.add("hidden");
  });
  
  // Chess PGN export
  $("btnChessExportPgn")?.addEventListener("click", async () => {
    try {
      const res = await api("/api/chess/pgn");
      if (!res || !res.pgn) {
        toast("No moves to export", true);
        return;
      }
      const blob = new Blob([res.pgn], { type: "application/x-chess-pgn" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `lamplight_chess_${Date.now()}.pgn`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast("PGN exported successfully! ♟️");
    } catch (e) {
      toast(`Failed to export PGN: ${e.message}`, true);
    }
  });
  
  // Council Presets
  document.querySelectorAll(".council-preset-btn").forEach(btn => {
    btn.onclick = () => {
      const t = $("councilTopic");
      if (t) t.value = btn.dataset.topic || "";
    };
  });
  
  $("btnCouncilExecSeq")?.addEventListener("click", () => {
    councilExecMode = "sequential";
    $("btnCouncilExecSeq").classList.add("on");
    $("btnCouncilExecPar").classList.remove("on");
  });
  $("btnCouncilExecPar")?.addEventListener("click", () => {
    councilExecMode = "parallel";
    $("btnCouncilExecPar").classList.add("on");
    $("btnCouncilExecSeq").classList.remove("on");
  });
  
  $("btnCouncilRun")?.addEventListener("click", () => runCouncil());
  
  // Debate Presets
  document.querySelectorAll(".debate-preset-pill").forEach(btn => {
    btn.onclick = () => {
      const t = $("debateTopic");
      if (t) t.value = btn.dataset.motion || "";
    };
  });
  
  $("btnDebateRun")?.addEventListener("click", () => runDebate());
  
  // History Drawer Filters
  $("btnFilterArenaAll")?.addEventListener("click", () => {
    arenaHistoryFilter = "all";
    $("btnFilterArenaAll").classList.add("on");
    $("btnFilterArenaCouncil").classList.remove("on");
    $("btnFilterArenaDebate").classList.remove("on");
    loadArenaHistory("all");
  });
  $("btnFilterArenaCouncil")?.addEventListener("click", () => {
    arenaHistoryFilter = "council";
    $("btnFilterArenaCouncil").classList.add("on");
    $("btnFilterArenaAll").classList.remove("on");
    $("btnFilterArenaDebate").classList.remove("on");
    loadArenaHistory("council");
  });
  $("btnFilterArenaDebate")?.addEventListener("click", () => {
    arenaHistoryFilter = "debate";
    $("btnFilterArenaDebate").classList.add("on");
    $("btnFilterArenaAll").classList.remove("on");
    $("btnFilterArenaCouncil").classList.remove("on");
    loadArenaHistory("debate");
  });
}

/* ---------- Skills & Slash Commands Engine ---------- */

async function loadSkills() {
  try {
    const j = await api("/api/skills");
    allSkills = j.skills || [];
    return allSkills;
  } catch (e) {
    console.error("Failed to load skills", e);
    return [];
  }
}

function checkSlashTrigger() {
  const m = $("msg");
  if (!m) return;
  const val = m.value;
  const pos = m.selectionStart || val.length;
  const textBefore = val.slice(0, pos);
  const match = textBefore.match(/(?:^|\n)\/([a-zA-Z0-9_-]*)$/);
  if (match) {
    const query = match[1].toLowerCase();
    showSlashMenu(query);
  } else {
    hideSlashMenu();
  }
}

function showSlashMenu(query = "") {
  if (!allSkills || !allSkills.length) {
    loadSkills().then(() => showSlashMenu(query));
    return;
  }
  filteredSkills = allSkills.filter(s => {
    if (s.mode !== "all" && s.mode !== mode) return false;
    if (!query) return true;
    return s.command.toLowerCase().includes(query) ||
           s.name.toLowerCase().includes(query) ||
           (s.description || "").toLowerCase().includes(query);
  });

  const badge = $("slashFilterBadge");
  if (badge) {
    if (query) {
      badge.textContent = `/${query}`;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }

  const list = $("slashList");
  if (!list) return;
  list.innerHTML = "";
  if (!filteredSkills.length) {
    list.innerHTML = `<div class="slash-item" style="cursor:default;opacity:.65;padding:12px 14px"><span class="slash-item-desc">No skill matches "/${esc(query)}". Click "New Skill" to create one.</span></div>`;
    slashSelectedIndex = -1;
  } else {
    slashSelectedIndex = 0;
    filteredSkills.forEach((s, idx) => {
      const item = document.createElement("div");
      item.className = "slash-item" + (idx === 0 ? " selected" : "");
      item.innerHTML = `
        <div class="slash-item-icon">${s.icon || "⚡"}</div>
        <div class="slash-item-main">
          <div class="slash-item-top">
            <span class="slash-item-title">${esc(s.name)}</span>
            <span class="slash-item-cmd">/${esc(s.command)}</span>
          </div>
          <span class="slash-item-desc">${esc(s.description || "")}</span>
        </div>
        <span class="slash-item-arrow">›</span>
      `;
      item.onmouseenter = () => {
        slashSelectedIndex = idx;
        renderSlashSelection();
      };
      item.onmousedown = (e) => {
        e.preventDefault();
        selectSkill(s);
      };
      list.appendChild(item);
    });
  }
  $("slashMenu").classList.remove("hidden");
}

function renderSlashSelection() {
  const items = $("slashList").querySelectorAll(".slash-item");
  items.forEach((it, i) => it.classList.toggle("selected", i === slashSelectedIndex));
  if (items[slashSelectedIndex]) {
    items[slashSelectedIndex].scrollIntoView({ block: "nearest" });
  }
}

function hideSlashMenu() {
  const m = $("slashMenu");
  if (m) m.classList.add("hidden");
  slashSelectedIndex = -1;
}

function selectSkill(s) {
  hideSlashMenu();
  const m = $("msg");
  if (m) {
    const val = m.value;
    m.value = val.replace(/(?:^|\n)\/[a-zA-Z0-9_-]*\s*$/, "").trim();
    autosize();
  }

  if (s.command === "dictate") {
    startRecording();
    return;
  }

  activeSkill = s;
  $("activeSkillIcon").textContent = s.icon || "⚡";
  $("activeSkillCmd").textContent = `/${s.command}`;
  $("activeSkillName").textContent = s.name;
  $("activeSkillBar").classList.remove("hidden");
  if (m) m.focus();
  toast(`Activated skill /${s.command}`);
}

window.toggleSkillPrompt = function(btn) {
  const card = btn.closest(".skill-card");
  const p = card.querySelector(".skill-card-prompt");
  if (!p) return;
  const open = p.classList.toggle("open");
  btn.textContent = open ? "Hide Directive" : "View Directive";
};

window.useSkillFromDrawer = function(id) {
  const s = allSkills.find(x => x.id === id);
  if (!s) return;
  selectSkill(s);
  closeDrawers();
};

window.editSkill = function(id) {
  const s = allSkills.find(x => x.id === id);
  if (!s) return;
  $("skillFormTitle").textContent = "Edit Custom Skill";
  $("editSkillId").value = s.id;
  $("skillCmd").value = s.command;
  $("skillIcon").value = s.icon || "⚡";
  $("skillName").value = s.name;
  $("skillMode").value = s.mode || "all";
  $("skillDesc").value = s.description || "";
  $("skillPrompt").value = s.prompt || "";
  $("btnSaveSkill").textContent = "Update Skill";
  $("skillFormCard").classList.remove("hidden");
  $("skillCmd").focus();
};

window.deleteSkillPrompt = async function(id) {
  const s = allSkills.find(x => x.id === id);
  if (!s) return;
  const ok = await askConfirm(`Delete custom skill "/${s.command}"?`);
  if (!ok) return;
  try {
    await api(`/api/skills/${id}`, { method: "DELETE" });
    toast(`Deleted /${s.command}`);
    await loadSkills();
    renderSkillsList(document.querySelector(".skill-tab-btn.on")?.dataset?.filter || "all");
  } catch (e) {
    toast(e.message, true);
  }
};

function renderSkillsList(filter = "all") {
  const container = $("skillsList");
  if (!container) return;
  container.innerHTML = "";

  const list = allSkills.filter(s => {
    if (filter === "builtin") return s.is_builtin === 1;
    if (filter === "custom") return s.is_builtin !== 1;
    return true;
  });

  if (!list.length) {
    container.innerHTML = `<div class="session-empty">${filter === "custom" ? "No custom skills yet. Click 'Create Custom Skill' above to make one!" : "No skills found."}</div>`;
    return;
  }

  list.forEach(s => {
    const card = document.createElement("div");
    card.className = "skill-card";
    const modeLabel = s.mode === "friend" ? "Friend Only" : s.mode === "nsfw" ? "After Dark Only" : s.mode === "architect" ? "Architect Only" : s.mode === "muse" ? "Muse Only" : "Global";
    card.innerHTML = `
      <div class="skill-card-top">
        <div class="skill-card-title-wrap">
          <span class="skill-card-icon">${s.icon || "⚡"}</span>
          <span class="skill-card-name">${esc(s.name)}</span>
          <span class="skill-card-cmd">/${esc(s.command)}</span>
        </div>
        <div class="skill-card-badges">
          <span class="skill-badge ${s.is_builtin ? "builtin" : "custom"}">${s.is_builtin ? "Built-in" : "Custom"}</span>
          <span class="skill-badge">${esc(modeLabel)}</span>
        </div>
      </div>
      <div class="skill-card-desc">${esc(s.description || "")}</div>
      <div class="skill-card-prompt">${esc(s.prompt || "")}</div>
      <div class="skill-card-actions">
        <button class="skill-action-btn primary" type="button" onclick="useSkillFromDrawer('${esc(s.id)}')">Use in Chat</button>
        <button class="skill-action-btn" type="button" onclick="toggleSkillPrompt(this)">View Directive</button>
        ${!s.is_builtin ? `
          <button class="skill-action-btn" type="button" onclick="editSkill('${esc(s.id)}')">Edit</button>
          <button class="skill-action-btn danger" type="button" onclick="deleteSkillPrompt('${esc(s.id)}')">Delete</button>
        ` : ""}
      </div>
    `;
    container.appendChild(card);
  });
}

function wireSkills() {
  const btnSlash = $("btnSlash");
  if (btnSlash) {
    btnSlash.onclick = () => {
      const menu = $("slashMenu");
      if (menu && !menu.classList.contains("hidden")) {
        hideSlashMenu();
      } else {
        const m = $("msg");
        if (m) {
          if (!m.value.startsWith("/")) {
            m.value = "/" + m.value;
          }
          autosize();
          m.focus();
        }
        showSlashMenu("");
      }
    };
  }

  const btnClear = $("btnClearSkill");
  if (btnClear) {
    btnClear.onclick = () => {
      activeSkill = null;
      $("activeSkillBar").classList.add("hidden");
      const m = $("msg");
      if (m) m.focus();
    };
  }

  const btnOpenSlash = $("btnOpenSkillFromSlash");
  if (btnOpenSlash) {
    btnOpenSlash.onclick = () => {
      hideSlashMenu();
      openDrawer("skillsPanel");
      loadSkills().then(() => {
        renderSkillsList();
        showSkillForm();
      });
    };
  }

  const btnToggle = $("btnToggleNewSkill");
  if (btnToggle) {
    btnToggle.onclick = () => showSkillForm();
  }

  const btnCancel = $("btnCancelSkillForm");
  if (btnCancel) btnCancel.onclick = () => $("skillFormCard").classList.add("hidden");
  const btnCancel2 = $("btnCancelSkillEdit");
  if (btnCancel2) btnCancel2.onclick = () => $("skillFormCard").classList.add("hidden");

  document.querySelectorAll(".skill-tab-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(".skill-tab-btn").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      renderSkillsList(b.dataset.filter || "all");
    };
  });

  const btnSave = $("btnSaveSkill");
  if (btnSave) {
    btnSave.onclick = async () => {
      const id = $("editSkillId").value.trim();
      const cmd = $("skillCmd").value.trim().replace(/^\//, "");
      const icon = $("skillIcon").value.trim() || "⚡";
      const name = $("skillName").value.trim() || cmd;
      const mode = $("skillMode").value;
      const desc = $("skillDesc").value.trim();
      const prompt = $("skillPrompt").value.trim();

      if (!cmd) { toast("Please enter a command trigger (e.g. eli5)", true); return; }
      if (!prompt) { toast("Please provide a prompt directive for this skill", true); return; }

      try {
        if (id) {
          await api(`/api/skills/${id}`, {
            method: "PUT",
            body: JSON.stringify({ command: cmd, icon, name, mode, description: desc, prompt })
          });
          toast(`Updated skill /${cmd} ✓`);
        } else {
          await api("/api/skills", {
            method: "POST",
            body: JSON.stringify({ command: cmd, icon, name, mode, description: desc, prompt })
          });
          toast(`Created skill /${cmd} ✓`);
        }
        $("skillFormCard").classList.add("hidden");
        await loadSkills();
        renderSkillsList(document.querySelector(".skill-tab-btn.on")?.dataset?.filter || "all");
      } catch (e) {
        toast(e.message, true);
      }
    };
  }
}

function showSkillForm() {
  $("skillFormTitle").textContent = "New Custom Skill";
  $("editSkillId").value = "";
  $("skillCmd").value = "";
  $("skillIcon").value = "⚡";
  $("skillName").value = "";
  $("skillMode").value = "all";
  $("skillDesc").value = "";
  $("skillPrompt").value = "";
  $("btnSaveSkill").textContent = "Save Skill";
  $("skillFormCard").classList.remove("hidden");
  $("skillCmd").focus();
}

/* ==========================================================================
   Top-Level View Manager (Chat Workspace, Chess Arena, Model Lab)
   ========================================================================== */
let labOn = false;

function setMainView(view) {
  const app = $("app");
  if (app) {
    app.classList.toggle("arena-mode", view === "chess");
    app.classList.toggle("lab-mode", view === "lab");
  }

  if (view === "lab") {
    labOn = true;
    chessOn = false;
    if (typeof chessAuto !== "undefined") {
      chessAuto = false;
      const ab = $("btnChessAuto");
      if (ab) ab.textContent = "Auto ▶";
    }
    const ws = $("workspace");
    if (ws) ws.classList.add("hidden");
    $("chatWrap")?.classList.add("hidden");
    $("composer")?.classList.add("hidden");
    $("chessView")?.classList.add("hidden");
    $("labView")?.classList.remove("hidden");
    $("btnLab")?.classList.add("active");
    if ($("btnChess")) $("btnChess").style.borderColor = "";
    initLab();
  } else if (view === "chess") {
    chessOn = true;
    labOn = false;
    const ws = $("workspace");
    if (ws) ws.classList.add("hidden");
    $("chatWrap")?.classList.add("hidden");
    $("composer")?.classList.add("hidden");
    $("labView")?.classList.add("hidden");
    $("chessView")?.classList.remove("hidden");
    if ($("btnChess")) $("btnChess").style.borderColor = "var(--amber)";
    $("btnLab")?.classList.remove("active");
    setArenaTab(arenaActiveTab || "chess");
  } else {
    // "chat" (default)
    labOn = false;
    chessOn = false;
    if (typeof chessAuto !== "undefined") {
      chessAuto = false;
      const ab = $("btnChessAuto");
      if (ab) ab.textContent = "Auto ▶";
    }
    const ws = $("workspace");
    if (ws) ws.classList.remove("hidden");
    $("chatWrap")?.classList.remove("hidden");
    $("composer")?.classList.remove("hidden");
    $("labView")?.classList.add("hidden");
    $("chessView")?.classList.add("hidden");
    if ($("btnChess")) $("btnChess").style.borderColor = "";
    $("btnLab")?.classList.remove("active");
  }
}

/* ==========================================================================
   Model Providers & Multi-Engine Management
   ========================================================================== */
async function loadProviders() {
  const list = $("providersList");
  if (!list) return;
  list.innerHTML = `<div class="muted" style="padding:8px 0;font-size:0.8rem;">Loading inference providers…</div>`;
  try {
    const res = await api("/api/providers");
    const provs = res.providers || [];
    if (!provs.length) {
      list.innerHTML = `<div class="muted" style="padding:8px 0;font-size:0.8rem;">No inference providers configured.</div>`;
      return;
    }
    list.innerHTML = provs.map(p => `
      <div class="provider-card ${p.is_active ? 'active' : ''}">
        <div class="provider-info">
          <div class="provider-name-row">
            <span class="provider-name">${esc(p.name)}</span>
            <span class="provider-type-badge">${p.provider_type === 'ollama' ? 'Ollama' : 'OpenAI Compat'}</span>
          </div>
          <div class="provider-url">${esc(p.base_url)}</div>
        </div>
        <div class="provider-actions">
          <button class="provider-toggle-btn ${p.is_active ? 'on' : ''}" data-id="${esc(p.id)}" data-active="${p.is_active}" type="button">
            ${p.is_active ? 'Active ✓' : 'Disabled ⊘'}
          </button>
          ${p.id !== 'p_ollama' ? `
            <button class="ghost btn-del-provider" data-id="${esc(p.id)}" type="button" title="Delete provider" style="padding:2px 6px;font-size:0.8rem;">🗑️</button>
          ` : ''}
        </div>
      </div>
    `).join("");

    list.querySelectorAll(".provider-toggle-btn").forEach(btn => {
      btn.onclick = async () => {
        const pid = btn.dataset.id;
        const cur = btn.dataset.active === "true" || btn.dataset.active === "1";
        try {
          await api(`/api/providers/${pid}`, {
            method: "PUT",
            body: JSON.stringify({ is_active: !cur })
          });
          toast(`Provider ${!cur ? 'enabled' : 'disabled'}`);
          await loadProviders();
          await refreshAllModelPickers();
        } catch (e) {
          toast(e.message, true);
        }
      };
    });

    list.querySelectorAll(".btn-del-provider").forEach(btn => {
      btn.onclick = async () => {
        const pid = btn.dataset.id;
        if (!await askConfirm("Delete this custom provider?")) return;
        try {
          await api(`/api/providers/${pid}`, { method: "DELETE" });
          toast("Provider removed");
          await loadProviders();
          await refreshAllModelPickers();
        } catch (e) {
          toast(e.message, true);
        }
      };
    });
  } catch (e) {
    list.innerHTML = `<div class="muted" style="color:var(--rose);font-size:0.8rem;">Error loading providers: ${esc(e.message)}</div>`;
  }
}

function wireProviderForm() {
  const btnOpen = $("btnOpenAddProvider");
  const form = $("addProviderForm");
  const btnCancel = $("btnCancelAddProvider");
  const btnSave = $("btnSaveProvider");

  if (btnOpen && form) {
    btnOpen.onclick = () => form.classList.remove("hidden");
  }
  if (btnCancel && form) {
    btnCancel.onclick = () => form.classList.add("hidden");
  }
  if (btnSave && form) {
    btnSave.onclick = async () => {
      const name = $("provName").value.trim();
      const type = $("provType").value;
      const url = $("provUrl").value.trim();
      const key = $("provKey").value.trim();

      if (!name) { toast("Please enter a provider name", true); return; }
      if (!url) { toast("Please enter the provider base URL", true); return; }

      try {
        await api("/api/providers", {
          method: "POST",
          body: JSON.stringify({
            name,
            provider_type: type,
            base_url: url,
            api_key: key || ""
          })
        });
        toast(`Added provider "${name}" ✓`);
        form.classList.add("hidden");
        $("provName").value = "";
        $("provUrl").value = "";
        $("provKey").value = "";
        await loadProviders();
        await refreshAllModelPickers();
      } catch (e) {
        toast("Failed to add provider: " + e.message, true);
      }
    };
  }
}

async function refreshAllModelPickers() {
  await loadPicker();
  if (labOn) await loadLabModels();
}

/* ==========================================================================
   Model Lab & Comparative Benchmarks Engine
   ========================================================================== */
const GREEK_NAMES = ["Alpha", "Beta", "Gamma", "Delta"];

let labState = {
  mode: "transparent", // "transparent" or "blind"
  exec: "sequential",  // "sequential" or "parallel"
  candidates: [],     // array of candidate model identifiers
  models: [],         // full models list from /api/models
  suites: [],         // suites list from /api/lab/suites
  activeSuite: null,  // selected suite
  currentExperiment: null,
  running: false
};

async function initLab() {
  await loadLabModels();
  await loadLabSuites();
  renderLabCandidatePickers();
}

async function loadLabModels() {
  try {
    const j = await api("/api/models");
    if (j && j.models) {
      labState.models = j.models;
      if (!labState.candidates.length) {
        if (j.models.length >= 2) {
          labState.candidates = [j.models[0].name, j.models[1].name];
        } else if (j.models.length === 1) {
          labState.candidates = [j.models[0].name];
        }
      }
    }
  } catch (e) {
    console.error("Failed to load models for Lab:", e);
  }
}

async function loadLabSuites() {
  try {
    const res = await api("/api/lab/suites");
    labState.suites = res.suites || [];
    renderLabSuitesMenu();
  } catch (e) {
    console.error("Failed to load suites:", e);
  }
}

function renderLabSuitesMenu() {
  const container = $("labSuitesList");
  if (!container) return;
  if (!labState.suites.length) {
    container.innerHTML = `<div class="muted" style="padding:10px;font-size:0.8rem;">No benchmark suites found</div>`;
    return;
  }
  container.innerHTML = labState.suites.map(s => `
    <div class="lab-suite-item" data-id="${esc(s.id)}">
      <div class="lab-suite-item-top">
        <span class="lab-suite-name">${esc(s.name || s.title)}</span>
        <span class="lab-suite-cat">${esc(s.category)}</span>
      </div>
      <div class="muted" style="font-size:0.72rem;line-height:1.3;margin-top:2px;">
        ${esc(s.description || s.prompt.slice(0, 90) + "…")}
      </div>
    </div>
  `).join("");

  container.querySelectorAll(".lab-suite-item").forEach(item => {
    item.onclick = () => {
      const suiteId = item.dataset.id;
      const suite = labState.suites.find(s => s.id === suiteId);
      if (suite) {
        labState.activeSuite = suite;
        $("labPrompt").value = suite.prompt;
        const badge = $("labActiveSuiteBadge");
        if (badge) {
          badge.textContent = suite.name || suite.title;
          badge.classList.remove("hidden");
        }
        $("labSuitesMenu").classList.add("hidden");
        toast(`Loaded benchmark suite: ${suite.name || suite.title}`);
      }
    };
  });
}

function renderLabCandidatePickers() {
  const container = $("labModelsPickers");
  if (!container) return;

  if (!labState.candidates.length && labState.models.length) {
    labState.candidates = [labState.models[0].name];
  }

  let html = labState.candidates.map((cand, idx) => `
    <div class="lab-model-picker-wrap">
      <span class="lab-slot-badge">#${idx + 1}</span>
      <select class="lab-model-select" data-index="${idx}">
        ${labState.models.map(m => `
          <option value="${esc(m.name)}" ${m.name === cand ? "selected" : ""}>
            ${esc(m.name)} (${(m.size / 1e9).toFixed(1)}GB)
          </option>
        `).join("")}
      </select>
      ${labState.candidates.length > 1 ? `
        <button class="lab-remove-model-btn" data-index="${idx}" title="Remove candidate" type="button">✕</button>
      ` : ""}
    </div>
  `).join("");

  if (labState.candidates.length < 4 && labState.models.length > labState.candidates.length) {
    html += `
      <button id="btnAddLabCandidate" class="lab-add-model-btn" type="button" title="Add another candidate model">
        + Add Model
      </button>
    `;
  }

  container.innerHTML = html;

  container.querySelectorAll(".lab-model-select").forEach(sel => {
    sel.onchange = () => {
      const idx = parseInt(sel.dataset.index, 10);
      labState.candidates[idx] = sel.value;
    };
  });

  container.querySelectorAll(".lab-remove-model-btn").forEach(btn => {
    btn.onclick = () => {
      const idx = parseInt(btn.dataset.index, 10);
      labState.candidates.splice(idx, 1);
      renderLabCandidatePickers();
    };
  });

  const addBtn = $("btnAddLabCandidate");
  if (addBtn) {
    addBtn.onclick = () => {
      if (labState.candidates.length >= 4) return;
      const unused = labState.models.find(m => !labState.candidates.includes(m.name));
      labState.candidates.push(unused ? unused.name : (labState.models[0]?.name || "llama3.2:3b"));
      renderLabCandidatePickers();
    };
  }
}

async function runLabBenchmark() {
  if (labState.running) return;
  const prompt = $("labPrompt").value.trim();
  if (!prompt) {
    toast("Please enter a benchmark prompt or choose a suite", true);
    $("labPrompt").focus();
    return;
  }
  if (!labState.candidates.length) {
    toast("Please select at least 1 candidate model", true);
    return;
  }

  labState.running = true;
  $("btnLabRun").disabled = true;
  $("labStatusNotice").classList.remove("hidden");
  const baseDesc = labState.exec === "sequential"
    ? `Running benchmark sequentially (VRAM-safe) across ${labState.candidates.length} models…`
    : `Streaming benchmark in parallel across ${labState.candidates.length} models…`;

  let labTimer = null;
  let elapsed = 0;
  $("labStatusText").innerHTML = `<strong>Benchmarking:</strong> ${baseDesc} <span class="muted" style="font-size:0.75rem;">(0s)</span>`;
  labTimer = setInterval(() => {
    elapsed += 1;
    if ($("labStatusText")) {
      $("labStatusText").innerHTML = `<strong>Benchmarking:</strong> ${baseDesc} <span class="muted" style="font-size:0.75rem;">(${elapsed}s elapsed)</span>`;
    }
  }, 1000);

  try {
    const temp = parseFloat($("labTemp").value) || 0.7;
    const isBlind = labState.mode === "blind";
    const res = await api("/api/lab/run", {
      method: "POST",
      body: JSON.stringify({
        models: labState.candidates,
        prompt: prompt,
        execution_mode: labState.exec,
        mode: labState.mode,
        temperature: temp,
        suite_name: labState.activeSuite ? (labState.activeSuite.name || labState.activeSuite.title) : "Custom"
      })
    });

    labState.currentExperiment = res;
    renderLabResults(res);
    toast(`Benchmark finished! Evaluated ${res.runs.length} models.`);
  } catch (e) {
    toast("Benchmark error: " + e.message, true);
  } finally {
    if (labTimer) clearInterval(labTimer);
    labState.running = false;
    $("btnLabRun").disabled = false;
    $("labStatusNotice").classList.add("hidden");
  }
}

function renderLabResults(exp) {
  const grid = $("labResultsGrid");
  if (!grid) return;
  if (!exp || !exp.runs || !exp.runs.length) {
    grid.innerHTML = `
      <div class="lab-empty-state">
        <span class="lab-empty-icon">⚖️</span>
        <h3>Ready to Benchmark</h3>
        <p>Select candidate models, type or pick a prompt, and hit <strong>Run Benchmark</strong> to compare responses, speed, and latency.</p>
      </div>`;
    return;
  }

  const count = Math.min(exp.runs.length, 4);
  grid.className = `lab-results-grid cols-${count}`;

  const isBlind = exp.mode === "blind" && !exp.revealed && !exp.winner_model;

  grid.innerHTML = exp.runs.map((run, idx) => {
    const isWinner = exp.winner_model && (exp.winner_model === run.model_name || run.user_voted);
    const greekName = GREEK_NAMES[idx] || `Model ${idx + 1}`;
    const displayName = isBlind ? (run.blind_label || `🎭 Model ${greekName}`) : esc(run.model_name);
    const provName = esc(run.provider_name || run.provider_id || "Local Ollama");
    const durSec = (run.duration_ms / 1000).toFixed(2);
    const subtitle = isBlind
      ? `<span class="lab-prov-badge blind">Masked Candidate</span>`
      : `<span class="lab-prov-badge">${provName}</span><span class="lab-time-badge">${durSec}s</span>`;

    const tpsVal = run.tokens_per_sec || run.tps || 0;
    const tps = tpsVal > 0 ? `${tpsVal} t/s` : "—";
    const ttft = run.ttft_ms > 0 ? `${run.ttft_ms}ms TTFT` : "—";
    const vram = run.vram_delta_mb ? `+${Math.round(run.vram_delta_mb)}MB VRAM` : null;
    const text = run.response_text || run.output_text || "";
    const tok = run.tokens_completion || run.completion_tokens || 0;

    return `
      <div class="lab-card ${isWinner ? 'winner' : ''}" data-model="${esc(run.model_name)}">
        <div class="lab-card-header">
          <div class="lab-card-title-wrap">
            <div class="lab-card-model-name">${displayName}</div>
            <div class="lab-card-model-sub">${subtitle}</div>
          </div>
          ${isWinner ? `<div class="lab-winner-badge">🏆 Winner</div>` : ""}
        </div>

        <div class="lab-card-telemetry">
          <span class="lab-metric-chip" title="Generation Speed"><svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg> ${tps}</span>
          <span class="lab-metric-chip" title="Time To First Token"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg> ${ttft}</span>
          <span class="lab-metric-chip" title="Total Generated Tokens"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg> ${tok} tok</span>
          ${vram ? `<span class="lab-metric-chip" title="Hardware VRAM Delta"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect><rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect><line x1="6" y1="6" x2="6.01" y2="6"></line><line x1="6" y1="18" x2="6.01" y2="18"></line></svg> ${vram}</span>` : ""}
        </div>

        <div class="lab-card-body">
          ${run.error_message
            ? `<div class="lab-card-error">❌ ${esc(run.error_message)}</div>`
            : md(text || "*(Empty response)*")
          }
        </div>

        <div class="lab-card-footer">
          ${isBlind ? `
            <button class="primary lab-vote-btn" data-model="${esc(run.model_name)}" type="button">
              🏆 Vote as Winner
            </button>
          ` : `
            <div class="lab-card-actions">
              <button class="ghost lab-copy-btn" type="button"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg> Copy</button>
            </div>
          `}
        </div>
      </div>
    `;
  }).join("");

  grid.querySelectorAll(".lab-vote-btn").forEach(btn => {
    btn.onclick = async () => {
      const modelName = btn.dataset.model;
      const expId = exp.experiment_id || exp.id;
      await submitLabVote(expId, modelName);
    };
  });

  grid.querySelectorAll(".lab-copy-btn").forEach(btn => {
    btn.onclick = () => {
      const card = btn.closest(".lab-card");
      const body = card.querySelector(".lab-card-body");
      const text = body ? body.innerText : "";
      navigator.clipboard.writeText(text).then(() => {
        btn.textContent = "Copied! ✓";
        setTimeout(() => btn.textContent = "📋 Copy", 2000);
      });
    };
  });
}

async function submitLabVote(experimentId, winnerModel) {
  try {
    const res = await api("/api/lab/vote", {
      method: "POST",
      body: JSON.stringify({
        experiment_id: experimentId,
        winner_model: winnerModel
      })
    });
    if (res && res.experiment) {
      labState.currentExperiment = res.experiment;
      labState.currentExperiment.revealed = true;
      labState.currentExperiment.mode = "transparent";
      renderLabResults(res.experiment);
    } else if (labState.currentExperiment) {
      labState.currentExperiment.winner_model = winnerModel;
      labState.currentExperiment.revealed = true;
      labState.currentExperiment.mode = "transparent";
      renderLabResults(labState.currentExperiment);
    }
    toast(`🏆 Vote recorded! Winner was ${winnerModel}. Model identities revealed!`);
  } catch (e) {
    toast("Voting failed: " + e.message, true);
  }
}

async function openLabHistory() {
  const drawer = $("labHistoryDrawer");
  if (!drawer) return;
  drawer.classList.remove("hidden");
  const list = $("labHistoryList");
  list.innerHTML = `<div class="muted" style="padding:16px;">Loading past experiments…</div>`;
  try {
    const res = await api("/api/lab/experiments");
    const data = res.experiments || [];
    if (!data.length) {
      list.innerHTML = `<div class="muted" style="padding:16px;">No past benchmark experiments found.</div>`;
      return;
    }
    list.innerHTML = data.map(exp => {
      const modelsCount = exp.runs_summary ? exp.runs_summary.length : (exp.runs ? exp.runs.length : 0);
      return `
        <div class="lab-history-card" data-id="${esc(exp.id)}">
          <div class="lab-history-top">
            <span class="lab-history-suite">${exp.mode === 'blind' ? 'Blind Arena 🎭' : 'Transparent Lab'}</span>
            <span class="lab-history-time">${timeAgo(exp.created_at)}</span>
          </div>
          <div class="lab-history-prompt">${esc(exp.prompt)}</div>
          <div class="muted" style="font-size:0.72rem;display:flex;justify-content:space-between;align-items:center;">
            <span>${modelsCount} models</span>
            ${exp.winner_model ? `<span class="lab-history-winner">🏆 ${esc(exp.winner_model)}</span>` : ''}
          </div>
        </div>
      `;
    }).join("");

    list.querySelectorAll(".lab-history-card").forEach(card => {
      card.onclick = async () => {
        const id = card.dataset.id;
        try {
          const full = await api(`/api/lab/experiments/${id}`);
          if (full && full.experiment) {
            labState.currentExperiment = full.experiment;
            renderLabResults(full.experiment);
            $("labPrompt").value = full.experiment.prompt;
            drawer.classList.add("hidden");
            toast("Loaded benchmark experiment from history");
          }
        } catch (err) {
          toast("Failed to load experiment: " + err.message, true);
        }
      };
    });
  } catch (e) {
    list.innerHTML = `<div class="lab-card-error" style="margin:16px;">Error: ${esc(e.message)}</div>`;
  }
}

function wireLab() {
  const btnTrans = $("btnLabModeTransparent");
  const btnBlind = $("btnLabModeBlind");
  if (btnTrans && btnBlind) {
    btnTrans.onclick = () => {
      labState.mode = "transparent";
      btnTrans.classList.add("on");
      btnBlind.classList.remove("on");
      if (labState.currentExperiment) renderLabResults(labState.currentExperiment);
    };
    btnBlind.onclick = () => {
      labState.mode = "blind";
      btnBlind.classList.add("on");
      btnTrans.classList.remove("on");
      if (labState.currentExperiment) renderLabResults(labState.currentExperiment);
    };
  }

  const btnSeq = $("btnLabExecSeq");
  const btnPar = $("btnLabExecPar");
  if (btnSeq && btnPar) {
    btnSeq.onclick = () => {
      labState.exec = "sequential";
      btnSeq.classList.add("on");
      btnPar.classList.remove("on");
    };
    btnPar.onclick = () => {
      labState.exec = "parallel";
      btnPar.classList.add("on");
      btnSeq.classList.remove("on");
    };
  }

  const btnSuites = $("btnLabSuites");
  const suitesMenu = $("labSuitesMenu");
  if (btnSuites && suitesMenu) {
    btnSuites.onclick = (e) => {
      e.stopPropagation();
      suitesMenu.classList.toggle("hidden");
    };
    document.addEventListener("click", (e) => {
      if (!suitesMenu.contains(e.target) && e.target !== btnSuites) {
        suitesMenu.classList.add("hidden");
      }
    });
  }

  const btnHistory = $("btnLabHistory");
  const btnCloseHistory = $("btnCloseLabHistory");
  if (btnHistory) btnHistory.onclick = () => openLabHistory();
  if (btnCloseHistory) btnCloseHistory.onclick = () => $("labHistoryDrawer").classList.add("hidden");

  const btnCloseLab = $("btnLabClose");
  if (btnCloseLab) btnCloseLab.onclick = () => setMainView("chat");

  const btnLab = $("btnLab");
  if (btnLab) btnLab.onclick = () => setMainView(labOn ? "chat" : "lab");

  const btnRun = $("btnLabRun");
  if (btnRun) btnRun.onclick = () => runLabBenchmark();

  const promptInput = $("labPrompt");
  if (promptInput) {
    promptInput.oninput = () => {
      if (labState.activeSuite && promptInput.value !== labState.activeSuite.prompt) {
        labState.activeSuite = null;
        const badge = $("labActiveSuiteBadge");
        if (badge) badge.classList.add("hidden");
      }
    };
  }

  const tempRange = $("labTempRange");
  const tempVal = $("labTempVal");
  const tempInput = $("labTemp");
  if (tempRange && tempVal && tempInput) {
    tempRange.oninput = () => {
      const v = parseFloat(tempRange.value).toFixed(2);
      tempVal.textContent = v;
      tempInput.value = tempRange.value;
    };
  }
}

/* ---------- Document Knowledge Hub Controller & Study Packs ---------- */
let docHubDocuments = [];

async function loadDocHub() {
  const listEl = $("docHubList");
  const countBadge = $("docHubCountBadge");
  if (!listEl) return;

  listEl.innerHTML = '<div class="muted" style="padding:24px;text-align:center;">Loading documents…</div>';

  try {
    const data = await api("/api/documents");
    docHubDocuments = Array.isArray(data) ? data : (data.documents || []);
    renderDocHubList();
    populateDocCompareSelects();
  } catch (err) {
    listEl.innerHTML = `<div class="error-banner" style="padding:16px;">Failed to load documents: ${esc(err.message)}</div>`;
  }
}

function renderDocHubList() {
  const listEl = $("docHubList");
  const countBadge = $("docHubCountBadge");
  const query = ($("docHubSearch")?.value || "").toLowerCase().trim();

  if (!listEl) return;

  const filtered = docHubDocuments.filter(d => {
    if (!query) return true;
    return (d.filename || "").toLowerCase().includes(query) || (d.summary || "").toLowerCase().includes(query);
  });

  if (countBadge) {
    countBadge.textContent = `${filtered.length} document${filtered.length === 1 ? "" : "s"}`;
  }

  if (filtered.length === 0) {
    listEl.innerHTML = query
      ? '<div class="muted" style="padding:24px;text-align:center;">No documents match your search.</div>'
      : '<div class="muted" style="padding:24px;text-align:center;">No documents uploaded yet. Upload a PDF, markdown, or text file to start!</div>';
    return;
  }

  listEl.innerHTML = filtered.map(doc => {
    const isPdf = (doc.filename || "").toLowerCase().endsWith(".pdf");
    const icon = isPdf ? "📕" : (doc.filename || "").endsWith(".md") ? "📝" : "📄";
    const pageInfo = doc.page_count ? ` · ${doc.page_count} page${doc.page_count === 1 ? "" : "s"}` : "";
    const sizeKb = Math.round((doc.file_size || 0) / 1024);

    return `
      <div class="doc-item" data-id="${doc.id}">
        <div class="doc-item-head">
          <div class="doc-icon">${icon}</div>
          <div class="doc-item-title-wrap">
            <div class="doc-item-filename" title="${esc(doc.filename)}">${esc(doc.filename)}</div>
            <div class="doc-item-meta">${sizeKb} KB${pageInfo} · Added ${timeAgo(doc.created_at)}</div>
          </div>
          <button class="ghost-xs danger" onclick="deleteDocHubDocument('${doc.id}')" title="Delete document">🗑️</button>
        </div>

        ${doc.summary ? `<div class="doc-summary-preview">${esc(doc.summary)}</div>` : ""}

        <div class="doc-actions-row">
          <button class="doc-action-pill" onclick="generateStudyPack('${doc.id}', 'flashcards')" title="Generate interactive 3D Flashcards">🃏 Flashcards</button>
          <button class="doc-action-pill" onclick="generateStudyPack('${doc.id}', 'quiz')" title="Generate interactive Multiple-Choice Quiz">❓ Quiz</button>
          <button class="doc-action-pill" onclick="generateStudyPack('${doc.id}', 'summary')" title="Generate Executive Summary">📝 Summary</button>
          <button class="doc-action-pill" onclick="chatWithDocument('${doc.id}')" title="Discuss in Chat with page citations">💬 Chat</button>
        </div>
      </div>
    `;
  }).join("");
}

function populateDocCompareSelects() {
  const selA = $("docCompareSelectA");
  const selB = $("docCompareSelectB");
  if (!selA || !selB) return;

  const opts = docHubDocuments.map(d => `<option value="${d.id}">${esc(d.filename)} (${d.page_count ? d.page_count + " pgs" : Math.round((d.file_size || 0) / 1024) + " KB"})</option>`).join("");
  selA.innerHTML = opts || '<option value="">No documents</option>';
  selB.innerHTML = opts || '<option value="">No documents</option>';
  if (docHubDocuments.length > 1) {
    selB.selectedIndex = 1;
  }
}

async function uploadDocHubFiles(fileList) {
  if (!fileList || fileList.length === 0) return;

  for (const file of fileList) {
    toast(`Uploading "${file.name}"…`);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const resp = await fetch("/api/documents/upload", {
        method: "POST",
        headers: {
          "X-Passcode": passcode
        },
        body: formData
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `Upload failed (${resp.status})`);
      }
      toast(`Saved "${file.name}" to Document Hub! ✓`);
    } catch (err) {
      toast(`Upload failed for "${file.name}": ` + err.message, true);
    }
  }
  loadDocHub();
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      const b64 = result.split(",")[1];
      resolve(b64);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function deleteDocHubDocument(docId) {
  const doc = docHubDocuments.find(d => d.id === docId);
  const name = doc ? doc.filename : "this document";
  if (!confirm(`Delete "${name}" from your Document Hub?`)) return;

  try {
    await api(`/api/documents/${docId}`, { method: "DELETE" });
    toast(`Deleted "${name}"`);
    loadDocHub();
  } catch (err) {
    toast("Failed to delete document: " + err.message, true);
  }
}

function chatWithDocument(docId) {
  const doc = docHubDocuments.find(d => d.id === docId);
  if (!doc) return;
  closeDrawers();
  const msgInput = $("msg");
  if (msgInput) {
    msgInput.value = `Please analyze document "${doc.filename}" and summarize the key insights (with page citations): `;
    msgInput.focus();
  }
  toast(`Ready to chat with "${doc.filename}"`);
}

async function generateStudyPack(docId, studyMode) {
  const doc = docHubDocuments.find(d => d.id === docId);
  const docName = doc ? doc.filename : "Document";

  toast(`Generating ${studyMode} for "${docName}"… This may take a few moments.`);
  closeDrawers();

  try {
    const res = await api(`/api/documents/${docId}/study`, {
      method: "POST",
      body: JSON.stringify({
        mode: studyMode
      })
    });

    if (studyMode === "summary") {
      canvasState.title = `Summary: ${docName}`;
      canvasState.lang = "markdown";
      canvasState.code = res.content || "# Summary\nNo summary generated.";
      canvasState.tab = "preview";
      toggleCanvas(true);
      toast("Executive summary opened in Canvas!");
    } else if (studyMode === "flashcards") {
      let cards = res.content;
      if (typeof cards === "string") {
        try {
          const match = cards.match(/\[\s*\{[\s\S]*\}\s*\]/);
          if (match) cards = JSON.parse(match[0]);
        } catch(e) {}
      }
      if (!Array.isArray(cards)) {
        canvasState.title = `Flashcards: ${docName}`;
        canvasState.lang = "markdown";
        canvasState.code = typeof res.content === "string" ? res.content : JSON.stringify(res.content, null, 2);
        canvasState.tab = "preview";
        toggleCanvas(true);
        return;
      }

      const htmlApp = buildFlashcardsApp(docName, cards);
      canvasState.title = `Flashcards: ${docName}`;
      canvasState.lang = "html";
      canvasState.code = htmlApp;
      canvasState.tab = "preview";
      toggleCanvas(true);
      toast(`Created ${cards.length} interactive flashcards in Canvas!`);
    } else if (studyMode === "quiz") {
      let questions = res.content;
      if (typeof questions === "string") {
        try {
          const match = questions.match(/\[\s*\{[\s\S]*\}\s*\]/);
          if (match) questions = JSON.parse(match[0]);
        } catch(e) {}
      }
      if (!Array.isArray(questions)) {
        canvasState.title = `Quiz: ${docName}`;
        canvasState.lang = "markdown";
        canvasState.code = typeof res.content === "string" ? res.content : JSON.stringify(res.content, null, 2);
        canvasState.tab = "preview";
        toggleCanvas(true);
        return;
      }

      const htmlApp = buildQuizApp(docName, questions);
      canvasState.title = `Quiz: ${docName}`;
      canvasState.lang = "html";
      canvasState.code = htmlApp;
      canvasState.tab = "preview";
      toggleCanvas(true);
      toast(`Created ${questions.length}-question interactive quiz in Canvas!`);
    }
  } catch (err) {
    toast(`Failed to generate ${studyMode}: ` + err.message, true);
  }
}

function buildFlashcardsApp(docName, cards) {
  const cardsJson = JSON.stringify(cards);
  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flashcards: ${esc(docName)}</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 24px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #090c14; color: #f1f5f9;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      min-height: 95vh;
    }
    .header { text-align: center; margin-bottom: 20px; }
    .badge {
      display: inline-block; padding: 4px 12px; background: rgba(240, 168, 50, 0.15);
      border: 1px solid rgba(240, 168, 50, 0.3); color: #f0a832; border-radius: 20px;
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
      margin-bottom: 8px;
    }
    .title { margin: 0 0 4px; font-size: 20px; color: #fff; }
    .subtitle { color: #94a3b8; font-size: 13px; margin: 0; }

    .progress-bar-wrap { width: 100%; max-width: 520px; height: 6px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; margin: 16px 0 20px; }
    .progress-bar-fill { height: 100%; background: #f0a832; width: 0%; transition: width 0.3s; }

    .card-scene {
      width: 100%; max-width: 520px; height: 320px; perspective: 1000px; cursor: pointer; margin-bottom: 24px;
    }
    .card-inner {
      width: 100%; height: 100%; position: relative; transform-style: preserve-3d;
      transition: transform 0.6s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .card-scene.is-flipped .card-inner { transform: rotateY(180deg); }
    .card-face {
      position: absolute; width: 100%; height: 100%; backface-visibility: hidden;
      border-radius: 16px; padding: 32px; display: flex; flex-direction: column;
      justify-content: space-between; border: 1px solid rgba(255,255,255,0.1);
      box-shadow: 0 16px 40px rgba(0,0,0,0.6);
    }
    .card-front {
      background: linear-gradient(145deg, #131722, #1b2030);
    }
    .card-back {
      background: linear-gradient(145deg, #1b2030, #131722);
      transform: rotateY(180deg);
      border-color: rgba(240,168,50,0.3);
    }
    .card-top { display: flex; justify-content: space-between; align-items: center; }
    .card-category { font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: #94a3b8; font-weight: 700; }
    .card-number { font-size: 12px; color: #64748b; font-weight: 600; }
    .card-text { font-size: 18px; line-height: 1.5; color: #fff; font-weight: 500; margin: auto 0; text-align: center; }
    .card-back .card-text { color: #f0ecdf; font-size: 16px; }
    .card-hint { font-size: 12px; color: #38bdf8; text-align: center; margin-top: 8px; }
    .card-prompt { font-size: 11px; color: #64748b; text-align: center; }

    .controls { display: flex; gap: 10px; align-items: center; justify-content: center; width: 100%; max-width: 520px; margin-bottom: 16px; }
    .ctrl-btn {
      background: #181d2a; border: 1px solid rgba(255,255,255,0.12); color: #e2e8f0;
      padding: 10px 18px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;
      transition: all 0.15s;
    }
    .ctrl-btn:hover { background: #232a3d; color: #fff; }
    .ctrl-btn.primary { background: #f0a832; color: #0b0802; border: 0; }
    .ctrl-btn.primary:hover { filter: brightness(1.1); }

    .score-row { display: flex; gap: 12px; justify-content: center; margin-top: 8px; font-size: 13px; color: #94a3b8; }
    .score-badge { padding: 4px 10px; border-radius: 6px; font-weight: 600; font-size: 12px; }
    .score-known { background: rgba(34, 197, 94, 0.15); color: #86efac; border: 1px solid rgba(34, 197, 94, 0.3); }
    .score-review { background: rgba(239, 68, 68, 0.15); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.3); }
  </style>
</head>
<body>
  <div class="header">
    <div class="badge">Study Pack · Interactive Flashcards</div>
    <h1 class="title">${esc(docName)}</h1>
    <p class="subtitle">Click the card or press Space to flip · Use arrows to navigate</p>
  </div>

  <div class="progress-bar-wrap">
    <div id="progFill" class="progress-bar-fill"></div>
  </div>

  <div id="cardScene" class="card-scene" onclick="flipCard()">
    <div class="card-inner">
      <div class="card-face card-front">
        <div class="card-top">
          <span id="cardCat" class="card-category">Concept</span>
          <span id="cardNum" class="card-number">Card 1 / 1</span>
        </div>
        <div id="cardQuestion" class="card-text">Question text</div>
        <div id="cardHint" class="card-hint"></div>
        <div class="card-prompt">Click to reveal answer ↺</div>
      </div>
      <div class="card-face card-back">
        <div class="card-top">
          <span class="card-category">Answer</span>
          <span id="cardNumBack" class="card-number">Card 1 / 1</span>
        </div>
        <div id="cardAnswer" class="card-text">Answer text</div>
        <div class="card-prompt">Click to flip back ↺</div>
      </div>
    </div>
  </div>

  <div class="controls">
    <button class="ctrl-btn" onclick="prevCard()">◀ Prev</button>
    <button class="ctrl-btn" onclick="flipCard()">Flip Card</button>
    <button class="ctrl-btn" onclick="markKnown(false)">Needs Review</button>
    <button class="ctrl-btn primary" onclick="markKnown(true)">Got It! ✓</button>
    <button class="ctrl-btn" onclick="nextCard()">Next ▶</button>
    <button class="ctrl-btn" onclick="shuffleCards()" title="Shuffle cards">🔀</button>
  </div>

  <div class="score-row">
    <span id="badgeKnown" class="score-badge score-known">0 Mastered</span>
    <span id="badgeReview" class="score-badge score-review">0 Review</span>
  </div>

  <script>
    const cards = ${cardsJson};
    let curIndex = 0;
    let knownSet = new Set();
    let reviewSet = new Set();
    let isFlipped = false;

    function render() {
      if (!cards.length) return;
      const c = cards[curIndex];
      isFlipped = false;
      document.getElementById('cardScene').classList.remove('is-flipped');

      document.getElementById('cardCat').textContent = c.category || 'Concept';
      document.getElementById('cardNum').textContent = 'Card ' + (curIndex + 1) + ' of ' + cards.length;
      document.getElementById('cardNumBack').textContent = 'Card ' + (curIndex + 1) + ' of ' + cards.length;
      document.getElementById('cardQuestion').textContent = c.front || c.question || 'No question';
      document.getElementById('cardAnswer').textContent = c.back || c.answer || 'No answer';
      document.getElementById('cardHint').textContent = c.hint ? '💡 Hint: ' + c.hint : '';

      const pct = Math.round(((curIndex + 1) / cards.length) * 100);
      document.getElementById('progFill').style.width = pct + '%';
      document.getElementById('badgeKnown').textContent = knownSet.size + ' Mastered';
      document.getElementById('badgeReview').textContent = reviewSet.size + ' Review';
    }

    function flipCard() {
      isFlipped = !isFlipped;
      document.getElementById('cardScene').classList.toggle('is-flipped', isFlipped);
    }
    function nextCard() {
      if (curIndex < cards.length - 1) { curIndex++; render(); }
    }
    function prevCard() {
      if (curIndex > 0) { curIndex--; render(); }
    }
    function markKnown(known) {
      if (known) {
        knownSet.add(curIndex);
        reviewSet.delete(curIndex);
      } else {
        reviewSet.add(curIndex);
        knownSet.delete(curIndex);
      }
      if (curIndex < cards.length - 1) nextCard();
      else render();
    }
    function shuffleCards() {
      for (let i = cards.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [cards[i], cards[j]] = [cards[j], cards[i]];
      }
      curIndex = 0;
      knownSet.clear();
      reviewSet.clear();
      render();
    }

    document.addEventListener('keydown', (e) => {
      if (e.code === 'Space') { e.preventDefault(); flipCard(); }
      else if (e.code === 'ArrowRight') nextCard();
      else if (e.code === 'ArrowLeft') prevCard();
    });

    render();
  <\/script>
</body>
</html>`;
}

function buildQuizApp(docName, questions) {
  const qJson = JSON.stringify(questions);
  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quiz: ${esc(docName)}</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 24px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #090c14; color: #f1f5f9;
      display: flex; flex-direction: column; align-items: center;
      min-height: 95vh;
    }
    .header { text-align: center; margin-bottom: 16px; }
    .badge {
      display: inline-block; padding: 4px 12px; background: rgba(56, 189, 248, 0.15);
      border: 1px solid rgba(56, 189, 248, 0.3); color: #38bdf8; border-radius: 20px;
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
      margin-bottom: 8px;
    }
    .title { margin: 0 0 4px; font-size: 20px; color: #fff; }
    .subtitle { color: #94a3b8; font-size: 13px; margin: 0; }

    .progress-bar-wrap { width: 100%; max-width: 600px; height: 6px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; margin: 16px 0 20px; }
    .progress-bar-fill { height: 100%; background: #38bdf8; width: 0%; transition: width 0.3s; }

    .quiz-card {
      background: #131722; border: 1px solid rgba(255,255,255,0.1);
      border-radius: 16px; padding: 32px; width: 100%; max-width: 600px;
      box-shadow: 0 16px 40px rgba(0,0,0,0.5);
    }
    .q-meta { font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px; }
    .q-text { font-size: 18px; font-weight: 600; line-height: 1.5; color: #fff; margin-bottom: 24px; }
    .options-list { display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px; }
    .opt-btn {
      display: flex; align-items: center; width: 100%; padding: 14px 18px;
      background: #181d2a; border: 1px solid rgba(255,255,255,0.1); border-radius: 10px;
      color: #e2e8f0; font-size: 14px; cursor: pointer; text-align: left;
      transition: all 0.15s; line-height: 1.4;
    }
    .opt-btn:hover:not(:disabled) { background: #232a3d; border-color: rgba(56, 189, 248, 0.4); }
    .opt-letter {
      display: inline-flex; align-items: center; justify-content: center;
      width: 24px; height: 24px; border-radius: 50%; background: rgba(255,255,255,0.08);
      font-weight: 700; font-size: 12px; margin-right: 12px; flex-shrink: 0;
    }
    .opt-btn.correct { background: rgba(34, 197, 94, 0.2); border-color: #22c55e; color: #86efac; }
    .opt-btn.correct .opt-letter { background: #22c55e; color: #000; }
    .opt-btn.incorrect { background: rgba(239, 68, 68, 0.2); border-color: #ef4444; color: #fca5a5; }
    .opt-btn.incorrect .opt-letter { background: #ef4444; color: #fff; }

    .explanation-box {
      margin-top: 18px; padding: 14px; border-radius: 8px; font-size: 13px; line-height: 1.5;
      background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.3); color: #bae6fd;
      display: none;
    }
    .card-footer { display: flex; justify-content: space-between; align-items: center; margin-top: 24px; }
    .score-indicator { font-size: 13px; color: #94a3b8; font-weight: 600; }
    .next-btn {
      background: #f0a832; color: #0b0802; border: 0; padding: 10px 22px; border-radius: 8px;
      font-weight: 700; font-size: 14px; cursor: pointer; display: none;
    }
    .next-btn:hover { filter: brightness(1.1); }

    .results-view { text-align: center; padding: 20px 0; }
    .score-big { font-size: 48px; font-weight: 800; color: #f0a832; margin: 16px 0 8px; }
    .score-msg { font-size: 15px; color: #94a3b8; margin-bottom: 24px; }
    .retry-btn {
      background: #38bdf8; color: #0b0802; border: 0; padding: 12px 28px; border-radius: 8px;
      font-weight: 700; font-size: 14px; cursor: pointer;
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="badge">Study Pack · Interactive Quiz</div>
    <h1 class="title">${esc(docName)}</h1>
    <p class="subtitle">Test your comprehension with automated instant feedback</p>
  </div>

  <div class="progress-bar-wrap">
    <div id="progFill" class="progress-bar-fill"></div>
  </div>

  <div id="quizContainer" class="quiz-card">
    <div id="questionView">
      <div id="qMeta" class="q-meta">Question 1 of 5</div>
      <div id="qText" class="q-text">Loading question…</div>
      <div id="optList" class="options-list"></div>
      <div id="expBox" class="explanation-box"></div>
      <div class="card-footer">
        <span id="scoreText" class="score-indicator">Score: 0 / 0</span>
        <button id="nextBtn" class="next-btn" onclick="nextQuestion()">Next Question ▶</button>
      </div>
    </div>
    <div id="resultsView" class="results-view" style="display:none;">
      <h2>Quiz Complete! 🎉</h2>
      <div id="finalScore" class="score-big">100%</div>
      <div id="finalScoreDetail" class="score-msg">You scored 5 out of 5 correctly.</div>
      <button class="retry-btn" onclick="restartQuiz()">Take Quiz Again ↺</button>
    </div>
  </div>

  <script>
    const questions = ${qJson};
    let curIdx = 0;
    let score = 0;
    let answered = false;

    function render() {
      if (!questions.length) return;
      if (curIdx >= questions.length) {
        showResults();
        return;
      }
      answered = false;
      const q = questions[curIdx];
      document.getElementById('qMeta').textContent = 'Question ' + (curIdx + 1) + ' of ' + questions.length;
      document.getElementById('qText').textContent = q.question;
      document.getElementById('expBox').style.display = 'none';
      document.getElementById('nextBtn').style.display = 'none';
      document.getElementById('scoreText').textContent = 'Score: ' + score + ' / ' + curIdx;

      const pct = Math.round((curIdx / questions.length) * 100);
      document.getElementById('progFill').style.width = pct + '%';

      const letters = ['A', 'B', 'C', 'D', 'E'];
      const optHtml = (q.options || []).map((opt, i) => {
        return '<button class="opt-btn" onclick="selectOption(' + i + ')">' +
          '<span class="opt-letter">' + (letters[i] || (i+1)) + '</span>' +
          '<span>' + opt + '</span>' +
        '</button>';
      }).join('');
      document.getElementById('optList').innerHTML = optHtml;
    }

    function selectOption(chosenIdx) {
      if (answered) return;
      answered = true;
      const q = questions[curIdx];
      const correctIdx = q.answer_index !== undefined ? q.answer_index : 0;
      const buttons = document.querySelectorAll('.opt-btn');

      buttons.forEach((btn, idx) => {
        btn.disabled = true;
        if (idx === correctIdx) {
          btn.classList.add('correct');
        } else if (idx === chosenIdx) {
          btn.classList.add('incorrect');
        }
      });

      if (chosenIdx === correctIdx) {
        score++;
      }

      document.getElementById('scoreText').textContent = 'Score: ' + score + ' / ' + (curIdx + 1);

      if (q.explanation) {
        const exp = document.getElementById('expBox');
        exp.textContent = '💡 ' + q.explanation;
        exp.style.display = 'block';
      }

      document.getElementById('nextBtn').style.display = 'block';
    }

    function nextQuestion() {
      curIdx++;
      render();
    }

    function showResults() {
      document.getElementById('questionView').style.display = 'none';
      document.getElementById('resultsView').style.display = 'block';
      const pct = Math.round((score / questions.length) * 100);
      document.getElementById('finalScore').textContent = pct + '%';
      document.getElementById('finalScoreDetail').textContent = 'You answered ' + score + ' of ' + questions.length + ' questions correctly.';
      document.getElementById('progFill').style.width = '100%';
    }

    function restartQuiz() {
      curIdx = 0;
      score = 0;
      document.getElementById('questionView').style.display = 'block';
      document.getElementById('resultsView').style.display = 'none';
      render();
    }

    render();
  <\/script>
</body>
</html>`;
}

async function runDocCompare() {
  const selA = $("docCompareSelectA");
  const selB = $("docCompareSelectB");
  if (!selA || !selB || !selA.value || !selB.value) {
    toast("Please select two documents to compare", true);
    return;
  }
  if (selA.value === selB.value) {
    toast("Please select two different documents to compare", true);
    return;
  }

  const idA = parseInt(selA.value, 10);
  const idB = parseInt(selB.value, 10);
  const docA = docHubDocuments.find(d => d.id === idA);
  const docB = docHubDocuments.find(d => d.id === idB);

  toast(`Comparing "${docA?.filename || 'Doc A'}" vs "${docB?.filename || 'Doc B'}"…`);
  closeDrawers();

  try {
    const res = await api("/api/documents/compare", {
      method: "POST",
      body: JSON.stringify({
        doc_id_a: idA,
        doc_id_b: idB
      })
    });

    canvasState.title = `Comparison: ${docA?.filename || 'A'} vs ${docB?.filename || 'B'}`;
    canvasState.lang = "markdown";
    canvasState.code = res.comparison || "# Document Comparison\nNo analysis returned.";
    canvasState.tab = "preview";
    toggleCanvas(true);
    toast("Comparative analysis generated in Canvas!");
  } catch (err) {
    toast("Comparison failed: " + err.message, true);
  }
}

function wireDocHub() {
  if ($("btnDocHub")) {
    $("btnDocHub").onclick = () => {
      openDrawer("docHubPanel");
      loadDocHub();
    };
  }

  const fileInput = $("docHubFileInput");
  const triggerBtn = $("btnDocHubUploadTrigger");
  const dropZone = $("docDropZone");

  if (triggerBtn && fileInput) {
    triggerBtn.onclick = () => fileInput.click();
    fileInput.onchange = () => {
      if (fileInput.files.length) {
        uploadDocHubFiles(fileInput.files);
        fileInput.value = "";
      }
    };
  }

  if (dropZone && fileInput) {
    dropZone.ondragover = (e) => {
      e.preventDefault();
      dropZone.classList.add("drag-over");
    };
    dropZone.ondragleave = () => dropZone.classList.remove("drag-over");
    dropZone.ondrop = (e) => {
      e.preventDefault();
      dropZone.classList.remove("drag-over");
      if (e.dataTransfer.files.length) {
        uploadDocHubFiles(e.dataTransfer.files);
      }
    };
  }

  if ($("btnToggleCompareForm")) {
    $("btnToggleCompareForm").onclick = () => {
      const f = $("docCompareForm");
      if (f) f.classList.toggle("hidden");
    };
  }

  if ($("btnRunDocCompare")) {
    $("btnRunDocCompare").onclick = runDocCompare;
  }

  if ($("docHubSearch")) {
    $("docHubSearch").oninput = renderDocHubList;
  }
}

/* ==========================================================================
   MODEL OBSERVATORY & TELEMETRY CONTROLLER (Phase D)
   ========================================================================== */
let obsPollInterval = null;

async function loadObservatory() {
  try {
    const [telemRes, logsRes] = await Promise.all([
      api("/api/observatory/telemetry"),
      api("/api/observatory/logs?limit=50")
    ]);

    renderObservatoryTelemetry(telemRes);
    renderObservatoryLogs(logsRes?.logs || []);
  } catch (err) {
    console.error("Observatory load failed:", err);
  }
}

function startObservatoryPolling() {
  stopObservatoryPolling();
  obsPollInterval = setInterval(() => {
    const p = $("observatoryPanel");
    if (p && !p.classList.contains("hidden") && p.classList.contains("open")) {
      loadObservatory();
    } else {
      stopObservatoryPolling();
    }
  }, 3000);
}

function stopObservatoryPolling() {
  if (obsPollInterval) {
    clearInterval(obsPollInterval);
    obsPollInterval = null;
  }
}

function renderObservatoryTelemetry(data) {
  if (!data) return;

  // 1. GPU Telemetry
  const gpu = data.gpu;
  if (gpu && gpu.available) {
    const gpuName = gpu.name || gpu.gpu_name || "NVIDIA GPU";
    if ($("obsGpuName")) $("obsGpuName").textContent = gpuName;

    if ($("obsGpuTemp")) {
      const tempVal = gpu.temperature_c !== null && gpu.temperature_c !== undefined ? gpu.temperature_c : null;
      const tempStr = tempVal !== null ? `${tempVal}°C` : "N/A";
      $("obsGpuTemp").textContent = tempStr;
      const isHot = typeof tempVal === "number" && tempVal > 80;
      $("obsGpuTemp").style.background = isHot ? "rgba(239,68,68,0.2)" : "rgba(240,168,50,0.15)";
      $("obsGpuTemp").style.color = isHot ? "#fca5a5" : "var(--amber)";
    }

    const vramUsed = Math.round(gpu.vram_used_mb ?? gpu.memory_used_mb ?? 0);
    const vramTotal = Math.round(gpu.vram_total_mb ?? gpu.memory_total_mb ?? 0);
    const rawPct = gpu.vram_used_pct ?? gpu.memory_used_pct;
    const vramPct = (rawPct !== undefined && rawPct !== null ? Number(rawPct) : (vramTotal > 0 ? (vramUsed / vramTotal) * 100 : 0)).toFixed(1);
    const vramFree = Math.round(gpu.vram_free_mb ?? gpu.memory_free_mb ?? Math.max(0, vramTotal - vramUsed));
    const gpuLoad = Math.round(gpu.gpu_util_pct ?? gpu.utilization_pct ?? 0);

    if ($("obsVramText")) $("obsVramText").textContent = `${vramUsed} / ${vramTotal} MB (${vramPct}%)`;
    if ($("obsVramBar")) {
      $("obsVramBar").style.width = `${Math.min(100, Math.max(0, Number(vramPct)))}%`;
      $("obsVramBar").style.background = Number(vramPct) > 85 ? "linear-gradient(90deg, #f59e0b, #ef4444)" : "linear-gradient(90deg, #f0a832, #f59e0b)";
    }
    if ($("obsGpuLoad")) $("obsGpuLoad").textContent = `${gpuLoad}%`;
    if ($("obsVramFree")) $("obsVramFree").textContent = `${vramFree} MB`;
  } else {
    if ($("obsGpuName")) $("obsGpuName").textContent = "Discrete GPU Not Detected";
    if ($("obsGpuTemp")) $("obsGpuTemp").textContent = "N/A";
    if ($("obsVramText")) $("obsVramText").textContent = "CPU-only mode active";
    if ($("obsVramBar")) $("obsVramBar").style.width = "0%";
    if ($("obsGpuLoad")) $("obsGpuLoad").textContent = "0%";
    if ($("obsVramFree")) $("obsVramFree").textContent = "0 MB";
  }

  // 2. System Memory (RAM) & CPU
  const sys = data.system;
  if (sys) {
    const cpuPct = Math.round(sys.cpu_util_pct ?? sys.cpu_pct ?? 0);
    const ramUsed = Number(sys.ram_used_gb ?? 0).toFixed(1);
    const ramTotal = Number(sys.ram_total_gb ?? 0).toFixed(1);
    const ramPct = Math.round(sys.ram_used_pct ?? (ramTotal > 0 ? (Number(ramUsed) / Number(ramTotal)) * 100 : 0));
    const ramFree = Number(sys.ram_free_gb ?? Math.max(0, Number(ramTotal) - Number(ramUsed))).toFixed(1);

    if ($("obsCpuBadge")) $("obsCpuBadge").textContent = `CPU: ${cpuPct}%`;
    if ($("obsRamText")) $("obsRamText").textContent = `${ramUsed} / ${ramTotal} GB (${ramPct}%)`;
    if ($("obsRamBar")) $("obsRamBar").style.width = `${Math.min(100, Math.max(0, ramPct))}%`;
    if ($("obsRamFree")) $("obsRamFree").textContent = `${ramFree} GB`;
    if ($("obsRamTotal")) $("obsRamTotal").textContent = `${ramTotal} GB`;
  }

  // 3. Inference KPIs
  const kpi = data.inference_kpi || data.stats;
  if (kpi) {
    if ($("obsKpiTotal")) $("obsKpiTotal").textContent = kpi.total_queries ?? kpi.total_inferences ?? 0;
    if ($("obsKpiSpeed")) $("obsKpiSpeed").textContent = `${kpi.avg_tokens_per_sec || 0.0}`;
    const lat = kpi.avg_latency_ms ?? kpi.avg_duration_ms ?? 0;
    if ($("obsKpiLatency")) $("obsKpiLatency").textContent = `${lat} ms`;
    const succ = kpi.success_rate_pct ?? kpi.success_rate ?? 100;
    if ($("obsKpiSuccess")) $("obsKpiSuccess").textContent = `${succ}%`;
  }

  // 4. Loaded Models in VRAM
  const loadedList = $("obsLoadedList");
  const loadedModels = data.loaded_models || [];
  if ($("obsLoadedCountBadge")) {
    $("obsLoadedCountBadge").textContent = `${loadedModels.length} active`;
  }

  if (loadedList) {
    if (!loadedModels.length) {
      loadedList.innerHTML = `<div class="muted" style="padding:16px;text-align:center;font-size:0.8rem;">No models resident in VRAM. (Ollama/vLLM unloaded)</div>`;
    } else {
      loadedList.innerHTML = loadedModels.map(m => {
        const vramMb = m.size_vram ? Math.round(m.size_vram / (1024 * 1024)) : (m.size ? Math.round(m.size / (1024 * 1024)) : 0);
        const vramText = vramMb > 1024 ? `${(vramMb / 1024).toFixed(1)} GB VRAM` : `${vramMb} MB VRAM`;
        const expires = m.expires_at ? `Expires: ${new Date(m.expires_at).toLocaleTimeString()}` : "Active in memory";
        return `
          <div class="obs-loaded-card">
            <div>
              <div class="obs-model-name">${esc(m.name)}</div>
              <div class="obs-model-details">${vramText} · ${expires}</div>
            </div>
            <button class="ghost-xs danger" type="button" onclick="ejectObservatoryModel('${esc(m.name)}')">Eject ⚡</button>
          </div>
        `;
      }).join("");
    }
  }
}

function renderObservatoryLogs(logs) {
  const list = $("obsLogsList");
  if (!list) return;

  if (!logs || !logs.length) {
    list.innerHTML = `<div class="muted" style="padding:16px;text-align:center;font-size:0.8rem;">No inference requests recorded yet.</div>`;
    return;
  }

  list.innerHTML = logs.map(l => {
    const isOk = l.status === "ok";
    const statusClass = isOk ? "ok" : "error";
    const statusText = isOk ? "OK" : "ERR";
    const t = l.created_at ? new Date(l.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : "";
    const speed = l.tokens_per_sec ? `${l.tokens_per_sec} t/s` : (l.duration_ms ? `${l.duration_ms}ms` : "-");
    const ttft = l.ttft_ms ? ` · TTFT: ${l.ttft_ms}ms` : "";
    const ioText = `${l.prompt_chars || 0} in / ${l.response_chars || 0} out`;
    return `
      <div class="obs-log-row">
        <div class="obs-log-main">
          <span class="obs-status-pill ${statusClass}">${statusText}</span>
          <div>
            <strong style="color:#f8fafc;font-size:0.78rem;">${esc(l.model || 'unknown')}</strong>
            <span class="muted" style="font-size:0.72rem;margin-left:4px;">(${esc(l.provider || 'local')})</span>
            <div class="obs-log-meta">${t} · ${ioText}${ttft}</div>
          </div>
        </div>
        <div style="text-align:right;">
          <span style="font-weight:700;color:var(--amber);font-family:ui-monospace,monospace;">${speed}</span>
          ${l.error_msg ? `<div style="color:#fca5a5;font-size:0.68rem;max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(l.error_msg)}</div>` : ''}
        </div>
      </div>
    `;
  }).join("");
}

async function ejectObservatoryModel(name) {
  toast(`Evicting ${name} from VRAM…`);
  try {
    const res = await api("/api/vram/eject", {
      method: "POST",
      body: JSON.stringify({ model: name })
    });
    toast(res.message || `Ejected ${name}`);
    await loadObservatory();
    loadVramStatus();
  } catch (err) {
    toast(`Failed to eject model: ${err.message}`, true);
  }
}

async function ejectAllObservatoryModels() {
  const btn = $("btnObservatoryEjectAll");
  if (btn) btn.disabled = true;
  toast("Evicting all loaded models from VRAM…");
  try {
    const res = await api("/api/vram/eject", {
      method: "POST",
      body: JSON.stringify({ all: true })
    });
    toast(res.message || "All models evicted from VRAM");
    await loadObservatory();
    loadVramStatus();
  } catch (err) {
    toast(`Failed to eject models: ${err.message}`, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function clearObservatoryLogs() {
  try {
    await api("/api/observatory/clear_logs", { method: "POST" });
    toast("Inference logs cleared");
    await loadObservatory();
  } catch (err) {
    toast(`Failed to clear logs: ${err.message}`, true);
  }
}

function wireObservatory() {
  const btn = $("btnObservatory");
  if (btn) {
    btn.onclick = () => {
      openDrawer("observatoryPanel");
      loadObservatory();
      startObservatoryPolling();
    };
  }

  if ($("btnObservatoryRefresh")) {
    $("btnObservatoryRefresh").onclick = () => loadObservatory();
  }

  if ($("btnObservatoryEjectAll")) {
    $("btnObservatoryEjectAll").onclick = ejectAllObservatoryModels;
  }

  if ($("btnObservatoryClearLogs")) {
    $("btnObservatoryClearLogs").onclick = clearObservatoryLogs;
  }
}

/* ==========================================================================
   PROMPT LIBRARY & VERSIONED PRESETS CONTROLLER (Phase E)
   ========================================================================== */
let promptTemplates = [];
let currentPromptCategory = "all";
let activeModalTemplate = null;

async function loadPrompts() {
  try {
    const res = await api("/api/prompt_templates");
    promptTemplates = res?.templates || [];
    renderPromptsList();
  } catch (err) {
    console.error("Failed to load prompt templates:", err);
  }
}

function renderPromptsList() {
  const list = $("promptsList");
  if (!list) return;

  const q = ($("promptSearchInput")?.value || "").trim().toLowerCase();
  let filtered = promptTemplates.slice();

  if (currentPromptCategory === "favorites") {
    filtered = filtered.filter(t => t.is_favorite === 1);
  } else if (currentPromptCategory !== "all") {
    filtered = filtered.filter(t => (t.category || "").toLowerCase() === currentPromptCategory.toLowerCase());
  }

  if (q) {
    filtered = filtered.filter(t =>
      (t.title || "").toLowerCase().includes(q) ||
      (t.description || "").toLowerCase().includes(q) ||
      (t.user_template || "").toLowerCase().includes(q)
    );
  }

  if (!filtered.length) {
    list.innerHTML = `<div class="muted" style="padding:28px 16px;text-align:center;font-size:0.85rem;">No prompt templates match your criteria.</div>`;
    return;
  }

  list.innerHTML = filtered.map(t => {
    const isFav = t.is_favorite === 1;
    const cat = esc(t.category || "General");
    const safeId = esc(String(t.id));
    const preview = esc(t.user_template || "").replace(/\{\{([^}]+)\}\}/g, '<span class="prompt-var-tag">{{$1}}</span>');
    const deleteBtn = !t.is_builtin ? `<button class="ghost-xs danger" type="button" onclick="deletePromptTemplate('${safeId}', event)" title="Delete template"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button>` : '';

    return `
      <div class="prompt-item-card" data-id="${safeId}">
        <div class="prompt-item-head">
          <div class="prompt-title-wrap">
            <span class="prompt-cat-tag">${cat}</span>
            <span class="prompt-title-text">${esc(t.title)}</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px;">
            <button class="prompt-fav-btn ${isFav ? 'active' : ''}" type="button" onclick="togglePromptFav('${safeId}', event)" title="Favorite">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="${isFav ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>
            </button>
            <button class="ghost-xs" type="button" onclick="openPromptEditor('${safeId}', event)" title="Edit template">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
            </button>
            ${deleteBtn}
          </div>
        </div>
        ${t.description ? `<div class="prompt-desc-text">${esc(t.description)}</div>` : ''}
        <div class="prompt-preview-snippet">${preview}</div>
        <div class="prompt-card-actions">
          <span class="muted" style="font-size:0.7rem;">v${t.version || 1} ${t.is_builtin ? '· Built-in' : ''}</span>
          <button class="prompt-action-pill" type="button" onclick="usePromptTemplate('${safeId}')">
            <span>Use Prompt</span>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
          </button>
        </div>
      </div>
    `;
  }).join("");
}

function openPromptEditor(tmplId = null, e = null) {
  if (e) e.stopPropagation();
  const f = $("promptEditorForm");
  if (!f) return;

  f.classList.remove("hidden");
  if (tmplId) {
    const t = promptTemplates.find(x => String(x.id) === String(tmplId));
    if (!t) return;
    $("promptEditId").value = t.id;
    $("promptTmplTitle").value = t.title || "";
    $("promptTmplCategory").value = t.category || "Coding";
    $("promptTmplDesc").value = t.description || "";
    $("promptTmplSystem").value = t.system_prompt || "";
    $("promptTmplUser").value = t.user_template || "";
  } else {
    $("promptEditId").value = "";
    $("promptTmplTitle").value = "";
    $("promptTmplCategory").value = "Coding";
    $("promptTmplDesc").value = "";
    $("promptTmplSystem").value = "";
    $("promptTmplUser").value = "";
  }
  $("promptTmplTitle").focus();
}

async function savePromptTemplate() {
  const idVal = $("promptEditId")?.value;
  const title = $("promptTmplTitle")?.value?.trim();
  const category = $("promptTmplCategory")?.value || "Coding";
  const desc = $("promptTmplDesc")?.value?.trim() || "";
  const system = $("promptTmplSystem")?.value?.trim() || "";
  const userTmpl = $("promptTmplUser")?.value?.trim() || "";

  if (!title) {
    toast("Please enter a template title", true);
    return;
  }
  if (!userTmpl) {
    toast("Please enter the user prompt template", true);
    return;
  }

  try {
    const payload = {
      title,
      category,
      description: desc,
      system_prompt: system,
      user_template: userTmpl
    };
    if (idVal) {
      payload.id = idVal;
    }

    const res = await api("/api/prompt_templates", {
      method: "POST",
      body: JSON.stringify(payload)
    });

    toast(res.message || "Template saved successfully!");
    if ($("promptEditorForm")) $("promptEditorForm").classList.add("hidden");
    await loadPrompts();
  } catch (err) {
    toast(`Failed to save template: ${err.message}`, true);
  }
}

async function togglePromptFav(tmplId, e) {
  if (e) e.stopPropagation();
  try {
    const res = await api(`/api/prompt_templates/${tmplId}/favorite`, { method: "POST" });
    const t = promptTemplates.find(x => String(x.id) === String(tmplId));
    if (t) t.is_favorite = res.is_favorite;
    renderPromptsList();
  } catch (err) {
    toast("Failed to toggle favorite: " + err.message, true);
  }
}

async function deletePromptTemplate(tmplId, e) {
  if (e) e.stopPropagation();
  if (!confirm("Are you sure you want to delete this prompt template?")) return;
  try {
    await api(`/api/prompt_templates/${tmplId}`, { method: "DELETE" });
    toast("Prompt template deleted");
    await loadPrompts();
  } catch (err) {
    toast("Failed to delete template: " + err.message, true);
  }
}

function usePromptTemplate(tmplId) {
  const t = promptTemplates.find(x => String(x.id) === String(tmplId));
  if (!t) return;

  // Extract variables: {{variable_name}}
  const matches = Array.from(t.user_template.matchAll(/\{\{([a-zA-Z0-9_\s]+)\}\}/g));
  const varNames = [...new Set(matches.map(m => m[1].trim()))];

  if (!varNames.length) {
    // No placeholders, insert directly
    insertPromptIntoChat(t.user_template, t.title);
    return;
  }

  // Open Variable Prompt Filler Modal
  activeModalTemplate = t;
  const modal = $("promptVarModal");
  const fields = $("promptModalFields");
  const titleEl = $("promptModalTitle");

  if (!modal || !fields) return;

  if (titleEl) titleEl.textContent = `Fill: ${t.title}`;
  fields.innerHTML = varNames.map((v, i) => {
    const isMultiline = /code|text|content|query|prompt|diff/i.test(v);
    const fieldHtml = isMultiline
      ? `<textarea class="prompt-var-input" data-var="${esc(v)}" data-vidx="${i}" rows="3" placeholder="Enter ${esc(v)}…"></textarea>`
      : `<input class="prompt-var-input" data-var="${esc(v)}" data-vidx="${i}" placeholder="Enter ${esc(v)}…" />`;
    return `
      <div class="prompt-modal-field">
        <label>${esc(v.replace(/_/g, ' '))}</label>
        ${fieldHtml}
      </div>
    `;
  }).join("");

  modal.classList.remove("hidden");
  const firstInput = fields.querySelector("input, textarea");
  if (firstInput) setTimeout(() => firstInput.focus(), 50);
}

function submitPromptModal() {
  if (!activeModalTemplate) return;
  const t = activeModalTemplate;
  const inputs = document.querySelectorAll("#promptModalFields .prompt-var-input");

  let rendered = t.user_template;
  inputs.forEach(input => {
    const varName = input.getAttribute("data-var");
    const val = input.value || "";
    if (varName) {
      const escapedVar = varName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      rendered = rendered.replaceAll(new RegExp(`\\{\\{\\s*${escapedVar}\\s*\\}\\}`, 'g'), val);
    }
  });

  $("promptVarModal").classList.add("hidden");
  insertPromptIntoChat(rendered, t.title);
  activeModalTemplate = null;
}

function insertPromptIntoChat(text, title = "") {
  closeDrawers();
  if (labOn) {
    const labInput = $("labPrompt");
    if (labInput) {
      labInput.value = text;
      labInput.focus();
      labInput.dispatchEvent(new Event("input", { bubbles: true }));
      toast(`Prompt "${title || 'Template'}" inserted into Model Lab!`);
      return;
    }
  }

  if (chessOn) {
    if (arenaActiveTab === "council") {
      const councilTopic = $("councilTopic");
      if (councilTopic) {
        councilTopic.value = text;
        councilTopic.focus();
        toast(`Prompt "${title || 'Template'}" set as Council Deliberation Topic!`);
        return;
      }
    } else if (arenaActiveTab === "debate") {
      const debateTopic = $("debateTopic");
      if (debateTopic) {
        debateTopic.value = text;
        debateTopic.focus();
        toast(`Prompt "${title || 'Template'}" set as Debate Topic!`);
        return;
      }
    }
  }

  setMainView("chat");
  const msgInput = $("msg");
  if (msgInput) {
    msgInput.value = text;
    msgInput.focus();
    msgInput.dispatchEvent(new Event("input", { bubbles: true }));
    msgInput.style.height = "auto";
    msgInput.style.height = Math.min(200, Math.max(48, msgInput.scrollHeight)) + "px";
  }
  toast(`Prompt "${title || 'Template'}" inserted into chat!`);
}

function wirePrompts() {
  const btn = $("btnPrompts");
  if (btn) {
    btn.onclick = () => {
      openDrawer("promptsPanel");
      loadPrompts();
    };
  }

  document.querySelectorAll(".prompt-tab-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(".prompt-tab-btn").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      currentPromptCategory = b.getAttribute("data-cat") || "all";
      renderPromptsList();
    };
  });

  if ($("promptSearchInput")) {
    $("promptSearchInput").oninput = renderPromptsList;
  }

  if ($("btnTogglePromptForm")) {
    $("btnTogglePromptForm").onclick = () => {
      const f = $("promptEditorForm");
      if (f) {
        const isHidden = f.classList.contains("hidden");
        if (isHidden) {
          openPromptEditor(null);
        } else {
          f.classList.add("hidden");
        }
      }
    };
  }

  if ($("btnCancelPromptEdit")) {
    $("btnCancelPromptEdit").onclick = () => {
      const f = $("promptEditorForm");
      if (f) f.classList.add("hidden");
    };
  }

  if ($("btnSavePromptTemplate")) {
    $("btnSavePromptTemplate").onclick = savePromptTemplate;
  }

  if ($("btnClosePromptModal")) {
    $("btnClosePromptModal").onclick = () => {
      $("promptVarModal").classList.add("hidden");
      activeModalTemplate = null;
    };
  }
  if ($("btnCancelPromptModal")) {
    $("btnCancelPromptModal").onclick = () => {
      $("promptVarModal").classList.add("hidden");
      activeModalTemplate = null;
    };
  }
  if ($("btnSubmitPromptModal")) {
    $("btnSubmitPromptModal").onclick = submitPromptModal;
  }
}

/* ==========================================================================
   AI WORKBENCH DROPDOWN MENU CONTROLLER
   ========================================================================== */
function closeWorkbenchMenu() {
  const menu = $("workbenchDropdown");
  const btn = $("btnWorkbenchMenu");
  if (menu) menu.classList.add("hidden");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function wireWorkbenchMenu() {
  const btn = $("btnWorkbenchMenu");
  const menu = $("workbenchDropdown");
  if (!btn || !menu) return;

  btn.onclick = (e) => {
    e.stopPropagation();
    const isHidden = menu.classList.contains("hidden");
    if (isHidden) {
      menu.classList.remove("hidden");
      btn.setAttribute("aria-expanded", "true");
    } else {
      closeWorkbenchMenu();
    }
  };

  document.addEventListener("click", (e) => {
    if (!menu.classList.contains("hidden") && !menu.contains(e.target) && !btn.contains(e.target)) {
      closeWorkbenchMenu();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.classList.contains("hidden")) {
      closeWorkbenchMenu();
    }
  });

  $("wbItemLab")?.addEventListener("click", () => {
    closeWorkbenchMenu();
    closeDrawers();
    setMainView("lab");
  });

  $("wbItemArena")?.addEventListener("click", () => {
    closeWorkbenchMenu();
    closeDrawers();
    setMainView("chess");
  });

  $("wbItemDocs")?.addEventListener("click", () => {
    closeWorkbenchMenu();
    closeDrawers();
    openDrawer("docHubPanel");
    loadDocHub();
  });

  $("wbItemObservatory")?.addEventListener("click", () => {
    closeWorkbenchMenu();
    closeDrawers();
    openDrawer("observatoryPanel");
    loadObservatory();
    startObservatoryPolling();
  });

  $("wbItemPrompts")?.addEventListener("click", () => {
    closeWorkbenchMenu();
    closeDrawers();
    openDrawer("promptsPanel");
    loadPrompts();
  });
}

boot();


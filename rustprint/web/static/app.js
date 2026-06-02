const $ = (id) => document.getElementById(id);

const fields = {
    "source.path": "source-path",
    "model.name": "model-name",
    "model.provider": "model-provider",
    "api.api_key": "api-key",
    "api.base_url": "api-base-url",
    "output.base_dir": "output-base",
    "output.cache": "output-cache",
    "git.branch_enabled": "git-branch-enabled",
    "git.branch_name": "git-branch-name",
    "git.commit": "git-commit",
    "requirement_refinement.enabled": "req-enabled",
    "requirement_refinement.rounds": "req-rounds",
    "execution_refinement.enabled": "exec-enabled",
    "execution_refinement.rounds": "exec-rounds",
    "execution_refinement.translate_tests": "exec-translate-tests",
    "run.force": "run-force",
};

const toggles = [
    ["git-branch-enabled", "git-branch-deps"],
    ["req-enabled", "req-deps"],
    ["exec-enabled", "exec-deps"],
];

function setField(path, value) {
    const el = $(fields[path]);
    if (!el) return;
    if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value ?? "";
}

function fillForm(config) {
    for (const path of Object.keys(fields)) {
        const [section, key] = path.split(".");
        setField(path, (config[section] || {})[key]);
    }
    syncToggles();
}

function buildConfig() {
    const config = {};
    for (const [path, id] of Object.entries(fields)) {
        const [section, key] = path.split(".");
        config[section] = config[section] || {};
        const el = $(id);
        if (el.type === "checkbox") config[section][key] = el.checked;
        else if (el.type === "number") config[section][key] = parseInt(el.value || "0", 10);
        else config[section][key] = el.value.trim();
    }
    return config;
}

function syncToggles() {
    toggles.forEach(([cb, deps]) => {
        const checkbox = $(cb);
        const target = $(deps);
        if (checkbox && target) target.classList.toggle("collapsed", !checkbox.checked);
    });
}

function setStatus(message, kind) {
    const el = $("status-message");
    el.textContent = message || "";
    el.className = "status" + (kind ? " " + kind : "");
}

function setPill(el, text, variant) {
    if (!el) return;
    el.className = "pill pill-" + variant;
    el.innerHTML = '<span class="pill-dot"></span>' + escapeHtml(text);
}

function statusText(stage) {
    if (stage.detail) return stage.detail;
    if (stage.status === "done") return "Completed";
    if (stage.status === "skipped") return "Skipped";
    if (stage.status === "active") return "In progress…";
    if (stage.total_rounds) return stage.total_rounds + " rounds";
    return "";
}

const OPEN_ICON =
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M4 5h6l2 2h8v11H4z"/><path d="M9 12h7"/><path d="M13 9l3 3-3 3"/></svg>';

const expanded = new Set();
let lastStages = [];

function openButton(path) {
    if (!path) return "";
    return '<button class="open-btn" type="button" data-path="' + escapeHtml(path) +
        '" title="Open in editor">' + OPEN_ICON + "</button>";
}

function renderChildren(node) {
    const children = node.children || [];
    if (!children.length) return "";
    const open = expanded.has(node.id);
    let html = '<ul class="substeps' + (open ? "" : " collapsed") + '">';
    children.forEach((child) => { html += renderSubstep(child); });
    html += "</ul>";
    return html;
}

function renderSubstep(node) {
    const hasChildren = Boolean(node.children && node.children.length);
    const open = expanded.has(node.id);
    const detail = statusText(node);
    const toggleAttr = hasChildren ? ' data-toggle="' + escapeHtml(node.id) + '"' : "";
    const chevron = hasChildren ? '<span class="chevron" aria-hidden="true"></span>' : "";
    return (
        '<li class="substep ' + (node.status || "pending") +
        (hasChildren ? " has-children" : "") + (open ? " open" : "") + '">' +
        '<div class="substep-row"' + toggleAttr + ">" +
        '<span class="dot"></span>' +
        '<div class="body"><div class="label">' + escapeHtml(node.label) + "</div>" +
        (detail ? '<div class="detail">' + escapeHtml(detail) + "</div>" : "") +
        "</div>" +
        chevron +
        openButton(node.path) +
        "</div>" +
        renderChildren(node) +
        "</li>"
    );
}

function renderStages(stages) {
    lastStages = stages || [];
    const list = $("stages");
    list.innerHTML = "";
    lastStages.forEach((stage) => {
        const hasChildren = Boolean(stage.children && stage.children.length);
        const open = expanded.has(stage.id);
        const li = document.createElement("li");
        li.className =
            "stage " + (stage.status || "pending") +
            (hasChildren ? " has-children" : "") + (open ? " open" : "");
        const detail = statusText(stage);
        const toggleAttr = hasChildren ? ' data-toggle="' + escapeHtml(stage.id) + '"' : "";
        const chevron = hasChildren ? '<span class="chevron" aria-hidden="true"></span>' : "";
        li.innerHTML =
            '<div class="stage-row"' + toggleAttr + ">" +
            '<span class="dot"></span>' +
            '<div class="body"><div class="label">' + escapeHtml(stage.label) + "</div>" +
            (detail ? '<div class="detail">' + escapeHtml(detail) + "</div>" : "") +
            "</div>" +
            chevron +
            openButton(stage.path) +
            "</div>" +
            renderChildren(stage);
        list.appendChild(li);
    });
}

function syncAutoExpand(nodes) {
    (nodes || []).forEach((node) => {
        if (!node.children || !node.children.length) return;
        if (node.status === "active") expanded.add(node.id);
        else expanded.delete(node.id);
        syncAutoExpand(node.children);
    });
}

function toggleStage(id) {
    if (expanded.has(id)) expanded.delete(id);
    else expanded.add(id);
    renderStages(lastStages);
}

async function openPath(path) {
    if (!path) return;
    setStatus("Opening " + path + "…", "");
    try {
        const res = await fetch("/api/open", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path }),
        });
        const data = await res.json();
        setStatus(data.message, data.status === "success" ? "ok" : "error");
    } catch (e) {
        setStatus("Open failed: " + e, "error");
    }
}

function updateProgress(stages) {
    const bar = $("progress-bar");
    if (!bar) return;
    const list = stages || [];
    if (!list.length) { bar.style.width = "0%"; return; }
    const completed = list.filter((s) => s.status === "done" || s.status === "skipped").length;
    bar.style.width = Math.round((completed / list.length) * 100) + "%";
}

function pad(n) { return String(n).padStart(2, "0"); }

function timestamp(ts) {
    const d = ts ? new Date(ts * 1000) : new Date();
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

function appendLog(event) {
    const log = $("log");
    const placeholder = log.querySelector(".empty");
    if (placeholder) placeholder.remove();
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    const line = document.createElement("div");
    line.className = "line " + (event.level || "info");
    line.innerHTML =
        '<span class="ts">' + timestamp(event.ts) + "</span>" +
        '<span class="msg">' + highlightLog(event.message) + "</span>";
    log.appendChild(line);
    if (atBottom) log.scrollTop = log.scrollHeight;
}

function clearLog() {
    $("log").innerHTML = '<div class="line empty"><span class="msg">No output yet.</span></div>';
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
}

const LOG_PATTERNS = [
    ["lg-tag", /\[[^\]]+\]/],
    ["lg-path", /(?:~|\/)[^\s:,()'"]+/],
    ["lg-key", /[A-Z][A-Za-z0-9_]*(?:\s[A-Z][A-Za-z0-9_]*)*:(?=\s|$)/],
    ["lg-ok", /\b(?:True|passed|completed|complete|done|success|successful|OK)\b/],
    ["lg-bad", /\b(?:False|failed|failure|Error|error|Traceback|Warning|warning)\b/],
    ["lg-num", /\b\d+(?:\.\d+)?(?:s|ms|%)?\b/],
];

const LOG_RE = new RegExp(LOG_PATTERNS.map((p) => "(" + p[1].source + ")").join("|"), "g");

function highlightSegment(text) {
    let out = "";
    let last = 0;
    let m;
    LOG_RE.lastIndex = 0;
    while ((m = LOG_RE.exec(text)) !== null) {
        if (m.index > last) out += escapeHtml(text.slice(last, m.index));
        let cls = "lg-num";
        for (let i = 1; i < m.length; i++) {
            if (m[i] !== undefined) { cls = LOG_PATTERNS[i - 1][0]; break; }
        }
        out += '<span class="' + cls + '">' + escapeHtml(m[0]) + "</span>";
        last = m.index + m[0].length;
        if (LOG_RE.lastIndex === m.index) LOG_RE.lastIndex++;
    }
    if (last < text.length) out += escapeHtml(text.slice(last));
    return out;
}

function highlightLog(message) {
    const text = message == null ? "" : String(message);
    const idx = text.indexOf(" | ");
    if (idx === -1) return highlightSegment(text);
    const name = text.slice(0, idx);
    const rest = text.slice(idx + 3);
    return '<span class="lg-name">' + escapeHtml(name) + "</span>" +
        '<span class="lg-bar"> | </span>' + highlightSegment(rest);
}

function applyState(state) {
    syncAutoExpand(state.stages);
    renderStages(state.stages);
    updateProgress(state.stages);
    const running = Boolean(state.running);
    $("start-btn").disabled = running;
    $("stop-btn").disabled = !running;
    const overall = $("overall-status");
    if (running) {
        const active = (state.stages || []).find((s) => s.status === "active");
        setPill(overall, active ? active.label : "Running…", "live");
        setStatus("Migration running…", "");
    } else if (state.error && state.error !== "stopped") {
        setPill(overall, "Failed", "error");
        setStatus("Error: " + state.error, "error");
    } else if (state.error === "stopped") {
        setPill(overall, "Stopped", "idle");
        setStatus("Stopped.", "");
    } else if (state.final_path) {
        setPill(overall, "Completed", "done");
        setStatus("Done. Final repo: " + state.final_path, "ok");
    } else {
        setPill(overall, "Idle", "idle");
    }
}

let configPath = null;

async function loadConfig() {
    try {
        const res = await fetch("/api/config");
        const data = await res.json();
        fillForm(data.config || {});
        configPath = data.config_path || null;
        $("config-path").textContent = configPath
            ? "Config: " + configPath
            : "No saved config yet for this directory.";
        $("config-open").disabled = !configPath;
    } catch (e) {
        setStatus("Failed to load config: " + e, "error");
    }
}

async function start() {
    setStatus("Starting…", "");
    try {
        const res = await fetch("/api/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config: buildConfig() }),
        });
        const data = await res.json();
        if (data.status !== "success") setStatus(data.message, "error");
    } catch (e) {
        setStatus("Start failed: " + e, "error");
    }
}

async function stop() {
    try {
        const res = await fetch("/api/stop", { method: "POST" });
        const data = await res.json();
        setStatus(data.message, data.status === "success" ? "" : "error");
    } catch (e) {
        setStatus("Stop failed: " + e, "error");
    }
}

function connectEvents() {
    const source = new EventSource("/api/events");
    source.onopen = () => setPill($("conn-pill"), "Live", "live");
    source.onmessage = (msg) => {
        let event;
        try { event = JSON.parse(msg.data); } catch (e) { return; }
        if (event.type === "state") applyState(event);
        else if (event.type === "log") appendLog(event);
        else if (event.type === "error") appendLog({ level: "error", message: event.message, ts: event.ts });
    };
    source.onerror = () => setPill($("conn-pill"), "Reconnecting…", "error");
}

$("start-btn").addEventListener("click", start);
$("stop-btn").addEventListener("click", stop);
$("log-clear").addEventListener("click", clearLog);
$("stages").addEventListener("click", (e) => {
    const openBtn = e.target.closest(".open-btn");
    if (openBtn) { openPath(openBtn.dataset.path); return; }
    const row = e.target.closest("[data-toggle]");
    if (row) toggleStage(row.dataset.toggle);
});
$("config-open").addEventListener("click", () => { if (configPath) openPath(configPath); });
$("api-key-toggle").addEventListener("click", () => {
    const input = $("api-key");
    const btn = $("api-key-toggle");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "hide" : "show";
});
toggles.forEach(([cb]) => {
    const checkbox = $(cb);
    if (checkbox) checkbox.addEventListener("change", syncToggles);
});

clearLog();
syncToggles();
loadConfig();
connectEvents();

// VideoAgent · Timeline UI. Vanilla ES module.
const root = document.getElementById("app");
let state = {
  instruction: "",
  duration: 60,
  job: null,
  busy: false,
};

function el(t, a = {}, ...c) {
  const e = document.createElement(t);
  for (const [k, v] of Object.entries(a)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v != null) e.setAttribute(k, v);
  }
  for (const x of c.flat()) {
    if (x == null) continue;
    e.append(typeof x === "string" || typeof x === "number" ? document.createTextNode(String(x)) : x);
  }
  return e;
}
function svg(tag, attrs = {}, ...children) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  for (const c of children.flat()) if (c) e.append(c);
  return e;
}

async function submit() {
  if (!state.instruction.trim() || state.busy) return;
  state.busy = true;
  state.job = null;
  render();
  try {
    const r = await fetch("/v1/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_url: "demo://source.mp4",
        instruction: state.instruction,
        duration_s: state.duration,
      }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const { job_id } = await r.json();
    const detail = await (await fetch(`/v1/jobs/${job_id}`)).json();
    state.job = detail;
  } catch (err) {
    state.job = { status: "failed", error: err.message };
  } finally {
    state.busy = false;
    render();
  }
}

function render() {
  root.innerHTML = "";
  root.append(topbar(), main());
}

function topbar() {
  const status = state.job?.status || (state.busy ? "planning…" : "ready");
  const cls = status === "ready" ? "ok" : status === "failed" ? "err" : "warn";
  return el(
    "div",
    { class: "topbar" },
    el("div", { class: "brand" },
      el("div", {}, "video", el("b", {}, "Agent")),
      el("small", {}, "natural-language NLE")),
    el("div", { class: "status" },
      "STATUS ", el("b", { class: cls }, String(status).toUpperCase())),
    el("div", { class: "status" },
      "JOB ", el("b", {}, state.job?.id ? state.job.id.slice(-6).toUpperCase() : "—"),
    ),
  );
}

function main() {
  return el("main", {}, composer(), work(), foot());
}

function composer() {
  return el(
    "div",
    { class: "composer" },
    el("div", { class: "label" }, "INSTRUCTION"),
    el("textarea", {
      placeholder: "e.g. cut the first 10 seconds and add a fade transition at 1:30",
      value: state.instruction,
      onInput: (e) => { state.instruction = e.target.value; },
      onKeydown: (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); },
    }),
    el("div", { class: "row" },
      el("div", { class: "meta" },
        "SOURCE DURATION ",
        el("input", {
          type: "number", min: "1", step: "0.1",
          value: String(state.duration),
          onInput: (e) => { state.duration = parseFloat(e.target.value) || 0; },
        }),
        " s",
      ),
      el("button", { class: "submit", onClick: submit, disabled: state.busy ? "" : null },
        state.busy ? "Planning…" : "Plan ⌘↩"),
    ),
  );
}

function work() {
  const job = state.job;
  return el(
    "div",
    { class: "work" },
    timelineView(job),
    inspectorView(job),
  );
}

function timelineView(job) {
  const ops = job?.plan?.ops || [];
  const dur = state.duration || 60;
  const W = 720, H = 220, PAD = 36;
  const x = (t) => PAD + ((W - 2 * PAD) * t) / dur;
  const w = (t) => ((W - 2 * PAD) * t) / dur;

  const sv = svg("svg", { class: "tl-svg", viewBox: `0 0 ${W} ${H}` });
  // Ruler ticks every 5s.
  for (let s = 0; s <= dur; s += Math.max(1, Math.floor(dur / 12))) {
    const px = x(s);
    sv.append(svg("line", { class: "ruler-line", x1: px, x2: px, y1: 18, y2: H - 18 }));
    const t = svg("text", { class: "ruler-text", x: px, y: 14, "text-anchor": "middle" });
    t.textContent = formatTime(s);
    sv.append(t);
  }
  // Source strip background.
  const stripY = 40, stripH = 80;
  sv.append(svg("rect", { class: "strip-bg", x: PAD, y: stripY, width: W - 2 * PAD, height: stripH }));
  // The single source clip.
  sv.append(svg("rect", { class: "clip", x: PAD, y: stripY, width: w(dur), height: stripH, rx: 3 }));
  const lab = svg("text", { class: "clip-text", x: PAD + 8, y: stripY + 18 });
  lab.textContent = "SOURCE";
  sv.append(lab);

  // Overlay each op.
  for (const op of ops) {
    if (op.op === "cut") {
      sv.append(svg("rect", {
        class: "cut", x: x(op.start_s), y: stripY,
        width: w(op.end_s - op.start_s), height: stripH,
      }));
      const t = svg("text", { class: "fade-text", x: x(op.start_s) + 4, y: stripY + stripH + 14 });
      t.textContent = `cut ${formatTime(op.start_s)}–${formatTime(op.end_s)}`;
      sv.append(t);
    } else if (op.op === "fade_in" || op.op === "fade_out") {
      sv.append(svg("rect", {
        class: "fade", x: x(op.at_s), y: stripY,
        width: w(op.duration_s), height: stripH,
      }));
      const t = svg("text", { class: "fade-text", x: x(op.at_s) + 4, y: stripY + stripH + 14 });
      t.textContent = `${op.op} ${formatTime(op.at_s)} +${op.duration_s}s`;
      sv.append(t);
    } else if (op.op === "trim") {
      // Show as the surviving region; mask everything else.
      sv.append(svg("rect", {
        class: "cut", x: PAD, y: stripY,
        width: w(op.keep_start_s), height: stripH,
      }));
      sv.append(svg("rect", {
        class: "cut", x: x(op.keep_end_s), y: stripY,
        width: w(dur - op.keep_end_s), height: stripH,
      }));
      const t = svg("text", { class: "fade-text", x: x(op.keep_start_s) + 4, y: stripY + stripH + 14 });
      t.textContent = `trim ${formatTime(op.keep_start_s)}–${formatTime(op.keep_end_s)}`;
      sv.append(t);
    }
  }

  // Audio strip background (placeholder lane for non-video ops).
  const audY = stripY + stripH + 40, audH = 30;
  sv.append(svg("rect", { class: "strip-bg", x: PAD, y: audY, width: W - 2 * PAD, height: audH }));
  const aLab = svg("text", { class: "strip-label", x: PAD + 8, y: audY + 18 });
  aLab.textContent = "AUDIO";
  sv.append(aLab);

  return el(
    "div",
    { class: "timeline" },
    el("header", {},
      el("span", {}, "Timeline"),
      el("span", { class: "duration" }, `${formatTime(dur)} total`)),
    sv,
  );
}

function inspectorView(job) {
  const ops = job?.plan?.ops || [];
  return el(
    "div",
    { class: "inspector" },
    el("h3", {}, "Operations"),
    el(
      "div",
      { class: "op-list" },
      ops.length === 0
        ? el("div", { class: "op-row empty" }, "No operations yet — type an instruction above.")
        : ops.map((op) =>
            el("div", { class: "op-row" },
              el("b", {}, op.op),
              el("div", { class: "args" }, JSON.stringify(omitOp(op))),
            ),
          ),
    ),
    job?.error ? el("div", { class: "error" }, "Error: " + job.error) : null,
  );
}

function omitOp(op) {
  const { op: _ign, ...rest } = op;
  return rest;
}

function formatTime(s) {
  if (!isFinite(s)) return "—";
  const mins = Math.floor(s / 60);
  const secs = (s - mins * 60).toFixed(s < 10 ? 1 : 0);
  return mins ? `${mins}:${String(secs).padStart(s < 60 + 10 ? 4 : 2, "0")}` : `${secs}s`;
}

function foot() {
  const job = state.job;
  return el("footer", { class: "foot" },
    el("span", {}, "ENGINE ", el("b", {}, "videoagent · v0.1")),
    el("span", {}, "OPS ", el("b", {}, String(job?.plan?.ops?.length ?? 0))),
    el("span", {}, "REPLANS ", el("b", {}, String(job?.raw_calls ? Math.max(0, job.raw_calls.length - (job.plan?.ops?.length ?? 0)) : 0))),
  );
}

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
});

render();

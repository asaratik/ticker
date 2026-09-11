// Ticker's page. Talks only to /ui/* on the server that served it, and puts
// every server-supplied string into the DOM as text, never as markup.
"use strict";

(() => {
  const LIVE_EVERY_MS = 1000;
  const STATE_EVERY_MS = 5000;
  const GAP_MS = 10000;          // a pause longer than this breaks the line
  const STALE_PULL_MS = 2 * 86400000;

  const METRIC_NAMES = {
    heart_rate_bpm: "Heart rate", rr_interval_ms: "Beat-to-beat interval",
    hrv_rmssd_ms: "HRV (RMSSD)", spo2_pct: "Blood oxygen",
    respiratory_rate_bpm: "Breathing rate", skin_temp_delta_c: "Skin temperature change",
    steps: "Steps", active_energy_kcal: "Active energy", sleep_stage: "Sleep stages",
    sleep_duration_s: "Sleep duration", weight_kg: "Weight", body_fat_pct: "Body fat",
  };

  const $ = (id) => document.getElementById(id);
  const token = takeToken();
  let live = null;
  let state = null;
  let stopped = false;
  let hoverT = null;            // time of the reading under the pointer or focus

  // -- plumbing ------------------------------------------------------------

  function takeToken() {
    // A remote page is opened as /?token=...; keep it for this tab only and
    // take it out of the address bar and history.
    const url = new URL(location.href);
    const given = url.searchParams.get("token");
    if (given) {
      try { sessionStorage.setItem("ticker-token", given); } catch (e) { /* private mode */ }
      url.searchParams.delete("token");
      history.replaceState(null, "", url.pathname + url.search + url.hash);
      return given;
    }
    try { return sessionStorage.getItem("ticker-token"); } catch (e) { return null; }
  }

  async function call(method, path, body) {
    const headers = {};
    if (token) headers["X-Ticker-Token"] = token;
    const init = { method, headers, cache: "no-store" };
    if (method === "POST") {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body || {});
    }
    const response = await fetch(path, init);
    let payload = null;
    try { payload = await response.json(); } catch (e) { /* no body */ }
    if (!response.ok) {
      const error = new Error((payload && payload.error) || response.statusText);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function h(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function svg(tag, attrs, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function badge(tone, icon, label) {
    return h("span", { class: "badge " + tone, role: "img", "aria-label": label, title: label, text: icon });
  }

  function ago(iso) {
    if (!iso) return "never";
    const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (seconds < 90) return "just now";
    if (seconds < 5400) return Math.round(seconds / 60) + " min ago";
    if (seconds < 129600) return Math.round(seconds / 3600) + " h ago";
    return Math.round(seconds / 86400) + " days ago";
  }

  function clock(ms) {
    return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function duration(seconds) {
    const h_ = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h_ ? h_ + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
  }

  function day(iso) { return iso ? iso.slice(0, 10) : "--"; }

  function say(id, text, ok) {
    const node = $(id);
    node.textContent = text || "";
    node.className = "form-message" + (text ? (ok ? " ok" : " bad") : "");
  }

  // -- live ------------------------------------------------------------------

  const LIVE_WORDS = {
    off: ["none", "–", "Off"],
    starting: ["warning", "…", "Starting"],
    searching: ["warning", "…", "Searching"],
    reconnecting: ["warning", "!", "Reconnecting"],
    connected: ["good", "✓", "Connected"],
    stopped: ["none", "–", "Stopped"],
    error: ["critical", "×", "Error"],
  };

  function renderLive(data) {
    live = data;
    for (const button of $("live-source").querySelectorAll("button")) {
      const on = button.dataset.source === data.source;
      button.setAttribute("aria-checked", on ? "true" : "false");
      button.disabled = data.pinned && !on;
    }

    const [tone, icon, word] = LIVE_WORDS[data.status] || ["warning", "?", data.status];
    const line = $("live-status");
    line.textContent = "";
    let text = data.message || word;
    if (data.status === "connected" && data.device) text = "Connected to " + data.device;
    if (data.source === "off") text = "No live device";
    line.append(badge(tone, icon, word), h("span", { text }));
    if (data.error) line.append(h("span", { class: "error-text", text: data.error }));

    $("bpm").textContent = data.bpm === null || data.bpm === undefined ? "--" : String(data.bpm);

    const session = data.session;
    const toggle = $("session-toggle");
    const label = $("session-label");
    if (session) {
      toggle.textContent = "Stop session";
      toggle.disabled = false;
      label.disabled = true;
      if (document.activeElement !== label) label.value = session.label || "";
      $("session-tiles").hidden = false;
      $("tile-avg").textContent = session.avg ?? "--";
      $("tile-min").textContent = session.min ?? "--";
      $("tile-max").textContent = session.max ?? "--";
      $("tile-time").textContent = duration(session.elapsed_s);
    } else {
      toggle.textContent = "Start session";
      toggle.disabled = data.status !== "connected" || !data.logging;
      if (label.disabled) { label.disabled = false; label.value = ""; }
      $("session-tiles").hidden = true;
    }

    const hints = {
      off: "Turn on a strap or watch to see live heart rate. Readings are saved only while a session runs.",
      ble: "Put the strap on (most sleep until they touch skin), or turn on Broadcast Heart Rate on a Garmin watch. Straps allow one or two connections, so close other apps using it.",
      http: "Point the Ticker Bridge app on your watch at the address above.",
    };
    $("live-hint").textContent = data.pinned
      ? "The live source is set by HRM_SOURCE in the environment."
      : (hints[data.source] || "");

    renderChart(data.points || [], (data.window_s || 300) * 1000);
    if (!$("chart-table").hidden) renderChartTable(data.points || []);
  }

  // -- the chart -------------------------------------------------------------

  const H = 200, M = { l: 38, r: 14, t: 12, b: 24 };
  let W = 640;                  // follows the card: drawn at real pixels, so text never shrinks
  let chart = null;             // what the last render drew, for hover

  function renderChart(points, windowMs) {
    const box = $("chart");
    const tip = $("chart-tip");
    for (const node of [...box.childNodes]) if (node !== tip) node.remove();
    W = Math.max(280, Math.round(box.clientWidth || 640));
    const now = Date.now();
    const x0 = now - windowMs;
    const shown = points.filter((p) => p[0] >= x0);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "chart-svg", role: "img",
      "aria-label": shown.length ? `Heart rate, last 5 minutes: ${shown.length} readings, latest ${shown[shown.length - 1][1]} bpm`
                                 : "Heart rate, last 5 minutes: no readings" });
    box.prepend(root);
    chart = null;

    if (!shown.length) {
      root.append(svg("line", { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, class: "axis-line" }));
      root.append(svg("text", { x: (W + M.l) / 2, y: H / 2, class: "empty", "text-anchor": "middle" },
        live && live.source === "off" ? "Turn on a live device to see readings" : "No readings in the last 5 minutes"));
      tip.hidden = true;
      return;
    }

    const values = shown.map((p) => p[1]);
    let lo = Math.floor((Math.min(...values) - 3) / 5) * 5;
    let hi = Math.ceil((Math.max(...values) + 3) / 5) * 5;
    if (hi - lo < 20) { const mid = (hi + lo) / 2; lo = Math.floor((mid - 10) / 5) * 5; hi = lo + 20; }
    const X = (t) => M.l + ((t - x0) / windowMs) * (W - M.l - M.r);
    const Y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * (H - M.t - M.b);

    for (const tick of [...new Set([lo, Math.round((lo + hi) / 10) * 5, hi])]) {
      root.append(svg("line", { x1: M.l, x2: W - M.r, y1: Y(tick), y2: Y(tick), class: tick === lo ? "axis-line" : "grid-line" }));
      root.append(svg("text", { x: M.l - 8, y: Y(tick) + 4, class: "tick", "text-anchor": "end" }, String(tick)));
    }
    const marks = W < 460 ? [[5, "start"], [3, "middle"], [1, "middle"], [0, "end"]]
      : [[5, "start"], [4, "middle"], [3, "middle"], [2, "middle"], [1, "middle"], [0, "end"]];
    for (const [minutes, anchor] of marks) {
      const t = now - minutes * 60000;
      if (t < x0) continue;
      root.append(svg("text", { x: X(t), y: H - 6, class: "tick", "text-anchor": anchor },
        minutes === 0 ? "now" : "−" + minutes + " min"));
    }

    let d = "";
    let previous = null;
    for (const [t, v] of shown) {
      d += (previous === null || t - previous > GAP_MS ? "M" : "L") + X(t).toFixed(1) + "," + Y(v).toFixed(1);
      previous = t;
    }
    root.append(svg("path", { d, class: "line" }));
    const [lastT, lastV] = shown[shown.length - 1];
    root.append(svg("circle", { cx: X(lastT), cy: Y(lastV), r: 4, class: "end-dot" }));

    const crosshair = svg("line", { y1: M.t, y2: H - M.b, class: "crosshair", visibility: "hidden" });
    const dot = svg("circle", { r: 4.5, class: "hover-dot", visibility: "hidden" });
    const hit = svg("rect", { x: M.l, y: 0, width: W - M.l - M.r, height: H, class: "hit" });
    root.append(crosshair, dot, hit);
    chart = { shown, X, Y, crosshair, dot, root };

    hit.addEventListener("pointermove", (event) => {
      const rect = root.getBoundingClientRect();
      const x = ((event.clientX - rect.left) / rect.width) * W;
      const t = x0 + ((x - M.l) / (W - M.l - M.r)) * windowMs;
      hoverT = nearest(shown, t)[0];
      showHover();
    });
    hit.addEventListener("pointerleave", () => { hoverT = null; showHover(); });
    showHover();
  }

  function nearest(points, t) {
    let lo = 0, hi = points.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (points[mid][0] < t) lo = mid; else hi = mid;
    }
    return Math.abs(points[lo][0] - t) <= Math.abs(points[hi][0] - t) ? points[lo] : points[hi];
  }

  function showHover() {
    const tip = $("chart-tip");
    if (!chart || hoverT === null) {
      tip.hidden = true;
      if (chart) { chart.crosshair.setAttribute("visibility", "hidden"); chart.dot.setAttribute("visibility", "hidden"); }
      return;
    }
    const [t, v] = nearest(chart.shown, hoverT);
    const x = chart.X(t), y = chart.Y(v);
    chart.crosshair.setAttribute("x1", x); chart.crosshair.setAttribute("x2", x);
    chart.crosshair.setAttribute("visibility", "visible");
    chart.dot.setAttribute("cx", x); chart.dot.setAttribute("cy", y);
    chart.dot.setAttribute("visibility", "visible");

    tip.textContent = "";
    tip.append(h("span", { class: "tooltip-value" }, h("span", { class: "tooltip-key" }), v + " bpm"),
               h("span", { class: "tooltip-label", text: clock(t) }));
    tip.hidden = false;
    const scale = chart.root.getBoundingClientRect().width / W;
    const left = x * scale, top = y * scale;
    const width = tip.offsetWidth;
    tip.style.transform = `translate(${Math.min(Math.max(left - width / 2, 0), chart.root.getBoundingClientRect().width - width)}px, ${Math.max(top - tip.offsetHeight - 12, 0)}px)`;
  }

  function renderChartTable(points) {
    // The chart's accessible twin: 30-second buckets, newest first.
    const buckets = new Map();
    for (const [t, v] of points) {
      const key = Math.floor(t / 30000) * 30000;
      const b = buckets.get(key) || { n: 0, sum: 0, min: v, max: v };
      b.n += 1; b.sum += v; b.min = Math.min(b.min, v); b.max = Math.max(b.max, v);
      buckets.set(key, b);
    }
    const rows = [...buckets.entries()].sort((a, b) => b[0] - a[0]).map(([key, b]) =>
      h("tr", null, h("td", { text: clock(key) }), h("td", { class: "num", text: String(Math.round(b.sum / b.n)) }),
        h("td", { class: "num", text: String(b.min) }), h("td", { class: "num", text: String(b.max) }),
        h("td", { class: "num", text: String(b.n) })));
    const table = h("table", { class: "data-table" },
      h("thead", null, h("tr", null, h("th", { text: "From" }), h("th", { class: "num", text: "Avg bpm" }),
        h("th", { class: "num", text: "Low" }), h("th", { class: "num", text: "High" }), h("th", { class: "num", text: "Readings" }))),
      h("tbody", null, rows.length ? rows : h("tr", null, h("td", { colspan: "5", class: "muted", text: "No readings yet" }))));
    $("chart-table").replaceChildren(table);
  }

  // -- everything else -------------------------------------------------------

  function sourceStatus(src) {
    if (src.problem) return ["critical", "×", "Not syncing: " + src.problem];
    const errors = Object.values(src.sync_errors || {});
    if (errors.length) return ["critical", "×", "Last sync failed"];
    if (!src.enabled) return ["none", "–", "Disconnected"];
    if (src.syncing) return ["warning", "…", "Syncing"];
    if (!src.last_data) return ["none", "–", "No data yet"];
    if (src.type === "cloud sync" && Date.now() - Date.parse(src.last_data) > STALE_PULL_MS)
      return ["serious", "!", "Stale"];
    return ["good", "✓", "Up to date"];
  }

  function renderSources(data) {
    const shown = (data.sources || []).filter((s) => s.last_data || s.type === "cloud sync");
    const list = $("sources");
    if (!shown.length) {
      list.replaceChildren(h("li", { class: "empty-state", text: "Nothing connected yet. Connect an account below, or turn on a live device." }));
      return;
    }
    list.replaceChildren(...shown.map((src) => {
      const [tone, icon, word] = sourceStatus(src);
      const meta = [word];
      if (src.last_data) meta.push("last data " + ago(src.last_data));
      if (src.last_sync) meta.push("synced " + ago(src.last_sync));
      // One line per distinct failure: a missing token fails every metric
      // the same way, and six copies of it say nothing six times.
      const byMessage = new Map();
      for (const [metric, message] of Object.entries(src.sync_errors || {})) {
        if (!byMessage.has(message)) byMessage.set(message, []);
        byMessage.get(message).push(METRIC_NAMES[metric] || metric);
      }
      const errors = [...byMessage.entries()].slice(0, 2).map(([message, metrics]) =>
        h("div", { class: "error-text", text: (byMessage.size > 1 ? metrics.join(", ") + ": " : "")
          + message + (/keyring|token|401|403/i.test(message) ? " Reconnect it under Connect." : "") }));
      const actions = [];
      if (src.type === "cloud sync" && src.enabled) {
        actions.push(h("button", { class: "btn small", type: "button", disabled: src.syncing,
          text: src.syncing ? "Syncing…" : "Sync now", onclick: () => syncNow(src.id) }));
        actions.push(h("button", { class: "btn small ghost", type: "button", text: "Disconnect",
          onclick: () => disconnect(src) }));
      }
      return h("li", { class: "source" }, badge(tone, icon, word),
        h("div", { class: "source-main" },
          h("div", { class: "source-name" }, src.name, h("span", { class: "muted", text: " · " + src.type })),
          h("div", { class: "source-meta", text: meta.join(" · ") }),
          src.devices ? h("div", { class: "source-meta", text: src.devices.join(", ") }) : null,
          errors),
        actions.length ? h("div", { class: "source-actions" }, actions) : null);
    }));
  }

  function renderJobs(data) {
    const jobs = (data.jobs || []).slice(0, 5);
    $("jobs").replaceChildren(...jobs.map((job) => {
      const tone = { running: ["warning", "…", "Running"], done: ["good", "✓", "Done"], failed: ["critical", "×", "Failed"],
                     needs_code: ["serious", "!", "Waiting for you"] }[job.status]
        || ["none", "?", job.status];
      if (job.kind === "garmin" && job.status === "needs_code") showGarminCode(job.id);
      let detail = job.detail || "";
      if (job.status === "running" && job.progress) detail = job.progress.toLocaleString() + " records read";
      return h("li", { class: "job" }, badge(...tone),
        h("div", null, h("div", { text: job.title }), detail ? h("div", { class: "job-detail", text: detail }) : null));
    }));
  }

  function renderMetrics(data) {
    const metrics = data.metrics || [];
    if (!metrics.length) {
      $("metrics").replaceChildren(h("p", { class: "empty-state", text: "No data yet." }));
    } else {
      $("metrics").replaceChildren(h("table", { class: "data-table" },
        h("thead", null, h("tr", null, h("th", { text: "Metric" }), h("th", { text: "Unit" }),
          h("th", { text: "From" }), h("th", { text: "To" }), h("th", { class: "num", text: "Days" }),
          h("th", { text: "Sources" }))),
        h("tbody", null, metrics.map((m) => h("tr", null,
          h("td", null, METRIC_NAMES[m.metric] || m.metric, h("span", { class: "sub", text: m.metric })),
          h("td", { text: m.unit }), h("td", { text: day(m.first) }), h("td", { text: day(m.last) }),
          h("td", { class: "num", text: m.days_with_data === null ? "--" : String(m.days_with_data) }),
          h("td", { text: (m.sources || []).join(", ") }))))));
    }
    $("notes").replaceChildren(...(data.notes || []).map((note) =>
      h("li", { class: "note" }, badge("serious", "!", "Note"), h("span", { text: note }))));
  }

  function renderState(data) {
    state = data;
    const app = data.app || {};
    $("app-status").textContent = app.problem ? app.problem
      : "Running since " + (app.started ? new Date(app.started).toLocaleString() : "just now") + " · " + (app.url || "");
    renderSources(data);
    renderJobs(data);
    renderMetrics(data);
    const agents = data.agents || {};
    $("agent-claude").textContent = agents.claude_code || "";
    $("agent-codex").textContent = agents.codex || "";
    $("agent-desktop").textContent = agents.claude_desktop || "";
    $("agent-http").textContent = agents.http || "";
    $("agent-local").textContent = agents.local || "";
    const connect = data.connect || {};
    $("fitbit-button").disabled = !connect.fitbit || !connect.keyring;
    $("garmin-button").disabled = !connect.garmin || !connect.keyring;
    if (!connect.garmin && connect.garmin_note) $("garmin-note").textContent = connect.garmin_note;
    $("fitbit-note").textContent = !connect.fitbit
      ? "Fitbit needs an application client id first: register one at dev.fitbit.com, set TICKER_FITBIT_CLIENT_ID and restart Ticker."
      : "Opens Fitbit's sign-in in a new tab; Ticker picks the account up once you approve.";
    $("foot").textContent = "Ticker " + (app.version || "") + " · data in " + (app.db || "?")
      + (app.timezone ? " · times in " + app.timezone : "");
  }

  // -- actions ---------------------------------------------------------------

  let garminJob = null;

  function showGarminCode(jobId) {
    garminJob = jobId;
    $("connect-garmin").open = true;
    $("garmin-code-form").hidden = false;
  }

  async function syncNow(id) {
    try {
      const reply = await call("POST", "/ui/sync/" + id);
      say("connect-message", reply.result.queued ? "Syncing now." : "Already syncing.", true);
    } catch (e) { say("connect-message", e.message, false); }
    refreshState();
  }

  async function disconnect(src) {
    if (!confirm(`Disconnect ${src.name}? Ticker forgets its credentials and stops syncing; data already recorded stays.`)) return;
    try {
      await call("POST", "/ui/disconnect/" + src.id);
      say("connect-message", src.name + " disconnected. Its data is still here.", true);
    } catch (e) { say("connect-message", e.message, false); }
    refreshState();
  }

  function wire() {
    for (const button of $("live-source").querySelectorAll("button")) {
      button.addEventListener("click", async () => {
        try { renderLive((await call("POST", "/ui/live/source", { source: button.dataset.source })).result); }
        catch (e) { say("connect-message", e.message, false); }
      });
    }

    $("session-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const running = live && live.session;
      try {
        const reply = running ? await call("POST", "/ui/session/stop")
                              : await call("POST", "/ui/session/start", { label: $("session-label").value });
        renderLive(reply.result);
      } catch (e) { $("live-hint").textContent = e.message; }
    });

    $("oura-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const reply = await call("POST", "/ui/connect/oura",
          { token: $("oura-token").value, name: $("oura-name").value });
        $("oura-token").value = "";
        $("connect-oura").open = false;
        say("connect-message", `Connected ${reply.result.name}. The first sync has started.`, true);
      } catch (e) { say("connect-message", e.message, false); }
      refreshState();
    });

    $("fitbit-button").addEventListener("click", async () => {
      // Opened now, inside the click, so the browser doesn't treat it as a
      // popup; pointed at Fitbit once the server has the sign-in URL.
      const tab = window.open("about:blank", "_blank");
      try {
        const reply = await call("POST", "/ui/connect/fitbit", {});
        if (tab) { tab.opener = null; tab.location.href = reply.result.url; }
        const node = $("connect-message");
        node.className = "form-message ok";
        node.replaceChildren("Finish signing in to Fitbit in the new tab. ",
          h("a", { href: reply.result.url, target: "_blank", rel: "noopener noreferrer", text: "Open it again" }));
      } catch (e) {
        if (tab) tab.close();
        say("connect-message", e.message, false);
      }
      refreshState();
    });

    $("garmin-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      $("garmin-button").disabled = true;
      say("connect-message", "Signing in to Garmin…", true);
      try {
        const reply = await call("POST", "/ui/connect/garmin",
          { email: $("garmin-email").value, password: $("garmin-password").value });
        $("garmin-password").value = "";
        const job = reply.result;
        if (job.status === "needs_code") {
          showGarminCode(job.job_id);
          say("connect-message", "Garmin sent you a code. Enter it to finish signing in.", true);
        } else if (job.status === "failed") {
          say("connect-message", job.detail || "Garmin sign-in failed.", false);
        } else {
          say("connect-message", "Signed in to Garmin. The first sync has started.", true);
        }
      } catch (e) { say("connect-message", e.message, false); }
      refreshState();
    });

    $("garmin-code-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await call("POST", "/ui/connect/garmin/code", { job_id: garminJob, code: $("garmin-code").value });
        $("garmin-code").value = "";
        $("garmin-code-form").hidden = true;
        say("connect-message", "Code sent. Finishing the sign-in…", true);
      } catch (e) { say("connect-message", e.message, false); }
      refreshState();
    });

    $("apple-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await call("POST", "/ui/import", { path: $("apple-path").value });
        say("connect-message", "Importing. Large exports take a few minutes; progress shows above.", true);
      } catch (e) { say("connect-message", e.message, false); }
      refreshState();
    });

    for (const button of document.querySelectorAll("[data-copy]")) {
      button.addEventListener("click", async () => {
        const text = $(button.dataset.copy).textContent;
        try { await navigator.clipboard.writeText(text); button.textContent = "Copied"; }
        catch (e) {
          const range = document.createRange();
          range.selectNodeContents($(button.dataset.copy));
          getSelection().removeAllRanges(); getSelection().addRange(range);
          button.textContent = "Selected";
        }
        setTimeout(() => { button.textContent = "Copy"; }, 1500);
      });
    }

    $("chart-table-toggle").addEventListener("click", () => {
      const table = $("chart-table");
      table.hidden = !table.hidden;
      $("chart-table-toggle").setAttribute("aria-expanded", String(!table.hidden));
      if (!table.hidden && live) renderChartTable(live.points || []);
    });

    $("chart").addEventListener("keydown", (event) => {
      if (!chart || !["ArrowLeft", "ArrowRight", "Home", "End", "Escape"].includes(event.key)) return;
      event.preventDefault();
      const shown = chart.shown;
      let index = hoverT === null ? shown.length - 1 : shown.indexOf(nearest(shown, hoverT));
      if (event.key === "ArrowLeft") index = Math.max(0, index - 1);
      if (event.key === "ArrowRight") index = Math.min(shown.length - 1, index + 1);
      if (event.key === "Home") index = 0;
      if (event.key === "End") index = shown.length - 1;
      hoverT = event.key === "Escape" ? null : shown[index][0];
      showHover();
    });
    $("chart").addEventListener("focus", () => {
      if (chart && hoverT === null) { hoverT = chart.shown[chart.shown.length - 1][0]; showHover(); }
    });
    $("chart").addEventListener("blur", () => { hoverT = null; showHover(); });

    $("quit").addEventListener("click", async () => {
      if (!confirm("Stop Ticker? Syncing, recording and agent access stop until you start it again.")) return;
      try { await call("POST", "/ui/quit"); } catch (e) { /* it may already be gone */ }
      stopped = true;
      $("app-status").textContent = "Ticker has stopped. Start it again with `ticker`; you can close this tab.";
      $("main").classList.add("stale");
    });
  }

  // -- the Ask box -------------------------------------------------------------

  const TOOL_WORDS = {
    get_overview: "Checked what's recorded",
    get_daily_summary: "Looked at daily values",
    get_sleep: "Looked at sleep",
    get_timeseries: "Looked at readings",
    list_sessions: "Listed sessions",
    get_session: "Opened a session",
    query_sql: "Ran a query",
  };
  let askConfig = null;
  let askHistory = [];          // [[question, answer], ...], newest last
  let asking = false;

  function inline(text) {
    // **bold** and `code`, as elements; everything else stays text.
    const nodes = [];
    for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
      if (!part) continue;
      if (part.startsWith("**") && part.endsWith("**") && part.length > 4) nodes.push(h("strong", { text: part.slice(2, -2) }));
      else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) nodes.push(h("code", { text: part.slice(1, -1) }));
      else nodes.push(part);
    }
    return nodes;
  }

  function markdown(text) {
    // Enough markdown for a model's answer -- paragraphs, lists, bold, code,
    // headings -- built as DOM nodes, never parsed as HTML.
    const out = document.createDocumentFragment();
    for (const block of text.trim().split(/\n\s*\n/)) {
      const lines = block.split("\n").filter((line) => line.trim());
      if (!lines.length) continue;
      const bullets = lines.every((line) => /^\s*[-*•]\s+/.test(line));
      const numbered = lines.every((line) => /^\s*\d+[.)]\s+/.test(line));
      if (bullets || numbered) {
        out.append(h(numbered ? "ol" : "ul", null, lines.map((line) =>
          h("li", null, inline(line.replace(/^\s*([-*•]|\d+[.)])\s+/, ""))))));
        continue;
      }
      const para = h("p");
      lines.forEach((line, index) => {
        if (index) para.append(h("br"));
        const heading = line.match(/^#{1,6}\s+(.*)$/);
        para.append(...(heading ? [h("strong", { text: heading[1] })] : inline(line)));
      });
      out.append(para);
    }
    return out;
  }

  function stepLine(step) {
    const args = Object.entries(step.arguments || {})
      .map(([key, value]) => key + " " + (Array.isArray(value) ? value.join(", ") : value)).join(" · ");
    return h("li", { class: step.ok ? "" : "failed" },
      (step.ok ? "✓ " : "× ") + (TOOL_WORDS[step.tool] || step.tool) + (args ? " (" + args + ")" : "")
      + (step.error ? " — " + step.error : "") + (step.truncated ? " — cut short" : ""));
  }

  function renderTurn(item, job) {
    item.replaceChildren(h("div", { class: "q", text: job.question }));
    if (job.steps && job.steps.length) item.append(h("ul", { class: "steps" }, job.steps.map(stepLine)));
    if (job.status === "running") item.append(h("div", { class: "pending", text: "Thinking…" }));
    else if (job.status === "failed") item.append(h("div", { class: "error-text", text: job.error || "That didn't work." }));
    else item.append(h("div", { class: "answer" }, markdown(job.answer || "")));
  }

  function renderAskConfig(config) {
    askConfig = config;
    const url = $("ask-url");
    if (document.activeElement !== url) url.value = config.url || "";
    const select = $("ask-model");
    const options = [...new Set([...(config.models || []), ...(config.model ? [config.model] : [])])];
    select.replaceChildren(...(options.length ? options : [""]).map((name) =>
      h("option", { value: name, text: name || "(no models)" })));
    select.value = config.model || options[0] || "";
    for (const control of $("ask-config-form").elements) control.disabled = !!config.pinned;

    let tone = "good", icon = "✓", text;
    const where = config.api === "openai" ? "an OpenAI-compatible server" : "Ollama";
    if (!config.reachable) { tone = "critical"; icon = "×"; text = config.error || "The model server isn't answering."; }
    else if (!config.models.length) { tone = "warning"; icon = "!"; text = "No models on " + config.url + " yet — try `ollama pull qwen3:8b`."; }
    else if (!config.model) { tone = "warning"; icon = "!"; text = "Pick a model under Model settings."; }
    else text = "Answering with " + config.model + " on " + where + (config.local
      ? " — your data stays on this computer."
      : " at " + config.url + " — questions and the data they need go there.");
    if (config.pinned) text += " (Set by TICKER_LLM_URL / TICKER_LLM_MODEL.)";
    $("ask-status").replaceChildren(badge(tone, icon, tone === "good" ? "Ready" : "Needs attention"), h("span", { text }));
    $("ask-button").disabled = asking || !(config.reachable && config.model);
  }

  async function refreshAskConfig() {
    try { renderAskConfig(await call("GET", "/ui/ask/config")); }
    catch (e) { $("ask-status").textContent = e.message; }
  }

  async function askQuestion(question) {
    question = question.trim();
    if (!question || asking) return;
    asking = true;
    $("ask-button").disabled = true;
    $("ask-suggestions").hidden = true;
    const item = h("li", { class: "turn" });
    $("ask-log").append(item);
    renderTurn(item, { question, status: "running", steps: [] });
    try {
      const reply = await call("POST", "/ui/ask", { question, history: askHistory });
      $("ask-input").value = "";
      let job;
      do {
        await new Promise((resolve) => setTimeout(resolve, 800));
        job = await call("GET", "/ui/ask/" + reply.result.id);
        renderTurn(item, job);
      } while (job.status === "running");
      if (job.status === "done") askHistory = [...askHistory, [question, job.answer]].slice(-3);
    } catch (e) {
      renderTurn(item, { question, status: "failed", error: e.message });
    } finally {
      asking = false;
      if (askConfig) renderAskConfig(askConfig);
      item.scrollIntoView({ block: "nearest" });
    }
  }

  function wireAsk() {
    $("ask-form").addEventListener("submit", (event) => {
      event.preventDefault();
      askQuestion($("ask-input").value);
    });
    for (const chip of $("ask-suggestions").querySelectorAll(".chip")) {
      chip.addEventListener("click", () => askQuestion(chip.textContent));
    }
    $("ask-settings-toggle").addEventListener("click", () => {
      const panel = $("ask-settings");
      panel.hidden = !panel.hidden;
      $("ask-settings-toggle").setAttribute("aria-expanded", String(!panel.hidden));
      if (!panel.hidden) refreshAskConfig();
    });
    $("ask-config-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = { url: $("ask-url").value };
      if ($("ask-model").value) body.model = $("ask-model").value;
      try { renderAskConfig((await call("POST", "/ui/ask/config", body)).result); }
      catch (e) { $("ask-status").textContent = e.message; }
    });
    $("ask-model").addEventListener("change", async () => {
      try { renderAskConfig((await call("POST", "/ui/ask/config", { model: $("ask-model").value })).result); }
      catch (e) { $("ask-status").textContent = e.message; }
    });
  }

  // -- polling ---------------------------------------------------------------

  function offline(error) {
    if (stopped) return;
    $("main").classList.add("stale");
    $("app-status").textContent = error && error.status === 401
      ? "This server wants a token: open the page as /?token=…"
      : "Can't reach Ticker. Is it still running? Start it with `ticker`.";
  }

  async function refreshLive() {
    if (stopped) return;
    try { renderLive(await call("GET", "/ui/live")); $("main").classList.remove("stale"); }
    catch (e) { offline(e); }
  }

  async function refreshState() {
    if (stopped) return;
    try { renderState(await call("GET", "/ui/state")); }
    catch (e) { offline(e); }
  }

  wire();
  wireAsk();
  refreshLive();
  refreshState();
  refreshAskConfig();
  setInterval(refreshLive, LIVE_EVERY_MS);
  setInterval(refreshState, STATE_EVERY_MS);
})();

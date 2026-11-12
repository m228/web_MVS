// Страница микроскопа — разломы и калибровка разломов по своим кадрам
// Часть бывшего microscope.js (IIFE снят: файлы microscope/01..04 делят общую глобальную область, порядок <script> важен).
"use strict";

  // ================= Разломы (отдельная подсистема CV) =================
  function fracFill(fr, ap) {
    const set = (id, v) => { const e = $(id); if (e != null && v != null) e.value = v; };
    if (fr) {
      set("fracConfThr", fr.conf_thr); set("fracWDark", fr.w_dark); set("fracWTex", fr.w_tex);
      set("fracDarkThr", fr.dark_thr); set("fracConfirm", fr.confirm_frames); set("fracMinArea", fr.min_area_frac);
      const st = $("fracStatus"); if (st) { st.textContent = fr.enabled === false ? "детект выключен" : "детект включён"; st.className = "micro-cv-status " + (fr.enabled === false ? "off" : "ok"); }
    }
    if (ap) {
      set("apStep", ap.step_um); set("apMinZones", ap.min_zones); set("apMaxGap", ap.max_gap_um);
      set("apMinGap", ap.min_gap_um); set("apCleanN", ap.clean_n); set("apWeakPct", ap.weak_pct);
    }
  }
  // Советы по подводу: по строке СВ-таблицы — проб, разломных, предложенный зазор в обе стороны (только предложения).
  // «дальше» (был разлом) — красным, «ближе» (подряд чистые) — зелёным; галочка записывает зазор в СВ-таблицу, снятие возвращает
  function apRender() {
    const tb = $("apAdvice"); if (!tb) return;
    const q = cvSerialQ() ? "?" + cvSerialQ() : "";
    api("/api/cv/approach/advice" + q).then((a) => {
      const rows = (a && a.rows) || [];
      const f = (v) => (v == null ? "—" : String(Math.round(v)));
      const chk = (r, on, from, to) => {
        const closer = to < from, cls = closer ? "micro-ap-closer" : "micro-ap-adv", word = closer ? "ближе" : "дальше";
        const tip = on ? "применено — снять галочку вернёт " + from : "записать в СВ-таблицу зазор " + to + " для СВ от " + r.sv_from;
        return '<label class="micro-ap-chk" title="' + tip + '"><input type="checkbox" class="' + (closer ? "is-closer" : "is-farther") + '" data-row="' + r.row +
          '" data-from="' + from + '" data-to="' + to + '"' + (on ? " checked" : "") + '><span class="' + cls + '">' + word + " " + from + " → " + to + "</span></label>";
      };
      const muted = (t, tip) => '<span class="micro-ap-cap"' + (tip ? ' title="' + tip + '"' : "") + ">" + t + "</span>";
      const adv = (r) => {
        if (r.applied) return chk(r, true, r.applied.from, r.applied.to);
        if (r.status === "advice" || r.status === "closer") return chk(r, false, Math.round(r.sp), r.proposed);
        if (r.status === "ceiling") return muted("потолок " + Math.round(a.max_gap_um));
        if (r.status === "floor") return muted("минимум " + Math.round(a.min_gap_um));
        if (r.status === "held") return muted("держим", "на " + r.hold.at + " у этой строки часто были разломы (" + r.hold.n_frac + " из " + r.hold.n + ")");
        if (r.status === "ok") return muted("чисто " + r.n_clean_tail + "/" + a.clean_n, "ещё " + (a.clean_n - r.n_clean_tail) + " чистых — и совет «ближе»");
        return "";
      };
      const dir = (r) => { const to = r.applied ? r.applied.to : r.proposed, from = r.applied ? r.applied.from : r.sp;
        return to == null ? (r.status === "ceiling" ? "is-frac" : "") : (to < from ? "is-closer" : "is-frac"); };
      const html = '<tr><th>СВ от</th><th>зазор</th><th>проб</th><th>разлом</th><th title="средний аналоговый датчик на приходе: разломные / чистые">аналог</th><th class="ap-adv-cell">совет</th></tr>' +
        rows.map((r) => { const c = dir(r); return "<tr" + (c ? ' class="' + c + '"' : "") + "><td>" + r.sv_from + "</td><td>" + f(r.sp) + "</td><td>" +
          (r.n || "—") + "</td><td>" + (r.n ? r.n_frac + " (" + Math.round(r.share * 100) + "%)" : "—") + "</td><td>" +
          (r.ai_frac != null || r.ai_ok != null ? f(r.ai_frac) + " / " + f(r.ai_ok) : "—") + '</td><td class="ap-adv-cell">' + adv(r) + "</td></tr>"; }).join("");
      if (tb.__h !== html) { tb.innerHTML = rows.length ? html : "<tr><td>нет строк таблицы</td></tr>"; tb.__h = html; }
    }).catch(() => {});
  }
  // после применения совета — тот же зазор в редакторе таблицы подвода (строка конфига i = i-я по порядку строка с СВ > 0)
  function apSyncTable(row, sp) {
    if (typeof cfg !== "object" || !cfg) return;
    if (cfg.SP) cfg.SP[row] = sp;
    const SVSP = cfg.SVSP || []; let k = -1;
    for (let i = 1; i <= row; i++) if (Number(SVSP[i]) > 0) k++;
    const tr = document.querySelectorAll("#svspBody tr")[k], gap = tr && tr.querySelector(".svsp-gap");
    if (gap) gap.value = sp;
  }
  async function apToggle(box) {
    const row = +box.dataset.row, from = +box.dataset.from, to = +box.dataset.to, on = box.checked;
    const h = $("apSaveHint");
    box.disabled = true;
    try {
      const r = await (await fetch("/api/cv/approach/apply", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ row, sp_from: from, sp_to: to, on }) })).json();
      if (r.status === "ok") { apSyncTable(row, r.sp); if (h) { h.textContent = on ? "записано в таблицу" : "возвращено"; setTimeout(() => (h.textContent = ""), 2500); } }
      else { box.checked = !on; if (h) { h.textContent = r.error || "не применено"; setTimeout(() => (h.textContent = ""), 6000); } }
    } catch (e) { box.checked = !on; if (h) h.textContent = "ошибка связи"; }
    box.disabled = false;
    apRender();
  }
  // время пробы из ts "2026-11-12_10_56_01" → "10:56" (с датой, если не сегодня)
  function fracTsLabel(ts, withDate) {
    const m = String(ts || "").match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})_(\d{2})/); if (!m) return "";
    const today = new Date(), sameDay = +m[1] === today.getFullYear() && +m[2] === today.getMonth() + 1 && +m[3] === today.getDate();
    return (withDate && !sameDay ? m[3] + "." + m[2] + " " : "") + m[4] + ":" + m[5];
  }
  function fracRender() {
    const q = cvSerialQ() ? "?" + cvSerialQ() : "";
    api("/api/cv/last" + q).then((r) => {
      const fr = (r && r.fracture && r.fracture.summary) || null;
      const setT = (id, v) => { const e = $(id); if (e) e.textContent = v; };
      const v = $("fracVerdict");
      setT("fracWhen", r && r.ts ? fracTsLabel(r.ts, true) + (r.sv != null ? " · СВ " + r.sv : "") : "");
      if (!fr) { ["fracZones", "fracArea", "fracConf", "fracVerdict"].forEach((i) => setT(i, "—")); if (v) v.className = "frac-verdict"; return; }
      setT("fracZones", fr.zones != null ? fr.zones : "—");
      setT("fracArea", fr.area_pct != null ? fr.area_pct + " %" : "—");
      setT("fracConf", fr.zones && fr.mean_conf != null ? fr.mean_conf : "—");
      if (v) { v.textContent = fr.has_fracture ? "есть разлом" : "чисто"; v.className = "frac-verdict " + (fr.has_fracture ? "is-frac" : "is-ok"); }
    }).catch(() => {});
  }
  // Тренд: зоны (левая ось) и % площади (правая ось) по последним 100 пробам; наведение — подсказка, клик — кадр пробы в окне CV
  const fracTr = { data: null, geom: null };
  function fracDrawTrend() {
    const cv = $("fracTrend"); if (!cv) return;
    const q = [cvSerialQ(), "series=frac_zones,frac_pct,sv", "limit=100"].filter(Boolean).join("&");
    api("/api/cv/trend?" + q).then((t) => { fracTr.data = t; fracPaint(); }).catch(() => {});
  }
  function fracPaint(hover) {
    const cv = $("fracTrend"), t = fracTr.data; if (!cv || !t) return;
    const sel = [...document.querySelectorAll("#fracSeries input:checked")].map((c) => c.value);
    const dpr = window.devicePixelRatio || 1, W = cv.clientWidth || 620, H = cv.clientHeight || 200;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    const muted = getCss("--muted", "#8a94a6"), grid = getCss("--border", "rgba(131,151,179,.28)");
    const ts = t.ts || [], n = ts.length, S = t.series || {};
    if (!n) { ctx.fillStyle = muted; ctx.font = "12px sans-serif"; ctx.fillText("нет проб", 12, H / 2); fracTr.geom = null; return; }
    const pad = { l: 30, r: 36, t: 10, b: 20 }, pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
    const xOf = (i) => pad.l + pw * (n <= 1 ? 0.5 : i / (n - 1));
    const top = (arr, minTop) => { let mx = minTop; (arr || []).forEach((v) => { if (v != null) mx = Math.max(mx, v); }); return mx; };
    const nice = (v) => { const p = Math.pow(10, Math.floor(Math.log10(v))); return [1, 2, 2.5, 5, 10].map((k) => k * p).find((k) => k >= v) || v; };
    const zMax = nice(top(S.frac_zones, 4)), pMax = nice(top(S.frac_pct, 5));
    const yOf = (v, mx) => pad.t + ph * (1 - v / mx);
    ctx.font = "10px sans-serif"; ctx.lineWidth = 1;
    for (let k = 0; k <= 4; k++) {                                   // сетка + подписи: слева зоны, справа % площади
      const y = pad.t + ph * k / 4;
      ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
      const zv = zMax * (1 - k / 4), pv = pMax * (1 - k / 4);
      ctx.textBaseline = "middle";
      if (sel.includes("frac_zones")) { ctx.fillStyle = "#e24b4a"; ctx.textAlign = "right"; ctx.fillText(String(Math.round(zv * 10) / 10), pad.l - 5, y); }
      if (sel.includes("frac_pct")) { ctx.fillStyle = "#ba7517"; ctx.textAlign = "left"; ctx.fillText(Math.round(pv * 10) / 10 + "%", W - pad.r + 5, y); }
    }
    if (hover != null) { ctx.strokeStyle = muted; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(xOf(hover), pad.t); ctx.lineTo(xOf(hover), pad.t + ph); ctx.stroke(); ctx.setLineDash([]); }
    const line = (arr, mx, col) => {
      ctx.strokeStyle = col; ctx.lineWidth = 1.8; ctx.lineJoin = "round"; ctx.beginPath();
      let started = false;
      arr.forEach((v, i) => { if (v == null) { started = false; return; } const x = xOf(i), y = yOf(v, mx); if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y); });
      ctx.stroke();
    };
    if (sel.includes("frac_pct") && S.frac_pct) line(S.frac_pct, pMax, "#ba7517");
    if (sel.includes("frac_zones") && S.frac_zones) {
      line(S.frac_zones, zMax, "#e24b4a");
      ctx.fillStyle = "#e24b4a";                                     // точки — пробы с разломом (их и стоит открывать)
      S.frac_zones.forEach((v, i) => { if (v > 0) { ctx.beginPath(); ctx.arc(xOf(i), yOf(v, zMax), i === hover ? 4.5 : 2.5, 0, 7); ctx.fill(); } });
    }
    // подписи времени — до 6 штук через равные промежутки
    ctx.fillStyle = muted; ctx.textBaseline = "alphabetic";
    const ticks = Math.max(2, Math.min(6, Math.floor(pw / 80), n));
    for (let k = 0; k < ticks; k++) {
      const i = Math.round((n - 1) * k / (ticks - 1));
      ctx.textAlign = k === 0 ? "left" : k === ticks - 1 ? "right" : "center"; ctx.fillText(fracTsLabel(ts[i], k === 0), xOf(i), H - 5);
    }
    ctx.textAlign = "left";
    fracTr.geom = { pad, pw, n, xOf };
  }
  function fracNearest(clientX) {
    const cv = $("fracTrend"), g = fracTr.geom; if (!cv || !g) return -1;
    const x = clientX - cv.getBoundingClientRect().left;
    return g.n <= 1 ? 0 : Math.max(0, Math.min(g.n - 1, Math.round((x - g.pad.l) / g.pw * (g.n - 1))));
  }
  function fracTipShow(i, msg) {
    const tip = $("fracTrendTip"), cv = $("fracTrend"), t = fracTr.data, g = fracTr.geom; if (!tip || !cv || !t || !g) return;
    const S = t.series || {}, z = S.frac_zones && S.frac_zones[i], p = S.frac_pct && S.frac_pct[i], sv = S.sv && S.sv[i];
    tip.innerHTML = "<b>" + fracTsLabel(t.ts[i], true) + "</b>" + (sv != null ? " · СВ " + sv : "") + (t.stage && t.stage[i] != null ? " · стадия " + t.stage[i] : "") +
      "<br>" + (z > 0 ? '<span class="is-bad">разлом: ' + z + " зон · " + (p != null ? p : 0) + " % площади</span>" : "чисто") +
      "<br><i>" + (msg || "нажми — кадр пробы") + "</i>";
    tip.hidden = false;
    const x = g.xOf(i), w = tip.offsetWidth, W = cv.clientWidth;
    tip.style.left = (x + 12 + w > W ? Math.max(0, x - 12 - w) : x + 12) + "px"; tip.style.top = "8px";
  }
  function wireFracTrend() {
    const cv = $("fracTrend"); if (!cv) return;
    cv.addEventListener("pointermove", (e) => { const i = fracNearest(e.clientX); if (i < 0) return; fracPaint(i); fracTipShow(i); });
    cv.addEventListener("pointerleave", () => { const tip = $("fracTrendTip"); if (tip) tip.hidden = true; fracPaint(); });
    cv.addEventListener("click", async (e) => {                      // кадр пробы в окне «Комп. зрение» с красными контурами
      const i = fracNearest(e.clientX); if (i < 0 || !fracTr.data) return;
      const ts = fracTr.data.ts[i];
      try {
        const r = await api("/api/cv/result?" + [cvSerialQ(), "ts=" + encodeURIComponent(ts)].filter(Boolean).join("&"));
        if (r && !r.empty) { cvSelectProbe(ts); fracTipShow(i, "открыто в окне «Комп. зрение»"); }
        else fracTipShow(i, "кадры этой пробы уже стёрты");
      } catch (err) { fracTipShow(i, "не открылось"); }
    });
    window.addEventListener("resize", () => { if (isFracPaneVisible()) fracPaint(); });
  }
  function fracRefresh() { fracRender(); apRender(); setTimeout(fracDrawTrend, 30); }
  function isFracPaneVisible() {
    const p = document.querySelector('.micro-ppane[data-ppane="frac"]');
    return p && !p.classList.contains("hidden");
  }
  function wireFrac() {
    Promise.all([api("/api/cv/fracture/settings").catch(() => null),
                 api("/api/cv/approach/settings").catch(() => null)]).then(([fr, ap]) => fracFill(fr, ap));
    document.querySelectorAll("#fracSeries input").forEach((c) => c.addEventListener("change", () => fracPaint()));
    wireFracTrend();
    const num = (id) => { const e = $(id); return e && e.value !== "" ? parseFloat(e.value) : undefined; };
    const fs = $("fracSaveBtn");
    if (fs) fs.addEventListener("click", async () => {
      await fetch("/api/cv/fracture/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conf_thr: num("fracConfThr"), w_dark: num("fracWDark"), w_tex: num("fracWTex"),
          dark_thr: num("fracDarkThr"), confirm_frames: num("fracConfirm"), min_area_frac: num("fracMinArea") }) }).catch(() => {});
      const h = $("fracSaveHint"); if (h) { h.textContent = "сохранено"; setTimeout(() => (h.textContent = ""), 1500); }
    });
    const apt = $("apAdvice");
    if (apt) apt.addEventListener("change", (e) => { if (e.target.matches('input[type="checkbox"][data-row]')) apToggle(e.target); });
    const aps = $("apSaveBtn");
    if (aps) aps.addEventListener("click", async () => {
      await fetch("/api/cv/approach/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ step_um: num("apStep"), min_zones: num("apMinZones"), max_gap_um: num("apMaxGap"),
          min_gap_um: num("apMinGap"), clean_n: num("apCleanN"), weak_pct: num("apWeakPct") }) }).catch(() => {});
      const h = $("apSaveHint"); if (h) { h.textContent = "сохранено"; setTimeout(() => (h.textContent = ""), 1500); }
      apRender();
    });
    fracRefresh();
    setInterval(() => { if (isFracPaneVisible()) fracRefresh(); }, 6000);
  }

  // ================= Калибровка разломов по своим кадрам =================
  // Кадры снимаются с камеры чистыми PNG, к каждому — метка (разлом / норма). Сервер считает зоны-кандидаты при
  // dark_thr / min_area; вес тёмности/текстуры и порог уверенности применяются ЗДЕСЬ по D и T зоны (conf = wD·D + wT·T),
  // поэтому ползунки двигаются мгновенно. Оценка и подбор — покадрово: кадр «ловит» разлом, если макс. уверенность
  // его зоны ≥ порога.
  const fracLab = { frames: [], sel: null, zones: [], all: [], allKey: null, sug: null, timer: null };
  const flNum = (id, d) => { const e = $(id); const v = e && e.value !== "" ? parseFloat(e.value) : NaN; return isNaN(v) ? d : v; };
  const flParams = () => ({ dark_thr: flNum("fracDarkThr", 30), min_area_frac: flNum("fracMinArea", 0.0018) });
  const flW = () => ({ wd: flNum("fracWDark", 0.6), wt: flNum("fracWTex", 0.4), thr: flNum("fracConfThr", 0.6) });
  const flConf = (z, w) => Math.min(1.5, w.wd * z.D + w.wt * z.T);
  const flQ = (o) => new URLSearchParams(o).toString();
  function flHint(t) { const h = $("fracLabHint"); if (h) { h.textContent = t || ""; if (t) setTimeout(() => { if (h.textContent === t) h.textContent = ""; }, 6000); } }

  async function flList() {
    try { fracLab.frames = (await api("/api/cv/fracture/lab/list")).frames || []; } catch (e) { return; }
    if (fracLab.sel && !fracLab.frames.some((f) => f.name === fracLab.sel)) fracLab.sel = null;
    flRenderList(); flEval();
  }
  function flRenderList() {
    const box = $("fracLabList"); if (!box) return;
    const lbl = { fracture: "разлом", ok: "норма", unknown: "?" };
    box.innerHTML = "";
    if (!fracLab.frames.length) { box.innerHTML = '<div class="micro-conn-hint">кадров пока нет — сними первый</div>'; return; }
    const mx = flMaxByName();
    fracLab.frames.forEach((f) => {
      const b = document.createElement("button"); b.type = "button"; b.className = "frac-lab-row" + (f.name === fracLab.sel ? " is-active" : "");
      const t = new Date(f.ts * 1000).toLocaleTimeString("ru-RU");
      const m = mx[f.name];
      let mark = "", cls = "";
      if (m != null) {
        const hit = m >= flW().thr;
        mark = m.toFixed(2); cls = f.label === "fracture" ? (hit ? "good" : "bad") : (f.label === "ok" ? (hit ? "bad" : "good") : "");
      }
      b.innerHTML = '<span>' + t + ' · ' + f.name.slice(3, 11) + '</span><span class="frac-lab-badge is-' + f.label + '">' + lbl[f.label] + '</span><span class="frac-lab-mark ' + cls + '" title="макс. уверенность зоны на кадре">' + mark + '</span>';
      b.addEventListener("click", () => flSelect(f.name));
      box.appendChild(b);
    });
  }
  function flMaxByName() {
    const w = flW(), out = {};
    fracLab.all.forEach((f) => { out[f.name] = f.zones.length ? Math.max(...f.zones.map((z) => flConf(z, w))) : 0; });
    return out;
  }
  async function flSelect(name) {
    fracLab.sel = name; fracLab.zones = [];
    $("fracLabViewWrap").hidden = false; $("fracLabLblRow").hidden = false;
    const img = $("fracLabImg"); img.onload = () => flDraw();
    img.src = "/api/cv/fracture/lab/image?name=" + encodeURIComponent(name);
    flRenderList();
    flLoadZones();
  }
  async function flLoadZones() {
    if (!fracLab.sel) return;
    const name = fracLab.sel;
    try { const r = await api("/api/cv/fracture/lab/zones?" + flQ({ name, ...flParams() })); if (name === fracLab.sel) { fracLab.zones = r.zones || []; flDraw(); } }
    catch (e) { /* нет данных */ }
  }
  function flDraw() {
    const cv = $("fracLabCanvas"), img = $("fracLabImg"); if (!cv || !img || !img.naturalWidth) return;
    cv.width = img.naturalWidth; cv.height = img.naturalHeight;
    const ctx = cv.getContext("2d"); ctx.clearRect(0, 0, cv.width, cv.height);
    const w = flW(), lw = Math.max(3, cv.width / 500);
    ctx.font = Math.round(cv.width / 70) + "px sans-serif"; ctx.textBaseline = "bottom";
    fracLab.zones.forEach((z) => {
      if (!z.poly || z.poly.length < 3) return;
      const c = flConf(z, w), pass = c >= w.thr;
      ctx.beginPath(); z.poly.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]))); ctx.closePath();
      if (pass) { ctx.globalAlpha = 0.18; ctx.fillStyle = "#e24b4a"; ctx.fill(); ctx.globalAlpha = 1; }
      ctx.setLineDash(pass ? [] : [lw * 2.5, lw * 1.5]); ctx.lineWidth = lw; ctx.strokeStyle = pass ? "#ff3b30" : "#b0b8c4"; ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = pass ? "#ffb4a8" : "#e0e5ec"; ctx.fillText(c.toFixed(2), z.bbox[0], Math.max(z.bbox[1] - 4, cv.width / 70));
    });
  }
  // оценка по всем размеченным кадрам при текущих порогах
  function flEvalRender() {
    const box = $("fracLabEval"); if (!box) return;
    const w = flW(), mx = flMaxByName();
    const bad = fracLab.all.filter((f) => f.label === "fracture"), ok = fracLab.all.filter((f) => f.label === "ok");
    if (!bad.length && !ok.length) { box.textContent = "Разметь кадры (разлом / норма), и здесь появится оценка порогов."; return; }
    const caught = bad.filter((f) => mx[f.name] >= w.thr).length, falsePos = ok.filter((f) => mx[f.name] >= w.thr).length;
    box.innerHTML = "Разломы поймано: <b class='" + (caught === bad.length ? "good" : "bad") + "'>" + caught + " из " + bad.length + "</b> · " +
      "ложных срабатываний на норме: <b class='" + (falsePos === 0 ? "good" : "bad") + "'>" + falsePos + " из " + ok.length + "</b>" +
      "<br><span class='micro-conn-hint'>вес D " + w.wd + " · вес T " + w.wt + " · порог " + w.thr + " · тёмное " + flParams().dark_thr + " · мин. площадь " + flParams().min_area_frac + "</span>";
    flRenderList();
  }
  async function flEval() {
    const key = JSON.stringify(flParams()) + "|" + fracLab.frames.map((f) => f.name + f.label).join();
    if (fracLab.allKey !== key) {
      const box = $("fracLabEval"); if (box && fracLab.frames.length) box.textContent = "считаю зоны по размеченным кадрам…";
      try { fracLab.all = (await api("/api/cv/fracture/lab/all?" + flQ(flParams()))).frames || []; fracLab.allKey = key; } catch (e) { return; }
    }
    flEvalRender();
  }
  // подбор: перебор веса тёмности (вес текстуры = 1 − вес) и порога; цель — поймать все разломы без ложных,
  // с максимальным запасом (порог посередине между «самым слабым разломом» и «самой сильной нормой»)
  function flSuggest() {
    const bad = fracLab.all.filter((f) => f.label === "fracture"), ok = fracLab.all.filter((f) => f.label === "ok");
    const sug = $("fracLabSug"), apply = $("fracLabApply");
    if (!bad.length || !ok.length) { if (sug) sug.textContent = "нужны кадры обоих видов: хотя бы по одному «разлом» и «норма»"; return; }
    let best = null;
    for (let wd = 0.1; wd <= 0.951; wd += 0.05) {
      const w = { wd, wt: 1 - wd };
      const mxOf = (f) => (f.zones.length ? Math.max(...f.zones.map((z) => flConf(z, w))) : 0);
      const fb = bad.map(mxOf), fo = ok.map(mxOf);
      const minBad = Math.min(...fb), maxOk = Math.max(...fo);
      let cand;
      if (minBad > maxOk) cand = { sep: true, margin: minBad - maxOk, thr: (minBad + maxOk) / 2, caught: bad.length, fp: 0 };
      else {                                           // не разделяются: берём порог с лучшим «поймано − 2·ложных»
        let b2 = null;
        for (let thr = 0.1; thr <= 1.2; thr += 0.01) {
          const caught = fb.filter((v) => v >= thr).length, fp = fo.filter((v) => v >= thr).length, score = caught - 2 * fp;
          if (!b2 || score > b2.score) b2 = { score, thr, caught, fp };
        }
        cand = { sep: false, margin: -1, thr: b2.thr, caught: b2.caught, fp: b2.fp, score: b2.score };
      }
      cand.wd = wd;
      if (!best || (cand.sep && !best.sep) || (cand.sep === best.sep && (cand.sep ? cand.margin > best.margin : cand.score > best.score))) best = cand;
    }
    fracLab.sug = best;
    const r = (x) => Math.round(x * 100) / 100;
    if (sug) sug.innerHTML = best.sep
      ? "Разделяются: вес D <b>" + r(best.wd) + "</b>, вес T <b>" + r(1 - best.wd) + "</b>, порог <b>" + r(best.thr) + "</b> · запас " + r(best.margin) + " (чем больше, тем надёжнее)"
      : "Идеально не разделяются. Лучший компромисс: вес D " + r(best.wd) + ", порог " + r(best.thr) + " — поймано " + best.caught + " из " + bad.length + ", ложных " + best.fp + " из " + ok.length + ". Подкрути «Порог тёмного»/«Мин. площадь» или добавь кадров.";
    if (apply) apply.disabled = false;
  }
  function wireFracLab() {
    const tog = $("fracLabToggle"), box = $("fracLab"); if (!tog || !box) return;
    tog.addEventListener("change", () => { box.hidden = !tog.checked; if (tog.checked) flList(); });
    const snap = (label) => async () => {
      flHint("снимаю кадр…");
      try {
        const r = await api("/api/cv/fracture/lab/snap?" + flQ({ label }));
        if (r.status === "ok") { flHint("сохранён " + r.name); await flList(); flSelect(r.name); }
        else flHint("не снят: " + (r.hint || r.error));
      } catch (e) { flHint("ошибка: " + e.message); }
    };
    $("fracLabSnapBad").addEventListener("click", snap("fracture"));
    $("fracLabSnapOk").addEventListener("click", snap("ok"));
    document.querySelectorAll("#fracLabLblRow [data-lbl]").forEach((b) => b.addEventListener("click", async () => {
      if (!fracLab.sel) return;
      try { await api("/api/cv/fracture/lab/label?" + flQ({ name: fracLab.sel, label: b.dataset.lbl })); fracLab.allKey = null; await flList(); } catch (e) { }
    }));
    $("fracLabDel").addEventListener("click", async () => {
      if (!fracLab.sel || !window.confirm("Удалить этот кадр калибровки?")) return;
      try { await api("/api/cv/fracture/lab/delete?" + flQ({ name: fracLab.sel })); fracLab.sel = null; $("fracLabViewWrap").hidden = true; $("fracLabLblRow").hidden = true; fracLab.allKey = null; await flList(); } catch (e) { }
    });
    $("fracLabSuggest").addEventListener("click", async () => { await flEval(); flSuggest(); });
    $("fracLabApply").addEventListener("click", () => {
      const s = fracLab.sug; if (!s) return;
      const set = (id, v) => { const e = $(id); if (e) e.value = v; };
      set("fracWDark", Math.round(s.wd * 100) / 100); set("fracWTex", Math.round((1 - s.wd) * 100) / 100); set("fracConfThr", Math.round(s.thr * 100) / 100);
      flDraw(); flEvalRender();
      const sb = $("fracSaveBtn"); if (sb) sb.click();               // сохранить теми же настройками, что и вручную
    });
    // пороги живьём: веса/порог — пересчёт здесь; тёмное/площадь — зоны с сервера (с задержкой)
    ["fracWDark", "fracWTex", "fracConfThr"].forEach((id) => { const e = $(id); if (e) e.addEventListener("input", () => { if (box.hidden) return; flDraw(); flEvalRender(); }); });
    ["fracDarkThr", "fracMinArea"].forEach((id) => { const e = $(id); if (e) e.addEventListener("input", () => {
      if (box.hidden) return; clearTimeout(fracLab.timer); fracLab.timer = setTimeout(() => { flLoadZones(); flEval(); }, 450);
    }); });
  }


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
      if ($("apEnable")) $("apEnable").checked = !!ap.enabled;
      set("apStep", ap.step_um); set("apMinZones", ap.min_zones); set("apMaxTotal", ap.max_total_um); set("apCount", ap.count);
    }
  }
  function fracRender() {
    const q = cvSerialQ() ? "?" + cvSerialQ() : "";
    api("/api/cv/last" + q).then((r) => {
      const fr = (r && r.fracture && r.fracture.summary) || null;
      const setT = (id, v) => { const e = $(id); if (e) e.textContent = v; };
      if (!fr) { ["fracZones", "fracArea", "fracConf", "fracCand", "fracFrames", "fracVerdict"].forEach((i) => setT(i, "—")); return; }
      setT("fracZones", fr.zones != null ? fr.zones : "—");
      setT("fracArea", fr.area_pct != null ? fr.area_pct + " %" : "—");
      setT("fracConf", fr.mean_conf != null ? fr.mean_conf : "—");
      setT("fracCand", fr.candidates != null ? fr.candidates : "—");
      setT("fracFrames", fr.confirm_frames != null ? "≥" + fr.confirm_frames : "—");
      const v = $("fracVerdict");
      if (v) { v.textContent = fr.has_fracture ? "ЕСТЬ РАЗЛОМ" : "чисто"; v.style.color = fr.has_fracture ? "var(--danger)" : "var(--success)"; }
    }).catch(() => {});
  }
  function fracDrawTrend() {
    const cv = $("fracTrend"); if (!cv) return;
    const sel = [...document.querySelectorAll("#fracSeries input:checked")].map((c) => c.value);
    const q = [cvSerialQ(), "series=" + (sel.join(",") || "frac_zones"), "limit=100"].filter(Boolean).join("&");
    api("/api/cv/trend?" + q).then((t) => {
      const ctx = cv.getContext("2d");
      const W = cv.width = cv.clientWidth || 620, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      const ts = t.ts || [], n = ts.length;
      if (!n) { ctx.fillStyle = getCss("--muted", "#8a94a6"); ctx.font = "12px sans-serif"; ctx.fillText("нет проб", 12, H / 2); return; }
      const pad = { l: 30, r: 8, t: 10, b: 18 }, pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
      ctx.strokeStyle = getCss("--border", "rgba(131,151,179,.28)"); ctx.lineWidth = 1;
      for (let k = 0; k <= 3; k++) { const y = pad.t + ph * k / 3; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke(); }
      const colors = { frac_zones: "#e24b4a", frac_pct: "#ba7517" };
      sel.forEach((name) => {
        const arr = (t.series || {})[name]; if (!arr) return;
        let mx = 1; arr.forEach((v) => { if (v != null) mx = Math.max(mx, v); });
        ctx.strokeStyle = colors[name] || "#888"; ctx.lineWidth = 2; ctx.beginPath();
        let started = false;
        arr.forEach((v, i) => {
          if (v == null) { started = false; return; }
          const x = pad.l + pw * (n <= 1 ? 0.5 : i / (n - 1));
          const y = pad.t + ph * (1 - v / (mx * 1.1));
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        });
        ctx.stroke();
      });
      ctx.fillStyle = getCss("--muted", "#8a94a6"); ctx.font = "10px monospace";
      const lbl = (i) => { const m = (ts[i] || "").match(/_(\d{2})_(\d{2})_\d{2}$/); return m ? m[1] + ":" + m[2] : ""; };
      ctx.fillText(lbl(0), pad.l, H - 5); const last = lbl(n - 1); ctx.fillText(last, W - pad.r - ctx.measureText(last).width, H - 5);
    }).catch(() => {});
  }
  function fracRefresh() { fracRender(); setTimeout(fracDrawTrend, 30); }
  function isFracPaneVisible() {
    const p = document.querySelector('.micro-ppane[data-ppane="frac"]');
    return p && !p.classList.contains("hidden");
  }
  function wireFrac() {
    Promise.all([api("/api/cv/fracture/settings").catch(() => null),
                 api("/api/cv/approach/settings").catch(() => null)]).then(([fr, ap]) => fracFill(fr, ap));
    document.querySelectorAll("#fracSeries input").forEach((c) => c.addEventListener("change", fracDrawTrend));
    const num = (id) => { const e = $(id); return e && e.value !== "" ? parseFloat(e.value) : undefined; };
    const fs = $("fracSaveBtn");
    if (fs) fs.addEventListener("click", async () => {
      await fetch("/api/cv/fracture/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conf_thr: num("fracConfThr"), w_dark: num("fracWDark"), w_tex: num("fracWTex"),
          dark_thr: num("fracDarkThr"), confirm_frames: num("fracConfirm"), min_area_frac: num("fracMinArea") }) }).catch(() => {});
      const h = $("fracSaveHint"); if (h) { h.textContent = "сохранено"; setTimeout(() => (h.textContent = ""), 1500); }
    });
    const enAp = $("apEnable");
    if (enAp) enAp.addEventListener("change", () => {
      fetch("/api/cv/approach/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: enAp.checked }) }).catch(() => {});
    });
    const aps = $("apSaveBtn");
    if (aps) aps.addEventListener("click", async () => {
      await fetch("/api/cv/approach/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ step_um: num("apStep"), min_zones: num("apMinZones"), max_total_um: num("apMaxTotal") }) }).catch(() => {});
      const h = $("apSaveHint"); if (h) { h.textContent = "сохранено"; setTimeout(() => (h.textContent = ""), 1500); }
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


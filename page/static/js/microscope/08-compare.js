// Страница микроскопа — вкладка «Сравнение»: варки после изменения против варок до него, две свободные группы (опыт / контроль: свои
// периоды и вода), одна варка против обычных, журнал изменений процесса (рецепт, подкачка, вода). Данные — /api/cv/compare, /api/cv/compare/around, /api/cv/compare/boil, /api/cv/changes (boil_compare.py).
// Что считать «лучше» и как собирается итог фразой — tasks/seed-quality-criteria.md. Графики, подсказка и печать — помощники отчёта
// (window.microRep из 05-report.js). Кривые роста — от заводки: у варок разной длины общий ноль — момент заводки.
"use strict";

let cmpShown = null;         // 02-cv.js зовёт при открытии вкладки: журнал и группы грузятся при первом показе

(function () {
  const g = (id) => document.getElementById(id);
  const R = window.microRep;
  if (!g("teleStripCompare") || !R) return;
  const { fmt, esc, tsLbl, dmy, modelName, plural, niceStep, yScale, chartHead, median, attachTip, hideTip, printDoc, openBoil } = R;
  let mode = "change", changes = [], last = null, lastW = 0, loaded = false, boilsLoaded = false, editId = null, together = [];

  const pad = (n) => String(n).padStart(2, "0");
  const toLocal = (t) => { if (t == null) return ""; const d = new Date(t * 1000); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()); };
  const fromLocal = (s) => (s ? new Date(s).getTime() / 1000 : null);
  const tLbl = (t) => { const d = new Date(t * 1000); return pad(d.getDate()) + "." + pad(d.getMonth() + 1) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()); };
  const serialQ = () => (typeof cvSerialQ === "function" ? cvSerialQ() : "");
  const qs = (o) => Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => k + "=" + encodeURIComponent(v)).concat(serialQ() ? [serialQ()] : []).join("&");
  const api = async (url, opt) => (await fetch(url, opt)).json();
  const hint = (t) => { g("cmpHint").textContent = t || ""; };
  const unit = (r) => (r.unit === "%" ? " %" : " " + r.unit);
  const signed = (v, d) => (v == null ? "—" : (v > 0 ? "+" : "") + fmt(v, d));
  // подписи групп: «до / после» — А после изменения, Б до; две группы — просто А и Б
  const LBL = { change: { a: "А · после", b: "Б · до" }, groups: { a: "А", b: "Б" } };
  const TIP = { change: { a: "Группа А — варки после изменения. Варка, которая шла в момент изменения, не идёт ни в одну группу",
    b: "Группа Б — варки до изменения: от прошлого изменения, но не раньше смены модели и не длиннее двух недель" },
  groups: { a: "Группа А (опыт) — варки целиком внутри периода; вода — по клапану или по провалу СВ", b: "Группа Б (контроль) — варки целиком внутри периода" } };
  const waterTxt = (w) => (w === true ? "с водой" : w === false ? "без воды" : "");

  // ===== журнал изменений =====
  async function loadChanges() {
    try { changes = (await api("/api/cv/changes?" + serialQ())).changes || []; } catch (e) { changes = []; }
    renderJournal(); fillChangeSelect();
  }
  function fillChangeSelect() {
    const sel = g("cmpChange"), cur = sel.value;
    sel.innerHTML = '<option value="">свой момент</option>' + changes.slice().reverse().map((c) => '<option value="' + c.id + '">' + esc(c.label + " · " + c.text) + "</option>").join("");
    if (cur && changes.some((c) => String(c.id) === cur)) sel.value = cur;
  }
  function renderJournal() {
    g("chgList").innerHTML = changes.length ? changes.slice().reverse().map((c) => '<div class="micro-cmp-row" data-id="' + c.id + '"><time>' + esc(c.label) + "</time><span>" + esc(c.text) + "</span>" +
      '<button type="button" data-act="cmp" title="Сравнить варки до и после этого изменения">сравнить</button>' +
      '<button type="button" data-act="edit" title="Поправить">✎</button><button type="button" class="is-del" data-act="del" title="Удалить">✕</button></div>').join("")
      : '<div class="micro-conn-hint">Пока пусто. Запиши, что и когда меняли, — потом здесь же сравнишь варки до и после.</div>';
  }
  function resetForm() {
    editId = null; g("chgText").value = ""; g("chgAt").value = toLocal(Date.now() / 1000);
    g("chgSaveBtn").textContent = "Добавить"; g("chgCancelBtn").hidden = true;
  }
  async function saveChange() {
    const at = g("chgAt").value, text = g("chgText").value.trim();
    if (!at || !text) { hint("для журнала нужны время и что изменили"); return; }
    const body = { t: at.replace("T", " "), text }; if (editId != null) body.id = editId;
    try {
      const r = await api("/api/cv/changes?" + serialQ(), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) { hint(r.error || "не сохранилось"); return; }
      changes = r.changes || []; resetForm(); renderJournal(); fillChangeSelect(); hint("");
    } catch (e) { hint("не сохранилось: " + e.message); }
  }
  async function journalClick(e) {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const c = changes.find((x) => String(x.id) === b.closest("[data-id]").dataset.id); if (!c) return;
    if (b.dataset.act === "edit") {
      editId = c.id; g("chgAt").value = toLocal(c.t); g("chgText").value = c.text;
      g("chgSaveBtn").textContent = "Сохранить"; g("chgCancelBtn").hidden = false; g("chgText").focus();
    } else if (b.dataset.act === "del") {
      if (!window.confirm("Удалить из журнала: " + c.label + " · " + c.text + "?")) return;
      try { const r = await api("/api/cv/changes/" + c.id + "?" + serialQ(), { method: "DELETE" }); changes = r.changes || []; renderJournal(); fillChangeSelect(); } catch (er) { hint("не удалилось: " + er.message); }
    } else {
      setMode("change"); g("cmpChange").value = String(c.id);
      if (await pickChange()) run();
    }
  }

  // ===== группы «до / после» =====
  async function around(t) {
    try {
      const r = await api("/api/cv/compare/around?" + qs({ t: Math.round(t) }));
      if (!r.ok) { hint(r.error); return false; }
      g("cmpAFrom").value = toLocal(r.a_from); g("cmpATo").value = toLocal(r.a_to); g("cmpBFrom").value = toLocal(r.b_from); g("cmpBTo").value = toLocal(r.b_to);
      together = r.together || [];          // изменения без целой варки между ними — одно изменение: Б до первого из них
      return true;
    } catch (e) { hint("не получилось: " + e.message); return false; }
  }
  async function pickChange() {
    const c = changes.find((x) => String(x.id) === g("cmpChange").value);
    if (!c) return false;
    g("cmpAt").value = toLocal(c.t);
    return around(c.t);
  }

  // ===== одна варка: список последних варок (новые первыми) =====
  async function loadBoils() {
    if (boilsLoaded) return;
    const sel = g("cmpBoil"), d = new Date(), f = new Date(); f.setDate(f.getDate() - 10);
    const day = (x) => x.getFullYear() + "-" + pad(x.getMonth() + 1) + "-" + pad(x.getDate());
    sel.innerHTML = "<option>загружаю…</option>";
    try {
      const rep = await api("/api/cv/report?" + qs({ from: day(f), to: day(d) }));
      const B = (rep.boils || []).filter((b) => b.fin_um != null).reverse();
      sel.innerHTML = B.length ? B.map((b) => '<option value="' + esc(b.start) + '">' + tsLbl(b.start) + " · мука " + fmt(b.fines_avg, 1) + " % · " + fmt(b.fin_um, 0) + " мкм" + (b.flag === "open" ? " · идёт" : "") + "</option>").join("")
        : "<option value=\"\">за 10 дней варок нет</option>";
      boilsLoaded = true;
    } catch (e) { sel.innerHTML = '<option value="">не загрузилось</option>'; }
  }

  function setMode(m) {
    mode = m === "one" || m === "groups" ? m : "change";
    document.querySelectorAll("#cmpModeSeg button").forEach((x) => x.classList.toggle("is-active", x.dataset.m === mode));
    g("cmpChangeBox").hidden = mode !== "change"; g("cmpGroups").hidden = mode === "one"; g("cmpOneBox").hidden = mode !== "one";
    if (mode !== "one") {
      ["a", "b"].forEach((k) => { g(k === "a" ? "cmpALbl" : "cmpBLbl").textContent = LBL[mode][k]; g(k === "a" ? "cmpAGroup" : "cmpBGroup").title = TIP[mode][k]; });
      if (mode === "groups" && !g("cmpAFrom").value && !g("cmpBFrom").value) {      // пусто — А последние двое суток, Б двое суток перед ними
        const now = Math.floor(Date.now() / 60000) * 60, day = 86400;
        g("cmpAFrom").value = toLocal(now - 2 * day); g("cmpATo").value = toLocal(now); g("cmpBFrom").value = toLocal(now - 4 * day); g("cmpBTo").value = toLocal(now - 2 * day);
      }
    }
    if (mode === "one") loadBoils();
  }

  async function run() {
    const v = (id) => g(id).value.replace("T", " ");
    hint("считаю…"); g("cmpGoBtn").disabled = true;
    try {
      if (mode === "change" || mode === "groups") {
        if (!g("cmpAFrom").value || !g("cmpBTo").value) { hint(mode === "change" ? "выбери изменение или свой момент" : "задай периоды групп А и Б"); return; }
        // режим и поля — на момент запроса: пока считается, их могут переключить или поправить
        const kind = mode, c = changes.find((x) => String(x.id) === g("cmpChange").value);
        const at = fromLocal(g("cmpAFrom").value), bTo = fromLocal(g("cmpBTo").value);
        const r = await api("/api/cv/compare?" + qs({ a_from: v("cmpAFrom"), a_to: v("cmpATo"), a_water: g("cmpAWater").value,
          b_from: v("cmpBFrom"), b_to: v("cmpBTo"), b_water: g("cmpBWater").value, mode: kind }));
        if (kind === "groups") last = { kind, r };
        else {
          last = { kind, r, change: c && Math.abs(c.t - at) < 61 ? c : null, at,
            together: together.filter((x) => Math.abs(x - bTo) < 61 || (x > bTo && x < at)).map((x) => changes.find((y) => Math.abs(y.t - x) < 61)).filter(Boolean) };
        }
      } else {
        const boil = g("cmpBoil").value; if (!boil) { hint("выбери варку"); return; }
        last = { kind: "one", r: await api("/api/cv/compare/boil?" + qs({ boil })) };
      }
      render(); hint(""); g("cmpPdfBtn").disabled = !last.r.ok;
    } catch (e) { hint("не получилось: " + e.message); } finally { g("cmpGoBtn").disabled = false; }
  }

  // ===== графики =====
  // кривые роста от заводки: тонкие — варки, толстые — медиана группы; one — одна варка поверх «обычно» (25–75 %) обычных
  function curves(cur, w, one, lbl) {
    const L = 44, R = 14, T = 22, H = 210, W = w - L - R, hh = T + H + 30;
    const lines = one ? [] : ["a", "b"].flatMap((k) => cur[k].boils.map((b) => ({ k, pts: b.pts })));
    const ends = one ? [cur.me.end] : [cur.a.end, cur.b.end];
    const xs = (one ? cur.me.pts.concat(cur.band.map((p) => [p[0], p[1]])) : lines.flatMap((l) => l.pts)).map((p) => p[0]);
    if (!xs.length) return "";
    const x0 = Math.max(-5, Math.min(...xs)), x1 = Math.max(...ends, 10) + 5;
    const inX = (p) => p[0] >= x0 && p[0] <= x1;
    const ys = (one ? cur.me.pts.map((p) => p[1]).concat(cur.band.flatMap((p) => [p[2], p[3]])) : lines.flatMap((l) => l.pts.filter(inX).map((p) => p[1]))).filter((v) => v != null);
    const y = yScale(Math.min(0, ...ys), Math.max(...ys) * 1.04, T, H, 5);
    const xf = (v) => L + ((v - x0) / (x1 - x0)) * W;
    const poly = (pts, cls) => (pts.length > 1 ? '<polyline class="' + cls + '" points="' + pts.filter(inX).map((p) => xf(p[0]).toFixed(1) + "," + y.f(p[1]).toFixed(1)).join(" ") + '"></polyline>' : "");
    let s = "";
    y.ticks.forEach((v) => { const yy = y.f(v).toFixed(1); s += '<line class="grid" x1="' + L + '" x2="' + (L + W) + '" y1="' + yy + '" y2="' + yy + '"></line><text class="ylab" x="' + (L - 6) + '" y="' + yy + '" dy="0.32em">' + fmt(v, y.d) + "</text>"; });
    const st = niceStep(x1 - x0, 7);
    for (let v = Math.ceil(x0 / st) * st; v <= x1; v += st) {
      const xx = xf(v).toFixed(1);
      s += '<line class="vgrid" x1="' + xx + '" x2="' + xx + '" y1="' + T + '" y2="' + (T + H) + '"></line><text class="xlab" x="' + xx + '" y="' + (T + H + 15) + '">' + (v > 0 ? "+" : "") + fmt(v, 0) + "</text>";
    }
    s += '<line class="axis" x1="' + L + '" x2="' + (L + W) + '" y1="' + (T + H) + '" y2="' + (T + H) + '"></line>' +
      '<text class="xlab" x="' + (L + W) + '" y="' + (T + H + 28) + '" text-anchor="end">мин от заводки</text>';
    if (x0 < 0) s += '<line class="mline" x1="' + xf(0).toFixed(1) + '" x2="' + xf(0).toFixed(1) + '" y1="' + (T - 4) + '" y2="' + (T + H) + '"></line><text class="mlab" x="' + (xf(0) + 4).toFixed(1) + '" y="' + (T + 6) + '">заводка</text>';
    if (one) {
      const band = cur.band.filter(inX);
      if (band.length > 1) s += '<polygon class="cband" points="' + band.map((p) => xf(p[0]).toFixed(1) + "," + y.f(p[3]).toFixed(1)).concat(band.slice().reverse().map((p) => xf(p[0]).toFixed(1) + "," + y.f(p[2]).toFixed(1))).join(" ") + '"></polygon>';
      s += poly(band.map((p) => [p[0], p[1]]), "cmed cb") + poly(cur.me.pts, "cmed ca");
      s += readyMark(xf, T, H, cur.me.end, "ca", "эта варка · " + fmt(cur.me.end, 0) + " мин", 0);
    } else {
      lines.forEach((l) => { s += poly(l.pts, "cthin c" + l.k); });
      ["b", "a"].forEach((k) => { s += poly(cur[k].band.map((p) => [p[0], p[1]]), "cmed c" + k); });
      [["a", "ca", "А", 0], ["b", "cb", "Б", 1]].forEach(([k, cls, lbl, row]) => { if (cur[k].boils.length) s += readyMark(xf, T, H, cur[k].end, cls, lbl + " · " + fmt(cur[k].end, 0) + " мин", row); });     // пустая группа — без отметки
    }
    const items = one ? [["dota", "эта варка"], ["dotb", "медиана обычных"], ["qband", "обычно (25–75 %)"]] : [["dota", lbl.a], ["dotb", lbl.b]];
    return '<div class="micro-rep-chart">' + chartHead("Средний размер кристалла, мкм", items) + (one ? "" : '<small class="micro-cmp-note">тонкие — варки, толстые — медиана группы, пунктир — обычный конец варки</small>') +
      '<svg viewBox="0 0 ' + w + " " + hh + '" width="' + w + '" height="' + hh + '" role="img" aria-label="Средний размер кристалла по минутам от заводки">' + s + "</svg></div>";
  }
  function readyMark(xf, T, H, end, cls, text, row) {
    const xx = xf(end).toFixed(1);
    return '<line class="cready ' + cls + '" x1="' + xx + '" x2="' + xx + '" y1="' + (T - 2) + '" y2="' + (T + H) + '"></line><text class="cready-lab ' + cls + '" x="' + (+xx - 4) + '" y="' + (T + 6 + row * 12) + '" text-anchor="end">' + esc(text) + "</text>";
  }
  // точки по показателю: две колонки (А | Б), каждая варка — точка, черта — медиана группы
  function strip(r, tg) {
    const w = 240, L = 40, R = 8, T = 6, H = 110, W = w - L - R, hh = T + H + 18;
    const A = r.pts.a, B = r.pts.b, all = A.concat(B).map((p) => p[1]);
    if (!all.length) return "";
    const lim = r.key === "fines_avg" ? [tg.fines_max] : r.key === "fin_um" ? [tg.size_min_um, tg.size_max_um] : [];
    let lo = Math.min(...all), hi = Math.max(...all); const pd = (hi - lo) * 0.1 || 1;
    const y = yScale(lo - pd, hi + pd, T, H, 3), inY = (v) => v >= y.lo && v <= y.hi;
    const isBad = (v) => (r.key === "fines_avg" ? v > tg.fines_max : r.key === "fin_um" ? v < tg.size_min_um || v > tg.size_max_um : false);
    let s = "";
    y.ticks.forEach((v) => { const yy = y.f(v).toFixed(1); s += '<line class="grid" x1="' + L + '" x2="' + (L + W) + '" y1="' + yy + '" y2="' + yy + '"></line><text class="ylab" x="' + (L - 6) + '" y="' + yy + '" dy="0.32em">' + fmt(v, y.d) + "</text>"; });
    lim.filter(inY).forEach((v) => { const yy = y.f(v).toFixed(1); s += '<line class="lim" x1="' + L + '" x2="' + (L + W) + '" y1="' + yy + '" y2="' + yy + '"></line>'; });
    [["a", A, 0.3, "А"], ["b", B, 0.72, "Б"]].forEach(([k, P, fx, lbl]) => {
      const cx = L + W * fx;
      P.forEach(([start, v], i) => {
        const xx = (cx + (((i * 37) % 11) - 5) * 2.4).toFixed(1), yy = y.f(v).toFixed(1);
        s += '<g class="pt" data-boil="' + esc(start) + '" data-tip="' + esc(lbl + " · " + tsLbl(start) + " · " + fmt(v, r.nd) + unit(r)) + '"><circle class="dot s' + k + (isBad(v) ? " bad" : "") + '" cx="' + xx + '" cy="' + yy + '" r="3"></circle><circle class="hit" cx="' + xx + '" cy="' + yy + '" r="7"></circle></g>';
      });
      if (P.length) { const m = median(P.map((p) => p[1])), yy = y.f(m).toFixed(1); s += '<line class="smed" x1="' + (cx - 20) + '" x2="' + (cx + 20) + '" y1="' + yy + '" y2="' + yy + '"></line>'; }
      s += '<text class="xlab" x="' + cx.toFixed(1) + '" y="' + (T + H + 14) + '">' + lbl + " · " + P.length + "</text>";
    });
    return '<div class="micro-rep-chart">' + chartHead(esc(r.name.charAt(0).toUpperCase() + r.name.slice(1)) + ", " + esc(r.unit), []) + '<svg viewBox="0 0 ' + w + " " + hh + '" width="' + w + '" height="' + hh + '" role="img" aria-label="' + esc(r.name) + '">' + s + "</svg></div>";
  }

  // ===== вывод =====
  function badge(r) {
    if (r.verdict === "few") return '<span class="micro-cmp-badge is-few" title="' + esc(r.text) + '">мало варок</span>';
    if (r.verdict === "noise") return '<span class="micro-cmp-badge" title="' + esc(r.text) + '">шум</span>';
    const up = r.diff > 0;
    if (r.kind === "size") return '<span class="micro-cmp-badge is-real" title="разница реальная">' + (up ? "крупнее" : "мельче") + "</span>";
    const good = !up, word = r.kind === "time" ? (up ? "дольше" : "короче") : (good ? "лучше" : "хуже");
    return '<span class="micro-cmp-badge ' + (good ? "is-good" : "is-bad") + '" title="разница реальная">' + word + "</span>";
  }
  const sumBox = (s) => '<div class="micro-cmp-sum is-' + esc(s.verdict || "none") + '"><b>' + esc(s.text) + "</b>" + (s.lines && s.lines.length ? "<ul>" + s.lines.map((l) => "<li>" + esc(l) + "</li>").join("") + "</ul>" : "") +
    (s.caveat ? "<small>" + esc(s.caveat) + "</small>" : "") + "</div>";
  const docHead = (title, sub, print) => '<div class="micro-rep-head"><div><div class="micro-rep-title">' + title + '</div><div class="micro-rep-sub">' + sub + "</div></div>" +
    (print ? '<img class="micro-rep-logo" src="/static/icon/Logo_Black.png" alt="Maximum Automation">' : "") + "</div>";
  const span = (spec) => tLbl(spec.from) + " – " + tLbl(spec.to) + (waterTxt(spec.water) ? ", " + waterTxt(spec.water) : "");
  const WHY = { edge: "шла в момент изменения", long: "затянутая", short: "короткая" };
  // ячейка группы: медиана крупно, под ней среднее (вывод и «А − Б» — по средним); разброс между варками — одно число
  const cell = (s, nd) => fmt(s.median, nd) + (s.mean != null && s.mean !== s.median ? "<small>ср. " + fmt(s.mean, nd) + "</small>" : "");

  // «до / после» (kind change) и две свободные группы (kind groups): одна страница, разные слова
  function groupsHtml(L, opt) {
    const r = L.r, ch = L.kind === "change", lbl = LBL[L.kind];
    const title = ch ? "Сравнение варок: до и после " + (L.change ? "изменения «" + esc(L.change.text) + "»" : "момента " + (L.at ? tLbl(L.at) : "")) : "Сравнение варок: А против Б";
    if (!r.ok) return docHead(title, "", opt.print) + '<div class="micro-rep-warn">' + esc(r.error || "не получилось") + "</div>";
    const nA = r.a.boils.length, nB = r.b.boils.length, why = ch ? WHY : { ...WHY, edge: "шла на границе периода" };
    const meta = [(ch ? "А — после: " : "А: ") + nA + " " + plural(nA, "варка", "варки", "варок") + " (" + span(r.a.spec) + ")", (ch ? "Б — до: " : "Б: ") + nB + " " + plural(nB, "варка", "варки", "варок") + " (" + span(r.b.spec) + ")",
      r.models && r.models.length ? "модель " + esc(modelName(r.models[0])) : "", "сформировано " + dmy(r.generated) + " " + new Date(r.generated * 1000).toTimeString().slice(0, 5)].filter(Boolean).join(" · ");
    let h = docHead(title, meta, opt.print);
    if (L.together && L.together.length) h += '<div class="micro-rep-warn">Вместе с ним: ' + L.together.map((c) => esc(c.label + " · " + c.text)).join("; ") +
      " — между изменениями не было целой варки, поэтому «до» — варки до " + esc(L.together[L.together.length - 1].label) + ", а эффект общий.</div>";
    (r.warnings || []).forEach((w) => { h += '<div class="micro-rep-warn">' + esc(w) + "</div>"; });
    h += sumBox(r.summary);
    const rows = r.metrics.map((m) => "<tr><td>" + esc(m.name) + '</td><td class="' + (m.off && m.off.a ? "is-bad" : "") + '">' + cell(m.a, m.nd) + '</td><td class="' + (m.off && m.off.b ? "is-bad" : "") + '">' + cell(m.b, m.nd) +
      "</td><td>" + signed(m.diff, m.nd) + "</td><td>" + badge(m) + "</td></tr>").join("");
    const cw = opt.width >= 1100 || opt.print ? Math.round((opt.width - 16) * 1.25 / 2.25) : opt.width;
    h += '<div class="micro-cmp-grid">' + (r.curves ? curves(r.curves, cw, false, lbl) : "<div></div>") +
      '<div><div class="micro-rep-wrap"><table class="micro-rep-table micro-cmp-table"><thead><tr><th>показатель</th><th>● А</th><th>● Б</th><th>А − Б</th><th>вывод</th></tr></thead><tbody>' + rows + "</tbody></table></div>" +
      '<div class="micro-rep-note">Медиана группы, под ней — среднее; «А − Б» и вывод — по средним. Красным — за целью маточника (мука выше ' + fmt(r.targets.fines_max, 0) + " %, размер вне " + fmt(r.targets.size_min_um, 0) + "–" + fmt(r.targets.size_max_um, 0) + " мкм). " +
      "Вывод — перестановочный тест: «лучше / хуже» — разница реальная; «мало варок» — разница может быть, но варок не хватает; «шум» — разницы не видно." +
      (r.excluded && r.excluded.length ? "<br>Не в счёт: " + r.excluded.map((x) => tsLbl(x.start) + " (" + (why[x.why] || x.why) + ")").join(", ") + "." : "") + "</div></div></div>";
    h += '<div class="micro-cmp-strips">' + r.metrics.filter((m) => m.pts).map((m) => strip(m, r.targets)).join("") + "</div>";
    return h;
  }

  function oneHtml(L, opt) {
    const r = L.r;
    if (!r.ok) return docHead("Варка против обычных", "", opt.print) + '<div class="micro-rep-warn">' + esc(r.error || "не получилось") + "</div>";
    const meta = ["обычные — " + r.ref.n + " " + plural(r.ref.n, "варка", "варки", "варок") + " той же модели" + (r.ref.from ? " (" + tsLbl(r.ref.from) + " – " + tsLbl(r.ref.to) + ")" : ""),
      r.model ? "модель " + esc(modelName(r.model)) : "", "сформировано " + dmy(r.generated) + " " + new Date(r.generated * 1000).toTimeString().slice(0, 5)].filter(Boolean).join(" · ");
    let h = docHead("Варка " + tsLbl(r.boil.start) + " против обычных", meta, opt.print);
    const txt = r.summary.text, verdict = /^Итог: хуже/.test(txt) ? "minus" : /^Итог: лучше/.test(txt) ? "plus" : /мало/.test(txt) ? "few" : "none";
    h += sumBox({ verdict, text: txt, lines: r.summary.lines });
    const B1 = { worse: ["is-bad", "хуже обычного"], better: ["is-good", "лучше обычного"], high: ["is-real", "крупнее обычного"], low: ["is-real", "мельче обычного"], usual: ["", "как обычно"], few: ["is-few", "мало варок"] };
    const tg = r.targets;
    const rows = r.metrics.map((m) => {
      const off = (m.key === "fines_avg" && m.value > tg.fines_max) || (m.key === "fin_um" && m.value != null && (m.value < tg.size_min_um || m.value > tg.size_max_um));
      const b = B1[m.verdict] || B1.usual;
      return "<tr><td>" + esc(m.name) + '</td><td class="' + (off ? "is-bad" : "") + '">' + fmt(m.value, m.nd) + unit(m) + "</td><td>" + (m.q1 == null ? "—" : fmt(m.q1, m.nd) + "–" + fmt(m.q3, m.nd)) +
        '</td><td title="доля обычных варок с меньшим значением">' + (m.place == null ? "—" : fmt(m.place, 0) + " %") + '</td><td><span class="micro-cmp-badge ' + b[0] + '">' + b[1] + "</span></td></tr>";
    }).join("");
    const cw = opt.width >= 1100 || opt.print ? Math.round((opt.width - 16) * 1.25 / 2.25) : opt.width;
    h += '<div class="micro-cmp-grid">' + curves(r.curves, cw, true) +
      '<div><div class="micro-rep-wrap"><table class="micro-rep-table micro-cmp-table"><thead><tr><th>показатель</th><th>варка</th><th>обычно (25–75 %)</th><th>место</th><th>вывод</th></tr></thead><tbody>' + rows + "</tbody></table></div>" +
      '<div class="micro-rep-note">«Место» — у какой доли обычных варок значение меньше: 95 % — почти самое большое. Хуже / лучше обычного — вне 10–90 % обычных варок; ' +
      "красным — за целью маточника. Варку можно открыть на «Трендах» кликом по таблице отчёта.</div></div></div>";
    return h;
  }

  function render() {
    const out = g("cmpOut"); if (!last) return;
    lastW = out.clientWidth; hideTip();
    const opt = { width: Math.max(320, Math.min(1400, lastW || 900)) };
    out.innerHTML = last.kind === "one" ? oneHtml(last, opt) : groupsHtml(last, opt);
  }
  function printCompare() {
    if (!last || !last.r.ok) return;
    const ser = last.r.serial ? " " + last.r.serial : "";
    const name = last.kind === "change" ? "Сравнение варок" + ser + " " + (last.change ? last.change.label : last.at ? tLbl(last.at) : "")
      : last.kind === "groups" ? "Сравнение варок" + ser + " А против Б" : "Варка " + tsLbl(last.r.boil.start) + " против обычных" + ser;
    printDoc(name, (last.kind === "one" ? oneHtml : groupsHtml)(last, { width: 1000, print: true }));
  }

  cmpShown = async function () {
    if (loaded) return;
    loaded = true;
    resetForm();
    await loadChanges();
    if (changes.length) { g("cmpChange").value = String(changes[changes.length - 1].id); await pickChange(); }      // самое свежее изменение
  };

  function init() {
    const out = g("cmpOut");
    out.addEventListener("click", (e) => { const p = e.target.closest("[data-boil]"); if (p) openBoil(p.dataset.boil, g("cmpHint")); });
    attachTip(out);
    if (typeof ResizeObserver === "function") {
      let tm = null;
      new ResizeObserver(() => { clearTimeout(tm); tm = setTimeout(() => { if (last && out.clientWidth && Math.abs(out.clientWidth - lastW) > 24) render(); }, 150); }).observe(out);
    }
    document.querySelectorAll("#cmpModeSeg button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.m)));
    g("cmpChange").addEventListener("change", () => { if (g("cmpChange").value) pickChange(); });
    g("cmpAt").addEventListener("change", () => {
      const t = fromLocal(g("cmpAt").value); if (t == null) return;
      const c = changes.find((x) => Math.abs(x.t - t) < 61); g("cmpChange").value = c ? String(c.id) : "";
      around(t);
    });
    g("cmpGoBtn").addEventListener("click", run);
    g("cmpPdfBtn").addEventListener("click", printCompare);
    g("chgSaveBtn").addEventListener("click", saveChange);
    g("chgCancelBtn").addEventListener("click", resetForm);
    g("chgText").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); saveChange(); } });
    g("chgList").addEventListener("click", journalClick);
    attachTip(g("chgList"));
    setMode("change");
  }
  init();
})();

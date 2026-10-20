// Страница микроскопа — вкладка «Отчёт»: отчёт по варкам за период (неделя / месяц / свой), CSV и PDF (печать)
// Данные — /api/cv/report (report.py). Файлы microscope/01..05 делят общую глобальную область.
// Раскладка: шапка → итоги → графики (мука, размер, рассев по дням) → по дням → варки → «как читать». Графики — свои SVG без
// библиотек (на заводе CDN может не открываться). PDF — тот же отчёт в отдельном документе (скрытый iframe): страница приложения
// в печать не попадает, поэтому нет пустых листов.
"use strict";

  (function () {
    const g = (id) => document.getElementById(id);
    let period = "week", lastQ = null, lastRep = null, lastW = 0, tip = null, titleSaved = null;
    const dstr = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    const fmt = (v, d) => (v == null ? "—" : Number(v).toFixed(d == null ? 1 : d).replace(".", ","));
    const range = (a, b, d) => (a == null ? "—" : fmt(a, d) + "–" + fmt(b, d));
    const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    // ts «2026-10-05_17_21_24» → «05.10 17:21» / «17:21»; день «2026-10-05» → «вс 05.10»
    const tsLbl = (ts) => { const m = String(ts || "").match(/^\d{4}-(\d\d)-(\d\d)_(\d\d)_(\d\d)/); return m ? m[2] + "." + m[1] + " " + m[3] + ":" + m[4] : (ts || "—"); };
    const hmLbl = (ts) => { const m = String(ts || "").match(/^\d{4}-\d\d-\d\d_(\d\d)_(\d\d)/); return m ? m[1] + ":" + m[2] : tsLbl(ts); };
    const WD = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
    const dayLbl = (day) => { const [y, m, d] = day.split("-").map(Number); return WD[new Date(y, m - 1, d).getDay()] + " " + String(d).padStart(2, "0") + "." + String(m).padStart(2, "0"); };
    const dmy = (t) => { const d = new Date(t * 1000); return String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0") + "." + d.getFullYear(); };
    const dur = (min) => { if (min == null) return "—"; const m = Math.round(min); return Math.floor(m / 60) + " ч " + String(m % 60).padStart(2, "0") + " мин"; };
    // «обычно» (25–75 %) — только от 4 варок: на 1–3 квартили ничего не значат, тогда просто «по N варкам»
    const signed = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + fmt(v, 0));
    const usual = (m, d, unit) => (!m || !m.n ? "" : m.n >= 4 ? "обычно " + fmt(m.q1, d) + "–" + fmt(m.q3, d) + (unit || "") : "по " + m.n + " " + (m.n === 1 ? "варке" : "варкам"));
    const svLbl = (v) => fmt(v, 1).replace(/,0$/, "");
    const plural = (n, one, few, many) => { const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many; };
    const modelName = (m) => (m ? String(m).replace(/\.pt(?=@|$)/, "") : "не записана");
    const FLAG = { open: ["идёт", "Варка ещё идёт — в итоги не входит"], short: ["короткая", "Короче обычной (оборвали, остановка) — в итоги не входит"],
      long: ["затянутая", "Длиннее обычной (простой или две варки слились) — в итоги не входит"] };

    // рассев: 6 сит отчёта → 4 группы для полосок и графика (>0,7 — всё крупнее, у маточника его мало)
    const SIEVE4 = ["<0,2", "0,2–0,5", "0,5–0,7", ">0,7"];
    const sieve4 = (s) => (!s || s.every((x) => x == null) ? null : [s[0] || 0, s[1] || 0, s[2] || 0, (s[3] || 0) + (s[4] || 0) + (s[5] || 0)]);
    const sieveTip = (v) => SIEVE4.map((l, i) => l + " мм — " + fmt(v[i], 1) + " %").join("\n");
    function sieveBar(s, title) {
      const v = sieve4(s); if (!v) return "—";
      return '<span class="micro-rep-sbar" data-tip="' + esc((title || "Рассев финиша") + "\n" + sieveTip(v)) + '">' +
        v.map((x, i) => (x >= 0.3 ? '<i class="s' + i + '" style="flex-grow:' + x.toFixed(2) + '"></i>' : "")).join("") + "</span>";
    }

    function query() {
      let from, to;
      const today = new Date();
      if (period === "custom") { from = g("repFrom").value; to = g("repTo").value; }
      else { const f = new Date(today); f.setDate(f.getDate() - (period === "week" ? 6 : 29)); from = dstr(f); to = dstr(today); }
      const fin = parseFloat(g("repFinSv").value) > 0 ? parseFloat(g("repFinSv").value) : 2;
      try { localStorage.setItem("microRepFinSv", String(fin)); } catch (e) { }
      return { from, to, q: [typeof cvSerialQ === "function" ? cvSerialQ() : "", "from=" + encodeURIComponent(from), "to=" + encodeURIComponent(to), "fin_sv=" + fin].filter(Boolean).join("&") };
    }

    function card(title, big, sub, boil) {
      return '<div class="micro-rep-card' + (boil ? ' micro-rep-link" data-boil="' + esc(boil) + '" title="Открыть эту варку на «Трендах»' : "") + '"><span>' + title + "</span><b>" + big + "</b>" + (sub ? "<small>" + sub + "</small>" : "") + "</div>";
    }

    // Столбцы таблицы «Варки»: key совпадает с полем отчёта (и с колонкой CSV); «начало» есть всегда. Выбор столбцов запоминается в браузере.
    const COLS = [
      { key: "duration_min", head: "длительность", cell: (b) => dur(b.duration_min), on: true },
      { key: "probes", head: "проб", cell: (b) => b.probes, on: false },
      { key: "sv", head: "СВ", tip: "СВ мин–макс за варку", cell: (b) => range(b.sv_min, b.sv_max, 1), on: false, csv: ["sv_min", "sv_max"] },
      { key: "sv_end", head: "СВ конец", tip: "Конечное СВ варки (медиана последних проб)", cell: (b) => fmt(b.sv_end, 1), on: true },
      { key: "finish_probes", head: "проб финиша", tip: "Сколько проб в пределах «финиш, СВ» от конечного — по ним считается мука и рассев", cell: (b) => b.finish_probes, on: false },
      { key: "fines_avg", head: "мука финиша, %", tip: "Среднее по пробам финиша (последние 1,5–2 СВ). 95 % интервал — в подсказке и усами на графике",
        cell: (b) => (b.fines_avg == null ? "—" : '<span title="95 % интервал: ' + range(b.fines_lo, b.fines_hi, 1) + ' — разница двух варок меньше интервала — шум, а не процесс">' + fmt(b.fines_avg, 1) + "</span>"),
        on: true, csv: ["fines_avg", "fines_lo", "fines_hi"] },
      { key: "fines_range", head: "мука мин–макс", tip: "По пробам финиша", cell: (b) => range(b.fines_min, b.fines_max, 1), on: false, csv: ["fines_min", "fines_max"] },
      { key: "fines_all", head: "мука по всей варке, %", tip: "Среднее по всем пробам после порога «Мука с СВ» (справочно: включает начало варки)", cell: (b) => fmt(b.fines_all, 1), on: false },
      { key: "reject_pct", head: "брак, %", cell: (b) => fmt(b.reject_pct, 1), on: false },
      { key: "frac_zones", head: "разломов", cell: (b) => b.frac_zones, on: false },
      { key: "mean_um", head: "размер средний, мкм", tip: "Средний размер кристалла по всем пробам варки", cell: (b) => fmt(b.mean_um, 0), on: false },
      { key: "size", head: (r) => "размер СВ " + svLbl(r.point_sv) + " → финиш, мкм", tip: "Размер на контрольной точке СВ (ранний прогноз итога) и на финише",
        cell: (b) => (b.point_um == null && b.fin_um == null ? "—" : fmt(b.point_um, 0) + " → " + fmt(b.fin_um, 0)), on: true, csv: ["point_um", "fin_um"] },
      { key: "grow_um", head: "прирост, мкм", tip: "Рост размера от контрольной точки СВ до финиша", cell: (b) => signed(b.grow_um), on: true },
      { key: "water", head: "вода", tip: "Вода на стабилизации: по клапану, а без клапана — по провалу СВ", cell: (b) => (b.water == null ? "—" : b.water ? (b.water_by === "sv" ? '<span title="по провалу СВ">да*</span>' : "да") : "нет"),
        on: true, csv: ["water", "water_dose"] },
      { key: "to_ready_min", head: (r) => "СВ " + svLbl(r.point_sv) + " → готовность, мин", cell: (b) => fmt(b.to_ready_min, 0), on: false },
      { key: "model", head: "модель", tip: "Модель распознавания (пишется в пробу с 1.9.76)", cell: (b) => esc(b.model ? modelName(b.model) : "—"), on: false },
      { key: "sieve", head: "рассев финиша", tip: "Доли сит на финише: <0,2 · 0,2–0,5 · 0,5–0,7 · >0,7 мм", cell: (b) => sieveBar(b.sieve), on: true },
    ];
    const headOf = (c, rep) => (typeof c.head === "function" ? c.head(rep || { point_sv: 86 }) : c.head);
    const LINKED = ["fines_avg", "fines_range", "fines_all"];     // значения муки в таблице — кликабельные: открывают эту варку на «Трендах»
    const COLS_KEY = "microRepCols2";        // 2 — новая раскладка отчёта (1.9.87): старый выбор столбцов сброшен на новые умолчания
    function colState() {
      let saved = null; try { saved = JSON.parse(localStorage.getItem(COLS_KEY) || "null"); } catch (e) { }
      return COLS.map((c) => ({ ...c, on: saved && c.key in saved ? !!saved[c.key] : c.on }));
    }
    function saveCols(state) { try { localStorage.setItem(COLS_KEY, JSON.stringify(Object.fromEntries(state.map((c) => [c.key, c.on])))); } catch (e) { } }
    const colOn = (state, key) => { const c = state.find((x) => x.key === key); return !c || c.on; };

    // ===== графики (SVG): общая шкала времени для муки и размера, дни — сеткой, вода — полосой, смена модели — линией =====
    function niceStep(span, n) {
      const raw = (span || 1) / Math.max(1, n), p = Math.pow(10, Math.floor(Math.log10(raw)));
      return [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw - 1e-12);
    }
    function yScale(lo, hi, top, h, n) {
      const step = niceStep(hi - lo, n || 4), y0 = Math.floor(lo / step) * step, y1 = Math.max(y0 + step, Math.ceil(hi / step) * step);
      const ticks = []; for (let v = y0; v <= y1 + step * 1e-6; v += step) ticks.push(+v.toFixed(6));
      const d = [0, 1, 2, 3].find((k) => Math.abs(step * Math.pow(10, k) - Math.round(step * Math.pow(10, k))) < 1e-6);     // 2,5 → 1 знак, 0,25 → 2
      return { ticks, step, d: d == null ? 2 : d, f: (v) => top + h - ((v - y0) / (y1 - y0)) * h };
    }
    function timeScale(boils, left, w) {
      const t0 = boils[0].t_start, t1 = Math.max(t0 + 3600, ...boils.map((b) => b.t_start + (b.duration_min || 0) * 60));
      const pad = (t1 - t0) * 0.01;
      return { t0: t0 - pad, t1: t1 + pad, f: (t) => left + ((t - (t0 - pad)) / (t1 - t0 + 2 * pad)) * w };
    }
    // сетка, подписи осей, полосы воды и линии смены модели — общие для графиков по времени
    function timeFrame(rep, x, y, L, T, W, H) {
      let s = "";
      rep.boils.reduce((runs, b) => {                     // вода: подряд идущие варки с водой — одна полоса
        if (b.water === true) { const last = runs[runs.length - 1]; if (last && last.open) last.b1 = b; else runs.push({ b0: b, b1: b, open: true }); }
        else if (runs.length) runs[runs.length - 1].open = false;
        return runs;
      }, []).forEach((r) => {
        const x0 = x.f(r.b0.t_start), x1 = Math.max(x0 + 2, x.f(r.b1.t_start + (r.b1.duration_min || 0) * 60));
        s += '<rect class="wband" x="' + x0.toFixed(1) + '" y="' + T + '" width="' + (x1 - x0).toFixed(1) + '" height="' + H + '"></rect>';
      });
      y.ticks.forEach((v) => {
        const yy = y.f(v).toFixed(1);
        s += '<line class="grid" x1="' + L + '" x2="' + (L + W) + '" y1="' + yy + '" y2="' + yy + '"></line><text class="ylab" x="' + (L - 6) + '" y="' + yy + '" dy="0.32em">' + fmt(v, y.d) + "</text>";
      });
      const days = [];
      const dd = new Date(x.t0 * 1000); dd.setHours(0, 0, 0, 0);
      for (; dd.getTime() / 1000 <= x.t1; dd.setDate(dd.getDate() + 1)) days.push(dd.getTime() / 1000);     // полночи по календарю (без сдвига на переводе часов)
      const every = Math.max(1, Math.ceil((days.length * 42) / W));          // подпись — не теснее ~42 px
      days.forEach((t, i) => {
        const xx = x.f(t);
        if (xx > L && xx < L + W) s += '<line class="vgrid" x1="' + xx.toFixed(1) + '" x2="' + xx.toFixed(1) + '" y1="' + T + '" y2="' + (T + H) + '"></line>';
        const mid = x.f(Math.min(Math.max(t + 43200, x.t0), x.t1));
        if (i % every === 0 && mid > L + 12 && mid < L + W - 12) { const dd = new Date(t * 1000); s += '<text class="xlab" x="' + mid.toFixed(1) + '" y="' + (T + H + 15) + '">' + String(dd.getDate()).padStart(2, "0") + "." + String(dd.getMonth() + 1).padStart(2, "0") + "</text>"; }
      });
      s += '<line class="axis" x1="' + L + '" x2="' + (L + W) + '" y1="' + (T + H) + '" y2="' + (T + H) + '"></line>';
      (rep.segments || []).slice(1).forEach((sg) => {
        const b = rep.boils.find((z) => z.start === sg.from); if (!b) return;
        const name = "модель " + modelName(sg.model), xx = x.f(b.t_start) - 3, right = xx + name.length * 5.6 + 8 > L + W;     // не влезает справа — подпись слева от линии
        s += '<line class="mline" x1="' + xx.toFixed(1) + '" x2="' + xx.toFixed(1) + '" y1="' + (T - 4) + '" y2="' + (T + H) + '"></line>' +
          '<text class="mlab" x="' + (xx + (right ? -4 : 4)).toFixed(1) + '" y="' + (T + 6) + '" text-anchor="' + (right ? "end" : "start") + '">' + esc(name) + "</text>";
      });
      return s;
    }
    // линия медианы по дням: точка дня — среднее время его типичных варок
    function dayLine(rep, x, y, key) {
      const pts = [];
      (rep.days || []).forEach((d) => {
        const v = d[key] && d[key].median; if (v == null) return;
        const bs = rep.boils.filter((b) => !b.flag && b.start.slice(0, 10) === d.day && b[key] != null);
        if (!bs.length) return;
        pts.push(x.f(bs.reduce((a, b) => a + b.t_start, 0) / bs.length).toFixed(1) + "," + y.f(v).toFixed(1));
      });
      return pts.length > 1 ? '<polyline class="dline" points="' + pts.join(" ") + '"></polyline>' : "";
    }
    function boilTip(rep, b) {
      return tsLbl(b.start) + " · " + dur(b.duration_min) + "\nмука финиша " + fmt(b.fines_avg, 1) + " %" + (b.fines_lo != null ? " (95 %: " + range(b.fines_lo, b.fines_hi, 1) + ")" : "") +
        "\nразмер СВ " + svLbl(rep.point_sv) + " → финиш: " + fmt(b.point_um, 0) + " → " + fmt(b.fin_um, 0) + " мкм" +
        (b.water != null ? "\nвода: " + (b.water ? "да" : "нет") : "") + (b.flag ? "\n" + FLAG[b.flag][0] + " — в итоги не входит" : "");
    }
    const legend = (items) => '<span class="micro-rep-legend">' + items.map((it) => '<span><i class="lg lg-' + it[0] + '"></i>' + it[1] + "</span>").join("") + "</span>";
    function chartHead(title, items) { return '<div class="micro-rep-chart__head"><b>' + title + "</b>" + legend(items) + "</div>"; }
    const extras = (rep) => [].concat(rep.boils.some((b) => b.water === true) ? [["water", "вода на стабилизации"]] : [], (rep.segments || []).length > 1 ? [["model", "смена модели"]] : []);

    function chartFines(rep, w) {
      const B = rep.boils.filter((b) => b.fines_avg != null);
      if (B.length < 2) return "";
      const L = 40, R = 12, T = 14, H = 150, W = w - L - R, hh = T + H + 22;
      const x = timeScale(rep.boils, L, W);
      // шкала — по точкам и усам типичных варок: широкий интервал обрывка не сжимает остальные
      const y = yScale(0, Math.max(...B.map((b) => (b.flag ? b.fines_avg : Math.max(b.fines_avg, b.fines_hi || 0)))), T, H, 5);
      let s = timeFrame(rep, x, y, L, T, W, H);
      B.forEach((b) => { if (!b.flag && b.fines_lo != null && b.fines_hi != null) { const xx = x.f(b.t_start).toFixed(1); s += '<line class="whisk" x1="' + xx + '" x2="' + xx + '" y1="' + y.f(b.fines_lo).toFixed(1) + '" y2="' + y.f(b.fines_hi).toFixed(1) + '"></line>'; } });
      s += dayLine(rep, x, y, "fines_avg");
      B.forEach((b) => {
        const xx = x.f(b.t_start).toFixed(1), yy = y.f(b.fines_avg).toFixed(1);
        s += '<g class="pt" data-boil="' + esc(b.start) + '" data-tip="' + esc(boilTip(rep, b)) + '"><circle class="dot s1' + (b.flag ? " is-off" : "") + '" cx="' + xx + '" cy="' + yy + '" r="3.5"></circle><circle class="hit" cx="' + xx + '" cy="' + yy + '" r="9"></circle></g>';
      });
      return '<div class="micro-rep-chart">' + chartHead("Мука на финише по варкам, %", [["dot1", "варка"], ["off", "не в итогах"], ["line", "медиана за день"], ["whisk", "95 % интервал"]].concat(extras(rep))) +
        '<svg viewBox="0 0 ' + w + " " + hh + '" width="' + w + '" height="' + hh + '" role="img" aria-label="Мука на финише по варкам">' + s + "</svg></div>";
    }

    function chartSize(rep, w) {
      const B = rep.boils.filter((b) => b.fin_um != null || b.point_um != null);
      if (B.length < 2) return "";
      const L = 40, R = 12, T = 14, H = 130, W = w - L - R, hh = T + H + 22;
      const x = timeScale(rep.boils, L, W);
      const vals = B.flatMap((b) => [b.fin_um, b.point_um]).filter((v) => v != null);
      const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.06 || 5;
      const y = yScale(lo - pad, hi + pad, T, H);
      let s = timeFrame(rep, x, y, L, T, W, H) + dayLine(rep, x, y, "fin_um");
      B.forEach((b) => {
        const xx = x.f(b.t_start).toFixed(1), tip = esc(boilTip(rep, b)), off = b.flag ? " is-off" : "";
        s += '<g class="pt" data-boil="' + esc(b.start) + '" data-tip="' + tip + '">';
        if (b.point_um != null) s += '<circle class="dot s2' + off + '" cx="' + xx + '" cy="' + y.f(b.point_um).toFixed(1) + '" r="3"></circle>';
        if (b.fin_um != null) s += '<circle class="dot s1' + off + '" cx="' + xx + '" cy="' + y.f(b.fin_um).toFixed(1) + '" r="3.5"></circle><circle class="hit" cx="' + xx + '" cy="' + y.f(b.fin_um).toFixed(1) + '" r="9"></circle>';
        s += "</g>";
      });
      return '<div class="micro-rep-chart">' + chartHead("Размер кристалла, мкм", [["dot1", "на финише"]].concat(B.some((b) => b.point_um != null) ? [["dot2", "на СВ " + svLbl(rep.point_sv)]] : [], [["line", "медиана финиша за день"]]).concat(extras(rep))) +
        '<svg viewBox="0 0 ' + w + " " + hh + '" width="' + w + '" height="' + hh + '" role="img" aria-label="Размер кристалла по варкам">' + s + "</svg></div>";
    }

    // рассев по дням: столбик 100 % на день, снизу самые мелкие (<0,2 мм), сверху — крупные; между долями — зазор 2 px
    function chartSieve(rep, w) {
      const D = (rep.days || []).map((d) => ({ d, v: sieve4(d.sieve) })).filter((x) => x.v);
      if (D.length < 2) return "";
      const L = 40, R = 12, T = 10, H = 110, W = w - L - R, hh = T + H + 22, slot = W / D.length, bw = Math.min(24, slot * 0.6);
      const y = yScale(0, 100, T, H);
      let s = "";
      [0, 50, 100].forEach((v) => { const yy = y.f(v).toFixed(1); s += '<line class="grid" x1="' + L + '" x2="' + (L + W) + '" y1="' + yy + '" y2="' + yy + '"></line><text class="ylab" x="' + (L - 6) + '" y="' + yy + '" dy="0.32em">' + v + "</text>"; });
      const every = Math.max(1, Math.ceil(42 / slot));
      D.forEach(({ d, v }, i) => {
        const tot = v.reduce((a, b) => a + b, 0) || 1, cx = L + slot * (i + 0.5), x0 = (cx - bw / 2).toFixed(1);
        let cur = 0;
        const top = v.reduce((k, val, j) => (val / tot > 0.002 ? j : k), 0);
        s += '<g class="sv" data-tip="' + esc(dayLbl(d.day) + " · рассев финиша\n" + sieveTip(v)) + '">';
        v.forEach((val, j) => {
          const h = (val / tot) * H; if (h <= 0.2) { cur += h; return; }
          const yb = T + H - cur, gap = j === top ? 0 : 2, hh2 = Math.max(0.5, h - gap), yt = yb - h + gap;
          if (j === top) {                                  // верх столбика скруглён на 4 px, низ прямой
            const r = Math.min(4, hh2 / 2, bw / 2), xr = +x0 + bw;
            s += '<path class="s' + j + '" d="M' + x0 + "," + yb.toFixed(1) + "V" + (yt + r).toFixed(1) + "Q" + x0 + "," + yt.toFixed(1) + " " + (+x0 + r).toFixed(1) + "," + yt.toFixed(1) +
              "H" + (xr - r).toFixed(1) + "Q" + xr.toFixed(1) + "," + yt.toFixed(1) + " " + xr.toFixed(1) + "," + (yt + r).toFixed(1) + "V" + yb.toFixed(1) + 'Z"></path>';
          } else s += '<rect class="s' + j + '" x="' + x0 + '" y="' + yt.toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + hh2.toFixed(1) + '"></rect>';
          cur += h;
        });
        s += '<rect class="hit" x="' + (cx - slot / 2).toFixed(1) + '" y="' + T + '" width="' + slot.toFixed(1) + '" height="' + H + '"></rect></g>';
        if (i % every === 0) s += '<text class="xlab" x="' + cx.toFixed(1) + '" y="' + (T + H + 15) + '">' + dayLbl(d.day).slice(3) + "</text>";
      });
      s += '<line class="axis" x1="' + L + '" x2="' + (L + W) + '" y1="' + (T + H) + '" y2="' + (T + H) + '"></line>';
      return '<div class="micro-rep-chart">' + chartHead("Рассев финиша по дням, % (среднее по варкам дня)", SIEVE4.map((l, i) => ["sv" + i, l + (i === 0 ? " мм" : "")])) +
        '<svg viewBox="0 0 ' + w + " " + hh + '" width="' + w + '" height="' + hh + '" role="img" aria-label="Рассев финиша по дням">' + s + "</svg></div>";
    }

    // ===== отчёт целиком (экран и печать): opt.width — ширина графиков, opt.print — версия для PDF =====
    function reportHtml(rep, opt) {
      const t = rep.totals, cols = colState(), vis = cols.filter((c) => c.on), on = (k) => colOn(cols, k);
      const first = rep.boils[0], last = rep.boils[rep.boils.length - 1];
      const segs = rep.segments || [], oneModel = segs.length === 1 && segs[0].model ? segs[0].model : null;
      const fromDay = dmy(rep.from), toDay = dmy(rep.to), dataFrom = first.start.slice(0, 10).split("-").reverse().join(".");
      const meta = [rep.serial ? "камера " + esc(rep.serial) : "", "период " + fromDay.slice(0, 5) + " – " + toDay + (dataFrom !== fromDay ? ", варки с " + dataFrom.slice(0, 5) : ""),
        oneModel ? "модель " + esc(modelName(oneModel)) : "", "сформирован " + dmy(rep.generated) + " " + new Date(rep.generated * 1000).toTimeString().slice(0, 5)].filter(Boolean).join(" · ");
      let h = '<div class="micro-rep-head"><div><div class="micro-rep-title">Отчёт по варкам</div><div class="micro-rep-sub">' + meta + "</div></div>" +
        (opt.print ? '<img class="micro-rep-logo" src="/static/icon/Logo_Black.png" alt="Maximum Automation">' : "") + "</div>";
      const off = t.boils - t.typical;
      h += '<div class="micro-rep-cards">' +
        card("Варок", t.boils, "проб " + t.probes + (off ? " · не в итогах " + off : "")) +
        (on("fines_avg") && t.fines_avg.n ? card("Мука на финише", fmt(t.fines_avg.median, 1) + " %", usual(t.fines_avg, 1, " %")) : "") +
        (on("size") && t.fin_um.n ? card("Размер на финише", fmt(t.fin_um.median, 0) + " мкм", usual(t.fin_um, 0)) : "") +
        (on("size") && t.point_um.n ? card("Размер на СВ " + svLbl(rep.point_sv), fmt(t.point_um.median, 0) + " мкм", usual(t.point_um, 0)) : "") +
        (on("duration_min") && t.duration_min.n ? card("Длительность", dur(t.duration_min.median), usual(t.duration_min, 0, " мин")) : "") +
        (on("water") && t.water_known ? card("Вода на стабилизации", t.water + " из " + t.water_known, plural(t.water_known, "варки", "варок", "варок") + " с водой") : "") +
        (on("reject_pct") && t.reject_pct.n ? card("Брак", fmt(t.reject_pct.median, 1) + " %", usual(t.reject_pct, 1, " %")) : "") +
        (on("frac_zones") ? card("Разломы", t.frac_zones, "варок с разломом: " + t.frac_boils) : "") +
        (on("fines_avg") && t.best && t.fines_avg.n >= 3 ? card("Лучшая варка", fmt(t.best.fines_avg, 1) + " %", tsLbl(t.best.start), opt.print ? null : t.best.start) : "") +
        (on("fines_avg") && t.worst && t.fines_avg.n >= 3 ? card("Худшая варка", fmt(t.worst.fines_avg, 1) + " %", tsLbl(t.worst.start), opt.print ? null : t.worst.start) : "") + "</div>";
      if (segs.length > 1) {
        h += '<div class="micro-rep-warn"><b>В периоде меняли модель распознавания.</b> Модели по-разному видят мелочь: цифры муки и размера ' +
          "разных моделей не сравнивают. Итоги выше смешивают модели — сравнивай по строкам:</div>" +
          '<div class="micro-rep-wrap"><table class="micro-rep-table micro-rep-table--models"><thead><tr><th>модель</th><th>варки</th><th>варок</th><th>мука финиша, %</th><th>размер финиша, мкм</th></tr></thead><tbody>' +
          segs.map((s) => "<tr><td>" + esc(modelName(s.model)) + "</td><td>" + tsLbl(s.from) + " – " + tsLbl(s.to) + "</td><td>" + s.boils + "</td><td>" + fmt(s.fines_avg.median, 1) +
            ' <small class="micro-ci">' + (s.fines_avg.n >= 4 ? range(s.fines_avg.q1, s.fines_avg.q3, 1) : "") + "</small></td><td>" + fmt(s.fin_um.median, 0) + "</td></tr>").join("") + "</tbody></table></div>";
      }
      const w = opt.width;
      h += '<div class="micro-rep-charts">' + (on("fines_avg") ? chartFines(rep, w) : "") + (on("size") ? chartSize(rep, w) : "") + (on("sieve") ? chartSieve(rep, w) : "") + "</div>";

      // по дням
      const dcols = [["день", (d) => dayLbl(d.day), true], ["варок", (d) => d.boils, true],
        ["мука финиша, %", (d) => fmt(d.fines_avg.median, 1) + (d.fines_avg.n >= 4 ? ' <small class="micro-ci">' + range(d.fines_avg.q1, d.fines_avg.q3, 1) + "</small>" : ""), on("fines_avg")],
        ["размер СВ " + svLbl(rep.point_sv) + ", мкм", (d) => fmt(d.point_um.median, 0), on("size")], ["размер финиша, мкм", (d) => fmt(d.fin_um.median, 0), on("size")],
        ["прирост, мкм", (d) => signed(d.grow_um.median), on("grow_um")],
        ["длительность", (d) => dur(d.duration_min.median), on("duration_min")], ["с водой", (d) => (d.water_known ? d.water + " из " + d.water_known : "—"), on("water")],
        ["рассев финиша", (d) => sieveBar(d.sieve, dayLbl(d.day) + " · рассев финиша"), on("sieve")]].filter((c) => c[2]);
      h += '<section class="micro-rep-sec micro-rep-sec--days"><div class="micro-rep-h">По дням <small>медиана по типичным варкам дня, серым — «обычно» (25–75 %)</small></div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr>' +
        dcols.map((c) => "<th>" + c[0] + "</th>").join("") + "</tr></thead><tbody>" + (rep.days || []).map((d) => "<tr>" + dcols.map((c) => "<td>" + c[1](d) + "</td>").join("") + "</tr>").join("") + "</tbody></table></div></section>";
      if ((rep.weeks || []).length > 1 && rep.to - rep.from > 35 * 86400) {
        h += '<section class="micro-rep-sec"><div class="micro-rep-h">По неделям</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>неделя</th><th>варок</th>' + (on("fines_avg") ? "<th>мука финиша, %</th>" : "") +
          (on("size") ? "<th>размер финиша, мкм</th>" : "") + "</tr></thead><tbody>" +
          rep.weeks.map((wk) => "<tr><td>" + wk.week + "</td><td>" + wk.boils + "</td>" + (on("fines_avg") ? "<td>" + fmt(wk.fines_avg.median, 1) + "</td>" : "") + (on("size") ? "<td>" + fmt(wk.fin_um.median, 0) + "</td>" : "") + "</tr>").join("") + "</tbody></table></div></section>";
      }

      h += '<div class="micro-rep-note"><b>Как читать</b><ul>' +
        "<li>Мука на финише — среднее по пробам последних " + svLbl(rep.fin_sv) + " СВ варки (только где СВ уже не ниже «Мука с СВ» " + svLbl(rep.fines_from_sv) + ").</li>" +
        "<li>Главное число — медиана, то есть типичная варка; «обычно» — половина варок лежит между этими числами (25–75 %).</li>" +
        "<li>Усы на графике муки — 95 % интервал варки: если интервалы двух варок перекрываются, разница между ними — шум пробы, а не процесс.</li>" +
        "<li>Размер на СВ " + svLbl(rep.point_sv) + " — ранний прогноз итога варки; прирост — рост размера от СВ " + svLbl(rep.point_sv) + " до финиша.</li>" +
        "<li>В итоги не входят идущая варка, короткие (короче " + Math.round((rep.short_k || 0.6) * 100) + " % от обычной длительности) и затянутые (длиннее в " + svLbl(rep.long_k || 1.5) + " раза) — в таблице они с пометкой.</li>" +
        (on("water") && rep.boils.some((x) => x.water != null && x.water_by === "sv") ? "<li>Вода «да*» — найдена по провалу СВ на стабилизации (клапан воды не читается).</li>" : "") +
        (segs.length === 1 && !segs[0].model ? "<li>Модель распознавания в пробах не записана (пробы до 1.9.76): если модель в этом периоде меняли, цифры до и после смены не сравнивать.</li>" : "") +
        "<li>Варка входит в период по времени начала; учитываются варки не короче " + rep.min_probes + " проб.</li></ul></div>";

      // варки: разделитель на каждый день; подряд ≥ 3 варок без муки — одной строкой
      const ncol = vis.length + 1;
      h += '<section class="micro-rep-sec"><div class="micro-rep-h">Варки</div><div class="micro-rep-wrap"><table class="micro-rep-table micro-rep-table--boils"><thead><tr><th>начало</th>' +
        vis.map((c) => "<th" + (c.tip ? ' title="' + esc(c.tip) + '"' : "") + ">" + headOf(c, rep) + "</th>").join("") + "</tr></thead><tbody>";
      let prevDay = null, i = 0;
      const B = rep.boils;
      while (i < B.length) {
        let j = i; while (j < B.length && B[j].fines_avg == null && B[j].finished) j++;
        if (j - i >= 3) {
          h += '<tr class="micro-rep-empty"><td colspan="' + ncol + '">' + (j - i) + " " + plural(j - i, "варка", "варки", "варок") + " без данных муки · " + tsLbl(B[i].start) + " – " + tsLbl(B[j - 1].start) + "</td></tr>";
          prevDay = null; i = j; continue;
        }
        const b = B[i], day = b.start.slice(0, 10);
        if (day !== prevDay) { h += '<tr class="micro-rep-day"><td colspan="' + ncol + '">' + dayLbl(day) + "</td></tr>"; prevDay = day; }
        const link = opt.print ? "" : ' class="micro-rep-link" title="Открыть эту варку на «Трендах»" data-boil="' + esc(b.start) + '"';
        const flag = b.flag ? ' <span class="micro-rep-flag" title="' + FLAG[b.flag][1] + '">' + FLAG[b.flag][0] + "</span>" : "";
        h += '<tr class="' + (b.flag ? "is-off" : "") + (b.finished ? "" : " is-open") + '"><td' + link + ">" + hmLbl(b.start) + flag + "</td>" +
          vis.map((c) => "<td" + (LINKED.includes(c.key) && !opt.print ? link : "") + ">" + c.cell(b) + "</td>").join("") + "</tr>";
        i++;
      }
      h += "</tbody></table></div></section>";

      return h;
    }

    function render(rep) {
      const out = g("repOut");
      if (!rep.boils.length) { out.innerHTML = '<div class="micro-conn-hint">За этот период варок нет (учитываются варки не короче ' + rep.min_probes + " проб).</div>"; return; }
      lastW = out.clientWidth;
      if (tip) tip.hidden = true;                 // точка под курсором исчезнет вместе со старым графиком — mouseout не придёт
      out.innerHTML = reportHtml(rep, { width: Math.max(320, Math.min(1400, lastW || 900)) });
    }

    // PDF: отчёт отдельным документом в скрытом iframe — в печать не попадает страница приложения (раньше давала 2–3 пустых листа)
    const PRINT_W = 1000;
    const docName = (rep) => "Отчёт по варкам" + (rep.serial ? " " + rep.serial : "") + " " + dmy(rep.from).slice(0, 5) + "–" + dmy(rep.to);
    function printReport() {
      if (!lastRep || !lastRep.boils.length) return;
      const old = g("repPrintFrame"); if (old) old.remove();
      const fr = document.createElement("iframe"), name = docName(lastRep);
      if (titleSaved == null) titleSaved = document.title;       // второй клик до возврата заголовка не запомнит уже подменённый
      fr.id = "repPrintFrame"; fr.setAttribute("aria-hidden", "true"); fr.tabIndex = -1;
      fr.style.cssText = "position:fixed;right:0;bottom:0;width:1px;height:1px;border:0;opacity:0;pointer-events:none";
      // свои поля страницы: внизу название и «стр. N из M»; пустой верх убирает колонтитул браузера (дата и адрес about:srcdoc)
      const box = (t) => '"' + String(t).replace(/["\\<>\u0000-\u001f]/g, "") + '"';
      const page = "<style>@page { @top-center { content: \"\"; } @bottom-left { content: " + box(name) + "; font: 8pt Inter, Arial, sans-serif; color: #777; } " +
        '@bottom-right { content: "стр. " counter(page) " из " counter(pages); font: 8pt Inter, Arial, sans-serif; color: #777; } }</style>';
      const css = (document.querySelector('link[rel="stylesheet"][href*="/static/style.css"]') || {}).href || "/static/style.css";     // с ?v=<версия>, как у страницы: после обновления — свежие стили
      fr.srcdoc = '<!doctype html><html lang="ru" data-theme="light"><head><meta charset="utf-8"><title>' + esc(name) + '</title><link rel="stylesheet" href="' + esc(css) + '">' + page + "</head>" +
        '<body class="micro-rep-doc"><div class="micro-rep-out">' + reportHtml(lastRep, { width: PRINT_W, print: true }) + "</div></body></html>";
      fr.onload = () => {
        const w = fr.contentWindow, restore = () => { if (titleSaved != null) { document.title = titleSaved; titleSaved = null; } };
        w.addEventListener("afterprint", restore);
        document.title = name;                   // имя PDF-файла в диалоге печати
        const img = w.document.querySelector("img");
        const go = () => { try { w.focus(); w.print(); } finally { setTimeout(restore, 500); } };
        if (img && !img.complete) { img.onload = img.onerror = go; } else go();
      };
      document.body.appendChild(fr);
    }

    // одна подсказка на весь отчёт: точки графиков, столбики рассева, полоски
    function initTip(root) {
      tip = document.createElement("div"); tip.className = "micro-rep-tip"; tip.hidden = true; document.body.appendChild(tip);
      const place = (e) => {
        const r = tip.getBoundingClientRect(), x = e.clientX + 14, y = e.clientY + 14;
        tip.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
        tip.style.top = (y + r.height > window.innerHeight - 8 ? e.clientY - r.height - 10 : y) + "px";
      };
      root.addEventListener("mouseover", (e) => { const t = e.target.closest("[data-tip]"); if (!t) return; tip.textContent = t.dataset.tip; tip.hidden = false; place(e); });
      root.addEventListener("mousemove", (e) => { if (!tip.hidden) place(e); });
      root.addEventListener("mouseout", (e) => { const t = e.target.closest("[data-tip]"); if (t && !t.contains(e.relatedTarget)) tip.hidden = true; });
    }

    // панель «Столбцы»: галочки по столбцам таблицы «Варки»; применяется сразу (без повторного запроса), запоминается, уходит и в CSV
    function renderColsPanel() {
      const box = g("repColsPanel"); if (!box) return;
      box.innerHTML = colState().map((c) => '<label title="' + esc(c.tip || "") + '"><input type="checkbox" data-k="' + c.key + '"' + (c.on ? " checked" : "") + " /> " + headOf(c, lastRep) + "</label>").join("");
      box.querySelectorAll("input").forEach((i) => i.addEventListener("change", () => {
        const st = colState(); st.find((c) => c.key === i.dataset.k).on = i.checked; saveCols(st);
        if (lastRep) render(lastRep);
      }));
    }
    const visibleKeys = () => colState().filter((c) => c.on).flatMap((c) => c.csv || [c.key]);

    async function generate() {
      const hint = g("repHint"), qq = query();
      if (!qq.from || !qq.to) { hint.textContent = "укажи обе даты"; return; }
      hint.textContent = "считаю…"; g("repGoBtn").disabled = true;
      try {
        const rep = await (await fetch("/api/cv/report?" + qq.q)).json();
        lastRep = rep;
        render(rep);
        renderColsPanel();
        lastQ = qq.q; g("repCsvBtn").disabled = false; g("repPdfBtn").disabled = !rep.boils.length; hint.textContent = "";
      } catch (e) { hint.textContent = "не получилось: " + e.message; }
      g("repGoBtn").disabled = false;
    }

    // клик по началу варки, значению муки, точке графика или карточке лучшей/худшей — переход на «Тренды», график растянут на эту варку
    async function openBoil(id) {
      const hint = g("repHint");
      try {
        if (typeof cvTrendSetMode === "function") cvTrendSetMode("crystals");
        const ok = await cvOpenBoil(id);                         // сначала грузим варку; не нашли — остаёмся в отчёте и говорим об этом
        if (!ok) { if (hint) hint.textContent = "варка " + tsLbl(id) + " не найдена в журнале"; return; }
        if (hint) hint.textContent = "";
        const btn = g("teleTrendBtn"); if (btn) btn.click();      // переключение вкладки перерисует график уже с шириной
      } catch (e) { if (hint) hint.textContent = "не открылось: " + e.message; }
    }

    function init() {
      if (!g("repGoBtn")) return;
      const out = g("repOut");
      out.addEventListener("click", (e) => { const td = e.target.closest("[data-boil]"); if (td) openBoil(td.dataset.boil); });
      initTip(out);
      // графики по ширине панели: вкладку открыли (была 0) или окно растянули — перерисовать
      if (typeof ResizeObserver === "function") {
        let tm = null;
        new ResizeObserver(() => { clearTimeout(tm); tm = setTimeout(() => { if (lastRep && lastRep.boils.length && out.clientWidth && Math.abs(out.clientWidth - lastW) > 24) render(lastRep); }, 150); }).observe(out);
      }
      document.querySelectorAll("#repPeriodSeg button").forEach((b) => b.addEventListener("click", () => {
        period = b.dataset.p;
        document.querySelectorAll("#repPeriodSeg button").forEach((x) => x.classList.toggle("is-active", x === b));
        g("repDates").hidden = period !== "custom";
        if (period === "custom" && !g("repTo").value) { const t = new Date(), f = new Date(); f.setDate(f.getDate() - 6); g("repFrom").value = dstr(f); g("repTo").value = dstr(t); }
      }));
      g("repGoBtn").addEventListener("click", generate);
      try { const f = localStorage.getItem("microRepFinSv"); if (f && parseFloat(f) > 0) g("repFinSv").value = f; } catch (e) { }
      renderColsPanel();
      g("repColsBtn").addEventListener("click", () => { g("repColsPanel").hidden = !g("repColsPanel").hidden; g("repColsBtn").classList.toggle("is-on", !g("repColsPanel").hidden); });
      g("repCsvBtn").addEventListener("click", () => { if (lastQ) window.location.href = "/api/cv/report.csv?" + lastQ + "&cols=" + encodeURIComponent(visibleKeys().join(",")); });
      g("repPdfBtn").addEventListener("click", printReport);      // диалог печати → «Сохранить как PDF»
    }
    init();
  })();

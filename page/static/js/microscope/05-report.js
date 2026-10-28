// Страница микроскопа — вкладка «Отчёт»: отчёт по варкам за период (неделя / месяц / свой), вид «графики» или «числа», CSV и PDF (печать)
// Данные — /api/cv/report (report.py). Файлы microscope/01..08 делят общую глобальную область; помощники графиков, подсказки и печати
// отданы в window.microRep — ими пользуется и «Сравнение» (08-compare.js).
// Графики: точка — варка, ось X — варки подряд (дни — разделителями, без дыр на ночь и простой), линия — скользящая медиана по 7 варкам,
// серая полоса — «обычно» (25–75 %) за период, красный пунктир — цели маточника (порог муки, коридор размера: cv.seed_target), точки за
// ними — красные; отметки — смена модели и журнал изменений (вкладка «Сравнение»). Только на экране: фон — часть суток по началу варки
// (значок над полосой), галочки в шапке графика — средние температура / разрежение варки поверх точек, своя шкала справа; под графиками —
// табличка по частям суток. Свои SVG без библиотек (на заводе CDN может не открываться). PDF — тот же вид отдельным документом в скрытом
// iframe: страница приложения в печать не попадает, пустых листов нет.
"use strict";

  (function () {
    const g = (id) => document.getElementById(id);
    let period = "week", lastQ = null, lastRep = null, lastW = 0, tip = null, titleSaved = null, view = "charts";
    try { if (localStorage.getItem("microRepView") === "numbers") view = "numbers"; } catch (e) { }
    const dstr = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    const fmt = (v, d) => (v == null ? "—" : Number(v).toFixed(d == null ? 1 : d).replace(".", ",").replace(/^-/, "−"));
    const range = (a, b, d) => (a == null ? "—" : fmt(a, d) + (a < 0 || b < 0 ? " … " : "–") + fmt(b, d));     // «−0,80 … −0,79», а не «−0,80–−0,79»
    const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    // ts «2026-10-05_17_21_24» → «05.10 17:21» / «17:21»; день «2026-10-05» → «вс 05.10»
    const tsLbl = (ts) => { const m = String(ts || "").match(/^\d{4}-(\d\d)-(\d\d)_(\d\d)_(\d\d)/); return m ? m[2] + "." + m[1] + " " + m[3] + ":" + m[4] : (ts || "—"); };
    const hmLbl = (ts) => { const m = String(ts || "").match(/^\d{4}-\d\d-\d\d_(\d\d)_(\d\d)/); return m ? m[1] + ":" + m[2] : tsLbl(ts); };
    const WD = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
    const dayLbl = (day) => { const [y, m, d] = day.split("-").map(Number); return WD[new Date(y, m - 1, d).getDay()] + " " + String(d).padStart(2, "0") + "." + String(m).padStart(2, "0"); };
    const dmy = (t) => { const d = new Date(t * 1000); return String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0") + "." + d.getFullYear(); };
    const dur = (min) => { if (min == null) return "—"; const m = Math.round(min); return Math.floor(m / 60) + " ч " + String(m % 60).padStart(2, "0") + " мин"; };
    // «обычно» (25–75 %) — только от 4 варок: на 1–3 квартили ничего не значат, тогда просто «по N варкам»
    const usual = (m, d, unit) => (!m || !m.n ? "" : m.n >= 4 ? "обычно " + fmt(m.q1, d) + "–" + fmt(m.q3, d) + (unit || "") : "по " + m.n + " " + (m.n === 1 ? "варке" : "варкам"));
    const svLbl = (v) => fmt(v, 1).replace(/,0$/, "");
    const plural = (n, one, few, many) => { const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many; };
    const modelName = (m) => (m ? String(m).replace(/\.pt(?=@|$)/, "") : "не записана");
    const FLAG = { open: ["идёт", "Варка ещё идёт — в итоги не входит"], short: ["короткая", "Короче обычной (оборвали, остановка) — в итоги не входит"],
      long: ["затянутая", "Длиннее обычной (простой или две варки слились) — в итоги не входит"] };
    // нетипичная варка (boil_anomaly): выходит за «обычное» по муке, размеру или сросткам — по сравнению с 20 прошлыми варками той же модели
    const anomOf = (b) => (b && b.anom && b.anom.state === "anomaly" ? b.anom : null);
    const anomText = (b, sep) => anomOf(b).items.map((i) => i.name + " " + fmt(i.value, i.nd) + " " + i.unit + " — " + (i.word || (i.dir === "high" ? "выше" : "ниже")) + " обычного (" + (i.q1 === i.q3 ? fmt(i.q1, i.nd) : range(i.q1, i.q3, i.nd)) + ")").join(sep || "; ");
    const TG0 = { fines_max: 10, size_min_um: 200, size_max_um: 300 };
    const tg = (rep) => (rep && rep.targets) || TG0;
    // время от заводки: у Буи «готовность» (стадия 9) в пробы почти не попадает — тогда до последней пробы (как в сравнении)
    function seedKey(rep) {
      const e = rep.boils.filter((b) => b.seed_end_min != null);
      return e.length && e.filter((b) => b.seed_ready_min != null).length < 0.8 * e.length ? "seed_end_min" : "seed_ready_min";
    }
    const seedName = (rep) => (seedKey(rep) === "seed_end_min" ? "заводка → последняя проба" : "заводка → готовность");
    // за целью маточника: мука выше порога, размер вне коридора
    const finesBad = (rep, v) => v != null && v > tg(rep).fines_max;
    const sizeBad = (rep, v) => v != null && (v < tg(rep).size_min_um || v > tg(rep).size_max_um);
    const bad = (on, html) => (on ? '<span class="is-bad">' + html + "</span>" : html);

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

    function card(title, big, sub, boil, isBad) {
      return '<div class="micro-rep-card' + (isBad ? " is-bad" : "") + (boil ? ' micro-rep-link" data-boil="' + esc(boil) + '" title="Открыть эту варку на «Трендах»' : "") + '"><span>' + title + "</span><b>" + big + "</b>" + (sub ? "<small>" + sub + "</small>" : "") + "</div>";
    }

    // Столбцы таблицы «Варки» (вид «числа»): key совпадает с полем отчёта (и с колонкой CSV); «начало» есть всегда. Выбор запоминается в браузере.
    const COLS = [
      { key: "duration_min", head: "длительность", cell: (b) => dur(b.duration_min), on: true },
      { key: "probes", head: "проб", cell: (b) => b.probes, on: false },
      { key: "sv", head: "СВ", tip: "СВ мин–макс за варку", cell: (b) => range(b.sv_min, b.sv_max, 1), on: false, csv: ["sv_min", "sv_max"] },
      { key: "sv_end", head: "СВ конец", tip: "Конечное СВ варки (медиана последних проб)", cell: (b) => fmt(b.sv_end, 1), on: false },
      { key: "finish_probes", head: "проб финиша", tip: "Сколько проб в пределах «финиш, СВ» от конечного — по ним считается мука и рассев", cell: (b) => b.finish_probes, on: false },
      { key: "fines_avg", head: "мука финиша, %", tip: "Среднее по пробам финиша (последние 1,5–2 СВ). Красным — выше порога маточника. 95 % интервал — в подсказке",
        cell: (b, rep) => (b.fines_avg == null ? "—" : '<span title="95 % интервал: ' + range(b.fines_lo, b.fines_hi, 1) + ' — разница двух варок меньше интервала — шум, а не процесс">' + bad(finesBad(rep, b.fines_avg), fmt(b.fines_avg, 1)) + "</span>"),
        on: true, csv: ["fines_avg", "fines_lo", "fines_hi"] },
      { key: "fines_range", head: "мука мин–макс", tip: "По пробам финиша", cell: (b) => range(b.fines_min, b.fines_max, 1), on: false, csv: ["fines_min", "fines_max"] },
      { key: "fines_all", head: "мука по всей варке, %", tip: "Среднее по всем пробам после порога «Мука с СВ» (справочно: включает начало варки)", cell: (b) => fmt(b.fines_all, 1), on: false },
      { key: "fin_um", head: "размер финиша, мкм", tip: "Средний размер кристалла на финише. Красным — вне коридора маточника", cell: (b, rep) => bad(sizeBad(rep, b.fin_um), fmt(b.fin_um, 0)), on: true },
      { key: "fin_cv", head: "однородность, %", tip: "Разброс размера кристаллов в пробе на финише (CV): меньше — однороднее", cell: (b) => fmt(b.fin_cv, 1), on: true },
      { key: "fin_agg_pct", head: "сростки, %", tip: "Доля сростков среди кристаллов на финише, по числу", cell: (b) => fmt(b.fin_agg_pct, 1), on: true },
      { key: "seed", head: "от заводки, мин", tip: "От заводки до готовности (стадия 9); со звёздочкой — до последней пробы, если «готовности» в пробах нет",
        cell: (b) => (b.seed_ready_min != null ? fmt(b.seed_ready_min, 0) : b.seed_end_min != null ? '<span title="до последней пробы">' + fmt(b.seed_end_min, 0) + "*</span>" : "—"),
        on: true, csv: ["seed_ready_min", "seed_end_min"] },
      { key: "water", head: "вода", tip: "Вода на стабилизации: по клапану, а без клапана — по провалу СВ", cell: (b) => (b.water == null ? "—" : b.water ? (b.water_by === "sv" ? '<span title="по провалу СВ">да*</span>' : "да") : "нет"),
        on: true, csv: ["water", "water_dose"] },
      { key: "reject_pct", head: "брак, %", cell: (b) => fmt(b.reject_pct, 1), on: false },
      { key: "frac_zones", head: "разломов", cell: (b) => b.frac_zones, on: false },
      { key: "mean_um", head: "размер средний, мкм", tip: "Средний размер кристалла по всем пробам варки", cell: (b) => fmt(b.mean_um, 0), on: false },
      { key: "model", head: "модель", tip: "Модель распознавания (пишется в пробу с 1.9.76)", cell: (b) => esc(b.model ? modelName(b.model) : "—"), on: false },
      { key: "sieve", head: "рассев финиша", tip: "Доли сит на финише: <0,2 · 0,2–0,5 · 0,5–0,7 · >0,7 мм", cell: (b) => sieveBar(b.sieve), on: true },
    ];
    const LINKED = ["fines_avg", "fines_range", "fines_all"];     // значения муки в таблице — кликабельные: открывают эту варку на «Трендах»
    // Графики (вид «графики»): по одному показателю на график; key — поле варки, val — своё значение. Выбор запоминается в браузере.
    const CHARTS = [
      { key: "fines_avg", title: "Мука на финише, %", d: 1, zero: true, whisk: true, on: true },
      { key: "fin_um", title: "Размер на финише, мкм", d: 0, corridor: true, on: true },
      { key: "fin_cv", title: "Однородность: разброс размера в пробе, %", d: 1, on: true },
      { key: "fin_agg_pct", title: "Сростки на финише, % по числу", d: 1, zero: true, on: true },
      { key: "seed", title: (rep) => seedName(rep).replace(/^з/, "З") + ", мин", d: 0, val: (b, rep) => b[seedKey(rep)], on: true },
      { key: "sieve", title: "Рассев финиша по дням, %", on: true },
      { key: "duration_min", title: "Длительность варки, мин", d: 0, on: false },
      { key: "sv_end", title: "СВ в конце варки", d: 1, on: false },
      { key: "water_dose", title: "Доза воды на стабилизации, %·с", d: 0, zero: true, on: false },
    ];
    const titleOf = (c, rep) => (typeof c.title === "function" ? c.title(rep || { boils: [] }) : c.title);
    const COLS_KEY = "microRepCols3";        // 3 — 1.9.89: столбцы «размер СВ 86 → финиш» и «прирост» убраны, добавлены однородность, сростки, от заводки
    const CHARTS_KEY = "microRepCharts1";
    function stateOf(list, key) {
      let saved = null; try { saved = JSON.parse(localStorage.getItem(key) || "null"); } catch (e) { }
      return list.map((c) => ({ ...c, on: saved && c.key in saved ? !!saved[c.key] : c.on }));
    }
    function saveState(st, key) { try { localStorage.setItem(key, JSON.stringify(Object.fromEntries(st.map((c) => [c.key, c.on])))); } catch (e) { } }
    const colOn = (st, key) => { const c = st.find((x) => x.key === key); return !c || c.on; };

    // ===== графики (SVG) =====
    function niceStep(span, n) {
      const raw = (span || 1) / Math.max(1, n), p = Math.pow(10, Math.floor(Math.log10(raw)));
      return [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw - 1e-12);
    }
    function yScale(lo, hi, top, h, n) {
      const step = niceStep(hi - lo, n || 4), y0 = Math.floor(lo / step) * step, y1 = Math.max(y0 + step, Math.ceil(hi / step) * step);
      const ticks = []; for (let v = y0; v <= y1 + step * 1e-6; v += step) ticks.push(+v.toFixed(6));
      const d = [0, 1, 2, 3].find((k) => Math.abs(step * Math.pow(10, k) - Math.round(step * Math.pow(10, k))) < 1e-6);     // 2,5 → 1 знак, 0,25 → 2
      return { ticks, step, d: d == null ? 2 : d, lo: y0, hi: y1, f: (v) => top + h - ((v - y0) / (y1 - y0)) * h };
    }
    const legend = (items) => '<span class="micro-rep-legend">' + items.map((it) => '<span><i class="lg lg-' + it[0] + '"></i>' + it[1] + "</span>").join("") + "</span>";
    function chartHead(title, items, extra) { return '<div class="micro-rep-chart__head"><b>' + title + "</b>" + legend(items) + (extra || "") + "</div>"; }
    const median = (v) => { const s = v.slice().sort((a, b) => a - b), n = s.length; return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null; };
    const quant = (s, p) => { if (!s.length) return null; const k = (s.length - 1) * p, i = Math.floor(k); return s[i] + (s[Math.min(i + 1, s.length - 1)] - s[i]) * (k - i); };
    const MED_N = 7;            // скользящая медиана — по стольким типичным варкам (3 до, 3 после)

    // части суток (report.DAY_PARTS: утро 08–14, день 14–20, вечер 20–02, ночь 02–08): значок 12×12 вокруг (0, 0) — рассвет, солнце, закат, луна
    const PART_ICON = {
      morning: '<path class="dpi-f" d="M-3,3.5A3,3 0 0 1 3,3.5Z"></path><path class="dpi-l" d="M-5.5,3.5H5.5M0,-1V-3.4M-3.4,0L-4.8,-1.3M3.4,0L4.8,-1.3"></path>',
      day: '<circle class="dpi-f" r="2.6"></circle><path class="dpi-l" d="M0,-4.3V-5.8M0,4.3V5.8M-4.3,0H-5.8M4.3,0H5.8M3,-3L4.1,-4.1M-3,-3L-4.1,-4.1M3,3L4.1,4.1M-3,3L-4.1,4.1"></path>',
      evening: '<path class="dpi-f" d="M-3,3.5A3,3 0 0 1 3,3.5Z"></path><path class="dpi-l" d="M-5.5,3.5H5.5M-2,-3.6L0,-1.6L2,-3.6"></path>',
      night: '<path class="dpi-f" d="M-0.09,-4.6A4.6,4.6 0 1 0 4.21,1.85A3.9,3.9 0 0 1 -0.09,-4.6Z"></path>',
    };
    const partOf = (rep, key) => (rep.day_parts || []).find((p) => p.key === key);
    const partHours = (p) => String(p.from).padStart(2, "0") + ":00–" + String(p.to).padStart(2, "0") + ":00";
    const partIcon = (key) => '<svg class="micro-rep-dpi" viewBox="-6 -6 12 12" width="12" height="12" aria-hidden="true">' + (PART_ICON[key] || "") + "</svg>";
    // наложение режима варки на графики по варкам: галочки в шапке, у каждого графика свои, запоминаются; только на экране.
    // span — наименьший размах правой шкалы: шум ниже точности ПЛК (разрежение — шаг 0,1 бар) не растягивается в «скачки»
    const OVER = [
      { key: "temp", field: "temp_avg", name: "температура", unit: "°C", d: 1, span: 4, tip: "Средняя температура в аппарате за варку (по пробам стадий 3–9), шкала справа" },
      { key: "vac", field: "vac_avg", name: "разрежение", unit: "бар", d: 2, span: 0.2, tip: "Среднее разрежение сверху за варку (по пробам стадий 3–9; ПЛК отдаёт с шагом 0,1 бар), шкала справа" },
    ];
    const OVER_KEY = "microRepOver1";
    let over = {};
    try { over = JSON.parse(localStorage.getItem(OVER_KEY) || "{}") || {}; } catch (e) { }
    const overOn = (ck, ok) => !!(over[ck] && over[ck][ok]);
    const OV_W = 44;            // ширина правой шкалы одного наложения, px
    // другие заводы (папка database/<завод>/mvs.db рядом с приложением): галочка у графика, поверх — медиана и «обычно» (25–75 %) их варок за тот же период;
    // только на экране. Нет папки — rep.others_available пуст и галочки нет
    let othersData = null, othersQ = null;
    const othersStat = (rep, c, pl) => {
      const val = c.val ? (b) => c.val(b, rep) : (b) => b[c.key];
      const v = (pl.boils || []).filter((b) => !b.flag).map(val).filter((a) => a != null).sort((a, b) => a - b);
      return v.length >= 3 ? { q1: quant(v, 0.25), med: quant(v, 0.5), q3: quant(v, 0.75), n: v.length } : null;
    };
    const othersFor = (rep, ck) => (overOn(ck, "others") && othersData && othersQ === lastQ ? othersData.filter((p) => p.boils && p.boils.length) : []);
    const anyOthersOn = () => Object.keys(over).some((k) => over[k] && over[k].others);
    async function loadOthers() {
      if (!lastQ || !lastRep || !(lastRep.others_available || []).length || !anyOthersOn()) return;
      if (othersData && othersQ === lastQ) return;
      try { othersData = (await (await fetch("/api/cv/others/report?" + lastQ)).json()).plants || []; othersQ = lastQ; } catch (e) { othersData = null; }
    }

    // общая рамка графиков по варкам: ось X — варки подряд; дни — разделителями и подписями, вода — полосой, смена модели и изменения — линиями;
    // parts — фон частей суток со значками над полосами (на экране)
    function seqFrame(rep, x, y, L, T, W, H, parts) {
      const B = rep.boils, n = B.length, step = W / n;
      let s = "";
      for (let i = 0; parts && i < n;) {                  // части суток: подряд идущие варки одной части — одна полоса, значок — если полоса шире 14 px
        let j = i; while (j + 1 < n && B[j + 1].part === B[i].part) j++;
        const p = partOf(rep, B[i].part), x0 = L + i * step, bw = (j - i + 1) * step;
        if (p) {
          s += '<rect class="dp dp-' + p.key + '" x="' + x0.toFixed(1) + '" y="' + T + '" width="' + bw.toFixed(1) + '" height="' + H + '"></rect>';
          if (bw >= 14) s += '<g class="dpi" transform="translate(' + (x0 + bw / 2).toFixed(1) + "," + (T - 13) + ')" data-tip="' + esc(p.name + " " + partHours(p) + " — по началу варки") + '">' +
            '<rect class="hit" x="-7" y="-7" width="14" height="14"></rect>' + PART_ICON[p.key] + "</g>";
        }
        i = j + 1;
      }
      for (let i = 0; i < n;) {                           // вода: подряд идущие варки с водой — одна полоса
        if (B[i].water !== true) { i++; continue; }
        let j = i; while (j + 1 < n && B[j + 1].water === true) j++;
        s += '<rect class="wband" x="' + (x(i) - step / 2).toFixed(1) + '" y="' + T + '" width="' + ((j - i + 1) * step).toFixed(1) + '" height="' + H + '"></rect>';
        i = j + 1;
      }
      y.ticks.forEach((v) => {
        const yy = y.f(v).toFixed(1);
        s += '<line class="grid" x1="' + L + '" x2="' + (L + W) + '" y1="' + yy + '" y2="' + yy + '"></line><text class="ylab" x="' + (L - 6) + '" y="' + yy + '" dy="0.32em">' + fmt(v, y.d) + "</text>";
      });
      const days = [];                                    // [день, первая варка, последняя]
      B.forEach((b, i) => { const d = b.start.slice(0, 10); if (!days.length || days[days.length - 1][0] !== d) days.push([d, i, i]); else days[days.length - 1][2] = i; });
      const every = Math.max(1, Math.ceil(days.length * 40 / W));          // подпись — не теснее ~40 px
      days.forEach(([d, i0, i1], k) => {
        if (i0 > 0) { const xx = (L + i0 * step).toFixed(1); s += '<line class="vgrid" x1="' + xx + '" x2="' + xx + '" y1="' + T + '" y2="' + (T + H) + '"></line>'; }
        if (k % every === 0) s += '<text class="xlab" x="' + ((x(i0) + x(i1)) / 2).toFixed(1) + '" y="' + (T + H + 15) + '">' + d.slice(8, 10) + "." + d.slice(5, 7) + "</text>";
      });
      s += '<line class="axis" x1="' + L + '" x2="' + (L + W) + '" y1="' + (T + H) + '" y2="' + (T + H) + '"></line>';
      const marks = [];                                   // смена модели и журнал изменений: x и подпись
      (rep.segments || []).slice(1).forEach((sg) => {
        const i = B.findIndex((b) => b.start === sg.from); if (i > 0) marks.push({ x: L + i * step, cls: "mline", text: "модель " + modelName(sg.model), tip: "С этой варки — модель распознавания " + modelName(sg.model) });
      });
      (rep.changes || []).forEach((c) => {
        const inb = B.findIndex((b) => b.t_start <= c.t && c.t <= b.t_end);     // изменение посреди варки — линия по этой варке
        const k = inb >= 0 ? null : B.filter((b) => b.t_start < c.t).length;
        const xx = inb >= 0 ? x(inb) : L + k * step;
        if (xx >= L && xx <= L + W) marks.push({ x: xx, cls: "cline", text: c.text, tip: c.label + " · " + c.text });
      });
      marks.sort((a, b) => a.x - b.x).forEach((m, i) => {
        const right = m.x + Math.min(m.text.length, 26) * 5.4 + 8 > L + W;      // не влезает справа — подпись слева от линии
        const ty = T + 7 + (i % 2) * 11, label = m.text.length > 26 ? m.text.slice(0, 25) + "…" : m.text;
        s += '<g data-tip="' + esc(m.tip) + '"><line class="' + m.cls + '" x1="' + m.x.toFixed(1) + '" x2="' + m.x.toFixed(1) + '" y1="' + (T - 4) + '" y2="' + (T + H) + '"></line>' +
          '<text class="mlab" x="' + (m.x + (right ? -4 : 4)).toFixed(1) + '" y="' + ty + '" text-anchor="' + (right ? "end" : "start") + '">' + esc(label) + "</text></g>";
      });
      return s;
    }
    function boilTip(rep, b) {
      const sk = seedKey(rep);
      return tsLbl(b.start) + " · " + dur(b.duration_min) + "\nмука финиша " + fmt(b.fines_avg, 1) + " %" + (b.fines_lo != null ? " (95 %: " + range(b.fines_lo, b.fines_hi, 1) + ")" : "") +
        "\nразмер финиша " + fmt(b.fin_um, 0) + " мкм · однородность " + fmt(b.fin_cv, 1) + " % · сростки " + fmt(b.fin_agg_pct, 1) + " %" +
        (b[sk] != null ? "\n" + seedName(rep) + " " + fmt(b[sk], 0) + " мин" : "") +
        (b.water != null ? "\nвода: " + (b.water ? "да" : "нет") : "") +
        (b.temp_avg != null || b.vac_avg != null ? "\n" + OVER.filter((o) => b[o.field] != null).map((o) => o.name + " " + fmt(b[o.field], o.d) + " " + o.unit).join(" · ") : "") +
        (b.flag ? "\n" + FLAG[b.flag][0] + " — в итоги не входит" : "") + (anomOf(b) ? "\nне как обычно: " + anomText(b) : "");
    }
    const extras = (rep) => [].concat(rep.boils.some((b) => b.water === true) ? [["water", "вода на стабилизации"]] : [], (rep.segments || []).length > 1 ? [["model", "смена модели"]] : [],
      (rep.changes || []).length ? [["change", "изменение"]] : []);

    // график одного показателя по варкам: точки, «обычно» периода, скользящая медиана, цели маточника; на экране — части суток и наложения
    function chartMetric(rep, w, c, opt) {
      const val = c.val ? (b) => c.val(b, rep) : (b) => b[c.key];
      const B = rep.boils, P = B.map((b, i) => ({ b, i, v: val(b) })).filter((p) => p.v != null);
      if (P.length < 2) return "";
      const screen = !(opt && opt.print), have = OVER.filter((o) => B.filter((b) => b[o.field] != null).length >= 2);
      const ovs = screen ? have.filter((o) => overOn(c.key, o.key)) : [];
      const L = 44, R = 12 + ovs.length * OV_W, T = 26, H = 130, W = w - L - R, hh = T + H + 22, step = W / B.length;
      const x = (i) => L + (i + 0.5) * step, t = tg(rep);
      const typ = P.filter((p) => !p.b.flag), tv = typ.map((p) => p.v).sort((a, b) => a - b);
      const lim = c.key === "fines_avg" ? [t.fines_max] : c.corridor ? [t.size_min_um, t.size_max_um] : [];
      const isBad = (v) => (c.key === "fines_avg" ? finesBad(rep, v) : c.corridor ? sizeBad(rep, v) : false);
      // шкала — по точкам, усам типичных варок и целям: широкий интервал обрывка не сжимает остальные
      const oth = screen ? othersFor(rep, c.key).map((pl, k) => ({ pl, k, st: othersStat(rep, c, pl) })).filter((o) => o.st) : [];
      const ext = P.map((p) => p.v).concat(c.whisk ? typ.filter((p) => p.b.fines_hi != null).map((p) => p.b.fines_hi) : [], lim, oth.flatMap((o) => [o.st.q1, o.st.q3]));
      let lo = Math.min(...ext), hi = Math.max(...ext); const pad = (hi - lo) * 0.08 || 1;
      lo = c.zero ? 0 : lo - pad; hi += pad;
      const y = yScale(lo, hi, T, H, 4);
      let s = seqFrame(rep, x, y, L, T, W, H, screen);
      if (tv.length >= 4) {                               // «обычно» за период: 25–75 % типичных варок
        const q1 = quant(tv, 0.25), q3 = quant(tv, 0.75);
        s += '<rect class="qband" x="' + L + '" y="' + y.f(q3).toFixed(1) + '" width="' + W + '" height="' + Math.max(1, y.f(q1) - y.f(q3)).toFixed(1) + '"><title>обычно ' + range(q1, q3, c.d) + "</title></rect>";
      }
      oth.forEach((o) => {                             // другой завод: «обычно» полосой на всю ширину, медиана пунктиром, подпись у правого края
        const yq3 = y.f(o.st.q3), yq1 = y.f(o.st.q1), ym = y.f(o.st.med).toFixed(1), nm = o.pl.name.charAt(0).toUpperCase() + o.pl.name.slice(1);
        const tipT = nm + ": медиана " + fmt(o.st.med, c.d) + ", обычно " + range(o.st.q1, o.st.q3, c.d) + " (" + o.st.n + " варок)" +
          (o.pl.models && o.pl.models.length ? "\nмодель " + o.pl.models.map(modelName).join(", ") : "") + (o.pl.fines_from_sv != null ? "\nмука с СВ " + fmt(o.pl.fines_from_sv, 0) : "") +
          "\nдругой завод: цифры муки и размера сравнимы приблизительно (модель и порог могут быть другими)";
        s += '<g data-tip="' + esc(tipT) + '"><rect class="oband oth-' + o.k + '" x="' + L + '" y="' + yq3.toFixed(1) + '" width="' + W + '" height="' + Math.max(1, yq1 - yq3).toFixed(1) + '"></rect>' +
          '<line class="oline oth-' + o.k + '" x1="' + L + '" x2="' + (L + W) + '" y1="' + ym + '" y2="' + ym + '"></line>' +
          '<text class="olabx oth-' + o.k + '" x="' + (L + 4) + '" y="' + ym + '" dy="-0.35em">' + esc(nm) + " " + fmt(o.st.med, c.d) + '</text>' +
          '<rect class="hit" x="' + L + '" y="' + (Math.min(yq3, +ym) - 2).toFixed(1) + '" width="' + W + '" height="' + (Math.max(yq1, +ym) - Math.min(yq3, +ym) + 4).toFixed(1) + '"></rect></g>';
      });
      lim.forEach((v, k) => {
        const yy = y.f(v).toFixed(1);
        s += '<line class="lim" x1="' + L + '" x2="' + (L + W) + '" y1="' + yy + '" y2="' + yy + '"></line><text class="limlab" x="' + (L + W - 2) + '" y="' + yy + '" dy="' + (k || c.key === "fines_avg" ? "-0.35em" : "1.05em") + '">' +
          (c.key === "fines_avg" ? "порог " + fmt(v, 0) + " %" : (k ? "коридор до " : "коридор от ") + fmt(v, 0)) + "</text>";
      });
      if (c.whisk) typ.forEach((p) => { if (p.b.fines_lo != null && p.b.fines_hi != null) { const xx = x(p.i).toFixed(1); s += '<line class="whisk" x1="' + xx + '" x2="' + xx + '" y1="' + y.f(p.b.fines_lo).toFixed(1) + '" y2="' + y.f(p.b.fines_hi).toFixed(1) + '"></line>'; } });
      if (typ.length >= 3) {                              // скользящая медиана по MED_N типичным варкам
        const h = Math.floor(MED_N / 2);
        const pts = typ.map((p, k) => x(p.i).toFixed(1) + "," + y.f(median(typ.slice(Math.max(0, k - h), k + h + 1).map((q) => q.v))).toFixed(1));
        s += '<polyline class="dline" points="' + pts.join(" ") + '"></polyline>';
      }
      ovs.forEach((o, k) => {                             // наложение под точками: своя шкала справа (числа — приглушённые, цвет ряда — у значка над шкалой)
        const v = B.map((b) => b[o.field]), vv = v.filter((a) => a != null);
        const lo = Math.min(...vv), hi = Math.max(...vv), mid = (lo + hi) / 2, half = Math.max((hi - lo) * 0.6, o.span / 2);
        const oy = yScale(mid - half, mid + half, T, H, 4), ax = L + W + k * OV_W;
        s += '<line class="axis" x1="' + ax + '" x2="' + ax + '" y1="' + T + '" y2="' + (T + H) + '"></line>' +
          '<path class="ovm ov-' + o.key + '" d="M' + (ax + 7) + "," + (T - 12) + "l3,3l-3,3l-3,-3z\"></path><text class=\"olab\" x=\"" + (ax + 13) + '" y="' + (T - 9) + '" dy="0.32em">' + o.unit + "</text>";
        oy.ticks.forEach((tk) => { const yy = oy.f(tk).toFixed(1); s += '<line class="axis" x1="' + ax + '" x2="' + (ax + 3) + '" y1="' + yy + '" y2="' + yy + '"></line><text class="olab" x="' + (ax + 5) + '" y="' + yy + '" dy="0.32em">' + fmt(tk, oy.d) + "</text>"; });
        let seg = [];
        const flush = () => { if (seg.length > 1) s += '<polyline class="ov ov-' + o.key + '" points="' + seg.join(" ") + '"></polyline>'; seg = []; };
        v.forEach((a, i) => { if (a == null) flush(); else seg.push(x(i).toFixed(1) + "," + oy.f(a).toFixed(1)); });
        flush();
        v.forEach((a, i) => {
          if (a == null) return;
          const xx = x(i), yy = oy.f(a);
          s += '<g data-tip="' + esc(tsLbl(B[i].start) + " · " + o.name + " " + fmt(a, o.d) + " " + o.unit) + '"><path class="ovm ov-' + o.key + '" d="M' + xx.toFixed(1) + "," + (yy - 3).toFixed(1) +
            'l3,3l-3,3l-3,-3z"></path><circle class="hit" cx="' + xx.toFixed(1) + '" cy="' + yy.toFixed(1) + '" r="6"></circle></g>';
        });
      });
      const r = Math.max(1.8, Math.min(3.5, step * 0.4));
      P.forEach((p) => {
        const xx = x(p.i).toFixed(1), yy = y.f(p.v).toFixed(1), cls = "dot s1" + (p.b.flag ? " is-off" : "") + (isBad(p.v) ? " bad" : "") + (screen && anomOf(p.b) ? " anom" : "");
        s += '<g class="pt" data-boil="' + esc(p.b.start) + '" data-tip="' + esc(boilTip(rep, p.b)) + '"><circle class="' + cls + '" cx="' + xx + '" cy="' + yy + '" r="' + r.toFixed(1) + '"></circle><circle class="hit" cx="' + xx + '" cy="' + yy + '" r="8"></circle></g>';
      });
      const items = [["dot1", "варка"]].concat(P.some((p) => p.b.flag) ? [["off", "не в итогах"]] : [], screen && P.some((p) => anomOf(p.b)) ? [["anom", "не как обычно"]] : [], [["line", "медиана по " + MED_N + " варкам"], ["qband", "обычно"]],
        c.whisk ? [["whisk", "95 %"]] : [], lim.length ? [["lim", c.key === "fines_avg" ? "порог" : "коридор"], ["bad", "за целью"]] : [], extras(rep));
      const plants = screen ? rep.others_available || [] : [];
      const box = screen && (have.length || plants.length) ? '<span class="micro-rep-over">' + (plants.length ? '<label title="Показать на графике варки других заводов из папки database (' + esc(plants.join(", ")) + '): медиана и «обычно» за тот же период. Только на экране"><input type="checkbox" data-over="' + c.key + ':others"' +
        (overOn(c.key, "others") ? " checked" : "") + ' /><i class="lg-oth"></i>другие заводы</label>' : "") + have.map((o) => '<label title="' + esc(o.tip) + '"><input type="checkbox" data-over="' + c.key + ":" + o.key + '"' +
        (overOn(c.key, o.key) ? " checked" : "") + ' /><i class="lg-ov ov-' + o.key + '"></i>' + o.name + "</label>").join("") + "</span>" : "";
      return '<div class="micro-rep-chart">' + chartHead(titleOf(c, rep), items, box) +
        '<svg viewBox="0 0 ' + w + " " + hh + '" width="' + w + '" height="' + hh + '" role="img" aria-label="' + esc(titleOf(c, rep)) + '">' + s + "</svg></div>";
    }

    // рассев по дням: столбик 100 % на день, снизу самые мелкие (<0,2 мм), сверху — крупные; между долями — зазор 2 px
    function chartSieve(rep, w) {
      const D = (rep.days || []).map((d) => ({ d, v: sieve4(d.sieve) })).filter((x) => x.v);
      if (D.length < 2) return "";
      const L = 44, R = 12, T = 10, H = 110, W = w - L - R, hh = T + H + 22, slot = W / D.length, bw = Math.min(24, slot * 0.6);
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
      const t = rep.totals, cols = stateOf(COLS, COLS_KEY), vis = cols.filter((c) => c.on), on = (k) => colOn(cols, k), T = tg(rep), sk = seedKey(rep);
      const first = rep.boils[0];
      const segs = rep.segments || [], oneModel = segs.length === 1 && segs[0].model ? segs[0].model : null;
      const fromDay = dmy(rep.from), toDay = dmy(rep.to), dataFrom = first.start.slice(0, 10).split("-").reverse().join(".");
      const meta = [rep.serial ? "камера " + esc(rep.serial) : "", "период " + fromDay.slice(0, 5) + " – " + toDay + (dataFrom !== fromDay ? ", варки с " + dataFrom.slice(0, 5) : ""),
        oneModel ? "модель " + esc(modelName(oneModel)) : "", "сформирован " + dmy(rep.generated) + " " + new Date(rep.generated * 1000).toTimeString().slice(0, 5)].filter(Boolean).join(" · ");
      let h = '<div class="micro-rep-head"><div><div class="micro-rep-title">Отчёт по варкам</div><div class="micro-rep-sub">' + meta + "</div></div>" +
        (opt.print ? '<img class="micro-rep-logo" src="/static/icon/Logo_Black.png" alt="Maximum Automation">' : "") + "</div>";
      const off = t.boils - t.typical;
      h += '<div class="micro-rep-cards">' +
        card("Варок", t.boils, "проб " + t.probes + (off ? " · не в итогах " + off : "")) +
        (t.fines_avg.n ? card("Мука на финише", fmt(t.fines_avg.median, 1) + " %", usual(t.fines_avg, 1, " %") + " · порог " + fmt(T.fines_max, 0), null, finesBad(rep, t.fines_avg.median)) : "") +
        (t.fin_um.n ? card("Размер на финише", fmt(t.fin_um.median, 0) + " мкм", usual(t.fin_um, 0) + " · цель " + fmt(T.size_min_um, 0) + "–" + fmt(T.size_max_um, 0), null, sizeBad(rep, t.fin_um.median)) : "") +
        (t.fin_cv && t.fin_cv.n ? card("Однородность", fmt(t.fin_cv.median, 1) + " %", "разброс размера · " + usual(t.fin_cv, 1)) : "") +
        (t.fin_agg_pct && t.fin_agg_pct.n ? card("Сростки", fmt(t.fin_agg_pct.median, 1) + " %", usual(t.fin_agg_pct, 1, " %")) : "") +
        (t[sk] && t[sk].n ? card(sk === "seed_end_min" ? "Заводка → конец" : "Заводка → готовность", dur(t[sk].median), usual(t[sk], 0, " мин")) : "") +
        (t.water_known ? card("Вода на стабилизации", t.water + " из " + t.water_known, plural(t.water_known, "варки", "варок", "варок") + " с водой") : "") +
        (t.best && t.fines_avg.n >= 3 ? card("Лучшая варка", fmt(t.best.fines_avg, 1) + " %", "мука · " + tsLbl(t.best.start), opt.print ? null : t.best.start) : "") +
        (t.worst && t.fines_avg.n >= 3 ? card("Худшая варка", fmt(t.worst.fines_avg, 1) + " %", "мука · " + tsLbl(t.worst.start), opt.print ? null : t.worst.start, finesBad(rep, t.worst.fines_avg)) : "") + "</div>";
      if (segs.length > 1) {
        h += '<div class="micro-rep-warn"><b>В периоде меняли модель распознавания.</b> Модели по-разному видят мелочь: цифры муки и размера ' +
          "разных моделей не сравнивают. Итоги выше смешивают модели — сравнивай по строкам:</div>" +
          '<div class="micro-rep-wrap"><table class="micro-rep-table micro-rep-table--models"><thead><tr><th>модель</th><th>варки</th><th>варок</th><th>мука финиша, %</th><th>размер финиша, мкм</th></tr></thead><tbody>' +
          segs.map((s) => "<tr><td>" + esc(modelName(s.model)) + "</td><td>" + tsLbl(s.from) + " – " + tsLbl(s.to) + "</td><td>" + s.boils + "</td><td>" + fmt(s.fines_avg.median, 1) +
            ' <small class="micro-ci">' + (s.fines_avg.n >= 4 ? range(s.fines_avg.q1, s.fines_avg.q3, 1) : "") + "</small></td><td>" + fmt(s.fin_um.median, 0) + "</td></tr>").join("") + "</tbody></table></div>";
      }
      if (rep.model_check) h += '<div class="micro-rep-warn"><b>Модель проверялась с разным результатом.</b> ' + esc(rep.model_check.detail) + ". Цифры муки и размера до и после нельзя сравнивать напрямую." + "</div>";
      if (view === "charts") {
        const w = opt.width;
        h += '<div class="micro-rep-charts">' + stateOf(CHARTS, CHARTS_KEY).filter((c) => c.on).map((c) => (c.key === "sieve" ? chartSieve(rep, w) : chartMetric(rep, w, c, opt))).join("") + "</div>";
        if (!opt.print) h += anomHtml(rep) + partsHtml(rep);
      } else h += numbersHtml(rep, opt, cols, vis, on);

      h += '<div class="micro-rep-note"><b>Как читать</b><ul>' +
        "<li>Мука на финише — среднее по пробам последних " + svLbl(rep.fin_sv) + " СВ варки (только где СВ уже не ниже «Мука с СВ» " + svLbl(rep.fines_from_sv) + ").</li>" +
        "<li>Главное число — медиана, то есть типичная варка; «обычно» — половина варок лежит между этими числами (25–75 %).</li>" +
        (view === "charts" ? "<li>На графиках: точка — варка (подряд, дни — разделителями), линия — медиана по " + MED_N + " соседним варкам, серая полоса — «обычно» за период. " +
          "Точка ниже или выше полосы — варка не как все; линия ушла из полосы — процесс сдвинулся.</li>" : "") +
        (view === "charts" && !opt.print ? "<li>Фон — часть суток по началу варки (смены 08–20 и 20–08 пополам): утро, день, вечер, ночь — значок над полосой. " +
          "Галочки «температура» / «разрежение» в шапке графика — средние за варку по пробам, шкала справа; в PDF не идут.</li>" : "") +
        "<li>Цели маточника: мука не выше " + fmt(T.fines_max, 0) + " %, размер на финише " + fmt(T.size_min_um, 0) + "–" + fmt(T.size_max_um, 0) + " мкм — красным всё, что за ними (меняются на вкладке «CV»).</li>" +
        "<li>Усы у муки — 95 % интервал варки: если интервалы двух варок перекрываются, разница между ними — шум пробы, а не процесс.</li>" +
        "<li>Однородность — разброс размера кристаллов в пробе: меньше — кристалл ровнее. Сростки — доля сросшихся кристаллов по числу.</li>" +
        "<li>В итоги не входят идущая варка, короткие (короче " + Math.round((rep.short_k || 0.6) * 100) + " % от обычной длительности) и затянутые (длиннее в " + svLbl(rep.long_k || 1.5) + " раза) — они с пометкой.</li>" +
        ((rep.changes || []).length ? "<li>Пунктир с подписью — изменение из журнала (вкладка «Сравнение»): там же сравниваются варки до и после.</li>" : "") +
        (rep.boils.some((x) => x.water != null && x.water_by === "sv") ? "<li>Вода «да*» — найдена по провалу СВ на стабилизации (клапан воды не читается).</li>" : "") +
        (sk === "seed_end_min" ? "<li>«Готовность» в пробы не попадает — время считается от заводки до последней пробы варки.</li>" : "") +
        (segs.length === 1 && !segs[0].model ? "<li>Модель распознавания в пробах не записана (пробы до 1.9.76): если модель в этом периоде меняли, цифры до и после смены не сравнивать.</li>" : "") +
        "<li>Варка входит в период по времени начала; учитываются варки не короче " + rep.min_probes + " проб.</li></ul></div>";
      return h;
    }

    // нетипичные варки периода (только на экране): список с причиной; «мало данных» — честно, без тревоги
    function anomHtml(rep) {
      const A = rep.anomalies; if (!A) return "";
      const bad = rep.boils.filter(anomOf);
      const note = "сравнение с " + A.ref_n + " прошлыми варками той же модели и порога, начатыми после последнего изменения в журнале; " +
        "тревога — только при заметном выходе за обычное. Варок без опоры (меньше " + A.min_ref + " прошлых): " + A.few;
      if (!bad.length) return '<section class="micro-rep-sec"><div class="micro-rep-h">Нетипичные варки <small>' + (A.judged ? "не найдено — " + A.judged + " " + plural(A.judged, "варка оценена", "варки оценены", "варок оценено") : "мало данных для оценки") + " · " + note + "</small></div></section>";
      return '<section class="micro-rep-sec"><div class="micro-rep-h">Нетипичные варки <small>' + bad.length + " из " + A.judged + " оценённых · " + note + "</small></div>" +
        '<ul class="micro-rep-anom">' + bad.map((b) => '<li><a class="micro-rep-link" data-boil="' + esc(b.start) + '" title="Открыть эту варку на «Трендах»">' + tsLbl(b.start) + "</a> — " + esc(anomText(b)) + "</li>").join("") + "</ul></section>";
    }

    // под графиками (только на экране): итоги по частям суток — честная проверка «день / ночь» вместо разглядывания фона
    function partsHtml(rep) {
      const P = (rep.day_parts || []).filter((p) => p.boils);
      if (P.length < 2) return "";
      const cell = (m, d, isBad) => (!m || m.median == null ? "—" : bad(isBad, fmt(m.median, d)) + (m.n >= 4 ? ' <small class="micro-ci">' + range(m.q1, m.q3, d) + "</small>" : ""));
      const cols = [["варок", (p) => p.boils], ["мука финиша, %", (p) => cell(p.fines_avg, 1, finesBad(rep, p.fines_avg.median))],
        ["размер финиша, мкм", (p) => cell(p.fin_um, 0, sizeBad(rep, p.fin_um.median))]].concat(
        OVER.filter((o) => P.some((p) => p[o.field] && p[o.field].n)).map((o) => [o.name + ", " + o.unit, (p) => cell(p[o.field], o.d, false)]));
      return '<section class="micro-rep-sec"><div class="micro-rep-h">По частям суток <small>по началу варки · медиана по типичным варкам, серым — «обычно» (25–75 %): ' +
        "перекрываются — разница частей суток в шуме пробы, а не в процессе</small></div>" +
        '<div class="micro-rep-wrap"><table class="micro-rep-table micro-rep-table--parts"><thead><tr><th>часть суток</th>' + cols.map((c) => "<th>" + c[0] + "</th>").join("") + "</tr></thead><tbody>" +
        P.map((p) => "<tr><td>" + partIcon(p.key) + p.name + ' <small class="micro-ci">' + partHours(p) + "</small></td>" + cols.map((c) => "<td>" + c[1](p) + "</td>").join("") + "</tr>").join("") +
        "</tbody></table></div></section>";
    }

    // вид «числа»: по дням (по неделям — для длинного периода) и все варки
    function numbersHtml(rep, opt, cols, vis, on) {
      const sk = seedKey(rep);
      const ci = (m, d) => (m.n >= 4 ? ' <small class="micro-ci">' + range(m.q1, m.q3, d) + "</small>" : "");
      const dcols = [["день", (d) => dayLbl(d.day), true], ["варок", (d) => d.boils, true],
        ["мука финиша, %", (d) => bad(finesBad(rep, d.fines_avg.median), fmt(d.fines_avg.median, 1)) + ci(d.fines_avg, 1), on("fines_avg")],
        ["размер финиша, мкм", (d) => bad(sizeBad(rep, d.fin_um.median), fmt(d.fin_um.median, 0)), on("fin_um")],
        ["однородность, %", (d) => fmt(d.fin_cv && d.fin_cv.median, 1), on("fin_cv")], ["сростки, %", (d) => fmt(d.fin_agg_pct && d.fin_agg_pct.median, 1), on("fin_agg_pct")],
        ["от заводки, мин", (d) => fmt(d[sk] && d[sk].median, 0), on("seed")],
        ["длительность", (d) => dur(d.duration_min.median), on("duration_min")], ["с водой", (d) => (d.water_known ? d.water + " из " + d.water_known : "—"), on("water")],
        ["рассев финиша", (d) => sieveBar(d.sieve, dayLbl(d.day) + " · рассев финиша"), on("sieve")]].filter((c) => c[2]);
      let h = '<section class="micro-rep-sec micro-rep-sec--days"><div class="micro-rep-h">По дням <small>медиана по типичным варкам дня, серым — «обычно» (25–75 %)</small></div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr>' +
        dcols.map((c) => "<th>" + c[0] + "</th>").join("") + "</tr></thead><tbody>" + (rep.days || []).map((d) => "<tr>" + dcols.map((c) => "<td>" + c[1](d) + "</td>").join("") + "</tr>").join("") + "</tbody></table></div></section>";
      if ((rep.weeks || []).length > 1 && rep.to - rep.from > 35 * 86400) {
        h += '<section class="micro-rep-sec"><div class="micro-rep-h">По неделям</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>неделя</th><th>варок</th>' + (on("fines_avg") ? "<th>мука финиша, %</th>" : "") +
          (on("fin_um") ? "<th>размер финиша, мкм</th>" : "") + "</tr></thead><tbody>" +
          rep.weeks.map((wk) => "<tr><td>" + wk.week + "</td><td>" + wk.boils + "</td>" + (on("fines_avg") ? "<td>" + fmt(wk.fines_avg.median, 1) + "</td>" : "") + (on("fin_um") ? "<td>" + fmt(wk.fin_um.median, 0) + "</td>" : "") + "</tr>").join("") + "</tbody></table></div></section>";
      }
      // варки: разделитель на каждый день; подряд ≥ 3 варок без муки — одной строкой
      const ncol = vis.length + 1;
      h += '<section class="micro-rep-sec"><div class="micro-rep-h">Варки</div><div class="micro-rep-wrap"><table class="micro-rep-table micro-rep-table--boils"><thead><tr><th>начало</th>' +
        vis.map((c) => "<th" + (c.tip ? ' title="' + esc(c.tip) + '"' : "") + ">" + c.head + "</th>").join("") + "</tr></thead><tbody>";
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
        const anomTag = !opt.print && anomOf(b) ? ' <span class="micro-rep-flag micro-rep-flag--anom" title="' + esc(anomText(b, "\n")) + '">не как обычно</span>' : "";
        h += '<tr class="' + (b.flag ? "is-off" : "") + (b.finished ? "" : " is-open") + '"><td' + link + ">" + hmLbl(b.start) + flag + anomTag + "</td>" +
          vis.map((c) => "<td" + (LINKED.includes(c.key) && !opt.print ? link : "") + ">" + c.cell(b, rep) + "</td>").join("") + "</tr>";
        i++;
      }
      return h + "</tbody></table></div></section>";
    }

    function render(rep) {
      const out = g("repOut");
      if (!rep.boils.length) { out.innerHTML = '<div class="micro-conn-hint">За этот период варок нет (учитываются варки не короче ' + rep.min_probes + " проб).</div>"; return; }
      lastW = out.clientWidth;
      if (tip) tip.hidden = true;                 // точка под курсором исчезнет вместе со старым графиком — mouseout не придёт
      out.innerHTML = reportHtml(rep, { width: Math.max(320, Math.min(1400, lastW || 900)) });
    }

    // PDF: документ в скрытом iframe — в печать не попадает страница приложения (раньше давала 2–3 пустых листа)
    const PRINT_W = 1000;
    function printDoc(name, inner) {
      const old = g("repPrintFrame"); if (old) old.remove();
      const fr = document.createElement("iframe");
      if (titleSaved == null) titleSaved = document.title;       // второй клик до возврата заголовка не запомнит уже подменённый
      fr.id = "repPrintFrame"; fr.setAttribute("aria-hidden", "true"); fr.tabIndex = -1;
      fr.style.cssText = "position:fixed;right:0;bottom:0;width:1px;height:1px;border:0;opacity:0;pointer-events:none";
      // свои поля страницы: внизу название и «стр. N из M»; пустой верх убирает колонтитул браузера (дата и адрес about:srcdoc)
      const box = (t) => '"' + String(t).replace(/["\\<>\u0000-\u001f]/g, "") + '"';
      const page = "<style>@page { @top-center { content: \"\"; } @bottom-left { content: " + box(name) + "; font: 8pt Inter, Arial, sans-serif; color: #777; } " +
        '@bottom-right { content: "стр. " counter(page) " из " counter(pages); font: 8pt Inter, Arial, sans-serif; color: #777; } }</style>';
      const css = (document.querySelector('link[rel="stylesheet"][href*="/static/style.css"]') || {}).href || "/static/style.css";     // с ?v=<версия>, как у страницы: после обновления — свежие стили
      fr.srcdoc = '<!doctype html><html lang="ru" data-theme="light"><head><meta charset="utf-8"><title>' + esc(name) + '</title><link rel="stylesheet" href="' + esc(css) + '">' + page + "</head>" +
        '<body class="micro-rep-doc"><div class="micro-rep-out">' + inner + "</div></body></html>";
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
    function printReport() {
      if (!lastRep || !lastRep.boils.length) return;
      printDoc("Отчёт по варкам" + (lastRep.serial ? " " + lastRep.serial : "") + " " + dmy(lastRep.from).slice(0, 5) + "–" + dmy(lastRep.to), reportHtml(lastRep, { width: PRINT_W, print: true }));
    }

    // одна подсказка на страницу: точки графиков, столбики рассева, полоски, отметки изменений (отчёт и сравнение)
    function attachTip(root) {
      if (!tip) { tip = document.createElement("div"); tip.className = "micro-rep-tip"; tip.hidden = true; document.body.appendChild(tip); }
      const place = (e) => {
        const r = tip.getBoundingClientRect(), x = e.clientX + 14, y = e.clientY + 14;
        tip.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
        tip.style.top = (y + r.height > window.innerHeight - 8 ? e.clientY - r.height - 10 : y) + "px";
      };
      root.addEventListener("mouseover", (e) => { const t = e.target.closest("[data-tip]"); if (!t) return; tip.textContent = t.dataset.tip; tip.hidden = false; place(e); });
      root.addEventListener("mousemove", (e) => { if (!tip.hidden) place(e); });
      root.addEventListener("mouseout", (e) => { const t = e.target.closest("[data-tip]"); if (t && !t.contains(e.relatedTarget)) tip.hidden = true; });
    }
    const hideTip = () => { if (tip) tip.hidden = true; };

    // панель «Показатели» (графики) / «Столбцы» (числа): галочки, применяются сразу (без повторного запроса) и запоминаются; столбцы уходят и в CSV
    function renderColsPanel() {
      const box = g("repColsPanel"); if (!box) return;
      const charts = view === "charts", list = charts ? CHARTS : COLS, key = charts ? CHARTS_KEY : COLS_KEY;
      g("repColsBtn").textContent = charts ? "Показатели" : "Столбцы";
      g("repColsBtn").title = charts ? "Какие графики показывать (запоминается)" : "Какие столбцы показывать в таблице (запоминается, действует и на CSV)";
      box.innerHTML = stateOf(list, key).map((c) => '<label title="' + esc(c.tip || "") + '"><input type="checkbox" data-k="' + c.key + '"' + (c.on ? " checked" : "") + " /> " +
        (charts ? titleOf(c, lastRep) : c.head) + "</label>").join("");
      box.querySelectorAll("input").forEach((i) => i.addEventListener("change", () => {
        const st = stateOf(list, key); st.find((c) => c.key === i.dataset.k).on = i.checked; saveState(st, key);
        if (lastRep && lastRep.boils.length) render(lastRep);
      }));
    }
    const visibleKeys = () => stateOf(COLS, COLS_KEY).filter((c) => c.on).flatMap((c) => c.csv || [c.key]);
    function setView(v) {
      view = v === "numbers" ? "numbers" : "charts";
      try { localStorage.setItem("microRepView", view); } catch (e) { }
      document.querySelectorAll("#repViewSeg button").forEach((x) => x.classList.toggle("is-active", x.dataset.v === view));
      renderColsPanel();
      if (lastRep && lastRep.boils.length) render(lastRep);
    }

    async function generate() {
      const hint = g("repHint"), qq = query();
      if (!qq.from || !qq.to) { hint.textContent = "укажи обе даты"; return; }
      hint.textContent = "считаю…"; g("repGoBtn").disabled = true;
      try {
        const rep = await (await fetch("/api/cv/report?" + qq.q)).json();
        lastRep = rep; lastQ = qq.q;
        render(rep);
        if (anyOthersOn() && (rep.others_available || []).length) loadOthers().then(() => { if (lastRep === rep) render(rep); });
        renderColsPanel();
        lastQ = qq.q; g("repCsvBtn").disabled = false; g("repPdfBtn").disabled = !rep.boils.length; hint.textContent = "";
      } catch (e) { hint.textContent = "не получилось: " + e.message; }
      g("repGoBtn").disabled = false;
    }

    // клик по началу варки, значению муки, точке графика или карточке лучшей/худшей — переход на «Тренды», график растянут на эту варку
    async function openBoil(id, hint) {
      try {
        if (typeof cvTrendSetMode === "function") cvTrendSetMode("crystals");
        const ok = await cvOpenBoil(id);                         // сначала грузим варку; не нашли — остаёмся на месте и говорим об этом
        if (!ok) { if (hint) hint.textContent = "варка " + tsLbl(id) + " не найдена в журнале"; return; }
        if (hint) hint.textContent = "";
        const btn = g("teleTrendBtn"); if (btn) btn.click();      // переключение вкладки перерисует график уже с шириной
      } catch (e) { if (hint) hint.textContent = "не открылось: " + e.message; }
    }

    function init() {
      if (!g("repGoBtn")) return;
      const out = g("repOut");
      out.addEventListener("click", (e) => { const td = e.target.closest("[data-boil]"); if (td) openBoil(td.dataset.boil, g("repHint")); });
      out.addEventListener("change", async (e) => {                 // галочка наложения в шапке графика: запомнить и перерисовать (другие заводы — с одним запросом на период)
        const i = e.target.closest("input[data-over]"); if (!i) return;
        const [ck, ok] = i.dataset.over.split(":");
        over[ck] = { ...(over[ck] || {}), [ok]: i.checked };
        try { localStorage.setItem(OVER_KEY, JSON.stringify(over)); } catch (e2) { }
        if (ok === "others" && i.checked) await loadOthers();
        if (lastRep && lastRep.boils.length) render(lastRep);
      });
      attachTip(out);
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
      document.querySelectorAll("#repViewSeg button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.v)));
      setView(view);
      g("repGoBtn").addEventListener("click", generate);
      try { const f = localStorage.getItem("microRepFinSv"); if (f && parseFloat(f) > 0) g("repFinSv").value = f; } catch (e) { }
      g("repColsBtn").addEventListener("click", () => { g("repColsPanel").hidden = !g("repColsPanel").hidden; g("repColsBtn").classList.toggle("is-on", !g("repColsPanel").hidden); });
      g("repCsvBtn").addEventListener("click", () => { if (lastQ) window.location.href = "/api/cv/report.csv?" + lastQ + "&cols=" + encodeURIComponent(visibleKeys().join(",")); });
      g("repPdfBtn").addEventListener("click", printReport);      // диалог печати → «Сохранить как PDF»
    }
    window.microRep = { fmt, range, esc, tsLbl, hmLbl, dayLbl, dmy, dur, plural, modelName, niceStep, yScale, legend, chartHead, median, quant, attachTip, hideTip, printDoc, openBoil };
    init();
  })();

// Страница микроскопа — вкладка «Отчёт»: отчёт по варкам за период (неделя / месяц / свой), CSV и PDF (печать)
// Данные — /api/cv/report (report.py). Файлы microscope/01..05 делят общую глобальную область.
"use strict";

  (function () {
    const g = (id) => document.getElementById(id);
    let period = "week", lastQ = null, lastRep = null;
    const dstr = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    const fmt = (v, d) => (v == null ? "—" : Number(v).toFixed(d == null ? 1 : d).replace(".", ","));
    const range = (a, b, d) => (a == null ? "—" : fmt(a, d) + "–" + fmt(b, d));
    // ts «2026-10-05_17_21_24» → «05.10 17:21»
    const tsLbl = (ts) => { const m = String(ts || "").match(/^\d{4}-(\d\d)-(\d\d)_(\d\d)_(\d\d)/); return m ? m[2] + "." + m[1] + " " + m[3] + ":" + m[4] : (ts || "—"); };
    const dur = (min) => (min == null ? "—" : Math.floor(min / 60) + " ч " + String(Math.round(min % 60)).padStart(2, "0") + " мин");

    function query() {
      let from, to;
      const today = new Date();
      if (period === "custom") { from = g("repFrom").value; to = g("repTo").value; }
      else { const f = new Date(today); f.setDate(f.getDate() - (period === "week" ? 6 : 29)); from = dstr(f); to = dstr(today); }
      const fin = parseFloat(g("repFinSv").value) > 0 ? parseFloat(g("repFinSv").value) : 2;
      try { localStorage.setItem("microRepFinSv", String(fin)); } catch (e) { }
      return { from, to, q: [typeof cvSerialQ === "function" ? cvSerialQ() : "", "from=" + encodeURIComponent(from), "to=" + encodeURIComponent(to), "fin_sv=" + fin].filter(Boolean).join("&") };
    }

    function card(title, big, sub) { return '<div class="micro-rep-card"><span>' + title + "</span><b>" + big + "</b>" + (sub ? "<small>" + sub + "</small>" : "") + "</div>"; }

    // Столбцы таблицы «Варки»: key совпадает с полем отчёта (и с колонкой CSV); «начало» есть всегда. Выбор столбцов запоминается в браузере.
    const COLS = [
      { key: "duration_min", head: "длительность", cell: (b) => dur(b.duration_min), on: true },
      { key: "probes", head: "проб", cell: (b) => b.probes, on: true },
      { key: "sv", head: "СВ", cell: (b) => range(b.sv_min, b.sv_max, 1), on: true, csv: ["sv_min", "sv_max"] },
      { key: "sv_end", head: "СВ конец", tip: "Конечное СВ варки (медиана последних проб)", cell: (b) => fmt(b.sv_end, 1), on: true },
      { key: "finish_probes", head: "проб финиша", tip: "Сколько проб в пределах «финиш, СВ» от конечного — по ним считается мука и рассев", cell: (b) => b.finish_probes, on: false },
      { key: "fines_avg", head: "мука на финише, %", tip: "Среднее по пробам финиша (последние 1,5–2 СВ)", cell: (b) => fmt(b.fines_avg, 2), on: true },
      { key: "fines_range", head: "мука мин–макс", tip: "По пробам финиша", cell: (b) => range(b.fines_min, b.fines_max, 2), on: true, csv: ["fines_min", "fines_max"] },
      { key: "fines_all", head: "мука по всей варке, %", tip: "Среднее по всем пробам после порога «Мука с СВ» (справочно: включает начало варки)", cell: (b) => fmt(b.fines_all, 2), on: false },
      { key: "reject_pct", head: "брак, %", cell: (b) => fmt(b.reject_pct, 1), on: false },
      { key: "frac_zones", head: "разломов", cell: (b) => b.frac_zones, on: false },
      { key: "mean_um", head: "размер, мкм", tip: "Средний размер кристалла", cell: (b) => fmt(b.mean_um, 0), on: true },
    ];
    const COLS_KEY = "microRepCols";
    function colState() {
      let saved = null; try { saved = JSON.parse(localStorage.getItem(COLS_KEY) || "null"); } catch (e) { }
      return COLS.map((c) => ({ ...c, on: saved && c.key in saved ? !!saved[c.key] : c.on }));
    }
    function saveCols(state) { try { localStorage.setItem(COLS_KEY, JSON.stringify(Object.fromEntries(state.map((c) => [c.key, c.on])))); } catch (e) { } }
    const colOn = (state, key) => { const c = state.find((x) => x.key === key); return !c || c.on; };

    function render(rep, from, to) {
      const t = rep.totals, out = g("repOut"), cols = colState(), vis = cols.filter((c) => c.on);
      if (!rep.boils.length) { out.innerHTML = '<div class="micro-conn-hint">За этот период варок нет (учитываются варки не короче ' + rep.min_probes + " проб).</div>"; return; }
      const mm = (m, d) => (m && m.avg != null ? "среднее " + fmt(m.avg, d) + " · " + fmt(m.min, d) + "–" + fmt(m.max, d) : "");
      let h = '<div class="micro-rep-title">Отчёт по варкам · ' + from.split("-").reverse().join(".") + " — " + to.split("-").reverse().join(".") + "</div>";
      h += '<div class="micro-rep-cards">' +
        card("Варок", t.boils, "проб " + t.probes) +
        (colOn(cols, "fines_avg") ? card("Мука на финише, %", fmt(t.fines_avg.avg, 2), mm(t.fines_avg, 2)) : "") +
        (colOn(cols, "reject_pct") ? card("Брак, %", fmt(t.reject_pct.avg, 1), mm(t.reject_pct, 1)) : "") +
        (colOn(cols, "duration_min") ? card("Длительность", dur(t.duration_min.avg), mm(t.duration_min, 0) ? "мин " + fmt(t.duration_min.min, 0) + " · макс " + fmt(t.duration_min.max, 0) + " мин" : "") : "") +
        (colOn(cols, "mean_um") ? card("Средний размер, мкм", fmt(t.mean_um.avg, 0), mm(t.mean_um, 0)) : "") +
        (colOn(cols, "frac_zones") ? card("Разломы", t.frac_zones, "варок с разломом: " + t.frac_boils) : "") +
        (colOn(cols, "fines_avg") && t.best ? card("Лучшая варка", fmt(t.best.fines_avg, 2) + " %", tsLbl(t.best.start) + " · муки меньше всего") : "") +
        (colOn(cols, "fines_avg") && t.worst ? card("Худшая варка", fmt(t.worst.fines_avg, 2) + " %", tsLbl(t.worst.start) + " · муки больше всего") : "") + "</div>";
      h += '<div class="micro-rep-h">Варки</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>начало</th>' +
        vis.map((c) => "<th" + (c.tip ? ' title="' + c.tip + '"' : "") + ">" + c.head + "</th>").join("") + "</tr></thead><tbody>";
      rep.boils.forEach((b) => {
        h += '<tr class="' + (b.finished ? "" : "is-open") + '"><td>' + tsLbl(b.start) + (b.finished ? "" : " (идёт)") + "</td>" + vis.map((c) => "<td>" + c.cell(b) + "</td>").join("") + "</tr>";
      });
      h += "</tbody></table></div>";
      if (colOn(cols, "fines_avg")) {
        h += '<div class="micro-rep-h">Рассев по ситам, % (пробы финиша)</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>варка</th>' +
          rep.sieve_labels.map((s) => "<th>" + s + "</th>").join("") + "</tr></thead><tbody>";
        rep.boils.forEach((b) => { h += "<tr><td>" + tsLbl(b.start) + "</td>" + b.sieve.map((v) => "<td>" + fmt(v, 1) + "</td>").join("") + "</tr>"; });
        h += "</tbody></table></div>";
      }
      if (rep.substages.length && colOn(cols, "fines_avg")) {
        h += '<div class="micro-rep-h">По подстадиям</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>подстадия</th><th>проб</th><th>время, мин</th><th>мука, % среднее</th><th>мин–макс</th></tr></thead><tbody>' +
          rep.substages.map((s) => "<tr><td>" + s.group + "</td><td>" + s.probes + "</td><td>" + fmt(s.minutes, 0) + "</td><td>" + fmt(s.fines_avg, 2) + "</td><td>" + range(s.fines_min, s.fines_max, 2) + "</td></tr>").join("") + "</tbody></table></div>";
      }
      if (rep.weeks.length > 1) {
        h += '<div class="micro-rep-h">По неделям</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>неделя</th><th>варок</th>' + (colOn(cols, "fines_avg") ? "<th>мука на финише, % среднее</th>" : "") +
          (colOn(cols, "reject_pct") ? "<th>брак, % среднее</th>" : "") + "</tr></thead><tbody>" +
          rep.weeks.map((w) => "<tr><td>" + w.week + "</td><td>" + w.boils + "</td>" + (colOn(cols, "fines_avg") ? "<td>" + fmt(w.fines_avg.avg, 2) + "</td>" : "") +
            (colOn(cols, "reject_pct") ? "<td>" + fmt(w.reject_pct.avg, 1) + "</td>" : "") + "</tr>").join("") + "</tbody></table></div>";
      }
      h += '<div class="micro-conn-hint" style="margin-top:10px">Мука варки — на финише: среднее по пробам, где СВ в пределах «финиш, СВ» (' + fmt(rep.fin_sv, 1) + ") от конечного, и только там, где СВ уже достигло «Мука с СВ» (" + fmt(rep.fines_from_sv, 1) + "). Варка входит в период по времени начала.</div>";
      out.innerHTML = h;
    }

    // панель «Столбцы»: галочки по столбцам таблицы «Варки»; применяется сразу (без повторного запроса), запоминается, уходит и в CSV
    function renderColsPanel() {
      const box = g("repColsPanel"); if (!box) return;
      box.innerHTML = colState().map((c) => '<label title="' + (c.tip || "") + '"><input type="checkbox" data-k="' + c.key + '"' + (c.on ? " checked" : "") + " /> " + c.head + "</label>").join("");
      box.querySelectorAll("input").forEach((i) => i.addEventListener("change", () => {
        const st = colState(); st.find((c) => c.key === i.dataset.k).on = i.checked; saveCols(st);
        if (lastRep) render(lastRep, lastRep.__from, lastRep.__to);
      }));
    }
    const visibleKeys = () => colState().filter((c) => c.on).flatMap((c) => c.csv || [c.key]);

    async function generate() {
      const hint = g("repHint"), qq = query();
      if (!qq.from || !qq.to) { hint.textContent = "укажи обе даты"; return; }
      hint.textContent = "считаю…"; g("repGoBtn").disabled = true;
      try {
        const rep = await (await fetch("/api/cv/report?" + qq.q)).json();
        rep.__from = qq.from; rep.__to = qq.to; lastRep = rep;
        render(rep, qq.from, qq.to);
        lastQ = qq.q; g("repCsvBtn").disabled = false; g("repPdfBtn").disabled = false; hint.textContent = "";
      } catch (e) { hint.textContent = "не получилось: " + e.message; }
      g("repGoBtn").disabled = false;
    }

    function init() {
      if (!g("repGoBtn")) return;
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
      g("repPdfBtn").addEventListener("click", () => window.print());     // диалог печати → «Сохранить как PDF»
    }
    init();
  })();

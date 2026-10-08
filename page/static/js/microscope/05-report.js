// Страница микроскопа — вкладка «Отчёт»: отчёт по варкам за период (неделя / месяц / свой), CSV и PDF (печать)
// Данные — /api/cv/report (report.py). Файлы microscope/01..05 делят общую глобальную область.
"use strict";

  (function () {
    const g = (id) => document.getElementById(id);
    let period = "week", lastQ = null;
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
      return { from, to, q: [typeof cvSerialQ === "function" ? cvSerialQ() : "", "from=" + encodeURIComponent(from), "to=" + encodeURIComponent(to)].filter(Boolean).join("&") };
    }

    function card(title, big, sub) { return '<div class="micro-rep-card"><span>' + title + "</span><b>" + big + "</b>" + (sub ? "<small>" + sub + "</small>" : "") + "</div>"; }

    function render(rep, from, to) {
      const t = rep.totals, out = g("repOut");
      if (!rep.boils.length) { out.innerHTML = '<div class="micro-conn-hint">За этот период варок нет (учитываются варки не короче ' + rep.min_probes + " проб).</div>"; return; }
      const mm = (m, d) => (m && m.avg != null ? "среднее " + fmt(m.avg, d) + " · " + fmt(m.min, d) + "–" + fmt(m.max, d) : "");
      let h = '<div class="micro-rep-title">Отчёт по варкам · ' + from.split("-").reverse().join(".") + " — " + to.split("-").reverse().join(".") + "</div>";
      h += '<div class="micro-rep-cards">' +
        card("Варок", t.boils, "проб " + t.probes) +
        card("Мука, %", fmt(t.fines_avg.avg, 2), mm(t.fines_avg, 2)) +
        card("Брак, %", fmt(t.reject_pct.avg, 1), mm(t.reject_pct, 1)) +
        card("Длительность", dur(t.duration_min.avg), mm(t.duration_min, 0) ? "мин " + fmt(t.duration_min.min, 0) + " · макс " + fmt(t.duration_min.max, 0) + " мин" : "") +
        card("Средний размер, мкм", fmt(t.mean_um.avg, 0), mm(t.mean_um, 0)) +
        card("Разломы", t.frac_zones, "варок с разломом: " + t.frac_boils) +
        (t.best ? card("Лучшая варка", fmt(t.best.fines_avg, 2) + " %", tsLbl(t.best.start) + " · муки меньше всего") : "") +
        (t.worst ? card("Худшая варка", fmt(t.worst.fines_avg, 2) + " %", tsLbl(t.worst.start) + " · муки больше всего") : "") + "</div>";
      h += '<div class="micro-rep-h">Варки</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>начало</th><th>длительность</th><th>проб</th><th>СВ</th>' +
        "<th>мука, %</th><th>мука мин–макс</th><th>брак, %</th><th>разломов</th><th>размер, мкм</th></tr></thead><tbody>";
      rep.boils.forEach((b) => {
        h += '<tr class="' + (b.finished ? "" : "is-open") + '"><td>' + tsLbl(b.start) + (b.finished ? "" : " (идёт)") + "</td><td>" + dur(b.duration_min) + "</td><td>" + b.probes + "</td><td>" + range(b.sv_min, b.sv_max, 1) +
          "</td><td>" + fmt(b.fines_avg, 2) + "</td><td>" + range(b.fines_min, b.fines_max, 2) + "</td><td>" + fmt(b.reject_pct, 1) + "</td><td>" + b.frac_zones + "</td><td>" + fmt(b.mean_um, 0) + "</td></tr>";
      });
      h += "</tbody></table></div>";
      h += '<div class="micro-rep-h">Рассев по ситам, % (среднее по пробам финиша)</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>варка</th>' +
        rep.sieve_labels.map((s) => "<th>" + s + "</th>").join("") + "</tr></thead><tbody>";
      rep.boils.forEach((b) => { h += "<tr><td>" + tsLbl(b.start) + "</td>" + b.sieve.map((v) => "<td>" + fmt(v, 1) + "</td>").join("") + "</tr>"; });
      h += "</tbody></table></div>";
      if (rep.substages.length) {
        h += '<div class="micro-rep-h">По подстадиям</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>подстадия</th><th>проб</th><th>время, мин</th><th>мука, % среднее</th><th>мин–макс</th></tr></thead><tbody>' +
          rep.substages.map((s) => "<tr><td>" + s.group + "</td><td>" + s.probes + "</td><td>" + fmt(s.minutes, 0) + "</td><td>" + fmt(s.fines_avg, 2) + "</td><td>" + range(s.fines_min, s.fines_max, 2) + "</td></tr>").join("") + "</tbody></table></div>";
      }
      if (rep.weeks.length > 1) {
        h += '<div class="micro-rep-h">По неделям</div><div class="micro-rep-wrap"><table class="micro-rep-table"><thead><tr><th>неделя</th><th>варок</th><th>мука, % среднее</th><th>брак, % среднее</th></tr></thead><tbody>' +
          rep.weeks.map((w) => "<tr><td>" + w.week + "</td><td>" + w.boils + "</td><td>" + fmt(w.fines_avg.avg, 2) + "</td><td>" + fmt(w.reject_pct.avg, 1) + "</td></tr>").join("") + "</tbody></table></div>";
      }
      h += '<div class="micro-conn-hint" style="margin-top:10px">Мука считается с первого достижения «Мука с СВ» (' + fmt(rep.fines_from_sv, 1) + ") до конца варки; варка входит в период по времени начала.</div>";
      out.innerHTML = h;
    }

    async function generate() {
      const hint = g("repHint"), qq = query();
      if (!qq.from || !qq.to) { hint.textContent = "укажи обе даты"; return; }
      hint.textContent = "считаю…"; g("repGoBtn").disabled = true;
      try {
        const rep = await (await fetch("/api/cv/report?" + qq.q)).json();
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
      g("repCsvBtn").addEventListener("click", () => { if (lastQ) window.location.href = "/api/cv/report.csv?" + lastQ; });
      g("repPdfBtn").addEventListener("click", () => window.print());     // диалог печати → «Сохранить как PDF»
    }
    init();
  })();

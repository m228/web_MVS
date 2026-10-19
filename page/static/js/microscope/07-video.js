// Страница микроскопа — вкладка «Ролик»: ролик варки через видео-сервис (video_service/, порт 8767).
// Вкладка видна, только пока сервис отвечает (/api/video/health раз в 5 с). Сборка идёт в сервисе; тут — список варок,
// прогресс и готовые ролики. Файлы microscope/01..07 делят общую глобальную область (api, $, sentCmd — из 01-core.js).
"use strict";

  let vidOnline = false, vidJob = null, vidPollT = null, vidLoadedAt = 0;
  const VID_REFRESH_MS = 30000;            // список варок на открытой вкладке — раз в 30 с (журнал читается целиком)
  const vidP2 = (n) => String(n).padStart(2, "0");
  const vidDate = (t) => { const d = new Date(t * 1000); return vidP2(d.getDate()) + "." + vidP2(d.getMonth() + 1); };
  const vidTime = (t) => { const d = new Date(t * 1000); return vidP2(d.getHours()) + ":" + vidP2(d.getMinutes()); };
  const vidDur = (s) => { const m = Math.round(s / 60); return m < 60 ? m + " мин" : Math.floor(m / 60) + " ч" + (m % 60 ? " " + (m % 60) + " мин" : ""); };
  const vidSv = (v) => (v == null ? "—" : String(Math.round(v * 10) / 10).replace(".", ","));
  const vidEsc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const vidProbes = (n) => { const m10 = n % 10, m100 = n % 100; return n + ((m10 === 1 && m100 !== 11) ? " проба" : (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) ? " пробы" : " проб"); };
  const vidVisible = () => { const p = $("teleStripVideo"); return !!p && !p.hidden; };

  async function vidHealth() {
    let h = null;
    try { h = await api("/api/video/health"); } catch (e) { }
    const on = !!(h && h.online);
    if (on !== vidOnline) {
      vidOnline = on;
      const btn = $("teleVideoBtn"), seg = document.querySelector(".micro-tele-seg");
      if (btn) btn.hidden = !on;
      if (seg) seg.classList.toggle("has-video", on);
      if (!on && btn && btn.classList.contains("is-active")) $("telePlateBtn").click();   // сервис остановили, а вкладка открыта — на «Плату»
    }
    const job = on && h.service && h.service.job;          // сборку запустили до перезагрузки страницы — подхватываем прогресс
    if (job && job.id !== vidJob) { vidJob = job.id; vidPoll(); }
    if (on && vidVisible() && Date.now() - vidLoadedAt > VID_REFRESH_MS) vidRefresh();
  }

  function vidMsg(t, bad) {
    const e = $("vidMsg"); if (!e) return;
    e.textContent = t || "";
    e.classList.toggle("is-bad", !!bad);
  }

  async function vidRefresh() {
    if (!vidOnline) return;
    vidLoadedAt = Date.now();
    try { const r = await api("/api/video/boils"); vidRenderBoils(r.boils || []); } catch (e) { vidMsg("варки не загрузились: " + e.message, true); }
    try { const f = await api("/api/video/files"); vidRenderFiles(f.files || [], f.error); } catch (e) { }
  }

  function vidRenderBoils(list) {
    const box = $("vidBoils"); if (!box) return;
    if (!list.length) { box.innerHTML = '<div class="micro-vid-empty">Варок в журнале пока нет.</div>'; return; }
    box.innerHTML = list.map((b) => {
      const gone = !b.on_disk, part = b.on_disk && b.on_disk < b.n;
      const meta = [vidDur(b.t_to - b.t_from), "СВ " + vidSv(b.sv_min) + "–" + vidSv(b.sv_max), vidProbes(b.n) + (part ? ", с кадрами " + b.on_disk : "")].join(" · ");
      return '<div class="micro-vid-row' + (gone ? " is-gone" : "") + '"><div class="micro-vid-main"><b>' +
        vidDate(b.t_from) + " · " + vidTime(b.t_from) + "–" + vidTime(b.t_to) + (b.finished ? "" : ' <span class="micro-vid-tag">идёт</span>') +
        "</b><span>" + meta + "</span></div>" +
        (gone ? '<span class="micro-vid-note" title="Кадры этой варки стёрты ротацией архива проб (поле «Варок в архиве» на вкладке «CV»)">кадры стёрты</span>'
          : '<button type="button" class="toolbar-btn toolbar-btn--neutral" data-boil="' + vidEsc(b.id) + '"' + (vidJob ? " disabled" : "") + ">Собрать ролик</button>") +
        "</div>";
    }).join("");
    box.querySelectorAll("button[data-boil]").forEach((bt) => bt.addEventListener("click", () => vidBuild(bt.dataset.boil)));
  }

  function vidRenderFiles(files, err) {
    const box = $("vidFiles"); if (!box) return;
    if (err) { box.innerHTML = '<div class="micro-vid-empty">' + vidEsc(err) + "</div>"; return; }
    if (!files.length) { box.innerHTML = '<div class="micro-vid-empty">Пока нет — собери ролик по варке слева.</div>'; return; }
    box.innerHTML = files.map((f) => '<div class="micro-vid-row"><div class="micro-vid-main"><b title="' + vidEsc(f.name) + '">' + vidEsc(f.name) +
      "</b><span>" + (f.size / 1048576).toFixed(1).replace(".", ",") + " МБ · " + vidDate(f.mtime) + " " + vidTime(f.mtime) + "</span></div>" +
      '<a class="toolbar-btn toolbar-btn--neutral" href="/api/video/file?name=' + encodeURIComponent(f.name) + '" download>Скачать</a></div>').join("");
  }

  async function vidBuild(id) {
    vidMsg("");
    let r = null;
    try { r = await api("/api/video/build", { boil: id }); } catch (e) { vidMsg("не запустилось: " + e.message, true); return; }
    if (!r || r.error || !r.id) { vidMsg((r && r.error) || "не запустилось", true); return; }
    vidJob = r.id;
    sentCmd("Ролик варки: сборка запущена");
    vidRefresh();
    vidPoll();
  }

  async function vidPoll() {
    clearTimeout(vidPollT);
    if (!vidJob) return;
    let j = null;
    try { j = await api("/api/video/job", { id: vidJob }); } catch (e) { }
    const box = $("vidProg"), bar = $("vidBar"), txt = $("vidProgTxt");
    if (!j || !j.state) {                                   // сервис перезапустили — этой сборки больше нет
      vidJob = null; if (box) box.hidden = true; vidRefresh(); return;
    }
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    if (box) box.hidden = false;
    if (j.state === "done") {
      if (bar) bar.style.width = "100%";
      if (txt) txt.textContent = "готово";
      vidMsg("Ролик готов: " + j.file);
      vidJob = null; vidRefresh();
      setTimeout(() => { if (!vidJob && box) box.hidden = true; }, 4000);
      return;
    }
    if (j.state === "error") {
      vidMsg("Ролик не собрался: " + (j.error || "ошибка"), true);
      vidJob = null; if (box) box.hidden = true; vidRefresh();
      return;
    }
    if (bar) bar.style.width = pct + "%";
    if (txt) txt.textContent = "собирается: " + pct + " %" + (j.probes ? " · " + vidProbes(j.probes) : "");
    vidPollT = setTimeout(vidPoll, 1000);
  }

  function wireVideo() {
    const btn = $("teleVideoBtn"); if (!btn) return;
    btn.addEventListener("click", vidRefresh);
    vidHealth();
    setInterval(vidHealth, 5000);
  }

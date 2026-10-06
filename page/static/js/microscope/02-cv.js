// Страница микроскопа — компьютерное зрение (CV): рассев, оверлей, лента проб, тренд
// Часть бывшего microscope.js (IIFE снят: файлы microscope/01..04 делят общую глобальную область, порядок <script> важен).
"use strict";

  // ================= Компьютерное зрение (CV) =================
  // Вкладка «CV» + переключатель окна камера/распознавание. Данные с /api/cv/*.
  const CV_GROUPS = ["small", "medium", "large", "reject"];
  const CV_SERIES_COLOR = { small: "#1d9e75", medium: "#378add", large: "#ba7517", reject: "#e24b4a", mean: "#7f77dd", median: "#d16fb8", sv: "#f2c94c",
    temp: "#ff8a4c", vac: "#4fd1e8", level: "#8aa4c8", current: "#a3d95b", cook_time: "#b9a98f", seed_age: "#ffb347" };
  let cvWinOn = false;          // окно показывает CV (true) или камеру (false)
  let cvLastResult = null;      // последняя проба (result.json)
  let cvPrevResult = null;      // предыдущая проба (для Δ к прошлой)
  let cvSamples = [];           // лента проб (новые сверху): [{ts, stage, summary, frames, fracture}]
  let cvView = null;            // проба, показанная в окне CV (по умолчанию — последняя)
  let cvViewPinned = false;     // выбрана старая проба из ленты — окно не прыгает на свежую
  let cvGalIdx = 0;             // индекс кадра показанной пробы
  let cvCurObjects = [];        // объекты текущего кадра окна CV (для наведения: bbox/size/area)
  let cvTrendData = null;       // {from, to, t[], ts[], stage[], series} — загруженный участок журнала
  let cvTrendView = null;       // {start, end} видимое окно, epoch-секунды
  let cvTrendHover = null;       // индекс пробы под линией-курсором тренда
  let cvTrendGeom = null;        // геометрия последней отрисовки тренда (для привязки курсора)
  let cvLastModel = null;        // имя детектора из /api/cv/health (для телеметрии)

  function cvSerialQ() { return camSerial ? "serial=" + encodeURIComponent(camSerial) : ""; }

  // --- переключатель окна ---
  function applyWinMode() {
    const seg = document.querySelector(".micro-win-seg");
    if (seg) seg.classList.toggle("cv", cvWinOn);
    const card = document.querySelector(".micro-cam-card");
    if (card) card.classList.toggle("cv-win", cvWinOn);   // CSS прячет камеру/плейсхолдер в режиме CV
    const camBtn = $("winCamBtn"), cvBtn = $("winCvBtn");
    if (camBtn) camBtn.classList.toggle("is-active", !cvWinOn);
    if (cvBtn) cvBtn.classList.toggle("is-active", cvWinOn);
    const stream = $("microCamStream"), ov = $("cvOverlay"), ph = $("camPlaceholder"), ovph = $("cvOverlayPh");
    if (cvWinOn) {
      const hasOv = ov && ov.getAttribute("src");
      if (ov) ov.hidden = !hasOv;
      if (ovph) ovph.hidden = !!hasOv;
    } else {
      if (ov) ov.hidden = true;
      if (ovph) ovph.hidden = true;
      // вернуть камеру: показать стрим, если он запущен, иначе плейсхолдер
      if (stream && stream.getAttribute("src")) { stream.hidden = false; if (ph) ph.hidden = true; }
      else if (ph) ph.hidden = false;
    }
    cvDrawOverlay();     // холст с контурами следует за картинкой (и прячется вместе с ней)
  }
  function wireWindow() {
    const camBtn = $("winCamBtn"), cvBtn = $("winCvBtn");
    if (camBtn) camBtn.addEventListener("click", () => { cvWinOn = false; applyWinMode(); });
    if (cvBtn) cvBtn.addEventListener("click", () => { cvWinOn = true; applyWinMode(); cvRefresh(); });
  }

  // --- настройки CV ---
  function cvFillSettings(cv) {
    if (!cv) return;
    const g = cv.groups || {}, sh = cv.shape || {};
    const set = (id, v) => { const e = $(id); if (e != null && v != null) e.value = v; };
    if ($("cvEnable")) $("cvEnable").checked = !!cv.enabled;
    set("cvSmallMax", g.small_max_um); set("cvMediumMax", g.medium_max_um);
    set("cvUmPerPx", cv.um_per_px); set("cvTiles", cv.tiles);
    set("cvMinCirc", sh.min_circularity); set("cvMinSol", sh.min_solidity);
    set("cvMaxAspect", sh.max_aspect); set("cvConf", cv.conf);
    set("cvSuspect", sh.suspect_aspect); set("cvRejectSv", cv.reject_from_sv);
    set("cvClusterGap", cv.cluster_gap_px); set("cvEdgeMargin", cv.edge_margin_px);
    if ($("cvSeamMerge")) $("cvSeamMerge").checked = cv.seam_merge !== false;
    if ($("cvSeamRefine")) $("cvSeamRefine").checked = cv.seam_refine !== false;
    if ($("cvBubble")) $("cvBubble").checked = cv.bubble_filter !== false;
    if ($("cvRejectAlways")) $("cvRejectAlways").checked = !!cv.reject_always;
    const vo = cv.volume || {};
    set("cvFinesUm", vo.fines_um); set("cvKThick", vo.k_thick);   // поля блока «Объём и мелочь»
    set("pcCvFrames", cv.frames_per_probe); set("pcCvGap", cv.gap_sec);   // поля на вкладке «Цикл»
    updateTriggerFields();
  }
  function cvCollectPatch() {
    const num = (id) => { const e = $(id); return e && e.value !== "" ? parseFloat(e.value) : undefined; };
    return {
      groups: { small_max_um: num("cvSmallMax"), medium_max_um: num("cvMediumMax") },
      shape: { min_circularity: num("cvMinCirc"), min_solidity: num("cvMinSol"), max_aspect: num("cvMaxAspect"), suspect_aspect: num("cvSuspect") },
      um_per_px: num("cvUmPerPx"), tiles: num("cvTiles"), conf: num("cvConf"),
      reject_from_sv: num("cvRejectSv"), cluster_gap_px: num("cvClusterGap"), edge_margin_px: num("cvEdgeMargin"),
      seam_merge: $("cvSeamMerge") ? $("cvSeamMerge").checked : undefined,
      seam_refine: $("cvSeamRefine") ? $("cvSeamRefine").checked : undefined,
      bubble_filter: $("cvBubble") ? $("cvBubble").checked : undefined, reject_always: $("cvRejectAlways") ? $("cvRejectAlways").checked : undefined,
    };
  }
  async function cvPostSettings(patch) {
    try {
      await fetch("/api/cv/settings", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
      });
    } catch (e) { /* CV необязателен */ }
  }

  function cvSetStatus(text, cls) {
    ["cvStatus", "cvStatus2"].forEach((id) => {
      const el = $(id); if (el) { el.textContent = text; el.className = "micro-cv-status" + (cls ? " " + cls : ""); }
    });
  }
  // статус сервиса + статус последнего разбора (чтобы отказ был виден, а не молчал).
  // Возвращает состояние разбора (running/ok/…): по нему кнопка «Разобрать пробу» ждёт конца.
  const CV_FAIL = ["no_camera", "no_frames", "sidecar_offline", "error"];
  // версия CV-сервиса внизу шапки: номер релиза, «старый» (сервис без версии — нужен UpdaterCV), «нет связи»
  function setVerCv(text, cls, title) {
    const e = $("verCv"); if (!e) return;
    e.textContent = text; e.className = "micro-ver" + (cls ? " " + cls : "");
    if (title) e.title = title;
  }
  async function cvHealth() {
    try {
      const h = await api("/api/cv/health");
      if (!h.enabled) setVerCv("CV сервис: выключен", "");
      else if (!h.online) setVerCv("CV сервис: нет связи", "is-off");
      else if (h.service && h.service.version) setVerCv("CV сервис v" + h.service.version, "is-ok");
      else setVerCv("CV сервис: старый (без версии)", "is-warn", "Сервис не отдаёт версию — это сборка до 1.7.24. Нарезка на нём прежняя (швы режут кристаллы). Запусти UpdaterCV.bat и перезапусти cv_service\\run.bat");
      const a = h.analysis || {};
      const tail = a.message ? " · " + a.message + (a.ts && a.state !== "running" ? " (" + a.ts + ")" : "") : "";
      const failed = CV_FAIL.includes(a.state);
      if (!h.enabled) { cvSetStatus("сервис: выключен" + tail, "off"); return a.state; }
      if (h.online) {
        const d = (h.service && h.service.detector) || {};
        cvLastModel = d.name || null;
        cvSetStatus("сервис: онлайн · " + (d.name || "?") + tail, failed ? "off" : "ok");
      } else { cvSetStatus("сервис: НЕ отвечает (" + (h.service_url || "") + ")" + tail, "off"); }
      return a.state;
    } catch (e) { cvSetStatus("сервис: —", ""); return null; }
  }

  // переключатель нижней секции: телеметрия платы ↔ распознавание
  function wireTeleToggle() {
    const seg = document.querySelector(".micro-tele-seg");
    const btns = { plate: $("telePlateBtn"), cv: $("teleCvBtn"), trend: $("teleTrendBtn") };
    const panes = { plate: $("teleStripPlate"), cv: $("teleStripCv"), trend: $("teleStripTrend") };
    if (!btns.plate || !btns.cv) return;
    function set(mode) {
      if (seg) { seg.classList.toggle("cv", mode === "cv"); seg.classList.toggle("trend", mode === "trend"); }
      Object.keys(btns).forEach((k) => {
        if (btns[k]) btns[k].classList.toggle("is-active", k === mode);
        if (panes[k]) panes[k].hidden = k !== mode;
      });
      if (mode !== "plate") cvRefresh();
      if (mode === "trend") setTimeout(cvDrawTrend, 30);     // canvas берёт ширину у показанной вкладки
    }
    Object.keys(btns).forEach((k) => { if (btns[k]) btns[k].addEventListener("click", () => set(k)); });
  }
  function isTeleCvVisible() { return ["teleStripCv", "teleStripTrend"].some((id) => { const e = $(id); return e && !e.hidden; }); }

  // --- рассев + статистика ---
  function cvRenderScatter() {
    const r = cvView || cvLastResult, s = r && r.summary;   // рассев — по пробе, показанной в окне
    const cnt = $("cvCount");
    // телеметрия распознавания (тайминги сайдкара + модель)
    const setTe = (id, v) => { const e = $(id); if (e) e.textContent = v; };
    const tm = (r && r.timing) || {};
    setTe("cvTimeTotal", tm.total_ms != null ? (Math.round(tm.total_ms) + " мс") : "—");
    setTe("cvTilesN", tm.tiles != null ? (tm.tiles + (tm.grid ? " (" + tm.grid + ")" : "")) : "—");
    setTe("cvInferNms", tm.infer_ms != null ? (Math.round(tm.infer_ms) + " / " + Math.round(tm.nms_ms || 0) + " мс") : "—");
    setTe("cvCountTele", s ? Math.round(s.count || 0) : "—");
    setTe("cvFramesN", r && r.frames ? r.frames.length : "—");
    setTe("cvModel", (s && s.model) || (cvLastModel || "—"));
    if (!s) { if (cnt) cnt.textContent = "нет пробы"; return; }
    if (cnt) cnt.textContent = "N=" + Math.round(s.count || 0);
    const groups = s.groups || {}, pct = s.groups_pct || {};
    const total = CV_GROUPS.reduce((a, g) => a + (groups[g] || 0), 0) || 1;
    document.querySelectorAll("#cvBars .micro-cv-row").forEach((row) => {
      const g = row.dataset.g;
      const i = row.querySelector("i"), b = row.querySelector("b"), em = row.querySelector("em");
      const v = Math.round(groups[g] || 0), p = pct[g] != null ? pct[g] : Math.round(100 * v / total);
      if (i) i.style.width = Math.max(0, Math.min(100, (100 * v / total))) + "%";
      if (b) b.textContent = v;
      if (em) em.textContent = p + "%";
    });
    cvRenderReasons(s, r && r.sv);
    cvRenderVolume(s);
    const sz = s.size_um || {};
    const setT = (id, v) => { const e = $(id); if (e) e.textContent = v; };
    setT("cvMean", sz.mean != null ? sz.mean + " мкм" : "—");
    setT("cvDensity", s.density_per_mm2 != null ? s.density_per_mm2 + " /мм²" : "—");
    setT("cvCvpct", sz.cv_pct != null ? sz.cv_pct + " %" : "—");
    setT("cvQuality", s.quality === "low" ? "низкое" : (s.quality || "—"));
    // Δ к прошлой пробе по среднему размеру
    const prevMean = !cvViewPinned && cvPrevResult && cvPrevResult.summary && cvPrevResult.summary.size_um
      ? cvPrevResult.summary.size_um.mean : null;
    const dEl = $("cvDelta");
    if (dEl) {
      if (prevMean != null && sz.mean != null) {
        const d = Math.round((sz.mean - prevMean) * 10) / 10;
        dEl.textContent = (d > 0 ? "+" : "") + d + " мкм";
        dEl.style.color = d > 0 ? "var(--success)" : (d < 0 ? "var(--danger)" : "");
      } else { dEl.textContent = "—"; dEl.style.color = ""; }
    }
  }

  // --- объём и мелочь: M1/M2/M3 + площадь, % от общего объёма кадров пробы ---
  const fmtPct = (v) => (v == null ? "—" : (v >= 10 ? v.toFixed(1) : v.toFixed(2)) + " %");
  function cvRenderVolume(s) {
    const vp = (s && s.volume_pct) || {}, cfg = (s && s.volume_cfg) || {};
    document.querySelectorAll("#cvVolGrid [data-v]").forEach((el) => {
      const [model, kind] = el.dataset.v.split(".");
      el.textContent = fmtPct(vp[model] ? vp[model][kind] : null);
    });
    const grid = $("cvVolGrid"); if (grid) grid.classList.toggle("is-idle", !(s && s.reject_active));
    const st = $("cvVolState");
    if (st) st.textContent = s && s.reject_active ? "" : "пока не готов";
    const lbl = $("cvVolLblFines"); if (lbl) lbl.textContent = "Мелочь <" + (cfg.fines_um != null ? cfg.fines_um : $("cvFinesUm") ? $("cvFinesUm").value : "");
    const n = $("cvFinesN"), pn = vp.n && vp.n.fines;
    if (n) n.textContent = "по числу " + (pn == null ? "—" : pn + " %");
  }
  function wireVolumeFields() {
    ["cvFinesUm", "cvKThick"].forEach((id) => {
      const e = $(id); if (!e) return;
      e.addEventListener("change", () => {
        if (!cvSettingsLoaded) return;
        const fu = parseFloat(($("cvFinesUm") || {}).value), k = parseFloat(($("cvKThick") || {}).value);
        if (!(fu > 0) || !(k > 0)) return;
        cvPostSettings({ volume: { fines_um: fu, k_thick: k } });
      });
    });
  }

  // --- оверлей поверх чистого кадра: слои по группам, подсветка формы кристалла под мышкой ---
  const CV_GROUP_NAMES = { small: "малая", medium: "средняя", large: "большая", reject: "брак", suspect: "вытянутый (не брак)", cut: "обрезан краем кадра или швом нарезки — не в рассеве", bubble: "пузырь воздуха (ровный круг) — не кристалл, не в рассеве" };
  const CV_GROUP_COLOR = { small: "#1d9e75", medium: "#378add", large: "#ba7517", reject: "#e24b4a", suspect: "#6ad1f5", cut: "#9aa3ad", bubble: "#e0b04a" };
  const CV_LAYERS_KEY = "microCvLayers";
  let cvLayers = { small: true, medium: true, large: true, reject: true, suspect: true, cut: true, bubble: true, sizes: false, conf: false, frac: true, detail: true };
  // порог уверенности из поля вкладки CV: кристаллы с conf ниже него скрываем (живой предпросмотр «а если поднять?»)
  function cvConfThr() { const e = $("cvConf"); const v = e && e.value !== "" ? parseFloat(e.value) : NaN; return isNaN(v) ? 0 : v; }
  let cvCurClean = false;       // текущий кадр чистый (контуры рисуем сами); false — старая проба с «впечёнными»
  let cvHoverObj = null;        // кристалл под мышкой (подсвечиваем форму)

  // где внутри <img> лежит сама картинка (object-fit: contain даёт поля по бокам/сверху)
  function cvImgGeom() {
    const ov = $("cvOverlay"); if (!ov || !ov.naturalWidth || !ov.clientWidth) return null;
    const bw = ov.clientWidth, bh = ov.clientHeight;
    const sc = Math.min(bw / ov.naturalWidth, bh / ov.naturalHeight);
    return { sc, ox: (bw - ov.naturalWidth * sc) / 2, oy: (bh - ov.naturalHeight * sc) / 2, bw, bh };
  }
  // Классификация кристалла по ТЕКУЩИМ порогам вкладки «CV» (поменял порог — оверлей сразу перекрасился,
  // без повторного прогона модели; сохранённый рассев пробы обновится при следующем разборе).
  // Причина дефекта определяется ВСЕГДА (и подписывается красным), в «% брака» идёт с порога по СВ — это
  // решает сервер при разборе. Правила зеркалят cv_analyzer._defect; размер-брак (tiny/huge) берём от сервера.
  const CV_REASONS = {
    needle: { name: "игла", cause: "раффиноза — приходит с сырьём (старая, подмороженная свёкла)", todo: "уваркой не убрать; снизить пересыщение, смотреть качество и хранение свёклы" },
    aggregate: { name: "сросток", cause: "высокое пересыщение, плохое перемешивание, много центров при заводке", todo: "вести по СВ и t, усилить циркуляцию, не переуваривать" },
    crooked: { name: "кривой", cause: "высокий фон несахаров, низкая чистота утфеля", todo: "поднять чистоту сиропа; держать стабильные t и циркуляцию" },
    tiny: { name: "мелочь (<0,25 мм)", cause: "слишком много центров при заводке", todo: "проверить дозировку затравки и СВ заводки" },
    huge: { name: "слишком крупный (>1,2 мм)", cause: "возможно сросток", todo: "проверить кристалл на срастание" },
  };
  const CV_NOTCH_MAX = 5;
  function cvClassify(o) {
    if (o.group === "cut") return { layer: "cut", reason: null };
    if (o.group === "bubble") return { layer: "bubble", reason: null };
    if (o.members > 1) return { layer: "reject", reason: "aggregate" };   // склеен из нескольких масок
    const thr = (id) => { const e = $(id); return e && e.value !== "" ? parseFloat(e.value) : null; };
    const tC = thr("cvMinCirc"), tS = thr("cvMinSol"), tA = thr("cvMaxAspect"), tU = thr("cvSuspect"), gS = thr("cvSmallMax"), gM = thr("cvMediumMax");
    if (o.circularity == null || [tC, tS, tA, gS, gM].some((v) => v == null || isNaN(v)))
      return { layer: o.defect ? "reject" : (o.suspect ? "suspect" : o.group), reason: o.defect || null };
    const n = o.notches || 0;
    let reason = null;
    if (o.aspect > tA) reason = "needle";
    else if (n >= 1 && n <= CV_NOTCH_MAX && (n >= 2 || o.solidity < tS)) reason = "aggregate";
    else if (n > CV_NOTCH_MAX || o.solidity < tS || o.circularity < tC) reason = "crooked";
    else if (o.defect === "tiny" || o.defect === "huge") reason = o.defect;
    if (reason) return { layer: "reject", reason };
    if (tU != null && !isNaN(tU) && o.aspect > tU) return { layer: "suspect", reason: null, suspect: true };
    return { layer: o.size_um < gS ? "small" : (o.size_um < gM ? "medium" : "large"), reason: null };
  }
  function cvGroupOf(o) { return cvClassify(o).layer; }
  function cvDrawOverlay() {
    const cv = $("cvOverlayCanvas"), ov = $("cvOverlay"); if (!cv || !ov) return;
    // верх картинки в карточке — к нему привязаны листалка кадров и кнопка fullscreen (над
    // картинкой в режиме CV стоит ряд слоёв, его высота зависит от ширины окна)
    const card = document.querySelector(".micro-cam-card");
    if (card && !ov.hidden && ov.offsetTop) card.style.setProperty("--cv-img-top", ov.offsetTop + "px");
    const g = cvCurClean && !ov.hidden ? cvImgGeom() : null;
    if (!g) { cv.hidden = true; return; }
    cv.hidden = false;
    const dpr = window.devicePixelRatio || 1;
    cv.style.left = ov.offsetLeft + "px"; cv.style.top = ov.offsetTop + "px";
    cv.style.width = g.bw + "px"; cv.style.height = g.bh + "px";
    if (cv.width !== Math.round(g.bw * dpr) || cv.height !== Math.round(g.bh * dpr)) {
      cv.width = Math.round(g.bw * dpr); cv.height = Math.round(g.bh * dpr);
    }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, g.bw, g.bh);
    const path = (poly) => {
      ctx.beginPath();
      poly.forEach((p, i) => { const x = g.ox + p[0] * g.sc, y = g.oy + p[1] * g.sc; if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.closePath();
    };
    ctx.lineJoin = "round";
    const confThr = cvConfThr(); let hiddenByConf = 0;
    cvCurObjects.forEach((o) => {
      if (!o.poly || o.poly.length < 3) return;
      if (o.conf != null && o.conf < confThr) { hiddenByConf++; return; }
      if (o === cvHoverObj) return;
      const gr = cvGroupOf(o); if (!cvLayers[gr]) return;
      ctx.strokeStyle = CV_GROUP_COLOR[gr] || "#ccc"; ctx.lineWidth = 1.5;
      ctx.setLineDash(gr === "suspect" ? [5, 3] : []);
      path(o.poly); ctx.stroke(); ctx.setLineDash([]);
      if (cvLayers.sizes && gr !== "reject" && gr !== "cut" && gr !== "bubble") {
        ctx.fillStyle = CV_GROUP_COLOR[gr]; ctx.font = "10px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(String(Math.round(o.size_um)), g.ox + o.cx * g.sc, g.oy + o.cy * g.sc);
      }
      if (cvLayers.conf && o.conf != null) {         // уверенность модели на кристалле; сомнительные — красным
        ctx.fillStyle = o.conf < 0.5 ? "#ff8a80" : "#e7eefb"; ctx.font = "10px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(o.conf.toFixed(2), g.ox + o.cx * g.sc, g.oy + o.cy * g.sc + (cvLayers.sizes ? 11 : 0));
      }
    });
    const cn = $("cvConfHidden"); if (cn) cn.textContent = hiddenByConf ? "скрыто по conf ≥ " + confThr + ": " + hiddenByConf : "";
    // зоны разломов (подтверждённые по серии кадров)
    if (cvLayers.frac) {
      (((cvView && cvView.fracture) || {}).zones || []).forEach((z) => {
        if (!z.poly || z.poly.length < 3) return;
        ctx.strokeStyle = "#ff3b30"; ctx.lineWidth = 2.5; ctx.setLineDash([7, 4]);
        path(z.poly); ctx.stroke(); ctx.setLineDash([]);
      });
    }
    // кристалл под мышкой — поверх всего: заливка формы + жирный контур
    const h = cvHoverObj;
    if (h && h.poly && h.poly.length >= 3) {
      const col = CV_GROUP_COLOR[cvGroupOf(h)] || "#fff";
      path(h.poly);
      ctx.globalAlpha = 0.3; ctx.fillStyle = col; ctx.fill(); ctx.globalAlpha = 1;
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 4; ctx.stroke();
      ctx.strokeStyle = col; ctx.lineWidth = 2.5; ctx.stroke();
    }
  }
  function cvPointInPoly(poly, x, y) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }
  function wireLayers() {
    try { Object.assign(cvLayers, JSON.parse(localStorage.getItem(CV_LAYERS_KEY) || "{}")); } catch (e) { }
    document.querySelectorAll("#cvLayers input").forEach((c) => {
      if (c.value in cvLayers) c.checked = !!cvLayers[c.value];
      c.addEventListener("change", () => {
        cvLayers[c.value] = c.checked;
        try { localStorage.setItem(CV_LAYERS_KEY, JSON.stringify(cvLayers)); } catch (e) { }
        cvDrawOverlay();
      });
    });
    // пороги на вкладке «CV» → оверлей перекрашивается сразу
    ["cvMinCirc", "cvMinSol", "cvMaxAspect", "cvSuspect", "cvSmallMax", "cvMediumMax", "cvConf"].forEach((id) => {
      const e = $(id); if (e) e.addEventListener("input", cvDrawOverlay);
    });
    const ov = $("cvOverlay");
    if (ov) {
      ov.addEventListener("load", cvDrawOverlay);
      if (window.ResizeObserver) new ResizeObserver(cvDrawOverlay).observe(ov);
    }
    window.addEventListener("resize", cvDrawOverlay);
    document.addEventListener("fullscreenchange", () => setTimeout(cvDrawOverlay, 60));
  }

  // причины брака и вытянутые под рассевом: счётчики всегда; в «брак» они идут с порога по СВ
  function cvRenderReasons(s, probeSv) {
    const box = $("cvReasons"); if (!box) return;
    const rs = s.reasons || {};
    const rows = ["needle", "aggregate", "crooked", "tiny", "huge"].filter((k) => k in rs && (rs[k] > 0 || k === "needle" || k === "aggregate" || k === "crooked"));
    let html = "";
    if (s.reject_active === false)
      html += '<div class="note">СВ ' + (probeSv != null ? probeSv : (s.sv != null ? s.sv : "—")) + ' — ниже порога: причины подписаны, в «брак» пока не считаются</div>';
    rows.forEach((k) => {
      const n = Math.round((rs[k] || 0) * 10) / 10;
      html += '<div class="rs' + (n ? "" : " is-zero") + '" title="' + CV_REASONS[k].cause + ' — ' + CV_REASONS[k].todo + '"><i style="background:#e24b4a"></i><span>' + CV_REASONS[k].name + '</span><b>' + n + '</b></div>';
    });
    const su = Math.round((s.suspect || 0) * 10) / 10;
    html += '<div class="rs' + (su ? "" : " is-zero") + '" title="Вытянутые 1,6–3,0: не брак, но рост доли — предупреждение"><i style="background:#6ad1f5"></i><span>вытянутые (не брак)</span><b>' + su + '</b></div>';
    box.innerHTML = html;
  }

  // --- лента проб слева от окна (новая сверху) + кадры выбранной пробы ---
  function cvOverlaySrc(ts, idx) {
    // проба неизменна → без анти-кэша: браузер кэширует, картинка не перегружается каждые 5 с
    return "/api/cv/overlay?" + [cvSerialQ(), "ts=" + encodeURIComponent(ts), "idx=" + idx].filter(Boolean).join("&");
  }
  function cvThumbSrc(ts) {
    return "/api/cv/thumb?" + [cvSerialQ(), "ts=" + encodeURIComponent(ts)].filter(Boolean).join("&");
  }
  // «2026-10-03_22_47_43» → «22:47:43» (withDate: «03.10 22:47:43»)
  function cvTsLabel(ts, withDate) {
    const m = String(ts || "").match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})_(\d{2})_(\d{2})$/);
    if (!m) return String(ts || "");
    const t = m[4] + ":" + m[5] + ":" + m[6];
    return withDate ? m[3] + "." + m[2] + " " + t : t;
  }
  // подпись пробы при наведении: когда, стадия варки, сколько кристаллов, средний размер
  function cvSampleTitle(s) {
    const sm = s.summary || {}, sz = sm.size_um || {}, fr = s.fracture || {};
    const parts = [cvTsLabel(s.ts, true)];
    if (s.stage != null) parts.push("стадия " + s.stage);
    if (sm.count != null) parts.push(Math.round(sm.count) + " крист.");
    if (sz.mean != null) parts.push("ср. " + sz.mean + " мкм");
    if (s.frames) parts.push("кадров " + s.frames);
    if (fr.has_fracture) parts.push("разлом");
    return parts.join(" · ");
  }
  function cvRenderStrip() {
    const wrap = $("cvStrip"); if (!wrap) return;
    // НЕ перестраиваем ленту целиком: новые пробы вставляются сверху, исчезнувшие (ротация) убираются,
    // остальные элементы остаются на месте — браузер сохраняет позицию прокрутки, и то, что ты
    // рассматриваешь, просто сдвигается ниже, а не «улетает».
    const have = new Map([...wrap.querySelectorAll(".micro-cv-strip__item")].map((b) => [b.dataset.ts, b]));
    const want = new Set(cvSamples.map((s) => s.ts));
    have.forEach((b, ts) => { if (!want.has(ts)) { b.remove(); have.delete(ts); } });
    let prevEl = null;                         // вставляем по порядку: новая проба — над предыдущей
    cvSamples.forEach((s) => {
      let b = have.get(s.ts);
      if (!b) {
        b = document.createElement("button");
        b.type = "button"; b.className = "micro-cv-strip__item"; b.dataset.ts = s.ts;
        b.title = cvSampleTitle(s);
        const im = document.createElement("img"); im.alt = ""; im.src = cvThumbSrc(s.ts);
        const cap = document.createElement("span");
        cap.textContent = cvTsLabel(s.ts).slice(0, 5) + (s.stage != null ? " · ст." + s.stage : "");
        b.appendChild(im); b.appendChild(cap);
        b.addEventListener("click", () => cvSelectProbe(s.ts));
        wrap.insertBefore(b, prevEl ? prevEl.nextSibling : wrap.firstChild);
      }
      prevEl = b;
    });
    wrap.hidden = !cvSamples.length;
    const cur = cvView && cvView.ts;
    wrap.querySelectorAll(".micro-cv-strip__item").forEach((b) => b.classList.toggle("is-active", b.dataset.ts === cur));
  }
  // выбрать пробу из ленты → показать её в окне «Комп. зрение». Выбранная проба ЗАКРЕПЛЯЕТСЯ: новые
  // пробы в окно сами не прыгают (они появляются в ленте сверху). Вернуть слежение — кнопка «авто».
  async function cvSelectProbe(ts) {
    const latest = cvLastResult && cvLastResult.ts;
    let r = (ts === latest) ? cvLastResult : null;
    if (!r) {
      try { r = await api("/api/cv/result?" + [cvSerialQ(), "ts=" + encodeURIComponent(ts)].filter(Boolean).join("&")); }
      catch (e) { r = null; }
    }
    if (!r || r.empty) return;
    cvView = r; cvViewPinned = true;
    cvGalIdx = 0; cvWinOn = true;              // переключаем верхнее окно в «Комп. зрение»
    cvRenderScatter(); cvRenderStrip(); cvShowFrame();
  }
  function cvViewFrames() { return ((cvView && cvView.frames) || []).filter((f) => f.overlay); }
  function cvShowFrame() {
    const ov = $("cvOverlay"); if (!ov) return;
    const pager = $("cvPager"), posEl = $("cvGalPos");
    const frames = cvViewFrames();
    if (!frames.length) { if (pager) pager.hidden = true; if (cvWinOn) applyWinMode(); return; }
    cvGalIdx = Math.max(0, Math.min(cvGalIdx, frames.length - 1));
    const ts = cvView.ts, idx = frames[cvGalIdx].idx != null ? frames[cvGalIdx].idx : cvGalIdx;
    const key = ts + "#" + idx;
    cvCurClean = frames[cvGalIdx].clean === true;
    if (ov.dataset.key !== key) {              // кадр сменился — грузим; иначе картинку не дёргаем
      ov.dataset.key = key;
      ov.src = cvOverlaySrc(ts, idx);
      cvLoadObjects(ts, idx);                  // объекты кадра для наведения (размер/форма)
    }
    if (posEl) posEl.textContent = cvTsLabel(ts) + " · " + (cvGalIdx + 1) + "/" + frames.length;
    if (pager) pager.hidden = false;
    cvSyncFollowBtn();
    if (cvWinOn) applyWinMode();
    cvDrawOverlay();
  }
  // объекты кадра для наведения и отрисовки (bbox/size_um/форма/контур)
  function cvLoadObjects(ts, idx) {
    cvCurObjects = []; cvHoverObj = null; cvDrawOverlay();
    const q = [cvSerialQ(), "ts=" + encodeURIComponent(ts), "idx=" + idx].filter(Boolean).join("&");
    const key = ts + "#" + idx;
    api("/api/cv/objects?" + q).then((r) => {
      if ($("cvOverlay").dataset.key !== key) return;       // успели переключить кадр
      cvCurObjects = (r && r.objects) || []; cvDrawOverlay();
    }).catch(() => {});
  }
  // наведение на кристалл в окне CV → подсветка его формы + тултип с размером/формой
  function wireHoverTip() {
    const ov = $("cvOverlay"), tip = $("cvHoverTip"), card = document.querySelector(".micro-cam-card");
    if (!ov || !tip || !card) return;
    const setHover = (o) => { if (o !== cvHoverObj) { cvHoverObj = o; cvDrawOverlay(); } };
    ov.addEventListener("mousemove", (e) => {
      const g = cvImgGeom();
      if (!cvWinOn || !cvCurObjects.length || !g) { tip.hidden = true; setHover(null); return; }
      const r = ov.getBoundingClientRect();
      const ix = (e.clientX - r.left - g.ox) / g.sc, iy = (e.clientY - r.top - g.oy) / g.sc;
      // самый маленький объект под курсором (кристаллы могут лежать друг на друге): по контуру,
      // а у старых проб без контура — по прямоугольнику. Скрытые слои не ловим.
      let best = null, bestArea = Infinity;
      for (const o of cvCurObjects) {
        const b = o.bbox; if (!b) continue;
        if (ix < b[0] || ix > b[0] + b[2] || iy < b[1] || iy > b[1] + b[3]) continue;
        if (cvCurClean && !cvLayers[cvGroupOf(o)]) continue;
        if (o.conf != null && o.conf < cvConfThr()) continue;
        if (o.poly && o.poly.length >= 3 && !cvPointInPoly(o.poly, ix, iy)) continue;
        const a = b[2] * b[3];
        if (a < bestArea) { bestArea = a; best = o; }
      }
      setHover(best);
      if (!best) { tip.hidden = true; return; }
      const cls = cvClassify(best), gr = CV_GROUP_NAMES[cls.layer] || best.group;
      const rs = cls.reason && CV_REASONS[cls.reason];
      const grp = best.members > 1 ? " · сросток из " + best.members + " кристаллов (маски соприкасаются)"
        : (best.seam_merged ? " · склеен из " + best.seam_merged + " частей по шву нарезки" : "");
      const why = rs ? '<span class="why"><b>' + rs.name + '</b> — ' + rs.cause + '.<br>Что делать: ' + rs.todo + '.</span>'
        : (cls.layer === "suspect" ? '<span class="why">Вытянутый, но не игла. Следи за долей таких: рост — сигнал про глюкозу/раффинозу.</span>' : "");
      // форма: округлость / выпуклость / вытянутость; значение за текущим порогом (поля вкладки
      // «CV») подсвечиваем — видно, из-за чего кристалл ушёл в брак и куда двигать порог
      const thr = (id) => { const e = $(id); return e && e.value !== "" ? parseFloat(e.value) : null; };
      const mark = (v, bad) => bad ? '<span class="is-bad">' + v + "</span>" : v;
      const tC = thr("cvMinCirc"), tS = thr("cvMinSol"), tA = thr("cvMaxAspect");
      const shape = best.circularity == null ? "" : "<br>округл. " + mark(best.circularity.toFixed(2), tC != null && best.circularity < tC) +
        " · выпукл. " + mark(best.solidity.toFixed(2), tS != null && best.solidity < tS) +
        " · вытянут. " + mark(best.aspect.toFixed(1), tA != null && best.aspect > tA) +
        (best.conf != null ? "<br>уверенность модели (conf) " + mark(best.conf.toFixed(2), best.conf < 0.5) : "");
      // объём по модели «призма» (M3), мм³ = мкм³ / 1e9; у кристалла мельче порога мелочи — пометка
      const v3 = best.vol_um3 && best.vol_um3.m3, fu = parseFloat(($("cvFinesUm") || {}).value);
      const vol = v3 == null ? "" : "<br>V <b>" + Number((v3 / 1e9).toPrecision(2)) + " мм³</b> (призма)" +
        (fu > 0 && best.size_um < fu && cls.reason !== "aggregate" ? " · мелочь" : "");
      const head = "Ø <b>" + best.size_um + " мкм</b> · S <b>" + Math.round(best.area_um2) + " мкм²</b>";
      // «подробно» выключено — коротко: только размер и площадь; включено — форма, причина, что делать, уверенность
      tip.innerHTML = cvLayers.detail === false ? head
        : head + "<br>" + best.length_um + "×" + best.width_um + " мкм · " + gr + grp + vol + shape + why;
      // позиция: справа-снизу от курсора; у правого/нижнего края карточки — слева/сверху, чтобы
      // подсказка не уезжала за край и не обрезалась. Размер меряем при left=0 (иначе у края
      // блок сжимается и переносит строки).
      const cardR = card.getBoundingClientRect();
      const cx = e.clientX - cardR.left, cy = e.clientY - cardR.top;
      tip.hidden = false;
      tip.style.transform = "none";
      tip.style.left = "0px"; tip.style.top = "0px";
      const tw = tip.offsetWidth, th = tip.offsetHeight, gap = 12, pad = 4;
      let x = cx + gap, y = cy + gap;
      if (x + tw > cardR.width - pad) x = cx - gap - tw;
      if (y + th > cardR.height - pad) y = cy - gap - th;
      tip.style.left = Math.max(pad, x) + "px";
      tip.style.top = Math.max(pad, y) + "px";
    });
    ov.addEventListener("mouseleave", () => { tip.hidden = true; setHover(null); });
  }
  // листалка кадров выбранной пробы (‹ › поверх окна «Комп. зрение»)
  function cvViewFrame(idx) {
    cvGalIdx = idx;
    cvViewPinned = true;            // смотрю кадры этой пробы — не уводить на свежую
    cvWinOn = true;
    cvShowFrame();                  // ставит overlay.src и (т.к. cvWinOn) applyWinMode
  }
  function cvSyncFollowBtn() {
    const b = $("cvFollowBtn"); if (b) b.classList.toggle("is-on", !cvViewPinned);
  }
  function wireGallery() {
    const fb = $("cvFollowBtn");
    if (fb) fb.addEventListener("click", () => {
      cvViewPinned = !cvViewPinned;
      if (!cvViewPinned && cvLastResult) {         // включили слежение — сразу к самой свежей пробе
        cvView = cvLastResult; cvGalIdx = 0;
        cvRenderScatter(); cvRenderStrip(); cvShowFrame();
      }
      cvSyncFollowBtn();
    });
    const prev = $("cvGalPrev"), next = $("cvGalNext");
    if (prev) prev.addEventListener("click", () => { if (cvGalIdx > 0) cvViewFrame(cvGalIdx - 1); });
    if (next) next.addEventListener("click", () => { if (cvGalIdx < cvViewFrames().length - 1) cvViewFrame(cvGalIdx + 1); });
  }

  // --- тренд по РЕАЛЬНОМУ времени (как в SCADA): выбор даты, сдвиг за пределы загруженного, масштаб ---
  // Данные берутся из журнала проб по дням (cv_history) — он не стирается ротацией кадров.
  const CV_TREND_KEY = "microCvTrendSeries3";   // 3: добавлены серии «стадия» и «СВ» (включены по умолчанию)
  const CV_PCT_SERIES = ["small", "medium", "large", "reject", "sv"];   // шкала слева, % (СВ тоже в % — те же 0–100)
  const CV_UM_SERIES = ["mean", "median"];                        // шкала справа, мкм
  // режим варки из ПЛК (рисуется пунктиром). Уровень — в %, на общей шкале 0–100; остальные — каждая на своей шкале
  // по видимому участку, но не уже «минимального размаха» (иначе шум на 0,2 °C выглядел бы бурей)
  const CV_REGIME = ["temp", "vac", "level", "current", "cook_time", "seed_age"];
  const CV_AUTO_SPAN = { temp: 10, vac: 0.5, current: 5, cook_time: 60, seed_age: 60 };
  const CV_MIN_SERIES = ["cook_time", "seed_age"];                // в журнале секунды, показываем минуты
  const CV_MAX_SPAN = 400 * 86400, CV_MIN_SPAN = 60;
  let cvTrendDay = null;         // выбранная дата «YYYY-MM-DD»
  let cvTrendDays = [];          // дни, за которые есть пробы
  let cvTrendFollow = true;      // вид сам подгоняется под выбранный день (пока не двигали/масштабировали)
  let cvTrendFetchTimer = null;
  let cvTrendSeq = 0;

  function cvSelectedSeries() {
    return [...document.querySelectorAll("#cvSeries input:checked")].map((c) => c.value);
  }
  const cvPad2 = (n) => String(n).padStart(2, "0");
  function cvDayStr(d) { return d.getFullYear() + "-" + cvPad2(d.getMonth() + 1) + "-" + cvPad2(d.getDate()); }
  function cvDayRange(day) {                     // «2026-10-04» → [epoch 00:00, epoch 24:00) в местном времени
    const p = day.split("-").map(Number), a = new Date(p[0], p[1] - 1, p[2], 0, 0, 0).getTime() / 1000;
    return [a, a + 86400];
  }
  function cvFmtT(t, withSec) {                  // epoch → «04.10 07:44[:55]»
    const d = new Date(t * 1000);
    return cvPad2(d.getDate()) + "." + cvPad2(d.getMonth() + 1) + " " + cvPad2(d.getHours()) + ":" + cvPad2(d.getMinutes()) +
      (withSec ? ":" + cvPad2(d.getSeconds()) : "");
  }
  async function cvTrendFetch(a, b) {            // загрузить пробы [a,b] (epoch, сек)
    const seq = ++cvTrendSeq;
    const q = [cvSerialQ(), "series=small,medium,large,reject,mean,median,count,stage,sv," + CV_REGIME.join(","), "from=" + Math.floor(a), "to=" + Math.ceil(b), "limit=5000"].filter(Boolean).join("&");
    const d = await api("/api/cv/trend?" + q);
    if (seq !== cvTrendSeq) return false;        // пока грузили, запросили другое — это устарело
    CV_MIN_SERIES.forEach((k) => { if (d.series && d.series[k]) d.series[k] = d.series[k].map((x) => (x == null ? null : x / 60)); });
    cvTrendData = d; return true;
  }
  // подгон вида под выбранный день: от первой до последней пробы, с полями
  function cvTrendFit() {
    const d = cvTrendData; if (!d || !d.t || !d.t.length) { const r = cvDayRange(cvTrendDay); cvTrendView = { start: r[0], end: r[1] }; return; }
    const lo = d.t[0], hi = d.t[d.t.length - 1];
    const pad = Math.max(120, (hi - lo) * 0.04);
    cvTrendView = { start: lo - pad, end: hi + pad };
  }
  async function cvLoadTrend(refreshDays) {
    try {
      // список дней — только при плановом обновлении; при смене даты он не нужен (быстрее отклик)
      if (refreshDays !== false || !cvTrendDays.length) {
        const days = ((await api("/api/cv/trend/days" + (cvSerialQ() ? "?" + cvSerialQ() : ""))).days) || [];
        cvTrendDays = days.map((x) => x.date);
      }
      if (!cvTrendDay) cvTrendDay = cvTrendDays.length ? cvTrendDays[cvTrendDays.length - 1] : cvDayStr(new Date());
      const di = $("cvTrendDate"); if (di && di.value !== cvTrendDay) di.value = cvTrendDay;
      if (cvTrendFollow) {
        const r = cvDayRange(cvTrendDay);
        if (await cvTrendFetch(r[0], r[1])) { cvTrendFit(); cvDrawTrend(); }
      } else cvTrendEnsure(0);
    } catch (e) { /* нет данных */ }
  }
  // загружен ли нужный участок; нет — подгрузить с запасом в один экран по бокам
  function cvTrendEnsure(delay) {
    clearTimeout(cvTrendFetchTimer);
    cvTrendFetchTimer = setTimeout(async () => {
      if (cvTrendFollow) return;           // «вся дата»: данные ведёт cvLoadTrend, отложенную подгрузку не шлём
      const v = cvTrendView, d = cvTrendData; if (!v) return;
      if (d && d.from != null && d.from <= v.start && d.to >= v.end) return;
      const span = v.end - v.start;
      try { if (await cvTrendFetch(v.start - span, v.end + span) && !cvTrendFollow) cvDrawTrend(); } catch (e) { }
    }, delay);
  }
  function cvTrendSetDay(day, fit) {
    cvTrendDay = day; cvTrendFollow = true; cvTrendHover = null;
    clearTimeout(cvTrendFetchTimer);
    const di = $("cvTrendDate"); if (di) di.value = day;
    cvLoadTrend(false);
  }
  function cvNiceStep(span) {                    // шаг подписей оси времени ≈ 6–8 меток на экран
    const steps = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 2592000];
    return steps.find((s) => span / s <= 8) || steps[steps.length - 1];
  }
  function cvNiceMax(v) {
    const p = Math.pow(10, Math.floor(Math.log10(Math.max(v, 1)))), m = [1, 2, 2.5, 5, 10].find((x) => x * p >= v) || 10;
    return m * p;
  }
  function cvDrawTrend() {
    const cv = $("cvTrend"); if (!cv) return;
    const ctx = cv.getContext("2d");
    const W = cv.width = cv.clientWidth || 620, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    const muted = getCss("--muted", "#8a94a6"), border = getCss("--border", "rgba(131,151,179,.28)");
    const d = cvTrendData, v = cvTrendView;
    cvTrendGeom = null;
    if (!d || !v) return;
    const t = d.t || [], series = d.series || {};
    const span = v.end - v.start;
    const sel = cvSelectedSeries();
    const showUm = sel.some((s) => CV_UM_SERIES.includes(s));
    const pad = { l: 34, r: showUm ? 42 : 10, t: 12, b: 34 };
    const plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b;
    const xAt = (tt) => pad.l + plotW * (tt - v.start) / span;
    cvTrendGeom = { padL: pad.l, plotW, start: v.start, span };
    // видимые точки (с запасом на одну соседнюю — линия уходит за край)
    let i0 = 0, i1 = t.length;
    while (i0 < t.length && t[i0] < v.start) i0++;
    while (i1 > i0 && t[i1 - 1] > v.end) i1--;
    const a = Math.max(0, i0 - 1), b = Math.min(t.length, i1 + 1);
    // шкала мкм справа — по видимым точкам выбранных серий
    let umMax = 10;
    for (let i = i0; i < i1; i++) for (const s of CV_UM_SERIES) if (sel.includes(s) && series[s] && series[s][i] != null) umMax = Math.max(umMax, series[s][i]);
    umMax = cvNiceMax(umMax * 1.05);
    // стадия — на своей шкале (0…max видимой, не меньше 10), чтобы ступеньки были читаемы
    let stageMax = 10;
    if (series.stage) for (let i = i0; i < i1; i++) if (series.stage[i] != null) stageMax = Math.max(stageMax, series.stage[i] + 1);
    const scaleOf = (name) => (name === "stage" ? stageMax : (CV_UM_SERIES.includes(name) ? umMax : 100));
    const colorOf = (name) => (name === "stage" ? getCss("--text", "#fff") : (CV_SERIES_COLOR[name] || "#888"));
    const yAt = (val, mx) => pad.t + plotH * (1 - Math.max(0, Math.min(1, val / mx)));
    // режим варки со своей шкалой: диапазон по видимым точкам (не уже CV_AUTO_SPAN) + 8 % полей
    const auto = {};
    for (const s of CV_REGIME) {
      if (!CV_AUTO_SPAN[s] || !sel.includes(s) || !series[s]) continue;
      let lo = Infinity, hi = -Infinity;
      for (let i = i0; i < i1; i++) { const x = series[s][i]; if (x != null) { lo = Math.min(lo, x); hi = Math.max(hi, x); } }
      if (lo === Infinity) continue;
      if (hi - lo < CV_AUTO_SPAN[s]) { const c = (hi + lo) / 2; lo = c - CV_AUTO_SPAN[s] / 2; hi = c + CV_AUTO_SPAN[s] / 2; }
      const p = (hi - lo) * 0.08; auto[s] = { lo: lo - p, hi: hi + p };
    }
    const yOf = (name, val, mx) => (auto[name]
      ? pad.t + plotH * (1 - Math.max(0, Math.min(1, (val - auto[name].lo) / (auto[name].hi - auto[name].lo))))
      : yAt(val, mx));
    // сетка: горизонтали + подписи слева (%) и справа (мкм)
    ctx.font = "10px monospace"; ctx.lineWidth = 1;
    for (let k = 0; k <= 4; k++) {
      const y = pad.t + plotH * k / 4;
      ctx.strokeStyle = border; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
      ctx.fillStyle = muted; ctx.textAlign = "right"; ctx.fillText(String(100 - k * 25), pad.l - 4, y + 3);
      if (showUm) { ctx.textAlign = "left"; ctx.fillText(String(Math.round(umMax * (4 - k) / 4)), W - pad.r + 4, y + 3); }
    }
    ctx.textAlign = "left"; ctx.fillStyle = muted;
    ctx.fillText("%", 4, pad.t + 4); if (showUm) ctx.fillText("мкм", W - pad.r + 4, pad.t - 2);
    // ось времени: вертикальные линии и подписи «ЧЧ:ММ», при смене суток — ещё и дата
    const step = cvNiceStep(span);
    const off = new Date(v.start * 1000).getTimezoneOffset() * 60;           // метки по МЕСТНОМУ времени
    let tick = Math.ceil((v.start - off) / step) * step + off, lastDay = null;
    ctx.textAlign = "center";
    for (; tick <= v.end; tick += step) {
      const x = xAt(tick), dt = new Date(tick * 1000), day = cvDayStr(dt);
      ctx.strokeStyle = border; ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + plotH); ctx.stroke();
      ctx.fillStyle = muted;
      ctx.fillText(cvPad2(dt.getHours()) + ":" + cvPad2(dt.getMinutes()), x, H - pad.b + 13);
      if (day !== lastDay) { ctx.fillText(cvPad2(dt.getDate()) + "." + cvPad2(dt.getMonth() + 1), x, H - pad.b + 25); lastDay = day; }
    }
    if (!t.length || i1 <= i0) {
      ctx.fillStyle = muted; ctx.font = "12px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("нет проб за этот период", pad.l + plotW / 2, pad.t + plotH / 2);
    }
    // линии идут КУСКАМИ — по одной варке. Разрыв между соседними пробами, если:
    //  • пауза больше обычной (≥ 5 обычных интервалов, но не меньше 10 мин) — сигналов нет, варка кончилась;
    //  • стадия упала (напр. 9 → 3) — началась новая варка.
    // «Обычный» интервал берём по ВСЕМ загруженным пробам, а не по видимым: при сдвиге в окне остаются
    // две точки из разных варок, и интервал по ним самим тянул линию через всю паузу.
    const dts = []; for (let i = 1; i < t.length; i++) dts.push(t[i] - t[i - 1]);
    dts.sort((x, y) => x - y);
    const gap = Math.max(600, (dts.length ? dts[Math.floor(dts.length / 2)] : 0) * 5);
    const stg = d.stage || [];
    const brewBreak = (i) => i > 0 && (t[i] - t[i - 1] > gap || (stg[i] != null && stg[i - 1] != null && stg[i] < stg[i - 1]));
    const drawSeries = (name) => {
      const arr = series[name]; if (!arr) return;
      const col = colorOf(name); const mx = scaleOf(name); const isStage = name === "stage";
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = isStage ? 1.5 : 2; ctx.beginPath();
      let started = false, prevY = 0;
      for (let i = a; i < b; i++) {
        const val = arr[i]; if (val == null) { started = false; continue; }
        const x = xAt(t[i]), y = yOf(name, val, mx);
        if (started && !brewBreak(i)) { if (isStage) ctx.lineTo(x, prevY); ctx.lineTo(x, y); } else ctx.moveTo(x, y);   // стадия — ступенькой
        started = true; prevY = y;
      }
      if (CV_REGIME.includes(name)) ctx.setLineDash([6, 4]);          // режим варки — пунктиром
      ctx.stroke(); ctx.setLineDash([]);
      if (isStage) {                                                   // цифры стадии на каждом переходе (и на первой видимой пробе)
        ctx.font = "bold 11px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "bottom";
        let last = null;
        for (let i = i0; i < i1; i++) {
          const val = arr[i]; if (val == null) continue;
          if (val !== last || brewBreak(i)) ctx.fillText(String(val), xAt(t[i]), yAt(val, mx) - 4);
          last = val;
        }
        ctx.textBaseline = "alphabetic";
        return;
      }
      if (i1 - i0 <= 120) for (let i = i0; i < i1; i++) {          // точки-пробы, пока их немного
        const val = arr[i]; if (val == null) continue;
        ctx.beginPath(); ctx.arc(xAt(t[i]), yOf(name, val, mx), 2.2, 0, Math.PI * 2); ctx.fill();
      }
    };
    ctx.save(); ctx.beginPath(); ctx.rect(pad.l, pad.t - 2, plotW, plotH + 4); ctx.clip();
    sel.filter((s) => CV_PCT_SERIES.includes(s)).forEach(drawSeries);
    sel.filter((s) => CV_UM_SERIES.includes(s)).forEach(drawSeries);
    sel.filter((s) => CV_REGIME.includes(s)).forEach(drawSeries);
    if (sel.includes("stage")) drawSeries("stage");           // стадия — поверх остальных
    // линия-курсор: стоит на ближайшей к мышке пробе
    const hi = cvTrendHover;
    if (hi != null && hi >= 0 && hi < t.length && t[hi] >= v.start && t[hi] <= v.end) {
      const x = xAt(t[hi]);
      ctx.strokeStyle = muted; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + plotH); ctx.stroke(); ctx.setLineDash([]);
      sel.forEach((name) => {
        const val = series[name] && series[name][hi]; if (val == null) return;
        ctx.fillStyle = colorOf(name);
        ctx.beginPath(); ctx.arc(x, yOf(name, val, scaleOf(name)), 4, 0, Math.PI * 2); ctx.fill();
      });
    }
    ctx.restore();
    cvTrendUpdateRows(); cvTrendUpdateSpan();
  }
  // значения в строках серий: под курсором, а без него — по последней видимой пробе
  function cvTrendUpdateRows() {
    const d = cvTrendData, v = cvTrendView; if (!d || !v) return;
    let i = cvTrendHover;
    if (i == null) { i = d.t.length - 1; while (i >= 0 && d.t[i] > v.end) i--; if (i >= 0 && d.t[i] < v.start) i = -1; }
    document.querySelectorAll("#cvSeries .micro-srow").forEach((row) => {
      const name = row.querySelector("input").value, val = i >= 0 && d.series[name] ? d.series[name][i] : null;
      const unit = row.dataset.unit || "", k = Math.pow(10, Number(row.dataset.dec) || 1);     // data-dec: знаков после запятой (по умолчанию 1)
      row.querySelector("b").textContent = val == null ? "—" : (Math.round(val * k) / k) + (unit === "%" ? "%" : " " + unit);
    });
  }
  function cvTrendUpdateSpan() {
    const el = $("cvTrendSpan"), v = cvTrendView, d = cvTrendData; if (!el || !v) return;
    const s = v.end - v.start;
    const len = s >= 86400 * 2 ? Math.round(s / 86400) + " дн" : (s >= 3600 ? (Math.round(s / 360) / 10) + " ч" : Math.round(s / 60) + " мин");
    const n = d && d.t ? d.t.filter((x) => x >= v.start && x <= v.end).length : 0;
    el.textContent = cvFmtT(v.start) + " — " + cvFmtT(v.end) + " · " + len + " · проб " + n;
  }
  function getCss(name, fallback) {
    try { const v = getComputedStyle(document.documentElement).getPropertyValue(name); return v ? v.trim() : fallback; }
    catch (e) { return fallback; }
  }
  // тултип линии-курсора: дата и время пробы, стадия варки, число кристаллов, рассев
  function cvTrendTipShow(i, x, msg) {
    const tip = $("cvTrendTip"), cv = $("cvTrend"); if (!tip || !cv || !cvTrendData) return;
    const d = cvTrendData, s = d.series || {};
    const val = (name) => (s[name] && s[name][i] != null ? Math.round(s[name][i] * 10) / 10 : null);
    const stage = d.stage && d.stage[i];
    const head = "<b>" + cvFmtT(d.t[i], true) + "</b>" + (stage != null ? " · стадия " + stage : "");
    const cnt = val("count"), mean = val("mean"), med = val("median");
    const svv = val("sv");
    const line2 = [svv != null ? "СВ " + svv : null, cnt != null ? Math.round(cnt) + " крист." : null, mean != null ? "среднее " + mean + " мкм" : null, med != null ? "медиана " + med + " мкм" : null].filter(Boolean).join(" · ");
    // режим варки (ПЛК) на момент пробы — все доступные значения, независимо от выбранных серий
    const rv = (k, n) => (s[k] && s[k][i] != null ? Math.round(s[k][i] * n) / n : null);
    const line3 = [rv("temp", 10) != null ? "t " + rv("temp", 10) + " °C" : null, rv("vac", 1000) != null ? "разр. " + rv("vac", 1000) + " бар" : null,
      rv("level", 10) != null ? "ур. " + rv("level", 10) + "%" : null, rv("current", 10) != null ? "ток " + rv("current", 10) + " А" : null,
      rv("cook_time", 1) != null ? "варка " + rv("cook_time", 1) + " мин" : null, rv("seed_age", 1) != null ? "с заводки " + rv("seed_age", 1) + " мин" : null].filter(Boolean).join(" · ");
    const names = { small: "малая", medium: "средняя", large: "большая", reject: "брак" };
    const groups = CV_GROUPS.map((g) => val(g) == null ? "" :
      '<span class="dot" style="background:' + CV_SERIES_COLOR[g] + '"></span>' + names[g] + " " + val(g) + "%").filter(Boolean).join("<br>");
    tip.innerHTML = head + (line2 ? "<br>" + line2 : "") + (line3 ? "<br>" + line3 : "") + (groups ? "<br>" + groups : "") + (msg ? "<br><i>" + msg + "</i>" : "");
    tip.hidden = false;
    const w = tip.offsetWidth, W = cv.clientWidth;
    tip.style.left = (x + 12 + w > W ? Math.max(0, x - 12 - w) : x + 12) + "px";
    tip.style.top = "8px";
  }
  function wireTrend() {
    // выбранные серии запоминаем в браузере
    try { const saved = JSON.parse(localStorage.getItem(CV_TREND_KEY) || "null");
      if (Array.isArray(saved)) document.querySelectorAll("#cvSeries input").forEach((c) => { c.checked = saved.includes(c.value); }); } catch (e) { }
    document.querySelectorAll("#cvSeries input").forEach((c) => c.addEventListener("change", () => {
      try { localStorage.setItem(CV_TREND_KEY, JSON.stringify(cvSelectedSeries())); } catch (e) { }
      cvDrawTrend();
    }));
    // дата: поле, «‹ ›» по дням с пробами, «Сегодня»
    const di = $("cvTrendDate");
    if (di) di.addEventListener("change", () => { if (di.value) cvTrendSetDay(di.value); });
    const stepDay = (dir) => {
      const cur = cvTrendDay || cvDayStr(new Date());
      const cand = dir < 0 ? cvTrendDays.filter((x) => x < cur).pop() : cvTrendDays.find((x) => x > cur);
      if (cand) cvTrendSetDay(cand);
    };
    // выгрузка проб в CSV (для разбора и обучения): всё, а с Shift — только выбранный день
    const csv = $("cvTrendCsv");
    if (csv) csv.addEventListener("click", (e) => {
      const q = [cvSerialQ()];
      if (e.shiftKey && cvTrendDay) { const r = cvDayRange(cvTrendDay); q.push("from=" + r[0], "to=" + r[1]); }
      const a = document.createElement("a");
      a.href = "/api/cv/trend/export?" + q.filter(Boolean).join("&");
      a.download = ""; document.body.appendChild(a); a.click(); a.remove();
    });
    const prev = $("cvTrendPrev"), next = $("cvTrendNext"), today = $("cvTrendToday");
    if (prev) prev.addEventListener("click", () => stepDay(-1));
    if (next) next.addEventListener("click", () => stepDay(1));
    if (today) today.addEventListener("click", () => cvTrendSetDay(cvTrendDays.length ? cvTrendDays[cvTrendDays.length - 1] : cvDayStr(new Date())));

    const cv = $("cvTrend"); if (!cv) return;
    const tip = $("cvTrendTip");
    let drag = null;
    const hoverOff = () => { if (cvTrendHover != null) { cvTrendHover = null; cvDrawTrend(); } if (tip) tip.hidden = true; };
    const nearest = (clientX) => {                       // ближайшая по X проба; -1 — нет проб в окне
      const d = cvTrendData, g = cvTrendGeom; if (!d || !g || !d.t.length) return -1;
      const tt = g.start + (clientX - cv.getBoundingClientRect().left - g.padL) / g.plotW * g.span;
      let lo = 0, hi = d.t.length - 1;
      while (hi - lo > 1) { const m = (lo + hi) >> 1; if (d.t[m] < tt) lo = m; else hi = m; }
      const i = Math.abs(d.t[lo] - tt) <= Math.abs(d.t[hi] - tt) ? lo : hi;
      // липнем только к пробе рядом с мышкой (≤ 40 px): в паузе между варками тултип не показываем
      const px = Math.abs(d.t[i] - tt) / g.span * g.plotW;
      return d.t[i] >= g.start && d.t[i] <= g.start + g.span && px <= 40 ? i : -1;
    };
    const xOf = (i) => cvTrendGeom.padL + cvTrendGeom.plotW * (cvTrendData.t[i] - cvTrendGeom.start) / cvTrendGeom.span;
    cv.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, view: Object.assign({}, cvTrendView), moved: false }; cv.setPointerCapture(e.pointerId); });
    cv.addEventListener("pointermove", (e) => {
      if (!cvTrendData || !cvTrendGeom) return;
      if (!drag) {                                       // наведение: линия «притягивается» к ближайшей пробе
        const i = nearest(e.clientX);
        if (i < 0) { hoverOff(); return; }
        if (i !== cvTrendHover) { cvTrendHover = i; cvDrawTrend(); }
        cvTrendTipShow(i, xOf(i));
        return;
      }
      if (Math.abs(e.clientX - drag.x) > 3) drag.moved = true;
      if (!drag.moved) return;
      if (tip) tip.hidden = true;
      // сдвиг по времени — без ограничений: можно уйти в любые даты
      const span = drag.view.end - drag.view.start;
      const dt = (e.clientX - drag.x) / cvTrendGeom.plotW * span;
      cvTrendFollow = false; cvTrendHover = null;
      cvTrendView = { start: drag.view.start - dt, end: drag.view.end - dt };
      cvDrawTrend(); cvTrendEnsure(150);
    });
    cv.addEventListener("pointerup", async (e) => {
      const d0 = drag; drag = null;
      if (!d0) return;
      if (d0.moved) { cvTrendEnsure(0); return; }
      // клик без сдвига по пробе → открыть её в окне CV (если кадры ещё не стёрты ротацией)
      const i = nearest(e.clientX); if (i < 0) return;
      const ts = cvTrendData.ts[i];
      try {
        const r = await api("/api/cv/result?" + [cvSerialQ(), "ts=" + encodeURIComponent(ts)].filter(Boolean).join("&"));
        if (r && !r.empty) cvSelectProbe(ts);
        else cvTrendTipShow(i, xOf(i), "кадры этой пробы уже стёрты (хранятся последние 50)");
      } catch (err) { }
    });
    cv.addEventListener("pointerleave", () => { if (!drag) hoverOff(); });
    cv.addEventListener("wheel", (e) => {
      const v = cvTrendView, g = cvTrendGeom; if (!v || !g) return; e.preventDefault();
      const frac = (e.clientX - cv.getBoundingClientRect().left - g.padL) / g.plotW;
      const span = v.end - v.start, center = v.start + Math.max(0, Math.min(1, frac)) * span;
      const k = e.deltaY > 0 ? 1.25 : 0.8;
      const ns = Math.max(CV_MIN_SPAN, Math.min(CV_MAX_SPAN, span * k));
      cvTrendFollow = false; cvTrendHover = null;
      cvTrendView = { start: center - (center - v.start) * ns / span, end: center + (v.end - center) * ns / span };
      cvDrawTrend(); cvTrendEnsure(200);
    }, { passive: false });
    // двойной клик — вся выбранная дата
    cv.addEventListener("dblclick", () => cvTrendSetDay(cvTrendDay || cvDayStr(new Date())));
    window.addEventListener("resize", cvDrawTrend);
  }

  // --- общий рефреш данных CV ---
  async function cvRefresh() {
    try {
      const q = cvSerialQ() ? "?" + cvSerialQ() : "";
      const [last, prev, smp] = await Promise.all([
        api("/api/cv/last" + q).catch(() => null),
        api("/api/cv/prev" + q).catch(() => null),
        api("/api/cv/samples" + (q ? q + "&" : "?") + "limit=20").catch(() => null),
      ]);
      cvLastResult = last && !last.empty ? last : null;
      cvPrevResult = prev && !prev.empty ? prev : null;
      cvSamples = (smp && smp.samples) || [];
      // старая проба не закреплена → окно следует за самой свежей
      if (!cvViewPinned || !cvView) {
        if (!cvView || !cvLastResult || cvView.ts !== cvLastResult.ts) cvGalIdx = 0;
        cvView = cvLastResult; cvViewPinned = false;
      }
      cvRenderScatter();
      cvRenderStrip();
      cvShowFrame();
      cvLoadTrend();
    } catch (e) { /* CV необязателен */ }
  }

  function cvModelRefresh() {
    const el = $("cvModelInfo"); if (!el) return;
    api("/api/cv/model").then((m) => {
      if (!m || m.error) { el.textContent = "сервис офлайн"; return; }
      const names = m.names ? Object.values(m.names).join(",") : "";
      el.textContent = (m.name || "—") + (m.seg ? " · seg" : "") + " · " + (m.device === "cpu" ? "CPU" : "GPU") + (names ? " · [" + names + "]" : "");
    }).catch(() => { el.textContent = "—"; });
  }
  function wireModelUpload() {
    const btn = $("cvModelBtn"), file = $("cvModelFile"), hint = $("cvModelHint");
    if (!btn || !file) return;
    btn.addEventListener("click", () => file.click());
    file.addEventListener("change", async () => {
      const f = file.files && file.files[0]; if (!f) return;
      if (hint) hint.textContent = "загрузка " + f.name + " (" + Math.round(f.size / 1e6) + "МБ)…";
      try {
        const buf = await f.arrayBuffer();
        const r = await fetch("/api/cv/model/upload?name=" + encodeURIComponent(f.name),
          { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: buf });
        const j = await r.json();
        if (j && j.detector) { if (hint) hint.textContent = "загружена: " + (j.detector.name || "ok"); cvModelRefresh(); cvHealth(); }
        else if (hint) hint.textContent = "ошибка: " + ((j && j.error) || "не удалось");
      } catch (e) { if (hint) hint.textContent = "ошибка загрузки"; }
      file.value = "";
      setTimeout(() => { if (hint) hint.textContent = ""; }, 6000);
    });
  }

  // настройки CV: повторяем, пока сервер не отдаст (иначе поля пустые, а «Сохранить пороги» ломает конфиг)
  function cvLoadSettings() {
    api("/api/cv/settings").then((c) => {
      if (!c || !c.groups) throw new Error("настройки CV ещё не готовы");
      cvFillSettings(c); cvSettingsLoaded = true; cvLoadTries = 0; lockSaves(false);
    }).catch(() => {
      cvSettingsLoaded = false; lockSaves(true);
      if (++cvLoadTries < 90) setTimeout(cvLoadSettings, 2000);
    });
  }
  function wireCV() {
    wireWindow();
    wireTeleToggle();
    wireGallery();
    wireTrend();
    wireModelUpload();
    wireHoverTip();
    wireLayers();
    wireVolumeFields();
    cvModelRefresh();
    const en = $("cvEnable");
    if (en) en.addEventListener("change", () => { updateTriggerFields(); cvPostSettings({ enabled: en.checked }).then(cvHealth); });
    const save = $("cvSaveBtn");
    if (save) save.addEventListener("click", async () => {
      if (!cvSettingsLoaded) return;
      await cvPostSettings(cvCollectPatch());
      const h = $("cvSaveHint"); if (h) { h.textContent = "сохранено"; setTimeout(() => (h.textContent = ""), 1500); }
      cvRenderScatter();
    });
    const an = $("cvAnalyzeBtn");
    if (an) an.addEventListener("click", async () => {
      an.disabled = true;
      try { await api("/api/cv/analyze"); } catch (e) { }
      // ждём конца разбора по статусу (а не вслепую): пока running — опрашиваем, максимум 60 с
      const t0 = Date.now();
      const tick = async () => {
        const st = await cvHealth();
        if (st === "running" && Date.now() - t0 < 60000) { setTimeout(tick, 700); return; }
        cvRefresh(); an.disabled = false;
      };
      setTimeout(tick, 300);
    });
    // подтянуть настройки + первичные данные
    cvLoadSettings();
    cvHealth();
    cvRefresh();
    setInterval(() => {
      cvHealth(); if (cvWinOn || isCvPaneVisible() || isTeleCvVisible()) cvRefresh();
      const mi = $("cvModelInfo");                       // модель не подтянулась при старте — пробуем снова
      if (mi && (mi.textContent === "—" || mi.textContent === "сервис офлайн")) cvModelRefresh();
    }, 5000);
  }
  function isCvPaneVisible() {
    const p = document.querySelector('.micro-ppane[data-ppane="cv"]');
    return p && !p.classList.contains("hidden");
  }


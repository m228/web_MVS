// Страница микроскопа: телеметрия платы + авто-цикл + РУЧНОЙ ПУЛЬТ (прямое управление платой).
// Работает поверх эндпоинтов /api/micro/* (см. app.py). Vanilla JS, без зависимостей.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 600;

  // Стадии варки (M.mode) — расшифровка для показа "2 (Набор)". SubMode показываем числом как есть.
  const STAGE_NAMES = {
    1: "Остановлен", 2: "Набор", 3: "Сгущение", 4: "Затравка", 5: "Подкачка",
    6: "Стабилизация", 7: "Рост", 8: "Уваривание", 9: "Готовность", 10: "Выгрузка",
    11: "Пропарка", 12: "Надвыгрузка", 13: "Собрать аппарат", 14: "Термоудар",
    20: "УНВ", 21: "Пауза",
  };
  const stageLabel = (m) => {
    if (m == null || m === "") return "(—)";
    const n = STAGE_NAMES[Number(m)];
    return n ? `${m} (${n})` : String(m);
  };

  let manualOn = false;
  let cfg = null;
  let camSerial = "";
  let manualConfirmShown = false;   // показан ли диалог «варка началась, выйти из ручного?»
  let autostartDone = false;        // автосеквенция «автостарт цикла» уже запущена?

  // авто-доводка мотора «Идти в позицию»: плата за одно нажатие делает лишь ОДИН шаг к цели,
  // поэтому программа сама повторяет команду и следит за позицией, останавливаясь у цели.
  const lastPos = { 1: null, 2: null };
  const autoDrive = { 1: null, 2: null };     // {target, timer, lastSeen, stale}
  const DRIVE_STOP_UM = 20;                    // ближе этого к цели — стоп (мкм)
  const DRIVE_TICK_MS = 700;

  async function api(path, params) {
    const q = params ? "?" + new URLSearchParams(params).toString() : "";
    const res = await fetch(path + q);
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  function sentCmd(label) {
    const el = $("lastCmd");
    if (el) el.textContent = label + " · " + new Date().toLocaleTimeString("ru-RU");
  }

  // вкладка «Цикл»: кнопка «Взять пробу» + сохранение параметров цикла
  function wireCycle() {
    const take = $("takeSampleBtn");
    if (take) take.addEventListener("click", async () => {
      try {
        const r = await api("/api/micro/take_sample");
        sentCmd("Взять пробу: " + (r.status || ""));
        // если проба сняла ручной режим — синхронизируем тумблер в UI
        if (r.was_manual) { const mt = $("manualToggle"); if (mt) mt.checked = false; }
        const h = $("cycHint");
        if (h) h.textContent = r.status === "started" ?
          (r.was_manual ? "ручной снят, цикл запущен" : "цикл запущен") :
          r.status === "busy" ? "цикл уже идёт" : (r.hint || "заблокировано");
      } catch (e) { const h = $("cycHint"); if (h) h.textContent = "ошибка: " + e.message; }
    });
    const skip = $("cycSkipBtn");
    if (skip) skip.addEventListener("click", async () => {
      try { const r = await api("/api/micro/cycle_skip"); sentCmd("Цикл: вперёд → шаг " + (r.mode ?? "")); }
      catch (e) { const h = $("cycHint"); if (h) h.textContent = "ошибка: " + e.message; }
    });
    const reset = $("cycResetBtn");
    if (reset) reset.addEventListener("click", async () => {
      try { await api("/api/micro/cycle_reset"); sentCmd("Цикл: сброс"); const h = $("cycHint"); if (h) h.textContent = "цикл сброшен"; }
      catch (e) { const h = $("cycHint"); if (h) h.textContent = "ошибка: " + e.message; }
    });
    const save = $("pcSave");
    if (save) save.addEventListener("click", async () => {
      const p = {
        retract_pos: $("pcRetract").value, pre_wash_sec: $("pcPreWash").value,
        dwell_sec: $("pcDwell").value, shot_interval_sec: $("pcShotInterval").value,
        pause_sec: $("pcPause").value,
        photo_format: $("pcFormatSw").checked ? "jpg" : "png",
        trigger_mode: $("pcTriggerSw").checked ? "sv" : "time",
        sv_from: $("pcSvFrom").value, sv_to: $("pcSvTo").value,
      };
      try {
        await api("/api/micro/settings", p);
        const h = $("pcHint"); if (h) h.textContent = "сохранено, автомат перезапущен";
        sentCmd("Параметры цикла сохранены");
      } catch (e) { const h = $("pcHint"); if (h) h.textContent = "ошибка: " + e.message; }
    });
    const trig = $("pcTriggerSw");
    if (trig) trig.addEventListener("change", () => {
      updateTriggerFields();
      const mode = trig.checked ? "sv" : "time";
      api("/api/micro/trigger_mode", { mode }).catch(() => {});   // применяется сразу
      sentCmd("Триггер пробы: " + (trig.checked ? "по СВ" : "по времени"));
    });
    const ign = $("ignoreStageToggle");
    if (ign) ign.addEventListener("change", () => {
      api("/api/micro/ignore_stage", { on: ign.checked ? 1 : 0 }).catch(() => {});   // сразу
      sentCmd("Варить без стадии: " + (ign.checked ? "вкл" : "выкл"));
    });
    const fmt = $("pcFormatSw");
    if (fmt) fmt.addEventListener("change", () => {
      const st = $("pcFormatState"); if (st) st.textContent = fmt.checked ? "JPG" : "PNG";
    });
    updateTriggerFields();
  }

  // приглушить неактуальные поля под выбранный триггер (не скрываем — видно, что неактивно):
  // «по времени» -> СВ от/до приглушены; «по СВ» -> пауза приглушена.
  function updateTriggerFields() {
    const sw = $("pcTriggerSw"); if (!sw) return;
    const sv = sw.checked;
    const st = $("pcTriggerState"); if (st) st.textContent = sv ? "по СВ" : "по времени";
    const swLabel = sw.closest(".micro-switch"); if (swLabel) swLabel.classList.toggle("is-sv", sv);
    const fromW = $("pcSvFromWrap"), toW = $("pcSvToWrap"), pauseW = $("pcPauseWrap");
    if (fromW) fromW.classList.toggle("micro-dim", !sv);
    if (toW) toW.classList.toggle("micro-dim", !sv);
    if (pauseW) pauseW.classList.toggle("micro-dim", sv);
  }

  // ---- камера: MVS SDK сам находит камеры Hikrobot ----
  async function autoDiscoverCameras() {
    try {
      const data = await api("/api/cams/detailed");
      if (data && typeof data === "object") {
        const avail = [], all = [];
        for (const [serial, entries] of Object.entries(data)) {
          all.push(serial);
          if (Array.isArray(entries) && entries.some((e) => e && e.available)) avail.push(serial);
        }
        return avail.length ? avail : all;
      }
    } catch (e) { /* нет камер / нет драйвера */ }
    return [];
  }

  // ---- КАМЕРА (нативно): левое окно = чистый видеопоток <img>, кнопки — в тулбаре,
  //      параметры — во вкладке пульта, телеметрия — полосой снизу. Всё через CameraApi. ----
  let camConnected = false;
  let camPhotoOn = false, camVideoOn = false;
  let hwMinute = 3, hwSecOn = 0;   // минута/секунда старта ежечасной промывки (из конфига)
  let camMetricsTimer = null;
  const CAM = () => window.CameraApi;

  function setCamIp(ip) {
    const el = $("camIpInfo");
    if (el) el.textContent = ip ? ip : "";
  }

  // выбрать камеру (серийник/IP): включить кнопки, подтянуть диапазоны параметров. Видео НЕ стартуем.
  // фото/видео появляются ТОЛЬКО после подключения потока (до этого их нельзя жать)
  function showCamSave(show) {
    document.querySelectorAll(".micro-cam-save").forEach((el) => { el.hidden = !show; });
    ["camPhotoBtn", "camVideoBtn", "camPhotoInterval", "camVideoDuration"].forEach((id) => {
      const b = $(id); if (b) b.disabled = !show;
    });
  }

  function setCamSerial(serial) {
    camSerial = serial || "";
    const has = !!serial;
    // запомнить серийник в конфиг микроскопа (для скринов/видео пробы на сервере),
    // если он отличается от уже сохранённого — без перезапуска платы
    if (has && cfg && cfg.camera_serial !== serial) {
      cfg.camera_serial = serial;
      api("/api/micro/camera_serial", { serial }).catch(() => {});
    }
    ["camConnectBtn", "camApplyBtn"].forEach((id) => {
      const b = $(id); if (b) b.disabled = !has;
    });
    showCamSave(false);   // фото/видео скрыты, пока не подключимся
    const info = $("camParamInfo"); if (info) info.textContent = has ? serial : "камера не найдена";
    camStop();  // на всякий: остановить прошлый поток, показать плейсхолдер
    if (has) {
      loadCamParams();
      loadSavedColor();   // подтянуть сохранённую цветокоррекцию в ползунки вкладки «Цвет»
      // IP камеры (разово, до старта потока — контрол-канал не занят стримом)
      api("/api/ip", { serial_number: serial }).then((r) => {
        if (r && r.ip && !/[a-z_]/i.test(String(r.ip))) setCamIp(r.ip);
      }).catch(() => {});
    } else setCamIp("");
  }

  // подтянуть сохранённую цветокоррекцию (worker.color) в ползунки вкладки «Цвет»,
  // чтобы UI показывал те же значения, что уже применены к потоку после перезапуска.
  async function loadSavedColor() {
    if (!camSerial || !$("clrGamma")) return;
    try {
      const r = await api("/api/camera/color", { serial_number: camSerial });
      const c = (r && r.color) || {};
      const setR = (id, v, dp) => { const e = $(id); if (e && v != null) { e.value = v; const l = $(id + "_v"); if (l) l.textContent = dp ? Number(v).toFixed(dp) : String(v); } };
      setR("clrGamma", c.gamma, 2); setR("clrContrast", c.contrast, 2); setR("clrBrightness", c.brightness, 0);
      setR("clrSat", c.saturation, 2); setR("clrHue", c.hue, 0); setR("clrSharp", c.sharpness, 2);
      setR("clrClarity", c.clarity, 1); setR("clrDenoise", c.denoise, 0);
      const wb = c.wb || {};
      if ($("wbAuto")) { $("wbAuto").checked = !!wb.auto; $("wbManual").classList.toggle("is-disabled", !!wb.auto); }
      setR("wbR", wb.r, 2); setR("wbG", wb.g, 2); setR("wbB", wb.b, 2);
      if (Array.isArray(c.ccm) && $("ccmEnable")) {
        $("ccmEnable").checked = true;
        document.querySelectorAll("#ccmGrid input").forEach((inp, i) => { if (c.ccm[i] != null) inp.value = c.ccm[i]; });
      } else if ($("ccmEnable")) { $("ccmEnable").checked = false; }
      if ($("clrPalette")) $("clrPalette").value = c.palette || "";
      if (typeof updateColorDim === "function") updateColorDim();
    } catch (e) { /* цвет не критичен */ }
  }

  async function loadCamParams() {
    if (!camSerial || !CAM()) return;
    try {
      const d = await CAM().getDataLimit(camSerial);
      if (!d) return;
      const setV = (id, v) => { const el = $(id); if (el && v != null) el.value = v; };
      const lim = (id, c) => { const el = $(id); if (el && c) { if (c.min != null) el.min = c.min; if (c.max != null) el.max = c.max; } };
      // компактный диапазон в скобках, чтобы подпись поля влезала в одну строку: (0–2448)
      const txt = (id, c) => { const el = $(id); if (el && c) el.textContent = "(" + (c.min ?? "?") + "–" + (c.max ?? "?") + ")"; };
      setV("camWidth", d.width && d.width.value); lim("camWidth", d.width); txt("camWidthLim", d.width);
      setV("camHeight", d.height && d.height.value); lim("camHeight", d.height); txt("camHeightLim", d.height);
      lim("camOffX", d.offset_x); lim("camOffY", d.offset_y);
      setV("camExpTime", d.exposure_time && d.exposure_time.value); lim("camExpTime", d.exposure_time); txt("camExpLim", d.exposure_time);
      fillOpts("camPixFmt", d.pixel_format);
      fillOpts("camExpAuto", d.exposure_auto);
    } catch (e) { /* камера ещё не запускалась — диапазоны появятся после старта */ }
  }

  // spec: либо массив вариантов, либо объект {value, options} (как отдаёт бэкенд для
  // pixel_format/exposure_auto). Раньше принимался только массив -> список форматов
  // пикселей не заполнялся, оставалось лишь «— как есть —» и кадр не декодировался.
  function fillOpts(id, spec) {
    const sel = $(id);
    if (!sel) return;
    const list = Array.isArray(spec) ? spec : (spec && Array.isArray(spec.options) ? spec.options : null);
    if (!list || !list.length) return;
    // текущее значение камеры (spec.value) — приоритетнее ранее выбранного в UI
    const specVal = (spec && !Array.isArray(spec) && spec.value != null) ? String(spec.value) : "";
    const cur = specVal || sel.value;
    // сохранить ведущий плейсхолдер «— как есть —» (value=""), если он был
    const ph = sel.querySelector('option[value=""]');
    sel.innerHTML = ph ? ph.outerHTML : "";
    list.forEach((o) => {
      const v = (o && typeof o === "object") ? (o.value != null ? o.value : o.name) : o;
      const opt = document.createElement("option");
      opt.value = v; opt.textContent = v; sel.appendChild(opt);
    });
    if (cur) sel.value = cur;
  }

  function camBuildQuery() {
    const q = new URLSearchParams();
    q.set("serial_number", camSerial);
    const put = (name, id) => { const el = $(id); if (el && el.value !== "") q.set(name, el.value); };
    put("width", "camWidth"); put("height", "camHeight");
    put("offset_x", "camOffX"); put("offset_y", "camOffY");
    put("fps", "camFps"); put("exposure_auto", "camExpAuto");
    put("exposure_time", "camExpTime"); put("pixel_format", "camPixFmt");
    return q;
  }

  function camConnect() {
    if (!camSerial) return;
    const img = $("microCamStream"), ph = $("camPlaceholder");
    img.src = "/api/camera/stream?" + camBuildQuery().toString();
    img.hidden = false;
    if (ph) ph.classList.add("hidden");
    camConnected = true;
    const b = $("camConnectBtn"); if (b) { b.textContent = "Остановить"; b.classList.add("toolbar-btn--danger"); b.classList.remove("toolbar-btn--primary"); }
    showCamSave(true);   // поток пошёл — показать фото/видео
    camSetStreamBadge();
    camStartMetrics();
    sentCmd("Камера: подключение");
  }

  function camStop() {
    const img = $("microCamStream"), ph = $("camPlaceholder");
    if (camConnected && camSerial && CAM()) { try { CAM().closeStream(camSerial); } catch (e) {} }
    camConnected = false;
    if (img) { img.hidden = true; img.removeAttribute("src"); }
    if (ph) { ph.textContent = camSerial ? "Нажми «Подключить» вверху — пойдёт видео." : "Камера не найдена. MVS ищет автоматически — проверь подключение/драйвер, либо укажи IP камеры."; ph.classList.remove("hidden"); }
    const b = $("camConnectBtn"); if (b) { b.textContent = "Подключить"; b.classList.add("toolbar-btn--primary"); b.classList.remove("toolbar-btn--danger"); }
    showCamSave(false);   // поток остановлен — фото/видео снова скрыть
    camStopMetrics();
    camSetStreamBadge();
  }

  // Разворот карточки камеры на весь экран + скрытие телеметрии (только в fullscreen)
  function wireCamFullscreen() {
    const card = document.querySelector(".micro-cam-card");
    const fullBtn = $("camFullBtn"), teleBtn = $("camTeleHideBtn");
    if (!card || !fullBtn) return;
    const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
    fullBtn.addEventListener("click", () => {
      if (fsEl()) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      } else {
        (card.requestFullscreen || card.webkitRequestFullscreen).call(card);
      }
    });
    if (teleBtn) teleBtn.addEventListener("click", () => card.classList.toggle("tele-hidden"));
    const onFsChange = () => {
      const on = fsEl() === card;
      fullBtn.classList.toggle("is-full", on);
      fullBtn.title = on ? "Свернуть" : "Развернуть на весь экран";
      if (!on) card.classList.remove("tele-hidden"); // при выходе — телеметрию вернуть
    };
    document.addEventListener("fullscreenchange", onFsChange);
    document.addEventListener("webkitfullscreenchange", onFsChange);
  }

  // Вкладка «Цвет»: цветокоррекция ЖИВЬЁМ через /api/camera/color (worker.color на хосте).
  // WB/гамма/палитра/CCM — всё постобработкой, поток НЕ перезапускается.
  const CCM_PRESETS = {
    neutral: [1, 0, 0, 0, 1, 0, 0, 0, 1],
    warm:    [0.90, 0, 0.10, 0, 1, 0, 0.15, 0, 0.95],   // порядок BGR: чуть больше R, меньше B
    cold:    [1.05, 0, -0.05, 0, 1, 0, -0.10, 0, 0.95],
    boost:   [1.20, -0.10, -0.10, -0.10, 1.20, -0.10, -0.10, -0.10, 1.20],
  };
  function wireColor() {
    if (!$("clrGamma")) return;
    let t = null;
    const debounce = (fn) => { clearTimeout(t); t = setTimeout(fn, 120); };
    const send = (params) => { if (camSerial) api("/api/camera/color", Object.assign({ serial_number: camSerial }, params)).catch(() => {}); };
    const ccmInputs = () => Array.from(document.querySelectorAll("#ccmGrid input"));

    // ползунки тон/свет
    [["clrGamma", "gamma", 2], ["clrContrast", "contrast", 2], ["clrBrightness", "brightness", 0],
     ["clrSat", "saturation", 2], ["clrHue", "hue", 0], ["clrSharp", "sharpness", 2],
     ["clrClarity", "clarity", 1], ["clrDenoise", "denoise", 0]].forEach(([id, key, dp]) => {
      const el = $(id); if (!el) return;
      el.addEventListener("input", () => {
        const v = Number(el.value);
        const lab = $(id + "_v"); if (lab) lab.textContent = dp ? v.toFixed(dp) : String(v);
        debounce(() => send({ [key]: v }));
      });
    });

    // баланс белого
    const wbSend = () => {
      if ($("wbAuto").checked) send({ wb_auto: 1 });
      else send({ wb_auto: 0, wb_r: Number($("wbR").value), wb_g: Number($("wbG").value), wb_b: Number($("wbB").value) });
    };
    $("wbAuto").addEventListener("change", () => { $("wbManual").classList.toggle("is-disabled", $("wbAuto").checked); wbSend(); });
    ["wbR", "wbG", "wbB"].forEach((id) => {
      $(id).addEventListener("input", () => {
        const lab = $(id + "_v"); if (lab) lab.textContent = Number($(id).value).toFixed(2);
        debounce(wbSend);
      });
    });

    // CCM: пресет заполняет матрицу, дальше правится вручную
    const ccmSend = () => send({ ccm: $("ccmEnable").checked ? ccmInputs().map((i) => Number(i.value)).join(",") : "" });
    $("ccmPreset").addEventListener("change", () => {
      const p = CCM_PRESETS[$("ccmPreset").value] || CCM_PRESETS.neutral;
      ccmInputs().forEach((inp) => { inp.value = p[Number(inp.dataset.ccm)]; });
      if ($("ccmEnable").checked) ccmSend();
    });
    $("ccmEnable").addEventListener("change", () => { ccmSend(); updateColorDim(); });
    ccmInputs().forEach((inp) => inp.addEventListener("input", () => { if ($("ccmEnable").checked) debounce(ccmSend); }));

    // псевдоцвет-палитра
    $("clrPalette").addEventListener("change", () => { send({ palette: $("clrPalette").value }); updateColorDim(); });
    updateColorDim();   // начальное приглушение неактивных параметров

    // сброс всего
    $("clrReset").addEventListener("click", () => {
      const set = (id, v, dp) => { const e = $(id); if (e) { e.value = v; const l = $(id + "_v"); if (l) l.textContent = dp ? Number(v).toFixed(dp) : String(v); } };
      set("clrGamma", 1, 2); set("clrContrast", 1, 2); set("clrBrightness", 0, 0); set("clrSat", 1, 2); set("clrHue", 0, 0); set("clrSharp", 0, 2); set("clrClarity", 0, 1); set("clrDenoise", 0, 0);
      set("wbR", 1, 2); set("wbG", 1, 2); set("wbB", 1, 2);
      $("wbAuto").checked = false; $("wbManual").classList.remove("is-disabled");
      $("ccmEnable").checked = false; $("ccmPreset").value = "neutral";
      ccmInputs().forEach((inp) => { inp.value = CCM_PRESETS.neutral[Number(inp.dataset.ccm)]; });
      $("clrPalette").value = "";
      send({ reset: 1 });
      updateColorDim();
    });
  }

  // приглушить неактивные параметры цвета: матрицу CCM (когда CCM выключен) и
  // палитру (когда «нет»). Клики не блокируем — только визуальный намёк неактивности.
  function updateColorDim() {
    const ccmG = $("ccmGrid"), ccmEn = $("ccmEnable"), pal = $("clrPalette");
    if (ccmG && ccmEn) ccmG.classList.toggle("micro-dim", !ccmEn.checked);
    if (pal) pal.classList.toggle("micro-dim", !pal.value);
  }

  // вкладка «Дамп»: показать/скачать/загрузить полный конфиг платы
  function wireDump() {
    if (!$("dumpShow")) return;
    const hint = (t) => { const h = $("dumpHint"); if (h) h.textContent = t || ""; };

    $("dumpShow").addEventListener("click", async () => {
      try {
        const cfg = await api("/api/micro/config_dump");
        const view = $("dumpView");
        if (view) { view.textContent = JSON.stringify(cfg, null, 2); view.hidden = false; }
        hint("текущий конфиг показан ниже");
      } catch (e) { hint("ошибка: " + e.message); }
    });

    $("dumpDownload").addEventListener("click", async () => {
      try {
        const cfg = await api("/api/micro/config_dump");
        const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const now = new Date();
        const p = (n) => String(n).padStart(2, "0");
        const stamp = now.getFullYear() + p(now.getMonth() + 1) + p(now.getDate()) + "_" + p(now.getHours()) + p(now.getMinutes());
        const a = document.createElement("a");
        a.href = url; a.download = "plate_config_" + stamp + ".json";
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        hint("файл скачан: plate_config_" + stamp + ".json");
        sentCmd("Дамп настроек скачан");
      } catch (e) { hint("ошибка: " + e.message); }
    });

    $("dumpLoadBtn").addEventListener("click", () => $("dumpFile").click());
    $("dumpFile").addEventListener("change", async (ev) => {
      const file = ev.target.files && ev.target.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        const data = JSON.parse(text);   // упадёт, если файл не JSON
        if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("файл не похож на конфиг");
        if (!window.confirm("Загрузить настройки из «" + file.name + "»?\n\nТекущие будут перезаписаны (копия сохранится в plate_config.backup.json), автомат перезапустится.")) {
          ev.target.value = ""; return;
        }
        const res = await fetch("/api/micro/config_import", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
        }).then((r) => r.json());
        if (res && res.status === "ok") {
          hint("дамп загружен, автомат перезапущен");
          sentCmd("Дамп настроек загружен: " + file.name);
          const view = $("dumpView"); if (view) { view.textContent = JSON.stringify(data, null, 2); view.hidden = false; }
        } else {
          hint("ошибка импорта: " + ((res && res.error) || "неизвестно"));
        }
      } catch (e) {
        hint("ошибка чтения файла: " + e.message);
      } finally {
        ev.target.value = "";   // чтобы повторный выбор того же файла срабатывал
      }
    });

    wireAutofocus();
  }

  // ---- автофокус М2: запуск, опрос статуса, таблица чёткости ----
  let afTimer = null;
  function wireAutofocus() {
    if (!$("afStart2")) return;
    const hint = (t) => { const h = $("afHint"); if (h) h.textContent = t || ""; };
    $("afStart2").addEventListener("click", async () => {
      const p = { start: $("afStart").value, end: $("afEnd").value, coarse: $("afCoarse").value, fine: $("afFine").value };
      try {
        const r = await api("/api/micro/autofocus", p);
        if (r && r.status === "started") { hint("идёт поиск фокуса…"); sentCmd("Автофокус запущен"); afPoll(); }
        else hint("не запущен: " + ((r && r.error) || "?"));
      } catch (e) { hint("ошибка: " + e.message); }
    });
    $("afStop").addEventListener("click", () => { api("/api/micro/autofocus/stop").catch(() => {}); hint("остановка…"); });
  }

  function afPoll() {
    if (afTimer) clearInterval(afTimer);
    const render = (s) => {
      const hint = $("afHint");
      if (hint) hint.textContent = s.message || (s.running ? "идёт…" : "");
      const wrap = $("afResult"); if (wrap) wrap.hidden = !(s.table && s.table.length);
      const body = $("afTableBody");
      if (body && s.table) {
        const bestPos = s.best ? s.best.pos : null;
        body.innerHTML = s.table.map((r) =>
          "<tr" + (r.pos === bestPos ? ' class="is-best"' : "") + "><td>" + r.pos + "</td><td>" + r.sharp + "</td></tr>").join("");
      }
      const best = $("afBest");
      if (best) best.textContent = s.best ? ("Лучший фокус: " + s.best.pos + " мкм (резкость " + s.best.sharp + ")") : "";
    };
    afTimer = setInterval(async () => {
      try {
        const s = await api("/api/micro/autofocus/status");
        render(s);
        if (!s.running) { clearInterval(afTimer); afTimer = null; }
      } catch (e) { clearInterval(afTimer); afTimer = null; }
    }, 800);
  }

  function camApply() {
    if (!camSerial) return;
    if (camConnected) camConnect();   // перезапуск потока с новыми параметрами
    sentCmd("Камера: применены параметры");
  }

  // единые сеттеры UI фото/видео (кнопка + флажок телеметрии + флаг состояния),
  // чтобы и тумблер, и авто-синк со статусом сервера меняли всё согласованно
  function setPhotoUI(on) {
    camPhotoOn = on;
    const b = $("camPhotoBtn"); if (b) b.classList.toggle("is-on", on);
    const f = $("camPhotoFlag"); if (f) f.classList.toggle("is-on", on);
  }
  function setVideoUI(on) {
    camVideoOn = on;
    const b = $("camVideoBtn"); if (b) b.classList.toggle("is-on", on);
    const f = $("camVideoFlag"); if (f) f.classList.toggle("is-on", on);
  }

  async function camPhotoToggle() {
    if (!camSerial || !CAM()) return;
    let on = camPhotoOn;
    try {
      if (!camPhotoOn) {
        const iv = Math.max(1, parseInt($("camPhotoInterval") && $("camPhotoInterval").value, 10) || 5);
        const fmt = ($("pcFormatSw") && $("pcFormatSw").checked) ? "jpg" : "png";
        await CAM().startPhotoSaving(camSerial, iv, "microscope", fmt); on = true;
      }
      else { await CAM().stopPhotoSaving(camSerial); on = false; }
    } catch (e) { on = false; }
    setPhotoUI(on);
    sentCmd(on ? "Камера: фото ВКЛ" : "Камера: фото выкл");
  }

  async function camVideoToggle() {
    if (!camSerial || !CAM()) return;
    let on = camVideoOn;
    try {
      if (!camVideoOn) {
        const dur = Math.max(0, parseInt($("camVideoDuration") && $("camVideoDuration").value, 10) || 0);
        await CAM().startVideoSaving(camSerial, dur, "microscope"); on = true;
      }
      else { await CAM().stopVideoSaving(camSerial); on = false; }
    } catch (e) { on = false; }
    setVideoUI(on);
    sentCmd(on ? "Камера: запись ВКЛ" : "Камера: запись выкл");
  }

  function camStartMetrics() {
    camStopMetrics();
    camMetricsTimer = setInterval(async () => {
      if (!camConnected || !camSerial || !CAM()) return;
      try { const d = await CAM().getMetrics(camSerial); if (d) camUpdateMetrics(d); } catch (e) {}
      // синк статуса записи/фото с сервера: авто-завершение видео по длительности
      // само гасит кнопку «Видео» и флажок (раньше кнопка залипала «включённой»)
      try {
        const s = await CAM().getVideoPhotoStatus(camSerial);
        if (s) {
          const rec = Number(s.video) === 1;
          if (rec !== camVideoOn) setVideoUI(rec);
          const ph = !!s.photo;
          if (ph !== camPhotoOn) setPhotoUI(ph);
        }
      } catch (e) {}
    }, 1000);
  }
  function camStopMetrics() { if (camMetricsTimer) { clearInterval(camMetricsTimer); camMetricsTimer = null; } }

  function camUpdateMetrics(d) {
    set("camFps_v", d.fps == null ? "—" : Number(d.fps).toFixed(2) + " fps");
    set("camImg_v", d.image_number == null ? "—" : d.image_number);
    set("camRes_v", (d.width == null && d.height == null) ? "—" : (d.width || 0) + " × " + (d.height || 0));
    set("camBw_v", d.bandwidth_mbps == null ? "—" : Number(d.bandwidth_mbps).toFixed(1) + " Mbps");
    set("camErr_v", d.errors == null ? "—" : d.errors);
  }

  function camSetStreamBadge() {
    const b = $("camStreamBadge");
    if (!b) return;
    b.textContent = camConnected ? "поток идёт" : "поток выкл";
    b.className = "micro-plc-badge" + (camConnected ? " is-on" : "");
  }

  function applyCamSerials(serials) {
    const found = $("camFound"), sel = $("camSelect"), ipInp = $("camIp"), ipGo = $("camIpGo");
    sel.hidden = true; ipInp.hidden = true; ipGo.hidden = true;
    if (serials.length === 1) { found.textContent = serials[0]; setCamSerial(serials[0]); return true; }
    if (serials.length > 1) {
      found.textContent = "";
      sel.hidden = false; sel.innerHTML = "";
      serials.forEach((s) => { const o = document.createElement("option"); o.value = s; o.textContent = s; sel.appendChild(o); });
      setCamSerial(serials[0]);
      return true;
    }
    found.textContent = "не найдена —";
    ipInp.hidden = false; ipGo.hidden = false;
    setCamSerial("");
    return false;
  }

  async function discoverWithRetry(attempt) {
    const ok = applyCamSerials(await autoDiscoverCameras());
    if (!ok && attempt < 8) setTimeout(() => discoverWithRetry(attempt + 1), 2500);
  }

  async function initCamera() {
    let cfgSerial = "";
    try {
      cfg = await api("/api/micro/config");
      if (cfg) {
        if (cfg.led_bright != null) { $("ledBright").value = cfg.led_bright; $("ledBrightVal").textContent = cfg.led_bright; }
        if (cfg.led_freq != null && $("ledFreq")) $("ledFreq").value = cfg.led_freq;
        // плата — только для инфо (адрес/порт/unit задаются в plate_config.json)
        const pInfo = $("plateInfo");
        if (pInfo) pInfo.textContent = (cfg.host || "—") + " · " + (cfg.port || 502) + " · unit " + (cfg.unit != null ? cfg.unit : 254);
        if (cfg.camera_ip) setCamIp(cfg.camera_ip);
        if ($("camModeToggle")) {
          const auto = cfg.camera_mode === "auto" || cfg.camera_mode === "trigger";
          $("camModeToggle").checked = auto;
          const st = $("camModeState"); if (st) st.textContent = auto ? "автомат" : "поток";
        }
        if (cfg.hourly_wash && $("hourlyWashToggle")) {
          $("hourlyWashToggle").checked = cfg.hourly_wash.enabled !== false;
          if (cfg.hourly_wash.minute != null) hwMinute = Number(cfg.hourly_wash.minute);
          if (cfg.hourly_wash.sec_on != null) hwSecOn = Number(cfg.hourly_wash.sec_on);
        }
        updateHourlyWashTimer();
        if (cfg.probe_cycle) {
          const pc = cfg.probe_cycle, sv = (id, v) => { const e = $(id); if (e && v != null) e.value = v; };
          sv("pcRetract", pc.retract_pos); sv("pcPreWash", pc.pre_wash_sec);
          sv("pcDwell", pc.dwell_sec); sv("pcShotInterval", pc.shot_interval_sec);
          sv("pcPause", pc.pause_sec); sv("pcSvFrom", pc.sv_from); sv("pcSvTo", pc.sv_to);
          const trSw = $("pcTriggerSw"); if (trSw) trSw.checked = (pc.trigger_mode === "sv");
          const fmSw = $("pcFormatSw"); if (fmSw) fmSw.checked = (pc.photo_format === "jpg");
          const fmSt = $("pcFormatState"); if (fmSt) fmSt.textContent = (pc.photo_format === "jpg") ? "JPG" : "PNG";
          const ignT = $("ignoreStageToggle"); if (ignT) ignT.checked = !!pc.ignore_stage;
          const ifT = $("ignoreFocusToggle"); if (ifT) ifT.checked = pc.ignore_focus !== false;
          updateTriggerFields();
        }
        const ca = $("cycleAutostartToggle"); if (ca) ca.checked = !!cfg.cycle_autostart;
        // фильтр датчика перемещения (1271): галочка + окно сек (серое/disabled при снятой галочке) + масштаб показа
        const sf = cfg.sensor_filter || {};
        const sfT = $("sensorFilterToggle"); if (sfT) sfT.checked = !!sf.enabled;
        const sfS = $("sensorFilterSec");
        if (sfS) { if (sf.avg_sec != null) sfS.value = sf.avg_sec; sfS.disabled = !(sfT && sfT.checked); }
        const sds = $("sensorDisplayScale"); if (sds && cfg.sensor_display_scale != null) sds.value = cfg.sensor_display_scale;
        // довод по абсолютнику (гибридный доезд подвода): галочка + допуски/повторы/пауза
        const fa = cfg.fine_approach || {};
        const faT = $("fineApproachToggle"); if (faT) faT.checked = !!fa.enabled;
        const faSet = (id, v) => { const e = $(id); if (e && v != null) e.value = v; };
        faSet("faCoarse", fa.coarse_tol_um); faSet("faFine", fa.fine_tol_um);
        faSet("faRetry", fa.max_retry); faSet("faPause", fa.pause_sec);
        // автокалибровка нуля М1
        const ac = cfg.autocal || {};
        const acT = $("autocalToggle"); if (acT) acT.checked = !!ac.enabled;
        faSet("acEveryN", ac.every_n); faSet("acLo", ac.sensor_lo);
        faSet("acHi", ac.sensor_hi); faSet("acTimeout", ac.timeout_sec);
        cfgSerial = cfg.camera_serial || "";
      }
    } catch (e) { /* конфиг недоступен */ }

    buildDqGrid();
    buildSvspTable();

    if (cfgSerial) {
      $("camSelect").hidden = true; $("camIp").hidden = true; $("camIpGo").hidden = true;
      $("camFound").textContent = cfgSerial;
      setCamSerial(cfgSerial);
      maybeAutostartCycle();
      return;
    }
    discoverWithRetry(0);
    maybeAutostartCycle();
  }

  // автостарт цикла после перезапуска: подключить камеру (когда найдена), пауза,
  // включить «Автомат». Работает только если галочка cycle_autostart включена.
  function maybeAutostartCycle() {
    if (!cfg || !cfg.cycle_autostart || autostartDone) return;
    autostartDone = true;
    const enableAuto = () => {
      syncManual(false);
      api("/api/micro/manual", { on: 0 }).catch(() => {});
      sentCmd("Автостарт: «Автомат» включён");
    };
    const tryConnect = (n) => {
      if (camConnected) { enableAuto(); return; }
      if (camSerial) { camConnect(); setTimeout(enableAuto, 2000); return; }
      if (n < 20) { setTimeout(() => tryConnect(n + 1), 1000); return; }
      enableAuto();   // камера не нашлась — всё равно включаем автомат (мотор/клапаны пойдут)
    };
    setTimeout(() => tryConnect(0), 1500);
  }

  // ---- DQ-сетка: строим кнопки по меткам из конфига ----
  function buildDqGrid() {
    const grid = $("dqGrid");
    if (!grid) return;
    const all = (cfg && cfg.dq && cfg.dq.labels) || ["трубка", "стекло", "воздух", "вых 4", "вых 5", "вых 6"];
    const labels = all.slice(0, 3);   // только 3 выхода (трубка/стекло/воздух); 4-6 не нужны
    grid.innerHTML = "";
    labels.forEach((lab, bit) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "toolbar-btn toolbar-btn--neutral micro-dq-btn";
      b.dataset.bit = String(bit);
      b.textContent = lab;
      b.addEventListener("click", () => {
        const on = !b.classList.contains("is-on");
        api("/api/micro/dq", { bit, on: on ? 1 : 0 }).then(() => { }).catch(() => { });
        sentCmd((on ? "DQ вкл: " : "DQ выкл: ") + lab);
      });
      grid.appendChild(b);
    });
  }

  // ---- таблица подвода: СВ (Brix) -> зазор (мкм), кривая SP[1..]/SVSP[1..] ----
  function buildSvspTable() {
    const body = $("svspBody");
    if (!body || !cfg) return;
    body.innerHTML = "";
    const SVSP = cfg.SVSP || [], SP = cfg.SP || [], FOCUS = cfg.FOCUS || [];
    for (let i = 1; i < SVSP.length && i < 50; i++) {
      if (Number(SVSP[i]) > 0) addSvspRow(SVSP[i], SP[i], FOCUS[i]);
    }
    if (!body.children.length) addSvspRow("", "", "");
  }

  function addSvspRow(brix, gap, focus) {
    const body = $("svspBody"); if (!body) return;
    const tr = document.createElement("tr");
    tr.innerHTML =
      '<td><input type="number" step="0.1" class="svsp-brix" value="' + (brix === "" ? "" : brix) + '" /></td>' +
      '<td><input type="number" step="10" class="svsp-gap" value="' + (gap === "" ? "" : gap) + '" /></td>' +
      '<td><input type="number" step="10" class="svsp-focus" title="0 = не двигать фокус" value="' + (focus === "" || focus == null ? "" : focus) + '" /></td>' +
      '<td><button type="button" class="svsp-del" title="удалить строку" aria-label="удалить">✕</button></td>';
    tr.querySelector(".svsp-del").addEventListener("click", () => tr.remove());
    body.appendChild(tr);
  }

  async function saveSvsp() {
    const hint = $("svspHint"); if (hint) hint.textContent = "сохраняю…";
    const rows = [...document.querySelectorAll("#svspBody tr")].map((tr) => ({
      brix: parseFloat(tr.querySelector(".svsp-brix").value),
      gap: parseInt(tr.querySelector(".svsp-gap").value, 10),
      focus: parseInt(tr.querySelector(".svsp-focus").value, 10),
    })).filter((r) => !isNaN(r.brix) && !isNaN(r.gap) && r.brix > 0)
       .sort((a, b) => a.brix - b.brix);   // FSM требует пороги СВ по возрастанию
    const SP = (cfg.SP || []).slice(), SVSP = (cfg.SVSP || []).slice(), FOCUS = (cfg.FOCUS || []).slice();
    for (let i = 1; i < 50; i++) { SVSP[i] = 0; FOCUS[i] = 0; }   // очистить кривую (SP[0]/[50] сохраняем)
    rows.forEach((r, k) => { const i = k + 1; SVSP[i] = r.brix; SP[i] = r.gap; FOCUS[i] = isNaN(r.focus) ? 0 : r.focus; });
    try {
      await api("/api/micro/recipe", { sp: JSON.stringify(SP), svsp: JSON.stringify(SVSP), focus: JSON.stringify(FOCUS) });
      cfg.SP = SP; cfg.SVSP = SVSP; cfg.FOCUS = FOCUS;
      if (hint) { hint.textContent = "сохранено ✓ (" + rows.length + " строк)"; setTimeout(() => { hint.textContent = ""; }, 2500); }
    } catch (e) { if (hint) hint.textContent = "ошибка сохранения"; }
  }

  // ---- связь / формат ----
  function setConn(state) {
    const wrap = $("microConnWrap"), el = $("microConn");
    if (!wrap) return;
    wrap.classList.remove("is-on", "is-warn", "is-off");
    if (state.connected) { wrap.classList.add("is-on"); if (el) el.textContent = "плата: связь"; }
    else if (state.reconnecting) { wrap.classList.add("is-warn"); if (el) el.textContent = "плата: переподключение…"; }
    else { wrap.classList.add("is-off"); if (el) el.textContent = "плата: нет связи"; }
  }

  // индикатор связи с ПЛК аппарата (источник данных варки по Modbus)
  function setPlcConn(plc) {
    const wrap = $("plcConnWrap"), el = $("plcConn");
    if (!wrap) return;
    wrap.classList.remove("is-on", "is-warn", "is-off");
    if (!plc || !plc.enabled) { wrap.classList.add("is-off"); if (el) el.textContent = "ПЛК: выкл"; }
    else if (plc.connected) { wrap.classList.add("is-on"); if (el) el.textContent = "ПЛК: обмен"; }
    else { wrap.classList.add("is-warn"); if (el) el.textContent = "ПЛК: нет связи"; }
  }

  const num = (v) => (v == null ? "—" : v);
  const um = (v) => (v == null ? "—" : v + " мкм");
  const pair = (a, b) => ((a == null && b == null) ? "—" : (num(a) + " / " + num(b)));
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  const HW = { 19: "hw1.3", 32: "hw2.0" };

  function bits6(v) { return (v == null) ? "—" : "0b" + Number(v).toString(2).padStart(6, "0"); }
  function hex(v) { return (v == null) ? "—" : "0x" + Number(v).toString(16).padStart(4, "0"); }
  function upt(s) {
    if (s == null) return "—";
    s = Number(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? (h + "ч " + m + "м") : (m + "м " + (s % 60) + "с");
  }

  // ---- опрос ----
  async function poll() {
    try {
      const d = await api("/api/micro/telemetry");
      const t = d.telemetry || {}, f = d.fsm || {}, e = d.ext || {};
      setConn(d.connection || {});

      // подтверждение перехода в Автомат: варка вошла в стадию 3, а мы в ручном
      if (f.manual_confirm && !manualConfirmShown) {
        manualConfirmShown = true;
        const yes = confirm("Началась варка (стадия " + (f.stage != null ? f.stage : 3) +
          "). Выйти из ручного режима и запустить авто-цикл?");
        api("/api/micro/" + (yes ? "confirm_auto" : "decline_auto")).then(() => {
          if (yes) { const mt = $("manualToggle"); if (mt) mt.checked = false; }
        }).catch(() => {});
      }
      if (!f.manual_confirm) manualConfirmShown = false;

      // позиции (сверху и в пульте)
      set("tPos1", um(t.pos1)); set("tPos2", um(t.pos2));
      lastPos[1] = e.m1_pos != null ? e.m1_pos : t.pos1;
      lastPos[2] = e.m2_pos != null ? e.m2_pos : t.pos2;
      set("m1Pos", um(lastPos[1]));
      set("m2Pos", um(lastPos[2]));
      // датчик перемещения: показ с множителем sensor_display_scale (свести с позицией; логику не трогает)
      const dScale = (cfg && cfg.sensor_display_scale != null) ? Number(cfg.sensor_display_scale) : 1;
      const sensorShown = e.sensor == null ? null : Math.round(e.sensor * dScale);
      set("m1Sensor", um(sensorShown)); set("m1Steps", num(e.m1_steps));
      set("m2Steps", num(e.m2_steps)); set("m2State", num(e.m2_state));
      set("tSensor", um(sensorShown));

      // питание / термо
      set("tTemp", t.temp == null ? "—" : t.temp + " °C");
      set("tU12v", t.u12v == null ? "—" : (t.u12v / 1000).toFixed(2) + " В");
      set("tFanPair", pair(e.fan1, e.fan2));

      // входы/выходы
      const diTxt = e.di != null ? bits6(e.di) : hex(t.di), dqTxt = bits6(e.dq);
      set("tDi", diTxt); set("tDq", dqTxt);
      set("tDi2", diTxt); set("tDq2", dqTxt);   // дубль во вкладке DQ (не всегда смотришь вниз)

      // автомат
      set("tSv", f.sv == null ? "—" : Number(f.sv).toFixed(2));
      set("microStep", f.step || "—");
      set("microMode", f.mode == null ? "—" : f.mode);
      set("valveTube", f.valve_tube ? "открыт" : "закрыт");
      set("valveGlass", f.valve_glass ? "открыт" : "закрыт");

      // вкладка «Цикл»: живой шаг + таймеры
      const cb = $("cycStepBadge"); if (cb) cb.textContent = f.step || "—";
      set("cycLabel", f.label || "—");
      // авария (подгон не сошёлся и т.п.): показать кнопку сброса, подсветить статус
      const faCF = $("faClearFault"); if (faCF) faCF.hidden = !f.fault;
      const cl = $("cycLabel"); if (cl) cl.classList.toggle("is-fault", !!f.fault);

      // автокалибровка: счётчик варок + статус (простой/поиск 0/жду датчик)
      const ac = f.autocal || {};
      set("acCount", ac.count == null ? "—" : ac.count);
      set("acEveryNShow", ac.every_n == null ? "—" : ac.every_n);
      const acStatusTxt = ac.active
        ? (ac.phase === "find_zero" ? "поиск 0" : ac.phase === "wait_sensor" ? "жду датчик в зоне нуля" : "идёт")
        : (f.fault ? "авария" : "ждёт пропарки");
      set("acStatus", acStatusTxt);
      set("cycSv", f.sv == null ? "—" : Number(f.sv).toFixed(1));
      // СВ рядом с видео камеры (телеметрия камеры) — чтобы было видно при просмотре потока
      set("camSv_v", f.sv == null ? "—" : Number(f.sv).toFixed(1));
      // датчик абсолютный (рядом с СВ) + стадия цикла (подвод/отвод/проба); в пробе — сколько с осталось.
      // всё под видео = видно в полноэкранном, не выходя из него.
      set("camSensor_v", sensorShown == null ? "—" : sensorShown);
      let camStepTxt = f.step || "—";
      if (f.mode === 23 && f.dwell_left_s != null) camStepTxt += " · " + f.dwell_left_s + " с";
      set("camStep_v", camStepTxt);
      // до следующей пробы — очень коротко (плитка под видео): авария/ручной/идёт/таймер/жду СВ
      let camNext;
      if (f.fault) camNext = "АВАРИЯ";
      else if (f.manual) camNext = "ручн.";
      else if (f.mode !== 0) camNext = "идёт";
      else if (!f.cyclic) camNext = "авто выкл";
      else if (!f.stage_ok) camNext = "нет варки";
      else {
        const lab = f.label || "";
        const mm = lab.match(/(\d+)\s*с/);
        camNext = /жду СВ|жду ±/.test(lab) ? "жду СВ" : (mm ? mm[1] + " с" : "—");
      }
      set("camNext_v", camNext);

      set("cycTarget", f.target == null ? "—" : f.target + " мкм");
      set("cycPos", um(t.pos1));
      // абсолютный датчик 1271 (по нему идёт доезд) — тем же масштабом показа, что и в телеметрии
      set("cycSensor", um(t.pos1_ai == null ? null : Math.round(t.pos1_ai * dScale)));
      set("cycTube", f.valve_tube ? "открыт" : "закрыт");
      set("cycGlass", f.valve_glass ? "открыт" : "закрыт");
      set("cycDwell", f.dwell_left_s == null ? "—" : f.dwell_left_s + " с");

      // DEBUG live-строка (обратная связь ручного ввода СВ): шаг/СВ/зазор/позиция
      set("dbgStep", f.step == null ? "—" : f.step + " (" + f.mode + ")");
      set("dbgSv", f.sv == null ? "—" : Number(f.sv).toFixed(2));
      set("dbgSp", f.m1_sp == null ? "—" : um(f.m1_sp));
      set("dbgPos", um(t.pos1));

      // система
      set("tSerial", num(e.serial));
      set("tUptime", upt(e.uptime_s));

      // данные с контроллера (ПЛК) — источник аппарата по Modbus (sv_source). Пока нет данных -> «(—)».
      const plc = d.plc || {}, pv = plc.values || {};
      setPlcConn(plc);
      set("pcSv", pv.sv == null ? "(—)" : Number(pv.sv).toFixed(2));
      set("pcStage", stageLabel(pv.stage));
      set("pcTemp", pv.temp_app == null ? "(—)" : pv.temp_app + " °C");
      // разрежение теперь в press_top (с фолбэком на старое поле vacuum) — как на мнемосхеме
      { const vac = pv.press_top != null ? pv.press_top : pv.vacuum;
        set("pcVacuum", vac == null ? "(—)" : vac); }
      const pb = $("plcBadge");
      if (pb) {
        if (!plc.enabled) { pb.textContent = "выключено"; pb.className = "micro-plc-badge"; }
        else if (plc.connected) { pb.textContent = "есть связь"; pb.className = "micro-plc-badge is-on"; }
        else { pb.textContent = "нет связи"; pb.className = "micro-plc-badge is-off"; }
      }

      // мнемосхема вакуум-аппарата (те же PLC-данные)
      const bar = (v) => (v == null ? "—" : Number(v).toFixed(3) + " бар");
      set("maBrix", pv.sv == null ? "—" : Number(pv.sv).toFixed(1));
      set("maTemp", pv.temp_app == null ? "—" : pv.temp_app + " °C");
      set("maPtop", bar(pv.press_top != null ? pv.press_top : pv.vacuum));
      set("maPbot", bar(pv.press_bot));
      set("maCurrent", pv.current == null ? "—" : Number(pv.current).toFixed(1) + " A");
      set("maLevel", pv.level == null ? "—" : Number(pv.level).toFixed(2) + " %");
      const fill = document.getElementById("maFill");
      if (fill) {
        const lv = pv.level == null ? 0 : Math.max(0, Math.min(100, Number(pv.level)));
        const H = 176, base = 206, h = Math.max(4, H * lv / 100);  // тело 30..206
        fill.setAttribute("y", base - h);
        fill.setAttribute("height", h);
      }

      // бейджи моторов (разрешение/направление)
      motorBadge("1", e.m1_enable, e.m1_dir);
      motorBadge("2", e.m2_enable, e.m2_dir);

      // DQ-кнопки: подсветка активных битов
      if (e.dq != null) {
        document.querySelectorAll(".micro-dq-btn").forEach((b) => {
          const on = (Number(e.dq) >> Number(b.dataset.bit)) & 1;
          b.classList.toggle("is-on", !!on);
        });
      }

      // вкладки: настройки моторов + охлаждение (read-only)
      set("sSpeed", pair(e.m1_speed, e.m2_speed));
      set("sMinSpeed", pair(e.m1_minspeed, e.m2_minspeed));
      set("sAccel", pair(e.m1_accel, e.m2_accel));
      set("sMaxTravel", pair(e.m1_maxtravel, e.m2_maxtravel));
      set("sDivider", dividerLabel(e.step_divider));
      set("sStepsRev", pair(e.m1_steps_rev, e.m2_steps_rev));
      set("sDistRev", pair(e.m1_dist_rev, e.m2_dist_rev));
      set("sKmm", pair(e.m1_k_steps_mm, e.m2_k_steps_mm));
      set("sStopSensor", num(e.m1_stop_sensor));
      set("sSlipLimit", num(e.m1_slip_limit));
      set("cTemp", t.temp == null ? "—" : t.temp + " °C");
      set("cFan", pair(e.fan1, e.fan2));
      set("cFanTh", pair(e.fan_on_temp, e.fan_off_temp));
      set("cAirTh", pair(e.air_on_temp, e.air_off_temp));
      set("cCamTh", pair(e.cam_on_temp, e.cam_off_temp));

      // ручной режим (синхронизируем UI с состоянием автомата)
      if (!!f.manual !== manualOn) syncManual(!!f.manual);
    } catch (e) {
      setConn({ connected: false, reconnecting: false });
    }
  }

  function dividerLabel(v) {
    if (v == null) return "—";
    return ({ 0: "1", 1: "1/2", 2: "1/4", 3: "1/8", 7: "1/16" })[v] || v;
  }

  function motorBadge(m, en, dir) {
    const enB = document.querySelector('.micro-badge[data-en="' + m + '"]');
    const dirB = document.querySelector('.micro-badge[data-dir="' + m + '"]');
    if (enB && en != null) { enB.textContent = en ? "разрешён" : "выключен"; enB.classList.toggle("is-on", !!en); }
    if (dirB && dir != null) { dirB.textContent = dir ? "вперёд" : "назад"; dirB.classList.toggle("is-rev", !dir); }
  }

  // ---- ручной режим ----
  function syncManual(on) {
    manualOn = on;
    $("manualToggle").checked = on;
    $("manualState").textContent = on ? "Ручной режим" : "Автомат";
    const sw = document.querySelector(".micro-switch--manual");
    if (sw) sw.classList.toggle("is-manual", on);
    $("microPult").classList.toggle("is-locked", !on);   // гейт панелей М1/М2/LED/DQ
  }

  // ---- кнопки ----
  function wire() {
    $("btnReload").addEventListener("click", async () => { await api("/api/micro/reload"); initCamera(); });

    const m1ssSave = $("m1StopSensorSave");
    if (m1ssSave) m1ssSave.addEventListener("click", async () => {
      const h = $("m1StopSensorHint");
      try {
        await api("/api/micro/m1_stop_sensor", { value: $("m1StopSensorInp").value });
        if (h) { h.textContent = "записано в плату"; setTimeout(() => { h.textContent = ""; }, 2500); }
        sentCmd("Стоп М1 по датчику: " + $("m1StopSensorInp").value + " мкм");
      } catch (e) { if (h) h.textContent = "ошибка: " + e.message; }
    });

    // фильтр датчика перемещения (1271): галочка гасит/зажигает поле «сек»; «Записать» шлёт оба + масштаб
    const sfT = $("sensorFilterToggle"), sfS = $("sensorFilterSec"), sdsInp = $("sensorDisplayScale");
    if (sfT) sfT.addEventListener("change", () => {
      if (sfS) sfS.disabled = !sfT.checked;
      api("/api/micro/sensor_filter", { enabled: sfT.checked ? 1 : 0 }).catch(() => {});
      if (cfg) (cfg.sensor_filter = cfg.sensor_filter || {}).enabled = sfT.checked;
      sentCmd("Фильтр датчика: " + (sfT.checked ? "вкл" : "выкл"));
    });
    const sfSave = $("sensorFilterSave");
    if (sfSave) sfSave.addEventListener("click", async () => {
      const h = $("sensorFilterHint");
      try {
        await api("/api/micro/sensor_filter", { enabled: (sfT && sfT.checked) ? 1 : 0, avg_sec: sfS ? sfS.value : 2 });
        if (sdsInp) { await api("/api/micro/sensor_display_scale", { value: sdsInp.value }); if (cfg) cfg.sensor_display_scale = Number(sdsInp.value); }
        if (cfg) { cfg.sensor_filter = cfg.sensor_filter || {}; cfg.sensor_filter.enabled = !!(sfT && sfT.checked); if (sfS) cfg.sensor_filter.avg_sec = Number(sfS.value); }
        if (h) { h.textContent = "сохранено"; setTimeout(() => { h.textContent = ""; }, 2500); }
        sentCmd("Фильтр датчика сохранён");
      } catch (e) { if (h) h.textContent = "ошибка: " + e.message; }
    });

    // довод по абсолютнику: галочка + поля (каждое по change шлёт настройку) + сброс аварии
    const faT = $("fineApproachToggle");
    if (faT) faT.addEventListener("change", () => {
      api("/api/micro/fine_approach", { enabled: faT.checked ? 1 : 0 }).catch(() => {});
      if (cfg) (cfg.fine_approach = cfg.fine_approach || {}).enabled = faT.checked;
      sentCmd("Довод по абсолютнику: " + (faT.checked ? "вкл" : "выкл"));
    });
    [["faCoarse", "coarse_tol_um"], ["faFine", "fine_tol_um"], ["faRetry", "max_retry"], ["faPause", "pause_sec"]]
      .forEach(([id, key]) => {
        const el = $(id);
        if (el) el.addEventListener("change", () => {
          const p = {}; p[key] = el.value;
          api("/api/micro/fine_approach", p).catch(() => {});
          if (cfg) (cfg.fine_approach = cfg.fine_approach || {})[key] = Number(el.value);
        });
      });
    const faCF = $("faClearFault");
    if (faCF) faCF.addEventListener("click", () => {
      api("/api/micro/clear_fault").then(() => sentCmd("Авария сброшена")).catch(() => {});
    });

    // автокалибровка нуля М1: галочка + поля + ручной старт + сброс счётчика
    const acT = $("autocalToggle");
    if (acT) acT.addEventListener("change", () => {
      api("/api/micro/autocal", { enabled: acT.checked ? 1 : 0 }).catch(() => {});
      if (cfg) (cfg.autocal = cfg.autocal || {}).enabled = acT.checked;
      sentCmd("Автокалибровка: " + (acT.checked ? "вкл" : "выкл"));
    });
    [["acEveryN", "every_n"], ["acLo", "sensor_lo"], ["acHi", "sensor_hi"], ["acTimeout", "timeout_sec"]]
      .forEach(([id, key]) => {
        const el = $(id);
        if (el) el.addEventListener("change", () => {
          const p = {}; p[key] = el.value;
          api("/api/micro/autocal", p).catch(() => {});
          if (cfg) (cfg.autocal = cfg.autocal || {})[key] = Number(el.value);
        });
      });
    const acStart = $("acStart");
    if (acStart) acStart.addEventListener("click", () => {
      api("/api/micro/autocal/start").then((r) => {
        const h = $("acHint");
        if (h) { h.textContent = r && r.status === "started" ? "калибровка запущена" : "занято (" + (r && r.status) + ")"; setTimeout(() => { h.textContent = ""; }, 3000); }
      }).catch(() => {});
    });
    const acReset = $("acReset");
    if (acReset) acReset.addEventListener("click", () => {
      api("/api/micro/autocal/reset").then(() => sentCmd("Счётчик варок сброшен")).catch(() => {});
    });

    const caT = $("cycleAutostartToggle");
    if (caT) caT.addEventListener("change", () => {
      api("/api/micro/cycle_autostart", { on: caT.checked ? 1 : 0 }).catch(() => {});
      if (cfg) cfg.cycle_autostart = caT.checked;
      sentCmd("Автостарт цикла: " + (caT.checked ? "вкл" : "выкл"));
    });

    // LED — включение подразумевается яркостью (>0 = вкл), отдельной кнопки нет
    const led = $("ledBright");
    led.addEventListener("input", () => { $("ledBrightVal").textContent = led.value; });
    led.addEventListener("change", () => api("/api/micro/led", { bright: led.value, on: Number(led.value) > 0 ? 1 : 0 }));
    $("ledFreq").addEventListener("change", () => api("/api/micro/led", { freq: $("ledFreq").value }));

    wireCycle();

    // камера (нативно): видео слева, кнопки тут, параметры во вкладке, телеметрия снизу
    $("camSelect").addEventListener("change", () => setCamSerial($("camSelect").value));
    $("camIpGo").addEventListener("click", () => { const ip = $("camIp").value.trim(); if (ip) setCamSerial(ip); });
    $("camConnectBtn").addEventListener("click", () => { camConnected ? camStop() : camConnect(); });
    $("camPhotoBtn").addEventListener("click", camPhotoToggle);
    $("camVideoBtn").addEventListener("click", camVideoToggle);
    $("camApplyBtn").addEventListener("click", camApply);
    const camImg = $("microCamStream");
    if (camImg) camImg.addEventListener("error", () => { if (camConnected) camStop(); });
    wireCamFullscreen();
    wireColor();
    wireDump();

    // таблица подвода СВ->зазор (вкладка «СВ/МКМ»)
    if ($("svspAdd")) $("svspAdd").addEventListener("click", () => addSvspRow("", ""));
    if ($("svspSave")) $("svspSave").addEventListener("click", saveSvsp);
    const ifT = $("ignoreFocusToggle");
    if (ifT) ifT.addEventListener("change", () => {
      api("/api/micro/ignore_focus", { on: ifT.checked ? 1 : 0 }).catch(() => {});
      sentCmd("Фокус по таблице: " + (ifT.checked ? "не использовать" : "использовать"));
    });

    // DEBUG: ручной ввод СВ/стадии для отладки цикла (убрать после отладки)
    const svOv = $("svOverrideToggle");
    if (svOv) svOv.addEventListener("change", async () => {
      try {
        await api("/api/micro/sv_override", { on: svOv.checked ? 1 : 0 });
        const h = $("svDebugHint");
        if (h) h.textContent = svOv.checked ? "ПЛК перехвачен — ручной СВ держится" : "ПЛК ведёт СВ";
        sentCmd("DEBUG перехват ПЛК: " + (svOv.checked ? "вкл" : "выкл"));
      } catch (e) { const h = $("svDebugHint"); if (h) h.textContent = "ошибка: " + e.message; }
    });
    if ($("svManualApply")) $("svManualApply").addEventListener("click", async () => {
      const sv = $("svManualInp").value, stage = $("stageManualInp").value;
      try {
        await api("/api/micro/sv", { value: sv });
        await api("/api/micro/stage", { value: stage });
        const h = $("svDebugHint"); if (h) h.textContent = "применено: СВ " + sv + ", стадия " + stage;
        sentCmd("DEBUG СВ " + sv + " / стадия " + stage);
      } catch (e) { const h = $("svDebugHint"); if (h) h.textContent = "ошибка: " + e.message; }
    });
    // DEBUG: запустить цикл немедленно (cmd=100 -> отвод SP[0] -> подвод по СВ)
    if ($("svCycleNow")) $("svCycleNow").addEventListener("click", async () => {
      try {
        await api("/api/micro/command", { cmd: 100 });
        const h = $("svDebugHint"); if (h) h.textContent = "цикл запущен — смотри «Шаг» ниже (20→21→22→23→24)";
        sentCmd("DEBUG запуск цикла (cmd 100)");
      } catch (e) { const h = $("svDebugHint"); if (h) h.textContent = "ошибка: " + e.message; }
    });

    // режим съёмки камеры — галочка (слева поток / справа автомат)
    const camModeT = $("camModeToggle");
    if (camModeT) camModeT.addEventListener("change", () => {
      const mode = camModeT.checked ? "auto" : "stream";
      const st = $("camModeState"); if (st) st.textContent = camModeT.checked ? "автомат" : "поток";
      api("/api/micro/settings", { camera_mode: mode }).catch(() => {});
      sentCmd("Камера: режим " + (camModeT.checked ? "автомат" : "поток"));
    });

    // ежечасная промывка (всегда доступна; идёт в простое раз в час)
    const hwT = $("hourlyWashToggle");
    if (hwT) hwT.addEventListener("change", () => {
      api("/api/micro/settings", { hourly_wash: hwT.checked ? 1 : 0 }).catch(() => {});
      sentCmd("Ежечасная промывка: " + (hwT.checked ? "вкл" : "выкл"));
      updateHourlyWashTimer();
    });

    // ручной режим
    $("manualToggle").addEventListener("change", () => {
      const on = $("manualToggle").checked;
      syncManual(on);
      api("/api/micro/manual", { on: on ? 1 : 0 }).then(() => { }).catch(() => { });
      sentCmd(on ? "Ручной режим ВКЛ" : "Ручной режим выкл");
    });

    // команды моторов (делегирование по [data-op][data-m])
    document.querySelectorAll(".micro-motor [data-op]").forEach((b) => {
      b.addEventListener("click", () => runMotorOp(b.dataset.m, b.dataset.op));
    });
    // Enter в поле ввода = нажать соответствующую кнопку
    [["1", "goto"], ["1", "steps"], ["2", "goto"], ["2", "steps"]].forEach(([m, op]) => {
      const inp = $("m" + m + (op === "goto" ? "GotoInp" : "StepsInp"));
      if (inp) inp.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); runMotorOp(m, op); } });
    });
    // бейджи разрешения/направления
    document.querySelectorAll(".micro-badge[data-en]").forEach((b) => {
      b.addEventListener("click", () => {
        const on = !b.classList.contains("is-on");
        api("/api/micro/motor", { m: b.dataset.en, op: on ? "enable" : "disable" });
        sentCmd("М" + b.dataset.en + (on ? " разрешён" : " выключен"));
      });
    });
    document.querySelectorAll(".micro-badge[data-dir]").forEach((b) => {
      b.addEventListener("click", () => {
        const fwd = b.classList.contains("is-rev");   // сейчас назад -> станет вперёд
        api("/api/micro/motor", { m: b.dataset.dir, op: fwd ? "dir_fwd" : "dir_back" });
        sentCmd("М" + b.dataset.dir + " направление " + (fwd ? "вперёд" : "назад"));
      });
    });

    // иконки-вкладки пульта (М1/М2/LED/DQ/охлаждение/настройки/камера)
    document.querySelectorAll(".micro-ptab").forEach((tab) => {
      tab.addEventListener("click", () => {
        const key = tab.dataset.ptab;
        document.querySelectorAll(".micro-ptab").forEach((x) => x.classList.toggle("is-active", x === tab));
        document.querySelectorAll(".micro-ppane").forEach((p) => p.classList.toggle("hidden", p.dataset.ppane !== key));
      });
    });
  }

  const OP_LABEL = {
    goto: "Идти в позицию", steps: "Выполнить шаги", shift: "Сдвинуть",
    home_start: "В начало", home_end: "В конец", find_zero: "Поиск 0",
    set_zero: "Установить 0", stop: "СТОП",
  };

  function sendMotor(m, op, value) {
    const params = { m, op };
    if (value != null) params.value = value;
    return api("/api/micro/motor", params).then((r) => {
      if (r && r.error) sentCmd("⚠ " + (OP_LABEL[op] || op) + ": " + r.error);
      return r;
    }).catch(() => { });
  }

  function stopAutoDrive(m) {
    const st = autoDrive[m];
    if (st && st.timer) clearInterval(st.timer);
    autoDrive[m] = null;
  }

  // авто-доводка: повторяем goto к цели, пока |позиция-цель| не станет < DRIVE_STOP_UM
  function startAutoDrive(m, target) {
    stopAutoDrive(m);
    sendMotor(m, "goto", target);   // первый импульс сразу
    const st = { target, lastSeen: lastPos[m], stale: 0, timer: null };
    autoDrive[m] = st;
    st.timer = setInterval(() => {
      const pos = lastPos[m];
      if (pos != null) {
        if (Math.abs(pos - target) < DRIVE_STOP_UM) {   // доехали к цели
          stopAutoDrive(m); sentCmd("М" + m + " · доведён к " + target); return;
        }
        if (st.lastSeen != null && Math.abs(pos - st.lastSeen) < 1) {
          if (++st.stale >= 6) {   // ~4 c без движения -> считаем, что дальше не идёт
            stopAutoDrive(m); sentCmd("М" + m + " · остановлен (нет движения)"); return;
          }
        } else st.stale = 0;
        st.lastSeen = pos;
      }
      sendMotor(m, "goto", target);   // следующий импульс к цели
    }, DRIVE_TICK_MS);
  }

  function runMotorOp(m, op) {
    let value = null;
    if (op === "goto") value = $("m" + m + "GotoInp").value;
    else if (op === "steps") value = $("m" + m + "StepsInp").value;

    if (op === "stop") stopAutoDrive(m);       // ручной СТОП гасит и авто-доводку

    // подтверждение убрано: у моторов безопасный ход по всему диапазону (спец. проход, упор не бьёт)

    if (op === "goto" && value !== "" && value != null) {   // авто-доводка вместо одиночного импульса
      startAutoDrive(m, Number(value));
      sentCmd("М" + m + " · авто-доводка → " + value);
      return;
    }

    sendMotor(m, op, value);
    sentCmd("М" + m + " · " + (OP_LABEL[op] || op) + (value != null ? " " + value : ""));
  }

  // обратный отсчёт до следующей ежечасной промывки (раз в час на минуте hwMinute:hwSecOn)
  function updateHourlyWashTimer() {
    const el = $("hourlyWashLeft"); if (!el) return;
    const hwT = $("hourlyWashToggle");
    if (hwT && !hwT.checked) { el.textContent = "выкл"; return; }
    const now = new Date();
    const next = new Date(now);
    next.setMinutes(hwMinute, hwSecOn, 0);
    if (next <= now) next.setHours(next.getHours() + 1);   // уже прошло в этом часу -> следующий час
    const s = Math.max(0, Math.round((next - now) / 1000));
    el.textContent = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    syncManual(false);
    initCamera();
    poll();
    setInterval(poll, POLL_MS);
    updateHourlyWashTimer();
    setInterval(updateHourlyWashTimer, 1000);
  });
})();

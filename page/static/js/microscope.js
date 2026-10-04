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
      if (!cfgLoaded) { loadHint("настройки ещё не загружены — подожди"); return; }
      const p = {
        retract_pos: $("pcRetract").value, pre_wash_sec: $("pcPreWash").value,
        dwell_sec: $("pcDwell").value, shot_interval_sec: $("pcShotInterval").value,
        pause_sec: $("pcPause").value,
        photo_format: $("pcFormatSw").checked ? "jpg" : "png",
        trigger_mode: pcTrigMode, arrive_sensor: pcArriveMode,
        sv_from: $("pcSvFrom").value, sv_to: $("pcSvTo").value,
      };
      try {
        await cvPostCycleFields();          // кадров на пробу / пауза CV — до перезапуска автомата
        await api("/api/micro/settings", p);
        const h = $("pcHint"); if (h) h.textContent = "сохранено, автомат перезапущен";
        sentCmd("Параметры цикла сохранены");
      } catch (e) { const h = $("pcHint"); if (h) h.textContent = "ошибка: " + e.message; }
    });
    document.querySelectorAll("#pcTrigger .micro-trig__btn").forEach((b) => {
      b.addEventListener("click", () => {
        const mode = b.dataset.trig;
        setTrigMode(mode);
        api("/api/micro/trigger_mode", { mode }).catch(() => {});   // применяется сразу
        sentCmd("Триггер пробы: " + TRIG_NAMES[mode]);
      });
    });
    document.querySelectorAll("#pcArrive .micro-trig__btn").forEach((b) => {
      b.addEventListener("click", () => {
        const mode = b.dataset.arr;
        setArriveMode(mode);
        api("/api/micro/arrive_sensor", { mode }).catch(() => {});   // применяется сразу
        sentCmd("Датчик прихода цикла: " + ARRIVE_NAMES[mode]);
      });
    });
    // кадров на пробу / пауза после разбора — настройки CV, применяются сразу (без перезапуска)
    ["pcCvFrames", "pcCvGap"].forEach((id) => {
      const e = $(id); if (e) e.addEventListener("change", () => { cvPostCycleFields(); });
    });
    const ign = $("ignoreStageToggle");
    if (ign) ign.addEventListener("change", () => {
      api("/api/micro/ignore_stage", { on: ign.checked ? 1 : 0 }).catch(() => {});   // сразу
      sentCmd("Варить без стадии: " + (ign.checked ? "вкл" : "выкл"));
    });
    const pe = $("photoEnableSw");
    if (pe) pe.addEventListener("change", () => {
      const st = $("photoEnableState"); if (st) st.textContent = pe.checked ? "вкл" : "выкл";
      api("/api/micro/photo_enabled", { on: pe.checked ? 1 : 0 }).catch(() => {});   // сразу
      sentCmd("Сырые фото пробы: " + (pe.checked ? "вкл" : "выкл"));
    });
    const fmt = $("pcFormatSw");
    if (fmt) fmt.addEventListener("change", () => {
      const st = $("pcFormatState"); if (st) st.textContent = fmt.checked ? "JPG" : "PNG";
    });
    updateTriggerFields();
  }

  // датчик прихода мотора для цикла: enc / calc / ai (три кнопки на вкладке «Цикл»)
  const ARRIVE_NAMES = { enc: "энкодер (1285)", calc: "расчётная позиция (1274)", ai: "аналоговый датчик (1271)" };
  let pcArriveMode = "enc";
  function setArriveMode(mode) {
    pcArriveMode = ARRIVE_NAMES[mode] ? mode : "enc";
    document.querySelectorAll("#pcArrive .micro-trig__btn").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.arr === pcArriveMode);
    });
  }
  // триггер пробы: time / sv / cv (три кнопки на вкладке «Цикл»)
  const TRIG_NAMES = { time: "по времени", sv: "по СВ", cv: "по CV" };
  let pcTrigMode = "time";
  function setTrigMode(mode) {
    pcTrigMode = TRIG_NAMES[mode] ? mode : "time";
    document.querySelectorAll("#pcTrigger .micro-trig__btn").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.trig === pcTrigMode);
    });
    updateTriggerFields();
  }
  // кадров на пробу / защитная пауза — лежат в настройках CV, шлём туда (применяется сразу)
  function cvPostCycleFields() {
    const num = (id) => { const e = $(id); return e && e.value !== "" ? parseInt(e.value, 10) : undefined; };
    return cvPostSettings({ frames_per_probe: num("pcCvFrames"), gap_sec: num("pcCvGap") });
  }

  // приглушить неактуальные поля под выбранный триггер и режим CV (не скрываем — видно, что
  // неактивно): СВ от/до — «по СВ» и «по CV»; пауза между пробами — «по времени» (и «по CV» при
  // выключенном CV); пауза после разбора — только «по CV»; выдержка/период скринов в секундах —
  // только при выключенном CV; кадров на пробу — только при включённом.
  function updateTriggerFields() {
    const dim = (id, on) => { const e = $(id); if (e) e.classList.toggle("micro-dim", on); };
    const cvOn = !!($("cvEnable") && $("cvEnable").checked);
    dim("pcSvFromWrap", pcTrigMode === "time");     // диапазон СВ работает в «по СВ» и «по CV»
    dim("pcSvToWrap", pcTrigMode === "time");
    dim("pcPauseWrap", pcTrigMode === "sv" || (pcTrigMode === "cv" && cvOn));
    dim("pcCvGapWrap", !(pcTrigMode === "cv" && cvOn));
    dim("pcCvFramesWrap", !cvOn);
    dim("pcDwellWrap", cvOn);
    dim("pcShotWrap", cvOn);
  }

  // ---- блокировки прошивки (рег. 1535): бит=1 → блокировка ОТКЛЮЧЕНА (квадратик закрашен) ----
  const LOCK_NAMES = {
    0: "блокирует калибровку по дельте 100 мкм, если AI0 1000 мкм",
    1: "блокирует низкую скорость при положении < 1 мм",
    2: "блокирует условие AI0 < ai0_min — датчик Home для M1",
    3: "блокирует условие m1_alert — датчик от контроллера для M1",
    4: "блокирует датчик Home для M2",
    5: "блокирует для M1: если позиция по энкодеру < 0, то обнуляем её и абсолютную тоже",
    6: "блокирует для M1 отслеживание ошибок движения",
  };
  // после клика держим подтверждённое значение 4 с: опрос страницы может принести старый снимок,
  // и квадратик «откатился» бы назад, хотя плата уже приняла запись
  let lockHold = null;   // {value, until}
  function renderLocks(value) {
    const row = $("locksRow"); if (!row) return;
    if (lockHold) {
      if (Date.now() > lockHold.until || Number(value) === lockHold.value) lockHold = null;
      else value = lockHold.value;
    }
    if (!row.children.length) {
      for (let bit = 15; bit >= 0; bit--) {
        const b = document.createElement("button");
        b.type = "button"; b.dataset.bit = bit;
        b.className = "micro-lock-bit" + (bit in LOCK_NAMES ? "" : " is-free");
        b.innerHTML = "<span>" + bit + "</span><i></i>";
        b.addEventListener("click", () => toggleLock(bit));
        row.appendChild(b);
      }
    }
    const known = value != null && !isNaN(Number(value));
    row.querySelectorAll(".micro-lock-bit").forEach((b) => {
      const bit = Number(b.dataset.bit), off = known && ((Number(value) >> bit) & 1) === 1;
      b.classList.toggle("is-off", off);
      b.classList.toggle("is-unknown", !known);
      const name = LOCK_NAMES[bit] || "нет описания (не используется)";
      b.title = "Бит " + bit + " — " + name + "\n" +
        (known ? (off ? "сейчас: блокировка ОТКЛЮЧЕНА" : "сейчас: блокировка включена") : "значение не прочитано");
    });
  }
  async function toggleLock(bit) {
    if (!(bit in LOCK_NAMES)) return;                 // биты без описания не трогаем
    const b = document.querySelector('.micro-lock-bit[data-bit="' + bit + '"]'); if (!b) return;
    if (b.classList.contains("is-unknown")) return;   // не знаем текущего значения — не пишем вслепую
    const wasOff = b.classList.contains("is-off");
    const msg = wasOff
      ? "Включить блокировку (бит " + bit + ") обратно?\n\n" + LOCK_NAMES[bit]
      : "ОТКЛЮЧИТЬ блокировку (бит " + bit + ")?\n\n" + LOCK_NAMES[bit] +
        "\n\nЭто запись в плату: защита хода М1 у стекла станет слабее.";
    if (!confirm(msg)) return;
    try {
      const r = await api("/api/micro/lock", { bit, disabled: wasOff ? 0 : 1 });
      if (r && r.value != null && !r.error) { lockHold = { value: Number(r.value), until: Date.now() + 4000 }; renderLocks(r.value); }
      sentCmd("Блокировка бит " + bit + ": " + (wasOff ? "включена" : "ОТКЛЮЧЕНА") + (r && r.error ? " — ошибка " + r.error : ""));
    } catch (e) { sentCmd("Блокировка бит " + bit + ": ошибка " + e.message); }
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

    // автокопии настроек: список по датам + откат
    const snapSel = $("dumpSnapSel");
    const loadSnaps = async () => {
      if (!snapSel) return;
      try {
        const r = await api("/api/micro/config_snapshots");
        const list = r.snapshots || [];
        snapSel.innerHTML = "";
        if (!list.length) { const o = document.createElement("option"); o.textContent = "копий пока нет"; o.value = ""; snapSel.appendChild(o); return; }
        list.forEach((s) => {
          const o = document.createElement("option"); o.value = s.name;
          const d = new Date(s.ts * 1000);
          o.textContent = d.toLocaleString("ru-RU") + " · " + Math.round(s.size / 1024 * 10) / 10 + " КБ";
          snapSel.appendChild(o);
        });
      } catch (e) { /* список недоступен */ }
    };
    loadSnaps();
    document.querySelector('.micro-ptab[data-ptab="dump"]')?.addEventListener("click", loadSnaps);
    const snapBtn = $("dumpSnapRestore");
    if (snapBtn) snapBtn.addEventListener("click", async () => {
      const name = snapSel && snapSel.value; if (!name) return;
      if (!window.confirm("Откатить настройки на копию от " + snapSel.options[snapSel.selectedIndex].textContent + "?\n\nТекущие сохранятся в plate_config.backup.json, автомат перезапустится.")) return;
      try {
        const res = await api("/api/micro/config_restore", { name });
        if (res && res.status === "ok") { hint("настройки восстановлены, автомат перезапущен — перезагрузи страницу"); sentCmd("Настройки восстановлены из автокопии"); }
        else hint("ошибка отката: " + ((res && res.error) || "неизвестно"));
      } catch (e) { hint("ошибка отката: " + e.message); }
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

  // Настройки читаются с сервера при открытии страницы. Сервер мог ещё не подняться (перезапуск, плата
  // переподключилась) — тогда поля остаются пустыми/заводскими, а «Сохранить» затёрло бы настоящие значения.
  // Поэтому: повторяем загрузку, пока не получится, а кнопки сохранения заблокированы до успеха.
  let cfgLoaded = false, cfgTries = 0, cvSettingsLoaded = false, cvLoadTries = 0;
  const SAVE_GUARD_IDS = ["pcSave", "svspSave", "cvSaveBtn", "cvEnable"];
  function lockSaves(on) {
    SAVE_GUARD_IDS.forEach((id) => {
      const b = $(id); if (!b) return;
      if (id === "cvSaveBtn" || id === "cvEnable") b.disabled = on || !cvSettingsLoaded;
      else b.disabled = on || !cfgLoaded;
    });
  }
  function loadHint(text) { const h = $("pcHint"); if (h) h.textContent = text || ""; }
  async function initCamera() {
    let cfgSerial = "";
    let loadedOk = false;
    try {
      cfg = await api("/api/micro/config");
      if (cfg && cfg.probe_cycle) {
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
          setTrigMode(pc.trigger_mode);
          setArriveMode(pc.arrive_sensor);
          const fmSw = $("pcFormatSw"); if (fmSw) fmSw.checked = (pc.photo_format === "jpg");
          const fmSt = $("pcFormatState"); if (fmSt) fmSt.textContent = (pc.photo_format === "jpg") ? "JPG" : "PNG";
          const peSw = $("photoEnableSw"); if (peSw) peSw.checked = (pc.photo_enabled === true);
          const peSt = $("photoEnableState"); if (peSt) peSt.textContent = (pc.photo_enabled === true) ? "вкл" : "выкл";
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
        loadedOk = true;
      }
    } catch (e) { /* конфиг недоступен */ }
    if (!loadedOk) {
      cfgLoaded = false; lockSaves(true);
      loadHint("загрузка настроек с сервера… (попытка " + (cfgTries + 1) + ")");
      if (++cfgTries < 90) setTimeout(initCamera, 2000);
      else loadHint("не удалось загрузить настройки — перезагрузи страницу");
      return;
    }
    cfgLoaded = true; cfgTries = 0; lockSaves(false); loadHint("");

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
      const dScale = (cfg && cfg.sensor_display_scale != null) ? Number(cfg.sensor_display_scale) : 0.1;
      const sensorShown = e.sensor == null ? null : Math.round(e.sensor * dScale);
      set("m1Sensor", um(sensorShown)); set("m1Enc", um(e.m1_enc)); set("m1Steps", num(e.m1_steps));
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
        ? (ac.phase === "find_zero" ? ("Поиск 0 (" + (ac.attempt || 1) + "/2)")
                                    : ("жду ноль (" + (ac.attempt || 1) + "/2)"))
        : (ac.result || "ждёт пропарки");
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
      else if (f.autocal && f.autocal.active) camNext = "калибр.";
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
      set("cycEnc", um(t.pos1_enc));   // энкодер М1 (1285) — по нему доезд цикла
      set("cycTube", f.valve_tube ? "открыт" : "закрыт");
      set("cycGlass", f.valve_glass ? "открыт" : "закрыт");

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
      renderLocks(e.locks);
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
        if (key === "cv") { cvRefresh(); setTimeout(cvDrawTrend, 30); }   // канвас рисуем, когда пане видима
        if (key === "frac") { fracRefresh(); }
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

  // ================= Компьютерное зрение (CV) =================
  // Вкладка «CV» + переключатель окна камера/распознавание. Данные с /api/cv/*.
  const CV_GROUPS = ["small", "medium", "large", "reject"];
  const CV_SERIES_COLOR = { small: "#1d9e75", medium: "#378add", large: "#ba7517", reject: "#e24b4a", mean: "#7f77dd", median: "#d16fb8" };
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
    if ($("cvRejectAlways")) $("cvRejectAlways").checked = !!cv.reject_always;
    set("pcCvFrames", cv.frames_per_probe); set("pcCvGap", cv.gap_sec);   // поля на вкладке «Цикл»
    updateTriggerFields();
  }
  function cvCollectPatch() {
    const num = (id) => { const e = $(id); return e && e.value !== "" ? parseFloat(e.value) : undefined; };
    return {
      groups: { small_max_um: num("cvSmallMax"), medium_max_um: num("cvMediumMax") },
      shape: { min_circularity: num("cvMinCirc"), min_solidity: num("cvMinSol"), max_aspect: num("cvMaxAspect"), suspect_aspect: num("cvSuspect") },
      um_per_px: num("cvUmPerPx"), tiles: num("cvTiles"), conf: num("cvConf"),
      reject_from_sv: num("cvRejectSv"), cluster_gap_px: num("cvClusterGap"), edge_margin_px: num("cvEdgeMargin"), reject_always: $("cvRejectAlways") ? $("cvRejectAlways").checked : undefined,
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
    const plateBtn = $("telePlateBtn"), cvBtn = $("teleCvBtn");
    const plate = $("teleStripPlate"), cvv = $("teleStripCv");
    if (!plateBtn || !cvBtn) return;
    function set(showCv) {
      if (seg) seg.classList.toggle("cv", showCv);
      plateBtn.classList.toggle("is-active", !showCv);
      cvBtn.classList.toggle("is-active", showCv);
      if (plate) plate.hidden = showCv;
      if (cvv) cvv.hidden = !showCv;
      if (showCv) { cvRefresh(); setTimeout(cvDrawTrend, 30); }
    }
    plateBtn.addEventListener("click", () => set(false));
    cvBtn.addEventListener("click", () => set(true));
  }
  function isTeleCvVisible() { const e = $("teleStripCv"); return e && !e.hidden; }

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
    const sz = s.size_um || {};
    const setT = (id, v) => { const e = $(id); if (e) e.textContent = v; };
    setT("cvMean", sz.mean != null ? sz.mean + " мкм" : "—");
    setT("cvMedian", sz.median != null ? sz.median + " мкм" : "—");
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

  // --- оверлей поверх чистого кадра: слои по группам, подсветка формы кристалла под мышкой ---
  const CV_GROUP_NAMES = { small: "малая", medium: "средняя", large: "большая", reject: "брак", suspect: "вытянутый (не брак)", cut: "обрезан краем — не в рассеве" };
  const CV_GROUP_COLOR = { small: "#1d9e75", medium: "#378add", large: "#ba7517", reject: "#e24b4a", suspect: "#6ad1f5", cut: "#9aa3ad" };
  const CV_LAYERS_KEY = "microCvLayers";
  let cvLayers = { small: true, medium: true, large: true, reject: true, suspect: true, cut: true, sizes: false, conf: false, frac: true, detail: true };
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
      if (cvLayers.sizes && gr !== "reject" && gr !== "cut") {
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
      const head = "Ø <b>" + best.size_um + " мкм</b> · S <b>" + Math.round(best.area_um2) + " мкм²</b>";
      // «подробно» выключено — коротко: только размер и площадь; включено — форма, причина, что делать, уверенность
      tip.innerHTML = cvLayers.detail === false ? head
        : head + "<br>" + best.length_um + "×" + best.width_um + " мкм · " + gr + grp + shape + why;
      const cardR = card.getBoundingClientRect();
      tip.style.left = (e.clientX - cardR.left) + "px";
      tip.style.top = (e.clientY - cardR.top) + "px";
      tip.hidden = false;
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
  const CV_TREND_KEY = "microCvTrendSeries";
  const CV_PCT_SERIES = ["small", "medium", "large", "reject"];   // шкала слева, %
  const CV_UM_SERIES = ["mean", "median"];                        // шкала справа, мкм
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
    const q = [cvSerialQ(), "series=small,medium,large,reject,mean,median,count", "from=" + Math.floor(a), "to=" + Math.ceil(b), "limit=5000"].filter(Boolean).join("&");
    const d = await api("/api/cv/trend?" + q);
    if (seq !== cvTrendSeq) return false;        // пока грузили, запросили другое — это устарело
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
    const scaleOf = (name) => (CV_UM_SERIES.includes(name) ? umMax : 100);
    const yAt = (val, mx) => pad.t + plotH * (1 - Math.max(0, Math.min(1, val / mx)));
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
      const col = CV_SERIES_COLOR[name] || "#888"; const mx = scaleOf(name);
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 2; ctx.beginPath();
      let started = false;
      for (let i = a; i < b; i++) {
        const val = arr[i]; if (val == null) { started = false; continue; }
        const x = xAt(t[i]), y = yAt(val, mx);
        if (started && !brewBreak(i)) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        started = true;
      }
      ctx.stroke();
      if (i1 - i0 <= 120) for (let i = i0; i < i1; i++) {          // точки-пробы, пока их немного
        const val = arr[i]; if (val == null) continue;
        ctx.beginPath(); ctx.arc(xAt(t[i]), yAt(val, mx), 2.2, 0, Math.PI * 2); ctx.fill();
      }
    };
    ctx.save(); ctx.beginPath(); ctx.rect(pad.l, pad.t - 2, plotW, plotH + 4); ctx.clip();
    sel.filter((s) => CV_PCT_SERIES.includes(s)).forEach(drawSeries);
    sel.filter((s) => CV_UM_SERIES.includes(s)).forEach(drawSeries);
    // линия-курсор: стоит на ближайшей к мышке пробе
    const hi = cvTrendHover;
    if (hi != null && hi >= 0 && hi < t.length && t[hi] >= v.start && t[hi] <= v.end) {
      const x = xAt(t[hi]);
      ctx.strokeStyle = muted; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + plotH); ctx.stroke(); ctx.setLineDash([]);
      sel.forEach((name) => {
        const val = series[name] && series[name][hi]; if (val == null) return;
        ctx.fillStyle = CV_SERIES_COLOR[name] || "#888";
        ctx.beginPath(); ctx.arc(x, yAt(val, scaleOf(name)), 4, 0, Math.PI * 2); ctx.fill();
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
      const unit = row.dataset.unit || "";
      row.querySelector("b").textContent = val == null ? "—" : (Math.round(val * 10) / 10) + (unit === "%" ? "%" : " " + unit);
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
    const line2 = [cnt != null ? Math.round(cnt) + " крист." : null, mean != null ? "общая " + mean + " мкм" : null, med != null ? "медиана " + med + " мкм" : null].filter(Boolean).join(" · ");
    const names = { small: "малая", medium: "средняя", large: "большая", reject: "брак" };
    const groups = CV_GROUPS.map((g) => val(g) == null ? "" :
      '<span class="dot" style="background:' + CV_SERIES_COLOR[g] + '"></span>' + names[g] + " " + val(g) + "%").filter(Boolean).join("<br>");
    tip.innerHTML = head + (line2 ? "<br>" + line2 : "") + (groups ? "<br>" + groups : "") + (msg ? "<br><i>" + msg + "</i>" : "");
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

  document.addEventListener("DOMContentLoaded", () => {
    lockSaves(true);          // пока настройки не загружены с сервера, сохранять нельзя
    api("/api/version").then((v) => { const e = $("verApp"); if (e) { e.textContent = "WebMVS v" + v.app; e.classList.add("is-ok"); } }).catch(() => {});
    wire();
    syncManual(false);
    initCamera();
    poll();
    setInterval(poll, POLL_MS);
    updateHourlyWashTimer();
    setInterval(updateHourlyWashTimer, 1000);
    wireCV();
    wireFrac();
  });
})();

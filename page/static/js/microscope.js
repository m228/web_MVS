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
  let camSettingsLoaded = false;

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

  function loadCamIframe(serial) {
    const frame = $("camIframe"), ph = $("camPlaceholder");
    camSerial = serial || "";
    // при смене камеры сбросить lazy-загруженные настройки (перезагрузятся с новым серийником)
    const sf = $("camSettingsIframe");
    if (sf) { sf.removeAttribute("src"); sf.hidden = true; }
    camSettingsLoaded = false;
    if (serial) {
      frame.src = "/camera?serial_number=" + encodeURIComponent(serial) + "&embed=1&cam=video";
      frame.hidden = false;
      ph.classList.add("hidden");
    } else {
      frame.hidden = true;
      frame.removeAttribute("src");
      ph.textContent = "Камера не найдена. MVS ищет автоматически — проверь подключение/драйвер, либо укажи IP камеры выше.";
      ph.classList.remove("hidden");
    }
  }

  // Настройки камеры (вкладка 📷): грузим ЛЕНИВО и БЕЗ видео (cam=settings), чтобы не было
  // второго потока к одной GigE-камере (стабильно работает только один поток).
  function loadCamSettings() {
    const sf = $("camSettingsIframe"), ph = $("camSettingsPh");
    if (camSettingsLoaded || !camSerial || !sf) return;
    sf.src = "/camera?serial_number=" + encodeURIComponent(camSerial) + "&embed=1&cam=settings";
    sf.hidden = false;
    if (ph) ph.classList.add("hidden");
    camSettingsLoaded = true;
  }

  function applyCamSerials(serials) {
    const found = $("camFound"), sel = $("camSelect"), ipInp = $("camIp"), ipGo = $("camIpGo");
    sel.hidden = true; ipInp.hidden = true; ipGo.hidden = true;
    if (serials.length === 1) { found.textContent = serials[0]; loadCamIframe(serials[0]); return true; }
    if (serials.length > 1) {
      found.textContent = "";
      sel.hidden = false; sel.innerHTML = "";
      serials.forEach((s) => { const o = document.createElement("option"); o.value = s; o.textContent = s; sel.appendChild(o); });
      loadCamIframe(serials[0]);
      return true;
    }
    found.textContent = "не найдена —";
    ipInp.hidden = false; ipGo.hidden = false;
    loadCamIframe("");
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
        $("cfgPlateHost").value = cfg.host || "";
        $("cfgPlatePort").value = cfg.port || "";
        $("cfgPlateUnit").value = cfg.unit != null ? cfg.unit : "";
        cfgSerial = cfg.camera_serial || "";
      }
    } catch (e) { /* конфиг недоступен */ }

    buildDqGrid();

    if (cfgSerial) {
      $("camSelect").hidden = true; $("camIp").hidden = true; $("camIpGo").hidden = true;
      $("camFound").textContent = cfgSerial;
      loadCamIframe(cfgSerial);
      return;
    }
    discoverWithRetry(0);
  }

  // ---- DQ-сетка: строим кнопки по меткам из конфига ----
  function buildDqGrid() {
    const grid = $("dqGrid");
    if (!grid) return;
    const labels = (cfg && cfg.dq && cfg.dq.labels) || ["трубка", "стекло", "воздух", "вых 4", "вых 5", "вых 6"];
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

  // ---- связь / формат ----
  function setConn(state) {
    const wrap = $("microConnWrap"), el = $("microConn");
    if (!wrap) return;
    wrap.classList.remove("is-on", "is-warn", "is-off");
    if (state.connected) { wrap.classList.add("is-on"); if (el) el.textContent = "подключено"; }
    else if (state.reconnecting) { wrap.classList.add("is-warn"); if (el) el.textContent = "переподключение…"; }
    else { wrap.classList.add("is-off"); if (el) el.textContent = "нет связи"; }
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

      // позиции (сверху и в пульте)
      set("tPos1", um(t.pos1)); set("tPos2", um(t.pos2));
      set("m1Pos", um(e.m1_pos != null ? e.m1_pos : t.pos1));
      set("m2Pos", um(e.m2_pos != null ? e.m2_pos : t.pos2));
      set("m1Sensor", um(e.sensor)); set("m1Steps", num(e.m1_steps));
      set("m2Steps", num(e.m2_steps)); set("m2State", num(e.m2_state));
      set("tSensor", um(e.sensor)); set("tEnc", um(e.m1_enc)); set("tSlip", um(e.m1_slip));
      set("tSteps", pair(e.m1_steps, e.m2_steps));

      // питание / термо
      set("tTemp", t.temp == null ? "—" : t.temp + " °C");
      set("tU12v", t.u12v == null ? "—" : (t.u12v / 1000).toFixed(2) + " В");
      set("tFan1", num(e.fan1)); set("tFan2", num(e.fan2));

      // входы/выходы
      set("tDi", e.di != null ? bits6(e.di) : hex(t.di));
      set("tDq", bits6(e.dq));

      // автомат
      set("tSv", f.sv == null ? "—" : Number(f.sv).toFixed(2));
      set("microStep", f.step || "—");
      set("microMode", f.mode == null ? "—" : f.mode);
      set("valveTube", f.valve_tube ? "открыт" : "закрыт");
      set("valveGlass", f.valve_glass ? "открыт" : "закрыт");

      // система
      set("tVer", num(e.version));
      set("tModVer", e.module_ver == null ? "—" : (HW[e.module_ver] || e.module_ver));
      set("tSerial", num(e.serial));
      set("tCycle", (e.cycle_us == null) ? "—" : e.cycle_us + " / " + num(e.cycle_peak_us) + " мкс");
      set("tUptime", upt(e.uptime_s));

      // данные с контроллера (ПЛК) — источник аппарата по Modbus (sv_source). Пока нет данных -> «(—)».
      const plc = d.plc || {}, pv = plc.values || {};
      set("pcSv", pv.sv == null ? "(—)" : Number(pv.sv).toFixed(2));
      set("pcStage", stageLabel(pv.stage));
      set("pcTemp", pv.temp_app == null ? "(—)" : pv.temp_app + " °C");
      set("pcVacuum", pv.vacuum == null ? "(—)" : pv.vacuum);
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
      set("maStage", pv.stage == null ? "—" : stageLabel(pv.stage));
      const fill = document.getElementById("maFill");
      if (fill) {
        const lv = pv.level == null ? 0 : Math.max(0, Math.min(100, Number(pv.level)));
        const H = 176, base = 206, h = Math.max(4, H * lv / 100);  // тело 30..206
        fill.setAttribute("y", base - h);
        fill.setAttribute("height", h);
      }
      const mab = $("maBadge");
      if (mab) { mab.textContent = pb ? pb.textContent : ""; mab.className = pb ? pb.className : "micro-plc-badge"; }

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
    $("manualState").textContent = on ? "ВКЛ" : "выкл";
    $("microPult").classList.toggle("is-locked", !on);   // гейт панелей М1/М2/LED/DQ
    $("manualHint").classList.toggle("hidden", on);
  }

  // ---- кнопки ----
  function wire() {
    $("btnReload").addEventListener("click", async () => { await api("/api/micro/reload"); initCamera(); });

    $("cfgApply").addEventListener("click", async () => {
      const hint = $("cfgHint"); hint.textContent = "применяю…";
      try {
        const p = {};
        if ($("cfgPlateHost").value) p.host = $("cfgPlateHost").value;
        if ($("cfgPlatePort").value) p.port = $("cfgPlatePort").value;
        if ($("cfgPlateUnit").value) p.unit = $("cfgPlateUnit").value;
        await api("/api/micro/settings", p);
        hint.textContent = "применено ✓";
        setTimeout(() => { hint.textContent = ""; }, 2500);
      } catch (e) { hint.textContent = "ошибка: " + e.message; }
    });

    // LED — включение подразумевается яркостью (>0 = вкл), отдельной кнопки нет
    const led = $("ledBright");
    led.addEventListener("input", () => { $("ledBrightVal").textContent = led.value; });
    led.addEventListener("change", () => api("/api/micro/led", { bright: led.value, on: Number(led.value) > 0 ? 1 : 0 }));
    $("ledFreq").addEventListener("change", () => api("/api/micro/led", { freq: $("ledFreq").value }));

    // камера
    $("camSelect").addEventListener("change", () => loadCamIframe($("camSelect").value));
    $("camIpGo").addEventListener("click", () => { const ip = $("camIp").value.trim(); if (ip) loadCamIframe(ip); });

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
    [["1", "goto"], ["1", "steps"], ["1", "shift"], ["2", "goto"], ["2", "steps"], ["2", "shift"]].forEach(([m, op]) => {
      const inp = $("m" + m + (op === "goto" ? "GotoInp" : op === "steps" ? "StepsInp" : "ShiftInp"));
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
        if (key === "cam") loadCamSettings();   // настройки камеры грузим лениво
      });
    });
  }

  const OP_LABEL = {
    goto: "Идти в позицию", steps: "Выполнить шаги", shift: "Сдвинуть",
    home_start: "В начало", home_end: "В конец", find_zero: "Поиск 0",
    set_zero: "Установить 0", stop: "СТОП",
  };
  const CONFIRM_OPS = { goto: 1, home_end: 1 };  // рискованные — спросить

  function runMotorOp(m, op) {
    let value = null;
    if (op === "goto") value = $("m" + m + "GotoInp").value;
    else if (op === "steps") value = $("m" + m + "StepsInp").value;
    else if (op === "shift") value = $("m" + m + "ShiftInp").value;

    if (CONFIRM_OPS[op]) {
      const what = op === "goto" ? ("М" + m + " → " + value + " мкм") : ("М" + m + " → в конец");
      if (!window.confirm("Двигать мотор?\n" + what + "\n\nУбедись, что путь свободен (от стекла).")) return;
    }
    const params = { m, op };
    if (value != null) params.value = value;
    api("/api/micro/motor", params).then((r) => {
      if (r && r.error) sentCmd("⚠ " + (OP_LABEL[op] || op) + ": " + r.error);
    }).catch(() => { });
    sentCmd("М" + m + " · " + (OP_LABEL[op] || op) + (value != null ? " " + value : ""));
  }

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    syncManual(false);
    initCamera();
    poll();
    setInterval(poll, POLL_MS);
  });
})();

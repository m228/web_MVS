// Страница микроскопа: опрос телеметрии/состояния автомата + команды на плату.
// Работает поверх эндпоинтов /api/micro/* (см. app.py). Vanilla JS, без зависимостей.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 600;
  let lastCyclic = false;

  async function api(path, params) {
    const q = params ? "?" + new URLSearchParams(params).toString() : "";
    const res = await fetch(path + q);
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  // ---- камера: MVS SDK сам находит камеры Hikrobot ----
  async function autoDiscoverCameras() {
    try {
      const data = await api("/api/cams/detailed");   // { серийник: [записи] }
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
    const frame = $("camIframe");
    const ph = $("camPlaceholder");
    if (serial) {
      frame.src = "/camera?serial_number=" + encodeURIComponent(serial) + "&embed=1";
      frame.hidden = false;
      ph.classList.add("hidden");
    } else {
      frame.hidden = true;
      frame.removeAttribute("src");
      ph.textContent = "Камера не найдена. MVS ищет автоматически — проверь подключение/драйвер, либо укажи IP камеры выше.";
      ph.classList.remove("hidden");
    }
  }

  async function initCamera() {
    let cfgSerial = "";
    try {
      const cfg = await api("/api/micro/config");
      if (cfg) {
        if (cfg.manual_sv != null) $("svInput").value = cfg.manual_sv;
        if (cfg.manual_stage != null) $("stageSelect").value = String(cfg.manual_stage);
        if (cfg.led_bright != null) {
          $("ledBright").value = cfg.led_bright;
          $("ledBrightVal").textContent = cfg.led_bright;
        }
        $("cfgPlateHost").value = cfg.host || "";
        $("cfgPlatePort").value = cfg.port || "";
        cfgSerial = cfg.camera_serial || "";
      }
    } catch (e) { /* конфиг недоступен */ }

    const found = $("camFound"), sel = $("camSelect"), ipInp = $("camIp"), ipGo = $("camIpGo");
    sel.hidden = true; ipInp.hidden = true; ipGo.hidden = true;

    if (cfgSerial) {                       // явный override в конфиге
      found.textContent = cfgSerial;
      loadCamIframe(cfgSerial);
      return;
    }

    const serials = await autoDiscoverCameras();
    if (serials.length === 1) {            // одна — грузим сразу
      found.textContent = serials[0];
      loadCamIframe(serials[0]);
    } else if (serials.length > 1) {       // несколько — список для выбора
      found.textContent = "";
      sel.hidden = false;
      sel.innerHTML = "";
      serials.forEach((s) => {
        const o = document.createElement("option"); o.value = s; o.textContent = s; sel.appendChild(o);
      });
      loadCamIframe(serials[0]);
    } else {                               // не нашлась — даём указать IP
      found.textContent = "не найдена —";
      ipInp.hidden = false; ipGo.hidden = false;
      loadCamIframe("");
    }
  }

  // ---- опрос телеметрии ----
  function setConn(state) {
    const el = $("microConn");
    if (state.connected) { el.textContent = "есть"; el.style.color = "var(--success)"; }
    else if (state.reconnecting) { el.textContent = "переподключение…"; el.style.color = "var(--warning)"; }
    else { el.textContent = "нет"; el.style.color = "var(--danger)"; }
  }

  function fmt(v, unit) {
    return (v == null) ? "—" : (unit ? v + " " + unit : String(v));
  }

  async function poll() {
    try {
      const d = await api("/api/micro/telemetry");
      const t = d.telemetry || {};
      const f = d.fsm || {};
      setConn(d.connection || {});

      $("tPos1").textContent = fmt(t.pos1, "мкм");
      $("tPos2").textContent = fmt(t.pos2, "мкм");
      $("tPos1ai").textContent = fmt(t.pos1_ai, "мкм");
      $("tTemp").textContent = (t.temp == null) ? "—" : t.temp + " °C";        // raw = °C (плата, ×1)
      $("tU12v").textContent = (t.u12v == null) ? "—" : (t.u12v / 1000).toFixed(2) + " В";  // raw в мВ
      $("tDi").textContent = (t.di == null) ? "—" : "0x" + Number(t.di).toString(16).padStart(4, "0");
      $("tSv").textContent = (f.sv == null) ? "—" : Number(f.sv).toFixed(2);

      $("microStep").textContent = f.step || "—";
      $("microMode").textContent = (f.mode == null) ? "—" : f.mode;
      $("valveTube").textContent = f.valve_tube ? "открыт" : "закрыт";
      $("valveGlass").textContent = f.valve_glass ? "открыт" : "закрыт";
      $("microCmd1").textContent = (f.cmd1 == null) ? "—" : "0x" + Number(f.cmd1).toString(16).padStart(4, "0");

      // расширенная телеметрия с платы (родные регистры)
      const e = d.ext || {};
      const onoff = (v) => (v == null ? "—" : (v ? "вкл" : "выкл"));
      $("tM1en").textContent = onoff(e.m1_enable);
      $("tM2en").textContent = onoff(e.m2_enable);
      $("tM1enc").textContent = (e.m1_enc == null) ? "—" : e.m1_enc + " мкм";
      $("tM1slip").textContent = (e.m1_slip == null) ? "—" : e.m1_slip + " мкм";
      $("tLed").textContent = (e.led_bright == null) ? "—" : e.led_bright + " %";
      $("tDq").textContent = (e.dq == null) ? "—" : "0b" + Number(e.dq).toString(2).padStart(3, "0");
      $("tFan").textContent = (e.fan1 == null) ? "—" : (e.fan1 + " / " + (e.fan2 == null ? "—" : e.fan2));
      $("tCycle").textContent = (e.cycle_us == null) ? "—" : e.cycle_us + " мкс";
      $("tVer").textContent = (e.version == null) ? "—" : e.version;

      // циклический режим
      lastCyclic = !!f.cyclic;
      $("cyclicState").textContent = lastCyclic ? "вкл" : "выкл";
      $("btnCyclic").classList.toggle("toolbar-btn--primary", lastCyclic);

      // стоп движения
      const inhibit = !!f.inhibit;
      $("btnStop").classList.toggle("hidden", inhibit);
      $("btnRelease").classList.toggle("hidden", !inhibit);
    } catch (e) {
      setConn({ connected: false, reconnecting: false });
    }
  }

  // ---- кнопки ----
  function wire() {
    $("cmdCycle").addEventListener("click", () => api("/api/micro/command", { cmd: 100 }));
    $("cmdRetract").addEventListener("click", () => api("/api/micro/command", { cmd: 200 }));
    $("cmdWashGlass").addEventListener("click", () => api("/api/micro/command", { cmd: 300 }));
    $("btnCyclic").addEventListener("click", () => api("/api/micro/cyclic", { on: lastCyclic ? 0 : 1 }));
    $("btnStop").addEventListener("click", () => api("/api/micro/stop", { on: 1 }));
    $("btnRelease").addEventListener("click", () => api("/api/micro/stop", { on: 0 }));
    $("btnReload").addEventListener("click", async () => {
      await api("/api/micro/reload");
      initCamera();  // конфиг мог поменять camera_serial / префиллы
    });

    $("cfgApply").addEventListener("click", async () => {
      const hint = $("cfgHint");
      hint.textContent = "применяю…";
      try {
        const p = {};
        if ($("cfgPlateHost").value) p.host = $("cfgPlateHost").value;
        if ($("cfgPlatePort").value) p.port = $("cfgPlatePort").value;
        await api("/api/micro/settings", p);
        hint.textContent = "применено ✓";
        setTimeout(() => { hint.textContent = ""; }, 2500);
      } catch (e) {
        hint.textContent = "ошибка: " + e.message;
      }
    });

    $("svApply").addEventListener("click", () => api("/api/micro/sv", { value: $("svInput").value }));
    $("stageSelect").addEventListener("change", () => api("/api/micro/stage", { value: $("stageSelect").value }));

    const led = $("ledBright");
    led.addEventListener("input", () => { $("ledBrightVal").textContent = led.value; });
    led.addEventListener("change", () => api("/api/micro/led", { bright: led.value }));
    $("ledOn").addEventListener("change", () => api("/api/micro/led", { on: $("ledOn").checked ? 1 : 0 }));

    // выбор камеры (список) / указание IP, если не нашлась
    $("camSelect").addEventListener("change", () => loadCamIframe($("camSelect").value));
    $("camIpGo").addEventListener("click", () => {
      const ip = $("camIp").value.trim();
      if (ip) loadCamIframe(ip);
    });

    // ручное движение моторов + аварийный стоп
    $("m1Go").addEventListener("click", () => api("/api/micro/move", { m: 1, pos: $("m1Pos").value }));
    $("m2Go").addEventListener("click", () => api("/api/micro/move", { m: 2, pos: $("m2Pos").value }));
    $("estopBtn").addEventListener("click", () => api("/api/micro/estop"));
  }

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    initCamera();
    poll();
    setInterval(poll, POLL_MS);
  });
})();

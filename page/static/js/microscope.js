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

  // ---- картинка микроскопа (переиспользуем GigE-стрим камеры) ----
  async function initCamera() {
    let serial = "";
    try {
      const cfg = await api("/api/micro/config");
      serial = (cfg && cfg.camera_serial) || "";
      // префилл ручных значений из конфига
      if (cfg) {
        if (cfg.manual_sv != null) $("svInput").value = cfg.manual_sv;
        if (cfg.manual_stage != null) $("stageSelect").value = String(cfg.manual_stage);
        if (cfg.led_bright != null) {
          $("ledBright").value = cfg.led_bright;
          $("ledBrightVal").textContent = cfg.led_bright;
        }
        // поля связи (камера/плата)
        $("cfgCamSerial").value = cfg.camera_serial || "";
        $("cfgCamIp").value = cfg.camera_ip || "";
        $("cfgPlateHost").value = cfg.host || "";
        $("cfgPlatePort").value = cfg.port || "";
      }
    } catch (e) { /* конфиг недоступен — оставляем плейсхолдер */ }

    // встраиваем полную камерную страницу (все настройки + поток) в компактном режиме
    const frame = $("camIframe");
    const placeholder = $("camPlaceholder");
    if (serial) {
      frame.src = "/camera?serial_number=" + encodeURIComponent(serial) + "&embed=1";
      frame.hidden = false;
      placeholder.classList.add("hidden");
    } else {
      frame.hidden = true;
      frame.removeAttribute("src");
      placeholder.classList.remove("hidden");
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
        const p = { camera_serial: $("cfgCamSerial").value, camera_ip: $("cfgCamIp").value };
        if ($("cfgPlateHost").value) p.host = $("cfgPlateHost").value;
        if ($("cfgPlatePort").value) p.port = $("cfgPlatePort").value;
        await api("/api/micro/settings", p);
        hint.textContent = "применено ✓";
        initCamera();
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
  }

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    initCamera();
    poll();
    setInterval(poll, POLL_MS);
  });
})();

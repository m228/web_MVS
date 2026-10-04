// Страница микроскопа — инициализация (DOMContentLoaded)
// Часть бывшего microscope.js (IIFE снят: файлы microscope/01..04 делят общую глобальную область, порядок <script> важен).
"use strict";

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
    wireFracLab();
  });

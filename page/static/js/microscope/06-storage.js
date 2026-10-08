// Страница микроскопа — карточка «Хранилище данных»: режим (файлы / файлы + база / база), перенос старых данных, сверка
// API: /api/cv/storage, /api/cv/storage/migrate, /api/cv/storage/mode. Файлы microscope/01..06 делят общую глобальную область.
"use strict";

  (function () {
    const g = (id) => document.getElementById(id);
    let state = null;
    const MODE_NAME = { files: "только файлы", both: "файлы + база", sqlite: "только база" };

    function renderRows(cams) {
      const box = g("stRows"); if (!box) return;
      box.innerHTML = (cams || []).map((c) =>
        '<div class="micro-st-row ' + (c.ok ? "is-ok" : "is-bad") + '"><span>' + c.serial + "</span><b>файлы " + c.files + " · база " + c.sqlite +
        (c.ok ? " ✓" : " · нет в базе " + c.only_files + ", расхождений " + c.different) + "</b></div>").join("") ||
        '<div class="micro-conn-hint">проб на этой машине пока нет</div>';
    }

    function render() {
      if (!state) return;
      document.querySelectorAll("#stModeSeg button").forEach((b) => b.classList.toggle("is-active", b.dataset.mode === state.mode));
      const info = g("stInfo");
      if (info) info.textContent = "Режим: " + (MODE_NAME[state.mode] || state.mode) + " · проб в базе: " + state.db_rows;
      renderRows(state.cameras);
    }

    async function load() {
      try { state = await (await fetch("/api/cv/storage")).json(); render(); } catch (e) { const i = g("stInfo"); if (i) i.textContent = "не получилось прочитать: " + e.message; }
    }

    async function migrate() {
      const btn = g("stMigrateBtn"), info = g("stInfo");
      btn.disabled = true; info.textContent = "переношу старые данные в базу…";
      try {
        const d = await (await fetch("/api/cv/storage/migrate", { method: "POST" })).json();
        const added = d.cameras.reduce((a, c) => a + c.added, 0), err = d.cameras.filter((c) => c.error);
        info.textContent = "перенос готов: добавлено проб " + added + (err.length ? " · ошибки: " + err.map((c) => c.serial + " — " + c.error).join("; ") : "");
        await load();
        if (!err.length) info.textContent = "перенос готов: добавлено проб " + added;
      } catch (e) { info.textContent = "перенос не удался: " + e.message; }
      btn.disabled = false;
    }

    async function setMode(mode) {
      const info = g("stInfo");
      if (state && mode === state.mode) return;
      try {
        const r = await fetch("/api/cv/storage/mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
        if (r.status === 409) {                                  // на «базу» — только после успешной сверки
          const d = await r.json(); renderRows(d.cameras);
          info.textContent = "сверка не прошла — сначала нажми «Перенести старые данные в базу», потом «Сверить»";
          return;
        }
        if (!r.ok) throw new Error(await r.text());
        await load();
        info.textContent = "режим: " + MODE_NAME[mode] + " · проб в базе: " + state.db_rows;
      } catch (e) { info.textContent = "не переключилось: " + e.message; }
    }

    function init() {
      if (!g("storageCard")) return;
      document.querySelectorAll("#stModeSeg button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
      g("stMigrateBtn").addEventListener("click", migrate);
      g("stCheckBtn").addEventListener("click", load);
      load();
    }
    init();
  })();

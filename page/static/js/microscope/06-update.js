// Страница микроскопа — кнопка «проверить / обновить» рядом с версиями в шапке.
// «проверить» → /api/update/check (GitHub); есть новая версия → кнопка меняется на «обновить → vX»; «обновить» → /api/update/run
// запускает update.ps1 из папки установки (останавливает приложение, ставит релиз, запускает снова); страница ждёт и перезагружается.
"use strict";

  (function () {
    const btn = document.getElementById("updBtn");
    if (!btn) return;
    let latest = null, resetT = null;
    const idle = () => { btn.className = "micro-ver micro-ver--btn"; btn.textContent = "проверить"; latest = null; };
    const say = (t, ms) => { btn.textContent = t; clearTimeout(resetT); if (ms) resetT = setTimeout(idle, ms); };

    async function check() {
      btn.classList.add("is-busy"); say("проверяю…");
      try {
        const r = await (await fetch("/api/update/check")).json();
        btn.classList.remove("is-busy");
        if (r.error) { say("нет связи с GitHub", 5000); btn.title = r.error; return; }
        if (r.update_available) { latest = r.latest; btn.classList.add("is-new"); btn.textContent = "обновить → v" + r.latest; btn.title = "Есть новая версия v" + r.latest + " (сейчас v" + r.current + "). Нажми — приложение перезапустится"; }
        else { say("v" + r.current + " — последняя", 4000); }
      } catch (e) { btn.classList.remove("is-busy"); say("не проверилось", 4000); }
    }

    async function run() {
      if (!window.confirm("Обновить до v" + latest + "? Приложение остановится и запустится заново (1–2 минуты). Во время варки лучше не обновлять.")) return;
      btn.classList.add("is-busy");
      // поток камеры закрываем штатно ДО остановки приложения (а не обрываем вместе с процессом): после обновления откроется новый
      let camWas = false;
      try { if (camConnected) { say("закрываю камеру…"); camWas = await camCloseGracefully(); if (camWas) { try { localStorage.setItem("microCamReconnect", "1"); } catch (e) { } } } } catch (e) { }
      say("запускаю…");
      try { window.__verBefore = (await (await fetch("/api/debug/info", { cache: "no-store" })).json()).version; } catch (e) { window.__verBefore = ""; }   // версию «до» берём ДО запуска
      let r;
      try { r = await (await fetch("/api/update/run")).json(); } catch (e) { r = null; }
      if (!r || !r.ok) {
        btn.classList.remove("is-busy"); btn.classList.remove("is-new"); say((r && r.error) || "не запустилось", 7000);
        if (camWas) { try { localStorage.removeItem("microCamReconnect"); camConnect(); sentCmd("Камера: обновление не запустилось, поток открыт снова"); } catch (e) { } }   // обновление не пошло — вернуть камеру
        return;
      }
      say("обновляю…");
      // приложение остановится, update.ps1 заменит файлы и запустит его снова: ждём, пока заработает новая версия, и перезагружаем страницу
      const started = Date.now();
      const poll = async () => {
        try {
          const i = await (await fetch("/api/debug/info", { cache: "no-store" })).json();
          if (i && i.version && i.version !== (window.__verBefore || "")) { window.location.reload(); return; }
        } catch (e) { /* приложение перезапускается */ }
        if (Date.now() - started > 8 * 60 * 1000) { say("долго — проверь update.log", 0); btn.classList.remove("is-busy"); return; }
        setTimeout(poll, 3000);
      };
      setTimeout(poll, 6000);
    }

    btn.addEventListener("click", () => { if (latest) run(); else check(); });
  })();

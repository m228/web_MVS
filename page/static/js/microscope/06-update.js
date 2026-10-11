// Страница микроскопа — кнопка «проверить / обновить» рядом с версиями в шапке. Обновление в две фазы:
//  1) «проверить» → /api/update/check (GitHub); есть новая версия → «обновить → vX»; нажал → релиз СКАЧИВАЕТСЯ В ФОНЕ
//     (/api/update/download?extract=0): приложение работает, камера, пробы и цикл не прерываются;
//  2) скачано и проверено → кнопка сама меняется на «перезапустить → vX» (сигнал «готов»); нажал в удобный момент → поток камеры
//     закрывается штатно, update.ps1 -ZipPath (/api/update/run?staged=1) без скачивания останавливает приложение, заменяет файлы и
//     запускает заново; страница ждёт новую версию, перезагружается и открывает новый поток.
"use strict";

  (function () {
    const btn = document.getElementById("updBtn");
    if (!btn) return;
    let latest = null, ready = null, resetT = null, watchT = null;
    const idle = () => { btn.className = "micro-ver micro-ver--btn"; btn.textContent = "проверить"; btn.title = ""; latest = null; ready = null; clearTimeout(watchT); };
    const say = (t, ms) => { btn.textContent = t; clearTimeout(resetT); if (ms) resetT = setTimeout(idle, ms); };

    const setNew = (r) => {
      latest = r.latest; ready = null; btn.classList.remove("is-busy"); btn.classList.add("is-new"); btn.textContent = "обновить → v" + r.latest;
      btn.title = "Есть новая версия v" + r.latest + " (сейчас v" + r.current + "). Нажми — она скачается в фоне, работа не прервётся";
    };
    const setReady = (v) => {
      latest = v; ready = v; clearTimeout(watchT); btn.classList.remove("is-busy"); btn.classList.add("is-new"); btn.textContent = "перезапустить → v" + v;
      btn.title = "Версия v" + v + " скачана и проверена. Нажми, когда готов: камера закроется штатно, приложение перезапустится (около минуты)";
    };

    // скачивание идёт на сервере (страницу могли перезагрузить): раз в 3 с спрашиваем, не готово ли
    function watchDownload() {
      btn.classList.add("is-busy"); say("скачиваю…");
      const tick = async () => {
        try {
          const r = await (await fetch("/api/update/check")).json();
          if (r.staged && r.staged === r.latest) { setReady(r.latest); return; }
          if (!r.downloading) { if (r.update_available) setNew(r); else idle(); return; }          // скачивание оборвалось — снова «обновить»
        } catch (e) { /* сервер занят — спросим ещё раз */ }
        watchT = setTimeout(tick, 3000);
      };
      watchT = setTimeout(tick, 3000);
    }

    async function check() {
      btn.classList.add("is-busy"); say("проверяю…");
      try {
        const r = await (await fetch("/api/update/check")).json();
        btn.classList.remove("is-busy");
        if (r.error) { say("нет связи с GitHub", 5000); btn.title = r.error; return; }
        if (!r.update_available) { say("v" + r.current + " — последняя", 4000); return; }
        if (r.staged && r.staged === r.latest) setReady(r.latest);
        else if (r.downloading) watchDownload();
        else setNew(r);
      } catch (e) { btn.classList.remove("is-busy"); say("не проверилось", 4000); }
    }

    // фаза 1: скачать в фоне (работа не прерывается)
    async function download() {
      if (!window.confirm("Скачать v" + latest + " в фоне? Работа, пробы и камера не прервутся. Когда скачается, кнопка станет «перезапустить» — нажмёшь её в удобный момент.")) return;
      btn.classList.add("is-busy"); say("скачиваю…");
      let r;
      try { r = await (await fetch("/api/update/download?extract=0")).json(); } catch (e) { r = null; }
      if (r && r.busy) { watchDownload(); return; }
      if (!r || !r.ok) { btn.classList.remove("is-busy"); btn.classList.remove("is-new"); say((r && r.error) || "не скачалось", 7000); return; }
      setReady(r.version || latest);
    }

    // фаза 2: быстрый перезапуск из уже скачанного архива
    async function apply() {
      if (!window.confirm("Перезапустить на v" + ready + " сейчас? Камера закроется штатно, приложение будет недоступно около минуты. Во время варки лучше выбрать удобный момент.")) return;
      btn.classList.add("is-busy");
      // поток камеры закрываем штатно ДО остановки приложения (а не обрываем вместе с процессом): после обновления откроется новый
      let camWas = false;
      try { if (camConnected) { say("закрываю камеру…"); camWas = await camCloseGracefully(); if (camWas) { try { localStorage.setItem("microCamReconnect", "1"); } catch (e) { } } } } catch (e) { }
      say("запускаю…");
      try { window.__verBefore = (await (await fetch("/api/debug/info", { cache: "no-store" })).json()).version; } catch (e) { window.__verBefore = ""; }   // версию «до» берём ДО запуска
      let r;
      try { r = await (await fetch("/api/update/run?staged=1")).json(); } catch (e) { r = null; }
      if (!r || !r.ok) {
        btn.classList.remove("is-busy"); setReady(ready); say((r && r.error) || "не запустилось", 0);
        if (camWas) { try { localStorage.removeItem("microCamReconnect"); camConnect(); sentCmd("Камера: перезапуск не пошёл, поток открыт снова"); } catch (e) { } }   // не пошло — вернуть камеру
        return;
      }
      say("перезапускаю…");
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

    btn.addEventListener("click", () => { if (ready) apply(); else if (latest) download(); else check(); });
  })();

// static/js/stage-names.js — названия стадий варки с сервера. Одна таблица на приложение — substages.py (/api/stage_names),
// своих копий в JS не держим. Подключать ДО скриптов, которые ими пользуются (микроскоп, plc-source.js).
//   StageNames.titles[k]    — «Сгущение», «УНВ» (для показа)
//   StageNames.stages[k]    — «сгущение» (как в substages.STAGE_NAMES)
//   StageNames.substages[c] — «рост 2» по коду Submode
//   StageNames.ready        — Promise: таблицы заполнены (или сервер так и не ответил — тогда пусто, показываем номера)
// Объекты заполняются на месте: ссылки на них, взятые до загрузки, остаются рабочими.
window.StageNames = (() => {
  const stages = {}, titles = {}, substages = {};
  async function load() {
    for (let i = 0; i < 3; i += 1) {                 // приложение могло перезапускаться — пара повторов
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 10000);
      try {
        const r = await fetch("/api/stage_names", { cache: "no-store", signal: ctl.signal });
        if (r.ok) {
          const d = await r.json();
          Object.entries(d.stages || {}).forEach(([k, v]) => { stages[k] = v; titles[k] = v.charAt(0).toUpperCase() + v.slice(1); });
          Object.assign(substages, d.substages || {});
          return;
        }
      } catch (e) { /* повторим */ } finally { clearTimeout(timer); }
      await new Promise((res) => setTimeout(res, 3000));
    }
  }
  return { stages, titles, substages, ready: load() };
})();

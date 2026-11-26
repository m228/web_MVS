// ---------- автостарт мультипросмотра ----------
// Раскладку (сколько ячеек и какая камера в какой) сервер помнит всегда (plate_config.multi_layout).
// Галочка «Автостарт» (взаимоисключается с «Автостартом цикла» микроскопа) говорит приложению открывать /multi при старте:
// страница сама расставляет камеры по ячейкам и подключает их. Чтобы открывалось быстро: сначала сохранённые RTSP-камеры
// (локальный файл) и сразу их потоки, а медленный опрос GigE-камер идёт после — их ячейки поднимаются, когда опрос закончен.
const AS = { ready: false, timer: null, on: false, saved: null, layoutDone: false, pending: null };

// вид страницы (открыта ли панель КЗ, её вкладка, выбранная ячейка) помнит браузер: после перезапуска всё возвращается как было.
// Читаем один раз при загрузке, до первых записей. Приближение камер сюда не входит — его помнит сервер (save_settings.json)
const VIEW_KEY = 'multiView';
const VIEW = (() => {
  try { return JSON.parse(localStorage.getItem(VIEW_KEY) || 'null') || {}; } catch (e) { return {}; }
})();
const viewNow = Object.assign({}, VIEW);

function saveView(patch) {
  Object.assign(viewNow, patch);
  try { localStorage.setItem(VIEW_KEY, JSON.stringify(viewNow)); } catch (e) { /* не критично */ }
}

// выбранная ячейка: −1 — фокус снят (общий вид КЗ); нет такой ячейки — первая
function savedFocus() {
  const f = VIEW.focus;
  return Number.isInteger(f) && f >= -1 && f < state.layout ? f : 0;
}

// панель КЗ открываем после конфига сервиса: без него вкладкам «Валы / Сок / Уровень» нечего показывать
async function restoreMotionDock() {
  if (VIEW.tab) MD.tab = VIEW.tab;      // вкладку помним и при закрытой панели
  if (!VIEW.dock) return;
  await mvRefreshConfig();
  setMotionDock(true);
}

// метка «страница перезагрузилась после обновления» (ставит 14-update.js перед запуском апдейтера): раскладка и подключённые камеры
// возвращаются как были, даже если «Автостарт» выключен. Метка одноразовая; старше получаса — не наша
const MU_FLAG = 'multiRestoreAfterUpdate';
const MU_FLAG_MAX_AGE_MS = 30 * 60 * 1000;
function takeUpdateFlag() {
  try {
    const raw = localStorage.getItem(MU_FLAG);
    if (!raw) return null;
    localStorage.removeItem(MU_FLAG);
    const flag = JSON.parse(raw);
    if (!flag || !Array.isArray(flag.tiles) || Date.now() - Number(flag.ts || 0) > MU_FLAG_MAX_AGE_MS) return null;
    flag.active = Array.isArray(flag.active) ? flag.active : [];
    return flag;
  } catch (e) {
    return null;
  }
}

// после первой расстановки (иначе стартовый setLayout(1) затёр бы сохранённую раскладку)
function saveMultiLayoutSoon() {
  if (!AS.ready) return;
  clearTimeout(AS.timer);
  AS.timer = setTimeout(() => {
    MultiApi.saveLayout(state.layout, state.tiles.map((t) => t.serial || null));
  }, 400);
}

// расставить сохранённые камеры по ячейкам и подключить; повторный вызов добирает то, что не нашлось в прошлый раз
function restoreMultiLayout() {
  // после обновления берём состояние, снятое в момент нажатия (какие камеры стояли и были подключены), а не сохранённое сервером
  const pend = AS.pending;
  const saved = pend || AS.saved;
  if ((!AS.on && !pend) || !saved || !Array.isArray(saved.tiles)) return;
  if (!AS.layoutDone) {
    AS.layoutDone = true;
    if (saved.layout && saved.layout !== state.layout) setLayout(saved.layout);
  }
  saved.tiles.forEach((serial, i) => {
    const tile = state.tiles[i];
    const source = serial ? getSource(serial) : null;
    if (!tile || !source || tile.serial) return;
    if (source.kind === 'gige' && !source.available) return;
    assignSource(i, serial);
    if (!pend || pend.active.includes(serial)) startStream(i);
  });
  setFocus(savedFocus());
}

// какой поток камеры идёт на показ (основной / дополнительный); анализ КЗ выбирается отдельно и по умолчанию остаётся на основном
async function initDisplayStreamToggle() {
  const box = document.getElementById('multiDisplaySubToggle');
  if (!box) return;
  try {
    const r = await fetch('/api/rtsp/display_stream').then((x) => x.json());
    box.checked = !!(r && r.substream);
  } catch (e) { /* галочка останется выключенной */ }
  box.addEventListener('change', async () => {
    const wanted = box.checked;
    try {
      const r = await fetch('/api/rtsp/display_stream', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ substream: wanted }) }).then((x) => x.json());
      if (!r || r.status !== 'ok') throw new Error('нет ответа');
      log.success('Показ: ' + (wanted ? 'дополнительный поток' : 'основной поток') + ' — применится при следующем подключении камер (перезапустите приложение)');
    } catch (e) {
      box.checked = !wanted;
      log.warn('Не удалось изменить поток для показа');
    }
  });
}

function initMultiAutostart() {
  initDisplayStreamToggle();
  const box = document.getElementById('multiAutostartToggle');
  if (!box) return;
  box.addEventListener('change', async () => {
    const wanted = box.checked;
    const r = await MultiApi.setAutostart(wanted);
    if (!r || r.status !== 'ok') {
      box.checked = !wanted;
      log.warn('Не удалось изменить автостарт мультипросмотра');
      return;
    }
    AS.on = wanted;
    log.success('Автостарт мультипросмотра: ' + (wanted ? 'включён' : 'выключен'));
  });
}

// читаем один раз при открытии страницы: галочка и сохранённая раскладка
async function loadMultiAutostart() {
  const cfg = await MultiApi.config();
  AS.on = !!(cfg && cfg.autostart);
  AS.saved = (cfg && cfg.layout) || null;
  AS.pending = takeUpdateFlag();
  const box = document.getElementById('multiAutostartToggle');
  if (box) box.checked = AS.on;
}

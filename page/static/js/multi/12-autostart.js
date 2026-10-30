// ---------- автостарт мультипросмотра ----------
// Раскладку (сколько ячеек и какая камера в какой) сервер помнит всегда (plate_config.multi_layout).
// Галочка «Автостарт» (взаимоисключается с «Автостартом цикла» микроскопа) говорит приложению открывать /multi при старте:
// страница сама расставляет камеры по ячейкам и подключает их. Чтобы открывалось быстро: сначала сохранённые RTSP-камеры
// (локальный файл) и сразу их потоки, а медленный опрос GigE-камер идёт после — их ячейки поднимаются, когда опрос закончен.
const AS = { ready: false, timer: null, on: false, saved: null, layoutDone: false };

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
  const saved = AS.saved;
  if (!AS.on || !saved || !Array.isArray(saved.tiles)) return;
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
    startStream(i);
  });
  setFocus(0);
}

function initMultiAutostart() {
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
  const box = document.getElementById('multiAutostartToggle');
  if (box) box.checked = AS.on;
}

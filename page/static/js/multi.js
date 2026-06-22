const log = {
  info: (m, p) => window.AppLog?.info('multi', m, p),
  success: (m, p) => window.AppLog?.success('multi', m, p),
  warn: (m, p) => window.AppLog?.warn('multi', m, p),
  error: (m, p) => window.AppLog?.error('multi', m, p),
  debug: (m, p) => window.AppLog?.debug('multi', m, p),
};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (symbol) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[symbol]));
}

// ---------- состояние ----------
const state = {
  layout: 1,
  // ячейка: { serial, connected, photo, video, streamUrl }
  tiles: [],
  focused: 0,
  cameras: [], // [{ serial, model, available }]
};

// ---------- доступные камеры (источник для шторки и выпадающих списков) ----------
async function loadCameras() {
  const data = await CameraApi.getCamsDetailed();
  const cameras = [];

  if (data) {
    for (const [serial, entries] of Object.entries(data)) {
      const list = entries || [];
      const entry = list.find((e) => e.available) || list[0] || {};
      cameras.push({ serial, model: entry.model || '', available: !!entry.available });
    }
  }

  cameras.sort((a, b) => String(a.serial).localeCompare(String(b.serial), 'ru'));
  state.cameras = cameras;

  renderDrawer();
  refreshTileSelects();
  log.info('Список камер загружен', { count: cameras.length });
}

// ---------- шторка слева (источник перетаскивания) ----------
function renderDrawer() {
  const list = document.getElementById('multiDrawerList');
  if (!list) return;

  list.innerHTML = '';

  if (!state.cameras.length) {
    list.innerHTML = '<div class="multi-drawer__empty">Камеры не найдены. Нажмите «Обновить».</div>';
    return;
  }

  state.cameras.forEach((cam) => {
    const chip = document.createElement('div');
    chip.className = 'camera-chip' + (cam.available ? '' : ' camera-chip--off');
    chip.draggable = cam.available;
    chip.dataset.serial = cam.serial;
    chip.innerHTML = `
      <span class="camera-chip__serial">${escapeHtml(cam.serial)}</span>
      <span class="camera-chip__model">${escapeHtml(cam.model || 'камера')}</span>
    `;

    chip.addEventListener('dragstart', (event) => {
      event.dataTransfer.setData('text/plain', cam.serial);
      event.dataTransfer.effectAllowed = 'copy';
      chip.classList.add('is-dragging');
    });
    chip.addEventListener('dragend', () => chip.classList.remove('is-dragging'));

    list.appendChild(chip);
  });
}

// ---------- раскладка ----------
function setLayout(n) {
  // перед перестройкой аккуратно гасим все живые потоки
  state.tiles.forEach((tile) => {
    if (tile.connected && tile.serial) stopStream(tile.serial);
  });

  const old = state.tiles;
  state.tiles = [];
  for (let i = 0; i < n; i += 1) {
    const prev = old[i];
    state.tiles.push({
      serial: prev ? prev.serial : null,
      connected: false,
      photo: false,
      video: false,
      streamUrl: null,
    });
  }

  state.layout = n;
  if (state.focused >= n) state.focused = 0;

  renderGrid();
  updateLayoutButtons();
  updateToolbar();
  log.info('Раскладка изменена', { layout: n });
}

function updateLayoutButtons() {
  document.querySelectorAll('.layout-btn').forEach((btn) => {
    btn.classList.toggle('is-active', Number(btn.dataset.layout) === state.layout);
  });
}

function getTileEl(index) {
  return document.querySelector(`.multi-tile[data-tile="${index}"]`);
}

function renderGrid() {
  const grid = document.getElementById('multiGrid');
  if (!grid) return;

  grid.className = `multi-grid multi-grid--${state.layout}`;
  grid.innerHTML = '';

  state.tiles.forEach((tile, index) => {
    const el = document.createElement('div');
    el.className = 'multi-tile';
    el.dataset.tile = String(index);
    el.innerHTML = `
      <div class="multi-tile__head">
        <select class="multi-tile__serial" data-tile-serial title="Серийный номер камеры в ячейке"></select>
        <span class="multi-tile__badge" data-tile-badge></span>
      </div>
      <div class="multi-tile__screen" data-tile-screen>
        <img class="multi-tile__frame hidden" alt="Кадр камеры" data-tile-frame />
        <div class="multi-tile__placeholder" data-tile-placeholder>NO CAMERA</div>
      </div>
      <div class="multi-tile__status">
        <span>FPS<strong data-metric="fps">0.00</strong></span>
        <span>Кадры<strong data-metric="images">0</strong></span>
        <span>Мбит/с<strong data-metric="bandwidth">0.0</strong></span>
        <span>Разрешение<strong data-metric="resolution">0 × 0</strong></span>
        <span>Ошибки<strong data-metric="errors">0</strong></span>
      </div>
    `;

    // клик по ячейке — перевести на неё фокус единого меню
    el.addEventListener('click', () => setFocus(index));

    // drag-n-drop: ячейка — приёмник камеры из шторки
    el.addEventListener('dragover', (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
      el.classList.add('is-drop');
    });
    el.addEventListener('dragleave', () => el.classList.remove('is-drop'));
    el.addEventListener('drop', (event) => {
      event.preventDefault();
      el.classList.remove('is-drop');
      const serial = event.dataTransfer.getData('text/plain');
      if (serial) assignSerial(index, serial);
    });

    // выпадающий серийник — ручной выбор камеры + перевод фокуса
    const select = el.querySelector('[data-tile-serial]');
    select.addEventListener('change', () => assignSerial(index, select.value || null));
    select.addEventListener('click', (event) => event.stopPropagation());

    grid.appendChild(el);
  });

  refreshTileSelects();
  renderAllTiles();
  highlightFocus();
}

// заполнить выпадающие списки серийников во всех ячейках
function refreshTileSelects() {
  state.tiles.forEach((tile, index) => {
    const el = getTileEl(index);
    if (!el) return;
    const select = el.querySelector('[data-tile-serial]');
    if (!select) return;

    const options = ['<option value="">— камера —</option>'].concat(
      state.cameras.map((cam) =>
        `<option value="${escapeHtml(cam.serial)}">${escapeHtml(cam.serial)}${cam.model ? ' · ' + escapeHtml(cam.model) : ''}</option>`
      )
    );
    select.innerHTML = options.join('');
    select.value = tile.serial || '';
  });
}

function renderTile(index) {
  const tile = state.tiles[index];
  const el = getTileEl(index);
  if (!tile || !el) return;

  const badge = el.querySelector('[data-tile-badge]');
  const placeholder = el.querySelector('[data-tile-placeholder]');
  const frame = el.querySelector('[data-tile-frame]');
  const select = el.querySelector('[data-tile-serial]');

  if (select) select.value = tile.serial || '';
  if (badge) badge.textContent = tile.connected ? '● в эфире' : (tile.serial ? 'готова' : 'пусто');

  el.classList.toggle('is-live', tile.connected);
  el.classList.toggle('is-empty', !tile.serial);

  if (!tile.connected) {
    if (frame) { frame.classList.add('hidden'); frame.src = ''; }
    if (placeholder) placeholder.classList.remove('hidden');
  } else {
    if (placeholder) placeholder.classList.add('hidden');
    if (frame) frame.classList.remove('hidden');
  }
}

function renderAllTiles() {
  state.tiles.forEach((_, index) => renderTile(index));
}

// ---------- назначение камеры на ячейку ----------
function assignSerial(index, serial) {
  serial = serial || null;

  // одна камера — максимум в одной ячейке: убрать её из других
  if (serial) {
    state.tiles.forEach((tile, i) => {
      if (i !== index && tile.serial === serial) {
        if (tile.connected) stopStream(serial);
        tile.serial = null;
        tile.connected = false;
        tile.photo = false;
        tile.video = false;
        renderTile(i);
      }
    });
  }

  const tile = state.tiles[index];
  if (tile.serial && tile.serial !== serial && tile.connected) {
    stopStream(tile.serial);
    tile.connected = false;
    tile.photo = false;
    tile.video = false;
  }

  tile.serial = serial;
  renderTile(index);
  setFocus(index);
  log.info('Камера назначена на ячейку', { tile: index, serial });
}

// ---------- фокус ----------
function setFocus(index) {
  state.focused = index;
  highlightFocus();
  updateToolbar();
}

function highlightFocus() {
  document.querySelectorAll('.multi-tile').forEach((el) => {
    el.classList.toggle('is-focused', Number(el.dataset.tile) === state.focused);
  });
}

// ---------- единое меню (действует на камеру в фокусе) ----------
function updateToolbar() {
  const tile = state.tiles[state.focused] || {};
  const serial = tile.serial || null;

  const focusEl = document.getElementById('multiFocusSerial');
  if (focusEl) focusEl.textContent = serial || '— не выбрана —';

  const connectBtn = document.getElementById('multiConnectBtn');
  const disconnectBtn = document.getElementById('multiDisconnectBtn');
  const photoBtn = document.getElementById('multiPhotoBtn');
  const videoBtn = document.getElementById('multiVideoBtn');

  const hasSerial = !!serial;
  if (connectBtn) connectBtn.disabled = !hasSerial || tile.connected;
  if (disconnectBtn) disconnectBtn.disabled = !hasSerial || !tile.connected;
  if (photoBtn) photoBtn.disabled = !hasSerial || !tile.connected;
  if (videoBtn) videoBtn.disabled = !hasSerial || !tile.connected;

  toggleIndicator('multiPhotoIndicator', tile.photo);
  toggleIndicator('multiVideoIndicator', tile.video);
}

function toggleIndicator(id, on) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('hidden', !on);
}

// ---------- стрим ----------
function startStream(index) {
  const tile = state.tiles[index];
  if (!tile || !tile.serial || tile.connected) return;

  const el = getTileEl(index);
  const frame = el?.querySelector('[data-tile-frame]');
  if (!frame) return;

  tile.streamUrl = `/api/camera/stream?serial_number=${encodeURIComponent(tile.serial)}`;
  tile.connected = true;
  frame.src = tile.streamUrl;

  renderTile(index);
  updateToolbar();
  log.success('Старт потока в ячейке', { tile: index, serial: tile.serial });
}

async function stopStream(serial) {
  if (!serial) return;
  try {
    await CameraApi.closeStreamForce(serial);
  } catch (error) {
    /* поток мог уже закрыться — не критично */
  }
}

async function disconnectTile(index) {
  const tile = state.tiles[index];
  if (!tile || !tile.serial) return;

  const el = getTileEl(index);
  const frame = el?.querySelector('[data-tile-frame]');
  if (frame) frame.src = '';

  const serial = tile.serial;
  tile.connected = false;
  tile.photo = false;
  tile.video = false;

  renderTile(index);
  resetTileMetrics(index);
  updateToolbar();

  await stopStream(serial);
  log.info('Отключение ячейки', { tile: index, serial });
}

// ---------- фото / видео (для камеры в фокусе) ----------
async function togglePhoto(index) {
  const tile = state.tiles[index];
  if (!tile || !tile.serial || !tile.connected) return;

  if (!tile.photo) {
    await CameraApi.startPhotoSaving(tile.serial, 5);
    tile.photo = true;
    log.info('Автосохранение фото включено', { serial: tile.serial });
  } else {
    await CameraApi.stopPhotoSaving(tile.serial);
    tile.photo = false;
    log.info('Автосохранение фото выключено', { serial: tile.serial });
  }
  updateToolbar();
}

async function toggleVideo(index) {
  const tile = state.tiles[index];
  if (!tile || !tile.serial || !tile.connected) return;

  if (!tile.video) {
    // большая длительность = пишем до ручной остановки
    await CameraApi.startVideoSaving(tile.serial, 86400);
    tile.video = true;
    log.info('Запись видео включена', { serial: tile.serial });
  } else {
    await CameraApi.stopVideoSaving(tile.serial);
    tile.video = false;
    log.info('Запись видео остановлена', { serial: tile.serial });
  }
  updateToolbar();
}

// ---------- метрики (строка состояния каждой ячейки) ----------
function setMetric(el, name, value) {
  const node = el.querySelector(`[data-metric="${name}"]`);
  if (node) node.textContent = value;
}

function updateTileMetrics(index, m) {
  const el = getTileEl(index);
  if (!el) return;
  setMetric(el, 'fps', Number(m.fps ?? 0).toFixed(2));
  setMetric(el, 'images', m.image_number ?? 0);
  setMetric(el, 'bandwidth', Number(m.bandwidth_mbps ?? 0).toFixed(1));
  setMetric(el, 'resolution', `${m.width ?? 0} × ${m.height ?? 0}`);
  setMetric(el, 'errors', m.errors ?? 0);
}

function resetTileMetrics(index) {
  updateTileMetrics(index, { fps: 0, image_number: 0, bandwidth_mbps: 0, width: 0, height: 0, errors: 0 });
}

function startMetricsPolling() {
  setInterval(async () => {
    for (let i = 0; i < state.tiles.length; i += 1) {
      const tile = state.tiles[i];
      if (!tile.connected || !tile.serial) continue;
      const metrics = await CameraApi.getMetrics(tile.serial);
      if (metrics && !metrics.error) updateTileMetrics(i, metrics);
    }
  }, 1000);
}

// ---------- инициализация ----------
function initLayoutButtons() {
  document.querySelectorAll('.layout-btn').forEach((btn) => {
    btn.addEventListener('click', () => setLayout(Number(btn.dataset.layout)));
  });
}

function initToolbar() {
  document.getElementById('multiConnectBtn')?.addEventListener('click', () => startStream(state.focused));
  document.getElementById('multiDisconnectBtn')?.addEventListener('click', () => disconnectTile(state.focused));
  document.getElementById('multiPhotoBtn')?.addEventListener('click', () => togglePhoto(state.focused));
  document.getElementById('multiVideoBtn')?.addEventListener('click', () => toggleVideo(state.focused));
}

async function initMultiPage() {
  log.info('Инициализация страницы мультипоточности');

  initLayoutButtons();
  initToolbar();
  document.getElementById('multiRefreshBtn')?.addEventListener('click', loadCameras);

  setLayout(1);
  await loadCameras();
  startMetricsPolling();

  // при уходе со страницы — закрыть все открытые потоки на бэкенде
  window.addEventListener('beforeunload', () => {
    state.tiles.forEach((tile) => {
      if (tile.connected && tile.serial) {
        fetch(`/api/camera/close_stream_force?serial_number=${encodeURIComponent(tile.serial)}`, { keepalive: true }).catch(() => {});
      }
    });
  });
}

window.addEventListener('DOMContentLoaded', () => {
  initMultiPage().catch((error) => {
    log.error('Ошибка инициализации мультипоточности', { error: error?.message ?? String(error) });
  });
});

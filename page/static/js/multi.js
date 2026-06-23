const log = {
  info: (m, p) => window.AppLog?.info('multi', m, p),
  success: (m, p) => window.AppLog?.success('multi', m, p),
  warn: (m, p) => window.AppLog?.warn('multi', m, p),
  error: (m, p) => window.AppLog?.error('multi', m, p),
  debug: (m, p) => window.AppLog?.debug('multi', m, p),
};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (s) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[s]));
}

const INFO_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><line x1="12" y1="11" x2="12" y2="16"></line><circle cx="12" cy="7.5" r="1" fill="currentColor" stroke="none"></circle></svg>';

const PHOTO_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>';

const VIDEO_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>';

function defaultGigeSettings() {
  return {
    width: 2448, height: 2048, offset_x: 0, offset_y: 0,
    fps: 1, exposure_auto: 'Off', exposure_time: 10000, pixel_format: '',
  };
}

// ---------- состояние ----------
const state = {
  layout: 1,
  tiles: [],          // { serial, kind, connected, photo, video }
  focused: 0,
  cameras: [],        // источники: { serial, kind:'gige'|'rtsp', label, model, ip, available, settings, connection }
  expandedSerial: null,
  rtspCounter: 0,
};

function getSource(serial) {
  return state.cameras.find((c) => c.serial === serial) || null;
}

function apiFor(kind) {
  return kind === 'rtsp' ? RtspApi : CameraApi;
}

// ---------- доступные камеры (GigE из сети + добавленные RTSP) ----------
async function loadCameras() {
  const rtsp = state.cameras.filter((c) => c.kind === 'rtsp');

  const data = await CameraApi.getCamsDetailed();
  const gige = [];
  const serials = [];

  if (data) {
    for (const [serial, entries] of Object.entries(data)) {
      const list = entries || [];
      const entry = list.find((e) => e.available) || list[0] || {};
      gige.push({
        serial,
        kind: 'gige',
        label: serial,
        model: entry.model || '',
        ip: null,
        available: !!entry.available,
        settings: defaultGigeSettings(),
        connection: null,
      });
      if (entry.available) serials.push(serial);
    }
  }

  // IP запрашиваем по одному на серийник (как на главной — без гонки за control)
  for (const serial of serials) {
    const response = await CameraApi.getIp(serial);
    const cam = gige.find((c) => c.serial === serial);
    if (cam && response?.ip) cam.ip = response.ip;
  }

  // переносим уже введённые пользователем настройки GigE между обновлениями
  const prevGige = state.cameras.filter((c) => c.kind === 'gige');
  gige.forEach((cam) => {
    const prev = prevGige.find((c) => c.serial === cam.serial);
    if (prev) cam.settings = prev.settings;
  });

  gige.sort((a, b) => String(a.serial).localeCompare(String(b.serial), 'ru'));
  state.cameras = gige.concat(rtsp);

  renderDrawer();
  refreshTileSelects();
  log.info('Список камер загружен', { gige: gige.length, rtsp: rtsp.length });
}

// ---------- шторка слева (источники, drag + настройки) ----------
function renderDrawer() {
  const list = document.getElementById('multiDrawerList');
  if (!list) return;

  list.innerHTML = '';

  if (!state.cameras.length) {
    list.innerHTML = '<div class="multi-drawer__empty">Камеры не найдены. Нажмите «Обновить» или «+ RTSP».</div>';
    return;
  }

  state.cameras.forEach((cam) => {
    const chip = document.createElement('div');
    const draggable = cam.kind === 'rtsp' || cam.available;
    chip.className = 'camera-chip' + (draggable ? '' : ' camera-chip--off');
    chip.draggable = false; // тащим только за шапку карточки, не за всю
    chip.dataset.source = cam.serial;

    const ipText = cam.kind === 'rtsp'
      ? (cam.ip || 'по URL')
      : (cam.ip || (cam.available ? '—' : 'недоступна'));

    chip.innerHTML = `
      <div class="camera-chip__main" data-chip-toggle draggable="${draggable}">
        <div class="camera-chip__info">
          <span class="camera-chip__serial">${escapeHtml(cam.label)}</span>
          <span class="camera-chip__model">${escapeHtml(cam.model || (cam.kind === 'rtsp' ? 'RTSP' : 'камера'))}</span>
          <span class="camera-chip__ip">IP: ${escapeHtml(ipText)}</span>
        </div>
        <div class="camera-chip__actions">
          ${cam.kind === 'gige' ? `<button type="button" class="camera-chip__info-btn" data-chip-info title="Информация о камере" aria-label="Информация о камере">${INFO_SVG}</button>` : ''}
          ${cam.kind === 'rtsp' ? `<button type="button" class="camera-chip__remove" data-chip-remove title="Удалить камеру" aria-label="Удалить">×</button>` : ''}
        </div>
      </div>
      <div class="camera-chip__settings" draggable="false" ${state.expandedSerial === cam.serial ? '' : 'hidden'}></div>
    `;

    const main = chip.querySelector('[data-chip-toggle]');
    if (draggable) {
      main.addEventListener('dragstart', (event) => {
        event.dataTransfer.setData('text/plain', cam.serial);
        event.dataTransfer.effectAllowed = 'copy';
        chip.classList.add('is-dragging');
      });
      main.addEventListener('dragend', () => chip.classList.remove('is-dragging'));
    }
    main.addEventListener('click', (event) => {
      if (event.target.closest('[data-chip-info]') || event.target.closest('[data-chip-remove]')) return;
      toggleChipSettings(cam.serial);
    });

    const infoBtn = chip.querySelector('[data-chip-info]');
    if (infoBtn) {
      infoBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        openInfoModal(cam.serial);
      });
    }

    const removeBtn = chip.querySelector('[data-chip-remove]');
    if (removeBtn) {
      removeBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        removeSource(cam.serial);
      });
    }

    const settingsBox = chip.querySelector('.camera-chip__settings');
    if (state.expandedSerial === cam.serial) {
      renderChipSettings(settingsBox, cam);
    }

    list.appendChild(chip);
  });
}

function toggleChipSettings(serial) {
  state.expandedSerial = state.expandedSerial === serial ? null : serial;
  renderDrawer();
}

// удаление источника (для добавленных вручную RTSP-камер)
function removeSource(serial) {
  state.tiles.forEach((tile, i) => {
    if (tile.serial === serial) {
      if (tile.connected) stopStream(serial, tile.kind);
      tile.serial = null;
      tile.kind = null;
      tile.connected = false;
      tile.photo = false;
      tile.video = false;
      renderTile(i);
    }
  });
  state.cameras = state.cameras.filter((c) => c.serial !== serial);
  if (state.expandedSerial === serial) state.expandedSerial = null;
  renderDrawer();
  refreshTileSelects();
  updateToolbar();
  log.info('Источник удалён', { serial });
}

// форма настроек внутри карточки источника (GigE — параметры камеры; RTSP — сводка)
function renderChipSettings(box, cam) {
  if (!box) return;

  if (cam.kind === 'rtsp') {
    const c = cam.connection || {};
    box.innerHTML = `
      <div class="chip-rtsp-summary">
        <div>URL: <strong>${escapeHtml(c.url || '—')}</strong></div>
        <div>Масштаб: <strong>${escapeHtml(c.scale ?? 100)}%</strong>, FPS: <strong>${escapeHtml(c.fps || 'авто')}</strong></div>
      </div>
    `;
    return;
  }

  const s = cam.settings;
  box.innerHTML = `
    <div class="chip-settings__grid">
      <label>Ширина<input type="number" data-set="width" value="${escapeHtml(s.width)}" /></label>
      <label>Высота<input type="number" data-set="height" value="${escapeHtml(s.height)}" /></label>
      <label>Смещение X<input type="number" data-set="offset_x" value="${escapeHtml(s.offset_x)}" /></label>
      <label>Смещение Y<input type="number" data-set="offset_y" value="${escapeHtml(s.offset_y)}" /></label>
      <label>FPS<input type="number" step="0.1" data-set="fps" value="${escapeHtml(s.fps)}" /></label>
      <label>Экспозиция<input type="number" data-set="exposure_time" value="${escapeHtml(s.exposure_time)}" /></label>
      <label class="chip-settings__wide">Автоэкспозиция
        <select data-set="exposure_auto">
          <option value="Off"${s.exposure_auto === 'Off' ? ' selected' : ''}>Off</option>
          <option value="Once"${s.exposure_auto === 'Once' ? ' selected' : ''}>Once</option>
          <option value="Continuous"${s.exposure_auto === 'Continuous' ? ' selected' : ''}>Continuous</option>
        </select>
      </label>
      <label class="chip-settings__wide">Формат пикселей (цвет)
        <select data-set="pixel_format">
          <option value=""${!s.pixel_format ? ' selected' : ''}>— как есть —</option>
          ${['RGB8', 'BGR8', 'Mono8', 'BayerRG8', 'BayerGB8', 'BayerGR8', 'BayerBG8', 'YUV422_8'].map((f) => `<option value="${f}"${s.pixel_format === f ? ' selected' : ''}>${f}</option>`).join('')}
        </select>
      </label>
    </div>
    <p class="chip-settings__hint">Настройки сохраняются за камерой и применяются при подключении.</p>
  `;

  box.querySelectorAll('[data-set]').forEach((input) => {
    input.addEventListener('click', (e) => e.stopPropagation());
    input.addEventListener('change', () => {
      const key = input.dataset.set;
      cam.settings[key] = input.value;
      log.debug('Настройка камеры изменена', { serial: cam.serial, [key]: input.value });
    });
  });
}

// ---------- раскладка ----------
function setLayout(n) {
  state.tiles.forEach((tile) => {
    if (tile.connected && tile.serial) stopStream(tile.serial, tile.kind);
  });

  const old = state.tiles;
  state.tiles = [];
  for (let i = 0; i < n; i += 1) {
    const prev = old[i];
    state.tiles.push({
      serial: prev ? prev.serial : null,
      kind: prev ? prev.kind : null,
      connected: false, photo: false, video: false,
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
        <select class="multi-tile__serial" data-tile-serial title="Камера в ячейке"></select>
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
        <div class="multi-tile__rec">
          <span class="rec-icon" data-rec-photo title="Сохранение фото">${PHOTO_SVG}</span>
          <span class="rec-icon" data-rec-video title="Запись видео">${VIDEO_SVG}</span>
        </div>
      </div>
    `;

    el.addEventListener('click', () => setFocus(index));

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
      if (serial) assignSource(index, serial);
    });

    const select = el.querySelector('[data-tile-serial]');
    select.addEventListener('change', () => assignSource(index, select.value || null));
    select.addEventListener('click', (event) => event.stopPropagation());

    grid.appendChild(el);
  });

  refreshTileSelects();
  renderAllTiles();
  highlightFocus();
}

function refreshTileSelects() {
  state.tiles.forEach((tile, index) => {
    const el = getTileEl(index);
    if (!el) return;
    const select = el.querySelector('[data-tile-serial]');
    if (!select) return;

    const options = ['<option value="">— камера —</option>'].concat(
      state.cameras.map((cam) =>
        `<option value="${escapeHtml(cam.serial)}">${escapeHtml(cam.label)}${cam.model ? ' · ' + escapeHtml(cam.model) : ''}</option>`
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

  const source = tile.serial ? getSource(tile.serial) : null;
  const badge = el.querySelector('[data-tile-badge]');
  const placeholder = el.querySelector('[data-tile-placeholder]');
  const frame = el.querySelector('[data-tile-frame]');
  const select = el.querySelector('[data-tile-serial]');

  if (select) select.value = tile.serial || '';
  if (badge) badge.textContent = tile.connected ? '● в эфире' : (source ? 'готова' : 'пусто');

  el.classList.toggle('is-live', tile.connected);
  el.classList.toggle('is-empty', !source);

  const recPhoto = el.querySelector('[data-rec-photo]');
  const recVideo = el.querySelector('[data-rec-video]');
  if (recPhoto) recPhoto.classList.toggle('is-active', !!tile.photo);
  if (recVideo) recVideo.classList.toggle('is-active', !!tile.video);

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

// ---------- назначение источника на ячейку ----------
function assignSource(index, serial) {
  serial = serial || null;
  const source = serial ? getSource(serial) : null;

  if (source) {
    state.tiles.forEach((tile, i) => {
      if (i !== index && tile.serial === serial) {
        if (tile.connected) stopStream(serial, tile.kind);
        tile.serial = null; tile.kind = null;
        tile.connected = false; tile.photo = false; tile.video = false;
        renderTile(i);
      }
    });
  }

  const tile = state.tiles[index];
  if (tile.serial && tile.serial !== serial && tile.connected) {
    stopStream(tile.serial, tile.kind);
    tile.connected = false; tile.photo = false; tile.video = false;
  }

  tile.serial = serial;
  tile.kind = source ? source.kind : null;
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

// ---------- единое меню ----------
function updateToolbar() {
  const tile = state.tiles[state.focused] || {};
  const source = tile.serial ? getSource(tile.serial) : null;

  const focusEl = document.getElementById('multiFocusSerial');
  if (focusEl) focusEl.textContent = source ? source.label : '— не выбрана —';

  const hasSource = !!source;
  setDisabled('multiConnectBtn', !hasSource || tile.connected);
  setDisabled('multiDisconnectBtn', !hasSource || !tile.connected);
  setDisabled('multiPhotoBtn', !hasSource || !tile.connected);
  setDisabled('multiVideoBtn', !hasSource || !tile.connected);

  toggleIndicator('multiPhotoIndicator', tile.photo);
  toggleIndicator('multiVideoIndicator', tile.video);
}

function setDisabled(id, disabled) {
  const el = document.getElementById(id);
  if (el) el.disabled = disabled;
}

function toggleIndicator(id, on) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('hidden', !on);
}

// ---------- стрим ----------
function buildStreamUrl(source) {
  if (source.kind === 'rtsp') {
    return RtspApi.buildStreamUrl(source.serial, source.connection || {});
  }
  const s = source.settings || {};
  const query = new URLSearchParams({ serial_number: source.serial });
  ['width', 'height', 'offset_x', 'offset_y', 'fps', 'exposure_auto', 'exposure_time', 'pixel_format'].forEach((key) => {
    const value = s[key];
    if (value !== '' && value !== null && value !== undefined) query.set(key, value);
  });
  return `/api/camera/stream?${query.toString()}`;
}

function startStream(index) {
  const tile = state.tiles[index];
  if (!tile || !tile.serial || tile.connected) return;
  const source = getSource(tile.serial);
  if (!source) return;

  const el = getTileEl(index);
  const frame = el?.querySelector('[data-tile-frame]');
  if (!frame) return;

  tile.connected = true;
  frame.src = buildStreamUrl(source);

  renderTile(index);
  updateToolbar();
  log.success('Старт потока в ячейке', { tile: index, serial: tile.serial, kind: tile.kind });
}

async function stopStream(serial, kind) {
  if (!serial) return;
  try {
    await apiFor(kind).closeStreamForce(serial);
  } catch (error) {
    /* поток мог уже закрыться */
  }
}

async function disconnectTile(index) {
  const tile = state.tiles[index];
  if (!tile || !tile.serial) return;

  const el = getTileEl(index);
  const frame = el?.querySelector('[data-tile-frame]');
  if (frame) frame.src = '';

  const { serial, kind } = tile;
  tile.connected = false; tile.photo = false; tile.video = false;

  renderTile(index);
  resetTileMetrics(index);
  updateToolbar();

  await stopStream(serial, kind);
  log.info('Отключение ячейки', { tile: index, serial });
}

// ---------- метрики ----------
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
      const metrics = await apiFor(tile.kind).getMetrics(tile.serial);
      if (metrics && !metrics.error) updateTileMetrics(i, metrics);
    }
  }, 1000);
}

// ---------- модалки ----------
function openModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  modal.classList.add('show');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  modal.classList.remove('show');
  modal.setAttribute('aria-hidden', 'true');
  if (!document.querySelector('.modal-backdrop.show')) {
    document.body.classList.remove('modal-open');
  }
}

function focusedSource() {
  const tile = state.tiles[state.focused] || {};
  return tile.serial ? getSource(tile.serial) : null;
}

// --- фото ---
function openPhotoModal() {
  const source = focusedSource();
  if (!source) return;
  const serialEl = document.getElementById('multiPhotoSerial');
  if (serialEl) serialEl.textContent = source.label;
  openModal('multiPhotoModal');
}

async function applyPhoto(on) {
  const tile = state.tiles[state.focused];
  const source = focusedSource();
  if (!tile || !source) return;
  const api = apiFor(tile.kind);
  if (on) {
    const interval = Number(document.getElementById('multiPhotoInterval')?.value) || 5;
    await api.startPhotoSaving(source.serial, interval);
    tile.photo = true;
  } else {
    await api.stopPhotoSaving(source.serial);
    tile.photo = false;
  }
  renderTile(state.focused);
  updateToolbar();
  closeModal('multiPhotoModal');
}

// --- видео ---
function openVideoModal() {
  const source = focusedSource();
  if (!source) return;
  const serialEl = document.getElementById('multiVideoSerial');
  if (serialEl) serialEl.textContent = source.label;
  openModal('multiVideoModal');
}

async function applyVideo(on) {
  const tile = state.tiles[state.focused];
  const source = focusedSource();
  if (!tile || !source) return;
  const api = apiFor(tile.kind);
  if (on) {
    const amount = Number(document.getElementById('multiVideoDuration')?.value) || 10;
    const unit = document.getElementById('multiVideoUnit')?.value || 'minutes';
    const seconds = unit === 'minutes' ? amount * 60 : amount;
    await api.startVideoSaving(source.serial, seconds);
    tile.video = true;
  } else {
    await api.stopVideoSaving(source.serial);
    tile.video = false;
  }
  renderTile(state.focused);
  updateToolbar();
  closeModal('multiVideoModal');
}

// --- информация о камере ---
async function openInfoModal(serial) {
  const source = getSource(serial);
  if (!source) return;

  const serialEl = document.getElementById('multiInfoSerial');
  const loader = document.getElementById('multiInfoLoader');
  const error = document.getElementById('multiInfoError');
  const listEl = document.getElementById('multiInfoList');

  if (serialEl) serialEl.textContent = source.label;
  if (error) { error.textContent = ''; error.classList.remove('show'); }
  if (listEl) listEl.innerHTML = '';
  if (loader) loader.classList.add('show');
  openModal('multiInfoModal');

  const data = await CameraApi.getCameraInfo(serial);
  if (loader) loader.classList.remove('show');

  if (!data || data.error) {
    if (error) { error.textContent = data?.error || 'Не удалось получить информацию'; error.classList.add('show'); }
    return;
  }

  if (listEl) {
    listEl.innerHTML = '';
    (data.items || []).forEach(({ label, value }) => {
      const row = document.createElement('div');
      row.className = 'info-row';
      row.innerHTML = `<dt class="info-row__label">${escapeHtml(label)}</dt><dd class="info-row__value">${escapeHtml(value)}</dd>`;
      listEl.appendChild(row);
    });
  }
}

// --- добавить RTSP ---
function openRtspModal() {
  const error = document.getElementById('multiRtspError');
  if (error) { error.textContent = ''; error.classList.remove('show'); }
  openModal('multiRtspModal');
}

function addRtspSource() {
  const form = document.getElementById('multiRtspForm');
  const error = document.getElementById('multiRtspError');
  if (!form) return;

  const fd = new FormData(form);
  const url = String(fd.get('url') || '').trim();
  const ip = String(fd.get('ip') || '').trim();

  if (!url && !ip) {
    if (error) { error.textContent = 'Укажите RTSP URL или IP-адрес'; error.classList.add('show'); }
    return;
  }

  const username = String(fd.get('username') || '').trim();
  const password = String(fd.get('password') || '');
  const port = Number(fd.get('port')) || 554;
  const channel = Number(fd.get('channel')) || 1;
  const subtype = Number(fd.get('subtype')) || 0;
  const scale = Number(fd.get('scale')) || 100;
  const fps = Number(fd.get('fps')) || 0;
  const name = String(fd.get('name') || '').trim();

  // если URL не задан — собираем Dahua/Hikvision-совместимый из IP
  let resolvedUrl = url;
  if (!resolvedUrl && ip) {
    const cred = username ? `${username}:${password}@` : '';
    resolvedUrl = `rtsp://${cred}${ip}:${port}/cam/realmonitor?channel=${channel}&subtype=${subtype}`;
  }

  const connection = { url: resolvedUrl, scale, fps: fps || null };

  // повторное добавление той же камеры (тот же URL) — обновляем на месте
  // с новыми настройками, а не создаём дубль
  const existing = state.cameras.find(
    (c) => c.kind === 'rtsp' && c.connection && c.connection.url === resolvedUrl
  );
  if (existing) {
    existing.label = name || existing.label;
    existing.ip = ip || existing.ip;
    existing.connection = connection;
    // если уже в эфире — перезапускаем поток с новыми настройками
    state.tiles.forEach((tile, i) => {
      if (tile.serial === existing.serial && tile.connected) {
        const el = getTileEl(i);
        const frame = el?.querySelector('[data-tile-frame]');
        if (frame) frame.src = buildStreamUrl(existing);
      }
    });
    renderDrawer();
    refreshTileSelects();
    closeModal('multiRtspModal');
    form.reset();
    log.info('RTSP-камера обновлена', { serial: existing.serial, url: resolvedUrl });
    return;
  }

  state.rtspCounter += 1;
  const serial = `rtsp_${state.rtspCounter}`;
  state.cameras.push({
    serial,
    kind: 'rtsp',
    label: name || `RTSP ${ip || state.rtspCounter}`,
    model: 'RTSP',
    ip: ip || null,
    available: true,
    settings: null,
    connection,
  });

  renderDrawer();
  refreshTileSelects();
  closeModal('multiRtspModal');
  form.reset();
  log.success('Добавлена RTSP-камера', { serial, ip, url: resolvedUrl });
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
  document.getElementById('multiPhotoBtn')?.addEventListener('click', openPhotoModal);
  document.getElementById('multiVideoBtn')?.addEventListener('click', openVideoModal);
}

function initModals() {
  document.getElementById('multiPhotoClose')?.addEventListener('click', () => closeModal('multiPhotoModal'));
  document.getElementById('multiPhotoOn')?.addEventListener('click', () => applyPhoto(true));
  document.getElementById('multiPhotoOff')?.addEventListener('click', () => applyPhoto(false));

  document.getElementById('multiVideoClose')?.addEventListener('click', () => closeModal('multiVideoModal'));
  document.getElementById('multiVideoOn')?.addEventListener('click', () => applyVideo(true));
  document.getElementById('multiVideoOff')?.addEventListener('click', () => applyVideo(false));

  document.getElementById('multiInfoClose')?.addEventListener('click', () => closeModal('multiInfoModal'));
  document.getElementById('multiInfoCloseFooter')?.addEventListener('click', () => closeModal('multiInfoModal'));

  document.getElementById('multiAddRtspBtn')?.addEventListener('click', openRtspModal);
  document.getElementById('multiRtspClose')?.addEventListener('click', () => closeModal('multiRtspModal'));
  document.getElementById('multiRtspCancel')?.addEventListener('click', () => closeModal('multiRtspModal'));
  document.getElementById('multiRtspAdd')?.addEventListener('click', addRtspSource);

  // клик по фону и Esc закрывают модалки
  document.querySelectorAll('.modal-backdrop').forEach((modal) => {
    modal.addEventListener('click', (event) => {
      if (event.target === modal) closeModal(modal.id);
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    document.querySelectorAll('.modal-backdrop.show').forEach((modal) => closeModal(modal.id));
  });
}

async function initMultiPage() {
  log.info('Инициализация страницы мультипоточности');

  initLayoutButtons();
  initToolbar();
  initModals();
  document.getElementById('multiRefreshBtn')?.addEventListener('click', loadCameras);

  setLayout(1);
  await loadCameras();
  startMetricsPolling();

  window.addEventListener('beforeunload', () => {
    state.tiles.forEach((tile) => {
      if (tile.connected && tile.serial) {
        const base = tile.kind === 'rtsp' ? '/api/rtsp/close_stream_force' : '/api/camera/close_stream_force';
        fetch(`${base}?serial_number=${encodeURIComponent(tile.serial)}`, { keepalive: true }).catch(() => {});
      }
    });
  });
}

window.addEventListener('DOMContentLoaded', () => {
  initMultiPage().catch((error) => {
    log.error('Ошибка инициализации мультипоточности', { error: error?.message ?? String(error) });
  });
});

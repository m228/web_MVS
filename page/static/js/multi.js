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

const LIGHT_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.4 1 2.5h6c0-1.1.3-1.8 1-2.5A6 6 0 0 0 12 3z"></path></svg>';

const ZOOM_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"></path></svg>';

const REGION_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3"></path><path d="M8.5 12h7M12 8.5v7"></path></svg>';

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

  // IP запрашиваем строго ПО ОДНОМУ серийнику за раз (не параллельно) — как на
  // главной. Это короткая control-операция с ia.destroy() в finally; гонки за
  // control нет. Стрим ломал не этот опрос, а отдельные правки (см. bug.txt №2).
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

// подгрузка сохранённых RTSP-камер из мини-базы (после перезапуска)
async function loadSavedRtsp() {
  const data = await RtspApi.listSaved();
  const items = (data && data.items) || [];

  items.forEach((it) => {
    if (!it.url) return;
    if (state.cameras.some((c) => c.kind === 'rtsp' && c.connection && c.connection.url === it.url)) return;

    state.rtspCounter += 1;
    state.cameras.push({
      serial: `rtsp_${state.rtspCounter}`,
      kind: 'rtsp',
      label: it.label || `RTSP ${it.ip || ''}`.trim(),
      model: 'RTSP',
      ip: it.ip || null,
      available: true,
      settings: null,
      connection: { url: it.url, scale: it.scale ?? 100, fps: it.fps || null },
      saved: true,
    });
  });

  if (items.length) {
    renderDrawer();
    refreshTileSelects();
    log.info('Загружены сохранённые RTSP-камеры', { count: items.length });
  }
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
  const source = getSource(serial);

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

  // удалённую вручную RTSP-камеру убираем и из мини-базы
  if (source && source.kind === 'rtsp' && source.connection && source.connection.url) {
    RtspApi.removeSaved(source.connection.url);
  }

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
      <label>Автоэкспозиция
        <select data-set="exposure_auto">
          <option value="Off"${s.exposure_auto === 'Off' ? ' selected' : ''}>Off</option>
          <option value="Once"${s.exposure_auto === 'Once' ? ' selected' : ''}>Once</option>
          <option value="Continuous"${s.exposure_auto === 'Continuous' ? ' selected' : ''}>Continuous</option>
        </select>
      </label>
      <label>Формат (RGB)
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
function newTile() {
  return { serial: null, kind: null, connected: false, photo: false, video: false, light: false, zoomFactor: 1, live: true, el: null };
}

function setLayout(n) {
  // закрываем потоки ТОЛЬКО у ячеек, которые уходят (index >= n);
  // оставшиеся (index < n) сохраняют поток и DOM — не пересоздаём их
  for (let i = n; i < state.tiles.length; i += 1) {
    const tile = state.tiles[i];
    if (tile && tile.connected && tile.serial) stopStream(tile.serial, tile.kind);
  }

  if (state.tiles.length > n) {
    state.tiles.length = n;
  } else {
    while (state.tiles.length < n) state.tiles.push(newTile());
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

// создаём DOM ячейки один раз; индекс читаем из data-tile (он стабилен,
// т.к. ячейки убираются только с конца)
function createTileEl() {
  const el = document.createElement('div');
  el.className = 'multi-tile';
  el.innerHTML = `
    <div class="multi-tile__head">
      <select class="multi-tile__serial" data-tile-serial title="Камера в ячейке"></select>
      <span class="multi-tile__badge" data-tile-badge></span>
      <button type="button" class="tile-region-btn" data-tile-region title="Зум областью — выделите рамку" aria-label="Зум областью" hidden>${REGION_SVG}</button>
    </div>
    <div class="multi-tile__screen" data-tile-screen>
      <img class="multi-tile__frame hidden" alt="Кадр камеры" data-tile-frame />
      <div class="multi-tile__placeholder" data-tile-placeholder>NO CAMERA</div>
      <div class="tile-marquee-layer" data-tile-marquee hidden>
        <div class="tile-marquee-box" data-tile-marquee-box hidden></div>
      </div>
    </div>
    <div class="multi-tile__status">
      <span>FPS<strong data-metric="fps">0.00</strong></span>
      <span>Кадры<strong data-metric="images">0</strong></span>
      <span>Мбит/с<strong data-metric="bandwidth">0.0</strong></span>
      <span>Разрешение<strong data-metric="resolution">0 × 0</strong></span>
      <span>Ошибки<strong data-metric="errors">0</strong></span>
      <span>Фото<strong data-metric="photo_count">0</strong></span>
      <span>Видео<strong data-metric="video_time">—</strong></span>
      <div class="multi-tile__rec">
        <span class="rec-icon" data-rec-light title="Подсветка">${LIGHT_SVG}</span>
        <span class="rec-icon" data-rec-zoom title="Зум">${ZOOM_SVG}</span>
        <span class="rec-icon" data-rec-photo title="Сохранение фото">${PHOTO_SVG}</span>
        <span class="rec-icon" data-rec-video title="Запись видео">${VIDEO_SVG}</span>
      </div>
    </div>
  `;

  const idx = () => Number(el.dataset.tile);

  el.addEventListener('click', () => setFocus(idx()));
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
    if (serial) assignSource(idx(), serial);
  });

  const select = el.querySelector('[data-tile-serial]');
  select.addEventListener('change', () => assignSource(idx(), select.value || null));
  select.addEventListener('click', (event) => event.stopPropagation());

  // зум областью (рамкой) в ячейке
  const regionBtn = el.querySelector('[data-tile-region]');
  regionBtn.addEventListener('click', (event) => {
    event.stopPropagation();
    setFocus(idx());
    onTileRegionClick(idx());
  });
  const marquee = el.querySelector('[data-tile-marquee]');
  marquee.addEventListener('mousedown', (event) => {
    const tile = state.tiles[idx()];
    if (!tile || !tile.regionMode) return;
    event.preventDefault();
    event.stopPropagation();
    const p = tileLayerXY(marquee, event);
    tileDrag = { index: idx(), x0: p.x, y0: p.y, x1: p.x, y1: p.y };
    updateTileMarqueeBox();
  });

  return el;
}

function renderGrid() {
  const grid = document.getElementById('multiGrid');
  if (!grid) return;

  grid.className = `multi-grid multi-grid--${state.layout}`;

  // у живых ячеек DOM (и их <img> с потоком) сохраняем — создаём только новым
  state.tiles.forEach((tile) => {
    if (!tile.el) tile.el = createTileEl();
  });
  state.tiles.forEach((tile, index) => { tile.el.dataset.tile = String(index); });
  grid.replaceChildren(...state.tiles.map((tile) => tile.el));

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
  const stalled = tile.connected && tile.live === false;
  if (badge) {
    badge.textContent = tile.connected
      ? (stalled ? '● нет кадров' : '● в эфире')
      : (source ? 'готова' : 'пусто');
  }

  el.classList.toggle('is-live', tile.connected && !stalled);
  el.classList.toggle('is-stalled', stalled);
  el.classList.toggle('is-empty', !source);

  const recPhoto = el.querySelector('[data-rec-photo]');
  const recVideo = el.querySelector('[data-rec-video]');
  if (recPhoto) recPhoto.classList.toggle('is-active', !!tile.photo);
  if (recVideo) recVideo.classList.toggle('is-active', !!tile.video);

  // индикаторы подсветки и зума (только для RTSP-камеры в эфире)
  const isRtsp = source && source.kind === 'rtsp';
  const recLight = el.querySelector('[data-rec-light]');
  const recZoom = el.querySelector('[data-rec-zoom]');
  const zoomOn = isRtsp && Number(tile.zoomFactor) > 1;
  if (recLight) recLight.classList.toggle('is-active', isRtsp && !!tile.light && tile.connected);
  if (recZoom) recZoom.classList.toggle('is-active', zoomOn && tile.connected);

  if (!tile.connected) {
    if (frame) { frame.classList.add('hidden'); frame.src = ''; }
    if (placeholder) placeholder.classList.remove('hidden');
    tile.regionMode = false;
  } else {
    if (placeholder) placeholder.classList.add('hidden');
    if (frame) frame.classList.remove('hidden');
  }

  applyTileRegionUI(index); // кнопка «зум областью» и слой рамки
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
  // настройки (фото/видео/подсветка/зум) — когда камера в фокусе подключена
  setDisabled('multiSettingsBtn', !hasSource || !tile.connected);
  // конфиг — только для GigE (у RTSP его нет); доступен и после остановки
  setDisabled('multiConfigBtn', !hasSource || source.kind !== 'gige');

  // индикатор записи на кнопке настроек: активно фото или видео
  toggleIndicator('multiRecIndicator', tile.photo || tile.video);
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

  // Несколько GigE одновременно разрешены: на SDK у каждой свой handle/поток/resend,
  // потоки независимы (полоса — физика, лечится разрешением/fps/Bayer).
  doStartStream(index);
}

function doStartStream(index) {
  const tile = state.tiles[index];
  if (!tile || !tile.serial || tile.connected) return;
  const source = getSource(tile.serial);
  if (!source) return;

  const el = getTileEl(index);
  const frame = el?.querySelector('[data-tile-frame]');
  if (!frame) return;

  tile.connected = true;
  tile.live = true;
  tile._lastImages = undefined;
  tile._lastFrameTs = Date.now();
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

// секунды -> «M:SS» (или «H:MM:SS»)
// formatDuration / showSavePath вынесены в ui.js (общие для camera/multi/rtsp)

function updateTileMetrics(index, m) {
  const el = getTileEl(index);
  if (!el) return;
  setMetric(el, 'fps', Number(m.fps ?? 0).toFixed(2));
  setMetric(el, 'images', m.image_number ?? 0);
  setMetric(el, 'bandwidth', Number(m.bandwidth_mbps ?? 0).toFixed(1));
  setMetric(el, 'resolution', `${m.width ?? 0} × ${m.height ?? 0}`);
  setMetric(el, 'errors', m.errors ?? 0);
  setMetric(el, 'photo_count', m.photo_count ?? 0);
  // длительность показываем, только если ячейка пишет видео
  const tile = state.tiles[index];
  setMetric(el, 'video_time', tile && tile.video ? formatDuration(m.video_elapsed) : '—');
}

function resetTileMetrics(index) {
  updateTileMetrics(index, { fps: 0, image_number: 0, bandwidth_mbps: 0, width: 0, height: 0, errors: 0, photo_count: 0, video_elapsed: 0 });
}

// порог «зависания»: если за столько мс не пришло ни одного нового кадра,
// считаем камеру не «в эфире» (даже если поток формально открыт)
// окно «свежести» кадра: камера на 1 fps + потери пакетов растит image_number
// рывками; маленькое окно мигало "нет кадров" при редких просадках. 10 c терпимо.
const LIVENESS_MS = 10000;

let metricsTimer = null;
function startMetricsPolling() {
  metricsTimer = setInterval(async () => {
    const now = Date.now();
    for (let i = 0; i < state.tiles.length; i += 1) {
      const tile = state.tiles[i];
      if (!tile.connected || !tile.serial) continue;

      const metrics = await apiFor(tile.kind).getMetrics(tile.serial);
      if (!metrics || metrics.error) continue;

      // синхронизируем статус фото/видео с РЕАЛЬНЫМ состоянием бэкенда:
      // авто-запись видео могла сама завершиться по длительности (save_video=0),
      // тогда индикатор/значок надо погасить, а не держать по клику пользователя.
      if (metrics.photo !== undefined || metrics.video !== undefined) {
        const photoOn = !!metrics.photo;
        const videoOn = Number(metrics.video) === 1;
        if (photoOn !== tile.photo || videoOn !== tile.video) {
          tile.photo = photoOn;
          tile.video = videoOn;
          renderTile(i);
          if (i === state.focused) updateToolbar();
        }
      }

      // индикатор зума в ячейке: кратность приходит в метриках RTSP
      if (metrics.zoom_factor !== undefined) {
        const zf = Number(metrics.zoom_factor) || 1;
        if (zf !== tile.zoomFactor) { tile.zoomFactor = zf; renderTile(i); }
      }

      updateTileMetrics(i, metrics);

      // живость определяем по росту счётчика кадров
      const images = Number(metrics.image_number ?? 0);
      if (tile._lastImages === undefined || images > tile._lastImages) {
        tile._lastImages = images;
        tile._lastFrameTs = now;
      }
      const live = (now - (tile._lastFrameTs || now)) < LIVENESS_MS;
      // RTSP подключилась успешно (есть кадры) — сохраняем её в мини-базу
      if (live && tile.kind === 'rtsp') saveRtspIfNeeded(tile.serial);
      if (live !== tile.live) {
        tile.live = live;
        renderTile(i);
      }
    }
  }, 1000);
}

// сохранить RTSP-источник в базу один раз, когда он реально начал отдавать кадры
function saveRtspIfNeeded(serial) {
  const src = getSource(serial);
  if (!src || src.kind !== 'rtsp' || src.saved || !src.connection || !src.connection.url) return;
  src.saved = true;
  RtspApi.saveCam({
    url: src.connection.url,
    label: src.label,
    ip: src.ip,
    scale: src.connection.scale,
    fps: src.connection.fps,
  });
  log.info('RTSP-камера сохранена в базу (подключение удалось)', { serial, url: src.connection.url });
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

// ---------- настройки камеры в фокусе (фото/видео/подсветка) ----------
let settingsCaps = null;   // возможности RTSP-камеры в открытой модалке

// строка возможности: точка + текст (общая для подсветки/зума)
function setCapLine(el, supported, textYes, textNo) {
  if (!el) return;
  el.dataset.state = supported ? 'yes' : 'no';
  const textEl = el.querySelector('.cap-text');
  if (textEl) textEl.textContent = supported ? textYes : textNo;
}

// прочитать имя проекта из поля модалки; пустое -> предупреждение и null (имя обязательно)
function readMultiProjectName(inputId) {
  const el = document.getElementById(inputId);
  const value = el ? el.value.trim() : '';
  if (!value) {
    log.warn('Не указано имя проекта');
    alert('Укажите имя проекта');
    return null;
  }
  return value;
}

// выставить число + единицу (сек/мин) по значению в секундах: кратное 60 показываем в минутах
function setMultiInterval(inputId, unitId, seconds) {
  if (seconds == null || seconds === '') return;
  const input = document.getElementById(inputId);
  if (!input) return;
  let value = Number(seconds);
  const unitSel = document.getElementById(unitId);
  if (unitSel && value >= 60 && value % 60 === 0) {
    value = value / 60;
    unitSel.value = 'minutes';
  } else if (unitSel) {
    unitSel.value = 'seconds';
  }
  input.value = value;
}

// подтянуть сохранённые настройки (имя проекта + интервал/длительность) в модалку настроек
async function prefillMultiSaveSettings(serial, kind) {
  if (!serial) return;
  const s = await apiFor(kind).getSaveSettings(serial);
  if (!s || s.error) return;
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el && val != null && val !== '') el.value = val;
  };
  setVal('multiPhotoProject', s.photo_project);
  setVal('multiVideoProject', s.video_project);
  setMultiInterval('multiPhotoInterval', 'multiPhotoUnit', s.photo_interval);
  setMultiInterval('multiVideoDuration', 'multiVideoUnit', s.video_duration);
}

function openSettingsModal() {
  const source = focusedSource();
  const tile = state.tiles[state.focused];
  if (!source || !tile) return;

  const serialEl = document.getElementById('multiSettingsSerial');
  if (serialEl) serialEl.textContent = source.label;

  // зоны «Подсветка/Зум/Камера(FPS)» — только для RTSP (у GigE их нет)
  const isRtsp = source.kind === 'rtsp';
  document.querySelectorAll('#multiSettingsModal .settings-zone--rtsp').forEach((z) => { z.hidden = !isRtsp; });
  // у GigE остаются только фото+видео — сворачиваем сетку в один узкий столбик
  document.querySelector('#multiSettingsModal .settings-zones')?.classList.toggle('is-gige', !isRtsp);

  // подставить текущий FPS камеры
  const fpsInput = document.getElementById('multiFps');
  if (fpsInput) fpsInput.value = (source.connection && source.connection.fps) || '';

  showSavePath('multiPhotoSavePath', null);
  showSavePath('multiVideoSavePath', null);
  openModal('multiSettingsModal');

  // подтянуть сохранённые имя проекта / интервал для этой камеры
  prefillMultiSaveSettings(source.serial, source.kind);

  settingsCaps = null;
  if (isRtsp) refreshSettingsCaps(source.serial);
}

// изменить FPS RTSP-камеры в фокусе (перезапуск потока с новым ограничением)
function applyMultiFps() {
  const tile = state.tiles[state.focused];
  const source = focusedSource();
  if (!tile || !source || source.kind !== 'rtsp' || !source.connection) return;

  const val = Number(document.getElementById('multiFps')?.value);
  source.connection.fps = (val && val > 0) ? val : null;

  // перезапускаем поток с новым FPS, если камера в эфире
  if (tile.connected) {
    const el = getTileEl(state.focused);
    const frame = el?.querySelector('[data-tile-frame]');
    if (frame) frame.src = buildStreamUrl(source);
  }

  // обновляем сохранённую запись в мини-базе, чтобы FPS пережил перезапуск
  if (source.saved && source.connection.url) {
    RtspApi.saveCam({
      url: source.connection.url, label: source.label, ip: source.ip,
      scale: source.connection.scale, fps: source.connection.fps,
    });
  }

  log.info('FPS изменён', { serial: source.serial, fps: source.connection.fps });
}

async function refreshSettingsCaps(serial) {
  const data = await RtspApi.getCapabilities(serial);
  settingsCaps = (data && !data.error)
    ? data
    : { reachable: false, white_light: false, optical_zoom: false, image_settings: false };
  applySettingsCaps();
  syncMultiLightState(serial);
  refreshMultiImageSettings(serial);
}

function applySettingsCaps() {
  const caps = settingsCaps || {};
  const hasLight = !!caps.white_light;

  setCapLine(document.getElementById('multiLightCap'), hasLight,
    'Белый прожектор поддерживается',
    caps.reachable ? 'Подсветка не поддерживается' : 'Камера не отвечает на управление');
  const sw = document.getElementById('multiLightSwitch');
  const lv = document.getElementById('multiLightLevel');
  if (sw) sw.disabled = !hasLight;
  if (lv) lv.disabled = !hasLight;
  if (!hasLight) reflectMultiLight(false);
  // зум делается рамкой прямо в ячейке (кнопка на плитке), в настройках его нет

  // настройки изображения
  const hasImage = !!caps.image_settings;
  setCapLine(document.getElementById('multiImageCap'), hasImage,
    'Настройки изображения доступны',
    caps.reachable ? 'Настройки изображения не поддерживаются' : 'Камера не отвечает на управление');
  setMultiImageControlsEnabled(hasImage);
}

// --- настройки изображения (экспозиция / баланс белого / день-ночь) ---
function multiImageEls() {
  return {
    wb: document.getElementById('multiImageWb'),
    dayNight: document.getElementById('multiImageDayNight'),
    compensation: document.getElementById('multiImageCompensation'),
    gainMin: document.getElementById('multiImageGainMin'),
    gainMax: document.getElementById('multiImageGainMax'),
    cap: document.getElementById('multiImageCap'),
  };
}

function setMultiImageControlsEnabled(enabled) {
  const e = multiImageEls();
  [e.wb, e.dayNight, e.compensation, e.gainMin, e.gainMax].forEach((el) => {
    if (el) el.disabled = !enabled;
  });
}

function populateMultiImageUI(data) {
  const e = multiImageEls();
  UIHelpers.fillSelect(e.wb, data.wb_presets, UIHelpers.IMAGE_WB_LABELS,
    data.white_balance && data.white_balance.mode);
  UIHelpers.fillSelect(e.dayNight, data.day_night_modes, UIHelpers.IMAGE_DAY_NIGHT_LABELS,
    data.day_night && data.day_night.mode);
  const exp = data.exposure || {};
  if (e.compensation && exp.compensation != null) e.compensation.value = exp.compensation;
  if (e.gainMin && exp.gain_min != null) e.gainMin.value = exp.gain_min;
  if (e.gainMax && exp.gain_max != null) e.gainMax.value = exp.gain_max;
}

async function refreshMultiImageSettings(serial) {
  if (!(settingsCaps && settingsCaps.image_settings)) {
    setMultiImageControlsEnabled(false);
    return;
  }
  const data = await RtspApi.getImageSettings(serial);
  if (!data || data.error || !data.reachable) {
    setCapLine(multiImageEls().cap, false, '', 'Камера не отвечает на управление');
    setMultiImageControlsEnabled(false);
    log.warn('Не удалось получить настройки изображения', data);
    return;
  }
  setCapLine(multiImageEls().cap, true, 'Настройки изображения доступны', '');
  setMultiImageControlsEnabled(true);
  populateMultiImageUI(data);
}

async function applyMultiWhiteBalance() {
  const source = focusedSource();
  const e = multiImageEls();
  if (!source || source.kind !== 'rtsp' || !e.wb) return;
  const data = await RtspApi.setWhiteBalance(source.serial, e.wb.value);
  if (!data || data.error || data.ok === false) {
    log.warn('Камера не подтвердила баланс белого', data);
    refreshMultiImageSettings(source.serial);
    return;
  }
  log.success('Баланс белого применён', { mode: e.wb.value });
}

async function applyMultiDayNight() {
  const source = focusedSource();
  const e = multiImageEls();
  if (!source || source.kind !== 'rtsp' || !e.dayNight) return;
  const data = await RtspApi.setDayNight(source.serial, e.dayNight.value);
  if (!data || data.error || data.ok === false) {
    log.warn('Камера не подтвердила режим день/ночь', data);
    refreshMultiImageSettings(source.serial);
    return;
  }
  log.success('Режим день/ночь применён', { mode: e.dayNight.value });
}

async function applyMultiExposure(field, value) {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return;
  const data = await RtspApi.setExposure(source.serial, { [field]: Number(value) });
  if (!data || data.error || data.ok === false) {
    log.warn('Камера не подтвердила экспозицию', data);
    refreshMultiImageSettings(source.serial);
    return;
  }
  log.success('Экспозиция применена', { [field]: Number(value) });
}

// --- фото ---
async function applyPhoto(on) {
  const tile = state.tiles[state.focused];
  const source = focusedSource();
  if (!tile || !source) return;
  const api = apiFor(tile.kind);
  if (on) {
    const project = readMultiProjectName('multiPhotoProject');
    if (project === null) return;
    const amount = Number(document.getElementById('multiPhotoInterval')?.value) || 5;
    const unit = document.getElementById('multiPhotoUnit')?.value || 'seconds';
    const seconds = unit === 'minutes' ? amount * 60 : amount;
    const data = await api.startPhotoSaving(source.serial, seconds, project);
    tile.photo = true;
    renderTile(state.focused);
    updateToolbar();
    showSavePath('multiPhotoSavePath', data);
    return;
  }
  await api.stopPhotoSaving(source.serial);
  tile.photo = false;
  renderTile(state.focused);
  updateToolbar();
  showSavePath('multiPhotoSavePath', null);
}

// --- видео ---
async function applyVideo(on) {
  const tile = state.tiles[state.focused];
  const source = focusedSource();
  if (!tile || !source) return;
  const api = apiFor(tile.kind);
  if (on) {
    const project = readMultiProjectName('multiVideoProject');
    if (project === null) return;
    const amount = Number(document.getElementById('multiVideoDuration')?.value) || 10;
    const unit = document.getElementById('multiVideoUnit')?.value || 'minutes';
    const seconds = unit === 'minutes' ? amount * 60 : amount;
    const data = await api.startVideoSaving(source.serial, seconds, project);
    tile.video = true;
    renderTile(state.focused);
    updateToolbar();
    showSavePath('multiVideoSavePath', data);
    return;
  }
  await api.stopVideoSaving(source.serial);
  tile.video = false;
  renderTile(state.focused);
  updateToolbar();
  showSavePath('multiVideoSavePath', null);
}

// --- подсветка (только RTSP-камера в фокусе) ---
function reflectMultiLight(on) {
  const sw = document.getElementById('multiLightSwitch');
  const label = document.getElementById('multiLightLabel');
  if (sw) sw.checked = on;
  if (label) label.textContent = on ? 'Включено' : 'Выключено';
}

function setTileLight(on) {
  const tile = state.tiles[state.focused];
  if (tile) { tile.light = on; renderTile(state.focused); }
}

async function syncMultiLightState(serial) {
  if (!(settingsCaps && settingsCaps.white_light)) { reflectMultiLight(false); setTileLight(false); return; }
  const data = await RtspApi.getLightState(serial);
  const on = !!(data && data.state === 'on');
  reflectMultiLight(on);
  setTileLight(on);
}

async function applyMultiLight(on) {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !(settingsCaps && settingsCaps.white_light)) return;
  const level = Number(document.getElementById('multiLightLevel')?.value) || 100;
  const data = await RtspApi.setLight(source.serial, on, level);
  if (!data || data.error || data.status === 'failed') {
    syncMultiLightState(source.serial); // откат тумблера к фактическому состоянию
    return;
  }
  reflectMultiLight(on);
  setTileLight(on);
}

// ---------- зум областью (рамкой) в ячейке ----------
let tileDrag = null; // { index, x0, y0, x1, y1 }

function tileLayerXY(layer, event) {
  const r = layer.getBoundingClientRect();
  return { x: event.clientX - r.left, y: event.clientY - r.top };
}

function updateTileMarqueeBox() {
  if (!tileDrag) return;
  const el = getTileEl(tileDrag.index);
  const box = el && el.querySelector('[data-tile-marquee-box]');
  if (!box) return;
  const { x0, y0, x1, y1 } = tileDrag;
  box.hidden = false;
  box.style.left = Math.min(x0, x1) + 'px';
  box.style.top = Math.min(y0, y1) + 'px';
  box.style.width = Math.abs(x1 - x0) + 'px';
  box.style.height = Math.abs(y1 - y0) + 'px';
}

// видимый прямоугольник видео внутри <img> (object-fit: contain, с полями)
function tileVideoRect(frame) {
  const nw = frame.naturalWidth, nh = frame.naturalHeight;
  const bw = frame.clientWidth, bh = frame.clientHeight;
  if (!nw || !nh || !bw || !bh) return null;
  const nr = nw / nh, br = bw / bh;
  if (nr > br) { const dh = bw / nr; return { dw: bw, dh, ox: 0, oy: (bh - dh) / 2 }; }
  const dw = bh * nr; return { dw, dh: bh, ox: (bw - dw) / 2, oy: 0 };
}

// кнопка (оранжевая при активности) и слой рамки в ячейке
function applyTileRegionUI(index) {
  const tile = state.tiles[index];
  const el = getTileEl(index);
  if (!tile || !el) return;
  const source = getSource(tile.serial);
  const isRtsp = source && source.kind === 'rtsp';
  const btn = el.querySelector('[data-tile-region]');
  const layer = el.querySelector('[data-tile-marquee]');
  const box = el.querySelector('[data-tile-marquee-box]');
  if (btn) {
    btn.hidden = !(isRtsp && tile.connected);
    btn.classList.toggle('is-region-active', !!tile.regionMode || Number(tile.zoomFactor) > 1);
  }
  if (layer) layer.hidden = !tile.regionMode;
  if (box && !tile.regionMode) box.hidden = true;
}

function onTileRegionClick(index) {
  const tile = state.tiles[index];
  const source = tile && getSource(tile.serial);
  if (!tile || !source || source.kind !== 'rtsp' || !tile.connected) return;
  if (Number(tile.zoomFactor) > 1) {
    resetTileZoom(index); // приближено → сброс на 1×
  } else {
    tile.regionMode = !tile.regionMode; // иначе — вход/выход из выделения
    applyTileRegionUI(index);
  }
}

async function resetTileZoom(index) {
  const tile = state.tiles[index];
  const source = tile && getSource(tile.serial);
  if (!source) return;
  await RtspApi.setZoom(source.serial, 1);
  tile.zoomFactor = 1;
  tile.regionMode = false;
  renderTile(index);
  applyTileRegionUI(index);
}

async function applyTileRegionZoom() {
  const drag = tileDrag;
  tileDrag = null;
  if (!drag) return;
  const index = drag.index;
  const tile = state.tiles[index];
  const el = getTileEl(index);
  const source = tile && getSource(tile.serial);
  const frame = el && el.querySelector('[data-tile-frame]');
  const box = el && el.querySelector('[data-tile-marquee-box]');
  if (box) box.hidden = true;
  if (!tile || !source || source.kind !== 'rtsp' || !frame) return;
  const rect = tileVideoRect(frame);
  if (!rect) return;
  if (Math.abs(drag.x1 - drag.x0) < 10 || Math.abs(drag.y1 - drag.y0) < 10) return;

  const { dw, dh, ox, oy } = rect;
  const cl = (v) => Math.max(0, Math.min(1, v));
  const nx0 = cl((Math.min(drag.x0, drag.x1) - ox) / dw);
  const ny0 = cl((Math.min(drag.y0, drag.y1) - oy) / dh);
  const nx1 = cl((Math.max(drag.x0, drag.x1) - ox) / dw);
  const ny1 = cl((Math.max(drag.y0, drag.y1) - oy) / dh);
  const rw = Math.max(0.02, nx1 - nx0);
  const rh = Math.max(0.02, ny1 - ny0);
  const ncx = (nx0 + nx1) / 2, ncy = (ny0 + ny1) / 2;
  let factor = Math.max(1, Math.min(4, Math.min(1 / rw, 1 / rh)));
  const z = 1 / factor;
  const px = factor > 1 ? cl((ncx - z / 2) / (1 - z)) : 0.5;
  const py = factor > 1 ? cl((ncy - z / 2) / (1 - z)) : 0.5;

  const data = await RtspApi.setZoomRegion(source.serial, factor, px, py);
  if (data && !data.error) tile.zoomFactor = data.factor || factor;
  tile.regionMode = false;
  renderTile(index);
  applyTileRegionUI(index);
}

// глобальные обработчики перетаскивания рамки в ячейке
window.addEventListener('mousemove', (event) => {
  if (!tileDrag) return;
  const el = getTileEl(tileDrag.index);
  const layer = el && el.querySelector('[data-tile-marquee]');
  if (!layer) return;
  const p = tileLayerXY(layer, event);
  tileDrag.x1 = p.x; tileDrag.y1 = p.y;
  updateTileMarqueeBox();
});
window.addEventListener('mouseup', () => {
  if (tileDrag) applyTileRegionZoom();
});

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

// --- текущий конфиг запуска камеры (фокусная GigE-ячейка) ---
const CONFIG_ROWS = [
  ['width', 'Ширина'],
  ['height', 'Высота'],
  ['offset_x', 'Смещение X'],
  ['offset_y', 'Смещение Y'],
  ['fps', 'FPS'],
  ['exposure_auto', 'Автоэкспозиция'],
  ['exposure_time', 'Время экспозиции, мкс'],
  ['pixel_format', 'Формат пикселей'],
];

async function openConfigModal() {
  const source = focusedSource();
  if (!source || source.kind !== 'gige') return;

  const serialEl = document.getElementById('multiConfigSerial');
  const listEl = document.getElementById('multiConfigList');
  const emptyEl = document.getElementById('multiConfigEmpty');

  if (serialEl) serialEl.textContent = source.label;
  if (listEl) listEl.innerHTML = '';
  if (emptyEl) emptyEl.hidden = true;
  openModal('multiConfigModal');

  const cfg = await CameraApi.getCurrentConfig(source.serial);
  const hasData = cfg && Object.keys(cfg).length > 0;
  if (emptyEl) emptyEl.hidden = hasData;
  if (!listEl || !hasData) return;

  listEl.innerHTML = '';
  CONFIG_ROWS.forEach(([key, label]) => {
    if (cfg[key] === undefined || cfg[key] === null) return;
    const row = document.createElement('div');
    row.className = 'info-row';
    row.innerHTML = `<dt class="info-row__label">${escapeHtml(label)}</dt><dd class="info-row__value">${escapeHtml(String(cfg[key]))}</dd>`;
    listEl.appendChild(row);
  });
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
  document.getElementById('multiSettingsBtn')?.addEventListener('click', openSettingsModal);
  document.getElementById('multiConfigBtn')?.addEventListener('click', openConfigModal);
}

function initModals() {
  // единая модалка настроек: зоны + управление
  document.getElementById('multiSettingsClose')?.addEventListener('click', () => closeModal('multiSettingsModal'));
  document.getElementById('multiFpsApply')?.addEventListener('click', applyMultiFps);
  document.getElementById('multiPhotoOn')?.addEventListener('click', () => applyPhoto(true));
  document.getElementById('multiPhotoOff')?.addEventListener('click', () => applyPhoto(false));
  document.getElementById('multiVideoOn')?.addEventListener('click', () => applyVideo(true));
  document.getElementById('multiVideoOff')?.addEventListener('click', () => applyVideo(false));
  document.getElementById('multiLightSwitch')?.addEventListener('change', (e) => applyMultiLight(e.target.checked));

  // настройки изображения: применяем сразу при изменении (ползунки — по отпусканию)
  document.getElementById('multiImageWb')?.addEventListener('change', applyMultiWhiteBalance);
  document.getElementById('multiImageDayNight')?.addEventListener('change', applyMultiDayNight);
  document.getElementById('multiImageCompensation')?.addEventListener('change', (e) => applyMultiExposure('compensation', e.target.value));
  document.getElementById('multiImageGainMin')?.addEventListener('change', (e) => applyMultiExposure('gain_min', e.target.value));
  document.getElementById('multiImageGainMax')?.addEventListener('change', (e) => applyMultiExposure('gain_max', e.target.value));

  document.getElementById('multiInfoClose')?.addEventListener('click', () => closeModal('multiInfoModal'));
  document.getElementById('multiInfoCloseFooter')?.addEventListener('click', () => closeModal('multiInfoModal'));

  document.getElementById('multiConfigClose')?.addEventListener('click', () => closeModal('multiConfigModal'));
  document.getElementById('multiConfigCloseFooter')?.addEventListener('click', () => closeModal('multiConfigModal'));

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
  await loadSavedRtsp();
  startMetricsPolling();

  window.addEventListener('beforeunload', () => {
    if (metricsTimer) clearInterval(metricsTimer);
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

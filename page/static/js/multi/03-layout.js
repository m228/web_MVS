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
    // «переподключение…» приоритетнее «нет кадров»: это не зависшая камера,
    // а идущее восстановление связи (бэкенд сам переоткрывает поток)
    badge.textContent = tile.connected
      ? (tile.reconnecting ? '● переподключение…' : (stalled ? '● нет кадров' : '● в эфире'))
      : (source ? 'готова' : 'пусто');
  }

  el.classList.toggle('is-live', tile.connected && !stalled);
  el.classList.toggle('is-stalled', stalled);
  el.classList.toggle('is-empty', !source);

  const recPhoto = el.querySelector('[data-rec-photo]');
  const recVideo = el.querySelector('[data-rec-video]');
  if (recPhoto) {
    recPhoto.classList.toggle('is-active', !!tile.photo);
    // фото включено, но файлы не пишутся — значок краснеет, причина в подсказке
    const bad = !!tile.photo && tile.photoHealth === 'stalled';
    recPhoto.classList.toggle('is-error', bad);
    recPhoto.title = bad ? `Фото не пишется: ${tile.photoError || 'причина неизвестна'}` : 'Сохранение фото';
  }
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


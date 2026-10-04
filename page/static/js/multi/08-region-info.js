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


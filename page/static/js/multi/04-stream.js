// ---------- единое меню ----------
function updateToolbar() {
  const tile = state.tiles[state.focused] || {};
  const source = tile.serial ? getSource(tile.serial) : null;

  const focusEl = document.getElementById('multiFocusSerial');
  if (focusEl) focusEl.textContent = source ? source.label : '— не выбрана —';

  const hasSource = !!source;
  const connected = hasSource && tile.connected;
  setDisabled('multiConnectBtn', !hasSource || tile.connected);
  setDisabled('multiDisconnectBtn', !connected);
  // фото/видео — для любой подключённой камеры (FPS/масштаб теперь в карточке источника)
  setDisabled('multiPhotoBtn', !connected);
  setDisabled('multiVideoBtn', !connected);
  // оптика (зум/фокус) — только для подключённой RTSP-камеры (CGI-управление)
  const rtspOn = connected && source && source.kind === 'rtsp';
  setDisabled('multiOpticBtn', !rtspOn);
  setDisabled('multiLightBtn', !rtspOn);
  setDisabled('multiImageBtn', !rtspOn);
  setDisabled('multiNetworkBtn', !rtspOn);
  // конфиг — только для GigE (у RTSP его нет); доступен и после остановки
  setDisabled('multiConfigBtn', !hasSource || source.kind !== 'gige');

  // индикаторы записи на своих иконках
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

      // состояние надёжности RTSP: идёт ли переподключение и пишутся ли фото
      if (metrics.reconnecting !== undefined || metrics.photo_health !== undefined) {
        const reconnecting = !!metrics.reconnecting;
        const health = metrics.photo_health ?? null;
        if (reconnecting !== !!tile.reconnecting || health !== tile.photoHealth) {
          tile.reconnecting = reconnecting;
          tile.photoHealth = health;
          tile.photoError = metrics.photo_error ?? null;
          renderTile(i);
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
    serial: src.serial,
    scale: src.connection.scale,
    fps: src.connection.fps,
  });
  log.info('RTSP-камера сохранена в базу (подключение удалось)', { serial, url: src.connection.url });
}


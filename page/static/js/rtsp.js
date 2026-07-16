function initRtspPage() {
  const form = document.getElementById('rtspForm');
  const serialElement = document.getElementById('rtspSerial');
  const rtspFrame = document.getElementById('rtspFrame');
  const rtspPlaceholder = document.getElementById('rtspPlaceholder');

  const metricFps = document.getElementById('metricFps');
  const metricImageNumber = document.getElementById('metricImageNumber');
  const metricBandwidth = document.getElementById('metricBandwidth');
  const metricResolution = document.getElementById('metricResolution');
  const metricErrors = document.getElementById('metricErrors');

  const buttons = {
    connect: document.getElementById('connectBtn'),
    stop: document.getElementById('stopBtn'),
    force_stop: document.getElementById('forceStopBtn'),
    snapshot: document.getElementById('snapshotBtn'),
    photo: document.getElementById('photoBtn'),
    video: document.getElementById('videoBtn'),
    light: document.getElementById('lightBtn'),
    zoom: document.getElementById('zoomBtn'),
  };

  const photoCard = document.getElementById('photoCard');
  const photoOnBtn = document.getElementById('photoOn');
  const photoOffBtn = document.getElementById('photoOff');
  const photoIndicator = document.getElementById('photoIndicator');

  const videoCard = document.getElementById('videoCard');
  const videoOnBtn = document.getElementById('videoOn');
  const videoOffBtn = document.getElementById('videoOff');
  const videoIndicator = document.getElementById('videoIndicator');

  const lightCard = document.getElementById('lightCard');
  const lightSwitch = document.getElementById('lightSwitch');
  const lightSwitchLabel = document.getElementById('lightSwitchLabel');
  const lightLevel = document.getElementById('lightLevel');
  const lightCap = document.getElementById('lightCap');
  const lightIndicator = document.getElementById('lightIndicator');

  const zoomCard = document.getElementById('zoomCard');
  const zoomCap = document.getElementById('zoomCap');
  const zoomLevels = document.getElementById('zoomLevels');
  const zoomIndicator = document.getElementById('zoomIndicator');
  const opticalZoomControls = document.getElementById('opticalZoomControls');
  const zoomOpticalHint = document.getElementById('zoomOpticalHint');
  const zoomPan = document.getElementById('zoomPan');
  const panGrid = document.getElementById('panGrid');

  if (!form || !rtspFrame || !rtspPlaceholder || !buttons.connect) {
    window.AppLog?.error('rtsp', 'Не найдены обязательные элементы RTSP-страницы');
    return;
  }

  const log = {
    info: (message, payload) => window.AppLog?.info('rtsp', message, payload),
    success: (message, payload) => window.AppLog?.success('rtsp', message, payload),
    warn: (message, payload) => window.AppLog?.warn('rtsp', message, payload),
    error: (message, payload) => window.AppLog?.error('rtsp', message, payload),
    debug: (message, payload) => window.AppLog?.debug('rtsp', message, payload),
  };

  let serial = null;
  let isConnected = false;
  let isLoading = false;
  let isStoppingStream = false;
  let isSavePhoto = false;
  let isSaveVideo = false;
  let metricsTimer = null;
  let statusTimer = null;
  let isLeavingPage = false;
  let streamErrorHandled = false;

  const photoPopup = UIHelpers.createPopupController(photoCard, buttons.photo);
  const videoPopup = UIHelpers.createPopupController(videoCard, buttons.video);
  const lightPopup = UIHelpers.createPopupController(lightCard, buttons.light);
  const zoomPopup = UIHelpers.createPopupController(zoomCard, buttons.zoom);

  // возможности камеры (заполняются после опроса) и текущая кратность цифрового зума
  let capabilities = null;
  let currentZoom = 1;

  log.info('RTSP-страница открыта');

  function readConnection() {
    const data = new FormData(form);
    const url = String(data.get('url') || '').trim();
    const ip = String(data.get('ip') || '').trim();

    return {
      url,
      ip,
      username: String(data.get('username') || 'admin').trim(),
      password: String(data.get('password') || ''),
      channel: Number(data.get('channel') || 1),
      subtype: Number(data.get('subtype') || 0),
      scale: Number(data.get('scale') || 100),
      fps: String(data.get('fps') || '').trim(),
    };
  }

  function makeSerial(connection) {
    if (connection.url) return 'RTSP-custom';
    if (connection.ip) return `RTSP-${connection.ip}`;
    return null;
  }

  function showNoVideo() {
    rtspFrame.classList.remove('visible');
    rtspPlaceholder.classList.remove('hidden');
  }

  function showVideo() {
    rtspFrame.classList.add('visible');
    rtspPlaceholder.classList.add('hidden');
  }

  function updateToolbarState() {
    buttons.stop.classList.toggle('hidden', !isConnected);
    buttons.snapshot.disabled = !isConnected;
    buttons.photo.disabled = !isConnected;
    buttons.video.disabled = !isConnected;
    if (buttons.light) buttons.light.disabled = !isConnected;
    if (buttons.zoom) buttons.zoom.disabled = !isConnected;
    buttons.connect.disabled = isLoading;
  }

  function updateModeIndicators() {
    if (photoIndicator) photoIndicator.classList.toggle('hidden', !isSavePhoto);
    if (videoIndicator) videoIndicator.classList.toggle('hidden', !isSaveVideo);
    if (photoOnBtn) photoOnBtn.disabled = isSavePhoto;
    if (photoOffBtn) photoOffBtn.disabled = !isSavePhoto;
    if (videoOnBtn) videoOnBtn.disabled = isSaveVideo;
    if (videoOffBtn) videoOffBtn.disabled = !isSaveVideo;
  }

  function updateMetricsUI(data) {
    if (metricFps) metricFps.textContent = `${Number(data.fps ?? 0).toFixed(2)} fps`;
    if (metricImageNumber) metricImageNumber.textContent = data.image_number ?? 0;
    if (metricBandwidth) metricBandwidth.textContent = `${Number(data.bandwidth_mbps ?? 0).toFixed(1)} Mbps`;
    if (metricResolution) metricResolution.textContent = `${data.width ?? 0} × ${data.height ?? 0}`;
    if (metricErrors) metricErrors.textContent = data.errors ?? 0;
  }

  function resetMetricsUI() {
    updateMetricsUI({ fps: 0, image_number: 0, bandwidth_mbps: 0, width: 0, height: 0, errors: 0 });
  }

  function stopMetricsPolling() {
    if (metricsTimer) {
      clearInterval(metricsTimer);
      metricsTimer = null;
    }
  }

  function startMetricsPolling() {
    stopMetricsPolling();
    metricsTimer = setInterval(syncMetrics, 1000);
  }

  async function syncMetrics() {
    if (!isConnected || !serial) return;
    const data = await RtspApi.getMetrics(serial);
    if (data && !data.error) updateMetricsUI(data);
  }

  function stopStatusPolling() {
    if (statusTimer) {
      clearInterval(statusTimer);
      statusTimer = null;
    }
  }

  function startStatusPolling() {
    stopStatusPolling();
    statusTimer = setInterval(syncVideoPhotoStatus, 1000);
  }

  async function syncVideoPhotoStatus() {
    if (!isConnected || !serial) return;
    const data = await RtspApi.getVideoPhotoStatus(serial);
    if (!data || data.error) return;

    isSavePhoto = !!data.photo;
    isSaveVideo = Number(data.video) === 1;
    updateModeIndicators();
  }

  function resetUI() {
    isConnected = false;
    isLoading = false;
    isStoppingStream = true;
    isSavePhoto = false;
    isSaveVideo = false;

    rtspFrame.src = '';
    showNoVideo();
    photoPopup.close();
    videoPopup.close();
    lightPopup.close();
    zoomPopup.close();
    capabilities = null;

    stopMetricsPolling();
    stopStatusPolling();
    resetMetricsUI();
    updateModeIndicators();
    updateToolbarState();
  }

  function connectCamera() {
    const connection = readConnection();
    serial = makeSerial(connection);

    if (!serial) {
      log.warn('Не указан IP или RTSP-URL');
      alert('Укажите IP-адрес или полный RTSP-URL');
      return;
    }

    serialElement.textContent = serial;
    log.info('Подключение к RTSP-камере', { serial });

    isStoppingStream = false;
    streamErrorHandled = false;
    isLoading = true;
    updateToolbarState();

    rtspFrame.src = RtspApi.buildStreamUrl(serial, connection);
  }

  async function stopCamera() {
    if (!serial) return;
    log.info('Остановка RTSP-потока', { serial });

    isStoppingStream = true;
    await RtspApi.closeStream(serial);
    resetUI();
  }

  async function forceStopCamera() {
    if (!serial) return;
    log.warn('Принудительная остановка RTSP-потока', { serial });

    isStoppingStream = true;
    await RtspApi.closeStreamForce(serial);
    resetUI();
  }

  async function takeSnapshot() {
    if (!serial || !isConnected) return;

    log.info('Запрошен снимок', { serial });

    try {
      const response = await fetch(RtspApi.snapshotUrl(serial), { cache: 'no-store' });
      const contentType = response.headers.get('content-type') || '';

      if (!contentType.startsWith('image/')) {
        const data = await response.json().catch(() => null);
        log.warn('Снимок не получен', data);
        alert('Не удалось получить снимок');
        return;
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `snapshot_${Date.now()}.jpg`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);

      log.success('Снимок сохранён на сервере и скачан', { serial });
    } catch (error) {
      log.error('Ошибка получения снимка', { error: error.message });
      alert('Ошибка получения снимка');
    }
  }

  // показать путь сохранения (папка + шаблон имени файла) из ответа сервера
  // showSavePath вынесена в ui.js (общая для camera/multi/rtsp)

  async function startPhotoSaving() {
    if (!isConnected) return;

    const interval = UIHelpers.getPositiveNumber(
      'input[name="photo_interval"]',
      'Не выбран интервал сохранения'
    );
    if (interval === null) return;

    // единица интервала: как в видео — минуты переводим в секунды
    const unitSelect = document.querySelector('select[name="photo_interval_unit"]');
    const unit = unitSelect ? unitSelect.value : 'seconds';
    const intervalInSeconds = unit === 'minutes' ? interval * 60 : interval;

    const data = await RtspApi.startPhotoSaving(serial, intervalInSeconds);
    if (!data || data.error) {
      log.warn('Сервер не подтвердил запуск сохранения фото', data);
      return;
    }

    log.success('Автосохранение фото включено', data);
    showSavePath('photoSavePath', data);
    await syncVideoPhotoStatus();
  }

  async function stopPhotoSaving() {
    const data = await RtspApi.stopPhotoSaving(serial);
    if (!data || data.error) {
      log.warn('Сервер не подтвердил отключение сохранения фото', data);
      return;
    }

    log.success('Автосохранение фото выключено', data);
    showSavePath('photoSavePath', null);
    await syncVideoPhotoStatus();
  }

  async function startVideoSaving() {
    if (!isConnected) return;

    const duration = UIHelpers.getPositiveNumber(
      'input[name="video_duration"]',
      'Не выбрана длительность записи'
    );
    if (duration === null) return;

    const unitSelect = document.querySelector('select[name="video_duration_unit"]');
    const unit = unitSelect ? unitSelect.value : 'minutes';
    const durationInSeconds = unit === 'minutes' ? duration * 60 : duration;

    const data = await RtspApi.startVideoSaving(serial, durationInSeconds);
    if (!data || data.error) {
      log.warn('Сервер не подтвердил запуск записи видео', data);
      return;
    }

    log.success('Запись видео включена', data);
    showSavePath('videoSavePath', data);
    await syncVideoPhotoStatus();
  }

  async function stopVideoSaving() {
    if (!isConnected) return;

    const data = await RtspApi.stopVideoSaving(serial);
    if (!data || data.error) {
      log.warn('Сервер не подтвердил отключение записи видео', data);
      return;
    }

    log.success('Запись видео выключена', data);
    showSavePath('videoSavePath', null);
    await syncVideoPhotoStatus();
  }

  // ---------- возможности камеры (свет / зум) ----------

  function setCapLine(el, supported, textYes, textNo) {
    if (!el) return;
    el.dataset.state = supported ? 'yes' : 'no';
    const textEl = el.querySelector('.cap-text');
    if (textEl) textEl.textContent = supported ? textYes : textNo;
  }

  // единое отражение состояния света: тумблер, лампочка, кнопка в тулбаре, индикатор
  function reflectLightOn(on) {
    if (lightSwitch) lightSwitch.checked = on;
    if (lightSwitchLabel) lightSwitchLabel.textContent = on ? 'Включено' : 'Выключено';
    // подсветка кнопки в тулбаре: лампочка «горит», когда свет включён
    if (buttons.light) buttons.light.classList.toggle('is-on', on);
    if (lightIndicator) lightIndicator.classList.toggle('hidden', !on);
  }

  function applyCapabilitiesUI() {
    const caps = capabilities || {};
    const hasLight = !!caps.white_light;
    const hasOptical = !!caps.optical_zoom;

    // --- подсветка ---
    setCapLine(lightCap, hasLight,
      'Белый прожектор поддерживается',
      caps.reachable ? 'Подсветка не поддерживается' : 'Камера не отвечает на управление');
    if (lightSwitch) lightSwitch.disabled = !hasLight;
    if (lightLevel) lightLevel.disabled = !hasLight;
    if (!hasLight) reflectLightOn(false);

    // --- зум ---
    setCapLine(zoomCap, true, 'Цифровой зум', 'Цифровой зум');
    if (opticalZoomControls) opticalZoomControls.hidden = !hasOptical;
    if (zoomOpticalHint) zoomOpticalHint.hidden = hasOptical;
  }

  async function refreshCapabilities(force) {
    if (!isConnected || !serial) return;
    const data = await RtspApi.getCapabilities(serial, force);
    if (data && !data.error) {
      capabilities = data;
      log.info('Возможности камеры получены', data);
    } else {
      capabilities = { reachable: false, white_light: false, optical_zoom: false };
      log.warn('Не удалось опросить возможности камеры', data);
    }
    applyCapabilitiesUI();
    syncLightState();
  }

  async function syncLightState() {
    if (!isConnected || !serial || !(capabilities && capabilities.white_light)) {
      reflectLightOn(false);
      return;
    }
    const data = await RtspApi.getLightState(serial);
    reflectLightOn(!!(data && data.state === 'on'));
  }

  async function setLight(on) {
    if (!serial || !(capabilities && capabilities.white_light)) return;
    const level = lightLevel ? Number(lightLevel.value) || 100 : 100;
    const data = await RtspApi.setLight(serial, on, level);
    if (!data || data.error || data.status === 'failed') {
      log.warn('Камера не подтвердила смену подсветки', data);
      syncLightState(); // откат тумблера к фактическому состоянию
      return;
    }
    log.success(on ? 'Подсветка включена' : 'Подсветка выключена', data);
    reflectLightOn(on);
  }

  function markZoomLevel(factor) {
    if (zoomLevels) {
      zoomLevels.querySelectorAll('button[data-zoom]').forEach((btn) => {
        btn.classList.toggle('active', Number(btn.dataset.zoom) === factor);
      });
    }
    if (zoomIndicator) zoomIndicator.classList.toggle('hidden', factor <= 1);
    // сетку области показываем только при зуме > 1
    if (zoomPan) zoomPan.hidden = factor <= 1;
  }

  // подсветить активную ячейку области (px, py: 0 / 0.5 / 1)
  function markPan(px, py) {
    if (!panGrid) return;
    panGrid.querySelectorAll('button[data-px]').forEach((btn) => {
      btn.classList.toggle('active',
        Number(btn.dataset.px) === px && Number(btn.dataset.py) === py);
    });
  }

  async function applyDigitalZoom(factor) {
    if (!serial) return;
    const data = await RtspApi.setZoom(serial, factor);
    if (!data || data.error) {
      log.warn('Камера не подтвердила цифровой зум', data);
      return;
    }
    currentZoom = data.factor || factor;
    markZoomLevel(currentZoom);
    markPan(0.5, 0.5); // новая кратность — вид в центре
    log.success('Цифровой зум ×' + currentZoom, data);
  }

  async function applyZoomPan(px, py) {
    if (!serial || currentZoom <= 1) return;
    const data = await RtspApi.setZoomPan(serial, px, py);
    if (!data || data.error) {
      log.warn('Камера не подтвердила смещение области зума', data);
      return;
    }
    markPan(px, py);
    log.success('Область зума смещена', data);
  }

  async function opticalZoom(direction) {
    if (!serial || !(capabilities && capabilities.optical_zoom)) return;
    const data = await RtspApi.opticalZoom(serial, direction);
    if (!data || data.error || data.status === 'failed') {
      log.warn('Камера не подтвердила оптический зум', data);
      return;
    }
    log.success('Оптический зум: ' + direction, data);
  }

  function handlePageLeave() {
    if (isLeavingPage || !serial) return;
    isLeavingPage = true;
    isStoppingStream = true;

    try {
      navigator.sendBeacon('/api/rtsp/close_stream?serial_number=' + encodeURIComponent(serial));
    } catch (error) {
      fetch('/api/rtsp/close_stream?serial_number=' + encodeURIComponent(serial), {
        method: 'GET',
        keepalive: true,
      }).catch(() => {});
    }
  }

  buttons.connect.addEventListener('click', connectCamera);
  buttons.stop.addEventListener('click', stopCamera);
  buttons.force_stop.addEventListener('click', forceStopCamera);
  buttons.snapshot.addEventListener('click', takeSnapshot);
  buttons.photo.addEventListener('click', (event) => {
    event.stopPropagation();
    photoPopup.toggle();
  });
  buttons.video.addEventListener('click', (event) => {
    event.stopPropagation();
    videoPopup.toggle();
  });
  if (buttons.light) buttons.light.addEventListener('click', (event) => {
    event.stopPropagation();
    lightPopup.toggle();
  });
  if (buttons.zoom) buttons.zoom.addEventListener('click', (event) => {
    event.stopPropagation();
    zoomPopup.toggle();
  });

  if (photoOnBtn) photoOnBtn.addEventListener('click', startPhotoSaving);
  if (photoOffBtn) photoOffBtn.addEventListener('click', stopPhotoSaving);
  if (videoOnBtn) videoOnBtn.addEventListener('click', startVideoSaving);
  if (videoOffBtn) videoOffBtn.addEventListener('click', stopVideoSaving);

  if (lightSwitch) lightSwitch.addEventListener('change', () => setLight(lightSwitch.checked));

  if (zoomLevels) zoomLevels.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-zoom]');
    if (btn) applyDigitalZoom(Number(btn.dataset.zoom));
  });
  if (panGrid) panGrid.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-px]');
    if (btn) applyZoomPan(Number(btn.dataset.px), Number(btn.dataset.py));
  });
  if (opticalZoomControls) opticalZoomControls.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-optical]');
    if (btn) opticalZoom(btn.dataset.optical);
  });

  if (photoCard) photoCard.addEventListener('click', (event) => event.stopPropagation());
  if (videoCard) videoCard.addEventListener('click', (event) => event.stopPropagation());
  if (lightCard) lightCard.addEventListener('click', (event) => event.stopPropagation());
  if (zoomCard) zoomCard.addEventListener('click', (event) => event.stopPropagation());

  document.addEventListener('click', (event) => {
    const insidePhoto = photoCard && photoCard.contains(event.target);
    const onPhotoBtn = buttons.photo && buttons.photo.contains(event.target);
    const insideVideo = videoCard && videoCard.contains(event.target);
    const onVideoBtn = buttons.video && buttons.video.contains(event.target);
    const insideLight = lightCard && lightCard.contains(event.target);
    const onLightBtn = buttons.light && buttons.light.contains(event.target);
    const insideZoom = zoomCard && zoomCard.contains(event.target);
    const onZoomBtn = buttons.zoom && buttons.zoom.contains(event.target);

    if (!insidePhoto && !onPhotoBtn) photoPopup.close();
    if (!insideVideo && !onVideoBtn) videoPopup.close();
    if (!insideLight && !onLightBtn) lightPopup.close();
    if (!insideZoom && !onZoomBtn) zoomPopup.close();
  });

  rtspFrame.addEventListener('load', () => {
    log.success('RTSP-поток успешно загружен', { serial });

    isLoading = false;
    isConnected = true;
    isStoppingStream = false;
    streamErrorHandled = false;

    showVideo();
    updateToolbarState();
    syncMetrics();
    startMetricsPolling();
    syncVideoPhotoStatus();
    startStatusPolling();

    // сброс зума и автоматический опрос возможностей (свет / оптический зум)
    currentZoom = 1;
    markZoomLevel(1);
    refreshCapabilities(false);
  });

  rtspFrame.addEventListener('error', () => {
    if (isStoppingStream || streamErrorHandled || !rtspFrame.src) {
      return;
    }

    streamErrorHandled = true;
    log.error('Ошибка загрузки RTSP-потока', { serial });
    resetUI();
  });

  window.addEventListener('beforeunload', handlePageLeave);
  window.addEventListener('pagehide', handlePageLeave);

  showNoVideo();
  updateToolbarState();
  updateModeIndicators();
}

window.addEventListener('DOMContentLoaded', initRtspPage);

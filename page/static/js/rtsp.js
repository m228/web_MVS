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
    image: document.getElementById('imageBtn'),
    network: document.getElementById('networkBtn'),
  };

  const photoCard = document.getElementById('photoCard');
  const photoOnBtn = document.getElementById('photoOn');
  const photoOffBtn = document.getElementById('photoOff');
  const photoIndicator = document.getElementById('photoIndicator');
  const photoHealthLine = document.getElementById('photoHealth');

  // бейдж «переподключение…» поверх кадра (см. reflectReliability)
  const reconnectBadge = document.getElementById('rtspReconnectBadge');

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
  const focusControls = document.getElementById('focusControls');
  const autoFocusBtn = document.getElementById('autoFocusBtn');
  const zoomPan = document.getElementById('zoomPan');
  const panX = document.getElementById('panX');
  const panY = document.getElementById('panY');

  const imageCard = document.getElementById('imageCard');
  const imageCap = document.getElementById('imageCap');
  const imageWb = document.getElementById('imageWb');
  const imageDayNight = document.getElementById('imageDayNight');
  const imageCompensation = document.getElementById('imageCompensation');
  const imageGainMin = document.getElementById('imageGainMin');
  const imageGainMax = document.getElementById('imageGainMax');

  const networkCard = document.getElementById('networkCard');
  const networkCap = document.getElementById('networkCap');
  const networkCurrentBox = document.getElementById('networkCurrent');
  const netCurIp = document.getElementById('netCurIp');
  const netCurMask = document.getElementById('netCurMask');
  const netCurGw = document.getElementById('netCurGw');
  const networkDhcp = document.getElementById('networkDhcp');
  const networkFields = document.getElementById('networkFields');
  const networkIp = document.getElementById('networkIp');
  const networkMask = document.getElementById('networkMask');
  const networkGateway = document.getElementById('networkGateway');
  const networkApplyBtn = document.getElementById('networkApplyBtn');

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
  const imagePopup = UIHelpers.createPopupController(imageCard, buttons.image);
  const networkPopup = UIHelpers.createPopupController(networkCard, buttons.network);

  // последние прочитанные с камеры сетевые настройки (для diff-подтверждения)
  let currentNetwork = null;

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
    if (buttons.image) buttons.image.disabled = !isConnected;
    if (buttons.network) buttons.network.disabled = !isConnected;
    const regionBtn = document.getElementById('regionZoomBtn');
    if (regionBtn) regionBtn.disabled = !isConnected;
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
    reflectReliability(data);
  }

  // надёжность: идёт ли переподключение и пишутся ли фото на самом деле.
  // Бэкенд сам восстанавливает поток, UI лишь честно показывает, что происходит.
  function reflectReliability(data) {
    const reconnecting = !!data.reconnecting;
    if (reconnectBadge) reconnectBadge.hidden = !reconnecting;

    const bad = isSavePhoto && data.photo_health === 'stalled';
    if (photoIndicator) photoIndicator.classList.toggle('mode-indicator--error', bad);
    if (buttons.photo) {
      buttons.photo.classList.toggle('is-save-error', bad);
      buttons.photo.title = bad
        ? `Фото не пишется: ${data.photo_error || 'причина неизвестна'}`
        : 'Автосохранение фото';
    }
    if (photoHealthLine) {
      photoHealthLine.hidden = !bad;
      photoHealthLine.textContent = bad ? `Не пишется: ${data.photo_error || ''}` : '';
    }
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
    imagePopup.close();
    networkPopup.close();
    setRegionMode(false);
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

  // прочитать имя проекта из поля; пустое -> предупреждение и null (имя обязательно)
  function readProjectName(inputName) {
    const el = document.querySelector(`input[name="${inputName}"]`);
    const value = el ? el.value.trim() : '';
    if (!value) {
      log.warn('Не указано имя проекта');
      alert('Укажите имя проекта');
      return null;
    }
    return value;
  }

  // выставить число + единицу (сек/мин) по значению в секундах: кратное 60 показываем
  // в минутах, но только если есть селектор единиц (иначе значение остаётся в секундах)
  function setIntervalField(inputName, unitName, seconds) {
    if (seconds == null || seconds === '') return;
    const input = document.querySelector(`input[name="${inputName}"]`);
    if (!input) return;
    let value = Number(seconds);
    const unitSel = unitName ? document.querySelector(`select[name="${unitName}"]`) : null;
    if (unitSel && value >= 60 && value % 60 === 0) {
      value = value / 60;
      unitSel.value = 'minutes';
    } else if (unitSel) {
      unitSel.value = 'seconds';
    }
    input.value = value;
  }

  // подтянуть сохранённые настройки (имя проекта + интервал/длительность) в модалки
  async function prefillSaveSettings() {
    if (!serial) return;
    const s = await RtspApi.getSaveSettings(serial);
    if (!s || s.error) return;
    const setVal = (name, val) => {
      const el = document.querySelector(`input[name="${name}"]`);
      if (el && val != null && val !== '') el.value = val;
    };
    setVal('photo_project', s.photo_project);
    setVal('video_project', s.video_project);
    setIntervalField('photo_interval', 'photo_interval_unit', s.photo_interval);
    setIntervalField('video_duration', 'video_duration_unit', s.video_duration);
    // формат фото (jpg/png) — тоже запоминаем между запусками
    const formatSelect = document.querySelector('select[name="photo_format"]');
    if (formatSelect && s.photo_format) formatSelect.value = s.photo_format;
  }

  async function startPhotoSaving() {
    if (!isConnected) return;

    const project = readProjectName('photo_project');
    if (project === null) return;

    const interval = UIHelpers.getPositiveNumber(
      'input[name="photo_interval"]',
      'Не выбран интервал сохранения'
    );
    if (interval === null) return;

    // единица интервала: как в видео — минуты переводим в секунды
    const unitSelect = document.querySelector('select[name="photo_interval_unit"]');
    const unit = unitSelect ? unitSelect.value : 'seconds';
    const intervalInSeconds = unit === 'minutes' ? interval * 60 : interval;

    // формат файла: jpg (компактно) или png (без потерь)
    const formatSelect = document.querySelector('select[name="photo_format"]');
    const photoFormat = formatSelect ? formatSelect.value : 'jpg';

    const data = await RtspApi.startPhotoSaving(serial, intervalInSeconds, project, photoFormat);
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

    const project = readProjectName('video_project');
    if (project === null) return;

    const duration = UIHelpers.getPositiveNumber(
      'input[name="video_duration"]',
      'Не выбрана длительность записи'
    );
    if (duration === null) return;

    const unitSelect = document.querySelector('select[name="video_duration_unit"]');
    const unit = unitSelect ? unitSelect.value : 'minutes';
    const durationInSeconds = unit === 'minutes' ? duration * 60 : duration;

    const data = await RtspApi.startVideoSaving(serial, durationInSeconds, project);
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

    // --- зум / фокус (оптика) ---
    const hasFocus = !!caps.focus;
    const hasAutoFocus = !!caps.auto_focus;
    setCapLine(zoomCap, hasOptical || hasFocus,
      'Оптический зум/фокус поддерживается',
      caps.reachable ? 'Оптика не поддерживается' : 'Камера не отвечает на управление');
    if (opticalZoomControls) opticalZoomControls.hidden = !hasOptical;
    if (focusControls) focusControls.hidden = !hasFocus;
    if (autoFocusBtn) autoFocusBtn.hidden = !hasAutoFocus;
    if (zoomOpticalHint) zoomOpticalHint.hidden = hasOptical || hasFocus;

    // --- настройки изображения ---
    const hasImage = !!caps.image_settings;
    setCapLine(imageCap, hasImage, 'Настройки изображения доступны',
      caps.reachable ? 'Настройки изображения не поддерживаются' : 'Камера не отвечает на управление');
    setImageControlsEnabled(hasImage);
  }

  async function refreshCapabilities(force) {
    if (!isConnected || !serial) return;
    const data = await RtspApi.getCapabilities(serial, force);
    if (data && !data.error) {
      capabilities = data;
      log.info('Возможности камеры получены', data);
    } else {
      capabilities = { reachable: false, white_light: false, optical_zoom: false, focus: false, auto_focus: false, image_settings: false };
      log.warn('Не удалось опросить возможности камеры', data);
    }
    applyCapabilitiesUI();
    syncLightState();
    refreshImageSettings();
    // восстановить UI зума из фактического состояния воркера (не из кэша)
    currentZoom = Number(capabilities.zoom_factor) || 1;
    markZoomLevel(currentZoom);
    setPanSliders(capabilities.zoom_pan_x, capabilities.zoom_pan_y);
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

  // ---------- настройки изображения (экспозиция / баланс белого / день-ночь) ----------

  function populateImageUI(data) {
    UIHelpers.fillSelect(imageWb, data.wb_presets, UIHelpers.IMAGE_WB_LABELS,
      data.white_balance && data.white_balance.mode);
    UIHelpers.fillSelect(imageDayNight, data.day_night_modes, UIHelpers.IMAGE_DAY_NIGHT_LABELS,
      data.day_night && data.day_night.mode);
    const exp = data.exposure || {};
    if (imageCompensation && exp.compensation != null) imageCompensation.value = exp.compensation;
    if (imageGainMin && exp.gain_min != null) imageGainMin.value = exp.gain_min;
    if (imageGainMax && exp.gain_max != null) imageGainMax.value = exp.gain_max;
  }

  function setImageControlsEnabled(enabled) {
    [imageWb, imageDayNight, imageCompensation, imageGainMin, imageGainMax].forEach((el) => {
      if (el) el.disabled = !enabled;
    });
  }

  async function refreshImageSettings() {
    if (!isConnected || !serial || !(capabilities && capabilities.image_settings)) {
      setImageControlsEnabled(false);
      return;
    }
    const data = await RtspApi.getImageSettings(serial);
    if (!data || data.error || !data.reachable) {
      setCapLine(imageCap, false, '', 'Камера не отвечает на управление');
      setImageControlsEnabled(false);
      log.warn('Не удалось получить настройки изображения', data);
      return;
    }
    setCapLine(imageCap, true, 'Настройки изображения доступны', '');
    setImageControlsEnabled(true);
    populateImageUI(data);
    log.info('Настройки изображения получены', data);
  }

  async function applyWhiteBalance() {
    if (!serial || !imageWb) return;
    const mode = imageWb.value;
    const data = await RtspApi.setWhiteBalance(serial, mode);
    if (!data || data.error || data.ok === false) {
      log.warn('Камера не подтвердила баланс белого', data);
      refreshImageSettings(); // откат к фактическому
      return;
    }
    log.success('Баланс белого применён', { mode });
  }

  async function applyDayNight() {
    if (!serial || !imageDayNight) return;
    const mode = imageDayNight.value;
    const data = await RtspApi.setDayNight(serial, mode);
    if (!data || data.error || data.ok === false) {
      log.warn('Камера не подтвердила режим день/ночь', data);
      refreshImageSettings();
      return;
    }
    log.success('Режим день/ночь применён', { mode });
  }

  // применить одно поле экспозиции (компенсация / пределы усиления)
  async function applyExposure(field, value) {
    if (!serial) return;
    const data = await RtspApi.setExposure(serial, { [field]: Number(value) });
    if (!data || data.error || data.ok === false) {
      log.warn('Камера не подтвердила экспозицию', data);
      refreshImageSettings();
      return;
    }
    log.success('Экспозиция применена', { [field]: Number(value) });
  }

  // ---------- сеть (смена IP-адреса камеры) ----------

  // простая проверка IPv4 (строгую валидность маски проверяет бэк через ipaddress)
  function isValidIp(value) {
    const m = String(value || '').trim().match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
    return !!m && m.slice(1).every((o) => Number(o) >= 0 && Number(o) <= 255);
  }

  // код ошибки от бэка → человеческий текст
  function describeNetErr(res) {
    const map = {
      bad_ip: 'некорректный IP', bad_mask: 'некорректная маска',
      bad_gateway: 'некорректный шлюз', no_host: 'нет адреса камеры',
      camera_rejected: 'камера отклонила запрос', rtsp_not_connected: 'камера не подключена',
    };
    const e = res && res.error;
    return map[e] || e || 'нет связи с камерой';
  }

  function setNetworkControlsEnabled(enabled) {
    [networkDhcp, networkIp, networkMask, networkGateway, networkApplyBtn].forEach((el) => {
      if (el) el.disabled = !enabled;
    });
  }

  // тумблер DHCP: при включённом DHCP поля статики скрываем (их выдаст сервер)
  function reflectDhcp() {
    if (networkFields) networkFields.classList.toggle('is-hidden', !!(networkDhcp && networkDhcp.checked));
  }

  async function loadNetwork() {
    if (!isConnected || !serial) return;
    if (networkCap) { networkCap.dataset.state = 'unknown'; }
    setCapLine(networkCap, false, '', 'Проверяю…');

    const data = await RtspApi.getNetwork(serial);
    if (!data || data.error || !data.reachable) {
      currentNetwork = null;
      if (networkCurrentBox) networkCurrentBox.hidden = true;
      setCapLine(networkCap, false, '',
        data && data.error === 'no_host' ? 'Управление недоступно' : 'Камера не отвечает на управление');
      setNetworkControlsEnabled(false);
      log.warn('Не удалось получить сетевые настройки', data);
      return;
    }

    currentNetwork = data;
    setCapLine(networkCap, true, 'Камера отвечает' + (data.mac ? ' · ' + data.mac : ''), '');

    if (netCurIp) netCurIp.textContent = data.ip || '—';
    if (netCurMask) netCurMask.textContent = data.mask || '—';
    if (netCurGw) netCurGw.textContent = data.gateway || '—';
    if (networkCurrentBox) networkCurrentBox.hidden = false;

    // предзаполняем поля текущими значениями камеры
    if (networkIp) networkIp.value = data.ip || '';
    if (networkMask) networkMask.value = data.mask || '';
    if (networkGateway) networkGateway.value = data.gateway || '';
    if (networkDhcp) networkDhcp.checked = !!data.dhcp;
    reflectDhcp();
    setNetworkControlsEnabled(true);
    log.info('Сетевые настройки получены', data);
  }

  // подставить новый адрес в форму подключения (чтобы connect был готов к новому IP)
  function applyNewAddressToForm(newIp, newUrl) {
    const ipInput = form.querySelector('input[name="ip"]');
    if (ipInput && newIp) ipInput.value = newIp;
    const urlInput = form.querySelector('input[name="url"]');
    if (urlInput && urlInput.value.trim() && newUrl) urlInput.value = newUrl;
  }

  async function applyNetworkDhcp() {
    if (!confirm('Включить DHCP?\n\nКамера получит новый IP автоматически от роутера. '
      + 'Текущий поток закроется — камеру нужно будет найти по новому адресу и подключить заново.')) return;

    const res = await RtspApi.setNetwork(serial, { dhcp: true });
    if (!res || res.error || !res.ok) {
      log.warn('Камера не подтвердила включение DHCP', res);
      alert('Не удалось включить DHCP: ' + describeNetErr(res));
      return;
    }
    log.success('DHCP включён', res);
    networkPopup.close();
    alert('DHCP включён. Камера получит новый IP от роутера — найдите её по новому адресу и подключитесь заново.');
    await stopCamera();
  }

  async function applyNetworkStatic() {
    const ip = networkIp.value.trim();
    const mask = networkMask.value.trim();
    const gateway = networkGateway.value.trim();

    if (!isValidIp(ip)) { alert('Некорректный IP-адрес'); networkIp.focus(); return; }
    if (!isValidIp(mask)) { alert('Некорректная маска подсети'); networkMask.focus(); return; }
    if (gateway && !isValidIp(gateway)) { alert('Некорректный шлюз'); networkGateway.focus(); return; }

    // diff-подтверждение: показываем только реально меняющиеся строки
    const cur = currentNetwork || {};
    const lines = [`IP:    ${cur.ip || '—'} → ${ip}`];
    if ((cur.mask || '') !== mask) lines.push(`Маска: ${cur.mask || '—'} → ${mask}`);
    if ((cur.gateway || '') !== gateway) lines.push(`Шлюз:  ${cur.gateway || '—'} → ${gateway}`);
    if (cur.dhcp) lines.push('DHCP:  вкл → выкл (статический адрес)');

    const msg = 'Сменить сетевые настройки камеры?\n\n' + lines.join('\n')
      + '\n\nПосле применения камера станет доступна по новому адресу. '
      + 'Убедитесь, что этот адрес в вашей подсети, иначе камера пропадёт из сети.';
    if (!confirm(msg)) return;

    const res = await RtspApi.setNetwork(serial, { ip, mask, gateway, dhcp: false });
    if (!res || res.error || !res.ok) {
      log.warn('Камера не подтвердила смену сети', res);
      alert('Не удалось сменить настройки: ' + describeNetErr(res));
      return;
    }

    const newIp = res.new_ip || ip;
    log.success('Сетевые настройки применены', res);
    networkPopup.close();

    // новый адрес — в форму, затем переподключаемся (даём камере применить смену)
    applyNewAddressToForm(newIp, res.new_url);
    await stopCamera();
    alert('IP изменён на ' + newIp + '. Переподключаюсь через несколько секунд…');
    setTimeout(() => { connectCamera(); }, 4000);
  }

  async function applyNetwork() {
    if (!serial || !isConnected) return;
    if (networkDhcp && networkDhcp.checked) {
      await applyNetworkDhcp();
    } else {
      await applyNetworkStatic();
    }
  }

  function markZoomLevel(factor) {
    if (zoomLevels) {
      zoomLevels.querySelectorAll('button[data-zoom]').forEach((btn) => {
        btn.classList.toggle('active', Number(btn.dataset.zoom) === factor);
      });
    }
    if (zoomIndicator) zoomIndicator.classList.toggle('hidden', factor <= 1);
    if (zoomPan) zoomPan.hidden = factor <= 1;
    updateRegionBtn(); // иконка «зум областью» отражает активность зума
  }

  // выставить ползунки области (px, py в 0..1); вертикаль перевёрнута: вправо = вверх
  function setPanSliders(px, py) {
    if (panX) panX.value = Math.round((px ?? 0.5) * 100);
    if (panY) panY.value = Math.round((1 - (py ?? 0.5)) * 100);
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
    setPanSliders(0.5, 0.5); // новая кратность — вид в центре
    log.success('Цифровой зум ×' + currentZoom, data);
  }

  // применить положение области из ползунков (плавно, во время перетаскивания)
  async function applyZoomPan() {
    if (!serial || currentZoom <= 1) return;
    const px = panX ? Number(panX.value) / 100 : 0.5;
    const py = panY ? 1 - Number(panY.value) / 100 : 0.5; // вправо = вверх
    const data = await RtspApi.setZoomPan(serial, px, py);
    if (!data || data.error) {
      log.warn('Камера не подтвердила смещение области зума', data);
    }
  }

  // ---------- ТЕСТ: зум выделением области (рамкой) поверх видео ----------
  const regionZoomBtn = document.getElementById('regionZoomBtn');
  const marqueeLayer = document.getElementById('zoomMarqueeLayer');
  const marqueeBox = document.getElementById('zoomMarqueeBox');
  let regionMode = false;
  let regionDrag = null;

  // иконка горит оранжевым, когда идёт выделение ИЛИ активен зум (>1×)
  function updateRegionBtn() {
    if (regionZoomBtn) regionZoomBtn.classList.toggle('is-region-active', regionMode || currentZoom > 1);
  }

  function setRegionMode(on) {
    regionMode = !!on && isConnected;
    if (marqueeLayer) marqueeLayer.hidden = !regionMode;
    if (marqueeBox) marqueeBox.hidden = true;
    if (!regionMode) regionDrag = null;
    updateRegionBtn();
  }

  // сброс зума на 1× (по повторному клику на иконку, когда уже приближено)
  async function resetRegionZoom() {
    if (!serial) return;
    const data = await RtspApi.setZoom(serial, 1);
    if (data && !data.error) { currentZoom = 1; markZoomLevel(1); }
    setRegionMode(false);
  }

  // видимый прямоугольник видео внутри <img> (object-fit: contain, с полями)
  function displayedVideoRect() {
    const nw = rtspFrame.naturalWidth, nh = rtspFrame.naturalHeight;
    const bw = rtspFrame.clientWidth, bh = rtspFrame.clientHeight;
    if (!nw || !nh || !bw || !bh) return null;
    const nr = nw / nh, br = bw / bh;
    if (nr > br) { const dh = bw / nr; return { dw: bw, dh, ox: 0, oy: (bh - dh) / 2 }; }
    const dw = bh * nr; return { dw, dh: bh, ox: (bw - dw) / 2, oy: 0 };
  }

  function layerXY(event) {
    const r = marqueeLayer.getBoundingClientRect();
    return { x: event.clientX - r.left, y: event.clientY - r.top };
  }

  function updateMarqueeBox() {
    if (!marqueeBox || !regionDrag) return;
    const { x0, y0, x1, y1 } = regionDrag;
    marqueeBox.hidden = false;
    marqueeBox.style.left = Math.min(x0, x1) + 'px';
    marqueeBox.style.top = Math.min(y0, y1) + 'px';
    marqueeBox.style.width = Math.abs(x1 - x0) + 'px';
    marqueeBox.style.height = Math.abs(y1 - y0) + 'px';
  }

  async function applyRegionZoom() {
    const rect = displayedVideoRect();
    if (!rect || !regionDrag || !serial) { setRegionMode(false); return; }
    const { x0, y0, x1, y1 } = regionDrag;
    if (Math.abs(x1 - x0) < 12 || Math.abs(y1 - y0) < 12) { setRegionMode(false); return; }

    const { dw, dh, ox, oy } = rect;
    const clamp01 = (v) => Math.max(0, Math.min(1, v));
    const nx0 = clamp01((Math.min(x0, x1) - ox) / dw);
    const ny0 = clamp01((Math.min(y0, y1) - oy) / dh);
    const nx1 = clamp01((Math.max(x0, x1) - ox) / dw);
    const ny1 = clamp01((Math.max(y0, y1) - oy) / dh);
    const rw = Math.max(0.02, nx1 - nx0);
    const rh = Math.max(0.02, ny1 - ny0);
    const ncx = (nx0 + nx1) / 2, ncy = (ny0 + ny1) / 2;

    // кратность: чтобы выделенный прямоугольник влез в область просмотра
    let factor = Math.max(1, Math.min(4, Math.min(1 / rw, 1 / rh)));
    const z = 1 / factor;
    const px = factor > 1 ? clamp01((ncx - z / 2) / (1 - z)) : 0.5;
    const py = factor > 1 ? clamp01((ncy - z / 2) / (1 - z)) : 0.5;

    const data = await RtspApi.setZoomRegion(serial, factor, px, py);
    if (data && !data.error) {
      currentZoom = data.factor || factor;
      markZoomLevel(currentZoom);
      setPanSliders(data.pan_x, data.pan_y);
      log.success('Зум по области ×' + currentZoom.toFixed(1), { px, py });
    }
    setRegionMode(false); // после выделения выходим из режима
  }

  if (regionZoomBtn) regionZoomBtn.addEventListener('click', () => {
    // приближено → повторный клик сбрасывает на 1×; иначе — вход в выделение
    if (currentZoom > 1) resetRegionZoom();
    else setRegionMode(!regionMode);
  });
  if (marqueeLayer) {
    marqueeLayer.addEventListener('mousedown', (event) => {
      if (!regionMode) return;
      event.preventDefault();
      const p = layerXY(event);
      regionDrag = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      updateMarqueeBox();
    });
    window.addEventListener('mousemove', (event) => {
      if (!regionDrag) return;
      const p = layerXY(event);
      regionDrag.x1 = p.x; regionDrag.y1 = p.y;
      updateMarqueeBox();
    });
    window.addEventListener('mouseup', () => {
      if (regionDrag) applyRegionZoom();
    });
  }

  // показать реальную позицию зума (0..1) на индикаторе
  function setZoomUI(zoom01) {
    if (zoom01 == null) return;
    const pct = Math.round(zoom01 * 100);
    const fill = document.getElementById('zoomBarFill');
    const val = document.getElementById('zoomVal');
    if (fill) fill.style.width = pct + '%';
    if (val) val.textContent = pct + '%';
  }

  // текущая позиция объектива (0..1) — обратка для индикатора зума и ползунка фокуса.
  // НЕ зависим от capabilities: дёргаем сразу по serial, иначе фокус «залипает» на 0,
  // пока не подтянутся возможности / не будет первого движения.
  async function initLens() {
    if (!serial) return;
    const st = await RtspApi.lensStatus(serial);
    if (!st || st.error) return;
    if (st.zoom != null) setZoomUI(st.zoom);
    if (st.focus != null) {
      const fs = document.getElementById('focusSlider');
      const fv = document.getElementById('focusVal');
      if (fs) fs.value = Math.round(st.focus * 100);
      if (fv) fv.textContent = Math.round(st.focus * 100) + '%';
    }
  }

  // зум — кнопки −/+ на удержание (Wide/Tele) с ЖИВОЙ обраткой: пока держишь, мотор
  // едет и индикатор обновляется реальным zoom с камеры (абсолютно зум не позиционируется).
  let zoomHoldTimer = null;
  async function zoomHoldStart(dir) {
    if (!serial || !(capabilities && capabilities.optical_zoom)) return;
    await RtspApi.opticalZoom(serial, dir, 5);
    if (zoomHoldTimer) clearInterval(zoomHoldTimer);
    zoomHoldTimer = setInterval(async () => {
      const st = await RtspApi.lensStatus(serial);
      if (st && !st.error && st.zoom != null) setZoomUI(st.zoom);
    }, 250);
  }
  async function zoomHoldStop() {
    if (zoomHoldTimer) { clearInterval(zoomHoldTimer); zoomHoldTimer = null; }
    if (!serial) return;
    await RtspApi.opticalZoom(serial, 'stop');
    const st = await RtspApi.lensStatus(serial);
    if (st && !st.error && st.zoom != null) setZoomUI(st.zoom);
  }

  // фокус — абсолютный ползунок (0..100 → 0..1)
  async function focusSet(sliderVal) {
    if (!serial || !(capabilities && capabilities.focus)) return;
    const fv = document.getElementById('focusVal');
    if (fv) fv.textContent = sliderVal + '%';
    const data = await RtspApi.focusAbs(serial, (sliderVal / 100).toFixed(4));
    if (!data || data.error || data.status === 'failed') log.warn('Камера не подтвердила фокус', data);
  }

  async function autoFocusOnce() {
    if (!serial || !(capabilities && capabilities.auto_focus)) return;
    const data = await RtspApi.autoFocus(serial);
    if (!data || data.error || data.status === 'failed') { log.warn('Камера не подтвердила автофокус', data); return; }
    log.success('Автофокус выполнен', data);
    initLens();   // фокус изменился — подтянуть ползунок
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
    initLens();   // подтянуть текущую позицию фокуса в ползунок
  });
  if (buttons.image) buttons.image.addEventListener('click', (event) => {
    event.stopPropagation();
    imagePopup.toggle();
  });
  if (buttons.network) buttons.network.addEventListener('click', (event) => {
    event.stopPropagation();
    const willOpen = !networkPopup.isOpen();
    networkPopup.toggle();
    if (willOpen) loadNetwork();
  });

  if (photoOnBtn) photoOnBtn.addEventListener('click', startPhotoSaving);
  if (photoOffBtn) photoOffBtn.addEventListener('click', stopPhotoSaving);
  if (videoOnBtn) videoOnBtn.addEventListener('click', startVideoSaving);
  if (videoOffBtn) videoOffBtn.addEventListener('click', stopVideoSaving);

  if (lightSwitch) lightSwitch.addEventListener('change', () => setLight(lightSwitch.checked));

  // настройки изображения: применяем сразу при изменении (ползунки — по отпусканию)
  if (imageWb) imageWb.addEventListener('change', applyWhiteBalance);
  if (imageDayNight) imageDayNight.addEventListener('change', applyDayNight);
  if (imageCompensation) imageCompensation.addEventListener('change', () => applyExposure('compensation', imageCompensation.value));
  if (imageGainMin) imageGainMin.addEventListener('change', () => applyExposure('gain_min', imageGainMin.value));
  if (imageGainMax) imageGainMax.addEventListener('change', () => applyExposure('gain_max', imageGainMax.value));

  if (networkDhcp) networkDhcp.addEventListener('change', reflectDhcp);
  if (networkApplyBtn) networkApplyBtn.addEventListener('click', applyNetwork);

  if (zoomLevels) zoomLevels.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-zoom]');
    if (btn) applyDigitalZoom(Number(btn.dataset.zoom));
  });
  if (panX) panX.addEventListener('input', applyZoomPan);
  if (panY) panY.addEventListener('input', applyZoomPan);
  // зум — кнопки −/+ на удержание, индикатор показывает реальный zoom
  const zoomJogRow = document.querySelector('#opticalZoomControls .optic-jog');
  if (zoomJogRow) {
    zoomJogRow.addEventListener('pointerdown', (e) => {
      const b = e.target.closest('button[data-zoom]');
      if (!b) return;
      e.preventDefault();
      zoomHoldStart(b.getAttribute('data-zoom'));
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach((ev) =>
      zoomJogRow.addEventListener(ev, zoomHoldStop));
  }
  // фокус — абсолютный ползунок (по отпусканию/изменению)
  const focusSlider = document.getElementById('focusSlider');
  if (focusSlider) {
    focusSlider.addEventListener('input', () => {
      const fv = document.getElementById('focusVal'); if (fv) fv.textContent = focusSlider.value + '%';
    });
    focusSlider.addEventListener('change', () => focusSet(Number(focusSlider.value)));
  }
  if (autoFocusBtn) autoFocusBtn.addEventListener('click', autoFocusOnce);

  if (photoCard) photoCard.addEventListener('click', (event) => event.stopPropagation());
  if (videoCard) videoCard.addEventListener('click', (event) => event.stopPropagation());
  if (lightCard) lightCard.addEventListener('click', (event) => event.stopPropagation());
  if (zoomCard) zoomCard.addEventListener('click', (event) => event.stopPropagation());
  if (imageCard) imageCard.addEventListener('click', (event) => event.stopPropagation());
  if (networkCard) networkCard.addEventListener('click', (event) => event.stopPropagation());

  document.addEventListener('click', (event) => {
    const insidePhoto = photoCard && photoCard.contains(event.target);
    const onPhotoBtn = buttons.photo && buttons.photo.contains(event.target);
    const insideVideo = videoCard && videoCard.contains(event.target);
    const onVideoBtn = buttons.video && buttons.video.contains(event.target);
    const insideLight = lightCard && lightCard.contains(event.target);
    const onLightBtn = buttons.light && buttons.light.contains(event.target);
    const insideZoom = zoomCard && zoomCard.contains(event.target);
    const onZoomBtn = buttons.zoom && buttons.zoom.contains(event.target);
    const insideImage = imageCard && imageCard.contains(event.target);
    const onImageBtn = buttons.image && buttons.image.contains(event.target);
    const insideNetwork = networkCard && networkCard.contains(event.target);
    const onNetworkBtn = buttons.network && buttons.network.contains(event.target);

    if (!insidePhoto && !onPhotoBtn) photoPopup.close();
    if (!insideVideo && !onVideoBtn) videoPopup.close();
    if (!insideLight && !onLightBtn) lightPopup.close();
    if (!insideZoom && !onZoomBtn) zoomPopup.close();
    if (!insideImage && !onImageBtn) imagePopup.close();
    if (!insideNetwork && !onNetworkBtn) networkPopup.close();
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

    // подтянуть сохранённые имя проекта / интервал в модалки
    prefillSaveSettings();
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

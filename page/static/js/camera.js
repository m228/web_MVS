function initCameraPage() {
  const form = document.getElementById('settingsForm');
  const serialElement = document.getElementById('cameraSerial');
  const cameraFrame = document.getElementById('cameraFrame');
  const cameraPlaceholder = document.getElementById('cameraPlaceholder');

  const metricFps = document.getElementById('metricFps');
  const metricImageNumber = document.getElementById('metricImageNumber');
  const metricBandwidth = document.getElementById('metricBandwidth');
  const metricResolution = document.getElementById('metricResolution');
  const metricErrors = document.getElementById('metricErrors');
  const metricPhotoCount = document.getElementById('metricPhotoCount');
  const metricVideoTime = document.getElementById('metricVideoTime');

  const configCard = document.getElementById('configCard');
  const configInfoBtn = document.getElementById('configInfoBtn');
  const configList = document.getElementById('configList');
  const configEmpty = document.getElementById('configEmpty');

  const params = new URLSearchParams(window.location.search);
  const serialNumber = params.get('serial_number');
  const interfaceId = params.get('interface_id') || '';
  const deviceHandle = params.get('device_handle') || '';

  const buttons = {
    start: document.getElementById('startBtn'),
    apply: document.getElementById('applyBtn'),
    stop: document.getElementById('stopBtn'),
    force_stop: document.getElementById('forceStopBtn'),
    photo: document.getElementById('photoBtn'),
    video: document.getElementById('videoBtn'),
    network_settings: document.getElementById('networkSettingsBtn'),
  };

  const photoCard = document.getElementById('photoCard');
  const photoOnBtn = document.getElementById('photoOn');
  const photoOffBtn = document.getElementById('photoOff');
  const photoIndicator = document.getElementById('photoIndicator');

  const videoCard = document.getElementById('videoCard');
  const videoOnBtn = document.getElementById('videoOn');
  const videoOffBtn = document.getElementById('videoOff');
  const videoIndicator = document.getElementById('videoIndicator');

  if (
    !form ||
    !serialElement ||
    !cameraFrame ||
    !cameraPlaceholder ||
    !buttons.start ||
    !buttons.apply ||
    !buttons.stop
  ) {
    window.AppLog?.error('camera', 'Не найдены обязательные элементы страницы камеры');
    return;
  }

  let isChange = false;
  let isConnected = false;
  let isLoading = false;
  let isSavePhoto = false;
  let isSaveVideo = false;
  let isStoppingStream = false;
  let statusTimer = null;
  let activeSliderWrap = null;
  let isLeavingPage = false;
  let forceStopTimer = null;
  let waitingSoftStop = false;
  let metricsTimer = null;
  let streamErrorHandled = false;


  const photoPopup = UIHelpers.createPopupController(photoCard, buttons.photo);
  const videoPopup = UIHelpers.createPopupController(videoCard, buttons.video);
  const configPopup = UIHelpers.createPopupController(configCard, configInfoBtn);

  serialElement.textContent = serialNumber ? serialNumber : 'не выбран';
  if (interfaceId && serialElement.parentElement) {
    const ifaceLine = document.createElement('div');
    ifaceLine.className = 'status-code';
    ifaceLine.style.marginTop = '6px';
    ifaceLine.textContent = 'Интерфейс: ' + interfaceId;
    serialElement.parentElement.insertAdjacentElement('afterend', ifaceLine);
  }

  const log = {
    info: (message, payload) => window.AppLog?.info('camera', message, payload),
    success: (message, payload) => window.AppLog?.success('camera', message, payload),
    warn: (message, payload) => window.AppLog?.warn('camera', message, payload),
    error: (message, payload) => window.AppLog?.error('camera', message, payload),
    debug: (message, payload) => window.AppLog?.debug('camera', message, payload),
  };

  log.info('Страница камеры открыта', { serialNumber });

  function refreshModeUI() {
    updateModeIndicators();
    updateSaveButtonsState();
  }

  function setSaveState({ photo = isSavePhoto, video = isSaveVideo } = {}) {
    const prevPhoto = isSavePhoto;
    const prevVideo = isSaveVideo;

    isSavePhoto = photo;
    isSaveVideo = video;

    if (prevPhoto !== isSavePhoto) {
      log.info(isSavePhoto ? 'Сохранение фото включено' : 'Сохранение фото выключено');
    }

    if (prevVideo !== isSaveVideo) {
      log.info(isSaveVideo ? 'Сохранение видео включено' : 'Сохранение видео выключено');
    }

    refreshModeUI();
  }

  function setApplyVisualState() {
    const applyBtn = buttons.apply;
    if (!applyBtn) return;

    applyBtn.disabled = !isConnected || !isChange;
    applyBtn.classList.toggle('grey-btn', !isConnected || !isChange);
  }

  function applyState(state) {
    for (const [name, config] of Object.entries(state)) {
      const button = buttons[name];
      if (!button) continue;

      button.classList.toggle('hidden', config.hidden ?? false);
      button.disabled = config.disabled ?? false;
    }

    setApplyVisualState();
  }

  function updateToolbarState() {
    const state = {
      start: { hidden: true, disabled: true },
      apply: { hidden: true, disabled: true },
      stop: { hidden: true, disabled: true },
      photo: { hidden: true, disabled: true },
      video: { hidden: true, disabled: true },
      network_settings: { hidden: false, disabled: !serialNumber },
    };

    if (!serialNumber) {
      state.start = { hidden: false, disabled: false };
      state.apply = { hidden: false, disabled: true };
      state.stop = { hidden: false, disabled: true };
      state.photo = { hidden: false, disabled: false };
      state.video = { hidden: false, disabled: false };
      applyState(state);
      return;
    }

    if (isLoading) {
      state.start = { hidden: false, disabled: true };
      state.network_settings = { hidden: false, disabled: true };
      applyState(state);
      return;
    }

    if (!isConnected) {
      state.start = { hidden: false, disabled: false };
      applyState(state);
      return;
    }

    state.apply = { hidden: false, disabled: !isChange };
    state.stop = { hidden: false, disabled: false };
    state.photo = { hidden: false, disabled: false };
    state.video = { hidden: false, disabled: false };

    applyState(state);
  }

  function showNoVideo() {
    cameraFrame.classList.remove('visible');
    cameraPlaceholder.classList.remove('hidden');
  }

  function showVideo() {
    cameraFrame.classList.add('visible');
    cameraPlaceholder.classList.add('hidden');
  }

  function updateModeIndicators() {
    if (photoIndicator) {
      photoIndicator.classList.toggle('hidden', !isSavePhoto);
    }

    if (videoIndicator) {
      videoIndicator.classList.toggle('hidden', !isSaveVideo);
    }
  }

  function updateSaveButtonsState() {
    if (photoOnBtn) photoOnBtn.disabled = isSavePhoto;
    if (photoOffBtn) photoOffBtn.disabled = !isSavePhoto;
    if (videoOnBtn) videoOnBtn.disabled = isSaveVideo;
    if (videoOffBtn) videoOffBtn.disabled = !isSaveVideo;
  }

  function markDirty() {
    if (!isConnected) return;

    if (!isChange) {
      log.debug('Параметры камеры изменены пользователем');
    }

    isChange = true;
    updateToolbarState();
  }

  function updateMetricsUI(data) {
    if (metricFps) metricFps.textContent = `${Number(data.fps ?? 0).toFixed(2)} fps`;
    if (metricImageNumber) metricImageNumber.textContent = data.image_number ?? 0;
    if (metricBandwidth) metricBandwidth.textContent = `${Number(data.bandwidth_mbps ?? 0).toFixed(1)} Mbps`;
    if (metricResolution) metricResolution.textContent = `${data.width ?? 0} × ${data.height ?? 0}`;
    if (metricErrors) metricErrors.textContent = data.errors ?? 0;
  }

  function resetMetricsUI() {
    updateMetricsUI({
      fps: 0,
      image_number: 0,
      bandwidth_mbps: 0,
      width: 0,
      height: 0,
      errors: 0,
    });
    if (metricPhotoCount) metricPhotoCount.textContent = '0';
    if (metricVideoTime) metricVideoTime.textContent = '—';
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
    if (!isConnected) return;

    const data = await CameraApi.getMetrics(serialNumber);
    if (!data) return;

    updateMetricsUI(data);
  }

  function resetCameraUI() {
    isConnected = false;
    isChange = false;
    isLoading = false;
    isStoppingStream = true;
    waitingSoftStop = false;

    setSaveState({ photo: false, video: false });

    cameraFrame.src = '';
    showNoVideo();

    photoPopup.close();
    videoPopup.close();
    configPopup.close();
    removeActiveSlider();

    updateToolbarState();
    stopMetricsPolling();
    resetMetricsUI();
  }

  function setFieldValue(name, value) {
    if (value === undefined || value === null) return;

    const field = form.querySelector(`[name="${name}"]`);
    if (field) {
      field.value = value;
    }
  }

  function setFieldLimits(name, config) {
    if (!config) return;

    const field = form.querySelector(`[name="${name}"]`);
    if (!field) return;

    if (config.min !== undefined && config.min !== null) {
      field.min = config.min;
    }

    if (config.max !== undefined && config.max !== null) {
      field.max = config.max;
    }

    if (config.step !== undefined && config.step !== null) {
      field.step = config.step;
    }
  }

  function setText(id, value) {
    if (value === undefined || value === null) return;

    const el = document.getElementById(id);
    if (el) {
      el.textContent = value;
    }
  }

  function fillSelectOptions(name, config) {
    if (!config) return;

    const select = form.querySelector(`[name="${name}"]`);
    if (!select) return;

    select.innerHTML = '';

    if (Array.isArray(config.options)) {
      for (const optionValue of config.options) {
        const option = document.createElement('option');
        option.value = optionValue;
        option.textContent = optionValue;

        if (optionValue === config.value) {
          option.selected = true;
        }

        select.appendChild(option);
      }
    } else if (config.value !== undefined && config.value !== null) {
      const option = document.createElement('option');
      option.value = config.value;
      option.textContent = config.value;
      option.selected = true;
      select.appendChild(option);
    }
  }

  async function loadDataLimitToForm() {
    if (!serialNumber) return;

    log.info('Загрузка ограничений камеры', { serialNumber });

    const data = await CameraApi.getDataLimit(serialNumber);
    if (!data) {
      log.debug('data_limit пока недоступен (камера не подключалась)', { serialNumber });
      return;
    }

    log.debug('Ограничения камеры загружены', data);

    setFieldValue('width', data.width?.value);
    setFieldValue('height', data.height?.value);
    setFieldValue('exposure_time', data.exposure_time?.value);

    setFieldLimits('width', data.width);
    setFieldLimits('height', data.height);
    setFieldLimits('offset_x', data.offset_x);
    setFieldLimits('offset_y', data.offset_y);
    setFieldLimits('exposure_time', data.exposure_time);

    fillSelectOptions('exposure_auto', data.exposure_auto);
    fillSelectOptions('pixel_format', data.pixel_format);

    setText('WidthMin', data.width?.min);
    setText('WidthMax', data.width?.max);

    setText('HeightMin', data.height?.min);
    setText('HeightMax', data.height?.max);

    setText('ExposureMin', data.exposure_time?.min);
    setText('ExposureMax', data.exposure_time?.max);
  }

  function buildQueryFromForm() {
    const formData = new FormData(form);
    const query = new URLSearchParams();

    query.set('serial_number', serialNumber);
    if (interfaceId) query.set('interface_id', interfaceId);
    if (deviceHandle) query.set('device_handle', deviceHandle);

    const fields = [
      ['width', 'width'],
      ['height', 'height'],
      ['offset_x', 'offset_x'],
      ['offset_y', 'offset_y'],
      ['fps', 'fps'],
      ['exposure_auto', 'exposure_auto'],
      ['exposure_time', 'exposure_time'],
      ['pixel_format', 'pixel_format'],
    ];

    for (const [formName, queryName] of fields) {
      const value = formData.get(formName);
      if (value !== '') {
        query.set(queryName, value);
      }
    }

    return query;
  }

  function removeActiveSlider() {
    if (activeSliderWrap) {
      activeSliderWrap.remove();
      activeSliderWrap = null;
    }
  }

  function createSliderForInput(input) {
    if (!input) return;

    const min = input.min;
    const max = input.max;
    const step = input.step && input.step !== 'any' ? input.step : '1';

    if (min === '' || max === '') return;

    removeActiveSlider();

    const wrap = document.createElement('div');
    wrap.className = 'input-slider-wrap';

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = min;
    slider.max = max;
    slider.step = step;
    slider.value = input.value || min;
    slider.className = 'input-slider';

    slider.addEventListener('input', () => {
      input.value = slider.value;
      markDirty();
    });

    input.addEventListener('input', () => {
      slider.value = input.value;
    });

    wrap.appendChild(slider);
    input.insertAdjacentElement('afterend', wrap);
    activeSliderWrap = wrap;
  }

  function initFieldSliders() {
    const sliderFields = [
      form.querySelector('[name="width"]'),
      form.querySelector('[name="height"]'),
      form.querySelector('[name="exposure_time"]'),
    ].filter(Boolean);

    sliderFields.forEach((field) => {
      field.addEventListener('focus', () => {
        createSliderForInput(field);
      });
    });
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
    if (!isConnected) return;

    const data = await CameraApi.getVideoPhotoStatus(serialNumber);
    if (!data) return;

    setSaveState({
      video: Number(data.video) === 1,
      photo: !!data.photo,
    });

    if (metricPhotoCount) metricPhotoCount.textContent = data.photo_count ?? 0;
    if (metricVideoTime) {
      metricVideoTime.textContent = Number(data.video) === 1
        ? formatDuration(data.video_elapsed)
        : '—';
    }
  }

  // секунды -> «M:SS» (или «H:MM:SS» если больше часа)
  // formatDuration / showSavePath вынесены в ui.js (общие для camera/multi/rtsp)

  function startStream() {
    const query = buildQueryFromForm();

    log.info('Старт потока', Object.fromEntries(query.entries()));

    isStoppingStream = false;
    streamErrorHandled = false;
    isLoading = true;
    updateToolbarState();
    cameraFrame.src = '/api/camera/stream?' + query.toString();
  }

  async function stopStreamOnly() {
    return await CameraApi.closeStream(serialNumber);
  }

  async function stopStreamForce() {
    return await CameraApi.closeStreamForce(serialNumber);
  }

  function showForceStopButton() {
    if (buttons.force_stop) {
      buttons.force_stop.classList.remove('hidden');
      buttons.force_stop.disabled = false;
    }
  }

  function hideForceStopButton() {
    if (buttons.force_stop) {
      buttons.force_stop.classList.add('hidden');
      buttons.force_stop.disabled = true;
    }
  }

  function clearForceStopTimer() {
    if (forceStopTimer) {
      clearTimeout(forceStopTimer);
      forceStopTimer = null;
    }
  }

  async function waitSoftStopResult() {
    const timeoutMs = 3000;
    const intervalMs = 200;
    const startedAt = Date.now();

    while (Date.now() - startedAt < timeoutMs) {
      const state = await CameraApi.getStreamState(serialNumber);

      if (state?.closed === true) {
        waitingSoftStop = false;
        hideForceStopButton();
        clearForceStopTimer();
        resetCameraUI();
        return true;
      }

      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }

    showForceStopButton();
    return false;
  }

  async function connectCamera() {
    log.info('Нажата кнопка "Подключить"');

    if (!serialNumber) {
      log.warn('Подключение отменено: камера не выбрана');
      alert('Камера не выбрана');
      return;
    }

    isConnected = false;
    isChange = false;
    startStream();
  }

  async function waitUntilStreamClosed(timeoutMs = 4000, intervalMs = 150) {
    const startedAt = Date.now();

    while (Date.now() - startedAt < timeoutMs) {
      const state = await CameraApi.getStreamState(serialNumber);

      if (state?.closed === true) {
        return true;
      }

      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }

    return false;
  }

  async function applySettings() {
    if (!serialNumber || !isConnected || !isChange) return;

    log.info('Применение новых настроек', Object.fromEntries(buildQueryFromForm().entries()));

    isConnected = false;
    isLoading = true;
    isStoppingStream = true;
    updateToolbarState();
    stopStatusPolling();

    await stopStreamOnly();

    const closed = await waitUntilStreamClosed();

    if (!closed) {
      log.warn('Поток не успел корректно закрыться перед применением настроек');
      isLoading = false;
      alert('Предыдущий поток не успел корректно закрыться');
      updateToolbarState();
      return;
    }

    cameraFrame.src = '';
    showNoVideo();
    startStream();
  }

  async function stopCamera() {
    log.info('Запрошена мягкая остановка потока');

    stopStatusPolling();
    hideForceStopButton();
    clearForceStopTimer();

    waitingSoftStop = true;
    isStoppingStream = true;

    const result = await stopStreamOnly();
    if (!result) {
      log.warn('Сервер не подтвердил мягкую остановку потока');
      waitingSoftStop = false;
      return;
    }

    forceStopTimer = setTimeout(() => {
      if (waitingSoftStop) {
        log.warn('Мягкая остановка зависла, показываю кнопку принудительной остановки');
        showForceStopButton();
      }
    }, 3000);

    const closed = await waitSoftStopResult();

    if (closed) {
      log.success('Поток остановлен мягко');
      updateToolbarState();
    }
  }

  async function forceStopCamera() {
    log.warn('Запрошена принудительная остановка потока');

    clearForceStopTimer();
    waitingSoftStop = false;
    isStoppingStream = true;

    await stopStreamForce();

    hideForceStopButton();
    resetCameraUI();
    updateToolbarState();
  }

  function openPhotoPopup() {
    photoPopup.toggle();
  }

  function openVideoPopup() {
    videoPopup.toggle();
  }

  // подписи и порядок строк текущего конфига
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

  async function renderCurrentConfig() {
    if (!configList) return;
    const cfg = await CameraApi.getCurrentConfig(serialNumber);
    const hasData = cfg && Object.keys(cfg).length > 0;

    if (configEmpty) configEmpty.hidden = hasData;
    configList.innerHTML = '';
    if (!hasData) return;

    CONFIG_ROWS.forEach(([key, label]) => {
      if (cfg[key] === undefined || cfg[key] === null) return;
      const row = document.createElement('div');
      row.className = 'info-row';
      // значение приходит с бэкенда (GenICam nodemap) — вставляем через textContent,
      // а не innerHTML, чтобы исключить XSS при неожиданном содержимом
      const dt = document.createElement('dt');
      dt.className = 'info-row__label';
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.className = 'info-row__value';
      dd.textContent = String(cfg[key]);
      row.append(dt, dd);
      configList.appendChild(row);
    });
  }

  async function openConfigPopup() {
    configPopup.toggle();
    if (configPopup.isOpen()) await renderCurrentConfig();
  }

  // показать путь сохранения (папка + шаблон имени файла) из ответа сервера
  async function startPhotoSaving() {
    if (!isConnected) return;

    const interval = UIHelpers.getPositiveNumber(
      'input[name="photo_interval"]',
      'Не выбран интервал сохранения'
    );
    if (interval === null) return;

    log.info('Запуск сохранения фото', { interval });

    const data = await CameraApi.startPhotoSaving(serialNumber, interval);
    if (!data) {
      log.warn('Сервер не подтвердил запуск сохранения фото');
      return;
    }

    log.success('Сохранение фото включено сервером', data);
    showSavePath('photoSavePath', data);
    await syncVideoPhotoStatus();
  }

  async function stopPhotoSaving() {
    log.info('Отключение сохранения фото');

    const data = await CameraApi.stopPhotoSaving(serialNumber);
    if (!data) {
      log.warn('Сервер не подтвердил отключение сохранения фото');
      return;
    }

    log.success('Сохранение фото выключено сервером', data);
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

    log.info('Запуск записи видео', {
      duration,
      unit,
      durationInSeconds,
    });

    const data = await CameraApi.startVideoSaving(serialNumber, durationInSeconds);
    if (!data) {
      log.warn('Сервер не подтвердил запуск записи видео');
      return;
    }

    log.success('Запись видео включена сервером', data);
    showSavePath('videoSavePath', data);
    await syncVideoPhotoStatus();
  }

  async function stopVideoSaving() {
    if (!isConnected) return;

    log.info('Отключение записи видео');

    const data = await CameraApi.stopVideoSaving(serialNumber);
    if (!data) {
      log.warn('Сервер не подтвердил отключение записи видео');
      return;
    }

    log.success('Запись видео выключена сервером', data);
    showSavePath('videoSavePath', null);
    await syncVideoPhotoStatus();
  }

  function cleanupCameraPage() {
    stopStatusPolling();
    resetCameraUI();
  }

  function notifyBackendBeforeUnload() {
    if (!serialNumber) return;

    const url = '/api/camera/close_stream?serial_number=' + encodeURIComponent(serialNumber);

    try {
      navigator.sendBeacon(url);
    } catch (error) {
      fetch(url, {
        method: 'GET',
        keepalive: true,
      }).catch(() => {});
    }
  }

  function handlePageLeave() {
    if (isLeavingPage) return;

    log.debug('Пользователь покидает страницу камеры');

    isLeavingPage = true;
    isStoppingStream = true;
    cleanupCameraPage();
    notifyBackendBeforeUnload();
  }

  function openNetworkSettings() {
    if (!serialNumber) {
      log.warn('Открытие сетевых настроек отменено: serial_number отсутствует');
      alert('Не выбран серийный номер камеры');
      return;
    }

    log.info('Переход к сетевым настройкам', { serialNumber });

    const query = new URLSearchParams({
      serial_number: serialNumber,
      open_network_settings: '1',
    });

    window.location.href = '/?' + query.toString();
  }

  const actions = {
    start: connectCamera,
    apply: applySettings,
    stop: stopCamera,
    force_stop: forceStopCamera,
    photo: openPhotoPopup,
    video: openVideoPopup,
    network_settings: openNetworkSettings,
  };

  for (const [name, button] of Object.entries(buttons)) {
    if (!button || !actions[name]) continue;
    button.addEventListener('click', actions[name]);
  }

  if (photoOnBtn) photoOnBtn.addEventListener('click', startPhotoSaving);
  if (photoOffBtn) photoOffBtn.addEventListener('click', stopPhotoSaving);
  if (videoOnBtn) videoOnBtn.addEventListener('click', startVideoSaving);
  if (videoOffBtn) videoOffBtn.addEventListener('click', stopVideoSaving);

  if (configInfoBtn) {
    configInfoBtn.addEventListener('click', (event) => {
      event.stopPropagation();
      openConfigPopup();
    });
  }
  if (configCard) {
    configCard.addEventListener('click', (event) => event.stopPropagation());
  }

  if (photoCard) {
    photoCard.addEventListener('click', (event) => event.stopPropagation());
  }

  if (videoCard) {
    videoCard.addEventListener('click', (event) => event.stopPropagation());
  }

  if (buttons.photo) {
    buttons.photo.addEventListener('click', (event) => event.stopPropagation());
  }

  if (buttons.video) {
    buttons.video.addEventListener('click', (event) => event.stopPropagation());
  }

  document.addEventListener('click', (event) => {
    const clickedInsideForm = event.target.closest('#settingsForm');
    const clickedSlider = event.target.closest('.input-slider-wrap');

    if (!clickedInsideForm && !clickedSlider) {
      removeActiveSlider();
    }
  });

  document.addEventListener('click', (event) => {
    const clickInsidePhoto = photoCard && photoCard.contains(event.target);
    const clickOnPhotoBtn = buttons.photo && buttons.photo.contains(event.target);

    const clickInsideVideo = videoCard && videoCard.contains(event.target);
    const clickOnVideoBtn = buttons.video && buttons.video.contains(event.target);

    if (!clickInsidePhoto && !clickOnPhotoBtn) {
      photoPopup.close();
    }

    if (!clickInsideVideo && !clickOnVideoBtn) {
      videoPopup.close();
    }

    const clickInsideConfig = configCard && configCard.contains(event.target);
    const clickOnConfigBtn = configInfoBtn && configInfoBtn.contains(event.target);
    if (!clickInsideConfig && !clickOnConfigBtn) {
      configPopup.close();
    }
  });

  window.addEventListener('resize', () => {
    if (photoPopup.isOpen()) photoPopup.open();
    if (videoPopup.isOpen()) videoPopup.open();
    if (configPopup.isOpen()) configPopup.open();
  });

  window.addEventListener(
    'scroll',
    () => {
      if (photoPopup.isOpen()) photoPopup.open();
      if (videoPopup.isOpen()) videoPopup.open();
      if (configPopup.isOpen()) configPopup.open();
    },
    true
  );

  const formFields = form.querySelectorAll('input, select');
  formFields.forEach((field) => {
    field.addEventListener('input', markDirty);
    field.addEventListener('change', markDirty);
  });

  cameraFrame.addEventListener('load', () => {
    log.success('Видеопоток успешно загружен');

    isLoading = false;
    isConnected = true;
    isChange = false;
    waitingSoftStop = false;
    isStoppingStream = false;
    streamErrorHandled = false;
    hideForceStopButton();
    clearForceStopTimer();

    showVideo();
    updateToolbarState();
    syncVideoPhotoStatus();
    startStatusPolling();
    syncMetrics();
    startMetricsPolling();
  });

  cameraFrame.addEventListener('error', () => {
  if (isStoppingStream || waitingSoftStop || streamErrorHandled || !cameraFrame.src) {
    return;
  }

  streamErrorHandled = true;
  log.error('Ошибка загрузки видеопотока');

  stopStatusPolling();
  resetCameraUI();
});

  window.addEventListener('beforeunload', handlePageLeave);
  window.addEventListener('pagehide', handlePageLeave);

  loadDataLimitToForm().then(() => {
    initFieldSliders();
  });

  showNoVideo();
  updateToolbarState();
  refreshModeUI();
}

window.addEventListener('DOMContentLoaded', initCameraPage);
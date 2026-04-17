// static/js/camera.js
function initCameraPage() {
  const form = document.getElementById('settingsForm');
  const serialElement = document.getElementById('cameraSerial');
  const cameraFrame = document.getElementById('cameraFrame');
  const cameraPlaceholder = document.getElementById('cameraPlaceholder');
  const applyIcon = document.getElementById('applyicon');

  const metricFps = document.getElementById('metricFps');
  const metricImageNumber = document.getElementById('metricImageNumber');
  const metricBandwidth = document.getElementById('metricBandwidth');
  const metricResolution = document.getElementById('metricResolution');
  const metricErrors = document.getElementById('metricErrors');
  const metricPacketsLost = document.getElementById('metricPacketsLost');

  const params = new URLSearchParams(window.location.search);
  const serialNumber = params.get('serial_number');

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
    return;
  }

  let isChange = false;
  let isConnected = false;
  let isLoading = false;
  let isSavePhoto = false;
  let isSaveVideo = false;
  let statusTimer = null;
  let activeSliderWrap = null;
  let isLeavingPage = false;
  let forceStopTimer = null;
  let waitingSoftStop = false;

  const photoPopup = UIHelpers.createPopupController(photoCard, buttons.photo);
  const videoPopup = UIHelpers.createPopupController(videoCard, buttons.video);

  serialElement.textContent = serialNumber ? serialNumber : 'не выбран';

  // =========================
  // UI STATE
  // =========================

  function refreshModeUI() {
    updateModeIndicators();
    updateSaveButtonsState();
  }

  function setSaveState({ photo = isSavePhoto, video = isSaveVideo } = {}) {
    isSavePhoto = photo;
    isSaveVideo = video;
    refreshModeUI();
  }

  function setApplyVisualState() {
    const applyBtn = buttons.apply;
    if (!applyBtn || !applyIcon) return;

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
      network_settings: { hidden: false, disabled: false },
    };

    if (!serialNumber) {
      state.start = { hidden: false, disabled: false };
      state.apply = { hidden: false, disabled: false };
      state.stop = { hidden: false, disabled: false };
      state.photo = { hidden: false, disabled: false };
      state.video = { hidden: false, disabled: false };
      applyState(state);
      return;
    }

    if (isLoading) {
      state.start = { hidden: false, disabled: true };
      state.network_settings = { hidden: true, disabled: true };
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
    isChange = true;
    updateToolbarState();
  }

  function resetCameraUI() {
    isConnected = false;
    isChange = false;
    isLoading = false;

    setSaveState({ photo: false, video: false });

    cameraFrame.src = '';
    showNoVideo();

    photoPopup.close();
    videoPopup.close();
    removeActiveSlider();

    updateToolbarState();
    stopMetricsPolling();
    resetMetricsUI();
  }

  let metricsTimer = null;

  function updateMetricsUI(data) {
    if (metricFps) metricFps.textContent = `${Number(data.fps ?? 0).toFixed(2)}fps`;
    if (metricImageNumber) metricImageNumber.textContent = data.image_number ?? 0;
    if (metricBandwidth) metricBandwidth.textContent = `${Number(data.bandwidth_mbps ?? 0).toFixed(1)}Mbps`;
    if (metricResolution) metricResolution.textContent = `${data.width ?? 0} x ${data.height ?? 0}`;
    if (metricErrors) metricErrors.textContent = data.errors ?? 0;
    if (metricPacketsLost) metricPacketsLost.textContent = data.packets_lost ?? 0;
  }

  function resetMetricsUI() {
    updateMetricsUI({
      fps: 0,
      image_number: 0,
      bandwidth_mbps: 0,
      width: 0,
      height: 0,
      errors: 0,
      packets_lost: 0,
    });
  }

  async function syncMetrics() {
    if (!isConnected) return;

    const data = await CameraApi.getMetrics();
    if (!data) return;

    updateMetricsUI(data);
  }

  function startMetricsPolling() {
    stopMetricsPolling();
    metricsTimer = setInterval(syncMetrics, 1000);
  }

  function stopMetricsPolling() {
    if (metricsTimer) {
      clearInterval(metricsTimer);
      metricsTimer = null;
    }
  }

  // =========================
  // FORM / SETTINGS
  // =========================

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

    const data = await CameraApi.getDataLimit(serialNumber);
    if (!data) return;

    // Подставляем только часть значений
    setFieldValue('width', data.width?.value);
    setFieldValue('height', data.height?.value);
    setFieldValue('exposure_time', data.exposure_time?.value);

    // Лимиты в input
    setFieldLimits('width', data.width);
    setFieldLimits('height', data.height);
    setFieldLimits('offset_x', data.offset_x);
    setFieldLimits('offset_y', data.offset_y);
    setFieldLimits('exposure_time', data.exposure_time);

    // Select
    fillSelectOptions('exposure_auto', data.exposure_auto);

    // min/max текст
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

    const fields = [
      ['width', 'width'],
      ['height', 'height'],
      ['offset_x', 'offset_x'],
      ['offset_y', 'offset_y'],
      ['fps', 'fps'],
      ['exposure_auto', 'exposure_auto'],
      ['exposure_time', 'exposure_time'],
    ];

    for (const [formName, queryName] of fields) {
      const value = formData.get(formName);
      if (value !== '') {
        query.set(queryName, value);
      }
    }

    return query;
  }

  // =========================
  // SLIDERS
  // =========================

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

  // =========================
  // STREAM
  // =========================

  function startStatusPolling() {
    stopStatusPolling();
    statusTimer = setInterval(syncVideoPhotoStatus, 1000);
  }

  function stopStatusPolling() {
    if (statusTimer) {
      clearInterval(statusTimer);
      statusTimer = null;
    }
  }

  async function syncVideoPhotoStatus() {
    if (!isConnected) return;

    const data = await CameraApi.getVideoPhotoStatus();
    if (!data) return;

    setSaveState({
      video: Number(data.video) === 1,
      photo: !!data.photo,
    });
  }

  function startStream() {
    const query = buildQueryFromForm();
    isLoading = true;
    updateToolbarState();
    cameraFrame.src = '/api/camera/stream?' + query.toString();
  }

  async function stopStreamOnly() {
    return await CameraApi.closeStream();
  }

  async function stopStreamForce() {
    return await CameraApi.closeStreamForce();
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
      const state = await CameraApi.getStreamState();

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
    if (!serialNumber) {
      alert('Камера не выбрана');
      return;
    }

    isConnected = false;
    isChange = false;
    startStream();
  }

  async function applySettings() {
    if (!serialNumber || !isConnected || !isChange) return;

    isConnected = false;
    isLoading = true;
    updateToolbarState();
    stopStatusPolling();

    await stopStreamOnly();

    cameraFrame.src = '';
    showNoVideo();
    startStream();
  }

  async function stopCamera() {
  stopStatusPolling();
  hideForceStopButton();
  clearForceStopTimer();

  waitingSoftStop = true;

  const result = await stopStreamOnly();
  if (!result) {
    waitingSoftStop = false;
    return;
  }

  forceStopTimer = setTimeout(() => {
    if (waitingSoftStop) {
      showForceStopButton();
    }
  }, 3000);

  const closed = await waitSoftStopResult();

  if (closed) {
    updateToolbarState();
  }
}
  async function forceStopCamera() {
    clearForceStopTimer();
    waitingSoftStop = false;

    await stopStreamForce();

    hideForceStopButton();
    resetCameraUI();
    updateToolbarState();
  }

  // =========================
  // PHOTO / VIDEO POPUPS
  // =========================

  function openPhotoPopup() {
    photoPopup.toggle();
  }

  function openVideoPopup() {
    videoPopup.toggle();
  }

  async function startPhotoSaving() {
    if (!isConnected) return;

    const interval = UIHelpers.getPositiveNumber(
      'input[name="photo_interval"]',
      'Не выбран интервал сохранения'
    );
    if (interval === null) return;

    const data = await CameraApi.startPhotoSaving(interval);
    if (!data) return;

    console.log('Ответ сервера:', data);
    await syncVideoPhotoStatus();
  }

  async function stopPhotoSaving() {
    const data = await CameraApi.stopPhotoSaving();
    if (!data) return;

    console.log('Ответ сервера:', data);
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

    const data = await CameraApi.startVideoSaving(durationInSeconds);
    if (!data) return;

    console.log('Ответ сервера:', data);
    await syncVideoPhotoStatus();
  }

  async function stopVideoSaving() {
    if (!isConnected) return;

    const data = await CameraApi.stopVideoSaving();
    if (!data) return;

    console.log('Ответ сервера:', data);
    await syncVideoPhotoStatus();
  }

  // =========================
  // PAGE LEAVE CLEANUP
  // =========================

  function cleanupCameraPage() {
    stopStatusPolling();
    resetCameraUI();
  }

  function notifyBackendBeforeUnload() {
    try {
      navigator.sendBeacon('/api/camera/close_stream');
    } catch (error) {
      fetch('/api/camera/close_stream', {
        method: 'GET',
        keepalive: true,
      }).catch(() => {});
    }
  }

  function handlePageLeave() {
    if (isLeavingPage) return;
    isLeavingPage = true;

    cleanupCameraPage();
    notifyBackendBeforeUnload();
  }

  function handleVisibilityChange() {
  if (document.visibilityState === 'hidden') {
    handlePageLeave();
  }
}

  // =========================
  // EVENTS
  // =========================

  function openNetworkSettings() {
    console.log('Открыть сетевые настройки');
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

  document.addEventListener('visibilitychange', handleVisibilityChange);

  for (const [name, button] of Object.entries(buttons)) {
    if (!button || !actions[name]) continue;
    button.addEventListener('click', actions[name]);
  }

  if (photoOnBtn) photoOnBtn.addEventListener('click', startPhotoSaving);
  if (photoOffBtn) photoOffBtn.addEventListener('click', stopPhotoSaving);
  if (videoOnBtn) videoOnBtn.addEventListener('click', startVideoSaving);
  if (videoOffBtn) videoOffBtn.addEventListener('click', stopVideoSaving);

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
  });

  window.addEventListener('resize', () => {
    if (photoPopup.isOpen()) photoPopup.open();
    if (videoPopup.isOpen()) videoPopup.open();
  });

  window.addEventListener(
    'scroll',
    () => {
      if (photoPopup.isOpen()) photoPopup.open();
      if (videoPopup.isOpen()) videoPopup.open();
    },
    true
  );

  const formFields = form.querySelectorAll('input, select');
  formFields.forEach((field) => {
    field.addEventListener('input', markDirty);
    field.addEventListener('change', markDirty);
  });

  cameraFrame.addEventListener('load', () => {
    isLoading = false;
    isConnected = true;
    isChange = false;
    waitingSoftStop = false;
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
    stopStatusPolling();
    resetCameraUI();
  });

  window.addEventListener('beforeunload', handlePageLeave);
  window.addEventListener('pagehide', handlePageLeave);

  // =========================
  // INIT
  // =========================

  loadDataLimitToForm().then(() => {
    initFieldSliders();
  });

  showNoVideo();
  updateToolbarState();
  refreshModeUI();
}

window.addEventListener('DOMContentLoaded', initCameraPage);
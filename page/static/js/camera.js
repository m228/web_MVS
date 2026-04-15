// static/js/camera .js
function initCameraPage() {
  const form = document.getElementById('settingsForm');
  const serialElement = document.getElementById('cameraSerial');
  const cameraFrame = document.getElementById('cameraFrame');
  const cameraPlaceholder = document.getElementById('cameraPlaceholder');
  const applyIcon = document.getElementById('applyicon');

  const params = new URLSearchParams(window.location.search);
  const serialNumber = params.get('serial_number');

  const buttons = {
    start: document.getElementById('startBtn'),
    apply: document.getElementById('applyBtn'),
    stop: document.getElementById('stopBtn'),
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

  serialElement.textContent = serialNumber ? serialNumber : 'не выбран';

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

  function showNoVideo() {
    cameraFrame.classList.remove('visible');
    cameraPlaceholder.classList.remove('hidden');
  }

  function showVideo() {
    cameraFrame.classList.add('visible');
    cameraPlaceholder.classList.add('hidden');
  }

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

  const photoPopup = UIHelpers.createPopupController(photoCard, buttons.photo);
  const videoPopup = UIHelpers.createPopupController(videoCard, buttons.video);

  function resetCameraUI() {
    isConnected = false;
    isChange = false;
    isLoading = false;

    setSaveState({ photo: false, video: false });

    cameraFrame.src = '';
    showNoVideo();

    photoPopup.close();
    videoPopup.close();

    updateToolbarState();
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

    const result = await stopStreamOnly();

    resetCameraUI();

    if (result?.status === 'stopped') {
      await new Promise((resolve) => setTimeout(resolve, 300));
      updateToolbarState();
    }
  }

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

  function openNetworkSettings() {
    console.log('Открыть сетевые настройки');
  }

  function markDirty() {
    if (!isConnected) return;
    isChange = true;
    updateToolbarState();
  }

  const actions = {
    start: connectCamera,
    apply: applySettings,
    stop: stopCamera,
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

    showVideo();
    updateToolbarState();
    syncVideoPhotoStatus();
    startStatusPolling();
  });

  cameraFrame.addEventListener('error', () => {
    stopStatusPolling();
    resetCameraUI();
  });

  showNoVideo();
  updateToolbarState();
  refreshModeUI();
}

window.addEventListener('DOMContentLoaded', initCameraPage);
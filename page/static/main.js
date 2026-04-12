// page index
async function apiGet(url, errorText = 'Ошибка запроса') {
  try {
    const response = await fetch(url, { cache: 'no-store' });
    return await response.json();
  } catch (error) {
    console.error(errorText, error);
    return null;
  }
}

async function loadStatus() {
  const data = await apiGet('/api/status', 'Ошибка получения статуса:');
  if (!data) return;

  const el = document.getElementById('status');
  if (!el) return;

  el.innerHTML = data.status
    ? '<span style="color: #16a34a; font-size: 20px;">✔</span>'
    : '<span style="color: #dc2626; font-size: 20px;">✖</span>';
}

async function count_cams() {
  const data = await apiGet('/api/count_cams', 'count_cams error:');
  if (!data) return;

  const el = document.getElementById('count_cams');
  if (el) {
    el.textContent = data.count;
  }
}

function updateTime() {
  const now = new Date();
  const timeString = now.toLocaleTimeString();
  const el = document.getElementById('time');
  if (el) {
    el.textContent = timeString;
  }
}

window.addEventListener('DOMContentLoaded', () => {
  count_cams();
  updateTime();
  setInterval(updateTime, 1000);
});

async function getIp(serial) {
  const data = await apiGet(
    `/api/ip?serial_number=${encodeURIComponent(serial)}`,
    'Ошибка получения IP:'
  );
  return data?.ip ?? 'Ошибка';
}

function openCamera(serial) {
  window.location.href = '/camera?serial_number=' + encodeURIComponent(serial);
}

async function loadCams() {
  const data = await apiGet('/api/cams', 'Ошибка получения списка камер:');
  if (!data) return;

  const table = document.getElementById('table');
  if (!table) return;

  table.innerHTML = '';

  for (const serial in data) {
    const ip = await getIp(serial);

    table.innerHTML += `
      <tr>
        <td>${serial}</td>
        <td>${data[serial]}</td>
        <td>${ip}</td>
        <td>
          <div class="table-actions">
            <button type="button" class="toolbar-btn" onclick="openCamera('${serial}')">
              <img src="/static/icon/connect.png" alt="Подключиться" class="toolbar-img">
            </button>
            <button type="button" class="toolbar-btn" onclick="alert('Сетевые настройки: ${serial}')">
              <img src="/static/icon/network-settings.png" alt="Сетевые настройки" class="toolbar-img">
            </button>
          </div>
        </td>
      </tr>
    `;
  }
}

// page camera
const form = document.getElementById('settingsForm');
const serialElement = document.getElementById('cameraSerial');
const cameraFrame = document.getElementById('cameraFrame');
const cameraPlaceholder = document.getElementById('cameraPlaceholder');
const applyIcon = document.getElementById('applyicon');

const params = new URLSearchParams(window.location.search);
const serialNumber = params.get('serial_number');

let isChange = false;
let isConnected = false;
let isLoading = false;

let isSavePhoto = false;
let isSaveVideo = false;
let statusTimer = null;

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
  form &&
  serialElement &&
  cameraFrame &&
  cameraPlaceholder &&
  buttons.start &&
  buttons.apply &&
  buttons.stop
) {
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

  function getPositiveNumber(selector, message) {
    const input = document.querySelector(selector);
    const rawValue = input ? input.value.trim() : '';

    if (!rawValue) {
      alert(message);
      return null;
    }

    const value = Number(rawValue);

    if (Number.isNaN(value) || value <= 0) {
      alert(message);
      return null;
    }

    return value;
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

    const data = await apiGet(
      '/api/camera/status_video_photo',
      'Ошибка получения статуса video/photo:'
    );
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
    return await apiGet('/api/camera/close_stream', 'Ошибка остановки потока:');
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

  function createPopupController(card, button) {
    function isOpen() {
      return !!(card && card.classList.contains('show'));
    }

    function close() {
      if (!card) return;
      card.classList.remove('show');
    }

    function open() {
      if (!card || !button) return;

      const rect = button.getBoundingClientRect();

      card.classList.add('show');
      card.style.left = '0px';
      card.style.top = '0px';

      const cardWidth = card.offsetWidth;
      const cardHeight = card.offsetHeight;

      let left = rect.left;
      let top = rect.bottom + 8;

      if (left + cardWidth > window.innerWidth - 10) {
        left = window.innerWidth - cardWidth - 10;
      }

      if (top + cardHeight > window.innerHeight - 10) {
        top = rect.top - cardHeight - 8;
      }

      if (left < 10) left = 10;
      if (top < 10) top = 10;

      card.style.left = `${left}px`;
      card.style.top = `${top}px`;
    }

    function toggle() {
      isOpen() ? close() : open();
    }

    return { isOpen, open, close, toggle };
  }

  const photoPopup = createPopupController(photoCard, buttons.photo);
  const videoPopup = createPopupController(videoCard, buttons.video);

  async function stopCamera() {
    stopStatusPolling();

    const result = await stopStreamOnly();

    resetCameraUI();

    if (result?.status === 'stopped') {
      await new Promise((r) => setTimeout(r, 300));
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

    const interval = getPositiveNumber(
      'input[name="photo_interval"]',
      'Не выбран интервал сохранения'
    );
    if (interval === null) return;

    const data = await apiGet(
      `/api/camera/on_save_photo?interval=${encodeURIComponent(interval)}`,
      'Ошибка запуска сохранения фото:'
    );
    if (!data) return;

    console.log('Ответ сервера:', data);
    await syncVideoPhotoStatus();
  }

  async function stopPhotoSaving() {
    const data = await apiGet(
      '/api/camera/off_save_photo',
      'Ошибка остановки сохранения фото:'
    );
    if (!data) return;

    console.log('Ответ сервера:', data);
    await syncVideoPhotoStatus();
  }

  async function startVideoSaving() {
    if (!isConnected) return;

    const duration = getPositiveNumber(
      'input[name="video_duration"]',
      'Не выбрана длительность записи'
    );
    if (duration === null) return;

    const unitSelect = document.querySelector('select[name="video_duration_unit"]');
    const unit = unitSelect ? unitSelect.value : 'minutes';
    const durationInSeconds = unit === 'minutes' ? duration * 60 : duration;

    const data = await apiGet(
      `/api/camera/on_save_video?duration=${encodeURIComponent(durationInSeconds)}`,
      'Ошибка запуска записи видео:'
    );
    if (!data) return;

    console.log('Ответ сервера:', data);
    await syncVideoPhotoStatus();
  }

  async function stopVideoSaving() {
    if (!isConnected) return;

    const data = await apiGet(
      '/api/camera/off_save_video',
      'Ошибка остановки записи видео:'
    );
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
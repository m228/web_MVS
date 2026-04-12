// page index
async function loadStatus() {
  const response = await fetch('/api/status');
  const data = await response.json();

  const el = document.getElementById('status');
  if (el) {
    if (data.status) {
      el.innerHTML = '<span style="color: #16a34a; font-size: 20px;">✔</span>';
    } else {
      el.innerHTML = '<span style="color: #dc2626; font-size: 20px;">✖</span>';
    }
  }
}

async function count_cams() {
  try {
    const response = await fetch('/api/count_cams');
    const data = await response.json();
    const el = document.getElementById('count_cams');
    if (el) {
      el.textContent = data.count;
    }
  } catch (error) {
    console.error('count_cams error:', error);
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
  try {
    const response = await fetch(`/api/ip?serial_number=${encodeURIComponent(serial)}`);
    const data = await response.json();
    return data.ip ?? 'Нет IP';
  } catch (error) {
    return 'Ошибка';
  }
}

function openCamera(serial) {
  window.location.href = '/camera?serial_number=' + encodeURIComponent(serial);
}

async function loadCams() {
  const response = await fetch('/api/cams');
  const data = await response.json();

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


const videoCard =  document.getElementById('videoCard');
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

  function setApplyVisualState() {
    const applyBtn = buttons.apply;
    if (!applyBtn || !applyIcon) return;

    if (!isConnected || !isChange) {
      applyBtn.disabled = true;
      applyBtn.classList.add('grey-btn');
    } else {
      applyBtn.disabled = false;
      applyBtn.classList.remove('grey-btn');
    }
  }

  function applyState(state) {
    for (const [name, config] of Object.entries(state)) {
      const button = buttons[name];
      if (!button) continue;

      const hidden = config.hidden ?? false;
      const disabled = config.disabled ?? false;

      button.classList.toggle('hidden', hidden);
      button.disabled = disabled;
    }

    setApplyVisualState();
  }

  function updateToolbarState() {
    if (!serialNumber) {
      applyState({ // отладочный режим
        start: { hidden: false, disabled: false }, // false true
        apply: { hidden: false, disabled: false }, // true true
        stop: { hidden: false, disabled: false }, // true true
        photo: { hidden: false, disabled: false }, // true true
        video: { hidden: false, disabled: false }, // true true
        network_settings: { hidden: false, disabled: false },// true true
      });
      return;
    }

    if (isLoading) {
      applyState({
        start: { hidden: false, disabled: true },
        apply: { hidden: true, disabled: true },
        stop: { hidden: true, disabled: true },
        photo: { hidden: true, disabled: true },
        video: { hidden: true, disabled: true },
        network_settings: { hidden: true, disabled: true },
      });
      return;
    }

    if (!isConnected) {
      applyState({
        start: { hidden: false, disabled: false },
        apply: { hidden: true, disabled: true },
        stop: { hidden: true, disabled: true },
        photo: { hidden: true, disabled: true },
        video: { hidden: true, disabled: true },
        network_settings: { hidden: false, disabled: false },
      });
      return;
    }

    applyState({
      start: { hidden: true, disabled: true },
      apply: { hidden: false, disabled: !isChange },
      stop: { hidden: false, disabled: false },
      photo: { hidden: false, disabled: false },
      video: { hidden: false, disabled: false },
      network_settings: { hidden: false, disabled: false },
    });
  }

  function buildQueryFromForm() {
    const formData = new FormData(form);
    const query = new URLSearchParams();

    query.set('serial_number', serialNumber);

    const width = formData.get('width');
    const height = formData.get('height');
    const offsetX = formData.get('offset_x');
    const offsetY = formData.get('offset_y');
    const fps = formData.get('fps');
    const exposureAuto = formData.get('exposure_auto');
    const exposureTime = formData.get('exposure_time');

    if (width !== '') query.set('width', width);
    if (height !== '') query.set('height', height);
    if (offsetX !== '') query.set('offset_x', offsetX);
    if (offsetY !== '') query.set('offset_y', offsetY);
    if (fps !== '') query.set('fps', fps);
    if (exposureAuto !== '') query.set('exposure_auto', exposureAuto);
    if (exposureTime !== '') query.set('exposure_time', exposureTime);

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

  function startStream() {
    const query = buildQueryFromForm();
    isLoading = true;
    updateToolbarState();

    cameraFrame.src = '/api/camera/stream?' + query.toString();
  }

  async function stopStreamOnly() {
  try {
    const response = await fetch('/api/camera/close_stream');
    const data = await response.json();
    return data;
  } catch (error) {
    console.error('Ошибка остановки потока:', error);
    return null;
  }
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
      if (!card) return;
      if (isOpen()) {
        close();
      } else {
        open();
      }
    }

    return { isOpen, open, close, toggle };
  }

  const photoPopup = createPopupController(photoCard, buttons.photo);
  const videoPopup = createPopupController(videoCard, buttons.video);

  async function stopCamera() {
    isLoading = false;

    const result = await stopStreamOnly();
    cameraFrame.src = '';
    showNoVideo();

    if (result?.status === 'stopped') {
      await new Promise(r => setTimeout(r, 300));
      updateToolbarState();
    }

    isConnected = false;
    isChange = false;
    isSavePhoto = false;
    isSaveVideo = false;

    photoPopup.close();
    videoPopup.close();
    updateToolbarState();
    updateModeIndicators();
    updateSaveButtonsState();
  }

  function openPhotoPopup() {
    photoPopup.toggle();
  }

  function openVideoPopup() {
    videoPopup.toggle();
  }

  async function startPhotoSaving() {
  if (!isConnected) return;

  try {
    const intervalInput = document.querySelector('input[name="photo_interval"]');
    const rawInterval = intervalInput ? intervalInput.value.trim() : '';

    if (!rawInterval) {
      alert('Не выбран интервал сохранения');
      return;
    }

    const interval = Number(rawInterval);

    if (Number.isNaN(interval) || interval <= 0) {
      alert('Не выбран интервал сохранения');
      return;
    }

    const response = await fetch(`/api/camera/on_save_photo?interval=${encodeURIComponent(interval)}`);
    const data = await response.json();

    console.log('Ответ сервера:', data);
    isSavePhoto = true;
    updateModeIndicators();
    updateSaveButtonsState();
  } catch (error) {
    console.error('Ошибка запуска сохранения фото:', error);
  }
}
  async function stopPhotoSaving() {
  try {
    const response = await fetch('/api/camera/off_save_photo');
    const data = await response.json();

    console.log('Ответ сервера:', data);
    isSavePhoto = false;
    updateModeIndicators();
    updateSaveButtonsState();
  } catch (error) {
    console.error('Ошибка остановки сохранения фото:', error);
  }
}

  async function startVideoSaving() {
  if (!isConnected) return;

  try {
    const durationInput = document.querySelector('input[name="video_duration"]');
    const unitSelect = document.querySelector('select[name="video_duration_unit"]');

    const rawDuration = durationInput ? durationInput.value.trim() : '';
    const unit = unitSelect ? unitSelect.value : 'minutes';

    if (!rawDuration) {
      alert('Не выбрана длительность записи');
      return;
    }

    const duration = Number(rawDuration);

    if (Number.isNaN(duration) || duration <= 0) {
      alert('Не выбрана длительность записи');
      return;
    }

    let durationInSeconds = duration;

    if (unit === 'minutes') {
      durationInSeconds = duration * 60;
    }

    const response = await fetch(
      `/api/camera/on_save_video?duration=${encodeURIComponent(durationInSeconds)}`
    );

    const data = await response.json();
    console.log('Ответ сервера:', data);

    isSaveVideo = true;
    updateModeIndicators();
    updateSaveButtonsState();
  } catch (error) {
    console.error('Ошибка запуска записи видео:', error);
  }
}

  async function stopVideoSaving() {
  if (!isConnected) return;

  try {
    const response = await fetch('/api/camera/off_save_video');
    const data = await response.json();

    console.log('Ответ сервера:', data);
    isSaveVideo = false;
    updateModeIndicators();
    updateSaveButtonsState();
  } catch (error) {
    console.error('Ошибка остановки записи видео:', error);
  }
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
  if (photoOnBtn) {
    photoOnBtn.disabled = isSavePhoto;
  }

  if (photoOffBtn) {
    photoOffBtn.disabled = !isSavePhoto;
  }

  if (videoOnBtn) {
    videoOnBtn.disabled = isSaveVideo;
  }

  if (videoOffBtn) {
    videoOffBtn.disabled = !isSaveVideo;
  }
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
    if (!button) continue;
    if (!actions[name]) continue;
    button.addEventListener('click', actions[name]);
  }

  if (photoOnBtn) {
    photoOnBtn.addEventListener('click', startPhotoSaving);
  }

  if (photoOffBtn) {
    photoOffBtn.addEventListener('click', stopPhotoSaving);
  }

  if (videoOnBtn) {
    videoOnBtn.addEventListener('click', startVideoSaving);
  }

  if (videoOffBtn) {
    videoOffBtn.addEventListener('click', stopVideoSaving);
  }

  if (photoCard) {
    photoCard.addEventListener('click', (event) => {
      event.stopPropagation();
    });
  }

  if (videoCard) {
    videoCard.addEventListener('click', (event) => {
      event.stopPropagation();
    });
  }

  if (buttons.photo) {
    buttons.photo.addEventListener('click', (event) => {
      event.stopPropagation();
    });
  }

  if (buttons.video) {
    buttons.video.addEventListener('click', (event) => {
      event.stopPropagation();
    });
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

  const fields = form.querySelectorAll('input, select');

  fields.forEach((field) => {
    field.addEventListener('input', markDirty);
    field.addEventListener('change', markDirty);
  });

  cameraFrame.addEventListener('load', () => {
    isLoading = false;
    isConnected = true;
    isChange = false;

    showVideo();
    updateToolbarState();
  });

  cameraFrame.addEventListener('error', () => {
    isLoading = false;
    isConnected = false;
    isChange = false;
    isSavePhoto = false;
    isSaveVideo = false;
    updateModeIndicators();
    updateSaveButtonsState();

    cameraFrame.src = '';
    showNoVideo();
    photoPopup.close();
    videoPopup.close();
    updateToolbarState();
    updateSaveButtonsState();
  });

  showNoVideo();
  updateToolbarState();
  updateModeIndicators();
  updateSaveButtonsState();
}
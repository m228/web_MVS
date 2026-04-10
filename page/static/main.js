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
  const response = await fetch('/api/count_cams');
  const data = await response.json();
  const el = document.getElementById('count_cams');
  if (el) {
    el.textContent = data.count;
  }
}

function updateTime() {
  const now = new Date();
  const timeString = now.toLocaleTimeString(); // например: 14:23:05
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
              <button type="button" class="toolbar-btn" onclick="openCamera('${serial}')"><img src="/static/icon/connect.png" alt="Подключиться" class="toolbar-img"></button>
              <button type="button" class="toolbar-btn" onclick="alert('Сетевые настройки: ${serial}')"><img src="/static/icon/network-settings.png" alt="Сетевые настройки" class="toolbar-img"></button>
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

const startBtn = document.getElementById('startStream');
const applyBtn = document.getElementById('applyBtn');
const stopBtn = document.getElementById('stopStream');
const applyIcon = document.getElementById('applyicon');

const params = new URLSearchParams(window.location.search);
const serialNumber = params.get('serial_number');

let isChange = false;
let isConnected = false;
let isLoading = false;

if (
  form &&
  serialElement &&
  cameraFrame &&
  cameraPlaceholder &&
  startBtn &&
  applyBtn &&
  stopBtn
) {
  serialElement.textContent = serialNumber ? serialNumber : 'не выбран';

  function setApplyVisualState() {
  if (!applyBtn || !applyIcon) return;

  if (!isConnected || !isChange) {
    applyBtn.disabled = true;
    applyIcon.src = '/static/icon/submit-gray.png';
  } else {
    applyBtn.disabled = false;
    applyIcon.src = '/static/icon/submit-green.png';
  }
}

  function updateToolbarState() {
    if (!serialNumber) {
      startBtn.classList.remove('hidden');
      applyBtn.classList.add('hidden');
      stopBtn.classList.add('hidden');

      startBtn.disabled = true;
      applyBtn.disabled = true;
      stopBtn.disabled = true;

      setApplyVisualState();
      return;
    }

    if (isLoading) {
      startBtn.classList.remove('hidden');
      applyBtn.classList.add('hidden');
      stopBtn.classList.add('hidden');

      startBtn.disabled = true;
      applyBtn.disabled = true;
      stopBtn.disabled = true;

      setApplyVisualState();
      return;
    }

    if (!isConnected) {
      startBtn.classList.remove('hidden');
      applyBtn.classList.add('hidden');
      stopBtn.classList.add('hidden');

      startBtn.disabled = false;
      applyBtn.disabled = true;
      stopBtn.disabled = true;

      setApplyVisualState();
      return;
    }

    startBtn.classList.add('hidden');
    applyBtn.classList.remove('hidden');
    stopBtn.classList.remove('hidden');

    applyBtn.disabled = !isChange;
    stopBtn.disabled = false;

    setApplyVisualState();
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
      await fetch('/api/camera/close_stream');
    } catch (error) {
      console.error('Ошибка остановки потока:', error);
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

  async function stopCamera() {
    isLoading = false;

    await stopStreamOnly();

    cameraFrame.src = '';
    showNoVideo();

    isConnected = false;
    isChange = false;

    updateToolbarState();
  }

  function markDirty() {
    if (!isConnected) return;

    isChange = true;
    updateToolbarState();
  }

  const fields = form.querySelectorAll('input, select');

  fields.forEach((field) => {
    field.addEventListener('input', markDirty);
    field.addEventListener('change', markDirty);
  });

  startBtn.addEventListener('click', connectCamera);
  applyBtn.addEventListener('click', applySettings);
  stopBtn.addEventListener('click', stopCamera);

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

    cameraFrame.src = '';
    showNoVideo();
    updateToolbarState();
  });

  showNoVideo();
  updateToolbarState();
}

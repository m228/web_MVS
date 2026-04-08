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

// page cams
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
            <button onclick="openCamera('${serial}')" title="Подключиться"><img src="/static/icon/connect.png" alt="Подключиться" class="toolbar-img"></button>
            <button onclick="alert('Сетевые настройки: ${serial}')" title="Подключиться"><span>⚙️</span></button>
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

const StartBtn = document.getElementById('startStream');
const applyBtn = document.getElementById('applyBtn');
const stopBtn = document.getElementById('stopStream');

const params = new URLSearchParams(window.location.search);
const serialNumber = params.get('serial_number');

let isChange = false;
let isConnected = false;

if (serialElement) {
  serialElement.textContent = serialNumber ? serialNumber : 'не выбран';
}

function updateToolbarState() {
  if (!serialNumber) {
    StartBtn.disabled = true;
    applyBtn.disabled = true;
    stopBtn.disabled = true;
    return;
  }

  if (!isConnected) {
    StartBtn.classList.remove('hidden');
    applyBtn.classList.add('hidden');
    stopBtn.classList.add('hidden');

    StartBtn.disabled = false;
    applyBtn.disabled = true;
    stopBtn.disabled = true;
    return;
  }

  StartBtn.classList.add('hidden');
  applyBtn.classList.remove('hidden');
  stopBtn.classList.remove('hidden');

  applyBtn.disabled = !isChange;
  stopBtn.disabled = false;
}

function buildQueryFromForm() {
  const formData = new FormData(form);

  return new URLSearchParams({
    serial_number: serialNumber,
    width: formData.get('width') || '',
    height: formData.get('height') || '',
    offset_x: formData.get('offset_x') || '',
    offset_y: formData.get('offset_y') || '',
    fps: formData.get('fps') || '',
    exposure_auto: formData.get('exposure_auto') || '',
    exposure_time: formData.get('exposure_time') || ''
  });
}

function startStream() {
  const query = buildQueryFromForm();
  cameraFrame.src = '/api/camera/stream?' + query.toString();
  cameraFrame.classList.add('visible');
  if (cameraPlaceholder) {
    cameraPlaceholder.classList.add('hidden');
  }
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

  startStream();
  isConnected = true;
  isChange = false;
  updateToolbarState();
}

async function applySettings() {
  if (!serialNumber || !isConnected || !isChange) return;

  await stopStreamOnly();
  cameraFrame.src = '';
  startStream();

  isChange = false;
  updateToolbarState();
}

async function stopCamera() {
  await stopStreamOnly();

  if (cameraFrame) {
    cameraFrame.src = '';
    cameraFrame.classList.remove('visible');
  }
  if (cameraPlaceholder) {
    cameraPlaceholder.classList.remove('hidden');
  }


  isConnected = false;
  isChange = false;
  updateToolbarState();
}

function markDirty() {
  if (!isConnected) return;
  isChange = true;
  updateToolbarState();
}

if (form) {
  const fields = form.querySelectorAll('input, select');

  fields.forEach(field => {
    field.addEventListener('input', markDirty);
    field.addEventListener('change', markDirty);
  });
}

if (StartBtn) {
  StartBtn.addEventListener('click', connectCamera);
}

if (applyBtn) {
  applyBtn.addEventListener('click', applySettings);
}

if (stopBtn) {
  stopBtn.addEventListener('click', stopCamera);
}

updateToolbarState();

// if (form && serialElement && cameraFrame && StartBtn && applyBtn && stopBtn) {
//   // весь код page camera здесь
// }

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
            <button onclick="openCamera('${serial}')" title="Подключиться"><span>🔌</span></button>
            <button onclick="alert('Сетевые настройки: ${serial}')" title="Подключиться"><span>⚙️</span></button>
        </td>
      </tr>
    `;
  }
}




// page camera
const form = document.getElementById('settingsForm');
const serialElement = document.getElementById('cameraSerial');

const params = new URLSearchParams(window.location.search);
const serialNumber = params.get('serial_number');

if (serialElement) {
  serialElement.innerText = serialNumber ? serialNumber : 'не выбран';
}

if (form) {
  const fields = form.querySelectorAll('input, select, button');

  if (!serialNumber) {
    fields.forEach(field => {
      field.disabled = true;
    });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();

    if (!serialNumber) {
      alert('Камера не выбрана');
      return;
    }

    const formData = new FormData(form);

    const query = new URLSearchParams({
      serial_number: serialNumber,
      width: formData.get('width') || '',
      height: formData.get('height') || '',
      offset_x: formData.get('offset_x') || '',
      offset_y: formData.get('offset_y') || '',
      fps: formData.get('fps') || '',
      exposure_auto: formData.get('exposure_auto') || '',
      exposure_time: formData.get('exposure_time') || ''
    });

    const img = document.getElementById('cameraFrame');

    if (img) {
      img.src = '/api/camera/stream?' + query.toString();
    }
  });
}
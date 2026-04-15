// static/js/index.js
function updateTime() {
  const now = new Date();
  const timeString = now.toLocaleTimeString();
  const el = document.getElementById('time');
  if (el) {
    el.textContent = timeString;
  }
}

function openCamera(serial) {
  window.location.href = '/camera?serial_number=' + encodeURIComponent(serial);
}

async function loadStatus() {
  const data = await CameraApi.getStatus();
  if (!data) return;

  const el = document.getElementById('status');
  if (!el) return;

  el.innerHTML = data.status
    ? '<span style="color: #16a34a; font-size: 20px;">✔</span>'
    : '<span style="color: #dc2626; font-size: 20px;">✖</span>';
}

async function countCams() {
  const data = await CameraApi.getCountCams();
  if (!data) return;

  const el = document.getElementById('count_cams');
  if (el) {
    el.textContent = data.count;
  }
}

async function loadCams() {
  const data = await CameraApi.getCams();
  if (!data) return;

  const table = document.getElementById('table');
  if (!table) return;

  table.innerHTML = '';

  for (const serial in data) {
    const ipData = await CameraApi.getIp(serial);
    const ip = ipData?.ip ?? 'Ошибка';

    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${serial}</td>
      <td>${data[serial]}</td>
      <td>${ip}</td>
      <td>
        <div class="table-actions">
          <button type="button" class="toolbar-btn" data-open-camera>
            <img src="/static/icon/connect.png" alt="Подключиться" class="toolbar-img">
          </button>
          <button type="button" class="toolbar-btn" data-network-settings>
            <img src="/static/icon/network-settings.png" alt="Сетевые настройки" class="toolbar-img">
          </button>
        </div>
      </td>
    `;

    const openBtn = row.querySelector('[data-open-camera]');
    const networkBtn = row.querySelector('[data-network-settings]');

    if (openBtn) {
      openBtn.addEventListener('click', () => {
        openCamera(serial);
      });
    }

    if (networkBtn) {
      networkBtn.addEventListener('click', () => {
        alert(`Сетевые настройки: ${serial}`);
      });
    }

    table.appendChild(row);
  }
}

function initIndexPage() {
  const table = document.getElementById('table');
  const timeEl = document.getElementById('time');
  const statusEl = document.getElementById('status');
  const countEl = document.getElementById('count_cams');
  const refreshBtn = document.getElementById('refreshCamsBtn');

  if (!table && !timeEl && !statusEl && !countEl) return;

  refreshCameras();

  if (refreshBtn) {
    refreshBtn.addEventListener('click', refreshCameras);
  }

  if (timeEl) {
    updateTime();
    setInterval(updateTime, 1000);
  }
}

async function refreshCameras() {
  await loadStatus();
  await countCams();
  await loadCams();
}

window.addEventListener('DOMContentLoaded', initIndexPage);
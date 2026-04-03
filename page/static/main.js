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

async function getIp(serial) {
  try {
    const response = await fetch(`/api/ip?serial_number=${encodeURIComponent(serial)}`);
    const data = await response.json();
    return data.ip ?? 'Нет IP';
  } catch (error) {
    return 'Ошибка';
  }
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
      </tr>
    `;
  }
}
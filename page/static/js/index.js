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

const ACCESS_STATUS_MAP = {
  0: 'Неизвестно',
  1: 'Ok',
  2: 'Только чтение',
  3: 'Нет доступа',
  4: 'Занята',
  5: 'OpenReadWrite',
  6: 'OpenReadOnly',
};

function getAccessStatusText(statusCode) {
  const code = Number(statusCode);
  return ACCESS_STATUS_MAP[code] ?? `Неизвестный статус (${statusCode})`;
}

function getAccessStatusVariant(statusCode) {
  const code = Number(statusCode);

  switch (code) {
    case 1:
      return 'status-badge--ok';
    case 2:
    case 6:
      return 'status-badge--warning';
    case 4:
      return 'status-badge--busy';
    case 5:
      return 'status-badge--info';
    default:
      return 'status-badge--danger';
  }
}

function canOpenCamera(statusCode) {
  const code = Number(statusCode);
  return code === 1;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (symbol) => {
    switch (symbol) {
      case '&':
        return '&amp;';
      case '<':
        return '&lt;';
      case '>':
        return '&gt;';
      case '"':
        return '&quot;';
      case "'":
        return '&#39;';
      default:
        return symbol;
    }
  });
}

function getDriverStatusHtml(isOnline) {
  return isOnline
    ? '<span class="status-chip status-chip--ok">Драйвер активен</span>'
    : '<span class="status-chip status-chip--error">Драйвер недоступен</span>';
}

function getAccessStatusHtml(statusCode) {
  const safeStatusText = escapeHtml(getAccessStatusText(statusCode));
  const safeStatusCode = escapeHtml(statusCode);

  return `
    <div class="status-cell">
      <span class="status-badge ${getAccessStatusVariant(statusCode)}">${safeStatusText}</span>
      <span class="status-code">Код доступа: ${safeStatusCode}</span>
    </div>
  `;
}

function getNetworkModalElements() {
  return {
    modal: document.getElementById('networkSettingsModal'),
    serial: document.getElementById('networkSettingsSerial'),
    closeBtn: document.getElementById('networkSettingsCloseBtn'),
    cancelBtn: document.getElementById('networkSettingsCancelBtn'),
    applyBtn: document.getElementById('networkSettingsApplyBtn'),
    loader: document.getElementById('networkSettingsLoader'),
    error: document.getElementById('networkSettingsError'),
    dhcpBadge: document.getElementById('networkDhcpBadge'),
    advancedToggle: document.getElementById('networkAdvancedToggle'),
    ipGroup: document.getElementById('networkIpGroup'),
    maskGroup: document.getElementById('networkMaskGroup'),
    gatewayGroup: document.getElementById('networkGatewayGroup'),
    maskRow: document.getElementById('networkMaskRow'),
    gatewayRow: document.getElementById('networkGatewayRow'),
    riskModal: document.getElementById('networkRiskModal'),
    riskAcceptBtn: document.getElementById('networkRiskAcceptBtn'),
    riskCancelBtn: document.getElementById('networkRiskCancelBtn'),
    riskCloseBtn: document.getElementById('networkRiskCloseBtn'),
  };
}

const networkSettingsState = {
  serial: null,
  advancedEnabled: false,
};

function isDhcpEnabled(value) {
  if (value === true || value === 1 || value === '1') return true;
  const normalized = String(value ?? '').trim().toLowerCase();
  return ['true', 'on', 'yes', 'enabled', 'enable'].includes(normalized);
}

function getIpv4Inputs(group) {
  if (!group) return [];
  return Array.from(group.querySelectorAll('.ip-octet'));
}

function splitIpv4(value) {
  const parts = String(value ?? '').split('.').slice(0, 4);

  while (parts.length < 4) {
    parts.push('');
  }

  return parts.map((part) => part.replace(/\D/g, '').slice(0, 3));
}

function setIpv4Value(group, value) {
  const inputs = getIpv4Inputs(group);
  const parts = splitIpv4(value);

  inputs.forEach((input, index) => {
    input.value = parts[index] ?? '';
  });
}

function readIpv4Value(group, fieldLabel) {
  const inputs = getIpv4Inputs(group);
  const parts = inputs.map((input) => input.value.trim());

  if (parts.some((part) => part === '')) {
    throw new Error(`Поле «${fieldLabel}» заполнено не полностью`);
  }

  for (const part of parts) {
    const number = Number(part);

    if (!Number.isInteger(number) || number < 0 || number > 255) {
      throw new Error(`Поле «${fieldLabel}» содержит неверное значение`);
    }
  }

  return parts.join('.');
}

function initIpOctetInputs(root = document) {
  const inputs = root.querySelectorAll('.ip-octet');

  inputs.forEach((input) => {
    if (input.dataset.ready === '1') return;
    input.dataset.ready = '1';

    input.addEventListener('input', () => {
      input.value = input.value.replace(/\D/g, '').slice(0, 3);

      if (input.value.length === 3) {
        const next = input.nextElementSibling?.nextElementSibling;
        if (next && next.classList.contains('ip-octet') && !next.disabled) {
          next.focus();
          next.select();
        }
      }
    });

    input.addEventListener('keydown', (event) => {
      if (event.key === '.' || event.key === ',') {
        event.preventDefault();
        const next = input.nextElementSibling?.nextElementSibling;
        if (next && next.classList.contains('ip-octet') && !next.disabled) {
          next.focus();
          next.select();
        }
      }

      if (event.key === 'Backspace' && !input.value) {
        const previous = input.previousElementSibling?.previousElementSibling;
        if (previous && previous.classList.contains('ip-octet') && !previous.disabled) {
          previous.focus();
        }
      }
    });

    input.addEventListener('paste', (event) => {
      const text = event.clipboardData?.getData('text') ?? '';

      if (!text.includes('.')) return;

      event.preventDefault();
      const parts = splitIpv4(text);
      const group = input.closest('.ip-inputs');
      setIpv4Value(group, parts.join('.'));
    });
  });
}

function setNetworkError(message) {
  const elements = getNetworkModalElements();
  if (!elements.error) return;

  if (!message) {
    elements.error.textContent = '';
    elements.error.classList.remove('show');
    return;
  }

  elements.error.textContent = message;
  elements.error.classList.add('show');
}

function setNetworkLoading(isLoading) {
  const elements = getNetworkModalElements();
  if (!elements.loader || !elements.applyBtn) return;

  elements.loader.classList.toggle('show', isLoading);
  elements.applyBtn.disabled = isLoading;
}

function setDhcpBadge(dhcpValue) {
  const elements = getNetworkModalElements();
  if (!elements.dhcpBadge) return;

  const enabled = isDhcpEnabled(dhcpValue);
  elements.dhcpBadge.textContent = enabled ? 'DHCP вкл.' : 'DHCP выкл.';
  elements.dhcpBadge.classList.toggle('dhcp-badge--on', enabled);
  elements.dhcpBadge.classList.toggle('dhcp-badge--off', !enabled);
}

function setAdvancedNetworkMode(enabled) {
  const elements = getNetworkModalElements();
  const rows = [elements.maskRow, elements.gatewayRow].filter(Boolean);
  const groups = [elements.maskGroup, elements.gatewayGroup].filter(Boolean);

  networkSettingsState.advancedEnabled = enabled;

  if (elements.advancedToggle) {
    elements.advancedToggle.checked = enabled;
  }

  rows.forEach((row) => {
    row.classList.toggle('is-locked', !enabled);
  });

  groups.forEach((group) => {
    getIpv4Inputs(group).forEach((input) => {
      input.disabled = !enabled;
    });
  });
}

function openSimpleModal(modal) {
  if (!modal) return;
  modal.classList.add('show');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
}

function closeSimpleModal(modal) {
  if (!modal) return;
  modal.classList.remove('show');
  modal.setAttribute('aria-hidden', 'true');

  const elements = getNetworkModalElements();
  const hasOpenedModal = [elements.modal, elements.riskModal].some(
    (item) => item && item.classList.contains('show')
  );

  if (!hasOpenedModal) {
    document.body.classList.remove('modal-open');
  }
}

function closeRiskModal() {
  const elements = getNetworkModalElements();
  closeSimpleModal(elements.riskModal);
}

function closeNetworkSettingsModal() {
  const elements = getNetworkModalElements();
  networkSettingsState.serial = null;
  setNetworkError('');
  setAdvancedNetworkMode(false);
  closeRiskModal();
  closeSimpleModal(elements.modal);
}

async function loadNetworkSettingsData(serial) {
  const elements = getNetworkModalElements();
  setNetworkLoading(true);
  setNetworkError('');

  const data = await CameraApi.getNetworkSettings(serial);

  setNetworkLoading(false);

  if (!data) {
    setNetworkError('Не удалось получить сетевые настройки');
    return;
  }

  if (data.error) {
    setNetworkError(data.error);
    return;
  }

  const ip = data.ip ?? data.address ?? '';
  const mask = data.mask ?? data.subnet_mask ?? '';
  const gateway = data.gateway ?? data.main_gateway ?? '';
  const dhcp = data.dhcp;

  setIpv4Value(elements.ipGroup, ip);
  setIpv4Value(elements.maskGroup, mask);
  setIpv4Value(elements.gatewayGroup, gateway);
  setDhcpBadge(dhcp);
}

async function openNetworkSettingsModal(serial) {
  const elements = getNetworkModalElements();
  if (!elements.modal) return;

  networkSettingsState.serial = serial;

  if (elements.serial) {
    elements.serial.textContent = serial;
  }

  setNetworkError('');
  setDhcpBadge(false);
  setIpv4Value(elements.ipGroup, '');
  setIpv4Value(elements.maskGroup, '');
  setIpv4Value(elements.gatewayGroup, '');
  setAdvancedNetworkMode(false);
  openSimpleModal(elements.modal);
  await loadNetworkSettingsData(serial);
}

async function applyNetworkSettings() {
  const elements = getNetworkModalElements();
  const serial = networkSettingsState.serial;

  if (!serial) return;

  try {
    const payload = {
      ip: readIpv4Value(elements.ipGroup, 'IP-адрес'),
    };

    if (networkSettingsState.advancedEnabled) {
      payload.mask = readIpv4Value(elements.maskGroup, 'Маска подсети');
      payload.gateway = readIpv4Value(elements.gatewayGroup, 'Основной шлюз');
    }

    setNetworkLoading(true);
    setNetworkError('');

    const result = await CameraApi.changeNetworkSettings(serial, payload);

    setNetworkLoading(false);

    if (!result) {
      setNetworkError('Не удалось применить сетевые настройки');
      alert('Не удалось применить сетевые настройки');
      return;
    }

    if (result.error) {
      setNetworkError(result.error);
      alert(result.error);
      return;
    }

    if (result.ip === 'not_driver') {
      setNetworkError('Драйвер не загружен');
      alert('Драйвер не загружен');
      return;
    }

    switch (result.ip) {
      case 'stream_not_closed':
        setNetworkError('Нельзя менять сетевые настройки при открытом потоке');
        alert('Нельзя менять сетевые настройки при открытом потоке');
        return;

      case 'no_changes':
        alert('Изменений нет');
        closeNetworkSettingsModal();
        return;

      case 'gateway==ip':
        setNetworkError('IP-адрес не должен совпадать со шлюзом');
        alert('IP-адрес не должен совпадать со шлюзом');
        return;

      case 'ip_busy':
        setNetworkError('Указанный IP уже занят');
        alert('Указанный IP уже занят');
        return;

      case 'node_map_not_available':
        setNetworkError('Не удалось получить доступ к настройкам камеры');
        alert('Не удалось получить доступ к настройкам камеры');
        return;

      case 'mask_gateway_not_changed_advanced_off':
        setNetworkError('Для изменения маски или шлюза включите расширенные настройки');
        alert('Для изменения маски или шлюза включите расширенные настройки');
        return;

      case 'ip_not_received':
        setNetworkError('Не удалось получить текущие сетевые настройки камеры');
        alert('Не удалось получить текущие сетевые настройки камеры');
        return;

      case 'ip_changed':
        alert('IP-адрес успешно изменён');
        closeNetworkSettingsModal();
        await refreshCameras();
        return;

      case 'mask_gateway_changed':
        alert('Маска и шлюз успешно изменены');
        closeNetworkSettingsModal();
        await refreshCameras();
        return;

      case 'ip_mask_gateway_changed':
        alert('IP, маска и шлюз успешно изменены');
        closeNetworkSettingsModal();
        await refreshCameras();
        return;

      case 'unknown':
        alert('Настройки применены, но сервер вернул неопределённый статус');
        closeNetworkSettingsModal();
        await refreshCameras();
        return;

      default:
        alert('Получен неизвестный ответ от сервера');
        console.log('Неизвестный ответ change_ip:', result);
    }
  } catch (error) {
    setNetworkLoading(false);
    setNetworkError(error.message || 'Ошибка проверки сетевых данных');
    alert(error.message || 'Ошибка проверки сетевых данных');
  }
}

function initNetworkSettingsModal() {
  const elements = getNetworkModalElements();
  if (!elements.modal) return;

  initIpOctetInputs(elements.modal);
  initIpOctetInputs(elements.riskModal || document);
  setAdvancedNetworkMode(false);

  if (elements.closeBtn) {
    elements.closeBtn.addEventListener('click', closeNetworkSettingsModal);
  }

  if (elements.cancelBtn) {
    elements.cancelBtn.addEventListener('click', closeNetworkSettingsModal);
  }

  if (elements.applyBtn) {
    elements.applyBtn.addEventListener('click', applyNetworkSettings);
  }

  if (elements.modal) {
    elements.modal.addEventListener('click', (event) => {
      if (event.target === elements.modal) {
        closeNetworkSettingsModal();
      }
    });
  }

  if (elements.riskModal) {
    elements.riskModal.addEventListener('click', (event) => {
      if (event.target === elements.riskModal) {
        closeRiskModal();
        setAdvancedNetworkMode(false);
      }
    });
  }

  if (elements.advancedToggle) {
    elements.advancedToggle.addEventListener('change', () => {
      if (elements.advancedToggle.checked) {
        openSimpleModal(elements.riskModal);
      } else {
        closeRiskModal();
        setAdvancedNetworkMode(false);
      }
    });
  }

  if (elements.riskAcceptBtn) {
    elements.riskAcceptBtn.addEventListener('click', () => {
      setAdvancedNetworkMode(true);
      closeRiskModal();
    });
  }

  const disableAdvanced = () => {
    setAdvancedNetworkMode(false);
    closeRiskModal();
  };

  if (elements.riskCancelBtn) {
    elements.riskCancelBtn.addEventListener('click', disableAdvanced);
  }

  if (elements.riskCloseBtn) {
    elements.riskCloseBtn.addEventListener('click', disableAdvanced);
  }

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;

    if (elements.riskModal && elements.riskModal.classList.contains('show')) {
      disableAdvanced();
      return;
    }

    if (elements.modal && elements.modal.classList.contains('show')) {
      closeNetworkSettingsModal();
    }
  });
}

function setRefreshButtonState(isLoading) {
  const refreshBtn = document.getElementById('refreshCamsBtn');
  if (!refreshBtn) return;

  refreshBtn.disabled = isLoading;
  refreshBtn.textContent = isLoading ? 'Обновление…' : 'Обновить список';
}

async function loadStatus() {
  const data = await CameraApi.getStatus();
  if (!data) return;

  const el = document.getElementById('status');
  if (!el) return;

  el.innerHTML = getDriverStatusHtml(!!data.status);
}

async function countCams() {
  const data = await CameraApi.getCountCams();
  if (!data) return;

  const el = document.getElementById('count_cams');
  if (el) {
    el.textContent = data.count ?? 0;
  }
}

async function loadCams() {
  const data = await CameraApi.getCams();
  if (!data) return;

  const table = document.getElementById('table');
  if (!table) return;

  table.innerHTML = '';

  const entries = Object.entries(data).sort(([serialA], [serialB]) =>
    String(serialA).localeCompare(String(serialB), 'ru')
  );

  if (!entries.length) {
    table.innerHTML = `
      <tr>
        <td colspan="4">
          <div class="table-empty-state">Камеры не найдены. Нажмите «Обновить список», чтобы повторить поиск.</div>
        </td>
      </tr>
    `;
    return;
  }

  const ipResponses = await Promise.all(
    entries.map(([serial, statusCode]) =>
      canOpenCamera(statusCode) ? CameraApi.getIp(serial) : Promise.resolve(null)
    )
  );

  entries.forEach(([serial, statusCode], index) => {
    const statusText = getAccessStatusText(statusCode);
    const ip = canOpenCamera(statusCode) ? ipResponses[index]?.ip ?? 'Ошибка' : '-';

    const actionsHtml = canOpenCamera(statusCode)
      ? `
        <div class="table-actions">
          <button type="button" class="action-btn action-btn--primary" data-open-camera>Подключиться</button>
          <button type="button" class="action-btn action-btn--secondary" data-network-settings>Сменить IP</button>
        </div>
      `
      : '<span class="table-empty">Недоступно</span>';

    const row = document.createElement('tr');
    row.innerHTML = `
      <td><span class="serial-chip">${escapeHtml(serial)}</span></td>
      <td>${getAccessStatusHtml(statusCode)}</td>
      <td><span class="ip-chip">${escapeHtml(ip)}</span></td>
      <td>${actionsHtml}</td>
    `;

    row.dataset.statusText = statusText;

    const openBtn = row.querySelector('[data-open-camera]');
    const networkBtn = row.querySelector('[data-network-settings]');

    if (openBtn) {
      openBtn.addEventListener('click', () => {
        openCamera(serial);
      });
    }

    if (networkBtn) {
      networkBtn.addEventListener('click', () => {
        openNetworkSettingsModal(serial);
      });
    }

    table.appendChild(row);
  });
}

function getAutoOpenSerial() {
  const params = new URLSearchParams(window.location.search);

  if (params.get('open_network_settings') !== '1') {
    return null;
  }

  return params.get('serial_number');
}

function clearAutoOpenQuery() {
  const url = new URL(window.location.href);
  url.searchParams.delete('open_network_settings');
  url.searchParams.delete('serial_number');

  const search = url.searchParams.toString();
  const nextUrl = `${url.pathname}${search ? `?${search}` : ''}${url.hash}`;
  window.history.replaceState({}, '', nextUrl);
}

async function refreshCameras() {
  setRefreshButtonState(true);

  try {
    await loadStatus();
    await countCams();
    await loadCams();
  } finally {
    setRefreshButtonState(false);
  }
}

async function initIndexPage() {
  const table = document.getElementById('table');
  const timeEl = document.getElementById('time');
  const statusEl = document.getElementById('status');
  const countEl = document.getElementById('count_cams');
  const refreshBtn = document.getElementById('refreshCamsBtn');

  if (!table && !timeEl && !statusEl && !countEl) return;

  initNetworkSettingsModal();
  await refreshCameras();

  const autoOpenSerial = getAutoOpenSerial();
  if (autoOpenSerial) {
    clearAutoOpenQuery();
    openNetworkSettingsModal(autoOpenSerial);
  }

  if (refreshBtn) {
    refreshBtn.addEventListener('click', refreshCameras);
  }

  if (timeEl) {
    updateTime();
    setInterval(updateTime, 1000);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  initIndexPage();
});

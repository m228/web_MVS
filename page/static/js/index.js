const log = {
  info: (message, payload) => window.AppLog?.info('index', message, payload),
  success: (message, payload) => window.AppLog?.success('index', message, payload),
  warn: (message, payload) => window.AppLog?.warn('index', message, payload),
  error: (message, payload) => window.AppLog?.error('index', message, payload),
  debug: (message, payload) => window.AppLog?.debug('index', message, payload),
};

function updateTime() {
  const now = new Date();
  const timeString = now.toLocaleTimeString();
  const el = document.getElementById('time');

  if (el) {
    el.textContent = timeString;
  }
}

function openCamera(serial, interfaceId, deviceHandle) {
  log.info('Открытие страницы камеры', { serial, interfaceId, deviceHandle });
  const query = new URLSearchParams({ serial_number: serial });
  if (interfaceId) query.set('interface_id', interfaceId);
  if (deviceHandle) query.set('device_handle', deviceHandle);
  window.location.href = '/camera?' + query.toString();
}

async function closeCameraStream(serial) {
  log.warn('Запрошено принудительное закрытие потока', { serial });

  const result = await CameraApi.closeStreamForce(serial);

  if (!result) {
    log.error('Не удалось закрыть поток', { serial });
    alert('Не удалось закрыть поток');
    return;
  }

  log.success('Поток закрыт', { serial, result });
  alert(`Поток закрыт для камеры ${serial}`);
  await refreshCameras();
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
  interfaceId: null,
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

function showNetworkError(message, payload) {
  setNetworkError(message);
  log.warn(message, payload);
  alert(message);
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
  log.debug('Закрытие модального окна сетевых настроек', {
    serial: networkSettingsState.serial,
  });

  networkSettingsState.serial = null;
  setNetworkError('');
  setAdvancedNetworkMode(false);
  closeRiskModal();
  closeSimpleModal(elements.modal);
}

async function loadNetworkSettingsData(serial, interfaceId, deviceHandle) {
  const elements = getNetworkModalElements();
  log.info('Загрузка сетевых настроек', { serial, interfaceId, deviceHandle });

  setNetworkLoading(true);
  setNetworkError('');

  const data = await CameraApi.getNetworkSettings(serial, interfaceId, deviceHandle);

  setNetworkLoading(false);

  if (!data) {
    log.error('Не удалось получить сетевые настройки', { serial });
    setNetworkError('Не удалось получить сетевые настройки');
    return;
  }

  if (data.error) {
    log.warn('Сервер вернул ошибку сетевых настроек', { serial, error: data.error });
    setNetworkError(data.error);
    return;
  }

  log.success('Сетевые настройки загружены', { serial, data });

  const ip = data.ip ?? data.address ?? '';
  const mask = data.mask ?? data.subnet_mask ?? '';
  const gateway = data.gateway ?? data.main_gateway ?? '';
  const dhcp = data.dhcp;

  setIpv4Value(elements.ipGroup, ip);
  setIpv4Value(elements.maskGroup, mask);
  setIpv4Value(elements.gatewayGroup, gateway);
  setDhcpBadge(dhcp);
}

async function openNetworkSettingsModal(serial, interfaceId, deviceHandle) {
  const elements = getNetworkModalElements();
  if (!elements.modal) return;

  log.info('Открытие модального окна сетевых настроек', { serial, interfaceId, deviceHandle });

  networkSettingsState.serial = serial;
  networkSettingsState.interfaceId = interfaceId || null;
  networkSettingsState.deviceHandle = deviceHandle || null;

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
  await loadNetworkSettingsData(serial, networkSettingsState.interfaceId, networkSettingsState.deviceHandle);
}

// после DeviceReset камера перезагружается ~5-15 сек, до этого discovery пустой
async function waitForCameraReboot(ms = 8000) {
  log.info('Жду перезагрузку камеры', { ms });
  await new Promise((resolve) => setTimeout(resolve, ms));
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

    log.info('Применение сетевых настроек', {
      serial,
      advanced: networkSettingsState.advancedEnabled,
    });
    log.debug('Payload сетевых настроек', payload);

    setNetworkLoading(true);
    setNetworkError('');

    const result = await CameraApi.changeNetworkSettings(serial, payload);

    setNetworkLoading(false);

    if (!result) {
      showNetworkError('Не удалось применить сетевые настройки', { serial, payload });
      return;
    }

    log.debug('Ответ changeNetworkSettings', result);

    if (result.error) {
      showNetworkError(result.error, { serial, result });
      return;
    }

    if (result.ip === 'not_driver') {
      showNetworkError('Драйвер не загружен', { serial, result });
      return;
    }

    switch (result.ip) {
      case 'stream_not_closed':
        showNetworkError('Нельзя менять сетевые настройки при открытом потоке', { serial, result });
        return;

      case 'no_changes':
        log.info('Изменений сетевых настроек нет', { serial });
        alert('Изменений нет');
        closeNetworkSettingsModal();
        return;

      case 'gateway==ip':
        showNetworkError('IP-адрес не должен совпадать со шлюзом', { serial, result });
        return;

      case 'ip_busy':
        showNetworkError('Указанный IP уже занят', { serial, result });
        return;

      case 'node_map_not_available':
        showNetworkError('Не удалось получить доступ к настройкам камеры', { serial, result });
        return;

      case 'mask_gateway_not_changed_advanced_off':
        showNetworkError('Для изменения маски или шлюза включите расширенные настройки', {
          serial,
          result,
        });
        return;

      case 'ip_not_received':
        showNetworkError('Не удалось получить текущие сетевые настройки камеры', {
          serial,
          result,
        });
        return;

      case 'ip_changed':
      case 'mask_gateway_changed':
      case 'ip_mask_gateway_changed':
        log.success('Сетевые настройки изменены, камера перезагружается', { serial, result });
        alert('Сетевые настройки изменены. Камера перезагружается, обновлю список через несколько секунд.');
        closeNetworkSettingsModal();
        await waitForCameraReboot();
        await refreshCameras();
        return;

      case 'unknown':
        log.warn('Настройки применены, но сервер вернул неопределённый статус', {
          serial,
          result,
        });
        alert('Настройки применены, но сервер вернул неопределённый статус');
        closeNetworkSettingsModal();
        await refreshCameras();
        return;

      default:
        log.error('Получен неизвестный ответ от сервера', { serial, result });
        alert('Получен неизвестный ответ от сервера');
        console.log('Неизвестный ответ change_ip:', result);
    }
  } catch (error) {
    setNetworkLoading(false);
    setNetworkError(error.message || 'Ошибка проверки сетевых данных');
    log.error('Ошибка проверки сетевых данных', {
      serial,
      error: error.message,
    });
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
        log.info('Запрошено включение расширенных сетевых настроек');
        openSimpleModal(elements.riskModal);
      } else {
        log.info('Расширенные сетевые настройки выключены');
        closeRiskModal();
        setAdvancedNetworkMode(false);
      }
    });
  }

  if (elements.riskAcceptBtn) {
    elements.riskAcceptBtn.addEventListener('click', async () => {
      log.warn('Подтверждено включение расширенных сетевых настроек');

      const result = await CameraApi.enableAdvancedNetworkSettings(networkSettingsState.serial);

      if (!result) {
        log.error('Не удалось включить расширенные сетевые настройки');
        alert('Не удалось включить расширенные сетевые настройки');
        setAdvancedNetworkMode(false);
        closeRiskModal();
        return;
      }

      log.success('Расширенные сетевые настройки включены', result);
      setAdvancedNetworkMode(true);
      closeRiskModal();
    });
  }

  const disableAdvanced = () => {
    log.info('Включение расширенных сетевых настроек отменено');
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

// ---------- модалка «Информация о камере» (read-only) ----------

function getCameraInfoElements() {
  return {
    modal: document.getElementById('cameraInfoModal'),
    serial: document.getElementById('cameraInfoSerial'),
    closeBtn: document.getElementById('cameraInfoCloseBtn'),
    closeFooterBtn: document.getElementById('cameraInfoCloseFooterBtn'),
    loader: document.getElementById('cameraInfoLoader'),
    error: document.getElementById('cameraInfoError'),
    list: document.getElementById('cameraInfoList'),
  };
}

function setCameraInfoError(message) {
  const el = getCameraInfoElements();
  if (!el.error) return;
  el.error.textContent = message || '';
  el.error.classList.toggle('show', !!message);
}

function renderCameraInfoItems(items) {
  const el = getCameraInfoElements();
  if (!el.list) return;

  el.list.innerHTML = '';

  if (!items || !items.length) {
    el.list.innerHTML = '<div class="info-empty">Нет данных для отображения</div>';
    return;
  }

  items.forEach(({ label, value }) => {
    const row = document.createElement('div');
    row.className = 'info-row';
    row.innerHTML = `
      <dt class="info-row__label">${escapeHtml(label)}</dt>
      <dd class="info-row__value">${escapeHtml(value)}</dd>
    `;
    el.list.appendChild(row);
  });
}

function closeCameraInfoModal() {
  const el = getCameraInfoElements();
  closeSimpleModal(el.modal);
}

async function openCameraInfoModal(serial, interfaceId, deviceHandle) {
  const el = getCameraInfoElements();
  if (!el.modal) return;

  log.info('Открытие информации о камере', { serial, interfaceId, deviceHandle });

  if (el.serial) el.serial.textContent = serial;
  setCameraInfoError('');
  renderCameraInfoItems([]);
  if (el.loader) el.loader.classList.add('show');
  openSimpleModal(el.modal);

  const data = await CameraApi.getCameraInfo(serial, interfaceId, deviceHandle);

  if (el.loader) el.loader.classList.remove('show');

  if (!data || data.error) {
    const message = data?.error || 'Не удалось получить информацию о камере';
    setCameraInfoError(message);
    log.warn('Ошибка получения информации о камере', { serial, message });
    return;
  }

  log.success('Информация о камере получена', { serial, count: data.items?.length || 0 });
  renderCameraInfoItems(data.items);
}

function initCameraInfoModal() {
  const el = getCameraInfoElements();
  if (!el.modal) return;

  if (el.closeBtn) el.closeBtn.addEventListener('click', closeCameraInfoModal);
  if (el.closeFooterBtn) el.closeFooterBtn.addEventListener('click', closeCameraInfoModal);

  el.modal.addEventListener('click', (event) => {
    if (event.target === el.modal) closeCameraInfoModal();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && el.modal.classList.contains('show')) {
      closeCameraInfoModal();
    }
  });
}

function setRefreshButtonState(isLoading) {
  const refreshBtn = document.getElementById('refreshCamsBtn');
  if (!refreshBtn) return;

  refreshBtn.disabled = isLoading;
  // кнопка теперь иконочная — не затираем содержимое, крутим значок
  refreshBtn.classList.toggle('is-loading', isLoading);
}

async function loadStatus() {
  const data = await CameraApi.getStatus();
  if (!data) {
    log.error('Не удалось получить статус драйвера');
    return;
  }

  log.debug('Статус драйвера', data);

  const el = document.getElementById('status');
  if (!el) return;

  el.innerHTML = getDriverStatusHtml(!!data.status);
}

async function countCams() {
  const data = await CameraApi.getCountCams();
  if (!data) {
    log.error('Не удалось получить количество камер');
    return;
  }

  log.debug('Количество камер', data);

  const el = document.getElementById('count_cams');
  if (el) {
    el.textContent = data.count ?? 0;
  }
}

function interfaceLabel(entry) {
  const ip = entry.interface_ip ? entry.interface_ip : null;
  const name = entry.interface_name || entry.interface_id || 'интерфейс не определён';
  return ip ? `${name} [${ip}]` : name;
}

// последний загруженный плоский список (для повторного рендера по чекбоксу «Показать все»)
const camsState = {
  rows: [],
  showAll: false,
};

async function loadCams() {
  log.info('Загрузка списка камер');

  const data = await CameraApi.getCamsDetailed();
  if (!data) {
    log.error('Не удалось получить список камер');
    return;
  }

  log.debug('Получены камеры', data);

  const table = document.getElementById('table');
  if (!table) return;

  table.innerHTML = '';

  // плоский список: одна строка = пара (серийник, интерфейс)
  const rows = [];
  for (const [serial, entries] of Object.entries(data)) {
    const sorted = [...entries].sort((a, b) => Number(b.available) - Number(a.available));
    sorted.forEach((entry) => rows.push({ serial, entry }));
  }

  rows.sort((a, b) =>
    String(a.serial).localeCompare(String(b.serial), 'ru') ||
    (interfaceLabel(a.entry) || '').localeCompare(interfaceLabel(b.entry) || '', 'ru')
  );

  log.info('Камер найдено (записей)', { count: rows.length });

  if (!rows.length) {
    table.innerHTML = `
      <tr>
        <td colspan="4">
          <div class="table-empty-state">Камеры не найдены. Нажмите «Обновить список», чтобы повторить поиск.</div>
        </td>
      </tr>
    `;
    return;
  }

  // ВАЖНО: control-канал у камеры один. Если делать параллельные getIp по каждому
  // дублю (для одного серийника их 5), они борются за -1005 AccessDenied.
  // Делаем один запрос на уникальный серийник и раздаём результат всем его строкам.
  const uniqueSerials = [...new Set(rows.filter((r) => r.entry.available).map((r) => r.serial))];
  const ipBySerial = new Map();

  // последовательно — чтобы между серийниками тоже не было гонки за общий продюсер
  for (const serial of uniqueSerials) {
    const response = await CameraApi.getIp(serial);
    if (response?.ip) ipBySerial.set(serial, response.ip);
  }

  camsState.rows = rows.map(({ serial, entry }) => ({
    serial,
    entry,
    ip: entry.available ? ipBySerial.get(serial) || null : null,
  }));

  renderCamsTable();
}

function renderCamsTable() {
  const table = document.getElementById('table');
  if (!table) return;

  const allRows = camsState.rows;
  // основной режим: для каждого серийника оставляем только записи с реально полученным IP.
  // если для серийника ни одна запись не отдала IP — показываем одну любую (хотя бы как индикатор).
  const visibleRows = camsState.showAll ? allRows : pickPrimaryRows(allRows);

  table.innerHTML = '';

  if (!visibleRows.length) {
    table.innerHTML = `
      <tr>
        <td colspan="4">
          <div class="table-empty-state">Камеры не найдены. Нажмите «Обновить список», чтобы повторить поиск.</div>
        </td>
      </tr>
    `;
    return;
  }

  visibleRows.forEach(({ serial, entry, ip }) => {
    const statusCode = entry.access_status;
    const statusText = getAccessStatusText(statusCode);
    // в подзаголовке показываем модель камеры (например, MV-CS050-10GC),
    // а если её нет — название интерфейса как раньше
    const subtitleText = entry.model || interfaceLabel(entry);
    const ipText = ip || (entry.available ? 'Ошибка' : '-');
    const hasIp = !!ip;

    const actionsHtml = hasIp
      ? `
        <div class="table-actions">
          <button type="button" class="action-btn action-btn--primary" data-open-camera>Подключиться</button>
          <button type="button" class="action-btn action-btn--secondary" data-network-settings>Сменить IP</button>
          <button type="button" class="action-btn action-btn--icon" data-camera-info title="Информация о камере" aria-label="Информация о камере">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><line x1="12" y1="11" x2="12" y2="16"></line><circle cx="12" cy="7.5" r="1" fill="currentColor" stroke="none"></circle></svg>
          </button>
        </div>
      `
      : `
        <div class="table-actions">
          <button type="button" class="action-btn action-btn--secondary" disabled title="Эта запись интерфейса не отвечает на запросы к камере">
            Недоступна
          </button>
        </div>
      `;

    const row = document.createElement('tr');
    row.innerHTML = `
      <td>
        <span class="serial-chip">${escapeHtml(serial)}</span>
        <div class="status-code" style="margin-top:4px;">${escapeHtml(subtitleText)}</div>
      </td>
      <td>${getAccessStatusHtml(statusCode)}</td>
      <td><span class="ip-chip">${escapeHtml(ipText)}</span></td>
      <td>${actionsHtml}</td>
    `;

    row.dataset.statusText = statusText;

    const openBtn = row.querySelector('[data-open-camera]');
    const networkBtn = row.querySelector('[data-network-settings]');

    if (openBtn) {
      openBtn.addEventListener('click', () => openCamera(serial, entry.interface_id, entry.device_handle));
    }

    if (networkBtn) {
      networkBtn.addEventListener('click', () => openNetworkSettingsModal(serial, entry.interface_id, entry.device_handle));
    }

    const infoBtn = row.querySelector('[data-camera-info]');
    if (infoBtn) {
      infoBtn.addEventListener('click', () => openCameraInfoModal(serial, entry.interface_id, entry.device_handle));
    }

    table.appendChild(row);
  });
}

// для каждого серийника — одна основная строка.
// IP у камеры один, поэтому нет смысла плодить дубли в обычном режиме.
function pickPrimaryRows(rows) {
  const bySerial = new Map();
  for (const row of rows) {
    if (!bySerial.has(row.serial)) {
      bySerial.set(row.serial, row);
      continue;
    }
    const current = bySerial.get(row.serial);
    // приоритет: с IP, потом available, потом любой
    const better = (row.ip && !current.ip)
      || (row.entry.available && !current.entry.available);
    if (better) bySerial.set(row.serial, row);
  }
  return [...bySerial.values()];
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
  log.info('Обновление списка камер');
  setRefreshButtonState(true);

  try {
    await loadStatus();
    await countCams();
    await loadCams();
    log.success('Список камер обновлён');
  } catch (error) {
    log.error('Ошибка обновления камер', {
      error: error?.message ?? String(error),
    });
    throw error;
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

  log.info('Инициализация главной страницы');

  initNetworkSettingsModal();
  initCameraInfoModal();
  await refreshCameras();

  const autoOpenSerial = getAutoOpenSerial();
  if (autoOpenSerial) {
    log.info('Автооткрытие сетевых настроек по query-параметру', { autoOpenSerial });
    clearAutoOpenQuery();
    openNetworkSettingsModal(autoOpenSerial);
  }

  if (refreshBtn) {
    refreshBtn.addEventListener('click', refreshCameras);
  }

  const showAllToggle = document.getElementById('showAllCamsBtn');
  if (showAllToggle) {
    showAllToggle.addEventListener('change', () => {
      camsState.showAll = showAllToggle.checked;
      log.debug('Режим показа списка камер изменён', { showAll: camsState.showAll });
      renderCamsTable();
    });
  }

  if (timeEl) {
    updateTime();
    setInterval(updateTime, 1000);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  log.info('Index страница загружена');
  initIndexPage().catch((error) => {
    log.error('Ошибка инициализации главной страницы', {
      error: error?.message ?? String(error),
    });
  });
});

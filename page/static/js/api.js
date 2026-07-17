// static/js/api.js
async function apiGet(url, errorText = 'Ошибка запроса', options = {}) {
  const {
    source = 'api',
    logRequest = false,
    logSuccess = false,
    timeout = 15000,
  } = options;

  if (logRequest) {
    window.AppLog?.debug(source, `GET ${url}`);
  }

  // без таймаута зависший бэкенд (напр. рестарт при обновлении) вешает fetch навсегда,
  // а вместе с ним и последовательные опросы камер в UI
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
    const data = await response.json();

    if (logSuccess) {
      window.AppLog?.success(source, `Ответ ${response.status} для ${url}`, data);
    }

    return data;
  } catch (error) {
    const message = error.name === 'AbortError' ? `Таймаут запроса (${timeout} мс)` : error.message;
    window.AppLog?.error(source, errorText, {
      url,
      error: message,
    });

    console.error(errorText, error);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

const CameraApi = {
  getNetworkSettings(serial, interfaceId, deviceHandle) {
    const query = new URLSearchParams({ serial_number: serial });
    if (interfaceId) query.set('interface_id', interfaceId);
    if (deviceHandle) query.set('device_handle', deviceHandle);
    return apiGet(`/api/get_network_settings?${query.toString()}`,
      'Ошибка получения сетевых настроек:');
  },

  changeNetworkSettings(serial, payload) {
    const query = new URLSearchParams({
      serial_number: serial,
      ip: payload.ip ?? '',
      mask: payload.mask ?? '',
      gateway: payload.gateway ?? '',
    });

    return apiGet(
      `/api/change_ip?${query.toString()}`,
      'Ошибка изменения сетевых настроек:'
    );
  },

  forceIp(serial, payload) {
    const query = new URLSearchParams({
      serial_number: serial,
      ip: payload.ip ?? '',
      mask: payload.mask ?? '',
      gateway: payload.gateway ?? '',
    });
    return apiGet(`/api/force_ip?${query.toString()}`, 'Ошибка ForceIP:');
  },

  getStatus() {
    return apiGet('/api/status', 'Ошибка получения статуса:');
  },

  getCountCams() {
    return apiGet('/api/count_cams', 'count_cams error:');
  },

  getCams() {
    return apiGet('/api/cams', 'Ошибка получения списка камер:');
  },

  getCamsDetailed() {
    return apiGet('/api/cams/detailed', 'Ошибка получения детального списка камер:');
  },

  getIp(serial, interfaceId, deviceHandle) {
    const query = new URLSearchParams({ serial_number: serial });
    if (interfaceId) query.set('interface_id', interfaceId);
    if (deviceHandle) query.set('device_handle', deviceHandle);
    return apiGet(`/api/ip?${query.toString()}`, 'Ошибка получения IP:');
  },

  getDataLimit(serial) {
  return apiGet(
    `/api/camera/data_limit?serial_number=${encodeURIComponent(serial)}`,
    'Ошибка получения data_limit:'
  );
},

  getCameraInfo(serial, interfaceId, deviceHandle) {
    const query = new URLSearchParams({ serial_number: serial });
    if (interfaceId) query.set('interface_id', interfaceId);
    if (deviceHandle) query.set('device_handle', deviceHandle);
    return apiGet(`/api/camera/info?${query.toString()}`, 'Ошибка получения информации о камере:');
  },

  enableAdvancedNetworkSettings(serial) {
    return apiGet(
        `/api/network_settings_advanced?serial_number=${encodeURIComponent(serial)}`,
        'Network settings advanced btn error'
    );
  },

  closeStream(serial) {
    return apiGet(
      `/api/camera/close_stream?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка остановки потока:'
    );
  },

  closeStreamForce(serial) {
    return apiGet(
      `/api/camera/close_stream_force?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка принудительной остановки потока:'
    );
  },

  getStreamState(serial) {
    return apiGet(
      `/api/camera/stream_state?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения состояния потока:',
      {
        source: 'api.stream_state',
        logRequest: false,
        logSuccess: false,
      }
    );
  },

  getMetrics(serial) {
    return apiGet(
      `/api/camera/metrics?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения метрик камеры:',
      {
        source: 'api.metrics',
        logRequest: false,
        logSuccess: false,
      }
    );
  },

  startPhotoSaving(serial, interval) {
    const query = new URLSearchParams({ serial_number: serial, interval });
    return apiGet(
      `/api/camera/on_save_photo?${query.toString()}`,
      'Ошибка запуска сохранения фото:'
    );
  },

  stopPhotoSaving(serial) {
    return apiGet(
      `/api/camera/off_save_photo?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка остановки сохранения фото:'
    );
  },

  startVideoSaving(serial, duration) {
    const query = new URLSearchParams({ serial_number: serial, duration });
    return apiGet(
      `/api/camera/on_save_video?${query.toString()}`,
      'Ошибка запуска записи видео:'
    );
  },

  stopVideoSaving(serial) {
    return apiGet(
      `/api/camera/off_save_video?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка остановки записи видео:'
    );
  },

  getVideoPhotoStatus(serial) {
    return apiGet(
      `/api/camera/status_video_photo?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения статуса video/photo:',
      {
        source: 'api.status_video_photo',
        logRequest: false,
        logSuccess: false,
      }
    );
  },

  getCurrentConfig(serial) {
    return apiGet(
      `/api/camera/current_config?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения текущего конфига камеры:',
      { source: 'api.current_config', logRequest: false, logSuccess: false }
    );
  }
};

window.CameraApi = CameraApi;

const RtspApi = {
  buildStreamUrl(serial, connection) {
    const query = new URLSearchParams({ serial_number: serial });

    if (connection.url) {
      query.set('url', connection.url);
    } else if (connection.ip) {
      query.set('ip', connection.ip);
      query.set('username', connection.username ?? 'admin');
      query.set('password', connection.password ?? '');
      query.set('channel', connection.channel ?? 1);
      query.set('subtype', connection.subtype ?? 0);
    }

    if (connection.scale) {
      query.set('scale', connection.scale);
    }

    if (connection.fps) {
      query.set('fps', connection.fps);
    }

    return `/api/rtsp/stream?${query.toString()}`;
  },

  snapshotUrl(serial) {
    return `/api/rtsp/snapshot?serial_number=${encodeURIComponent(serial)}`;
  },

  closeStream(serial) {
    return apiGet(
      `/api/rtsp/close_stream?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка остановки RTSP-потока:'
    );
  },

  closeStreamForce(serial) {
    return apiGet(
      `/api/rtsp/close_stream_force?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка принудительной остановки RTSP-потока:'
    );
  },

  getStreamState(serial) {
    return apiGet(
      `/api/rtsp/stream_state?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения состояния RTSP-потока:',
      { source: 'api.rtsp.stream_state', logRequest: false, logSuccess: false }
    );
  },

  getMetrics(serial) {
    return apiGet(
      `/api/rtsp/metrics?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения метрик RTSP:',
      { source: 'api.rtsp.metrics', logRequest: false, logSuccess: false }
    );
  },

  startPhotoSaving(serial, interval) {
    const query = new URLSearchParams({ serial_number: serial, interval });
    return apiGet(`/api/rtsp/on_save_photo?${query.toString()}`, 'Ошибка запуска сохранения фото (RTSP):');
  },

  stopPhotoSaving(serial) {
    return apiGet(
      `/api/rtsp/off_save_photo?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка остановки сохранения фото (RTSP):'
    );
  },

  startVideoSaving(serial, duration) {
    const query = new URLSearchParams({ serial_number: serial, duration });
    return apiGet(`/api/rtsp/on_save_video?${query.toString()}`, 'Ошибка запуска записи видео (RTSP):');
  },

  stopVideoSaving(serial) {
    return apiGet(
      `/api/rtsp/off_save_video?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка остановки записи видео (RTSP):'
    );
  },

  getVideoPhotoStatus(serial) {
    return apiGet(
      `/api/rtsp/status_video_photo?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения статуса video/photo (RTSP):',
      { source: 'api.rtsp.status_video_photo', logRequest: false, logSuccess: false }
    );
  },

  // возможности камеры (белый прожектор / оптический зум); refresh=1 — пересъём
  getCapabilities(serial, refresh = false) {
    const query = new URLSearchParams({ serial_number: serial });
    if (refresh) query.set('refresh', '1');
    return apiGet(`/api/rtsp/capabilities?${query.toString()}`,
      'Ошибка опроса возможностей RTSP-камеры:',
      { source: 'api.rtsp.capabilities', logRequest: false, logSuccess: false });
  },

  setLight(serial, on, level = 100) {
    const query = new URLSearchParams({ serial_number: serial, on: on ? 1 : 0, level });
    return apiGet(`/api/rtsp/light?${query.toString()}`, 'Ошибка управления подсветкой (RTSP):');
  },

  getLightState(serial) {
    return apiGet(
      `/api/rtsp/light_state?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения состояния подсветки (RTSP):',
      { source: 'api.rtsp.light_state', logRequest: false, logSuccess: false }
    );
  },

  setZoom(serial, factor) {
    const query = new URLSearchParams({ serial_number: serial, factor });
    return apiGet(`/api/rtsp/zoom?${query.toString()}`, 'Ошибка цифрового зума (RTSP):');
  },

  setZoomPan(serial, px, py) {
    const query = new URLSearchParams({ serial_number: serial, px, py });
    return apiGet(`/api/rtsp/zoom?${query.toString()}`, 'Ошибка смещения зума (RTSP):');
  },

  // зум по выделенной области: кратность + центр одним запросом
  setZoomRegion(serial, factor, px, py) {
    const query = new URLSearchParams({ serial_number: serial, factor, px, py });
    return apiGet(`/api/rtsp/zoom?${query.toString()}`, 'Ошибка зума по области (RTSP):');
  },

  opticalZoom(serial, direction) {
    const query = new URLSearchParams({ serial_number: serial, direction });
    return apiGet(`/api/rtsp/optical_zoom?${query.toString()}`, 'Ошибка оптического зума (RTSP):');
  },

  // мини-база сохранённых RTSP-камер
  listSaved() {
    return apiGet('/api/rtsp/saved', 'Ошибка загрузки сохранённых RTSP:',
      { source: 'api.rtsp.saved', logRequest: false, logSuccess: false });
  },

  saveCam(entry) {
    const query = new URLSearchParams({
      url: entry.url || '',
      label: entry.label || '',
      ip: entry.ip || '',
      scale: entry.scale ?? 100,
      fps: entry.fps ?? 0,
    });
    return apiGet(`/api/rtsp/save?${query.toString()}`, 'Ошибка сохранения RTSP в базу:');
  },

  removeSaved(url) {
    return apiGet(`/api/rtsp/remove_saved?url=${encodeURIComponent(url)}`, 'Ошибка удаления RTSP из базы:');
  },
};

window.RtspApi = RtspApi;

const NetApi = {
  status() {
    return apiGet('/api/net/status', 'Ошибка статуса сети:',
      { source: 'api.net', logRequest: false, logSuccess: false });
  },
  enableJumbo(adapter) {
    return apiGet(`/api/net/enable_jumbo?adapter=${encodeURIComponent(adapter)}`, 'Ошибка включения jumbo:');
  },
  enableFilter(adapter) {
    return apiGet(`/api/net/enable_filter?adapter=${encodeURIComponent(adapter)}`, 'Ошибка включения фильтра:');
  },
  disableJumbo(adapter) {
    return apiGet(`/api/net/disable_jumbo?adapter=${encodeURIComponent(adapter)}`, 'Ошибка выключения jumbo:');
  },
  disableFilter(adapter) {
    return apiGet(`/api/net/disable_filter?adapter=${encodeURIComponent(adapter)}`, 'Ошибка выключения фильтра:');
  },
};

window.NetApi = NetApi;

const UpdateApi = {
  check() {
    return apiGet('/api/update/check', 'Ошибка проверки обновлений:',
      { source: 'api.update', logRequest: false, logSuccess: false });
  },
  download() {
    return apiGet('/api/update/download', 'Ошибка скачивания обновления:',
      { source: 'api.update' });
  },
  apply() {
    return apiGet('/api/update/apply', 'Ошибка применения обновления:',
      { source: 'api.update' });
  },
  // текущая версия + окружение (используем для показа версии при загрузке)
  info() {
    return apiGet('/api/debug/info', 'Ошибка получения версии:',
      { source: 'api.update', logRequest: false, logSuccess: false });
  },
};

window.UpdateApi = UpdateApi;
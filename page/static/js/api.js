// static/js/api.js
async function apiGet(url, errorText = 'Ошибка запроса', options = {}) {
  const {
    source = 'api',
    logRequest = true,
    logSuccess = true,
  } = options;

  if (logRequest) {
    window.AppLog?.debug(source, `GET ${url}`);
  }

  try {
    const response = await fetch(url, { cache: 'no-store' });
    const data = await response.json();

    if (logSuccess) {
      window.AppLog?.success(source, `Ответ ${response.status} для ${url}`, data);
    }

    return data;
  } catch (error) {
    window.AppLog?.error(source, errorText, {
      url,
      error: error.message,
    });

    console.error(errorText, error);
    return null;
  }
}

const CameraApi = {
  getNetworkSettings(serial) {
    return apiGet(
      `/api/get_network_settings?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения сетевых настроек:'
    );
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

  getStatus() {
    return apiGet('/api/status', 'Ошибка получения статуса:');
  },

  getCountCams() {
    return apiGet('/api/count_cams', 'count_cams error:');
  },

  getCams() {
    return apiGet('/api/cams', 'Ошибка получения списка камер:');
  },

  getIp(serial) {
    return apiGet(
      `/api/ip?serial_number=${encodeURIComponent(serial)}`,
      'Ошибка получения IP:'
    );
  },

  getDataLimit(serial) {
  return apiGet(
    `/api/camera/data_limit?serial_number=${encodeURIComponent(serial)}`,
    'Ошибка получения data_limit:'
  );
},
  enableAdvancedNetworkSettings() {
    return apiGet(
        `/api/network_settings_advanced`, 'Network settings advanced btn error'
    );
  },

  closeStream() {
    return apiGet('/api/camera/close_stream', 'Ошибка остановки потока:');
  },

  closeStreamForce() {
    return apiGet('/api/camera/close_stream_force', 'Ошибка принудительной остановки потока:');
  },

  getStreamState() {
    return apiGet('/api/camera/stream_state', 'Ошибка получения состояния потока:', {
      source: 'api.stream_state',
      logRequest: false,
      logSuccess: false,
    });
  },

  getMetrics() {
    return apiGet('/api/camera/metrics', 'Ошибка получения метрик камеры:', {
      source: 'api.metrics',
      logRequest: false,
      logSuccess: false,
    });
  },

  startPhotoSaving(interval) {
    return apiGet(
      `/api/camera/on_save_photo?interval=${encodeURIComponent(interval)}`,
      'Ошибка запуска сохранения фото:'
    );
  },

  stopPhotoSaving() {
    return apiGet(
      '/api/camera/off_save_photo',
      'Ошибка остановки сохранения фото:'
    );
  },

  startVideoSaving(duration) {
    return apiGet(
      `/api/camera/on_save_video?duration=${encodeURIComponent(duration)}`,
      'Ошибка запуска записи видео:'
    );
  },

  stopVideoSaving() {
    return apiGet(
      '/api/camera/off_save_video',
      'Ошибка остановки записи видео:'
    );
  },

  getVideoPhotoStatus() {
    return apiGet('/api/camera/status_video_photo', 'Ошибка получения статуса video/photo:', {
      source: 'api.status_video_photo',
      logRequest: false,
      logSuccess: false,
    });
  }
};

window.CameraApi = CameraApi;
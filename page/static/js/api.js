// static/js/api.js
async function apiGet(url, errorText = 'Ошибка запроса', options = {}) {
  const {
    source = 'api',
    logRequest = false,
    logSuccess = false,
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
};

window.RtspApi = RtspApi;
// static/js/api.js
async function apiGet(url, errorText = 'Ошибка запроса') {
  try {
    const response = await fetch(url, { cache: 'no-store' });
    return await response.json();
  } catch (error) {
    console.error(errorText, error);
    return null;
  }
}

const CameraApi = {
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

  getDataLimit() {
  return apiGet('/api/camera/data_limit', 'Ошибка получения data_limit:');
  },

  closeStream() {
    return apiGet('/api/camera/close_stream', 'Ошибка остановки потока:');
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
    return apiGet(
      '/api/camera/status_video_photo',
      'Ошибка получения статуса video/photo:'
    );
  }
};

window.CameraApi = CameraApi;
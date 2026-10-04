// ---------- Подсветка / Изображение по иконкам (popup под иконкой) ----------
let multiLightPopup = null;
let multiImagePopup = null;

function openLightPopup() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !multiLightPopup) return;
  setCapLine(document.getElementById('multiLightCap'), false, '', 'Проверяю…');
  multiLightPopup.toggle();
  if (multiLightPopup.isOpen()) { settingsCaps = null; refreshSettingsCaps(source.serial); }
}

function openImagePopup() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !multiImagePopup) return;
  setCapLine(document.getElementById('multiImageCap'), false, '', 'Проверяю…');
  multiImagePopup.toggle();
  if (multiImagePopup.isOpen()) { settingsCaps = null; refreshSettingsCaps(source.serial); }
}

let multiPhotoPopup = null;
let multiVideoPopup = null;
function openPhotoPopup() {
  const source = focusedSource();
  if (!source || !multiPhotoPopup) return;
  multiPhotoPopup.toggle();
  if (multiPhotoPopup.isOpen()) { showSavePath('multiPhotoSavePath', null); prefillMultiSaveSettings(source.serial, source.kind); }
}
function openVideoPopup() {
  const source = focusedSource();
  if (!source || !multiVideoPopup) return;
  multiVideoPopup.toggle();
  if (multiVideoPopup.isOpen()) { showSavePath('multiVideoSavePath', null); prefillMultiSaveSettings(source.serial, source.kind); }
}

// изменить FPS RTSP-камеры в фокусе (перезапуск потока с новым ограничением)

async function refreshSettingsCaps(serial) {
  const data = await RtspApi.getCapabilities(serial);
  settingsCaps = (data && !data.error)
    ? data
    : { reachable: false, white_light: false, optical_zoom: false, image_settings: false };
  applySettingsCaps();
  syncMultiLightState(serial);
  refreshMultiImageSettings(serial);
}

function applySettingsCaps() {
  const caps = settingsCaps || {};
  const hasLight = !!caps.white_light;

  setCapLine(document.getElementById('multiLightCap'), hasLight,
    'Белый прожектор поддерживается',
    caps.reachable ? 'Подсветка не поддерживается' : 'Камера не отвечает на управление');
  const sw = document.getElementById('multiLightSwitch');
  const lv = document.getElementById('multiLightLevel');
  if (sw) sw.disabled = !hasLight;
  if (lv) lv.disabled = !hasLight;
  if (!hasLight) reflectMultiLight(false);
  // зум делается рамкой прямо в ячейке (кнопка на плитке), в настройках его нет

  // настройки изображения
  const hasImage = !!caps.image_settings;
  setCapLine(document.getElementById('multiImageCap'), hasImage,
    'Настройки изображения доступны',
    caps.reachable ? 'Настройки изображения не поддерживаются' : 'Камера не отвечает на управление');
  setMultiImageControlsEnabled(hasImage);
}

// --- настройки изображения (экспозиция / баланс белого / день-ночь) ---
function multiImageEls() {
  return {
    wb: document.getElementById('multiImageWb'),
    dayNight: document.getElementById('multiImageDayNight'),
    compensation: document.getElementById('multiImageCompensation'),
    gainMin: document.getElementById('multiImageGainMin'),
    gainMax: document.getElementById('multiImageGainMax'),
    cap: document.getElementById('multiImageCap'),
  };
}

function setMultiImageControlsEnabled(enabled) {
  const e = multiImageEls();
  [e.wb, e.dayNight, e.compensation, e.gainMin, e.gainMax].forEach((el) => {
    if (el) el.disabled = !enabled;
  });
}

function populateMultiImageUI(data) {
  const e = multiImageEls();
  UIHelpers.fillSelect(e.wb, data.wb_presets, UIHelpers.IMAGE_WB_LABELS,
    data.white_balance && data.white_balance.mode);
  UIHelpers.fillSelect(e.dayNight, data.day_night_modes, UIHelpers.IMAGE_DAY_NIGHT_LABELS,
    data.day_night && data.day_night.mode);
  const exp = data.exposure || {};
  if (e.compensation && exp.compensation != null) e.compensation.value = exp.compensation;
  if (e.gainMin && exp.gain_min != null) e.gainMin.value = exp.gain_min;
  if (e.gainMax && exp.gain_max != null) e.gainMax.value = exp.gain_max;
}

async function refreshMultiImageSettings(serial) {
  if (!(settingsCaps && settingsCaps.image_settings)) {
    setMultiImageControlsEnabled(false);
    return;
  }
  const data = await RtspApi.getImageSettings(serial);
  if (!data || data.error || !data.reachable) {
    setCapLine(multiImageEls().cap, false, '', 'Камера не отвечает на управление');
    setMultiImageControlsEnabled(false);
    log.warn('Не удалось получить настройки изображения', data);
    return;
  }
  setCapLine(multiImageEls().cap, true, 'Настройки изображения доступны', '');
  setMultiImageControlsEnabled(true);
  populateMultiImageUI(data);
}

async function applyMultiWhiteBalance() {
  const source = focusedSource();
  const e = multiImageEls();
  if (!source || source.kind !== 'rtsp' || !e.wb) return;
  const data = await RtspApi.setWhiteBalance(source.serial, e.wb.value);
  if (!data || data.error || data.ok === false) {
    log.warn('Камера не подтвердила баланс белого', data);
    refreshMultiImageSettings(source.serial);
    return;
  }
  log.success('Баланс белого применён', { mode: e.wb.value });
}

async function applyMultiDayNight() {
  const source = focusedSource();
  const e = multiImageEls();
  if (!source || source.kind !== 'rtsp' || !e.dayNight) return;
  const data = await RtspApi.setDayNight(source.serial, e.dayNight.value);
  if (!data || data.error || data.ok === false) {
    log.warn('Камера не подтвердила режим день/ночь', data);
    refreshMultiImageSettings(source.serial);
    return;
  }
  log.success('Режим день/ночь применён', { mode: e.dayNight.value });
}

async function applyMultiExposure(field, value) {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return;
  const data = await RtspApi.setExposure(source.serial, { [field]: Number(value) });
  if (!data || data.error || data.ok === false) {
    log.warn('Камера не подтвердила экспозицию', data);
    refreshMultiImageSettings(source.serial);
    return;
  }
  log.success('Экспозиция применена', { [field]: Number(value) });
}

// --- фото ---
async function applyPhoto(on) {
  const tile = state.tiles[state.focused];
  const source = focusedSource();
  if (!tile || !source) return;
  const api = apiFor(tile.kind);
  if (on) {
    const project = readMultiProjectName('multiPhotoProject');
    if (project === null) return;
    const amount = Number(document.getElementById('multiPhotoInterval')?.value) || 5;
    const unit = document.getElementById('multiPhotoUnit')?.value || 'seconds';
    const seconds = unit === 'minutes' ? amount * 60 : amount;
    // формат файла: png (без потерь) или jpg
    const photoFormat = document.getElementById('multiPhotoFormat')?.value || 'png';
    const data = await api.startPhotoSaving(source.serial, seconds, project, photoFormat);
    tile.photo = true;
    renderTile(state.focused);
    updateToolbar();
    showSavePath('multiPhotoSavePath', data);
    return;
  }
  await api.stopPhotoSaving(source.serial);
  tile.photo = false;
  renderTile(state.focused);
  updateToolbar();
  showSavePath('multiPhotoSavePath', null);
}

// --- видео ---
async function applyVideo(on) {
  const tile = state.tiles[state.focused];
  const source = focusedSource();
  if (!tile || !source) return;
  const api = apiFor(tile.kind);
  if (on) {
    const project = readMultiProjectName('multiVideoProject');
    if (project === null) return;
    const amount = Number(document.getElementById('multiVideoDuration')?.value) || 10;
    const unit = document.getElementById('multiVideoUnit')?.value || 'minutes';
    const seconds = unit === 'minutes' ? amount * 60 : amount;
    const data = await api.startVideoSaving(source.serial, seconds, project);
    tile.video = true;
    renderTile(state.focused);
    updateToolbar();
    showSavePath('multiVideoSavePath', data);
    return;
  }
  await api.stopVideoSaving(source.serial);
  tile.video = false;
  renderTile(state.focused);
  updateToolbar();
  showSavePath('multiVideoSavePath', null);
}

// --- подсветка (только RTSP-камера в фокусе) ---
function reflectMultiLight(on) {
  const sw = document.getElementById('multiLightSwitch');
  const label = document.getElementById('multiLightLabel');
  if (sw) sw.checked = on;
  if (label) label.textContent = on ? 'Вкл' : 'Выкл';
}

function setTileLight(on) {
  const tile = state.tiles[state.focused];
  if (tile) { tile.light = on; renderTile(state.focused); }
}

async function syncMultiLightState(serial) {
  if (!(settingsCaps && settingsCaps.white_light)) { reflectMultiLight(false); setTileLight(false); return; }
  const data = await RtspApi.getLightState(serial);
  const on = !!(data && data.state === 'on');
  reflectMultiLight(on);
  setTileLight(on);
}

async function applyMultiLight(on) {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !(settingsCaps && settingsCaps.white_light)) return;
  const level = Number(document.getElementById('multiLightLevel')?.value) || 100;
  const data = await RtspApi.setLight(source.serial, on, level);
  if (!data || data.error || data.status === 'failed') {
    syncMultiLightState(source.serial); // откат тумблера к фактическому состоянию
    return;
  }
  reflectMultiLight(on);
  setTileLight(on);
}


// ---------- модалки ----------
function openModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  modal.classList.add('show');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  modal.classList.remove('show');
  modal.setAttribute('aria-hidden', 'true');
  if (!document.querySelector('.modal-backdrop.show')) {
    document.body.classList.remove('modal-open');
  }
}

function focusedSource() {
  const tile = state.tiles[state.focused] || {};
  return tile.serial ? getSource(tile.serial) : null;
}

// ---------- настройки камеры в фокусе (фото/видео/подсветка) ----------
let settingsCaps = null;   // возможности RTSP-камеры в открытой модалке

// строка возможности: точка + текст (общая для подсветки/зума)
function setCapLine(el, supported, textYes, textNo) {
  if (!el) return;
  el.dataset.state = supported ? 'yes' : 'no';
  const textEl = el.querySelector('.cap-text');
  if (textEl) textEl.textContent = supported ? textYes : textNo;
}

// прочитать имя проекта из поля модалки; пустое -> предупреждение и null (имя обязательно)
function readMultiProjectName(inputId) {
  const el = document.getElementById(inputId);
  const value = el ? el.value.trim() : '';
  if (!value) {
    log.warn('Не указано имя проекта');
    alert('Укажите имя проекта');
    return null;
  }
  return value;
}

// выставить число + единицу (сек/мин) по значению в секундах: кратное 60 показываем в минутах
function setMultiInterval(inputId, unitId, seconds) {
  if (seconds == null || seconds === '') return;
  const input = document.getElementById(inputId);
  if (!input) return;
  let value = Number(seconds);
  const unitSel = document.getElementById(unitId);
  if (unitSel && value >= 60 && value % 60 === 0) {
    value = value / 60;
    unitSel.value = 'minutes';
  } else if (unitSel) {
    unitSel.value = 'seconds';
  }
  input.value = value;
}

// подтянуть сохранённые настройки (имя проекта + интервал/длительность) в модалку настроек
async function prefillMultiSaveSettings(serial, kind) {
  if (!serial) return;
  const s = await apiFor(kind).getSaveSettings(serial);
  if (!s || s.error) return;
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el && val != null && val !== '') el.value = val;
  };
  setVal('multiPhotoProject', s.photo_project);
  setVal('multiVideoProject', s.video_project);
  setMultiInterval('multiPhotoInterval', 'multiPhotoUnit', s.photo_interval);
  setMultiInterval('multiVideoDuration', 'multiVideoUnit', s.video_duration);
  // формат фото (png/jpg) — запоминаем между запусками
  setVal('multiPhotoFormat', s.photo_format);
}



// static/js/ui.js
function getPositiveNumber(selector, message) {
  const input = document.querySelector(selector);
  const rawValue = input ? input.value.trim() : '';

  if (!rawValue) {
    window.AppLog?.warn('ui', message, { selector, rawValue });
    alert(message);
    return null;
  }

  const value = Number(rawValue);

  if (Number.isNaN(value) || value <= 0) {
    window.AppLog?.warn('ui', 'Введено некорректное число', {
      selector,
      rawValue,
    });
    alert(message);
    return null;
  }

  return value;
}

function createPopupController(card, button) {
  function isOpen() {
    return !!(card && card.classList.contains('show'));
  }

  function close() {
    if (!card) return;
    card.classList.remove('show');
  }

  function open() {
    if (!card || !button) return;

    const rect = button.getBoundingClientRect();

    card.classList.add('show');
    card.style.left = '0px';
    card.style.top = '0px';

    const cardWidth = card.offsetWidth;
    const cardHeight = card.offsetHeight;

    let left = rect.left;
    let top = rect.bottom + 8;

    if (left + cardWidth > window.innerWidth - 10) {
      left = window.innerWidth - cardWidth - 10;
    }

    if (top + cardHeight > window.innerHeight - 10) {
      top = rect.top - cardHeight - 8;
    }

    if (left < 10) left = 10;
    if (top < 10) top = 10;

    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
  }

  function toggle() {
    isOpen() ? close() : open();
  }

  return { isOpen, open, close, toggle };
}

// общие для camera/multi/rtsp: были продублированы дословно в трёх файлах
function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return hh > 0 ? `${hh}:${pad(mm)}:${pad(ss)}` : `${mm}:${pad(ss)}`;
}

function showSavePath(elementId, data) {
  const el = document.getElementById(elementId);
  if (!el) return;
  if (data && data.save_dir) {
    el.textContent = `Сохранение в: ${data.save_dir}\\${data.file_pattern || ''}`;
    el.hidden = false;
  } else {
    el.hidden = true;
    el.textContent = '';
  }
}

// --- настройки изображения (общие для rtsp/multi): подписи режимов + заполнение select ---
// русские подписи режимов (значение с камеры → подпись); неизвестное значение = как есть
const IMAGE_WB_LABELS = {
  Auto: 'Авто', Sunny: 'Дневной свет', Cloudy: 'Облачно', Home: 'Лампа накаливания',
  Office: 'Люминесцентная', Night: 'Ночь', Outdoor: 'Улица',
};
const IMAGE_DAY_NIGHT_LABELS = { Color: 'Цвет', BlackWhite: 'Чёрно-белый' };

// заполнить <select> вариантами value→подпись и выставить текущее значение
function fillSelect(select, values, labels, current) {
  if (!select) return;
  select.innerHTML = '';
  (values || []).forEach((value) => {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = labels[value] || value;
    select.appendChild(opt);
  });
  if (current != null) select.value = current;
}

window.UIHelpers = {
  getPositiveNumber,
  createPopupController,
  formatDuration,
  showSavePath,
  fillSelect,
  IMAGE_WB_LABELS,
  IMAGE_DAY_NIGHT_LABELS,
};
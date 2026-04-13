// static/js/ui.js
function getPositiveNumber(selector, message) {
  const input = document.querySelector(selector);
  const rawValue = input ? input.value.trim() : '';

  if (!rawValue) {
    alert(message);
    return null;
  }

  const value = Number(rawValue);

  if (Number.isNaN(value) || value <= 0) {
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

window.UIHelpers = {
  getPositiveNumber,
  createPopupController
};
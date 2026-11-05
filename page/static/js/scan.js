const log = {
  info: (m, p) => window.AppLog?.info('scan', m, p),
  success: (m, p) => window.AppLog?.success('scan', m, p),
  error: (m, p) => window.AppLog?.error('scan', m, p),
};

const form = document.getElementById('scanForm');
const startBtn = document.getElementById('scanStartBtn');
const stopBtn = document.getElementById('scanStopBtn');
const statusEl = document.getElementById('scanStatus');
const album = document.getElementById('scanAlbum');
const dlg = document.getElementById('scanAddDlg');
const addForm = document.getElementById('scanAddForm');

let timer = null;
let addIp = null;
let rendered = new Map(); // ip -> {el, key}: плитки не пересоздаём, иначе мигают картинки

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[s]));

async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  return r.json();
}

function tileKey(it) { return `${it.status}|${it.saved ? 1 : 0}`; }

function renderTile(it) {
  const el = document.createElement('div');
  el.className = `scan-tile scan-tile--${it.status}${it.saved ? ' scan-tile--saved' : ''}`;
  el.title = it.saved ? 'Уже добавлена' : 'Двойной клик — добавить камеру';
  const img = it.status === 'frame'
    ? `<img class="scan-tile__img" src="/api/scan/frame?ip=${encodeURIComponent(it.ip)}&t=${Date.now()}" alt="" />`
    : `<div class="scan-tile__img scan-tile__img--none">${it.status === 'pending' ? 'снимаю…' : 'нет кадра'}</div>`;
  el.innerHTML = `${img}<div class="scan-tile__ip">${esc(it.ip)}</div>`;
  el.addEventListener('dblclick', () => openAdd(it));
  return el;
}

function renderAlbum(items) {
  const alive = new Set(items.map((i) => i.ip));
  for (const [ip, r] of rendered) {
    if (!alive.has(ip)) { r.el.remove(); rendered.delete(ip); }
  }
  let prev = null;
  for (const it of items) {
    const key = tileKey(it);
    let r = rendered.get(it.ip);
    if (!r || r.key !== key) {
      const el = renderTile(it);
      if (r) r.el.replaceWith(el); else album.appendChild(el);
      r = { el, key };
      rendered.set(it.ip, r);
    }
    // порядок по IP: плитка встаёт после предыдущей
    if (prev ? r.el.previousElementSibling !== prev : album.firstElementChild !== r.el) {
      prev ? prev.after(r.el) : album.prepend(r.el);
    }
    prev = r.el;
  }
}

function renderStatus(st) {
  const withFrame = st.items.filter((i) => i.status === 'frame').length;
  if (st.running) {
    statusEl.textContent = st.phase === 'ports'
      ? `Ищу камеры… ${st.done} / ${st.total} адресов, найдено ${st.items.length}`
      : `Снимаю кадры… ${st.done} / ${st.total}`;
  } else if (st.items.length || st.seconds) {
    statusEl.textContent = `Готово за ${st.seconds} с${st.stopped ? ' (остановлено)' : ''}: камер ${st.items.length}, с кадром ${withFrame}`;
  } else {
    statusEl.textContent = '';
  }
  startBtn.disabled = st.running;
  stopBtn.classList.toggle('hidden', !st.running);
}

async function poll() {
  let st;
  try { st = await (await fetch('/api/scan/state')).json(); } catch { return; }
  renderStatus(st);
  renderAlbum(st.items);
  if (st.running) timer = setTimeout(poll, 1000); else timer = null;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const d = new FormData(form);
  const res = await post('/api/scan/start', {
    prefix: String(d.get('prefix') || '').trim(),
    first: Number(d.get('first')),
    last: Number(d.get('last')),
    user: String(d.get('user') || 'admin').trim(),
    passwords: String(d.get('passwords') || '').split(/[\s,]+/).filter(Boolean),
  });
  if (res.error) {
    statusEl.textContent = res.error === 'already_running' ? 'Сканирование уже идёт' : 'Проверьте подсеть и диапазон (например 10.32.2, от 1 до 254)';
    return;
  }
  log.info('Сканирование запущено', { prefix: d.get('prefix') });
  rendered.forEach((r) => r.el.remove());
  rendered.clear();
  if (!timer) poll();
});

stopBtn.addEventListener('click', () => post('/api/scan/stop'));

function openAdd(it) {
  if (it.saved && !confirm(`${it.ip} уже в списке RTSP. Добавить заново (перезапишет)?`)) return;
  addIp = it.ip;
  document.getElementById('scanAddTitle').textContent = `Добавить ${it.ip}`;
  addForm.user.value = form.user.value || 'admin';
  addForm.password.value = '';
  document.getElementById('scanAddHint').textContent = it.status === 'frame'
    ? 'Пароль можно оставить пустым — возьмётся тот, с которым снят кадр.'
    : 'Кадр не получен: введите пароль камеры.';
  dlg.showModal();
}

document.getElementById('scanAddCancel').addEventListener('click', () => dlg.close());
addForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const res = await post('/api/scan/add', { ip: addIp, user: addForm.user.value, password: addForm.password.value });
  dlg.close();
  if (res.ok) {
    log.success('Камера добавлена', { ip: addIp });
    statusEl.textContent = `${addIp} добавлена в список RTSP`;
    poll();
  } else {
    log.error('Не удалось добавить камеру', { ip: addIp, error: res.error });
    statusEl.textContent = `Не удалось добавить ${addIp}: ${res.error || 'ошибка'}`;
  }
});

poll(); // страницу могли открыть во время идущего скана

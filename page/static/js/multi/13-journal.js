// ---------- журнал камер ----------
// Лента эпизодов из cam_events (их пишет фоновый монитор сервера, страница для этого не нужна): обрыв связи,
// серые кадры, сбои КЗ, зона «крутится/стоит». Окно обновляется само, пока открыто.
const CJ = { timer: null, key: 'cj_filters', seq: 0 };
const CJ_KINDS = {
  link: ['link_lost'],
  gray: ['gray'],
  cv: ['cv_dead', 'cv_frozen', 'cv_corrupt'],
  zone: ['zone_on', 'zone_off'],
};
const CJ_LIMIT = 500;

function cjText(e) {
  const tail = e.info ? ' · ' + e.info : '';
  switch (e.kind) {
    case 'link_lost': return 'Нет связи, переподключение';
    case 'gray': return 'Серые кадры' + tail;
    case 'cv_dead': return 'КЗ без данных' + tail;
    case 'cv_frozen': return 'КЗ: картинка застыла';
    case 'cv_corrupt': return 'КЗ: битый поток';
    case 'zone_on': return 'Зона «' + (e.info || e.zone) + '» крутится';
    case 'zone_off': return 'Зона «' + (e.info || e.zone) + '» стоит';
    default: return e.kind;
  }
}

// цвет метки: зелёная — крутится, красная — стоит, оранжевая — сбой, серая — нет данных
function cjTone(kind) {
  if (kind === 'zone_on') return 'ok';
  if (kind === 'zone_off') return 'stop';
  if (kind === 'link_lost' || kind === 'cv_dead') return 'dead';
  return 'warn';
}

function cjFmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + ' с';
  if (s < 3600) return Math.floor(s / 60) + ' мин ' + String(s % 60).padStart(2, '0') + ' с';
  return Math.floor(s / 3600) + ' ч ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + ' мин';
}

function cjFmtTime(t) {
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, '0');
  const hms = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  return d.toDateString() === new Date().toDateString() ? hms : `${p(d.getDate())}.${p(d.getMonth() + 1)} ${hms}`;
}

function cjSelectedKinds() {
  const kinds = [];
  document.querySelectorAll('[data-cj-kind]').forEach((box) => {
    if (box.checked) kinds.push(...CJ_KINDS[box.dataset.cjKind]);
  });
  return kinds;
}

function cjSaveFilters() {
  try {
    localStorage.setItem(CJ.key, JSON.stringify({
      hours: document.getElementById('cjHours').value,
      min: document.getElementById('cjMin').value,
      kinds: [...document.querySelectorAll('[data-cj-kind]')].filter((b) => b.checked).map((b) => b.dataset.cjKind),
    }));
  } catch (err) { /* не критично */ }
}

function cjRestoreFilters() {
  try {
    const f = JSON.parse(localStorage.getItem(CJ.key) || 'null');
    if (!f) return;
    if (f.hours) document.getElementById('cjHours').value = f.hours;
    if (f.min) document.getElementById('cjMin').value = f.min;
    if (Array.isArray(f.kinds)) document.querySelectorAll('[data-cj-kind]').forEach((b) => { b.checked = f.kinds.includes(b.dataset.cjKind); });
  } catch (err) { /* не критично */ }
}

function cjFillCameras() {
  const sel = document.getElementById('cjCam');
  const cur = sel.value;
  sel.innerHTML = '<option value="">все камеры</option>' + state.cameras.filter((c) => c.kind === 'rtsp')
    .map((c) => `<option value="${escapeHtml(c.serial)}">${escapeHtml(c.label)}</option>`).join('');
  sel.value = cur;
}

async function cjLoad() {
  const mine = ++CJ.seq;      // ответ на устаревший запрос (фильтр успели сменить) не показываем
  const list = document.getElementById('cjList');
  const kinds = cjSelectedKinds();
  if (!kinds.length) {
    list.innerHTML = '<p class="net-hint">Выберите, какие события показывать.</p>';
    document.getElementById('cjCount').textContent = '—';
    return;
  }
  const data = await MultiApi.events({
    cam: document.getElementById('cjCam').value,
    hours: document.getElementById('cjHours').value,
    min_s: document.getElementById('cjMin').value,
    kinds: kinds.join(','),
    limit: CJ_LIMIT,
  });
  if (mine !== CJ.seq) return;
  if (!data || !Array.isArray(data.events)) {
    list.innerHTML = '<p class="net-hint">Не удалось прочитать журнал.</p>';
    return;
  }
  const rows = data.events;
  document.getElementById('cjCount').textContent = rows.length >= CJ_LIMIT ? `последние ${CJ_LIMIT}` : `событий: ${rows.length}`;
  if (!rows.length) {
    list.innerHTML = '<p class="net-hint">За этот период событий нет.</p>';
    return;
  }
  list.innerHTML = rows.map((e) => {
    const running = e.t1 === null;
    return `<div class="cj-row"><span class="cj-t">${cjFmtTime(e.t0)}</span>` +
      `<span class="cj-d${running ? ' is-run' : ''}">${running ? 'идёт · ' : ''}${cjFmtDur(e.dur)}</span>` +
      `<span class="cj-c" title="${escapeHtml(e.cam)}">${escapeHtml(e.label)}</span>` +
      `<span class="cj-x"><i class="cj-dot cj-dot--${cjTone(e.kind)}"></i>${escapeHtml(cjText(e))}</span></div>`;
  }).join('');
}

function openCameraJournal() {
  cjFillCameras();
  openModal('cameraJournalModal');
  cjLoad();
  clearInterval(CJ.timer);
  CJ.timer = setInterval(() => {
    if (!document.getElementById('cameraJournalModal').classList.contains('show')) {
      clearInterval(CJ.timer);
      CJ.timer = null;
      return;
    }
    cjLoad();
  }, 5000);
}

function initCameraJournal() {
  cjRestoreFilters();
  document.getElementById('cameraJournalBtn')?.addEventListener('click', openCameraJournal);
  document.getElementById('cjClose')?.addEventListener('click', () => closeModal('cameraJournalModal'));
  ['cjCam', 'cjHours', 'cjMin'].forEach((id) => document.getElementById(id)?.addEventListener('change', () => { cjSaveFilters(); cjLoad(); }));
  document.querySelectorAll('[data-cj-kind]').forEach((b) => b.addEventListener('change', () => { cjSaveFilters(); cjLoad(); }));
}

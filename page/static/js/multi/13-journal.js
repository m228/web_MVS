// ---------- журнал камер ----------
// Лента эпизодов из cam_events (их пишет фоновый монитор сервера, страница для этого не нужна): обрыв связи,
// серые кадры, сбои КЗ, зона «крутится/стоит». Окно обновляется само, пока открыто.
const CJ = { timer: null, key: 'cj_filters', seq: 0, tab: 'journal' };
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

// ---------- вкладка «Отчёт смены» ----------
// По каждой камере: сбои связи/кадров/КЗ, сколько зона крутилась и стояла, остановки, сколько записано фото и видео.
// Смена — день 08–20, ночь 20–08, сутки 08–08 (как в отчёте микроскопа); идущая смена считается до «сейчас».
const CR = { seq: 0 };

function crDur(s) {
  s = Math.round(s);
  if (s < 60) return s + ' с';
  if (s < 3600) return Math.round(s / 60) + ' мин';
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h + ' ч' + (m ? ' ' + m + ' мин' : '');
}

function crCamHtml(c, win) {
  const faults = [];
  if (c.link.n) faults.push(`обрывов связи ${c.link.n} (${crDur(c.link.lost_s)})`);
  if (c.gray.n) faults.push(`серых кадров ${c.gray.n} (${crDur(c.gray.s)})`);
  if (c.cv.n) faults.push(`КЗ без данных ${c.cv.n} (${crDur(c.cv.s)})`);
  const zones = c.zones.map((z) => {
    const pct = (v) => (win > 0 ? Math.min(100, (v / win) * 100) : 0).toFixed(1);
    const stops = z.stops_n
      ? `остановок ${z.stops_n}, дольше всех ${crDur(z.longest_s)}`
      : 'остановок нет';
    const tip = z.top.map((s) => `${cjFmtTime(s.t0)} — ${crDur(s.dur)}`).join('\n');
    return `<div class="cr-zone"><span class="cr-zn">${escapeHtml(z.name)}</span>` +
      `<span class="cr-bar" title="крутилась ${crDur(z.run_s)}, стояла ${crDur(z.stop_s)}">` +
      `<i class="cr-run" style="width:${pct(z.run_s)}%"></i><i class="cr-stop" style="width:${pct(z.stop_s)}%"></i></span>` +
      `<span class="cr-zt">крутилась ${crDur(z.run_s)} · стояла ${crDur(z.stop_s)}</span>` +
      `<span class="cr-zs"${tip ? ` title="${escapeHtml(tip)}"` : ''}>${stops}</span></div>`;
  }).join('');
  return `<div class="cr-cam"><div class="cr-head"><b>${escapeHtml(c.label)}</b>` +
    `<span class="cr-files">фото ${c.files.photos} · видео ${c.files.videos}</span></div>` +
    `<div class="cr-faults${faults.length ? '' : ' is-ok'}">${faults.length ? faults.join(' · ') : 'сбоев не было'}</div>${zones}</div>`;
}

async function crLoad() {
  const mine = ++CR.seq;
  const list = document.getElementById('crList');
  const q = {};
  const date = document.getElementById('crDate').value;
  if (date) { q.date = date; q.part = document.getElementById('crPart').value; }
  const data = await MultiApi.report(q);
  if (mine !== CR.seq) return;
  if (!data || data.error) {
    list.innerHTML = '<p class="net-hint">Не удалось построить отчёт.</p>';
    return;
  }
  if (!date) {                 // первая загрузка без параметров — сервер сказал, какая смена идёт
    document.getElementById('crDate').value = data.date;
    document.getElementById('crPart').value = data.part;
  }
  const f = (t) => new Date(t * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  const live = data.t1 > data.now;
  document.getElementById('cjCount').textContent = `${f(data.t0)} — ${f(Math.min(data.t1, data.now))}${live ? ' (идёт)' : ''}`;
  list.innerHTML = data.cameras.length
    ? data.cameras.map((c) => crCamHtml(c, data.window_s)).join('')
    : '<p class="net-hint">Камер нет.</p>';
}

// шаг по сменам: день → ночь → день следующих суток; для «суток» — на день
function crStep(dir) {
  const dateEl = document.getElementById('crDate'), partEl = document.getElementById('crPart');
  if (!dateEl.value) return;
  const d = new Date(dateEl.value + 'T12:00:00');
  let part = partEl.value;
  if (part === 'all') {
    d.setDate(d.getDate() + dir);
  } else if (dir > 0) {
    if (part === 'night') d.setDate(d.getDate() + 1);
    part = part === 'day' ? 'night' : 'day';
  } else {
    if (part === 'day') d.setDate(d.getDate() - 1);
    part = part === 'day' ? 'night' : 'day';
  }
  dateEl.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  partEl.value = part;
  crLoad();
}

function cjSetTab(tab) {
  CJ.tab = tab;
  document.querySelectorAll('[data-cj-tab]').forEach((b) => b.classList.toggle('is-active', b.dataset.cjTab === tab));
  document.getElementById('cjJournal').hidden = tab !== 'journal';
  document.getElementById('cjReport').hidden = tab !== 'report';
  if (tab === 'report') crLoad(); else cjLoad();
}

function openCameraJournal() {
  cjFillCameras();
  openModal('cameraJournalModal');
  cjSetTab(CJ.tab);
  clearInterval(CJ.timer);
  let n = 0;
  CJ.timer = setInterval(() => {
    if (!document.getElementById('cameraJournalModal').classList.contains('show')) {
      clearInterval(CJ.timer);
      CJ.timer = null;
      return;
    }
    n += 1;
    if (CJ.tab === 'journal') cjLoad();
    else if (n % 12 === 0) crLoad();      // отчёт обновляем раз в минуту (считает файлы на диске)
  }, 5000);
}

function initCameraJournal() {
  cjRestoreFilters();
  document.querySelectorAll('[data-cj-tab]').forEach((b) => b.addEventListener('click', () => cjSetTab(b.dataset.cjTab)));
  ['crDate', 'crPart'].forEach((id) => document.getElementById(id)?.addEventListener('change', crLoad));
  document.getElementById('crPrev')?.addEventListener('click', () => crStep(-1));
  document.getElementById('crNext')?.addEventListener('click', () => crStep(1));
  document.getElementById('crNow')?.addEventListener('click', () => { document.getElementById('crDate').value = ''; crLoad(); });
  document.getElementById('cameraJournalBtn')?.addEventListener('click', openCameraJournal);
  document.getElementById('cjClose')?.addEventListener('click', () => closeModal('cameraJournalModal'));
  ['cjCam', 'cjHours', 'cjMin'].forEach((id) => document.getElementById(id)?.addEventListener('change', () => { cjSaveFilters(); cjLoad(); }));
  document.querySelectorAll('[data-cj-kind]').forEach((b) => b.addEventListener('change', () => { cjSaveFilters(); cjLoad(); }));
}

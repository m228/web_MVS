// ---------- КЗ: запись видео со звуком на диск (материал для разметки звуков) ----------
// Блок под параметрами камеры. Настройки живут в сервисе движения отдельно от конфига КЗ: меняются сразу и анализ не перезапускают.
const VR = { data: null, at: 0, busy: false, bound: false, area: null };
const VR_EVERY_MS = 3000;

function vrHtml(cam) {
  if (!cam) return '';
  const id = escapeHtml(cam.id);
  return `<details class="md-det" open><summary>Запись видео и звука ${mdHelp('Пишет на диск этого ПК куски по 10 минут: картинку (по умолчанию второй поток, можно только нужную область) и звук камеры без пережатия. Имя файла — время начала куска. Это отдельное подключение к камере; анализ движения от него не зависит.')}</summary>
    <div class="vr-box" id="vrBox" data-cam="${id}">
      <div class="md-fields">
        ${mdField('Писать на диск', '<label class="switch-toggle md-sw md-sw--sm"><span class="switch"><input type="checkbox" data-vr="on" disabled /><span class="switch-track"></span></span></label>')}
        ${mdField('Поток', `<select data-vr="stream" disabled>${mdSel([['sub', 'второй (меньше места)'], ['main', 'основной']], 'sub')}</select>`)}
        ${mdField('Кадров/с в файле', '<input type="number" data-vr="fps" min="1" max="25" step="1" disabled />')}
        ${mdField('Лимит на все записи, ГБ', '<input type="number" data-vr="limit_gb" min="1" max="10000" step="1" disabled />', 'Общий на все камеры. Когда записи его превышают, удаляются самые старые куски.')}
        ${mdField('Область кадра', '<span class="vr-area"><span id="vrAreaTxt">—</span><button type="button" class="btn-secondary md-x" data-vr="pick" disabled>Выбрать</button><button type="button" class="btn-secondary md-x" data-vr="full" hidden>Весь кадр</button></span>', '', 'md-wide')}
      </div>
      <p class="vr-state net-hint" id="vrState" aria-live="polite">загружаю…</p>
      <p class="vr-warn" id="vrWarn" hidden></p>
      <p class="net-hint vr-folder" id="vrFolder"></p>
    </div></details>`;
}

const vrNum = (v) => String(v).replace('.', ',');
const vrSize = (mb) => (mb >= 1024 ? vrNum(Math.round(mb / 102.4) / 10) + ' ГБ' : vrNum(mb) + ' МБ');

function vrPaint() {
  const box = mdEl('vrBox');
  if (!box || !VR.data) return;
  const d = VR.data;
  const state = mdEl('vrState');
  const c = d.cameras && d.cameras[box.dataset.cam];
  if (!c) {
    state.textContent = d.error ? 'запись недоступна: ' + d.error + ' (нужен обновлённый сервис движения)' : 'камеры ещё нет в сервисе движения';
    state.className = 'vr-state net-hint is-bad';
    return;
  }
  const set = (name, value) => {
    const el = box.querySelector('[data-vr="' + name + '"]');
    if (!el) return;
    el.disabled = false;
    if (document.activeElement === el) return;          // не перебиваем то, что человек сейчас набирает
    if (el.type === 'checkbox') el.checked = !!value; else el.value = value;
  };
  set('on', c.on);
  set('stream', c.stream);
  set('fps', c.fps);
  set('limit_gb', d.limit_gb);
  box.querySelector('[data-vr="pick"]').disabled = false;
  box.querySelector('[data-vr="full"]').hidden = !c.area;
  mdEl('vrAreaTxt').textContent = c.area ? Math.round(c.area[2] * 100) + ' × ' + Math.round(c.area[3] * 100) + ' % кадра' : 'весь кадр';
  let text = 'выключена', cls = '';
  if (c.on && c.running) { text = '● идёт запись' + (c.audio ? ' · звук: ' + c.audio : c.audio === '' ? ' · без звука' : ''); cls = c.audio === '' ? 'is-warn' : 'is-ok'; }
  else if (c.on && c.error) { text = 'не пишется: ' + c.error; cls = 'is-bad'; }
  else if (c.on) { text = 'подключаюсь…'; cls = 'is-warn'; }
  state.textContent = text + ' · кусков: ' + c.files + ', ' + vrSize(c.size_mb) + ' · все камеры: ' + vrSize(d.total_mb) + ' из ' + vrNum(d.limit_gb) + ' ГБ'
    + (d.free_gb == null ? '' : ' · свободно на диске ' + vrNum(d.free_gb) + ' ГБ');
  state.className = 'vr-state net-hint ' + cls;
  const warn = mdEl('vrWarn');
  warn.hidden = !(c.on && c.warn);
  warn.textContent = c.warn || '';
  mdEl('vrFolder').textContent = 'Папка: ' + c.folder;
}

// вызывается при каждой перерисовке панели КЗ: рисуем из памяти и не чаще раза в 3 с спрашиваем сервис
function vrTick(force) {
  if (!mdEl('vrBox')) return;
  vrBind();
  vrPaint();
  if (VR.busy || !MV.online || (!force && Date.now() - VR.at < VR_EVERY_MS)) return;
  VR.busy = true;
  mvApi('/api/motion/video_rec').then((r) => {
    VR.data = r && r.cameras ? r : { error: (r && (r.error || r.detail)) || 'нет ответа', cameras: {} };
  }).finally(() => { VR.busy = false; VR.at = Date.now(); vrPaint(); });
}

async function vrSend(body) {
  const r = await mvJson('/api/motion/video_rec', body);
  if (!r || !r.cameras) {
    alert('Запись видео: ' + ((r && (r.error || r.detail)) || 'нет ответа сервиса'));
    VR.at = 0;
    vrTick(true);
    return false;
  }
  VR.data = r;
  VR.at = Date.now();
  vrPaint();
  return true;
}

function vrBind() {
  if (VR.bound) return;
  const body = mdEl('mdBody');
  if (!body) return;
  VR.bound = true;
  body.addEventListener('change', (e) => {
    const name = e.target.dataset && e.target.dataset.vr;
    const box = mdEl('vrBox');
    if (!name || !box) return;
    e.target.blur();
    if (name === 'limit_gb' && VR.data && Number(e.target.value) * 1024 < VR.data.total_mb
      && !confirm('Записей уже ' + vrSize(VR.data.total_mb) + '. С лимитом ' + vrNum(e.target.value) + ' ГБ самые старые куски удалятся сразу и без возврата. Уменьшить?')) {
      e.target.value = VR.data.limit_gb;
      return;
    }
    if (name === 'limit_gb') vrSend({ limit_gb: Number(e.target.value) });
    else vrSend({ camera: box.dataset.cam, [name]: e.target.type === 'checkbox' ? e.target.checked : name === 'fps' ? Number(e.target.value) : e.target.value });
  });
  body.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('button[data-vr]');
    const box = mdEl('vrBox');
    if (!b || !box) return;
    if (b.dataset.vr === 'full') vrSend({ camera: box.dataset.cam, area: null });
    else if (b.dataset.vr === 'pick') vrOpenArea(box.dataset.cam);
  });
}

// ---------- окно выбора области: тянем прямоугольник по кадру камеры ----------
function vrModal() {
  let m = mdEl('vrModal');
  if (m) return m;
  m = document.createElement('div');
  m.className = 'modal-backdrop';
  m.id = 'vrModal';
  m.innerHTML = `<div class="network-modal__card motion-mark__card" role="dialog" aria-modal="true">
    <div class="network-modal__header"><div class="network-modal__title-group"><h2>Область записи</h2><span class="network-modal__serial">Камера: <strong id="vrCam">—</strong></span></div>
      <button type="button" class="modal-close-btn" data-vra="close" aria-label="Закрыть">×</button></div>
    <div class="network-modal__body">
      <div class="vr-stage" id="vrStage"><img id="vrImg" alt="Кадр камеры" draggable="false" /><div class="vr-rect" id="vrRect" hidden></div></div>
      <p class="net-hint" id="vrHint"></p>
    </div>
    <div class="network-modal__footer"><button type="button" class="btn-secondary" data-vra="close">Отмена (Esc)</button><button type="button" class="btn-primary" data-vra="save" id="vrSave" disabled>Сохранить область</button></div>
  </div>`;
  document.body.appendChild(m);
  m.addEventListener('click', (e) => {
    const b = e.target.closest('[data-vra]');
    if (!b) return;
    if (b.dataset.vra === 'close') vrCloseArea();
    else vrSaveArea();
  });
  const stage = mdEl('vrStage');
  const point = (e) => {
    const r = mdEl('vrImg').getBoundingClientRect();
    const clip = (v) => Math.min(1, Math.max(0, v));
    return [clip((e.clientX - r.left) / r.width), clip((e.clientY - r.top) / r.height)];
  };
  stage.addEventListener('pointerdown', (e) => {
    if (!VR.area || !mdEl('vrImg').naturalWidth) return;
    e.preventDefault();
    stage.setPointerCapture(e.pointerId);
    VR.area.from = point(e);
  });
  stage.addEventListener('pointermove', (e) => {
    const a = VR.area;
    if (!a || !a.from) return;
    const [x, y] = point(e), [x0, y0] = a.from;
    a.rect = [Math.min(x, x0), Math.min(y, y0), Math.abs(x - x0), Math.abs(y - y0)];
    vrDrawRect();
  });
  const done = () => { if (VR.area) VR.area.from = null; };
  stage.addEventListener('pointerup', done);
  stage.addEventListener('pointercancel', done);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && VR.area) vrCloseArea(); });
  return m;
}

function vrDrawRect() {
  const box = mdEl('vrRect'), r = VR.area && VR.area.rect;
  box.hidden = !r;
  if (r) Object.assign(box.style, { left: r[0] * 100 + '%', top: r[1] * 100 + '%', width: r[2] * 100 + '%', height: r[3] * 100 + '%' });
  const ok = !!r && r[2] >= 0.05 && r[3] >= 0.05;
  mdEl('vrSave').disabled = !ok;
  mdEl('vrHint').textContent = !r ? 'Потяните мышью прямоугольник: колокол и камни. В файл пойдёт только он.'
    : ok ? 'Область ' + Math.round(r[2] * 100) + ' × ' + Math.round(r[3] * 100) + ' % кадра. Потяните ещё раз, чтобы переделать.' : 'Слишком маленькая область — меньше 5 % стороны кадра.';
}

async function vrOpenArea(cam) {
  const m = vrModal();
  const c = VR.data && VR.data.cameras && VR.data.cameras[cam];
  const area = VR.area = { cam, rect: c && c.area ? c.area.slice() : null, from: null, url: '' };
  mdEl('vrCam').textContent = (mvCam(cam) || {}).name || cam;
  mdEl('vrImg').removeAttribute('src');
  vrDrawRect();
  m.classList.add('show');
  try {
    const r = await fetch('/api/motion/snapshot?camera=' + encodeURIComponent(cam) + '&w=1280&t=' + Date.now());
    if (VR.area !== area) return;                       // окно успели закрыть
    if (!r.ok) { mdEl('vrHint').textContent = 'Кадра ещё нет — камера не подключилась. Проверьте, что КЗ для неё включено.'; return; }
    area.url = URL.createObjectURL(await r.blob());
    mdEl('vrImg').src = area.url;
  } catch (e) { mdEl('vrHint').textContent = 'Кадр не получен: ' + e; }
}

function vrCloseArea() {
  if (VR.area && VR.area.url) URL.revokeObjectURL(VR.area.url);
  VR.area = null;
  mdEl('vrModal')?.classList.remove('show');
}

async function vrSaveArea() {
  const a = VR.area;
  if (!a || !a.rect) return;
  const area = a.rect.map((v) => Math.round(v * 1000) / 1000);
  area[2] = Math.min(area[2], Math.round((1 - area[0]) * 1000) / 1000);
  area[3] = Math.min(area[3], Math.round((1 - area[1]) * 1000) / 1000);
  if (await vrSend({ camera: a.cam, area })) vrCloseArea();
}

// ---------- КЗ (компьютерное зрение): режим «Движение» для RTSP-камер в ячейках ----------
// Камера в сервисе движения живёт под тем же серийником (rtsp_N), что и в ячейке. Тут: кнопка «КЗ» в меню (только RTSP),
// всплывающее окно (включить, зоны, пороги), слой с зонами поверх кадра ячейки и разметка зоны кликами по кадру.
// Настройки лежат в сервисе (/api/motion/config); группы и вывод в ПЛК — на странице «Движение».
const MV = {
  online: false,
  cfg: null,          // последний конфиг сервиса
  states: {},         // id камеры -> состояние из /api/motion/state
  popup: null,
  marking: null,      // { index, pts: [[x, y] в пикселях кадра ячейки] }
  tick: 0,
  busy: false,
};

const mvCam = (serial) => (MV.cfg && MV.cfg.cameras ? MV.cfg.cameras.find((c) => c.id === serial) : null) || null;
const mvEnabled = (serial) => !!(mvCam(serial) && mvCam(serial).enabled);

async function mvApi(path, opts) {
  try {
    const r = await fetch(path, opts);
    return await r.json();
  } catch (e) {
    return { error: String(e) };
  }
}

function mvJson(path, body, method) {
  return mvApi(path, { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

async function mvRefreshConfig() {
  const h = await mvApi('/api/motion/health');
  MV.online = !!(h && h.online);
  if (!MV.online) return;
  const cfg = await mvApi('/api/motion/config');
  if (cfg && !cfg.error) MV.cfg = cfg;
}

// значок «КЗ» виден только у RTSP; у GigE его нет совсем
function updateMotionButton(source) {
  const btn = document.getElementById('multiMotionBtn');
  if (!btn) return;
  const rtsp = !!source && source.kind === 'rtsp';
  btn.hidden = !rtsp;
  btn.disabled = !rtsp;
  btn.classList.toggle('is-on', rtsp && mvEnabled(source.serial));
  const ind = document.getElementById('multiMotionIndicator');
  if (ind) ind.classList.toggle('hidden', !(rtsp && mvEnabled(source.serial)));
  if (!rtsp && MV.popup && MV.popup.isOpen()) MV.popup.close();
}

// ---------- сохранение изменений камеры в сервисе ----------
// читаем свежий конфиг, правим только эту камеру и кладём обратно — не затираем правки с другой страницы
async function mvEditCamera(serial, mutate) {
  const cfg = await mvApi('/api/motion/config');
  if (!cfg || cfg.error) return cfg || { error: 'нет ответа сервиса' };
  const cam = (cfg.cameras || []).find((c) => c.id === serial);
  if (!cam) return { error: 'камера не включена в анализ' };
  mutate(cam);
  const r = await mvJson('/api/motion/config', cfg, 'PUT');
  if (r && !r.error) MV.cfg = r;
  return r;
}

// ---------- всплывающее окно ----------
function openMotionPopup() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !MV.popup) return;
  MV.popup.toggle();
  if (MV.popup.isOpen()) mvRefreshConfig().then(renderMotionPopup);
}

function renderMotionPopup() {
  const source = focusedSource();
  const card = document.getElementById('multiMotionCard');
  if (!card || !source || source.kind !== 'rtsp') return;
  const cap = document.getElementById('multiMotionCap');
  const sw = document.getElementById('multiMotionSwitch');
  const body = document.getElementById('multiMotionBody');
  const on = mvEnabled(source.serial);
  setCapLine(cap, MV.online, 'Сервис движения работает',
    'Сервис движения не запущен (motion_service\\setup.bat, потом перезапуск web_MVS)');
  sw.disabled = !MV.online;
  sw.checked = on;
  document.getElementById('multiMotionLabel').textContent = on ? 'Движение: анализ включён' : 'Выкл';
  body.hidden = !(MV.online && on);
  document.getElementById('multiMotionFull').href = '/motion?camera=' + encodeURIComponent(source.serial);
  if (!body.hidden) renderMotionZones(source.serial);
  updateMotionButton(source);
}

function renderMotionZones(serial) {
  const box = document.getElementById('multiMotionZones');
  const cam = mvCam(serial);
  const zones = (cam && cam.zones) || [];
  if (!zones.length) {
    box.innerHTML = '<p class="net-hint">Зон нет. Нажмите «Разметить зону» и обведите на картинке то, что должно двигаться (лента, шнек).</p>';
    return;
  }
  box.innerHTML = zones.map((z, i) => `
    <div class="cv-zone" data-zi="${i}">
      <input type="text" data-zf="name" value="${escapeHtml(z.name)}" title="Название зоны" />
      <span><span class="cv-bit" data-zbit="${escapeHtml(z.id)}">–</span>
        <button type="button" class="btn-secondary" data-zdel title="Удалить зону" style="min-height:0;padding:2px 8px">✕</button></span>
      <div class="cv-row">
        <select data-zf="metric" title="Метрика: diff — разность кадров, flow — оптический поток">
          <option value="diff"${z.metric === 'diff' ? ' selected' : ''}>diff</option>
          <option value="flow"${z.metric === 'flow' ? ' selected' : ''}>flow</option></select>
        on <input type="number" step="0.05" min="0" data-zf="on" value="${z.on}" title="Включить при значении выше" />
        off <input type="number" step="0.05" min="0" data-zf="off" value="${z.off}" title="Выключить при значении ниже" />
        <span data-zval="${escapeHtml(z.id)}">значение –</span>
      </div>
    </div>`).join('');
}

async function onMotionZoneEdit(event) {
  const row = event.target.closest('[data-zi]');
  const source = focusedSource();
  if (!row || !source) return;
  const idx = Number(row.dataset.zi);
  const field = event.target.dataset.zf;
  const isDel = event.target.hasAttribute('data-zdel');
  if (!field && !isDel) return;
  if (isDel && !confirm('Удалить зону?')) return;
  const r = await mvEditCamera(source.serial, (cam) => {
    const z = cam.zones[idx];
    if (!z) return;
    if (isDel) {
      const id = z.id;
      cam.zones.splice(idx, 1);
      (cam.groups || []).forEach((g) => { g.zones = g.zones.filter((x) => x !== id); if (g.order) g.order = g.order.filter((x) => x !== id); });
      cam.groups = (cam.groups || []).filter((g) => g.zones.length);
    } else if (field === 'name') {
      z.name = event.target.value;
    } else if (field === 'metric') {
      z.metric = event.target.value;
      z.on = z.metric === 'flow' ? 0.9 : 4.0;
      z.off = z.metric === 'flow' ? 0.6 : 2.0;
    } else {
      z[field] = parseFloat(event.target.value) || 0;
    }
  });
  if (r && r.error) {
    alert('Не сохранено: ' + r.error);
    log.warn('КЗ: зона не сохранена', r);
  }
  renderMotionZones(source.serial);
  drawAllMotionTiles();
}

async function onMotionSwitch(event) {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return;
  const want = event.target.checked;
  const r = await mvJson('/api/motion/vision', {
    serial: source.serial, enabled: want, name: source.label, url: (source.connection && source.connection.url) || '',
  });
  if (r && r.error) {
    alert('КЗ: ' + r.error);
    event.target.checked = !want;
    return;
  }
  if (r && r.cameras) MV.cfg = r;
  if (!want) cancelMotionMarking();
  log.info('КЗ: анализ движения ' + (want ? 'включён' : 'выключен'), { serial: source.serial });
  renderMotionPopup();
  drawAllMotionTiles();
}

// ---------- геометрия: кадр в ячейке (object-fit: contain) ----------
function tileFrameGeom(index) {
  const el = getTileEl(index);
  if (!el) return null;
  const screen = el.querySelector('[data-tile-screen]');
  const frame = el.querySelector('[data-tile-frame]');
  if (!screen || !frame || !frame.naturalWidth) return null;
  const w = screen.clientWidth, h = screen.clientHeight;
  const nw = frame.naturalWidth, nh = frame.naturalHeight;
  const k = Math.min(w / nw, h / nh);
  return { w, h, nw, nh, k, ox: (w - nw * k) / 2, oy: (h - nh * k) / 2 };
}

// точки зоны хранятся в пикселях кадра, на котором размечали (frame_size), поэтому приводим к кадру ячейки
function zonePointsOnTile(z, cam, g) {
  const fs = cam.frame_size || [g.nw, g.nh];
  return z.points.map((p) => [g.ox + p[0] * (g.nw / fs[0]) * g.k, g.oy + p[1] * (g.nh / fs[1]) * g.k]);
}

function drawMotionTile(index) {
  const tile = state.tiles[index];
  const el = getTileEl(index);
  if (!tile || !el) return;
  const canvas = el.querySelector('[data-tile-motion]');
  const badge = el.querySelector('[data-tile-cv]');
  if (!canvas) return;
  const cam = tile.serial && tile.kind === 'rtsp' ? mvCam(tile.serial) : null;
  const on = !!(cam && cam.enabled);
  const st = on ? (MV.states[tile.serial] || {}).result : null;

  if (badge) {
    badge.hidden = !on;
    if (on) {
      const total = (st && st.zones ? st.zones.filter((z) => z.enabled !== false).length : cam.zones.length);
      const act = st && st.zones ? st.zones.filter((z) => z.bit === 1).length : 0;
      const dead = !MV.online || (st && !st.alive) || !st;
      badge.textContent = dead ? 'КЗ · нет данных' : 'КЗ ' + act + '/' + total;
      badge.classList.toggle('is-active', !dead && act > 0);
      badge.classList.toggle('is-bad', dead);
    }
  }

  const g = tileFrameGeom(index);
  const w = g ? g.w : el.querySelector('[data-tile-screen]').clientWidth;
  const h = g ? g.h : el.querySelector('[data-tile-screen]').clientHeight;
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!g || !tile.connected) return;

  if (on) {
    const live = {};
    ((st && st.zones) || []).forEach((z) => { live[z.id] = z; });
    ctx.lineWidth = 2;
    ctx.font = 'bold 13px sans-serif';
    cam.zones.forEach((z) => {
      if (z.enabled === false) return;
      const pts = zonePointsOnTile(z, cam, g);
      const lz = live[z.id];
      const act = lz && lz.bit === 1;
      ctx.strokeStyle = act ? '#22c55e' : '#f59e0b';
      ctx.fillStyle = act ? 'rgba(34,197,94,.22)' : 'rgba(245,158,11,.12)';
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.closePath(); ctx.fill(); ctx.stroke();
      const label = z.name + (lz ? '  ' + Number(lz.value).toFixed(2) : '');
      ctx.lineWidth = 3; ctx.strokeStyle = '#000'; ctx.strokeText(label, pts[0][0] + 4, pts[0][1] + 15);
      ctx.fillStyle = '#fff'; ctx.fillText(label, pts[0][0] + 4, pts[0][1] + 15); ctx.lineWidth = 2;
    });
  }

  if (MV.marking && MV.marking.index === index) {
    const pts = MV.marking.pts.map((p) => [g.ox + p[0] * g.k, g.oy + p[1] * g.k]);
    ctx.strokeStyle = '#38bdf8'; ctx.fillStyle = '#38bdf8'; ctx.lineWidth = 2;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    if (MV.marking.cursor) {
      const c = MV.marking.cursor;
      ctx.lineTo(g.ox + c[0] * g.k, g.oy + c[1] * g.k);
    }
    ctx.stroke();
    pts.forEach((p) => { ctx.beginPath(); ctx.arc(p[0], p[1], 4, 0, 7); ctx.fill(); });
  }
}

function drawAllMotionTiles() {
  state.tiles.forEach((_, i) => drawMotionTile(i));
}

// ---------- разметка зоны кликами по кадру ячейки ----------
function startMotionMarking() {
  const index = state.focused;
  const tile = state.tiles[index];
  const source = tile && tile.serial ? getSource(tile.serial) : null;
  if (!source || source.kind !== 'rtsp' || !mvEnabled(source.serial)) return;
  if (!tile.connected || !tileFrameGeom(index)) {
    alert('Сначала подключите камеру в ячейке (кнопка ▶ в меню) — зону обводят по живой картинке.');
    return;
  }
  cancelMotionMarking();
  MV.marking = { index, pts: [], cursor: null };
  if (MV.popup) MV.popup.close();
  const el = getTileEl(index);
  el.querySelector('[data-tile-motion]').classList.add('is-marking');
  const hint = document.createElement('div');
  hint.className = 'tile-cv-hint';
  hint.dataset.tileCvHint = '1';
  hint.innerHTML = '<span>Кликайте по углам зоны. ПКМ — убрать точку.</span>' +
    '<button type="button" class="btn-primary" data-cv-close>Замкнуть (Enter)</button>' +
    '<button type="button" class="btn-secondary" data-cv-cancel>Отмена (Esc)</button>';
  hint.addEventListener('click', (e) => {
    e.stopPropagation();
    if (e.target.closest('[data-cv-close]')) closeMotionPolygon();
    else if (e.target.closest('[data-cv-cancel]')) cancelMotionMarking();
  });
  el.querySelector('[data-tile-screen]').appendChild(hint);
  drawMotionTile(index);
}

function cancelMotionMarking() {
  if (!MV.marking) return;
  const index = MV.marking.index;
  MV.marking = null;
  const el = getTileEl(index);
  if (el) {
    el.querySelector('[data-tile-motion]')?.classList.remove('is-marking');
    el.querySelectorAll('[data-tile-cv-hint]').forEach((n) => n.remove());
  }
  drawMotionTile(index);
}

function markPoint(index, event) {
  const g = tileFrameGeom(index);
  const canvas = getTileEl(index).querySelector('[data-tile-motion]');
  const r = canvas.getBoundingClientRect();
  const x = (event.clientX - r.left) * (canvas.width / r.width);
  const y = (event.clientY - r.top) * (canvas.height / r.height);
  return [Math.max(0, Math.min(g.nw, Math.round((x - g.ox) / g.k))), Math.max(0, Math.min(g.nh, Math.round((y - g.oy) / g.k)))];
}

async function closeMotionPolygon() {
  const m = MV.marking;
  if (!m || m.pts.length < 3) return;
  const index = m.index;
  const tile = state.tiles[index];
  const g = tileFrameGeom(index);
  const name = prompt('Название зоны', 'Зона');
  if (name === null) return;
  const pts = m.pts.slice();
  cancelMotionMarking();
  const r = await mvEditCamera(tile.serial, (cam) => {
    // точки хранятся в системе кадра, в которой размечена камера; новая зона приводится к ней
    const fs = cam.frame_size || [g.nw, g.nh];
    cam.frame_size = fs;
    let n = 1;
    while (cam.zones.some((z) => z.id === 'z' + n)) n += 1;
    cam.zones.push({
      id: 'z' + n, name: (name || '').trim() || 'Зона ' + n, enabled: true, type: 'motion', metric: 'diff',
      points: pts.map((p) => [Math.round(p[0] * fs[0] / g.nw), Math.round(p[1] * fs[1] / g.nh)]),
      on: 4.0, off: 2.0, invert: false, smooth: 5, frames_to_on: 3, frames_to_off: 4, blur: 5, flicker_comp: true, out: null,
    });
  });
  if (r && r.error) {
    alert('Зона не сохранена: ' + r.error);
    return;
  }
  log.success('КЗ: зона добавлена', { serial: tile.serial });
  drawAllMotionTiles();
  openMotionPopup();
}

// ---------- опрос и инициализация ----------
function startMotionPolling() {
  const loop = async () => {
    try {
      MV.tick += 1;
      if (MV.tick % 10 === 1) await mvRefreshConfig();          // конфиг подтягиваем раз в ~10 с (правки со страницы «Движение»)
      const any = state.tiles.some((t) => t.serial && t.kind === 'rtsp' && mvEnabled(t.serial));
      if (MV.online && any) {
        const st = await mvApi('/api/motion/state');
        if (st && st.cameras) st.cameras.forEach((c) => { MV.states[c.id] = c; });
        paintMotionLive();
      }
      drawAllMotionTiles();
      updateMotionButton(focusedSource());
    } catch (e) { /* опрос не должен падать */ }
    setTimeout(loop, 1000);
  };
  mvRefreshConfig().then(() => { drawAllMotionTiles(); loop(); });
  window.addEventListener('resize', drawAllMotionTiles);
}

// живые числа в открытом окне (без перерисовки полей, чтобы не сбивать ввод)
function paintMotionLive() {
  const source = focusedSource();
  if (!source || !MV.popup || !MV.popup.isOpen()) return;
  const st = (MV.states[source.serial] || {}).result;
  if (!st || !st.zones) return;
  st.zones.forEach((z) => {
    const bit = document.querySelector(`[data-zbit="${z.id}"]`);
    if (bit) { bit.textContent = z.bit; bit.classList.toggle('on', z.bit === 1); }
    const val = document.querySelector(`[data-zval="${z.id}"]`);
    if (val) val.textContent = 'значение ' + Number(z.value).toFixed(2) + ' (flow ' + Number(z.flow).toFixed(2) + ', diff ' + Number(z.diff).toFixed(1) + ')';
  });
}

function initMotionVision() {
  const card = document.getElementById('multiMotionCard');
  const btn = document.getElementById('multiMotionBtn');
  if (!card || !btn) return;
  MV.popup = UIHelpers.createPopupController(card, btn);
  btn.addEventListener('click', (e) => { e.stopPropagation(); openMotionPopup(); });
  card.addEventListener('click', (e) => e.stopPropagation());
  document.getElementById('multiMotionSwitch')?.addEventListener('change', onMotionSwitch);
  document.getElementById('multiMotionMark')?.addEventListener('click', startMotionMarking);
  const zones = document.getElementById('multiMotionZones');
  zones?.addEventListener('change', onMotionZoneEdit);
  zones?.addEventListener('click', (e) => { if (e.target.hasAttribute('data-zdel')) onMotionZoneEdit(e); });

  document.addEventListener('click', (e) => {
    if (!MV.popup || !MV.popup.isOpen()) return;
    if (!card.contains(e.target) && !btn.contains(e.target)) MV.popup.close();
  });
  document.addEventListener('keydown', (e) => {
    if (!MV.marking || /INPUT|SELECT|TEXTAREA/.test((e.target || {}).tagName || '')) return;
    if (e.key === 'Enter') closeMotionPolygon();
    else if (e.key === 'Escape') cancelMotionMarking();
  });

  // клики по слою ячейки в режиме разметки; сами слои создаются в createTileEl — ловим всплытием на сетке
  const grid = document.getElementById('multiGrid');
  grid?.addEventListener('click', (e) => {
    const layer = e.target.closest('[data-tile-motion]');
    if (!layer || !MV.marking) return;
    const tileEl = layer.closest('.multi-tile');
    if (Number(tileEl.dataset.tile) !== MV.marking.index) return;
    e.stopPropagation();
    MV.marking.pts.push(markPoint(MV.marking.index, e));
    drawMotionTile(MV.marking.index);
  });
  grid?.addEventListener('mousemove', (e) => {
    if (!MV.marking) return;
    const layer = e.target.closest('[data-tile-motion]');
    if (!layer || Number(layer.closest('.multi-tile').dataset.tile) !== MV.marking.index) return;
    MV.marking.cursor = markPoint(MV.marking.index, e);
    drawMotionTile(MV.marking.index);
  });
  grid?.addEventListener('contextmenu', (e) => {
    const layer = e.target.closest('[data-tile-motion]');
    if (!layer || !MV.marking) return;
    e.preventDefault();
    MV.marking.pts.pop();
    drawMotionTile(MV.marking.index);
  });
}

// ---------- КЗ (компьютерное зрение): режим «Движение» для RTSP-камер в ячейках ----------
// Камера в сервисе движения живёт под тем же серийником (rtsp_N), что и в ячейке. Здесь: общее состояние, опрос сервиса,
// кнопка «КЗ» в меню и слой с зонами поверх кадра ячейки (зелёная — крутится, красная — стоит, серая — нет данных/битый поток).
// Настройки, тренды, ПЛК и сценарии — в нижней панели (11-motion-dock.js), разметка зоны — в большом окне.
const MV = {
  online: false,
  cfg: null,          // последний конфиг сервиса (камеры, ПЛК, сценарии)
  states: {},         // id камеры -> состояние из /api/motion/state
  scen: [],           // состояние сценариев записи в ПЛК
  dockOpen: false,
  tick: 0,
  dirty: false,       // есть несохранённые правки конфига (опрос не затирает их)
};

const MV_COLORS = {
  on: ['#4ade80', 'rgba(74,222,128,.20)'],
  off: ['#f87171', 'rgba(248,113,113,.16)'],
  dead: ['#9ca3af', 'rgba(156,163,175,.16)'],
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
  if (MV.dirty) return;
  const h = await mvApi('/api/motion/health');
  MV.online = !!(h && h.online);
  if (!MV.online) return;
  const cfg = await mvApi('/api/motion/config');
  if (cfg && !cfg.error && !MV.dirty) MV.cfg = cfg;
}

// состояние камеры для рисования: 'on' | 'off' | 'dead' (нет связи, фриз, битый поток)
function mvZoneMode(st, zoneLive) {
  if (!MV.online || !st || !st.alive || st.corrupt || !zoneLive) return 'dead';
  return zoneLive.bit === 1 ? 'on' : 'off';
}

// значок «КЗ» в меню виден только у RTSP; у GigE его нет совсем
function updateMotionButton(source) {
  const btn = document.getElementById('multiMotionBtn');
  if (!btn) return;
  const rtsp = !!source && source.kind === 'rtsp';
  btn.hidden = !rtsp;
  btn.disabled = !rtsp;
  btn.classList.toggle('is-on', rtsp && mvEnabled(source.serial));
  const ind = document.getElementById('multiMotionIndicator');
  if (ind) ind.classList.toggle('hidden', !(rtsp && mvEnabled(source.serial)));
  if (typeof renderMotionDock === 'function') renderMotionDock();
}

function toggleMotionDock() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return;
  setMotionDock(!MV.dockOpen);
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

function tileMotionStatusText(st) {
  if (!MV.online) return 'сервис КЗ не запущен';
  if (!st) return 'нет данных';
  if (st.corrupt) return 'битый поток';
  if (!st.alive) return st.reason || 'нет данных';
  return '';
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
  const problem = on ? tileMotionStatusText(st) : '';

  if (badge) {
    badge.hidden = !on;
    if (on) {
      const total = (st && st.zones ? st.zones.filter((z) => z.enabled !== false).length : cam.zones.length);
      const act = st && st.zones ? st.zones.filter((z) => z.bit === 1).length : 0;
      badge.textContent = problem ? 'КЗ · ' + problem : 'КЗ ' + act + '/' + total;
      badge.classList.toggle('is-active', !problem);
      badge.classList.toggle('is-bad', !!problem);
    }
  }

  const g = tileFrameGeom(index);
  const w = g ? g.w : el.querySelector('[data-tile-screen]').clientWidth;
  const h = g ? g.h : el.querySelector('[data-tile-screen]').clientHeight;
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!g || !tile.connected || !on) return;

  const live = {};
  ((st && st.zones) || []).forEach((z) => { live[z.id] = z; });
  ctx.lineWidth = 2;
  ctx.font = 'bold 13px sans-serif';
  cam.zones.forEach((z) => {
    if (z.enabled === false) return;
    const pts = zonePointsOnTile(z, cam, g);
    const lz = live[z.id];
    const [stroke, fill] = MV_COLORS[mvZoneMode(st, lz)];
    ctx.strokeStyle = stroke;
    ctx.fillStyle = fill;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath(); ctx.fill(); ctx.stroke();
    const label = z.name + (lz && !problem ? '  ' + Number(lz.value).toFixed(2) : '');
    ctx.lineWidth = 3; ctx.strokeStyle = '#000'; ctx.strokeText(label, pts[0][0] + 4, pts[0][1] + 15);
    ctx.fillStyle = '#fff'; ctx.fillText(label, pts[0][0] + 4, pts[0][1] + 15); ctx.lineWidth = 2;
  });
}

function drawAllMotionTiles() {
  state.tiles.forEach((_, i) => drawMotionTile(i));
}

// ---------- подсказка при наведении: значения diff / flow по зоне под курсором ----------
function pointInPoly(pt, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i, i += 1) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function onMotionHover(event) {
  const tip = document.getElementById('mvTip');
  if (!tip) return;
  const screen = event.target.closest && event.target.closest('[data-tile-screen]');
  const tileEl = screen && screen.closest('.multi-tile');
  const index = tileEl ? Number(tileEl.dataset.tile) : -1;
  const tile = state.tiles[index];
  const cam = tile && tile.serial && tile.kind === 'rtsp' ? mvCam(tile.serial) : null;
  const g = cam && cam.enabled ? tileFrameGeom(index) : null;
  if (!g) { tip.hidden = true; return; }
  const r = screen.getBoundingClientRect();
  const pt = [event.clientX - r.left, event.clientY - r.top];
  const st = (MV.states[tile.serial] || {}).result;
  const hit = cam.zones.find((z) => z.enabled !== false && pointInPoly(pt, zonePointsOnTile(z, cam, g)));
  if (!hit) { tip.hidden = true; return; }
  const lz = ((st && st.zones) || []).find((z) => z.id === hit.id);
  const problem = tileMotionStatusText(st);
  tip.textContent = hit.name + (problem ? ' — ' + problem : lz
    ? ' · ' + (lz.bit ? 'крутится' : 'стоит') + ' · diff ' + Number(lz.diff).toFixed(2) + ' · flow ' + Number(lz.flow).toFixed(2) : '');
  tip.style.left = Math.min(event.clientX + 14, window.innerWidth - 260) + 'px';
  tip.style.top = event.clientY + 16 + 'px';
  tip.hidden = false;
}

// ---------- опрос и инициализация ----------
function startMotionPolling() {
  const loop = async () => {
    try {
      MV.tick += 1;
      if (MV.tick % 10 === 1) await mvRefreshConfig();          // конфиг подтягиваем раз в ~10 с
      const any = MV.dockOpen || state.tiles.some((t) => t.serial && t.kind === 'rtsp' && mvEnabled(t.serial));
      if (MV.online && any) {
        const st = await mvApi('/api/motion/state');
        if (st && st.cameras) st.cameras.forEach((c) => { MV.states[c.id] = c; });
        if (st && st.scenarios) MV.scen = st.scenarios;
        if (typeof paintMotionDockLive === 'function') paintMotionDockLive();
      }
      drawAllMotionTiles();
      updateMotionButton(focusedSource());
    } catch (e) { /* опрос не должен падать */ }
    setTimeout(loop, 1000);
  };
  mvRefreshConfig().then(() => { drawAllMotionTiles(); loop(); });
  window.addEventListener('resize', drawAllMotionTiles);
}

function initMotionVision() {
  const btn = document.getElementById('multiMotionBtn');
  if (!btn) return;
  btn.addEventListener('click', (e) => { e.stopPropagation(); toggleMotionDock(); });
  document.getElementById('multiGrid')?.addEventListener('mousemove', onMotionHover);
  document.getElementById('multiGrid')?.addEventListener('mouseleave', () => { const t = document.getElementById('mvTip'); if (t) t.hidden = true; });
  if (typeof initMotionDock === 'function') initMotionDock();
}

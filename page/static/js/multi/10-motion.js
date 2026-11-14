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

// значок «КЗ» в меню виден у RTSP-камеры в фокусе и в общем виде (фокуса нет, но RTSP-камеры есть); у GigE его нет совсем
function updateMotionButton(source) {
  const btn = document.getElementById('multiMotionBtn');
  if (!btn) return;
  const rtsp = !!source && source.kind === 'rtsp';
  const overview = !source && state.cameras.some((c) => c.kind === 'rtsp');
  const on = rtsp ? mvEnabled(source.serial) : (overview && ((MV.cfg && MV.cfg.cameras) || []).some((c) => c.enabled));
  btn.hidden = !(rtsp || overview);
  btn.disabled = !(rtsp || overview);
  btn.classList.toggle('is-on', !!on);
  const ind = document.getElementById('multiMotionIndicator');
  if (ind) ind.classList.toggle('hidden', !on);
  if (typeof renderMotionDock === 'function') renderMotionDock();
}

function toggleMotionDock() {
  const source = focusedSource();
  if (source && source.kind !== 'rtsp') return;
  setMotionDock(!MV.dockOpen);
}

// ---------- геометрия: кадр в ячейке (object-fit: contain) ----------
function tileFrameGeom(index) {
  const el = getTileEl(index);
  const tile = state.tiles[index];
  if (!el || !tile) return null;
  const screen = el.querySelector('[data-tile-screen]');
  const frame = el.querySelector('[data-tile-frame]');
  if (!screen || !frame) return null;
  // картинка скрыта «глазиком» (src пустой): геометрию берём по последнему кадру или по размеру разметки зон,
  // чтобы зоны остались на чёрном экране там же, где были на картинке
  if (frame.naturalWidth) tile._nat = [frame.naturalWidth, frame.naturalHeight];
  let nat = frame.naturalWidth ? tile._nat : null;
  if (!nat && tile.hidden) nat = tile._nat || (tile.serial && (mvCam(tile.serial) || {}).frame_size) || null;
  if (!nat) return null;
  const w = screen.clientWidth, h = screen.clientHeight;
  const nw = nat[0], nh = nat[1];
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
  const maskBtn = el.querySelector('[data-tile-mask]');         // кнопка «разметка» — только у камеры с включённым КЗ
  if (maskBtn) {
    maskBtn.hidden = !on;
    const off = !!tile.markupOff;
    if (maskBtn.dataset.off !== String(off)) {
      maskBtn.dataset.off = String(off);
      maskBtn.classList.toggle('is-eye-off', off);
      maskBtn.innerHTML = off ? MARK_OFF_SVG : MARK_SVG;
      maskBtn.title = off ? 'Показать разметку КЗ' : 'Скрыть разметку КЗ (зоны, палки, подписи) — останется чистая картинка';
    }
  }

  // картинка скрыта, а зоны КЗ есть — на чёрном экране только они (с названием и значением), надпись «картинка скрыта» не мешает
  const ph = el.querySelector('[data-tile-placeholder]');
  if (ph && tile.connected && tile.hidden) ph.classList.toggle('hidden', !!(on && cam.zones.some((z) => z.enabled !== false)));

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

  // лента снимков уровня слева и открытый из неё снимок (15-level.js): пока снимок открыт, рисуется его разметка, а не живая
  if (lvTileDraw(index, el, canvas, cam, on)) return;

  const g = tileFrameGeom(index);
  const w = g ? g.w : el.querySelector('[data-tile-screen]').clientWidth;
  const h = g ? g.h : el.querySelector('[data-tile-screen]').clientHeight;
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!g || !tile.connected || !on || tile.markupOff) return;       // разметка скрыта кнопкой — чистая картинка

  const live = {};
  ((st && st.zones) || []).forEach((z) => { live[z.id] = z; });
  ctx.lineWidth = 2;
  ctx.font = 'bold 13px sans-serif';
  cam.zones.forEach((z) => {
    if (z.enabled === false) return;
    if (z.type === 'cycle') { drawCycleOnTile(ctx, z, cam, g, live[z.id], mvZoneMode(st, live[z.id])); return; }
    if (z.type === 'level') return;                 // стёкла уровня рисует drawLevelOnTile ниже
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
  // рассинхрон валов: подпись по центру сверху, красная — тревога
  ((st && st.groups) || []).filter((lg) => lg.type === 'mirror').forEach((lg, k) => {
    const bad = !!problem || !lg.valid;
    const txt = 'Δ ' + (bad || lg.delta == null ? '—' : (lg.delta > 0 ? '+' : '') + lg.delta.toFixed(1) + '°');
    ctx.font = 'bold 18px sans-serif';
    ctx.textAlign = 'center';
    ctx.lineWidth = 4; ctx.strokeStyle = '#000'; ctx.strokeText(txt, g.w / 2, 26 + k * 24);
    ctx.fillStyle = bad ? '#9ca3af' : lg.alarm ? '#f87171' : '#4ade80'; ctx.fillText(txt, g.w / 2, 26 + k * 24);
    ctx.textAlign = 'left'; ctx.font = 'bold 13px sans-serif'; ctx.lineWidth = 2;
  });
  // разности валов в ряду и между рядами (вкладка «Валы»): строка на пару, в которой есть вал этой камеры; красная — тревога
  const prs = typeof shPairsOfCamera === 'function' ? shPairsOfCamera(cam.id) : [];
  if (prs.length) {
    const mirrors = ((st && st.groups) || []).filter((lg) => lg.type === 'mirror').length;
    ctx.font = 'bold 13px sans-serif'; ctx.textAlign = 'center';
    prs.forEach((p, k) => {
      const txt = p.short + (p.valid ? '   Δ ' + shSgn(p.angle_diff, 1) + '°   Δv ' + shSgn(p.rpm_diff, 2) : '   —');
      const y = 26 + mirrors * 24 + 16 + k * 17;
      ctx.lineWidth = 3; ctx.strokeStyle = '#000'; ctx.strokeText(txt, g.w / 2, y);
      ctx.fillStyle = !p.valid || problem ? '#9ca3af' : (p.alarm_angle || p.alarm_rpm) ? '#f87171' : '#e5e7eb'; ctx.fillText(txt, g.w / 2, y);
    });
    ctx.textAlign = 'left'; ctx.lineWidth = 2;
  }
  drawLevelOnTile(ctx, cam, g, st, problem);
}

// уровень жидкости (декантатор): стёкла нижнее — голубое, верхнее — жёлтое; на каждом его заполнение, сверху по центру Total и вклад стёкол.
// opts.labels === false — без надписей (миниатюры в ленте снимков: рамки, заливка, линия уровня и контур жидкости)
const MV_LEVEL_COL = ['#38bdf8', '#fbbf24'];
function drawLevelOnTile(ctx, cam, g, st, problem, opts) {
  const cg = (cam.groups || []).find((x) => x.type === 'level');
  if (!cg) return;
  const labels = !(opts && opts.labels === false);
  const lg = ((st && st.groups) || []).find((x) => x.id === cg.id);
  const live = {};
  ((st && st.zones) || []).forEach((z) => { live[z.id] = z; });
  const ok = !problem && !!lg && !!lg.valid;
  ctx.lineWidth = 2; ctx.font = 'bold 13px sans-serif';
  cg.zones.forEach((zid, i) => {
    const z = cam.zones.find((q) => q.id === zid);
    if (!z || z.enabled === false) return;
    const pts = zonePointsOnTile(z, cam, g);
    const lz = live[zid];
    const col = ok ? MV_LEVEL_COL[i % 2] : '#9ca3af';
    const path = () => { ctx.beginPath(); pts.forEach((p, k) => (k ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]))); ctx.closePath(); };
    ctx.lineWidth = 2; ctx.strokeStyle = col; ctx.fillStyle = col + '1a';
    path(); ctx.fill(); ctx.stroke();
    if (!ok || !lz) return;
    // «палка» уровня: линия на измеренной высоте внутри стекла и заливка под ней — двигаются вместе с жидкостью, как палка вала с углом
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const yl = y1 - (y1 - y0) * Math.max(0, Math.min(100, lz.level)) / 100;
    ctx.save();
    path(); ctx.clip();
    ctx.fillStyle = col + '55'; ctx.fillRect(x0, yl, x1 - x0, y1 - yl);
    ctx.lineWidth = 3; ctx.strokeStyle = '#fff'; ctx.beginPath(); ctx.moveTo(x0, yl); ctx.lineTo(x1, yl); ctx.stroke();
    ctx.restore();
    if (!labels) return;
    // подпись справа от стекла на высоте уровня: стрелка и заполнение стекла
    const label = Math.round(lz.level) + ' %';
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.moveTo(x1 + 3, yl); ctx.lineTo(x1 + 11, yl - 5); ctx.lineTo(x1 + 11, yl + 5); ctx.closePath(); ctx.fill();
    ctx.lineWidth = 3; ctx.strokeStyle = '#000'; ctx.strokeText(label, x1 + 14, yl + 5);
    ctx.fillStyle = '#fff'; ctx.fillText(label, x1 + 14, yl + 5); ctx.lineWidth = 2;
  });
  if (ok && lg.contour && lg.contour_wh) {          // тонкий контур найденной жидкости
    const cw = lg.contour_wh[0], kx = (g.nw / cw) * g.k;
    ctx.strokeStyle = '#4ade80'; ctx.lineWidth = 1.5;
    lg.contour.forEach((poly) => {
      ctx.beginPath();
      poly.forEach((p, k) => (k ? ctx.lineTo(g.ox + p[0] * kx, g.oy + p[1] * kx) : ctx.moveTo(g.ox + p[0] * kx, g.oy + p[1] * kx)));
      ctx.closePath(); ctx.stroke();
    });
    ctx.lineWidth = 2;
  }
  if (!labels) return;
  const main = ok ? lvFmt(lg.total) + ' %' : '—';
  ctx.textAlign = 'center';
  ctx.font = 'bold 20px sans-serif';
  ctx.lineWidth = 4; ctx.strokeStyle = '#000'; ctx.strokeText(main, g.w / 2, 28);
  ctx.fillStyle = ok ? '#fff' : '#9ca3af'; ctx.fillText(main, g.w / 2, 28);
  if (ok) {
    const sub = lvFmt(lg.g1) + ' + ' + lvFmt(lg.g2);
    ctx.font = 'bold 13px sans-serif';
    ctx.lineWidth = 3; ctx.strokeText(sub, g.w / 2, 46);
    ctx.fillStyle = '#d1d5db'; ctx.fillText(sub, g.w / 2, 46);
  }
  ctx.textAlign = 'left'; ctx.font = 'bold 13px sans-serif'; ctx.lineWidth = 2;
}

// направление вращения вала значком: ↻ по часовой, ↺ против, ■ стоит (числа оборотов не показываем)
function mvDirIcon(rpm) {
  const v = Number(rpm) || 0;
  return Math.abs(v) < 0.02 ? '■' : v > 0 ? '↻' : '↺';
}

// угол синего конца палки: напротив красного
const mvBlueAngle = (red) => (Number(red) + 180) % 360;

const MV_ROD_RED = '#ef4444';
const MV_ROD_BLUE = '#38bdf8';

// зона-цикл: окружность поиска вокруг оси вала и палка — красный конец на найденном угле, синий напротив
function drawCycleOnTile(ctx, z, cam, g, lz, mode) {
  const fs = cam.frame_size || [g.nw, g.nh];
  const sx = (g.nw / fs[0]) * g.k, sy = (g.nh / fs[1]) * g.k;
  const cx = g.ox + z.center[0] * sx, cy = g.oy + z.center[1] * sy, r = z.radius * (sx + sy) / 2;
  ctx.lineWidth = 1.5; ctx.strokeStyle = MV_COLORS[mode][0]; ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.stroke(); ctx.setLineDash([]);
  if (mode === 'dead' || !lz || !lz.valid) return;
  const a = (lz.angle * Math.PI) / 180, dx = Math.sin(a) * r, dy = -Math.cos(a) * r;
  ctx.lineWidth = 3;
  if (lz.coast) ctx.setLineDash([6, 5]);                   // палки не видно — угол держится по скорости вращения (пунктир)
  ctx.strokeStyle = MV_ROD_RED; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + dx, cy + dy); ctx.stroke();
  ctx.strokeStyle = MV_ROD_BLUE; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx - dx, cy - dy); ctx.stroke();
  ctx.setLineDash([]);
  // подпись под окружностью: вал со значком направления, ниже значения красного и синего концов (0–360° от вертикали по часовой)
  const x = cx - r * 0.55, y = cy + r + 16;
  const sh = typeof shShaftOf === 'function' ? shShaftOf(cam.id, z.id) : null;          // вал назначен рядом камеры: НЛВ… и скорость
  const rpmTxt = sh && lz.valid ? '  ' + Math.abs(lz.rpm).toFixed(1) + ' об/мин' : '';
  [[(sh ? sh.short : z.name) + '  ' + mvDirIcon(lz.rpm) + rpmTxt + (lz.coast ? '  ~' : ''), '#fff'], ['красный ' + Number(lz.angle).toFixed(1) + '°', MV_ROD_RED], ['синий ' + mvBlueAngle(lz.angle).toFixed(1) + '°', MV_ROD_BLUE]].forEach(([txt, col], i) => {
    ctx.lineWidth = 3; ctx.strokeStyle = '#000'; ctx.strokeText(txt, x, y + i * 17);
    ctx.fillStyle = col; ctx.fillText(txt, x, y + i * 17);
  });
  ctx.lineWidth = 2;
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
  const g = cam && cam.enabled && !tile.lvView ? tileFrameGeom(index) : null;     // открыт снимок уровня — живые значения не подсказываем
  if (!g) { tip.hidden = true; return; }
  const r = screen.getBoundingClientRect();
  const pt = [event.clientX - r.left, event.clientY - r.top];
  const st = (MV.states[tile.serial] || {}).result;
  const hit = cam.zones.find((z) => z.enabled !== false && pointInPoly(pt, zonePointsOnTile(z, cam, g)));
  if (!hit) { tip.hidden = true; return; }
  const lz = ((st && st.zones) || []).find((z) => z.id === hit.id);
  const problem = tileMotionStatusText(st);
  tip.textContent = hit.name + (problem ? ' — ' + problem : lz && hit.type === 'level'
    ? ' · заполнено ' + Number(lz.level).toFixed(1) + ' % · сырое ' + Number(lz.raw).toFixed(1) + ' %' : lz
    ? ' · ' + (lz.bit ? 'крутится' : 'стоит') + ' · diff ' + Number(lz.diff).toFixed(2) + ' · flow ' + Number(lz.flow).toFixed(2) : '');
  tip.style.left = Math.min(event.clientX + 14, window.innerWidth - 260) + 'px';
  tip.style.top = event.clientY + 16 + 'px';
  tip.hidden = false;
}

// ---------- подгонка радиуса вала: тянем за край окружности прямо на плитке ----------
// Геометрия считается от реального кадра потока (tileFrameGeom), поэтому с цифровым зумом камеры работает так же:
// радиус хранится в пикселях кадра, на котором идёт анализ.
const ROD_GRAB_PX = 9;           // насколько близко к линии окружности курсор «цепляет» её

function rodHit(event) {
  const screen = event.target.closest && event.target.closest('[data-tile-screen]');
  const tileEl = screen && screen.closest('.multi-tile');
  if (!screen || !tileEl) return null;
  const index = Number(tileEl.dataset.tile);
  const tile = state.tiles[index];
  const cam = tile && tile.serial && tile.kind === 'rtsp' ? mvCam(tile.serial) : null;
  const g = cam && cam.enabled ? tileFrameGeom(index) : null;
  if (!g) return null;
  const fs = cam.frame_size || [g.nw, g.nh];
  const sx = (g.nw / fs[0]) * g.k, sy = (g.nh / fs[1]) * g.k, s = (sx + sy) / 2;
  const box = screen.getBoundingClientRect();
  const px = event.clientX - box.left, py = event.clientY - box.top;
  let best = null;
  cam.zones.forEach((z) => {
    if (z.type !== 'cycle' || z.enabled === false) return;
    const cx = g.ox + z.center[0] * sx, cy = g.oy + z.center[1] * sy;
    const off = Math.abs(Math.hypot(px - cx, py - cy) - z.radius * s);
    if (off <= ROD_GRAB_PX && (!best || off < best.off)) best = { index, screen, cam, z, cx, cy, s, fs, off };
  });
  return best;
}

function initRodRadiusDrag() {
  const grid = document.getElementById('multiGrid');
  if (!grid) return;
  let drag = null;

  grid.addEventListener('mousemove', (e) => {
    if (drag) return;
    const hit = rodHit(e);
    const screen = e.target.closest && e.target.closest('[data-tile-screen]');
    if (screen) screen.style.cursor = hit ? 'nwse-resize' : '';
  });

  grid.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    const hit = rodHit(e);
    if (!hit) return;
    e.preventDefault();
    e.stopPropagation();
    drag = hit;
    const box = hit.screen.getBoundingClientRect();
    const move = (ev) => {
      const r = Math.hypot(ev.clientX - box.left - drag.cx, ev.clientY - box.top - drag.cy) / drag.s;
      drag.z.radius = Math.round(Math.max(20, Math.min(drag.fs[0], r)));
      drawMotionTile(drag.index);
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up, true);
      const swallow = (ev) => { ev.stopPropagation(); };            // после перетаскивания не считаем клик кликом по плитке
      document.addEventListener('click', swallow, { capture: true, once: true });
      setTimeout(() => document.removeEventListener('click', swallow, true), 0);
      drag = null;
      if (typeof mdCommit === 'function') mdCommit();             // сохранить конфиг в сервис КЗ
      if (typeof MD !== 'undefined' && MD) { MD.key = ''; if (typeof renderMotionDock === 'function') renderMotionDock(); }
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up, true);
  }, true);
}

// ---------- опрос и инициализация ----------
function startMotionPolling() {
  const loop = async () => {
    try {
      MV.tick += 1;
      if (MV.tick % 10 === 1) await mvRefreshConfig();          // конфиг подтягиваем раз в ~10 с
      if (MV.tick % 15 === 2 && MV.online) lvStripsRefresh();    // лента снимков уровня (новый снимок раз в минуту) — без ожидания
      const any = MV.dockOpen || state.tiles.some((t) => t.serial && t.kind === 'rtsp' && mvEnabled(t.serial));
      if (MV.online && any) {
        const st = await mvApi('/api/motion/state');
        if (st && st.cameras) st.cameras.forEach((c) => { MV.states[c.id] = c; });
        if (st && st.scenarios) MV.scen = st.scenarios;
        if (st && st.shafts) MV.shafts = st.shafts;       // скорости и разности валов (вкладка «Валы», подписи на плитке)
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
  initRodRadiusDrag();
  if (typeof initMotionDock === 'function') initMotionDock();
}

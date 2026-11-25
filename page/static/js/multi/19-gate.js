// ---------- КЗ · шибер: заполнение 0–100 % по указателю на линейке ----------
// Зона типа gate: ruler — четыре угла линейки (нижняя сторона 0 %, верхняя 100 %, flip — наоборот), points — где ходит указатель,
// цвет указателя — те же поля и та же пипетка, что у красного конца вала. Считает motion_service/motion_engine.py (GateRT);
// тут разметка, карточка на вкладке «Зоны · тренды» и рисунок на плитке: линейка заполняется до уровня стрелки.
const GT_RED_DEF = [10, 165, 100, 70];                       // как GATE_RED_HSV в сервисе
const GT_NO_DATA = 65535;                                    // что пишет сценарий, когда значения нет
const GT_COL = '#38bdf8', GT_FILL = 'rgba(74,222,128,.45)', GT_HOLD = 'rgba(251,191,36,.45)';

const gtFmt = (v) => Number(v).toFixed(1).replace('.', ',') + ' %';
const gtNum = (v, d) => (v == null ? d : v);

// четыре угла в любом порядке → [низ слева, низ справа, верх справа, верх слева] (как order_ruler в сервисе)
function gtOrder(pts) {
  const p = pts.slice().sort((a, b) => a[1] - b[1]);
  const top = p.slice(0, 2).sort((a, b) => a[0] - b[0]), bot = p.slice(2).sort((a, b) => a[0] - b[0]);
  return [bot[0], bot[1], top[1], top[0]];
}

// линия уровня t (0 — низ, 1 — верх): точки на левой и правой сторонах линейки
function gtLine(r, t) {
  const mix = (a, b) => [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])];
  return [mix(r[0], r[3]), mix(r[1], r[2])];
}

// ---------- плитка ----------
function drawGateOnTile(ctx, z, cam, g, lz, problem) {
  if (!z.ruler || z.ruler.length !== 4) return;
  const on = (pts) => zonePointsOnTile({ points: pts }, cam, g);
  const ruler = on(z.ruler), track = on(z.points);
  const ok = !problem && !!lz && !!lz.valid;
  const path = (pts) => { ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]))); ctx.closePath(); };
  const text = (s, x, y, align) => { ctx.textAlign = align || 'left'; ctx.lineWidth = 4; ctx.strokeStyle = '#000'; ctx.strokeText(s, x, y); ctx.fillStyle = ok ? '#fff' : '#cbd5e1'; ctx.fillText(s, x, y); ctx.textAlign = 'left'; ctx.lineWidth = 2; };
  ctx.save();
  ctx.lineWidth = 1; ctx.setLineDash([5, 4]); ctx.strokeStyle = 'rgba(250,204,21,.85)'; path(track); ctx.stroke(); ctx.setLineDash([]);
  if (ok) {                                                  // заполнение: от стороны «0 %» до уровня указателя
    const base = gtLine(ruler, z.flip ? 1 : 0), lvl = gtLine(ruler, lz.t);
    ctx.fillStyle = lz.held ? GT_HOLD : GT_FILL; path([base[0], base[1], lvl[1], lvl[0]]); ctx.fill();
    ctx.lineWidth = 3; ctx.strokeStyle = '#fff'; ctx.beginPath(); ctx.moveTo(lvl[0][0], lvl[0][1]); ctx.lineTo(lvl[1][0], lvl[1][1]); ctx.stroke();
  }
  ctx.lineWidth = 2; ctx.strokeStyle = ok ? GT_COL : '#9ca3af'; path(ruler); ctx.stroke();
  const top = gtLine(ruler, 1)[0];
  ctx.font = 'bold 13px sans-serif';
  text(z.name, top[0], Math.max(14, top[1] - 6));
  if (!ok) { text('— %', gtLine(ruler, 0.5)[0][0] - 6, gtLine(ruler, 0.5)[0][1], 'right'); ctx.restore(); return; }
  const [l, r] = gtLine(ruler, lz.t);
  const [p0, p1] = on([[lz.box[0], lz.box[1]], [lz.box[2], lz.box[3]]]), c = on([lz.ptr])[0];
  const right = c[0] >= (l[0] + r[0]) / 2;                   // указатель справа от линейки (обычно) или слева
  const from = [right ? p0[0] - 3 : p1[0] + 3, c[1]], to = right ? r : l;
  const col = lz.held ? '#fbbf24' : '#4ade80';
  ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 2;
  ctx.strokeRect(p0[0] - 3, p0[1] - 3, p1[0] - p0[0] + 6, p1[1] - p0[1] + 6);
  if ((right && from[0] > to[0]) || (!right && from[0] < to[0])) {      // стрелка от указателя к линейке
    const a = Math.atan2(to[1] - from[1], to[0] - from[0]), h = 10;
    ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(from[0], from[1]); ctx.lineTo(to[0], to[1]); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(to[0], to[1]); ctx.lineTo(to[0] - h * Math.cos(a - 0.45), to[1] - h * Math.sin(a - 0.45)); ctx.lineTo(to[0] - h * Math.cos(a + 0.45), to[1] - h * Math.sin(a + 0.45)); ctx.closePath(); ctx.fill();
  }
  ctx.font = 'bold 18px sans-serif';                         // число — с другой стороны линейки, на высоте уровня
  const side = right ? l : r;
  text(gtFmt(lz.pct), side[0] + (right ? -6 : 6), side[1] + 6, right ? 'right' : 'left');
  ctx.restore();
}

// ---------- карточка на вкладке «Зоны · тренды» ----------
function gtScenarios(camId, zoneId) {
  return ((MV.cfg && MV.cfg.scenarios) || []).filter((s) => s.sources.some((x) => x.camera === camId && x.zone === zoneId));
}

function gtPlcHtml(cam, z, a) {
  const list = gtScenarios(cam.id, z.id);
  const txt = list.map((s) => escapeHtml(s.name) + ' → ' + (s.out ? 'рег. ' + s.out.address : 'регистр не задан')).join('; ');
  return `<div class="md-zone__row"><span class="md-lbl" title="В ПЛК значение пишет сценарий (вкладка «ПЛК»): × 10 (54,3 % → 543) или как есть, целые проценты (54) — выбирается в сценарии; нет данных — 65535">в ПЛК</span>` +
    (list.length ? `<span class="net-hint">${txt}</span>` : `<button type="button" class="btn-secondary md-x" ${a} data-f="gtsc" title="Создать сценарий записи этого шибера и открыть его: останется задать регистр">создать сценарий</button>`) + '</div>';
}

function gtColorsHtml(cam, z, a) {
  const k = escapeHtml(cam.id + '/' + z.id);
  const head = SH_HSV_COLS.red.map(([lbl, , , tip]) => `<div class="sh-th" title="${escapeHtml(tip)}">${lbl}</div>`).join('');
  const rows = (z.red_more || []).map((_, n) => shExtraRowHtml('red', z, a, n)).join('') + (z.red_not || []).map((_, n) => shNotRowHtml('red', z, a, n)).join('');
  // обёртка с id: по нему панель помнит, раскрыт ли блок, после перерисовки (у двух камер шиберы называются одинаково)
  return `<div id="md-gt-${escapeHtml(cam.id + '-' + z.id)}"><details class="md-det md-gate__col"><summary>Цвет указателя · ${escapeHtml(z.name)}</summary>
    <div class="sh-col" data-shcol="${k}" data-shkind="gate" data-cam="${escapeHtml(cam.id)}" data-zone="${escapeHtml(z.id)}">
      <div class="sh-col__head"><b>${escapeHtml(z.name)}</b><span class="net-hint">${escapeHtml(cam.name)}</span><span class="md-sp"></span>
        <button type="button" class="btn-secondary md-x" ${a} data-f="hsvreset" title="Вернуть цвет указателя по умолчанию (красный)">по умолчанию</button></div>
      <div class="sh-col__body">
        <div class="sh-prev"><img alt="" width="${SH_PREV_W}" data-shprev="${k}" />
          <div class="sh-seg"><button type="button" class="on" data-md="shpick" data-f="view" data-v="mask" title="Что принимается за указатель: красные точки внутри его зоны">маска</button><button type="button" data-md="shpick" data-f="view" data-v="raw" title="Сам кадр без маски">кадр</button></div></div>
        <div class="sh-tbl"><div class="sh-th"></div>${head}<div class="sh-th"></div>${shColorRowHtml('red', z, a, cam.id + '/' + z.id, 'Указатель')}${rows}</div>
      </div>
      <div class="net-hint sh-col__note" data-shprevn="${k}"></div>
    </div></details></div>`;
}

function gtCardHtml(cam, z, i) {
  const a = `data-md="z" data-cam="${escapeHtml(cam.id)}" data-i="${i}"`;
  const k = escapeHtml(cam.id + '-' + z.id);
  return `<div class="md-zone md-gate">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><input type="text" ${a} data-f="name" value="${escapeHtml(z.name)}" title="${escapeHtml(z.id)}" />
        <b class="md-gate__v" id="md-zv-${k}" title="Заполнение линейки">–</b><span class="md-sp"></span>
        <button type="button" class="btn-secondary md-x" ${a} data-f="del" title="Удалить шибер">✕</button></div>
      <div class="md-zstat" id="md-st-${k}"></div>
      <div class="md-zone__row"><span class="md-lbl" title="Указатель пропал (пар, человек перед ним): столько секунд держим последнее значение, потом «нет данных»">держать, с</span>${mdNum(a + ' data-f="gthold"', gtNum(z.hold_s, 10), 'min="0" max="3600" step="1"')}
        <span class="md-lbl" title="Пятно цвета меньше этого числа точек кадра за указатель не принимаем. Сколько точек у пятна сейчас — в строке выше">пятно от</span>${mdNum(a + ' data-f="gtarea"', gtNum(z.min_area, 200), 'min="1" step="50"')}
        <span class="md-lbl" title="Значение — медиана стольких последних кадров: гасит дрожание, но запаздывает примерно на половину этих кадров">кадров</span>${mdNum(a + ' data-f="gtsmooth"', gtNum(z.smooth, 5), 'min="1" max="100" step="1"')}</div>
      <div class="md-zone__row"><label class="md-lbl md-gate__chk" title="Обычно 0 % — нижняя сторона линейки. С галочкой — верхняя"><input type="checkbox" ${a} data-f="gtflip"${z.flip ? ' checked' : ''} /> 0 % сверху</label></div>
      ${gtPlcHtml(cam, z, a)}
    </div>
    <div class="md-zone__chart"><canvas id="md-ch-${k}" height="84"></canvas></div>
  </div>${gtColorsHtml(cam, z, a)}`;
}

// строка под названием: что сейчас и сводка за период
function gtStatHtml(z, cur, st) {
  const s = st && st.flow;
  const sum = s ? ` · ср ${gtFmt(s.avg)} · мин ${gtFmt(s.min)} · макс ${gtFmt(s.max)}` : '';
  if (!cur) return 'нет данных';
  if (!cur.valid) return `<span title="Пятно цвета указателя в его зоне меньше порога «пятно от» или его нет. Проверьте цвет (блок ниже) и зону">${escapeHtml(cur.reason || 'указатель не найден')} · пятно ${cur.area} точек</span>${sum}`;
  return (cur.held ? '<span style="color:var(--warning)">указателя не видно, держу последнее</span>' : `пятно ${cur.area} точек`) + sum;
}

function gtDrawChart(cv, d, t, ok) {
  const vals = d.flow.map((v, i) => (d.bit[i] ? v : null));
  mdDrawSeries(cv, t, vals, ok, 0, 100, { h: cv.height || 84, tip: (i) => (vals[i] == null ? 'нет данных' : 'заполнение <b>' + gtFmt(vals[i]) + '</b>') });
}

// поля карточки; true — событие обработано
function gtOnZone(t, cam, z) {
  const f = t.dataset.f;
  if (f === 'hsvreset') { mdEdit(() => { z.red_hsv = GT_RED_DEF.slice(); z.red_more = []; z.red_not = []; }, true); return true; }
  if (f === 'gtsc') {                                         // сценарий без регистра: пока человек не задал адрес, в ПЛК ничего не уходит
    const list = (MV.cfg.scenarios = MV.cfg.scenarios || []);
    let n = 1;
    while (list.some((s) => s.id === 's' + n)) n += 1;
    MD.open['sc-s' + n] = true;
    mdEdit(() => list.push({ id: 's' + n, name: cam.name + ' · ' + z.name, rule: 'value', signal: 'move', sources: [{ camera: cam.id, zone: z.id }], out: null }), false);
    MD.tab = 'plc'; MD.key = ''; renderMotionDock();
    return true;
  }
  if (f === 'gtflip') { mdEdit(() => { z.flip = t.checked; }); return true; }
  const num = { gthold: ['hold_s', 0, 3600], gtarea: ['min_area', 1, 10000000], gtsmooth: ['smooth', 1, 100] }[f];
  if (!num) return false;
  mdEdit(() => { const v = parseInt(t.value, 10); z[num[0]] = Math.max(num[1], Math.min(num[2], Number.isFinite(v) ? v : num[1])); });
  return true;
}

// ---------- окно разметки: сначала 4 угла линейки, потом зона указателя ----------
function gtMarkHint(m) {
  if (!m.polys.length) {
    const has = ((mvCam(m.cam) || {}).zones || []).some((z) => z.type === 'gate');
    return 'Шаг 1 из 2. Линейка: кликните четыре угла. Нижняя сторона — это 0 %, верхняя — 100 %; ведите обе вдоль меток линейки (по ним программа понимает наклон). ПКМ — убрать точку.' +
      (has ? ' Шибер у камеры уже есть: новая разметка заменит линейку и зону, цвет и сценарий останутся.' : '');
  }
  return 'Шаг 2 из 2. Обведите зону, где ходит указатель (рядом с линейкой, на всю длину хода). Другое красное в неё попадать не должно. Enter — готово (минимум 3 точки).';
}

// клик в окне разметки: четвёртый угол замыкает линейку
function gtMarkClicked(m) {
  if (m.polys.length || m.pts.length < 4) return;
  m.polys.push(gtOrder(m.pts));
  m.pts = [];
  m.cursor = null;
}

// линейка стоит вертикально (та же проверка, что в сервисе): иначе он отверг бы весь конфиг уже после закрытия окна
function gtRulerOk(r) {
  const [bl, br, tr, tl] = r;
  return Math.min(bl[1], br[1]) - Math.max(tl[1], tr[1]) >= 4 && br[0] - bl[0] >= 1 && tr[0] - tl[0] >= 1;
}

function gtMarkSave(m, cam, fw, fh) {
  if (!m.polys.length || m.pts.length < 3) return;
  if (!gtRulerOk(m.polys[0])) {
    mdEl('mmHint').textContent = 'Линейка должна стоять вертикально: два угла внизу, два вверху. Нажимайте «Убрать точку», пока не вернётесь к линейке, и отметьте углы заново.';
    return;
  }
  mdEdit(() => {
    const fs = cam.frame_size || [fw, fh];
    cam.frame_size = fs;
    const sc = (p) => [Math.round(p[0] * fs[0] / fw), Math.round(p[1] * fs[1] / fh)];
    const geo = { ruler: gtOrder(m.polys[0].map(sc)), points: m.pts.map(sc) };
    const old = cam.zones.find((z) => z.type === 'gate');     // шибер на камере один: повторная разметка меняет только геометрию
    if (old) { Object.assign(old, geo, { enabled: true }); return; }
    let n = 1;
    while (cam.zones.some((z) => z.id === 'gate' + n) || (cam.groups || []).some((q) => q.id === 'gate' + n)) n += 1;
    cam.zones.push(Object.assign({ id: 'gate' + n, name: 'Шибер', enabled: true, type: 'gate', metric: 'flow', on: 0, off: 0, invert: false, track_flow: false, out: null,
      smooth: 5, flip: false, hold_s: 10, min_area: 200, red_hsv: GT_RED_DEF.slice(), red_more: [], red_not: [] }, geo));
  }, true);
  log.success('КЗ: шибер размечен', { serial: cam.id });
  closeMarkModal();
}

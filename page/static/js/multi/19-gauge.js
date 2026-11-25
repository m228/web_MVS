// ---------- КЗ · уровнемер: значение 0–100 % по картинке ----------
// Два вида с общими разметкой, карточкой, плиткой, удержанием и сценарием в ПЛК:
//   столбик (зона tube) — уровень виден цветом: от начала шкалы «заполнено», дальше «пусто» (магнитный указатель, стекло). Цвета берутся пипеткой.
//   стрелка (зона gate, бывший шибер) — линейка (ruler: четыре угла) и цветной указатель рядом (points — где он ходит), цвет — как у палки вала.
// Считает motion_service/motion_engine.py (TubeRT, GateRT); тут разметка, карточка на вкладке «Зоны · тренды», пипетка и рисунок на плитке.
const GG_TYPES = ['gate', 'tube'];
const GG_KIND = { tube: 'столбик', gate: 'стрелка' };
const GG_DIRS = [['up', 'снизу вверх'], ['down', 'сверху вниз'], ['right', 'слева направо'], ['left', 'справа налево']];
const GG_CLASS = { fill: 'заполнено', empty: 'пусто' };
const GG_RED_DEF = [10, 165, 100, 70];                       // как GATE_RED_HSV в сервисе
const GG_COL = '#38bdf8', GG_FILL = 'rgba(74,222,128,.38)', GG_HOLD = 'rgba(251,191,36,.42)';
const GG_HOLD_DEF = 60, GG_TOL_DEF = 12, GG_QUALITY_DEF = 80, GG_MAX_COLORS = 6, GG_L_WEIGHT = 0.5;      // как в сервисе
const GG_PREV_W = 480, GG_PIP_W = 900, GG_PIP_EVERY = 5;

const ggIs = (z) => !!z && GG_TYPES.includes(z.type);
const ggFmt = (v) => Number(v).toFixed(1).replace('.', ',') + ' %';
const ggNum = (v, d) => (v == null ? d : v);
const ggNoData = (v) => v < 0 || v === 65535;                // −1 — нет данных; сервис движения до 3.0.44 слал 65535
const ggHoldText = (lz) => (lz.hold_left == null ? 'держу' : 'держу ' + lz.hold_left + ' с');      // старый сервис срока не шлёт

// ---------- геометрия ----------
// «вдоль шкалы» (растёт от 0 % к 100 %) и «поперёк» — как _uv в сервисе
const ggV = (p, dir) => (dir === 'down' ? p[1] : dir === 'right' ? p[0] : dir === 'left' ? -p[0] : -p[1]);
const ggU = (p, dir) => (dir === 'right' || dir === 'left' ? p[1] : p[0]);

// четыре угла в любом порядке → [начало A, начало B, конец B, конец A]: боковые стороны шкалы — 0→3 и 1→2 (как в tube_grid сервиса);
// при направлении «снизу вверх» это [низ слева, низ справа, верх справа, верх слева] — порядок линейки стрелки (order_ruler)
function ggQuad(pts, dir) {
  const by = pts.slice().sort((a, b) => ggV(a, dir) - ggV(b, dir));
  const s = by.slice(0, 2).sort((a, b) => ggU(a, dir) - ggU(b, dir)), e = by.slice(2).sort((a, b) => ggU(a, dir) - ggU(b, dir));
  return [s[0], s[1], e[1], e[0]];
}

// два клика по диагонали → прямоугольник
function ggRect(p, q) {
  const x0 = Math.min(p[0], q[0]), x1 = Math.max(p[0], q[0]), y0 = Math.min(p[1], q[1]), y1 = Math.max(p[1], q[1]);
  return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
}
const ggShape = (pts) => (pts.length === 2 ? ggRect(pts[0], pts[1]) : pts.slice());

// линия уровня четырёхугольника на доле t: точки на двух боковых сторонах
function ggLine(q, t) {
  const mix = (a, b) => [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])];
  return [mix(q[0], q[3]), mix(q[1], q[2])];
}

// то же для любой разметки столбика: q — четырёхугольник, иначе описанный прямоугольник по направлению (многоугольник обрезается им на рисунке)
function ggFrame(pts, dir) {
  if (pts.length === 4) return ggQuad(pts, dir);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return ggQuad(ggRect([Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]), dir);
}

const ggPath = (ctx, pts) => { ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]))); ctx.closePath(); };

// ---------- плитка ----------
function drawGaugeOnTile(ctx, z, cam, g, lz, problem) {
  const tube = z.type === 'tube';
  if (tube ? !(z.points && z.points.length >= 3) : !(z.ruler && z.ruler.length === 4)) return;
  const on = (pts) => zonePointsOnTile({ points: pts }, cam, g);
  // удержанное значение показываем и когда с камерой беда: сервис ещё держит его и пишет в ПЛК
  const ok = !!lz && !!lz.valid && (!problem || !!lz.held);
  const zone = on(z.points);
  const dir = tube ? z.direction || 'up' : 'up';
  const frame = tube ? ggFrame(zone, dir) : on(z.ruler);
  const text = (s, x, y, align, col) => {
    ctx.textAlign = align || 'left'; ctx.lineWidth = 4; ctx.strokeStyle = '#000'; ctx.strokeText(s, x, y);
    ctx.fillStyle = col || (ok ? '#fff' : '#cbd5e1'); ctx.fillText(s, x, y); ctx.textAlign = 'left'; ctx.lineWidth = 2;
  };
  const outline = tube ? (zone.length === 4 ? frame : zone) : frame;
  ctx.save();
  if (!tube) { ctx.lineWidth = 1; ctx.setLineDash([5, 4]); ctx.strokeStyle = 'rgba(250,204,21,.85)'; ggPath(ctx, zone); ctx.stroke(); ctx.setLineDash([]); }
  const start = !tube && z.flip ? 1 : 0;
  if (ok) {                                                  // заполнение: от «0 %» до уровня
    const base = ggLine(frame, start), lvl = ggLine(frame, lz.t);
    ctx.save();
    if (tube && zone.length !== 4) { ggPath(ctx, zone); ctx.clip(); }
    ctx.fillStyle = lz.held ? GG_HOLD : GG_FILL; ggPath(ctx, [base[0], base[1], lvl[1], lvl[0]]); ctx.fill();
    ctx.restore();
  }
  ctx.lineWidth = 1.5; ctx.strokeStyle = ok ? GG_COL : '#9ca3af'; ggPath(ctx, outline); ctx.stroke();
  if (ok) {                                                  // черта уровня выступает за разметку: на узкой колонке внутри её не разглядеть
    const [p, q] = ggLine(frame, lz.t), len = Math.hypot(q[0] - p[0], q[1] - p[1]) || 1, k = tube ? 8 / len : 0;
    const from = [p[0] - k * (q[0] - p[0]), p[1] - k * (q[1] - p[1])], to = [q[0] + k * (q[0] - p[0]), q[1] + k * (q[1] - p[1])];
    [['#000', 5], ['#fff', 2.5]].forEach(([col, w]) => { ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath(); ctx.moveTo(from[0], from[1]); ctx.lineTo(to[0], to[1]); ctx.stroke(); });
    ctx.lineWidth = 2;
  }
  const xs = outline.map((p) => p[0]), ys = outline.map((p) => p[1]);
  ctx.font = 'bold 13px sans-serif';
  text(z.name, Math.min(...xs), Math.max(14, Math.min(...ys) - 6));
  if (tube) {                                                // число — сбоку на высоте уровня (у лежачей шкалы — под ней)
    const flat = dir === 'right' || dir === 'left';
    const lvl = ggLine(frame, ok ? lz.t : 0.5);
    const mid = [(lvl[0][0] + lvl[1][0]) / 2, (lvl[0][1] + lvl[1][1]) / 2];
    const right = Math.max(...xs) + 70 < ctx.canvas.width;
    const x = flat ? mid[0] : right ? Math.max(...xs) + 12 : Math.min(...xs) - 12;
    const y = flat ? Math.max(...ys) + 20 : mid[1] + 6;
    const al = flat ? 'center' : right ? 'left' : 'right';
    ctx.font = 'bold 18px sans-serif';
    text(ok ? ggFmt(lz.pct) : '— %', x, y, al);
    if (ok && lz.held) { ctx.font = 'bold 12px sans-serif'; text(ggHoldText(lz), x, y + 16, al, '#fbbf24'); }
    ctx.restore();
    return;
  }
  if (!ok) { ctx.font = 'bold 18px sans-serif'; const m = ggLine(frame, 0.5)[0]; text('— %', m[0] - 6, m[1], 'right'); ctx.restore(); return; }
  const [l, r] = ggLine(frame, lz.t);
  let right = true;                                          // указатель справа от линейки (обычно) или слева
  if (lz.ptr && lz.box) {
    const [p0, p1] = on([[lz.box[0], lz.box[1]], [lz.box[2], lz.box[3]]]), c = on([lz.ptr])[0];
    right = c[0] >= (l[0] + r[0]) / 2;
    const from = [right ? p0[0] - 3 : p1[0] + 3, c[1]], to = right ? r : l;
    const col = lz.held ? '#fbbf24' : '#4ade80';
    ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 2;
    ctx.strokeRect(p0[0] - 3, p0[1] - 3, p1[0] - p0[0] + 6, p1[1] - p0[1] + 6);
    if ((right && from[0] > to[0]) || (!right && from[0] < to[0])) {      // стрелка от указателя к линейке
      const a = Math.atan2(to[1] - from[1], to[0] - from[0]), h = 10;
      ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(from[0], from[1]); ctx.lineTo(to[0], to[1]); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(to[0], to[1]); ctx.lineTo(to[0] - h * Math.cos(a - 0.45), to[1] - h * Math.sin(a - 0.45)); ctx.lineTo(to[0] - h * Math.cos(a + 0.45), to[1] - h * Math.sin(a + 0.45)); ctx.closePath(); ctx.fill();
    }
  }
  ctx.font = 'bold 18px sans-serif';                         // число — с другой стороны линейки, на высоте уровня
  const side = right ? l : r, x = side[0] + (right ? -6 : 6), al = right ? 'right' : 'left';
  text(ggFmt(lz.pct), x, side[1] + 6, al);
  if (lz.held) { ctx.font = 'bold 12px sans-serif'; text(ggHoldText(lz), x, side[1] + 22, al, '#fbbf24'); }
  ctx.restore();
}

// подсказка при наведении на плитку
function ggTipText(z, lz, problem) {
  if (lz && lz.valid && (!problem || lz.held)) return z.name + ' · ' + ggFmt(lz.pct) + (lz.held ? ' (' + ggHoldText(lz) + ' последнее: ' + (lz.reason || problem || 'не видно') + ')' : '');
  return z.name + ' — ' + (problem || (lz && lz.reason) || 'нет данных');
}

// ---------- карточка на вкладке «Зоны · тренды» ----------
function ggScenarios(camId, zoneId) {
  return ((MV.cfg && MV.cfg.scenarios) || []).filter((s) => s.sources.some((x) => x.camera === camId && x.zone === zoneId));
}

function ggPlcHtml(cam, z, a) {
  const list = ggScenarios(cam.id, z.id);
  const txt = list.map((s) => escapeHtml(s.name) + ' → ' + (s.out ? 'рег. ' + s.out.address : 'регистр не задан')).join('; ');
  return `<div class="md-zone__row"><span class="md-lbl" title="В ПЛК значение пишет сценарий (вкладка «ПЛК»): × 10 (54,3 % → 543) или как есть, целые проценты (54) — выбирается в сценарии; нет данных — −1 (переменную в ПЛК объявить INT: в UINT это 65535)">в ПЛК</span>` +
    (list.length ? `<span class="net-hint">${txt}</span>` : `<button type="button" class="btn-secondary md-x" ${a} data-f="ggsc" title="Создать сценарий записи этого уровнемера и открыть его: останется задать регистр">создать сценарий</button>`) + '</div>';
}

// блок «Цвета» стрелки: те же таблица, пипетка и «не ловить», что у конца палки (16-shafts.js)
function ggPointerColorsHtml(cam, z, a) {
  const k = escapeHtml(cam.id + '/' + z.id);
  const head = SH_HSV_COLS.red.map(([lbl, , , tip]) => `<div class="sh-th" title="${escapeHtml(tip)}">${lbl}</div>`).join('');
  const rows = (z.red_more || []).map((_, n) => shExtraRowHtml('red', z, a, n)).join('') + (z.red_not || []).map((_, n) => shNotRowHtml('red', z, a, n)).join('');
  // обёртка с id: по нему панель помнит, раскрыт ли блок, после перерисовки (у двух камер уровнемеры называются одинаково)
  return `<div id="md-gg-${escapeHtml(cam.id + '-' + z.id)}"><details class="md-det md-gauge__col"><summary>Цвета · ${escapeHtml(z.name)}</summary>
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

const ggView = {};                                           // уровнемер → 'mask' | 'raw': что на маленькой картинке столбика

function ggColorRowHtml(z, a, w) {
  const chips = (z[w + '_colors'] || []).map((c, n) => `<span class="sh-chip" title="L ${c[0]}, a ${c[1]}, b ${c[2]}"><i class="sh-dot sh-dot--lg" style="background:${ggLabCss(c)}"></i>${n + 1}` +
    `<button type="button" class="gg-x" ${a} data-f="ggchipdel" data-w="${w}" data-n="${n}" title="Убрать этот образец" aria-label="Убрать образец">×</button></span>`).join('');
  return `<div class="gg-crow"><b>${GG_CLASS[w]}</b><div class="gg-chips">${chips || '<span class="net-hint">цвет не взят</span>'}</div>` +
    `<button type="button" class="btn-secondary md-x" ${a} data-f="ggpip" data-w="${w}" title="Откроется большой кадр: кликните по цвету «${GG_CLASS[w]}» на колонке. Образцов может быть до ${GG_MAX_COLORS}: в тени, на свету, вечером">пипетка</button></div>`;
}

function ggTubeColorsHtml(cam, z, a) {
  const k = escapeHtml(cam.id + '/' + z.id);
  const view = ggView[cam.id + '/' + z.id] || 'mask';
  const need = !(z.fill_colors || []).length || !(z.empty_colors || []).length;
  const tol = ggNum(z.tol, GG_TOL_DEF);
  const seg = [['mask', 'маска', 'Красное — «заполнено», голубое — «пусто», тёмное — чужое; белая черта — уровень на этом кадре'], ['raw', 'кадр', 'Сам кадр без маски']]
    .map(([v, t, tip]) => `<button type="button" class="${view === v ? 'on' : ''}" ${a} data-f="ggview" data-v="${v}" title="${tip}">${t}</button>`).join('');
  return `<div id="md-gg-${escapeHtml(cam.id + '-' + z.id)}"><details class="md-det md-gauge__col"${need ? ' open' : ''}><summary>Цвета · ${escapeHtml(z.name)}</summary>
    <div class="sh-col gg-col" data-ggcol="${k}" data-cam="${escapeHtml(cam.id)}" data-zone="${escapeHtml(z.id)}">
      <div class="sh-col__body">
        <div class="sh-prev gg-prev"><img alt="" data-ggprev="${k}" /><div class="sh-seg">${seg}</div></div>
        <div class="gg-side">${ggColorRowHtml(z, a, 'fill')}${ggColorRowHtml(z, a, 'empty')}
          <label class="gg-tol" title="Насколько цвет точки может отличаться от образца. Фон краснеет на маске — уменьшите; колонка вся тёмная — увеличьте">допуск цвета
            <input type="range" class="sh-rng" style="--trk:linear-gradient(90deg,#334155,#a855f7)" ${a} data-f="ggtol" min="3" max="40" step="1" value="${tol}" /><output>${tol}</output></label>
          <div class="net-hint">${need ? 'Возьмите пипеткой по одному цвету «заполнено» и «пусто» — без них значения нет.' : 'Внутри разметки колонка должна делиться на красную и голубую части, а фон — не краситься: иначе закрытая колонка прочтётся неверно. Лучше четыре точки вплотную к колонке.'}</div></div>
      </div>
      <div class="net-hint sh-col__note" data-ggprevn="${k}"></div>
    </div></details></div>`;
}

function ggCardHtml(cam, z, i) {
  const a = `data-md="z" data-cam="${escapeHtml(cam.id)}" data-i="${i}"`;
  const k = escapeHtml(cam.id + '-' + z.id);
  const tube = z.type === 'tube';
  const own = tube
    ? `<span class="md-lbl" title="Сколько % строк колонки должно сходиться с картиной «до уровня заполнено, дальше пусто». Меньше — колонку не видно (закрыли, пар, блик, не те цвета): значение не берём. Сколько сходится сейчас — в строке выше">качество от, %</span>${mdNum(a + ' data-f="ggquality"', ggNum(z.min_quality, GG_QUALITY_DEF), 'min="50" max="100" step="1"')}`
    : `<span class="md-lbl" title="Пятно цвета меньше этого числа точек кадра за указатель не принимаем. Сколько точек у пятна сейчас — в строке выше">пятно от</span>${mdNum(a + ' data-f="ggarea"', ggNum(z.min_area, 200), 'min="1" step="50"')}`;
  const way = tube
    ? `<span class="md-lbl" title="Где на разметке 0 % и куда растёт уровень">растёт</span><select ${a} data-f="ggdir">${mdSel(GG_DIRS, z.direction || 'up')}</select>`
    : `<label class="md-lbl md-gauge__chk" title="Обычно 0 % — нижняя сторона линейки. С галочкой — верхняя"><input type="checkbox" ${a} data-f="ggflip"${z.flip ? ' checked' : ''} /> 0 % сверху</label>`;
  return `<div class="md-zone md-gauge">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><input type="text" ${a} data-f="name" value="${escapeHtml(z.name)}" title="${escapeHtml(z.id)}" />
        <span class="gg-kind" title="Вид уровнемера">${GG_KIND[z.type]}</span><b class="md-gauge__v" id="md-zv-${k}" title="Уровень, % шкалы">–</b><span class="md-sp"></span>
        <button type="button" class="btn-secondary md-x" ${a} data-f="ggremark" title="Разметить заново: цвета, настройки и сценарий останутся">разметить</button>
        <button type="button" class="btn-secondary md-x" ${a} data-f="del" title="Удалить уровнемер">✕</button></div>
      <div class="md-zstat" id="md-st-${k}"></div>
      <div class="md-zone__row"><span class="md-lbl" title="Не видно (человек, пар, пропала картинка): столько секунд держим последнее значение, потом «нет данных» (−1)">держать, с</span>${mdNum(a + ' data-f="gghold"', ggNum(z.hold_s, GG_HOLD_DEF), 'min="0" max="3600" step="5"')}
        ${own}
        <span class="md-lbl" title="Значение — медиана стольких последних кадров: гасит дрожание, но запаздывает примерно на половину этих кадров">кадров</span>${mdNum(a + ' data-f="ggsmooth"', ggNum(z.smooth, 5), 'min="1" max="100" step="1"')}</div>
      <div class="md-zone__row">${way}</div>
      ${ggPlcHtml(cam, z, a)}
    </div>
    <div class="md-zone__chart"><canvas id="md-ch-${k}" height="84"></canvas></div>
  </div>${tube ? ggTubeColorsHtml(cam, z, a) : ggPointerColorsHtml(cam, z, a)}`;
}

// число рядом с названием в карточке
const ggValueText = (z) => (z.valid ? ggFmt(z.pct) : '— %');

// строка под названием: что сейчас и сводка за период
function ggStatHtml(z, cur, st) {
  const s = st && st.flow;
  const sum = s ? ` · ср ${ggFmt(s.avg)} · мин ${ggFmt(s.min)} · макс ${ggFmt(s.max)}` : '';
  if (!cur) return 'нет данных';
  const now = z.type === 'tube' ? `сходится ${cur.quality} % строк` : `пятно ${cur.area} точек`;
  const why = escapeHtml(cur.reason || '');
  if (!cur.valid) return `<span style="color:var(--warning)">нет данных (−1): ${why || 'значение не читается'}</span> · ${now}${sum}`;
  return (cur.held ? `<span style="color:var(--warning)">${ggHoldText(cur)} последнее: ${why || 'не видно'}</span>` : now) + sum;
}

function ggDrawChart(cv, d, t, ok) {
  const vals = d.flow.map((v, i) => (d.bit[i] ? v : null));
  mdDrawSeries(cv, t, vals, ok, 0, 100, { h: cv.height || 84, tip: (i) => (vals[i] == null ? 'нет данных' : 'уровень <b>' + ggFmt(vals[i]) + '</b>') });
}

// поля карточки; true — событие обработано
function ggOnZone(t, cam, z) {
  const f = t.dataset.f;
  if (f === 'hsvreset') { mdEdit(() => { z.red_hsv = GG_RED_DEF.slice(); z.red_more = []; z.red_not = []; }, true); return true; }
  if (f === 'ggsc') {                                         // сценарий без регистра: пока человек не задал адрес, в ПЛК ничего не уходит
    const list = (MV.cfg.scenarios = MV.cfg.scenarios || []);
    let n = 1;
    while (list.some((s) => s.id === 's' + n)) n += 1;
    MD.open['sc-s' + n] = true;
    mdEdit(() => list.push({ id: 's' + n, name: cam.name + ' · ' + z.name, rule: 'value', signal: 'move', sources: [{ camera: cam.id, zone: z.id }], out: null }), false);
    MD.tab = 'plc'; MD.key = ''; renderMotionDock();
    return true;
  }
  if (f === 'ggremark') { openMarkModal('gauge', { camera: cam.id, replace: z.id }); return true; }
  if (f === 'ggflip') { mdEdit(() => { z.flip = t.checked; }); return true; }
  if (f === 'ggdir') { mdEdit(() => { z.direction = t.value; }, true); return true; }
  if (f === 'ggview') {
    const block = t.closest('[data-ggcol]');
    ggView[block.dataset.ggcol] = t.dataset.v;
    t.closest('.sh-seg').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === t));
    ggPrevLoad(block, true);
    return true;
  }
  if (f === 'ggpip') { ggPipOpen(cam.id, z.id, t.dataset.w); return true; }
  if (f === 'ggchipdel') { mdEdit(() => { (z[t.dataset.w + '_colors'] || []).splice(Number(t.dataset.n), 1); }, true); return true; }
  if (f === 'ggtol') { mdEdit(() => { z.tol = Math.max(1, Math.min(60, Number(t.value) || GG_TOL_DEF)); }); return true; }
  const num = { gghold: ['hold_s', 0, 3600], ggarea: ['min_area', 1, 10000000], ggsmooth: ['smooth', 1, 100], ggquality: ['min_quality', 50, 100] }[f];
  if (!num) return false;
  mdEdit(() => { const v = parseInt(t.value, 10); z[num[0]] = Math.max(num[1], Math.min(num[2], Number.isFinite(v) ? v : num[1])); });
  return true;
}

// ---------- маленькая картинка «что видит детектор» (столбик) ----------
const ggPrevAt = {};
const ggColorsStr = (list) => (list || []).map((c) => c.join(',')).join(';');
function ggPrevLoad(block, force) {
  const key = block.dataset.ggcol, img = block.querySelector('img[data-ggprev]'), note = block.querySelector('[data-ggprevn]');
  const cam = mvCam(block.dataset.cam), z = cam && cam.zones.find((q) => q.id === block.dataset.zone);
  if (!img || !z || (!force && Date.now() - (ggPrevAt[key] || 0) < 4000)) return;
  ggPrevAt[key] = Date.now();
  const slider = block.querySelector('input[data-f="ggtol"]');
  const q = new URLSearchParams({ camera: block.dataset.cam, zone: block.dataset.zone, w: String(GG_PREV_W), raw: ggView[key] === 'raw' ? '1' : '0',
    fill: ggColorsStr(z.fill_colors), empty: ggColorsStr(z.empty_colors), tol: slider ? slider.value : String(ggNum(z.tol, GG_TOL_DEF)), t: String(Date.now()) });
  fetch('/api/motion/rod_mask?' + q).then((r) => (r.ok ? r.blob() : Promise.reject(r.status))).then((b) => {
    const old = img.src;
    img.src = URL.createObjectURL(b);
    if (old && old.startsWith('blob:')) URL.revokeObjectURL(old);
    if (note) note.textContent = '';
  }).catch(() => { if (note) note.textContent = 'картинки нет: камера переподключается, не в сети или сервис движения старой версии (после новой разметки подождите несколько секунд)'; });
}
function ggPrevPaint() {
  document.querySelectorAll('[data-ggcol]').forEach((b) => { if (!b.closest('details:not([open])')) ggPrevLoad(b, false); });     // свёрнутый блок кадры не тянет
}
let ggSlideTimer = 0;
function ggOnSlide(t) {                                      // ползунок допуска двигают: значение рядом + быстрое обновление картинки
  const o = t.parentElement && t.parentElement.querySelector('output');
  if (o) o.textContent = t.value;
  const block = t.closest('[data-ggcol]');
  if (!block) return;
  clearTimeout(ggSlideTimer);
  ggSlideTimer = setTimeout(() => ggPrevLoad(block, true), 250);
}

// ---------- цвет в Lab (как cv2.COLOR_BGR2Lab: sRGB, белая точка D65) ----------
const ggLin = (c) => { const v = c / 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
function ggRgb2Lab(r, g, b) {
  const R = ggLin(r), G = ggLin(g), B = ggLin(b);
  const X = (0.412453 * R + 0.357580 * G + 0.180423 * B) / 0.950456;
  const Y = 0.212671 * R + 0.715160 * G + 0.072169 * B;
  const Z = (0.019334 * R + 0.119193 * G + 0.950227 * B) / 1.088754;
  const f = (v) => (v > 0.008856 ? Math.cbrt(v) : 7.787 * v + 16 / 116);
  return [Y > 0.008856 ? 116 * Math.cbrt(Y) - 16 : 903.3 * Y, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))];
}
function ggLabRgb(c) {
  const [L, a, b] = c;
  const fy = (L + 16) / 116, fx = fy + a / 500, fz = fy - b / 200;
  const inv = (f) => (f * f * f > 0.008856 ? f * f * f : (f - 16 / 116) / 7.787);
  const X = inv(fx) * 0.950456, Y = L > 7.9996 ? fy * fy * fy : L / 903.3, Z = inv(fz) * 1.088754;
  const gm = (v) => { const q = Math.max(0, Math.min(1, v)); return Math.round(255 * (q <= 0.0031308 ? 12.92 * q : 1.055 * Math.pow(q, 1 / 2.4) - 0.055)); };
  return [gm(3.240479 * X - 1.53715 * Y - 0.498535 * Z), gm(-0.969256 * X + 1.875991 * Y + 0.041556 * Z), gm(0.055648 * X - 0.204043 * Y + 1.057311 * Z)];
}
const ggLabCss = (c) => `rgb(${ggLabRgb(c).join(',')})`;
const ggDist = (lab, list) => (list || []).reduce((d, c) => Math.min(d, Math.hypot((lab[0] - c[0]) * GG_L_WEIGHT, lab[1] - c[1], lab[2] - c[2])), Infinity);
// чем детектор считает цвет: 'fill' | 'empty' | '' (чужой) — как TubeRT.classify
function ggMatch(z, lab) {
  const tol = ggNum(z.tol, GG_TOL_DEF), f = ggDist(lab, z.fill_colors), e = ggDist(lab, z.empty_colors);
  return f < tol && f <= e ? 'fill' : e < tol && e < f ? 'empty' : '';
}

// ---------- пипетка столбика: большой кадр, лупа под курсором и «чем это сейчас считается» ----------
const ggPip = { el: null, cam: '', zone: '', w: 'fill', cv: null, ctx: null, lp: null, lctx: null, timer: 0, left: GG_PIP_EVERY, freeze: false, tint: false, loaded: false, busy: false, msgT: 0 };
const ggPipZone = () => { const cam = mvCam(ggPip.cam); return cam && cam.zones.find((q) => q.id === ggPip.zone); };

function ggPipOpen(camId, zoneId, which) {
  ggPipClose();
  const cam = mvCam(camId), z = cam && cam.zones.find((q) => q.id === zoneId);
  if (!z) return;
  Object.assign(ggPip, { cam: camId, zone: zoneId, w: which === 'empty' ? 'empty' : 'fill', left: GG_PIP_EVERY, freeze: false, tint: false, loaded: false, busy: false });
  const el = document.createElement('div');
  el.className = 'sh-pipov';
  el.innerHTML = `<div class="sh-pipov__box" role="dialog" aria-label="Пипетка уровнемера">
    <div class="sh-pipov__head"><b>Пипетка · ${escapeHtml(z.name)}</b><span class="net-hint">${escapeHtml(cam.name)}</span>
      <div class="sh-seg" data-ggw><button type="button" data-w="fill">заполнено</button><button type="button" data-w="empty">пусто</button></div>
      <span class="md-sp"></span><span class="net-hint" data-ggclock></span>
      <label class="sh-chk"><input type="checkbox" data-ggfreeze /> стоп-кадр</label>
      <label class="sh-chk"><input type="checkbox" data-ggtint /> показать, что ловится</label>
      <button type="button" class="btn-secondary md-x" data-ggnow>обновить кадр</button>
      <button type="button" class="btn-secondary md-x" data-ggdone>готово</button></div>
    <div class="sh-pipov__stage"><canvas data-pipcv></canvas><canvas class="sh-pipov__tint" data-ggtc></canvas>
      <div class="sh-loupe" data-ggloupe hidden><canvas width="120" height="120"></canvas><div class="sh-loupe__tag" data-ggtag></div></div>
      <div class="sh-pipov__msg" data-ggmsg>загружаю кадр…</div></div>
    <div class="sh-pipov__foot" data-ggfoot></div></div>`;
  document.body.appendChild(el);
  ggPip.el = el;
  ggPip.cv = el.querySelector('[data-pipcv]');
  ggPip.ctx = ggPip.cv.getContext('2d', { willReadFrequently: true });
  ggPip.lp = el.querySelector('[data-ggloupe] canvas');
  ggPip.lctx = ggPip.lp.getContext('2d');
  ggPip.lctx.imageSmoothingEnabled = false;
  el.addEventListener('mousedown', (e) => { if (e.target === el) ggPipClose(); });
  el.querySelector('[data-ggdone]').addEventListener('click', ggPipClose);
  el.querySelector('[data-ggnow]').addEventListener('click', () => { ggPip.left = GG_PIP_EVERY; ggPipLoad(); });
  el.querySelector('[data-ggfreeze]').addEventListener('change', (e) => { ggPip.freeze = e.target.checked; ggPipClock(); });
  el.querySelector('[data-ggtint]').addEventListener('change', (e) => { ggPip.tint = e.target.checked; ggPipTint(); });
  el.querySelectorAll('[data-ggw] button').forEach((b) => b.addEventListener('click', () => { ggPip.w = b.dataset.w; ggPipFoot(); }));
  ggPip.cv.addEventListener('mousemove', ggPipMove);
  ggPip.cv.addEventListener('mouseleave', () => { const l = el.querySelector('[data-ggloupe]'); if (l) l.hidden = true; });
  ggPip.cv.addEventListener('click', ggPipClick);
  document.addEventListener('keydown', ggPipKey);
  ggPipFoot();
  ggPipLoad();
  ggPip.timer = setInterval(() => { if (!ggPip.freeze && --ggPip.left <= 0) { ggPip.left = GG_PIP_EVERY; ggPipLoad(); } ggPipClock(); }, 1000);
}
function ggPipKey(e) { if (e.key === 'Escape') ggPipClose(); }
function ggPipClose() {
  clearInterval(ggPip.timer);
  clearTimeout(ggPip.msgT);
  document.removeEventListener('keydown', ggPipKey);
  if (ggPip.el) ggPip.el.remove();
  ggPip.el = null;
}
function ggPipClock() {
  const c = ggPip.el && ggPip.el.querySelector('[data-ggclock]');
  if (c) c.textContent = ggPip.freeze ? 'кадр стоит' : 'кадр обновится через ' + ggPip.left + ' с';
}
function ggPipFoot() {
  const z = ggPipZone();
  if (!ggPip.el || !z) return;
  ggPip.el.querySelectorAll('[data-ggw] button').forEach((b) => b.classList.toggle('on', b.dataset.w === ggPip.w));
  const chips = (w) => (z[w + '_colors'] || []).map((c, i) => `<span class="sh-chip"><i class="sh-dot sh-dot--lg" style="background:${ggLabCss(c)}"></i>${GG_CLASS[w]} ${i + 1}</span>`).join('');
  ggPip.el.querySelector('[data-ggfoot]').innerHTML = (chips('fill') + chips('empty') || '<span class="net-hint">образцов пока нет</span>') +
    `<span class="net-hint">Берём цвет «${GG_CLASS[ggPip.w]}»: наведите на колонку и кликните. «Показать, что ловится» красит весь кадр, а считаются только точки внутри разметки. Убрать образец — крестиком в карточке.</span>`;
}
async function ggPipLoad() {
  if (!ggPip.el || ggPip.busy) return;
  ggPip.busy = true;
  const el = ggPip.el, msg = el.querySelector('[data-ggmsg]');
  try {
    const r = await fetch('/api/motion/rod_mask?' + new URLSearchParams({ camera: ggPip.cam, zone: ggPip.zone, raw: '1', w: String(GG_PIP_W), t: String(Date.now()) }));
    if (!r.ok) throw new Error(r.status);
    const bmp = await createImageBitmap(await r.blob());
    if (ggPip.el !== el) return;
    ggPip.cv.width = bmp.width; ggPip.cv.height = bmp.height;
    ggPip.ctx.drawImage(bmp, 0, 0);
    const tc = el.querySelector('[data-ggtc]');
    tc.width = bmp.width; tc.height = bmp.height;
    ggPip.loaded = true;
    msg.hidden = true;
    ggPipTint();
  } catch (e) {
    if (ggPip.el === el && !ggPip.loaded) { msg.hidden = false; msg.textContent = 'Кадра пока нет: камера подключается, не в сети или сервис движения старой версии.'; }
  } finally { ggPip.busy = false; }
}
// подсветка того, что ловится сейчас: красным «заполнено», голубым «пусто»
function ggPipTint() {
  const tc = ggPip.el && ggPip.el.querySelector('[data-ggtc]'), z = ggPipZone();
  if (!tc || !z || !ggPip.loaded) return;
  const t = tc.getContext('2d');
  t.clearRect(0, 0, tc.width, tc.height);
  if (!ggPip.tint) return;
  const d = ggPip.ctx.getImageData(0, 0, tc.width, tc.height).data, out = t.createImageData(tc.width, tc.height), o = out.data;
  const seen = new Map();                                    // одинаковые цвета считаем один раз
  for (let i = 0; i < d.length; i += 4) {
    const key = (d[i] << 16) | (d[i + 1] << 8) | d[i + 2];
    let m = seen.get(key);
    if (m === undefined) { m = ggMatch(z, ggRgb2Lab(d[i], d[i + 1], d[i + 2])); seen.set(key, m); }
    if (m) { o[i] = m === 'fill' ? 255 : 40; o[i + 1] = m === 'fill' ? 40 : 200; o[i + 2] = m === 'fill' ? 40 : 235; o[i + 3] = 190; }
  }
  t.putImageData(out, 0, 0);
}
function ggPipPos(e) {
  const r = ggPip.cv.getBoundingClientRect();
  return { x: Math.floor((e.clientX - r.left) * ggPip.cv.width / r.width), y: Math.floor((e.clientY - r.top) * ggPip.cv.height / r.height) };
}
// цвет под курсором: медиана по окну 5×5 в Lab — колонка узкая, окно больше захватило бы фон
function ggPipSample(cx, cy) {
  const W = ggPip.cv.width, H = ggPip.cv.height, r = 2;
  const x0 = Math.max(0, cx - r), y0 = Math.max(0, cy - r), w = Math.min(W, cx + r + 1) - x0, h = Math.min(H, cy + r + 1) - y0;
  if (w < 1 || h < 1) return null;
  const d = ggPip.ctx.getImageData(x0, y0, w, h).data, px = [];
  for (let i = 0; i < w * h; i++) px.push(ggRgb2Lab(d[i * 4], d[i * 4 + 1], d[i * 4 + 2]));
  const mid = (c) => { const a = px.map((p) => p[c]).sort((p, q) => p - q), n = a.length; return n % 2 ? a[n >> 1] : (a[(n >> 1) - 1] + a[n >> 1]) / 2; };
  return [0, 1, 2].map((c) => Math.round(mid(c) * 10) / 10);
}
function ggPipMove(e) {
  const z = ggPipZone();
  if (!ggPip.loaded || !z) return;
  const { x, y } = ggPipPos(e), loupe = ggPip.el.querySelector('[data-ggloupe]');
  const lab = ggPipSample(x, y);
  if (!lab) { loupe.hidden = true; return; }
  const now = ggMatch(z, lab), ok = now === ggPip.w, N = 15, S = ggPip.lp.width;
  const head = ok ? '✓ уже «' + GG_CLASS[now] + '»' : now ? 'сейчас это «' + GG_CLASS[now] + '»' : '＋ клик добавит';
  ggPip.lctx.clearRect(0, 0, S, S);
  ggPip.lctx.drawImage(ggPip.cv, x - (N >> 1), y - (N >> 1), N, N, 0, 0, S, S);
  ggPip.lctx.strokeStyle = '#fff'; ggPip.lctx.lineWidth = 1;
  ggPip.lctx.strokeRect(S / 2 - S / N * 2.5, S / 2 - S / N * 2.5, S / N * 5, S / N * 5);
  loupe.className = 'sh-loupe ' + (ok ? 'ok' : 'no');
  loupe.querySelector('[data-ggtag]').innerHTML = `<i class="sh-dot sh-dot--lg" style="background:${ggLabCss(lab)}"></i><span><b>${head}</b><br><small>берём «${GG_CLASS[ggPip.w]}» · L ${lab[0]} a ${lab[1]} b ${lab[2]}</small></span>`;
  const st = ggPip.el.querySelector('.sh-pipov__stage').getBoundingClientRect();
  loupe.hidden = false;
  let lx = e.clientX - st.left + 22, ly = e.clientY - st.top + 24;
  const lw = loupe.offsetWidth + 6, lh = loupe.offsetHeight + 6;
  if (lx + lw > st.width) lx = e.clientX - st.left - lw - 22;
  if (ly + lh > st.height) ly = e.clientY - st.top - lh - 18;
  loupe.style.left = Math.max(0, lx) + 'px';
  loupe.style.top = Math.max(0, ly) + 'px';
}
function ggPipClick(e) {
  const z = ggPipZone();
  if (!ggPip.loaded || !z) return;
  const { x, y } = ggPipPos(e), lab = ggPipSample(x, y);
  const msg = (text) => { const f = ggPip.el && ggPip.el.querySelector('[data-ggmsg]'); if (f) { f.hidden = false; f.textContent = text; clearTimeout(ggPip.msgT); ggPip.msgT = setTimeout(() => { f.hidden = true; }, 3500); } };
  if (!lab) return;
  const w = ggPip.w, list = z[w + '_colors'] || [], other = w === 'fill' ? 'empty' : 'fill';
  if (ggMatch(z, lab) === w) { msg('Этот цвет уже ловится как «' + GG_CLASS[w] + '»'); return; }
  if (ggDist(lab, z[other + '_colors']) < 3) { msg('Такой же цвет уже взят как «' + GG_CLASS[other] + '»: сначала уберите тот образец'); return; }
  if (list.length >= GG_MAX_COLORS) { msg('Не больше ' + GG_MAX_COLORS + ' образцов — уберите ненужный в карточке'); return; }
  mdEdit(() => { z[w + '_colors'] = list.concat([lab]); }, true);
  const next = !(z[other + '_colors'] || []).length ? other : w;      // взяли первый цвет — сразу переходим ко второму
  msg('Добавлен цвет «' + GG_CLASS[w] + '»' + (next !== w ? '. Теперь кликните по «' + GG_CLASS[next] + '»' : ''));
  ggPip.w = next;
  ggPipFoot();
  ggPipTint();
}

// ---------- окно разметки (вкладка «Уровнемер») ----------
// MD.mark: gk — вид ('tube' | 'gate'), dir — направление столбика, ruler — замкнутая линейка стрелки (шаг 2), replace — id зоны при «разметить заново»
function ggMarkReset(m, opts) {
  const cam = mvCam(m.cam), old = opts && opts.replace && cam ? cam.zones.find((q) => q.id === opts.replace) : null;
  Object.assign(m, { error: null, replace: old ? old.id : null, gk: old ? old.type : m.gk || 'tube', dir: old && old.direction ? old.direction : 'up', dirTouched: !!old });
}

function ggMarkSetSub(m) {                                    // переключатели над кадром: вид и направление
  const box = mdEl('mmGauge');
  box.hidden = !m.gauge;
  if (!m.gauge) return;
  box.querySelectorAll('[data-ggk]').forEach((b) => { b.classList.toggle('on', b.dataset.ggk === m.gk); b.disabled = !!m.replace && b.dataset.ggk !== m.gk; });
  mdEl('mmDirBox').hidden = m.gk !== 'tube';
  mdEl('mmDir').value = m.dir;
}

function ggMarkError(m, text) { m.error = { text, pts: m.pts.length, ruler: !!m.ruler }; mdEl('mmHint').textContent = m.hint = text; }

function ggMarkHint(m) {
  if (m.error && m.error.pts === m.pts.length && m.error.ruler === !!m.ruler) return m.error.text;
  m.error = null;
  const again = m.replace ? 'Разметка заново: цвета, настройки и сценарий останутся. ' : '';
  const how = 'Два клика по диагонали — прямоугольник, три и больше — по точкам (наклонную колонку лучше обвести четырьмя). ПКМ — убрать точку.';
  if (m.gk === 'tube') return again + 'Обведите шкалу колонки от 0 до 100 %: края разметки и есть начало и конец шкалы. ' + how + ' Enter — готово.';
  if (!m.ruler) return again + 'Шаг 1 из 2. Линейка: два клика по диагонали и Enter — или четыре угла по наклону меток. Нижняя сторона — 0 %, верхняя — 100 %. ПКМ — убрать точку.';
  return 'Шаг 2 из 2. Обведите зону, где ходит указатель (рядом с линейкой, на всю длину хода): два клика — прямоугольник, три и больше — по точкам. Другое того же цвета попадать не должно. Enter — готово.';
}

function ggMarkUi(m) {
  const step1 = m.gk === 'gate' && !m.ruler;
  if (m.gk === 'tube' && !m.dirTouched && m.pts.length >= 2) {       // широкая и низкая разметка — шкала лежачая
    const xs = m.pts.map((p) => p[0]), ys = m.pts.map((p) => p[1]);
    m.dir = Math.max(...xs) - Math.min(...xs) > 1.5 * (Math.max(...ys) - Math.min(...ys)) ? 'right' : 'up';
    mdEl('mmDir').value = m.dir;
  }
  mdEl('mmClose').hidden = !step1;
  mdEl('mmClose').textContent = 'Дальше (Enter)';
  mdEl('mmClose').disabled = m.pts.length !== 2;
  mdEl('mmOk').hidden = step1 || m.pts.length < 2;
  mdEl('mmOk').textContent = 'Готово';
  mdEl('mmUndo').disabled = !m.pts.length && !m.ruler;
  mdEl('mmHint').textContent = m.hint = ggMarkHint(m);
  ggMarkSetSub(m);
}

// линейка стоит вертикально (та же проверка, что в сервисе): иначе он отверг бы весь конфиг уже после закрытия окна
function ggRulerOk(r) {
  const [bl, br, tr, tl] = r;
  return Math.min(bl[1], br[1]) - Math.max(tl[1], tr[1]) >= 4 && br[0] - bl[0] >= 1 && tr[0] - tl[0] >= 1;
}

// шаг 1 → шаг 2 у стрелки: линейка из двух точек (прямоугольник) или четырёх углов
function ggMarkNext(m) {
  if (m.gk !== 'gate' || m.ruler || (m.pts.length !== 2 && m.pts.length !== 4)) {
    if (m.gk === 'gate' && !m.ruler) ggMarkError(m, 'Линейке нужны две точки (прямоугольник) или ровно четыре угла.');
    return;
  }
  const r = ggQuad(ggShape(m.pts), 'up');
  if (!ggRulerOk(r)) { ggMarkError(m, 'Линейка должна стоять вертикально: два угла внизу, два вверху. Уберите точки (ПКМ) и отметьте заново.'); return; }
  m.rulerPts = m.pts.slice();
  m.ruler = r;
  m.pts = [];
  m.cursor = null;
  mdMarkUi();
}

// клик в окне разметки: четвёртый угол линейки сразу замыкает её
function ggMarkClicked(m) { if (m.gk === 'gate' && !m.ruler && m.pts.length === 4) ggMarkNext(m); }

function ggMarkUndo(m) {
  if (!m.pts.length && m.ruler) { m.pts = m.rulerPts.slice(0, -1); m.ruler = null; } else m.pts.pop();      // назад к линейке: без последней точки
}

function ggMarkEnter(m) { if (m.gk === 'gate' && !m.ruler) ggMarkNext(m); else mdMarkSave(); }

// рисунок разметки в окне (и в лупе): u — толщина «единицы», sx / sy — масштаб кадра в отображаемые пиксели
function ggPaintMark(g, u, m, sx, sy) {
  const at = (p) => [p[0] * sx, p[1] * sy];
  const dots = (pts) => pts.forEach((p) => { const q = at(p); g.beginPath(); g.arc(q[0], q[1], 4 * u, 0, 7); g.fill(); });
  const ends = (quad) => {                                    // подписи «0 %» и «100 %» у начала и конца шкалы
    const q = quad.map(at);
    g.font = 'bold ' + 13 * u + 'px sans-serif'; g.textAlign = 'center';
    [['0 %', 0, 1], ['100 %', 3, 2]].forEach(([t, i, j]) => {
      const x = (q[i][0] + q[j][0]) / 2, y = (q[i][1] + q[j][1]) / 2;
      g.save(); g.lineWidth = 4 * u; g.strokeStyle = '#000'; g.strokeText(t, x, y + 5 * u); g.fillStyle = '#fff'; g.fillText(t, x, y + 5 * u); g.restore();
    });
    g.textAlign = 'left';
  };
  if (m.ruler) { g.strokeStyle = '#38bdf8'; g.fillStyle = 'rgba(56,189,248,.18)'; ggPath(g, m.ruler.map(at)); g.fill(); g.stroke(); ends(m.ruler); }
  g.strokeStyle = m.ruler ? '#fbbf24' : '#38bdf8'; g.fillStyle = m.ruler ? 'rgba(251,191,36,.16)' : 'rgba(56,189,248,.16)';
  let shape = null;
  if (m.pts.length === 1 && m.cursor) { g.setLineDash([6 * u, 4 * u]); ggPath(g, ggRect(m.pts[0], m.cursor).map(at)); g.stroke(); g.setLineDash([]); }
  else if (m.pts.length === 2) shape = ggRect(m.pts[0], m.pts[1]);
  else if (m.pts.length > 2) shape = m.pts;
  if (shape) {
    const dir = m.gk === 'tube' ? m.dir : 'up';
    const pts = shape.length === 4 ? ggQuad(shape, dir) : shape;
    ggPath(g, pts.map(at)); g.fill(); g.stroke();
    if (m.gk === 'tube' || !m.ruler) ends(ggFrame(shape, dir));
    g.strokeStyle = m.ruler ? '#fbbf24' : '#38bdf8';
  }
  g.fillStyle = m.ruler ? '#fbbf24' : '#38bdf8';
  dots(m.pts);
}

function ggMarkSave(m, cam, fw, fh) {
  if (m.pts.length < 2 || (m.gk === 'gate' && !m.ruler)) return;
  const shape = ggShape(m.pts);
  const dir = m.gk === 'tube' ? m.dir : 'up';
  const along = shape.map((p) => ggV(p, dir)), across = shape.map((p) => ggU(p, dir));
  if (m.gk === 'tube' && (Math.max(...along) - Math.min(...along) < 8 || Math.max(...across) - Math.min(...across) < 2)) {
    ggMarkError(m, 'Разметка слишком мала вдоль или поперёк шкалы. Уберите точки (ПКМ) и обведите колонку заново.');
    return;
  }
  if (m.gk === 'gate' && shape.length < 3) return;
  mdEdit(() => {
    const fs = cam.frame_size || [fw, fh];
    cam.frame_size = fs;
    const sc = (p) => [Math.round(p[0] * fs[0] / fw), Math.round(p[1] * fs[1] / fh)];
    const pts = (shape.length === 4 ? ggQuad(shape, dir) : shape).map(sc);
    const geo = m.gk === 'tube' ? { points: pts, direction: dir } : { ruler: ggQuad(m.ruler.map(sc), 'up'), points: pts };
    const old = m.replace && cam.zones.find((z) => z.id === m.replace);
    if (old) { Object.assign(old, geo, { enabled: true }); return; }
    const used = (id) => cam.zones.some((z) => z.id === id) || (cam.groups || []).some((q) => q.id === id);
    let n = 1;
    while (used(m.gk + n)) n += 1;
    let k = cam.zones.filter(ggIs).length + 1;
    while (cam.zones.some((z) => z.name === 'Уровнемер ' + k)) k += 1;
    const base = { id: m.gk + n, name: 'Уровнемер ' + k, enabled: true, type: m.gk, metric: 'flow', on: 0, off: 0, invert: false, track_flow: false, out: null, smooth: 5, hold_s: GG_HOLD_DEF };
    const own = m.gk === 'tube'
      ? { fill_colors: [], empty_colors: [], tol: GG_TOL_DEF, min_quality: GG_QUALITY_DEF }
      : { flip: false, min_area: 200, red_hsv: GG_RED_DEF.slice(), red_more: [], red_not: [] };
    cam.zones.push(Object.assign(base, own, geo));
  }, true);
  log.success('КЗ: уровнемер размечен', { serial: cam.id });
  closeMarkModal();
  MD.tab = 'zones'; MD.key = ''; renderMotionDock();
}

// ---------- КЗ · вкладка «Валы»: скорости и разности четырёх валов (НЛВ, НПВ, ВЛВ, ВПВ) ----------
// Н/В — нижний/верхний ряд (поле «ряд валов» у камеры), Л/П — левый/правый вал в ряду (по положению оси в кадре).
// Четыре пары: НЛВ–НПВ, ВЛВ–ВПВ, НЛВ–ВЛВ, НПВ–ВПВ; у каждой разность скоростей (об/мин, по модулю) и разность углов (°, от эталона).
// Расчёт и запись в ПЛК — motion_service/motion_shafts.py; тут только настройки, показ и график. Снимок приходит в MV.shafts из /api/motion/state.
const SH_IDS = ['nl', 'np', 'vl', 'vp'];
const SH_PAIRS = ['nlr', 'vlr', 'lnv', 'rnv'];
const SH_NAME = { nl: 'НЛВ', np: 'НПВ', vl: 'ВЛВ', vp: 'ВПВ' };
const SH_FULL = { nl: 'нижний левый вал', np: 'нижний правый вал', vl: 'верхний левый вал', vp: 'верхний правый вал' };
const SH_PAIR_NAME = { nlr: 'НЛВ–НПВ', vlr: 'ВЛВ–ВПВ', lnv: 'НЛВ–ВЛВ', rnv: 'НПВ–ВПВ' };
const SH_PAIR_NOTE = { nlr: 'левый и правый нижнего ряда', vlr: 'левый и правый верхнего ряда', lnv: 'левые валы двух рядов', rnv: 'правые валы двух рядов' };
// статистика разностей за период (ср / мин / макс / по модулю): считает сервис по секундной истории (до 5 ч, в памяти, без кадров с удержанием)
const SH_STAT_WINS = [[300, '5 мин'], [900, '15 мин'], [1800, '30 мин'], [3600, '1 ч'], [7200, '2 ч'], [10800, '3 ч'], [18000, '5 ч']];
const SH_STAT_KEY = 'mvs_sh_stat_win';
const shLoadStatWin = () => { try { const v = Number(localStorage.getItem(SH_STAT_KEY)); if (SH_STAT_WINS.some(([x]) => x === v)) return v; } catch (e) { /* не критично */ } return 3600; };
const SH = { histAt: 0, busy: false, hist: {}, note: '', noteCls: '', statWin: shLoadStatWin(), stats: {}, statAt: 0, statBusy: false };

const shCfg = () => (MV.cfg && MV.cfg.shafts) || null;
const shSnap = () => MV.shafts || null;
const shSgn = (v, d) => (v > 0 ? '+' : '') + Number(v).toFixed(d);

// подпись вала на плитке: «НЛВ» для зоны-цикла камеры, которая назначена валом; иначе null
function shShaftOf(camId, zoneId) {
  const s = shSnap();
  if (!s || !s.shafts) return null;
  return SH_IDS.map((id) => s.shafts[id]).find((x) => x && x.camera === camId && x.zone === zoneId) || null;
}

// разности пар, в которых участвует вал этой камеры — для подписи на плитке
function shPairsOfCamera(camId) {
  const s = shSnap();
  if (!s || !s.pairs) return [];
  return SH_PAIRS.map((id) => s.pairs[id]).filter((p) => p && (s.shafts[p.a].camera === camId || s.shafts[p.b].camera === camId));
}

// ---- карта потерь: где (угол палки) и когда (час суток) палка пропадает, по секундным трендам сервиса ----
const SH_LOSS_WINS = [[86400, '24 ч'], [7 * 86400, '7 сут'], [14 * 86400, '14 сут']];
const SH_LOSS_KEY = 'mvs_sh_loss_win';
const shLoss = { win: (() => { try { const v = Number(localStorage.getItem(SH_LOSS_KEY)); if (SH_LOSS_WINS.some(([x]) => x === v)) return v; } catch (e) { /* не критично */ } return 7 * 86400; })(), data: {}, at: {}, busy: false };

function mdLossHtml() {
  const blocks = [];
  mdCfgCams().forEach((c) => {
    (c.zones || []).forEach((z) => {
      if (z.type !== 'cycle' || z.enabled === false) return;
      const sh = shShaftOf(c.id, z.id);
      const k = escapeHtml(c.id + '/' + z.id);
      blocks.push(`<div class="md-zone sh-loss" data-shloss="${k}" data-cam="${escapeHtml(c.id)}" data-zone="${escapeHtml(z.id)}">
        <div class="md-zone__ctl">
          <div class="md-zone__row"><b>${escapeHtml(sh ? sh.short : z.name)}</b><span class="net-hint">${escapeHtml(c.name)}</span></div>
          <div class="md-zstat" data-shlosst="${k}">загружаю…</div>
        </div>
        <div class="md-zone__chart sh-loss__ch"><canvas width="170" height="170" data-shlossring="${k}"></canvas><canvas width="360" height="84" data-shlosshour="${k}"></canvas></div>
      </div>`);
    });
  });
  if (!blocks.length) return '<span class="net-hint">Размеченных валов нет: нужны зоны-цикл («Разметить валы»).</span>';
  return `<div class="md-zone__row"><span class="md-lbl">за</span><select data-md="shloss" title="За какой период показать карту">${mdSel(SH_LOSS_WINS, shLoss.win)}</select>
    <span class="net-hint" title="Секунда считается «найдена», если палку увидели хотя бы в одном кадре секунды. Время без картинки (обрыв связи) в расчёт не идёт. Секунды полной потери (угла нет) входят в часы, но не в углы.">зелёный — палка найдена, красный — теряется, серый — мало данных</span></div>${blocks.join('')}`;
}

const shLossColor = (p) => `hsl(${Math.round(120 * Math.max(0, Math.min(1, p)))}, 62%, 44%)`;

function shDrawLoss(block) {
  const k = block.dataset.shloss, d = shLoss.data[k], note = block.querySelector('[data-shlosst]');
  const ring = block.querySelector('[data-shlossring]'), hrs = block.querySelector('[data-shlosshour]');
  if (!d || !ring || !hrs) return;
  if (d.error) { note.textContent = d.error; return; }
  const MINN = 120;                                           // меньше секунд — «мало данных»
  const nsec = d.sector.n.length, deg = 360 / nsec;
  const g = ring.getContext('2d'), W = ring.width, cx = W / 2, R = W / 2 - 4, r0 = R * 0.5;
  g.clearRect(0, 0, W, W);
  let worstS = null;
  for (let i = 0; i < nsec; i += 1) {
    const n = d.sector.n[i], p = n >= MINN ? d.sector.f[i] / n : null;
    const a0 = ((i * deg - 90) * Math.PI) / 180, a1 = (((i + 1) * deg - 90) * Math.PI) / 180;
    g.beginPath(); g.arc(cx, cx, R, a0, a1); g.arc(cx, cx, r0, a1, a0, true); g.closePath();
    g.fillStyle = p === null ? 'rgba(120,120,130,.35)' : shLossColor(p); g.fill();
    g.strokeStyle = 'rgba(0,0,0,.35)'; g.lineWidth = 1; g.stroke();
    if (p !== null && (worstS === null || p < worstS.p)) worstS = { p, i };
  }
  g.fillStyle = '#6b7280'; g.font = 'bold 13px sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText(d.avg === null ? '—' : Math.round(d.avg) + ' %', cx, cx);
  g.font = '10px sans-serif'; g.fillText('0°', cx, 11);
  const h = hrs.getContext('2d'), HW = hrs.width, HH = hrs.height, bw = HW / 24, top = 12, bot = 14;
  h.clearRect(0, 0, HW, HH);
  let worstH = null;
  for (let i = 0; i < 24; i += 1) {
    const n = d.hour.n[i], p = n >= MINN ? d.hour.f[i] / n : null;
    const bh = p === null ? 6 : Math.max(2, p * (HH - top - bot));
    h.fillStyle = p === null ? 'rgba(120,120,130,.35)' : shLossColor(p);
    h.fillRect(i * bw + 1, HH - bot - bh, bw - 2, bh);
    if (p !== null && (worstH === null || p < worstH.p)) worstH = { p, i };
  }
  h.fillStyle = '#9ca3af'; h.font = '10px sans-serif'; h.textAlign = 'center';
  [0, 6, 12, 18, 23].forEach((x) => h.fillText(String(x), x * bw + bw / 2, HH - 2));
  const days = Math.round(d.window_s / 86400 * 10) / 10;
  note.innerHTML = d.seconds < MINN ? 'за этот период данных почти нет' :
    `найдена в среднем <b>${d.avg} %</b> времени` +
    (worstS && worstS.p < d.avg / 100 - 0.05 ? ` · хуже всего на углах <b>${worstS.i * deg}–${(worstS.i + 1) * deg}°</b> (${Math.round(worstS.p * 100)} %)` : '') +
    (worstH && worstH.p < d.avg / 100 - 0.05 ? ` · хуже всего в <b>${worstH.i}–${worstH.i + 1} ч</b> (${Math.round(worstH.p * 100)} %)` : '') +
    (worstS && worstH && worstS.p >= d.avg / 100 - 0.05 && worstH.p >= d.avg / 100 - 0.05 ? (d.avg >= 97 ? ' · потерь почти нет' : ' · потери равномерные: ни угол, ни час не выделяются') : '');
  ring.title = 'Доля «найдена» по углу красного конца палки (0° — вверх, по часовой), ' + deg + '° на сектор. За ' + days + ' сут.';
  hrs.title = 'Доля «найдена» по часам суток (местное время). За ' + days + ' сут.';
}

async function shLossRefresh(force) {
  if (MD.tab !== 'shafts' || shLoss.busy) return;
  const blocks = [...document.querySelectorAll('[data-shloss]')];
  if (!blocks.length) return;
  const now = Date.now(), cams = [...new Set(blocks.map((b) => b.dataset.cam))];
  const stale = cams.filter((id) => force || !shLoss.at[id] || shLoss.at[id].win !== shLoss.win || now - shLoss.at[id].t > 60000);
  blocks.forEach((b) => { if (shLoss.data[b.dataset.shloss] && !stale.includes(b.dataset.cam)) shDrawLoss(b); });
  if (!stale.length) return;
  shLoss.busy = true;
  try {
    await Promise.all(stale.map(async (id) => {
      shLoss.at[id] = { t: now, win: shLoss.win };
      const r = await mvApi('/api/motion/loss_map?camera=' + encodeURIComponent(id) + '&window=' + shLoss.win);
      blocks.filter((b) => b.dataset.cam === id).forEach((b) => {
        const one = r && !r.error && r.zones ? r.zones[b.dataset.zone] : null;
        shLoss.data[b.dataset.shloss] = one ? Object.assign({ window_s: r.window_s }, one) : { error: (r && r.error) || 'нет ответа сервиса (или он старой версии)' };
        shDrawLoss(b);
      });
    }));
  } finally { shLoss.busy = false; }
}

function mdOnShLoss(t) {
  shLoss.win = Number(t.value);
  try { localStorage.setItem(SH_LOSS_KEY, String(shLoss.win)); } catch (e) { /* не критично */ }
  shLossRefresh(true);
}

// ---- цвета палок: пороги красного и синего конца по каждому валу + превью того, что попадает в маску, + пипетка ----
// Таблица: у каждого конца палки четыре порога (оттенок от/до, насыщенность от, яркость от) в одну строку; ползунки покрашены в свой цвет.
// Пипетка: клик по палке на картинке — сервис находит её цвет (rod_pick), диапазон ставится вокруг него; следующий клик добавляет цвет в диапазон.
const SH_RED_DEF = [16, 165, 50, 55];
const SH_BLUE_DEF = [82, 135, 55, 60];
// колонки таблицы: [подпись, индекс в массиве порогов, максимум, подсказка]; у красного «от» — индекс 1, «до» — 0 (красный лежит по краям круга оттенков)
const SH_HSV_COLS = {
  red: [['оттенок от', 1, 179, 'Красный лежит по краям круга оттенков: от этого значения до 179 и от 0 до «оттенок до»'], ['оттенок до', 0, 179, 'Верхняя граница красного за нулём круга оттенков: от 0 до этого значения'],
    ['насыщенность от', 2, 255, 'Минимальная насыщенность. Ниже — цвет блёклый (корпус, фон)'], ['яркость от', 3, 255, 'Минимальная яркость. Ниже — тень']],
  blue: [['оттенок от', 0, 179, 'Голубой/синий: нижняя граница оттенка (0–179, шкала OpenCV)'], ['оттенок до', 1, 179, 'Верхняя граница оттенка'],
    ['насыщенность от', 2, 255, 'Минимальная насыщенность. Бирюзовый корпус рядом по цвету, поэтому слишком низкое значение захватывает и его'], ['яркость от', 3, 255, 'Минимальная яркость']],
};
const SH_END = { red: ['Красный конец', MV_ROD_RED, 0], blue: ['Синий конец', MV_ROD_BLUE, 200] };    // подпись, цвет точки, базовый оттенок (°) для заливки ползунков
const SH_HUE_TRK = 'linear-gradient(90deg,' + Array.from({ length: 13 }, (_, i) => `hsl(${Math.round(i * 29.8)},85%,50%)`).join(',') + ')';
const SH_PREV_W = 240;
const shView = {};                                         // вал → 'mask' | 'raw': что показывать на картинке
const SHP = { act: null };                                 // открытое окно пипетки: { key, w } (подробности — shPip ниже)

function shHsvOf(z, w) {
  const v = z[w + '_hsv'];
  return Array.isArray(v) && v.length === 4 ? v : (w === 'red' ? SH_RED_DEF : SH_BLUE_DEF);
}

function shTrack(col, base) {
  if (col === 1 || col === 0) return SH_HUE_TRK;
  return col === 2 ? `linear-gradient(90deg,#6b7280,hsl(${base},100%,50%))` : `linear-gradient(90deg,#000,hsl(${base},90%,62%))`;
}

function shColorRowHtml(w, z, a, key) {
  const v = shHsvOf(z, w), [name, color, base] = SH_END[w];
  const cells = SH_HSV_COLS[w].map(([lbl, k, max, tip]) => `<label class="sh-hsv" title="${escapeHtml(tip)}">` +
    `<input type="range" class="sh-rng" style="--trk:${shTrack(k, base)}" ${a} data-f="hsv" data-w="${w}" data-k="${k}" min="0" max="${max}" step="1" value="${v[k]}" /><output>${v[k]}</output></label>`).join('');
  return `<div class="sh-tn"><i class="sh-dot" style="background:${color}"></i>${name}</div>${cells}` +
    `<div class="sh-tact"><button type="button" class="btn-secondary md-x sh-pipbtn" data-md="shpick" data-w="${w}" data-f="pip" title="Откроется большой кадр: наведите на ${w === 'red' ? 'красный' : 'синий'} конец палки — лупа покажет цвет и «ловится / не ловится», клик добавит цвет отдельной строкой «ещё цвет». Оттенков у одного конца может быть несколько (светлый, тёмный, на солнце)">пипетка</button></div>`;
}

// дополнительный цвет конца палки (red_more / blue_more): обычный диапазон «от и до» с теми же тремя порогами; в маску он добавляется «ИЛИ»
function shExtraRowHtml(w, z, a, n) {
  const r = (z[w + '_more'] || [])[n], base = SH_END[w][2];
  const cells = [0, 1, 2, 3].map((k) => `<label class="sh-hsv"><input type="range" class="sh-rng" style="--trk:${shTrack(k, base)}" ${a} data-f="hsvx" data-w="${w}" data-n="${n}" data-k="${k}" min="0" max="${k < 2 ? 179 : 255}" step="1" value="${r[k]}" /><output>${r[k]}</output></label>`).join('');
  return `<div class="sh-tn sh-tn--more"><i class="sh-dot" style="background:hsl(${Math.round(r[0] + r[1])},75%,48%)"></i>ещё цвет</div>${cells}` +
    `<div class="sh-tact"><button type="button" class="btn-secondary md-x" ${a} data-f="hsvxdel" data-w="${w}" data-n="${n}" title="Убрать этот цвет из списка">убрать</button></div>`;
}

function mdRodColorsHtml() {
  const blocks = [];
  mdCfgCams().forEach((c) => {
    (c.zones || []).forEach((z, i) => {
      if (z.type !== 'cycle' || z.enabled === false) return;
      const a = `data-md="z" data-cam="${escapeHtml(c.id)}" data-i="${i}"`;
      const sh = shShaftOf(c.id, z.id);
      const k = escapeHtml(c.id + '/' + z.id);
      const head = SH_HSV_COLS.red.map(([lbl, , , tip]) => `<div class="sh-th" title="${escapeHtml(tip)}">${lbl}</div>`).join('');
      const more = (w) => (z[w + '_more'] || []).map((_, n) => shExtraRowHtml(w, z, a, n)).join('');
      blocks.push(`<div class="sh-col" data-shcol="${k}" data-cam="${escapeHtml(c.id)}" data-zone="${escapeHtml(z.id)}">
        <div class="sh-col__head"><b>${escapeHtml(sh ? sh.short : z.name)}</b><span class="net-hint">${escapeHtml(c.name)} · ${escapeHtml(z.name)}</span><span class="md-sp"></span>
          <button type="button" class="btn-secondary md-x" ${a} data-f="hsvreset" title="Вернуть пороги цвета этого вала к значениям по умолчанию">по умолчанию</button></div>
        <div class="sh-col__body">
          <div class="sh-prev"><img alt="" width="${SH_PREV_W}" data-shprev="${k}" />
            <div class="sh-seg"><button type="button" class="on" data-md="shpick" data-f="view" data-v="mask" title="Что считает палкой детектор: красные точки — красный конец, голубые — синий">маска</button><button type="button" data-md="shpick" data-f="view" data-v="raw" title="Сам кадр без масок: найти палку и взять цвет пипеткой">кадр</button></div></div>
          <div class="sh-tbl"><div class="sh-th"></div>${head}<div class="sh-th"></div>${shColorRowHtml('red', z, a, c.id + '/' + z.id)}${more('red')}${shColorRowHtml('blue', z, a, c.id + '/' + z.id)}${more('blue')}</div>
        </div>
        <div class="net-hint sh-col__note" data-shprevn="${k}"></div>
      </div>`);
    });
  });
  if (!blocks.length) return '<span class="net-hint">Размеченных валов нет: нужны зоны-цикл («Разметить валы»).</span>';
  return `<div class="net-hint sh-colhint">Палка красно-синяя: ловим её красный и синий конец по цвету. На картинке красные точки — то, что принято за красный конец, голубые — за синий; нужно, чтобы палка светилась целиком, а корпус и фон — нет. Двигайте ползунки или нажмите «пипетка»: откроется большой кадр, наведите на палку и кликните — цвет добавится отдельной строкой «ещё цвет» (у одного конца их может быть до шести: светлый, тёмный, на солнце). Изменения сохраняются сразу.</div><div class="sh-cols">${blocks.join('')}</div>`;
}

// превью: пробные пороги берём прямо с ползунков, сервис возвращает кадр вокруг оси с масками; не чаще раза в 4 с на вал, при правке — сразу
const shPrevAt = {};
function shHsvStr(block, w) {
  return [0, 1, 2, 3].map((k) => { const r = block.querySelector(`input[data-w="${w}"][data-k="${k}"]`); return r ? r.value : ''; }).join(',');
}
function shMoreStr(block, w) {                            // «H1,H2,S,V;H1,H2,S,V» по строкам «ещё цвет» — пустая строка тоже посылается: «дополнительных нет»
  const rows = {};
  block.querySelectorAll(`input[data-f="hsvx"][data-w="${w}"]`).forEach((i) => { (rows[i.dataset.n] = rows[i.dataset.n] || [])[Number(i.dataset.k)] = i.value; });
  return Object.keys(rows).sort((x, y) => x - y).map((n) => rows[n].join(',')).join(';');
}
function shPrevLoad(block, force) {
  const k = block.dataset.shcol, img = block.querySelector('img[data-shprev]'), note = block.querySelector('[data-shprevn]');
  if (!img || (!force && Date.now() - (shPrevAt[k] || 0) < 4000)) return;
  shPrevAt[k] = Date.now();
  const raw = shView[k] === 'raw' ? '&raw=1' : '';
  const url = '/api/motion/rod_mask?camera=' + encodeURIComponent(block.dataset.cam) + '&zone=' + encodeURIComponent(block.dataset.zone) +
    '&red=' + shHsvStr(block, 'red') + '&blue=' + shHsvStr(block, 'blue') + '&red_more=' + shMoreStr(block, 'red') + '&blue_more=' + shMoreStr(block, 'blue') + '&w=' + SH_PREV_W * 2 + raw + '&t=' + Date.now();      // вдвое больше, чем показываем: чётче на плотных экранах
  fetch(url).then((r) => (r.ok ? r.blob() : Promise.reject(r.status))).then((b) => {
    const u = URL.createObjectURL(b);
    const old = img.src;
    img.src = u;
    if (old && old.startsWith('blob:')) URL.revokeObjectURL(old);
    if (note && !SHP.act) note.textContent = '';
  }).catch(() => { if (note) note.textContent = 'картинки нет: камера не подключена или сервис движения старой версии'; });
}
function shPrevPaint() {
  document.querySelectorAll('[data-shcol]').forEach((b) => shPrevLoad(b, false));
}
let shSlideTimer = 0;
function shOnSlide(t) {                                   // ползунок двигают: значение рядом + быстрое обновление картинки
  const o = t.parentElement && t.parentElement.querySelector('output');
  if (o) o.textContent = t.value;
  const block = t.closest('[data-shcol]');
  if (!block) return;
  clearTimeout(shSlideTimer);
  shSlideTimer = setTimeout(() => shPrevLoad(block, true), 250);
}

// ---- пипетка: большое окно с кадром вокруг оси, лупа под курсором и «ловится / не ловится» ----
// Кадр берём с сервера (rod_mask?raw=1, родное разрешение) и обновляем не чаще раза в SH_PIP_EVERY секунд, чтобы успеть нажать (есть «стоп-кадр»).
// Цвет под курсором считаем здесь, по тому кадру, который виден: палка за 5 с поворачивается на ~24°, цвет с более позднего кадра с сервера был бы чужим.
const SH_PIP_EVERY = 5;
const SH_PIP_W = 900;
const shPip = { el: null, key: '', w: 'red', cv: null, ctx: null, lp: null, lctx: null, timer: 0, left: SH_PIP_EVERY, freeze: false, tint: false, loaded: false, busy: false };

function shRgb2Hsv(r, g, b) {                              // как в OpenCV: оттенок 0..179, насыщенность и яркость 0..255
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d > 0) {
    if (mx === r) h = ((g - b) / d) % 6; else if (mx === g) h = (b - r) / d + 2; else h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  return [Math.round(h / 2) % 180, mx ? Math.round(255 * d / mx) : 0, mx];
}

// цвет «с кончика пипетки»: в окне ±3 px берём пиксели, сильнее всего отличающиеся от фона окна (медианы) — это и есть тонкая палка; оттенок усредняем по кругу
function shPipSample(cx, cy) {
  const W = shPip.cv.width, H = shPip.cv.height, r = 3;
  const x0 = Math.max(0, cx - r), y0 = Math.max(0, cy - r), x1 = Math.min(W, cx + r + 1), y1 = Math.min(H, cy + r + 1);
  const w = x1 - x0, h = y1 - y0;
  if (w < 1 || h < 1) return null;
  const d = shPip.ctx.getImageData(x0, y0, w, h).data, n = w * h;
  const med = (ch) => { const a = []; for (let i = 0; i < n; i++) a.push(d[i * 4 + ch]); a.sort((p, q) => p - q); return a[n >> 1]; };
  const m = [med(0), med(1), med(2)];
  const px = [];
  for (let i = 0; i < n; i++) {
    const R = d[i * 4], G = d[i * 4 + 1], B = d[i * 4 + 2], hsv = shRgb2Hsv(R, G, B);
    px.push({ R, G, B, hsv, dist: Math.hypot(R - m[0], G - m[1], B - m[2]), live: hsv[1] >= 40 && hsv[2] >= 40 });
  }
  const pool = px.filter((p) => p.live).length >= 3 ? px.filter((p) => p.live) : px;      // тени и блёклое — не палка
  pool.sort((p, q) => q.dist - p.dist);
  const top = pool.slice(0, Math.max(3, Math.round(0.04 * n)));
  const sx = top.reduce((a, p) => a + Math.sin(p.hsv[0] * 2 * Math.PI / 180), 0), cxs = top.reduce((a, p) => a + Math.cos(p.hsv[0] * 2 * Math.PI / 180), 0);
  const hue = Math.round(((Math.atan2(sx, cxs) * 180 / Math.PI + 360) % 360) / 2) % 180;
  const mid = (arr) => arr.sort((p, q) => p - q)[arr.length >> 1];
  return { h: hue, s: mid(top.map((p) => p.hsv[1])), v: mid(top.map((p) => p.hsv[2])), rgb: [Math.round(top.reduce((a, p) => a + p.R, 0) / top.length), Math.round(top.reduce((a, p) => a + p.G, 0) / top.length), Math.round(top.reduce((a, p) => a + p.B, 0) / top.length)] };
}

// какие диапазоны сейчас у конца палки (читаем с ползунков карточки: там то, что видит детектор)
function shPipRanges(key, w) {
  const block = document.querySelector(`[data-shcol="${key}"]`);
  const out = { main: [0, 0, 0, 0], more: [] };
  if (!block) return out;
  const val = (sel) => { const r = block.querySelector(sel); return r ? parseInt(r.value, 10) || 0 : 0; };
  out.main = [0, 1, 2, 3].map((k) => val(`input[data-f="hsv"][data-w="${w}"][data-k="${k}"]`));
  const rows = {};
  block.querySelectorAll(`input[data-f="hsvx"][data-w="${w}"]`).forEach((i) => { (rows[i.dataset.n] = rows[i.dataset.n] || [])[Number(i.dataset.k)] = parseInt(i.value, 10) || 0; });
  out.more = Object.keys(rows).sort((a, b) => a - b).map((n) => rows[n]);
  return out;
}
// ловится ли цвет: '' — нет, иначе чем именно («основной» / «ещё цвет N»)
function shPipCovered(key, w, h, s, v) {
  const R = shPipRanges(key, w), m = R.main;
  if (s >= m[2] && v >= m[3] && (w === 'red' ? (h <= m[0] || h >= m[1]) : (h >= m[0] && h <= m[1]))) return 'основной диапазон';
  const i = R.more.findIndex((r) => h >= r[0] && h <= r[1] && s >= r[2] && v >= r[3]);
  return i >= 0 ? 'ещё цвет ' + (i + 1) : '';
}

function shPipOpen(key, w) {
  shPipClose();
  const block = document.querySelector(`[data-shcol="${key}"]`);
  if (!block) return;
  const name = (block.querySelector('.sh-col__head b') || {}).textContent || '';
  const sub = (block.querySelector('.sh-col__head .net-hint') || {}).textContent || '';
  SHP.act = { key, w };
  Object.assign(shPip, { key, w, left: SH_PIP_EVERY, freeze: false, tint: false, loaded: false });
  const el = document.createElement('div');
  el.className = 'sh-pipov';
  el.innerHTML = `<div class="sh-pipov__box" role="dialog" aria-label="Пипетка">
    <div class="sh-pipov__head"><b>Пипетка</b><span class="net-hint">${escapeHtml(name)} · ${escapeHtml(sub)}</span>
      <div class="sh-seg" data-pipend><button type="button" data-w="red">красный конец</button><button type="button" data-w="blue">синий конец</button></div>
      <span class="md-sp"></span>
      <span class="net-hint" data-pipclock></span>
      <label class="sh-chk"><input type="checkbox" data-pipfreeze /> стоп-кадр</label>
      <label class="sh-chk"><input type="checkbox" data-piptint /> показать, что ловится</label>
      <button type="button" class="btn-secondary md-x" data-pipnow>обновить кадр</button>
      <button type="button" class="btn-secondary md-x" data-pipdone>готово</button></div>
    <div class="sh-pipov__stage"><canvas data-pipcv></canvas><canvas data-piptc class="sh-pipov__tint"></canvas>
      <div class="sh-loupe" data-piploupe hidden><canvas width="120" height="120"></canvas><div class="sh-loupe__tag" data-piptag></div></div>
      <div class="sh-pipov__msg" data-pipmsg>загружаю кадр…</div></div>
    <div class="sh-pipov__foot" data-pipfoot></div></div>`;
  document.body.appendChild(el);
  shPip.el = el;
  shPip.cv = el.querySelector('[data-pipcv]');
  shPip.ctx = shPip.cv.getContext('2d', { willReadFrequently: true });
  shPip.lp = el.querySelector('[data-piploupe] canvas');
  shPip.lctx = shPip.lp.getContext('2d');
  shPip.lctx.imageSmoothingEnabled = false;
  el.addEventListener('mousedown', (e) => { if (e.target === el) shPipClose(); });
  el.querySelector('[data-pipdone]').addEventListener('click', shPipClose);
  el.querySelector('[data-pipnow]').addEventListener('click', () => { shPip.left = SH_PIP_EVERY; shPipLoad(); });
  el.querySelector('[data-pipfreeze]').addEventListener('change', (e) => { shPip.freeze = e.target.checked; shPipClock(); });
  el.querySelector('[data-piptint]').addEventListener('change', (e) => { shPip.tint = e.target.checked; shPipTint(); });
  el.querySelectorAll('[data-pipend] button').forEach((b) => b.addEventListener('click', () => { shPip.w = SHP.act.w = b.dataset.w; shPipEnd(); shPipTint(); }));
  shPip.cv.addEventListener('mousemove', shPipMove);
  shPip.cv.addEventListener('mouseleave', () => { const l = el.querySelector('[data-piploupe]'); if (l) l.hidden = true; });
  shPip.cv.addEventListener('click', shPipClick);
  document.addEventListener('keydown', shPipKey);
  shPipEnd();
  shPipLoad();
  shPip.timer = setInterval(() => { if (!shPip.freeze && --shPip.left <= 0) { shPip.left = SH_PIP_EVERY; shPipLoad(); } shPipClock(); }, 1000);
}
function shPipKey(e) { if (e.key === 'Escape') shPipClose(); }
function shPipClose() {
  clearInterval(shPip.timer);
  document.removeEventListener('keydown', shPipKey);
  if (shPip.el) shPip.el.remove();
  shPip.el = null;
  SHP.act = null;
}
function shPipClock() {
  const c = shPip.el && shPip.el.querySelector('[data-pipclock]');
  if (c) c.textContent = shPip.freeze ? 'кадр стоит' : 'кадр обновится через ' + shPip.left + ' с';
}
// какой конец сейчас берём и что в его списке цветов
function shPipEnd() {
  if (!shPip.el) return;
  shPip.el.querySelectorAll('[data-pipend] button').forEach((b) => b.classList.toggle('on', b.dataset.w === shPip.w));
  const R = shPipRanges(shPip.key, shPip.w), dotH = (r) => Math.round(r[0] + r[1]);
  const m = R.main, mainDot = shPip.w === 'red' ? 0 : Math.round(m[0] + m[1]);
  const chip = (txt, hue) => `<span class="sh-chip"><i class="sh-dot" style="background:hsl(${hue},75%,48%)"></i>${txt}</span>`;
  const mainTxt = shPip.w === 'red' ? `основной: оттенок ≤ ${m[0]} или ≥ ${m[1]}, насыщ. ≥ ${m[2]}, ярк. ≥ ${m[3]}` : `основной: оттенок ${m[0]}–${m[1]}, насыщ. ≥ ${m[2]}, ярк. ≥ ${m[3]}`;
  shPip.el.querySelector('[data-pipfoot]').innerHTML = '<span class="net-hint">Что сейчас ловится у ' + (shPip.w === 'red' ? 'красного' : 'синего') + ' конца:</span>' + chip(mainTxt, mainDot) +
    R.more.map((r, i) => chip(`ещё цвет ${i + 1}: оттенок ${r[0]}–${r[1]}, насыщ. ≥ ${r[2]}, ярк. ≥ ${r[3]}`, dotH(r))).join('') +
    '<span class="net-hint">Наведите на палку: в лупе — цвет, который возьмётся; зелёная галочка — такой цвет уже ловится, красный плюс — клик добавит его.</span>';
}
async function shPipLoad() {
  if (!shPip.el || shPip.busy) return;
  shPip.busy = true;
  const block = document.querySelector(`[data-shcol="${shPip.key}"]`);
  const msg = shPip.el.querySelector('[data-pipmsg]');
  try {
    const r = await fetch('/api/motion/rod_mask?camera=' + encodeURIComponent(block.dataset.cam) + '&zone=' + encodeURIComponent(block.dataset.zone) + '&raw=1&w=' + SH_PIP_W + '&t=' + Date.now());
    if (!r.ok) throw new Error(r.status);
    const bmp = await createImageBitmap(await r.blob());
    if (!shPip.el) return;
    shPip.cv.width = bmp.width; shPip.cv.height = bmp.height;
    shPip.ctx.drawImage(bmp, 0, 0);
    const tc = shPip.el.querySelector('[data-piptc]');
    tc.width = bmp.width; tc.height = bmp.height;
    shPip.loaded = true;
    msg.hidden = true;
    shPipTint();
  } catch (e) {
    if (!shPip.loaded) { msg.hidden = false; msg.textContent = 'Кадра нет: камера не подключена или сервис движения старой версии.'; }
  } finally { shPip.busy = false; }
}
// подсветка того, что ловится сейчас (по ползункам карточки): красным для красного конца, голубым для синего
function shPipTint() {
  const tc = shPip.el && shPip.el.querySelector('[data-piptint]') ? shPip.el.querySelector('[data-piptc]') : null;
  if (!tc || !shPip.loaded) return;
  const t = tc.getContext('2d');
  t.clearRect(0, 0, tc.width, tc.height);
  if (!shPip.tint) return;
  const R = shPipRanges(shPip.key, shPip.w), m = R.main, red = shPip.w === 'red';
  const src = shPip.ctx.getImageData(0, 0, tc.width, tc.height), out = t.createImageData(tc.width, tc.height), d = src.data, o = out.data;
  for (let i = 0; i < d.length; i += 4) {
    const [h, s, v] = shRgb2Hsv(d[i], d[i + 1], d[i + 2]);
    const hit = (s >= m[2] && v >= m[3] && (red ? (h <= m[0] || h >= m[1]) : (h >= m[0] && h <= m[1]))) || R.more.some((r) => h >= r[0] && h <= r[1] && s >= r[2] && v >= r[3]);
    if (hit) { o[i] = red ? 255 : 40; o[i + 1] = red ? 40 : 200; o[i + 2] = red ? 40 : 255; o[i + 3] = 175; }
  }
  t.putImageData(out, 0, 0);
}
function shPipPos(e) {
  const r = shPip.cv.getBoundingClientRect();
  return { x: Math.floor((e.clientX - r.left) * shPip.cv.width / r.width), y: Math.floor((e.clientY - r.top) * shPip.cv.height / r.height), r };
}
function shPipMove(e) {
  if (!shPip.loaded) return;
  const { x, y, r } = shPipPos(e), loupe = shPip.el.querySelector('[data-piploupe]');
  const smp = shPipSample(x, y);
  if (!smp) { loupe.hidden = true; return; }
  const by = shPipCovered(shPip.key, shPip.w, smp.h, smp.s, smp.v);
  const ok = !!by, N = 15, S = shPip.lp.width;
  shPip.lctx.clearRect(0, 0, S, S);
  shPip.lctx.drawImage(shPip.cv, x - (N >> 1), y - (N >> 1), N, N, 0, 0, S, S);     // 15×15 пикселей кадра, увеличенных, без сглаживания
  shPip.lctx.strokeStyle = '#fff'; shPip.lctx.lineWidth = 1;
  shPip.lctx.strokeRect(S / 2 - S / N / 2, S / 2 - S / N / 2, S / N, S / N);
  loupe.className = 'sh-loupe ' + (ok ? 'ok' : 'no');
  loupe.querySelector('[data-piptag]').innerHTML = `<i class="sh-dot sh-dot--lg" style="background:rgb(${smp.rgb.join(',')})"></i><span><b>${ok ? '✓ ловится' : '＋ не ловится'}</b><br><small>${ok ? escapeHtml(by) : 'клик добавит цвет'} · H ${smp.h} S ${smp.s} V ${smp.v}</small></span>`;
  const st = shPip.el.querySelector('.sh-pipov__stage').getBoundingClientRect();
  loupe.hidden = false;                                    // размер лупы с подписью — после заполнения: у края кадра она переходит на другую сторону курсора
  let lx = e.clientX - st.left + 22, ly = e.clientY - st.top + 24;
  const lw = loupe.offsetWidth + 6, lh = loupe.offsetHeight + 6;
  if (lx + lw > st.width) lx = e.clientX - st.left - lw - 22;
  if (ly + lh > st.height) ly = e.clientY - st.top - lh - 18;
  loupe.style.left = Math.max(0, lx) + 'px';
  loupe.style.top = Math.max(0, ly) + 'px';
  loupe.hidden = false;
  shPip.cur = smp;
}
function shPipClick(e) {
  if (!shPip.loaded) return;
  const { x, y } = shPipPos(e), smp = shPipSample(x, y);
  const msg = (html) => { const f = shPip.el && shPip.el.querySelector('[data-pipmsg]'); if (f) { f.hidden = false; f.innerHTML = html; clearTimeout(shPip.msgT); shPip.msgT = setTimeout(() => { f.hidden = true; }, 3500); } };
  if (!smp) return;
  const key = shPip.key, w = shPip.w, by = shPipCovered(key, w, smp.h, smp.s, smp.v);
  if (by) { msg('Этот цвет уже ловится (' + escapeHtml(by) + ')'); return; }
  const block = document.querySelector(`[data-shcol="${key}"]`);
  const cam = block && mvCam(block.dataset.cam), z = cam && cam.zones.find((q) => q.id === block.dataset.zone);
  if (!z) return;
  if ((z[w + '_more'] || []).length >= 6) { msg('Не больше шести дополнительных цветов на конец палки — уберите ненужный в таблице.'); return; }
  const cl = (q, hi) => Math.max(0, Math.min(hi, Math.round(q)));
  const range = [cl(smp.h - 6, 179), cl(smp.h + 6, 179), cl(Math.max(10, smp.s - 45), 255), cl(Math.max(10, smp.v - 50), 255)];
  mdEdit(() => { z[w + '_more'] = (z[w + '_more'] || []).concat([range]); }, true);      // сохраняет и перерисовывает карточки; окно остаётся
  msg('Добавлен цвет: H ' + smp.h + ', S ' + smp.s + ', V ' + smp.v);
  shPipEnd();
  shPipTint();
  const nb = document.querySelector(`[data-shcol="${key}"]`);
  if (nb) shPrevLoad(nb, true);
  shPipMove(e);
}

function mdOnShPick(t) {
  const block = t.closest('[data-shcol]');
  if (!block) return;
  const key = block.dataset.shcol;
  if (t.dataset.f === 'view') {                            // переключатель «маска / кадр» на маленькой картинке карточки
    shView[key] = t.dataset.v;
    block.querySelectorAll('.sh-seg button').forEach((b) => b.classList.toggle('on', b === t));
    shPrevLoad(block, true);
    return;
  }
  shPipOpen(key, t.dataset.w);                             // пипетка: большое окно
}

function mdShaftsTabHtml() {
  const sc = shCfg();
  if (!MV.cfg) return '<p class="net-hint">Нет связи с сервисом движения.</p>';
  if (!sc) return '<p class="net-hint">Сервис движения старой версии: обновите его (UpdaterMotion.bat) — расчёта валов в нём ещё нет.</p>';
  const cams = mdCfgCams();
  const rows = cams.map((c) => `<label class="md-camrow"><span>${escapeHtml(c.name)}${mdIpTag(c.url)}</span>` +
    `<select data-md="c" data-cam="${escapeHtml(c.id)}" data-f="row" title="Какой ряд валов видит камера. Левый и правый вал определяются по положению осей в кадре (нужны две зоны-цикла «Разметить валы»)">` +
    mdSel([['', 'не задан'], ['lower', 'нижний (Н)'], ['upper', 'верхний (В)']], c.row || '') + '</select></label>').join('');
  const shafts = SH_IDS.map((id) => {
    const o = sc.shafts[id].out_rpm;
    return `<tr><td><b>${SH_NAME[id]}</b> <span class="net-hint">${SH_FULL[id]}</span></td><td id="md-shs-${id}" class="net-hint">–</td>` +
      `<td class="md-num" id="md-sha-${id}">–</td><td class="md-num" id="md-shv-${id}">–</td>` +
      `<td>${mdOutCell(o, `data-md="sh" data-id="${id}"`, false, 'sr', [['', 'нет'], ['holding', 'регистр']])}</td></tr>`;
  }).join('');
  const pairs = SH_PAIRS.map((id) => mdShaftPairHtml(id, sc.pairs[id])).join('');
  const colors = mdRodColorsHtml();
  const loss = mdLossHtml();
  const autocal = mdAutocalHtml();
  return `<details class="md-det" open><summary>Ряды камер ${mdHelp('Задайте каждой камере ряд валов. Левый и правый вал в ряду определяются по положению оси в кадре: левее — левый. У камеры должны быть размечены две зоны-цикла (кнопка «Разметить валы»).')}</summary>
      <div class="md-camlist">${rows || '<span class="net-hint">В сервисе движения камер нет.</span>'}</div></details>
    <details class="md-det" open><summary>Скорости валов ${mdHelp('Скорость вала — обороты в минуту по наклону угла за окно (в карточке зоны). Регистр пишется ×100; 0 — нет данных. То же, что «скорость» в зоне-цикле, только по названию вала.')}</summary>
      <table class="md-table"><thead><tr><th>Вал</th><th>Откуда</th><th>Угол</th><th>Скорость</th><th>Регистр скорости в ПЛК</th></tr></thead><tbody>${shafts}</tbody></table></details>
    <details class="md-det" open><summary>Карта потерь ${mdHelp('Где и когда палка пропадает: кольцо — по углу красного конца (0° вверху, по часовой), столбики — по часам суток. Помогает понять, виновато ли освещение в определённый час или палка теряется в одном положении (блик, тень, цвет корпуса).')}</summary>${loss}</details>
    <details class="md-det" open><summary>Автоподгонка ${mdHelp('Сама подбирает цвета красной и синей половин палки, центр оси и радиус по записи вращения. Нужна после сдвига камеры, смены света или когда палка то пропадает, то ловится. Результат применяется только вашей кнопкой.')}</summary>${autocal}</details>
    <details class="md-det"><summary>Цвета палок ${mdHelp('Красная и синяя половины палки находятся по цвету. Если палка то пропадает, то ловится — подберите пороги: картинка показывает, что именно попадает в маску. У каждого вала свои пороги (ракурс и свет разные).')}</summary>${colors}</details>
    <div class="md-sub">Разности валов</div>
    <div class="md-statwin">Среднее / мин / макс за <select data-md="sh" data-f="statwin">${mdSel(SH_STAT_WINS, SH.statWin)}</select> ${mdHelp('Среднее, минимум, максимум и среднее по модулю Δ угла и Δ скорости каждой пары за выбранный период — по всем четырём парам сразу. Берутся только секунды, когда видны обе палки и ни у одной нет удержания. История копится в сервисе движения (до 5 ч) и после его перезапуска начинается заново.')}</div>
    <div class="net-hint" id="md-sh-note"></div>
    ${pairs}`;
}

function mdShaftPairHtml(id, p) {
  const a = `data-md="sh" data-id="${id}"`;
  return `<div class="md-zone md-mirror" data-shpair="${id}">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><b>${SH_PAIR_NAME[id]}</b><span class="net-hint">${SH_PAIR_NOTE[id]}</span><span class="md-sp"></span>
        <span class="cv-bit" id="md-shb-${id}" title="Тревога: разность больше допуска (угла или скорости)">–</span></div>
      <div class="md-zstat" id="md-shst-${id}"></div>
      <div class="md-zone__row"><span class="md-lbl" title="Отклонение углов от эталона, °. Левый и правый вал одного ряда — зеркально (сумма), левые и правые валы рядов — одинаково (разность)">Δ угла</span><span class="md-num" id="md-sha-p-${id}">–</span>
        <span class="md-lbl" title="|скорость первого| − |скорость второго|, об/мин. Направление вращения не важно">Δ скорости</span><span class="md-num" id="md-shr-${id}">–</span></div>
      <div class="md-zone__row md-mx-set"><span class="md-lbl" title="Тревога, когда |Δ угла| не меньше допуска. 0 — тревога по углу выключена">допуск угла, °</span>${mdNum(a + ' data-f="tol_angle"', p.tol_angle, 'min="0" max="180" step="0.5"')}
        <span class="md-lbl" title="Какое значение считать нулём. Камеры смотрят под разными ракурсами, поэтому ноль снимается вручную, когда валы стоят в нужном положении">эталон, °</span>${mdNum(a + ' data-f="ref"', p.ref, 'min="-180" max="180" step="0.5"')}
        <button type="button" class="btn-secondary md-x" ${a} data-f="calib" title="Принять текущее положение валов за ноль разности углов (нужны оба вала в кадре)">эталон = сейчас</button>
        <button type="button" class="btn-secondary md-x" ${a} data-f="calibavg" title="Автоподбор: ноль = среднее значение разности углов за период, выбранный вверху блока. Валы при этом идут как обычно, точно ловить «ноль» не нужно">эталон = среднее за период</button></div>
      <div class="md-zone__row md-mx-set"><span class="md-lbl" title="Тревога, когда |Δ скорости| не меньше допуска. 0 — тревога по скорости выключена">допуск скорости, об/мин</span>${mdNum(a + ' data-f="tol_rpm"', p.tol_rpm, 'min="0" step="0.1"')}</div>
      <div class="md-zone__row md-mx-set">
        <label class="switch-toggle md-sw md-sw--sm" title="Волна Δ за оборот — перекос камер, он повторяется. Сервис сам выучивает её по скользящему окну и вычитает: Δ держится около эталона, а свет и положение камер подстраиваются. Учится только форма волны: настоящий разъезд валов в неё не уходит и виден в Δ сразу. Тревога и запись в ПЛК идут по Δ с коррекцией."><span class="switch"><input type="checkbox" ${a} data-f="learn"${p.learn === false ? '' : ' checked'} /><span class="switch-track"></span></span><span class="md-lbl md-lbl--on">автокоррекция</span></label>
        <span class="md-lbl" title="За какое время учится форма: скользящее окно. Больше — спокойнее, меньше — быстрее следует за светом и камерой">окно</span>${mdNum(a + ' data-f="learnmin"', p.learn_min || 30, 'min="5" max="240" step="5"')}<span class="md-lbl">мин</span>
        <span class="net-hint" id="md-shl-${id}"></span></div>
      <div class="md-zone__row md-mx-set"><span class="md-lbl" title="Регистр: Δ угла в 0,1° со знаком (uint16). Адрес не задан — в ПЛК не пишется. ПЛК включается на вкладке «ПЛК · сценарии · группы»">Δ угла → ПЛК</span>${mdOutCell(p.out_angle, a, false, 'oa', [['', 'нет'], ['holding', 'регистр']])}
        <span class="md-lbl" title="Регистр: Δ скорости в 0,01 об/мин со знаком (uint16)">Δ скорости → ПЛК</span>${mdOutCell(p.out_rpm, a, false, 'or', [['', 'нет'], ['holding', 'регистр']])}</div>
    </div>
    <div class="md-zone__chart md-mx2">
      <div class="md-mxc"><span class="md-mxc__t">Δ угла без коррекции, °</span><canvas id="md-shc-w-${id}" height="64"></canvas></div>
      <div class="md-mxc"><span class="md-mxc__t">Δ угла с коррекцией, °</span><canvas id="md-shc-a-${id}" height="64"></canvas></div>
      <div class="md-mxc"><span class="md-mxc__t">Δ скорости, об/мин</span><canvas id="md-shc-r-${id}" height="64"></canvas></div>
    </div>
  </div>`;
}

const shClamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

async function mdOnShafts(t) {
  const sc = shCfg();
  if (!sc) return;
  const id = t.dataset.id, f = t.dataset.f;
  if (f === 'statwin') {                                     // период статистики: один на весь блок
    SH.statWin = Number(t.value);
    SH.stats = {};
    try { localStorage.setItem(SH_STAT_KEY, String(SH.statWin)); } catch (e) { /* не критично */ }
    mdPaintShaftStats();
    mdShaftsStats(true);
    return;
  }
  if (f === 'calib') { shCalibrate(id); return; }
  if (f === 'calibavg') { shCalibrateAvg(id); return; }
  const out = (cur, kindF) => (f === kindF ? (t.value ? { kind: t.value, address: (cur && cur.address) || 0 } : null) : cur);
  mdEdit(() => {
    if (SH_PAIRS.includes(id)) {
      const p = sc.pairs[id];
      if (f === 'tol_angle') p.tol_angle = shClamp(parseFloat(t.value) || 0, 0, 180);
      else if (f === 'tol_rpm') p.tol_rpm = Math.max(0, parseFloat(t.value) || 0);
      else if (f === 'ref') p.ref = shClamp(parseFloat(t.value) || 0, -180, 180);
      else if (f === 'learn') p.learn = t.checked;
      else if (f === 'learnmin') p.learn_min = shClamp(parseInt(t.value, 10) || 30, 5, 240);
      else if (f === 'oakind') p.out_angle = out(p.out_angle, 'oakind');
      else if (f === 'oaaddr') { if (p.out_angle) p.out_angle.address = shClamp(parseInt(t.value, 10) || 0, 0, 65535); }
      else if (f === 'orkind') p.out_rpm = out(p.out_rpm, 'orkind');
      else if (f === 'oraddr') { if (p.out_rpm) p.out_rpm.address = shClamp(parseInt(t.value, 10) || 0, 0, 65535); }
    } else if (sc.shafts[id]) {
      const s = sc.shafts[id];
      if (f === 'srkind') s.out_rpm = out(s.out_rpm, 'srkind');
      else if (f === 'sraddr') { if (s.out_rpm) s.out_rpm.address = shClamp(parseInt(t.value, 10) || 0, 0, 65535); }
    }
  }, /kind$/.test(f));
}

function shSetNote(text, cls) {
  SH.note = text || '';
  SH.noteCls = cls || '';
  const n = mdEl('md-sh-note');
  if (n) { n.textContent = SH.note; n.style.color = cls === 'bad' ? '#f87171' : ''; }
}

// «эталон = сейчас»: сервис запоминает текущую разность за ноль и сам сохраняет конфиг; потом перечитываем его
async function shCalibrate(pid) {
  const p = shSnap() && shSnap().pairs && shSnap().pairs[pid];
  if (!p || !p.valid) { shSetNote('Нет данных: у обоих валов пары должна быть найдена палка (ряд камеры задан, КЗ включено).', 'bad'); return; }
  if (!confirm('Принять текущее положение валов ' + SH_PAIR_NAME[pid] + ' за ноль разности углов? Валы должны стоять там, где разность должна быть равна нулю.')) return;
  if (MV.dirty) { clearTimeout(MD.saveTimer); await mdFlush(); }       // несохранённые правки уйдут раньше, чтобы эталон не затёрся
  const r = await mvJson('/api/motion/shafts_calibrate', { pair: pid });
  if (!r || r.error) { shSetNote('Эталон не снят: ' + ((r && r.error) || 'нет ответа'), 'bad'); return; }
  MV.dirty = false;
  await mvRefreshConfig();
  MD.key = '';
  renderMotionDock();
  shSetNote('Эталон ' + SH_PAIR_NAME[pid] + ' = ' + shSgn(r.ref, 1) + '°, разность углов теперь около нуля.', 'ok');
}

// автоподбор эталона: ноль = среднее Δ угла за выбранный период. Разовое действие по кнопке (не постоянная подстройка:
// постоянная впитала бы настоящий рассинхрон). Если за период валы реально разошлись, эталон тоже съедет — поэтому показываем цифры и спрашиваем.
async function shCalibrateAvg(pid) {
  const st = SH.stats[pid];
  const need = 30;
  if (!st || st.window_s !== SH.statWin || !st.angle || st.n < need || st.ref_suggest == null) {
    shSetNote('Мало данных для ' + SH_PAIR_NAME[pid] + ': за ' + mdFmtWin(SH.statWin) + ' набралось ' + ((st && st.n) || 0) + ' с с видимыми палками без удержания, нужно хотя бы ' + need + '. Подождите или выберите период короче.', 'bad');
    return;
  }
  const a = st.angle;
  const after = shSgn(a.min - a.avg, 1) + '…' + shSgn(a.max - a.avg, 1);
  if (!confirm('Принять за ноль среднее ' + SH_PAIR_NAME[pid] + ' за ' + mdFmtWin(st.span_s) + ' (' + st.n + ' с)?\n\n' +
    'Эталон: ' + shSgn(st.ref, 1) + '° → ' + shSgn(st.ref_suggest, 1) + '°\n' +
    'Среднее Δ сейчас ' + shSgn(a.avg, 1) + '°, после эталона среднее станет ≈ 0, колебания ' + after + '°.\n\n' +
    'Делайте, когда валы работают штатно: если за этот период они реально разошлись, расхождение уйдёт в эталон.')) return;
  if (MV.dirty) { clearTimeout(MD.saveTimer); await mdFlush(); }
  const r = await mvJson('/api/motion/shafts_calibrate', { pair: pid, window: SH.statWin });
  if (!r || r.error) { shSetNote('Эталон не снят: ' + ((r && r.error) || 'нет ответа'), 'bad'); return; }
  MV.dirty = false;
  await mvRefreshConfig();
  MD.key = '';
  renderMotionDock();
  SH.stats = {};
  mdShaftsStats(true);
  shSetNote('Эталон ' + SH_PAIR_NAME[pid] + ': ' + shSgn(r.before, 1) + '° → ' + shSgn(r.ref, 1) + '° (среднее за ' + mdFmtWin(r.span_s) + ').', 'ok');
}

// ср / мин / макс за период под каждой парой (значения приходят раз в 5 с, перерисовка — вместе с общим опросом)
const shFmtStat = (a, unit, d) => `ср <b class="md-zstat__cur">${shSgn(a.avg, d)}${unit}</b> · мин ${shSgn(a.min, d)} · макс ${shSgn(a.max, d)} · по модулю ${Number(a.avg_abs).toFixed(d)}`;
function mdPaintShaftStats() {
  SH_PAIRS.forEach((id) => {
    const el = mdEl('md-shst-' + id);
    if (!el) return;
    const st = SH.stats[id];
    if (!st) { el.innerHTML = ''; return; }
    if (!st.angle) { el.innerHTML = `<div class="md-zstat__row">за ${mdFmtWin(st.window_s)} нет данных: палки не видны или идёт удержание</div>`; return; }
    const note = st.span_s < st.window_s * 0.9 ? ` <span class="md-zstat__note">накоплено за ${mdFmtWin(st.span_s)}</span>` : '';
    el.innerHTML = `<div class="md-zstat__row is-main" title="Отклонение углов от эталона за период; «по модулю» — среднее |Δ|, показывает, насколько валы расходятся в целом, без взаимной компенсации + и −">Δ угла за ${mdFmtWin(st.window_s)}: ${shFmtStat(st.angle, '°', 1)}${note}</div>` +
      `<div class="md-zstat__row" title="|скорость первого| − |скорость второго| за период">Δ скорости: ${shFmtStat(st.rpm, ' об/мин', 2)}</div>`;
  });
}

async function mdShaftsStats(force) {
  if (MD.tab !== 'shafts' || SH.statBusy || (!force && Date.now() - SH.statAt < 5000)) return;
  SH.statBusy = true;
  SH.statAt = Date.now();
  const win = SH.statWin;
  try {
    for (const id of SH_PAIRS) {
      const r = await mvApi('/api/motion/shafts_stats?pair=' + id + '&window=' + win);
      if (r && !r.error && win === SH.statWin) SH.stats[id] = r;
    }
  } finally { SH.statBusy = false; }
  mdPaintShaftStats();
}

// живые значения: таблица валов и разности пар (раз в секунду вместе с опросом сервиса)
function mdPaintShafts() {
  const s = shSnap();
  if (!s || !s.shafts) return;
  const camName = (id) => { const c = id && mvCam(id); return c ? c.name : ''; };
  SH_IDS.forEach((id) => {
    const x = s.shafts[id];
    const src = mdEl('md-shs-' + id), ang = mdEl('md-sha-' + id), v = mdEl('md-shv-' + id);
    if (src) src.textContent = x.assigned ? camName(x.camera) + ' · ' + ((mvCam(x.camera) || { zones: [] }).zones.find((z) => z.id === x.zone) || { name: x.zone }).name : 'не назначен (задайте ряд камеры и разметьте валы)';
    if (ang) ang.textContent = x.valid ? x.angle.toFixed(1) + '°' : '—';
    if (v) { v.textContent = x.valid ? Math.abs(x.rpm).toFixed(2) + ' об/мин ' + mvDirIcon(x.rpm) : '—'; v.style.opacity = x.coast ? '.5' : ''; }
  });
  SH_PAIRS.forEach((id) => {
    const p = s.pairs[id];
    const a = mdEl('md-sha-p-' + id), r = mdEl('md-shr-' + id), b = mdEl('md-shb-' + id);
    if (a) { a.textContent = p.valid ? shSgn(p.angle_diff, 1) + '°' : '—'; a.style.color = p.valid && p.alarm_angle ? '#f87171' : ''; }
    if (r) { r.textContent = p.valid ? shSgn(p.rpm_diff, 2) + ' об/мин' : '—'; r.style.color = p.valid && p.alarm_rpm ? '#f87171' : ''; }
    if (b) { const al = p.alarm_angle || p.alarm_rpm ? 1 : 0; b.textContent = al; b.style.color = al ? '#f87171' : ''; }
    const l = mdEl('md-shl-' + id);
    if (l) l.textContent = !p.learn ? 'выключена' : (p.learn.ready ? 'учится · форма ±' + (p.learn.amp / 2).toFixed(1) + '°' : 'копит: ' + p.learn.bins + ' из 36 ячеек');
  });
  if (SH.noteCls === '' && !SH.note) shSetNote('', '');
  mdShaftsHist();
  mdShaftsStats();
  mdPaintShaftStats();
  shPrevPaint();
  shLossRefresh(false);
  shAcPaintAll();
}

// графики разностей: тянем историю сервиса раз в 5 с, пока вкладка открыта
async function mdShaftsHist(force) {
  if (MD.tab !== 'shafts' || SH.busy || (!force && Date.now() - SH.histAt < 5000)) return;
  SH.busy = true;
  SH.histAt = Date.now();
  try {
    const win = MD.win || 600;
    for (const id of SH_PAIRS) {
      const h = await mvApi('/api/motion/shafts_history?pair=' + id + '&window=' + win + '&points=300');
      if (h && !h.error && h.t) { SH.hist[id] = h; mdDrawShaftChart(id); }
    }
  } finally { SH.busy = false; }
}

function mdDrawShaftChart(id) {
  const h = SH.hist[id];
  if (!h || h.t.length < 2) return;
  const ok = h.t.map(() => 1);
  const span = (vals, min) => Math.max(min, Math.ceil(Math.max(...vals.map((v) => Math.abs(v))) * 1.2));
  const ca = mdEl('md-shc-a-' + id), cr = mdEl('md-shc-r-' + id), cw = mdEl('md-shc-w-' + id);
  const pc = (shCfg() || { pairs: {} }).pairs[id] || {};
  const m = span((h.angle_raw || []).concat(h.angle), 5);             // один масштаб у обоих графиков угла: разницу с коррекцией и без видно сразу
  if (cw && h.angle_raw) mdDrawSeries(cw, h.t, h.angle_raw, ok, -m, m, { lines: [{ v: 0, color: '#64748b' }] });
  if (ca) { mdDrawSeries(ca, h.t, h.angle, ok, -m, m, { lines: [{ v: 0, color: '#64748b' }].concat(pc.tol_angle ? [{ v: pc.tol_angle, color: '#f59e0b' }, { v: -pc.tol_angle, color: '#f59e0b' }] : []) }); }
  if (cr) { const m = span(h.rpm, 1); mdDrawSeries(cr, h.t, h.rpm, ok, -m, m, { lines: [{ v: 0, color: '#64748b' }].concat(pc.tol_rpm ? [{ v: pc.tol_rpm, color: '#f59e0b' }, { v: -pc.tol_rpm, color: '#f59e0b' }] : []) }); }
}

// ---- автоподгонка валов: запись вращения (≈2 оборота) → подбор цветов, оси и радиуса → «было → стало» → «Применить» ----
// Считает сервис движения (motion_autocal.py); тут кнопка, ход, результат. Результат — предложение: настройки меняются только по «Применить».
const SHAC = { data: {}, at: {}, busy: {}, sec: 150 };
const shAcRunning = (d) => !!d && ['starting', 'recording', 'tuning'].includes(d.state);

function mdAutocalHtml() {
  const cams = mdCfgCams().filter((c) => (c.zones || []).some((z) => z.type === 'cycle' && z.enabled !== false));
  if (!cams.length) return '<span class="net-hint">Размеченных валов нет: нужны зоны-цикл («Разметить валы»).</span>';
  return `<div class="net-hint">Палки крутятся сами, ничего двигать не надо. Сервис запишет вращение (по умолчанию 150 с — около двух оборотов), подберёт цвета красной и синей половин, центр оси и радиус и покажет, что стало лучше. Применяется кнопкой.</div>` + cams.map((c) => {
    const k = escapeHtml(c.id);
    return `<div class="md-zone sh-ac" data-shac="${k}"><div class="md-zone__ctl">
      <div class="md-zone__row"><b>${escapeHtml(c.name)}</b>${mdIpTag(c.url)}<span class="md-sp"></span>
        <span class="md-lbl" title="Сколько секунд записывать вращение. Вал делает оборот примерно за 75 с; от 75 до 300 с">запись, с</span>${mdNum(`data-md="shac" data-cam="${k}" data-f="sec"`, SHAC.sec, 'min="75" max="300" step="15"')}
        <button type="button" class="btn-secondary md-x" data-md="shac" data-cam="${k}" data-f="start" title="Записать вращение палок и подобрать цвета, центр и радиус">Подогнать валы</button></div>
      <div data-shacst="${k}"></div></div></div>`;
  }).join('');
}

const shPct = (v) => Math.round(100 * v) + ' %';
function shAcResultHtml(d) {
  const ids = Object.keys(d.results || {});
  const rows = ids.map((zid) => {
    const r = d.results[zid], b = r.before, a = r.after;
    const lost = Math.abs(b.rpm) < 0.25;                              // до подгонки палку не удавалось отслеживать (залипала на неподвижной цели)
    const sect = (a.sectors || []).map((p, i) => `<i style="background:${shLossColor(p)}" title="${i * 30}–${i * 30 + 30}°: ${shPct(p)}"></i>`).join('');
    const o = r.old, n = r.new, hs = (v) => 'H ' + v[0] + '–' + v[1] + ', S ≥ ' + v[2] + ', V ≥ ' + v[3];
    const rd = (v) => 'красный H ≤ ' + v[0] + ' или ≥ ' + v[1] + ', S ≥ ' + v[2] + ', V ≥ ' + v[3];
    const tip = `Было: ось ${o.center.map(Math.round).join(', ')}, радиус ${Math.round(o.radius)}; ${rd(o.red_hsv)}; синий ${hs(o.blue_hsv)}.\n` +
      `Стало: ось ${n.center.map(Math.round).join(', ')}, радиус ${Math.round(n.radius)}; ${rd(n.red_hsv)}; синий ${hs(n.blue_hsv)}.\n` +
      `Кадров в записи ${r.frames}, расчёт ${Math.round(r.seconds)} с. На кадрах, не участвовавших в подборе, найдена ${shPct(r.after_val.found)}.`;
    const verdict = (d.applied || []).includes(zid) ? '<b>применено</b>' : (r.improved ? '' : '<span class="net-hint">лучше не стало — не предлагаю</span>');
    return `<div class="md-zone__row sh-acrow" title="${escapeHtml(tip)}"><b>${escapeHtml(r.name)}</b>
      <span>найдена ${lost ? '<span class="net-hint">до: сбивалась</span>' : '<span class="net-hint">' + shPct(b.found) + '</span>'} → <b>${shPct(a.found)}</b></span>
      <span class="sh-strip">${sect}</span>
      <span class="net-hint">${a.revs.toFixed(1).replace('.', ',')} оборота в записи · ${Math.abs(a.rpm).toFixed(2).replace('.', ',')} об/мин</span>${verdict}</div>`;
  }).join('');
  const any = ids.some((k) => d.results[k].improved && !(d.applied || []).includes(k));
  return rows + (any ? `<div class="md-zone__row"><button type="button" class="btn-secondary md-x" data-md="shac" data-cam="${escapeHtml(d.camera)}" data-f="apply" title="Записать найденные настройки в зоны. Прежний конфиг остаётся в резервной копии (data\\backups)">Применить</button></div>` : '');
}

function shAcPaint(cam) {
  const box = document.querySelector(`[data-shacst="${cam}"]`);
  if (!box) return;
  const d = SHAC.data[cam];
  let html = '';
  if (!d || d.state === 'idle') html = '';
  else if (shAcRunning(d)) html = `<div class="sh-bar"><i style="width:${Math.round(100 * (d.progress || 0))}%"></i></div><div class="md-zone__row"><span class="net-hint">${escapeHtml(d.text || '')}</span><span class="md-sp"></span>` +
    `<button type="button" class="btn-secondary md-x" data-md="shac" data-cam="${escapeHtml(cam)}" data-f="cancel">Отмена</button></div>`;
  else if (d.state === 'error') html = `<div class="net-hint" style="color:#f87171">Не вышло: ${escapeHtml(d.error || 'ошибка')}</div>`;
  else if (d.state === 'cancelled') html = '<div class="net-hint">Отменено.</div>';
  else if (d.state === 'done') html = shAcResultHtml(d);
  if (box.dataset.h !== html) { box.dataset.h = html; box.innerHTML = html; }
}

async function shAcRefresh(cam, force) {
  const d = SHAC.data[cam];
  const every = shAcRunning(d) ? 1500 : 30000;
  if (SHAC.busy[cam] || (!force && Date.now() - (SHAC.at[cam] || 0) < every)) return;
  SHAC.busy[cam] = true;
  try {
    const r = await mvApi('/api/motion/autocal?camera=' + encodeURIComponent(cam));
    SHAC.at[cam] = Date.now();
    SHAC.data[cam] = r && r.error ? { state: 'error', error: /HTTP 404|HTTP 405/.test(r.error) ? 'сервис движения старой версии: обновите его (UpdaterMotion.bat)' : r.error } : r;
  } finally { SHAC.busy[cam] = false; }
  shAcPaint(cam);
}

function shAcPaintAll() {
  document.querySelectorAll('[data-shac]').forEach((b) => { shAcPaint(b.dataset.shac); shAcRefresh(b.dataset.shac, false); });
}

async function mdOnShAc(t) {
  const cam = t.dataset.cam, f = t.dataset.f;
  if (f === 'sec') { SHAC.sec = shClamp(parseInt(t.value, 10) || 150, 75, 300); t.value = SHAC.sec; return; }
  if (f === 'start') {
    if (MV.dirty) { clearTimeout(MD.saveTimer); await mdFlush(); }
    const r = await mvJson('/api/motion/autocal', { camera: cam, zones: [], seconds: SHAC.sec });
    SHAC.data[cam] = r && r.error ? { state: 'error', error: r.error } : r;
    SHAC.at[cam] = Date.now();
    shAcPaint(cam);
  } else if (f === 'cancel') {
    await mvJson('/api/motion/autocal/cancel', { camera: cam });
    shAcRefresh(cam, true);
  } else if (f === 'apply') {
    if (!confirm('Записать найденные настройки (ось, радиус, цвета) в зоны камеры? Прежний конфиг останется в резервной копии (data\\backups).')) return;
    if (MV.dirty) { clearTimeout(MD.saveTimer); await mdFlush(); }
    const r = await mvJson('/api/motion/autocal/apply', { camera: cam, zones: [] });
    if (r && r.error) { SHAC.data[cam] = Object.assign({}, SHAC.data[cam], { state: 'error', error: r.error }); shAcPaint(cam); return; }
    MV.dirty = false;
    await mvRefreshConfig();
    await shAcRefresh(cam, true);
    MD.key = '';
    renderMotionDock();
  }
}

// ---- плавающий виджет «разность валов»: четыре вала-круга с углами и разности углов (Верх / Низ / слева / справа), как на SCADA ----
// Включается значком в верхней панели, двигается мышью (за любое место), колесо — размер; место, размер и «открыт» помнит браузер.
// В кружке — угол красного конца вала (°, от вертикали по часовой); снаружи — разность углов пары от эталона (красная — выше допуска).
const SH_WD = { w: 210, h: 280, r: 40 };
const SH_WD_CIRCLE = { vl: [56, 82], vp: [154, 82], nl: [56, 198], np: [154, 198] };        // вал → центр круга
const SH_WD_PAIR = { vlr: [105, 22, 'Верх'], nlr: [105, 258, 'Низ'], lnv: [56, 140, ''], rnv: [154, 140, ''] };   // пара → центр подписи и название
const SHW_KEY = 'mvs_shafts_widget';
const SHW = { open: false, x: null, y: null, s: 1.15, el: null, cv: null, drag: null, corr: true };       // corr: показывать значения после автокоррекции (около нуля) или сырые
const SH_WD_TOGGLE = [146, 247, 56, 22];                    // переключатель «авто / сырые»: x, y, ширина, высота в координатах SH_WD
const SH_WD_EDGES = [['nlr', 'nl', 'np'], ['vlr', 'vl', 'vp'], ['lnv', 'nl', 'vl'], ['rnv', 'np', 'vp']];   // пара → (вал A, вал B): Δ пары = отклонение A − отклонение B

// отклонение каждого вала от общего положения, °: подгонка по разностям пар (после автокоррекции), сумма отклонений = 0.
// Если все валы в фазе, все четыре около нуля; разъехался один — он уходит от нуля, остальные почти не двигаются.
function shwDeviations(s) {
  const e = {}, edges = SH_WD_EDGES.filter(([pid]) => s.pairs[pid] && s.pairs[pid].valid && Number.isFinite(s.pairs[pid].angle_diff));
  const ids = SH_IDS.filter((id) => edges.some(([, a, b]) => a === id || b === id));
  ids.forEach((id) => { e[id] = 0; });
  for (let it = 0; it < 60; it++) {                                   // Гаусс–Зейдель на задаче Δ(A,B) = e[A] − e[B]
    ids.forEach((id) => {
      let sum = 0, n = 0;
      edges.forEach(([pid, a, b]) => {
        const d = s.pairs[pid].angle_diff;
        if (a === id) { sum += e[b] + d; n++; } else if (b === id) { sum += e[a] - d; n++; }
      });
      if (n) e[id] = sum / n;
    });
    const m = ids.reduce((t, id) => t + e[id], 0) / (ids.length || 1);
    ids.forEach((id) => { e[id] -= m; });
  }
  return e;
}

// сам рисунок в координатах SH_WD; off — данных нет (КЗ не работает): везде прочерки
function shPaintWidget(ctx, s, off, corr) {
  const { w, h, r } = SH_WD, F = (px, b) => (b ? 'bold ' : '') + px + 'px sans-serif';
  const dev = corr ? shwDeviations(s) : null;
  const bad = !off && SH_PAIRS.some((id) => s.pairs[id] && s.pairs[id].valid && s.pairs[id].alarm_angle);        // красное — только настоящий аларм по углу (допуск угла > 0 на вкладке «Валы»)
  ctx.save();
  ctx.clearRect(0, 0, w, h);
  ctx.beginPath(); ctx.roundRect(0, 0, w, h, 18); ctx.fillStyle = 'rgba(15,17,22,.92)'; ctx.fill();
  ctx.lineWidth = 2; ctx.strokeStyle = bad ? '#f87171' : 'rgba(148,163,184,.45)'; ctx.stroke();
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  SH_PAIRS.forEach((id) => {
    const p = s.pairs[id], [x, y, name] = SH_WD_PAIR[id], ok = !off && p && p.valid;
    ctx.font = F(22, true); ctx.fillStyle = !ok ? '#9ca3af' : (p.alarm_angle) ? '#f87171' : '#f3f4f6';
    const dv = corr || !Number.isFinite(p && p.angle_diff_raw) ? (p && p.angle_diff) : p.angle_diff_raw;         // «сырые» — до автокоррекции (старый сервис отдаёт только одно значение)
    ctx.fillText(ok ? shSgn(dv, 1) + '°' : '—', x, y);
    if (name) { ctx.textAlign = 'left'; ctx.font = F(13); ctx.fillStyle = '#9ca3af'; ctx.fillText(name, 14, y); ctx.textAlign = 'center'; }
  });
  SH_IDS.forEach((id) => {
    const x = s.shafts[id], [cx, cy] = SH_WD_CIRCLE[id], ok = !off && x && x.valid;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fillStyle = 'rgba(255,255,255,.08)'; ctx.fill();
    ctx.lineWidth = 1.5; ctx.strokeStyle = 'rgba(148,163,184,.55)'; ctx.stroke();
    if (ok) {                                  // стрелка вала: красная половина на угле, синяя напротив
      const a = (x.angle * Math.PI) / 180, dx = Math.sin(a) * (r - 4), dy = -Math.cos(a) * (r - 4);
      ctx.globalAlpha = x.coast ? 0.45 : 1; ctx.lineWidth = 4; ctx.lineCap = 'round';
      ctx.strokeStyle = MV_ROD_BLUE; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx - dx, cy - dy); ctx.stroke();
      ctx.strokeStyle = MV_ROD_RED; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + dx, cy + dy); ctx.stroke();
      ctx.globalAlpha = 1;
    }
    ctx.beginPath(); ctx.roundRect(cx - 27, cy - 11, 54, 22, 7); ctx.fillStyle = 'rgba(30,41,59,.95)'; ctx.fill();
    ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(148,163,184,.6)'; ctx.stroke();
    ctx.font = F(15, true); ctx.fillStyle = !ok ? '#9ca3af' : x.coast ? '#7b828f' : '#f3f4f6';       // палка не видна (удержание) — угол тусклый, как и стрелка
    const dd = dev && dev[id];
    ctx.fillText(!ok ? '—' : dev ? (Number.isFinite(dd) ? shSgn(dd, 1) + '°' : '—') : (((Math.round(x.angle) % 360) + 360) % 360) + '°', cx, cy + 1);
  });
  const [tx, ty, tw, th] = SH_WD_TOGGLE;                       // переключатель: с автокоррекцией (значки волны → ровная линия) / без
  ctx.beginPath(); ctx.roundRect(tx, ty, tw, th, th / 2);
  ctx.fillStyle = corr ? 'rgba(56,189,248,.22)' : 'rgba(148,163,184,.12)'; ctx.fill();
  ctx.lineWidth = 1; ctx.strokeStyle = corr ? 'rgba(56,189,248,.8)' : 'rgba(148,163,184,.5)'; ctx.stroke();
  ctx.lineWidth = 1.6; ctx.lineCap = 'round'; ctx.strokeStyle = corr ? '#7dd3fc' : '#9ca3af';
  ctx.beginPath();
  if (corr) { ctx.moveTo(tx + 8, ty + th / 2); ctx.lineTo(tx + 20, ty + th / 2); }
  else { ctx.moveTo(tx + 8, ty + th / 2 + 4); ctx.bezierCurveTo(tx + 11, ty + th / 2 - 8, tx + 14, ty + th / 2 + 8, tx + 17, ty + th / 2 - 4); ctx.lineTo(tx + 20, ty + th / 2 + 3); }
  ctx.stroke();
  ctx.textAlign = 'left'; ctx.font = F(11, true); ctx.fillStyle = corr ? '#7dd3fc' : '#9ca3af';
  ctx.fillText(corr ? 'авто' : 'сырые', tx + 25, ty + th / 2 + 0.5);
  ctx.restore();
}

function shwSave() {
  try { localStorage.setItem(SHW_KEY, JSON.stringify({ open: SHW.open, x: SHW.x, y: SHW.y, s: SHW.s, corr: SHW.corr })); } catch (e) { /* не критично */ }
}

// не даём окну уехать за экран (после смены размера окна браузера или масштаба виджета)
function shwClamp() {
  const w = SH_WD.w * SHW.s, h = SH_WD.h * SHW.s;
  SHW.x = Math.max(0, Math.min(window.innerWidth - w, SHW.x));
  SHW.y = Math.max(0, Math.min(window.innerHeight - h, SHW.y));
}

function shwPlace() {
  if (!SHW.el) return;
  shwClamp();
  const dpr = window.devicePixelRatio || 1, w = SH_WD.w * SHW.s, h = SH_WD.h * SHW.s;
  SHW.el.style.left = SHW.x + 'px'; SHW.el.style.top = SHW.y + 'px';
  SHW.el.style.width = w + 'px'; SHW.el.style.height = h + 'px';
  SHW.cv.width = Math.round(w * dpr); SHW.cv.height = Math.round(h * dpr);
  shwPaint();
}

function shwPaint() {
  if (!SHW.open || !SHW.cv) return;
  const s = shSnap();
  if (!s || !s.shafts || !s.pairs) return;
  const ctx = SHW.cv.getContext('2d'), k = SHW.cv.width / SH_WD.w;
  ctx.setTransform(k, 0, 0, k, 0, 0);
  shPaintWidget(ctx, s, !MV.online, SHW.corr);
}

function shwBuild() {
  if (SHW.el) return;
  const el = document.createElement('div');
  el.className = 'shw'; el.hidden = true;
  el.title = 'Разность углов валов, °. «авто» — значения после автокоррекции, около нуля; «сырые» — без неё (в кружках — угол вала). Переключатель — внизу справа. Перетащи, чтобы подвинуть; колесо мыши — размер.';
  el.innerHTML = '<canvas class="shw__cv"></canvas><button type="button" class="shw__x" title="Закрыть" aria-label="Закрыть">×</button>';
  document.body.appendChild(el);
  SHW.el = el; SHW.cv = el.querySelector('canvas');
  el.querySelector('.shw__x').addEventListener('click', (e) => { e.stopPropagation(); shwSet(false); });
  el.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.shw__x')) return;
    SHW.drag = { dx: e.clientX - SHW.x, dy: e.clientY - SHW.y, x0: e.clientX, y0: e.clientY };
    el.setPointerCapture(e.pointerId); el.classList.add('is-drag');
  });
  el.addEventListener('pointermove', (e) => {
    if (!SHW.drag) return;
    SHW.x = e.clientX - SHW.drag.dx; SHW.y = e.clientY - SHW.drag.dy;
    shwClamp(); el.style.left = SHW.x + 'px'; el.style.top = SHW.y + 'px';
  });
  const end = (e) => {
    if (!SHW.drag) return;
    const d = SHW.drag; SHW.drag = null; el.classList.remove('is-drag');
    if (e.type === 'pointerup' && Math.hypot(e.clientX - d.x0, e.clientY - d.y0) < 4) {             // клик без перетаскивания по переключателю
      const [tx, ty, tw, th] = SH_WD_TOGGLE, px = (e.clientX - SHW.x) / SHW.s, py = (e.clientY - SHW.y) / SHW.s;
      if (px >= tx - 4 && px <= tx + tw + 4 && py >= ty - 4 && py <= ty + th + 4) { SHW.corr = !SHW.corr; shwPaint(); }
    }
    shwSave();
  };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
  el.addEventListener('wheel', (e) => {
    e.preventDefault();
    SHW.s = Math.max(0.7, Math.min(2.2, SHW.s * (e.deltaY < 0 ? 1.08 : 1 / 1.08)));
    shwPlace(); shwSave();
  }, { passive: false });
  window.addEventListener('resize', () => { if (SHW.open) shwPlace(); });
}

function shwSet(on) {
  SHW.open = !!on;
  shwBuild();
  SHW.el.hidden = !SHW.open;
  document.getElementById('multiShaftsBtn')?.classList.toggle('is-on', SHW.open);
  if (SHW.open) {
    if (SHW.x === null) { SHW.x = window.innerWidth - SH_WD.w * SHW.s - 24; SHW.y = 110; }
    shwPlace();
  }
  shwSave();
}

// значок виден, когда у камер назначены валы (иначе показывать нечего); вызывается раз в секунду из опроса КЗ
function shwUpdate() {
  const s = shSnap(), btn = document.getElementById('multiShaftsBtn');
  const has = !!(s && s.shafts && SH_IDS.some((id) => s.shafts[id] && s.shafts[id].assigned));
  if (btn) btn.hidden = !has;
  if (SHW.open) shwPaint();
}

function shwInit() {
  try {
    const v = JSON.parse(localStorage.getItem(SHW_KEY) || 'null');
    if (v) { SHW.x = Number.isFinite(v.x) ? v.x : null; SHW.y = Number.isFinite(v.y) ? v.y : null; SHW.s = Number.isFinite(v.s) ? Math.max(0.7, Math.min(2.2, v.s)) : SHW.s; SHW.open = !!v.open; if (typeof v.corr === 'boolean') SHW.corr = v.corr; }
  } catch (e) { /* не критично */ }
  document.getElementById('multiShaftsBtn')?.addEventListener('click', (e) => { e.stopPropagation(); shwSet(!SHW.open); });
  if (SHW.open) shwSet(true);
}

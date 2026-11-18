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
const SH = { histAt: 0, busy: false, hist: {}, note: '', noteCls: '' };

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

// ---- цвета палок: пороги красной и синей половин по каждому валу + превью того, что попадает в маску ----
const SH_RED_DEF = [16, 165, 50, 55];
const SH_BLUE_DEF = [82, 135, 55, 60];
const SH_HSV_FIELDS = {
  red: [['оттенок до', 179, 'Красный лежит по краям круга оттенков: от 0 до этого значения и от «оттенок от» до 179'], ['оттенок от', 179, 'Красный лежит по краям круга оттенков: от 0 до «оттенок до» и от этого значения до 179'],
    ['насыщ. от', 255, 'Минимальная насыщенность. Ниже — цвет считается блёклым (корпус, фон)'], ['яркость от', 255, 'Минимальная яркость. Ниже — тень']],
  blue: [['оттенок от', 179, 'Голубой/синий: нижняя граница оттенка (0–179, шкала OpenCV)'], ['оттенок до', 179, 'Верхняя граница оттенка'],
    ['насыщ. от', 255, 'Минимальная насыщенность. Бирюзовый корпус рядом по цвету, поэтому слишком низкое значение захватывает и его'], ['яркость от', 255, 'Минимальная яркость']],
};

function shHsvOf(z, w) {
  const v = z[w + '_hsv'];
  return Array.isArray(v) && v.length === 4 ? v : (w === 'red' ? SH_RED_DEF : SH_BLUE_DEF);
}

function shColorRowHtml(w, z, a) {
  const v = shHsvOf(z, w);
  const cells = SH_HSV_FIELDS[w].map(([lbl, max, tip], k) => `<label class="sh-hsv" title="${escapeHtml(tip)}"><span>${lbl}</span>` +
    `<input type="range" ${a} data-f="hsv" data-w="${w}" data-k="${k}" min="0" max="${max}" step="1" value="${v[k]}" /><output>${v[k]}</output></label>`).join('');
  return `<div class="md-zone__row sh-hsvrow"><b class="sh-hsvname" style="color:${w === 'red' ? MV_ROD_RED : MV_ROD_BLUE}">${w === 'red' ? 'красная' : 'синяя'} половина</b>${cells}</div>`;
}

function mdRodColorsHtml() {
  const blocks = [];
  mdCfgCams().forEach((c) => {
    (c.zones || []).forEach((z, i) => {
      if (z.type !== 'cycle' || z.enabled === false) return;
      const a = `data-md="z" data-cam="${escapeHtml(c.id)}" data-i="${i}"`;
      const sh = shShaftOf(c.id, z.id);
      const k = escapeHtml(c.id + '/' + z.id);
      blocks.push(`<div class="md-zone sh-col" data-shcol="${k}" data-cam="${escapeHtml(c.id)}" data-zone="${escapeHtml(z.id)}">
        <div class="md-zone__ctl">
          <div class="md-zone__row"><b>${escapeHtml(sh ? sh.short : z.name)}</b><span class="net-hint">${escapeHtml(c.name)}</span><span class="md-sp"></span>
            <button type="button" class="btn-secondary md-x" ${a} data-f="hsvreset" title="Вернуть пороги цвета этого вала к значениям по умолчанию">по умолчанию</button></div>
          ${shColorRowHtml('red', z, a)}${shColorRowHtml('blue', z, a)}
        </div>
        <div class="md-zone__chart sh-prev"><img alt="" data-shprev="${k}" /><div class="net-hint" data-shprevn="${k}"></div></div>
      </div>`);
    });
  });
  if (!blocks.length) return '<span class="net-hint">Размеченных валов нет: нужны зоны-цикл («Разметить валы»).</span>';
  return `<div class="net-hint">Красные точки на картинке — то, что детектор считает красной половиной палки, голубые — синей. Двигайте пороги и смотрите, чтобы палка была целиком, а корпус и фон не светились. Изменения сохраняются сразу.</div>${blocks.join('')}`;
}

// превью: пробные пороги берём прямо с ползунков, сервис возвращает кадр вокруг оси с масками; не чаще раза в 4 с на вал, при правке — сразу
const shPrevAt = {};
function shHsvStr(block, w) {
  return [0, 1, 2, 3].map((k) => { const r = block.querySelector(`input[data-w="${w}"][data-k="${k}"]`); return r ? r.value : ''; }).join(',');
}
function shPrevLoad(block, force) {
  const k = block.dataset.shcol, img = block.querySelector('img[data-shprev]'), note = block.querySelector('[data-shprevn]');
  if (!img || (!force && Date.now() - (shPrevAt[k] || 0) < 4000)) return;
  shPrevAt[k] = Date.now();
  const url = '/api/motion/rod_mask?camera=' + encodeURIComponent(block.dataset.cam) + '&zone=' + encodeURIComponent(block.dataset.zone) +
    '&red=' + shHsvStr(block, 'red') + '&blue=' + shHsvStr(block, 'blue') + '&t=' + Date.now();
  fetch(url).then((r) => (r.ok ? r.blob() : Promise.reject(r.status))).then((b) => {
    const u = URL.createObjectURL(b);
    const old = img.src;
    img.src = u;
    if (old && old.startsWith('blob:')) URL.revokeObjectURL(old);
    if (note) note.textContent = '';
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
    <div class="net-hint" id="md-sh-note"></div>
    ${pairs}`;
}

function mdShaftPairHtml(id, p) {
  const a = `data-md="sh" data-id="${id}"`;
  return `<div class="md-zone md-mirror" data-shpair="${id}">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><b>${SH_PAIR_NAME[id]}</b><span class="net-hint">${SH_PAIR_NOTE[id]}</span><span class="md-sp"></span>
        <span class="cv-bit" id="md-shb-${id}" title="Тревога: разность больше допуска (угла или скорости)">–</span></div>
      <div class="md-zone__row"><span class="md-lbl" title="Отклонение углов от эталона, °. Левый и правый вал одного ряда — зеркально (сумма), левые и правые валы рядов — одинаково (разность)">Δ угла</span><span class="md-num" id="md-sha-p-${id}">–</span>
        <span class="md-lbl" title="|скорость первого| − |скорость второго|, об/мин. Направление вращения не важно">Δ скорости</span><span class="md-num" id="md-shr-${id}">–</span></div>
      <div class="md-zone__row md-mx-set"><span class="md-lbl" title="Тревога, когда |Δ угла| не меньше допуска. 0 — тревога по углу выключена">допуск угла, °</span>${mdNum(a + ' data-f="tol_angle"', p.tol_angle, 'min="0" max="180" step="0.5"')}
        <span class="md-lbl" title="Какое значение считать нулём. Камеры смотрят под разными ракурсами, поэтому ноль снимается вручную, когда валы стоят в нужном положении">эталон, °</span>${mdNum(a + ' data-f="ref"', p.ref, 'min="-180" max="180" step="0.5"')}
        <button type="button" class="btn-secondary md-x" ${a} data-f="calib" title="Принять текущее положение валов за ноль разности углов (нужны оба вала в кадре)">эталон = сейчас</button></div>
      <div class="md-zone__row md-mx-set"><span class="md-lbl" title="Тревога, когда |Δ скорости| не меньше допуска. 0 — тревога по скорости выключена">допуск скорости, об/мин</span>${mdNum(a + ' data-f="tol_rpm"', p.tol_rpm, 'min="0" step="0.1"')}</div>
      <div class="md-zone__row md-mx-set"><span class="md-lbl" title="Регистр: Δ угла в 0,1° со знаком (uint16). Адрес не задан — в ПЛК не пишется. ПЛК включается на вкладке «ПЛК · сценарии · группы»">Δ угла → ПЛК</span>${mdOutCell(p.out_angle, a, false, 'oa', [['', 'нет'], ['holding', 'регистр']])}
        <span class="md-lbl" title="Регистр: Δ скорости в 0,01 об/мин со знаком (uint16)">Δ скорости → ПЛК</span>${mdOutCell(p.out_rpm, a, false, 'or', [['', 'нет'], ['holding', 'регистр']])}</div>
    </div>
    <div class="md-zone__chart md-mx2">
      <div class="md-mxc"><span class="md-mxc__t">Δ угла, °</span><canvas id="md-shc-a-${id}" height="64"></canvas></div>
      <div class="md-mxc"><span class="md-mxc__t">Δ скорости, об/мин</span><canvas id="md-shc-r-${id}" height="64"></canvas></div>
    </div>
  </div>`;
}

const shClamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

async function mdOnShafts(t) {
  const sc = shCfg();
  if (!sc) return;
  const id = t.dataset.id, f = t.dataset.f;
  if (f === 'calib') { shCalibrate(id); return; }
  const out = (cur, kindF) => (f === kindF ? (t.value ? { kind: t.value, address: (cur && cur.address) || 0 } : null) : cur);
  mdEdit(() => {
    if (SH_PAIRS.includes(id)) {
      const p = sc.pairs[id];
      if (f === 'tol_angle') p.tol_angle = shClamp(parseFloat(t.value) || 0, 0, 180);
      else if (f === 'tol_rpm') p.tol_rpm = Math.max(0, parseFloat(t.value) || 0);
      else if (f === 'ref') p.ref = shClamp(parseFloat(t.value) || 0, -180, 180);
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
  });
  if (SH.noteCls === '' && !SH.note) shSetNote('', '');
  mdShaftsHist();
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
  const ca = mdEl('md-shc-a-' + id), cr = mdEl('md-shc-r-' + id);
  const pc = (shCfg() || { pairs: {} }).pairs[id] || {};
  if (ca) { const m = span(h.angle, 5); mdDrawSeries(ca, h.t, h.angle, ok, -m, m, { lines: [{ v: 0, color: '#64748b' }].concat(pc.tol_angle ? [{ v: pc.tol_angle, color: '#f59e0b' }, { v: -pc.tol_angle, color: '#f59e0b' }] : []) }); }
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

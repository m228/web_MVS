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
  return `<details class="md-det" open><summary>Ряды камер ${mdHelp('Задайте каждой камере ряд валов. Левый и правый вал в ряду определяются по положению оси в кадре: левее — левый. У камеры должны быть размечены две зоны-цикла (кнопка «Разметить валы»).')}</summary>
      <div class="md-camlist">${rows || '<span class="net-hint">В сервисе движения камер нет.</span>'}</div></details>
    <details class="md-det" open><summary>Скорости валов ${mdHelp('Скорость вала — обороты в минуту по наклону угла за окно (в карточке зоны). Регистр пишется ×100; 0 — нет данных. То же, что «скорость» в зоне-цикле, только по названию вала.')}</summary>
      <table class="md-table"><thead><tr><th>Вал</th><th>Откуда</th><th>Угол</th><th>Скорость</th><th>Регистр скорости в ПЛК</th></tr></thead><tbody>${shafts}</tbody></table></details>
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

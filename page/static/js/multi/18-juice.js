// ---------- КЗ · вкладка «Сок»: прозрачность сока в стаканах ----------
// Круг на каждом стакане (зона типа clarity), по цвету внутри круга сервис считает мутность 0–100: 0 — цвет «чистого» сока, 100 — цвет «грязного».
// Оба цвета задаёт оператор: наливает чистый сок, ждёт и жмёт «чистый»; на мутном — «грязный». «Чистый» у каждого стакана свой (свет разный),
// «грязный» — общий для всех стаканов (сдвиг цвета от «чистого») или свой у стакана. Расчёт — motion_service/motion_engine.py (ClarityRT);
// тут только разметка, калибровка, живые значения и тренд за 2 часа. Круги на кадре рисует 10-motion.js (drawClarityOnTile).
const JC_WIN = 7200;                                         // тренд за 2 часа (дальше сервис в быстрой истории не хранит)
const JC_SEC = [[10, '10 с'], [30, '30 с'], [60, '1 мин'], [120, '2 мин']];
const JC_SEC_KEY = 'mvs_jc_sec';
const jcLoadSec = () => { try { const v = Number(localStorage.getItem(JC_SEC_KEY)); if (JC_SEC.some(([x]) => x === v)) return v; } catch (e) { /* не критично */ } return 30; };
const JC = { sec: jcLoadSec(), msg: {}, hist: {}, stats: {}, at: 0, busy: false };
const JC_SPREAD_WARN = 6;                                     // разброс цвета за период (Lab) больше — цвет плавал, эталон шаткий

const jcZones = (cam) => ((cam && cam.zones) || []).filter((z) => z.type === 'clarity');

// номер следующего стакана: после самого большого из уже размеченных (id j1, j2, …)
function jcNextNumber(cam) {
  const n = jcZones(cam).map((z) => parseInt(String(z.id).replace(/^\D+/, ''), 10)).filter(Number.isFinite);
  return n.length ? Math.max(...n) + 1 : 1;
}

// «Добавить стаканы» из окна разметки: по кружку на каждую кликнутую точку, нумерация продолжается
function jcMarkSave(m, cam, fw, fh) {
  const pts = m.pts.slice();
  if (!pts.length) return;
  mdEdit(() => {
    const fs = cam.frame_size || [fw, fh];
    cam.frame_size = fs;
    const n0 = jcNextNumber(cam);
    pts.forEach((p, i) => cam.zones.push({
      id: 'j' + (n0 + i), name: 'Стакан ' + (n0 + i), enabled: true, type: 'clarity', metric: 'flow', points: [],
      on: 0, off: 0, invert: false, track_flow: false, smooth: 15, frames_to_on: 1, frames_to_off: 1, blur: 0, flicker_comp: false, out: null,
      center: [Math.round(p[0] * fs[0] / fw), Math.round(p[1] * fs[1] / fh)], radius: mdRodRadius(fs, m.rodPct),
      core: 0.7, glare_pct: 10, off_max: 1, clean: null, dirty: null, dirty_mode: 'shared',
    }));
  }, true);
  log.success('КЗ: стаканы добавлены', { serial: cam.id, count: pts.length });
  closeMarkModal();
  MD.tab = 'juice';
  MD.key = '';
  renderMotionDock();
}

// ---- вкладка ----
const jcHasClean = (z) => Array.isArray(z.clean);
const jcHasDirty = (cam, z) => (z.dirty_mode === 'own' ? Array.isArray(z.dirty) : Array.isArray(cam.clarity_shift) && jcHasClean(z));
const jcMark = (ok, tip) => `<span class="jc-ok${ok ? ' is-set' : ''}" title="${escapeHtml(tip)}">${ok ? '✓' : '—'}</span>`;

function jcRowHtml(cam, z) {
  const i = cam.zones.indexOf(z), a = `data-md="jc" data-cam="${escapeHtml(cam.id)}" data-i="${i}"`;
  const k = escapeHtml(cam.id + '-' + z.id);
  const own = z.dirty_mode === 'own';
  return `<div class="md-zone jc-row" id="jc-r-${k}">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><input type="text" ${a} data-f="name" value="${escapeHtml(z.name)}" title="Название сборника. Двойной клик по кружку на кадре тоже переименует" />
        <span class="jc-v" id="jc-v-${k}" title="Мутность 0–100: 0 — прозрачный, 100 — самый мутный">—</span><span class="jc-trk"><i id="jc-b-${k}"></i></span>
        <button type="button" class="btn-secondary md-x" ${a} data-f="del" title="Удалить стакан">✕</button></div>
      <div class="md-zone__row">
        <button type="button" class="btn-secondary md-x" ${a} data-f="clean" title="В этом стакане сейчас чистый сок: его цвет за последние ${JC.sec} с запомнится как «чистый» (0)">чистый</button>${jcMark(jcHasClean(z), 'Цвет «чистого» сока этого стакана задан')}
        <button type="button" class="btn-secondary md-x" ${a} data-f="dirty" title="В этом стакане сейчас самый мутный сок (100). ${own ? 'Цвет запомнится как «грязный» только этого стакана' : 'Запомнится сдвиг цвета от «чистого» этого стакана — он общий для всех стаканов в режиме «общий»'}">грязный</button>${jcMark(jcHasDirty(cam, z), own ? 'Свой цвет «грязного» задан' : 'Общий сдвиг «грязного» задан, и у стакана есть «чистый»')}
        <select ${a} data-f="dmode" title="«Грязный» общий: один сдвиг цвета от «чистого» на все стаканы (мутный сок нужен лишь в одном). «Свой»: у этого стакана собственный цвет «грязного»">${mdSel([['shared', 'грязный: общий'], ['own', 'грязный: свой']], z.dirty_mode || 'shared')}</select></div>
      <div class="jc-st" id="jc-s-${k}"></div>
      <details class="jc-more"><summary>положение, отбор точек, ПЛК</summary>
        <div class="md-zone__row"><span class="md-lbl" title="Центр кружка на кадре (пиксели кадра разметки)">центр x, y</span>${mdNum(a + ' data-f="cx"', Math.round(z.center[0]), 'min="0" step="1"')}${mdNum(a + ' data-f="cy"', Math.round(z.center[1]), 'min="0" step="1"')}
          <span class="md-lbl" title="Радиус кружка, пиксели кадра разметки">радиус</span>${mdNum(a + ' data-f="radius"', z.radius, 'min="3" step="1"')}</div>
        <div class="md-zone__row"><span class="md-lbl" title="Какая часть радиуса берётся для цвета: меньше — дальше от стенки стакана">берём, %</span>${mdNum(a + ' data-f="core"', Math.round((z.core || 0.7) * 100), 'min="20" max="100" step="5"')}
          <span class="md-lbl" title="Сколько самых ярких точек круга отбросить (блики, пена). 0 — ничего не отбрасывать">блики, %</span>${mdNum(a + ' data-f="glare"', z.glare_pct == null ? 10 : z.glare_pct, 'min="0" max="50" step="1"')}
          <span class="md-lbl" title="Цвет дальше от шкалы «чистый — грязный», чем столько её длин, не считается мутностью (рука, чужой предмет). 0 — не проверять">не на шкале</span>${mdNum(a + ' data-f="offmax"', z.off_max == null ? 1 : z.off_max, 'min="0" max="100" step="0.1"')}</div>
        <div class="md-zone__row"><span class="md-lbl">мутность в ПЛК</span>${mdOutCell(z.out, a, false, 'o', [['', 'нет'], ['holding', 'регистр']])}<span class="net-hint" title="0–100; нет значения (нет эталонов, кадра, цвет не на шкале) — 65535, чтобы не спутать с «чистый»">регистр: 0–100, нет данных — 65535</span></div>
      </details>
    </div>
    <div class="md-zone__chart"><canvas id="jc-ch-${k}" height="64"></canvas></div>
  </div>`;
}

function jcCamHtml(cam, all) {
  const zs = jcZones(cam);
  return (all ? `<div class="md-camhead"><b>${escapeHtml(cam.name)}${mdIpTag(cam.url)}</b></div>` : '') +
    `<div class="md-zone__row jc-bar"><span class="md-lbl" title="За сколько последних секунд берётся цвет для эталона (медиана)">эталон по последним</span>
      <select data-md="jc" data-f="sec">${mdSel(JC_SEC, JC.sec)}</select>
      <button type="button" class="btn-secondary md-x" data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="cleanall" title="Налейте во все стаканы чистый сок, подождите и нажмите: цвет каждого стакана запомнится как «чистый» (0)">Все стаканы — чистые</button>
      <span class="net-hint" id="jc-m-${escapeHtml(cam.id)}">${escapeHtml(JC.msg[cam.id] || '')}</span></div>` +
    zs.map((z) => jcRowHtml(cam, z)).join('');
}

function jcTabHtml(mode, cam) {
  if (mode !== 'all' && !(cam && cam.enabled)) return '<p class="net-hint">Включите КЗ переключателем справа сверху — для этой камеры появятся зоны.</p>';
  const cams = mdShownCams().filter((c) => jcZones(c).length);
  if (!cams.length) {
    return '<p class="net-hint">Стаканов нет. Выберите камеру со стаканами, нажмите «Разметить стаканы» и кликните по центру каждого по порядку. ' +
      'Дальше: налейте чистый сок и нажмите «чистый» у стакана (или «Все стаканы — чистые»), потом на мутном — «грязный».</p>';
  }
  return `<div class="md-zones">${cams.map((c) => jcCamHtml(c, mode === 'all')).join('')}</div>`;
}

// ---- калибровка: цвет за последние секунды с сервиса → эталон в конфиге ----
function jcSay(camId, text, bad) {
  JC.msg[camId] = text;
  const el = mdEl('jc-m-' + camId);
  if (el) { el.textContent = text; el.style.color = bad ? 'var(--danger)' : ''; }
}

async function jcCalibrate(cam, ids, kind) {
  jcSay(cam.id, 'снимаю цвет…');
  const r = await mvJson('/api/motion/clarity_sample', { camera: cam.id, zones: ids, seconds: JC.sec });
  if (!r || r.error || !r.zones) { jcSay(cam.id, 'не вышло: ' + ((r && (r.error || r.detail)) || 'нет ответа сервиса'), true); return; }
  const done = [], bad = [], shaky = [];
  mdEdit(() => {
    ids.forEach((id) => {
      const z = cam.zones.find((q) => q.id === id), s = r.zones[id];
      if (!z) return;
      if (!s || s.error) { bad.push(z.name + ': ' + ((s && s.error) || 'нет данных')); return; }
      if (s.spread > JC_SPREAD_WARN) shaky.push(z.name);
      if (kind === 'clean') { z.clean = s.lab; done.push(z.name); return; }
      if (z.dirty_mode === 'own') { z.dirty = s.lab; done.push(z.name); return; }
      if (!z.clean) { bad.push(z.name + ': сначала «чистый» этого стакана'); return; }
      cam.clarity_shift = s.lab.map((v, k) => Math.round((v - z.clean[k]) * 100) / 100);        // общий сдвиг «грязного» от «чистого» этого стакана
      done.push(z.name);
    });
  }, true);
  const what = kind === 'clean' ? '«чистый»' : '«грязный»';
  const parts = [];
  if (done.length) parts.push(what + ' задан: ' + (done.length > 3 ? done.length + ' стаканов' : done.join(', ')));
  if (shaky.length) parts.push('цвет плавал за период (' + shaky.join(', ') + ') — подождите и повторите');
  if (bad.length) parts.push(bad.join('; '));
  jcSay(cam.id, parts.join(' · ') || 'нечего задавать', !done.length || !!bad.length);
}

function jcOnEvent(t) {
  const f = t.dataset.f;
  if (f === 'sec') {
    JC.sec = Number(t.value);
    try { localStorage.setItem(JC_SEC_KEY, String(JC.sec)); } catch (e) { /* не критично */ }
    return;
  }
  const cam = mvCam(t.dataset.cam);
  if (!cam) return;
  if (f === 'cleanall') { jcCalibrate(cam, jcZones(cam).map((z) => z.id), 'clean'); return; }
  const z = cam.zones[Number(t.dataset.i)];
  if (!z || z.type !== 'clarity') return;
  if (f === 'clean' || f === 'dirty') { jcCalibrate(cam, [z.id], f); return; }
  if (f === 'del') {
    if (!confirm('Удалить «' + z.name + '»?')) return;
    mdEdit(() => {
      cam.zones.splice(cam.zones.indexOf(z), 1);
      (MV.cfg.scenarios || []).forEach((s) => { s.sources = s.sources.filter((x) => !(x.camera === cam.id && x.zone === z.id)); });
    }, true);
    return;
  }
  mdEdit(() => {
    if (f === 'name') z.name = t.value;
    else if (f === 'dmode') z.dirty_mode = t.value;
    else if (f === 'cx' || f === 'cy') z.center[f === 'cx' ? 0 : 1] = Math.max(0, parseFloat(t.value) || 0);
    else if (f === 'radius') z.radius = Math.max(3, parseFloat(t.value) || 3);
    else if (f === 'core') z.core = Math.max(0.2, Math.min(1, (parseFloat(t.value) || 70) / 100));
    else if (f === 'glare') z.glare_pct = Math.max(0, Math.min(50, parseFloat(t.value) || 0));
    else if (f === 'offmax') z.off_max = Math.max(0, Math.min(100, parseFloat(t.value) || 0));
    else if (f === 'okind') z.out = t.value ? { kind: 'holding', address: (z.out && z.out.address) || 0 } : null;
    else if (f === 'oaddr' && z.out) z.out.address = parseInt(t.value, 10) || 0;
  }, f === 'okind' || f === 'dmode');
}

// ---- живые значения и тренд ----
const jcFmt = (v) => String(Math.round(Number(v)));

function jcPaintLive() {
  mdShownCams().forEach((c) => {
    const res = (MV.states[c.id] || {}).result;
    const alive = !!(res && res.alive && !res.corrupt);
    const sr = JC.stats[c.id];
    jcZones(c).forEach((zc) => {
      const k = c.id + '-' + zc.id;
      const z = alive ? ((res.zones || []).find((q) => q.id === zc.id)) : null;
      const v = mdEl('jc-v-' + k), b = mdEl('jc-b-' + k), s = mdEl('jc-s-' + k);
      const ok = !!(z && z.valid);
      if (v) { v.textContent = ok ? jcFmt(z.turb) : '—'; v.style.color = ok ? mvClarityColor(z.turb) : ''; }
      if (b) { b.style.width = ok ? Math.max(2, z.turb) + '%' : '0'; b.style.background = ok ? mvClarityColor(z.turb) : ''; }
      if (s) {
        const st = sr && sr.zones && sr.zones[zc.id] && sr.zones[zc.id].flow;
        const stat = st ? ` · за 2 ч: ср ${jcFmt(st.avg)} · мин ${jcFmt(st.min)} · макс ${jcFmt(st.max)}` : '';
        s.textContent = ok ? 'цвет L ' + z.lab[0].toFixed(0) + ' a ' + z.lab[1].toFixed(0) + ' b ' + z.lab[2].toFixed(0) + stat
          : !alive ? (res && (res.reason || 'нет данных')) || 'нет данных' : z ? z.reason + (z.lab ? ' · цвет L ' + z.lab[0].toFixed(0) + ' a ' + z.lab[1].toFixed(0) + ' b ' + z.lab[2].toFixed(0) : '') : 'зона ещё не считается (ждём кадр)';
      }
    });
  });
  if (Date.now() - JC.at > 10000) jcRefresh(false);
}

async function jcRefresh(force) {
  if (MD.tab !== 'juice' || !MV.dockOpen || JC.busy) return;
  if (!force && Date.now() - JC.at < 10000) return;
  const cams = mdShownCams().filter((c) => jcZones(c).length);
  if (!cams.length) return;
  JC.busy = true;
  JC.at = Date.now();
  try {
    const first = document.querySelector('.jc-row .md-zone__chart canvas');
    const pts = Math.max(60, Math.min(1200, first ? first.clientWidth : 500));
    const res = await Promise.all(cams.map((c) => Promise.all([
      mvApi('/api/motion/history?camera=' + encodeURIComponent(c.id) + '&window=' + JC_WIN + '&points=' + pts),
      mvApi('/api/motion/stats?camera=' + encodeURIComponent(c.id) + '&window=' + JC_WIN)])));
    res.forEach(([h, st], n) => {
      const c = cams[n];
      if (st && !st.error && st.zones) JC.stats[c.id] = st;
      if (!h || h.error || !h.zones) return;
      jcZones(c).forEach((z) => {
        const d = h.zones[z.id], cv = mdEl('jc-ch-' + c.id + '-' + z.id);
        if (!d || !cv) return;
        mdDrawSeries(cv, h.t || [], d.flow.map((v, i) => (d.bit[i] ? v : null)), h.ok || [], 0, 100, { win: JC_WIN, h: 64, lines: [{ v: 50, color: '#64748b' }] });
      });
    });
  } finally { JC.busy = false; }
  jcPaintLive();
}

// ---------- КЗ · вкладка «Сок»: прозрачность сока в стаканах ----------
// Круг на каждом стакане (зона типа clarity), по цвету внутри круга сервис считает мутность 0–100: «чистый» стоит на 10, «грязный» на 90 (чище — ниже 10, грязнее — выше 90).
// Оба цвета задаёт оператор: наливает чистый сок, ждёт и жмёт «чистый»; на мутном — «грязный». «Чистый» у каждого стакана свой (свет разный),
// «грязный» — общий для всех стаканов (сдвиг цвета от «чистого») или свой у стакана. Расчёт — motion_service/motion_engine.py (ClarityRT);
// тут только разметка, калибровка, живые значения и тренд за 2 часа. Круги на кадре рисует 10-motion.js (drawClarityOnTile).
const JC_WIN = 7200;                                         // тренд за 2 часа (дальше сервис в быстрой истории не хранит)
const JC_SEC = [[10, '10 с'], [30, '30 с'], [60, '1 мин'], [120, '2 мин']];
const JC_LABELS = [['', '— не задана'], ['clear', 'прозрачный'], ['turbid', 'мутный'], ['dirty', 'грязный']];
const JC_BASE_STEPS = [[1, '1 мин'], [2, '2 мин'], [5, '5 мин'], [10, '10 мин']];
const JC_SEC_KEY = 'mvs_jc_sec';
const jcLoadSec = () => { try { const v = Number(localStorage.getItem(JC_SEC_KEY)); if (JC_SEC.some(([x]) => x === v)) return v; } catch (e) { /* не критично */ } return 30; };
const JC = { sec: jcLoadSec(), msg: {}, hist: {}, stats: {}, at: 0, busy: false };
const JC_SMOOTH_S = 30, JC_HOLD_S = 20;                         // сглаживание цвета (медиана, с) и выдержка смены состояния (с) по умолчанию, как в сервисе
const JC_T_TURB = 20, JC_T_DIRTY = 66;                       // пороги по умолчанию (как в сервисе, шкала 10/90): мутный с 20, грязный с 66 из 100
const JC_SPREAD_WARN = 6;                                    // разброс цвета за период (Lab) больше — цвет плавал, эталон шаткий

const jcT = (z, key, dflt) => (z && z[key] != null ? z[key] : dflt);   // порог зоны (у старых зон без порогов — по умолчанию)
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
      t_turb: JC_T_TURB, t_dirty: JC_T_DIRTY, hyst: 2, scale: 2,
    }));
  }, true);
  log.success('КЗ: стаканы добавлены', { serial: cam.id, count: pts.length });
  closeMarkModal();
  MD.tab = 'juice';
  MD.key = '';
  renderMotionDock();
}

// ---- вкладка ----
const jcShared = (cam) => cam.clarity_refs !== 'own';          // один «чистый» и один «грязный» на все стаканы (по умолчанию)
const jcMed = (arrs) => [0, 1, 2].map((k) => { const v = arrs.map((c) => c[k]).sort((x, y) => x - y), m = v.length >> 1; return Math.round((v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2) * 100) / 100; });
// общий «чистый» камеры; нет — медиана «чистых» стаканов (как в сервисе)
const jcBaseClean = (cam) => {
  if (Array.isArray(cam.clarity_clean)) return cam.clarity_clean;
  const own = cam.zones.filter((q) => Array.isArray(q.clean)).map((q) => q.clean);
  return own.length ? jcMed(own) : null;
};
const jcHasClean = (cam, z) => (jcShared(cam) ? !!jcBaseClean(cam) : Array.isArray(z.clean) || !!jcBaseClean(cam));
const jcHasDirty = (cam, z) => (!jcShared(cam) && z.dirty_mode === 'own' ? Array.isArray(z.dirty) : Array.isArray(cam.clarity_shift) && jcHasClean(cam, z));
const jcMark = (ok, tip) => `<span class="jc-ok${ok ? ' is-set' : ''}" title="${escapeHtml(tip)}">${ok ? '✓' : '—'}</span>`;

function jcRowHtml(cam, z) {
  const i = cam.zones.indexOf(z), a = `data-md="jc" data-cam="${escapeHtml(cam.id)}" data-i="${i}"`;
  const k = escapeHtml(cam.id + '-' + z.id);
  const shared = jcShared(cam), own = !shared && z.dirty_mode === 'own';
  return `<div class="md-zone jc-row" id="jc-r-${k}">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><input type="text" ${a} data-f="name" value="${escapeHtml(z.name)}" title="Название сборника. Двойной клик по кружку на кадре тоже переименует" />
        <span class="jc-v" id="jc-v-${k}" title="Мутность 0–100: «чистый» эталон = 10, «грязный» = 90; чище — ниже, грязнее — выше">—</span><span class="jc-w" id="jc-w-${k}"></span><span class="jc-trk"><i id="jc-b-${k}"></i></span>
        <button type="button" class="btn-secondary md-x" ${a} data-f="del" title="Удалить стакан">✕</button></div>
      <div class="md-zone__row">
        <button type="button" class="btn-secondary md-x" ${a} data-f="clean" title="В этом стакане сейчас чистый сок: его цвет за последние ${JC.sec} с запомнится как «чистый» (10)${shared ? ' — один на все стаканы' : ''}">чистый</button>${jcMark(jcHasClean(cam, z), jcShared(cam) ? 'Общий «чистый» на все стаканы задан' : Array.isArray(z.clean) ? 'Цвет «чистого» сока этого стакана задан' : 'Свой «чистый» не снят — берётся общий камеры')}
        <button type="button" class="btn-secondary md-x" ${a} data-f="dirty" title="В этом стакане сейчас самый мутный сок (90). ${shared ? 'Цвет запомнится как «грязный» для всех стаканов' : own ? 'Цвет запомнится как «грязный» только этого стакана' : 'Запомнится сдвиг цвета от «чистого» этого стакана — он общий для всех стаканов в режиме «общий»'}">грязный</button>${jcMark(jcHasDirty(cam, z), own ? 'Свой цвет «грязного» задан' : 'Общий сдвиг «грязного» задан, и есть «чистый»')}
        ${shared ? '' : `<select ${a} data-f="dmode" title="«Грязный» общий: один сдвиг цвета от «чистого» на все стаканы (мутный сок нужен лишь в одном). «Свой»: у этого стакана собственный цвет «грязного»">${mdSel([['shared', 'грязный: общий'], ['own', 'грязный: свой']], z.dirty_mode || 'shared')}</select>`}</div>
      <div class="md-zone__row"><span class="md-lbl" title="Ваша метка этого стакана для базы: что в нём на самом деле. Пишется в index.csv вместе с цветом, потом сравним с авто-состоянием">метка для базы</span>
        <select ${a} data-f="label">${mdSel(JC_LABELS, z.label || '')}</select></div>
      <div class="jc-st" id="jc-s-${k}"></div>
      <details class="jc-more"><summary>положение, отбор точек, ПЛК</summary>
        <div class="md-zone__row"><span class="md-lbl" title="Центр кружка на кадре (пиксели кадра разметки)">центр x, y</span>${mdNum(a + ' data-f="cx"', Math.round(z.center[0]), 'min="0" step="1"')}${mdNum(a + ' data-f="cy"', Math.round(z.center[1]), 'min="0" step="1"')}
          <span class="md-lbl" title="Радиус кружка, пиксели кадра разметки">радиус</span>${mdNum(a + ' data-f="radius"', z.radius, 'min="3" step="1"')}
          <span class="md-lbl" title="Сдвинуть кружок на 1 пиксель. Мышью его можно таскать прямо на кадре">сдвиг</span>${[['←', -1, 0], ['→', 1, 0], ['↑', 0, -1], ['↓', 0, 1]].map(([s, dx, dy]) => `<button type="button" class="btn-secondary md-x" ${a} data-f="nudge" data-dx="${dx}" data-dy="${dy}">${s}</button>`).join('')}</div>
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
  const jb = cam.juice_base || { on: false, every_min: 2 };
  return (all ? `<div class="md-camhead"><b>${escapeHtml(cam.name)}${mdIpTag(cam.url)}</b></div>` : '') +
    `<div class="md-zone__row jc-bar"><span class="md-lbl" title="За сколько последних секунд берётся цвет для эталона (медиана)">эталон по последним</span>
      <select data-md="jc" data-f="sec">${mdSel(JC_SEC, JC.sec)}</select>
      <span class="md-lbl" title="С какой мутности сок считается мутным (оранжевый). Ниже — прозрачный (зелёный). Общий для всех стаканов камеры">мутный от</span>${mdNum(`data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="tturb"`, jcT(zs[0], 't_turb', JC_T_TURB), 'min="0" max="99" step="1"')}
      <span class="md-lbl" title="С какой мутности сок считается грязным (красный). Общий для всех стаканов камеры">грязный от</span>${mdNum(`data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="tdirty"`, jcT(zs[0], 't_dirty', JC_T_DIRTY), 'min="1" max="100" step="1"')}
      <span class="md-lbl" title="Инерция значения. Цвет подтягивается к измеренному медленно (за столько секунд), а если отклонение держится — разгоняется. Пар над стаканом, блик, рука не успевают его сдвинуть, настоящее изменение подхватывается за это время. 0 — без сглаживания">сглаживание, с</span>${mdNum(`data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="tsmooth"`, jcT(zs[0], 'smooth_s', JC_SMOOTH_S), 'min="0" max="600" step="5"')}
      <span class="md-lbl" title="Состояние (прозрачный / мутный / грязный) меняется, только если новое держится столько секунд подряд. 0 — сразу">держать, с</span>${mdNum(`data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="thold"`, jcT(zs[0], 'hold_s', JC_HOLD_S), 'min="0" max="3600" step="5"')}
      <button type="button" class="btn-secondary md-x" data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="cleanall" title="Налейте во все стаканы чистый сок, подождите и нажмите: цвет каждого стакана запомнится как «чистый» (10)">Все стаканы — чистые</button>
      <span class="net-hint" id="jc-m-${escapeHtml(cam.id)}">${escapeHtml(JC.msg[cam.id] || '')}</span></div>` +
    `<div class="md-zone__row jc-bar"><label class="switch-toggle md-sw md-sw--sm" title="Раз в N минут сервис пишет кадр и цвет, мутность, состояние и вашу метку каждого стакана в папку data\juice_base\${escapeHtml(cam.id)}\<день>. Для набора базы, например на ночь. Старше 14 суток удаляется"><span class="switch"><input type="checkbox" data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="jbon"${jb.on ? ' checked' : ''} /><span class="switch-track"></span></span><span class="md-lbl md-lbl--on" style="white-space:nowrap">собирать базу</span></label>
      <span class="md-lbl">каждые</span><select data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="jbstep">${mdSel(JC_BASE_STEPS, jb.every_min)}</select>
      <label class="switch-toggle md-sw md-sw--sm" title="Включено: один «чистый» и один «грязный» на все стаканы — снимаются кнопками на любом стакане. Выключено: у каждого стакана свои"><span class="switch"><input type="checkbox" data-md="jc" data-cam="${escapeHtml(cam.id)}" data-f="refs"${jcShared(cam) ? ' checked' : ''} /><span class="switch-track"></span></span><span class="md-lbl md-lbl--on" style="white-space:nowrap">один эталон на все</span></label>
      <span class="net-hint" title="Общий «чистый» и сдвиг «грязного» от него (Lab). Снимаются кнопками «чистый» и «грязный» на любом стакане">${(() => { const c = jcBaseClean(cam), d = cam.clarity_shift; return (c ? 'чистый: L ' + c[0].toFixed(0) + ' a ' + c[1].toFixed(0) + ' b ' + c[2].toFixed(0) : 'чистый не задан') + ' · ' + (Array.isArray(d) ? 'грязный: ΔL ' + d[0].toFixed(0) + ' Δa ' + d[1].toFixed(0) + ' Δb ' + d[2].toFixed(0) : 'грязный не задан — нажмите «грязный» на мутном стакане'); })()}</span></div>` +
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
  const done = [], bad = [], shaky = [], cleans = [], shared = jcShared(cam);
  mdEdit(() => {
    ids.forEach((id) => {
      const z = cam.zones.find((q) => q.id === id), s = r.zones[id];
      if (!z) return;
      if (!s || s.error) { bad.push(z.name + ': ' + ((s && s.error) || 'нет данных')); return; }
      if (s.spread > JC_SPREAD_WARN) shaky.push(z.name);
      if (kind === 'clean') {
        if (shared) cleans.push(s.lab);                    // общий режим: один «чистый» на все стаканы (по нескольким стаканам — медиана)
        else { z.clean = s.lab; if (!Array.isArray(cam.clarity_clean)) cam.clarity_clean = s.lab; }   // свой; первый снятый становится общим для стаканов без своего
        done.push(z.name);
        return;
      }
      if (!shared && z.dirty_mode === 'own') { z.dirty = s.lab; done.push(z.name); return; }
      const cl = shared ? jcBaseClean(cam) : (z.clean || jcBaseClean(cam));
      if (!cl) { bad.push(z.name + ': сначала снимите «чистый» хотя бы у одного стакана'); return; }
      cam.clarity_shift = s.lab.map((v, k) => Math.round((v - cl[k]) * 100) / 100);        // общий сдвиг «грязного» от «чистого» этого стакана
      done.push(z.name);
    });
    if (cleans.length) cam.clarity_clean = jcMed(cleans);
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
  if (f === 'refs') { mdEdit(() => { cam.clarity_refs = t.checked ? 'shared' : 'own'; }, true); jcSay(cam.id, t.checked ? 'один эталон на все стаканы' : 'у каждого стакана свой эталон'); return; }
  if (f === 'jbon' || f === 'jbstep') {                       // сбор базы: на всю камеру
    mdEdit(() => {
      const jb = cam.juice_base = Object.assign({ on: false, every_min: 2 }, cam.juice_base);
      if (f === 'jbon') jb.on = !!t.checked; else jb.every_min = Number(t.value) || 2;
    }, false);
    jcSay(cam.id, f === 'jbon' ? (t.checked ? 'база пишется' : 'база выключена') : '');
    return;
  }
  if (f === 'tsmooth' || f === 'thold') {                     // сглаживание и выдержка: одним числом на все стаканы камеры
    const key = f === 'tsmooth' ? 'smooth_s' : 'hold_s', v = Math.max(0, Math.min(f === 'tsmooth' ? 600 : 3600, parseFloat(t.value) || 0));
    mdEdit(() => jcZones(cam).forEach((z) => { z[key] = v; }), false);
    t.value = v;
    jcSay(cam.id, f === 'tsmooth' ? 'сглаживание: ' + v + ' с' : 'состояние держится: ' + v + ' с');
    return;
  }
  if (f === 'tturb' || f === 'tdirty') {                      // пороги одним числом на все стаканы камеры; «мутный» обязан быть ниже «грязного» — иначе сервис отклонит конфиг
    const v = Math.max(0, Math.min(100, parseFloat(t.value) || 0)), zs = jcZones(cam);
    const lo = f === 'tturb' ? v : jcT(zs[0], 't_turb', JC_T_TURB), hi = f === 'tdirty' ? v : jcT(zs[0], 't_dirty', JC_T_DIRTY);
    if (lo >= hi) { jcSay(cam.id, 'порог «мутный» должен быть ниже «грязного»', true); t.value = jcT(zs[0], f === 'tturb' ? 't_turb' : 't_dirty', f === 'tturb' ? JC_T_TURB : JC_T_DIRTY); return; }
    mdEdit(() => zs.forEach((z) => { z[f === 'tturb' ? 't_turb' : 't_dirty'] = v; }), false);
    jcSay(cam.id, '');
    JC.at = 0;                                                // линии порогов на графиках перерисуем при ближайшем обновлении
    return;
  }
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
  if (f === 'nudge') {
    mdEdit(() => { z.center = [Math.max(0, z.center[0] + Number(t.dataset.dx)), Math.max(0, z.center[1] + Number(t.dataset.dy))]; }, true);
    return;
  }
  mdEdit(() => {
    if (f === 'label') z.label = t.value;
    else if (f === 'name') z.name = t.value;
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
      const v = mdEl('jc-v-' + k), b = mdEl('jc-b-' + k), s = mdEl('jc-s-' + k), w = mdEl('jc-w-' + k);
      const ok = !!(z && z.valid);
      const col = ok ? mvClarityColor(z) : '';
      if (v) { v.textContent = ok ? jcFmt(z.turb) : '—'; v.style.color = col; }
      if (w) { w.textContent = ok ? MV_CLARITY_WORDS[mvClarityLevel(z)] : ''; w.style.color = col; }
      if (b) { b.style.width = ok ? Math.max(2, z.turb) + '%' : '0'; b.style.background = col; }
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
        mdDrawSeries(cv, h.t || [], d.flow.map((v, i) => (d.bit[i] ? v : null)), h.ok || [], 0, 100, { win: JC_WIN, h: 64, lines: [{ v: jcT(z, 't_turb', JC_T_TURB), color: MV_CLARITY_COLORS[1] }, { v: jcT(z, 't_dirty', JC_T_DIRTY), color: MV_CLARITY_COLORS[2] }] });
      });
    });
  } finally { JC.busy = false; }
  jcPaintLive();
}

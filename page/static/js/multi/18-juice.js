// ---------- КЗ · вкладка «Сок»: прозрачность сока в стаканах ----------
// Круг на каждом стакане (зона типа clarity), по цвету внутри круга сервис считает мутность 0–100: «чистый» стоит на 10, «грязный» на 90 (чище — ниже 10, грязнее — выше 90).
// Оба цвета задаёт оператор: наливает чистый сок, ждёт и жмёт «чистый»; на мутном — «грязный». «Чистый» у каждого стакана свой (свет разный),
// «грязный» — общий для всех стаканов (сдвиг цвета от «чистого») или свой у стакана. Расчёт — motion_service/motion_engine.py (ClarityRT);
// тут только разметка, калибровка, живые значения и тренды за 2 часа. Круги на кадре рисует 10-motion.js (drawClarityOnTile).
// Вид строки (22.11): слева значок состояния, имя, число и иконки-кнопки; справа два графика друг над другом — мутность и движение («сок есть / пусто»).
// Пороги — метки ▲ / ▼ на графиках: тянутся мышью, двойной клик по числу — ввести число. Слова остались только в подсказках.
const JC_WIN = 7200;                                         // тренд за 2 часа (дальше сервис в быстрой истории не хранит)
const JC_SEC = [[10, '10 с'], [30, '30 с'], [60, '1 мин'], [120, '2 мин']];
const JC_LABELS = [['', '— не задана'], ['clear', 'прозрачный'], ['turbid', 'мутный'], ['dirty', 'грязный']];
const JC_BASE_STEPS = [[1, '1 мин'], [2, '2 мин'], [5, '5 мин'], [10, '10 мин']];
const JC_SEC_KEY = 'mvs_jc_sec';
const jcLoadSec = () => { try { const v = Number(localStorage.getItem(JC_SEC_KEY)); if (JC_SEC.some(([x]) => x === v)) return v; } catch (e) { /* не критично */ } return 30; };
const JC = { sec: jcLoadSec(), msg: {}, hist: {}, stats: {}, at: 0, busy: false, open: {} };   // open — у каких стаканов раскрыты настройки (по шестерёнке)
const JC_SMOOTH_S = 30, JC_HOLD_S = 20;                         // сглаживание цвета (медиана, с) и выдержка смены состояния (с) по умолчанию, как в сервисе
const JC_T_TURB = 20, JC_T_DIRTY = 66;                       // пороги по умолчанию (как в сервисе, шкала 10/90): мутный с 20, грязный с 66 из 100
const JC_SPREAD_WARN = 6;                                    // разброс цвета за период (Lab) больше — цвет плавал, эталон шаткий
// «Сок есть / пусто» по движению в кружке (как в сервисе): пороги у каждого стакана свои, окно / сглаживание / выдержка — одни на камеру
const JC_MV_HI = 1.1, JC_MV_LO = 0.9, JC_MV_WIN_S = 10, JC_MV_SMOOTH_S = 20, JC_MV_HOLD_S = 30;
const JC_MV_UP = 1.35, JC_MV_DOWN = 1.12;                    // кнопка «пусто»: «сок есть» = шум × 1,35 (не меньше +0,2), «пусто» = шум × 1,12
const JC_H_TURB = 60, JC_H_MV = 52;                          // высота графиков, px (то же в CSS .jc-cw canvas)

const jcT = (z, key, dflt) => (z && z[key] != null ? z[key] : dflt);   // порог зоны (у старых зон без порогов — по умолчанию)
const jcZones = (cam) => ((cam && cam.zones) || []).filter((z) => z.type === 'clarity');
const jcMvOn = (cam) => jcZones(cam).some((z) => z.mv_on);

// ---- иконки (линейные, 24×24; цвет — currentColor) ----
const JC_DROP = 'M12 3.2c3.6 4.2 6 7.2 6 10.2a6 6 0 0 1-12 0c0-3 2.4-6 6-10.2z';
const JC_P = {
  drop: `<path d="${JC_DROP}"/>`,
  half: `<path d="${JC_DROP}"/><path d="M6 13.4h12a6 6 0 0 1-12 0z" fill="currentColor"/>`,
  full: `<path d="${JC_DROP}" fill="currentColor"/>`,
  off: `<path d="${JC_DROP}" stroke-dasharray="2.2 2.4"/><path d="M4 4l16 16"/>`,
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
  tag: '<path d="M3.5 12.5v-8h8L20 13l-8 8z"/><circle cx="8" cy="9" r="1.2"/>',
  plug: '<path d="M9 3v5M15 3v5M6.5 8h11v3a5.5 5.5 0 0 1-11 0z"/><path d="M12 16.5V21"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  wave: '<path d="M2.5 12c2.5-6 5-6 7.5 0s5 6 7.5 0 3-3 4-3"/>',
  win: '<path d="M3 7h18v10H3z"/><path d="M8 7v10M13 7v10"/>',
  db: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  core: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4"/>',
  sun: '<circle cx="12" cy="12" r="3.5"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/>',
  ruler: '<path d="M3 9h18v6H3z"/><path d="M7 9v3M11 9v4M15 9v3"/>',
  pin: '<circle cx="12" cy="12" r="3"/><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/>',
  motion: '<path d="M2.5 14l4-6 4 8 4-10 4 8h3"/>',
};
const jcIcon = (n) => `<svg class="jc-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${JC_P[n]}</svg>`;
const JC_STATE_ICON = ['drop', 'half', 'full', 'off'];       // прозрачный, мутный, грязный, пусто (как уровни mvClarityLevel)

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
// sRGB <-> Lab (D65, как cv2.COLOR_BGR2Lab у сервиса: L 0..100): палитра задаёт эталон руками
const jcLin = (c) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
const jcGam = (c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055);
function jcHexToLab(hex) {
  const r = jcLin(parseInt(hex.slice(1, 3), 16) / 255), g = jcLin(parseInt(hex.slice(3, 5), 16) / 255), b = jcLin(parseInt(hex.slice(5, 7), 16) / 255);
  const f = (t) => (t > 216 / 24389 ? Math.cbrt(t) : (24389 / 27 * t + 16) / 116);
  const fx = f((0.412453 * r + 0.357580 * g + 0.180423 * b) / 0.950456), fy = f(0.212671 * r + 0.715160 * g + 0.072169 * b), fz = f((0.019334 * r + 0.119193 * g + 0.950227 * b) / 1.088754);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)].map((v) => Math.round(v * 100) / 100);
}
function jcLabToHex(lab) {
  const fy = (lab[0] + 16) / 116, fx = fy + lab[1] / 500, fz = fy - lab[2] / 200;
  const fi = (t) => (t * t * t > 216 / 24389 ? t * t * t : (116 * t - 16) * 27 / 24389);
  const x = 0.950456 * fi(fx), y = fi(fy), z = 1.088754 * fi(fz);
  const lin = [3.240479 * x - 1.537150 * y - 0.498535 * z, -0.969256 * x + 1.875992 * y + 0.041556 * z, 0.055648 * x - 0.204043 * y + 1.057311 * z];
  return '#' + lin.map((v) => Math.round(255 * Math.max(0, Math.min(1, jcGam(Math.max(0, Math.min(1, v)))))).toString(16).padStart(2, '0')).join('');
}
// цвет «грязного» сейчас: свой, либо «чистый» + общий сдвиг; нет — null
function jcDirtyLab(cam, z) {
  if (!jcShared(cam) && z.dirty_mode === 'own') return Array.isArray(z.dirty) ? z.dirty : null;
  const cl = jcShared(cam) ? jcBaseClean(cam) : (z.clean || jcBaseClean(cam));
  return cl && Array.isArray(cam.clarity_shift) ? cl.map((v, k) => v + cam.clarity_shift[k]) : null;
}
// палитра: значок 🎨, поверх него невидимый выбор цвета (клик открывает окно выбора); применяется при закрытии окна
const jcPalette = (a, f, lab, tip) => `<span class="jc-pal" title="${escapeHtml(tip)}">🎨<input type="color" ${a} data-f="${f}" value="${jcLabToHex(lab || [50, 0, 0])}" /></span>`;

const jcCk = (ok) => (ok ? '<i class="jc-ck">✓</i>' : '');                         // зелёная галочка в углу кнопки: эталон задан
const jcA = (cam, z) => `data-md="jc" data-cam="${escapeHtml(cam.id)}" data-i="${cam.zones.indexOf(z)}"`;
const jcNum = (a, f, v, extra) => `<input type="number" class="jc-n" ${a} data-f="${f}" value="${escapeHtml(v)}" ${extra || ''} />`;
const jcLbl = (icon, tip, inner) => `<span class="jc-lb" title="${escapeHtml(tip)}">${jcIcon(icon)}${inner}</span>`;
// метка порога на графике: ▲ / ▼ и число; тянется мышью, двойной клик — ввести число
const jcTag = (w, tip) => `<span class="jc-tag jc-tag--${w}" data-w="${w}" title="${escapeHtml(tip)}"><i>${w === 'lo' ? '▼' : '▲'}</i><b></b></span>`;

function jcRowHtml(cam, z) {
  const a = jcA(cam, z), k = escapeHtml(cam.id + '-' + z.id);
  const shared = jcShared(cam), own = !shared && z.dirty_mode === 'own', mv = jcMvOn(cam), open = !!JC.open[cam.id + '-' + z.id];
  const labCol = { clear: 'var(--success)', turbid: 'var(--warning)', dirty: 'var(--danger)' }[z.label] || '';
  return `<div class="jc-row" id="jc-r-${k}">
    <div class="jc-l">
      <div class="jc-l1"><span class="jc-state" id="jc-i-${k}" title="Нет значения"></span>
        <input type="text" class="jc-name" ${a} data-f="name" value="${escapeHtml(z.name)}" title="Название сборника. Двойной клик по кружку на кадре тоже переименует" />
        <span class="jc-v" id="jc-v-${k}" title="Мутность 0–100: «чистый» эталон = 10, «грязный» = 90; чище — ниже, грязнее — выше">—</span><span class="jc-trk"><i id="jc-b-${k}"></i></span>
        <button type="button" class="jc-ib jc-x" ${a} data-f="del" title="Удалить стакан">${jcIcon('x')}</button></div>
      <div class="jc-l2">
        <button type="button" class="jc-ib" ${a} data-f="clean" title="В этом стакане сейчас чистый сок: его цвет за последние ${JC.sec} с запомнится как «чистый» (10)${shared ? ' — один на все стаканы' : ''}. ${jcHasClean(cam, z) ? 'Задан' : 'Не задан'}">${jcIcon('drop')}${jcCk(jcHasClean(cam, z))}</button>${jcPalette(a, 'clean_pick', jcBaseClean(cam) && (shared ? jcBaseClean(cam) : (z.clean || jcBaseClean(cam))), 'Задать цвет «чистого» руками: темнее или светлее. Сейчас показан текущий эталон')}
        <button type="button" class="jc-ib" ${a} data-f="dirty" title="В этом стакане сейчас самый мутный сок (90). ${shared ? 'Цвет запомнится как «грязный» для всех стаканов' : own ? 'Цвет запомнится как «грязный» только этого стакана' : 'Запомнится сдвиг цвета от «чистого» этого стакана — он общий для всех стаканов в режиме «общий»'}. ${jcHasDirty(cam, z) ? 'Задан' : 'Не задан'}">${jcIcon('half')}${jcCk(jcHasDirty(cam, z))}</button>${jcPalette(a, 'dirty_pick', jcDirtyLab(cam, z), 'Задать цвет «грязного» руками: чем он дальше от «чистого», тем шире шкала 10–90. Сейчас показан текущий эталон')}
        ${mv ? `<button type="button" class="jc-ib" ${a} data-f="mvcal" title="В этом стакане сейчас пусто: шум движения за последние ${JC.sec} с запомнится, пороги ▲ и ▼ на графике движения встанут сами (шум × ${JC_MV_UP} и × ${JC_MV_DOWN})">${jcIcon('off')}</button>` : ''}
        <label class="jc-ib jc-lab" title="Ваша метка стакана для базы: что в нём на самом деле. Пишется в index.csv вместе с цветом, потом сравним с авто-состоянием${z.label ? ' (сейчас: ' + (JC_LABELS.find((x) => x[0] === z.label) || [0, z.label])[1] + ')' : ''}" ${labCol ? `style="color:${labCol}"` : ''}>${jcIcon('tag')}<select ${a} data-f="label">${mdSel(JC_LABELS, z.label || '')}</select></label>
        <button type="button" class="jc-ib${open ? ' is-on' : ''}" ${a} data-f="gear" title="Положение, отбор точек, ПЛК">${jcIcon('gear')}</button>
        ${z.out ? `<span class="jc-plc" title="Мутность уходит в регистр ${z.out.address} (0–100, пусто — 0, нет данных — 65535)">${jcIcon('plug')}${z.out.address}</span>` : ''}</div>
      <div class="jc-st" id="jc-s-${k}"></div>
    </div>
    <div class="jc-charts">
      <div class="jc-cw" data-cam="${escapeHtml(cam.id)}" data-i="${cam.zones.indexOf(z)}" data-kind="turb"><canvas id="jc-ch-${k}" height="${JC_H_TURB}"></canvas>
        <span class="jc-cap" title="Мутность за 2 часа">${jcIcon('drop')}</span>
        ${jcTag('turb', 'Мутный от — общий порог всех стаканов камеры. Тяните мышью, двойной клик — ввести число')}${jcTag('dirty', 'Грязный от — общий порог всех стаканов камеры. Тяните мышью, двойной клик — ввести число')}</div>
      ${mv ? `<div class="jc-cw" data-cam="${escapeHtml(cam.id)}" data-i="${cam.zones.indexOf(z)}" data-kind="mv"><canvas id="jc-mv-${k}" height="${JC_H_MV}"></canvas>
        <span class="jc-cap" title="Движение в кружке: насколько отличаются средние старой и новой половин последних кадров. Сок течёт — выше, стакан пуст — на уровне шума">${jcIcon('motion')}<b id="jc-mvv-${k}"></b></span>
        ${jcTag('hi', 'Выше — сок есть. Тяните мышью, двойной клик — ввести число')}${jcTag('lo', 'Ниже — стакан пуст (мутность 0, серый кружок). Между метками состояние не меняется. Тяните мышью, двойной клик — ввести число')}</div>` : ''}
    </div>
    <div class="jc-set" id="jc-set-${k}"${open ? '' : ' hidden'}>
      <div class="jc-card">
        ${jcLbl('pin', 'Центр кружка на кадре (пиксели кадра разметки). Двигать можно и мышью прямо на картинке', `x${jcNum(a, 'cx', Math.round(z.center[0]), 'min="0" step="1"')} y${jcNum(a, 'cy', Math.round(z.center[1]), 'min="0" step="1"')}`)}
        ${jcLbl('core', 'Радиус кружка, пиксели кадра разметки', 'r' + jcNum(a, 'radius', z.radius, 'min="3" step="1"'))}
        ${jcLbl('core', 'Какая часть радиуса берётся для цвета, %: меньше — дальше от стенки стакана', '<em>%</em>' + jcNum(a, 'core', Math.round((z.core || 0.7) * 100), 'min="20" max="100" step="5"'))}
        ${jcLbl('sun', 'Сколько самых ярких точек круга отбросить, % (блики, пена). 0 — ничего не отбрасывать', jcNum(a, 'glare', z.glare_pct == null ? 10 : z.glare_pct, 'min="0" max="50" step="1"'))}
        ${jcLbl('ruler', 'Цвет дальше от шкалы «чистый — грязный», чем столько её длин, не считается мутностью (рука, чужой предмет). 0 — не проверять', jcNum(a, 'offmax', z.off_max == null ? 1 : z.off_max, 'min="0" max="100" step="0.1"'))}
        ${shared ? '' : `<select ${a} data-f="dmode" title="«Грязный» общий: один сдвиг цвета от «чистого» на все стаканы (мутный сок нужен лишь в одном). «Свой»: у этого стакана собственный цвет «грязного»">${mdSel([['shared', 'грязный: общий'], ['own', 'грязный: свой']], z.dirty_mode || 'shared')}</select>`}
      </div>
      <div class="jc-card jc-card--plc" title="Куда уходит мутность этого стакана: 0–100; пустой стакан (нет движения) — 0; нет значения (нет эталонов, кадра, цвет не на шкале) — 65535, чтобы не спутать с «чистый»">
        ${jcIcon('plug')}${mdOutCell(z.out, a, false, 'o', [['', 'нет'], ['holding', 'регистр']])}<span class="jc-hint">0–100 · пусто 0 · нет данных 65535</span>
      </div>
    </div>
  </div>`;
}

function jcCamHtml(cam, all) {
  const zs = jcZones(cam), cid = escapeHtml(cam.id), c = `data-md="jc" data-cam="${cid}"`;
  const jb = cam.juice_base || { on: false, every_min: 2 }, mv = jcMvOn(cam);
  const sw = (f, on, label, tip) => `<label class="switch-toggle md-sw md-sw--sm" title="${escapeHtml(tip)}"><span class="switch"><input type="checkbox" ${c} data-f="${f}"${on ? ' checked' : ''} /><span class="switch-track"></span></span><span class="md-lbl md-lbl--on" style="white-space:nowrap">${label}</span></label>`;
  const refs = (() => { const b = jcBaseClean(cam), d = cam.clarity_shift; return (b ? 'чистый: L ' + b[0].toFixed(0) + ' a ' + b[1].toFixed(0) + ' b ' + b[2].toFixed(0) : 'чистый не задан') + ' · ' + (Array.isArray(d) ? 'грязный: ΔL ' + d[0].toFixed(0) + ' Δa ' + d[1].toFixed(0) + ' Δb ' + d[2].toFixed(0) : 'грязный не задан — нажмите «грязный» на мутном стакане'); })();
  return (all ? `<div class="md-camhead"><b>${escapeHtml(cam.name)}${mdIpTag(cam.url)}</b></div>` : '') +
    `<div class="jc-bar">
      ${jcLbl('clock', 'За сколько последних секунд берётся цвет (и шум движения) для эталона — медиана', `<select data-md="jc" data-f="sec">${mdSel(JC_SEC, JC.sec)}</select>`)}
      ${jcLbl('wave', 'Инерция цвета, с. Цвет подтягивается к измеренному медленно, а если отклонение держится — разгоняется. Пар, блик, рука не успевают его сдвинуть. 0 — без сглаживания', jcNum(c, 'tsmooth', jcT(zs[0], 'smooth_s', JC_SMOOTH_S), 'min="0" max="600" step="5"'))}
      ${jcLbl('clock', 'Состояние (прозрачный / мутный / грязный) меняется, только если новое держится столько секунд подряд. 0 — сразу', jcNum(c, 'thold', jcT(zs[0], 'hold_s', JC_HOLD_S), 'min="0" max="3600" step="5"'))}
      <button type="button" class="jc-ib" ${c} data-f="cleanall" title="Налейте во все стаканы чистый сок, подождите и нажмите: цвет каждого стакана запомнится как «чистый» (10)">${jcIcon('drop')}все</button>
      <span class="jc-sep"></span>
      ${sw('jbon', jb.on, jcIcon('db'), 'Раз в N минут сервис пишет кадр и цвет, мутность, состояние и вашу метку каждого стакана в папку data\\juice_base\\' + cam.id + '\\<день>. Для набора базы, например на ночь. Старше 14 суток удаляется')}
      <select ${c} data-f="jbstep" title="Как часто писать базу">${mdSel(JC_BASE_STEPS, jb.every_min)}</select>
      ${sw('refs', jcShared(cam), 'один эталон', 'Включено: один «чистый» и один «грязный» на все стаканы — снимаются кнопками на любом стакане. Выключено: у каждого стакана свои. ' + refs)}
      <span class="jc-sep"></span>
      ${sw('mvon', mv, 'пусто по движению', 'Включено: нет движения в кружке — стакан пуст (серый кружок «пусто», мутность 0, в ПЛК 0); есть движение — сок есть, мутность считается по цвету. Для стаканов, из которых временно убрали сок')}
      ${mv ? `${jcLbl('win', 'Сколько последних секунд кадров сравниваем: окно делится пополам, движение — разница средних картинок половин. Больше — устойчивее к шуму, но медленнее', jcNum(c, 'mvwin', jcT(zs[0], 'mv_win_s', JC_MV_WIN_S), 'min="2" max="120" step="1"'))}
      ${jcLbl('wave', 'Медиана движения за столько секунд: короткие всплески (рука, пар) не меняют значение. 0 — без сглаживания', jcNum(c, 'mvsmooth', jcT(zs[0], 'mv_smooth_s', JC_MV_SMOOTH_S), 'min="0" max="600" step="5"'))}
      ${jcLbl('clock', '«Сок есть» / «пусто» меняется, только если новое состояние держится столько секунд подряд. Сок вводят постепенно — резко он не пропадает. 0 — сразу', jcNum(c, 'mvhold', jcT(zs[0], 'mv_hold_s', JC_MV_HOLD_S), 'min="0" max="3600" step="5"'))}
      <button type="button" class="jc-ib" ${c} data-f="mvcalall" title="Во всех стаканах сейчас пусто: у каждого шум движения за последние ${JC.sec} с запомнится, пороги встанут сами">${jcIcon('off')}все</button>` : ''}
      <span class="jc-msg" id="jc-m-${cid}">${escapeHtml(JC.msg[cam.id] || '')}</span>
    </div>` + zs.map((z) => jcRowHtml(cam, z)).join('');
}

function jcTabHtml(mode, cam) {
  if (mode !== 'all' && !(cam && cam.enabled)) return '<p class="net-hint">Включите КЗ переключателем справа сверху — для этой камеры появятся зоны.</p>';
  const cams = mdShownCams().filter((c) => jcZones(c).length);
  if (!cams.length) {
    return '<p class="net-hint">Стаканов нет. Выберите камеру со стаканами, нажмите «Разметить стаканы» и кликните по центру каждого по порядку. ' +
      'Дальше: налейте чистый сок и нажмите каплю («чистый») у стакана или «все» в шапке, потом на мутном — полукаплю («грязный»).</p>';
  }
  return `<div class="md-zones jc-zones">${cams.map((c) => jcCamHtml(c, mode === 'all')).join('')}</div>`;
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

// «пусто»: шум движения за последние секунды (сервис отдаёт его вместе с цветом) → пороги зоны. «Сок есть» = шум × 1,35 (не меньше +0,2), «пусто» = шум × 1,12.
async function jcCalibrateMove(cam, ids) {
  jcSay(cam.id, 'снимаю шум движения…');
  const r = await mvJson('/api/motion/clarity_sample', { camera: cam.id, zones: ids, seconds: JC.sec });
  if (!r || r.error || !r.zones) { jcSay(cam.id, 'не вышло: ' + ((r && (r.error || r.detail)) || 'нет ответа сервиса'), true); return; }
  const done = [], bad = [];
  mdEdit(() => {
    ids.forEach((id) => {
      const z = cam.zones.find((q) => q.id === id), s = r.zones[id];
      if (!z) return;
      if (!s || s.error) { bad.push(z.name + ': ' + ((s && s.error) || 'нет данных')); return; }
      if (!s.move) { bad.push(z.name + ': движение ещё не посчитано (подождите около ' + Math.round(jcT(z, 'mv_win_s', JC_MV_WIN_S) + jcT(z, 'mv_smooth_s', JC_MV_SMOOTH_S) / 2) + ' с после включения)'); return; }
      const fl = s.move.floor;
      z.mv_hi = Math.round(Math.max(fl * JC_MV_UP, fl + 0.2) * 100) / 100;
      z.mv_lo = Math.round(fl * JC_MV_DOWN * 100) / 100;
      done.push(z.name + ' ' + fl.toFixed(2));
    });
  }, true);
  const parts = [];
  if (done.length) parts.push('пусто: шум ' + (done.length > 3 ? done.length + ' стаканов' : done.join(', ')) + ' — пороги заданы');
  if (bad.length) parts.push(bad.join('; '));
  jcSay(cam.id, parts.join(' · ') || 'нечего задавать', !done.length || !!bad.length);
}

// ---- пороги-метки на графиках ----
// w: turb / dirty — мутность (общие на все стаканы камеры, целые 0–100, turb < dirty); hi / lo — движение («сок есть» выше «пусто», у каждого стакана свои, шаг 0,01).
// clamp: при перетаскивании значение подрезается по допустимому, при вводе числа вне допустимого — отказ с текстом. → '' (применено) или текст, почему нет
function jcApplyThr(cam, z, w, v, clamp) {
  if (!Number.isFinite(v)) return 'не число';
  const zs = jcZones(cam);
  if (w === 'turb' || w === 'dirty') {
    const tt = jcT(zs[0], 't_turb', JC_T_TURB), td = jcT(zs[0], 't_dirty', JC_T_DIRTY);
    const [lo, hi] = w === 'turb' ? [0, td - 1] : [tt + 1, 100];
    v = Math.round(v);
    if (v < lo || v > hi) { if (!clamp) return w === 'turb' ? '«мутный» должен быть от 0 до ' + (td - 1) + ' (ниже «грязного»)' : '«грязный» должен быть от ' + (tt + 1) + ' до 100 (выше «мутного»)'; v = Math.max(lo, Math.min(hi, v)); }
    zs.forEach((q) => { q.t_turb = w === 'turb' ? v : tt; q.t_dirty = w === 'dirty' ? v : td; });
    return '';
  }
  const hi = jcT(z, 'mv_hi', JC_MV_HI), lo = jcT(z, 'mv_lo', JC_MV_LO);
  const [mn, mx] = w === 'hi' ? [lo + 0.02, 1000] : [0, hi - 0.02];
  v = Math.round(v * 100) / 100;
  if (v < mn || v > mx) { if (!clamp) return w === 'hi' ? '«сок есть» должен быть выше «пусто» (' + lo.toFixed(2) + ')' : '«пусто» должно быть ниже «сок есть» (' + hi.toFixed(2) + ')'; v = Math.round(Math.max(mn, Math.min(mx, v)) * 100) / 100; }
  z.mv_hi = w === 'hi' ? v : hi;
  z.mv_lo = w === 'lo' ? v : lo;
  return '';
}

// метки встают на высоту своих линий (та же шкала, что у mdDrawSeries: нижняя полоска 13 px, поле 6 px сверху)
function jcPlaceTags(cv, hh, lo, hi, items) {
  const wrap = cv.parentElement, plotH = hh - 13;
  wrap.dataset.lo = lo; wrap.dataset.hi = hi; wrap.dataset.hh = hh;
  items.forEach(([w, v]) => {
    const el = wrap.querySelector('.jc-tag[data-w="' + w + '"]');
    if (!el) return;
    // верхняя метка пары (dirty, hi) стоит над своей линией, нижняя (turb, lo) — под ней: близкие пороги не наезжают друг на друга
    const y = plotH - ((v - lo) / (hi - lo)) * (plotH - 6) - 1, up = w === 'dirty' || w === 'hi';
    el.style.top = Math.max(0, Math.min(hh - 18, Math.round(up ? y - 18 : y))) + 'px';
    const b = el.querySelector('b');
    if (b) b.textContent = w === 'hi' || w === 'lo' ? v.toFixed(2) : String(Math.round(v));
  });
}

// оба графика стакана по истории h; в диапазон движения тянутся пороги (верх шкалы подбирается по данным и порогам)
function jcDrawZone(c, z, h, fixTop) {
  const d = h && h.zones && h.zones[z.id];
  if (!d) return;
  const k = c.id + '-' + z.id, cv = mdEl('jc-ch-' + k);
  if (cv) {
    const tt = jcT(z, 't_turb', JC_T_TURB), td = jcT(z, 't_dirty', JC_T_DIRTY);
    mdDrawSeries(cv, h.t || [], d.flow.map((v, i) => (d.bit[i] ? v : null)), h.ok || [], 0, 100, { win: JC_WIN, h: JC_H_TURB, lines: [{ v: tt, color: MV_CLARITY_COLORS[1] }, { v: td, color: MV_CLARITY_COLORS[2] }] });
    jcPlaceTags(cv, JC_H_TURB, 0, 100, [['turb', tt], ['dirty', td]]);
  }
  const cm = mdEl('jc-mv-' + k);
  if (cm && jcMvOn(c)) {
    const hi = jcT(z, 'mv_hi', JC_MV_HI), lo = jcT(z, 'mv_lo', JC_MV_LO);
    // до 22.11 в diff лежала яркость L (15–60): такие значения при масштабе не учитываем, ещё два часа они уходят из окна
    const mx = (d.diff || []).reduce((m, v) => (v > m && v < 20 ? v : m), 0);
    const top = fixTop || Math.min(20, Math.max(3, Math.ceil(Math.max(mx * 1.1, hi * 1.8))));     // fixTop: пока метку тянут, шкала стоит — иначе она убегает за мышью
    mdDrawSeries(cm, h.t || [], (d.diff || []).map((v) => (v > 0 && v < 20 ? v : null)), h.ok || [], 0, top, { win: JC_WIN, h: JC_H_MV, band: { lo, hi, color: 'rgba(74,222,128,.07)' }, lines: [{ v: hi, color: MV_CLARITY_COLORS[0] }, { v: lo, color: MV_CLARITY_COLORS[3] }] });
    jcPlaceTags(cm, JC_H_MV, 0, top, [['hi', hi], ['lo', lo]]);
  }
}

// после смены порога: мутность общая на камеру — перерисовать все стаканы, движение — только этот
function jcRedraw(cam, w, z, fixTop) {
  const h = JC.hist[cam.id];
  if (!h) return;
  (w === 'turb' || w === 'dirty' ? jcZones(cam) : [z]).forEach((q) => jcDrawZone(cam, q, h, fixTop));
}

const jcTagCtx = (tg) => { const wrap = tg.parentElement, cam = wrap && mvCam(wrap.dataset.cam); return { wrap, cam, z: cam && cam.zones[Number(wrap.dataset.i)] }; };
const JC_DRAG = { cur: null };

document.addEventListener('pointerdown', (e) => {
  const tg = e.target.closest && e.target.closest('.jc-tag');
  if (!tg || e.target.tagName === 'INPUT' || e.button) return;
  const { wrap, cam, z } = jcTagCtx(tg);
  if (!z) return;
  e.preventDefault();
  try { tg.setPointerCapture(e.pointerId); } catch (err) { /* не критично */ }
  // метка стоит рядом с линией, не на ней: запоминаем, на сколько пикселей от линии её схватили, чтобы значение не прыгало
  const w = tg.dataset.w, cv = wrap.querySelector('canvas'), r = cv.getBoundingClientRect();
  const lo = Number(wrap.dataset.lo), hi = Number(wrap.dataset.hi), plotH = Number(wrap.dataset.hh) - 13;
  const cur = w === 'turb' ? jcT(jcZones(cam)[0], 't_turb', JC_T_TURB) : w === 'dirty' ? jcT(jcZones(cam)[0], 't_dirty', JC_T_DIRTY) : w === 'hi' ? jcT(z, 'mv_hi', JC_MV_HI) : jcT(z, 'mv_lo', JC_MV_LO);
  const lineY = r.top + plotH - ((cur - lo) / (hi - lo)) * (plotH - 6) - 1;
  JC_DRAG.cur = { tg, wrap, cam, z, w, moved: false, off: e.clientY - lineY };
});

document.addEventListener('pointermove', (e) => {
  const d = JC_DRAG.cur;
  if (!d) return;
  const cv = d.wrap.querySelector('canvas'), r = cv.getBoundingClientRect();
  const lo = Number(d.wrap.dataset.lo), hi = Number(d.wrap.dataset.hi), plotH = Number(d.wrap.dataset.hh) - 13;
  const v = Math.min(hi, lo + ((plotH - 1 - (e.clientY - d.off - r.top)) / (plotH - 6)) * (hi - lo));    // выше верха шкалы не уходим
  if (!d.moved) { d.moved = true; d.tg.classList.add('is-drag'); }
  if (!jcApplyThr(d.cam, d.z, d.w, v, true)) jcRedraw(d.cam, d.w, d.z, d.w === 'hi' || d.w === 'lo' ? hi : undefined);
});

const jcDragEnd = () => {
  const d = JC_DRAG.cur;
  if (!d) return;
  JC_DRAG.cur = null;
  d.tg.classList.remove('is-drag');
  if (d.moved) { mdEdit(() => {}, false); jcSay(d.cam.id, ''); }      // правка уже в конфиге: сохраняем (автосохранение через 3 с)
};
document.addEventListener('pointerup', jcDragEnd);
document.addEventListener('pointercancel', jcDragEnd);

// двойной клик по метке: число вместо метки, Enter / уход из поля — применить, Esc — отмена
document.addEventListener('dblclick', (e) => {
  const tg = e.target.closest && e.target.closest('.jc-tag');
  if (!tg || tg.querySelector('input')) return;
  const { cam, z } = jcTagCtx(tg);
  if (!z) return;
  const w = tg.dataset.w, b = tg.querySelector('b'), mvw = w === 'hi' || w === 'lo';
  const inp = document.createElement('input');
  inp.type = 'number'; inp.step = mvw ? '0.05' : '1'; inp.value = b.textContent; inp.className = 'jc-tag-in';
  b.hidden = true;
  tg.appendChild(inp);
  inp.focus(); inp.select();
  let done = false;
  const finish = (ok) => {
    if (done) return;
    done = true;
    const v = parseFloat(inp.value);
    inp.remove(); b.hidden = false;
    if (!ok) return;
    const err = jcApplyThr(cam, z, w, v, false);
    if (err) { jcSay(cam.id, err, true); return; }
    mdEdit(() => {}, false);
    jcSay(cam.id, '');
    jcRedraw(cam, w, z);
  };
  inp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') finish(true); else if (ev.key === 'Escape') finish(false); ev.stopPropagation(); });
  inp.addEventListener('blur', () => finish(true));
});

// ручной эталон из палитры: то же, что снятие с кадра, только цвет задан рукой
function jcPick(cam, z, kind, lab) {
  const shared = jcShared(cam);
  mdEdit(() => {
    if (kind === 'clean') {
      if (shared) {
        const old = jcBaseClean(cam);
        if (old && Array.isArray(cam.clarity_shift)) cam.clarity_shift = cam.clarity_shift.map((v, k) => Math.round((v - (lab[k] - old[k])) * 100) / 100);   // «грязный» остаётся на месте: сдвиг пересчитан от нового «чистого»
        cam.clarity_clean = lab;
      } else { z.clean = lab; if (!Array.isArray(cam.clarity_clean)) cam.clarity_clean = lab; }
      return;
    }
    if (!shared && z.dirty_mode === 'own') { z.dirty = lab; return; }
    const cl = shared ? jcBaseClean(cam) : (z.clean || jcBaseClean(cam));
    if (!cl) return;
    cam.clarity_shift = lab.map((v, k) => Math.round((v - cl[k]) * 100) / 100);
  }, true);
  const cl = shared ? jcBaseClean(cam) : (z.clean || jcBaseClean(cam));
  if (kind === 'dirty' && !cl && !(!shared && z.dirty_mode === 'own')) { jcSay(cam.id, 'сначала задайте «чистый» (снимком или палитрой)', true); return; }
  jcSay(cam.id, (kind === 'clean' ? '«чистый»' : '«грязный»') + ' задан вручную: L ' + lab[0].toFixed(0) + ' a ' + lab[1].toFixed(0) + ' b ' + lab[2].toFixed(0));
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
  if (f === 'mvcalall') { jcCalibrateMove(cam, jcZones(cam).map((z) => z.id)); return; }
  if (f === 'mvon') {                                         // определение «сок есть / пусто» — на всю камеру
    mdEdit(() => jcZones(cam).forEach((z) => { z.mv_on = !!t.checked; }), true);
    jcSay(cam.id, t.checked ? 'движение определяется: подождите ~' + (JC_MV_WIN_S + JC_MV_SMOOTH_S / 2) + ' с, потом кнопка «все» с перечёркнутой каплей, если стаканы пусты' : 'определение по движению выключено');
    return;
  }
  if (f === 'mvwin' || f === 'mvsmooth' || f === 'mvhold') {   // окно, сглаживание, выдержка — одним числом на все стаканы камеры
    const key = { mvwin: 'mv_win_s', mvsmooth: 'mv_smooth_s', mvhold: 'mv_hold_s' }[f], max = { mvwin: 120, mvsmooth: 600, mvhold: 3600 }[f], min = f === 'mvwin' ? 2 : 0;
    const v = Math.max(min, Math.min(max, parseFloat(t.value) || 0));
    mdEdit(() => jcZones(cam).forEach((z) => { z[key] = v; }), false);
    t.value = v;
    jcSay(cam.id, '');
    return;
  }
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
  const z = cam.zones[Number(t.dataset.i)];
  if (!z || z.type !== 'clarity') return;
  if (f === 'clean' || f === 'dirty') { jcCalibrate(cam, [z.id], f); return; }
  if (f === 'clean_pick' || f === 'dirty_pick') { jcPick(cam, z, f === 'clean_pick' ? 'clean' : 'dirty', jcHexToLab(t.value)); return; }
  if (f === 'mvcal') { jcCalibrateMove(cam, [z.id]); return; }
  if (f === 'gear') {                                         // настройки стакана: показать / спрятать, состояние помним до перезагрузки страницы
    const k = cam.id + '-' + z.id, box = mdEl('jc-set-' + k);
    JC.open[k] = !JC.open[k];
    if (box) box.hidden = !JC.open[k];
    t.classList.toggle('is-on', !!JC.open[k]);
    return;
  }
  if (f === 'del') {
    if (!confirm('Удалить «' + z.name + '»?')) return;
    mdEdit(() => {
      cam.zones.splice(cam.zones.indexOf(z), 1);
      (MV.cfg.scenarios || []).forEach((s) => { s.sources = s.sources.filter((x) => !(x.camera === cam.id && x.zone === z.id)); });
    }, true);
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
  }, f === 'okind' || f === 'dmode' || f === 'label' || f === 'oaddr');   // метка и адрес показаны ещё и в значках строки — перерисовать
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
      const v = mdEl('jc-v-' + k), b = mdEl('jc-b-' + k), s = mdEl('jc-s-' + k), ic = mdEl('jc-i-' + k);
      const ok = !!(z && z.valid), empty = !!(z && z.empty);
      const lv = ok || empty ? mvClarityLevel(z) : -1;
      const col = lv >= 0 ? mvClarityColor(z) : '';
      if (v) { v.textContent = ok ? jcFmt(z.turb) : empty ? '0' : '—'; v.style.color = col; }
      if (b) { b.style.width = ok ? Math.max(2, z.turb) + '%' : '0'; b.style.background = col; }
      if (ic) {                                                // значок состояния: капля пустая / половина / полная / перечёркнутая; слово — в подсказке
        if (ic.dataset.lv !== String(lv)) { ic.dataset.lv = String(lv); ic.innerHTML = lv >= 0 ? jcIcon(JC_STATE_ICON[lv]) : jcIcon('drop'); }
        ic.style.color = col || 'var(--muted)';
        ic.title = lv >= 0 ? MV_CLARITY_WORDS[lv] : 'нет значения';
      }
      const mvv = mdEl('jc-mvv-' + k);
      if (mvv) {
        mvv.textContent = z && z.juice != null ? Number(z.move).toFixed(2) : alive ? '…' : '';
        mvv.style.color = z && z.juice != null ? (z.juice ? 'var(--success)' : '#94a3b8') : '';
        mvv.title = z && z.juice != null ? (z.juice ? 'сок есть' : 'пусто') : 'движение считается (' + Math.round(jcT(zc, 'mv_win_s', JC_MV_WIN_S) + jcT(zc, 'mv_smooth_s', JC_MV_SMOOTH_S) / 2) + ' с после включения)';
      }
      if (s) {
        const st = sr && sr.zones && sr.zones[zc.id] && sr.zones[zc.id].flow;
        const stat = st ? ` · за 2 ч: ср ${jcFmt(st.avg)} · мин ${jcFmt(st.min)} · макс ${jcFmt(st.max)}` : '';
        const lab = z && z.lab ? 'цвет L ' + z.lab[0].toFixed(0) + ' a ' + z.lab[1].toFixed(0) + ' b ' + z.lab[2].toFixed(0) : '';
        const txt = ok ? lab + stat
          : empty ? 'нет сока' + (lab ? ' · ' + lab : '')
          : !alive ? (res && (res.reason || 'нет данных')) || 'нет данных' : z ? z.reason + (z.lab ? ' · ' + lab : '') : 'зона ещё не считается (ждём кадр)';
        s.textContent = txt;
        s.title = txt;
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
    const first = document.querySelector('.jc-cw canvas');
    const pts = Math.max(60, Math.min(1200, first ? first.clientWidth : 500));
    const res = await Promise.all(cams.map((c) => Promise.all([
      mvApi('/api/motion/history?camera=' + encodeURIComponent(c.id) + '&window=' + JC_WIN + '&points=' + pts),
      mvApi('/api/motion/stats?camera=' + encodeURIComponent(c.id) + '&window=' + JC_WIN)])));
    res.forEach(([h, st], n) => {
      const c = cams[n];
      if (st && !st.error && st.zones) JC.stats[c.id] = st;
      if (!h || h.error || !h.zones) return;
      JC.hist[c.id] = h;                                      // кэш: метки порогов перерисовывают графики без запроса
      if (!JC_DRAG.cur) jcZones(c).forEach((z) => jcDrawZone(c, z, h));
    });
  } finally { JC.busy = false; }
  jcPaintLive();
}

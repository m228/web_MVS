// ---------- КЗ: нижняя панель — «Зоны · тренды» и «ПЛК · сценарии · группы» + окно разметки зоны ----------
// Фокус на RTSP-камере — панель этой камеры; фокуса нет (пустая ячейка или клик мимо ячеек) — общий вид: все камеры с КЗ.
// Правки применяются сразу (автосохранение с задержкой, PUT /api/motion/config).
const MD_WIN_KEY = 'mvTrendWin';
const MD_WIN_MIN = 30;
const MD_WIN_MAX = 7200;
// статистика зон (ср / мин / макс за окно): считает сервис движения, окно до суток
const MD_STAT_KEY = 'mvStatWin';
const MD_VIS_WINS = [[7200, '2 ч'], [14400, '4 ч'], [86400, '24 ч']];
const MD_VIS_KEY = 'mdVisWin';
const MD_VIS_COLORS = ['#6c7bff', '#f59e0b', '#34d399', '#f472b6'];     // левая — синяя, правая — оранжевая
function mdLoadVisWin() {
  try {
    const v = Number(localStorage.getItem(MD_VIS_KEY));
    if (MD_VIS_WINS.some(([x]) => x === v)) return v;
  } catch (e) { /* без памяти браузера — по умолчанию */ }
  return 86400;
}
const MD_STAT_WINS = [[300, '5 мин'], [900, '15 мин'], [3600, '1 ч'], [10800, '3 ч'], [28800, '8 ч'], [86400, '24 ч']];
const MD_STAT_EVERY_MS = 5000;
const MD_OPEN_KEY = 'mvZoneOpenAll';

// окно трендов, секунд (колесо мыши над графиком); запоминаем между перезапусками страницы
function mdLoadWin() {
  try {
    const v = Number(localStorage.getItem(MD_WIN_KEY));
    if (v >= MD_WIN_MIN && v <= MD_WIN_MAX) return v;
  } catch (e) { /* без памяти браузера — по умолчанию */ }
  return 1200;
}

function mdLoadStatWin() {
  try {
    const v = Number(localStorage.getItem(MD_STAT_KEY));
    if (MD_STAT_WINS.some(([s]) => s === v)) return v;
  } catch (e) { /* без памяти браузера — по умолчанию */ }
  return 10800;
}

function mdLoadOpenAll() {
  try { return localStorage.getItem(MD_OPEN_KEY) === '1'; } catch (e) { return false; }
}

const MD = {
  peekKey: '',          // что сейчас показано во всплывающем кадре (пусто — ничего)
  peekFrames: {},       // id камеры → { t, p } кадр для подсказки (кэш на 3 с)
  statWin: mdLoadStatWin(),
  visWin: mdLoadVisWin(),   // период графика «Видимость стрелок», с
  vis: {},              // id камеры → ответ /api/motion/visibility
  visAt: {},            // id камеры → когда и за какой период запрошено
  stats: {},            // id камеры → ответ /api/motion/stats
  statAt: 0,
  statBusy: false,
  tab: 'zones',
  key: '',
  saveTimer: null,
  rev: 0,
  saveLabel: '',
  saveCls: '',
  editGrp: null,        // { cam, i } — редактируемая группа (i = -1 — новая)
  trendBusy: false,
  trendDirty: false,
  win: mdLoadWin(),
  open: {},             // ключи «камера-зона» раскрытых строк окон
  openAll: mdLoadOpenAll(),   // галочка «подробно» — раскрыты все строки
  winTimer: null,
  mark: null,           // { cam, pts, cursor, fw, fh, closed }
  markBusy: false,
};
const MD_THR = { flow: [0.9, 0.6], diff: [4.0, 2.0] };
const mdEl = (id) => document.getElementById(id);

// 'cam' — в фокусе RTSP-камера; 'all' — фокуса нет (пустая ячейка / клик мимо): все камеры; 'none' — в фокусе GigE (панели нет)
function mdMode() {
  const s = focusedSource();
  if (!s) return 'all';
  return s.kind === 'rtsp' ? 'cam' : 'none';
}

function mdCamera() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return { source: null, cam: null };
  return { source, cam: mvCam(source.serial) };
}

const mdCfgCams = () => (MV.cfg && MV.cfg.cameras) || [];

// IP камеры из адреса потока — подпись справа в скобках, чтобы отличать камеры с похожими именами (логин и пароль не показываем)
function mdIp(url) {
  const m = /^[a-z]+:\/\/(?:[^@/]*@)?([^:/?#]+)/i.exec(String(url || ''));
  return m ? m[1] : '';
}
const mdIpTag = (url) => { const ip = mdIp(url); return ip ? ` <span class="md-ip">(${escapeHtml(ip)})</span>` : ''; };

// камеры, чьи зоны и тренды показываем: в фокусе — одна, иначе все с включённым КЗ
function mdShownCams() {
  if (mdMode() === 'cam') {
    const { cam } = mdCamera();
    return cam && cam.enabled ? [cam] : [];
  }
  return mdCfgCams().filter((c) => c.enabled);
}

function mdFmtWin(s) {
  if (s < 120) return Math.round(s) + ' с';
  if (s < 3600) return Math.round(s / 60) + ' мин';
  return (Math.round(s / 360) / 10).toString().replace('.', ',') + ' ч';
}

function setMotionDock(open) {
  MV.dockOpen = open;
  saveView({ dock: open });
  const dock = mdEl('motionDock');
  if (dock) dock.hidden = !open;
  document.getElementById('multiMotionBtn')?.classList.toggle('is-dock', open);
  MD.key = '';
  renderMotionDock();
  if (open) setTimeout(drawAllMotionTiles, 50);
}

// ---------- автосохранение ----------
const MD_SAVE_DELAY = 3000;   // мс тишины после последней правки, потом уходит на сервис
function mdPaintSave() {
  const el = mdEl('mdSave');
  if (el) { el.textContent = MD.saveLabel; el.className = 'motion-dock__save ' + MD.saveCls; }
}

function mdCommit() {
  MV.dirty = true;
  MD.rev += 1;
  MD.saveLabel = 'изменено, сохраню через 3 с…';
  MD.saveCls = '';
  mdPaintSave();
  clearTimeout(MD.saveTimer);
  MD.saveTimer = setTimeout(mdFlush, MD_SAVE_DELAY);
}

async function mdFlush() {
  const rev = MD.rev;
  const r = await mvJson('/api/motion/config', MV.cfg, 'PUT');
  if (r && r.error) {
    MD.saveLabel = 'не сохранено: ' + r.error;
    MD.saveCls = 'is-bad';
    MV.dirty = false;            // следующий опрос вернёт конфиг сервиса
  } else if (rev === MD.rev) {   // за время запроса новых правок не было
    MV.cfg = r;
    MV.dirty = false;
    MD.saveLabel = '✓ сохранено ' + new Date().toLocaleTimeString();
    MD.saveCls = 'is-ok';
  }
  mdPaintSave();
}

// правка → тут же в локальный конфиг, на сервис уйдёт через 3 с после последней правки
// «вести»: по цвету / по отражателям — одним переключателем на все валы камеры (под кнопкой «Разметить»)
const mdLeadZones = (cam) => (cam ? cam.zones.filter((z) => z.type === 'cycle' && z.enabled !== false && (z.tip_hsv || []).length) : []);

function mdPaintLead(cam) {
  const box = mdEl('mdLead');
  if (!box) return;
  const cyc = !!cam && cam.zones.some((z) => z.type === 'cycle' && z.enabled !== false);
  box.hidden = !cyc;
  if (!cyc) return;
  const zs = mdLeadZones(cam), n = zs.filter((z) => z.tip_lead).length;
  const cur = !zs.length ? '' : n === zs.length ? '1' : n === 0 ? '0' : 'mix';
  box.classList.toggle('is-off', !zs.length || !cam.enabled);
  box.querySelectorAll('button').forEach((b) => {
    b.classList.toggle('on', b.dataset.v === cur);
    b.disabled = !zs.length || !cam.enabled;
  });
  box.title = !zs.length ? 'Отражатели не заданы ни у одного вала: угол только по цвету (вкладка «Валы» → «Цвета палок» → «Светоотражатели»)'
    : cur === 'mix' ? 'У валов по-разному: нажмите, чтобы выровнять' : 'Чем вести угол вала: по цвету (отражатели уточняют) или по отражателям (цвет показывает, где красный конец)';
}

function mdOnLead(e) {
  const b = e.target.closest('button[data-v]');
  const { cam } = mdCamera();
  if (!b || b.disabled || !cam) return;
  mdEdit(() => { mdLeadZones(cam).forEach((z) => { z.tip_lead = b.dataset.v === '1'; }); }, true);
}

function mdEdit(mutate, rerender) {
  mutate();
  mdCommit();
  if (rerender) { MD.key = ''; renderMotionDock(); }
  drawAllMotionTiles();
}

// ---------- каркас панели ----------
function mdPill(st, conn) {
  if (!MV.online) return ['сервис КЗ не запущен', 'is-bad'];
  if (!conn) return ['КЗ выключено', ''];
  if (!st) return ['нет данных', 'is-bad'];
  if (st.result && st.result.corrupt) return ['битый поток — результаты не учитываются', 'is-warn'];
  if (!st.connected) return ['нет связи с камерой', 'is-bad'];
  if (st.result && !st.result.alive) return [st.result.reason || 'нет данных', 'is-bad'];
  const ld = mdPerfTotal(st.id).load;
  const hot = ld != null && ld >= 60;
  return ['жива · ' + st.fps + ' к/с' + (st.decoder === 'gpu' ? ' · GPU' : '') + (hot ? ' · загрузка ' + Math.round(ld) + ' %' : ''), ld != null && ld >= 100 ? 'is-bad' : hot ? 'is-warn' : 'is-ok'];
}

// ---------- замеры: сколько времени уходит на кадр (вкладка «Камеры») ----------
// Этапы одного кадра идут друг за другом в потоке камеры, поэтому их среднее складываем и сравниваем с интервалом между кадрами.
const MD_PERF_STAGES = [['frame', 'чтение кадра', 'Забрать кадр из потока и декодировать'], ['zones', 'зоны', 'Зоны «стоит / движется», в том числе шахта жома'],
  ['shafts', 'валы', 'Поиск палок по цвету'], ['juice', 'сок', 'Мутность сока по кругам']];
function mdMs(v) { return (v == null ? '—' : (Math.round(v * 10) / 10).toString().replace('.', ',')) + ' мс'; }
function mdPerfTotal(cid) {
  let sum = 0, mx = 0, budget = null, any = false;
  MD_PERF_STAGES.forEach(([k]) => {
    const m = MV.perf && MV.perf[cid + ':' + k];
    if (!m) return;
    any = true; sum += m.avg; mx += m.max;
    if (m.budget_ms) budget = m.budget_ms;
  });
  return { any, sum, max: mx, budget, load: any && budget ? 100 * sum / budget : null };
}
function mdPerfHtml(c) {
  const t = mdPerfTotal(c.id);
  if (!t.any) return '<span class="md-dim">время обработки кадра: нет данных</span>';
  const chip = (label, m, hint) => {
    const cls = m.load != null && m.load >= 100 ? ' is-bad' : m.load != null && m.load >= 60 ? ' is-warn' : '';
    return '<span class="md-perf__it' + cls + '" title="' + hint + '. Среднее ' + mdMs(m.avg) + ', p95 ' + mdMs(m.p95) + ', макс ' + mdMs(m.max) + ', кадров ' + m.n + '">'
      + label + ' <b>' + mdMs(m.avg) + '</b></span>';
  };
  let h = MD_PERF_STAGES.map(([k, label, hint]) => {
    const m = MV.perf[c.id + ':' + k];
    return m ? chip(label, m, hint) : '';
  }).join('');
  (c.groups || []).filter((g) => g.type === 'level').forEach((g) => {     // уровень декантатора считается в своём потоке раз в infer_interval_s
    const m = MV.perf[g.id + ':level'];
    if (m) h += chip('уровень', m, 'Весь круг: кадр → сервис уровня → уровень');
  });
  const cls = t.load != null && t.load >= 100 ? ' is-bad' : t.load != null && t.load >= 60 ? ' is-warn' : '';
  return '<span class="md-perf__sum' + cls + '" title="Сумма средних по этапам к интервалу между кадрами анализа (' + mdMs(t.budget) + '). Выше 100 % — не успеваем">на кадр <b>'
    + mdMs(t.sum) + '</b> из ' + mdMs(t.budget).replace(' мс', '') + ' · ' + Math.round(t.load) + ' %</span>' + h;
}

// общий вид: сколько камер с КЗ и все ли живы
function mdAllPill() {
  if (!MV.online) return ['сервис КЗ не запущен', 'is-bad'];
  const on = mdCfgCams().filter((c) => c.enabled);
  if (!on.length) return ['КЗ нигде не включено', ''];
  const bad = on.filter((c) => mdPill(MV.states[c.id], true)[1] !== 'is-ok').length;
  const rec = on.filter((c) => (MV.states[c.id] || {}).recording).length;
  return [on.length + ' с КЗ' + (bad ? ' · проблем: ' + bad : '') + (rec ? ' · пишется: ' + rec : ''), bad ? 'is-warn' : 'is-ok'];
}

function renderMotionDock() {
  const dock = mdEl('motionDock');
  if (!dock) return;
  const mode = mdMode();
  dock.hidden = !MV.dockOpen || mode === 'none';   // фокус на GigE-ячейке — панель прячется и возвращается сама
  if (dock.hidden) return;
  const { source, cam } = mdCamera();
  const all = mode === 'all';
  if (MD.tab !== 'zones' && MD.tab !== 'plc' && MD.tab !== 'cams' && MD.tab !== 'level' && MD.tab !== 'shafts' && MD.tab !== 'juice') MD.tab = 'zones';

  const cams = mdCfgCams();
  // вкладки «Уровень / Сок / Валы» есть, только когда это размечено (кнопки «Разметить …» в шапке); нет разметки — нет вкладки
  const has = {
    level: cams.some((c) => !!lvGroup(c) || c.zones.some((z) => z.type === 'level')),
    juice: cams.some((c) => c.zones.some((z) => z.type === 'clarity')),
    shafts: cams.some((c) => c.zones.some((z) => z.type === 'cycle') || (c.groups || []).some((g) => g.type === 'mirror')),
  };
  dock.querySelectorAll('[data-md-tab]').forEach((b) => { if (b.dataset.mdTab in has) b.hidden = !has[b.dataset.mdTab]; });
  if (MD.tab in has && !has[MD.tab] && MV.cfg) MD.tab = 'zones';
  const camKey = all
    ? cams.map((c) => c.id + (c.enabled ? '1' : '0') + c.zones.length + '/' + (c.groups || []).length).join(',')
    : (cam ? [cam.enabled ? 1 : 0, cam.zones.length, (cam.groups || []).length].join('/') : 'x');
  const key = [mode, source ? source.serial : '', MD.tab, MV.online, camKey, JSON.stringify(MD.editGrp),
    ((MV.cfg && MV.cfg.scenarios) || []).length, state.cameras.length, cams.length,
    MD.tab === 'plc' ? mdPlcWritesRows().map((r) => r.name + r.to).join(',') : ''].join('|');

  // шапка: название, тумблер КЗ (серый, когда выключено), разметка, запись
  dock.classList.toggle('is-cv-off', all ? !cams.some((c) => c.enabled) : !(cam && cam.enabled));
  mdEl('mdCamName').textContent = all ? 'Все камеры' : (source.label || source.serial);
  const sw = mdEl('mdSwitch');
  sw.disabled = !MV.online;
  sw.checked = !!(cam && cam.enabled);
  mdEl('mdSwitchWrap').hidden = all;
  mdEl('mdMark').hidden = all;
  mdEl('mdRec').hidden = all;
  mdEl('mdAll').hidden = all;
  mdEl('mdMark').disabled = !(cam && cam.enabled);
  mdEl('mdRec').disabled = !(cam && cam.enabled);
  mdPaintLead(all ? null : cam);
  mdEl('mdRecAll').hidden = !all;
  mdEl('mdRecZip').hidden = !all;
  mdEl('mdRecAll').disabled = !cams.some((c) => c.enabled);
  dock.querySelectorAll('[data-md-tab]').forEach((b) => b.classList.toggle('is-active', b.dataset.mdTab === MD.tab));
  const cap = mdEl('mdCap');
  setCapLine(cap, MV.online, 'Сервис движения работает', 'Сервис движения не запущен (motion_service\\setup.bat, потом перезапуск web_MVS)');
  mdPaintSave();
  if (key === MD.key) return;
  MD.key = key;
  saveView({ tab: MD.tab });

  const body = mdEl('mdBody');
  const restoreUi = mdSnapUi(body);
  if (MD.tab === 'plc') {
    body.innerHTML = mdPlcHtml();
    mdBindPlc();
    if (MD.editGrp) mdSyncGroupEditor();
  } else if (MD.tab === 'cams') {
    body.innerHTML = mdCamsTabHtml();
  } else if (MD.tab === 'shafts') {
    body.innerHTML = mdShaftsTabHtml();
    mdShaftsHist(true);
  } else if (MD.tab === 'juice') {
    body.innerHTML = jcTabHtml(mode, cam);
    jcRefresh(true);
  } else if (MD.tab === 'level') {
    body.innerHTML = mdLevelTabHtml(mode, cam);
    lvPaintModel();
    lvRefresh(true);
  } else {
    body.innerHTML = mdZonesTabHtml(mode, cam);
    mdDrawTrends();
  }
  restoreUi();
  paintMotionDockLive();
}

// Перерисовка вкладки (выбор регистра, цвета и т.п.) пересоздаёт DOM: раскрытые <details> закрывались, а страница из-за
// схлопнувшейся высоты прыгала вверх. Запоминаем раскрытые блоки и прокрутку и возвращаем после — только в той же вкладке.
function mdSnapUi(body) {
  if (MD.snapTab !== MD.tab) { MD.snapTab = MD.tab; return () => { }; }
  const detKey = (d) => ((d.parentElement && d.parentElement.closest('[id]')) || {}).id + '|' + ((d.querySelector('summary') || {}).textContent || '').trim().slice(0, 40);
  const open = {};
  body.querySelectorAll('details').forEach((d) => { open[detKey(d)] = d.open; });
  const x = window.scrollX, y = window.scrollY, h = body.offsetHeight;
  body.style.minHeight = h + 'px';
  return () => {
    body.querySelectorAll('details').forEach((d) => { const k = detKey(d); if (k in open) d.open = open[k]; });
    body.style.minHeight = '';
    window.scrollTo(x, y);
  };
}

const mdSel = (opts, cur) => opts.map(([v, t]) => `<option value="${v}"${String(v) === String(cur) ? ' selected' : ''}>${t}</option>`).join('');
const mdNum = (attrs, v, extra) => `<input type="number" ${attrs} value="${escapeHtml(v)}" ${extra || ''} />`;
// поле «подпись слева — значение справа» для сеток настроек
const MD_TREND_HELP = 'Колесо мыши над графиком — масштаб по времени, правый край всегда «сейчас».\n' +
  'Линия — метрика зоны. Пунктир: красный — порог «on», оранжевый — «off».\n' +
  'Нижняя полоска: зелёная — крутится, красная — стоит, серая — нет данных или битый поток. Серый фон — кадры, которые не учитывались.';

// значок «?» с подсказкой при наведении — вместо абзацев пояснений под блоками
const mdHelp = (text) => `<span class="md-help" title="${escapeHtml(text)}">?</span>`;
const mdField = (label, ctl, title, cls) => `<label${title ? ` title="${escapeHtml(title)}"` : ''}${cls ? ` class="${cls}"` : ''}><span>${label}</span>${ctl}</label>`;

// ---------- вкладка «Зоны · тренды» ----------
function mdOutCell(o, attrs, withState, pre = 'o', kinds = [['', 'нет'], ['holding', 'регистр'], ['coil', 'coil']]) {
  // адрес нужен только когда вывод включён — иначе поле лишь теснит соседей по строке; pre — приставка полей (o — основной вывод, r — скорость)
  return `<div class="md-out"><select ${attrs} data-f="${pre}kind">${mdSel(kinds, o ? o.kind : '')}</select>` +
    (o && o.kind ? `<input type="number" min="0" max="65535" ${attrs} data-f="${pre}addr" value="${o.address}" title="Адрес регистра / coil" placeholder="адрес" />` : '') +
    // что писать — только у зоны с признаком вида окна: бит «движется» или состояние окна 0 пусто / 1 жом стоит / 2 движется
    (withState && o && o.kind ? `<select ${attrs} data-f="oval" title="Что писать: бит «движется» 0/1 или состояние окна: 0 — пусто, 1 — жом стоит, 2 — движется (пока пороги вида окна не заданы, пишется бит)">${mdSel([['bit', 'движение'], ['state', 'состояние окна']], o.value || 'bit')}</select>` : '') + '</div>';
}

const MD_FILL_METRICS = [['off', 'нет'], ['rel', 'яркость к металлу'], ['lum', 'яркость'], ['tex', 'зернистость']];
const MD_FILL_STATE = ['пусто', 'жом стоит', 'движется'];
const MD_FILL_HELP = 'Вид окна — есть ли в нём жом. По движению пустое окно и окно со стоящим жомом неотличимы; вид окна различает их.\n' +
  'Яркость к металлу — яркость окна, делённая на яркость металла вокруг него (свет и солнце на неё почти не влияют: жом светлый ≈ 0.9, пустое окно тёмное ≈ 0.65). Яркость — средний свет в окне, зернистость — резкость мелких деталей. Пока пороги on/off равны 0, значения только измеряются (в панели и в записи CSV), решения нет.\n' +
  'Когда пороги заданы: выше on — жом есть (с галочкой «инверсия» наоборот: ниже off — жом есть). Состояние окна: 0 пусто, 1 жом стоит, 2 движется.\n' +
  'Под порогами — график признака (2 часа назад, колесо мыши — масштаб; наведите курсор — значение и время) и среднее / мин / макс за выбранный период: так видно, чем «жом есть» отличается от «пусто». Копится в сервисе движения в памяти, после его перезапуска начинается заново.';
const MD_FILL_NAME = { rel: 'яркость к металлу', lum: 'яркость', tex: 'зернистость' };
const mdFillFmt = (m, v) => (v == null || Number.isNaN(Number(v)) ? '–' : Number(v).toFixed(m === 'lum' ? 0 : 2));

// ср / мин / макс признаков вида окна за период (st — zones[id].fill из /api/motion/stats), главный признак жирным
function mdFillStatHtml(z, cur, st) {
  const row = (m) => {
    const s = st && st[m];
    const c = cur && cur.tex !== undefined ? mdFillFmt(m, cur[m]) : '–';
    const txt = `${MD_FILL_NAME[m]}: сейчас <b class="md-zstat__cur">${c}</b>` + (s ? ` · ср ${mdFillFmt(m, s.avg)} · мин ${mdFillFmt(m, s.min)} · макс ${mdFillFmt(m, s.max)}` : ' · ср – · мин – · макс –');
    return `<div class="md-zstat__row${z.fill_metric === m ? ' is-main' : ''}">${txt}</div>`;
  };
  return (z.fill_metric === 'rel' ? row('rel') : '') + row('lum') + row('tex');       // «к металлу» считается только когда выбран (кольцо вокруг окна), у остальных он ноль
}
function mdFillText(z) {
  if (!z || z.tex === undefined) return '–';
  const base = z.fill_metric === 'lum' ? 'яркость ' + Number(z.lum).toFixed(0) : z.fill_metric === 'rel' ? 'отн. яркость ' + Number(z.rel).toFixed(2) : 'зерн. ' + Number(z.tex).toFixed(2);
  return z.state == null ? base + ' · пороги не заданы' : base + ' · ' + MD_FILL_STATE[z.state];
}

// зона слева, её тренд справа
function mdZoneCardHtml(cam, z, i) {
  const a = `data-md="z" data-cam="${escapeHtml(cam.id)}" data-i="${i}"`;
  const k = escapeHtml(cam.id + '-' + z.id);
  if (z.type === 'cycle') {
    return `<div class="md-zone">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><input type="text" ${a} data-f="name" value="${escapeHtml(z.name)}" title="${escapeHtml(z.id)}" />
        <span class="cv-bit" id="md-zb-${k}" title="Палка найдена">–</span><span class="md-num" id="md-zv-${k}" title="Угол красного конца от вертикали по часовой, °">–</span><span class="md-sp"></span>
        <button type="button" class="btn-secondary md-x" ${a} data-f="del" title="Удалить палку">✕</button></div>
      <div class="md-zstat" id="md-st-${k}" title="Угол красного конца палки от вертикали по часовой (0–360°) и уверенность поиска"></div>
      <div class="md-zone__row"><span class="md-lbl" title="Ось вала на кадре (пиксели кадра разметки): точка, вокруг которой вращается палка">ось x, y</span>${mdNum(a + ' data-f="cx"', Math.round(z.center[0]), 'min="0" step="1"')}${mdNum(a + ' data-f="cy"', Math.round(z.center[1]), 'min="0" step="1"')}
        <span class="md-lbl" title="Окружность вокруг оси, внутри которой ищется палка: должна доставать до её концов">радиус, px</span>${mdNum(a + ' data-f="radius"', z.radius, 'min="20" step="5"')}</div>
      <div class="md-zone__row"><span class="md-lbl">угол в ПЛК</span>${mdOutCell(z.out, a)}<span class="net-hint">регистр — угол в 0,1°</span></div>
      <div class="md-zone__row"><span class="md-lbl">скорость в ПЛК</span>${mdOutCell(z.rpm_out, a, false, 'r', [['', 'нет'], ['holding', 'регистр']])}<span class="net-hint" title="Знаковый int16: ↻ плюс, ↺ минус (−0,79 об/мин → 65457). Стоит — 0">регистр — об/мин ×100, со знаком</span></div>
    </div>
    <div class="md-zone__chart"><canvas id="md-ch-${k}" height="84"></canvas></div>
  </div>`;
  }
  const open = MD.openAll || MD.open[k];
  const fm = z.fill_metric && z.fill_metric !== 'off';
  const arrows = (hi, lo, extra) => `<span class="md-ar" title="включить, когда значение ${z.invert ? 'НИЖЕ off' : 'ВЫШЕ on'}">▲</span>${hi}<span class="md-ar" title="выключить, когда значение ${z.invert ? 'выше on' : 'ниже off'}">▼</span>${lo}${extra || ''}`;
  // пороги вида окна: сначала тот, при котором жом ЕСТЬ. Обычно — выше on; с инверсией — ниже off (стрелки и подписи меняются местами)
  const fillArrows = (hi, lo) => (z.fill_invert
    ? `<span class="md-ar" title="жом есть, когда значение НИЖЕ off">▼</span>${lo}<span class="md-ar" title="жома нет, когда значение выше on">▲</span>${hi}`
    : `<span class="md-ar" title="жом есть, когда значение ВЫШЕ on">▲</span>${hi}<span class="md-ar" title="жома нет, когда значение ниже off">▼</span>${lo}`);
  return `<div class="md-zr${open ? ' is-open' : ''}" id="md-zr-${k}" data-st="n">
    <div class="md-zr__head" data-zt="${k}" title="Нажмите, чтобы раскрыть настройки окна">
      <span class="md-zr__bar"></span>
      <div class="md-zr__nm"><b>${escapeHtml(z.name)}</b><span class="md-zr__pill" id="md-zp-${k}">–</span></div>
      <div class="md-zr__gz"><div class="md-gl"><span>движ.</span><i id="md-gm-${k}"></i><em id="md-zv-${k}">–</em></div>${fm ? `<div class="md-gl"><span>вид</span><i id="md-gf-${k}"></i><em id="md-gfv-${k}">–</em></div>` : ''}</div>
      <div class="md-zone__chart"><canvas id="md-ch-${k}" height="56"></canvas></div>
      <span class="md-zr__chev">⌄</span>
    </div>
    <div class="md-zr__det">
      <div class="md-zr__col">
        <div class="md-zr__t">Движение</div>
        <div class="md-zone__row"><span class="md-lbl">название</span><input type="text" ${a} data-f="name" value="${escapeHtml(z.name)}" title="${escapeHtml(z.id)}" />
          <label class="md-lbl" title="Считать оптический поток (flow) для этой зоны. Самое тяжёлое в анализе: если зона решает по diff и flow не нужен, выключите — процессор разгрузится, а flow этой зоны в графиках и записях будет 0"><input type="checkbox" ${a} data-f="trackflow"${z.metric === 'flow' ? ' checked disabled' : (z.track_flow === false ? '' : ' checked')} /> flow</label></div>
        <div class="md-zone__row"><span class="md-lbl">метрика</span><select ${a} data-f="type">${mdSel([['motion', 'motion'], ['presence', 'presence']], z.type)}</select>
          <select ${a} data-f="metric">${mdSel([['diff', 'diff'], ['flow', 'flow']], z.metric)}</select></div>
        <div class="md-zone__row"><span class="md-lbl">порог</span>${arrows(mdNum(a + ' data-f="on"', z.on, 'step="0.05" min="0"'), mdNum(a + ' data-f="off"', z.off, 'step="0.05" min="0"'))}</div>
        <div class="md-zone__row"><span class="md-lbl" title="Защита от одиночных всплесков: бит зоны меняется, только когда порог выполнен столько кадров анализа ПОДРЯД. Один проскочивший кадр счёт сбрасывает.">кадров подряд</span>${arrows(mdNum(a + ' data-f="fon"', z.frames_to_on == null ? 3 : z.frames_to_on, 'min="1" max="1000"'), mdNum(a + ' data-f="foff"', z.frames_to_off == null ? 4 : z.frames_to_off, 'min="1" max="1000"'))}</div>
        ${mdLearnHtml(cam, z, a)}
        <div class="md-zstat" id="md-st-${k}" title="Значения диффа и потока по зоне: сейчас, среднее, минимум и максимум за выбранный период (только кадры в норме). Жирным — метрика, по которой решает зона."></div>
      </div>
      <div class="md-zr__col">
        <div class="md-zr__t">Вид окна ${mdHelp(MD_FILL_HELP)}</div>
        <div class="md-zone__row"><span class="md-lbl">признак</span><select ${a} data-f="fmetric">${mdSel(MD_FILL_METRICS, z.fill_metric || 'off')}</select></div>` +
      (fm ? `<div class="md-zone__row"><span class="md-lbl">порог</span>${fillArrows(mdNum(a + ' data-f="fllon"', z.fill_on || 0, 'step="0.01" min="0" title="Верхний порог вида окна. 0 — пороги не заданы, только измерение"'), mdNum(a + ' data-f="flloff"', z.fill_off || 0, 'step="0.01" min="0" title="Нижний порог вида окна"'))}
          <label class="md-lbl" title="Перевернуть решение: жом есть, когда значение НИЖЕ порога off (пусто — когда выше on). Включайте, если жом в этом окне темнее, чем пустое окно (например, конец засвечен)."><input type="checkbox" ${a} data-f="finvert"${z.fill_invert ? ' checked' : ''} /> инверсия</label></div>
        <div class="md-zone__row"><span class="md-lbl md-fill" id="md-zf-${k}">–</span></div>
        <div class="md-zone__chart"><canvas id="md-fch-${k}" height="52"></canvas></div>
        <div class="md-zstat" id="md-fst-${k}" title="Признаки вида окна: сейчас, среднее, минимум и максимум за выбранный период (только кадры в норме). Жирным — признак, по которому решает окно."></div>` : '') +
      (z.out ? `<div class="md-zone__row"><span class="md-lbl" title="Прямой вывод из карточки зоны больше не настраивается: пишите в ПЛК через сценарий. Старый вывод продолжает работать, пока его не убрать.">ПЛК (старый вывод): ${z.out.kind === 'coil' ? 'coil' : 'рег.'} ${z.out.address}</span><button type="button" class="btn-secondary md-x" ${a} data-f="oclear" title="Убрать старый вывод из зоны">убрать</button></div>` : '') + `
        <div class="md-zr__del"><button type="button" class="btn-secondary md-x" ${a} data-f="del" title="Удалить зону">удалить окно</button></div>
      </div>
    </div>
  </div>`;
}

// «Видимость стрелок»: доля времени, когда стрелка найдена, окнами за период; смотреть, помогли ли подсветка, свет или сдвиг камеры
function mdVisHtml(cam) {
  if (!cam.zones.some((z) => z.type === 'cycle' && z.enabled !== false)) return '';
  const k = escapeHtml(cam.id);
  return `<div class="md-zone md-vis">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><b>Видимость стрелок и отражателей</b><span class="md-sp"></span><span class="md-lbl">за</span><select data-md="vis" title="За какой период показать график и среднее">${mdSel(MD_VIS_WINS, MD.visWin)}</select></div>
      <div class="md-zstat" id="md-vl-${k}">загружаю…</div>
      <div class="net-hint" title="Секунда считается «найдена», если стрелку увидели хотя бы в одном кадре секунды; когда стрелки нет или она достроена по скорости — «не найдена». Отражатель конца «виден», если хотя бы в одном кадре секунды сила кончика не меньше 0,3 и угол не достроен по скорости. Красный конец — тот, что по цвету определён как красный. Время без картинки (обрыв связи) в расчёт не идёт. Сплошная линия — стрелка, пунктир — отражатель красного конца, точки — отражатель синего.">доля времени, когда стрелка найдена · сплошная — стрелка, пунктир — красный отражатель, точки — синий</div>
    </div>
    <div class="md-zone__chart"><canvas id="md-vis-${k}" height="110"></canvas></div>
  </div>`;
}

function mdVisTime(ts, win) {
  const d = new Date(ts * 1000), p2 = (n) => String(n).padStart(2, '0');
  return (win > 86400 ? p2(d.getDate()) + '.' + p2(d.getMonth() + 1) + ' ' : '') + p2(d.getHours()) + ':' + p2(d.getMinutes());
}

function mdDrawVis(cam) {
  const cv = mdEl('md-vis-' + cam.id);
  if (!cv) return;
  const d = MD.vis[cam.id];
  const lg = mdEl('md-vl-' + cam.id);
  const ids = d && d.zones ? Object.keys(d.zones) : [];
  if (lg) {
    lg.innerHTML = !d ? 'загружаю…' : d.error ? escapeHtml(d.error)
      : ids.map((id, n) => {
        const nm = escapeHtml((d.names && d.names[id]) || id), col = MD_VIS_COLORS[n % MD_VIS_COLORS.length], pc = (v) => (v == null ? '—' : v.toFixed(1) + ' %');
        const ta = d.tips_avg && d.tips_avg[id];
        return `<div style="color:${col}">● ${nm} — стрелка <b>${pc(d.avg[id])}</b></div>` +
          (ta ? `<div style="color:${col}">┄ ${nm} — отражатель красного конца <b>${pc(ta.r)}</b> · синего <b>${pc(ta.b)}</b></div>` : '');
      }).join('') + (!d.tips && mdCamHasTips(cam) ? '<div class="net-hint">отражатели: обновите сервис движения</div>' : '');
  }
  const w = cv.clientWidth, hh = cv.height || 110;
  if (!w) return;
  if (cv.width !== w) cv.width = w;
  const g = cv.getContext('2d');
  g.clearRect(0, 0, w, hh);
  g.font = '10px sans-serif'; g.fillStyle = '#94a3b8';
  const vals = ids.flatMap((id) => d.zones[id]).filter((v) => v != null);   // пусто у стрелок — рисуем только если есть хоть какие-то данные
  if (!d || !d.t || !vals.length) { g.fillText(d && d.error ? '' : d ? 'нет данных за период' : 'загружаю…', 8, hh / 2); return; }
  const padL = 30, padB = 14, plotW = w - padL - 4, plotH = hh - padB - 6;
  const lo = 0, hi = 100, stepV = 20;                       // шкала всегда 0–100 %
  const X = (i) => padL + (d.t.length > 1 ? (i / (d.t.length - 1)) * plotW : 0);
  const Y = (v) => 3 + plotH - ((v - lo) / (hi - lo)) * plotH;
  g.lineWidth = 1; g.strokeStyle = 'rgba(148,163,184,.22)';
  for (let v = hi; v >= lo; v -= stepV) {
    g.beginPath(); g.moveTo(padL, Y(v)); g.lineTo(w - 4, Y(v)); g.stroke();
    g.fillText(v + '%', 2, Math.max(9, Y(v) + 3));
  }
  for (let j = 0; j <= 4; j += 1) {
    const i = Math.round((j * (d.t.length - 1)) / 4);
    g.textAlign = j === 0 ? 'left' : j === 4 ? 'right' : 'center';
    g.fillText(mdVisTime(d.t[i], d.window_s), j === 0 ? padL : j === 4 ? w - 4 : X(i), hh - 2);
  }
  g.textAlign = 'left';
  ids.forEach((id, n) => {
    g.strokeStyle = MD_VIS_COLORS[n % MD_VIS_COLORS.length]; g.lineWidth = 1.8; g.beginPath();
    let pen = false;
    d.zones[id].forEach((v, i) => {
      if (v == null) { pen = false; return; }
      if (pen) g.lineTo(X(i), Y(v)); else g.moveTo(X(i), Y(v));
      pen = true;
    });
    g.stroke();
    const tp = d.tips && d.tips[id];
    if (!tp) return;
    [['r', [6, 3], 1.6], ['b', [1.5, 3], 1.3]].forEach(([k, dash, lw]) => {      // отражатель красного конца — пунктир, синего — точки
      g.strokeStyle = MD_VIS_COLORS[n % MD_VIS_COLORS.length]; g.lineWidth = lw; g.setLineDash(dash); g.beginPath();
      let on = false;
      tp[k].forEach((v, i) => {
        if (v == null) { on = false; return; }
        if (on) g.lineTo(X(i), Y(v)); else g.moveTo(X(i), Y(v));
        on = true;
      });
      g.stroke();
    });
    g.setLineDash([]);
  });
}

// у камеры есть зона-цикл с отражателями (для подсказки «обновите сервис», если он не прислал tips)
const mdCamHasTips = (cam) => cam.zones.some((z) => z.type === 'cycle' && z.enabled !== false && (z.tip_hsv || []).length && Number(z.tip_w == null ? 1 : z.tip_w) > 0);

// подтянуть с сервиса (не чаще раза в минуту, сразу при смене периода) и нарисовать
async function mdVisRefresh(cams, force) {
  const now = Date.now();
  await Promise.all(cams.map(async (c) => {
    if (!c.zones.some((z) => z.type === 'cycle' && z.enabled !== false)) return;
    const at = MD.visAt[c.id];
    if (!force && at && at.win === MD.visWin && now - at.t < 60000) return;
    MD.visAt[c.id] = { t: now, win: MD.visWin };
    const r = await mvApi('/api/motion/visibility?camera=' + encodeURIComponent(c.id) + '&window=' + MD.visWin);
    MD.vis[c.id] = r && !r.error ? r : { error: (r && r.error) || 'нет ответа сервиса' };
    mdDrawVis(c);
  }));
}

function mdOnVis(t) {
  MD.visWin = Number(t.value);
  try { localStorage.setItem(MD_VIS_KEY, String(MD.visWin)); } catch (err) { /* не критично */ }
  mdVisRefresh(mdShownCams().filter((c) => c.zones.length), true);
}

const mdLastDelta = {};   // группа → последнее надёжное Δ (палки видны): в удержании показываем его, а не достроенное

// карточка синхронности валов: слева Δ, автокоррекция (тумблер), допуск и эталон, вывод в ПЛК; справа два графика Δ — без коррекции и с ней
function mdMirrorCardHtml(cam, g) {
  const a = `data-md="mg" data-cam="${escapeHtml(cam.id)}" data-gid="${escapeHtml(g.id)}"`;
  const k = escapeHtml(cam.id + '-' + g.id);
  const names = g.zones.map((id) => { const z = cam.zones.find((q) => q.id === id); return escapeHtml(z ? z.name : id); }).join(' ↔ ');
  return `<div class="md-zone md-mirror">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><b>${escapeHtml(g.name)}</b><span class="net-hint">${names}</span><span class="md-sp"></span>
        <span class="cv-bit" id="md-mxb-${k}" title="Авария: рассинхрон больше допуска">–</span><span class="md-num" id="md-mxv-${k}" title="Отклонение от нормы, °">–</span></div>
      <div class="md-zstat" id="md-mxs-${k}"></div>
      <div class="md-zstat md-mxd" id="md-mxd-${k}"></div>
      <div class="md-zone__row md-mx-set">
        <label class="switch-toggle md-sw md-sw--sm" title="Волна Δ за оборот — перекос камеры, он повторяется. Сервис сам выучивает её по скользящему окну и вычитает: Δ держится около нуля, а свет и положение камеры подстраиваются. Учится только форма волны: настоящий сдвиг валов в неё не уходит и виден в Δ сразу."><span class="switch"><input type="checkbox" ${a} data-f="learn"${g.learn === false ? '' : ' checked'} /><span class="switch-track"></span></span><span class="md-lbl md-lbl--on">автокоррекция</span></label>
        <span class="md-lbl" title="За какое время учится форма: скользящее окно. Больше — спокойнее, меньше — быстрее следует за светом и камерой">окно</span>${mdNum(a + ' data-f="learnmin"', g.learn_min || 30, 'min="5" max="240" step="5"')}<span class="md-lbl">мин</span>
        <span class="net-hint" id="md-mxl-${k}"></span></div>
      <div class="md-zone__row md-mx-set"><span class="md-lbl" title="Авария, когда отклонение по модулю не меньше допуска">допуск, °</span>${mdNum(a + ' data-f="tol"', g.tol, 'min="0.5" max="180" step="0.5"')}
        <span class="md-lbl" title="Какое значение суммы углов считать нормой (уровень Δ). Автокоррекция убирает волну, а уровень остаётся за вами">эталон, °</span>${mdNum(a + ' data-f="ref"', g.ref, 'min="-180" max="180" step="0.5"')}
        <span class="md-lbl" title="Регистр — Δ в 0,1° со знаком; coil — авария">вывод в ПЛК</span>${mdOutCell(g.out ? { kind: g.out.kind === 'coils' ? 'coil' : 'holding', address: g.out.address } : null, a)}</div>` +
    (g.table ? `<div class="md-zone__row"><span class="md-lbl">ручная таблица</span><span class="net-hint">перекос от ${Math.min(...g.table).toFixed(1)} до ${Math.max(...g.table).toFixed(1)}°</span><button type="button" class="btn-secondary md-x" ${a} data-f="tclear" title="Убрать ручную таблицу поправки: дальше работает только автокоррекция">сбросить</button></div>` : '') + `
    </div>
    <div class="md-zone__chart md-mx2">
      <div class="md-mxc"><span class="md-mxc__t">без коррекции</span><canvas id="md-mx0-${k}" height="64"></canvas></div>
      <div class="md-mxc"><span class="md-mxc__t">с коррекцией</span><canvas id="md-mx-${k}" height="64"></canvas></div>
    </div>
  </div>`;
}

// параметры камеры: поля в 2–3 колонки
function mdCameraParamsHtml(cam) {
  const so = cam.status_out;
  const a = `data-md="c" data-cam="${escapeHtml(cam.id)}"`;
  return `<details class="md-det" open><summary>Параметры камеры ${mdHelp('При смене настроек камеры сервис переподключается к потоку (несколько секунд без данных).')}</summary>
    <div class="md-fields">
      ${mdField('Название', `<input type="text" ${a} data-f="name" value="${escapeHtml(cam.name)}" />`)}
      ${mdField('Адрес потока', `<input type="text" ${a} data-f="url" value="${escapeHtml(cam.url)}" />`, '', 'md-wide')}
      ${mdField('Поток', `<select ${a} data-f="substream">${mdSel([['0', 'основной'], ['1', 'дополнительный (легче)']], cam.substream ? '1' : '0')}</select>`, 'Дополнительный поток камеры Dahua (subtype=1): картинка меньше, процессор нагружается заметно слабее. Пороги после смены снимите заново — значения diff и flow зависят от разрешения')}
      ${mdField('Декодирование', `<select ${a} data-f="hw_accel">${mdSel([['off', 'процессор'], ['auto', 'видеоядро (авто)'], ['d3d11', 'видеоядро (Direct3D 11)']], cam.hw_accel || 'auto')}</select>`, 'Видеоядро процессора (Intel UHD и т.п.) декодирует видео вместо процессора. Не открылось или не дало кадров — сервис сам вернётся на процессор')}
      ${mdField('Ряд валов', `<select ${a} data-f="row">${mdSel([['', 'не задан'], ['lower', 'нижний (Н)'], ['upper', 'верхний (В)']], cam.row || '')}</select>`, 'Какой ряд валов видит камера: нижний (Н) или верхний (В). Нужен для разностей между валами разных камер — вкладка «Валы»')}
      <div class="md-sub">Анализ движения</div>
      ${mdField('Кадров/с в анализ', mdNum(a + ' data-f="proc_fps"', cam.proc_fps, 'min="0.2" max="30" step="0.5"'))}
      ${mdField('Шаг сравнения, кадров', mdNum(a + ' data-f="frame_gap"', cam.frame_gap, 'min="1" max="10"'), 'Сравнивать кадр не с соседним, а через N кадров: медленное движение виднее')}
      <div class="md-sub">Защита от сбоев</div>
      ${mdField('Зависла через, с', mdNum(a + ' data-f="freeze_timeout_s"', cam.freeze_timeout_s, 'min="1"'), 'Картинка не меняется дольше — камера зависла: в ПЛК нули и статус 0')}
      ${mdField('Серая каша: доля кадра', mdNum(a + ' data-f="corrupt_ratio"', cam.corrupt_ratio == null ? 0.3 : cam.corrupt_ratio, 'min="0" max="1" step="0.05"'), 'Доля кадра, залитая ровным серым (так выглядит битый H.264). 0 — не проверять')}
      ${mdField('Пауза после каши, с', mdNum(a + ' data-f="corrupt_hold_s"', cam.corrupt_hold_s == null ? 2 : cam.corrupt_hold_s, 'min="0" max="60" step="0.5"'), 'Сколько секунд после каши результаты ещё не учитываются')}
      ${mdField('Статус «камера жива»', `<span class="md-out"><select ${a} data-f="skind">${mdSel([['', 'нет'], ['holding', 'регистр'], ['coil', 'coil']], so ? so.kind : '')}</select>` +
        (so && so.kind ? `<input type="number" ${a} data-f="saddr" min="0" max="65535" value="${so.address}" title="Адрес" placeholder="адрес" />` : '') + '</span>')}
    </div></details>`;
}

// общий вид: все RTSP-камеры с тумблером КЗ
function mdCamerasListHtml() {
  const rtsp = state.cameras.filter((c) => c.kind === 'rtsp');
  if (!rtsp.length) return '';
  return `<details class="md-det" open><summary>Камеры</summary><div class="md-camlist">${rtsp.map((c) =>
    `<label class="md-camrow"><span class="switch"><input type="checkbox" data-md="cs" data-serial="${escapeHtml(c.serial)}"${mvEnabled(c.serial) ? ' checked' : ''}${MV.online ? '' : ' disabled'} /><span class="switch-track"></span></span>` +
    `<span>${escapeHtml(c.label)}${mdIpTag(c.connection && c.connection.url)}</span><span class="motion-dock__pill" id="md-cp-${escapeHtml(c.serial)}"></span></label>`).join('')}</div></details>`;
}

// ---------- вкладка «Камеры»: всё, что есть в сервисе, и используется ли оно ----------
// Камера «используется», если её зона стоит в сценарии, пишет в ПЛК сама (вывод зоны или статуса, группа с выводом).
function mdCamUsage(cam) {
  const scen = ((MV.cfg && MV.cfg.scenarios) || []).filter((s) => s.sources.some((x) => x.camera === cam.id))
    .map((s) => s.name + (s.out ? ' (' + (s.out.kind === 'coil' ? 'coil ' : 'рег. ') + s.out.address + ')' : ''));
  const plc = [];
  cam.zones.forEach((z) => { if (z.out) plc.push(z.name + ' → ' + (z.out.kind === 'coil' ? 'coil ' : 'рег. ') + z.out.address); });
  (cam.groups || []).forEach((g) => { if (g.out) plc.push('группа ' + g.name + ' → ' + (g.out.kind === 'coils' ? 'coil ' : 'рег. ') + g.out.address); });
  if (cam.status_out) plc.push('статус → ' + (cam.status_out.kind === 'coil' ? 'coil ' : 'рег. ') + cam.status_out.address);
  return { scen, plc, used: scen.length > 0 || plc.length > 0 };
}

function mdCamsTabHtml() {
  const cams = mdCfgCams();
  if (!cams.length) return '<p class="net-hint">В сервисе движения камер нет.</p>';
  const byIp = {};
  cams.forEach((c) => { const ip = mdIp(c.url); if (ip) (byIp[ip] = byIp[ip] || []).push(c); });
  const info = cams.map((c) => ({ c, u: mdCamUsage(c) }));
  const idle = info.filter((x) => !x.u.used).length;
  const rows = info.map(({ c, u }) => {
    const ip = mdIp(c.url);
    const twins = (byIp[ip] || []).filter((x) => x.id !== c.id);
    const flows = c.zones.filter((z) => z.type !== 'level' && z.type !== 'clarity' && (z.metric === 'flow' || z.track_flow !== false)).length;
    const inUi = state.cameras.some((q) => q.serial === c.id);
    const use = u.used
      ? [u.scen.length ? 'сценарии: ' + u.scen.map(escapeHtml).join(', ') : '', u.plc.length ? 'в ПЛК: ' + u.plc.map(escapeHtml).join(', ') : ''].filter(Boolean).join('<br>')
      : '<span class="md-cam__idle">не используется</span>';
    const btn = u.used
      ? `<button type="button" class="btn-secondary md-x" disabled title="Камера стоит в сценарии или пишет в ПЛК. Сначала уберите её оттуда.">Убрать</button>`
      : `<button type="button" class="btn-secondary md-x" data-md="cm" data-cam="${escapeHtml(c.id)}" data-f="rm" title="Убрать камеру и её зоны из сервиса движения">Убрать</button>`;
    return `<div class="md-cam${u.used ? '' : ' is-idle'}" data-peek-cam="${escapeHtml(c.id)}">
      <div class="md-cam__who"><b>${escapeHtml(c.name)}</b>${mdIpTag(c.url)}<span class="md-dim">${escapeHtml(c.id)}${inUi ? '' : ' · нет в списке камер страницы'}</span>
        <span class="motion-dock__pill" id="md-cr-${escapeHtml(c.id)}"></span></div>
      <div class="md-cam__load">${c.substream ? 'подпоток' : 'основной поток'} · ${c.proc_fps} к/с · зон: ${c.zones.length}${flows ? ' · flow считается: ' + flows : ''}${c.enabled ? '' : ' · выключена'}${twins.length ? '<br>та же камера, что: ' + twins.map((x) => escapeHtml(x.id)).join(', ') : ''}</div>
      <div class="md-cam__use">${use}</div>
      <div class="md-cam__act">${btn}</div>
      <div class="md-perf" id="md-perf-${escapeHtml(c.id)}">${mdPerfHtml(c)}</div></div>`;
  }).join('');
  return `<p class="net-hint">Камер в сервисе: ${cams.length}, используется: ${cams.length - idle}${idle ? `, <b>не используется: ${idle}</b> (считают впустую и грузят процессор)` : ''}. Наведите на строку — покажу кадр с зонами.</p><div class="md-cams">${rows}</div>`;
}

function mdOnCamRow(t) {
  const cam = mvCam(t.dataset.cam);
  if (!cam || t.dataset.f !== 'rm') return;
  if (mdCamUsage(cam).used) return;
  if (!confirm('Убрать камеру «' + cam.name + '» (' + cam.id + ') вместе с её зонами (' + cam.zones.length + ')? Она нигде не используется.')) return;
  mdPeekHide();
  mdEdit(() => { MV.cfg.cameras = MV.cfg.cameras.filter((c) => c.id !== cam.id); }, true);
}

// есть что показать на вкладке «Зоны · тренды»: обычные зоны (стёкла уровня — не в счёт) или карточка уровня
// (круги прозрачности сока — отдельная вкладка «Сок»)
const mdHasZoneCards = (c) => c.zones.some((z) => z.type !== 'level' && z.type !== 'clarity') || !!lvGroup(c);

function mdZonesTabHtml(mode, cam) {
  const all = mode === 'all';
  if (!all && !(cam && cam.enabled)) {
    return '<p class="net-hint">Включите КЗ переключателем справа сверху — для этой камеры появятся зоны, тренды и вывод в ПЛК.</p>';
  }
  const shown = mdShownCams();
  let html = '';
  if (!shown.length) {
    html += '<p class="net-hint">КЗ ещё ни у одной камеры не включено. Включите его ниже или выберите ячейку с RTSP-камерой и нажмите тумблер справа сверху.</p>';
  } else if (shown.every((c) => !mdHasZoneCards(c))) {
    html += '<p class="net-hint">Зон нет. Нажмите «Разметить зону» и обведите на кадре то, что должно двигаться (лента, шнек).</p>';
  } else {
    html += `<div class="md-trendbar"><label class="md-lbl md-openall" title="Раскрыть настройки всех окон"><input type="checkbox" id="mdOpenAll"${MD.openAll ? ' checked' : ''} /> подробно</label><span>Тренды за последние <b id="mdWinLbl">${mdFmtWin(MD.win)}</b> ${mdHelp(MD_TREND_HELP)}</span>` +
      `<span class="md-statwin">Среднее / мин / макс за <select id="mdStatWin">${mdSel(MD_STAT_WINS, MD.statWin)}</select> ${mdHelp('Среднее, минимум и максимум diff и flow каждой зоны за выбранный период, только по кадрам в норме. Копятся в сервисе движения и лежат на диске (data\\trends): после обновления или перезапуска сервиса не обнуляются; хранятся 10 суток. Подробно — CSV-запись и архив. Значения сглажены, как на графиках.')}</span></div><div class="md-zones">`;
    shown.forEach((c) => {
      if (!mdHasZoneCards(c)) return;
      if (all) html += `<div class="md-camhead"><b>${escapeHtml(c.name)}${mdIpTag(c.url)}</b><span class="motion-dock__pill" id="md-cp-${escapeHtml(c.id)}"></span></div>`;
      html += c.zones.map((z, i) => (z.type === 'level' || z.type === 'clarity' ? '' : mdZoneCardHtml(c, z, i))).join('');      // стёкла уровня — на вкладке «Уровень»
      html += mdVisHtml(c);
      html += mdLevelCardHtml(c);
      html += (c.groups || []).filter((g) => g.type === 'mirror').map((g) => mdMirrorCardHtml(c, g)).join('');
    });
    html += '</div>';
  }
  html += all ? mdCamerasListHtml() : mdCameraParamsHtml(cam);
  return html;
}

function mdOnZone(t, cam) {
  const i = Number(t.dataset.i), f = t.dataset.f, z = cam.zones[i];
  if (!z) return;
  if (mdOnLearn(t, cam, z)) return;
  if (f === 'hsvreset') {
    mdEdit(() => { z.red_hsv = SH_RED_DEF.slice(); z.blue_hsv = SH_BLUE_DEF.slice(); z.red_more = []; z.blue_more = []; z.red_not = []; z.blue_not = []; }, true);
    return;
  }
  if (f === 'tipwhite') {                                         // светоотражатели: добавить «пересвет» (белое под прожектором) одной кнопкой
    mdEdit(() => { const arr = z.tip_hsv || []; if (arr.length < 6 && !arr.some((r) => r.join() === SH_TIP_WHITE.join())) z.tip_hsv = arr.concat([SH_TIP_WHITE.slice()]); }, true);
    return;
  }
  if (f === 'hsvndel') {                                       // убрать исключённый цвет («не ловить») конца палки или цвет светоотражателя (w = 'tip')
    mdEdit(() => { (z[t.dataset.w === 'tip' ? 'tip_hsv' : t.dataset.w + '_not'] || []).splice(Number(t.dataset.n), 1); }, true);
    return;
  }
  if (f === 'hsvn') {                                             // порог «ящика» [H1, H2, S1, S2, V1, V2]: исключённый цвет конца (red/blue_not) или светоотражатель (tip_hsv); оттенок «от» выше «до» — через ноль круга
    const w = t.dataset.w, n = Number(t.dataset.n), k = Number(t.dataset.k), key = w === 'tip' ? 'tip_hsv' : w + '_not';
    let fixed = false;
    mdEdit(() => {
      const arr = (z[key] || []).map((r) => r.slice());
      const r = arr[n];
      if (!r) return;
      r[k] = Math.max(0, Math.min(k < 2 ? 179 : 255, parseInt(t.value, 10) || 0));
      if (k >= 2) {                                               // «от» насыщенности/яркости не выше «до»: пары (2,3) и (4,5)
        const lo = k & ~1, hi = lo + 1;
        if (r[lo] > r[hi]) { if (k === lo) r[hi] = r[lo]; else r[lo] = r[hi]; fixed = true; }
      }
      z[key] = arr;
    }, false);
    if (fixed) { MD.key = ''; renderMotionDock(); }
    return;
  }
  if (f === 'hsvxdel') {                                          // убрать дополнительный цвет конца палки
    mdEdit(() => { (z[t.dataset.w + '_more'] || []).splice(Number(t.dataset.n), 1); }, true);
    return;
  }
  if (f === 'hsvx') {                                             // порог дополнительного цвета (строка «ещё цвет»)
    const w = t.dataset.w, n = Number(t.dataset.n), k = Number(t.dataset.k);
    let fixed = false;
    mdEdit(() => {
      const arr = (z[w + '_more'] || []).map((r) => r.slice());
      const r = arr[n];
      if (!r) return;
      r[k] = Math.max(0, Math.min(k < 2 ? 179 : 255, parseInt(t.value, 10) || 0));
      if (r[0] > r[1]) { if (k === 0) r[1] = r[0]; else r[0] = r[1]; fixed = true; }   // нижняя граница оттенка не выше верхней
      z[w + '_more'] = arr;
    }, false);
    if (fixed) { MD.key = ''; renderMotionDock(); }
    return;
  }
  if (f === 'hsv') {                                              // пороги цвета палки (вкладка «Валы»): H/S/V красной и синей половин
    const w = t.dataset.w, k = Number(t.dataset.k);
    let fixed = false;
    mdEdit(() => {
      const arr = shHsvOf(z, w).slice();
      arr[k] = Math.max(0, Math.min(k < 2 ? 179 : 255, parseInt(t.value, 10) || 0));
      if (w === 'blue' && arr[0] > arr[1]) { if (k === 0) arr[1] = arr[0]; else arr[0] = arr[1]; fixed = true; }   // у синего нижняя граница не выше верхней
      z[w + '_hsv'] = arr;
    }, false);
    if (fixed) { MD.key = ''; renderMotionDock(); }
    return;
  }
  if (f === 'del') {
    if (!confirm('Удалить зону «' + z.name + '»?')) return;
    mdEdit(() => {
      cam.zones.splice(i, 1);
      (cam.groups || []).forEach((g) => { g.zones = g.zones.filter((x) => x !== z.id); if (g.order) g.order = g.order.filter((x) => x !== z.id); });
      cam.groups = (cam.groups || []).filter((g) => g.zones.length && (g.type !== 'mirror' || g.zones.length === 2));
      (MV.cfg.scenarios || []).forEach((s) => { s.sources = s.sources.filter((x) => !(x.camera === cam.id && x.zone === z.id)); });
    }, true);
    return;
  }
  mdEdit(() => {
    if (f === 'radius') z.radius = Math.max(20, parseFloat(t.value) || 20);
    else if (f === 'cx' || f === 'cy') z.center[f === 'cx' ? 0 : 1] = Math.max(0, parseFloat(t.value) || 0);
    else if (f === 'name') z.name = t.value;
    else if (f === 'type') { z.type = t.value; z.invert = t.value === 'presence'; }
    else if (f === 'metric') { z.metric = t.value; z.on = MD_THR[z.metric][0]; z.off = MD_THR[z.metric][1]; }
    else if (f === 'on' || f === 'off') z[f] = parseFloat(t.value) || 0;
    else if (f === 'fon' || f === 'foff') z[f === 'fon' ? 'frames_to_on' : 'frames_to_off'] = Math.max(1, Math.min(1000, parseInt(t.value, 10) || 1));
    else if (f === 'trackflow') z.track_flow = t.checked;
    else if (f === 'okind') z.out = t.value ? { kind: t.value, address: (z.out && z.out.address) || 0, value: (z.out && z.out.value) || 'bit' } : null;
    else if (f === 'oaddr' && z.out) z.out.address = parseInt(t.value, 10) || 0;
    else if (f === 'rkind') z.rpm_out = t.value ? { kind: 'holding', address: (z.rpm_out && z.rpm_out.address) || 0 } : null;
    else if (f === 'raddr' && z.rpm_out) z.rpm_out.address = parseInt(t.value, 10) || 0;
    else if (f === 'oval' && z.out) z.out.value = t.value;
    else if (f === 'fmetric') { z.fill_metric = t.value; z.fill_invert = t.value === 'tex'; }   // яркость: жом светлее; зернистость: жом гладче
    else if (f === 'finvert') z.fill_invert = t.checked;
    else if (f === 'fllon' || f === 'flloff') z[f === 'fllon' ? 'fill_on' : 'fill_off'] = Math.max(0, parseFloat(t.value) || 0);
    else if (f === 'oclear') z.out = null;
  }, f === 'type' || f === 'metric' || f === 'okind' || f === 'rkind' || f === 'fmetric' || f === 'finvert' || f === 'oclear');
}

// «Проверить связь с сервером»: пульс на сервер мониторинга прямо сейчас (тот же, что шлёт приложение раз в несколько минут) и ответ сервера
async function mdOnPulse() {
  const b = mdEl('mdPulse'), m = mdEl('mdPulseMsg');
  if (!b || !m) return;
  b.disabled = true;
  m.className = 'net-hint';
  m.textContent = 'отправляю пульс…';
  let r;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 40000);
  try { r = await (await fetch('/api/sysmon/send_test', { cache: 'no-store', signal: ctl.signal })).json(); } catch (e) { r = { ok: false, error: 'приложение не ответило' }; }
  clearTimeout(timer);
  b.disabled = false;
  const ok = !!(r && r.ok);
  m.className = 'net-hint ' + (ok ? 'md-pulse--ok' : 'md-pulse--bad');
  m.textContent = ok ? 'сервер принял пульс' : ((r && r.error) || 'нет связи с сервером');
  clearTimeout(mdOnPulse.t);
  mdOnPulse.t = setTimeout(() => { m.textContent = ''; }, 30000);
}

// карточка синхронности: допуск, эталон, автокоррекция, вывод
async function mdOnMirror(t, cam) {
  const g = (cam.groups || []).find((q) => q.id === t.dataset.gid);
  if (!g) return;
  const f = t.dataset.f;
  if (f === 'tclear') { mdEdit(() => { g.table = null; }, true); return; }
  mdEdit(() => {
    if (f === 'tol') g.tol = Math.max(0.5, Math.min(180, parseFloat(t.value) || 10));
    else if (f === 'ref') g.ref = Math.max(-180, Math.min(180, parseFloat(t.value) || 0));
    else if (f === 'learn') g.learn = t.checked;
    else if (f === 'learnmin') g.learn_min = Math.max(5, Math.min(240, parseInt(t.value, 10) || 30));
    else if (f === 'okind') g.out = t.value ? { kind: t.value === 'coil' ? 'coils' : 'holding', address: (g.out && g.out.address) || 0 } : null;
    else if (f === 'oaddr' && g.out) g.out.address = parseInt(t.value, 10) || 0;
  }, f === 'okind');
}

function mdOnCamera(t, cam) {
  const f = t.dataset.f;
  mdEdit(() => {
    if (f === 'name' || f === 'url') cam[f] = t.value.trim();
    else if (f === 'skind') cam.status_out = t.value ? { kind: t.value, address: (cam.status_out && cam.status_out.address) || 0 } : null;
    else if (f === 'saddr') { if (cam.status_out) cam.status_out.address = parseInt(t.value, 10) || 0; }
    else if (f === 'frame_gap') cam[f] = parseInt(t.value, 10) || 1;
    else if (f === 'hw_accel') cam[f] = t.value;
    else if (f === 'substream') cam[f] = t.value === '1';
    else if (f === 'row') cam.row = t.value;
    else cam[f] = parseFloat(t.value) || 0;
  }, f === 'skind' || f === 'row');
}

// ---------- тренды ----------
async function mdDrawTrends() {
  if (MD.tab !== 'zones' || !MV.dockOpen) return;
  if (MD.trendBusy) { MD.trendDirty = true; return; }
  const cams = mdShownCams().filter((c) => c.zones.length);
  if (!cams.length) return;
  const first = document.querySelector('.md-zone__chart canvas');
  const pts = Math.max(60, Math.min(1200, first ? first.clientWidth : 500));
  MD.trendBusy = true;
  const res = await Promise.all(cams.map((c) => mvApi('/api/motion/history?camera=' + encodeURIComponent(c.id) + '&window=' + MD.win + '&points=' + pts)));
  MD.trendBusy = false;
  res.forEach((h, k) => {
    if (!h || h.error || !h.zones) return;
    cams[k].zones.forEach((z) => { mdDrawChart(cams[k], z, h.zones[z.id], h.t || [], h.ok || []); mdDrawFillChart(cams[k], z, h.fill); });
    mdDrawMirrorCharts(cams[k], h);
    mdDrawLevelChart(cams[k], h);
  });
  cams.forEach((c) => mdDrawVis(c));            // из кэша: карточка могла перерисоваться
  mdVisRefresh(cams, false);
  if (MD.trendDirty) { MD.trendDirty = false; mdDrawTrends(); }
}

// ---------- значения зон: сейчас / среднее / мин / макс ----------
const mdNumFmt = (v) => (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2));

// значения вала: красный и синий конец палки (0–360° от вертикали по часовой, синий напротив красного) и значок направления вращения
function mdRodAnglesHtml(z) {
  const rpm = Math.abs(Number(z.rpm) || 0);
  const speed = z.moving === false ? 'стоит' : rpm > 0.02 ? `${rpm.toFixed(2)} об/мин` : '–';
  return `<span style="color:${MV_ROD_RED}">красный <b>${Number(z.angle).toFixed(1)}°</b></span> · <span style="color:${MV_ROD_BLUE}">синий <b>${mvBlueAngle(z.angle).toFixed(1)}°</b></span> · <b title="Скорость вращения вала">${speed}</b> <span title="Направление вращения: ↻ по часовой, ↺ против, ■ стоит">${mvDirIcon(z.rpm)}</span>`;
}

// чем держится угол: показываем только когда видна одна половина палки (обе видны — это норма)
function mdRodSrcHtml(z) {
  if (z.src === 'tip') return ` · <span style="color:${MV_ROD_TIP}" title="Цветные половины палки сейчас не видны, угол держится по светоотражателям на концах">по светоотражателям</span>`;
  const t = z.src === 'red' ? 'только красная половина' : z.src === 'blue' ? 'только синяя половина' : '';
  const col = z.src === 'red' ? MV_ROD_RED : MV_ROD_BLUE;
  return t ? ` · <span style="color:${col}" title="Вторая половина палки сейчас не видна, угол считается по этой">${t}</span>` : '';
}

function mdZoneStatHtml(z, cur, st) {
  if (z.type === 'cycle') {
    return cur && cur.valid ? `${mdRodAnglesHtml(cur)} · ${cur.coast ? '<span title="Палки не видно (переход по тёмному, блик): угол держится по скорости вращения до coast_s секунд">не видно, держу по скорости</span>' : 'уверенность ' + Number(cur.conf).toFixed(2) + mdRodSrcHtml(cur)}` : 'палка не найдена';
  }
  const row = (m) => {
    const c = cur ? mdNumFmt(Number(cur[m])) : '–';
    const s = st && st[m];
    const txt = `${m}: сейчас <b class="md-zstat__cur">${c}</b>` + (s ? ` · ср ${mdNumFmt(s.avg)} · мин ${mdNumFmt(s.min)} · макс ${mdNumFmt(s.max)}` : ' · ср – · мин – · макс –');
    return `<div class="md-zstat__row${z.metric === m ? ' is-main' : ''}">${txt}</div>`;
  };
  return row('diff') + row('flow');
}

// Δ с коррекцией за период: среднее / мин / макс (и сама поправка), считает сервис по каждому кадру с видимыми палками
const mdSgn = (v) => (v > 0 ? '+' : '') + Number(v).toFixed(1);
function mdMirrorStatHtml(s, sr) {
  const d = s.delta, cr = s.corr;
  const note = sr && s.span_s < sr.window_s * 0.9 ? ` <span class="md-zstat__note">накоплено за ${mdFmtWin(s.span_s)}</span>` : '';
  return `<div class="md-zstat__row is-main" title="Отклонение Δ уже с поправкой автокоррекции, по каждому кадру, где видны обе палки (без удержания)">Δ с коррекцией за ${mdFmtWin(sr.window_s)}: ср <b class="md-zstat__cur">${mdSgn(d.avg)}°</b> · мин ${mdSgn(d.min)} · макс ${mdSgn(d.max)}${note}</div>` +
    `<div class="md-zstat__row" title="Сколько вычитается из сырой суммы углов: ручная таблица + выученная форма">поправка: ср ${mdSgn(cr.avg)} · от ${mdSgn(cr.min)} до ${mdSgn(cr.max)}°</div>`;
}

function mdPaintZoneStats() {
  mdLoadStats();
  mdShownCams().forEach((c) => {
    const res = (MV.states[c.id] || {}).result;
    const sr = MD.stats[c.id];
    (c.groups || []).filter((g) => g.type === 'mirror').forEach((g) => {
      const el = mdEl('md-mxd-' + c.id + '-' + g.id);
      const gs = sr && sr.groups && sr.groups[g.id];
      if (el) el.innerHTML = gs ? mdMirrorStatHtml(gs, sr) : '';
    });
    const live = {};
    ((res && res.zones) || []).forEach((z) => { live[z.id] = z; });
    mdLevelCardLive(c, sr);
    c.zones.forEach((z) => {
      const fe = mdEl('md-fst-' + c.id + '-' + z.id);
      if (fe && z.fill_metric && z.fill_metric !== 'off') {
        const zs = sr && sr.zones && sr.zones[z.id];
        fe.innerHTML = mdFillStatHtml(z, live[z.id], zs && zs.fill);
      }
      const el = mdEl('md-st-' + c.id + '-' + z.id);
      if (!el) return;
      el.innerHTML = mdZoneStatHtml(z, live[z.id], sr && sr.zones && sr.zones[z.id]);
      // накоплено меньше, чем просили (сервис недавно стартовал): честно говорим, за сколько на самом деле
      if (z.type !== 'cycle' && sr && sr.zones && sr.zones[z.id] && sr.span_s < sr.window_s * 0.9) {
        const n = document.createElement('div');
        n.className = 'md-zstat__note';
        n.textContent = 'накоплено за ' + mdFmtWin(sr.span_s);
        el.appendChild(n);
      }
    });
  });
}

// раз в несколько секунд (и сразу после смены периода) спрашиваем сервис движения; по одному запросу на камеру
async function mdLoadStats(force) {
  if (MD.statBusy || MD.tab !== 'zones' || !MV.dockOpen) return;
  if (!force && Date.now() - MD.statAt < MD_STAT_EVERY_MS) return;
  const cams = mdShownCams().filter((c) => c.zones.length);
  if (!cams.length) return;
  MD.statBusy = true;
  MD.statAt = Date.now();
  const win = MD.statWin;
  try {
    const res = await Promise.all(cams.map((c) => mvApi('/api/motion/stats?camera=' + encodeURIComponent(c.id) + '&window=' + win)));
    res.forEach((r, i) => { if (r && !r.error && r.zones && win === MD.statWin) MD.stats[cams[i].id] = r; });
  } finally {
    MD.statBusy = false;
  }
  mdPaintZoneStats();
}

// график зоны: ось X — время, правый край — «сейчас», левый — «сейчас − окно»
// временной ряд значений vals (null — нет данных) в диапазоне [lo, hi]; wrap — угол 0..360: на переходе через ноль линию не тянем;
// lines — горизонтальные пунктиры [{v, color}]; bad[i] — серый фон «кадров нет»
function mdDrawSeries(cv, t, vals, ok, lo, hi, opts) {
  const w = cv.clientWidth, hh = (opts && opts.h) || 84;
  if (!w) return;
  if (cv.width !== w) cv.width = w;
  const g = cv.getContext('2d');
  g.clearRect(0, 0, w, hh);
  const strip = opts && opts.strip ? 7 : 0;          // полоска состояния под графиком (цвет на каждую точку)
  const plotH = hh - 13 - strip, win = (opts && opts.win) || MD.win, n = vals.length;
  const Y = (v) => plotH - ((v - lo) / (hi - lo)) * (plotH - 6) - 1;
  g.fillStyle = '#94a3b8'; g.font = '10px sans-serif';
  g.fillText(String(hi), 3, 10); g.fillText(String(lo), 3, plotH - 2);
  g.fillText('−' + mdFmtWin(win), 3, hh - 1);
  g.textAlign = 'right'; g.fillText('сейчас', w - 3, hh - 1); g.textAlign = 'left';
  cv._hv = null;
  if (n < 2) return;
  const tEnd = t[n - 1];
  const X = (i) => w - 1 - ((tEnd - t[i]) / win) * (w - 2);
  const dt = (t[n - 1] - t[0]) / (n - 1);
  const bw = Math.max(1.5, (dt / win) * w + 1);
  const gap = Math.max(dt * 3, 5);
  if (opts.tip) cv._hv = { t, vals, ok, win, w, tEnd, dt, tip: opts.tip };      // для подсказки при наведении (mdOnChartHover)
  for (let i = 0; i < n; i += 1) if (ok[i] === 0) { g.fillStyle = 'rgba(156,163,175,.30)'; g.fillRect(X(i) - bw, 0, bw, plotH); }
  if (strip) for (let i = 0; i < n; i += 1) { g.fillStyle = ok[i] === 0 || !opts.strip[i] ? '#9ca3af' : opts.strip[i]; g.fillRect(X(i) - bw, plotH + 3, bw, strip); }
  if (opts.band) { g.fillStyle = opts.band.color; g.fillRect(0, Y(opts.band.hi), w, Y(opts.band.lo) - Y(opts.band.hi)); }     // подсвеченная полоса между двумя значениями (зона гистерезиса)
  g.setLineDash([4, 3]); g.lineWidth = 1;
  (opts.lines || []).forEach((l) => { g.strokeStyle = l.color; g.beginPath(); g.moveTo(0, Y(l.v)); g.lineTo(w, Y(l.v)); g.stroke(); });
  g.setLineDash([]);
  g.strokeStyle = '#38bdf8'; g.lineWidth = 1.6; g.beginPath();
  let pen = false;
  vals.forEach((v, i) => {
    if (v == null || ok[i] === 0) { pen = false; return; }
    const brk = !pen || t[i] - t[i - 1] > gap || (opts.wrap && Math.abs(v - vals[i - 1]) > 180);
    if (brk) g.moveTo(X(i), Y(v)); else g.lineTo(X(i), Y(v));
    pen = true;
  });
  g.stroke();
}

// график признака вида окна (яркость к металлу / яркость / зернистость): линия, пороги, полоска «жом есть / пусто»; fh — h.fill из /api/motion/history
function mdDrawFillChart(cam, z, fh) {
  const cv = mdEl('md-fch-' + cam.id + '-' + z.id);
  const m = z.fill_metric;
  if (!cv || !m || m === 'off') return;
  const d = fh && fh.zones && fh.zones[z.id];
  const vals = d ? d[m] : [];
  const decide = (z.fill_on || 0) > 0;
  const seen = vals.filter((v) => v != null).concat(decide ? [z.fill_on, z.fill_off] : []);
  let lo = seen.length ? Math.min(...seen) : 0, hi = seen.length ? Math.max(...seen) : 1;
  const pad = Math.max((hi - lo) * 0.1, m === 'lum' ? 2 : 0.02);
  lo -= pad; hi += pad;
  if (m === 'lum') { lo = Math.max(0, Math.floor(lo)); hi = Math.min(255, Math.ceil(hi)); } else { lo = Math.max(0, Math.round(lo * 100) / 100); hi = Math.round(hi * 100) / 100; }
  const fill = d ? d.fill : [];
  const lines = decide ? [{ v: z.fill_on, color: '#ef4444' }, { v: z.fill_off, color: '#f59e0b' }] : [];
  const tip = (i) => {
    const f = fill[i];
    return MD_FILL_NAME[m] + ' <b>' + mdFillFmt(m, vals[i]) + '</b>' + (decide && f != null ? ' · ' + (f ? 'жом есть' : 'пусто') : '');
  };
  mdDrawSeries(cv, fh ? fh.t || [] : [], vals, fh ? fh.ok || [] : [], lo, hi, { h: cv.height || 72, lines, tip, strip: decide ? fill.map((f) => (f == null ? null : f ? '#4ade80' : '#f87171')) : null });
}

// подсказка при наведении на график с _hv: ближайшая точка по времени — значение и время (подсказка и вертикальная линия — два плавающих элемента)
function mdTipEls() {
  let tip = document.getElementById('mdChartTip'), line = document.getElementById('mdChartTipLine');
  if (!tip) {
    tip = document.createElement('div'); tip.id = 'mdChartTip'; tip.className = 'md-ctip'; tip.hidden = true;
    line = document.createElement('div'); line.id = 'mdChartTipLine'; line.className = 'md-ctipline'; line.hidden = true;
    document.body.append(line, tip);
  }
  return [tip, line];
}
function mdOnChartHover(e) {
  const cv = e.target.closest && e.target.closest('canvas');
  const hv = cv && cv._hv;
  const [tip, line] = mdTipEls();
  if (!hv) { tip.hidden = true; line.hidden = true; return; }
  const r = cv.getBoundingClientRect();
  const x = Math.max(0, Math.min(hv.w - 1, (e.clientX - r.left) * (hv.w / r.width)));
  const tc = hv.tEnd - ((hv.w - 1 - x) / (hv.w - 2)) * hv.win;
  let a = 0, b = hv.t.length - 1;                       // ближайшая по времени точка (ряд отсортирован)
  while (b - a > 1) { const mid = (a + b) >> 1; if (hv.t[mid] < tc) a = mid; else b = mid; }
  const i = Math.abs(hv.t[a] - tc) <= Math.abs(hv.t[b] - tc) ? a : b;
  const d = new Date(hv.t[i] * 1000), p2 = (v) => String(v).padStart(2, '0');
  const when = p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
  const gap = Math.abs(hv.t[i] - tc) > Math.max(hv.dt * 3, 5);
  tip.innerHTML = gap || hv.vals[i] == null || hv.ok[i] === 0 ? 'нет данных · ' + when : hv.tip(i) + ' · ' + when;
  tip.hidden = false;
  tip.style.left = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8) + 'px';
  tip.style.top = Math.max(4, r.top - tip.offsetHeight - 6) + 'px';
  line.hidden = false;
  line.style.left = (r.left + (x * r.width) / hv.w) + 'px';
  line.style.top = r.top + 'px';
  line.style.height = r.height + 'px';
}

// поправка по таблице (36 ячеек по 10°, линейно по кругу) — как mirror_corr в сервисе; нет таблицы — 0
function mdMirrorCorr(table, theta) {
  if (!table || table.length !== 36) return 0;
  const x = (((theta % 360) + 360) % 360) / 10, i = Math.floor(x) % 36, f = x - Math.floor(x);
  return table[i] + (table[(i + 1) % 36] - table[i]) * f;
}

// отклонение валов во времени: Δ = θ1 + θ2 − эталон (зеркально) или θ1 − θ2 − эталон, приведённое к −180..180; по истории углов двух зон
function mdDrawMirrorCharts(cam, h) {
  const wrap = (x) => ((((x + 180) % 360) + 360) % 360) - 180;
  (cam.groups || []).filter((g) => g.type === 'mirror').forEach((g) => {
    const cv = mdEl('md-mx-' + cam.id + '-' + g.id), cv0 = mdEl('md-mx0-' + cam.id + '-' + g.id);
    const a = h.zones[g.zones[0]], b = h.zones[g.zones[1]];
    if (!cv || !cv0 || !a || !b) return;
    const live = (((MV.states[cam.id] || {}).result || {}).groups || []).find((q) => q.id === g.id);
    const shape = g.learn === false ? null : (live && live.shape);         // выученная форма: сейчас одна на всё окно графика (плывёт медленно)
    const plain = [], fixed = [];
    a.flow.forEach((v, i) => {
      if (!a.bit[i] || !b.bit[i]) { plain.push(null); fixed.push(null); return; }
      const raw = g.mode === 'same' ? v - b.flow[i] : v + b.flow[i];
      plain.push(wrap(raw - g.ref));                                                           // как есть: только уровень (эталон)
      fixed.push(wrap(raw - mdMirrorCorr(g.table, v) - mdMirrorCorr(shape, v) - g.ref));       // с поправкой: ручная таблица + выученная форма
    });
    const span = Math.max(20, Math.ceil((g.tol * 2) / 10) * 10);                               // одна шкала на оба графика: сравнивать на глаз
    const o = { h: 64, lines: [{ v: g.tol, color: '#ef4444' }, { v: -g.tol, color: '#ef4444' }, { v: 0, color: '#64748b' }] };
    mdDrawSeries(cv0, h.t || [], plain, h.ok || [], -span, span, o);
    mdDrawSeries(cv, h.t || [], fixed, h.ok || [], -span, span, o);
  });
}

function mdDrawChart(cam, z, d, t, ok) {
  const cv = mdEl('md-ch-' + cam.id + '-' + z.id);
  if (!cv || !d) return;
  if (z.type === 'cycle') { mdDrawSeries(cv, t, d.flow.map((v, i) => (d.bit[i] ? v : null)), ok, 0, 360, { wrap: true, lines: [{ v: 180, color: '#64748b' }] }); return; }
  const w = cv.clientWidth, hh = cv.height || 84, strip = 7;
  if (!w) return;
  if (cv.width !== w) cv.width = w;
  const g = cv.getContext('2d');
  g.clearRect(0, 0, w, hh);
  cv._hv = null;
  const ys = z.metric === 'flow' ? d.flow : d.diff, n = ys.length;
  const win = MD.win;
  const plotH = hh - strip - 13;
  const top = Math.max(z.on * 1.6, ...ys, 0.0001);
  const Y = (v) => plotH - (v / top) * (plotH - 6) - 1;
  g.fillStyle = '#94a3b8'; g.font = '10px sans-serif';
  g.fillText(top.toFixed(top < 10 ? 2 : 0), 3, 10);
  g.fillText('−' + mdFmtWin(win), 3, hh - 1);
  g.textAlign = 'right'; g.fillText('сейчас', w - 3, hh - 1); g.textAlign = 'left';
  if (n < 2) return;
  const tEnd = t[n - 1];
  const X = (i) => w - 1 - ((tEnd - t[i]) / win) * (w - 2);
  const dt = (t[n - 1] - t[0]) / (n - 1);
  const bw = Math.max(1.5, (dt / win) * w + 1);
  const gap = Math.max(dt * 3, 5);   // разрыв в данных (камера молчала) — линию не тянем
  cv._hv = { t, vals: ys, ok, win, w, tEnd, dt, tip: (i) => z.metric + ' <b>' + mdNumFmt(ys[i]) + '</b>' };      // подсказка при наведении
  for (let i = 0; i < n; i += 1) {
    const bad = ok[i] === 0;
    const x = X(i) - bw;
    if (bad) { g.fillStyle = 'rgba(156,163,175,.30)'; g.fillRect(x, 0, bw, plotH); }
    else if (d.bit[i]) { g.fillStyle = 'rgba(74,222,128,.20)'; g.fillRect(x, 0, bw, plotH); }
    g.fillStyle = bad ? '#9ca3af' : (d.bit[i] ? '#4ade80' : '#f87171');
    g.fillRect(x, plotH + 3, bw, strip);
  }
  g.setLineDash([4, 3]); g.lineWidth = 1;
  g.strokeStyle = '#ef4444'; g.beginPath(); g.moveTo(0, Y(z.on)); g.lineTo(w, Y(z.on)); g.stroke();
  g.strokeStyle = '#f59e0b'; g.beginPath(); g.moveTo(0, Y(z.off)); g.lineTo(w, Y(z.off)); g.stroke();
  g.setLineDash([]);
  g.strokeStyle = '#38bdf8'; g.lineWidth = 1.6; g.beginPath();
  ys.forEach((v, i) => (i && t[i] - t[i - 1] <= gap ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))));
  g.stroke();
}

// колесо мыши над графиком: вверх — ближе (короче окно), вниз — дальше; окно общее для всех графиков
function mdOnWheel(e) {
  const cv = e.target.closest && e.target.closest('.md-zone__chart canvas');
  if (!cv) return;
  e.preventDefault();
  MD.win = Math.max(MD_WIN_MIN, Math.min(MD_WIN_MAX, Math.round(MD.win * (e.deltaY < 0 ? 0.8 : 1.25))));
  try { localStorage.setItem(MD_WIN_KEY, String(MD.win)); } catch (err) { /* не критично */ }
  const lbl = mdEl('mdWinLbl');
  if (lbl) lbl.textContent = mdFmtWin(MD.win);
  clearTimeout(MD.winTimer);
  MD.winTimer = setTimeout(mdDrawTrends, 120);
}

// ---------- вкладка «ПЛК · сценарии · группы» ----------
function mdSourceOptions() {
  const out = [];
  mdCfgCams().forEach((c) => {
    c.zones.forEach((z) => { if (z.type !== 'cycle' && z.type !== 'level' && z.type !== 'clarity') out.push({ camera: c.id, zone: z.id, key: c.id + '/' + z.id, label: c.name + ' · ' + z.name, ip: mdIpTag(c.url) }); });
    (c.groups || []).forEach((g) => { if (g.type !== 'mirror' && g.type !== 'level') out.push({ camera: c.id, group: g.id, key: 'g:' + c.id + '/' + g.id, label: c.name + ' · ' + g.name, ip: '' }); });
  });
  return out;
}

// «Что пишем в ПЛК»: все выходы, заданные у окон, стаканов, валов, групп и камер (кроме сценариев) — куда именно уходит значение
const MD_WRITE_WHAT = { clarity: 'мутность сока', cycle: 'угол вала', level: 'уровень стекла' };
function mdPlcWritesRows() {
  const addr = (o) => (o.kind === 'coil' || o.kind === 'coils' ? 'coil ' : 'рег. ') + o.address;
  const rows = [];
  mdCfgCams().forEach((c) => {
    const add = (what, name, o) => rows.push({ cam: c, name, what, to: addr(o) });
    c.zones.forEach((z) => {
      if (z.out && z.enabled !== false) add(MD_WRITE_WHAT[z.type] || (z.out.value === 'state' ? 'состояние окна' : 'движение'), z.name, z.out);
      if (z.rpm_out && z.enabled !== false) add('обороты', z.name, z.rpm_out);
    });
    (c.groups || []).forEach((g) => { if (g.out) add(g.type === 'mirror' ? 'рассинхрон валов' : 'вычисление', g.name, g.out); });
    if (c.status_out) add('камера жива', c.name, c.status_out);
  });
  return rows;
}
function mdPlcWritesHtml(m) {
  const rows = mdPlcWritesRows();
  const to = m.enable ? 'ПЛК ' + escapeHtml(m.host || '—') + ' : ' + escapeHtml(m.port) : 'ПЛК выключен — пока никуда не пишется';
  const list = rows.length ? `<div class="md-sbox">${rows.map((r) => `<div class="md-sr"><div class="md-sr__head"><span class="md-zr__bar"></span>
      <div class="md-sr__nm"><b>${escapeHtml(r.name)}</b><div class="md-sr__sum"><span>${escapeHtml(r.cam.name)} · ${escapeHtml(r.what)}</span></div></div>
      <div class="md-sr__val"><b>${r.to}</b><span class="md-wst" data-cam="${escapeHtml(r.cam.id)}"></span></div></div></div>`).join('')}</div>`
    : '<p class="net-hint">Пока ничего. Регистр задаётся в самой разметке: у окна, стакана («мутность в ПЛК»), вала или вычисления; здесь они появятся сами.</p>';
  return `<h3 class="md-h md-h--row md-h--sec">Что пишем в ПЛК <span class="md-dim">${to}</span></h3>${list}`;
}

function mdPlcHtml() {
  const m = (MV.cfg && MV.cfg.modbus) || { enable: false, host: '', port: 502, device_id: 1, write_period_s: 1 };
  const a = 'data-md="p"';
  const list = ((MV.cfg && MV.cfg.scenarios) || []).map((s, i) => mdScenarioHtml(s, i)).join('');
  const scens = list ? `<div class="md-sbox">${list}</div>` : '<p class="net-hint">Сценариев нет. Сценарий берёт окна и зоны любых камер или вычисления, считает «хоть одно» / «все» / «сколько» (по жому или по движению) и пишет ОДНО значение в регистр или coil. Только сценарии пишут в ПЛК значения зон.</p>';
  return `<div class="md-h md-h--row md-h--sec">Подключение</div>
    <div class="md-sbox md-plcbox ${m.enable ? 'is-on' : 'is-off'}" id="mdPlcBlock"><div class="md-sr${MD.open['plc'] ? ' is-open' : ''}">
      <div class="md-sr__head md-plcbox__head" data-zt="plc" title="Нажмите, чтобы раскрыть настройки подключения">
        <span class="md-zr__bar"></span>
        <div class="md-sr__nm"><b>ПЛК ${escapeHtml(m.host || '—')} : ${escapeHtml(m.port)}</b><div class="md-sr__sum"><span id="mdPlcState">${m.enable ? 'включено' : 'выключено — в ПЛК ничего не пишется'}</span><span id="md-plcsum"></span></div></div>
        <label class="switch-toggle md-sw" title="Включить связь с ПЛК: сценарии начнут записывать значения в контроллер"><span class="switch"><input type="checkbox" ${a} data-f="enable"${m.enable ? ' checked' : ''} /><span class="switch-track"></span></span></label>
        <span class="md-zr__chev">⌄</span>
      </div>
      <div class="md-sr__det">
        <div class="md-plc__grid">
          <label><span>IP контроллера</span><input type="text" class="md-plc__ip" ${a} data-f="host" value="${escapeHtml(m.host)}" placeholder="192.168.0.10" /></label>
          <label><span>Порт</span>${mdNum(a + ' data-f="port"', m.port, 'min="1" max="65535"')}</label>
          <label><span>Unit ID</span>${mdNum(a + ' data-f="device_id"', m.device_id, 'min="0" max="255"')}</label>
          <label title="Значения пишутся сразу при изменении и ещё раз с этим периодом"><span>Обновлять не реже, с</span>${mdNum(a + ' data-f="write_period_s"', m.write_period_s, 'min="0.1" step="0.1"')}</label>
          <label class="md-plc__ping"><span>Проверка связи, регистр</span>${mdNum('id="mdPingAddr"', 0, 'min="0" max="65535"')}<button type="button" class="btn-secondary md-x" id="mdPing">Прочитать</button></label>
        </div>
        <span class="net-hint" id="mdPingInfo">Проверка только читает один регистр, в ПЛК ничего не пишется.</span>
      </div></div></div>
    ${mdPlcWritesHtml(m)}
    <h3 class="md-h md-h--row md-h--sec"><button type="button" class="md-add" data-md="s" data-f="add" title="Добавить сценарий" aria-label="Добавить сценарий">+</button>Сценарии записи <span class="md-dim">объединяют окна и зоны в одно значение</span></h3>${scens}
    ${mdGroupsSectionHtml()}`;
}

const MD_SC_SIGNALS = [['pulp', 'Жом есть'], ['move', 'Движение']];
const MD_SC_RULES = [['or', 'Хоть одно'], ['and', 'Все'], ['sum', 'Сколько']];
function mdScenarioSummary(s, cams) {
  const pulp = s.signal === 'pulp';
  const what = { or: pulp ? 'хоть в одном жом' : 'хоть одно крутится', and: pulp ? 'жом во всех' : 'все крутятся', sum: pulp ? 'сколько окон с жомом' : 'сколько крутится' }[s.rule] || '';
  const names = s.sources.map((x) => { const c = cams.find((q) => q.id === x.camera), z = c && (x.group ? (c.groups || []).find((q) => q.id === x.group) : c.zones.find((q) => q.id === x.zone)); return z ? z.name : null; }).filter(Boolean);
  const tags = names.slice(0, 5).map((n) => `<span class="md-sr__tag">${escapeHtml(n)}</span>`).join('') + (names.length > 5 ? `<span class="md-sr__tag">+${names.length - 5}</span>` : '');
  return `<span>${pulp ? 'жом' : 'движение'} · ${what}</span>${tags || '<span class="md-sr__tag">нет источников</span>'}`;
}

function mdScenarioHtml(s, i) {
  const a = `data-md="s" data-i="${i}"`;
  const cams = mdCfgCams();
  const sid = escapeHtml(s.id);
  const opts = mdSourceOptions();
  const chosen = new Set(s.sources.map((x) => (x.group ? 'g:' + x.camera + '/' + x.group : x.camera + '/' + x.zone)));
  const seg = (name, list, cur, f) => `<span class="md-seg">${list.map(([v, t]) => `<label><input type="radio" name="${name}" ${a} data-f="${f}" value="${v}"${cur === v ? ' checked' : ''} /><span>${t}</span></label>`).join('')}</span>`;
  const src = opts.length ? opts.map((o) => { const v = escapeHtml(o.key); return `<label class="md-cp${o.group ? ' md-cp--grp' : ''}" ${o.group ? '' : `data-peek="${v}"`} title="${o.group ? 'Вычисление: берётся его значение (сумма, уровень, % наработки)' : ''}"><input type="checkbox" ${a} data-f="src" value="${v}"${chosen.has(o.key) ? ' checked' : ''} />${o.group ? '<b class="md-sig">Σ</b>' : `<i class="md-dot" id="md-sd-${escapeHtml(o.camera)}-${escapeHtml(o.zone)}"></i>`}<span>${escapeHtml(o.label)}${o.ip}</span></label>`; }).join('')
    : '<span class="net-hint">нет размеченных зон</span>';
  const open = MD.open['sc-' + s.id];
  const outTxt = s.out ? (s.out.kind === 'coil' ? 'coil ' : 'рег. ') + s.out.address : 'не пишем';
  return `<div class="md-sr${open ? ' is-open' : ''}" id="md-sr-${sid}" data-st="i">
    <div class="md-sr__head" data-zt="sc-${sid}" data-peek-scn="${i}" title="Нажмите, чтобы раскрыть настройки. Наведите — покажу кадры источников с подсвеченными зонами">
      <span class="md-zr__bar"></span>
      <div class="md-sr__nm"><b>${escapeHtml(s.name)}</b><div class="md-sr__sum">${mdScenarioSummary(s, cams)}</div></div>
      <div class="md-sr__val"><b id="md-sv-${sid}">–</b><span><span id="md-su-${sid}"></span> → ${outTxt}</span></div>
      <span class="md-zr__chev">⌄</span>
    </div>
    <div class="md-sr__det">
      <div><div class="md-zr__t">Название</div><input type="text" ${a} data-f="name" value="${escapeHtml(s.name)}" /></div>
      <div><div class="md-zr__t">Источники</div><div class="md-sr__chips">${src}</div></div>
      <div class="md-sr__g3">
        <div><div class="md-zr__t">Что считать</div>${seg('sg-' + sid, MD_SC_SIGNALS, s.signal || 'move', 'signal')}</div>
        <div><div class="md-zr__t">Правило</div>${seg('rl-' + sid, MD_SC_RULES, s.rule, 'rule')}</div>
        <div><div class="md-zr__t">Выход</div><div class="md-out"><select ${a} data-f="okind">${mdSel([['', 'не писать'], ['holding', 'регистр'], ['coil', 'coil']], s.out ? s.out.kind : '')}</select>
          <input type="number" min="0" max="65535" ${a} data-f="oaddr" value="${s.out ? s.out.address : 0}" title="Адрес" /></div></div>
      </div>
      <div class="md-sr__foot"><span class="net-hint" id="md-ss-${sid}"></span><button type="button" class="btn-secondary md-x md-sr__del" ${a} data-f="del" title="Удалить сценарий">удалить сценарий</button></div>
    </div>
  </div>`;
}

function mdOnPlc(t) {
  const m = MV.cfg.modbus;
  const f = t.dataset.f;
  mdEdit(() => {
    if (f === 'enable') {
      m.enable = t.checked;
      // блок «загорается» сразу, без перерисовки вкладки
      mdEl('mdPlcBlock')?.classList.toggle('is-on', m.enable);
      mdEl('mdPlcBlock')?.classList.toggle('is-off', !m.enable);
      const st = mdEl('mdPlcState');
      if (st) st.textContent = m.enable ? 'включено' : 'выключено — в ПЛК ничего не пишется';
    }
    else if (f === 'host') m.host = t.value.trim();
    else if (f === 'write_period_s') m.write_period_s = parseFloat(t.value) || 1;
    else m[f] = parseInt(t.value, 10) || (f === 'port' ? 502 : 0);
  });
}

function mdOnScenario(t) {
  const f = t.dataset.f;
  const list = (MV.cfg.scenarios = MV.cfg.scenarios || []);
  if (f === 'add') {
    let n = 1;
    while (list.some((s) => s.id === 's' + n)) n += 1;
    MD.open['sc-s' + n] = true;
    mdEdit(() => list.push({ id: 's' + n, name: 'Сценарий ' + n, rule: 'or', signal: 'move', sources: [], out: { kind: 'holding', address: 0 } }), true);
    return;
  }
  const s = list[Number(t.dataset.i)];
  if (!s) return;
  if (f === 'del') {
    if (!confirm('Удалить сценарий «' + s.name + '»?')) return;
    mdEdit(() => list.splice(Number(t.dataset.i), 1), true);
    return;
  }
  mdEdit(() => {
    if (f === 'name') s.name = t.value;
    else if (f === 'rule') s.rule = t.value;
    else if (f === 'signal') s.signal = t.value;
    else if (f === 'okind') s.out = t.value ? { kind: t.value, address: (s.out && s.out.address) || 0 } : null;
    else if (f === 'oaddr') { if (s.out) s.out.address = parseInt(t.value, 10) || 0; }
    else if (f === 'src') {
      const isGrp = t.value.startsWith('g:');
      const [camera, id] = (isGrp ? t.value.slice(2) : t.value).split('/');
      s.sources = s.sources.filter((x) => !(x.camera === camera && (isGrp ? x.group === id : x.zone === id)));
      if (t.checked) s.sources.push(isGrp ? { camera, group: id } : { camera, zone: id });
    }
  });
}

function mdBindPlc() {
  mdEl('mdPing')?.addEventListener('click', async () => {
    const m = MV.cfg.modbus, info = mdEl('mdPingInfo');
    info.textContent = 'читаю…';
    const r = await mvJson('/api/motion/modbus_ping', { host: m.host, port: m.port, device_id: m.device_id, address: parseInt(mdEl('mdPingAddr').value, 10) || 0 });
    info.textContent = r.error ? r.error : (r.ok ? 'связь есть, регистр = ' + r.info + ' (только чтение)' : 'нет связи: ' + r.info);
  });
}

// ---------- группы зон (ниже сценариев; по всем камерам) ----------
const MD_GROUP_TYPES = { sum: 'сумма зон', monotonic: 'уровень снизу вверх', worktimer: 'наработка за окно', mirror: 'синхронность валов', level: 'уровень жидкости' };
function mdGroupRowHtml(cam, g, i) {
  const a = `data-md="g" data-cam="${escapeHtml(cam.id)}" data-i="${i}"`;
  const k = escapeHtml(cam.id + '-' + g.id);
  const names = g.zones.map((id) => { const z = cam.zones.find((q) => q.id === id); return `<span class="md-sr__tag">${escapeHtml(z ? z.name : id)}</span>`; }).join('');
  const legacy = g.out ? ({ holding: 'регистр', bits: 'биты в регистр', coils: 'coil на зону' }[g.out.kind] + ' @' + g.out.address) : '';
  const open = MD.open['gr-' + k];
  const det = [];
  if (g.type === 'worktimer') det.push(`окно ${g.window_s} с · шкала ${g.scale} · мин. зон ${g.min_active}`);
  if (g.type === 'monotonic') det.push('порядок снизу вверх — как зоны в списке');
  if (legacy) det.push(`<span title="Прямой вывод группы больше не настраивается: пишите в ПЛК через сценарий. Старый вывод продолжает работать, пока его не перенесёте.">ПЛК (старый вывод): ${legacy}</span>${g.out.kind === 'holding' ? `<button type="button" class="btn-secondary md-x" ${a} data-f="tosc" title="Создать сценарий с этим выходом и убрать вывод у группы">перенести в сценарий</button>` : ''}`);
  return `<div class="md-sr${open ? ' is-open' : ''}" id="md-gr-${k}" data-st="i">
    <div class="md-sr__head" data-zt="gr-${k}" title="Нажмите, чтобы раскрыть">
      <span class="md-zr__bar"></span>
      <div class="md-sr__nm"><b>${escapeHtml(g.name)}</b><div class="md-sr__sum"><span>${MD_GROUP_TYPES[g.type] || g.type}</span><span class="md-dim">${escapeHtml(cam.name)}</span>${names}</div></div>
      <div class="md-sr__val"><b id="md-gv-${k}">–</b><span>${legacy ? 'вывод: ' + legacy : 'без вывода'}</span></div>
      <span class="md-zr__chev">⌄</span>
    </div>
    <div class="md-sr__det">
      ${det.length ? `<div class="md-sr__foot">${det.join('')}</div>` : ''}
      <div class="md-sr__foot">${g.type === 'mirror' ? '' : `<button type="button" class="btn-secondary md-x" ${a} data-f="edit">Изменить</button>`}<button type="button" class="btn-secondary md-x md-sr__del" ${a} data-f="del" title="Удалить вычисление">удалить</button></div>
    </div>
  </div>`;
}

function mdGroupsSectionHtml() {
  const rows = [];
  mdCfgCams().forEach((cam) => (cam.groups || []).forEach((g, i) => { if (g.type !== 'level') rows.push(mdGroupRowHtml(cam, g, i)); }));   // «уровень» — на своей вкладке
  const list = rows.length ? `<div class="md-sbox">${rows.join('')}</div>` : '<p class="net-hint">Вычислений нет. Вычисление складывает окна одной камеры: сумма — сколько активно, уровень снизу вверх — шахта, наработка за окно — доля времени в % (лента жома). Результат берёт сценарий и пишет в ПЛК. Зоны из РАЗНЫХ камер объединяйте в сценарии выше.</p>';
  return `<div class="md-groups"><h3 class="md-h md-h--row md-h--sec"><button type="button" class="md-add" data-md="g" data-f="add" title="Добавить вычисление" aria-label="Добавить вычисление">+</button>Вычисления <span class="md-dim">считают окна одной камеры; результат берёт сценарий</span></h3>${list}<div id="mdGrpEd">${MD.editGrp ? mdGroupEditorHtml() : ''}</div></div>`;
}

function mdGeZonesHtml(cam, g) {
  const zs = cam.zones.filter((z) => z.type !== 'level' && z.type !== 'clarity');
  if (!zs.length) return '<span class="net-hint">у камеры нет зон</span>';
  return zs.map((z) => `<label class="md-chk"><input type="checkbox" value="${escapeHtml(z.id)}"${g.zones.includes(z.id) ? ' checked' : ''} />${escapeHtml(z.name)}</label>`).join('');
}

function mdGroupEditorHtml() {
  const e = MD.editGrp;
  const cam = e && mvCam(e.cam);
  if (!cam) return '';
  const g = e.i >= 0 ? cam.groups[e.i] : { id: '', name: '', type: 'sum', zones: [], out: { kind: 'holding', address: 0 }, window_s: 60, min_active: 1, agg: 'any', scale: 100 };
  if (!g) return '';
  const camOpts = mdCfgCams().filter((c) => c.zones.length).map((c) => [c.id, escapeHtml(c.name)]);
  // сетка 2–3 колонки: подпись слева, поле справа; зоны, подсказка и кнопки — на всю ширину
  return `<div class="md-card md-ge" id="mdGe">
    ${mdField('Камера', `<select id="geCam"${e.i >= 0 ? ' disabled' : ''}>${mdSel(camOpts, cam.id)}</select>`)}
    ${mdField('Название', `<input type="text" id="geName" value="${escapeHtml(g.name)}" placeholder="Уровень шахты / Наработка" />`)}
    ${mdField('Тип', `<select id="geType">${mdSel([['sum', 'sum — сколько зон активно'], ['monotonic', 'monotonic — уровень снизу вверх'], ['worktimer', 'worktimer — наработка за окно']], g.type)}</select>`)}
    <label class="md-gt" data-for="worktimer"><span>Окно, с</span><input type="number" id="geWin" min="1" value="${g.window_s || 60}" /></label>
    <label class="md-gt" data-for="worktimer" title="Минимум активных зон"><span>Мин. зон</span><input type="number" id="geMin" min="1" value="${g.min_active || 1}" /></label>
    <label class="md-gt" data-for="worktimer" title="100 = проценты; 1000 или 4095 — для аналогового выхода"><span>Шкала</span><input type="number" id="geScale" min="1" value="${g.scale || 100}" /></label>
    <label class="md-gt" data-for="worktimer"><span>Рабочая, если</span><select id="geAgg">${mdSel([['any', 'хоть один кадр активен'], ['majority', 'не меньше половины кадров'], ['all', 'все кадры активны']], g.agg || 'any')}</select></label>
    <div class="md-ge__zones"><span>Зоны</span><span class="md-ge__chk" id="geZones">${mdGeZonesHtml(cam, g)}</span></div>
    <p class="net-hint md-gt md-ge__full" data-for="monotonic">Порядок «снизу вверх» = порядок зон в списке (первая зона — низ шахты).</p>
    <div class="md-ge__btns"><button type="button" class="btn-primary" data-md="g" data-f="ok">Готово</button><button type="button" class="btn-secondary" data-md="g" data-f="cancel">Отмена</button></div></div>`;
}

function mdSyncGroupEditor() {
  const t = mdEl('geType');
  if (!t) return;
  document.querySelectorAll('#mdGe .md-gt').forEach((n) => { n.hidden = n.dataset.for !== t.value; });
}

function mdOnGroup(t) {
  const f = t.dataset.f;
  if (f === 'add') {
    const { cam: focused } = mdCamera();
    const first = focused && focused.zones.length ? focused : mdCfgCams().find((c) => c.zones.length);
    if (!first) { alert('Сначала разметьте хотя бы одну зону'); return; }
    MD.editGrp = { cam: first.id, i: -1 };
    MD.key = '';
    renderMotionDock();
    return;
  }
  if (f === 'edit') { MD.editGrp = { cam: t.dataset.cam, i: Number(t.dataset.i) }; MD.key = ''; renderMotionDock(); return; }
  if (f === 'cancel') { MD.editGrp = null; MD.key = ''; renderMotionDock(); return; }
  const cam = mvCam(f === 'ok' ? (MD.editGrp && MD.editGrp.cam) : t.dataset.cam);
  if (!cam) return;
  if (f === 'del') {
    const i = Number(t.dataset.i);
    if (!confirm('Удалить группу «' + cam.groups[i].name + '»?')) return;
    const gid = cam.groups[i].id;
    mdEdit(() => { cam.groups.splice(i, 1); (MV.cfg.scenarios || []).forEach((sc) => { sc.sources = sc.sources.filter((x) => !(x.camera === cam.id && x.group === gid)); }); }, true);
    return;
  }
  if (f === 'tosc') {          // старый вывод группы → сценарий с тем же регистром; у группы вывод снимаем
    const g = cam.groups[Number(t.dataset.i)];
    if (!g || !g.out || g.out.kind !== 'holding') return;
    const list = (MV.cfg.scenarios = MV.cfg.scenarios || []);
    let n = 1;
    while (list.some((x) => x.id === 's' + n)) n += 1;
    MD.open['sc-s' + n] = true;
    mdEdit(() => {
      const sc = { id: 's' + n, name: cam.name + ' · ' + g.name, rule: 'sum', signal: 'move', sources: [{ camera: cam.id, group: g.id }], out: { kind: 'holding', address: g.out.address } };
      if (cam.modbus) sc.modbus = { ...cam.modbus };      // у камеры был свой ПЛК — сценарий пишет туда же
      list.push(sc);
      g.out = null;
    }, true);
    return;
  }
  if (f === 'ok') {
    const ids = [...document.querySelectorAll('#mdGe #geZones input[type=checkbox]:checked')].map((x) => x.value);
    if (!ids.length) { alert('Выберите хотя бы одну зону'); return; }
    const type = mdEl('geType').value;
    const idx = MD.editGrp.i;
    const old = idx >= 0 ? cam.groups[idx] : null;
    const ng = { id: old ? old.id : '', name: mdEl('geName').value.trim() || 'Вычисление', type, zones: ids,
      out: old ? old.out : null };      // вывод в ПЛК у группы больше не правится: пишет сценарий; старый вывод сохраняем как был
    if (!ng.id) { let n = 1; while ((cam.groups || []).some((q) => q.id === 'g' + n)) n += 1; ng.id = 'g' + n; }
    if (type === 'monotonic') ng.order = cam.zones.map((z) => z.id).filter((id) => ids.includes(id));
    if (type === 'worktimer') {
      Object.assign(ng, { window_s: parseInt(mdEl('geWin').value, 10) || 60, min_active: Math.min(ids.length, parseInt(mdEl('geMin').value, 10) || 1),
        agg: mdEl('geAgg').value, scale: parseInt(mdEl('geScale').value, 10) || 100 });
    }
    mdEdit(() => {
      cam.groups = cam.groups || [];
      if (idx >= 0) cam.groups[idx] = ng; else cam.groups.push(ng);
      MD.editGrp = null;
    }, true);
  }
}

// ---------- живые значения ----------
// шкала порога: закрашено текущее значение, синие метки — off и on; между ними серая зона гистерезиса
function mdGauge(v, off, on, lo, hi, color) {
  const p = (x) => Math.max(0, Math.min(100, ((x - lo) / (hi - lo)) * 100)).toFixed(1);
  const [a, b] = off <= on ? [off, on] : [on, off];
  return `<svg viewBox="0 0 100 10" preserveAspectRatio="none"><rect x="0" y="3" width="100" height="4" rx="2" class="md-g-bg"/><rect x="${p(a)}" y="3" width="${(p(b) - p(a)).toFixed(1)}" height="4" class="md-g-hy"/>` +
    `<rect x="0" y="3" width="${p(v)}" height="4" rx="2" style="fill:${color}"/><line x1="${p(a)}" x2="${p(a)}" y1="0" y2="10" class="md-g-m"/><line x1="${p(b)}" x2="${p(b)}" y1="0" y2="10" class="md-g-m"/></svg>`;
}

// строка окна: цвет и плашка состояния (красный — пусто, жёлтый — жом стоит, зелёный — жом едет) и шкалы порогов
const MD_ZSTATE = { 0: ['e', 'пусто'], 1: ['p', 'жом стоит'], 2: ['m', 'жом едет'] };
function mdPaintZoneRow(k, zc, zl) {
  const row = mdEl('md-zr-' + k);
  if (!row) return;
  let st, txt;
  if (zl.state != null) [st, txt] = MD_ZSTATE[zl.state];
  else if (zl.bit) [st, txt] = ['m', 'движется'];
  else [st, txt] = ['i', 'стоит'];
  row.dataset.st = st;
  const pl = mdEl('md-zp-' + k);
  if (pl) pl.textContent = txt;
  const gm = mdEl('md-gm-' + k);
  if (gm) {
    const v = Number(zl.value) || 0;
    gm.innerHTML = mdGauge(v, zc.off, zc.on, 0, Math.max(zc.on * 2, v * 1.15, 0.5), zl.bit ? 'var(--success)' : 'var(--muted)');
  }
  const gf = mdEl('md-gf-' + k);
  if (gf && zl.tex !== undefined) {
    const m = zc.fill_metric, v = Number(m === 'lum' ? zl.lum : m === 'rel' ? zl.rel : zl.tex) || 0;
    const [lo, hi] = m === 'rel' ? [0.4, 1.0] : m === 'lum' ? [0, 255] : [0, Math.max(zc.fill_on * 1.6, v * 1.2, 1)];
    gf.innerHTML = mdGauge(v, zc.fill_off, zc.fill_on, lo, hi, zl.fill ? 'var(--warning)' : 'var(--danger)');
    const gv = mdEl('md-gfv-' + k);
    if (gv) gv.textContent = m === 'lum' ? v.toFixed(0) : v.toFixed(2);
  }
}

function paintMotionDockLive() {
  if (!MV.dockOpen) return;
  const mode = mdMode();
  if (mode === 'none') return;
  if (MD.tab === 'shafts' && typeof mdPaintShafts === 'function') mdPaintShafts();
  const { source, cam } = mdCamera();
  const pe = mdEl('mdPill');
  if (pe) {
    const [pill, cls] = mode === 'all' ? mdAllPill() : mdPill(MV.states[source.serial], cam && cam.enabled);
    pe.textContent = pill;
    pe.className = 'motion-dock__pill ' + cls;
  }
  const st0 = source && MV.states[source.serial];
  const rec = mdEl('mdRec');
  if (rec) {
    const on = !!(st0 && st0.recording);
    rec.textContent = on ? '■' : '●';
    rec.classList.toggle('is-rec', on);
    rec.title = on ? 'Остановить запись метрик в CSV' : 'Писать метрики каждого кадра в CSV (для калибровки порогов)';
  }
  const recAll = mdEl('mdRecAll');
  if (recAll && mode === 'all') {
    const n = mdCfgCams().filter((c) => c.enabled && (MV.states[c.id] || {}).recording).length;
    recAll.textContent = n ? '■ все (' + n + ')' : '● все';
    recAll.classList.toggle('is-rec', n > 0);
  }
  mdCfgCams().forEach((c) => {
    const st = MV.states[c.id];
    const res = st && st.result;
    mdLearnTick(c, res);
    ((res && res.zones) || []).forEach((z) => {
      const bit = mdEl('md-zb-' + c.id + '-' + z.id);
      if (bit) { bit.textContent = z.bit; bit.classList.toggle('on', z.bit === 1); }
      const val = mdEl('md-zv-' + c.id + '-' + z.id);
      if (val) val.textContent = Number(z.value).toFixed(2);
      const fl = mdEl('md-zf-' + c.id + '-' + z.id);
      if (fl) fl.textContent = mdFillText(z);
      const zc = c.zones.find((q) => q.id === z.id);
      if (zc && zc.type !== 'cycle') mdPaintZoneRow(c.id + '-' + z.id, zc, z);
    });
    ((res && res.groups) || []).forEach((g) => {
      const b = mdEl('md-gv-' + c.id + '-' + g.id);
      if (b) { b.textContent = g.type === 'mirror' ? g.alarm : g.value; b.classList.toggle('on', g.type === 'mirror' ? g.alarm === 0 : g.value > 0); }
      const grow = mdEl('md-gr-' + c.id + '-' + g.id);
      if (grow) grow.dataset.st = g.type === 'mirror' ? (g.alarm ? 'e' : 'm') : g.value > 0 ? 'm' : 'i';
      if (g.type !== 'mirror') return;
      const k = c.id + '-' + g.id, ok = g.valid && g.delta != null;
      const v = mdEl('md-mxv-' + k);
      if (ok && !g.hold) mdLastDelta[k] = g.delta;
      const shown = ok ? (g.hold && mdLastDelta[k] != null ? mdLastDelta[k] : g.delta) : null;      // удержание: палку не видно, угол достроен — Δ с него не показываем
      if (v) {
        v.textContent = shown != null ? (shown > 0 ? '+' : '') + shown.toFixed(1) + '°' : '—';
        v.style.color = ok && g.alarm ? '#f87171' : '';
        v.style.opacity = ok && g.hold ? '.45' : '';
        v.title = ok && g.hold ? 'Палку не видно, угол достроен по скорости: показано последнее надёжное значение' : 'Отклонение от нормы, °';
      }
      const ab = mdEl('md-mxb-' + k);
      if (ab) { ab.textContent = g.alarm; ab.style.color = g.alarm ? '#f87171' : ''; }
      const ls = mdEl('md-mxl-' + k);
      if (ls) ls.textContent = g.learn ? (g.learn.ready ? 'учится · форма ±' + (g.learn.amp / 2).toFixed(1) + '°' : 'копит: ' + g.learn.bins + ' из 36 ячеек') : 'выключена';
      const s = mdEl('md-mxs-' + k);
      if (s) {                  // оба вала: красный и синий конец и направление каждого; ниже — отклонение Δ уже в заголовке карточки
        const rod = (zid, name) => { const z = (res.zones || []).find((q) => q.id === zid); return z && z.valid ? `<div>${escapeHtml(name)}: ${mdRodAnglesHtml(z)}</div>` : `<div>${escapeHtml(name)}: не видно</div>`; };
        const ids = ((c.groups || []).find((q) => q.id === g.id) || { zones: [] }).zones;
        s.innerHTML = ids.map((zid) => rod(zid, (c.zones.find((q) => q.id === zid) || { name: zid }).name)).join('');
      }
    });
  });
  // статус каждой камеры (заголовки зон и список камер в общем виде)
  state.cameras.filter((c) => c.kind === 'rtsp').forEach((c) => {
    const p = mdEl('md-cp-' + c.serial);
    if (!p) return;
    const [txt, cls] = mdPill(MV.states[c.serial], mvEnabled(c.serial));
    p.textContent = txt;
    p.className = 'motion-dock__pill ' + cls;
  });
  (MV.scen || []).forEach((s) => {
    const b = mdEl('md-sv-' + s.id);
    if (b) b.textContent = s.value;
    const bits = s.bits || [];
    const pulp = s.signal === 'pulp';
    const row = mdEl('md-sr-' + s.id);
    if (row) row.dataset.st = s.value > 0 ? (pulp ? 'p' : 'm') : 'i';
    const un = mdEl('md-su-' + s.id);
    const sc = ((MV.cfg && MV.cfg.scenarios) || []).find((q) => q.id === s.id);
    if (un) un.textContent = sc && sc.rule === 'sum' ? 'из ' + bits.length : s.value > 0 ? 'да' : 'нет';
    const info = mdEl('md-ss-' + s.id);
    if (info) {
      info.textContent = (pulp ? 'жом в ' : 'крутится ') + bits.filter(Boolean).length + ' из ' + bits.length + ' · пишем ' + s.value + ' · ' +
        (!s.plc ? 'запись в ПЛК выключена' : s.ok === null ? 'ПЛК: ждёт' : s.ok ? 'ПЛК: записано' : 'ПЛК: ошибка ' + (s.error || ''));
    }
  });
  const ps = mdEl('md-plcsum');
  if (ps) {
    const w = (MV.scen || []).filter((q) => q.plc && q.out);
    const direct = mdPlcWritesRows(), n = w.length + direct.length;
    const bad = w.some((q) => q.ok === false) || direct.some((r) => ((MV.states[r.cam.id] || {}).plc || {}).ok === false);
    const good = w.every((q) => q.ok) && direct.every((r) => ((MV.states[r.cam.id] || {}).plc || {}).ok);
    ps.textContent = !(MV.cfg && MV.cfg.modbus && MV.cfg.modbus.enable) ? '' : !n ? ' · пока нечего писать (регистры не заданы)' : bad ? ' · ошибка записи' : good ? ' · связь есть, пишем ' + n : ' · ждём ответа';
    document.querySelectorAll('.md-wst').forEach((e) => {
      const p = (MV.states[e.dataset.cam] || {}).plc || {};
      e.textContent = !(MV.cfg.modbus && MV.cfg.modbus.enable) ? 'запись выключена' : p.ok == null ? 'ждёт' : p.ok ? 'записано' : 'ошибка ' + (p.error || '');
    });
  }
  // точка у каждой зоны в списке источников: зелёная — крутится, красная — стоит, серая — нет кадров
  mdCfgCams().forEach((c) => {
    const res = (MV.states[c.id] || {}).result;
    c.zones.forEach((zc) => {
      const dot = mdEl('md-sd-' + c.id + '-' + zc.id);
      if (!dot) return;
      const z = res && res.alive ? (res.zones || []).find((q) => q.id === zc.id) : null;
      dot.className = 'md-dot ' + (!z ? 'is-na' : z.bit ? 'is-on' : 'is-off');
    });
  });
  const box = mdEl('mdPlcCams');
  if (box && MD.tab === 'plc' && MV.cfg) {
    box.innerHTML = '<h3 class="md-h">Камеры и запись</h3><div class="md-camgrid">' + MV.cfg.cameras.filter((c) => c.enabled).map((c) => {
      const s = MV.states[c.id], p = (s && s.plc) || {};
      return `<div class="md-card md-card--sq"><b>${escapeHtml(c.name)}</b>${mdIpTag(c.url)}<span class="net-hint">${!MV.cfg.modbus.enable ? 'запись выключена' : p.ok === null || p.ok === undefined ? 'ПЛК: ждёт' : p.ok ? 'ПЛК: ок' : 'ПЛК: ошибка ' + escapeHtml(p.error || '')}</span></div>`;
    }).join('') + '</div>';
  }
  if (MD.tab === 'cams') {
    mdCfgCams().forEach((c) => {
      const p = mdEl('md-cr-' + c.id);
      if (!p) return;
      const [txt, cls] = mdPill(MV.states[c.id], c.enabled);
      p.textContent = txt;
      p.className = 'motion-dock__pill ' + cls;
      const pf = mdEl('md-perf-' + c.id);
      if (pf) pf.innerHTML = mdPerfHtml(c);
    });
  }
  if (MD.tab === 'level') mdLevelPaintLive();
  if (MD.tab === 'juice') jcPaintLive();
  if (MD.tab === 'zones') mdPaintZoneStats();
  if (MD.tab === 'zones' && (MV.tick % 2 === 0)) mdDrawTrends();
}

// ---------- всплывающий кадр: на какой камере и где именно эта зона ----------
// Наведение на источник сценария — кадр этой камеры с подсвеченной зоной; на строку сценария — кадры всех его источников.
function mdPeekFrame(camId) {
  const hit = MD.peekFrames[camId];
  if (hit && Date.now() - hit.t < 3000) return hit.p;
  const p = fetch('/api/motion/snapshot?camera=' + encodeURIComponent(camId) + '&w=640')
    .then((r) => (r.ok ? r.blob() : null)).then((b) => (b ? createImageBitmap(b) : null)).catch(() => null);
  MD.peekFrames[camId] = { t: Date.now(), p };
  return p;
}

function mdPeekCanvas(cam, bmp, hotIds, width) {
  const cv = document.createElement('canvas');
  cv.width = width;
  cv.height = Math.round(width * bmp.height / bmp.width);
  const g = cv.getContext('2d');
  g.drawImage(bmp, 0, 0, cv.width, cv.height);
  const fs = cam.frame_size || [bmp.width, bmp.height];
  const sx = cv.width / fs[0], sy = cv.height / fs[1];
  g.font = '600 12px sans-serif';
  cam.zones.forEach((z) => {
    if (!z.points || z.points.length < 3) return;
    const hot = hotIds.includes(z.id);
    g.beginPath();
    z.points.forEach(([x, y], i) => (i ? g.lineTo(x * sx, y * sy) : g.moveTo(x * sx, y * sy)));
    g.closePath();
    if (hot) { g.fillStyle = 'rgba(250,204,21,.32)'; g.fill(); }
    g.lineWidth = hot ? 3 : 1;
    g.strokeStyle = hot ? '#facc15' : 'rgba(255,255,255,.4)';
    g.stroke();
    if (hot) {
      const [x, y] = z.points[0];
      g.fillStyle = '#facc15';
      g.fillText(z.name, Math.max(4, Math.min(cv.width - 60, x * sx)), Math.max(14, y * sy - 5));
    }
  });
  return cv;
}

async function mdPeekShow(items, ev) {
  const box = mdEl('mdPeek');
  if (!box) return;
  const byCam = new Map();
  items.forEach(({ camera, zone }) => { byCam.set(camera, (byCam.get(camera) || []).concat(zone)); });
  const key = MD.peekKey;
  box.hidden = false;
  box.textContent = 'загружаю кадр…';
  mdPeekMove(ev);
  const parts = await Promise.all([...byCam].map(async ([camId, zones]) => {
    const cam = mvCam(camId);
    const bmp = cam ? await mdPeekFrame(camId) : null;
    return { cam, bmp, zones };
  }));
  if (MD.peekKey !== key) return;            // пока грузилось, мышь ушла
  box.textContent = '';
  box.classList.toggle('is-multi', parts.length > 1);
  parts.forEach(({ cam, bmp, zones }) => {
    if (!cam) return;
    const fig = document.createElement('div');
    const names = zones.filter(Boolean).map((id) => { const z = cam.zones.find((q) => q.id === id); return z ? z.name : id; }).join(', ');
    const cap = document.createElement('div');
    cap.textContent = cam.name + (mdIp(cam.url) ? ' (' + mdIp(cam.url) + ')' : '') + (names ? ' · ' + names : '');
    fig.appendChild(cap);
    if (bmp) fig.appendChild(mdPeekCanvas(cam, bmp, zones, parts.length > 1 ? 300 : 440));
    else { const na = document.createElement('div'); na.className = 'md-dim'; na.textContent = 'кадра нет — камера не подключена'; fig.appendChild(na); }
    box.appendChild(fig);
  });
  mdPeekMove(ev);
}

function mdPeekMove(ev) {
  const box = mdEl('mdPeek');
  if (!box || box.hidden || !ev) return;
  const pad = 16;
  const x = Math.min(ev.clientX + pad, window.innerWidth - box.offsetWidth - 8);
  const y = Math.min(ev.clientY + pad, window.innerHeight - box.offsetHeight - 8);
  box.style.left = Math.max(8, x) + 'px';
  box.style.top = Math.max(8, y) + 'px';
}

function mdPeekHide() {
  MD.peekKey = '';
  const box = mdEl('mdPeek');
  if (box) box.hidden = true;
}

function mdBindPeek(body) {
  const box = document.createElement('div');
  box.id = 'mdPeek';
  box.className = 'mv-peek';
  box.hidden = true;
  document.body.appendChild(box);
  const SEL = '[data-peek],[data-peek-scn],[data-peek-cam]';
  const target = (e) => (e.target.closest ? e.target.closest(SEL) : null);
  body?.addEventListener('mouseover', (e) => {
    const t = target(e);
    if (!t) return;
    let key, items;
    if (t.dataset.peek) {
      const [camera, zone] = t.dataset.peek.split('/');
      key = t.dataset.peek;
      items = [{ camera, zone }];
    } else if (t.dataset.peekCam) {
      const cam = mvCam(t.dataset.peekCam);
      key = 'cam' + t.dataset.peekCam;
      items = cam ? (cam.zones.length ? cam.zones.map((z) => ({ camera: cam.id, zone: z.id })) : [{ camera: cam.id, zone: '' }]) : [];
      if (!items.length) return;
    } else {
      const s = ((MV.cfg && MV.cfg.scenarios) || [])[Number(t.dataset.peekScn)];
      key = 'scn' + t.dataset.peekScn;
      items = s ? s.sources.flatMap((x) => {         // источник-группа — показываем её окна
        if (!x.group) return [x];
        const g = ((mvCam(x.camera) || {}).groups || []).find((q) => q.id === x.group);
        return g ? g.zones.map((z) => ({ camera: x.camera, zone: z })) : [];
      }) : [];
      if (!items.length) return;
    }
    if (key === MD.peekKey) return;
    MD.peekKey = key;
    mdPeekShow(items, e);
  });
  body?.addEventListener('mousemove', (e) => { if (MD.peekKey) mdPeekMove(e); });
  body?.addEventListener('mouseout', (e) => {
    const to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest(SEL) : null;
    if (!to || to !== target(e)) mdPeekHide();
  });
  body?.addEventListener('click', mdPeekHide);
}

// ---------- включение КЗ и запись CSV ----------
async function mdSetEnabled(serial, want, onFail) {
  const src = getSource(serial);
  const r = await mvJson('/api/motion/vision', { serial, enabled: want, name: src ? src.label : serial, url: (src && src.connection && src.connection.url) || '' });
  if (r && r.error) { alert('КЗ: ' + r.error); if (onFail) onFail(); return; }
  if (r && r.cameras) MV.cfg = r;
  log.info('КЗ: анализ движения ' + (want ? 'включён' : 'выключен'), { serial });
  MD.key = '';
  renderMotionDock();
  drawAllMotionTiles();
}

async function mdOnRec() {
  const { source } = mdCamera();
  if (!source) return;
  const st = MV.states[source.serial];
  const r = await mvJson('/api/motion/record', { camera: source.serial, on: !(st && st.recording) });
  if (r && r.error) alert('Запись CSV: ' + r.error);
}

// запись СРАЗУ по всем камерам с КЗ: «● все» включает у всех (если у части уже идёт — досоздаёт остальным), «■ все» гасит всех
async function mdOnRecAll() {
  const on = mdCfgCams().filter((c) => c.enabled);
  const rec = on.filter((c) => (MV.states[c.id] || {}).recording).length;
  const r = await mvJson('/api/motion/record_all', { on: rec < on.length });
  if (r && r.error) alert('Запись CSV: ' + r.error);
}

// все записи одним архивом (CSV по камерам + summary.txt); через fetch, чтобы ошибку («записей нет») показать, а не скачать пустышку
async function mdOnRecZip() {
  try {
    const r = await fetch('/api/motion/records.zip');
    if (!r.ok) { alert('Архив записей: ' + (await r.text())); return; }
    const m = /filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob());
    a.download = m ? m[1] : 'motion_records.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  } catch (e) { alert('Архив записей: ' + e); }
}

// ---------- окно разметки зоны ----------
async function mdLoadMarkFrame() {
  const m = MD.mark;
  if (!m || m.busy || m.pts.length) return;       // пока не поставлена первая точка, кадр живой; потом замирает
  m.busy = true;
  try {
    const r = await fetch('/api/motion/snapshot?camera=' + encodeURIComponent(m.cam) + '&w=1280&t=' + Date.now());
    if (!r.ok) { m.noFrame = true; mdEl('mmHint').textContent = 'Кадра ещё нет — камера не подключилась. Подождите или проверьте адрес потока.'; return; }
    if (m.noFrame) { m.noFrame = false; mdEl('mmHint').textContent = m.hint; }      // кадр пришёл — убрать устаревшее «кадра нет»
    m.fw = Number(r.headers.get('X-Frame-W')) || m.fw;
    m.fh = Number(r.headers.get('X-Frame-H')) || m.fh;
    const blob = await r.blob();
    const img = mdEl('mmImg');
    const old = img.src;
    img.src = URL.createObjectURL(blob);
    if (old.startsWith('blob:')) URL.revokeObjectURL(old);
  } catch (e) { /* ждём следующий кадр */ } finally { m.busy = false; }
}

// вкладки окна разметки: одна кнопка «Разметить», внутри — что размечаем
function mdMarkKindOf(kind) { return kind === true || kind === 'rods' ? 'rods' : kind === 'glass' || kind === 'juice' ? kind : 'zone'; }   // false/событие click → зона

function openMarkModal(kind) {
  const { source, cam } = mdCamera();
  if (!source || !cam || !cam.enabled) return;
  MD.mark = { cam: cam.id, fw: 0, fh: 0, busy: false };
  mdEl('mmCam').textContent = source.label || source.serial;
  mdEl('mmImg').removeAttribute('src');
  mdMarkSetKind(mdMarkKindOf(kind));
  const modal = mdEl('motionMarkModal');
  modal.classList.add('show');
  modal.setAttribute('aria-hidden', 'false');
  mdLoadMarkFrame();
  MD.markTimer = setInterval(mdLoadMarkFrame, 1000);
  mdMarkUi();
}

// переключить вид разметки (вкладка): точки и форма сбрасываются, кадр и камера остаются
function mdMarkSetKind(kind) {
  const rods = kind === 'rods', glass = kind === 'glass', juice = kind === 'juice';
  Object.assign(MD.mark, { kind, pts: [], cursor: null, closed: false, rods, glass, juice, polys: [] });   // тот же объект: загрузка кадра держит на него ссылку
  document.querySelectorAll('#mmTabs [data-mm-kind]').forEach((b) => b.classList.toggle('is-active', b.dataset.mmKind === kind));
  mdEl('mmTitle').textContent = rods ? 'Разметка валов' : glass ? 'Разметка стёкол' : juice ? 'Разметка стаканов' : 'Разметка зоны';
  mdEl('mmOk').textContent = rods ? 'Добавить валы' : glass ? 'Готово' : juice ? 'Добавить стаканы' : 'Добавить зону';
  mdEl('mmClose').textContent = 'Замкнуть (Enter)';
  mdEl('mmForm').hidden = true;
  mdEl('mmOk').hidden = true;
  mdEl('mmClose').hidden = rods || juice;
  mdEl('mmHint').textContent = juice
    ? 'Кликайте по центру каждого стакана по порядку: первый клик — стакан 1, и так далее. ПКМ — убрать последнюю точку. Радиус кружка — ползунком ниже (кружок должен лежать внутри стакана, на соке, без стенки). Enter — готово.'
    : rods
    ? 'Кликните по оси левого вала (красная точка в центре палки), затем по оси правого. ПКМ — убрать точку. Радиус окружности — ползунком ниже (она должна доставать до концов палки). Enter — готово.'
    : glass
      ? 'Обведите НИЖНЕЕ (рабочее) стекло: клики по углам внутри стекла. ПКМ — убрать точку. Enter — замкнуть (минимум 3 точки). Потом обведите верхнее; если стекло одно — «Готово».'
      : 'Кликайте по углам зоны. ПКМ — убрать точку. «Замкнуть» или Enter — готово (минимум 3 точки). Кадр замирает после первой точки.';
  MD.mark.hint = mdEl('mmHint').textContent;
  MD.mark.rodPct = juice ? 2 : 17;                 // стакан — маленький кружок (2 % ширины кадра: ролик 19.11, 1920 px — стакан ≈ 40 px), вал — большая окружность
  mdEl('mmRodR').min = juice ? 1 : 3;
  mdEl('mmRodR').step = juice ? 0.5 : 1;
  mdEl('mmRodR').value = MD.mark.rodPct; mdEl('mmRodRv').textContent = MD.mark.rodPct + ' %';
  mdEl('mmRodBox').hidden = !(rods || juice);
  mdMarkUi();
}

function closeMarkModal() {
  clearInterval(MD.markTimer);
  const modal = mdEl('motionMarkModal');
  modal.classList.remove('show');
  modal.setAttribute('aria-hidden', 'true');
  const old = mdEl('mmImg').src;
  if (old.startsWith('blob:')) URL.revokeObjectURL(old);
  MD.mark = null;
}

function mdMarkUi() {
  const m = MD.mark;
  if (!m) return;
  if (m.rods) {
    mdEl('mmOk').hidden = m.pts.length < 2;
    mdEl('mmUndo').disabled = !m.pts.length;
  } else if (m.juice) {
    mdEl('mmOk').hidden = !m.pts.length;
    mdEl('mmOk').textContent = m.pts.length ? 'Добавить стаканы: ' + m.pts.length : 'Добавить стаканы';
    mdEl('mmUndo').disabled = !m.pts.length;
  } else if (m.glass) {
    mdEl('mmClose').textContent = m.polys.length ? 'Замкнуть верхнее (Enter)' : 'Замкнуть нижнее (Enter)';
    mdEl('mmClose').disabled = m.pts.length < 3;
    mdEl('mmOk').hidden = !m.polys.length;
    mdEl('mmUndo').disabled = !m.pts.length && !m.polys.length;
  } else {
    mdEl('mmClose').disabled = m.closed || m.pts.length < 3;
    mdEl('mmUndo').disabled = m.closed || !m.pts.length;
  }
  mdDrawMark();
}

// радиус поиска палки по умолчанию, пикселей кадра разметки: 17 % ширины (на 1920 — 326; достаёт до концов палки у ступицы)
const mdRodRadius = (fs, pct) => Math.round(fs[0] * (pct || 17) / 100);

function mdDrawMark() {
  const m = MD.mark, img = mdEl('mmImg'), cv = mdEl('mmCanvas');
  if (!m || !img.clientWidth) return;
  const w = img.clientWidth, h = img.clientHeight;
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const g = cv.getContext('2d');
  g.clearRect(0, 0, w, h);
  mdPaintMark(g, 1);
  mdDrawLoupe();
}

// метки разметки (зоны-ориентиры, точки, окружности) в координатах отображаемого кадра; u — толщина «единицы» (1/увеличение в лупе)
function mdPaintMark(g, u) {
  const m = MD.mark, img = mdEl('mmImg');
  const w = img.clientWidth, h = img.clientHeight;
  const cam = mvCam(m.cam);
  const fw = m.fw || img.naturalWidth, fh = m.fh || img.naturalHeight;
  if (!fw || !fh) return;
  const sx = w / fw, sy = h / fh;
  const rs = (cam && cam.frame_size) || [fw, fh];
  g.lineWidth = 2 * u; g.font = 'bold ' + 14 * u + 'px sans-serif';
  ((cam && cam.zones) || []).forEach((z) => {      // уже размеченные зоны — для ориентира
    g.strokeStyle = '#9ca3af'; g.fillStyle = 'rgba(156,163,175,.14)';
    g.beginPath();
    if (z.type === 'cycle' || z.type === 'clarity') {
      g.arc(z.center[0] * (fw / rs[0]) * sx, z.center[1] * (fh / rs[1]) * sy, z.radius * (fw / rs[0]) * sx, 0, 7); g.stroke();
      return;
    }
    z.points.forEach((p, i) => { const x = p[0] * (fw / rs[0]) * sx, y = p[1] * (fh / rs[1]) * sy; if (i) g.lineTo(x, y); else g.moveTo(x, y); });
    g.closePath(); g.fill(); g.stroke();
  });
  if (m.juice) {                                   // стаканы: номер и кружок выбранного радиуса у каждой точки
    const n0 = jcNextNumber(cam);
    g.strokeStyle = '#34d399'; g.fillStyle = '#34d399'; g.textAlign = 'center';
    m.pts.forEach((p, i) => {
      const x = p[0] * sx, y = p[1] * sy;
      g.beginPath(); g.arc(x, y, 3 * u, 0, 7); g.fill();
      g.beginPath(); g.arc(x, y, mdRodRadius(rs, m.rodPct) * (fw / rs[0]) * sx, 0, 7); g.stroke();
      g.fillText(String(n0 + i), x, y - mdRodRadius(rs, m.rodPct) * (fw / rs[0]) * sx - 5 * u);
    });
    g.textAlign = 'left';
    return;
  }
  if (m.rods) {                                    // валы: точка оси и окружность поиска стандартного радиуса
    g.strokeStyle = '#38bdf8'; g.fillStyle = '#38bdf8';
    m.pts.forEach((p) => {
      g.beginPath(); g.arc(p[0] * sx, p[1] * sy, 4 * u, 0, 7); g.fill();
      g.beginPath(); g.arc(p[0] * sx, p[1] * sy, mdRodRadius(rs, m.rodPct) * (fw / rs[0]) * sx, 0, 7); g.stroke();
    });
    return;
  }
  (m.polys || []).forEach((poly, k) => {              // стёкла, которые уже обведены: нижнее — голубое, верхнее — жёлтое
    g.strokeStyle = k ? '#fbbf24' : '#38bdf8'; g.fillStyle = k ? 'rgba(251,191,36,.18)' : 'rgba(56,189,248,.18)';
    g.beginPath();
    poly.forEach((p, i) => (i ? g.lineTo(p[0] * sx, p[1] * sy) : g.moveTo(p[0] * sx, p[1] * sy)));
    g.closePath(); g.fill(); g.stroke();
  });
  g.strokeStyle = m.glass && m.polys.length ? '#fbbf24' : '#38bdf8'; g.fillStyle = g.strokeStyle;
  g.beginPath();
  m.pts.forEach((p, i) => (i ? g.lineTo(p[0] * sx, p[1] * sy) : g.moveTo(p[0] * sx, p[1] * sy)));
  if (m.closed) g.closePath();
  else if (m.cursor) g.lineTo(m.cursor[0] * sx, m.cursor[1] * sy);
  g.stroke();
  m.pts.forEach((p) => { g.beginPath(); g.arc(p[0] * sx, p[1] * sy, 4 * u, 0, 7); g.fill(); });
}

// лупа справа сверху: кусок кадра вокруг курсора в MD_LOUPE_K раз крупнее, с метками и прицелом — чтобы ставить точки точно
const MD_LOUPE = 220, MD_LOUPE_K = 4;
function mdDrawLoupe() {
  const m = MD.mark, lp = mdEl('mmLoupe'), img = mdEl('mmImg');
  if (!lp) return;
  if (!m || m.closed || !m.mouse || !img.clientWidth || !img.naturalWidth) { lp.hidden = true; return; }
  const w = img.clientWidth, h = img.clientHeight;
  const fw = m.fw || img.naturalWidth, fh = m.fh || img.naturalHeight;
  const sx = w / fw, sy = h / fh;
  const cx = m.mouse[0] * sx, cy = m.mouse[1] * sy;            // центр лупы в отображаемых пикселях
  const dpr = window.devicePixelRatio || 1, L = Math.round(MD_LOUPE * dpr);
  if (lp.width !== L) { lp.width = L; lp.height = L; }
  lp.hidden = false;
  lp.classList.toggle('left', cx > w - MD_LOUPE - 40 && cy < MD_LOUPE + 40);      // курсор подошёл к лупе — она уходит влево
  const g = lp.getContext('2d');
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.fillStyle = '#000'; g.fillRect(0, 0, L, L);
  const hx = MD_LOUPE / (2 * MD_LOUPE_K) / sx, hy = MD_LOUPE / (2 * MD_LOUPE_K) / sy;   // полуразмер области в пикселях кадра
  const kx = img.naturalWidth / fw, ky = img.naturalHeight / fh;
  g.imageSmoothingEnabled = true;
  g.drawImage(img, (m.mouse[0] - hx) * kx, (m.mouse[1] - hy) * ky, 2 * hx * kx, 2 * hy * ky, 0, 0, L, L);
  const k = MD_LOUPE_K * dpr;                                   // метки: из отображаемых пикселей в пиксели лупы
  g.setTransform(k, 0, 0, k, L / 2 - cx * k, L / 2 - cy * k);
  mdPaintMark(g, 1 / MD_LOUPE_K);
  g.setTransform(1, 0, 0, 1, 0, 0);                             // прицел по центру: тёмная подложка + белая линия, с зазором вокруг точки
  const c = L / 2, gap = 7 * dpr, len = 22 * dpr;
  [['#000', 3 * dpr], ['#fff', 1.2 * dpr]].forEach(([col, lw]) => {
    g.strokeStyle = col; g.lineWidth = lw; g.beginPath();
    g.moveTo(c - gap - len, c); g.lineTo(c - gap, c); g.moveTo(c + gap, c); g.lineTo(c + gap + len, c);
    g.moveTo(c, c - gap - len); g.lineTo(c, c - gap); g.moveTo(c, c + gap); g.lineTo(c, c + gap + len);
    g.stroke();
  });
}

function mdMarkPoint(event) {
  const m = MD.mark, cv = mdEl('mmCanvas'), r = cv.getBoundingClientRect();
  const fw = m.fw || mdEl('mmImg').naturalWidth, fh = m.fh || mdEl('mmImg').naturalHeight;
  return [Math.max(0, Math.min(fw, Math.round((event.clientX - r.left) / r.width * fw))), Math.max(0, Math.min(fh, Math.round((event.clientY - r.top) / r.height * fh)))];
}

// убрать последнюю точку; у стёкол, когда точек нет, — вернуться к правке ранее замкнутого полигона
function mdMarkUndo() {
  const m = MD.mark;
  if (!m || m.closed) return;
  if (m.glass && !m.pts.length && m.polys.length) m.pts = m.polys.pop();
  else m.pts.pop();
  mdMarkUi();
}

function mdMarkClose() {
  const m = MD.mark;
  if (m && m.glass) {                                  // стёкла: замкнули полигон — следующий; второй замкнут — сохраняем
    if (m.pts.length < 3) return;
    m.polys.push(m.pts);
    m.pts = [];
    m.cursor = null;
    if (m.polys.length >= 2) { mdMarkSave(); return; }
    mdEl('mmHint').textContent = 'Нижнее готово. Теперь обведите ВЕРХНЕЕ стекло (жёлтое) или нажмите «Готово», если стекло одно.';
    mdMarkUi();
    return;
  }
  if (!m || m.closed || m.pts.length < 3) return;
  m.closed = true;
  m.cursor = null;
  mdEl('mmForm').hidden = false;
  mdEl('mmOk').hidden = false;
  mdEl('mmClose').hidden = true;
  mdEl('mmName').focus();
  mdMarkUi();
}

// два вала: две зоны-цикла и группа mirror (синхронность); центры — кликнутые точки в пикселях кадра разметки
function mdMarkSaveRods(m, cam, fw, fh) {
  if (m.pts.length < 2) return;
  const pts = m.pts.slice(0, 2).sort((p, q) => p[0] - q[0]);          // левый вал — первый
  mdEdit(() => {
    const fs = cam.frame_size || [fw, fh];
    cam.frame_size = fs;
    const free = (pre) => { let n = 1; while (cam.zones.some((z) => z.id === pre + n) || (cam.groups || []).some((q) => q.id === pre + n)) n += 1; return pre + n; };
    const ids = [free('rod'), null];
    const mk = (id, name, p) => ({ id, name, enabled: true, type: 'cycle', metric: 'flow', points: [], on: 0, off: 0, invert: false, track_flow: false, smooth: 3,
      frames_to_on: 1, frames_to_off: 1, blur: 0, flicker_comp: false, out: null,
      center: [Math.round(p[0] * fs[0] / fw), Math.round(p[1] * fs[1] / fh)], radius: mdRodRadius(fs, m.rodPct) });
    cam.zones.push(mk(ids[0], 'Вал левый', pts[0]));
    ids[1] = free('rod');
    cam.zones.push(mk(ids[1], 'Вал правый', pts[1]));
    cam.groups = cam.groups || [];
    cam.groups.push({ id: free('sync'), name: 'Синхронность валов', type: 'mirror', zones: ids, mode: 'mirror', ref: 0, tol: 10, frames_to_on: 3, frames_to_off: 5, out: null });
  }, true);
  log.success('КЗ: валы добавлены', { serial: cam.id });
  closeMarkModal();
}

function mdMarkSave() {
  const m = MD.mark, cam = m && mvCam(m.cam);
  if (!cam) return;
  const fw = m.fw || mdEl('mmImg').naturalWidth, fh = m.fh || mdEl('mmImg').naturalHeight;
  if (m.rods) { mdMarkSaveRods(m, cam, fw, fh); return; }
  if (m.juice) { jcMarkSave(m, cam, fw, fh); return; }
  if (m.glass) { lvMarkSave(m, cam, fw, fh); return; }
  const metric = mdEl('mmMetric').value, type = mdEl('mmType').value;
  const name = mdEl('mmName').value.trim();
  const pts = m.pts.slice();
  mdEdit(() => {
    const fs = cam.frame_size || [fw, fh];
    cam.frame_size = fs;
    let n = 1;
    while (cam.zones.some((z) => z.id === 'z' + n)) n += 1;
    cam.zones.push({
      id: 'z' + n, name: name || 'Зона ' + n, enabled: true, type, metric,
      points: pts.map((p) => [Math.round(p[0] * fs[0] / fw), Math.round(p[1] * fs[1] / fh)]),
      on: MD_THR[metric][0], off: MD_THR[metric][1], invert: type === 'presence', smooth: 5, frames_to_on: 3, frames_to_off: 4, blur: 5, flicker_comp: true, out: null,
    });
  }, true);
  log.success('КЗ: зона добавлена', { serial: cam.id });
  closeMarkModal();
}

// ---------- инициализация ----------
function initMotionDock() {
  const tip = document.createElement('div');
  tip.id = 'mvTip';
  tip.className = 'mv-tip';
  tip.hidden = true;
  document.body.appendChild(tip);

  mdEl('mdSwitch')?.addEventListener('change', (e) => {
    const { source } = mdCamera();
    if (source) mdSetEnabled(source.serial, e.target.checked, () => { e.target.checked = !e.target.checked; });
  });
  mdEl('mdAll')?.addEventListener('click', () => setFocus(-1));   // общий вид: фокус снимаем
  mdEl('mdMark')?.addEventListener('click', () => openMarkModal('zone'));
  mdEl('mdLead')?.addEventListener('click', mdOnLead);
  mdEl('mmTabs')?.addEventListener('click', (e) => {
    const b = e.target.closest('[data-mm-kind]');
    if (b && MD.mark && MD.mark.kind !== b.dataset.mmKind) mdMarkSetKind(b.dataset.mmKind);
  });
  mdEl('mdRec')?.addEventListener('click', mdOnRec);
  mdEl('mdRecAll')?.addEventListener('click', mdOnRecAll);
  mdEl('mdRecZip')?.addEventListener('click', mdOnRecZip);
  mdEl('mdPulse')?.addEventListener('click', mdOnPulse);
  document.querySelectorAll('[data-md-tab]').forEach((b) => b.addEventListener('click', () => {
    MD.tab = b.dataset.mdTab;
    MD.key = '';
    renderMotionDock();
  }));

  const body = mdEl('mdBody');
  mdBindPeek(body);
  const dispatch = (e) => {
    const t = e.target;
    const kind = t.dataset && t.dataset.md;
    if (!kind) return;
    if (kind === 'z') { const cam = mvCam(t.dataset.cam); if (cam) mdOnZone(t, cam); }
    else if (kind === 'g') mdOnGroup(t);
    else if (kind === 'vis') mdOnVis(t);
    else if (kind === 'mg') { const cam = mvCam(t.dataset.cam); if (cam) mdOnMirror(t, cam); }
    else if (kind === 'c') { const cam = mvCam(t.dataset.cam); if (cam) mdOnCamera(t, cam); }
    else if (kind === 'p') mdOnPlc(t);
    else if (kind === 's') mdOnScenario(t);
    else if (kind === 'cm') mdOnCamRow(t);
    else if (kind === 'lv') mdOnLevel(t);
    else if (kind === 'sh') mdOnShafts(t);
    else if (kind === 'jc') jcOnEvent(t);
    else if (kind === 'shloss') mdOnShLoss(t);
    else if (kind === 'shac') mdOnShAc(t);
    else if (kind === 'shpick') mdOnShPick(t);
    else if (kind === 'cs') mdSetEnabled(t.dataset.serial, t.checked, () => { t.checked = !t.checked; });
  };
  body?.addEventListener('change', (e) => { if (e.target.tagName !== 'BUTTON') dispatch(e); });
  body?.addEventListener('input', (e) => { if (e.target.dataset && (e.target.dataset.f === 'hsv' || e.target.dataset.f === 'hsvx' || e.target.dataset.f === 'hsvn') && typeof shOnSlide === 'function') shOnSlide(e.target); });
  body?.addEventListener('click', (e) => { if (e.target.tagName === 'BUTTON' && e.target.dataset.md) dispatch(e); });
  body?.addEventListener('change', (e) => {
    if (e.target.id === 'geType') mdSyncGroupEditor();
    // другая камера в редакторе группы — перечитываем её зоны, не трогая остальные поля
    if (e.target.id === 'geCam' && MD.editGrp) {
      MD.editGrp.cam = e.target.value;
      const cam = mvCam(e.target.value);
      if (cam) mdEl('geZones').innerHTML = mdGeZonesHtml(cam, { zones: [] });
    }
  });
  body?.addEventListener('click', (e) => {          // строка окна: нажали на шапку — раскрыли настройки
    const h = e.target.closest && e.target.closest('[data-zt]');
    if (!h || e.target.closest('label, input, select, button, a')) return;
    const row = h.parentElement, key = h.dataset.zt;
    const open = row.classList.toggle('is-open');
    if (open) MD.open[key] = true; else delete MD.open[key];
    if (open) mdDrawTrends();
  });
  body?.addEventListener('change', (e) => {          // «подробно» — раскрыть / свернуть все строки
    if (e.target.id !== 'mdOpenAll') return;
    MD.openAll = e.target.checked;
    try { localStorage.setItem(MD_OPEN_KEY, MD.openAll ? '1' : '0'); } catch (err) { /* не критично */ }
    MD.open = {};
    body.querySelectorAll('.md-zr').forEach((r) => r.classList.toggle('is-open', MD.openAll));
  });
  body?.addEventListener('wheel', mdOnWheel, { passive: false });
  body?.addEventListener('mousemove', mdOnChartHover);
  body?.addEventListener('mouseleave', () => { const [tip, line] = mdTipEls(); tip.hidden = true; line.hidden = true; });
  body?.addEventListener('change', (e) => {
    if (e.target.id !== 'mdStatWin') return;
    MD.statWin = Number(e.target.value);
    MD.stats = {};
    try { localStorage.setItem(MD_STAT_KEY, String(MD.statWin)); } catch (err) { /* не критично */ }
    mdPaintZoneStats();
    mdLoadStats(true);
  });

  // окно разметки
  const cv = mdEl('mmCanvas');
  cv?.addEventListener('click', (e) => {
    const m = MD.mark;
    if (!m || m.closed || (m.rods && m.pts.length >= 2)) return;
    m.pts.push(mdMarkPoint(e));
    mdMarkUi();
  });
  cv?.addEventListener('mousemove', (e) => { if (MD.mark && !MD.mark.closed) { MD.mark.cursor = MD.mark.mouse = mdMarkPoint(e); mdDrawMark(); } });
  cv?.addEventListener('mouseleave', () => { if (MD.mark) MD.mark.mouse = null; mdDrawLoupe(); });
  cv?.addEventListener('contextmenu', (e) => { e.preventDefault(); mdMarkUndo(); });
  mdEl('mmImg')?.addEventListener('load', mdDrawMark);
  mdEl('mmUndo')?.addEventListener('click', mdMarkUndo);
  mdEl('mmRodR')?.addEventListener('input', (e) => {
    if (!MD.mark) return;
    MD.mark.rodPct = Number(e.target.value) || 17;
    mdEl('mmRodRv').textContent = MD.mark.rodPct + ' %';
    mdDrawMark();
  });
  mdEl('mmCancel')?.addEventListener('click', closeMarkModal);
  mdEl('mmX')?.addEventListener('click', closeMarkModal);
  mdEl('mmClose')?.addEventListener('click', mdMarkClose);
  mdEl('mmOk')?.addEventListener('click', mdMarkSave);
  document.addEventListener('keydown', (e) => {
    if (!MD.mark) return;
    if (e.key === 'Escape') closeMarkModal();
    else if (MD.mark.glass) { if (e.key === 'Enter') { if (MD.mark.pts.length >= 3) mdMarkClose(); else if (MD.mark.polys.length) mdMarkSave(); } }
    else if (MD.mark.rods) { if (e.key === 'Enter' && MD.mark.pts.length >= 2) mdMarkSave(); }
    else if (MD.mark.juice) { if (e.key === 'Enter' && MD.mark.pts.length) mdMarkSave(); }
    else if (e.key === 'Enter' && !MD.mark.closed && !/INPUT|SELECT|TEXTAREA/.test((e.target || {}).tagName || '')) mdMarkClose();
    else if (e.key === 'Enter' && MD.mark.closed) mdMarkSave();
  });
  window.addEventListener('resize', mdDrawMark);
  window.addEventListener('beforeunload', (e) => { if (MV.dirty) { e.preventDefault(); e.returnValue = ''; } });
}

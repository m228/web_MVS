// ---------- КЗ · вкладка «Уровень»: уровень жидкости по YOLO-модели и зонам-стёклам (декантатор) ----------
// Модель живёт в сервисе уровня (level_service); стёкла — два полигона на кадре (нижнее, верхнее). Уровень каждого стекла — доля его
// площади, залитая маской жидкости; Total = (нижнее + верхнее) · 50 %, в ПЛК уходит ×10 (54,3 % → 543). В ПЛК пишет сценарий
// «Уровень · камера» со своей связью, выключенной по умолчанию. Серверная часть — motion_service/motion_level.py.
const LV_DEF = { conf: 0.35, imgsz: 0, geometry: 'lvldec', infer_interval_s: 2, alpha: 0.93, warmup_s: 10, glass1_min: 15, glass2_min: 20,
  glass1_full: 100, glass2_full: 100, hold_s: 30, contour: false, liquid_class: 'liquid' };
// [мин, макс] числовых параметров группы (как проверяет сервер)
const LV_NUM = { conf: [0.01, 0.99], imgsz: [0, 4096], infer_interval_s: [0.2, 600], alpha: [0, 0.999], warmup_s: [0, 600],
  glass1_min: [0, 100], glass2_min: [0, 100], glass1_full: [0, 100], glass2_full: [0, 100], hold_s: [1, 3600] };
const LV = { svc: null, models: null, at: 0, busy: false, msg: '', msgCls: '' };

const lvGroup = (cam) => ((cam && cam.groups) || []).find((g) => g.type === 'level') || null;
const lvP = (g, k) => (g && g[k] !== undefined ? g[k] : LV_DEF[k]);
const lvScenId = (cam) => 'lvl_' + cam.id;
const lvScenario = (cam) => ((MV.cfg && MV.cfg.scenarios) || []).find((s) => s.id === lvScenId(cam)) || null;
const lvFmt = (v) => Number(v).toFixed(1).replace('.', ',');

// состояние группы-уровня камеры из опроса сервиса (null — нет данных)
function lvLive(cam) {
  const g = lvGroup(cam);
  const res = (MV.states[cam.id] || {}).result;
  return g && res ? (res.groups || []).find((x) => x.id === g.id) || null : null;
}

// ---------- сервис уровня: онлайн ли, какая модель, какие файлы ----------
async function lvRefresh(force) {
  if (LV.busy || (!force && Date.now() - LV.at < 5000)) return;
  LV.busy = true;
  try {
    const [h, m] = await Promise.all([mvApi('/api/level/health'), mvApi('/api/level/models')]);
    LV.svc = h && !h.error ? h : { online: false };
    LV.models = m && !m.error ? m : null;
    LV.at = Date.now();
  } finally { LV.busy = false; }
  if (MD.tab === 'level') lvPaintModel();
}

function lvMsg(text, cls) {
  LV.msg = text;
  LV.msgCls = cls || '';
  const el = mdEl('lvMsg');
  if (el) { el.textContent = text; el.className = 'lv-msg ' + (cls || ''); }
}

function lvPaintModel() {
  const cap = mdEl('lvCap');
  const s = LV.svc;
  const m = s && s.online && s.service ? s.service.model : null;
  if (cap) {
    if (!s) setCapLine(cap, true, 'Проверяю сервис уровня…', '');
    else if (!s.online) setCapLine(cap, false, '', 'Сервис уровня не запущен (level_service\\setup.bat, потом перезапуск web_MVS)');
    else if (!m) setCapLine(cap, false, '', 'Сервис уровня работает, модель не загружена — загрузите .pt');
    else setCapLine(cap, true, 'Модель ' + m.name + ' · ' + m.classes.join(', ') + ' · ' + m.device, '');
  }
  const sel = mdEl('lvModelSel');
  if (sel) {
    const files = (LV.models && LV.models.files) || [];
    const cur = (LV.models && LV.models.current) || '';
    const sig = files.join('|') + '#' + cur;
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.innerHTML = files.length ? mdSel(files.map((f) => [escapeHtml(f), escapeHtml(f)]), escapeHtml(cur)) : '<option value="">— нет файлов —</option>';
    }
    sel.disabled = !files.length;
  }
}

// ---------- вкладка ----------
function mdLevelTabHtml(mode, cam) {
  if (mode === 'all') {
    const rows = mdCfgCams().filter((c) => c.enabled && lvGroup(c)).map((c) =>
      `<div class="md-card"><b>${escapeHtml(c.name)}</b>${mdIpTag(c.url)}<span class="lv-all" id="lv-all-${escapeHtml(c.id)}">–</span></div>`).join('');
    return '<p class="net-hint">Выберите ячейку с RTSP-камерой — здесь настраивается уровень жидкости (декантатор).</p>' + rows;
  }
  if (!(cam && cam.enabled)) {
    return '<p class="net-hint">Включите КЗ переключателем справа сверху — для этой камеры появится уровень жидкости.</p>';
  }
  const g = lvGroup(cam);
  const sc = lvScenario(cam);
  const m = (sc && sc.modbus) || { enable: false, host: '', port: 502, device_id: 1 };
  const a = 'data-md="lv"';
  const num = (f, v, min, max, step) => `<input type="number" ${a} data-f="${f}" value="${escapeHtml(v)}" min="${min}" max="${max}" step="${step || 'any'}" />`;
  // пары рядом: нижнее | верхнее, остальное — по смыслу (модель, сглаживание, маска, запас)
  const par = g ? `<div class="lv-par"><div class="md-h">Параметры</div><div class="md-fields md-fields--c">
      ${mdField('Уверенность модели', num('conf', lvP(g, 'conf'), 0.01, 0.99, 0.01), 'Порог уверенности 0…1: объекты слабее отбрасываются')}
      ${mdField('Опрос модели, с', num('infer_interval_s', lvP(g, 'infer_interval_s'), 0.2, 600, 0.1), 'Как часто кадр отправляется на распознавание')}
      ${mdField('Сглаживание', num('alpha', lvP(g, 'alpha'), 0, 0.999, 0.01), 'EMA: ближе к 1 — плавнее и инертнее (0 — без сглаживания)')}
      ${mdField('Прогрев, с', num('warmup_s', lvP(g, 'warmup_s'), 0, 600, 1), 'Первые секунды после старта без сглаживания: значение быстро набирает реальное')}
      ${mdField('Осадок нижнее, %', num('glass1_min', lvP(g, 'glass1_min'), 0, 100, 1), 'Ниже этого заполнения нижнее стекло считается пустым (осадок, налёт). Держите низким — это рабочий диапазон')}
      ${mdField('Осадок верхнее, %', num('glass2_min', lvP(g, 'glass2_min'), 0, 100, 1), 'Ниже этого заполнения верхнее стекло считается пустым: не форсит нижнее в 100 %')}
      ${mdField('Полное нижнее, %', num('glass1_full', lvP(g, 'glass1_full'), 0, 100, 1), 'Сырой уровень, который модель даёт при ПОЛНОМ стекле: растягивается до 100 %. 100 — без калибровки')}
      ${mdField('Полное верхнее, %', num('glass2_full', lvP(g, 'glass2_full'), 0, 100, 1), 'То же для верхнего стекла (на заводе LVLDec стояло 84)')}
      ${mdField('Геометрия маски', `<select ${a} data-f="geometry">${mdSel([['lvldec', 'как в LVLDec'], ['exact', 'точная']], lvP(g, 'geometry'))}</select>`,
        'Как в LVLDec — цифры совпадают с заводскими (маска растянута вместе с полями letterbox, края съезжают к центру). Точная — без растяжения; тогда «Полное» подберите заново')}
      ${mdField('Класс жидкости', `<input type="text" ${a} data-f="liquid_class" value="${escapeHtml(lvP(g, 'liquid_class'))}" />`, 'Имя класса модели, которое считается жидкостью (у моделей LVLDec — liquid)')}
      ${mdField('Размер входа', num('imgsz', lvP(g, 'imgsz'), 0, 4096, 32), '0 — тот, на котором обучена модель')}
      ${mdField('Держать без модели, с', num('hold_s', lvP(g, 'hold_s'), 1, 3600, 1), 'Сколько держать последний уровень, если модель молчит; потом — ноль')}
    </div></div>` : '';
  // запись в ПЛК — карточка справа от параметров; квадратик справа в шапке включает запись
  const plc = g ? `<div class="lv-plc2${m.enable ? ' is-on' : ''}">
    <div class="lv-plc2__head"><b>Запись в ПЛК</b><span class="md-dim" id="lvPlcInfo"></span>
      <label class="lv-sq" title="Писать Total ×10 в регистр контроллера. Выключено — в ПЛК ничего не уходит"><input type="checkbox" ${a} data-f="p_en"${m.enable ? ' checked' : ''} /><span></span></label></div>
    <div class="md-fields md-fields--c lv-plc2__body">
      ${mdField('IP контроллера', `<input type="text" ${a} data-f="p_host" value="${escapeHtml(m.host)}" placeholder="192.168.0.10" />`)}
      ${mdField('Порт', num('p_port', m.port, 1, 65535, 1))}
      ${mdField('Unit ID', num('p_dev', m.device_id, 0, 255, 1))}
      ${mdField('Регистр Total ×10', num('p_addr', sc && sc.out ? sc.out.address : 0, 0, 65535, 1), 'Целое 0…1000: 54,3 % → 543 (SCADA делит на 10)')}
    </div></div>` : '';
  const names = g ? g.zones.map((id) => { const z = cam.zones.find((q) => q.id === id); return z ? z.name : id; }) : [];
  const glasses = g ? `<div class="lv-glasses">${names.map((n, i) =>
    `<span class="lv-glass"><i style="background:${i ? LV_COL.up : LV_COL.low}"></i>${escapeHtml(n)}<button type="button" class="btn-secondary md-x" ${a} data-f="del_glass" data-gi="${i}" title="${i ? 'Удалить верхнее стекло' : 'Удалить стёкла: без нижнего (рабочего) уровень не считается'}">✕</button></span>`).join('')}${names.length < 2 ? '<span class="lv-glass lv-glass--empty">верхнего стекла нет</span>' : ''}</div>` : '';
  return `<div class="lv">
    <p class="cap-line" id="lvCap" data-state="unknown"><span class="cap-dot"></span><span class="cap-text">Проверяю сервис уровня…</span></p>
    <div class="lv-row">
      <select id="lvModelSel" ${a} data-f="model" title="Модель из level_service\\model — выбор загружает её сразу"></select>
      <button type="button" class="btn-secondary md-x" ${a} data-f="pick" title="Залить .pt или .onnx с этого компьютера: сервис сохранит и горячо подхватит без перезапуска">Загрузить модель…</button>
      <input type="file" id="lvFile" accept=".pt,.onnx" hidden ${a} data-f="upload" />
    </div>
    <div class="lv-now"><b id="lvTotal">–</b><span class="lv-unit">%</span><span class="md-dim" id="lvParts"></span><span class="md-dim" id="lvMeta"></span></div>
    <div class="lv-row">
      ${glasses}
      <button type="button" class="btn-primary md-x" ${a} data-f="mark" title="Обведите на кадре нижнее (рабочее) стекло, потом верхнее">${g ? 'Разметить заново' : 'Разметить стёкла'}</button>
      ${g ? `<label class="micro-check" title="Тонкий контур найденной жидкости на плитке камеры"><input type="checkbox" ${a} data-f="contour"${lvP(g, 'contour') ? ' checked' : ''} /><span class="micro-check__text">контур жидкости</span></label>` : ''}
      <span class="lv-msg ${LV.msgCls}" id="lvMsg">${escapeHtml(LV.msg)}</span>
    </div>
    ${g ? '' : '<p class="net-hint">Стёкол ещё нет. «Разметить стёкла» — обвести на кадре нижнее и верхнее.</p>'}
    ${g ? `<div class="lv-cols">${par}${plc}</div>` : ''}
  </div>`;
}

// живые значения: вызывается из paintMotionDockLive раз в секунду
function mdLevelPaintLive() {
  lvRefresh(false);
  const { cam } = mdCamera();
  if (!cam) {                                          // общий вид: по строке на камеру
    mdCfgCams().filter((c) => c.enabled && lvGroup(c)).forEach((c) => {
      const el = mdEl('lv-all-' + c.id), lg = lvLive(c);
      if (el) el.textContent = lg && lg.valid ? 'Total ' + lvFmt(lg.total) + ' %  ·  ' + lvFmt(lg.g1) + ' + ' + lvFmt(lg.g2) : (lg && lg.err) || 'нет данных';
    });
    return;
  }
  const lg = lvLive(cam);
  const tot = mdEl('lvTotal');
  if (!tot) return;
  tot.textContent = lg && lg.valid ? lvFmt(lg.total) : '–';
  tot.classList.toggle('is-off', !(lg && lg.valid));
  const parts = mdEl('lvParts');
  if (parts) parts.textContent = lg && lg.valid ? 'нижнее ' + lvFmt(lg.g1) + ' + верхнее ' + lvFmt(lg.g2) : '';
  const meta = mdEl('lvMeta');
  if (meta) {
    meta.textContent = !lvGroup(cam) ? '' : !lg ? 'ждём данных' : lg.valid
      ? (lg.warmup ? 'прогрев · ' : '') + lg.ms + ' мс · уверенность ' + lg.conf.toFixed(2)
      : (lg.err || 'ждём первого ответа модели');
  }
  const info = mdEl('lvPlcInfo');
  if (info) {
    const sc = (MV.scen || []).find((q) => q.id === lvScenId(cam));
    info.textContent = !sc ? '' : !sc.plc ? 'выключена' : sc.ok === null ? 'ждёт ответа ПЛК' : sc.ok ? 'записано: ' + sc.value : 'ошибка: ' + (sc.error || '');
  }
}

// ---------- действия ----------
function lvEnsureScenario(cam, g) {
  const list = (MV.cfg.scenarios = MV.cfg.scenarios || []);
  let sc = list.find((s) => s.id === lvScenId(cam));
  if (!sc) {
    sc = { id: lvScenId(cam), name: 'Уровень · ' + cam.name, rule: 'sum', signal: 'move', sources: [{ camera: cam.id, group: g.id }],
      out: { kind: 'holding', address: 0 }, modbus: { enable: false, host: '', port: 502, device_id: 1, write_period_s: 1 } };
    list.push(sc);
  }
  if (!sc.sources.some((x) => x.camera === cam.id && x.group === g.id)) sc.sources = [{ camera: cam.id, group: g.id }];   // группу удаляли и разметили заново
  return sc;
}

// стёкла (1–2 полигона в пикселях кадра разметки) → зоны type=level, группа-уровень и сценарий записи; размеченное прежде заменяется
function lvSetZones(cam, polys) {
  cam.zones = cam.zones || [];
  cam.groups = cam.groups || [];
  const free = (pre) => { let n = 0, id = pre; while (cam.zones.some((z) => z.id === id) || cam.groups.some((q) => q.id === id)) { n += 1; id = pre + n; } return id; };
  let g = lvGroup(cam);
  if (!g) { g = { id: free('lvl'), name: 'Уровень', type: 'level', zones: [], out: null }; cam.groups.push(g); }
  g.zones.slice(polys.length).forEach((id) => { cam.zones = cam.zones.filter((z) => z.id !== id); });   // верхнее стекло убрали
  const names = ['Нижнее стекло', 'Верхнее стекло'];
  g.zones = polys.map((pts, i) => {
    let z = g.zones[i] && cam.zones.find((q) => q.id === g.zones[i]);
    if (!z) {
      z = { id: free(i ? 'glass_up' : 'glass_low'), name: names[i], enabled: true, type: 'level', metric: 'flow', points: [], on: 0, off: 0, invert: false,
        track_flow: false, smooth: 1, frames_to_on: 1, frames_to_off: 1, blur: 0, flicker_comp: false, out: null };
      cam.zones.push(z);
    }
    z.points = pts;
    return z.id;
  });
  lvEnsureScenario(cam, g);
  return g;
}

// из окна разметки (11-motion-dock.js): m.polys — замкнутые полигоны в пикселях показанного кадра fw×fh
function lvMarkSave(m, cam, fw, fh) {
  const polys = m.polys.slice();
  if (m.pts.length >= 3 && polys.length < 2) polys.push(m.pts.slice());
  if (!polys.length) return;
  mdEdit(() => {
    const fs = cam.frame_size || [fw, fh];
    cam.frame_size = fs;
    lvSetZones(cam, polys.map((poly) => poly.map((p) => [Math.round(p[0] * fs[0] / fw), Math.round(p[1] * fs[1] / fh)])));
  }, true);
  log.success('КЗ: стёкла размечены', { serial: cam.id, glasses: polys.length });
  closeMarkModal();
}

async function lvUpload(file) {
  if (!file) return;
  lvMsg('загружаю ' + file.name + ' (' + Math.max(1, Math.round(file.size / 1048576)) + ' МБ)…', '');
  try {
    const resp = await fetch('/api/level/model/upload?name=' + encodeURIComponent(file.name), { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } });
    const r = await resp.json();
    if (r.error) lvMsg('Модель не загрузилась: ' + r.error, 'is-bad');
    else { lvMsg('✓ модель загружена: ' + r.model.name, 'is-ok'); log.success('КЗ: модель уровня загружена', { name: r.model.name }); }
  } catch (e) { lvMsg('Модель не загрузилась: ' + e, 'is-bad'); }
  const f = mdEl('lvFile');
  if (f) f.value = '';
  lvRefresh(true);
}

async function lvLoadModel(name) {
  if (!name) return;
  lvMsg('загружаю ' + name + '…', '');
  const r = await mvJson('/api/level/model/load', { name });
  lvMsg(r && r.error ? 'Модель не загрузилась: ' + r.error : '✓ модель: ' + name, r && r.error ? 'is-bad' : 'is-ok');
  lvRefresh(true);
}

function mdOnLevel(t) {
  const { cam } = mdCamera();
  if (!cam) return;
  const f = t.dataset.f;
  if (f === 'pick') { mdEl('lvFile')?.click(); return; }
  if (f === 'upload') { lvUpload(t.files && t.files[0]); return; }
  if (f === 'mark') { openMarkModal('glass'); return; }
  if (f === 'model') { lvLoadModel(t.value); return; }
  const g = lvGroup(cam);
  if (!g) return;
  if (f === 'contour') { mdEdit(() => { g.contour = t.checked; }); return; }
  if (f === 'geometry' || f === 'liquid_class') { mdEdit(() => { g[f] = (t.value || '').trim() || LV_DEF[f]; }); return; }
  if (LV_NUM[f]) {
    let v = parseFloat(String(t.value).replace(',', '.'));
    if (Number.isNaN(v)) { t.value = lvP(g, f); return; }
    v = Math.max(LV_NUM[f][0], Math.min(LV_NUM[f][1], v));
    t.value = v;
    mdEdit(() => { g[f] = v; });
    return;
  }
  if (f === 'del_glass') { lvDelGlass(cam, g, Number(t.dataset.gi)); return; }
  if (f.startsWith('p_')) {                             // запись в ПЛК: сценарий «Уровень · камера» со своей связью
    const sc = lvEnsureScenario(cam, g);
    if (f === 'p_en' && t.checked && !String(sc.modbus.host || '').trim()) {
      t.checked = false;
      lvMsg('Укажите IP контроллера, потом включайте запись', 'is-bad');
      return;
    }
    mdEdit(() => {
      if (f === 'p_en') { sc.modbus.enable = t.checked; t.closest('.lv-plc2')?.classList.toggle('is-on', t.checked); }
      else if (f === 'p_host') sc.modbus.host = t.value.trim();
      else if (f === 'p_port') sc.modbus.port = parseInt(t.value, 10) || 502;
      else if (f === 'p_dev') sc.modbus.device_id = parseInt(t.value, 10) || 0;
      else if (f === 'p_addr') sc.out = { kind: 'holding', address: Math.max(0, parseInt(t.value, 10) || 0) };
    });
  }
}

// удалить стекло: верхнее — только его; нижнее — рабочее, без него уровень не считается, поэтому уходят оба
// (настройки связи с ПЛК остаются в сценарии и подхватятся при новой разметке)
function lvDelGlass(cam, g, i) {
  const both = i === 0 && g.zones.length > 1;
  const q = i ? 'Удалить верхнее стекло?' : both ? 'Нижнее стекло — рабочее: без него уровень не считается.\nУдалить оба стекла?' : 'Удалить стекло?';
  if (!confirm(q)) return;
  const ids = i ? g.zones.slice(1) : g.zones.slice();
  mdEdit(() => {
    cam.zones = cam.zones.filter((z) => !ids.includes(z.id));
    g.zones = g.zones.filter((id) => !ids.includes(id));
    if (!g.zones.length) {
      cam.groups = cam.groups.filter((x) => x.id !== g.id);
      const sc = lvScenario(cam);
      if (sc) sc.sources = sc.sources.filter((x) => !(x.camera === cam.id && x.group === g.id));
    }
  }, true);
  log.success('КЗ: стекло удалено', { serial: cam.id, glass: i ? 'верхнее' : 'нижнее' });
}

// ---------- карточка уровня на «Зоны · тренды» (как у синхронности валов: слева значения, справа график) ----------
const LV_COL = { total: 'var(--text)', low: '#38bdf8', up: '#fbbf24' };      // Total — толстая линия цвета текста темы, нижнее стекло — голубая, верхнее — жёлтая (как на плитке)
const lvTextColor = () => (getComputedStyle(document.documentElement).getPropertyValue('--text') || '').trim() || '#e2e8f0';

function mdLevelCardHtml(cam) {
  const g = lvGroup(cam);
  if (!g) return '';
  const k = escapeHtml(cam.id);
  const names = g.zones.map((id) => { const z = cam.zones.find((q) => q.id === id); return escapeHtml(z ? z.name : id); }).join(' + ');
  return `<div class="md-zone md-level">
    <div class="md-zone__ctl">
      <div class="md-zone__row"><b>${escapeHtml(g.name)}</b><span class="net-hint">${names}</span><span class="md-sp"></span>
        <span class="md-num md-lv-total" id="md-lvn-${k}" title="Total: нижнее + верхнее стекло, 0…100 %">–</span></div>
      <div class="md-zstat" id="md-lvs-${k}"></div>
      <div class="net-hint" id="md-lvm-${k}"></div>
      <div class="net-hint"><span style="color:${LV_COL.total}">● Total</span> <span style="color:${LV_COL.low}">● нижнее стекло</span>${g.zones.length > 1 ? ` <span style="color:${LV_COL.up}">● верхнее стекло</span>` : ''} · линии стёкол — заполнение стекла, %</div>
    </div>
    <div class="md-zone__chart"><canvas id="md-lvc-${k}" height="84"></canvas></div>
  </div>`;
}

// живые значения карточки: Total сейчас, по стеклам — сейчас / среднее / мин / макс за выбранный период, строка про модель
function mdLevelCardLive(cam, sr) {
  const g = lvGroup(cam);
  if (!g) return;
  const lg = lvLive(cam);
  const res = (MV.states[cam.id] || {}).result;
  const live = {};
  ((res && res.zones) || []).forEach((z) => { live[z.id] = z; });
  const tot = mdEl('md-lvn-' + cam.id);
  if (tot) { tot.textContent = lg && lg.valid ? lvFmt(lg.total) + ' %' : '–'; tot.style.color = lg && lg.valid ? '' : '#9ca3af'; }
  const stats = (zid) => sr && sr.zones && sr.zones[zid] && sr.zones[zid].flow;
  const rows = g.zones.map((zid, i) => {
    const z = cam.zones.find((q) => q.id === zid);
    const s = stats(zid), lz = live[zid];
    const cur = lz && lg && lg.valid ? lvFmt(lz.level) : '–';
    return `<div class="md-zstat__row${i ? '' : ' is-main'}" style="color:${i ? LV_COL.up : LV_COL.low}">${escapeHtml(z ? z.name : zid)}: сейчас <b class="md-zstat__cur">${cur}</b> %` +
      (s ? ` · ср ${lvFmt(s.avg)} · мин ${lvFmt(s.min)} · макс ${lvFmt(s.max)}` : ' · ср – · мин – · макс –') + '</div>';
  });
  const avgs = g.zones.map((zid) => stats(zid)).filter(Boolean);
  if (avgs.length === g.zones.length && sr) {
    // среднее Total = сумма средних уровней стёкол / 2 (мин и макс Total из мин/макс стёкол не сложить — они в разное время)
    rows.unshift(`<div class="md-zstat__row" title="Среднее Total за период: (среднее нижнего + среднее верхнего) / 2">Total за ${mdFmtWin(sr.window_s)}: ср <b>${lvFmt(avgs.reduce((a, s) => a + s.avg, 0) / 2)}</b> %</div>`);
  }
  const el = mdEl('md-lvs-' + cam.id);
  if (el) {
    el.innerHTML = rows.join('') + (sr && sr.span_s < sr.window_s * 0.9 ? `<div class="md-zstat__note">накоплено за ${mdFmtWin(sr.span_s)}</div>` : '');
  }
  const m = mdEl('md-lvm-' + cam.id);
  if (m) {
    const p = (MV.scen || []).find((q) => q.id === lvScenId(cam));
    const plc = !p || !p.plc ? 'запись в ПЛК выключена' : p.ok === null ? 'ПЛК: ждёт ответа' : p.ok ? 'ПЛК: записано ' + p.value : 'ПЛК: ошибка ' + (p.error || '');
    m.textContent = (!lg ? 'ждём данных' : lg.valid ? 'модель ' + lg.model + ' · ' + lg.ms + ' мс · уверенность ' + lg.conf.toFixed(2) : (lg.err || 'ждём первого ответа модели')) + ' · ' + plc;
  }
}

// график уровня: Total и заполнение каждого стекла во времени; ось X — как у остальных графиков (правый край «сейчас», окно — колесом мыши)
function mdDrawLevelChart(cam, h) {
  const g = lvGroup(cam);
  const cv = g && mdEl('md-lvc-' + cam.id);
  const a = g && h.zones && h.zones[g.zones[0]];
  if (!cv || !a) return;
  const b = g.zones[1] ? h.zones[g.zones[1]] : null;
  const t = h.t || [], ok = h.ok || [], n = t.length;
  const w = cv.clientWidth, hh = cv.height || 84;
  if (!w) return;
  if (cv.width !== w) cv.width = w;
  const c = cv.getContext('2d');
  c.clearRect(0, 0, w, hh);
  const plotH = hh - 13, win = MD.win;
  const Y = (v) => plotH - (Math.max(0, Math.min(100, v)) / 100) * (plotH - 6) - 1;
  c.fillStyle = '#94a3b8'; c.font = '10px sans-serif';
  c.fillText('100', 3, 10); c.fillText('0', 3, plotH - 2);
  c.fillText('−' + mdFmtWin(win), 3, hh - 1);
  c.textAlign = 'right'; c.fillText('сейчас', w - 3, hh - 1); c.textAlign = 'left';
  if (n < 2) return;
  const tEnd = t[n - 1];
  const X = (i) => w - 1 - ((tEnd - t[i]) / win) * (w - 2);
  const dt = (t[n - 1] - t[0]) / (n - 1);
  const bw = Math.max(1.5, (dt / win) * w + 1);
  const gap = Math.max(dt * 3, 5);
  for (let i = 0; i < n; i += 1) if (ok[i] === 0) { c.fillStyle = 'rgba(156,163,175,.30)'; c.fillRect(X(i) - bw, 0, bw, plotH); }
  c.setLineDash([4, 3]); c.lineWidth = 1; c.strokeStyle = '#64748b';
  c.beginPath(); c.moveTo(0, Y(50)); c.lineTo(w, Y(50)); c.stroke();
  c.setLineDash([]);
  const line = (vals, color, width) => {
    c.strokeStyle = color; c.lineWidth = width; c.beginPath();
    let pen = false;
    vals.forEach((v, i) => {
      if (v == null || ok[i] === 0) { pen = false; return; }
      if (!pen || t[i] - t[i - 1] > gap) c.moveTo(X(i), Y(v)); else c.lineTo(X(i), Y(v));
      pen = true;
    });
    c.stroke();
  };
  if (b) line(b.flow, LV_COL.up, 1.2);
  line(a.flow, LV_COL.low, 1.2);
  line(a.flow.map((v, i) => v / 2 + (b ? b.flow[i] / 2 : 0)), lvTextColor(), 2);      // Total = (нижнее + верхнее) / 2 в процентах стекла
}

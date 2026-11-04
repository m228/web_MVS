// ---------- КЗ · вкладка «Уровень»: уровень жидкости по YOLO-модели и зонам-стёклам (декантатор) ----------
// Модель живёт в сервисе уровня (level_service); стёкла — два полигона на кадре (нижнее, верхнее). Уровень каждого стекла — доля его
// площади, залитая маской жидкости; Total = (нижнее + верхнее) · 50 %, в ПЛК уходит ×10 (54,3 % → 543). В ПЛК пишет сценарий
// «Уровень · камера» со своей связью, выключенной по умолчанию. Серверная часть — motion_service/motion_level.py.
const LV_DEF = { conf: 0.35, imgsz: 0, geometry: 'lvldec', infer_interval_s: 2, alpha: 0.93, warmup_s: 10, glass1_min: 15, glass2_min: 20,
  glass1_full: 100, glass2_full: 100, hold_s: 30, contour: false, liquid_class: 'liquid' };
// [мин, макс] числовых параметров группы (как проверяет сервер)
const LV_NUM = { conf: [0.01, 0.99], imgsz: [0, 4096], infer_interval_s: [0.2, 600], alpha: [0, 0.999], warmup_s: [0, 600],
  glass1_min: [0, 100], glass2_min: [0, 100], glass1_full: [0, 100], glass2_full: [0, 100], hold_s: [1, 3600] };
const LV = { svc: null, models: null, at: 0, busy: false, msg: '', msgCls: '', notes: [] };

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
      sel.innerHTML = files.length ? mdSel(files.map((f) => [escapeHtml(f), escapeHtml(f)]), cur) : '<option value="">— нет файлов —</option>';
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
  const par = g ? `<details class="lv-par"${LV.parOpen ? ' open' : ''}><summary>Параметры</summary><div class="md-fields">
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
    </div></details>` : '';
  const plc = g ? `<div class="md-h md-h--row md-h--sec">Запись в ПЛК <span class="md-dim" id="lvPlcInfo"></span></div>
    <div class="md-card lv-plc ${m.enable ? 'is-on' : ''}">
      <label class="switch-toggle md-sw" title="Писать Total ×10 в регистр контроллера. Выключено — в ПЛК ничего не уходит"><span class="switch"><input type="checkbox" ${a} data-f="p_en"${m.enable ? ' checked' : ''} /><span class="switch-track"></span></span></label>
      <div class="md-fields lv-plcf">
        ${mdField('IP контроллера', `<input type="text" ${a} data-f="p_host" value="${escapeHtml(m.host)}" placeholder="192.168.0.10" />`)}
        ${mdField('Порт', num('p_port', m.port, 1, 65535, 1))}
        ${mdField('Unit ID', num('p_dev', m.device_id, 0, 255, 1))}
        ${mdField('Регистр Total ×10', num('p_addr', sc && sc.out ? sc.out.address : 0, 0, 65535, 1), 'Целое 0…1000: 54,3 % → 543 (SCADA делит на 10)')}
      </div></div>` : '';
  return `<div class="lv">
    <p class="cap-line" id="lvCap" data-state="unknown"><span class="cap-dot"></span><span class="cap-text">Проверяю сервис уровня…</span></p>
    <div class="lv-row">
      <select id="lvModelSel" ${a} data-f="model" title="Модель из level_service\\model — выбор загружает её сразу"></select>
      <button type="button" class="btn-secondary md-x" ${a} data-f="pick" title="Залить .pt или .onnx с этого компьютера: сервис сохранит и горячо подхватит без перезапуска">Загрузить модель…</button>
      <input type="file" id="lvFile" accept=".pt,.onnx" hidden ${a} data-f="upload" />
    </div>
    <div class="lv-now"><b id="lvTotal">–</b><span class="lv-unit">%</span><span class="md-dim" id="lvParts"></span><span class="md-dim" id="lvMeta"></span></div>
    <div class="lv-row">
      <button type="button" class="btn-primary md-x" ${a} data-f="mark" title="Обведите на кадре нижнее (рабочее) стекло, потом верхнее">${g ? 'Разметить стёкла заново' : 'Разметить стёкла'}</button>
      <button type="button" class="btn-secondary md-x" ${a} data-f="import" title="Перенести настройки LVLDec: зоны, пороги, камеру, ПЛК — из его data\\config.ini">Импорт config.ini LVLDec…</button>
      <input type="file" id="lvIni" accept=".ini,.txt" hidden ${a} data-f="ini" />
      ${g ? `<label class="micro-check" title="Тонкий контур найденной жидкости на плитке камеры"><input type="checkbox" ${a} data-f="contour"${lvP(g, 'contour') ? ' checked' : ''} /><span class="micro-check__text">контур жидкости</span></label>` : ''}
      <span class="lv-msg ${LV.msgCls}" id="lvMsg">${escapeHtml(LV.msg)}</span>
    </div>
    ${LV.notes.length ? `<ul class="lv-notes">${LV.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join('')}</ul>` : ''}
    ${g ? '' : '<p class="net-hint">Стёкол ещё нет. «Разметить стёкла» — обвести на кадре нижнее и верхнее; либо «Импорт config.ini LVLDec» — перенести всё, что настроено в LVLDec.</p>'}
    ${par}${plc}
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

// перенос настроек LVLDec: камера в сервисе уже есть, меняем зоны, пороги и сценарий записи (связь с ПЛК остаётся выключенной)
async function lvImport(cam, file) {
  if (!file) return;
  const r = await mvJson('/api/motion/level_import', { ini: await file.text() });
  const inp = mdEl('lvIni');
  if (inp) inp.value = '';
  if (!r || r.error) { lvMsg('Импорт: ' + ((r && r.error) || 'нет ответа'), 'is-bad'); return; }
  const base = [1920, 1080];             // зоны LVLDec заданы в пикселях кадра основного потока (1920×1080)
  const notes = r.notes.slice();
  if (r.url && mdIp(r.url) && mdIp(r.url) !== mdIp(cam.url)) notes.unshift('адрес камеры в config.ini: ' + mdIp(r.url) + ', в приложении: ' + (mdIp(cam.url) || '—') + ' — зоны перенесены как есть');
  mdEdit(() => {
    if (!cam.frame_size || !(cam.zones || []).length) cam.frame_size = base.slice();
    const fs = cam.frame_size;
    const sc = (p) => [Math.round(p[0] * fs[0] / base[0]), Math.round(p[1] * fs[1] / base[1])];
    let g = lvGroup(cam);
    if (r.zones.lower) g = lvSetZones(cam, [r.zones.lower, r.zones.upper].filter(Boolean).map((poly) => poly.map(sc)));
    if (!g) return;
    Object.assign(g, r.group);
    const s = lvEnsureScenario(cam, g);
    s.out = { kind: 'holding', address: r.plc.address };
    s.modbus = { enable: false, host: r.plc.host, port: r.plc.port, device_id: r.plc.device_id, write_period_s: 1 };
  }, true);
  LV.notes = notes;
  LV.parOpen = true;
  lvMsg(lvGroup(cam) ? '✓ перенесено из config.ini' : 'Зон в config.ini нет — разметьте стёкла', lvGroup(cam) ? 'is-ok' : 'is-bad');
  MD.key = '';
  renderMotionDock();
  log.success('КЗ: импорт config.ini LVLDec', { serial: cam.id });
}

function mdOnLevel(t) {
  const { cam } = mdCamera();
  if (!cam) return;
  const f = t.dataset.f;
  if (f === 'pick') { mdEl('lvFile')?.click(); return; }
  if (f === 'import') { mdEl('lvIni')?.click(); return; }
  if (f === 'upload') { lvUpload(t.files && t.files[0]); return; }
  if (f === 'ini') { lvImport(cam, t.files && t.files[0]); return; }
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
  if (f.startsWith('p_')) {                             // запись в ПЛК: сценарий «Уровень · камера» со своей связью
    const sc = lvEnsureScenario(cam, g);
    if (f === 'p_en' && t.checked && !String(sc.modbus.host || '').trim()) {
      t.checked = false;
      lvMsg('Укажите IP контроллера, потом включайте запись', 'is-bad');
      return;
    }
    mdEdit(() => {
      if (f === 'p_en') { sc.modbus.enable = t.checked; t.closest('.lv-plc')?.classList.toggle('is-on', t.checked); }
      else if (f === 'p_host') sc.modbus.host = t.value.trim();
      else if (f === 'p_port') sc.modbus.port = parseInt(t.value, 10) || 502;
      else if (f === 'p_dev') sc.modbus.device_id = parseInt(t.value, 10) || 0;
      else if (f === 'p_addr') sc.out = { kind: 'holding', address: Math.max(0, parseInt(t.value, 10) || 0) };
    });
  }
}

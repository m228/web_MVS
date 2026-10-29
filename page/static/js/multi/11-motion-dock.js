// ---------- КЗ: нижняя панель со вкладками (Зоны · Группы · Камера · Тренды · ПЛК/сценарии) + окно разметки зоны ----------
// Камера — та, что в фокусе (только RTSP). Правки применяются сразу (автосохранение с задержкой, PUT /api/motion/config).
const MD = {
  tab: 'zones',
  key: '',
  saveTimer: null,
  rev: 0,
  saveLabel: '',
  saveCls: '',
  editGrp: null,        // индекс редактируемой группы, -1 — новая
  trendBusy: false,
  mark: null,           // { cam, pts, cursor, fw, fh, closed }
  markBusy: false,
};
const MD_THR = { flow: [0.9, 0.6], diff: [4.0, 2.0] };
const mdEl = (id) => document.getElementById(id);

function mdCamera() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return { source: null, cam: null };
  return { source, cam: mvCam(source.serial) };
}

function setMotionDock(open) {
  MV.dockOpen = open;
  const dock = mdEl('motionDock');
  if (dock) dock.hidden = !open;
  document.getElementById('multiMotionBtn')?.classList.toggle('is-dock', open);
  MD.key = '';
  renderMotionDock();
  if (open) setTimeout(drawAllMotionTiles, 50);
}

// ---------- автосохранение ----------
function mdPaintSave() {
  const el = mdEl('mdSave');
  if (el) { el.textContent = MD.saveLabel; el.className = 'motion-dock__save ' + MD.saveCls; }
}

function mdCommit() {
  MV.dirty = true;
  MD.rev += 1;
  MD.saveLabel = 'сохраняю…';
  MD.saveCls = '';
  mdPaintSave();
  clearTimeout(MD.saveTimer);
  MD.saveTimer = setTimeout(mdFlush, 700);
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

// правка → тут же в локальный конфиг, на сервис уйдёт через 0.7 с после последней правки
function mdEdit(mutate, rerender) {
  mutate();
  mdCommit();
  if (rerender) { MD.key = ''; renderMotionDock(); }
  drawAllMotionTiles();
}

// ---------- каркас панели ----------
function mdPill(st, conn) {
  if (!MV.online) return ['сервис КЗ не запущен', 'is-bad'];
  if (!conn) return ['анализ выключен', ''];
  if (!st) return ['нет данных', 'is-bad'];
  if (st.result && st.result.corrupt) return ['битый поток — результаты не учитываются', 'is-warn'];
  if (!st.connected) return ['нет связи с камерой', 'is-bad'];
  if (st.result && !st.result.alive) return [st.result.reason || 'нет данных', 'is-bad'];
  return ['жива · ' + st.fps + ' к/с', 'is-ok'];
}

function renderMotionDock() {
  const dock = mdEl('motionDock');
  if (!dock) return;
  const { source, cam } = mdCamera();
  dock.hidden = !MV.dockOpen || !source;           // фокус на GigE-ячейке — панель прячется и возвращается сама
  if (dock.hidden) return;
  const key = [source.serial, MD.tab, MV.online, cam ? cam.enabled : 'x', cam ? cam.zones.length : 0, cam ? (cam.groups || []).length : 0,
    ((MV.cfg && MV.cfg.scenarios) || []).length, MD.editGrp].join('|');
  mdEl('mdCamName').textContent = source.label || source.serial;
  const sw = mdEl('mdSwitch');
  sw.disabled = !MV.online;
  sw.checked = !!(cam && cam.enabled);
  mdEl('mdSwitchLabel').textContent = cam && cam.enabled ? 'Анализ включён' : 'Выкл';
  mdEl('mdMark').disabled = !(cam && cam.enabled);
  mdEl('mdRec').disabled = !(cam && cam.enabled);
  dock.querySelectorAll('[data-md-tab]').forEach((b) => b.classList.toggle('is-active', b.dataset.mdTab === MD.tab));
  const cap = mdEl('mdCap');
  setCapLine(cap, MV.online, 'Сервис движения работает', 'Сервис движения не запущен (motion_service\\setup.bat, потом перезапуск web_MVS)');
  mdPaintSave();
  if (key === MD.key) return;
  MD.key = key;
  const body = mdEl('mdBody');
  if (MD.tab !== 'plc' && !(cam && cam.enabled)) {
    body.innerHTML = '<p class="net-hint">Включите «Анализ» переключателем справа сверху — для этой камеры появятся зоны, тренды и вывод в ПЛК.</p>';
    return;
  }
  body.innerHTML = { zones: mdZonesHtml, groups: mdGroupsHtml, camera: mdCameraHtml, trends: mdTrendsHtml, plc: mdPlcHtml }[MD.tab](cam);
  if (MD.tab === 'plc') mdBindPlc();
  if (MD.tab === 'trends') mdDrawTrends();
  paintMotionDockLive();
}

const mdSel = (opts, cur) => opts.map(([v, t]) => `<option value="${v}"${String(v) === String(cur) ? ' selected' : ''}>${t}</option>`).join('');
const mdNum = (attrs, v, extra) => `<input type="number" ${attrs} value="${escapeHtml(v)}" ${extra || ''} />`;

// ---------- вкладка «Зоны» ----------
function mdOutCell(o, attrs) {
  return `<select ${attrs} data-f="okind">${mdSel([['', 'нет'], ['holding', 'регистр'], ['coil', 'coil']], o ? o.kind : '')}</select>` +
    ` <input type="number" min="0" max="65535" ${attrs} data-f="oaddr" value="${o ? o.address : 0}" title="Адрес регистра / coil" />`;
}

function mdZonesHtml(cam) {
  if (!cam.zones.length) return '<p class="net-hint">Зон нет. Нажмите «Разметить зону» и обведите на кадре то, что должно двигаться (лента, шнек).</p>';
  const rows = cam.zones.map((z, i) => {
    const a = `data-md="z" data-i="${i}"`;
    return `<tr>
      <td><input type="text" ${a} data-f="name" value="${escapeHtml(z.name)}" title="${escapeHtml(z.id)}" /></td>
      <td><span class="cv-bit" id="md-zb-${escapeHtml(z.id)}">–</span></td>
      <td class="md-num" id="md-zv-${escapeHtml(z.id)}">–</td>
      <td><select ${a} data-f="type">${mdSel([['motion', 'motion'], ['presence', 'presence']], z.type)}</select></td>
      <td><select ${a} data-f="metric">${mdSel([['diff', 'diff'], ['flow', 'flow']], z.metric)}</select></td>
      <td>${mdNum(a + ' data-f="on"', z.on, 'step="0.05" min="0" title="Включить при значении ' + (z.invert ? 'НИЖЕ off' : 'ВЫШЕ on') + '"')}</td>
      <td>${mdNum(a + ' data-f="off"', z.off, 'step="0.05" min="0"')}</td>
      <td>${mdOutCell(z.out, a)}</td>
      <td><button type="button" class="btn-secondary md-x" ${a} data-f="del" title="Удалить зону">✕</button></td></tr>`;
  }).join('');
  return `<div class="md-scroll"><table class="md-table"><thead><tr><th>Зона</th><th>Бит</th><th title="Значение метрики, по которой зона принимает решение">Знач.</th><th>Тип</th><th>Метрика</th><th>on</th><th>off</th><th>Вывод в ПЛК</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="net-hint">Значения идут сразу, вывод в ПЛК не обязателен. Правки сохраняются сами. diff и flow — при наведении на зону в ячейке.</p>`;
}

function mdOnZone(t, cam) {
  const i = Number(t.dataset.i), f = t.dataset.f, z = cam.zones[i];
  if (!z) return;
  if (f === 'del') {
    if (!confirm('Удалить зону «' + z.name + '»?')) return;
    mdEdit(() => {
      cam.zones.splice(i, 1);
      (cam.groups || []).forEach((g) => { g.zones = g.zones.filter((x) => x !== z.id); if (g.order) g.order = g.order.filter((x) => x !== z.id); });
      cam.groups = (cam.groups || []).filter((g) => g.zones.length);
      (MV.cfg.scenarios || []).forEach((s) => { s.sources = s.sources.filter((x) => !(x.camera === cam.id && x.zone === z.id)); });
    }, true);
    return;
  }
  mdEdit(() => {
    if (f === 'name') z.name = t.value;
    else if (f === 'type') { z.type = t.value; z.invert = t.value === 'presence'; }
    else if (f === 'metric') { z.metric = t.value; z.on = MD_THR[z.metric][0]; z.off = MD_THR[z.metric][1]; }
    else if (f === 'on' || f === 'off') z[f] = parseFloat(t.value) || 0;
    else if (f === 'okind') z.out = t.value ? { kind: t.value, address: (z.out && z.out.address) || 0 } : null;
    else if (f === 'oaddr' && z.out) z.out.address = parseInt(t.value, 10) || 0;
  }, f === 'type' || f === 'metric');
}

// ---------- вкладка «Группы» ----------
function mdGroupsHtml(cam) {
  const groups = cam.groups || [];
  const list = groups.length ? groups.map((g, i) => {
    const out = g.out ? ({ holding: 'регистр', bits: 'биты в регистр', coils: 'coil на зону' }[g.out.kind] + ' @' + g.out.address) : 'без вывода';
    const names = g.zones.map((id) => { const z = cam.zones.find((q) => q.id === id); return escapeHtml(z ? z.name : id); }).join(', ');
    return `<div class="md-card"><b>${escapeHtml(g.name)}</b><span class="net-hint">${g.type} · ${names} · ${out}</span><span class="md-sp"></span>
      <span class="cv-bit" id="md-gv-${escapeHtml(g.id)}">–</span>
      <button type="button" class="btn-secondary md-x" data-md="g" data-f="edit" data-i="${i}">Изменить</button>
      <button type="button" class="btn-secondary md-x" data-md="g" data-f="del" data-i="${i}">✕</button></div>`;
  }).join('') : '<p class="net-hint">Групп нет — каждая зона пишется в ПЛК сама (колонка «Вывод в ПЛК» на вкладке «Зоны»). Группа складывает зоны одной камеры: sum — сколько активно, monotonic — уровень снизу вверх (шахта), worktimer — наработка в % за окно (лента жома). Зоны из РАЗНЫХ камер объединяйте в «ПЛК · сценарии».</p>';
  return list + '<div class="popup-actions"><button type="button" class="btn-secondary" data-md="g" data-f="add">+ Группа</button></div><div id="mdGrpEd">' + (MD.editGrp !== null ? mdGroupEditorHtml(cam) : '') + '</div>';
}

function mdGroupEditorHtml(cam) {
  const g = MD.editGrp >= 0 ? cam.groups[MD.editGrp] : { id: '', name: '', type: 'sum', zones: [], out: { kind: 'holding', address: 0 }, window_s: 60, min_active: 1, agg: 'any', scale: 100 };
  const zs = cam.zones.map((z) => `<label class="md-chk"><input type="checkbox" value="${escapeHtml(z.id)}"${g.zones.includes(z.id) ? ' checked' : ''} />${escapeHtml(z.name)}</label>`).join('');
  return `<div class="md-card md-card--col" id="mdGe">
    <label>Название <input type="text" id="geName" value="${escapeHtml(g.name)}" placeholder="Уровень шахты / Наработка" /></label>
    <label>Тип <select id="geType">${mdSel([['sum', 'sum — сколько зон активно'], ['monotonic', 'monotonic — уровень снизу вверх'], ['worktimer', 'worktimer — наработка за окно']], g.type)}</select></label>
    <div>Зоны: ${zs}</div>
    <label>Вывод <select id="geKind">${mdSel([['', 'нет'], ['holding', 'значение в регистр'], ['bits', 'биты зон в один регистр'], ['coils', 'coil на каждую зону']], g.out ? g.out.kind : '')}</select>
      адрес <input type="number" id="geAddr" min="0" max="65535" value="${(g.out && g.out.address) || 0}" /></label>
    <p class="net-hint md-gt" data-for="monotonic">Порядок «снизу вверх» = порядок зон в списке (первая зона — низ шахты).</p>
    <label class="md-gt" data-for="worktimer">Окно, с <input type="number" id="geWin" min="1" value="${g.window_s || 60}" /></label>
    <label class="md-gt" data-for="worktimer">Мин. активных зон <input type="number" id="geMin" min="1" value="${g.min_active || 1}" /></label>
    <label class="md-gt" data-for="worktimer">Секунда рабочая, если <select id="geAgg">${mdSel([['any', 'хоть один кадр активен'], ['majority', 'не меньше половины кадров'], ['all', 'все кадры активны']], g.agg || 'any')}</select></label>
    <label class="md-gt" data-for="worktimer">Шкала <input type="number" id="geScale" min="1" value="${g.scale || 100}" title="100 = проценты; 1000 или 4095 — для аналогового выхода" /></label>
    <div class="popup-actions"><button type="button" class="btn-primary" data-md="g" data-f="ok">Готово</button><button type="button" class="btn-secondary" data-md="g" data-f="cancel">Отмена</button></div></div>`;
}

function mdSyncGroupEditor() {
  const t = mdEl('geType');
  if (!t) return;
  document.querySelectorAll('#mdGe .md-gt').forEach((n) => { n.hidden = n.dataset.for !== t.value; });
}

function mdOnGroup(t, cam) {
  const f = t.dataset.f, i = Number(t.dataset.i);
  if (f === 'add') { MD.editGrp = -1; MD.key = ''; renderMotionDock(); mdSyncGroupEditor(); return; }
  if (f === 'edit') { MD.editGrp = i; MD.key = ''; renderMotionDock(); mdSyncGroupEditor(); return; }
  if (f === 'cancel') { MD.editGrp = null; MD.key = ''; renderMotionDock(); return; }
  if (f === 'del') {
    if (!confirm('Удалить группу «' + cam.groups[i].name + '»?')) return;
    mdEdit(() => { cam.groups.splice(i, 1); }, true);
    return;
  }
  if (f === 'ok') {
    const ids = [...document.querySelectorAll('#mdGe input[type=checkbox]:checked')].map((x) => x.value);
    if (!ids.length) { alert('Выберите хотя бы одну зону'); return; }
    const type = mdEl('geType').value;
    const old = MD.editGrp >= 0 ? cam.groups[MD.editGrp] : null;
    const ng = { id: old ? old.id : '', name: mdEl('geName').value.trim() || 'Группа', type, zones: ids,
      out: mdEl('geKind').value ? { kind: mdEl('geKind').value, address: parseInt(mdEl('geAddr').value, 10) || 0 } : null };
    if (!ng.id) { let n = 1; while ((cam.groups || []).some((q) => q.id === 'g' + n)) n += 1; ng.id = 'g' + n; }
    if (type === 'monotonic') ng.order = cam.zones.map((z) => z.id).filter((id) => ids.includes(id));
    if (type === 'worktimer') {
      Object.assign(ng, { window_s: parseInt(mdEl('geWin').value, 10) || 60, min_active: Math.min(ids.length, parseInt(mdEl('geMin').value, 10) || 1),
        agg: mdEl('geAgg').value, scale: parseInt(mdEl('geScale').value, 10) || 100 });
    }
    mdEdit(() => {
      cam.groups = cam.groups || [];
      if (MD.editGrp >= 0) cam.groups[MD.editGrp] = ng; else cam.groups.push(ng);
      MD.editGrp = null;
    }, true);
  }
}

// ---------- вкладка «Камера» ----------
function mdCameraHtml(cam) {
  const so = cam.status_out;
  const a = 'data-md="c"';
  return `<div class="md-form">
    <label>Название</label><input type="text" ${a} data-f="name" value="${escapeHtml(cam.name)}" />
    <label>Адрес потока</label><input type="text" ${a} data-f="url" value="${escapeHtml(cam.url)}" />
    <label>Кадров/с в анализ</label>${mdNum(a + ' data-f="proc_fps"', cam.proc_fps, 'min="0.2" max="30" step="0.5"')}
    <label title="Сравнивать кадр не с соседним, а через N кадров: медленное движение виднее">Шаг сравнения, кадров</label>${mdNum(a + ' data-f="frame_gap"', cam.frame_gap, 'min="1" max="10"')}
    <label title="Картинка не меняется дольше — камера зависла: в ПЛК нули и статус 0">Камера зависла через, с</label>${mdNum(a + ' data-f="freeze_timeout_s"', cam.freeze_timeout_s, 'min="1"')}
    <label title="Доля кадра, залитая ровным серым (так выглядит битый H.264). 0 — не проверять">Серая каша: доля кадра</label>${mdNum(a + ' data-f="corrupt_ratio"', cam.corrupt_ratio == null ? 0.3 : cam.corrupt_ratio, 'min="0" max="1" step="0.05"')}
    <label title="Сколько секунд после каши результаты ещё не учитываются">Пауза после каши, с</label>${mdNum(a + ' data-f="corrupt_hold_s"', cam.corrupt_hold_s == null ? 2 : cam.corrupt_hold_s, 'min="0" max="60" step="0.5"')}
    <label>Статус «камера жива»</label><div><select ${a} data-f="skind">${mdSel([['', 'нет'], ['holding', 'регистр'], ['coil', 'coil']], so ? so.kind : '')}</select>
      адрес <input type="number" ${a} data-f="saddr" min="0" max="65535" value="${so ? so.address : 0}" /></div>
  </div><p class="net-hint">При смене настроек камеры сервис переподключается к потоку (несколько секунд без данных).</p>`;
}

function mdOnCamera(t, cam) {
  const f = t.dataset.f;
  mdEdit(() => {
    if (f === 'name' || f === 'url') cam[f] = t.value.trim();
    else if (f === 'skind') cam.status_out = t.value ? { kind: t.value, address: (cam.status_out && cam.status_out.address) || 0 } : null;
    else if (f === 'saddr') { if (cam.status_out) cam.status_out.address = parseInt(t.value, 10) || 0; }
    else if (f === 'frame_gap') cam[f] = parseInt(t.value, 10) || 1;
    else cam[f] = parseFloat(t.value) || 0;
  });
}

// ---------- вкладка «Тренды» ----------
function mdTrendsHtml(cam) {
  if (!cam.zones.length) return '<p class="net-hint">Зон нет — графику нечего показывать.</p>';
  return cam.zones.map((z) => `<div class="md-chart"><span class="md-chart__lbl" title="${escapeHtml(z.name)}">${escapeHtml(z.name)} <small>(${z.metric})</small></span>
    <canvas id="md-ch-${escapeHtml(z.id)}" height="96"></canvas></div>`).join('') +
    '<p class="net-hint">Линия — метрика зоны. Пунктир: красный — порог «on», оранжевый — «off». Нижняя полоска: зелёная — крутится, красная — стоит, серая — нет данных или битый поток. Серый фон — кадры, которые не учитывались.</p>';
}

async function mdDrawTrends() {
  if (MD.tab !== 'trends' || !MV.dockOpen || MD.trendBusy) return;
  const { cam } = mdCamera();
  if (!cam || !cam.enabled || !cam.zones.length) return;
  MD.trendBusy = true;
  const h = await mvApi('/api/motion/history?camera=' + encodeURIComponent(cam.id) + '&n=600');
  MD.trendBusy = false;
  if (!h || h.error || !h.zones) return;
  cam.zones.forEach((z) => mdDrawChart(z, h.zones[z.id], h.t || [], h.ok || []));
}

function mdDrawChart(z, d, t, ok) {
  const cv = mdEl('md-ch-' + z.id);
  if (!cv || !d) return;
  const w = cv.clientWidth, hh = 96, strip = 8;
  if (cv.width !== w) cv.width = w;
  const g = cv.getContext('2d');
  g.clearRect(0, 0, w, hh);
  const ys = z.metric === 'flow' ? d.flow : d.diff, n = ys.length;
  if (n < 2) return;
  const plotH = hh - strip - 14;
  const top = Math.max(z.on * 1.6, ...ys, 0.0001);
  const X = (i) => (i / (n - 1)) * (w - 2) + 1;
  const Y = (v) => plotH - (v / top) * (plotH - 6) - 1;
  const bw = w / n + 1;
  for (let i = 0; i < n; i += 1) {
    const bad = ok[i] === 0;
    if (bad) { g.fillStyle = 'rgba(156,163,175,.30)'; g.fillRect(X(i) - bw / 2, 0, bw, plotH); }
    else if (d.bit[i]) { g.fillStyle = 'rgba(74,222,128,.20)'; g.fillRect(X(i) - bw / 2, 0, bw, plotH); }
    g.fillStyle = bad ? '#9ca3af' : (d.bit[i] ? '#4ade80' : '#f87171');
    g.fillRect(X(i) - bw / 2, plotH + 3, bw, strip);
  }
  g.setLineDash([4, 3]); g.lineWidth = 1;
  g.strokeStyle = '#ef4444'; g.beginPath(); g.moveTo(0, Y(z.on)); g.lineTo(w, Y(z.on)); g.stroke();
  g.strokeStyle = '#f59e0b'; g.beginPath(); g.moveTo(0, Y(z.off)); g.lineTo(w, Y(z.off)); g.stroke();
  g.setLineDash([]);
  g.strokeStyle = '#38bdf8'; g.lineWidth = 1.6; g.beginPath();
  ys.forEach((v, i) => (i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))));
  g.stroke();
  g.fillStyle = '#94a3b8'; g.font = '10px sans-serif';
  g.fillText(top.toFixed(top < 10 ? 2 : 0), 3, 10);
  const span = t.length > 1 ? Math.round(t[t.length - 1] - t[0]) : 0;
  g.fillText('−' + (span >= 120 ? Math.round(span / 60) + ' мин' : span + ' с'), 3, hh - 1);
  g.textAlign = 'right'; g.fillText('сейчас', w - 3, hh - 1); g.textAlign = 'left';
}

// ---------- вкладка «ПЛК · сценарии» ----------
function mdSourceOptions() {
  const out = [];
  ((MV.cfg && MV.cfg.cameras) || []).forEach((c) => c.zones.forEach((z) => out.push({ camera: c.id, zone: z.id, label: c.name + ' · ' + z.name })));
  return out;
}

function mdPlcHtml() {
  const m = (MV.cfg && MV.cfg.modbus) || { enable: false, host: '', port: 502, device_id: 1, write_period_s: 1 };
  const a = 'data-md="p"';
  const scens = ((MV.cfg && MV.cfg.scenarios) || []).map((s, i) => mdScenarioHtml(s, i)).join('') || '<p class="net-hint">Сценариев нет. Сценарий берёт зоны любых камер, считает «хоть одна крутится» / «все крутятся» / «сколько крутится» и пишет ОДНО значение в регистр или coil.</p>';
  return `<div class="md-cols">
    <div><h3 class="md-h">Подключение к ПЛК</h3><div class="md-form">
      <label>Писать в ПЛК</label><input type="checkbox" ${a} data-f="enable"${m.enable ? ' checked' : ''} />
      <label>IP контроллера</label><input type="text" ${a} data-f="host" value="${escapeHtml(m.host)}" />
      <label>Порт / Unit ID</label><div>${mdNum(a + ' data-f="port"', m.port, 'min="1" max="65535"')} ${mdNum(a + ' data-f="device_id"', m.device_id, 'min="0" max="255"')}</div>
      <label title="Значения пишутся сразу при изменении и ещё раз с этим периодом">Обновлять не реже, с</label>${mdNum(a + ' data-f="write_period_s"', m.write_period_s, 'min="0.1" step="0.1"')}
      <label>Проверка связи</label><div>регистр ${mdNum('id="mdPingAddr"', 0, 'min="0" max="65535"')} <button type="button" class="btn-secondary" id="mdPing">Прочитать</button></div>
    </div><p class="net-hint" id="mdPingInfo">Проверка только читает один регистр, в ПЛК ничего не пишется.</p>
    <div id="mdPlcCams"></div></div>
    <div><h3 class="md-h">Сценарии записи</h3>${scens}
      <div class="popup-actions"><button type="button" class="btn-secondary" data-md="s" data-f="add">+ Сценарий</button></div></div></div>`;
}

function mdScenarioHtml(s, i) {
  const a = `data-md="s" data-i="${i}"`;
  const opts = mdSourceOptions();
  const chosen = new Set(s.sources.map((x) => x.camera + '/' + x.zone));
  const src = opts.length ? opts.map((o) => `<label class="md-chk"><input type="checkbox" ${a} data-f="src" value="${escapeHtml(o.camera + '/' + o.zone)}"${chosen.has(o.camera + '/' + o.zone) ? ' checked' : ''} />${escapeHtml(o.label)}</label>`).join('')
    : '<span class="net-hint">нет размеченных зон</span>';
  return `<div class="md-card md-card--col">
    <div class="md-row"><input type="text" ${a} data-f="name" value="${escapeHtml(s.name)}" /><span class="md-sp"></span>
      <span class="cv-bit" id="md-sv-${escapeHtml(s.id)}">–</span>
      <button type="button" class="btn-secondary md-x" ${a} data-f="del" title="Удалить сценарий">✕</button></div>
    <div class="md-flow"><div class="md-flow__src">${src}</div><span class="md-flow__arrow">→</span>
      <div class="md-flow__rule"><select ${a} data-f="rule">${mdSel([['or', 'ИЛИ — хоть одна крутится'], ['and', 'И — все крутятся'], ['sum', 'Сумма — сколько крутится']], s.rule)}</select></div>
      <span class="md-flow__arrow">→</span>
      <div class="md-flow__out"><select ${a} data-f="okind">${mdSel([['', 'не писать'], ['holding', 'регистр'], ['coil', 'coil']], s.out ? s.out.kind : '')}</select>
        <input type="number" min="0" max="65535" ${a} data-f="oaddr" value="${s.out ? s.out.address : 0}" title="Адрес" /></div></div>
    <span class="net-hint" id="md-ss-${escapeHtml(s.id)}"></span></div>`;
}

function mdOnPlc(t) {
  const m = MV.cfg.modbus;
  const f = t.dataset.f;
  mdEdit(() => {
    if (f === 'enable') m.enable = t.checked;
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
    mdEdit(() => list.push({ id: 's' + n, name: 'Сценарий ' + n, rule: 'or', sources: [], out: { kind: 'holding', address: 0 } }), true);
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
    else if (f === 'okind') s.out = t.value ? { kind: t.value, address: (s.out && s.out.address) || 0 } : null;
    else if (f === 'oaddr') { if (s.out) s.out.address = parseInt(t.value, 10) || 0; }
    else if (f === 'src') {
      const [camera, zone] = t.value.split('/');
      s.sources = s.sources.filter((x) => !(x.camera === camera && x.zone === zone));
      if (t.checked) s.sources.push({ camera, zone });
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

// ---------- живые значения ----------
function paintMotionDockLive() {
  if (!MV.dockOpen) return;
  const { source, cam } = mdCamera();
  if (!source) return;
  const st = MV.states[source.serial];
  const [pill, cls] = mdPill(st, cam && cam.enabled);
  const pe = mdEl('mdPill');
  if (pe) { pe.textContent = pill; pe.className = 'motion-dock__pill ' + cls; }
  const rec = mdEl('mdRec');
  if (rec) rec.textContent = st && st.recording ? '■ остановить запись' : '● запись CSV';
  const res = st && st.result;
  ((res && res.zones) || []).forEach((z) => {
    const bit = mdEl('md-zb-' + z.id);
    if (bit) { bit.textContent = z.bit; bit.classList.toggle('on', z.bit === 1); }
    const val = mdEl('md-zv-' + z.id);
    if (val) val.textContent = Number(z.value).toFixed(2);
  });
  ((res && res.groups) || []).forEach((g) => {
    const b = mdEl('md-gv-' + g.id);
    if (b) { b.textContent = g.value; b.classList.toggle('on', g.value > 0); }
  });
  (MV.scen || []).forEach((s) => {
    const b = mdEl('md-sv-' + s.id);
    if (b) { b.textContent = s.value; b.classList.toggle('on', s.value > 0); }
    const info = mdEl('md-ss-' + s.id);
    if (info) {
      info.textContent = 'источников: ' + (s.bits || []).length + ' · биты: ' + (s.bits || []).join('') + ' · ' +
        (!s.plc ? 'запись в ПЛК выключена' : s.ok === null ? 'ПЛК: ждёт' : s.ok ? 'ПЛК: записано' : 'ПЛК: ошибка ' + (s.error || ''));
    }
  });
  const box = mdEl('mdPlcCams');
  if (box && MD.tab === 'plc' && MV.cfg) {
    box.innerHTML = '<h3 class="md-h">Камеры и запись</h3>' + MV.cfg.cameras.filter((c) => c.enabled).map((c) => {
      const s = MV.states[c.id], p = (s && s.plc) || {};
      return `<div class="md-card"><b>${escapeHtml(c.name)}</b><span class="md-sp"></span><span class="net-hint">${!MV.cfg.modbus.enable ? 'запись выключена' : p.ok === null || p.ok === undefined ? 'ПЛК: ждёт' : p.ok ? 'ПЛК: ок' : 'ПЛК: ошибка ' + escapeHtml(p.error || '')}</span></div>`;
    }).join('');
  }
  if (MD.tab === 'trends' && (MV.tick % 2 === 0)) mdDrawTrends();
}

// ---------- включение анализа и запись CSV ----------
async function mdOnSwitch(event) {
  const { source } = mdCamera();
  if (!source) return;
  const want = event.target.checked;
  const r = await mvJson('/api/motion/vision', { serial: source.serial, enabled: want, name: source.label, url: (source.connection && source.connection.url) || '' });
  if (r && r.error) { alert('КЗ: ' + r.error); event.target.checked = !want; return; }
  if (r && r.cameras) MV.cfg = r;
  log.info('КЗ: анализ движения ' + (want ? 'включён' : 'выключен'), { serial: source.serial });
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

// ---------- окно разметки зоны ----------
async function mdLoadMarkFrame() {
  const m = MD.mark;
  if (!m || m.busy || m.pts.length) return;       // пока не поставлена первая точка, кадр живой; потом замирает
  m.busy = true;
  try {
    const r = await fetch('/api/motion/snapshot?camera=' + encodeURIComponent(m.cam) + '&w=1280&t=' + Date.now());
    if (!r.ok) { mdEl('mmHint').textContent = 'Кадра ещё нет — камера не подключилась. Подождите или проверьте адрес потока.'; return; }
    m.fw = Number(r.headers.get('X-Frame-W')) || m.fw;
    m.fh = Number(r.headers.get('X-Frame-H')) || m.fh;
    const blob = await r.blob();
    const img = mdEl('mmImg');
    const old = img.src;
    img.src = URL.createObjectURL(blob);
    if (old.startsWith('blob:')) URL.revokeObjectURL(old);
  } catch (e) { /* ждём следующий кадр */ } finally { m.busy = false; }
}

function openMarkModal() {
  const { source, cam } = mdCamera();
  if (!source || !cam || !cam.enabled) return;
  MD.mark = { cam: cam.id, pts: [], cursor: null, fw: 0, fh: 0, closed: false, busy: false };
  mdEl('mmCam').textContent = source.label || source.serial;
  mdEl('mmImg').removeAttribute('src');
  mdEl('mmForm').hidden = true;
  mdEl('mmOk').hidden = true;
  mdEl('mmClose').hidden = false;
  mdEl('mmHint').textContent = 'Кликайте по углам зоны. ПКМ — убрать точку. «Замкнуть» или Enter — готово (минимум 3 точки). Кадр замирает после первой точки.';
  const modal = mdEl('motionMarkModal');
  modal.classList.add('show');
  modal.setAttribute('aria-hidden', 'false');
  mdLoadMarkFrame();
  MD.markTimer = setInterval(mdLoadMarkFrame, 1000);
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
  mdEl('mmClose').disabled = m.closed || m.pts.length < 3;
  mdEl('mmUndo').disabled = m.closed || !m.pts.length;
  mdDrawMark();
}

function mdDrawMark() {
  const m = MD.mark, img = mdEl('mmImg'), cv = mdEl('mmCanvas');
  if (!m || !img.clientWidth) return;
  const w = img.clientWidth, h = img.clientHeight;
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const g = cv.getContext('2d');
  g.clearRect(0, 0, w, h);
  const cam = mvCam(m.cam);
  const fw = m.fw || img.naturalWidth, fh = m.fh || img.naturalHeight;
  if (!fw || !fh) return;
  const sx = w / fw, sy = h / fh;
  const rs = (cam && cam.frame_size) || [fw, fh];
  g.lineWidth = 2; g.font = 'bold 14px sans-serif';
  ((cam && cam.zones) || []).forEach((z) => {      // уже размеченные зоны — для ориентира
    g.strokeStyle = '#9ca3af'; g.fillStyle = 'rgba(156,163,175,.14)';
    g.beginPath();
    z.points.forEach((p, i) => { const x = p[0] * (fw / rs[0]) * sx, y = p[1] * (fh / rs[1]) * sy; if (i) g.lineTo(x, y); else g.moveTo(x, y); });
    g.closePath(); g.fill(); g.stroke();
  });
  g.strokeStyle = '#38bdf8'; g.fillStyle = '#38bdf8';
  g.beginPath();
  m.pts.forEach((p, i) => (i ? g.lineTo(p[0] * sx, p[1] * sy) : g.moveTo(p[0] * sx, p[1] * sy)));
  if (m.closed) g.closePath();
  else if (m.cursor) g.lineTo(m.cursor[0] * sx, m.cursor[1] * sy);
  g.stroke();
  m.pts.forEach((p) => { g.beginPath(); g.arc(p[0] * sx, p[1] * sy, 4, 0, 7); g.fill(); });
}

function mdMarkPoint(event) {
  const m = MD.mark, cv = mdEl('mmCanvas'), r = cv.getBoundingClientRect();
  const fw = m.fw || mdEl('mmImg').naturalWidth, fh = m.fh || mdEl('mmImg').naturalHeight;
  return [Math.max(0, Math.min(fw, Math.round((event.clientX - r.left) / r.width * fw))), Math.max(0, Math.min(fh, Math.round((event.clientY - r.top) / r.height * fh)))];
}

function mdMarkClose() {
  const m = MD.mark;
  if (!m || m.closed || m.pts.length < 3) return;
  m.closed = true;
  m.cursor = null;
  mdEl('mmForm').hidden = false;
  mdEl('mmOk').hidden = false;
  mdEl('mmClose').hidden = true;
  mdEl('mmName').focus();
  mdMarkUi();
}

function mdMarkSave() {
  const m = MD.mark, cam = m && mvCam(m.cam);
  if (!cam) return;
  const fw = m.fw || mdEl('mmImg').naturalWidth, fh = m.fh || mdEl('mmImg').naturalHeight;
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

  mdEl('mdSwitch')?.addEventListener('change', mdOnSwitch);
  mdEl('mdMark')?.addEventListener('click', openMarkModal);
  mdEl('mdRec')?.addEventListener('click', mdOnRec);
  mdEl('mdClose')?.addEventListener('click', () => setMotionDock(false));
  document.querySelectorAll('[data-md-tab]').forEach((b) => b.addEventListener('click', () => {
    MD.tab = b.dataset.mdTab;
    MD.key = '';
    renderMotionDock();
  }));

  const body = mdEl('mdBody');
  const dispatch = (e) => {
    const t = e.target;
    const kind = t.dataset && t.dataset.md;
    if (!kind) return;
    const { cam } = mdCamera();
    if (kind === 'z' && cam) mdOnZone(t, cam);
    else if (kind === 'g' && cam) mdOnGroup(t, cam);
    else if (kind === 'c' && cam) mdOnCamera(t, cam);
    else if (kind === 'p') mdOnPlc(t);
    else if (kind === 's') mdOnScenario(t);
  };
  body?.addEventListener('change', (e) => { if (e.target.tagName !== 'BUTTON') dispatch(e); });
  body?.addEventListener('click', (e) => { if (e.target.tagName === 'BUTTON' && e.target.dataset.md) dispatch(e); });
  body?.addEventListener('change', (e) => { if (e.target.id === 'geType') mdSyncGroupEditor(); });

  // окно разметки
  const cv = mdEl('mmCanvas');
  cv?.addEventListener('click', (e) => {
    const m = MD.mark;
    if (!m || m.closed) return;
    m.pts.push(mdMarkPoint(e));
    mdMarkUi();
  });
  cv?.addEventListener('mousemove', (e) => { if (MD.mark && !MD.mark.closed) { MD.mark.cursor = mdMarkPoint(e); mdDrawMark(); } });
  cv?.addEventListener('contextmenu', (e) => { e.preventDefault(); if (MD.mark && !MD.mark.closed) { MD.mark.pts.pop(); mdMarkUi(); } });
  mdEl('mmImg')?.addEventListener('load', mdDrawMark);
  mdEl('mmUndo')?.addEventListener('click', () => { if (MD.mark && !MD.mark.closed) { MD.mark.pts.pop(); mdMarkUi(); } });
  mdEl('mmCancel')?.addEventListener('click', closeMarkModal);
  mdEl('mmX')?.addEventListener('click', closeMarkModal);
  mdEl('mmClose')?.addEventListener('click', mdMarkClose);
  mdEl('mmOk')?.addEventListener('click', mdMarkSave);
  document.addEventListener('keydown', (e) => {
    if (!MD.mark) return;
    if (e.key === 'Escape') closeMarkModal();
    else if (e.key === 'Enter' && !MD.mark.closed && !/INPUT|SELECT|TEXTAREA/.test((e.target || {}).tagName || '')) mdMarkClose();
    else if (e.key === 'Enter' && MD.mark.closed) mdMarkSave();
  });
  window.addEventListener('resize', mdDrawMark);
  window.addEventListener('beforeunload', (e) => { if (MV.dirty) { e.preventDefault(); e.returnValue = ''; } });
}

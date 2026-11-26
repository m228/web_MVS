// ---------- КЗ: звук камеры в раскрытой карточке — колонка «Звук», график уровня с этапами, карточка камеры без окон ----------
// Микрофон у камеры один: колонка во всех её окнах показывает одно и то же вычисление «этапы по звуку» (группа kiln_sound).
// Уровень по секундам и этап копит сервис движения (data\motion.db) и отдаёт в /api/motion/history полем sound.
const MD_SND_STAGE = ['ожидание', 'сброс', 'засыпка', 'долгий шум'];          // номер этапа из истории звука
const MD_SND_COLOR = ['#64748b', '#f59e0b', '#4ade80', '#a78bfa'];
const MD_SND_KEY = { drop: 1, load: 2, noise: 3 };                            // этап из состояния камеры → тот же номер
const MD_SND_THR = 30;
const MD_SND_HELP = 'Звук камеры: уровень в полосе камней (2–8 кГц) над фоном. Громче порога не меньше 1 с — событие: первое — сброс, следующее через 8–45 с — засыпка. Картинка не нужна — работает и ночью. Первые 30 с слушает фон.\n' +
  'Микрофон у камеры один: настройка общая для всех её окон. В ПЛК этап пишет сценарий (вкладка «ПЛК»), коды этапов — там же, в вычислении «этапы по звуку».\n' +
  'График — по секундам, пунктир — порог; полоска под ним: жёлтый — сброс, зелёный — засыпка, фиолетовый — долгий шум, серый — ожидание. Хранится вместе с трендами.';
const MD_SND_LEGEND = `<span class="md-snd-lg">${[1, 2].map((i) => `<span><i style="background:${MD_SND_COLOR[i]}"></i>${MD_SND_STAGE[i]}</span>`).join('')}</span>`;

const mdSndGroup = (cam) => ((cam && cam.groups) || []).find((g) => g.type === 'kiln_sound') || null;
// окна — зоны с карточкой «движение / яркость»; палки, уровнемеры, стёкла уровня и стаканы окнами не считаются
const mdHasWindows = (cam) => cam.zones.some((z) => !['cycle', 'level', 'clarity'].includes(z.type) && !ggIs(z));
// отдельная карточка звука — у камеры без окон: в общем виде только когда звук включён, у выбранной камеры всегда (чтобы было где включить)
const mdSndOwnCard = (cam, all) => !mdHasWindows(cam) && (!all || !!mdSndGroup(cam));

function mdSndSwitch(cam, a) {
  return `<div class="md-zr__sw"><label class="md-lbl" title="Слушать звук камеры и считать этапы печи по звуку"><input type="checkbox" ${a} data-f="on"${mdSndGroup(cam) ? ' checked' : ''} /> <span class="md-zr__t">Звук</span></label>${mdHelp(MD_SND_HELP)}</div>`;
}

// колонка «Звук»: галочка в заголовке, порог, текущий уровень и этап, счётчики, ошибка
function mdSndColHtml(cam) {
  const g = mdSndGroup(cam), c = escapeHtml(cam.id);
  const a = `data-md="snd" data-cam="${c}"`;
  const v = (name) => `data-sv="${name}" data-sndcam="${c}"`;
  return `<div class="md-zr__col md-sndcol">${mdSndSwitch(cam, a)}` + (g ? `
        <div class="md-zone__row"><span class="md-lbl" title="На сколько звук должен быть громче фона, чтобы это было событием">порог</span>${mdNum(a + ' data-f="thr"', g.threshold_db || MD_SND_THR, 'min="10" max="60" step="1"')}<span class="md-lbl">дБ над фоном</span></div>
        <div class="md-zone__row"><span class="md-snd-pill" ${v('pill')}>–</span><span class="md-lbl" ${v('lvl')}></span></div>
        <div class="md-gl md-gl--wide"><i ${v('bar')}></i></div>
        <div class="md-snd-cnt" ${v('cnt')}></div>
        <div class="md-snd-err" ${v('err')}></div>` : '') + '</div>';
}

// полоска «звук» в свёрнутой шапке — рядом с «движ.» и «ярк.»
function mdSndGlHtml(cam) {
  if (!mdSndGroup(cam)) return '';
  const c = escapeHtml(cam.id);
  return `<div class="md-gl" title="Звук камеры, дБ над фоном; метка — порог"><span>звук</span><i data-sv="bar" data-sndcam="${c}"></i><em data-sv="num" data-sndcam="${c}">–</em></div>`;
}

const mdSndChartHtml = (cam, id) => mdBigChartHtml('sound', id, 'звук', `data-sndchart="${escapeHtml(cam.id)}"`, MD_SND_LEGEND);

// камера без окон: выключенный звук — одна строка с галочкой, включённый — такая же раскрывающаяся карточка, как у окна
function mdSndCardHtml(cam) {
  const c = escapeHtml(cam.id), k = 'snd~' + c;                 // «~» в id зон не бывает — с карточкой окна не совпадёт
  if (!mdSndGroup(cam)) return `<div class="md-zr md-snd-off"><div class="md-zr__col md-sndcol">${mdSndSwitch(cam, `data-md="snd" data-cam="${c}"`)}</div></div>`;
  const open = MD.openAll || MD.open[k];
  return `<div class="md-zr md-zr--snd${open ? ' is-open' : ''}" id="md-zr-${k}" data-st="i">
    <div class="md-zr__head" data-zt="${k}" title="Нажмите, чтобы раскрыть настройки звука">
      <span class="md-zr__bar"></span>
      <div class="md-zr__nm"><b>Звук камеры</b><span class="md-zr__pill" data-sv="head" data-sndcam="${c}">–</span></div>
      <div class="md-zr__gz">${mdSndGlHtml(cam)}</div>
      <div class="md-zone__chart"><canvas id="md-ch-${k}" data-mini="1" data-sndchart="${c}" height="40"></canvas></div>
      <span class="md-zr__chev">⌄</span>
    </div>
    <div class="md-zr__det md-zr__det--snd">${mdSndColHtml(cam)}<div class="md-zr__charts">${mdSndChartHtml(cam, 'md-sch-' + k)}</div></div>
  </div>`;
}

// ---------- галочка и порог ----------
function mdOnSound(t, cam) {
  const g = mdSndGroup(cam);
  if (t.dataset.f === 'thr') {
    if (!g) return;
    const v = parseFloat(t.value);
    const thr = Number.isFinite(v) ? Math.min(60, Math.max(10, v)) : MD_SND_THR;
    mdEdit(() => { g.threshold_db = thr; }, false);
    document.querySelectorAll('#mdBody input[data-md="snd"][data-f="thr"]').forEach((i) => { if (i.dataset.cam === cam.id) i.value = thr; });
    mdSndPaint(cam);
    mdDrawTrends();
    return;
  }
  if (t.checked) {
    if (g) return;
    let n = 1;
    while ((cam.groups || []).some((q) => q.id === 'g' + n)) n += 1;
    if (!mdHasWindows(cam)) MD.open['snd~' + cam.id] = true;
    mdEdit(() => {
      cam.groups = (cam.groups || []).concat([{ id: 'g' + n, name: 'Этапы по звуку', type: 'kiln_sound', zones: [], out: null,
        threshold_db: MD_SND_THR, codes: Object.fromEntries(MD_SOUND_STAGES.map(([k, , d]) => [k, d])) }]);
    }, true);
    return;
  }
  if (!g) return;
  const uses = (x) => x.camera === cam.id && x.group === g.id;
  const used = ((MV.cfg && MV.cfg.scenarios) || []).filter((s) => s.sources.some(uses));
  if (used.length && !confirm('Звук этой камеры берёт сценарий «' + used.map((s) => s.name).join('», «') + '». Выключить «Звук»? Вычисление будет убрано из сценария: вместо этапа он будет писать в ПЛК 0.')) {
    t.checked = true;
    return;
  }
  delete MD.open['snd~' + cam.id];
  mdEdit(() => {
    cam.groups = cam.groups.filter((q) => q !== g);
    (MV.cfg.scenarios || []).forEach((s) => { s.sources = s.sources.filter((x) => !uses(x)); });
  }, true);
}

// ---------- живые значения ----------
function mdSndPaint(cam) {
  const g = mdSndGroup(cam);
  if (!g) return;
  const res = (MV.states[cam.id] || {}).result;
  const s = MV.online ? ((res && res.groups) || []).find((q) => q.id === g.id && q.type === 'kiln_sound') : null;   // без сервиса этап в памяти устарел
  const thr = g.threshold_db || MD_SND_THR;
  const live = !!(s && s.stage);
  const level = live && s.level != null ? Number(s.level) : null;
  const code = live ? MD_SND_KEY[s.stage] || 0 : 0;
  const stage = !s ? (MV.online ? 'ждём сервис' : 'нет данных') : s.stage_name;
  const color = code ? MD_SND_COLOR[code] : 'var(--muted)';
  const gauge = mdGauge(level == null ? 0 : level, thr, thr, 0, Math.max(60, thr + 15), color);
  const text = {
    num: level == null ? '–' : String(Math.round(level)),
    pill: live ? stage + ' · ' + mdKilnTime(s.since_s) : stage,
    head: stage,
    lvl: level == null ? (live ? 'копится фон' : '') : Math.round(level) + ' дБ над фоном',
    cnt: s ? 'сбросов ' + s.drops + ' · засыпок ' + s.loads + (s.drop_ago_s == null ? '' : ' · сброс ' + mdKilnTime(s.drop_ago_s) + ' назад') : '',
    err: !MV.online ? 'сервис КЗ не запущен' : s && !live ? s.error || 'нет звука' : '',
  };
  document.querySelectorAll('#mdBody [data-sv]').forEach((el) => {
    if (el.dataset.sndcam !== cam.id) return;
    const name = el.dataset.sv;
    if (name === 'bar') el.innerHTML = gauge;
    else el.textContent = text[name];
    if (name === 'pill') el.dataset.s = live ? String(code) : 'off';
    if (name === 'head') el.closest('.md-zr').dataset.st = !live ? 'e' : code === 3 ? 'p' : code ? 'm' : 'i';
  });
}

// ---------- график ----------
// все графики звука этой камеры (в карточках окон и в её собственной); h — ответ /api/motion/history, end — правый край «сейчас»
function mdDrawSound(cam, h, end) {
  const g = mdSndGroup(cam);
  if (!g) return;
  // сервис уже считает эту группу, а истории в ответе нет — значит он старый; пока настройка только сохраняется — просто ждём
  const accepted = !MV.dirty && ((((MV.states[cam.id] || {}).result || {}).groups) || []).some((q) => q.id === g.id && q.type === 'kiln_sound');
  const missing = accepted ? 'истории звука нет — обновите сервис движения' : 'ждём сервис…';
  document.querySelectorAll('#mdBody canvas[data-sndchart]').forEach((cv) => {
    if (cv.dataset.sndchart === cam.id) mdDrawSoundChart(cv, g.threshold_db || MD_SND_THR, h.sound, end, missing);
  });
}

// уровень над фоном (корзина — максимум), пунктир порога, под графиком — полоска этапов; snd нет — пишем missing
function mdDrawSoundChart(cv, thr, snd, end, missing) {
  const w = cv.clientWidth, mini = !!cv.dataset.mini, hh = cv.height || 80, strip = 7;
  if (!w) return;
  if (cv.width !== w) cv.width = w;
  const g = cv.getContext('2d');
  g.clearRect(0, 0, w, hh);
  cv._hv = null;
  const win = MD.win, plotH = hh - strip - (mini ? 3 : 13);
  const t = (snd && snd.t) || [], vals = (snd && snd.level) || [], stage = (snd && snd.stage) || [], n = t.length;
  const lo = -10, hi = Math.ceil(Math.max(60, thr + 15, ...vals) / 10) * 10;
  const Y = (v) => plotH - ((Math.max(lo, v) - lo) / (hi - lo)) * (plotH - 6) - 1;
  g.fillStyle = '#94a3b8'; g.font = '10px sans-serif';
  if (!mini) {
    g.fillText(hi + ' дБ', 3, 10);
    g.fillText('−' + mdFmtWin(win), 3, hh - 1);
    g.textAlign = 'right'; g.fillText('сейчас', w - 3, hh - 1); g.textAlign = 'left';
  }
  const note = !snd ? missing : n ? '' : 'за это время звука нет';
  if (note) { g.textAlign = 'center'; g.fillText(note, w / 2, plotH / 2 + 3); g.textAlign = 'left'; }
  if (!snd) return;
  g.setLineDash([4, 3]); g.lineWidth = 1; g.strokeStyle = '#ef4444';
  g.beginPath(); g.moveTo(0, Y(thr)); g.lineTo(w, Y(thr)); g.stroke();
  g.setLineDash([]);
  if (!n) return;
  const tEnd = end || t[n - 1], step = snd.step || 1, gap = Math.max(step * 3, 5);
  const X = (ts) => w - 1 - ((tEnd - ts) / win) * (w - 2);
  const bw = Math.max(1.5, (step / win) * w + 1);
  for (let i = 0; i < n; i += 1) {
    const x = X(t[i]) - bw;
    if (stage[i]) { g.globalAlpha = 0.18; g.fillStyle = MD_SND_COLOR[stage[i]]; g.fillRect(x, 0, bw, plotH); g.globalAlpha = 1; }
    g.globalAlpha = stage[i] ? 1 : 0.45; g.fillStyle = MD_SND_COLOR[stage[i]] || MD_SND_COLOR[0];
    g.fillRect(x, plotH + 3, bw, strip);
    g.globalAlpha = 1;
  }
  g.strokeStyle = '#38bdf8'; g.lineWidth = 1.6; g.beginPath();
  vals.forEach((v, i) => (i && t[i] - t[i - 1] <= gap ? g.lineTo(X(t[i]), Y(v)) : g.moveTo(X(t[i]), Y(v))));
  g.stroke();
  cv._hv = { t, vals, ok: t.map(() => 1), win, w, tEnd, dt: step,                 // подсказка при наведении (mdOnChartHover)
    tip: (i) => 'звук <b>' + Math.round(vals[i]) + ' дБ</b> · ' + (MD_SND_STAGE[stage[i]] || '') };
}

// ---------- КЗ: блок «Яркость» зоны — обучение порогов «есть / нет» и высота графиков в раскрытой карточке ----------
// Принцип «к какому состоянию ближе»: оператор записывает, как зона выглядит, когда материал (камень, жом) есть, и когда его нет;
// пороги ставятся посередине между двумя наборами. Что светлее — всё равно: «инверсия» выставляется сама.
// Применяет человек кнопкой. Записи лежат в браузере: два состояния могут быть разнесены на часы, перезагрузка страницы их не теряет.
const MD_BR_KEY = 'mdBrightLearn';
const MD_BR_PH = { yes: 'есть', no: 'нет' };
const MD_BR_FLOOR = { lum: 3, rel: 0.03, tex: 0.3 };   // разброс меньше этого считаем таким: иначе ровная картинка даёт «отличный запас» на копеечной разнице
const MD_BR_GOOD = 1;            // зазор между наборами не меньше разброса внутри набора — «годится»
const MD_BR_WEAK = 0.3;          // меньше — «почти не различить»
const MDB = { dur: 60, z: mdBrLoad() };   // z: «камера-зона» → { sig, yes: [..], no: [..], phase, until, lastTs }

function mdBrLoad() {
  try {
    const d = JSON.parse(localStorage.getItem(MD_BR_KEY)) || {};
    Object.values(d).forEach((L) => { L.phase = null; });
    return d;
  } catch (err) { return {}; }
}
function mdBrSave() {
  try { localStorage.setItem(MD_BR_KEY, JSON.stringify(MDB.z)); } catch (err) { /* не критично */ }
}

// записи годятся, только пока зона та же: другой признак или другое место — это другие числа
const mdBrSig = (z) => z.fill_metric + ' ' + JSON.stringify(z.points);
function mdBrGet(cam, z) {
  const L = MDB.z[cam.id + '-' + z.id];
  return L && L.sig === mdBrSig(z) ? L : null;
}

// yes / no — значения признака при «есть» и «нет». null — данных мало; ok=false — наборы перекрываются, пороги не посчитать
function mdBrCalc(yes, no, metric) {
  if (!yes || !no || yes.length < MD_LEARN_MIN || no.length < MD_LEARN_MIN) return null;
  const my = mdPct(yes, 50), mn = mdPct(no, 50);
  const invert = my < mn;                                  // «есть» темнее, чем «нет»
  const low = mdPct(invert ? yes : no, 95), high = mdPct(invert ? no : yes, 5);   // ближние друг к другу края наборов
  if (!(high > low)) return { my, mn, ok: false };
  const gap = high - low, d = mdLearnDigits(high, gap);
  const off = mdLearnRound(low + 0.4 * gap, d);            // середина зазора ± десятая часть: «к какому ближе» без дребезга
  let on = mdLearnRound(low + 0.6 * gap, d);
  if (on <= off) on = mdLearnRound(off + 10 ** -d, d);
  const spread = Math.max(mdPct(yes, 95) - mdPct(yes, 5), mdPct(no, 95) - mdPct(no, 5), MD_BR_FLOOR[metric] || 0);
  const ratio = gap / Math.max(spread, 1e-9);
  return { my, mn, on, off, invert, ok: true, grade: ratio >= MD_BR_GOOD ? 'good' : ratio >= MD_BR_WEAK ? 'weak' : 'bad' };
}

const mdBrDone = (L, ph) => !!(L && L.phase !== ph && L[ph] && L[ph].length >= MD_LEARN_MIN);
function mdBrBtnText(L, ph) {
  if (L && L.phase === ph) return '■ ' + Math.max(0, Math.ceil((L.until - Date.now()) / 1000)) + ' с';
  return (mdBrDone(L, ph) ? '✓ ' : '') + MD_BR_PH[ph] + ' ▶';
}

// строка «обучить» и место под результат — внутри блока «Яркость»
function mdBrightHtml(cam, z, a) {
  const k = escapeHtml(cam.id + '-' + z.id), L = mdBrGet(cam, z);
  const btn = (ph) => {
    const rec = L && L.phase === ph;
    return `<button type="button" class="btn-secondary md-lbtn${rec ? ' is-rec' : ''}${mdBrDone(L, ph) ? ' is-done' : ''}" ${a} data-f="b${ph}" id="md-bb-${ph}-${k}"` +
      ` title="${rec ? 'Остановить запись' : `Записать, как выглядит зона, когда материал ${ph === 'yes' ? 'ЕСТЬ' : 'отсутствует'}. Начинайте, когда это состояние держится уже с полминуты, и держите его, пока идёт отсчёт`}">${mdBrBtnText(L, ph)}</button>`;
  };
  return `<div class="md-zone__row md-learn"><span class="md-lbl" title="Запишите оба состояния — «есть» и «нет», порядок любой. Программа поставит пороги посередине между ними и сама решит, нужна ли инверсия">обучить</span>` +
    `${btn('yes')}${btn('no')}<select ${a} data-f="bdur" title="Сколько записывать каждое состояние">${mdSel(MD_LEARN_DURS, MDB.dur)}</select></div>` +
    `<div class="md-learn__res" id="md-br-${k}">${mdBrResHtml(cam, z, a)}</div>`;
}

function mdBrResHtml(cam, z, a) {
  const L = mdBrGet(cam, z);
  if (!L || (!L.yes && !L.no)) return '';
  if (L.phase) return `<span class="md-learn__msg">идёт запись «${MD_BR_PH[L.phase]}»: значений ${L[L.phase].length}…</span>`;
  const reset = `<button type="button" class="btn-secondary md-x md-learn__reset" ${a} data-f="breset" title="Забыть записи и начать заново">сбросить</button>`;
  const few = ['yes', 'no'].find((ph) => L[ph] && L[ph].length < MD_LEARN_MIN);
  if (few) {
    return `<span class="md-learn__msg md-learn--bad">«${MD_BR_PH[few]}»: записано всего ${L[few].length} значений (нужно ≥ ${MD_LEARN_MIN}) — камера молчала, шла медленно или панель была закрыта. Запишите ещё раз или дольше.</span>${reset}`;
  }
  const miss = ['yes', 'no'].filter((ph) => !L[ph]);
  if (miss.length) return `<span class="md-learn__msg">записано «${MD_BR_PH[miss[0] === 'yes' ? 'no' : 'yes']}». Теперь запишите «${MD_BR_PH[miss[0]]}».</span>${reset}`;
  const m = z.fill_metric, r = mdBrCalc(L.yes, L.no, m);
  const vals = `есть ≈ ${mdLearnFmt(r.my)} · нет ≈ ${mdLearnFmt(r.mn)}`;
  if (!r.ok) {
    return `<span class="md-learn__msg md-learn--bad">${vals}: значения перекрываются, по этому признаку «есть» и «нет» не различить. Попробуйте другой признак, сдвиньте зону или проверьте экспозицию.</span>${reset}`;
  }
  const word = { good: 'годится', weak: 'слабо: возможны ложные срабатывания', bad: 'почти не различить' }[r.grade];
  const same = z.fill_on === r.on && z.fill_off === r.off && !!z.fill_invert === r.invert;
  return `<div class="md-learn__line md-learn--${r.grade}"><span>${vals} — ${word}</span></div><div class="md-learn__line">` + (same
    ? `<span class="md-learn--good">✓ пороги стоят: ▲ ${mdLearnNum(r.on)}, ▼ ${mdLearnNum(r.off)}</span>`
    : `<span>порог ▲ <b>${mdLearnNum(r.on)}</b>, ▼ <b>${mdLearnNum(r.off)}</b>${r.invert ? ', с инверсией («есть» темнее)' : ''}</span>` +
      `<button type="button" class="btn-primary md-learn__apply" ${a} data-f="bapply" title="Поставить эти пороги зоне">Применить</button>`) + `${reset}</div>`;
}

// кнопки и список строки «обучить» блока «Яркость»; true — поле обработано здесь
function mdOnBright(t, cam, z) {
  const f = t.dataset.f;
  if (!['byes', 'bno', 'bdur', 'bapply', 'breset'].includes(f)) return false;
  const k = cam.id + '-' + z.id;
  if (f === 'bdur') { MDB.dur = Number(t.value) || 60; return true; }
  if (f === 'breset') { delete MDB.z[k]; mdBrSave(); mdBrRepaint(cam, z); return true; }
  if (f === 'byes' || f === 'bno') {
    const ph = f.slice(1);
    let L = mdBrGet(cam, z);
    if (!L) L = MDB.z[k] = { sig: mdBrSig(z), yes: null, no: null, phase: null, until: 0, lastTs: 0 };
    if (L.phase === ph) L.phase = null;                    // повторное нажатие — стоп, что успели — оставляем
    else Object.assign(L, { phase: ph, until: Date.now() + MDB.dur * 1000, lastTs: 0, [ph]: [] });
    mdBrSave();
    mdBrRepaint(cam, z);
    return true;
  }
  const L = mdBrGet(cam, z), r = L && mdBrCalc(L.yes, L.no, z.fill_metric);
  if (r && r.ok) mdEdit(() => { z.fill_on = r.on; z.fill_off = r.off; z.fill_invert = r.invert; }, true);
  return true;
}

function mdBrRepaint(cam, z) {
  const k = cam.id + '-' + z.id, L = mdBrGet(cam, z);
  ['yes', 'no'].forEach((ph) => {
    const b = mdEl('md-bb-' + ph + '-' + k);
    if (!b) return;
    b.textContent = mdBrBtnText(L, ph);
    b.classList.toggle('is-rec', !!(L && L.phase === ph));
    b.classList.toggle('is-done', mdBrDone(L, ph));
  });
  const res = mdEl('md-br-' + k);
  if (res) res.innerHTML = mdBrResHtml(cam, z, `data-md="z" data-cam="${escapeHtml(cam.id)}" data-i="${cam.zones.indexOf(z)}"`);
}

// каждый опрос состояния: идёт запись — берём новое значение признака (только кадры в норме и только новые)
function mdBrightTick(cam, res) {
  cam.zones.forEach((z) => {
    const L = mdBrGet(cam, z);
    if (!L || !L.phase) return;
    const zl = res && res.alive && !res.corrupt ? (res.zones || []).find((q) => q.id === z.id) : null;
    if (Date.now() >= L.until) L.phase = null;             // время вышло (панель была закрыта) — значение уже из чужого состояния
    else if (zl && zl.fill_metric === z.fill_metric && !MV.dirty && res.ts && res.ts !== L.lastTs) {   // сервис уже считает выбранный признак
      L.lastTs = res.ts;
      L[L.phase].push(Number(zl[z.fill_metric]) || 0);
    }
    mdBrSave();
    mdBrRepaint(cam, z);
  });
}

// ---------- высота графиков в раскрытой карточке: тянем мышью за полоску под графиком, общая для всех зон, запоминается ----------
const MD_CH_KEY = 'mdChartH';
const MD_CH_MIN = 48, MD_CH_MAX = 360, MD_CH_DEF = 80;
const mdClampH = (v) => Math.max(MD_CH_MIN, Math.min(MD_CH_MAX, Math.round(v)));
const MD_CH = mdLoadChartH();

function mdLoadChartH() {
  let d = {};
  try { d = JSON.parse(localStorage.getItem(MD_CH_KEY)) || {}; } catch (err) { /* не критично */ }
  return { motion: mdClampH(Number(d.motion) || MD_CH_DEF), fill: mdClampH(Number(d.fill) || MD_CH_DEF) };
}

// график раскрытой карточки: kind — motion | fill; колесо мыши и подсказка работают как у остальных (.md-zone__chart)
function mdBigChartHtml(kind, id, title) {
  return `<div class="md-zc"><div class="md-zr__t">${title}</div>` +
    `<div class="md-zone__chart"><canvas id="${id}" data-h="${kind}" height="${MD_CH[kind]}" style="height:${MD_CH[kind]}px"></canvas></div>` +
    `<div class="md-grip" data-grip="${kind}" title="Потяните вверх или вниз — высота графика «${title}», запоминается. Двойной щелчок — как было"></div></div>`;
}

function mdSetChartH(body, kind, h, done) {
  MD_CH[kind] = mdClampH(h);
  body.querySelectorAll('canvas[data-h="' + kind + '"]').forEach((c) => {
    c.style.height = MD_CH[kind] + 'px';
    if (done) c.height = MD_CH[kind];            // пока тянут — картинка растягивается стилем, перерисуем, когда отпустят
  });
  if (!done) return;
  try { localStorage.setItem(MD_CH_KEY, JSON.stringify(MD_CH)); } catch (err) { /* не критично */ }
  mdDrawTrends();
}

function mdBindGrips(body) {
  body.addEventListener('pointerdown', (e) => {
    const grip = e.target.closest && e.target.closest('[data-grip]');
    if (!grip || e.button !== 0) return;
    e.preventDefault();
    const kind = grip.dataset.grip, y0 = e.clientY, h0 = MD_CH[kind];
    const move = (ev) => mdSetChartH(body, kind, h0 + ev.clientY - y0, false);
    const up = (ev) => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      mdSetChartH(body, kind, ev.type === 'pointerup' ? h0 + ev.clientY - y0 : MD_CH[kind], true);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  });
  body.addEventListener('dblclick', (e) => {
    const grip = e.target.closest && e.target.closest('[data-grip]');
    if (grip) mdSetChartH(body, grip.dataset.grip, MD_CH_DEF, true);
  });
}

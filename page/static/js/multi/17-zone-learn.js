// ---------- КЗ: обучение порогов зоны «стоит / движется» ----------
// Оператор держит зону в покое и жмёт «стоит ▶», потом в движении — «движется ▶». Пока идёт запись, складываем значения зоны из
// опроса /api/motion/state (сглаженные diff и flow — ровно то, что сравнивается с порогами), по двум наборам считаем on/off и запас.
// Применяет человек кнопкой: одно кривое обучение не должно молча сломать выход в ПЛК. Сервис движения ничего об обучении не знает.
const MD_LEARN_DURS = [[30, '30 с'], [60, '1 мин'], [120, '2 мин']];
const MD_LEARN_MIN = 8;          // меньше значений в наборе — камера молчала, считать не по чему
const MD_LEARN_WEAK = 1.5;       // запас ниже — «почти не различить» (применить можно, решение Макса)
const MD_LEARN_GOOD = 3;         // запас выше — «годится»
const MD_LEARN_BETTER = 1.3;     // другая метрика разделяет во столько раз лучше — подсказываем переключиться
const MD_LEARN_PH = { still: 'стоит', move: 'движется' };
const MDL = { dur: 30, z: {} };  // z: «камера-зона» → { phase, until, lastTs, still: {diff, flow}, move: {diff, flow} }

// перцентиль p (0–100) с линейной интерполяцией
function mdPct(arr, p) {
  const s = arr.slice().sort((a, b) => a - b);
  if (!s.length) return 0;
  const x = (s.length - 1) * p / 100, i = Math.floor(x), f = x - i;
  return i + 1 < s.length ? s[i] + (s[i + 1] - s[i]) * f : s[i];
}

// знаков после запятой для порога: по величине (12.3 / 4.07 / 0.635), но не грубее десятой доли зазора «стоит — движется»,
// иначе при узком зазоре округление выносит on за сигнал и зона не включится никогда
function mdLearnDigits(v, gap) {
  const byV = v >= 10 ? 1 : v >= 1 ? 2 : 3;
  return Math.min(4, Math.max(byV, Math.ceil(-Math.log10(Math.max(gap, 1e-9) / 10))));
}
const mdLearnRound = (v, d) => Math.round(v * 10 ** d) / 10 ** d;

// Шум = p95 «стоит», сигнал = p10 «движется». off чуть выше шума (четверть зазора), on — посередине.
// null — данных мало; ok=false — движение не выше покоя (перепутаны кнопки или зона не видит движения), пороги не посчитать.
function mdLearnCalc(still, move) {
  if (!still || !move || still.length < MD_LEARN_MIN || move.length < MD_LEARN_MIN) return null;
  const noise = mdPct(still, 95), sig = mdPct(move, 10);
  const ratio = sig / Math.max(noise, 1e-6);
  if (!(sig > noise)) return { noise, sig, ratio, ok: false };
  const gap = sig - noise, d = mdLearnDigits(sig, gap);
  const off = mdLearnRound(noise + 0.25 * gap, d);
  let on = mdLearnRound(noise + 0.5 * gap, d);
  if (on <= off) on = mdLearnRound(off + 10 ** -d, d);
  return { noise, sig, ratio, on, off, ok: true, grade: ratio < MD_LEARN_WEAK ? 'bad' : ratio < MD_LEARN_GOOD ? 'weak' : 'good' };
}

const mdLearnSet = () => ({ diff: [], flow: [] });
function mdLearnGet(k) {
  if (!MDL.z[k]) MDL.z[k] = { phase: null, until: 0, lastTs: 0, still: null, move: null };
  return MDL.z[k];
}

// строка «обучить» и место под результат — внутри блока «Движение» раскрытой строки зоны
function mdLearnHtml(cam, z, a) {
  const k = cam.id + '-' + z.id, L = MDL.z[k];
  const btn = (ph) => {
    const rec = L && L.phase === ph;
    return `<button type="button" class="btn-secondary md-lbtn${rec ? ' is-rec' : ''}${mdLearnDone(L, ph) ? ' is-done' : ''}" ${a} data-f="l${ph}" id="md-lb-${ph}-${escapeHtml(k)}"` +
      ` title="${rec ? 'Остановить запись' : `Записать, как зона выглядит, когда ${MD_LEARN_PH[ph] === 'стоит' ? 'всё стоит' : 'идёт движение'}. Держите это состояние, пока идёт отсчёт`}">${mdLearnBtnText(L, ph)}</button>`;
  };
  return `<div class="md-zone__row md-learn"><span class="md-lbl" title="Автоподбор порогов: запишите покой и движение, программа посчитает on/off и покажет, насколько уверенно зона их различает. Порядок записи любой">обучить</span>` +
    `${btn('still')}${btn('move')}<select ${a} data-f="ldur" title="Сколько записывать каждое состояние">${mdSel(MD_LEARN_DURS, MDL.dur)}</select></div>` +
    `<div class="md-learn__res" id="md-lr-${escapeHtml(k)}">${mdLearnResHtml(cam, z, a)}</div>`;
}

// запись состояния есть и годится для расчёта (✓ на кнопке)
const mdLearnDone = (L, ph) => !!(L && L.phase !== ph && L[ph] && L[ph].diff.length >= MD_LEARN_MIN);

function mdLearnBtnText(L, ph) {
  if (L && L.phase === ph) return '■ ' + Math.max(0, Math.ceil((L.until - Date.now()) / 1000)) + ' с';
  return (mdLearnDone(L, ph) ? '✓ ' : '') + MD_LEARN_PH[ph] + ' ▶';
}

const mdLearnFmt = (v) => (v >= 10 ? v.toFixed(1) : v >= 1 ? v.toFixed(2) : v.toFixed(3)).replace('.', ',');
const mdLearnNum = (v) => String(v).replace('.', ',');     // порог — как посчитан, без потери знаков
const mdLearnX = (r) => (r > 99 ? '> 99' : r.toFixed(1).replace('.', ','));

function mdLearnResHtml(cam, z, a) {
  const L = MDL.z[cam.id + '-' + z.id];
  if (!L || (!L.still && !L.move)) return '';
  if (L.phase) return `<span class="md-learn__msg">идёт запись «${MD_LEARN_PH[L.phase]}»: значений ${L[L.phase].diff.length}…</span>`;
  const reset = `<button type="button" class="btn-secondary md-x md-learn__reset" ${a} data-f="lreset" title="Забыть записи и начать заново">сбросить</button>`;
  const few = ['still', 'move'].find((ph) => L[ph] && L[ph].diff.length < MD_LEARN_MIN);
  if (few) {
    return `<span class="md-learn__msg md-learn--bad">«${MD_LEARN_PH[few]}»: записано всего ${L[few].diff.length} значений (нужно ≥ ${MD_LEARN_MIN}) — камера молчала, поток битый или панель была закрыта. Запишите ещё раз.</span>`;
  }
  const miss = ['still', 'move'].filter((ph) => !L[ph]);
  if (miss.length) return `<span class="md-learn__msg">записано «${MD_LEARN_PH[miss[0] === 'still' ? 'move' : 'still']}». Теперь запишите «${MD_LEARN_PH[miss[0]]}».</span>`;
  const m = z.metric, r = mdLearnCalc(L.still[m], L.move[m]);     // не null: недобор значений отсеян выше
  if (!r.ok) {
    return `<span class="md-learn__msg md-learn--bad">${m}: в «движется» значение не выше, чем в «стоит» (${mdLearnFmt(r.sig)} против ${mdLearnFmt(r.noise)}). Перепутаны записи или зона не видит движения: сдвиньте или увеличьте её.</span>${reset}`;
  }
  const word = { good: 'годится', weak: 'слабо: возможны ложные срабатывания', bad: 'почти не различить: сдвиньте или увеличьте зону, проверьте свет' }[r.grade];
  const same = z.on === r.on && z.off === r.off;
  let html = `<div class="md-learn__line md-learn--${r.grade}"><span><b>${m}</b>: стоит ≤ ${mdLearnFmt(r.noise)} · движется ≥ ${mdLearnFmt(r.sig)} · запас ×${mdLearnX(r.ratio)} — ${word}</span></div>` +
    `<div class="md-learn__line">` + (same
      ? `<span class="md-learn--good">✓ пороги стоят: ▲ ${mdLearnNum(r.on)}, ▼ ${mdLearnNum(r.off)}</span>`
      : `<span>порог ▲ ${mdLearnNum(z.on)} → <b>${mdLearnNum(r.on)}</b>, ▼ ${mdLearnNum(z.off)} → <b>${mdLearnNum(r.off)}</b></span>` +
        `<button type="button" class="btn-primary md-learn__apply" ${a} data-f="lapply" title="Поставить эти пороги зоне">Применить</button>`) + `${reset}</div>`;
  const o = m === 'diff' ? 'flow' : 'diff';
  const ro = Math.max(...L.move[o], 0) > 0 ? mdLearnCalc(L.still[o], L.move[o]) : null;     // flow у зоны не считается — одни нули, не сравниваем
  if (ro && ro.ok && ro.ratio >= MD_LEARN_WEAK && ro.ratio > r.ratio * MD_LEARN_BETTER) {
    html += `<div class="md-learn__line md-learn__hint"><span>${o} разделяет лучше: запас ×${mdLearnX(ro.ratio)} против ×${mdLearnX(r.ratio)}</span>` +
      `<button type="button" class="btn-secondary md-learn__apply" ${a} data-f="lmetric" title="Переключить зону на ${o} с порогами ▲ ${mdLearnNum(ro.on)} / ▼ ${mdLearnNum(ro.off)}">перейти на ${o}</button></div>`;
  }
  return html;
}

// кнопки и список строки «обучить»; true — поле обучения (обработано здесь)
function mdOnLearn(t, cam, z) {
  const f = t.dataset.f;
  if (!['lstill', 'lmove', 'ldur', 'lapply', 'lmetric', 'lreset'].includes(f)) return false;
  const k = cam.id + '-' + z.id, L = mdLearnGet(k);
  if (f === 'ldur') { MDL.dur = Number(t.value) || 30; return true; }
  if (f === 'lreset') { delete MDL.z[k]; mdLearnRepaint(cam, z); return true; }
  if (f === 'lstill' || f === 'lmove') {
    const ph = f.slice(1);
    if (L.phase === ph) { L.phase = null; mdLearnRepaint(cam, z); return true; }   // повторное нажатие — стоп, что успели — оставляем
    L.phase = ph;
    L.until = Date.now() + MDL.dur * 1000;
    L.lastTs = 0;
    L[ph] = mdLearnSet();
    mdLearnRepaint(cam, z);
    return true;
  }
  if (!L.still || !L.move) return true;
  const m = f === 'lmetric' ? (z.metric === 'diff' ? 'flow' : 'diff') : z.metric;
  const r = mdLearnCalc(L.still[m], L.move[m]);
  if (!r || !r.ok) return true;
  mdEdit(() => { z.metric = m; z.on = r.on; z.off = r.off; }, true);
  return true;
}

// перерисовать кнопки и результат одной зоны, не трогая остальную панель
function mdLearnRepaint(cam, z) {
  const k = cam.id + '-' + z.id, L = MDL.z[k];
  ['still', 'move'].forEach((ph) => {
    const b = mdEl('md-lb-' + ph + '-' + k);
    if (!b) return;
    b.textContent = mdLearnBtnText(L, ph);
    b.classList.toggle('is-rec', !!(L && L.phase === ph));
    b.classList.toggle('is-done', mdLearnDone(L, ph));
  });
  const res = mdEl('md-lr-' + k);
  if (res) res.innerHTML = mdLearnResHtml(cam, z, `data-md="z" data-cam="${escapeHtml(cam.id)}" data-i="${cam.zones.indexOf(z)}"`);
}

// каждый опрос состояния: идёт запись — берём новое значение зоны (только кадры в норме и только новые: опрос чаще кадров)
function mdLearnTick(cam, res) {
  Object.keys(MDL.z).forEach((k) => {
    const L = MDL.z[k];
    if (!L.phase || k.indexOf(cam.id + '-') !== 0) return;
    const z = cam.zones.find((q) => cam.id + '-' + q.id === k);
    if (!z) { delete MDL.z[k]; return; }
    const zl = res && res.alive && !res.corrupt ? (res.zones || []).find((q) => q.id === z.id) : null;
    if (zl && res.ts && res.ts !== L.lastTs) {
      L.lastTs = res.ts;
      L[L.phase].diff.push(Number(zl.diff) || 0);
      L[L.phase].flow.push(Number(zl.flow) || 0);
    }
    if (Date.now() >= L.until) L.phase = null;
    mdLearnRepaint(cam, z);
  });
}

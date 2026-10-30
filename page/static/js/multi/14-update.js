// ---------- кнопка «Обновить» ----------
// Одно нажатие: проверка версии → подтверждение → 5 с на отмену → update.ps1 (тот же апдейтер, что update.bat: сам качает релиз,
// останавливает приложение, заменяет файлы основного приложения и установленных дополнений, запускает заново). Приложение работает
// от админа, поэтому апдейтер наследует права. Новую вкладку после обновления не открываем (--no-open): эта же вкладка дожидается
// сервера и перезагружается, а 12-autostart.js по метке MU_FLAG возвращает раскладку и поднимает те камеры, что были подключены.
const MU = { busy: false, resetT: null };
const MU_COUNTDOWN_S = 5;
// Скачивание релиза (77 МБ) на объекте идёт 2–4 минуты, но бывает и ~30 (обрыв связи и повтор, update.log 30.10): таймауты — только
// страховка от молчаливого зависания, ошибку апдейтера страница ловит по update.log сразу
const MU_NO_RESTART_MS = 45 * 60 * 1000;     // если за это время приложение так и не остановилось — апдейтер не сработал
const MU_TOTAL_MS = 60 * 60 * 1000;

function muEls() {
  return { btn: document.getElementById('multiUpdateBtn'), box: document.getElementById('multiUpdateStatus') };
}

// kind: ok | warn | error; error висит, пока не нажмут снова (ошибка не должна пропадать сама)
function muSay(text, kind, opts = {}) {
  const { box } = muEls();
  if (!box) return;
  clearTimeout(MU.resetT);
  box.replaceChildren();
  if (!text) { box.hidden = true; return; }
  box.hidden = false;
  box.className = 'mu-status mu-status--' + (kind || 'warn');
  const span = document.createElement('span');
  span.textContent = text;
  box.appendChild(span);
  if (opts.cancel) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'mu-status__cancel';
    b.textContent = 'Отмена';
    b.addEventListener('click', opts.cancel);
    box.appendChild(b);
  }
  if (opts.ms) MU.resetT = setTimeout(() => muSay(''), opts.ms);
}

function muFinish() {
  MU.busy = false;
  const { btn } = muEls();
  if (btn) btn.disabled = false;
}

function muSleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// 5 секунд на отмену; true — досчитали, false — отменили
async function muCountdown(text) {
  let cancelled = false;
  for (let s = MU_COUNTDOWN_S; s > 0 && !cancelled; s--) {
    muSay(`${text} через ${s} с`, 'warn', { cancel: () => { cancelled = true; } });
    await muSleep(1000);
  }
  return !cancelled;
}

function muSaveFlag(from) {
  try {
    localStorage.setItem(MU_FLAG, JSON.stringify({
      ts: Date.now(),
      from,
      layout: state.layout,
      tiles: state.tiles.map((t) => t.serial || null),
      active: state.tiles.filter((t) => t.connected && t.serial).map((t) => t.serial),
    }));
  } catch (e) { /* без localStorage камеры подключатся только по «Автостарту» */ }
}

function muPatchFlag(patch) {
  try {
    const raw = localStorage.getItem(MU_FLAG);
    const flag = raw ? JSON.parse(raw) : null;
    if (flag) localStorage.setItem(MU_FLAG, JSON.stringify(Object.assign(flag, patch)));
  } catch (e) { /* ignore */ }
}

function muDropFlag() {
  try { localStorage.removeItem(MU_FLAG); } catch (e) { /* ignore */ }
}

async function muVersion() {
  try {
    // /api/version — лёгкий: /api/debug/info при каждом вызове пишет «Версии окружения» в журнал (раз в 3 с он забивался этим)
    const r = await fetch('/api/version', { cache: 'no-store' });
    if (!r.ok) return null;
    return (await r.json()).app || null;
  } catch (e) {
    return null;
  }
}

// ход апдейтера из update.log (строки не раньше since): последняя строка — для статуса; «ОШИБКА: …» без скобок — скрипт упал целиком;
// «--- итог ---» — скрипт дошёл до конца (summary — строки после него)
async function muLogState(since) {
  let lines = [];
  try {
    const r = await fetch('/api/update/log?since=' + since, { cache: 'no-store' });
    if (r.ok) lines = (await r.json()).lines || [];
  } catch (e) { /* сервер занят или останавливается */ }
  let failed = '';
  lines.forEach((l) => { if (l.startsWith('ОШИБКА: ')) failed = l.slice(8); });
  const end = lines.lastIndexOf('--- итог ---');
  return { failed, done: end >= 0, summary: end >= 0 ? lines.slice(end + 1).join('; ') : '', last: lines.length ? lines[lines.length - 1] : '' };
}

// ждём: сервер остановится (замена файлов) и вернётся; новая версия → перезагрузка (или «окно можно закрыть», если открылась своя вкладка)
async function muWatch(before, latest, noBrowser, since) {
  const started = Date.now();
  let wentDown = false;
  let lg = { failed: '', done: false, summary: '', last: '' };
  while (Date.now() - started < MU_TOTAL_MS) {
    await muSleep(3000);
    const v = await muVersion();
    if (v === null) {
      if (!wentDown) log.warn('Приложение остановилось для обновления');
      wentDown = true;
      muSay(`Перезапускаюсь${latest ? ' на v' + latest : ''}: камеры вернутся сами, около минуты…`, 'warn');
      continue;
    }
    lg = await muLogState(since);
    if (v !== before) {
      if (noBrowser) {
        muSay(`Готово: v${v}. Перезагружаю страницу…`, 'ok');
        await muSleep(1000);
        window.location.reload();
      } else {
        muDropFlag();
        muSay(`Обновлено до v${v}. Приложение открыло страницу в новой вкладке, это окно можно закрыть.`, 'ok');
        muFinish();
      }
      return;
    }
    if (wentDown) {
      // приложение перезапускалось, но версия прежняя — апдейтер откатился: вернуть камеры и объяснить на перезагруженной странице
      muPatchFlag({ note: `Обновление до v${latest || '?'} не удалось, возвращена v${v}.${lg.failed ? ' ' + lg.failed + '.' : ''} Подробности в update.log рядом с web_MVS.exe.` });
      if (noBrowser) window.location.reload();
      else {
        muDropFlag();
        muSay(`Обновление не удалось, возвращена v${v}.${lg.failed ? ' ' + lg.failed + '.' : ''} Подробности: update.log рядом с web_MVS.exe.`, 'error');
        muFinish();
      }
      return;
    }
    if (lg.failed) {
      // скрипт упал до остановки приложения (нет связи с GitHub, архив не скачался…): приложение работает как работало
      muDropFlag();
      muSay(`Обновление не удалось: ${lg.failed}. Приложение работает как раньше. Подробности: update.log рядом с web_MVS.exe.`, 'error');
      muFinish();
      return;
    }
    if (lg.done) {
      muDropFlag();
      muSay(`Апдейтер закончил без перезапуска${lg.summary ? ': ' + lg.summary : ''}.`, 'warn');
      muFinish();
      return;
    }
    if (Date.now() - started > MU_NO_RESTART_MS) break;
    muSay(`Обновляю${latest ? ' до v' + latest : ''}: ${lg.last || 'запускаю апдейтер'} (камеры пока работают)`, 'warn');
  }
  muDropFlag();
  muSay('Приложение так и не перезапустилось. Подробности: update.log рядом с web_MVS.exe.', 'error');
  muFinish();
}

async function onMultiUpdate() {
  if (MU.busy) return;
  MU.busy = true;
  const { btn } = muEls();
  if (btn) btn.disabled = true;
  muSay('Проверяю версию…', 'warn');

  const chk = await UpdateApi.check();
  if (!chk) { muSay('Не удалось проверить: сервер не ответил.', 'error'); muFinish(); return; }
  if (chk.error) { muSay(chk.error + ' Подробности в update.log.', 'error'); muFinish(); return; }
  if (!chk.update_available) { muSay(`Установлена v${chk.current} — последняя.`, 'ok', { ms: 4000 }); muFinish(); return; }

  // всё вместе: основное приложение + установленные дополнения (CV микроскопа, видео, КЗ RTSP)
  const comps = await UpdateApi.components();
  const keys = ['main'];
  ((comps && comps.components) || []).forEach((c) => { if (c.key !== 'main' && c.installed) keys.push(c.key); });

  if (!window.confirm(`Обновить v${chk.current} → v${chk.latest}?\n\nКамеры отключатся примерно на минуту и поднимутся сами. Страница перезагрузится.`)) {
    muSay(''); muFinish(); return;
  }
  if (!(await muCountdown(`Обновление до v${chk.latest} начнётся`))) {
    muSay('Обновление отменено.', 'ok', { ms: 3000 }); muFinish(); return;
  }

  muSaveFlag(chk.current);
  // сначала докачка: приложение берёт из релиза только изменившиеся файлы (несколько МБ вместо ~77), камеры работают.
  // Не вышла — ничего страшного: update.ps1 скачает архив целиком сам (системный прокси, повторы)
  muSay('Докачиваю изменившиеся файлы (камеры работают)…', 'warn');
  let staged = false;
  try {
    const d = await (await fetch('/api/update/download?extract=0&delta_only=1', { cache: 'no-store' })).json();
    if (d && d.ok) {
      staged = true;
      const st = d.delta || {};
      log.info('Обновление докачано', st);
      if (st.fetched && st.full_size) muSay(`Докачано ${(st.fetched / 1048576).toFixed(1)} МБ из ${(st.full_size / 1048576).toFixed(0)} МБ. Запускаю апдейтер…`, 'warn');
    }
  } catch (e) { /* докачки нет — полная загрузка в update.ps1 */ }
  if (!staged) muSay('Запускаю апдейтер (архив целиком)…', 'warn');
  const since = Math.floor(Date.now() / 1000) - 2;      // строки update.log с этого момента — наш запуск
  let r = await UpdateApi.run(keys, false, true, staged);
  if (staged && (!r || !r.ok)) r = await UpdateApi.run(keys, false, true, false);   // скачанный архив не подошёл — пусть update.ps1 скачает сам
  if (!r || !r.ok) {
    muDropFlag();
    muSay((r && r.error) || 'Не удалось запустить обновление.', 'error');
    muFinish();
    return;
  }
  log.warn('Запущено обновление', { components: r.components, no_browser: r.no_browser });
  await muWatch(chk.current, chk.latest, !!r.no_browser, since);
}

async function initMultiUpdate() {
  const { btn } = muEls();
  if (!btn) return;
  btn.addEventListener('click', onMultiUpdate);
  // после перезагрузки: сказать, чем кончилось (успех или откат)
  const p = AS.pending;
  if (!p) return;
  if (p.note) { muSay(p.note, 'error'); return; }
  const v = await muVersion();
  if (v && p.from && v !== p.from) muSay(`Обновлено: v${p.from} → v${v}.`, 'ok', { ms: 10000 });
}

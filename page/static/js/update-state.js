// Автообновление на странице: раз в 5 с спрашивает /api/update/state (кэш фонового потока, без сети и без записи в журнал),
// отдаёт состояние кнопкам «обновить / перезапустить» и рисует ОДНУ плашку, когда приложение собирается само перезапуститься
// (варки нет, обновление скачано): «перезапуск через N с · Отмена». Во время отсчёта опрос идёт раз в секунду.
// Страницы подписываются: UpdateState.on('state' | 'countdown' | 'cancel' | 'launch', fn)
//   state     — каждое свежее состояние (staged = версия, готовая к перезапуску);
//   countdown — отсчёт начался {version, seconds}: здесь страница готовится (закрыть камеру, запомнить раскладку);
//   cancel    — отсчёт снят («Отмена» или варка началась): вернуть как было;
//   launch    — сервер запустил update.ps1 (или перестал отвечать): ждать новую версию и перезагрузиться.
"use strict";

(function () {
  const POLL_MS = 5000;
  const POLL_COUNTDOWN_MS = 1000;
  const subs = { state: [], countdown: [], cancel: [], launch: [] };
  let last = null;
  let counting = false;
  let version = '';
  let deadline = 0;
  let toast = null;
  let ticker = null;
  let failStreak = 0;

  function emit(kind, arg) {
    subs[kind].forEach((fn) => { try { fn(arg); } catch (e) { /* страница сама отвечает за свой обработчик */ } });
  }

  function on(kind, fn) {
    if (!subs[kind]) return;
    subs[kind].push(fn);
    if (kind === 'state' && last) fn(last);
  }

  function ensureToast() {
    if (toast) return toast;
    toast = document.createElement('div');
    toast.className = 'upd-toast';
    toast.setAttribute('role', 'status');
    toast.hidden = true;
    const text = document.createElement('span');
    text.className = 'upd-toast__text';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'upd-toast__cancel';
    btn.textContent = 'Отмена';
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await fetch('/api/update/auto_cancel', { cache: 'no-store' }); } catch (e) { /* следующий опрос покажет, что вышло */ }
      btn.disabled = false;
    });
    toast.append(text, btn);
    document.body.appendChild(toast);
    return toast;
  }

  function paint() {
    const t = ensureToast();
    const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    t.querySelector('.upd-toast__text').textContent = `Варки нет — обновляюсь до v${version}: перезапуск через ${left} с`;
    t.hidden = false;
  }

  function hide() {
    clearInterval(ticker);
    ticker = null;
    if (toast) toast.hidden = true;
  }

  async function poll() {
    let s = null;
    try {
      const r = await fetch('/api/update/state', { cache: 'no-store' });
      if (r.ok) s = await r.json();
    } catch (e) { /* сервер занят или перезапускается */ }
    const a = s && s.auto;
    const left = a ? a.restart_in_s : null;
    failStreak = s ? 0 : failStreak + 1;
    // одиночный сбой опроса на отсчёте — не запуск: ждём ещё один ответ (сервер, который остановился, не ответит и второй раз)
    if (!s && counting && failStreak < 2) { setTimeout(poll, POLL_COUNTDOWN_MS); return; }

    if (s) {
      last = s;
      emit('state', s);
    }
    if (s && left !== null && left !== undefined) {
      version = s.staged || version;
      deadline = Date.now() + left * 1000;
      if (!counting) {
        counting = true;
        emit('countdown', { version, seconds: left });
        clearInterval(ticker);
        ticker = setInterval(paint, 250);
      }
      paint();
    } else if (counting) {
      counting = false;
      if (!s || (a && a.launched)) {
        // отсчёт дошёл до нуля: сервер запустил апдейтер (или уже остановился) — дальше страница ждёт новую версию
        hide();
        emit('launch', { version });
      } else {
        hide();
        emit('cancel', {});
      }
    }
    setTimeout(poll, counting ? POLL_COUNTDOWN_MS : POLL_MS);
  }

  window.UpdateState = { on, last: () => last };
  setTimeout(poll, 1500);
})();

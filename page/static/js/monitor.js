// Страница «Мониторинг»: пульс этого ПК (sysmon.py) — как дела, настройки отправки на сервер, журнал перезапусков и тревог.
const log = {
  info: (m, p) => window.AppLog?.info('monitor', m, p),
  warn: (m, p) => window.AppLog?.warn('monitor', m, p),
  error: (m, p) => window.AppLog?.error('monitor', m, p),
};
const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (s) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[s]));
}

function fmtTime(t) {
  return new Date(t * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return 'только что';
  if (s < 3600) return Math.round(s / 60) + ' мин назад';
  if (s < 86400) return Math.floor(s / 3600) + ' ч назад';
  return Math.floor(s / 86400) + ' д назад';
}

function levelClass(level) {
  return level === 'critical' || level === 'high' ? 'is-bad' : (level === 'medium' ? 'is-warn' : 'is-info');
}

function renderLink(send, cfg) {
  const el = $('monLink');
  let cls = 'mon-chip', text;
  if (!send.configured) { text = cfg.enabled ? 'нет адреса или токена' : 'отправка выключена'; }
  else if (send.ok === true) { cls += ' is-ok'; text = 'на связи · ' + ago(send.last_ok_t); }
  else if (send.ok === false) { cls += ' is-bad'; text = 'нет связи с сервером'; }
  else { cls += ' is-warn'; text = 'ждёт первой отправки'; }
  el.className = cls;
  el.textContent = text;

  const info = [];
  if (send.ok === false && send.error) info.push(send.error);
  if (send.pending > 0) info.push('в очереди ' + send.pending + ' событ.');
  const box = $('monSendInfo');
  box.textContent = info.join(' · ');
  box.classList.toggle('is-bad', send.ok === false);
}

function renderAlerts(alerts) {
  const box = $('monAlerts');
  if (!alerts.length) { box.innerHTML = '<span class="mon-chip is-ok">тревог нет</span>'; return; }
  box.innerHTML = alerts.map((a) =>
    `<div class="mon-alert ${levelClass(a.level)}"><b>${escapeHtml(a.text)}</b><small>с ${fmtTime(a.since)}</small></div>`).join('');
}

function renderLog(state) {
  const items = (state.starts || []).map((s) => ({ t: s.t, level: s.level, text: s.text }));
  (state.recent || []).forEach((e) => { if (e.kind !== 'start') items.push({ t: e.t, level: e.level, text: e.text }); });
  items.sort((a, b) => b.t - a.t);
  $('monLog').innerHTML = items.slice(0, 40).map((i) =>
    `<div class="mon-log__row ${levelClass(i.level)}"><span class="mon-log__t">${fmtTime(i.t)}</span><span>${escapeHtml(i.text)}</span></div>`
  ).join('') || '<div class="table-empty-state">Записей пока нет.</div>';
}

let formFilled = false;
function fillForm(cfg) {
  $('monEnabled').checked = !!cfg.enabled;
  $('monPlant').value = cfg.plant || '';
  $('monUrl').value = cfg.url || '';
  $('monToken').value = '';
  $('monToken').placeholder = cfg.token_set ? 'задан' + (cfg.token_tail ? ' · …' + cfg.token_tail : '') + ' — оставь пустым, чтобы не менять' : 'вставь токен завода';
  const min = String(Math.round((cfg.interval_s || 300) / 60));
  if (![...$('monInterval').options].some((o) => o.value === min)) $('monInterval').add(new Option(min + ' мин', min));
  $('monInterval').value = min;
  formFilled = true;
}

async function load() {
  const state = await apiGet('/api/sysmon/state', 'Не удалось получить состояние мониторинга', { source: 'monitor' });
  if (!state || state.error) {
    $('monText').textContent = (state && state.error) || 'Нет связи с приложением.';
    return;
  }
  $('monText').textContent = state.snapshot.text;
  renderAlerts(state.alerts || []);
  renderLink(state.send, state.config);
  renderLog(state);
  if (!formFilled) fillForm(state.config);
}

function setMsg(text, bad) {
  const el = $('monMsg');
  el.textContent = text;
  el.classList.toggle('is-bad', !!bad);
}

async function save() {
  const payload = {
    enabled: $('monEnabled').checked,
    plant: $('monPlant').value.trim(),
    url: $('monUrl').value.trim(),
    interval_s: Number($('monInterval').value) * 60,
  };
  const token = $('monToken').value.trim();
  if (token) payload.token = token;
  try {
    const res = await (await fetch('/api/sysmon/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    })).json();
    if (res.status !== 'ok') { setMsg(res.error || 'Не сохранилось', true); return false; }
    fillForm(res.config);
    setMsg('Сохранено');
    log.info('Настройки мониторинга сохранены');
    load();
    return true;
  } catch (e) {
    setMsg('Не сохранилось: ' + e.message, true);
    return false;
  }
}

async function testLink() {
  if (!(await save())) return;
  setMsg('Проверяю…');
  const res = await apiGet('/api/sysmon/send_test', 'Проверка связи не удалась', { source: 'monitor', timeout: 30000 });
  await load();
  if (res && res.ok) { setMsg('Сервер принял пульс'); return; }
  const err = (res && res.error) || 'Нет ответа от приложения';
  // причину отправки уже показывает строка под кнопками — не дублируем
  setMsg($('monSendInfo').textContent.includes(err) ? '' : err, true);
}

window.addEventListener('DOMContentLoaded', () => {
  log.info('Страница мониторинга загружена');
  $('monSave').addEventListener('click', save);
  $('monTest').addEventListener('click', testLink);
  load();
  setInterval(() => { if (!document.hidden) load(); }, POLL_MS);
});

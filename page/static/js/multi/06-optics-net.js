// ---------- Оптика: зум-jog + фокус-ползунок + автофокус (RTSP-камера в фокусе) ----------
// см. память rtsp-multi-sync — те же функции, что на RTSP-странице (rtsp.js).
let multiOpticPopup = null;   // createPopupController, создаётся в init
let multiOpticCaps = null;

function openOpticPopup() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !multiOpticPopup) return;
  ['multiOpticalZoomControls', 'multiFocusControls'].forEach((id) => {
    const e = document.getElementById(id); if (e) e.hidden = true;
  });
  const af = document.getElementById('multiAutoFocusBtn'); if (af) af.hidden = true;
  const hint = document.getElementById('multiOpticHint'); if (hint) hint.hidden = true;
  setCapLine(document.getElementById('multiOpticCap'), false, '', 'Проверяю…');
  multiOpticPopup.toggle();
  if (multiOpticPopup.isOpen()) { refreshOpticCaps(source.serial); initMultiLens(source.serial); }
}

async function refreshOpticCaps(serial) {
  const data = await RtspApi.getCapabilities(serial);
  const caps = (data && !data.error)
    ? data : { reachable: false, optical_zoom: false, focus: false, auto_focus: false };
  multiOpticCaps = caps;
  const hasOptical = !!caps.optical_zoom, hasFocus = !!caps.focus, hasAuto = !!caps.auto_focus;
  setCapLine(document.getElementById('multiOpticCap'), hasOptical || hasFocus,
    'Оптический зум/фокус поддерживается',
    caps.reachable ? 'Оптика не поддерживается' : 'Камера не отвечает на управление');
  const z = document.getElementById('multiOpticalZoomControls'); if (z) z.hidden = !hasOptical;
  const f = document.getElementById('multiFocusControls'); if (f) f.hidden = !hasFocus;
  const af = document.getElementById('multiAutoFocusBtn'); if (af) af.hidden = !hasAuto;
  const hint = document.getElementById('multiOpticHint'); if (hint) hint.hidden = hasOptical || hasFocus;
  initMultiLens(serial);
}

function setMultiZoomUI(zoom01) {
  if (zoom01 == null) return;
  const pct = Math.round(zoom01 * 100);
  const fill = document.getElementById('multiZoomBarFill');
  const val = document.getElementById('multiZoomVal');
  if (fill) fill.style.width = pct + '%';
  if (val) val.textContent = pct + '%';
}

// подтянуть реальные позиции зума/фокуса (обратка). НЕ зависим от caps — дёргаем сразу
// по serial, иначе фокус «залипает» на 0 до первого движения.
async function initMultiLens(serial) {
  if (!serial) return;
  const st = await RtspApi.lensStatus(serial);
  if (!st || st.error) return;
  if (st.zoom != null) setMultiZoomUI(st.zoom);
  if (st.focus != null) {
    const fs = document.getElementById('multiFocusSlider');
    const fv = document.getElementById('multiFocusVal');
    if (fs) fs.value = Math.round(st.focus * 100);
    if (fv) fv.textContent = Math.round(st.focus * 100) + '%';
  }
}

// зум — кнопки −/+ на удержание с живой обраткой (реальный zoom с камеры)
let multiZoomHoldTimer = null;
async function multiZoomHoldStart(dir) {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !(multiOpticCaps && multiOpticCaps.optical_zoom)) return;
  await RtspApi.opticalZoom(source.serial, dir, 5);
  if (multiZoomHoldTimer) clearInterval(multiZoomHoldTimer);
  multiZoomHoldTimer = setInterval(async () => {
    const st = await RtspApi.lensStatus(source.serial);
    if (st && !st.error && st.zoom != null) setMultiZoomUI(st.zoom);
  }, 250);
}
async function multiZoomHoldStop() {
  if (multiZoomHoldTimer) { clearInterval(multiZoomHoldTimer); multiZoomHoldTimer = null; }
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return;
  await RtspApi.opticalZoom(source.serial, 'stop');
  const st = await RtspApi.lensStatus(source.serial);
  if (st && !st.error && st.zoom != null) setMultiZoomUI(st.zoom);
}

// фокус — абсолютный ползунок (0..100 → 0..1)
async function multiFocusSet(val) {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !(multiOpticCaps && multiOpticCaps.focus)) return;
  const fv = document.getElementById('multiFocusVal'); if (fv) fv.textContent = val + '%';
  const data = await RtspApi.focusAbs(source.serial, (val / 100).toFixed(4));
  if (!data || data.error || data.status === 'failed') log.warn('Камера не подтвердила фокус', data);
}

async function multiAutoFocus() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return;
  const data = await RtspApi.autoFocus(source.serial);
  if (!data || data.error || data.status === 'failed') log.warn('Камера не подтвердила автофокус', data);
  else { log.success('Автофокус выполнен', data); initMultiLens(source.serial); }
}

// ---------- Сеть: смена IP RTSP-камеры в фокусе (popup под иконкой) ----------
// см. память rtsp-multi-sync — та же функция, что на RTSP-странице (rtsp.js).
let multiNetworkPopup = null;
let multiCurrentNetwork = null;

function isValidIpMulti(v) {
  return /^(\d{1,3}\.){3}\d{1,3}$/.test(v) && v.split('.').every((n) => +n >= 0 && +n <= 255);
}
function describeNetErrMulti(res) {
  if (!res) return 'нет ответа';
  return res.error || res.message || 'ошибка';
}
function setMultiNetEnabled(enabled) {
  ['multiNetworkDhcp', 'multiNetworkIp', 'multiNetworkMask', 'multiNetworkGateway', 'multiNetworkApplyBtn']
    .forEach((id) => { const e = document.getElementById(id); if (e) e.disabled = !enabled; });
}
function reflectMultiDhcp() {
  const f = document.getElementById('multiNetworkFields');
  const d = document.getElementById('multiNetworkDhcp');
  if (f) f.classList.toggle('is-hidden', !!(d && d.checked));
}

function openNetworkPopup() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp' || !multiNetworkPopup) return;
  setCapLine(document.getElementById('multiNetworkCap'), false, '', 'Проверяю…');
  const cur = document.getElementById('multiNetworkCurrent'); if (cur) cur.hidden = true;
  setMultiNetEnabled(false);
  multiNetworkPopup.toggle();
  if (multiNetworkPopup.isOpen()) loadMultiNetwork(source.serial);
}

async function loadMultiNetwork(serial) {
  const data = await RtspApi.getNetwork(serial);
  const g = (id) => document.getElementById(id);
  if (!data || data.error || !data.reachable) {
    multiCurrentNetwork = null;
    setCapLine(g('multiNetworkCap'), false, '',
      data && data.error === 'no_host' ? 'Управление недоступно' : 'Камера не отвечает на управление');
    setMultiNetEnabled(false);
    return;
  }
  multiCurrentNetwork = data;
  setCapLine(g('multiNetworkCap'), true, 'Камера отвечает' + (data.mac ? ' · ' + data.mac : ''), '');
  if (g('multiNetCurIp')) g('multiNetCurIp').textContent = data.ip || '—';
  if (g('multiNetCurMask')) g('multiNetCurMask').textContent = data.mask || '—';
  if (g('multiNetCurGw')) g('multiNetCurGw').textContent = data.gateway || '—';
  const cur = g('multiNetworkCurrent'); if (cur) cur.hidden = false;
  if (g('multiNetworkIp')) g('multiNetworkIp').value = data.ip || '';
  if (g('multiNetworkMask')) g('multiNetworkMask').value = data.mask || '';
  if (g('multiNetworkGateway')) g('multiNetworkGateway').value = data.gateway || '';
  if (g('multiNetworkDhcp')) g('multiNetworkDhcp').checked = !!data.dhcp;
  reflectMultiDhcp();
  setMultiNetEnabled(true);
}

async function applyMultiNetwork() {
  const source = focusedSource();
  if (!source || source.kind !== 'rtsp') return;
  const dhcp = document.getElementById('multiNetworkDhcp');
  if (dhcp && dhcp.checked) {
    if (!confirm('Включить DHCP? Камера получит новый IP от роутера, поток закроется — '
      + 'найдите её по новому адресу и подключите заново.')) return;
    const res = await RtspApi.setNetwork(source.serial, { dhcp: true });
    if (!res || res.error || !res.ok) { alert('Не удалось включить DHCP: ' + describeNetErrMulti(res)); return; }
    log.success('DHCP включён', res);
    multiNetworkPopup.close();
    alert('DHCP включён. Найдите камеру по новому адресу и подключите заново.');
    disconnectTile(state.focused);
    return;
  }
  const ip = document.getElementById('multiNetworkIp').value.trim();
  const mask = document.getElementById('multiNetworkMask').value.trim();
  const gateway = document.getElementById('multiNetworkGateway').value.trim();
  if (!isValidIpMulti(ip)) { alert('Некорректный IP-адрес'); return; }
  if (!isValidIpMulti(mask)) { alert('Некорректная маска подсети'); return; }
  if (gateway && !isValidIpMulti(gateway)) { alert('Некорректный шлюз'); return; }
  const cur = multiCurrentNetwork || {};
  if (!confirm('Сменить IP камеры ' + (cur.ip || '—') + ' → ' + ip + '?\n\nКамера станет доступна по '
    + 'новому адресу — он должен быть в вашей подсети, иначе камера пропадёт из сети.')) return;
  const res = await RtspApi.setNetwork(source.serial, { ip, mask, gateway, dhcp: false });
  if (!res || res.error || !res.ok) { alert('Не удалось сменить настройки: ' + describeNetErrMulti(res)); return; }
  const newIp = res.new_ip || ip;
  log.success('Сетевые настройки применены', res);
  multiNetworkPopup.close();
  // обновить источник (IP/URL) и переподключить поток на новый адрес
  if (source.connection && source.connection.url && cur.ip) {
    source.connection.url = source.connection.url.replace(cur.ip, newIp);
  }
  source.ip = newIp;
  if (source.saved && source.connection && source.connection.url) {
    RtspApi.saveCam({ url: source.connection.url, label: source.label, ip: newIp, serial: source.serial,
      scale: source.connection.scale, fps: source.connection.fps });
  }
  renderDrawer();   // обновить карточку источника — новый IP/URL сразу видны
  disconnectTile(state.focused);
  alert('IP изменён на ' + newIp + '. Переподключаюсь через несколько секунд…');
  setTimeout(() => startStream(state.focused), 4000);
}


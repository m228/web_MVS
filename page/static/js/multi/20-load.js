// ---------- «Только анализ» и панель «Нагрузка» ----------
// «Только анализ» — настройка сервера: приложение не держит RTSP-камеры без зрителей и записи, картинки в ячейках скрыты
// (зоны и значения КЗ остаются), нужную камеру открывают «глазиком». Состояние приходит вместе с замером нагрузки —
// поэтому все открытые страницы (в том числе на самом сервере) переключаются вместе.
const HOST_LOAD = { analysisOnly: false, timer: null, busy: false };
const HOST_LOAD_PERIOD_MS = 3000;

function applyAnalysisOnly(on) {
  on = !!on;
  const box = document.getElementById('multiAnalysisToggle');
  if (box) box.checked = on;
  if (HOST_LOAD.analysisOnly === on) return;
  HOST_LOAD.analysisOnly = on;
  // новую камеру (ещё не в базе) не прячем: в базу она попадает по первым кадрам, а у скрытой их нет
  state.tiles.forEach((tile, i) => {
    if (tile.connected && tile.kind === 'rtsp' && (!on || getSource(tile.serial)?.saved)) setTileHidden(i, on);
  });
}

// до расстановки камер по ячейкам: иначе потоки успели бы открыться и тут же закрыться
async function initAnalysisOnly() {
  const box = document.getElementById('multiAnalysisToggle');
  try {
    const r = await fetch('/api/rtsp/analysis_only').then((x) => x.json());
    applyAnalysisOnly(r && r.analysis_only);
  } catch (e) { /* галочка останется выключенной, состояние доберём с замером нагрузки */ }
  box?.addEventListener('change', async () => {
    const wanted = box.checked;
    HOST_LOAD.busy = true;                      // пока сервер не ответил, замер нагрузки не откатывает галочку назад
    try {
      const r = await fetch('/api/rtsp/analysis_only', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ analysis_only: wanted }) }).then((x) => x.json());
      if (!r || r.status !== 'ok') throw new Error('нет ответа');
      applyAnalysisOnly(wanted);
      log.success(wanted ? 'Только анализ: приложение не берёт камеры на показ' : 'Только анализ выключен — камеры снова показываются');
    } catch (e) {
      box.checked = !wanted;
      log.warn('Не удалось переключить «Только анализ»');
    } finally {
      HOST_LOAD.busy = false;
    }
  });
}

const hlNum = (v) => (v == null ? '—' : (v < 10 ? Number(v).toFixed(1) : String(Math.round(v))));
const HL_LIMIT_NAMES = { net: 'сеть', cpu: 'процессор', gpu: 'видеоядро' };

function hlRow(label, value, sub, pct, title) {
  const level = pct == null ? '' : (pct >= 85 ? ' is-bad' : (pct >= 70 ? ' is-warn' : ''));
  const bar = pct == null ? '' : `<div class="host-load__bar${level}"><i style="width:${Math.max(0, Math.min(100, pct))}%"></i></div>`;
  return `<div class="host-load__row"${title ? ` title="${escapeHtml(title)}"` : ''}>
    <div class="host-load__head"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>${bar}
    ${sub ? `<div class="host-load__sub">${escapeHtml(sub)}</div>` : ''}</div>`;
}

function hlEstimate(r) {
  const e = r.estimate;
  if (!e) return hlRow('Запас', '—', r.streams.cameras ? 'нагрузка от камер пока слишком мала для оценки' : 'посчитается, когда камеры будут работать', null);
  const n = e.cameras;
  const value = n < 1 ? 'меньше камеры' : (n > 20 ? 'больше 20 камер' : `≈ ещё ${Math.floor(n)} кам.`);
  const fps = n >= 1 && n <= 20 ? `≈ ${Math.round(Math.floor(n) * r.streams.fps)} к/с · ` : '';
  const title = 'Оценка: каждая новая камера нагружает ПК как нынешние в среднем. Пороги: сеть 70 %, процессор и видеоядро 85 %. '
    + Object.entries(e.by).map(([k, v]) => `${HL_LIMIT_NAMES[k]}: ${v > 20 ? '>20' : v}`).join(', ');
  return hlRow('Запас', value, `${fps}первым кончится ${HL_LIMIT_NAMES[e.limit_by]}`, null, title);
}

function renderHostLoad(r) {
  const body = document.getElementById('hostLoadBody');
  const box = document.getElementById('hostLoad');
  if (!body || !box || !r || !r.streams) return;
  box.hidden = false;
  if (!r.ready) { body.innerHTML = '<div class="host-load__sub">замеряю…</div>'; return; }
  const rows = [];
  const withCams = (r.adapters || []).filter((a) => a.camera_link);
  (withCams.length ? withCams : (r.adapters || [])).forEach((a) => {
    const pct = a.link_mbps && a.rx_mbps != null ? (100 * Math.max(a.rx_mbps, a.tx_mbps || 0)) / a.link_mbps : null;
    const link = a.link_mbps ? (a.link_mbps >= 1000 ? `${a.link_mbps / 1000} Гбит/с` : `${a.link_mbps} Мбит/с`) : 'скорость неизвестна';
    rows.push(hlRow(`Сеть · ${a.name}`, `↓ ${hlNum(a.rx_mbps)} Мбит/с`, `↑ ${hlNum(a.tx_mbps)} · линк ${link}${a.cameras ? ` · камер ${a.cameras}` : ''}`, pct,
      'Весь трафик адаптера, не только программы: ↓ приём, ↑ отдача'));
  });
  if (r.cpu) {
    const parts = [`приложение ${hlNum(r.cpu.app)}`, `анализ ${hlNum(r.cpu.motion)}`];
    if (r.cpu.other >= 0.5) parts.push(`другие службы ${hlNum(r.cpu.other)}`);
    rows.push(hlRow('Процессор', `${hlNum(r.cpu.total)} %`, `из них ${parts.join(' · ')} %`, r.cpu.total, 'Весь процессор ПК и доля этой программы и её служб'));
  }
  if (r.gpu && r.gpu.total >= 0.5) {
    rows.push(hlRow('Видеоядро (декод)', `${hlNum(r.gpu.total)} %`, `из них наши ${hlNum(r.gpu.ours)} %`, r.gpu.total, 'Как график Video Decode в диспетчере задач'));
  }
  const s = r.streams;
  const approx = s.fps_assumed ? '≈ ' : '';
  rows.push(hlRow('Камеры', String(s.cameras), `декод: приложение ${s.app} + анализ ${s.motion} · ${approx}${s.decode_fps} к/с${s.motion_remote ? ' · анализ на другом ПК' : ''}`, null,
    'Каждое подключение декодируется отдельно: показ приложения и анализ КЗ'));
  if (s.to_browsers_mbps >= 0.1) rows.push(hlRow('В браузеры', `≈ ${hlNum(s.to_browsers_mbps)} Мбит/с`, 'картинки, которые отдаёт приложение', null));
  rows.push(hlEstimate(r));
  body.innerHTML = rows.join('');
}

async function pollHostLoad() {
  try {
    const r = await fetch('/api/host/load').then((x) => x.json());
    if (!HOST_LOAD.busy && r && r.analysis_only !== undefined) applyAnalysisOnly(r.analysis_only);
    renderHostLoad(r);
  } catch (e) { /* сервер перезапускается — оставляем прошлые цифры */ }
}

function startHostLoadPolling() {
  pollHostLoad();
  HOST_LOAD.timer = setInterval(pollHostLoad, HOST_LOAD_PERIOD_MS);
}

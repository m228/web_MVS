const log = {
  info: (m, p) => window.AppLog?.info('network', m, p),
  success: (m, p) => window.AppLog?.success('network', m, p),
  warn: (m, p) => window.AppLog?.warn('network', m, p),
  error: (m, p) => window.AppLog?.error('network', m, p),
};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (s) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[s]));
}

// jumbo считаем включённым, если значение содержит число >= 2000 (т.е. не 1500/Disabled)
function jumboOn(value) {
  const n = parseInt(String(value).replace(/\D/g, ''), 10);
  return Number.isFinite(n) && n >= 2000;
}

function renderAdminBanner(isAdmin) {
  const banner = document.getElementById('netAdminBanner');
  if (!banner) return;
  if (isAdmin) {
    banner.classList.add('hidden');
    banner.textContent = '';
  } else {
    banner.classList.remove('hidden');
    banner.textContent = 'Приложение запущено без прав администратора — включение jumbo-кадров и фильтр-драйвера недоступно. Запустите Start.bat от имени администратора.';
  }
}

function adapterCard(adapter, isAdmin) {
  const el = document.createElement('div');
  el.className = 'net-adapter';

  const isUp = String(adapter.status).toLowerCase() === 'up';
  const jOn = jumboOn(adapter.jumbo);
  const filterText = adapter.filter_present
    ? (adapter.filter_enabled ? 'включён' : 'установлен, выключен')
    : 'не установлен';

  el.innerHTML = `
    <div class="net-adapter__head">
      <h3>${escapeHtml(adapter.name)}</h3>
      <span class="net-chip ${isUp ? 'net-chip--ok' : 'net-chip--off'}">${escapeHtml(adapter.status)}</span>
    </div>
    <div class="net-adapter__ip">IP: ${escapeHtml(adapter.ip || '—')}</div>

    <div class="net-row">
      <div class="net-row__label">
        <span class="net-dot ${jOn ? 'net-dot--ok' : 'net-dot--off'}"></span>
        Jumbo-кадры: <strong>${escapeHtml(adapter.jumbo || 'нет')}</strong>
      </div>
      <button type="button" class="btn-secondary net-btn" data-jumbo ${isAdmin ? '' : 'disabled'}>Включить</button>
    </div>

    <div class="net-row">
      <div class="net-row__label">
        <span class="net-dot ${adapter.filter_enabled ? 'net-dot--ok' : 'net-dot--off'}"></span>
        Фильтр-драйвер GigE: <strong>${escapeHtml(filterText)}</strong>
      </div>
      <button type="button" class="btn-secondary net-btn" data-filter ${(!adapter.filter_present || !isAdmin) ? 'disabled' : ''}>Включить</button>
    </div>
  `;

  const jumboBtn = el.querySelector('[data-jumbo]');
  jumboBtn?.addEventListener('click', async () => {
    jumboBtn.disabled = true;
    jumboBtn.textContent = '…';
    const res = await NetApi.enableJumbo(adapter.name);
    if (res?.ok) {
      log.success('Jumbo-кадры включены', { adapter: adapter.name, jumbo: res.jumbo });
    } else {
      alert('Не удалось включить jumbo: ' + (res?.error || 'ошибка'));
    }
    loadStatus();
  });

  const filterBtn = el.querySelector('[data-filter]');
  filterBtn?.addEventListener('click', async () => {
    filterBtn.disabled = true;
    filterBtn.textContent = '…';
    const res = await NetApi.enableFilter(adapter.name);
    if (res?.ok) {
      log.success('Фильтр-драйвер GigE включён', { adapter: adapter.name });
    } else {
      alert('Не удалось включить фильтр-драйвер: ' + (res?.error || 'ошибка'));
    }
    loadStatus();
  });

  return el;
}

async function loadStatus() {
  const container = document.getElementById('netAdapters');
  if (container) container.innerHTML = '<div class="table-empty-state">Загрузка…</div>';

  const data = await NetApi.status();
  if (!data) {
    if (container) container.innerHTML = '<div class="table-empty-state">Не удалось получить состояние сети.</div>';
    return;
  }

  renderAdminBanner(!!data.admin);

  const adapters = data.adapters || [];
  if (!container) return;
  container.innerHTML = '';

  if (!adapters.length) {
    container.innerHTML = '<div class="table-empty-state">Сетевые адаптеры не найдены' +
      (data.error ? ' (' + escapeHtml(data.error) + ')' : '') + '.</div>';
    return;
  }

  adapters.forEach((adapter) => container.appendChild(adapterCard(adapter, !!data.admin)));
}

window.addEventListener('DOMContentLoaded', () => {
  log.info('Страница сети загружена');
  document.getElementById('netRefreshBtn')?.addEventListener('click', loadStatus);
  loadStatus();
});

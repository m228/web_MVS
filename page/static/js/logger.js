(function () {
  const MAX_ITEMS = 500;
  const BACKEND_POLL_MS = 1000;

  const state = {
    items: [],
    localId: 0,
    backendLastId: 0,
    backendTimer: null,
    backendErrorShown: false,
    elements: {},
    filter: new Set(['info', 'success', 'warn', 'error', 'debug']),
  };

  function normalizeLevel(level) {
    return ['success', 'warn', 'error', 'debug'].includes(level) ? level : 'info';
  }

  function safeStringify(value) {
    const seen = new WeakSet();

    try {
      return JSON.stringify(
        value,
        (key, val) => {
          if (val instanceof Error) {
            return {
              name: val.name,
              message: val.message,
              stack: val.stack,
            };
          }

          if (typeof val === 'function') {
            return `[Function ${val.name || 'anonymous'}]`;
          }

          if (val && typeof val === 'object') {
            if (seen.has(val)) return '[Circular]';
            seen.add(val);
          }

          return val;
        },
        2
      );
    } catch (error) {
      return String(value);
    }
  }

  function isPanelOpen() {
    const panel = state.elements.panel;
    return !!(panel && !panel.classList.contains('hidden'));
  }

  function syncToggleState() {
    const toggle = state.elements.toggle;
    if (!toggle) return;
    toggle.setAttribute('aria-expanded', String(isPanelOpen()));
  }

  function add(entry) {
    const item = {
      id: entry.id ?? `local-${++state.localId}`,
      time: entry.time ?? new Date().toISOString(),
      source: entry.source ?? 'ui',
      level: normalizeLevel(entry.level),
      message: entry.message ?? '',
      payload: entry.payload ?? null,
    };

    state.items.push(item);

    if (state.items.length > MAX_ITEMS) {
      state.items.shift();
    }

    render();
    return item;
  }

  function render() {
    const { list, counter } = state.elements;
    if (!list) return;

    list.innerHTML = '';

    state.items
      .slice()
      .reverse()
      .filter((item) => state.filter.has(item.level))
      .forEach((item) => {
        const card = document.createElement('article');
        card.className = `debug-entry debug-entry--${item.level}`;

        const head = document.createElement('div');
        head.className = 'debug-entry__head';

        const meta = document.createElement('strong');
        meta.textContent = `[${new Date(item.time).toLocaleTimeString('ru-RU')}] ${item.source}`;

        const level = document.createElement('span');
        level.textContent = item.level.toUpperCase();

        const message = document.createElement('div');
        message.className = 'debug-entry__message';
        message.textContent = item.message;

        head.append(meta, level);
        card.append(head, message);

        if (item.payload !== null && item.payload !== undefined) {
          const pre = document.createElement('pre');
          pre.className = 'debug-entry__payload';
          pre.textContent =
            typeof item.payload === 'string' ? item.payload : safeStringify(item.payload);
          card.appendChild(pre);
        }

        list.appendChild(card);
      });

    if (counter) {
      counter.textContent = String(state.items.length);
    }
  }

  function info(source, message, payload) {
    return add({ source, level: 'info', message, payload });
  }

  function success(source, message, payload) {
    return add({ source, level: 'success', message, payload });
  }

  function warn(source, message, payload) {
    return add({ source, level: 'warn', message, payload });
  }

  function error(source, message, payload) {
    return add({ source, level: 'error', message, payload });
  }

  function debug(source, message, payload) {
    return add({ source, level: 'debug', message, payload });
  }

  function clear() {
    state.items = [];
    render();
  }

  function formatForExport() {
    return state.items
      .map((item) => {
        const time = new Date(item.time).toLocaleString('ru-RU');
        const head = `[${time}] ${item.level.toUpperCase()} ${item.source} — ${item.message}`;

        if (item.payload === null || item.payload === undefined) {
          return head;
        }

        const payload =
          typeof item.payload === 'string' ? item.payload : safeStringify(item.payload);
        return `${head}\n${payload}`;
      })
      .join('\n\n');
  }

  function download() {
    const text = formatForExport();
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);

    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const link = document.createElement('a');
    link.href = url;
    link.download = `logs_${stamp}.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);

    info('logger', 'Логи выгружены в файл', { count: state.items.length });
  }

  function openPanel() {
    const panel = state.elements.panel;
    if (!panel) return;

    panel.classList.remove('hidden');
    syncToggleState();
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function closePanel() {
    const panel = state.elements.panel;
    if (!panel) return;

    panel.classList.add('hidden');
    syncToggleState();
  }

  function toggle() {
    if (isPanelOpen()) {
      closePanel();
    } else {
      openPanel();
    }
  }

  async function fetchBackendLogs() {
    if (!state.elements.panel) return;

    try {
      const response = await fetch(`/api/debug/logs?since_id=${state.backendLastId}`, {
        cache: 'no-store',
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const data = await response.json();

      (data.items || []).forEach((item) => {
        add({
          id: item.id,
          time: item.time,
          source: item.source || 'backend',
          level: item.level || 'info',
          message: item.message || '',
          payload: item.payload ?? null,
        });
      });

      if (typeof data.last_id === 'number') {
        state.backendLastId = data.last_id;
      }

      state.backendErrorShown = false;
    } catch (err) {
      if (!state.backendErrorShown) {
        warn('logger', 'Не удалось получить backend-логи', { error: err.message });
        state.backendErrorShown = true;
      }
    }
  }

  function patchConsole() {
    ['log', 'info', 'warn', 'error'].forEach((method) => {
      const original = console[method].bind(console);

      console[method] = (...args) => {
        add({
          source: 'console',
          level: method === 'log' ? 'debug' : method,
          message: args
            .map((item) => (typeof item === 'string' ? item : safeStringify(item)))
            .join(' '),
          payload: args.length > 1 ? args : null,
        });

        original(...args);
      };
    });
  }

  function patchAlert() {
    const originalAlert = window.alert.bind(window);

    window.alert = (message) => {
      warn('alert', String(message));
      return originalAlert(message);
    };
  }

  function buildFilterBar() {
    const { panel, list } = state.elements;
    if (!panel || !list || state.elements.filterBar) return;

    const bar = document.createElement('div');
    bar.className = 'debug-filter';

    const levels = [
      ['info', 'INFO'],
      ['success', 'SUCCESS'],
      ['warn', 'WARNING'],
      ['error', 'ERROR'],
      ['debug', 'DEBUG'],
    ];

    levels.forEach(([level, label]) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `debug-filter__btn debug-filter__btn--${level} is-active`;
      btn.textContent = label;
      btn.addEventListener('click', () => {
        if (state.filter.has(level)) {
          state.filter.delete(level);
          btn.classList.remove('is-active');
        } else {
          state.filter.add(level);
          btn.classList.add('is-active');
        }
        render();
      });
      bar.appendChild(btn);
    });

    list.parentNode.insertBefore(bar, list);
    state.elements.filterBar = bar;
  }

  function mount() {
    state.elements.panel = document.getElementById('debugConsoleSection');
    state.elements.list = document.getElementById('debugConsoleList');
    state.elements.counter = document.getElementById('logCounter');
    state.elements.toggle = document.getElementById('logsBtn');
    state.elements.clear = document.getElementById('debugClearBtn');
    state.elements.save = document.getElementById('debugSaveBtn');

    state.elements.toggle?.addEventListener('click', toggle);
    state.elements.clear?.addEventListener('click', clear);
    state.elements.save?.addEventListener('click', download);

    buildFilterBar();
    render();
    syncToggleState();

    if (state.elements.panel) {
      info('logger', 'Отладочная консоль инициализирована');
      fetchBackendLogs();
      state.backendTimer = setInterval(fetchBackendLogs, BACKEND_POLL_MS);
    }
  }

  patchConsole();
  patchAlert();

  window.addEventListener('error', (event) => {
    error('window', event.message, {
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
    });
  });

  window.addEventListener('unhandledrejection', (event) => {
    error('promise', 'Unhandled promise rejection', event.reason);
  });

  window.AppLog = {
    add,
    info,
    success,
    warn,
    error,
    debug,
    clear,
    download,
    open: openPanel,
    close: closePanel,
    toggle,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();

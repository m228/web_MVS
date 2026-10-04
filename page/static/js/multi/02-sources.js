// ---------- доступные камеры (GigE из сети + добавленные RTSP) ----------
async function loadCameras() {
  const rtsp = state.cameras.filter((c) => c.kind === 'rtsp');

  const data = await CameraApi.getCamsDetailed();
  const gige = [];
  const serials = [];

  if (data) {
    for (const [serial, entries] of Object.entries(data)) {
      const list = entries || [];
      const entry = list.find((e) => e.available) || list[0] || {};
      gige.push({
        serial,
        kind: 'gige',
        label: serial,
        model: entry.model || '',
        ip: null,
        available: !!entry.available,
        settings: defaultGigeSettings(),
        connection: null,
      });
      if (entry.available) serials.push(serial);
    }
  }

  // IP запрашиваем строго ПО ОДНОМУ серийнику за раз (не параллельно) — как на
  // главной. Это короткая control-операция с ia.destroy() в finally; гонки за
  // control нет. Стрим ломал не этот опрос, а отдельные правки (см. bug.txt №2).
  for (const serial of serials) {
    const response = await CameraApi.getIp(serial);
    const cam = gige.find((c) => c.serial === serial);
    if (cam && response?.ip) cam.ip = response.ip;
  }

  // переносим уже введённые пользователем настройки GigE между обновлениями
  const prevGige = state.cameras.filter((c) => c.kind === 'gige');
  gige.forEach((cam) => {
    const prev = prevGige.find((c) => c.serial === cam.serial);
    if (prev) cam.settings = prev.settings;
  });

  gige.sort((a, b) => String(a.serial).localeCompare(String(b.serial), 'ru'));
  state.cameras = gige.concat(rtsp);

  renderDrawer();
  refreshTileSelects();
  log.info('Список камер загружен', { gige: gige.length, rtsp: rtsp.length });
}

// подгрузка сохранённых RTSP-камер из мини-базы (после перезапуска)
async function loadSavedRtsp() {
  const data = await RtspApi.listSaved();
  const items = (data && data.items) || [];

  items.forEach((it) => {
    if (!it.url) return;
    if (state.cameras.some((c) => c.kind === 'rtsp' && c.connection && c.connection.url === it.url)) return;

    state.rtspCounter += 1;
    state.cameras.push({
      // серийник берём из базы: тогда камера сохраняет свой ключ между запусками
      // (а с ним — проект/интервал автосохранения и автоподключение)
      serial: it.serial || `rtsp_${state.rtspCounter}`,
      kind: 'rtsp',
      label: it.label || `RTSP ${it.ip || ''}`.trim(),
      model: 'RTSP',
      ip: it.ip || null,
      available: true,
      settings: null,
      connection: { url: it.url, scale: it.scale ?? 100, fps: it.fps || null },
      saved: true,
      autostart: !!it.autostart,
    });
  });

  if (items.length) {
    renderDrawer();
    refreshTileSelects();
    log.info('Загружены сохранённые RTSP-камеры', { count: items.length });
  }
}

// ---------- шторка слева (источники, drag + настройки) ----------
function renderDrawer() {
  const list = document.getElementById('multiDrawerList');
  if (!list) return;

  list.innerHTML = '';

  if (!state.cameras.length) {
    list.innerHTML = '<div class="multi-drawer__empty">Камеры не найдены. Нажмите «Обновить» или «+ RTSP».</div>';
    return;
  }

  state.cameras.forEach((cam) => {
    const chip = document.createElement('div');
    const draggable = cam.kind === 'rtsp' || cam.available;
    chip.className = 'camera-chip' + (draggable ? '' : ' camera-chip--off');
    chip.draggable = false; // тащим только за шапку карточки, не за всю
    chip.dataset.source = cam.serial;

    const ipText = cam.kind === 'rtsp'
      ? (cam.ip || 'по URL')
      : (cam.ip || (cam.available ? '—' : 'недоступна'));

    chip.innerHTML = `
      <div class="camera-chip__main" data-chip-toggle draggable="${draggable}">
        <div class="camera-chip__info">
          <span class="camera-chip__serial">${escapeHtml(cam.label)}</span>
          <span class="camera-chip__model">${escapeHtml(cam.model || (cam.kind === 'rtsp' ? 'RTSP' : 'камера'))}</span>
          <span class="camera-chip__ip">IP: ${escapeHtml(ipText)}</span>
        </div>
        <div class="camera-chip__actions">
          ${cam.kind === 'gige' ? `<button type="button" class="camera-chip__info-btn" data-chip-info title="Информация о камере" aria-label="Информация о камере">${INFO_SVG}</button>` : ''}
          ${cam.kind === 'rtsp' ? `<button type="button" class="camera-chip__remove" data-chip-remove title="Удалить камеру" aria-label="Удалить">×</button>` : ''}
        </div>
      </div>
      <div class="camera-chip__settings" draggable="false" ${state.expandedSerial === cam.serial ? '' : 'hidden'}></div>
    `;

    const main = chip.querySelector('[data-chip-toggle]');
    if (draggable) {
      main.addEventListener('dragstart', (event) => {
        event.dataTransfer.setData('text/plain', cam.serial);
        event.dataTransfer.effectAllowed = 'copy';
        chip.classList.add('is-dragging');
      });
      main.addEventListener('dragend', () => chip.classList.remove('is-dragging'));
    }
    main.addEventListener('click', (event) => {
      if (event.target.closest('[data-chip-info]') || event.target.closest('[data-chip-remove]')) return;
      toggleChipSettings(cam.serial);
    });

    const infoBtn = chip.querySelector('[data-chip-info]');
    if (infoBtn) {
      infoBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        openInfoModal(cam.serial);
      });
    }

    const removeBtn = chip.querySelector('[data-chip-remove]');
    if (removeBtn) {
      removeBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        removeSource(cam.serial);
      });
    }

    const settingsBox = chip.querySelector('.camera-chip__settings');
    if (state.expandedSerial === cam.serial) {
      renderChipSettings(settingsBox, cam);
    }

    list.appendChild(chip);
  });
}

function toggleChipSettings(serial) {
  state.expandedSerial = state.expandedSerial === serial ? null : serial;
  renderDrawer();
}

// удаление источника (для добавленных вручную RTSP-камер)
function removeSource(serial) {
  const source = getSource(serial);

  state.tiles.forEach((tile, i) => {
    if (tile.serial === serial) {
      if (tile.connected) stopStream(serial, tile.kind);
      tile.serial = null;
      tile.kind = null;
      tile.connected = false;
      tile.photo = false;
      tile.video = false;
      renderTile(i);
    }
  });
  state.cameras = state.cameras.filter((c) => c.serial !== serial);
  if (state.expandedSerial === serial) state.expandedSerial = null;

  // удалённую вручную RTSP-камеру убираем и из мини-базы
  if (source && source.kind === 'rtsp' && source.connection && source.connection.url) {
    RtspApi.removeSaved(source.connection.url);
  }

  renderDrawer();
  refreshTileSelects();
  updateToolbar();
  log.info('Источник удалён', { serial });
}

// применить FPS/масштаб RTSP-камеры прямо из карточки (перезапуск потока при эфире)
function applyChipParam(cam, param, value) {
  const c = cam.connection || (cam.connection = {});
  if (param === 'fps') c.fps = (value && +value > 0) ? +value : null;
  else if (param === 'scale') c.scale = (value && +value > 0) ? Math.min(100, Math.max(1, +value)) : 100;
  const idx = state.tiles.findIndex((t) => t.serial === cam.serial);
  if (idx >= 0 && state.tiles[idx].connected) {
    const el = getTileEl(idx);
    const frame = el?.querySelector('[data-tile-frame]');
    if (frame) frame.src = buildStreamUrl(cam);
  }
  if (cam.saved && c.url) {
    RtspApi.saveCam({ url: c.url, label: cam.label, ip: cam.ip, serial: cam.serial, scale: c.scale, fps: c.fps });
  }
  log.info(param + ' изменён', { serial: cam.serial, [param]: param === 'fps' ? c.fps : c.scale });
  renderDrawer();
}

// inline-редактор FPS/масштаба в карточке: клик по значению -> поле + авто/✓/✗
function bindChipParamEditor(box, cam) {
  const editor = box.querySelector('[data-editor]');
  const lab = box.querySelector('[data-editor-lab]');
  const inp = box.querySelector('[data-editor-inp]');
  const auto = box.querySelector('[data-editor-auto]');
  const ok = box.querySelector('[data-editor-ok]');
  const cancel = box.querySelector('[data-editor-cancel]');
  if (!editor) return;
  let curParam = null;
  [editor, inp, auto, ok, cancel].forEach((el) => el && el.addEventListener('click', (e) => e.stopPropagation()));

  box.querySelectorAll('[data-edit]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      curParam = btn.dataset.edit;
      const c = cam.connection || {};
      lab.textContent = curParam === 'fps' ? 'FPS' : 'Масштаб %';
      inp.value = curParam === 'fps' ? (c.fps || '') : (c.scale ?? 100);
      if (auto) auto.hidden = curParam !== 'fps';   // «авто» только для FPS
      editor.hidden = false;
      inp.focus();
    });
  });
  if (ok) ok.addEventListener('click', (e) => { e.stopPropagation(); applyChipParam(cam, curParam, inp.value); });
  if (cancel) cancel.addEventListener('click', (e) => { e.stopPropagation(); editor.hidden = true; });
  if (auto) auto.addEventListener('click', (e) => { e.stopPropagation(); applyChipParam(cam, curParam, ''); });
}

// форма настроек внутри карточки источника (GigE — параметры камеры; RTSP — сводка)
function renderChipSettings(box, cam) {
  if (!box) return;

  if (cam.kind === 'rtsp') {
    const c = cam.connection || {};
    box.innerHTML = `
      <div class="chip-rtsp-summary">
        <div class="chip-url-row">
          <span class="chip-url">URL: <strong>${escapeHtml(c.url || '—')}</strong></span>
          <button type="button" class="chip-copy" data-chip-copy title="Скопировать URL" aria-label="Скопировать URL" ${c.url ? '' : 'disabled'}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
          </button>
        </div>
        <div class="chip-params">
          <button type="button" class="chip-param" data-edit="scale" title="Изменить масштаб потока">Масштаб: <strong>${escapeHtml(c.scale ?? 100)}%</strong></button>
          <button type="button" class="chip-param" data-edit="fps" title="Изменить FPS">FPS: <strong>${escapeHtml(c.fps || 'авто')}</strong></button>
        </div>
        <div class="chip-editor" data-editor hidden>
          <span class="chip-editor__lab" data-editor-lab></span>
          <input type="number" class="chip-editor__inp" data-editor-inp min="1" />
          <button type="button" class="chip-editor__auto" data-editor-auto title="Авто">авто</button>
          <button type="button" class="chip-editor__ok" data-editor-ok title="Применить">✓</button>
          <button type="button" class="chip-editor__cancel" data-editor-cancel title="Отмена">✗</button>
        </div>
        <label class="chip-autostart" title="Автостарт: камера поднимается при запуске программы и сама продолжает автосохранение — браузер открывать не нужно">
          <input type="checkbox" data-chip-autostart ${cam.autostart ? 'checked' : ''} ${c.url ? '' : 'disabled'} />
          <span>Автостарт</span>
        </label>
      </div>
    `;

    const autostartBox = box.querySelector('[data-chip-autostart]');
    if (autostartBox) {
      autostartBox.addEventListener('click', (e) => e.stopPropagation());
      autostartBox.addEventListener('change', () => toggleAutostart(cam, autostartBox));
    }
    bindChipParamEditor(box, cam);
    const copyBtn = box.querySelector('[data-chip-copy]');
    if (copyBtn) {
      copyBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const url = c.url || '';
        if (!url) return;
        navigator.clipboard?.writeText(url)
          .then(() => { copyBtn.classList.add('is-copied'); setTimeout(() => copyBtn.classList.remove('is-copied'), 1200); log.success('URL скопирован'); })
          .catch(() => log.warn('Не удалось скопировать URL'));
      });
    }
    return;
  }

  const s = cam.settings;
  box.innerHTML = `
    <div class="chip-settings__grid">
      <label>Ширина<input type="number" data-set="width" value="${escapeHtml(s.width)}" /></label>
      <label>Высота<input type="number" data-set="height" value="${escapeHtml(s.height)}" /></label>
      <label>Смещение X<input type="number" data-set="offset_x" value="${escapeHtml(s.offset_x)}" /></label>
      <label>Смещение Y<input type="number" data-set="offset_y" value="${escapeHtml(s.offset_y)}" /></label>
      <label>FPS<input type="number" step="0.1" data-set="fps" value="${escapeHtml(s.fps)}" /></label>
      <label>Экспозиция<input type="number" data-set="exposure_time" value="${escapeHtml(s.exposure_time)}" /></label>
      <label>Автоэкспозиция
        <select data-set="exposure_auto">
          <option value="Off"${s.exposure_auto === 'Off' ? ' selected' : ''}>Off</option>
          <option value="Once"${s.exposure_auto === 'Once' ? ' selected' : ''}>Once</option>
          <option value="Continuous"${s.exposure_auto === 'Continuous' ? ' selected' : ''}>Continuous</option>
        </select>
      </label>
      <label>Формат (RGB)
        <select data-set="pixel_format">
          <option value=""${!s.pixel_format ? ' selected' : ''}>— как есть —</option>
          ${['RGB8', 'BGR8', 'Mono8', 'BayerRG8', 'BayerGB8', 'BayerGR8', 'BayerBG8', 'YUV422_8'].map((f) => `<option value="${f}"${s.pixel_format === f ? ' selected' : ''}>${f}</option>`).join('')}
        </select>
      </label>
    </div>
    <p class="chip-settings__hint">Настройки сохраняются за камерой и применяются при подключении.</p>
  `;

  box.querySelectorAll('[data-set]').forEach((input) => {
    input.addEventListener('click', (e) => e.stopPropagation());
    input.addEventListener('change', () => {
      const key = input.dataset.set;
      cam.settings[key] = input.value;
      log.debug('Настройка камеры изменена', { serial: cam.serial, [key]: input.value });
    });
  });
}

// автоподключение RTSP-камеры при старте программы (галочка в карточке источника).
// Камера должна быть в мини-базе: автостарт живёт в ней, а не в памяти процесса.
async function toggleAutostart(cam, checkbox) {
  const url = cam.connection && cam.connection.url;
  if (!url) return;
  const enabled = checkbox.checked;

  // не сохранённую камеру сперва кладём в базу, иначе автостарту нечего помечать
  if (!cam.saved) {
    await RtspApi.saveCam({
      url, label: cam.label, ip: cam.ip, serial: cam.serial,
      scale: cam.connection.scale, fps: cam.connection.fps,
    });
    cam.saved = true;
  }

  const data = await RtspApi.setAutostart(url, enabled, cam.serial);
  if (!data || data.error) {
    checkbox.checked = !enabled; // откат галочки к фактическому состоянию
    log.warn('Не удалось изменить автоподключение', { serial: cam.serial, error: data?.error });
    return;
  }

  cam.autostart = enabled;
  log.success(enabled ? 'Автоподключение включено' : 'Автоподключение выключено', { serial: cam.serial });
}


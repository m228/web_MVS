// ---------- инициализация ----------
function initLayoutButtons() {
  document.querySelectorAll('.layout-btn').forEach((btn) => {
    btn.addEventListener('click', () => setLayout(Number(btn.dataset.layout)));
  });
}

function initToolbar() {
  document.getElementById('multiConnectBtn')?.addEventListener('click', () => startStream(state.focused));
  document.getElementById('multiDisconnectBtn')?.addEventListener('click', () => disconnectTile(state.focused));
  document.getElementById('multiConfigBtn')?.addEventListener('click', openConfigModal);

  // оптика: popup под иконкой + ползунки (зум-jog, фокус-абс), автофокус — клик
  multiOpticPopup = UIHelpers.createPopupController(
    document.getElementById('multiOpticCard'), document.getElementById('multiOpticBtn'));
  document.getElementById('multiOpticBtn')?.addEventListener('click', (e) => {
    e.stopPropagation(); openOpticPopup();
  });
  const mZoomJog = document.querySelector('#multiOpticalZoomControls .optic-jog');
  if (mZoomJog) {
    mZoomJog.addEventListener('pointerdown', (e) => {
      const b = e.target.closest('button[data-zoom]');
      if (!b) return;
      e.preventDefault();
      multiZoomHoldStart(b.getAttribute('data-zoom'));
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach((ev) =>
      mZoomJog.addEventListener(ev, multiZoomHoldStop));
  }
  const mfs = document.getElementById('multiFocusSlider');
  if (mfs) {
    mfs.addEventListener('input', () => {
      const fv = document.getElementById('multiFocusVal'); if (fv) fv.textContent = mfs.value + '%';
    });
    mfs.addEventListener('change', () => multiFocusSet(Number(mfs.value)));
  }
  document.getElementById('multiAutoFocusBtn')?.addEventListener('click', multiAutoFocus);
  document.getElementById('multiOpticCard')?.addEventListener('click', (e) => e.stopPropagation());

  // фото / видео: popup под иконкой
  multiPhotoPopup = UIHelpers.createPopupController(
    document.getElementById('multiPhotoCard'), document.getElementById('multiPhotoBtn'));
  multiVideoPopup = UIHelpers.createPopupController(
    document.getElementById('multiVideoCard'), document.getElementById('multiVideoBtn'));
  document.getElementById('multiPhotoBtn')?.addEventListener('click', (e) => { e.stopPropagation(); openPhotoPopup(); });
  document.getElementById('multiVideoBtn')?.addEventListener('click', (e) => { e.stopPropagation(); openVideoPopup(); });
  document.getElementById('multiPhotoCard')?.addEventListener('click', (e) => e.stopPropagation());
  document.getElementById('multiVideoCard')?.addEventListener('click', (e) => e.stopPropagation());
  document.addEventListener('click', (e) => {
    [[multiPhotoPopup, 'multiPhotoCard', 'multiPhotoBtn'], [multiVideoPopup, 'multiVideoCard', 'multiVideoBtn']]
      .forEach(([p, cardId, btnId]) => {
        if (!p || !p.isOpen()) return;
        const card = document.getElementById(cardId), btn = document.getElementById(btnId);
        if (card && !card.contains(e.target) && btn && !btn.contains(e.target)) p.close();
      });
  });

  // подсветка / изображение: popup под иконкой
  multiLightPopup = UIHelpers.createPopupController(
    document.getElementById('multiLightCard'), document.getElementById('multiLightBtn'));
  multiImagePopup = UIHelpers.createPopupController(
    document.getElementById('multiImageCard'), document.getElementById('multiImageBtn'));
  document.getElementById('multiLightBtn')?.addEventListener('click', (e) => { e.stopPropagation(); openLightPopup(); });
  document.getElementById('multiImageBtn')?.addEventListener('click', (e) => { e.stopPropagation(); openImagePopup(); });
  document.getElementById('multiLightCard')?.addEventListener('click', (e) => e.stopPropagation());
  document.getElementById('multiImageCard')?.addEventListener('click', (e) => e.stopPropagation());
  document.addEventListener('click', (e) => {
    [[multiLightPopup, 'multiLightCard', 'multiLightBtn'], [multiImagePopup, 'multiImageCard', 'multiImageBtn']]
      .forEach(([p, cardId, btnId]) => {
        if (!p || !p.isOpen()) return;
        const card = document.getElementById(cardId), btn = document.getElementById(btnId);
        if (card && !card.contains(e.target) && btn && !btn.contains(e.target)) p.close();
      });
  });

  // сеть: popup под иконкой + смена IP
  multiNetworkPopup = UIHelpers.createPopupController(
    document.getElementById('multiNetworkCard'), document.getElementById('multiNetworkBtn'));
  document.getElementById('multiNetworkBtn')?.addEventListener('click', (e) => {
    e.stopPropagation(); openNetworkPopup();
  });
  document.getElementById('multiNetworkApplyBtn')?.addEventListener('click', applyMultiNetwork);
  document.getElementById('multiNetworkDhcp')?.addEventListener('change', reflectMultiDhcp);
  document.getElementById('multiNetworkCard')?.addEventListener('click', (e) => e.stopPropagation());

  // закрыть popup (оптики/сети) по клику вне него и вне его кнопки
  document.addEventListener('click', (e) => {
    [['multiOpticPopup', 'multiOpticCard', 'multiOpticBtn'],
     ['multiNetworkPopup', 'multiNetworkCard', 'multiNetworkBtn']].forEach(([pop, cardId, btnId]) => {
      const p = (pop === 'multiOpticPopup') ? multiOpticPopup : multiNetworkPopup;
      if (!p || !p.isOpen()) return;
      const card = document.getElementById(cardId);
      const btn = document.getElementById(btnId);
      if (card && !card.contains(e.target) && btn && !btn.contains(e.target)) p.close();
    });
  });
}

function initModals() {
  // единая модалка настроек: зоны + управление
  document.getElementById('multiPhotoOn')?.addEventListener('click', () => applyPhoto(true));
  document.getElementById('multiPhotoOff')?.addEventListener('click', () => applyPhoto(false));
  document.getElementById('multiVideoOn')?.addEventListener('click', () => applyVideo(true));
  document.getElementById('multiVideoOff')?.addEventListener('click', () => applyVideo(false));
  document.getElementById('multiLightSwitch')?.addEventListener('change', (e) => applyMultiLight(e.target.checked));

  // настройки изображения: применяем сразу при изменении (ползунки — по отпусканию)
  document.getElementById('multiImageWb')?.addEventListener('change', applyMultiWhiteBalance);
  document.getElementById('multiImageDayNight')?.addEventListener('change', applyMultiDayNight);
  document.getElementById('multiImageCompensation')?.addEventListener('change', (e) => applyMultiExposure('compensation', e.target.value));
  document.getElementById('multiImageGainMin')?.addEventListener('change', (e) => applyMultiExposure('gain_min', e.target.value));
  document.getElementById('multiImageGainMax')?.addEventListener('change', (e) => applyMultiExposure('gain_max', e.target.value));

  document.getElementById('multiInfoClose')?.addEventListener('click', () => closeModal('multiInfoModal'));
  document.getElementById('multiInfoCloseFooter')?.addEventListener('click', () => closeModal('multiInfoModal'));

  document.getElementById('multiConfigClose')?.addEventListener('click', () => closeModal('multiConfigModal'));
  document.getElementById('multiConfigCloseFooter')?.addEventListener('click', () => closeModal('multiConfigModal'));

  document.getElementById('multiAddRtspBtn')?.addEventListener('click', openRtspModal);
  document.getElementById('multiRtspClose')?.addEventListener('click', () => closeModal('multiRtspModal'));
  document.getElementById('multiRtspCancel')?.addEventListener('click', () => closeModal('multiRtspModal'));
  document.getElementById('multiRtspAdd')?.addEventListener('click', addRtspSource);

  // клик по фону и Esc закрывают модалки
  document.querySelectorAll('.modal-backdrop').forEach((modal) => {
    modal.addEventListener('click', (event) => {
      if (event.target === modal) closeModal(modal.id);
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    document.querySelectorAll('.modal-backdrop.show').forEach((modal) => closeModal(modal.id));
  });
}

async function initMultiPage() {
  log.info('Инициализация страницы мультипоточности');

  initLayoutButtons();
  initToolbar();
  initModals();
  document.getElementById('multiRefreshBtn')?.addEventListener('click', loadCameras);

  setLayout(1);
  await loadCameras();
  await loadSavedRtsp();
  startMetricsPolling();

  window.addEventListener('beforeunload', () => {
    if (metricsTimer) clearInterval(metricsTimer);
    state.tiles.forEach((tile) => {
      if (tile.connected && tile.serial) {
        const base = tile.kind === 'rtsp' ? '/api/rtsp/close_stream_force' : '/api/camera/close_stream_force';
        fetch(`${base}?serial_number=${encodeURIComponent(tile.serial)}`, { keepalive: true }).catch(() => {});
      }
    });
  });
}

window.addEventListener('DOMContentLoaded', () => {
  initMultiPage().catch((error) => {
    log.error('Ошибка инициализации мультипоточности', { error: error?.message ?? String(error) });
  });
});

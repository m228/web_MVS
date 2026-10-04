const log = {
  info: (m, p) => window.AppLog?.info('multi', m, p),
  success: (m, p) => window.AppLog?.success('multi', m, p),
  warn: (m, p) => window.AppLog?.warn('multi', m, p),
  error: (m, p) => window.AppLog?.error('multi', m, p),
  debug: (m, p) => window.AppLog?.debug('multi', m, p),
};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (s) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[s]));
}

const INFO_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><line x1="12" y1="11" x2="12" y2="16"></line><circle cx="12" cy="7.5" r="1" fill="currentColor" stroke="none"></circle></svg>';

const PHOTO_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>';

const VIDEO_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>';

const LIGHT_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.4 1 2.5h6c0-1.1.3-1.8 1-2.5A6 6 0 0 0 12 3z"></path></svg>';

const ZOOM_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"></path></svg>';

const REGION_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3"></path><path d="M8.5 12h7M12 8.5v7"></path></svg>';

function defaultGigeSettings() {
  return {
    width: 2448, height: 2048, offset_x: 0, offset_y: 0,
    fps: 1, exposure_auto: 'Off', exposure_time: 10000, pixel_format: '',
  };
}

// ---------- состояние ----------
const state = {
  layout: 1,
  tiles: [],          // { serial, kind, connected, photo, video }
  focused: 0,
  cameras: [],        // источники: { serial, kind:'gige'|'rtsp', label, model, ip, available, settings, connection }
  expandedSerial: null,
  rtspCounter: 0,
};

function getSource(serial) {
  return state.cameras.find((c) => c.serial === serial) || null;
}

function apiFor(kind) {
  return kind === 'rtsp' ? RtspApi : CameraApi;
}


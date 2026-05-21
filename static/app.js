/* ══════════════════════════════════════════════════════════════
   RoSE Ground Station — Frontend application
══════════════════════════════════════════════════════════════ */

'use strict';

// ── Constants ────────────────────────────────────────────────
const WS_ORIGIN   = `ws://${location.host}`;
const TOKEN_KEY   = 'rose_token';

// ── Global state ─────────────────────────────────────────────
const state = {
  estopActive: false,
  augerOn: false,
  hiddenCameras: new Set(),
  visibleCameras: new Set(),
  cameraRotations: {},
  cameraStatuses: {},
  cameraStatusTimers: {},
  nativeCameras: new Set(),
  cameraVisibilitySaved: false,
  cameras: [],
  stillPausedCameras: [],
  warnings: {
    soc:  { warning: 30, critical: 15 },
    cur:  { warning: 20, critical: 30 },
    temp: { warning: 60, critical: 80 },
  },
  logTotal: 0,
  logErrors: 0,
  logWarns:  0,
  logFilter: '',
  logLevel:  'ALL',
  alertLogKeys: new Map(),
  gsUrl: '',
  missionMode: 'default',
  dashboardTimer: {
    elapsedMs: 0,
    running: false,
    laps: [],
    serverTs: 0,
    receivedAt: 0,
  },
  network: {
    pollTimer: null,
    saving: false,
  },
};

// ══════════════════════════════════════════════════════════════
// TOKEN MANAGEMENT
// ══════════════════════════════════════════════════════════════

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

function setToken(t) {
  sessionStorage.setItem(TOKEN_KEY, t);
}

function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

/** Append ?token=... to a URL string */
function withToken(url) {
  const t = getToken();
  const sep = url.includes('?') ? '&' : '?';
  return t ? `${url}${sep}token=${encodeURIComponent(t)}` : url;
}

// ══════════════════════════════════════════════════════════════
// TOAST NOTIFICATIONS
// ══════════════════════════════════════════════════════════════

const TOAST_ICONS = { error: '⊗', warn: '⚠', info: 'ℹ', success: '✓' };
const IMPORTANT_LOG_SOURCES = new Set([
  'ros2', 'bridge', 'rclpy', 'estop', 'can', 'drive', 'arm', 'payload', 'comms', 'radio', 'battery'
]);

function showToast(type, title, message, duration = 5000) {
  const container = document.getElementById('toastContainer');
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.innerHTML = `
    <span class="toast-icon ${type}">${TOAST_ICONS[type] || 'ℹ'}</span>
    <div class="toast-body">
      <div class="toast-title">${escHtml(title)}</div>
      ${message ? `<div class="toast-msg">${escHtml(String(message))}</div>` : ''}
    </div>
    <button class="toast-close" title="Dismiss">✕</button>
  `;
  t.querySelector('.toast-close').addEventListener('click', () => dismissToast(t));
  container.appendChild(t);
  if (duration > 0) setTimeout(() => dismissToast(t), duration);
  return t;
}

function dismissToast(el) {
  if (!el || !el.parentNode) return;
  el.classList.add('fade-out');
  setTimeout(() => el.remove(), 320);
}

function isImportantLog(entry) {
  const lvl = (entry.level || '').toUpperCase();
  const source = String(entry.source || '').toLowerCase();
  const msg = String(entry.msg || '').toLowerCase();
  if (lvl === 'ERROR') return true;
  if (lvl !== 'WARN') return false;
  if (IMPORTANT_LOG_SOURCES.has(source)) return true;
  return ['ros', 'timeout', 'disconnect', 'lost', 'failed', 'fault', 'critical', 'estop', 'battery'].some(term => msg.includes(term));
}

function maybeShowImportantLogToast(entry) {
  if (!isImportantLog(entry)) return;
  const lvl = (entry.level || '').toUpperCase();
  const key = `${lvl}:${entry.source || ''}:${entry.msg || ''}`;
  const now = Date.now();
  const last = state.alertLogKeys.get(key) || 0;
  if (now - last < 15000) return;
  state.alertLogKeys.set(key, now);
  if (state.alertLogKeys.size > 200) {
    const oldest = [...state.alertLogKeys.keys()].slice(0, 50);
    oldest.forEach(k => state.alertLogKeys.delete(k));
  }
  showToast(
    lvl === 'ERROR' ? 'error' : 'warn',
    `ROS2 ${lvl}`,
    `[${entry.source || 'system'}] ${entry.msg || ''}`,
    lvl === 'ERROR' ? 0 : 10000
  );
}

// ══════════════════════════════════════════════════════════════
// UTILITY
// ══════════════════════════════════════════════════════════════

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function apiFetch(path, options = {}) {
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 8000);
  const method = (options.method || 'GET').toUpperCase();
  try {
    const headers = { ...(options.headers || {}) };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(path, { signal: ctrl.signal, ...options, headers });
    clearTimeout(timeout);

    // Token rejected mid-session → force re-login
    if (res.status === 401 || res.status === 403) {
      clearToken();
      showAuthOverlay();
      throw new Error('Session expired — please log in again');
    }

    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        detail = j.detail || j.error || detail;
      } catch (_) {
        try {
          const text = await res.text();
          if (text) detail = text.slice(0, 500);
        } catch (_) {}
      }
      throw new Error(`${method} ${path} failed: ${detail}`);
    }
    return await res.json();
  } catch (err) {
    clearTimeout(timeout);
    if (err.name === 'AbortError') throw new Error(`${method} ${path} timed out after 8 seconds`);
    throw err;
  }
}

function fmtTime(isoStr) {
  try { return new Date(isoStr).toLocaleTimeString('en-US', { hour12: false, fractionalSecondDigits: 2 }); }
  catch (_) { return isoStr; }
}

function asFiniteNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function fmtNumber(value, digits = 1, suffix = '') {
  const n = asFiniteNumber(value);
  return n === null ? '--' : `${n.toFixed(digits)}${suffix}`;
}

function setBarWidth(id, value, maxVal = 100) {
  const bar = document.getElementById(id);
  if (!bar) return;
  const n = asFiniteNumber(value);
  const max = asFiniteNumber(maxVal) || 100;
  bar.style.width = n === null ? '0%' : `${Math.min(100, Math.max(0, (n / max) * 100))}%`;
}

// ══════════════════════════════════════════════════════════════
// AUTHENTICATION
// ══════════════════════════════════════════════════════════════

function showAuthOverlay() {
  document.getElementById('authOverlay').classList.remove('hidden');
  document.getElementById('authPassword').value = '';
  document.getElementById('authPassword').focus();
  hideAuthError();
}

function hideAuthOverlay() {
  document.getElementById('authOverlay').classList.add('hidden');
}

function showAuthError(msg) {
  const el = document.getElementById('authError');
  el.textContent = msg;
  el.classList.remove('hidden');
  const input = document.getElementById('authPassword');
  input.classList.add('shake');
  setTimeout(() => input.classList.remove('shake'), 400);
}

function hideAuthError() {
  document.getElementById('authError').classList.add('hidden');
}

async function initAuth() {
  // Fetch server info to show the URL on the login screen (public endpoint)
  try {
    const info = await fetch('/api/info').then(r => r.json());
    const primaryUrl = info.urls?.[0] || `http://${location.host}`;
    state.gsUrl = primaryUrl;
    document.getElementById('authConnectUrl').textContent =
      `Connect at: ${primaryUrl}`;
    populateGsUrlChip(info.urls || [primaryUrl]);
  } catch (_) {
    document.getElementById('authConnectUrl').textContent = `Connect at: http://${location.host}`;
    state.gsUrl = `http://${location.host}`;
    populateGsUrlChip([`http://${location.host}`]);
  }

  // Check existing token
  const existing = getToken();
  if (existing) {
    try {
      await apiFetch('/api/health');
      // Token still valid — skip login screen
      hideAuthOverlay();
      bootApp();
      return;
    } catch (_) {
      clearToken();
    }
  }

  // Show login form
  showAuthOverlay();

  document.getElementById('authForm').addEventListener('submit', async e => {
    e.preventDefault();
    const password = document.getElementById('authPassword').value;
    const btn      = document.getElementById('authSubmit');
    const lbl      = document.getElementById('authSubmitLabel');
    if (!password) return;

    btn.disabled = true;
    lbl.textContent = 'Connecting…';
    hideAuthError();

    try {
      const res = await fetch('/api/auth', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || 'Incorrect password');
      }
      const { token } = await res.json();
      setToken(token);
      hideAuthOverlay();
      bootApp();
    } catch (err) {
      showAuthError(err.message);
    } finally {
      btn.disabled = false;
      lbl.textContent = 'Connect';
    }
  });
}

// ══════════════════════════════════════════════════════════════
// GS URL CHIP (header)
// ══════════════════════════════════════════════════════════════

function populateGsUrlChip(urls) {
  const primaryUrl = urls[0] || `http://${location.host}`;
  const chip = document.getElementById('gsUrlChip');
  document.getElementById('gsUrlText').textContent = primaryUrl;

  // Tooltip with all IPs
  if (urls.length > 1) chip.title = `All addresses:\n${urls.join('\n')}\nClick to copy`;

  chip.addEventListener('click', () => {
    navigator.clipboard.writeText(primaryUrl).then(() => {
      const copied = document.getElementById('gsUrlCopied');
      copied.classList.remove('hidden');
      setTimeout(() => copied.classList.add('hidden'), 1500);
    }).catch(() => showToast('warn', 'Copy failed', 'Use Ctrl+C to copy manually'));
  });
}

// ══════════════════════════════════════════════════════════════
// TAB NAVIGATION
// ══════════════════════════════════════════════════════════════

function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => activateTab(btn.dataset.tab));
  });
}

function activateTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${name}`));
  if (name === 'dashboard' && _map) _map.invalidateSize();
}

// ══════════════════════════════════════════════════════════════
// WEBSOCKET — TELEMETRY
// ══════════════════════════════════════════════════════════════

let _telemWS = null;
let _telemRetry = 1000;

function connectTelemetryWS() {
  const url = withToken(`${WS_ORIGIN}/ws/telemetry`);
  _telemWS = new WebSocket(url);

  _telemWS.addEventListener('open', () => {
    _telemRetry = 1000;
    setApiStatus(true);
  });

  _telemWS.addEventListener('message', e => {
    try { handleTelemetryMsg(JSON.parse(e.data)); } catch (_) {}
  });

  _telemWS.addEventListener('close', e => {
    setApiStatus(false);
    // Code 4001 means auth rejected — force re-login
    if (e.code === 4001) { clearToken(); showAuthOverlay(); return; }
    setTimeout(connectTelemetryWS, Math.min(_telemRetry, 30000));
    _telemRetry = Math.min(_telemRetry * 1.5, 30000);
  });

  _telemWS.addEventListener('error', () => _telemWS.close());
}

function setApiStatus(online) {
  const dot   = document.getElementById('apiDot');
  const label = document.getElementById('apiLabel');
  dot.className     = `status-dot ${online ? 'dot-green' : 'dot-red'}`;
  label.textContent = online ? 'API Online' : 'Reconnecting…';
}

function handleTelemetryMsg(msg) {
  if (msg.type !== 'telemetry') return;
  updateTelemetry(msg.telemetry);
  updateGPS(msg.gnss);
  updateSubsysStatus(msg.payload_connected, msg.arm_connected, msg.drive_connected);
  updateSubsystemOverview(msg.subsystems || {});
  updateComms(msg.comms || {});
  updatePayloadArduino(msg.payload_arduino || {});
  updateLifeAnalysis(msg.life_analysis || {});
  updateLedController(msg.led_controller || {});
  updateMotorTelemetry(msg.motor_telemetry || {});
  updateDashboardTimer(msg.dashboard_timer || {});
}

// ══════════════════════════════════════════════════════════════
// TELEMETRY DISPLAY
// ══════════════════════════════════════════════════════════════

function levelClass(value, warn, crit) {
  if (!Number.isFinite(Number(value))) return 'warn';
  if (value >= crit) return 'crit';
  if (value >= warn) return 'warn';
  return 'ok';
}

function levelClassInv(value, warn, crit) {
  if (!Number.isFinite(Number(value))) return 'warn';
  if (value <= crit) return 'crit';
  if (value <= warn) return 'warn';
  return 'ok';
}

function applyTelemetryCard(id, barId, badgeId, value, maxVal, cls) {
  const card  = document.getElementById(id);
  const bar   = document.getElementById(barId);
  const badge = document.getElementById(badgeId);
  if (!card || !bar || !badge) return;
  const n = asFiniteNumber(value);
  const pct = n === null ? 0 : Math.min(100, Math.max(0, (n / maxVal) * 100));

  bar.style.width     = pct + '%';
  card.className      = 'telem-card' + (cls !== 'ok' ? ` state-${cls}` : '');
  bar.className       = 'telem-bar'  + (cls !== 'ok' ? ` ${cls}` : '');
  badge.className     = 'telem-badge' + (cls !== 'ok' ? ` ${cls}` : '');
  badge.textContent   = n === null ? 'NO DATA' : cls.toUpperCase();
}

function updateTelemetry(t) {
  if (!document.getElementById('socVal')) return;
  t = t || {};
  const w = state.warnings;

  const socCls = levelClassInv(t.soc, w.soc.warning, w.soc.critical);
  document.getElementById('socVal').textContent = fmtNumber(t.soc, 1);
  applyTelemetryCard('socCard', 'socBar', 'socBadge', t.soc, 100, socCls);

  const curCls = levelClass(t.current, w.cur.warning, w.cur.critical);
  document.getElementById('curVal').textContent = fmtNumber(t.current, 1);
  applyTelemetryCard('curCard', 'curBar', 'curBadge', t.current, w.cur.critical * 1.2, curCls);

  const tempCls = levelClass(t.temperature, w.temp.warning, w.temp.critical);
  document.getElementById('tempVal').textContent = fmtNumber(t.temperature, 1);
  applyTelemetryCard('tempCard', 'tempBar', 'tempBadge', t.temperature, w.temp.critical * 1.2, tempCls);

  document.getElementById('voltVal').textContent = fmtNumber(t.voltage, 1);
}

// ══════════════════════════════════════════════════════════════
// GPS MAP
// ══════════════════════════════════════════════════════════════

let _map = null;
let _roverMarker = null;
let _gpsTrail = null;
let _trailPoints = [];
const MAX_TRAIL = 120;

function initMap() {
  _map = L.map('gpsMap', { center: [43.6532, -79.3832], zoom: 17, attributionControl: false });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 20 }).addTo(_map);

  const icon = L.divIcon({
    className: '',
    html: `<div style="width:14px;height:14px;border-radius:50%;background:#ff37a8;border:2px solid #fff;box-shadow:0 0 8px #ff37a8aa"></div>`,
    iconSize: [14, 14], iconAnchor: [7, 7],
  });

  _roverMarker = L.marker([43.6532, -79.3832], { icon }).addTo(_map);
  _gpsTrail = L.polyline([], { color: '#ff37a8', weight: 2, opacity: 0.5 }).addTo(_map);
}

function updateGPS(g) {
  g = g || {};
  if (!g.valid || !_map) return;
  const lat = asFiniteNumber(g.lat);
  const lon = asFiniteNumber(g.lon);
  if (lat === null || lon === null) return;
  const ll = [lat, lon];
  _roverMarker.setLatLng(ll);
  _trailPoints.push(ll);
  if (_trailPoints.length > MAX_TRAIL) _trailPoints.shift();
  _gpsTrail.setLatLngs(_trailPoints);
  if (!_map.getBounds().contains(ll)) _map.panTo(ll, { animate: true, duration: 0.5 });

  document.getElementById('gpsLat').textContent     = lat.toFixed(6);
  document.getElementById('gpsLon').textContent     = lon.toFixed(6);
  document.getElementById('gpsFix').textContent     = g.fix;
  document.getElementById('gpsSats').textContent    = g.satellites ?? '--';
  document.getElementById('gpsUpdated').textContent = `Updated ${new Date().toLocaleTimeString()}`;

  const badge = document.getElementById('gpsBadge');
  badge.textContent = g.fix === '3D' ? '3D FIX' : g.fix === '2D' ? '2D FIX' : 'NO FIX';
  badge.className   = `gps-fix-badge${g.fix === '3D' ? ' fix3d' : g.fix === '2D' ? ' fix2d' : ''}`;
}

// ══════════════════════════════════════════════════════════════
// CONTROLLER STATUS (Gamepad API — fully client-side)
// ══════════════════════════════════════════════════════════════

function initGamepadPolling() {
  setInterval(() => {
    const gamepads  = navigator.getGamepads ? navigator.getGamepads() : [];
    const connected = Array.from(gamepads).some(g => g !== null && g.connected);
    const dot   = document.getElementById('controllerDot');
    const label = document.getElementById('controllerLabel');
    if (connected) {
      const gp = Array.from(gamepads).find(g => g && g.connected);
      dot.className     = 'status-dot dot-green';
      label.textContent = (gp.id || 'Controller').substring(0, 22);
    } else {
      dot.className     = 'status-dot dot-red';
      label.textContent = 'No Controller';
    }
  }, 500);

  window.addEventListener('gamepadconnected',    e => showToast('success', 'Controller Connected', e.gamepad.id));
  window.addEventListener('gamepaddisconnected', ()  => showToast('warn',  'Controller Disconnected', 'No gamepad detected'));
}

// ══════════════════════════════════════════════════════════════
// E-STOP
// ══════════════════════════════════════════════════════════════

function initEStop() {
  document.getElementById('resetEstopBtn')?.addEventListener('click', resetEstop);
  document.getElementById('capture360Btn')?.addEventListener('click', capture360Image);
  document.getElementById('closeCapture360Modal')?.addEventListener('click', closeCapture360Modal);
}

async function activateEstop() {
  state.estopActive = true;
  document.getElementById('estopOverlay').classList.remove('hidden');
  try {
    await apiFetch('/api/estop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: true }),
    });
  } catch (err) {
    showToast('error', 'E-Stop API Error', err.message);
  }
}

async function resetEstop() {
  state.estopActive = false;
  document.getElementById('estopOverlay').classList.add('hidden');
  try {
    await apiFetch('/api/estop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: false }),
    });
    showToast('info', 'E-Stop Reset', 'System ready');
  } catch (err) {
    showToast('error', 'E-Stop Reset Error', err.message);
  }
}

function closeCapture360Modal() {
  document.getElementById('capture360Modal').classList.add('hidden');
}

async function closeCameraStillModal() {
  document.getElementById('cameraStillModal')?.classList.add('hidden');
  const paused = [...(state.stillPausedCameras || [])];
  state.stillPausedCameras = [];
  for (const id of paused) {
    if (state.visibleCameras.has(String(id))) {
      await showCamera(String(id), { persist: false, quiet: true });
    }
  }
}

async function capture360Image() {
  const modal = document.getElementById('capture360Modal');
  const status = document.getElementById('capture360Status');
  const command = document.getElementById('capture360Command');
  const img = document.getElementById('capture360Img');
  const placeholder = document.getElementById('capture360Placeholder');
  modal.classList.remove('hidden');
  status.textContent = 'Sending servo command to LED Arduino and stitching camera frames...';
  command.textContent = 'Command bytes: pending';
  img.classList.add('hidden');
  placeholder.classList.remove('hidden');

  try {
    const res = await apiFetch('/api/camera360/capture', { method: 'POST' });
    const capture = res.capture || {};
    const bytes = (capture.command_bytes || []).map(b => `0x${Number(b).toString(16).padStart(2, '0').toUpperCase()}`).join(' ');
    command.textContent = `Command bytes: ${bytes || '--'}`;
    img.src = capture.image_data_url || '';
    img.classList.toggle('hidden', !capture.image_data_url);
    placeholder.classList.toggle('hidden', !!capture.image_data_url);
    status.textContent = `Captured ${capture.source_frames || 0} frames and stitched on Jetson`;
  } catch (err) {
    status.textContent = `Capture failed: ${err.message}`;
    showToast('error', '360 Capture Failed', err.message);
  }
}

async function captureCameraStill(id) {
  id = String(id);
  const modal = document.getElementById('cameraStillModal');
  const title = document.getElementById('cameraStillTitle');
  const status = document.getElementById('cameraStillStatus');
  const img = document.getElementById('cameraStillImg');
  const placeholder = document.getElementById('cameraStillPlaceholder');
  const download = document.getElementById('cameraStillDownload');

  modal?.classList.remove('hidden');
  if (title) title.textContent = `${cameraName(id)} Still`;
  if (status) status.textContent = 'Pausing streams and capturing HD still...';
  if (img) {
    img.classList.add('hidden');
    img.removeAttribute('src');
  }
  placeholder?.classList.remove('hidden');
  download?.classList.add('hidden');

  try {
    const res = await apiFetch(`/api/camera/${encodeURIComponent(id)}/still`, { method: 'POST' });
    const image = res.image || {};
    state.stillPausedCameras = (res.paused_camera_ids || []).map(String);
    if (!image.image_data_url) throw new Error('No image returned from rover');
    img.src = image.image_data_url;
    img.classList.remove('hidden');
    placeholder?.classList.add('hidden');
    const filename = `${cameraName(id).replace(/[^a-z0-9_-]+/gi, '_')}_${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`;
    download.href = image.image_data_url;
    download.download = filename;
    download.classList.remove('hidden');
    status.textContent = `Captured ${image.width || '--'}x${image.height || '--'} still. Streams resume when this popup closes.`;
  } catch (err) {
    status.textContent = `Still capture failed: ${err.message}`;
    showToast('error', 'Still Capture Failed', err.message);
  }
}

// ══════════════════════════════════════════════════════════════
// CAMERA FEEDS — src set after auth so token can be appended
// ══════════════════════════════════════════════════════════════

function initCameras() {
  loadCameraRotations();
  try {
    const raw = localStorage.getItem('rose_visible_cameras');
    state.cameraVisibilitySaved = raw !== null;
    const saved = JSON.parse(raw || '[]');
    state.visibleCameras = new Set(saved.map(String));
  } catch (_) {
    state.visibleCameras = new Set();
    state.cameraVisibilitySaved = false;
  }

  document.getElementById('refreshCamerasBtn')?.addEventListener('click', () => refreshCameras(true));
  document.getElementById('showAllCamerasBtn')?.addEventListener('click', showAllCameras);
  document.getElementById('closeCameraStillModal')?.addEventListener('click', closeCameraStillModal);
  refreshCameras(false).catch(() => renderCameraGrid());
  setInterval(refreshCameraStatus, 3000);
}

async function refreshCameras(force = true) {
  const priorIds = cameraIdsSignature();
  let res;
  try {
    res = force
      ? await apiFetch('/api/cameras/refresh', { method: 'POST' })
      : await apiFetch('/api/cameras');
  } catch (err) {
    if (force) showToast('error', 'Camera Refresh Failed', err.message);
    throw err;
  }
  state.cameras = res.cameras || [];
  const known = new Set(state.cameras.map(cam => String(cam.id)));
  if (!state.cameraVisibilitySaved) {
    state.visibleCameras = new Set(state.cameras.map(cam => String(cam.id)));
  }
  state.hiddenCameras = new Set(state.cameras.map(cam => String(cam.id)).filter(id => !state.visibleCameras.has(id)));
  state.visibleCameras = new Set([...state.visibleCameras].filter(id => known.has(id)));
  const nextIds = cameraIdsSignature();
  if (priorIds !== nextIds || force) renderCameraGrid();
  else updateCameraLabels();
  renderCameraVisibility();
}

async function refreshCameraStatus() {
  if (!state.cameras.length) return;
  try {
    const res = await apiFetch('/api/cameras');
    state.cameras = res.cameras || [];
    updateCameraLabels();
    renderCameraVisibility();
    state.cameras.forEach(cam => updateCameraStatusFromMetadata(String(cam.id), cam));
  } catch (_) {}
}

function cameraIdsSignature() {
  return state.cameras.map(cam => String(cam.id)).sort().join('|');
}

function retryCam(id) {
  const safeId = cssSafeId(id);
  const img = document.getElementById(`camImg-${safeId}`);
  if (!img) return;
  if (state.nativeCameras.has(String(id))) return;
  clearCameraStatusTimer(id);
  setCameraStatus(id, 'connecting', 'Waiting for video frames', 'Ground station MJPEG endpoint is open. Waiting for the first JPEG frame from the rover receiver.');
  img.src = withToken(`/api/camera/${encodeURIComponent(id)}`) + `&_=${Date.now()}`;
  state.cameraStatusTimers[id] = setTimeout(() => {
    setCameraStatus(id, 'waiting', 'No frame received yet', 'The stream request was sent, but no decoded JPEG frame has reached the browser. Check rover camera process, Rocket M2 congestion, or packet loss.');
  }, 6000);
  applyCameraRotation(id);
}

function stopNativeCamera(id) {
  id = String(id);
  state.nativeCameras.delete(id);
  const safeId = cssSafeId(id);
  const video = document.getElementById(`camVideo-${safeId}`);
  const img = document.getElementById(`camImg-${safeId}`);
  if (video) {
    video.pause();
    video.removeAttribute('src');
    video.load();
    video.classList.add('hidden');
  }
  if (img) {
    img.src = '';
    img.classList.remove('hidden');
  }
}

function cameraName(id) {
  const cam = state.cameras.find(c => String(c.id) === String(id));
  return cam?.label || cam?.device || `Camera ${id}`;
}

function updateCameraLabels() {
  state.cameras.forEach(cam => {
    const id = String(cam.id);
    const input = document.getElementById(`camLabel-${cssSafeId(id)}`);
    if (input && document.activeElement !== input) input.value = cameraName(id);
  });
}

function cameraStatusDetail(cam) {
  if (!cam) return 'Camera has not been discovered by the rover service yet.';
  const bits = [];
  if (cam.port) bits.push(`UDP ${cam.port}`);
  if (cam.mode?.width && cam.mode?.height) bits.push(`${cam.mode.width}x${cam.mode.height}`);
  if (cam.mode?.fps) bits.push(`${cam.mode.fps} fps`);
  if (cam.bitrate || cam.stream_budget?.bitrate) bits.push(`${cam.bitrate || cam.stream_budget.bitrate} kbps`);
  if (cam.stream_budget?.total_bitrate) bits.push(`shared cap ${cam.stream_budget.total_bitrate} kbps`);
  return bits.length ? bits.join(' · ') : 'Stream is active, waiting for frame metadata.';
}

function clearCameraStatusTimer(id) {
  if (state.cameraStatusTimers[id]) clearTimeout(state.cameraStatusTimers[id]);
  delete state.cameraStatusTimers[id];
}

function setCameraStatus(id, level, title, detail = '') {
  state.cameraStatuses[String(id)] = { level, title, detail };
  renderCameraStatus(id);
}

function renderCameraStatus(id) {
  const status = state.cameraStatuses[String(id)];
  const overlay = document.getElementById(`camStatus-${cssSafeId(id)}`);
  if (!overlay) return;
  if (!status || status.level === 'live') {
    overlay.classList.add('hidden');
    return;
  }
  overlay.classList.remove('hidden');
  overlay.dataset.level = status.level;
  overlay.innerHTML = `
    <div class="camera-spinner" aria-hidden="true"></div>
    <div class="camera-status-title">${escHtml(status.title)}</div>
    <div class="camera-status-detail">${escHtml(status.detail)}</div>
  `;
}

function updateCameraStatusFromMetadata(id, cam) {
  const img = document.getElementById(`camImg-${cssSafeId(id)}`);
  if (state.nativeCameras.has(String(id))) return;
  if (!img || state.hiddenCameras.has(String(id))) return;
  if (!cam?.streaming) {
    setCameraStatus(id, 'idle', 'Stream stopped', 'This camera is visible in the grid but the rover stream is not active yet.');
    return;
  }
  if (!img.complete || !img.naturalWidth) {
    setCameraStatus(id, 'connecting', 'Connecting to stream', cameraStatusDetail(cam));
  }
}

function saveHiddenCameras() {
  state.cameraVisibilitySaved = true;
  localStorage.setItem('rose_visible_cameras', JSON.stringify([...state.visibleCameras]));
}

function loadCameraRotations() {
  try {
    const raw = localStorage.getItem('rose_camera_rotations');
    const parsed = JSON.parse(raw || '{}');
    state.cameraRotations = Object.fromEntries(
      Object.entries(parsed).map(([id, deg]) => [String(id), Number(deg) || 0])
    );
  } catch (_) {
    state.cameraRotations = {};
  }
}

function saveCameraRotations() {
  localStorage.setItem('rose_camera_rotations', JSON.stringify(state.cameraRotations));
}

function applyCameraRotation(id) {
  const img = document.getElementById(`camImg-${cssSafeId(id)}`);
  const video = document.getElementById(`camVideo-${cssSafeId(id)}`);
  const deg = Number(state.cameraRotations[String(id)] || 0) % 360;
  const transform = deg ? `rotate(${deg}deg)` : '';
  if (img) img.style.transform = transform;
  if (video) video.style.transform = transform;
}

function rotateCamera(id) {
  id = String(id);
  const next = (Number(state.cameraRotations[id] || 0) + 90) % 360;
  if (next) state.cameraRotations[id] = next;
  else delete state.cameraRotations[id];
  saveCameraRotations();
  applyCameraRotation(id);
}

async function hideCamera(id) {
  id = String(id);
  stopNativeCamera(id);
  state.hiddenCameras.add(id);
  state.visibleCameras.delete(id);
  saveHiddenCameras();
  renderCameraVisibility();
  setCameraStatus(id, 'idle', 'Camera hidden', 'The rover stream is being stopped to save bandwidth.');
  try {
    await apiFetch(`/api/camera/${encodeURIComponent(id)}/stop`, { method: 'POST' });
  } catch (err) {
    showToast('warn', 'Camera Stop Failed', err.message);
  }
}

async function showCamera(id, options = {}) {
  id = String(id);
  stopNativeCamera(id);
  state.hiddenCameras.delete(id);
  state.visibleCameras.add(id);
  if (options.persist !== false) saveHiddenCameras();
  renderCameraVisibility();
  setCameraStatus(id, 'starting', 'Requesting rover stream', 'Ground station is asking the rover camera service to start or rebalance this stream.');
  try {
    const res = await apiFetch(`/api/camera/${encodeURIComponent(id)}/start`, { method: 'POST' });
    const camera = res.camera;
    if (camera) {
      state.cameras = state.cameras.map(cam => String(cam.id) === id ? { ...cam, ...camera } : cam);
    }
    setCameraStatus(id, 'connecting', 'Stream requested', cameraStatusDetail(camera));
    retryCam(id);
  } catch (err) {
    setCameraStatus(id, 'error', 'Camera start failed', err.message);
    if (!options.quiet) showToast('error', 'Camera Start Failed', err.message);
  }
}

async function showNativeCamera(id) {
  id = String(id);
  const safeId = cssSafeId(id);
  const img = document.getElementById(`camImg-${safeId}`);
  if (!img) return;

  if (state.nativeCameras.has(id)) {
    stopNativeCamera(id);
    showCamera(id, { quiet: true });
    return;
  }

  clearCameraStatusTimer(id);
  setCameraStatus(id, 'starting', 'Starting native stream', 'Stopping the RPi MJPEG receiver and asking the browser to decode the rover H.264 stream directly.');
  try {
    await apiFetch(`/api/camera/${encodeURIComponent(id)}/stop`, { method: 'POST' });
  } catch (_) {}

  try {
    const res = await apiFetch(`/api/camera/${encodeURIComponent(id)}/native`, { method: 'POST' });
    state.nativeCameras.add(id);
    img.src = `${res.url}?_=${Date.now()}`;
    img.classList.remove('hidden');
    setCameraStatus(id, 'connecting', 'Native stream requested', 'The browser is connecting directly to the rover camera service. Decode should happen on this device, not on the RPi.');
  } catch (err) {
    stopNativeCamera(id);
    setCameraStatus(id, 'error', 'Native stream failed', err.message);
    showToast('error', 'Native Camera Failed', err.message);
  }
}

async function saveCameraLabel(id, label) {
  id = String(id);
  const clean = String(label || '').trim();
  try {
    const res = await apiFetch(`/api/camera/${encodeURIComponent(id)}/label`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: clean }),
    });
    if (res.camera) {
      state.cameras = state.cameras.map(cam => String(cam.id) === id ? { ...cam, ...res.camera } : cam);
    }
    updateCameraLabels();
    renderCameraVisibility();
    showToast('success', 'Camera Name Saved', `${cameraName(id)} will persist on this ground station.`, 2500);
  } catch (err) {
    showToast('error', 'Camera Name Save Failed', err.message, 0);
  }
}

function showAllCameras() {
  state.hiddenCameras.clear();
  state.visibleCameras = new Set(state.cameras.map(cam => String(cam.id)));
  saveHiddenCameras();
  renderCameraVisibility();
  state.cameras.forEach(cam => showCamera(String(cam.id)));
}

function cssSafeId(id) {
  return String(id).replace(/[^a-zA-Z0-9_-]/g, '_');
}

function renderCameraGrid() {
  const grid = document.getElementById('cameraGrid');
  if (!grid) return;
  if (!state.cameras.length) {
    grid.innerHTML = '<div class="camera-empty">Press Refresh Cameras to discover connected rover cameras.</div>';
    return;
  }
  grid.innerHTML = state.cameras.map(cam => {
    const id = String(cam.id);
    const safeId = cssSafeId(id);
    return `
      <div class="camera-cell" id="cam-${safeId}" data-camera-id="${escHtml(id)}">
        <input class="camera-label-input" id="camLabel-${safeId}" data-camera-id="${escHtml(id)}" value="${escHtml(cameraName(id))}" title="Edit camera name" />
        <div class="camera-tools">
          <button class="camera-still-btn" data-camera-id="${escHtml(id)}" title="Capture HD still from ${escHtml(cameraName(id))}">Still</button>
          <button class="camera-native-btn" data-camera-id="${escHtml(id)}" title="Experimental native browser decode for ${escHtml(cameraName(id))}">Native</button>
          <button class="camera-rotate-btn" data-camera-id="${escHtml(id)}" title="Rotate ${escHtml(cameraName(id))} camera 90 degrees">Rotate</button>
          <button class="camera-hide-btn" data-camera-id="${escHtml(id)}" title="Hide ${escHtml(cameraName(id))} camera">Hide</button>
        </div>
        <img class="camera-img" id="camImg-${safeId}" alt="${escHtml(cameraName(id))} camera" />
        <video class="camera-video hidden" id="camVideo-${safeId}" muted playsinline controls></video>
        <div class="camera-status" id="camStatus-${safeId}" data-level="idle"></div>
      </div>
    `;
  }).join('');
  grid.querySelectorAll('.camera-label-input').forEach(input => {
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        input.blur();
      }
    });
    input.addEventListener('blur', () => saveCameraLabel(input.dataset.cameraId, input.value));
  });
  grid.querySelectorAll('.camera-hide-btn').forEach(btn => {
    btn.addEventListener('click', () => hideCamera(btn.dataset.cameraId));
  });
  grid.querySelectorAll('.camera-still-btn').forEach(btn => {
    btn.addEventListener('click', () => captureCameraStill(btn.dataset.cameraId));
  });
  grid.querySelectorAll('.camera-native-btn').forEach(btn => {
    btn.addEventListener('click', () => showNativeCamera(btn.dataset.cameraId));
  });
  grid.querySelectorAll('.camera-rotate-btn').forEach(btn => {
    btn.addEventListener('click', () => rotateCamera(btn.dataset.cameraId));
  });
  state.cameras.forEach(cam => {
    const id = String(cam.id);
    const safeId = cssSafeId(id);
    const img = document.getElementById(`camImg-${safeId}`);
    const video = document.getElementById(`camVideo-${safeId}`);
    if (!img) return;
    img.addEventListener('error', () => {
      if (state.nativeCameras.has(id)) return;
      clearCameraStatusTimer(id);
      setCameraStatus(id, 'error', 'Feed unavailable', 'The browser could not read the MJPEG stream. Retrying while the camera remains visible.');
      if (!state.hiddenCameras.has(id)) setTimeout(() => retryCam(id), 3000);
    });
    img.addEventListener('load', () => {
      clearCameraStatusTimer(id);
      setCameraStatus(id, 'live', state.nativeCameras.has(id) ? 'Native decode' : 'Live', '');
      applyCameraRotation(id);
    });
    if (video) {
      video.addEventListener('canplay', () => {
        clearCameraStatusTimer(id);
        setCameraStatus(id, 'live', 'Native decode', 'This browser is decoding the rover stream directly.');
      });
      video.addEventListener('error', () => {
        if (!state.nativeCameras.has(id)) return;
        clearCameraStatusTimer(id);
        setCameraStatus(id, 'error', 'Native feed unavailable', 'The browser could not play the direct rover stream. Switch back to MJPEG or restart the rover camera service.');
      });
    }
    applyCameraRotation(id);
    if (!state.hiddenCameras.has(id)) {
      setCameraStatus(id, 'idle', 'Preparing stream', 'Waiting to request this rover camera stream.');
      if (!state.nativeCameras.has(id)) showCamera(id);
    }
  });
}

function renderCameraVisibility() {
  state.cameras.forEach(cam => {
    const id = String(cam.id);
    const cell = document.getElementById(`cam-${cssSafeId(id)}`);
    if (cell) cell.classList.toggle('camera-hidden', state.hiddenCameras.has(id));
  });
  const dock = document.getElementById('hiddenCameraDock');
  if (!dock) return;
  const known = new Set(state.cameras.map(cam => String(cam.id)));
  const hidden = [...state.hiddenCameras].filter(id => known.has(id)).sort();
  dock.classList.toggle('hidden', hidden.length === 0);
  dock.innerHTML = hidden.map(id => `
    <button class="hidden-camera-btn" data-camera-id="${id}" title="Show ${escHtml(cameraName(id))} camera">
      ${escHtml(cameraName(id))}
    </button>
  `).join('');
  dock.querySelectorAll('.hidden-camera-btn').forEach(btn => {
    btn.addEventListener('click', () => showCamera(btn.dataset.cameraId));
  });
}

// ══════════════════════════════════════════════════════════════
// DASHBOARD MODE BUTTONS + SYNCED TIMER
// ══════════════════════════════════════════════════════════════

const MISSION_TAB_SETS = {
  default: null,
  equipment: new Set(['dashboard', 'cameras']),
  delivery: new Set(['dashboard', 'cameras']),
  autonav: new Set(['dashboard', 'cameras', 'logs', 'files', 'system', 'network']),
};

function initDashboardControls() {
  state.missionMode = localStorage.getItem('rose_mission_mode') || 'default';
  applyMissionMode(state.missionMode);

  document.querySelectorAll('.mission-mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.missionMode;
      state.missionMode = state.missionMode === mode ? 'default' : mode;
      localStorage.setItem('rose_mission_mode', state.missionMode);
      applyMissionMode(state.missionMode);
    });
  });

  document.getElementById('timerPauseBtn')?.addEventListener('click', () => {
    sendTimerAction(state.dashboardTimer.running ? 'pause' : 'start');
  });
  document.getElementById('timerStopBtn')?.addEventListener('click', () => sendTimerAction('stop'));
  document.getElementById('timerResetBtn')?.addEventListener('click', () => sendTimerAction('reset'));
  document.getElementById('timerLapBtn')?.addEventListener('click', () => sendTimerAction('lap'));

  refreshDashboardTimer().catch(() => {});
  setInterval(renderDashboardTimer, 120);
}

function applyMissionMode(mode) {
  const allowed = MISSION_TAB_SETS[mode] || null;
  document.querySelectorAll('.mission-mode-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.missionMode === mode);
  });
  document.querySelectorAll('.tab-btn').forEach(btn => {
    const visible = !allowed || allowed.has(btn.dataset.tab);
    btn.classList.toggle('hidden', !visible);
  });
  const active = document.querySelector('.tab-btn.active')?.dataset.tab;
  if (allowed && active && !allowed.has(active)) activateTab('dashboard');
}

function formatTimer(ms) {
  const totalCentis = Math.max(0, Math.floor(ms / 10));
  const centis = totalCentis % 100;
  const totalSeconds = Math.floor(totalCentis / 100);
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(centis).padStart(2, '0')}`;
  }
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(centis).padStart(2, '0')}`;
}

function currentTimerElapsed() {
  const timer = state.dashboardTimer;
  if (!timer.running) return timer.elapsedMs;
  return timer.elapsedMs + (Date.now() - timer.receivedAt);
}

function updateDashboardTimer(timer) {
  if (!timer || typeof timer.elapsed_ms !== 'number') return;
  state.dashboardTimer = {
    elapsedMs: timer.elapsed_ms,
    running: !!timer.running,
    laps: Array.isArray(timer.laps) ? timer.laps : [],
    serverTs: Number(timer.server_ts || 0) * 1000,
    receivedAt: Date.now(),
  };
  renderDashboardTimer();
}

function renderDashboardTimer() {
  const display = document.getElementById('dashboardTimerDisplay');
  if (!display) return;
  const timer = state.dashboardTimer;
  display.textContent = formatTimer(currentTimerElapsed());

  const stateEl = document.getElementById('timerSyncState');
  if (stateEl) {
    stateEl.textContent = timer.running ? 'RUNNING' : 'PAUSED';
    stateEl.className = `timer-sync-state ${timer.running ? 'running' : 'paused'}`;
  }

  const pauseBtn = document.getElementById('timerPauseBtn');
  if (pauseBtn) pauseBtn.textContent = timer.running ? 'Pause' : 'Start';

  const laps = document.getElementById('timerLaps');
  if (!laps) return;
  laps.innerHTML = timer.laps.slice().reverse().map(lap => `
    <div class="timer-lap">
      <span>Lap ${escHtml(lap.index ?? '')}</span>
      <strong>${formatTimer(Number(lap.elapsed_ms || 0))}</strong>
    </div>
  `).join('');
}

async function refreshDashboardTimer() {
  const res = await apiFetch('/api/dashboard/timer');
  updateDashboardTimer(res.timer || {});
}

async function sendTimerAction(action) {
  try {
    const res = await apiFetch('/api/dashboard/timer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    updateDashboardTimer(res.timer || {});
  } catch (err) {
    showToast('error', 'Timer Sync Failed', err.message);
  }
}

// ══════════════════════════════════════════════════════════════
// WARNING CONFIG MODAL
// ══════════════════════════════════════════════════════════════

function initWarningModal() {
  const saved = localStorage.getItem('rose_warnings');
  if (saved) {
    try {
      const w = JSON.parse(saved);
      Object.assign(state.warnings.soc,  { warning: w.soc.warning,  critical: w.soc.critical  });
      Object.assign(state.warnings.cur,  { warning: w.cur.warning,  critical: w.cur.critical  });
      Object.assign(state.warnings.temp, { warning: w.temp.warning, critical: w.temp.critical });
    } catch (_) {}
  }
  renderThresholdLabels();
  syncWarningsToServer().catch(() => {});

  document.getElementById('openWarningModal')?.addEventListener('click', () => {
    populateWarningModal();
    document.getElementById('warningModal')?.classList.remove('hidden');
  });
  document.getElementById('closeWarningModal')?.addEventListener('click',  closeWarningModal);
  document.getElementById('cancelWarningModal')?.addEventListener('click', closeWarningModal);

  document.getElementById('saveWarningModal')?.addEventListener('click', async () => {
    const w = {
      soc:  { warning: +document.getElementById('wcSocWarn').value,  critical: +document.getElementById('wcSocCrit').value  },
      cur:  { warning: +document.getElementById('wcCurWarn').value,  critical: +document.getElementById('wcCurCrit').value  },
      temp: { warning: +document.getElementById('wcTempWarn').value, critical: +document.getElementById('wcTempCrit').value },
    };
    Object.assign(state.warnings, w);
    localStorage.setItem('rose_warnings', JSON.stringify(w));
    renderThresholdLabels();
    closeWarningModal();
    try {
      await syncWarningsToServer();
      showToast('success', 'Thresholds Updated', 'Warning levels saved');
    } catch (err) {
      showToast('warn', 'Server Sync Failed', err.message);
    }
  });
}

function populateWarningModal() {
  const w = state.warnings;
  document.getElementById('wcSocWarn').value  = w.soc.warning;
  document.getElementById('wcSocCrit').value  = w.soc.critical;
  document.getElementById('wcCurWarn').value  = w.cur.warning;
  document.getElementById('wcCurCrit').value  = w.cur.critical;
  document.getElementById('wcTempWarn').value = w.temp.warning;
  document.getElementById('wcTempCrit').value = w.temp.critical;
}

function closeWarningModal() {
  document.getElementById('warningModal')?.classList.add('hidden');
}

function renderThresholdLabels() {
  const w = state.warnings;
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  setText('socWarnTh', w.soc.warning);
  setText('socCritTh', w.soc.critical);
  setText('curWarnTh', w.cur.warning);
  setText('curCritTh', w.cur.critical);
  setText('tempWarnTh', w.temp.warning);
  setText('tempCritTh', w.temp.critical);
}

async function syncWarningsToServer() {
  const w = state.warnings;
  await apiFetch('/api/warnings/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      soc_warning: w.soc.warning,      soc_critical: w.soc.critical,
      current_warning: w.cur.warning,  current_critical: w.cur.critical,
      temperature_warning: w.temp.warning, temperature_critical: w.temp.critical,
    }),
  });
}

// ══════════════════════════════════════════════════════════════
// LOGS TAB
// ══════════════════════════════════════════════════════════════

let _logWS = null;
let _logRetry = 1000;

function connectLogWS() {
  const url = withToken(`${WS_ORIGIN}/ws/logs`);
  _logWS = new WebSocket(url);

  _logWS.addEventListener('open', () => { _logRetry = 1000; });

  _logWS.addEventListener('message', e => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'logs') appendLogEntries(msg.entries);
    } catch (_) {}
  });

  _logWS.addEventListener('close', e => {
    if (e.code === 4001) return; // auth rejected — don't retry
    setTimeout(connectLogWS, Math.min(_logRetry, 30000));
    _logRetry = Math.min(_logRetry * 1.5, 30000);
  });

  _logWS.addEventListener('error', () => _logWS.close());
}

function appendLogEntries(entries) {
  const console_ = document.getElementById('logConsole');
  const filter   = state.logFilter.toLowerCase();
  const level    = state.logLevel;

  entries.forEach(entry => {
    state.logTotal++;
    const lvl = (entry.level || '').toUpperCase();
    if (lvl === 'ERROR') state.logErrors++;
    if (lvl === 'WARN')  state.logWarns++;
    maybeShowImportantLogToast(entry);

    const el = document.createElement('div');
    el.className = `log-entry log-${lvl.toLowerCase()}`;
    el.dataset.level  = lvl;
    el.dataset.source = (entry.source || '').toLowerCase();
    el.dataset.msg    = (entry.msg || '').toLowerCase();

    el.innerHTML = `
      <span class="log-ts">${fmtTime(entry.ts)}</span>
      <span class="log-level">${escHtml(lvl)}</span>
      <span class="log-source">${escHtml(entry.source || '')}</span>
      <span class="log-msg">${escHtml(entry.msg || '')}</span>
    `;

    if (!matchesLogFilter(el, filter, level)) el.classList.add('log-hidden');
    console_.appendChild(el);
  });

  // Prune DOM
  while (console_.children.length > 2000) console_.removeChild(console_.firstChild);

  updateLogStats();
  if (document.getElementById('logAutoScroll').checked) {
    console_.scrollTop = console_.scrollHeight;
  }
}

function matchesLogFilter(el, filterStr, level) {
  if (level !== 'ALL' && el.dataset.level !== level) return false;
  if (filterStr) {
    const hay = el.dataset.msg + ' ' + el.dataset.source + ' ' + el.dataset.level;
    if (!hay.includes(filterStr)) return false;
  }
  return true;
}

function applyLogFilters() {
  const filter = state.logFilter.toLowerCase();
  const level  = state.logLevel;
  document.querySelectorAll('#logConsole .log-entry').forEach(el => {
    el.classList.toggle('log-hidden', !matchesLogFilter(el, filter, level));
  });
  if (document.getElementById('logAutoScroll').checked) {
    const c = document.getElementById('logConsole');
    c.scrollTop = c.scrollHeight;
  }
}

function updateLogStats() {
  document.getElementById('logTotal').textContent      = state.logTotal;
  document.getElementById('logErrorCount').textContent = state.logErrors;
  document.getElementById('logWarnCount').textContent  = state.logWarns;
}

function initLogs() {
  document.getElementById('logSearch').addEventListener('input', e => {
    state.logFilter = e.target.value;
    applyLogFilters();
  });
  document.getElementById('logLevelFilter').addEventListener('change', e => {
    state.logLevel = e.target.value;
    applyLogFilters();
  });
  document.getElementById('logClearBtn').addEventListener('click', () => {
    document.getElementById('logConsole').innerHTML = '';
    state.logTotal = 0; state.logErrors = 0; state.logWarns = 0;
    updateLogStats();
  });
}

// ══════════════════════════════════════════════════════════════
// FILES TAB
// ══════════════════════════════════════════════════════════════

function initFiles() {
  document.getElementById('getFilesBtn')?.addEventListener('click', () => refreshStorageFiles());
  document.getElementById('refreshFilesBtn')?.addEventListener('click', () => refreshStorageFiles());
}

async function refreshStorageFiles(filePath = '') {
  const btn = document.getElementById('getFilesBtn');
  const refreshBtn = document.getElementById('refreshFilesBtn');
  const status = document.getElementById('filesStatus');
  const icon = document.getElementById('getFilesBtnIcon');
  const viewer = document.getElementById('textFileViewer');
  const content = document.getElementById('textFileContent');
  const meta = document.getElementById('textFileMeta');
  const devicesEl = document.getElementById('usbDeviceList');
  if (btn) btn.disabled = true;
  if (refreshBtn) refreshBtn.disabled = true;
  if (icon) icon.textContent = '↻';
  if (status) {
    status.textContent = filePath ? 'Fetching selected file...' : 'Refreshing external storage...';
    status.className = 'files-status';
  }
  if (viewer) viewer.classList.add('hidden');
  if (content) content.textContent = '';
  if (meta) meta.textContent = '';
  if (devicesEl) devicesEl.innerHTML = '';

  try {
    const data = await apiFetch(filePath ? `/api/files?path=${encodeURIComponent(filePath)}` : '/api/files');
    renderStorageFiles(data);
  } catch (err) {
    const isNoStorage = err.message.includes('503')
      || err.message.toLowerCase().includes('no_storage')
      || err.message.toLowerCase().includes('no external');
    if (isNoStorage) {
      status.textContent = 'No external storage device detected.';
      status.className = 'files-status';
    } else {
      status.textContent = `Error: ${err.message}`;
      status.className = 'files-status error';
      showToast('error', 'Storage Fetch Error', err.message);
    }
  } finally {
    if (btn) btn.disabled = false;
    if (refreshBtn) refreshBtn.disabled = false;
    if (icon) icon.textContent = '⇓';
  }
}

function renderStorageFiles(data) {
  const status = document.getElementById('filesStatus');
  const devicesEl = document.getElementById('usbDeviceList');
  const viewer = document.getElementById('textFileViewer');
  const content = document.getElementById('textFileContent');
  const meta = document.getElementById('textFileMeta');
  const devices = data.devices || [];
  const selected = data.selected_file || null;
  const text = data.content ?? '';

  const fileCount = devices.reduce((sum, device) => sum + (device.text_files || []).length, 0);
  status.textContent = `${devices.length} storage device${devices.length === 1 ? '' : 's'} found, ${fileCount} text file${fileCount === 1 ? '' : 's'} available.`;
  status.className = 'files-status ok';

  devicesEl.innerHTML = devices.map(device => `
    <div class="usb-device">
      <div class="usb-device-info">
        <strong>${escHtml(device.label || device.mount || 'External storage')}</strong>
        <span>${escHtml(device.mount || '')}</span>
        <div class="storage-file-list">
          ${(device.text_files || []).map(file => `
            <button class="storage-file-btn" data-file-path="${escHtml(file.path || file.relative_path || '')}">
              <span>${escHtml(file.relative_path || file.name || 'Text file')}</span>
              <small>${formatBytes(file.size_bytes || 0)}</small>
            </button>
          `).join('') || '<span class="storage-empty">No text files found</span>'}
        </div>
      </div>
      <span>${(device.text_files || []).length} text files</span>
    </div>
  `).join('');

  devicesEl.querySelectorAll('.storage-file-btn').forEach(button => {
    button.addEventListener('click', () => {
      const filePath = button.dataset.filePath || '';
      if (filePath) refreshStorageFiles(filePath);
    });
  });

  if (selected) {
    viewer.classList.remove('hidden');
    meta.textContent = `${selected.relative_path || selected.name} • ${formatBytes(selected.size_bytes || 0)}${selected.truncated ? ' • truncated' : ''}`;
    content.textContent = text;
  } else {
    viewer.classList.remove('hidden');
    meta.textContent = 'No text file found on the detected USB storage.';
    content.textContent = '';
  }
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

// ══════════════════════════════════════════════════════════════
// SYSTEM OVERVIEW
// ══════════════════════════════════════════════════════════════

function updateSubsysStatus(payloadConn, armConn, driveConn) {
  updateControllerTopicHealth(armConn, driveConn);

  const pd = document.querySelector('#sysPayloadConn .status-dot');
  if (pd) {
    pd.className = `status-dot ${payloadConn ? 'dot-green' : 'dot-red'}`;
    document.getElementById('sysPayloadVal').textContent = payloadConn ? 'Connected' : 'Disconnected';
  }
  const ad = document.querySelector('#sysArmConn .status-dot');
  if (ad) {
    ad.className = `status-dot ${armConn ? 'dot-green' : 'dot-red'}`;
    document.getElementById('sysArmVal').textContent = armConn ? 'Connected' : 'Disconnected';
  }

  updateSubsysHeader('payload', payloadConn);

  const pb = document.getElementById('payloadBody');
  if (pb) pb.classList.toggle('greyed', !payloadConn);
  const dd = document.querySelector('#sysDriveConn .status-dot');
  if (dd) {
    dd.className = `status-dot ${driveConn ? 'dot-green' : 'dot-red'}`;
    document.getElementById('sysDriveVal').textContent = driveConn ? 'Connected' : 'Disconnected';
  }
}

function updateControllerTopicHealth(armConn, driveConn) {
  const setHealth = (name, connected) => {
    const label = connected ? 'Online' : 'Offline';
    const dotCls = connected ? 'dot-green' : 'dot-red';
    const headerDot = document.getElementById(`${name}ControllerDot`);
    const headerVal = document.getElementById(`${name}ControllerVal`);
    const dashDot = document.getElementById(`dash${name.charAt(0).toUpperCase() + name.slice(1)}ControllerDot`);
    const dashVal = document.getElementById(`dash${name.charAt(0).toUpperCase() + name.slice(1)}ControllerVal`);
    const dashCard = document.getElementById(`${name}ControllerCard`);
    if (headerDot) headerDot.className = `status-dot ${dotCls}`;
    if (headerVal) headerVal.textContent = label.toUpperCase();
    if (dashDot) dashDot.className = `status-dot ${dotCls}`;
    if (dashVal) dashVal.textContent = `${label} on ROS network`;
    if (dashCard) dashCard.classList.toggle('offline', !connected);
  };
  setHealth('arm', !!armConn);
  setHealth('drive', !!driveConn);
}

function updateSubsysHeader(name, connected) {
  const dot   = document.getElementById(`${name}Dot`);
  const label = document.getElementById(`${name}StatusLabel`);
  if (!dot || !label) return;
  dot.className     = `status-dot ${connected ? 'dot-green' : 'dot-red'}`;
  label.textContent = `${name.charAt(0).toUpperCase() + name.slice(1)} — ${connected ? 'Connected' : 'Disconnected'}`;
}

async function refreshSystemData() {
  try {
    const d = await apiFetch('/api/system');
    const jetsonTemp = d.jetson_cpu_temp ?? d.jetson_temp;
    const jetsonGpuTemp = d.jetson_gpu_temp;
    const currentIp = d.rover_current_ip || d.current_ip || d.rover_ips?.[0] || '--';
    document.getElementById('sysJetsonTemp').textContent = fmtNumber(jetsonTemp, 1, ' °C');
    setBarWidth('sysJetsonTempBar', jetsonTemp, 100);
    document.getElementById('sysJetsonGpuTemp').textContent = fmtNumber(jetsonGpuTemp, 1, ' °C');
    setBarWidth('sysJetsonGpuTempBar', jetsonGpuTemp, 100);
    document.getElementById('sysCurrentIp').textContent = currentIp;
    const netIp = document.getElementById('netGroundStationIp');
    if (netIp) netIp.textContent = d.ground_station_ip || '--';
    const roverIp = document.getElementById('netRoverIp');
    if (roverIp) roverIp.textContent = currentIp;
    updateSubsystemOverview(d.subsystems || {});
    updateComms(d.comms || {});
    updateSystemHealth(d);
    updatePayloadArduino(d.payload_arduino || {});
    updateLifeAnalysis(d.life_analysis || {});
    updateLedController(d.led_controller || {});
    updateMotorTelemetry(d.motor_telemetry || {});
  } catch (_) {
    // Non-blocking
  }
}

function setHealthStatus(dotId, valId, stateName, label) {
  const dot = document.getElementById(dotId);
  const val = document.getElementById(valId);
  if (!dot || !val) return;
  const cls = stateName === 'ok' ? 'dot-green' : stateName === 'warn' ? 'dot-yellow' : stateName === 'bad' ? 'dot-red' : 'dot-grey';
  dot.className = `status-dot ${cls}`;
  val.textContent = label;
}

function updateSystemHealth(d) {
  const subsystems = d.subsystems || {};
  if (subsystems.payload) updateSystemHealthConnection('sysPayloadConn', 'sysPayloadVal', subsystems.payload);
  if (subsystems.arm) updateSystemHealthConnection('sysArmConn', 'sysArmVal', subsystems.arm);
  if (subsystems.drive) updateSystemHealthConnection('sysDriveConn', 'sysDriveVal', subsystems.drive);

  const imuKnown = typeof d.imu_online === 'boolean';
  setHealthStatus('sysImuDot', 'sysImuVal', imuKnown ? (d.imu_online ? 'ok' : 'bad') : 'unknown', imuKnown ? (d.imu_online ? 'Online' : 'Offline') : 'No data');

  const gnssKnown = typeof d.gnss_module_online === 'boolean';
  setHealthStatus('sysGnssDot', 'sysGnssVal', gnssKnown ? (d.gnss_module_online ? 'ok' : 'bad') : 'unknown', gnssKnown ? (d.gnss_module_online ? 'Online' : 'Offline') : 'No data');

  const comms = d.comms || {};
  const stabilities = [comms.link_24ghz?.stability, comms.link_900mhz?.stability]
    .map(asFiniteNumber)
    .filter(v => v !== null);
  const bestRadio = stabilities.length ? Math.max(...stabilities) : null;
  const radioState = bestRadio === null ? 'unknown' : bestRadio >= 70 ? 'ok' : bestRadio >= 45 ? 'warn' : 'bad';
  setHealthStatus('sysRadioDot', 'sysRadioVal', radioState, bestRadio === null ? 'No data' : `${Math.round(bestRadio)}% best link`);

  const motors = Object.values(d.motor_telemetry?.drive || {});
  if (!motors.length) {
    setHealthStatus('sysCanDot', 'sysCanVal', 'unknown', 'No motor data');
  } else {
    const offline = motors.filter(m => !m.connected).length;
    const activeFaults = motors.filter(m => Number(m.faults || 0) > 0).length;
    const stickyFaults = motors.filter(m => Number(m.sticky_faults || 0) > 0).length;
    const canState = offline || activeFaults ? 'bad' : stickyFaults ? 'warn' : 'ok';
    const canLabel = offline
      ? `${offline} offline`
      : activeFaults
        ? `${activeFaults} active faults`
        : stickyFaults
          ? `${stickyFaults} sticky faults`
          : 'Nominal';
    setHealthStatus('sysCanDot', 'sysCanVal', canState, canLabel);
  }
}

function updateSystemHealthConnection(itemId, valId, subsystem) {
  const dot = document.querySelector(`#${itemId} .status-dot`);
  const val = document.getElementById(valId);
  if (!dot || !val) return;
  const connected = subsystem.connected === true;
  const status = connected ? (subsystem.status || 'nominal') : 'critical';
  dot.className = `status-dot ${connected ? (status === 'nominal' ? 'dot-green' : status === 'degraded' ? 'dot-yellow' : 'dot-red') : 'dot-red'}`;
  val.textContent = connected ? (subsystem.summary || 'Connected') : 'Disconnected';
}

function setStatusDot(dotId, value) {
  const dot = document.getElementById(dotId);
  if (dot) dot.className = `status-dot ${value ? 'dot-green' : 'dot-red'}`;
}

function updatePayloadArduino(a) {
  a = a || {};
  setStatusDot('payloadArduinoPubDot', !!a.publisher_active);
  setStatusDot('payloadArduinoSubDot', !!a.subscriber_active);
  setStatusDot('payloadArduinoConnDot', !!a.connected);
  const pubVal = document.getElementById('payloadArduinoPubVal');
  const subVal = document.getElementById('payloadArduinoSubVal');
  const connVal = document.getElementById('payloadArduinoConnVal');
  if (pubVal) pubVal.textContent = a.publisher_active ? 'Active' : 'Not detected';
  if (subVal) subVal.textContent = a.subscriber_active ? 'Active' : 'Not detected';
  if (connVal) connVal.textContent = a.connected ? 'Connected' : 'Disconnected';
  const tempEl = document.getElementById('payloadTempVal');
  const moistureEl = document.getElementById('payloadMoistureVal');
  if (tempEl) tempEl.textContent = fmtNumber(a.temperature_c, 1);
  if (moistureEl) moistureEl.textContent = fmtNumber(a.moisture_pct, 1);
}

function updateLifeAnalysis(life) {
  const status = document.getElementById('lifeAnalysisStatus');
  if (!status) return;
  status.textContent = life.running ? 'Running analysis...' : life.radar ? 'Radar output loaded' : 'Idle';
  if (life.radar?.image_data_url) {
    renderRadarGraph(life.radar.image_data_url);
  }
}

function renderRadarGraph(dataUrl) {
  const output = document.getElementById('radarOutput');
  const img = document.getElementById('radarGraphImg');
  if (!output || !img) return;
  output.querySelector('span')?.classList.add('hidden');
  img.src = dataUrl;
  img.classList.remove('hidden');
}

function updateLedController(c) {
  setStatusDot('ledArduinoDot', !!c.connected);
  setStatusDot('ledArduinoPubDot', !!c.publisher_active);
  setStatusDot('ledArduinoSubDot', !!c.subscriber_active);
  setStatusDot('camera360Dot', !!(c.camera_360_connected && c.camera_360_streaming));

  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  setText('ledArduinoVal', c.connected ? 'Connected' : 'Disconnected');
  setText('ledArduinoPubVal', c.publisher_active ? 'Active' : 'Not detected');
  setText('ledArduinoSubVal', c.subscriber_active ? 'Active' : 'Not detected');
  setText('camera360Val', c.camera_360_connected ? (c.camera_360_streaming ? 'Streaming' : 'Connected, no stream') : 'Disconnected');
  setText('camera360Mode', c.camera_360_mode || '--');

  const grid = document.getElementById('ledGrid');
  if (!grid) return;
  const leds = Array.isArray(c.leds) ? c.leds : [];
  grid.innerHTML = leds.map(led => {
    const r = Number(led.r) || 0;
    const g = Number(led.g) || 0;
    const b = Number(led.b) || 0;
    const color = led.on ? `rgb(${r}, ${g}, ${b})` : '#333';
    const shadow = led.on ? `0 0 10px rgba(${r}, ${g}, ${b}, 0.65)` : 'none';
    return `
      <div class="led-item">
        <div class="led-swatch" style="background:${color};box-shadow:${shadow}"></div>
        <div class="led-info">
          <div class="led-label">${escHtml(led.label || `LED ${led.id ?? ''}`)}</div>
          <div class="led-state${led.on ? '' : ' led-off'}">${led.on ? 'ON' : 'OFF'} · R${r} G${g} B${b}</div>
        </div>
      </div>
    `;
  }).join('') || '<div class="led-empty">No LED telemetry</div>';
}

function linkClass(stability) {
  if (!Number.isFinite(Number(stability))) return 'dot-yellow';
  if (stability >= 70) return 'dot-green';
  if (stability >= 45) return 'dot-yellow';
  return 'dot-red';
}

function updateComms(comms) {
  // Navbar radio chips are driven by Rocket polling so they show Rocket link quality.
  // System health still reads comms directly from /api/system.
  void comms;
}

// ══════════════════════════════════════════════════════════════
// NETWORK TAB — Rocket radio polling
// ══════════════════════════════════════════════════════════════

function networkDotClass(status) {
  if (status === 'good' || status === 'online') return 'dot-green';
  if (status === 'degraded') return 'dot-yellow';
  if (status === 'poor' || status === 'offline') return 'dot-red';
  return 'dot-grey';
}

function networkStatusLabel(status) {
  return String(status || 'unknown').replace(/_/g, ' ').toUpperCase();
}

function formatNullable(value, digits, suffix = '') {
  if (value === null || value === undefined || value === '') return '--';
  const n = asFiniteNumber(value);
  return n === null ? '--' : `${n.toFixed(digits)}${suffix}`;
}

function setNetworkText(prefix, field, text) {
  const el = document.getElementById(`network${prefix}${field}`);
  if (el) el.textContent = text;
}

function updateNetworkCard(prefix, radio) {
  radio = radio || {};
  const status = radio.status || 'unconfigured';
  const card = document.getElementById(`networkCard${prefix}`);
  if (card) card.dataset.status = status;
  setNetworkText(prefix, 'Status', networkStatusLabel(status));
  setNetworkText(prefix, 'Latency', formatNullable(radio.latency_ms, 1));
  const quality = radio.link_quality_pct === null || radio.link_quality_pct === undefined ? null : asFiniteNumber(radio.link_quality_pct);
  setNetworkText(prefix, 'Quality', quality === null ? '--' : String(Math.round(quality)));
  setNetworkText(prefix, 'DisplayIp', radio.ip || '--');
  setNetworkText(prefix, 'Reachable', radio.configured ? (radio.reachable ? 'Reachable' : 'Offline') : 'Not configured');
  setNetworkText(prefix, 'Signal', formatNullable(radio.signal_dbm, 0, ' dBm'));
  setNetworkText(prefix, 'Noise', formatNullable(radio.noise_floor_dbm, 0, ' dBm'));
  setNetworkText(prefix, 'Ccq', formatNullable(radio.ccq_pct, 0, '%'));

  const txErrors = radio.tx_errors === null || radio.tx_errors === undefined ? null : asFiniteNumber(radio.tx_errors);
  const rxErrors = radio.rx_errors === null || radio.rx_errors === undefined ? null : asFiniteNumber(radio.rx_errors);
  const errText = txErrors === null && rxErrors === null ? '--' : `TX ${txErrors ?? '--'} / RX ${rxErrors ?? '--'}`;
  setNetworkText(prefix, 'Errors', errText);
}

function updateHeaderNetworkStatus(m2, m900) {
  const apply = (dotId, valId, radio) => {
    const dot = document.getElementById(dotId);
    const val = document.getElementById(valId);
    if (!dot || !val || !radio) return;
    dot.className = `status-dot ${networkDotClass(radio.status)}`;
    const q = radio.link_quality_pct === null || radio.link_quality_pct === undefined ? null : asFiniteNumber(radio.link_quality_pct);
    val.textContent = q === null ? '--%' : `${Math.round(q)}%`;
  };
  apply('link24Dot', 'link24Val', m2);
  apply('link900Dot', 'link900Val', m900);
}

function updateNetworkStatus(data) {
  const cfg = data.config || {};
  const m2 = data.rockets?.m2 || {};
  const m900 = data.rockets?.m900 || {};

  const m2Input = document.getElementById('networkM2Ip');
  const m900Input = document.getElementById('networkM900Ip');
  if (m2Input && document.activeElement !== m2Input) m2Input.value = cfg.m2_ip || '';
  if (m900Input && document.activeElement !== m900Input) m900Input.value = cfg.m900_ip || '';

  updateNetworkCard('M2', m2);
  updateNetworkCard('M900', m900);
  updateHeaderNetworkStatus(m2, m900);

  const updated = document.getElementById('networkUpdated');
  if (updated) updated.textContent = data.ts ? `Updated ${new Date(data.ts * 1000).toLocaleTimeString()}` : 'Not polled';
  const snmpState = document.getElementById('networkSnmpState');
  if (snmpState) snmpState.textContent = data.snmp?.available ? 'snmpget available' : 'snmpget not installed';
  const community = document.getElementById('networkSnmpCommunity');
  if (community) community.textContent = data.snmp?.community || '--';
}

async function refreshNetworkStatus(showErrors = false) {
  try {
    const data = await apiFetch('/api/network/status');
    updateNetworkStatus(data);
  } catch (err) {
    if (showErrors) showToast('error', 'Network Poll Failed', err.message);
  }
}

async function saveNetworkConfig() {
  const btn = document.getElementById('networkSaveBtn');
  const m2Ip = document.getElementById('networkM2Ip')?.value || '';
  const m900Ip = document.getElementById('networkM900Ip')?.value || '';
  if (btn) btn.disabled = true;
  try {
    await apiFetch('/api/network/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ m2_ip: m2Ip, m900_ip: m900Ip }),
    });
    showToast('success', 'Network Saved', 'Rocket IP addresses updated');
    await refreshNetworkStatus(true);
  } catch (err) {
    showToast('error', 'Network Save Failed', err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function initNetwork() {
  document.getElementById('networkSaveBtn')?.addEventListener('click', saveNetworkConfig);
  document.getElementById('networkPollBtn')?.addEventListener('click', () => refreshNetworkStatus(true));
  document.querySelectorAll('#networkM2Ip, #networkM900Ip').forEach(input => {
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') saveNetworkConfig();
    });
  });
  refreshNetworkStatus(false);
  state.network.pollTimer = setInterval(() => refreshNetworkStatus(false), 3000);
}

function updateSubsystemOverview(subsystems) {
  const grid = document.getElementById('subsystemOverviewGrid');
  if (!grid) return;
  const order = ['payload', 'arm', 'drive'];
  grid.innerHTML = order.map(key => {
    const s = subsystems[key] || {};
    const connected = s.connected === true;
    const status = connected ? (s.status || 'nominal') : 'critical';
    const dot = connected ? (status === 'nominal' ? 'dot-green' : status === 'degraded' ? 'dot-yellow' : 'dot-red') : 'dot-red';
    const metrics = (s.metrics || []).slice(0, 3).map(m => `
      <div class="subsystem-metric">
        <span>${escHtml(m.label || '')}</span>
        <strong>${escHtml(m.value || '--')}</strong>
      </div>
    `).join('');
    return `
      <article class="subsystem-card ${escHtml(status)}">
        <div class="subsystem-card-head">
          <div class="subsystem-title">
            <span class="status-dot ${dot}"></span>
            <strong>${escHtml(s.label || key)}</strong>
          </div>
          <span class="subsystem-state">${connected ? escHtml(status).toUpperCase() : 'OFFLINE'}</span>
        </div>
        <div class="subsystem-summary">${escHtml(s.summary || '')}</div>
        <div class="subsystem-metrics">${metrics}</div>
      </article>
    `;
  }).join('');
}

function motorStatus(motor) {
  if (!motor.connected) return { cls: 'badge-error', label: 'OFFLINE' };
  if ((motor.faults || 0) || (motor.sticky_faults || 0)) return { cls: 'badge-error', label: 'FAULT' };
  return { cls: 'badge-ok', label: 'OK' };
}

function formatFaultSummary(motor) {
  const active = motor.fault_names || [];
  const sticky = motor.sticky_fault_names || [];
  const parts = [];
  if (active.length) parts.push(active.join(', '));
  if (sticky.length) parts.push(`sticky: ${sticky.join(', ')}`);
  if (!parts.length) return 'None';
  return parts.join(' | ');
}

function updateMotorTelemetry(motorTelemetry) {
  renderMotorTable('drive', motorTelemetry.drive || {});
  renderMotorTable('arm', motorTelemetry.arm || {});
}

function renderMotorTable(group, motorsById) {
  const table = document.getElementById(`${group}MotorTable`);
  if (!table) return;
  const motors = Object.values(motorsById).sort((a, b) => Number(a.device_id) - Number(b.device_id));
  const header = `
    <div class="drive-row drive-header">
      <span>Motor</span><span>Temp</span><span>Amps</span><span>Volts</span><span>Faults</span><span></span>
    </div>
  `;
  if (!motors.length) {
    table.innerHTML = `${header}<div class="motor-empty">Waiting for ${group} motor telemetry</div>`;
    return;
  }
  const rows = motors.map(motor => {
    const status = motorStatus(motor);
    const faults = formatFaultSummary(motor);
    const id = Number(motor.device_id) || 0;
    const rawFaults = `F 0x${Number(motor.faults || 0).toString(16).padStart(4, '0')} / S 0x${Number(motor.sticky_faults || 0).toString(16).padStart(4, '0')}`;
    return `
      <div class="drive-row motor-row ${motor.connected ? '' : 'offline'}">
        <span title="${escHtml(rawFaults)}">${escHtml(motor.name || `id ${id}`)} <em>#${id}</em></span>
        <span>${fmtNumber(motor.motor_temperature_c, 1, ' °C')}</span>
        <span>${fmtNumber(motor.motor_current_a, 1, ' A')}</span>
        <span>${fmtNumber(motor.bus_voltage_v, 1, ' V')}</span>
        <span class="${status.cls}" title="${escHtml(faults)}">${status.label}</span>
        <button class="btn-secondary btn-sm clear-faults-btn" data-group="${escHtml(group)}" data-device-id="${id}">Clear</button>
      </div>
    `;
  }).join('');
  table.innerHTML = header + rows;
}

function initMotorControls() {
  document.addEventListener('click', async e => {
    const btn = e.target.closest('.clear-faults-btn');
    if (!btn) return;
    const group = btn.dataset.group;
    const deviceId = Number(btn.dataset.deviceId || 0);
    btn.disabled = true;
    try {
      await apiFetch('/api/motors/clear_faults', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group, device_id: deviceId }),
      });
      showToast('success', 'Clear Faults Sent', `${group} motor ${deviceId || 'all'}`);
    } catch (err) {
      showToast('error', 'Clear Faults Failed', err.message);
    } finally {
      btn.disabled = false;
    }
  });
}

// ══════════════════════════════════════════════════════════════
// PAYLOAD CONTROLS
// ══════════════════════════════════════════════════════════════

function initPayloadControls() {
  document.getElementById('elevatorUp').addEventListener('click',   () => sendElevator('up'));
  document.getElementById('elevatorDown').addEventListener('click', () => sendElevator('down'));
  document.getElementById('carouselCW').addEventListener('click',   () => sendCarousel('cw'));
  document.getElementById('carouselCCW').addEventListener('click',  () => sendCarousel('ccw'));

  document.getElementById('augerSlider').addEventListener('input', e => {
    document.getElementById('augerPct').textContent = `${e.target.value}%`;
  });

  document.getElementById('augerToggle').addEventListener('click', () => {
    state.augerOn = !state.augerOn;
    const btn = document.getElementById('augerToggle');
    btn.classList.toggle('on', state.augerOn);
    document.getElementById('augerToggleLabel').textContent = state.augerOn ? 'ON' : 'OFF';
    sendAuger();
  });

  document.getElementById('startLifeAnalysisBtn').addEventListener('click', startLifeAnalysis);
  document.getElementById('lifeRadarFile').addEventListener('change', handleRadarFile);
}

async function sendElevator(direction) {
  const steps = parseInt(document.getElementById('elevatorSteps').value, 10);
  if (isNaN(steps) || steps <= 0) { showToast('warn', 'Elevator', 'Enter a valid step count'); return; }
  const fb = document.getElementById('elevatorFeedback');
  fb.textContent = `Sending ${direction.toUpperCase()} ${steps} steps...`;
  try {
    await apiFetch('/api/payload/elevator', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction, steps }),
    });
    fb.textContent = `✓ ${direction.toUpperCase()} ${steps} steps sent`;
  } catch (err) {
    fb.textContent = `⊗ ${err.message}`;
    showToast('error', 'Elevator Command Failed', err.message);
  }
}

async function sendCarousel(direction) {
  const steps = parseInt(document.getElementById('carouselSteps').value, 10);
  if (isNaN(steps) || steps <= 0) { showToast('warn', 'Carousel', 'Enter a valid step count'); return; }
  const fb = document.getElementById('carouselFeedback');
  fb.textContent = `Sending ${direction.toUpperCase()} ${steps} steps…`;
  try {
    await apiFetch('/api/payload/carousel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction, steps }),
    });
    fb.textContent = `✓ ${direction.toUpperCase()} ${steps} steps sent`;
  } catch (err) {
    fb.textContent = `⊗ ${err.message}`;
    showToast('error', 'Carousel Command Failed', err.message);
  }
}

async function sendAuger() {
  const speed = parseInt(document.getElementById('augerSlider').value, 10);
  const fb = document.getElementById('augerFeedback');
  fb.textContent = `Sending auger ${state.augerOn ? 'ON' : 'OFF'} @ ${speed}%…`;
  try {
    await apiFetch('/api/payload/auger', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed, enabled: state.augerOn }),
    });
    fb.textContent = `✓ Auger ${state.augerOn ? 'ON' : 'OFF'} @ ${speed}%`;
  } catch (err) {
    fb.textContent = `⊗ ${err.message}`;
    showToast('error', 'Auger Command Failed', err.message);
    // Roll back toggle
    state.augerOn = !state.augerOn;
    const btn = document.getElementById('augerToggle');
    btn.classList.toggle('on', state.augerOn);
    document.getElementById('augerToggleLabel').textContent = state.augerOn ? 'ON' : 'OFF';
  }
}

async function startLifeAnalysis() {
  const status = document.getElementById('lifeAnalysisStatus');
  status.textContent = 'Starting analysis...';
  try {
    const res = await apiFetch('/api/payload/life-analysis/start', { method: 'POST' });
    updateLifeAnalysis(res.life_analysis || {});
    showToast('info', 'Life Analysis', 'Start signal sent');
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    showToast('error', 'Life Analysis Failed', err.message);
  }
}

function handleRadarFile(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    const image_data_url = String(reader.result || '');
    renderRadarGraph(image_data_url);
    document.getElementById('lifeAnalysisStatus').textContent = `Loaded ${file.name}`;
    try {
      await apiFetch('/api/payload/life-analysis/radar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_data_url, summary: file.name }),
      });
    } catch (err) {
      showToast('warn', 'Radar Save Failed', err.message);
    }
  };
  reader.readAsDataURL(file);
}

// ══════════════════════════════════════════════════════════════
// BOOT — called after successful authentication
// ══════════════════════════════════════════════════════════════

function bootApp() {
  initTabs();
  initEStop();
  initWarningModal();
  initCameras();
  initDashboardControls();
  initMap();
  initGamepadPolling();
  initLogs();
  initFiles();
  initPayloadControls();
  initMotorControls();
  initNetwork();

  connectTelemetryWS();
  connectLogWS();

  refreshSystemData();
  setInterval(refreshSystemData, 2000);

  setTimeout(() => _map && _map.invalidateSize(), 300);
}

// ══════════════════════════════════════════════════════════════
// ENTRY POINT
// ══════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  initAuth();   // handles login screen, then calls bootApp() on success
});

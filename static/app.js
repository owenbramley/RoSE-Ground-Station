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
      try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return await res.json();
  } catch (err) {
    clearTimeout(timeout);
    if (err.name === 'AbortError') throw new Error('Request timed out');
    throw err;
  }
}

function fmtTime(isoStr) {
  try { return new Date(isoStr).toLocaleTimeString('en-US', { hour12: false, fractionalSecondDigits: 2 }); }
  catch (_) { return isoStr; }
}

function fmtUptime(s) {
  if (!Number.isFinite(Number(s))) return '--';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
}

function asFiniteNumber(value) {
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
    html: `<div style="width:14px;height:14px;border-radius:50%;background:#00d4aa;border:2px solid #fff;box-shadow:0 0 8px #00d4aaaa"></div>`,
    iconSize: [14, 14], iconAnchor: [7, 7],
  });

  _roverMarker = L.marker([43.6532, -79.3832], { icon }).addTo(_map);
  _gpsTrail = L.polyline([], { color: '#00d4aa', weight: 2, opacity: 0.5 }).addTo(_map);
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
  document.getElementById('estopBtn').addEventListener('click', activateEstop);
  document.getElementById('resetEstopBtn').addEventListener('click', resetEstop);
  document.getElementById('capture360Btn').addEventListener('click', capture360Image);
  document.getElementById('closeCapture360Modal').addEventListener('click', closeCapture360Modal);
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

// ══════════════════════════════════════════════════════════════
// CAMERA FEEDS — src set after auth so token can be appended
// ══════════════════════════════════════════════════════════════

function initCameras() {
  for (let i = 0; i < 4; i++) {
    const img = document.getElementById(`camImg${i}`);
    const err = document.getElementById(`camErr${i}`);

    // Set src with token embedded as query param (img can't send headers)
    img.src = withToken(`/api/camera/${i}`);

    img.addEventListener('error', () => {
      img.classList.add('hidden');
      err.classList.remove('hidden');
      setTimeout(() => retryCam(i), 3000);
    });
    img.addEventListener('load', () => {
      img.classList.remove('hidden');
      err.classList.add('hidden');
    });
  }
}

function retryCam(id) {
  const img = document.getElementById(`camImg${id}`);
  const err = document.getElementById(`camErr${id}`);
  img.src = withToken(`/api/camera/${id}`) + `&_=${Date.now()}`;
  img.classList.remove('hidden');
  err.classList.add('hidden');
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
  document.getElementById('getFilesBtn').addEventListener('click', async () => {
    const btn    = document.getElementById('getFilesBtn');
    const status = document.getElementById('filesStatus');
    btn.disabled = true;
    document.getElementById('getFilesBtnIcon').textContent = '↻';
    status.textContent = '';
    status.className = 'files-status';

    try {
      const data = await apiFetch('/api/files');
      status.textContent = JSON.stringify(data);
      status.className = 'files-status ok';
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
        showToast('error', 'Files API Error', err.message);
      }
    } finally {
      btn.disabled = false;
      document.getElementById('getFilesBtnIcon').textContent = '⇓';
    }
  });
}

// ══════════════════════════════════════════════════════════════
// SYSTEM OVERVIEW
// ══════════════════════════════════════════════════════════════

function updateSubsysStatus(payloadConn, armConn, driveConn) {
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
    document.getElementById('sysJetsonTemp').textContent = fmtNumber(jetsonTemp, 1, ' °C');
    setBarWidth('sysJetsonTempBar', jetsonTemp, 100);
    document.getElementById('sysCpuPct').textContent = fmtNumber(d.cpu_percent, 1, ' %');
    setBarWidth('sysCpuBar', d.cpu_percent, 100);
    const ramUsed = asFiniteNumber(d.ram_used_gb);
    const ramTotal = asFiniteNumber(d.ram_total_gb);
    document.getElementById('sysRam').textContent = ramUsed === null || ramTotal === null ? '--' : `${ramUsed.toFixed(1)} / ${ramTotal.toFixed(1)} GB`;
    setBarWidth('sysRamBar', ramUsed, ramTotal || 100);
    document.getElementById('sysUptime').textContent = fmtUptime(d.uptime_s);
    updateSubsystemOverview(d.subsystems || {});
    updateComms(d.comms || {});
    updatePayloadArduino(d.payload_arduino || {});
    updateLifeAnalysis(d.life_analysis || {});
    updateLedController(d.led_controller || {});
    updateMotorTelemetry(d.motor_telemetry || {});
  } catch (_) {
    // Non-blocking
  }
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
  comms = comms || {};
  const link24 = comms.link_24ghz;
  const link900 = comms.link_900mhz;
  if (link24) {
    const stability = asFiniteNumber(link24.stability);
    document.getElementById('link24Val').textContent = stability === null ? '--' : `${Math.round(stability)}%`;
    document.getElementById('link24Dot').className = `status-dot ${linkClass(link24.stability)}`;
  }
  if (link900) {
    const stability = asFiniteNumber(link900.stability);
    document.getElementById('link900Val').textContent = stability === null ? '--' : `${Math.round(stability)}%`;
    document.getElementById('link900Dot').className = `status-dot ${linkClass(link900.stability)}`;
  }
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
  initMap();
  initGamepadPolling();
  initLogs();
  initFiles();
  initPayloadControls();
  initMotorControls();

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

/* ══════════════════════════════════════════════════════════════
   RoSE Ground Station — Frontend application
══════════════════════════════════════════════════════════════ */

'use strict';

// ── Constants ────────────────────────────────────────────────
const WS_ORIGIN   = `ws://${location.host}`;
const TOKEN_KEY   = 'rose_token';
const CLIENT_ID_KEY = 'rose_client_id';
const CAMERA_DECODE_MODE_KEY = 'rose_camera_decode_mode';
let _appBooted = false;
let _authInitialized = false;
const CAMERA_START_JITTER_MS = Math.floor(Math.random() * 2000);

// ── Global state ─────────────────────────────────────────────
const state = {
  estopActive: false,
  hiddenCameras: new Set(),
  visibleCameras: new Set(),
  cameraRotations: {},
  cameraStatuses: {},
  cameraStatusTimers: {},
  cameraConnectTimers: {},
  cameraStartSeq: {},
  nativeCameras: new Set(),
  nativeCameraUrls: {},
  nativeCameraUrlIndex: {},
  cameraRetryTimers: {},
  cameraRetryAttempts: {},
  cameraDecodeMode: 'browser',
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
  network: {
    pollTimer: null,
    saving: false,
  },
  cameraSettings: {
    saving: false,
    maxBitrateKbps: null,
  },
};

// ══════════════════════════════════════════════════════════════
// TOKEN MANAGEMENT
// ══════════════════════════════════════════════════════════════

function getToken() {
  try {
    const token = localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || '';
    if (token && !localStorage.getItem(TOKEN_KEY)) localStorage.setItem(TOKEN_KEY, token);
    return token;
  } catch (_) {
    return sessionStorage.getItem(TOKEN_KEY) || '';
  }
}

function setToken(t) {
  try {
    localStorage.setItem(TOKEN_KEY, t);
  } catch (_) {}
  sessionStorage.setItem(TOKEN_KEY, t);
}

function clearToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch (_) {}
  sessionStorage.removeItem(TOKEN_KEY);
}

/** Append ?token=... to a URL string */
function withToken(url) {
  const t = getToken();
  const sep = url.includes('?') ? '&' : '?';
  const parts = [];
  if (t) parts.push(`token=${encodeURIComponent(t)}`);
  parts.push(`client_id=${encodeURIComponent(getClientId())}`);
  return `${url}${sep}${parts.join('&')}`;
}

function getClientId() {
  let id = localStorage.getItem(CLIENT_ID_KEY) || '';
  if (!id) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    id = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
    localStorage.setItem(CLIENT_ID_KEY, id);
  }
  return id;
}

// ══════════════════════════════════════════════════════════════
// TOAST NOTIFICATIONS
// ══════════════════════════════════════════════════════════════

const TOAST_ICONS = { error: '⊗', warn: '⚠', info: 'ℹ', success: '✓' };
const IMPORTANT_LOG_SOURCES = new Set([
  'ros2', 'bridge', 'rclpy', 'estop', 'can', 'drive', 'arm', 'comms', 'radio', 'battery'
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

function showAuthOverlay(options = {}) {
  const { clearPassword = false, focus = true } = options;
  document.getElementById('authOverlay').classList.remove('hidden');
  const input = document.getElementById('authPassword');
  if (clearPassword) input.value = '';
  if (focus && document.activeElement !== input && !input.closest('.hidden')) input.focus();
  hideAuthError();
}

function hideAuthOverlay() {
  document.getElementById('authOverlay').classList.add('hidden');
  document.getElementById('authPassword').value = '';
}

function showAuthError(msg) {
  const el = document.getElementById('authError');
  el.textContent = msg;
  el.classList.remove('hidden');
  const input = document.getElementById('authPassword');
  if (input) {
    input.classList.add('shake');
    setTimeout(() => input.classList.remove('shake'), 400);
  }
}

function hideAuthError() {
  document.getElementById('authError').classList.add('hidden');
}

async function initAuth() {
  if (_authInitialized) return;
  _authInitialized = true;

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

  showAuthOverlay({ clearPassword: false, focus: false });
  document.getElementById('authSubmitLabel').textContent = 'Connect';

  document.getElementById('authForm').addEventListener('submit', async e => {
    e.preventDefault();
    const btn      = document.getElementById('authSubmit');
    const lbl      = document.getElementById('authSubmitLabel');

    btn.disabled = true;
    lbl.textContent = 'Connecting...';
    hideAuthError();

    try {
      await connectUiSession();
      const res = await fetch('/api/auth', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: '' }),
      });
      if (!res.ok) throw new Error('Could not start passwordless session');
      const { token } = await res.json();
      setToken(token);
      hideAuthOverlay();
      startUiHeartbeat();
      bootApp();
    } catch (err) {
      showAuthError(err.message);
    } finally {
      btn.disabled = false;
      lbl.textContent = 'Connect';
    }
  });

  try {
    await connectUiSession();
    const res = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: '' }),
    });
    const { token } = await res.json();
    setToken(token || '');
    hideAuthOverlay();
    startUiHeartbeat();
    bootApp();
  } catch (err) {
    showAuthError(err.message);
  }
}

async function connectUiSession() {
  const res = await fetch('/api/session/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: getClientId() }),
  });
  if (!res.ok) {
    const j = await res.json().catch(() => ({}));
    throw new Error(j.detail || 'Ground station UI is full. Try again after another device disconnects.');
  }
  return res.json();
}

function startUiHeartbeat() {
  if (state.uiHeartbeatTimer) clearInterval(state.uiHeartbeatTimer);
  const beat = () => {
    fetch('/api/session/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: getClientId() }),
      keepalive: true,
    }).then(res => {
      if (res.status === 409 || res.status === 429) {
        showAuthOverlay({ focus: false });
        showAuthError('This browser no longer has a UI slot. Connect again when fewer than 3 devices are active.');
      }
    }).catch(() => {});
  };
  state.uiHeartbeatTimer = setInterval(beat, 5000);
  window.addEventListener('pagehide', () => {
    navigator.sendBeacon?.(
      '/api/session/disconnect',
      new Blob([JSON.stringify({ client_id: getClientId() })], { type: 'application/json' })
    );
  }, { once: true });
}

// ══════════════════════════════════════════════════════════════
// GS URL CHIP (header)
// ══════════════════════════════════════════════════════════════

function populateGsUrlChip(urls) {
  const primaryUrl = urls[0] || `http://${location.host}`;
  const chip = document.getElementById('gsUrlChip');
  if (!chip) return;
  const text = document.getElementById('gsUrlText');
  if (text) text.textContent = primaryUrl;

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
  updateSubsysStatus(msg.arm_connected, msg.drive_connected);
  updateSubsystemOverview(msg.subsystems || {});
  updateComms(msg.comms || {});
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

function telemetryField(t, key) {
  const value = t?.[key];
  const meta = t?._meta?.[key] || {};
  return {
    value,
    stale: meta.stale === true && asFiniteNumber(value) !== null,
    age_s: asFiniteNumber(meta.age_s),
  };
}

function applyTelemetryCard(id, barId, badgeId, field, maxVal, cls) {
  const card  = document.getElementById(id);
  const bar   = document.getElementById(barId);
  const badge = document.getElementById(badgeId);
  if (!card || !bar || !badge) return;
  const value = field && typeof field === 'object' && 'value' in field ? field.value : field;
  const stale = field && typeof field === 'object' && field.stale === true;
  const n = asFiniteNumber(value);
  const pct = n === null ? 0 : Math.min(100, Math.max(0, (n / maxVal) * 100));
  const displayCls = stale && cls !== 'crit' ? 'warn' : cls;

  bar.style.width     = pct + '%';
  card.className      = 'telem-card' + (displayCls !== 'ok' ? ` state-${displayCls}` : '');
  bar.className       = 'telem-bar'  + (displayCls !== 'ok' ? ` ${displayCls}` : '');
  badge.className     = 'telem-badge' + (displayCls !== 'ok' ? ` ${displayCls}` : '');
  badge.textContent   = n === null ? 'NO DATA' : stale ? 'STALE' : cls.toUpperCase();
}

function updateTelemetry(t) {
  if (!document.getElementById('socVal')) return;
  t = t || {};
  const w = state.warnings;

  const soc = telemetryField(t, 'soc');
  const socCls = levelClassInv(soc.value, w.soc.warning, w.soc.critical);
  document.getElementById('socVal').textContent = fmtNumber(soc.value, 1);
  applyTelemetryCard('socCard', 'socBar', 'socBadge', soc, 100, socCls);

  const cur = telemetryField(t, 'current');
  const curCls = levelClass(cur.value, w.cur.warning, w.cur.critical);
  document.getElementById('curVal').textContent = fmtNumber(cur.value, 1);
  applyTelemetryCard('curCard', 'curBar', 'curBadge', cur, w.cur.critical * 1.2, curCls);

  const temp = telemetryField(t, 'temperature');
  const tempCls = levelClass(temp.value, w.temp.warning, w.temp.critical);
  document.getElementById('tempVal').textContent = fmtNumber(temp.value, 1);
  applyTelemetryCard('tempCard', 'tempBar', 'tempBadge', temp, w.temp.critical * 1.2, tempCls);

  const voltage = telemetryField(t, 'voltage');
  document.getElementById('voltVal').textContent = fmtNumber(voltage.value, 1);
}

// ══════════════════════════════════════════════════════════════
// GPS MAP
// ══════════════════════════════════════════════════════════════

let _map = null;
let _roverMarker = null;
let _gpsTrail = null;
let _trailPoints = [];
const MAX_TRAIL = 120;
const MAP_TILE_SIZE = 256;
const MAP_MIN_ZOOM = 7;
const MAP_MAX_ZOOM = 16;
const MAP_NATIVE_ZOOM = 15;

function clamp(n, min, max) {
  return Math.min(max, Math.max(min, n));
}

function mapTileUrl(z, x, y) {
  const nativeZ = Math.min(z, MAP_NATIVE_ZOOM);
  const scale = 2 ** (z - nativeZ);
  const nativeX = Math.floor(x / scale);
  const nativeY = Math.floor(y / scale);
  return `/map_tiles/usgs_topo/${nativeZ}/${nativeX}/${nativeY}.jpg`;
}

function latLonToWorld(lat, lon, zoom) {
  const sinLat = Math.sin(clamp(lat, -85.05112878, 85.05112878) * Math.PI / 180);
  const scale = MAP_TILE_SIZE * (2 ** zoom);
  return {
    x: ((lon + 180) / 360) * scale,
    y: (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * scale,
  };
}

class OfflineTileMap {
  constructor(container, center, zoom) {
    this.container = container;
    this.center = center;
    this.zoom = clamp(zoom, MAP_MIN_ZOOM, MAP_MAX_ZOOM);
    this.tiles = new Map();

    this.layer = document.createElement('div');
    this.layer.className = 'offline-map-layer';
    this.trailSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    this.trailSvg.classList.add('offline-map-trail');
    this.trailLine = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    this.trailLine.setAttribute('fill', 'none');
    this.trailLine.setAttribute('stroke', '#ff37a8');
    this.trailLine.setAttribute('stroke-width', '2');
    this.trailLine.setAttribute('stroke-opacity', '0.5');
    this.trailSvg.appendChild(this.trailLine);

    const overlay = container.querySelector('#gpsMapOverlay');
    container.insertBefore(this.layer, overlay || null);
    container.insertBefore(this.trailSvg, overlay || null);

    window.addEventListener('resize', () => this.invalidateSize());
    this.render();
  }

  project(ll) {
    const centerPx = latLonToWorld(this.center[0], this.center[1], this.zoom);
    const pointPx = latLonToWorld(ll[0], ll[1], this.zoom);
    const rect = this.container.getBoundingClientRect();
    return {
      x: (rect.width / 2) + pointPx.x - centerPx.x,
      y: (rect.height / 2) + pointPx.y - centerPx.y,
    };
  }

  getBounds() {
    const rect = this.container.getBoundingClientRect();
    return {
      contains: ll => {
        const p = this.project(ll);
        return p.x >= 0 && p.x <= rect.width && p.y >= 0 && p.y <= rect.height;
      },
    };
  }

  panTo(ll) {
    this.center = ll;
    this.render();
  }

  invalidateSize() {
    this.render();
  }

  setMarker(marker) {
    this.marker = marker;
    this.container.insertBefore(marker.el, this.trailSvg.nextSibling);
    this.positionMarker();
  }

  positionMarker() {
    if (!this.marker) return;
    const p = this.project(this.marker.ll);
    this.marker.el.style.transform = `translate(${p.x - 15}px, ${p.y - 15}px)`;
  }

  setTrail(points) {
    const projected = points.map(ll => {
      const p = this.project(ll);
      return `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    });
    this.trailLine.setAttribute('points', projected.join(' '));
  }

  render() {
    const rect = this.container.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    const centerPx = latLonToWorld(this.center[0], this.center[1], this.zoom);
    const minX = Math.floor((centerPx.x - rect.width / 2) / MAP_TILE_SIZE);
    const maxX = Math.floor((centerPx.x + rect.width / 2) / MAP_TILE_SIZE);
    const minY = Math.floor((centerPx.y - rect.height / 2) / MAP_TILE_SIZE);
    const maxY = Math.floor((centerPx.y + rect.height / 2) / MAP_TILE_SIZE);
    const tileCount = 2 ** this.zoom;
    const needed = new Set();

    for (let x = minX; x <= maxX; x += 1) {
      for (let y = minY; y <= maxY; y += 1) {
        if (y < 0 || y >= tileCount) continue;
        const wrappedX = ((x % tileCount) + tileCount) % tileCount;
        const key = `${this.zoom}/${wrappedX}/${y}`;
        needed.add(key);
        let img = this.tiles.get(key);
        if (!img) {
          img = document.createElement('img');
          img.className = 'offline-map-tile';
          img.alt = '';
          img.draggable = false;
          img.src = mapTileUrl(this.zoom, wrappedX, y);
          this.tiles.set(key, img);
          this.layer.appendChild(img);
        }
        img.style.transform = `translate(${(x * MAP_TILE_SIZE - centerPx.x + rect.width / 2).toFixed(1)}px, ${(y * MAP_TILE_SIZE - centerPx.y + rect.height / 2).toFixed(1)}px)`;
      }
    }

    for (const [key, img] of this.tiles.entries()) {
      if (!needed.has(key)) {
        img.remove();
        this.tiles.delete(key);
      }
    }

    this.trailSvg.setAttribute('viewBox', `0 0 ${rect.width} ${rect.height}`);
    this.positionMarker();
    this.setTrail(_trailPoints);
  }
}

class OfflineMarker {
  constructor(ll, html) {
    this.ll = ll;
    this.el = document.createElement('div');
    this.el.className = 'offline-map-marker rover-heading-icon';
    this.el.innerHTML = html;
  }

  setLatLng(ll) {
    this.ll = ll;
    _map?.positionMarker();
  }
}

class OfflinePolyline {
  setLatLngs(points) {
    _map?.setTrail(points);
  }
}

function initMap() {
  const container = document.getElementById('gpsMap');
  _map = new OfflineTileMap(container, [38.5733, -109.5498], 15);
  _roverMarker = new OfflineMarker([38.5733, -109.5498], `<div class="rover-heading-arrow" id="roverHeadingArrow"></div>`);
  _gpsTrail = new OfflinePolyline();
  _map.setMarker(_roverMarker);
}

function updateGPS(g) {
  g = g || {};
  if (!_map) return;
  const connected = g.connected === true;
  const valid = g.valid === true;
  const overlay = document.getElementById('gpsMapOverlay');
  if (overlay) {
    overlay.classList.toggle('hidden', valid);
    overlay.textContent = connected ? 'GPS Fix Pending' : 'GPS Not Connected';
  }

  document.getElementById('gpsFix').textContent = connected ? (valid ? (g.fix || 'FIX') : 'PENDING') : 'NOT CONNECTED';
  document.getElementById('gpsSats').textContent = g.satellites ?? '--';
  document.getElementById('gpsUpdated').textContent = connected && g.updated_at
    ? `Updated ${new Date(g.updated_at).toLocaleTimeString()}`
    : '';

  const badge = document.getElementById('gpsBadge');
  if (badge) {
    badge.textContent = !connected ? 'GPS NOT CONNECTED' : valid ? `${g.fix || 'GPS'} FIX` : 'GPS FIX PENDING';
    badge.className = `gps-fix-badge ${!connected ? 'offline' : valid ? 'fix3d' : 'pending'}`;
  }

  if (!valid) {
    document.getElementById('gpsLat').textContent = '--';
    document.getElementById('gpsLon').textContent = '--';
    return;
  }

  const lat = asFiniteNumber(g.lat);
  const lon = asFiniteNumber(g.lon);
  if (lat === null || lon === null) return;
  const ll = [lat, lon];
  _roverMarker.setLatLng(ll);
  const heading = asFiniteNumber(g.heading_deg);
  const arrow = document.getElementById('roverHeadingArrow');
  if (arrow) arrow.style.transform = `rotate(${heading === null ? 0 : heading}deg)`;
  _trailPoints.push(ll);
  if (_trailPoints.length > MAX_TRAIL) _trailPoints.shift();
  _gpsTrail.setLatLngs(_trailPoints);
  if (!_map.getBounds().contains(ll)) _map.panTo(ll, { animate: true, duration: 0.5 });

  document.getElementById('gpsLat').textContent     = lat.toFixed(6);
  document.getElementById('gpsLon').textContent     = lon.toFixed(6);
}

// ══════════════════════════════════════════════════════════════
// CONTROLLER STATUS (Gamepad API — fully client-side)
// ══════════════════════════════════════════════════════════════

function initGamepadPolling() {
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
  state.cameraDecodeMode = 'browser';
  loadCameraSettings().catch(() => {});
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
  document.getElementById('saveCameraBitrateBtn')?.addEventListener('click', saveCameraSettings);
  document.getElementById('closeCameraStillModal')?.addEventListener('click', closeCameraStillModal);
  refreshCameras(true, { quiet: true }).catch(() => renderCameraGrid());
  setInterval(refreshCameraStatus, 3000);
}

async function loadCameraSettings() {
  const res = await apiFetch('/api/camera-settings');
  const settings = res.settings || {};
  state.cameraSettings.maxBitrateKbps = Number(settings.max_bitrate_kbps || 0) || null;
  const input = document.getElementById('cameraMaxBitrate');
  if (input && state.cameraSettings.maxBitrateKbps) input.value = state.cameraSettings.maxBitrateKbps;
}

async function saveCameraSettings() {
  const input = document.getElementById('cameraMaxBitrate');
  const btn = document.getElementById('saveCameraBitrateBtn');
  const value = Math.round(Number(input?.value || 0));
  if (!Number.isFinite(value) || value < 50) {
    showToast('warn', 'Invalid Bitrate', 'Enter a max bitrate of at least 50 kbps.');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const res = await apiFetch('/api/camera-settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ max_bitrate_kbps: value }),
    });
    state.cameraSettings.maxBitrateKbps = res.settings?.max_bitrate_kbps || value;
    if (input) input.value = state.cameraSettings.maxBitrateKbps;
    showToast('success', 'Camera Bitrate Saved', `Max bitrate scale is ${state.cameraSettings.maxBitrateKbps} kbps.`, 2500);
  } catch (err) {
    showToast('error', 'Camera Bitrate Save Failed', err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function loadCameraDecodeMode() {
  state.cameraDecodeMode = 'browser';
  localStorage.setItem(CAMERA_DECODE_MODE_KEY, 'browser');
}

function renderCameraDecodeMode() {
  state.cameraDecodeMode = 'browser';
}

function updateCameraDecodeButtons() {
  state.cameraDecodeMode = 'browser';
}

function setCameraDecodeMode(mode) {
  state.cameraDecodeMode = 'browser';
  localStorage.setItem(CAMERA_DECODE_MODE_KEY, 'browser');
  const visibleIds = state.cameras
    .map(cam => String(cam.id))
    .filter(id => !state.hiddenCameras.has(id));
  visibleIds.forEach((id, index) => scheduleShowCamera(id, {
    quiet: true,
    delayMs: CAMERA_START_JITTER_MS + index * 600,
  }));
}

async function refreshCameras(force = true, options = {}) {
  const priorIds = cameraIdsSignature();
  let res;
  try {
    res = force
      ? await apiFetch('/api/cameras/refresh', { method: 'POST' })
      : await apiFetch('/api/cameras');
  } catch (err) {
    if (force && !options.quiet) showToast('error', 'Camera Refresh Failed', err.message);
    throw err;
  }
  state.cameras = res.cameras || [];
  applyServerCameraRotations();
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
  if (!state.cameras.length) {
    try {
      const res = await apiFetch('/api/cameras/refresh', { method: 'POST' });
      state.cameras = res.cameras || [];
      applyServerCameraRotations();
      if (state.cameras.length) {
        renderCameraGrid();
        renderCameraVisibility();
      }
    } catch (_) {}
    return;
  }
  try {
    const res = await apiFetch('/api/cameras');
    state.cameras = res.cameras || [];
    applyServerCameraRotations();
    updateCameraLabels();
    renderCameraVisibility();
    state.cameras.forEach(cam => updateCameraStatusFromMetadata(String(cam.id), cam));
  } catch (_) {}
}

function cameraIdsSignature() {
  return state.cameras.map(cam => String(cam.id)).sort().join('|');
}

function retryCam(id) {
  id = String(id);
  const img = document.getElementById(`camImg-${cssSafeId(id)}`);
  if (!img || state.hiddenCameras.has(id)) return;
  clearCameraStatusTimer(id);
  setCameraStatus(id, 'connecting', 'Retrying local decode', 'Reconnecting this browser directly to the rover camera service.');
  scheduleShowCamera(id, { quiet: true, delayMs: 500 });
}

function clearCameraRetry(id) {
  id = String(id);
  if (state.cameraRetryTimers[id]) clearTimeout(state.cameraRetryTimers[id]);
  delete state.cameraRetryTimers[id];
  state.cameraRetryAttempts[id] = 0;
}

function scheduleCameraRecovery(id, reason = '') {
  id = String(id);
  if (state.hiddenCameras.has(id) || state.cameraRetryTimers[id]) return;
  const attempt = Number(state.cameraRetryAttempts[id] || 0) + 1;
  state.cameraRetryAttempts[id] = attempt;
  const delayMs = Math.min(30000, 1500 * (2 ** Math.min(attempt - 1, 5))) + Math.floor(Math.random() * 750);
  setCameraStatus(
    id,
    'error',
    'Recovering camera stream',
    `${reason || 'The direct rover stream failed.'} Resetting this camera and retrying in ${(delayMs / 1000).toFixed(1)} seconds.`
  );
  state.cameraRetryTimers[id] = setTimeout(async () => {
    delete state.cameraRetryTimers[id];
    if (state.hiddenCameras.has(id)) return;
    setCameraStatus(id, 'connecting', 'Resetting rover camera', 'Requesting a per-camera reset before reopening the direct stream.');
    try {
      await apiFetch(`/api/camera/${encodeURIComponent(id)}/reset`, { method: 'POST' });
    } catch (err) {
      showToast('warn', 'Camera Reset Failed', err.message, 3500);
    }
    retryCam(id);
  }, delayMs);
}

function stopNativeCamera(id) {
  id = String(id);
  nextCameraStartSeq(id);
  if (state.cameraConnectTimers[id]) clearTimeout(state.cameraConnectTimers[id]);
  delete state.cameraConnectTimers[id];
  if (state.cameraRetryTimers[id]) clearTimeout(state.cameraRetryTimers[id]);
  delete state.cameraRetryTimers[id];
  delete state.cameraRetryAttempts[id];
  clearCameraStatusTimer(id);
  state.nativeCameras.delete(id);
  delete state.nativeCameraUrls[id];
  delete state.nativeCameraUrlIndex[id];
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

function releaseCameraElements() {
  Object.values(state.cameraConnectTimers || {}).forEach(timer => clearTimeout(timer));
  state.cameraConnectTimers = {};
  Object.values(state.cameraRetryTimers || {}).forEach(timer => clearTimeout(timer));
  state.cameraRetryTimers = {};
  document.querySelectorAll('.camera-img').forEach(img => {
    img.removeAttribute('src');
  });
  document.querySelectorAll('.camera-video').forEach(video => {
    video.pause();
    video.removeAttribute('src');
    video.load();
  });
}

function setNativeCameraSource(id, index = 0) {
  id = String(id);
  const urls = state.nativeCameraUrls[id] || [];
  const url = urls[index];
  const img = document.getElementById(`camImg-${cssSafeId(id)}`);
  if (!url || !img) return false;
  state.nativeCameraUrlIndex[id] = index;
  const sep = url.includes('?') ? '&' : '?';
  img.removeAttribute('src');
  img.dataset.lastSourceSetAt = String(Date.now());
  img.src = `${url}${sep}_=${Date.now()}`;
  img.classList.remove('hidden');
  return true;
}

function waitForNativeFrame(id, seq, readyDelayMs = 2500) {
  id = String(id);
  const check = () => {
    if (!isCurrentCameraStart(id, seq) || !state.nativeCameras.has(id)) return;
    const img = document.getElementById(`camImg-${cssSafeId(id)}`);
    if (img?.naturalWidth) {
      setCameraStatus(id, 'live', 'Browser decode', '');
    } else {
      const nextIndex = Number(state.nativeCameraUrlIndex[id] || 0) + 1;
      if (setNativeCameraSource(id, nextIndex)) {
        setCameraStatus(id, 'connecting', 'Trying alternate browser stream', 'The first direct rover URL opened but did not produce a frame.');
        waitForNativeFrame(id, seq, readyDelayMs);
      } else {
        scheduleCameraRecovery(id, 'The stream URL opened but did not produce a frame.');
      }
    }
  };
  clearCameraStatusTimer(id);
  state.cameraStatusTimers[id] = setTimeout(check, readyDelayMs);
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

function nextCameraStartSeq(id) {
  id = String(id);
  state.cameraStartSeq[id] = (Number(state.cameraStartSeq[id] || 0) + 1) % 1000000;
  return state.cameraStartSeq[id];
}

function isCurrentCameraStart(id, seq) {
  return state.cameraStartSeq[String(id)] === seq;
}

function scheduleShowCamera(id, options = {}) {
  id = String(id);
  if (state.cameraConnectTimers[id]) clearTimeout(state.cameraConnectTimers[id]);
  const delayMs = Math.max(0, Number(options.delayMs || 0));
  state.cameraConnectTimers[id] = setTimeout(() => {
    delete state.cameraConnectTimers[id];
    showCamera(id, options);
  }, delayMs);
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
  if (!img || state.hiddenCameras.has(String(id))) return;
  if (!state.nativeCameras.has(String(id)) && (!img.complete || !img.naturalWidth)) {
    setCameraStatus(id, 'connecting', 'Connecting to local decode stream', cameraStatusDetail(cam));
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

function cameraPersistKey(id) {
  const cam = state.cameras.find(c => String(c.id) === String(id));
  return String(cam?.stable_key || id);
}

function applyServerCameraRotations() {
  state.cameras.forEach(cam => {
    const id = String(cam.id);
    const key = String(cam.stable_key || id);
    if (Number.isFinite(Number(cam.rotation))) {
      const rotation = Number(cam.rotation) % 360;
      if (rotation) {
        state.cameraRotations[id] = rotation;
        state.cameraRotations[key] = rotation;
      }
    }
  });
  saveCameraRotations();
}

function saveCameraRotations() {
  localStorage.setItem('rose_camera_rotations', JSON.stringify(state.cameraRotations));
}

function applyCameraRotation(id) {
  const img = document.getElementById(`camImg-${cssSafeId(id)}`);
  const video = document.getElementById(`camVideo-${cssSafeId(id)}`);
  const deg = Number(state.cameraRotations[String(id)] || state.cameraRotations[cameraPersistKey(id)] || 0) % 360;
  const transform = deg ? `rotate(${deg}deg)` : '';
  if (img) img.style.transform = transform;
  if (video) video.style.transform = transform;
}

function rotateCamera(id) {
  id = String(id);
  const key = cameraPersistKey(id);
  const next = (Number(state.cameraRotations[id] || state.cameraRotations[key] || 0) + 90) % 360;
  if (next) {
    state.cameraRotations[id] = next;
    state.cameraRotations[key] = next;
  } else {
    delete state.cameraRotations[id];
    delete state.cameraRotations[key];
  }
  saveCameraRotations();
  applyCameraRotation(id);
  apiFetch(`/api/camera/${encodeURIComponent(id)}/orientation`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rotation: next }),
  }).catch(err => showToast('warn', 'Camera Orientation Save Failed', err.message));
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
  return startBrowserCamera(id, options);
}

async function startBrowserCamera(id, options = {}) {
  id = String(id);
  const seq = nextCameraStartSeq(id);
  const safeId = cssSafeId(id);
  const img = document.getElementById(`camImg-${safeId}`);
  if (!img) return;

  clearCameraStatusTimer(id);
  state.hiddenCameras.delete(id);
  state.visibleCameras.add(id);
  if (options.persist !== false) saveHiddenCameras();
  renderCameraVisibility();
  setCameraStatus(id, 'starting', 'Starting local decode', 'Connecting this browser directly to the rover camera stream.');

  try {
    const res = await apiFetch(`/api/camera/${encodeURIComponent(id)}/native`, { method: 'POST' });
    if (!isCurrentCameraStart(id, seq)) return;
    const urls = Array.isArray(res.urls) && res.urls.length ? res.urls : [res.url].filter(Boolean);
    if (!urls.length) throw new Error('Rover camera service did not return a browser stream URL');
    state.nativeCameras.add(id);
    state.nativeCameraUrls[id] = urls;
    setNativeCameraSource(id, 0);
    setCameraStatus(id, 'connecting', 'Local stream requested', 'This browser is connecting directly to the rover camera service. JPEG decode happens on this computer.');
    waitForNativeFrame(id, seq);
  } catch (err) {
    stopNativeCamera(id);
    setCameraStatus(id, 'error', 'Browser stream failed', err.message);
    if (!options.quiet) showToast('error', 'Browser Camera Failed', err.message);
  }
}

async function showNativeCamera(id) {
  return showCamera(id);
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
  state.cameras.forEach((cam, index) => scheduleShowCamera(String(cam.id), {
    delayMs: CAMERA_START_JITTER_MS + index * 600,
  }));
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
      clearCameraStatusTimer(id);
      if (state.nativeCameras.has(id)) {
        const nextIndex = Number(state.nativeCameraUrlIndex[id] || 0) + 1;
        if (setNativeCameraSource(id, nextIndex)) {
          setCameraStatus(id, 'connecting', 'Trying alternate browser stream', 'The first direct rover URL was not reachable from this computer, so the browser is trying another discovered camera-service address.');
          return;
        }
        scheduleCameraRecovery(id, 'This computer could not read any direct rover MJPEG URL.');
        return;
      }
      setCameraStatus(id, 'error', 'Feed unavailable', 'The browser could not read the MJPEG stream. Retrying while the camera remains visible.');
      if (!state.hiddenCameras.has(id)) setTimeout(() => retryCam(id), 3000);
    });
    img.addEventListener('load', () => {
      clearCameraStatusTimer(id);
      clearCameraRetry(id);
      setCameraStatus(id, 'live', 'Local decode', '');
      applyCameraRotation(id);
    });
    if (video) {
      video.addEventListener('canplay', () => {
        clearCameraStatusTimer(id);
        setCameraStatus(id, 'live', 'Browser decode', 'This browser is decoding the rover stream directly.');
      });
      video.addEventListener('error', () => {
        if (!state.nativeCameras.has(id)) return;
        clearCameraStatusTimer(id);
        scheduleCameraRecovery(id, 'The browser could not play the direct rover stream.');
      });
    }
    applyCameraRotation(id);
    if (!state.hiddenCameras.has(id)) {
      setCameraStatus(id, 'idle', 'Preparing stream', 'Waiting to request this rover camera stream.');
      scheduleShowCamera(id, { delayMs: CAMERA_START_JITTER_MS + 400 + Math.floor(Math.random() * 1200) });
    }
  });
  updateCameraDecodeButtons();
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

function updateSubsysStatus(armConn, driveConn) {
  updateControllerTopicHealth(armConn, driveConn);

  const ad = document.querySelector('#sysArmConn .status-dot');
  if (ad) {
    ad.className = `status-dot ${armConn ? 'dot-green' : 'dot-red'}`;
    document.getElementById('sysArmVal').textContent = armConn ? 'Connected' : 'Disconnected';
  }

  const dd = document.querySelector('#sysDriveConn .status-dot');
  if (dd) {
    dd.className = `status-dot ${driveConn ? 'dot-green' : 'dot-red'}`;
    document.getElementById('sysDriveVal').textContent = driveConn ? 'Connected' : 'Disconnected';
  }
}

function updateControllerTopicHealth(armConn, driveConn) {
  const connectedCount = (armConn ? 1 : 0) + (driveConn ? 1 : 0);
  const headerDot = document.getElementById('controllerDot');
  const headerCount = document.getElementById('controllerCount');
  if (headerDot) {
    headerDot.className = `status-dot ${connectedCount === 2 ? 'dot-green' : connectedCount === 1 ? 'dot-yellow' : 'dot-red'}`;
  }
  if (headerCount) headerCount.textContent = `${connectedCount}/2`;

  const setHealth = (name, connected) => {
    const label = connected ? 'Online' : 'Offline';
    const dotCls = connected ? 'dot-green' : 'dot-red';
    const dashDot = document.getElementById(`dash${name.charAt(0).toUpperCase() + name.slice(1)}ControllerDot`);
    const dashVal = document.getElementById(`dash${name.charAt(0).toUpperCase() + name.slice(1)}ControllerVal`);
    const dashCard = document.getElementById(`${name}ControllerCard`);
    if (dashDot) dashDot.className = `status-dot ${dotCls}`;
    if (dashVal) {
      dashVal.textContent = `${label} on ROS network`;
      dashVal.classList.toggle('online', connected);
      dashVal.classList.toggle('offline', !connected);
    }
    if (dashCard) dashCard.classList.toggle('offline', !connected);
  };
  setHealth('arm', !!armConn);
  setHealth('drive', !!driveConn);
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
    const online = motors.length - offline;
    const canState = offline ? 'bad' : 'ok';
    const canLabel = `${online}/${motors.length} online`;
    setHealthStatus('sysCanDot', 'sysCanVal', canState, canLabel);
  }
}

function updateSystemHealthConnection(itemId, valId, subsystem) {
  const dot = document.querySelector(`#${itemId} .status-dot`);
  const val = document.getElementById(valId);
  if (!dot || !val) return;
  const connected = subsystem.connected === true;
  dot.className = `status-dot ${connected ? 'dot-green' : 'dot-red'}`;
  val.textContent = connected ? (subsystem.summary || 'Connected') : 'Disconnected';
}

function setStatusDot(dotId, value) {
  const dot = document.getElementById(dotId);
  if (dot) dot.className = `status-dot ${value ? 'dot-green' : 'dot-red'}`;
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
  if (snmpState) {
    const backend = data.snmp?.backend;
    snmpState.textContent = backend === 'snmpget'
      ? 'snmpget available'
      : data.snmp?.available
        ? 'built-in SNMP'
        : 'SNMP unavailable';
  }
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
  const order = ['arm', 'drive'];
  grid.innerHTML = order.map(key => {
    const s = subsystems[key] || {};
    const connected = s.connected === true;
    const status = connected ? 'online' : 'offline';
    const dot = connected ? 'dot-green' : 'dot-red';
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
          <span class="subsystem-state">${connected ? 'ONLINE' : 'OFFLINE'}</span>
        </div>
        <div class="subsystem-summary">${escHtml(s.summary || '')}</div>
        <div class="subsystem-metrics">${metrics}</div>
      </article>
    `;
  }).join('');
}

function motorStatus(motor) {
  if (!motor.connected) return { cls: 'badge-error', label: 'OFFLINE' };
  return { cls: 'badge-ok', label: 'ONLINE' };
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
  updateMotorHeaderCount('drive', motorTelemetry.drive || {});
  updateMotorHeaderCount('arm', motorTelemetry.arm || {});
}

function updateMotorHeaderCount(group, motorsById) {
  return;
}

function renderMotorTable(group, motorsById) {
  const table = document.getElementById(`${group}MotorTable`);
  if (!table) return;
  const motors = Object.values(motorsById).sort((a, b) => Number(a.device_id) - Number(b.device_id));
  const header = `
    <div class="drive-row drive-header">
      <span>Motor</span><span>Temp</span><span>Amps</span><span>Volts</span><span>Status</span><span></span>
    </div>
  `;
  if (!motors.length) {
    table.innerHTML = `${header}<div class="motor-empty">Waiting for ${group} motor telemetry</div>`;
    return;
  }
  const rows = motors.map(motor => {
    const status = motorStatus(motor);
    const id = Number(motor.device_id) || 0;
    const rawFaults = `F 0x${Number(motor.faults || 0).toString(16).padStart(4, '0')} / S 0x${Number(motor.sticky_faults || 0).toString(16).padStart(4, '0')}`;
    return `
      <div class="drive-row motor-row ${motor.connected ? '' : 'offline'}">
        <span title="${escHtml(rawFaults)}">${escHtml(motor.name || `id ${id}`)} <em>#${id}</em></span>
        <span>${fmtNumber(motor.motor_temperature_c, 1, ' °C')}</span>
        <span>${fmtNumber(motor.motor_current_a, 1, ' A')}</span>
        <span>${fmtNumber(motor.bus_voltage_v, 1, ' V')}</span>
        <span class="${status.cls}">${status.label}</span>
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
// BOOT — called after successful authentication
// ══════════════════════════════════════════════════════════════

function bootApp() {
  if (_appBooted) return;
  _appBooted = true;

  initTabs();
  initEStop();
  initWarningModal();
  initCameras();
  initDashboardControls();
  initMap();
  initGamepadPolling();
  initLogs();
  initFiles();
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

window.addEventListener('pagehide', releaseCameraElements);

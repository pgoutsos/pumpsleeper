/**
 * PumpSleeper Card — Custom Home Assistant Lovelace card
 *
 * Installation:
 *   1. Place this file in your HA config /www/ folder
 *   2. Add as a resource: Settings → Dashboards → Resources
 *        URL:  /local/pumpsleeper-card.js
 *        Type: JavaScript module
 *   3. Add to a dashboard:
 *        type: custom:pumpsleeper-card
 *        entities:
 *          device_online:        binary_sensor.pumpsleeper_device_online
 *          main_pump_running:    binary_sensor.pumpsleeper_main_pump_running
 *          signal_strength:      sensor.pumpsleeper_signal_strength
 *          mode:                 select.pumpsleeper_mode
 *          operating_status:     sensor.pumpsleeper_operating_status
 *          last_ping_ts:         sensor.pumpsleeper_last_device_ping
 *          mode_switched_ts:     sensor.pumpsleeper_mode_switched_at
 *          main_runs_today:      sensor.pumpsleeper_main_pump_runs_today
 *          main_runtime_today:   sensor.pumpsleeper_main_pump_runtime_today
 *          main_gallons_today:   sensor.pumpsleeper_main_pump_gallons_today
 *          main_last_run:        sensor.pumpsleeper_main_pump_last_run
 *          backup_runs_today:    sensor.pumpsleeper_backup_pump_runs_today
 *          backup_runtime_today: sensor.pumpsleeper_backup_pump_runtime_today
 *          backup_gallons_today: sensor.pumpsleeper_backup_pump_gallons_today
 *          backup_last_run:      sensor.pumpsleeper_backup_pump_last_run
 *          backup_last_trigger:  sensor.pumpsleeper_backup_pump_last_trigger
 *          battery_voltage:      sensor.pumpsleeper_backup_battery_voltage
 *          loaded_voltage:       sensor.pumpsleeper_backup_loaded_voltage
 */

const PENDING_WINDOW_MS = 3 * 60 * 1000;   // 3 minutes, same as web dashboard
const PING_STALE_MS     = 10 * 60 * 1000;  // 10 minutes = 5 missed 2-min pings → offline

class PumpSleeperCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._timer = null;
  }

  connectedCallback() {
    // Tick every second to keep the pending countdown live
    this._timer = setInterval(() => this._tickStatus(), 1000);
  }

  disconnectedCallback() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  }

  setConfig(config) {
    if (!config.entities) {
      throw new Error('PumpSleeper card requires an "entities" block in its config.');
    }
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  // ── Helpers ────────────────────────────────────────────────────────────────

  _state(entityId, fallback = 'unavailable') {
    if (!entityId || !this._hass || !this._hass.states[entityId]) return fallback;
    return this._hass.states[entityId].state;
  }

  _formatDuration(secs) {
    const s = parseFloat(secs);
    if (isNaN(s) || s <= 0) return '0s';
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const r = Math.round(s % 60);
    return r > 0 ? `${m}m ${r}s` : `${m}m`;
  }

  _formatTs(ts) {
    if (!ts || ts === 'unavailable' || ts === 'unknown') return '—';
    try {
      return new Date(ts).toLocaleString('en-US', {
        month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit',
      });
    } catch (_) { return ts; }
  }

  _isUnknown(val) {
    return !val || val === 'unavailable' || val === 'unknown';
  }

  // ── Pending / Online / Offline logic ───────────────────────────────────────
  // Mirrors the same logic used in the PumpSleeper web dashboard.

  _getLinkStatus() {
    const e           = (this._config || {}).entities || {};
    const lastPingTs  = this._state(e.last_ping_ts);
    const modeSwitchedTs = this._state(e.mode_switched_ts);
    const now         = Date.now();

    if (this._isUnknown(modeSwitchedTs)) {
      // No mode switch recorded — just use device_online sensor
      return this._state(e.device_online) === 'on' ? 'online' : 'offline';
    }

    const switchedMs = new Date(modeSwitchedTs).getTime();
    const pingMs     = this._isUnknown(lastPingTs) ? 0 : new Date(lastPingTs).getTime();

    if (pingMs > switchedMs && (now - pingMs) < PING_STALE_MS) {
      return 'online';   // device has pinged since the last mode switch AND recently
    }
    if ((now - switchedMs) < PENDING_WINDOW_MS) {
      return 'pending';  // within 3-minute window, waiting for first ping
    }
    return 'offline';    // window expired, no ping received
  }

  _getPendingCountdown() {
    const e = (this._config || {}).entities || {};
    const modeSwitchedTs = this._state(e.mode_switched_ts);
    if (this._isUnknown(modeSwitchedTs)) return null;
    const switchedMs = new Date(modeSwitchedTs).getTime();
    const remaining  = PENDING_WINDOW_MS - (Date.now() - switchedMs);
    if (remaining <= 0) return null;
    const totalSecs  = Math.ceil(remaining / 1000);
    const m = Math.floor(totalSecs / 60);
    const s = totalSecs % 60;
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  }

  // ── Live status tick (every second, updates only the badge) ────────────────

  _tickStatus() {
    const badge = this.shadowRoot.querySelector('.status-pill');
    if (!badge) return;
    const status    = this._getLinkStatus();
    const countdown = status === 'pending' ? this._getPendingCountdown() : null;
    badge.className = `status-pill status-${status}`;
    badge.innerHTML = `<div class="dot"></div>${this._statusLabel(status, countdown)}`;
  }

  _statusLabel(status, countdown) {
    if (status === 'pending') {
      return countdown ? `Pending — ${countdown}` : 'Pending';
    }
    return status === 'online' ? 'Online' : 'Offline';
  }

  // ── Full render ────────────────────────────────────────────────────────────

  _render() {
    if (!this._hass || !this._config) return;
    const e = this._config.entities || {};

    const linkStatus  = this._getLinkStatus();
    const countdown   = linkStatus === 'pending' ? this._getPendingCountdown() : null;
    const mainRunning = this._state(e.main_pump_running) === 'on';
    const mode        = this._state(e.mode, 'proxy');
    const rssi        = this._state(e.signal_strength);
    const opStatus    = this._state(e.operating_status);

    const mainRuns      = this._state(e.main_runs_today,     '0');
    const mainRuntime   = this._state(e.main_runtime_today,  '0');
    const mainGallons   = this._state(e.main_gallons_today,  '0');
    const mainLastRun   = this._state(e.main_last_run);

    const backupRuns    = this._state(e.backup_runs_today,     '0');
    const backupRuntime = this._state(e.backup_runtime_today,  '0');
    const backupGallons = this._state(e.backup_gallons_today,  '0');
    const backupLastRun = this._state(e.backup_last_run);
    const backupTrigger = this._state(e.backup_last_trigger);
    const battV         = this._state(e.battery_voltage);
    const loadedV       = this._state(e.loaded_voltage);

    const rssiBar = (dbm) => {
      const v = parseFloat(dbm);
      if (isNaN(v)) return '';
      const pct   = Math.max(0, Math.min(100, (v + 100) / 60 * 100));
      const color = pct > 60 ? '#10b981' : pct > 30 ? '#f59e0b' : '#ef4444';
      return `<div class="rssi-bar-wrap">
        <div class="rssi-bar-track">
          <div class="rssi-bar-fill" style="width:${pct}%;background:${color}"></div>
        </div>
        <span class="rssi-val">${v} dBm</span>
      </div>`;
    };

    const stat = (label, value) => `
      <div class="stat">
        <span class="stat-label">${label}</span>
        <span class="stat-value">${value}</span>
      </div>`;

    this.shadowRoot.innerHTML = `
      <style>
        * { box-sizing: border-box; }
        :host { display: block; }

        .card {
          background: var(--ha-card-background, var(--card-background-color, #fff));
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,.15));
          padding: 16px;
          font-family: var(--primary-font-family, sans-serif);
          color: var(--primary-text-color);
        }

        /* ── Header ── */
        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 14px;
        }
        .header-left {
          display: flex;
          align-items: center;
          gap: 10px;
          flex-wrap: wrap;
        }
        .logo {
          font-size: 1.15em;
          font-weight: 700;
          letter-spacing: -0.01em;
        }
        .logo span { color: #3b82f6; }

        /* Status pill — colour driven by class */
        .status-pill {
          display: flex;
          align-items: center;
          gap: 5px;
          font-size: 0.75em;
          font-weight: 600;
          padding: 3px 9px;
          border-radius: 20px;
          white-space: nowrap;
        }
        .status-online   { background:#10b98118; color:#10b981; border:1px solid #10b98140; }
        .status-pending  { background:#f59e0b18; color:#f59e0b; border:1px solid #f59e0b40; }
        .status-offline  { background:#ef444418; color:#ef4444; border:1px solid #ef444440; }
        .dot { width:7px; height:7px; border-radius:50%; background:currentColor; }

        .op-badge {
          font-size: 0.72em;
          padding: 2px 8px;
          border-radius: 10px;
          font-weight: 600;
          background: var(--divider-color, #e0e0e0);
          color: var(--secondary-text-color);
        }

        /* ── Signal ── */
        .rssi-bar-wrap { display:flex; align-items:center; gap:8px; }
        .rssi-bar-track { width:60px; height:5px; background:var(--divider-color,#e0e0e0); border-radius:3px; overflow:hidden; }
        .rssi-bar-fill  { height:100%; border-radius:3px; transition:width .4s ease; }
        .rssi-val { font-size:.75em; color:var(--secondary-text-color); white-space:nowrap; }

        /* ── Mode toggle ── */
        .mode-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          background: var(--secondary-background-color, #f5f5f5);
          border-radius: 8px;
          padding: 10px 14px;
          margin-bottom: 14px;
        }
        .mode-label { font-size:.82em; color:var(--secondary-text-color); font-weight:500; }
        .toggle { display:flex; align-items:center; gap:4px; }
        .toggle-btn {
          padding: 4px 14px;
          border-radius: 20px;
          font-size: .8em;
          font-weight: 600;
          cursor: pointer;
          border: 1px solid transparent;
          transition: all .18s;
          user-select: none;
        }
        .toggle-btn.active-proxy     { background:#10b98120; color:#10b981; border-color:#10b98150; }
        .toggle-btn.active-takeover  { background:#f59e0b20; color:#f59e0b; border-color:#f59e0b50; }
        .toggle-btn.inactive         { color:var(--secondary-text-color); }
        .toggle-btn.inactive:hover   { background:var(--divider-color,#e0e0e0); }

        /* ── Pump grid ── */
        .pumps { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px; }
        .pump-card { background:var(--secondary-background-color,#f5f5f5); border-radius:8px; padding:12px; }
        .pump-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
        .pump-title  { font-size:.78em; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--secondary-text-color); }
        .running-badge { font-size:.68em; font-weight:700; padding:2px 7px; border-radius:8px; letter-spacing:.04em; }
        .run-on  { background:#10b98120; color:#10b981; animation:blink 1.4s ease-in-out infinite; }
        .run-off { background:var(--divider-color,#ddd); color:var(--secondary-text-color); }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.45} }

        /* ── Stats ── */
        .stat { display:flex; justify-content:space-between; align-items:center; padding:4px 0; font-size:.82em; border-bottom:1px solid var(--divider-color,#e0e0e0); }
        .stat:last-child { border-bottom:none; }
        .stat-label { color:var(--secondary-text-color); }
        .stat-value { font-weight:600; }

        /* ── Battery ── */
        .battery-section { background:var(--secondary-background-color,#f5f5f5); border-radius:8px; padding:12px; }
        .section-title   { font-size:.78em; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--secondary-text-color); margin-bottom:10px; }
        .bat-grid  { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
        .bat-item  { display:flex; flex-direction:column; gap:2px; }
        .bat-label { font-size:.75em; color:var(--secondary-text-color); }
        .bat-value { font-size:1.05em; font-weight:700; }
      </style>

      <div class="card">

        <!-- Header -->
        <div class="header">
          <div class="header-left">
            <div class="logo">Pump<span>Sleeper</span></div>
            <div class="status-pill status-${linkStatus}">
              <div class="dot"></div>
              ${this._statusLabel(linkStatus, countdown)}
            </div>
            ${!this._isUnknown(opStatus) ? `<span class="op-badge">${opStatus}</span>` : ''}
          </div>
          ${!this._isUnknown(rssi) ? rssiBar(rssi) : ''}
        </div>

        <!-- Mode toggle -->
        <div class="mode-row">
          <span class="mode-label">Server Mode</span>
          <div class="toggle">
            <span class="toggle-btn ${mode === 'proxy'    ? 'active-proxy'    : 'inactive'}" data-mode="proxy">Proxy</span>
            <span class="toggle-btn ${mode === 'takeover' ? 'active-takeover' : 'inactive'}" data-mode="takeover">Takeover</span>
          </div>
        </div>

        <!-- Pumps -->
        <div class="pumps">
          <div class="pump-card">
            <div class="pump-header">
              <span class="pump-title">Main Pump</span>
              <span class="running-badge ${mainRunning ? 'run-on' : 'run-off'}">${mainRunning ? 'RUNNING' : 'IDLE'}</span>
            </div>
            ${stat('Runs today',    mainRuns)}
            ${stat('Runtime today', this._formatDuration(mainRuntime))}
            ${stat('Gallons today', this._isUnknown(mainGallons) || mainGallons === '0' ? '0 gal' : `${mainGallons} gal`)}
            ${stat('Last run',      this._formatTs(mainLastRun))}
          </div>

          <div class="pump-card">
            <div class="pump-header">
              <span class="pump-title">Backup Pump</span>
              <span class="running-badge run-off">IDLE</span>
            </div>
            ${stat('Runs today',    backupRuns)}
            ${stat('Runtime today', this._formatDuration(backupRuntime))}
            ${stat('Gallons today', this._isUnknown(backupGallons) || backupGallons === '0' ? '0 gal' : `${backupGallons} gal`)}
            ${stat('Last run',      this._formatTs(backupLastRun))}
            ${!this._isUnknown(backupTrigger) ? stat('Last trigger', backupTrigger) : ''}
          </div>
        </div>

        <!-- Battery -->
        <div class="battery-section">
          <div class="section-title">🔋 Backup Battery</div>
          <div class="bat-grid">
            <div class="bat-item">
              <span class="bat-label">Standby voltage</span>
              <span class="bat-value">${this._isUnknown(battV) ? '—' : `${battV} V`}</span>
            </div>
            <div class="bat-item">
              <span class="bat-label">Loaded voltage</span>
              <span class="bat-value">${this._isUnknown(loadedV) ? '—' : `${loadedV} V`}</span>
            </div>
          </div>
        </div>

      </div>
    `;

    // Mode toggle click handlers
    this.shadowRoot.querySelectorAll('.toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const newMode = btn.dataset.mode;
        if (newMode === mode || !e.mode) return;
        this._hass.callService('select', 'select_option', {
          entity_id: e.mode,
          option: newMode,
        });
      });
    });
  }

  static getStubConfig() {
    return {
      entities: {
        device_online:        'binary_sensor.pumpsleeper_device_online',
        main_pump_running:    'binary_sensor.pumpsleeper_main_pump_running',
        signal_strength:      'sensor.pumpsleeper_signal_strength',
        mode:                 'select.pumpsleeper_mode',
        operating_status:     'sensor.pumpsleeper_operating_status',
        last_ping_ts:         'sensor.pumpsleeper_last_device_ping',
        mode_switched_ts:     'sensor.pumpsleeper_mode_switched_at',
        main_runs_today:      'sensor.pumpsleeper_main_pump_runs_today',
        main_runtime_today:   'sensor.pumpsleeper_main_pump_runtime_today',
        main_gallons_today:   'sensor.pumpsleeper_main_pump_gallons_today',
        main_last_run:        'sensor.pumpsleeper_main_pump_last_run',
        backup_runs_today:    'sensor.pumpsleeper_backup_pump_runs_today',
        backup_runtime_today: 'sensor.pumpsleeper_backup_pump_runtime_today',
        backup_gallons_today: 'sensor.pumpsleeper_backup_pump_gallons_today',
        backup_last_run:      'sensor.pumpsleeper_backup_pump_last_run',
        backup_last_trigger:  'sensor.pumpsleeper_backup_pump_last_trigger',
        battery_voltage:      'sensor.pumpsleeper_backup_battery_voltage',
        loaded_voltage:       'sensor.pumpsleeper_backup_loaded_voltage',
      },
    };
  }

  getCardSize() { return 5; }
}

customElements.define('pumpsleeper-card', PumpSleeperCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type:        'pumpsleeper-card',
  name:        'PumpSleeper Card',
  description: 'Status card for PumpSleeper sump pump monitor',
});

/* Shared application shell, map instance, and tab router. */

const App = (() => {
  const tabs = {};
  let activeTab = null;
  let map = null;

  // Shared carrier color palette (matches scripts/simulation/run_12month_lopf.py)
  const CARRIER_COLORS = {
    'solar': '#FFD700', 'onwind': '#4CAF50', 'offwind': '#00BCD4',
    'run_of_river': '#2196F3', 'reservoir': '#1565C0',
    'biogas': '#8BC34A', 'biomass': '#795548', 'waste': '#607D8B',
    'gas_ccgt': '#FF9800', 'gas_chp': '#FF5722', 'gas': '#FF9800',
    'coal': '#424242', 'lignite': '#6D4C41', 'oil': '#E91E63',
    'other': '#9E9E9E', 'hydrogen': '#00E5FF',
    'residential_cts': '#3498db', 'industry': '#9b59b6', 'large_industry': '#e74c3c',
    'import_FR': '#003399', 'import_AT': '#CC0000', 'import_CH': '#CC0000',
    'import_NL': '#FF6600', 'import_DK': '#CC0000', 'import_PL': '#FFFFFF',
    'import_CZ': '#003399', 'import_NO': '#003399', 'import_SE': '#003399',
    'import_BE': '#000000', 'import_LU': '#003399',
  };
  const VOLTAGE_COLORS = { 380: '#e74c3c', 220: '#27ae60', 110: '#3498db' };

  function colorForCarrier(c) { return CARRIER_COLORS[c] || '#9E9E9E'; }
  function colorForVoltage(v) {
    if (v >= 380) return VOLTAGE_COLORS[380];
    if (v >= 220) return VOLTAGE_COLORS[220];
    return VOLTAGE_COLORS[110];
  }
  function colorForLoading(pct) {
    if (pct < 50)  return '#2ecc71';
    if (pct < 75)  return '#f1c40f';
    if (pct < 100) return '#e67e22';
    if (pct < 200) return '#e74c3c';
    return '#ff69b4';
  }

  // ── Map init ─────────────────────────────────────────────────────────
  function initMap() {
    map = L.map('map', { zoomControl: true, preferCanvas: true })
            .setView([51.2, 10.4], 6);
    L.tileLayer(
      'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
      { attribution: '&copy; CartoDB &copy; OSM', maxZoom: 18 }
    ).addTo(map);
    return map;
  }

  // ── Sidebar / detail helpers ────────────────────────────────────────
  const sidebar = () => document.getElementById('sidebar');
  const detailPanel = () => document.getElementById('detail-panel');
  const mapEl = () => document.getElementById('map');

  function setSidebar(html) { sidebar().innerHTML = html; }
  function showDetail(html) {
    const panel = detailPanel();
    panel.innerHTML = `<button class="close-btn" onclick="App.hideDetail()">&times;</button>` + html;
    panel.classList.remove('hidden');
    mapEl().classList.add('with-detail');
    setTimeout(() => map.invalidateSize(), 100);
  }
  function hideDetail() {
    detailPanel().classList.add('hidden');
    mapEl().classList.remove('with-detail');
    setTimeout(() => map.invalidateSize(), 100);
  }

  let toastTimer = null;
  function toast(msg, ms = 3000) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add('hidden'), ms);
  }

  // ── Fetch helper ────────────────────────────────────────────────────
  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${text}`);
    }
    return res.json();
  }

  // ── Tab management ─────────────────────────────────────────────────
  function registerTab(name, mod) { tabs[name] = mod; }

  async function activate(name) {
    if (!tabs[name]) return;
    if (activeTab && tabs[activeTab].deactivate) {
      tabs[activeTab].deactivate(map);
    }
    hideDetail();
    activeTab = name;
    document.querySelectorAll('.tab-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === name));
    setSidebar('<div class="muted"><span class="spinner"></span>Loading…</div>');
    try {
      await tabs[name].activate(map);
    } catch (err) {
      console.error(err);
      toast('Tab failed: ' + err.message, 5000);
      setSidebar(`<div style="color:#e74c3c">${err.message}</div>`);
    }
  }

  function init() {
    initMap();
    document.querySelectorAll('.tab-btn').forEach(b => {
      b.addEventListener('click', () => activate(b.dataset.tab));
    });
    activate('production');
  }

  return {
    init, registerTab, activate, api, toast,
    map: () => map, setSidebar, showDetail, hideDetail,
    colorForCarrier, colorForVoltage, colorForLoading,
    CARRIER_COLORS, VOLTAGE_COLORS,
  };
})();

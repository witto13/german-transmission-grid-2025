/* Tab 5 — Power flow runner. Date picker → submit → poll → results. */

const TabPf = (() => {
  let lineLayer = null;
  let topology = null;
  let dispatchChart = null;
  let pollHandle = null;
  let currentJobId = null;

  const today = new Date(2025, 3, 15); // April 15, 2025 default
  const fmt = d => d.toISOString().slice(0, 10);

  const SIDEBAR_HTML = () => `
    <h2>Power Flow</h2>
    <div class="muted" style="line-height:1.4">
      LOPF on grid_beta (7723 buses, 12911 lines, 18792 gens). PyPSA's LP-build
      phase is the bottleneck — expect <b>~5–10 min per snapshot</b>. Aggregation
      groups generators by (bus, carrier) before solving, which is required to
      make the LP tractable.
    </div>

    <h3>Date range</h3>
    <div class="field">
      <label>Start (00:00 UTC)</label>
      <input type="date" id="pf-start" value="${fmt(today)}" min="2025-01-01" max="2025-12-31">
    </div>
    <div class="field">
      <label>End date (last hour 23:00)</label>
      <input type="date" id="pf-end" value="${fmt(today)}" min="2025-01-01" max="2025-12-31">
    </div>

    <h3>Options</h3>
    <label class="checkbox-row"><input type="checkbox" id="pf-aggregate" checked>
      Aggregate generators by (bus, carrier)</label>
    <div class="muted" style="font-size:10px">Without this, even 1h takes &gt;15 min.</div>

    <button class="btn" id="pf-run" style="margin-top:10px">Run</button>
    <button class="btn secondary" id="pf-jobs" style="margin-top: 6px">Recent jobs</button>

    <div id="pf-status" class="summary-box hidden" style="margin-top: 10px"></div>

    <h3>Result summary</h3>
    <div id="pf-summary" class="muted">No job loaded.</div>

    <div id="dispatch-chart-wrap" class="hidden"><canvas id="dispatch-chart"></canvas></div>

    <h3 style="margin-top:14px">Top 20 most-loaded lines</h3>
    <div id="pf-toplines" class="muted">—</div>
  `;

  async function activate(map) {
    App.setSidebar(SIDEBAR_HTML());

    // Pre-fetch topology so we can overlay line loading
    if (!topology) {
      topology = await App.api('/api/topology?country=DE&include_geom=false');
    }

    document.getElementById('pf-run').onclick = () => submitJob();
    document.getElementById('pf-jobs').onclick = () => showJobsList();

    drawTopologyBase(map);
  }

  function deactivate(map) {
    if (lineLayer) { map.removeLayer(lineLayer); lineLayer = null; }
    if (dispatchChart) { dispatchChart.destroy(); dispatchChart = null; }
    if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
  }

  function drawTopologyBase(map, lineLoading = null) {
    if (lineLayer) map.removeLayer(lineLayer);
    const busCoord = {};
    topology.buses.forEach(b => busCoord[b.bus_id] = [b.lat, b.lon]);
    const features = topology.lines.map(l => {
      const c0 = busCoord[l.bus0], c1 = busCoord[l.bus1];
      if (!c0 || !c1) return null;
      let coords = [c0, c1];
      if (l.parallel_count > 1) {
        const off = (l.parallel_index - (l.parallel_count - 1) / 2) * 0.004;
        coords = offsetCoords(coords[0], coords[1], off);
      }
      const pct = lineLoading?.[String(l.line_id)];
      const color = pct != null ? App.colorForLoading(pct) : App.colorForVoltage(l.v_nom);
      const w = pct != null ? Math.max(1, Math.min(5, pct / 25)) : (l.v_nom >= 380 ? 1.6 : 1);
      const opacity = pct != null ? 0.95 : 0.55;
      const polyline = L.polyline(coords, { color, weight: w, opacity });
      polyline.bindTooltip(
        `<b>Line ${l.line_id}</b> · ${l.v_nom} kV<br>` +
        `s_nom: ${l.s_nom?.toFixed(0)} MW<br>` +
        (pct != null ? `<b style="color:${color}">Max loading: ${pct}%</b>` : '')
      );
      return polyline;
    }).filter(Boolean);
    lineLayer = L.layerGroup(features).addTo(map);
  }

  function offsetCoords(b0, b1, offset) {
    const dx = b1[1] - b0[1], dy = b1[0] - b0[0];
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len < 1e-8) return [b0, b1];
    const px = -dy / len * offset, py = dx / len * offset;
    return [[b0[0] + px, b0[1] + py], [b1[0] + px, b1[1] + py]];
  }

  async function submitJob() {
    const startStr = document.getElementById('pf-start').value;
    const endStr   = document.getElementById('pf-end').value;
    if (!startStr || !endStr) { App.toast('Pick a date range'); return; }
    const start = startStr + 'T00:00:00';
    const end   = endStr   + 'T23:00:00';

    const hours = (new Date(end) - new Date(start)) / 3600000 + 1;
    const aggregate = document.getElementById('pf-aggregate').checked;
    const estMin = hours * (aggregate ? 7 : 15);
    if (hours > 6) {
      if (!confirm(`${hours.toFixed(0)} hours × ~${aggregate ? 7 : 15} min/snapshot ≈ ${estMin.toFixed(0)} min. Continue?`)) return;
    }

    const btn = document.getElementById('pf-run');
    btn.disabled = true; btn.textContent = 'Submitting…';

    try {
      const res = await App.api('/api/pf/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ start, end, mode: 'lopf', aggregate }),
      });
      currentJobId = res.job_id;
      App.toast('Job ' + currentJobId + ' submitted');
      pollStatus();
    } catch (e) {
      App.toast('Submit failed: ' + e.message, 6000);
      btn.disabled = false; btn.textContent = 'Run';
    }
  }

  async function pollStatus() {
    if (pollHandle) clearInterval(pollHandle);
    const statusEl = document.getElementById('pf-status');
    statusEl.classList.remove('hidden');

    const update = async () => {
      try {
        const s = await App.api('/api/pf/status/' + currentJobId);
        statusEl.innerHTML = `
          <div class="stat"><span>Job</span><span class="val">${s.job_id}</span></div>
          <div class="stat"><span>State</span><span class="val">${s.state}</span></div>
          <div class="stat"><span>Snapshots</span><span class="val">${s.n_snapshots}</span></div>
          <div class="progress-bar"><div class="fill" style="width: ${s.progress}%"></div></div>
          <div style="font-size: 11px; color: #aaa">${s.message}</div>`;
        if (s.state === 'done') {
          clearInterval(pollHandle); pollHandle = null;
          await loadResult(currentJobId);
          const btn = document.getElementById('pf-run');
          btn.disabled = false; btn.textContent = 'Run';
        } else if (s.state === 'failed') {
          clearInterval(pollHandle); pollHandle = null;
          App.toast('Job failed: ' + s.message, 8000);
          const btn = document.getElementById('pf-run');
          btn.disabled = false; btn.textContent = 'Run';
        }
      } catch (e) {
        clearInterval(pollHandle); pollHandle = null;
        App.toast('Polling error: ' + e.message);
      }
    };
    update();
    pollHandle = setInterval(update, 2000);
  }

  async function loadResult(jobId) {
    const r = await App.api('/api/pf/result/' + jobId);
    document.getElementById('pf-summary').innerHTML = `
      <div class="stat"><span>Snapshots</span><span class="val">${r.n_snapshots}</span></div>
      <div class="stat"><span>Total load</span><span class="val">${(r.total_load_mwh / 1000).toFixed(1)} GWh</span></div>
      <div class="stat"><span>Total gen</span><span class="val">${(r.total_gen_mwh / 1000).toFixed(1)} GWh</span></div>
      ${r.objective != null ? `<div class="stat"><span>Cost (objective)</span><span class="val">${(r.objective / 1e6).toFixed(2)} M€</span></div>` : ''}`;

    drawDispatchChart(r);
    drawTopLines(r);

    // Overlay line loading on the map
    drawTopologyBase(App.map(), r.line_max_loading);
  }

  function drawDispatchChart(r) {
    document.getElementById('dispatch-chart-wrap').classList.remove('hidden');
    if (dispatchChart) dispatchChart.destroy();

    const labels = r.timeseries.map(h => h.t.slice(5, 16).replace('T', ' '));
    const carriers = r.carriers.filter(c => c !== '_load' && r.totals_mwh[c] && Math.abs(r.totals_mwh[c]) > 0.5);

    const datasets = carriers.map(c => ({
      label: c,
      data: r.timeseries.map(h => (h[c] || 0) / 1000),  // GW
      backgroundColor: App.colorForCarrier(c),
      borderColor: App.colorForCarrier(c),
      borderWidth: 0,
      stack: 'gen',
    }));
    datasets.push({
      label: 'Load',
      data: r.timeseries.map(h => h.load_mw / 1000),
      type: 'line',
      borderColor: '#fff',
      borderWidth: 2,
      fill: false,
      pointRadius: 0,
    });

    dispatchChart = new Chart(document.getElementById('dispatch-chart'), {
      type: 'bar',
      data: { labels, datasets },
      options: {
        plugins: { legend: { labels: { color: '#aaa', font: { size: 9 } }, position: 'bottom' } },
        scales: {
          x: { stacked: true, ticks: { color: '#888', maxRotation: 90, autoSkip: true, maxTicksLimit: 12 } },
          y: { stacked: true, ticks: { color: '#888' }, title: { display: true, text: 'GW', color: '#aaa' } },
        },
        maintainAspectRatio: false,
        responsive: true,
      },
    });
  }

  function drawTopLines(r) {
    const html = r.top_lines.map((l, i) => `
      <div class="topN-item">
        <span class="topN-rank">#${i+1}</span>
        <span class="topN-info">${l.v_nom}kV line ${l.line_id}<br>
          <span style="color:#888">${l.s_nom?.toFixed(0)} MW · bus ${l.bus0}↔${l.bus1}</span></span>
        <span class="topN-val">${l.max_loading_pct}%</span>
      </div>`).join('');
    document.getElementById('pf-toplines').innerHTML = html || '<div class="muted">No loading data.</div>';
  }

  async function showJobsList() {
    const jobs = await App.api('/api/pf/jobs');
    if (jobs.length === 0) { App.toast('No prior jobs'); return; }
    const html = jobs.slice(0, 10).map(j => `
      <div class="topN-item" onclick="TabPf._loadJob('${j.job_id}')">
        <span class="topN-rank">${j.state[0].toUpperCase()}</span>
        <span class="topN-info">${j.job_id} · ${j.start.slice(0,10)} → ${j.end.slice(0,10)}<br>
          <span style="color:#888">${j.n_snapshots}h · ${j.state}</span></span>
        <span class="topN-val" style="color:#aaa">${j.progress}%</span>
      </div>`).join('');
    App.showDetail(`<h3>Recent PF jobs</h3>${html}`);
  }

  async function _loadJob(jobId) {
    currentJobId = jobId;
    App.hideDetail();
    try {
      await loadResult(jobId);
    } catch (e) {
      App.toast('Could not load: ' + e.message);
    }
  }

  return { activate, deactivate, _loadJob };
})();

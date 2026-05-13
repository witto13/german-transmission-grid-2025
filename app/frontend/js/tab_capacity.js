/* Tab 4 — Capacity & Load overlay. Same topology base + bus-level aggregates. */

const TabCapacity = (() => {
  let layers = { buses: null, lines: null, trafos: null, links: null };
  let data = null;
  let busChart = null;
  let state = { v_nom: [220, 380], country: 'DE', metric: 'capacity' };

  const SIDEBAR_HTML = () => `
    <h2>Capacity &amp; Load</h2>
    <div class="muted">Bus circle size = aggregate per bus. Click a bus for the carrier mix.</div>

    <h3>Voltage</h3>
    <div>
      <label class="checkbox-row"><input type="checkbox" data-v="380" ${state.v_nom.includes(380) ? 'checked' : ''}> 380 kV</label>
      <label class="checkbox-row"><input type="checkbox" data-v="220" ${state.v_nom.includes(220) ? 'checked' : ''}> 220 kV</label>
      <label class="checkbox-row"><input type="checkbox" data-v="110" ${state.v_nom.includes(110) ? 'checked' : ''}> 110 kV</label>
    </div>

    <h3>Bus size by</h3>
    <div>
      <label class="checkbox-row"><input type="radio" name="metric" value="capacity" ${state.metric === 'capacity' ? 'checked' : ''}> Installed capacity (MW)</label>
      <label class="checkbox-row"><input type="radio" name="metric" value="load" ${state.metric === 'load' ? 'checked' : ''}> Peak load (MW)</label>
      <label class="checkbox-row"><input type="radio" name="metric" value="net" ${state.metric === 'net' ? 'checked' : ''}> Net (capacity − load)</label>
    </div>

    <button class="btn" id="apply" style="margin-top:10px">Reload</button>

    <div id="cap-summary" class="summary-box" style="margin-top: 12px"></div>

    <h3>Parallel-line corridors</h3>
    <div id="parallel-list" style="max-height: 200px; overflow-y: auto"></div>
  `;

  async function activate(map) {
    App.setSidebar(SIDEBAR_HTML());
    document.getElementById('apply').onclick = () => reload(map);
    document.querySelectorAll('input[name=metric]').forEach(r => {
      r.onchange = () => { state.metric = r.value; render(map); };
    });
    await reload(map);
  }

  function deactivate(map) {
    Object.values(layers).forEach(l => { if (l) map.removeLayer(l); });
    layers = { buses: null, lines: null, trafos: null, links: null };
    if (busChart) { busChart.destroy(); busChart = null; }
  }

  async function reload(map) {
    state.v_nom = Array.from(document.querySelectorAll('#sidebar input[data-v]:checked'))
                        .map(el => parseFloat(el.dataset.v));
    const params = new URLSearchParams();
    if (state.v_nom.length > 0) params.set('v_nom', state.v_nom.join(','));
    params.set('country', state.country);

    document.getElementById('cap-summary').innerHTML = '<div class="muted"><span class="spinner"></span>Loading…</div>';
    data = await App.api('/api/topology/enriched?' + params.toString());
    render(map);
  }

  function render(map) {
    Object.values(layers).forEach(l => { if (l) map.removeLayer(l); });

    const busCoord = {};
    data.buses.forEach(b => busCoord[b.bus_id] = [b.lat, b.lon]);

    // Choose metric per bus
    const metricVal = b => {
      const cap = b.capacity?.total_mw || 0;
      const ld = b.load?.total_peak_mw || 0;
      if (state.metric === 'capacity') return cap;
      if (state.metric === 'load') return ld;
      return cap - ld;
    };

    const maxAbs = Math.max(1, ...data.buses.map(b => Math.abs(metricVal(b))));

    const busFeatures = data.buses.map(b => {
      const v = metricVal(b);
      const r = Math.max(2, Math.min(28, Math.sqrt(Math.abs(v) / maxAbs) * 28));
      let color = App.colorForVoltage(b.v_nom);
      if (state.metric === 'net') {
        color = v >= 0 ? '#2ecc71' : '#e74c3c';  // green = surplus, red = deficit
      }
      const m = L.circleMarker([b.lat, b.lon], {
        radius: r,
        fillColor: color, color: '#000', weight: 0.5, fillOpacity: 0.7,
      });
      const cap = b.capacity?.total_mw || 0;
      const ld = b.load?.total_peak_mw || 0;
      m.bindTooltip(`<b>Bus ${b.bus_id}</b> · ${b.v_nom} kV<br>Capacity: ${cap.toFixed(0)} MW<br>Peak load: ${ld.toFixed(0)} MW`);
      m.on('click', () => showBusBreakdown(b));
      return m;
    });
    layers.buses = L.layerGroup(busFeatures).addTo(map);

    // Lines (with parallel offset + thickness by s_nom)
    const maxSnom = Math.max(1, ...data.lines.map(l => l.s_nom || 0));
    const lineFeatures = data.lines.map(l => {
      let coords = [busCoord[l.bus0], busCoord[l.bus1]].filter(c => c);
      if (coords.length < 2) return null;
      if (l.parallel_count > 1) {
        const off = (l.parallel_index - (l.parallel_count - 1) / 2) * 0.005;
        coords = offsetCoords(coords[0], coords[1], off);
      }
      const w = Math.max(0.6, Math.min(5, (l.s_nom || 0) / maxSnom * 5));
      const polyline = L.polyline(coords, {
        color: App.colorForVoltage(l.v_nom),
        weight: w, opacity: 0.85,
      });
      polyline.bindTooltip(
        `<b>Line ${l.line_id}</b> · ${l.v_nom} kV<br>` +
        `s_nom: ${l.s_nom?.toFixed(0)} MW<br>` +
        (l.parallel_count > 1 ? `<b>${l.parallel_count} parallel circuits</b>` : '')
      );
      return polyline;
    }).filter(Boolean);
    layers.lines = L.layerGroup(lineFeatures).addTo(map);

    // Trafos & links
    layers.trafos = L.layerGroup(data.transformers.map(t => {
      const c0 = busCoord[t.bus0], c1 = busCoord[t.bus1];
      if (!c0 || !c1) return null;
      return L.polyline([c0, c1], { color: '#FF9800', weight: 2.5, opacity: 0.85, dashArray: '6,4' })
              .bindTooltip(`<b>Trafo ${t.trafo_id}</b> · ${t.s_nom?.toFixed(0)} MW`);
    }).filter(Boolean)).addTo(map);

    layers.links = L.layerGroup(data.links.map(l => {
      const c0 = busCoord[l.bus0], c1 = busCoord[l.bus1];
      if (!c0 || !c1) return null;
      return L.polyline([c0, c1], { color: '#00e5ff', weight: 4, opacity: 0.9, dashArray: '10,6' })
              .bindTooltip(`<b>HVDC ${l.link_id}</b> · ${l.p_nom?.toFixed(0)} MW`);
    }).filter(Boolean)).addTo(map);

    // Summary
    const totalCap = data.buses.reduce((s, b) => s + (b.capacity?.total_mw || 0), 0);
    const totalLoad = data.buses.reduce((s, b) => s + (b.load?.total_peak_mw || 0), 0);
    document.getElementById('cap-summary').innerHTML = `
      <div class="stat"><span>Buses</span><span class="val">${data.buses.length}</span></div>
      <div class="stat"><span>Total capacity</span><span class="val">${(totalCap / 1000).toFixed(1)} GW</span></div>
      <div class="stat"><span>Total peak load</span><span class="val">${(totalLoad / 1000).toFixed(1)} GW</span></div>
      <div class="stat"><span>Lines</span><span class="val">${data.lines.length}</span></div>
      <div class="stat"><span>Trafos</span><span class="val">${data.transformers.length}</span></div>
      <div class="stat"><span>HVDC</span><span class="val">${data.links.length}</span></div>`;

    // Top parallel-line corridors
    const corridors = {};
    data.lines.forEach(l => {
      if (l.parallel_count > 1 && l.parallel_index === 0) {
        const key = `${Math.min(l.bus0, l.bus1)}-${Math.max(l.bus0, l.bus1)}-${l.v_nom}`;
        corridors[key] = { v_nom: l.v_nom, n: l.parallel_count, s_nom: l.s_nom, bus0: l.bus0, bus1: l.bus1 };
      }
    });
    const top = Object.values(corridors).sort((a, b) => (b.n * b.s_nom) - (a.n * a.s_nom)).slice(0, 30);
    document.getElementById('parallel-list').innerHTML = top.map(c => `
      <div class="topN-item" onclick="App.map().setView([${(busCoord[c.bus0]||[0,0])[0]}, ${(busCoord[c.bus0]||[0,0])[1]}], 11)">
        <span class="topN-rank">${c.n}×</span>
        <span class="topN-info">${c.v_nom}kV bus ${c.bus0}↔${c.bus1}</span>
        <span class="topN-val" style="color:#fff">${(c.n * c.s_nom).toFixed(0)} MW</span>
      </div>`).join('') || '<div class="muted">No parallel corridors at current voltages.</div>';
  }

  function offsetCoords(b0, b1, offset) {
    const dx = b1[1] - b0[1];
    const dy = b1[0] - b0[0];
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len < 1e-8) return [b0, b1];
    const px = -dy / len * offset;
    const py = dx / len * offset;
    return [[b0[0] + px, b0[1] + py], [b1[0] + px, b1[1] + py]];
  }

  function showBusBreakdown(b) {
    const cap = b.capacity || { total_mw: 0, by_carrier: {} };
    const ld  = b.load     || { total_peak_mw: 0, by_carrier: {} };

    const capLabels = Object.keys(cap.by_carrier);
    const capValues = capLabels.map(k => cap.by_carrier[k]);
    const capColors = capLabels.map(k => App.colorForCarrier(k));

    const ldLabels = Object.keys(ld.by_carrier);
    const ldValues = ldLabels.map(k => ld.by_carrier[k]);
    const ldColors = ldLabels.map(k => App.colorForCarrier(k));

    App.showDetail(`
      <h3>Bus #${b.bus_id} · ${b.v_nom} kV</h3>
      <div class="muted" style="margin-bottom: 8px">${b.country}</div>

      <div class="stat"><span>Total capacity</span><span class="val">${cap.total_mw.toFixed(0)} MW</span></div>
      <div class="stat"><span>Total peak load</span><span class="val">${ld.total_peak_mw.toFixed(0)} MW</span></div>
      <div class="stat"><span>Net</span><span class="val">${(cap.total_mw - ld.total_peak_mw).toFixed(0)} MW</span></div>

      <h3 style="margin-top:14px">Capacity mix</h3>
      <div id="bus-chart-wrap"><canvas id="bus-chart"></canvas></div>

      <h3>Carrier breakdown</h3>
      ${capLabels.map((k, i) => `
        <div class="carrier-row">
          <span class="carrier-dot" style="background:${capColors[i]}"></span>
          <span class="carrier-name">${k}</span>
          <span class="carrier-val">${capValues[i].toFixed(1)} MW</span>
        </div>`).join('') || '<div class="muted">No generation at this bus.</div>'}

      <h3 style="margin-top:14px">Load breakdown</h3>
      ${ldLabels.map((k, i) => `
        <div class="carrier-row">
          <span class="carrier-dot" style="background:${ldColors[i]}"></span>
          <span class="carrier-name">${k}</span>
          <span class="carrier-val">${ldValues[i].toFixed(1)} MW</span>
        </div>`).join('') || '<div class="muted">No load at this bus.</div>'}
    `);

    if (busChart) busChart.destroy();
    if (capLabels.length > 0) {
      busChart = new Chart(document.getElementById('bus-chart'), {
        type: 'doughnut',
        data: {
          labels: capLabels,
          datasets: [{ data: capValues, backgroundColor: capColors, borderColor: '#16213e', borderWidth: 1 }],
        },
        options: {
          plugins: { legend: { display: false } },
          maintainAspectRatio: false,
          responsive: true,
        },
      });
    }
  }

  return { activate, deactivate };
})();

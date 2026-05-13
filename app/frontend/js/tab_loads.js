/* Tab 2 — Load by County. Choropleth of 426 Landkreise. */

const TabLoads = (() => {
  let layer = null;
  let metric = 'peak_mw';   // or 'annual_gwh'
  let dataFC = null;

  const SIDEBAR_HTML = () => `
    <h2>Load by County</h2>
    <div class="muted">Choropleth of German Landkreise. Click a county for the breakdown.</div>

    <h3>Metric</h3>
    <div>
      <label class="checkbox-row"><input type="radio" name="metric" value="peak_mw" ${metric === 'peak_mw' ? 'checked' : ''}> Peak load (MW)</label>
      <label class="checkbox-row"><input type="radio" name="metric" value="annual_gwh" ${metric === 'annual_gwh' ? 'checked' : ''}> Annual energy (GWh)</label>
    </div>

    <h3>Color Scale</h3>
    <div id="legend"></div>

    <div id="summary" class="summary-box" style="margin-top: 14px"></div>
  `;

  function gradient(v, max) {
    if (max <= 0) return '#0f3460';
    const t = Math.min(1, Math.sqrt(v / max));
    // Blue (cool) → red (hot)
    const colors = [
      [15, 52, 96],     // dark blue
      [52, 152, 219],   // blue
      [241, 196, 15],   // yellow
      [231, 76, 60],    // red
    ];
    const idx = t * (colors.length - 1);
    const i = Math.floor(idx);
    const f = idx - i;
    const a = colors[i];
    const b = colors[Math.min(i + 1, colors.length - 1)];
    const r = Math.round(a[0] + (b[0] - a[0]) * f);
    const g = Math.round(a[1] + (b[1] - a[1]) * f);
    const bb = Math.round(a[2] + (b[2] - a[2]) * f);
    return `rgb(${r},${g},${bb})`;
  }

  function renderLegend(max) {
    const stops = [0, 0.25, 0.5, 0.75, 1].map(t => Math.round(max * t * t));
    document.getElementById('legend').innerHTML = stops.map(v => `
      <div class="legend-item">
        <div class="legend-color" style="background:${gradient(v, max)};width:30px"></div>
        ${v.toLocaleString()} ${metric === 'peak_mw' ? 'MW' : 'GWh'}
      </div>`).join('');
  }

  async function activate(map) {
    App.setSidebar(SIDEBAR_HTML());

    document.querySelectorAll('input[name=metric]').forEach(r => {
      r.onchange = (e) => { metric = e.target.value; render(map); };
    });

    if (!dataFC) {
      dataFC = await App.api('/api/loads/by-county');
    }
    render(map);
  }

  function deactivate(map) {
    if (layer) { map.removeLayer(layer); layer = null; }
  }

  function render(map) {
    if (layer) { map.removeLayer(layer); }

    const max = Math.max(...dataFC.features.map(f => f.properties[metric] || 0));
    renderLegend(max);

    layer = L.geoJSON(dataFC, {
      style: feature => {
        const v = feature.properties[metric] || 0;
        return {
          fillColor: gradient(v, max),
          weight: 0.5, color: '#fff', fillOpacity: 0.78,
        };
      },
      onEachFeature: (feature, lyr) => {
        const p = feature.properties;
        lyr.bindTooltip(
          `<b>${p.name}</b><br>` +
          `Peak: ${p.peak_mw.toLocaleString()} MW<br>` +
          `Annual: ${p.annual_gwh.toLocaleString()} GWh`
        );
        lyr.on('click', () => showCountyDetail(p.krs_id, p.name));
        lyr.on('mouseover', () => lyr.setStyle({ weight: 2, color: '#e94560' }));
        lyr.on('mouseout',  () => lyr.setStyle({ weight: 0.5, color: '#fff' }));
      }
    }).addTo(map);

    const totalPeak = dataFC.features.reduce((s, f) => s + f.properties.peak_mw, 0);
    const totalAnnual = dataFC.features.reduce((s, f) => s + f.properties.annual_gwh, 0);
    document.getElementById('summary').innerHTML = `
      <div class="stat"><span>Counties</span><span class="val">${dataFC.features.length}</span></div>
      <div class="stat"><span>Total peak</span><span class="val">${(totalPeak / 1000).toFixed(1)} GW</span></div>
      <div class="stat"><span>Total annual</span><span class="val">${(totalAnnual / 1000).toFixed(1)} TWh</span></div>`;
  }

  async function showCountyDetail(krs_id, name) {
    App.showDetail(`<h3>${name}</h3><div class="muted"><span class="spinner"></span>Loading…</div>`);
    try {
      const d = await App.api('/api/loads/by-county/' + krs_id);
      const carriers = d.by_carrier.map(c => `
        <div class="carrier-row">
          <span class="carrier-dot" style="background:${App.colorForCarrier(c.carrier)}"></span>
          <span class="carrier-name">${c.carrier}</span>
          <span class="carrier-val">${c.peak_mw.toLocaleString()} MW</span>
        </div>
        <div style="font-size:11px;color:#888;margin-left:18px">${c.annual_gwh.toLocaleString()} GWh / year</div>
      `).join('');

      const buses = d.top_buses.map((b, i) => `
        <div class="topN-item" onclick="App.map().setView([${b.lat}, ${b.lon}], 12)">
          <span class="topN-rank">#${i+1}</span>
          <span class="topN-info">Bus ${b.bus} (${b.v_nom} kV)<br><span style="color:#888">${b.annual_gwh.toLocaleString()} GWh/yr</span></span>
          <span class="topN-val">${b.peak_mw.toLocaleString()} MW</span>
        </div>`).join('');

      App.showDetail(`
        <h3>${d.name}</h3>
        <div class="muted" style="margin-bottom: 10px">AGS ${d.ags} · NUTS ${d.nuts}</div>
        <h3 style="color:#aaa;font-size:11px">By Carrier</h3>
        ${carriers}
        <h3 style="color:#aaa;font-size:11px;margin-top:14px">Top Load Buses</h3>
        ${buses}
      `);
    } catch (e) {
      App.showDetail(`<div style="color:#e74c3c">Error: ${e.message}</div>`);
    }
  }

  return { activate, deactivate };
})();

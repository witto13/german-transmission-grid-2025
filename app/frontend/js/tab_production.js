/* Tab 1 — Production map. Markers for all generators with filters + click-for-info. */

const TabProduction = (() => {
  let cluster = null;
  let carriers = [];
  let state = { carrier: [], v_nom: [], pnom_min: 1, country: 'DE' };

  const SIDEBAR_HTML = () => `
    <h2>Production</h2>
    <div class="muted">Click a generator on the map to see its full DB record.</div>

    <h3>Carrier</h3>
    <div id="carrier-list" style="max-height: 240px; overflow-y: auto;"></div>

    <h3>Bus Voltage</h3>
    <div>
      <label class="checkbox-row"><input type="checkbox" data-v="380" checked> 380 kV</label>
      <label class="checkbox-row"><input type="checkbox" data-v="220" checked> 220 kV</label>
      <label class="checkbox-row"><input type="checkbox" data-v="110" checked> 110 kV</label>
    </div>

    <h3>Min p_nom (MW)</h3>
    <div class="field">
      <input type="number" id="pnom-min" min="0" value="${state.pnom_min}" step="0.1">
    </div>

    <h3>Country</h3>
    <div class="field">
      <select id="country">
        <option value="">All</option>
        <option value="DE" selected>Germany</option>
        <option value="AT">Austria</option>
        <option value="CH">Switzerland</option>
        <option value="FR">France</option>
        <option value="NL">Netherlands</option>
        <option value="DK">Denmark</option>
        <option value="PL">Poland</option>
        <option value="CZ">Czechia</option>
        <option value="BE">Belgium</option>
        <option value="LU">Luxembourg</option>
      </select>
    </div>

    <button class="btn" id="apply-btn">Apply Filters</button>
    <div id="result-summary" class="summary-box" style="margin-top: 10px"></div>
  `;

  function renderCarrierList() {
    const html = carriers.map(c => `
      <label class="checkbox-row">
        <input type="checkbox" data-c="${c.carrier}" ${state.carrier.length === 0 || state.carrier.includes(c.carrier) ? 'checked' : ''}>
        <span class="carrier-dot" style="background:${App.colorForCarrier(c.carrier)};display:inline-block"></span>
        ${c.carrier}
        <span style="float:right;color:#888">${(c.total_mw / 1000).toFixed(1)} GW</span>
      </label>`).join('');
    document.getElementById('carrier-list').innerHTML = html;
  }

  async function activate(map) {
    App.setSidebar(SIDEBAR_HTML());

    if (carriers.length === 0) {
      carriers = await App.api('/api/generators/carriers');
    }
    renderCarrierList();

    document.getElementById('apply-btn').onclick = () => loadGenerators(map);

    cluster = L.markerClusterGroup({
      chunkedLoading: true,
      maxClusterRadius: 50,
      disableClusteringAtZoom: 11,
    });
    map.addLayer(cluster);

    await loadGenerators(map);
  }

  function deactivate(map) {
    if (cluster) {
      map.removeLayer(cluster);
      cluster = null;
    }
  }

  async function loadGenerators(map) {
    // Read filter state from sidebar
    state.carrier = Array.from(document.querySelectorAll('#carrier-list input:checked'))
                         .map(el => el.dataset.c);
    state.v_nom = Array.from(document.querySelectorAll('#sidebar input[data-v]:checked'))
                        .map(el => el.dataset.v);
    state.pnom_min = parseFloat(document.getElementById('pnom-min').value) || 0;
    state.country = document.getElementById('country').value;

    const params = new URLSearchParams();
    if (state.carrier.length > 0 && state.carrier.length < carriers.length) {
      params.set('carrier', state.carrier.join(','));
    }
    if (state.v_nom.length > 0 && state.v_nom.length < 3) {
      params.set('v_nom', state.v_nom.join(','));
    }
    if (state.pnom_min > 0) params.set('pnom_min', state.pnom_min);
    if (state.country) params.set('country', state.country);
    params.set('limit', 20000);

    document.getElementById('result-summary').innerHTML =
      `<div class="muted"><span class="spinner"></span>Loading…</div>`;

    const fc = await App.api('/api/generators?' + params.toString());
    cluster.clearLayers();

    let totalMw = 0;
    const markers = fc.features.map(f => {
      const p = f.properties;
      totalMw += p.p_nom;
      const r = Math.max(3, Math.min(20, Math.sqrt(p.p_nom) * 0.7));
      const m = L.circleMarker([f.geometry.coordinates[1], f.geometry.coordinates[0]], {
        radius: r,
        fillColor: App.colorForCarrier(p.carrier),
        color: '#000', weight: 0.5, fillOpacity: 0.85,
      });
      m.on('click', () => showGenDetail(p.id));
      m.bindTooltip(`<b>${p.carrier}</b> · ${p.p_nom.toFixed(1)} MW (id ${p.id})`);
      return m;
    });
    cluster.addLayers(markers);

    document.getElementById('result-summary').innerHTML = `
      <div class="stat"><span>Generators</span><span class="val">${fc.count.toLocaleString()}</span></div>
      <div class="stat"><span>Total Capacity</span><span class="val">${(totalMw / 1000).toFixed(1)} GW</span></div>`;
  }

  async function showGenDetail(gid) {
    App.showDetail('<div class="muted"><span class="spinner"></span>Loading…</div>');
    try {
      const d = await App.api('/api/generators/' + gid);
      const fields = [
        ['Generator ID', d.generator_id],
        ['Carrier', d.carrier],
        ['Bus', d.bus],
        ['Bus voltage (kV)', d.bus_v_nom],
        ['Country', d.country],
        ['Capacity (MW)', d.p_nom?.toFixed?.(2)],
        ['Marginal cost (€/MWh)', d.marginal_cost?.toFixed?.(2)],
        ['Efficiency', d.efficiency?.toFixed?.(3)],
        ['Build year', d.build_year],
        ['Lifetime', d.lifetime],
        ['Capital cost', d.capital_cost],
        ['p_min_pu (static)', d.p_min_pu],
        ['p_max_pu (static)', d.p_max_pu],
        ['p_set (static)', d.p_set],
        ['Sign', d.sign],
        ['Lon', d.lon?.toFixed?.(5)],
        ['Lat', d.lat?.toFixed?.(5)],
      ].filter(([_, v]) => v !== null && v !== undefined && v !== '');
      App.showDetail(`
        <h3>Generator #${d.generator_id}</h3>
        <div style="margin: 6px 0; padding: 6px 8px; background: ${App.colorForCarrier(d.carrier)}30; border-left: 3px solid ${App.colorForCarrier(d.carrier)}; border-radius: 3px;">
          <b>${d.carrier}</b> · ${d.p_nom?.toFixed?.(1)} MW
        </div>
        ${fields.map(([k, v]) => `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('')}
      `);
    } catch (e) {
      App.showDetail(`<div style="color:#e74c3c">Error: ${e.message}</div>`);
    }
  }

  return { activate, deactivate };
})();

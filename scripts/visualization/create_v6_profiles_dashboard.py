#!/usr/bin/env python3
"""
Generate interactive HTML dashboard showing:
1. Per-country 8760-hour import/export profiles
2. Per-country node count and load/gen allocation per node
"""

import json
import os
import numpy as np
import pandas as pd
from sqlalchemy import create_engine

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
V6 = 'eGon2025v6'
OUT_PATH = '/root/egon_2025_project/results/v6/profiles_dashboard.html'

COUNTRY_NAMES = {
    'AT': 'Austria', 'BE': 'Belgium', 'CH': 'Switzerland', 'CZ': 'Czechia',
    'DK': 'Denmark', 'FR': 'France', 'LU': 'Luxembourg', 'NL': 'Netherlands',
    'NO': 'Norway', 'PL': 'Poland', 'SE': 'Sweden',
}


def main():
    engine = create_engine(DB_URI)

    # Load generators, loads, buses
    gens = pd.read_sql(
        f"SELECT generator_id, bus, carrier, p_nom FROM grid.egon_etrago_generator "
        f"WHERE scn_name = '{V6}' AND carrier LIKE 'import_%%'", engine)
    loads = pd.read_sql(
        f"SELECT load_id, bus, carrier, p_set FROM grid.egon_etrago_load "
        f"WHERE scn_name = '{V6}' AND carrier LIKE 'export_%%'", engine)
    buses = pd.read_sql(
        f"SELECT bus_id, v_nom, x, y, country FROM grid.egon_etrago_bus "
        f"WHERE scn_name = '{V6}' AND country <> 'DE'", engine)

    # Load timeseries
    gen_ts = pd.read_sql(
        f"SELECT generator_id, p_max_pu FROM grid.egon_etrago_generator_timeseries "
        f"WHERE scn_name = '{V6}'", engine)
    load_ts = pd.read_sql(
        f"SELECT load_id, p_set FROM grid.egon_etrago_load_timeseries "
        f"WHERE scn_name = '{V6}'", engine)

    # Build per-country data
    countries_data = {}
    country_order = sorted(COUNTRY_NAMES.keys())

    for country in country_order:
        cgens = gens[gens['carrier'] == f'import_{country}'].copy()
        cloads = loads[loads['carrier'] == f'export_{country}'].copy()

        if len(cgens) == 0 and len(cloads) == 0:
            continue

        # Node info
        nodes = []
        for _, g in cgens.iterrows():
            bid = int(g['bus'])
            b = buses[buses['bus_id'] == bid]
            if len(b) == 0:
                continue
            b = b.iloc[0]
            # Find matching load
            matching_load = cloads[cloads['bus'] == bid]
            export_mw = float(matching_load.iloc[0]['p_set']) if len(matching_load) > 0 else 0
            nodes.append({
                'bus_id': bid,
                'v_nom': int(b['v_nom']),
                'lon': round(float(b['x']), 3),
                'lat': round(float(b['y']), 3),
                'import_mw': round(float(g['p_nom']), 1),
                'export_mw': round(export_mw, 1),
                'gen_id': int(g['generator_id']),
            })

        # Aggregate timeseries: sum of (p_nom * p_max_pu) across all country gens
        import_profile = np.zeros(8760)
        for _, g in cgens.iterrows():
            gid = int(g['generator_id'])
            ts_row = gen_ts[gen_ts['generator_id'] == gid]
            if len(ts_row) == 0:
                continue
            p_max_pu = np.array(ts_row.iloc[0]['p_max_pu'], dtype=float)
            import_profile += float(g['p_nom']) * p_max_pu

        export_profile = np.zeros(8760)
        for _, ld in cloads.iterrows():
            lid = int(ld['load_id'])
            ts_row = load_ts[load_ts['load_id'] == lid]
            if len(ts_row) == 0:
                continue
            p_set_ts = np.array(ts_row.iloc[0]['p_set'], dtype=float)
            export_profile += p_set_ts

        # Downsample to daily averages for plotting (365 points)
        import_daily = import_profile.reshape(365, 24).mean(axis=1)
        export_daily = export_profile.reshape(365, 24).mean(axis=1)

        # Also compute weekly averages (52 weeks)
        n_full_weeks = 8760 // 168  # 52 full weeks
        import_weekly = import_profile[:n_full_weeks * 168].reshape(n_full_weeks, 168).mean(axis=1)
        export_weekly = export_profile[:n_full_weeks * 168].reshape(n_full_weeks, 168).mean(axis=1)

        # One sample day (hourly) for a winter day (day 15) and summer day (day 180)
        import_winter_day = import_profile[15*24:(15+1)*24].tolist()
        import_summer_day = import_profile[180*24:(180+1)*24].tolist()
        export_winter_day = export_profile[15*24:(15+1)*24].tolist()
        export_summer_day = export_profile[180*24:(180+1)*24].tolist()

        countries_data[country] = {
            'name': COUNTRY_NAMES[country],
            'nodes': nodes,
            'n_nodes': len(nodes),
            'total_import_mw': round(sum(n['import_mw'] for n in nodes), 0),
            'total_export_mw': round(sum(n['export_mw'] for n in nodes), 0),
            'import_daily': [round(float(x), 1) for x in import_daily],
            'export_daily': [round(float(x), 1) for x in export_daily],
            'import_weekly': [round(float(x), 1) for x in import_weekly],
            'export_weekly': [round(float(x), 1) for x in export_weekly],
            'import_winter_day': [round(float(x), 1) for x in import_winter_day],
            'import_summer_day': [round(float(x), 1) for x in import_summer_day],
            'export_winter_day': [round(float(x), 1) for x in export_winter_day],
            'export_summer_day': [round(float(x), 1) for x in export_summer_day],
            'annual_import_twh': round(float(import_profile.sum() / 1e6), 2),
            'annual_export_twh': round(float(export_profile.sum() / 1e6), 2),
        }
        print(f"  {country} ({COUNTRY_NAMES[country]}): {len(nodes)} nodes, "
              f"import={countries_data[country]['annual_import_twh']} TWh, "
              f"export={countries_data[country]['annual_export_twh']} TWh")

    json_str = json.dumps(countries_data, separators=(',', ':'))
    country_list_js = json.dumps(country_order)

    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>V6 Cross-Border Profiles Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f6fa; color: #2d3436; }}
        .header {{ background: linear-gradient(135deg, #2c3e50, #3498db); color: white; padding: 20px 30px; }}
        .header h1 {{ font-size: 22px; font-weight: 600; }}
        .header p {{ font-size: 13px; opacity: 0.8; margin-top: 4px; }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}

        .country-tabs {{
            display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 20px;
            background: white; padding: 12px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .country-tab {{
            padding: 8px 16px; border: 2px solid #ddd; border-radius: 6px;
            cursor: pointer; font-size: 13px; font-weight: 600; transition: all 0.15s;
            background: white;
        }}
        .country-tab:hover {{ background: #ebf5fb; border-color: #3498db; }}
        .country-tab.active {{ background: #3498db; color: white; border-color: #3498db; }}

        .dashboard {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
        @media (max-width: 900px) {{ .dashboard {{ grid-template-columns: 1fr; }} }}

        .card {{
            background: white; border-radius: 8px; padding: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .card h3 {{ font-size: 14px; color: #636e72; margin-bottom: 10px; font-weight: 600; }}
        .card.wide {{ grid-column: 1 / -1; }}

        .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }}
        .summary-item {{ text-align: center; padding: 12px; background: #f8f9fa; border-radius: 6px; }}
        .summary-item .value {{ font-size: 24px; font-weight: 700; color: #2d3436; }}
        .summary-item .label {{ font-size: 11px; color: #636e72; margin-top: 2px; }}

        .node-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
        .node-table th {{ background: #f8f9fa; padding: 8px 10px; text-align: left; font-weight: 600; border-bottom: 2px solid #ddd; }}
        .node-table td {{ padding: 6px 10px; border-bottom: 1px solid #eee; }}
        .node-table tr:hover {{ background: #f8f9fa; }}
        .node-table .bar-cell {{ position: relative; }}
        .bar {{ height: 16px; border-radius: 3px; display: inline-block; vertical-align: middle; }}
        .bar-import {{ background: #3498db; }}
        .bar-export {{ background: #e74c3c; }}
        .pct {{ font-size: 11px; color: #999; margin-left: 4px; }}

        .chart-container {{ position: relative; height: 250px; }}
        .chart-container.tall {{ height: 300px; }}

        .view-toggle {{ display: flex; gap: 4px; margin-bottom: 8px; }}
        .view-btn {{
            padding: 4px 12px; border: 1px solid #ddd; border-radius: 4px;
            cursor: pointer; font-size: 11px; background: white;
        }}
        .view-btn.active {{ background: #3498db; color: white; border-color: #3498db; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>eGon2025v6 — Cross-Border Exchange Profiles</h1>
        <p>Import generators &amp; export loads at 56 foreign buses across 11 countries | 8,760 hourly profiles</p>
    </div>
    <div class="container">
        <div class="country-tabs" id="tabs"></div>
        <div class="summary-grid" id="summary"></div>
        <div class="dashboard">
            <div class="card wide">
                <h3 id="profileTitle">Annual Profile (Daily Averages)</h3>
                <div class="view-toggle">
                    <button class="view-btn active" onclick="setView('daily')">Daily avg</button>
                    <button class="view-btn" onclick="setView('weekly')">Weekly avg</button>
                </div>
                <div class="chart-container tall"><canvas id="profileChart"></canvas></div>
            </div>
            <div class="card">
                <h3>Winter Day (Jan 16) — Hourly</h3>
                <div class="chart-container"><canvas id="winterChart"></canvas></div>
            </div>
            <div class="card">
                <h3>Summer Day (Jun 30) — Hourly</h3>
                <div class="chart-container"><canvas id="summerChart"></canvas></div>
            </div>
            <div class="card wide">
                <h3 id="nodeTitle">Node Allocation</h3>
                <div style="overflow-x: auto;">
                    <table class="node-table" id="nodeTable">
                        <thead><tr>
                            <th>Bus ID</th><th>Voltage</th><th>Lon</th><th>Lat</th>
                            <th>Import (MW)</th><th>Share</th>
                            <th>Export (MW)</th><th>Share</th>
                        </tr></thead>
                        <tbody id="nodeBody"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        var DATA = {json_str};
        var COUNTRIES = {country_list_js};
        var currentCountry = COUNTRIES[0];
        var currentView = 'daily';
        var profileChart, winterChart, summerChart;

        function initTabs() {{
            var tabs = document.getElementById('tabs');
            COUNTRIES.forEach(function(c) {{
                var d = DATA[c];
                if (!d) return;
                var btn = document.createElement('button');
                btn.className = 'country-tab' + (c === currentCountry ? ' active' : '');
                btn.id = 'tab_' + c;
                btn.innerHTML = '<span style="font-size:15px;">' + c + '</span><br><span style="font-size:10px;color:#999;">' + d.name + ' (' + d.n_nodes + ')</span>';
                btn.onclick = function() {{ switchCountry(c); }};
                tabs.appendChild(btn);
            }});
        }}

        function switchCountry(c) {{
            currentCountry = c;
            COUNTRIES.forEach(function(cc) {{
                var el = document.getElementById('tab_' + cc);
                if (el) el.classList.toggle('active', cc === c);
            }});
            updateAll();
        }}

        function setView(v) {{
            currentView = v;
            document.querySelectorAll('.view-btn').forEach(function(btn) {{
                btn.classList.toggle('active', btn.textContent.toLowerCase().indexOf(v) >= 0);
            }});
            updateProfileChart();
        }}

        function updateAll() {{
            updateSummary();
            updateProfileChart();
            updateDayCharts();
            updateNodeTable();
        }}

        function updateSummary() {{
            var d = DATA[currentCountry];
            document.getElementById('summary').innerHTML =
                '<div class="summary-item"><div class="value">' + d.n_nodes + '</div><div class="label">Border Nodes</div></div>' +
                '<div class="summary-item"><div class="value" style="color:#3498db;">' + Math.round(d.total_import_mw).toLocaleString() + '</div><div class="label">Max Import (MW)</div></div>' +
                '<div class="summary-item"><div class="value" style="color:#e74c3c;">' + Math.round(d.total_export_mw).toLocaleString() + '</div><div class="label">Max Export (MW)</div></div>' +
                '<div class="summary-item"><div class="value">' + d.annual_import_twh + ' / ' + d.annual_export_twh + '</div><div class="label">Import / Export (TWh/yr)</div></div>';
        }}

        function updateProfileChart() {{
            var d = DATA[currentCountry];
            var importData, exportData, labels;

            if (currentView === 'weekly') {{
                importData = d.import_weekly;
                exportData = d.export_weekly;
                labels = importData.map(function(_, i) {{ return 'W' + (i + 1); }});
                document.getElementById('profileTitle').textContent = d.name + ' — Annual Profile (Weekly Averages)';
            }} else {{
                importData = d.import_daily;
                exportData = d.export_daily;
                labels = importData.map(function(_, i) {{
                    var date = new Date(2025, 0, i + 1);
                    return (date.getMonth() + 1) + '/' + date.getDate();
                }});
                document.getElementById('profileTitle').textContent = d.name + ' — Annual Profile (Daily Averages)';
            }}

            if (profileChart) profileChart.destroy();
            profileChart = new Chart(document.getElementById('profileChart'), {{
                type: 'line',
                data: {{
                    labels: labels,
                    datasets: [
                        {{
                            label: 'Import capacity (MW)',
                            data: importData,
                            borderColor: '#3498db', backgroundColor: 'rgba(52,152,219,0.1)',
                            fill: true, borderWidth: 1.5, pointRadius: 0, tension: 0.3
                        }},
                        {{
                            label: 'Export demand (MW)',
                            data: exportData,
                            borderColor: '#e74c3c', backgroundColor: 'rgba(231,76,60,0.1)',
                            fill: true, borderWidth: 1.5, pointRadius: 0, tension: 0.3
                        }}
                    ]
                }},
                options: {{
                    responsive: true, maintainAspectRatio: false,
                    interaction: {{ mode: 'index', intersect: false }},
                    scales: {{
                        x: {{ ticks: {{ maxTicksLimit: currentView === 'weekly' ? 26 : 12, font: {{ size: 10 }} }} }},
                        y: {{ title: {{ display: true, text: 'MW' }}, beginAtZero: true }}
                    }},
                    plugins: {{ legend: {{ position: 'top', labels: {{ font: {{ size: 11 }} }} }} }}
                }}
            }});
        }}

        function makeDayChart(canvasId, importData, exportData, title) {{
            var labels = Array.from({{length: 24}}, function(_, i) {{ return i + ':00'; }});
            return new Chart(document.getElementById(canvasId), {{
                type: 'line',
                data: {{
                    labels: labels,
                    datasets: [
                        {{
                            label: 'Import (MW)', data: importData,
                            borderColor: '#3498db', backgroundColor: 'rgba(52,152,219,0.15)',
                            fill: true, borderWidth: 2, pointRadius: 2, tension: 0.3
                        }},
                        {{
                            label: 'Export (MW)', data: exportData,
                            borderColor: '#e74c3c', backgroundColor: 'rgba(231,76,60,0.15)',
                            fill: true, borderWidth: 2, pointRadius: 2, tension: 0.3
                        }}
                    ]
                }},
                options: {{
                    responsive: true, maintainAspectRatio: false,
                    scales: {{
                        x: {{ ticks: {{ font: {{ size: 10 }} }} }},
                        y: {{ title: {{ display: true, text: 'MW' }}, beginAtZero: true }}
                    }},
                    plugins: {{ legend: {{ position: 'top', labels: {{ font: {{ size: 11 }} }} }} }}
                }}
            }});
        }}

        function updateDayCharts() {{
            var d = DATA[currentCountry];
            if (winterChart) winterChart.destroy();
            if (summerChart) summerChart.destroy();
            winterChart = makeDayChart('winterChart', d.import_winter_day, d.export_winter_day, 'Winter Day');
            summerChart = makeDayChart('summerChart', d.import_summer_day, d.export_summer_day, 'Summer Day');
        }}

        function updateNodeTable() {{
            var d = DATA[currentCountry];
            document.getElementById('nodeTitle').textContent = d.name + ' — ' + d.n_nodes + ' Border Node' + (d.n_nodes > 1 ? 's' : '');

            var maxImport = Math.max.apply(null, d.nodes.map(function(n) {{ return n.import_mw; }}));
            var maxExport = Math.max.apply(null, d.nodes.map(function(n) {{ return n.export_mw; }}));

            var html = '';
            d.nodes.sort(function(a, b) {{ return b.import_mw - a.import_mw; }});
            d.nodes.forEach(function(n) {{
                var impPct = (n.import_mw / d.total_import_mw * 100).toFixed(1);
                var expPct = d.total_export_mw > 0 ? (n.export_mw / d.total_export_mw * 100).toFixed(1) : '0.0';
                var impBarW = maxImport > 0 ? Math.round(n.import_mw / maxImport * 80) : 0;
                var expBarW = maxExport > 0 ? Math.round(n.export_mw / maxExport * 80) : 0;
                html += '<tr>' +
                    '<td><b>' + n.bus_id + '</b></td>' +
                    '<td>' + n.v_nom + ' kV</td>' +
                    '<td>' + n.lon + '</td>' +
                    '<td>' + n.lat + '</td>' +
                    '<td class="bar-cell"><span class="bar bar-import" style="width:' + impBarW + 'px;"></span> ' + Math.round(n.import_mw) + '</td>' +
                    '<td><span class="pct">' + impPct + '%</span></td>' +
                    '<td class="bar-cell"><span class="bar bar-export" style="width:' + expBarW + 'px;"></span> ' + Math.round(n.export_mw) + '</td>' +
                    '<td><span class="pct">' + expPct + '%</span></td>' +
                    '</tr>';
            }});
            document.getElementById('nodeBody').innerHTML = html;
        }}

        initTabs();
        updateAll();
    </script>
</body>
</html>'''

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w') as f:
        f.write(html)
    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"\nDashboard written to {OUT_PATH} ({size_kb:.0f} KB)")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Run linear optimal power flow (LOPF) on the eGon2025 German transmission grid.

Purpose
-------
Performs a 24-hour linear optimal power flow simulation for a single spring day
(April 15, 2025) to validate the network model and produce realistic dispatch
and line-loading results. The optimization minimizes total generation cost using
a merit-order approach with technology-specific marginal costs.

Algorithm / Method
------------------
1. Loads all grid components (buses, lines, transformers, generators, loads)
   from the PostgreSQL database for the ``eGon2025`` scenario.
2. Builds a PyPSA network with 24 hourly snapshots (00:00--23:00).
3. Assigns synthetic capacity-factor profiles to variable generators:
   - Solar: bell curve peaking at 13:00, max CF ~0.40
   - Onshore wind: gentle diurnal pattern, average CF ~0.25
   - Offshore wind: flatter profile, average CF ~0.35
   - Must-run (biogas, biomass, waste, run-of-river): fixed 80% CF
4. Applies an ENTSO-E-style German weekday load profile (normalized) to all
   loads, scaling by each load's ``p_set`` value.
5. Runs LOPF with the CBC solver (lines set to ``s_max_pu=1e6``, effectively
   unconstrained) to obtain least-cost dispatch.
6. Analyzes and reports generation dispatch by carrier, line loading
   statistics, and energy balance.

Inputs
------
- PostgreSQL database (``egon-data`` on port 59734):
  - ``grid.egon_etrago_bus`` (buses for eGon2025)
  - ``grid.egon_etrago_line`` (lines, must have nonzero impedance x)
  - ``grid.egon_etrago_transformer`` (transformers)
  - ``grid.egon_etrago_generator`` (generators with carrier and p_nom)
  - ``grid.egon_etrago_load`` (loads with p_set)
- CBC solver at ``/root/miniconda3/envs/egon2025/bin/cbc``

Outputs
-------
- ``results/powerflow_april15.nc`` -- Full PyPSA network in netCDF format
  (includes all component data and time-series results).
- ``results/dispatch_april15.csv`` -- Hourly generation dispatch by carrier
  (MW), 24 rows x N carrier columns.
- ``results/line_loading_april15.csv`` -- Per-line loading statistics (mean,
  max, and s_nom in MVA).

Usage
-----
::

    conda activate egon2025
    python scripts/run_powerflow.py
"""

import sys
import numpy as np
import pandas as pd
from sqlalchemy import create_engine
import pypsa
import warnings
import logging

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger(__name__)

# ── Database connection ──────────────────────────────────────────────────
ENGINE = create_engine('postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')
SCN = 'eGon2025'

# ── Time setup: April 15 2025, 24 hourly snapshots ──────────────────────
DATE = '2025-04-15'
snapshots = pd.date_range(f'{DATE} 00:00', f'{DATE} 23:00', freq='h')


def load_table(table, extra_cols=''):
    """Load a grid table for the eGon2025 scenario."""
    sql = f"SELECT * FROM grid.{table} WHERE scn_name = '{SCN}'"
    df = pd.read_sql(sql, ENGINE)
    log.info(f"  {table}: {len(df)} rows")
    return df


def german_load_profile(hours):
    """
    Typical German weekday load profile (normalized to peak=1.0).
    Based on ENTSO-E typical April weekday shape.
    """
    # Hour-by-hour factors (0-23h), peak around 12:00 and 19:00
    profile = np.array([
        0.62, 0.58, 0.55, 0.54, 0.55, 0.58,  # 00-05: night
        0.65, 0.75, 0.85, 0.92, 0.96, 0.98,  # 06-11: morning ramp
        1.00, 0.98, 0.95, 0.93, 0.92, 0.94,  # 12-17: midday/afternoon
        0.96, 0.95, 0.90, 0.82, 0.74, 0.67,  # 18-23: evening decline
    ])
    return profile[hours]


def solar_profile(hours):
    """
    Moderate April solar capacity factor profile.
    Sunrise ~06:30, sunset ~20:00, peak CF ~0.40 at 13:00.
    """
    # Approximate with shifted cosine
    cf = np.zeros(len(hours))
    for i, h in enumerate(hours):
        if 6 <= h <= 20:
            # Bell curve centered at 13:00 (slightly after noon for April)
            cf[i] = max(0, 0.40 * np.cos(np.pi * (h - 13.0) / 14.0) ** 1.5)
    return cf


def wind_onshore_profile(hours):
    """
    Moderate April onshore wind profile. Average CF ~0.25.
    Wind tends to be slightly higher in afternoon.
    """
    base = 0.20
    variation = 0.10 * np.sin(np.pi * (hours - 4) / 24)  # gentle diurnal
    noise = np.array([
        0.22, 0.20, 0.19, 0.18, 0.19, 0.21,
        0.23, 0.25, 0.27, 0.28, 0.30, 0.31,
        0.32, 0.31, 0.30, 0.28, 0.27, 0.26,
        0.25, 0.24, 0.23, 0.22, 0.21, 0.20,
    ])
    return noise[hours]


def wind_offshore_profile(hours):
    """
    Moderate April offshore wind profile. Average CF ~0.35.
    Less diurnal variation than onshore.
    """
    noise = np.array([
        0.33, 0.32, 0.31, 0.30, 0.31, 0.32,
        0.34, 0.35, 0.36, 0.37, 0.38, 0.38,
        0.39, 0.38, 0.37, 0.36, 0.36, 0.35,
        0.34, 0.34, 0.33, 0.33, 0.32, 0.32,
    ])
    return noise[hours]


def build_network():
    """Build PyPSA network from database."""
    log.info("Loading data from database...")
    buses = load_table('egon_etrago_bus')
    lines = load_table('egon_etrago_line')
    trafos = load_table('egon_etrago_transformer')
    gens = load_table('egon_etrago_generator')
    loads = load_table('egon_etrago_load')

    # ── Create network ───────────────────────────────────────────────────
    log.info("Building PyPSA network...")
    n = pypsa.Network()
    n.set_snapshots(snapshots)

    # ── Buses ────────────────────────────────────────────────────────────
    buses = buses.set_index('bus_id')
    n.madd('Bus',
           buses.index.astype(str),
           v_nom=buses['v_nom'].values,
           x=buses['x'].values,
           y=buses['y'].values,
           carrier='AC')

    # ── Lines ────────────────────────────────────────────────────────────
    # Filter out lines with zero impedance (would cause numerical issues)
    valid_lines = lines[lines['x'] > 0].copy()
    log.info(f"  Lines with x>0: {len(valid_lines)} of {len(lines)}")

    if len(valid_lines) == 0:
        log.error("No lines have impedance data! Cannot run power flow.")
        sys.exit(1)

    n.madd('Line',
           valid_lines['line_id'].astype(str),
           bus0=valid_lines['bus0'].astype(str).values,
           bus1=valid_lines['bus1'].astype(str).values,
           x=valid_lines['x'].values,
           r=valid_lines['r'].values,
           s_nom=valid_lines['s_nom'].values,
           length=valid_lines['length'].values,
           v_nom=valid_lines['v_nom'].values,
           s_nom_extendable=False,
           s_max_pu=1e6)  # Effectively unconstrained

    # ── Transformers ─────────────────────────────────────────────────────
    trafos = trafos.copy()
    zero_x = trafos[(trafos['x'] == 0) & (trafos['s_nom'] > 0)]
    if len(zero_x) > 0:
        log.warning(f"{len(zero_x)} transformers have x=0 — "
                    f"run apply_jao_params.py first")
        trafos.loc[zero_x.index, 'x'] = 0.10

    n.madd('Transformer',
           trafos['trafo_id'].astype(str),
           bus0=trafos['bus0'].astype(str).values,
           bus1=trafos['bus1'].astype(str).values,
           x=trafos['x'].values,
           r=trafos['r'].values,
           s_nom=trafos['s_nom'].values,
           tap_ratio=trafos['tap_ratio'].values,
           phase_shift=trafos['phase_shift'].values,
           s_max_pu=1e6)  # Unconstrained

    # ── Generators ───────────────────────────────────────────────────────
    gens = gens.copy()

    # Marginal costs for merit order dispatch (€/MWh)
    mc_map = {
        'solar': 0, 'onwind': 0, 'offwind': 0,
        'run_of_river': 0, 'reservoir': 5,
        'biogas': 25, 'biomass': 30, 'waste': 15,
        'lignite': 35, 'coal': 45, 'gas': 55,
        'oil': 80, 'other': 60, 'hydrogen': 100,
    }
    gens['marginal_cost'] = gens['carrier'].map(mc_map).fillna(50)

    # Identify variable vs dispatchable generators
    variable_carriers = {'solar', 'onwind', 'offwind'}
    must_run_carriers = {'run_of_river', 'biogas', 'biomass', 'waste'}

    n.madd('Generator',
           gens['generator_id'].astype(str),
           bus=gens['bus'].astype(str).values,
           carrier=gens['carrier'].values,
           p_nom=gens['p_nom'].values,
           marginal_cost=gens['marginal_cost'].values,
           p_min_pu=0.0,
           p_max_pu=1.0)

    # ── Time-varying generator profiles ──────────────────────────────────
    hours = np.arange(24)

    solar_cf = solar_profile(hours)
    onwind_cf = wind_onshore_profile(hours)
    offwind_cf = wind_offshore_profile(hours)

    # Must-run plants at fixed output
    must_run_cf = np.full(24, 0.80)  # 80% capacity factor

    log.info("Setting time-varying capacity factors...")

    # Solar generators
    solar_ids = gens[gens['carrier'] == 'solar']['generator_id'].astype(str)
    if len(solar_ids) > 0:
        solar_ts = pd.DataFrame(
            np.tile(solar_cf, (len(solar_ids), 1)).T,
            index=snapshots,
            columns=solar_ids
        )
        n.generators_t.p_max_pu = pd.concat(
            [n.generators_t.p_max_pu, solar_ts], axis=1)

    # Onshore wind
    onwind_ids = gens[gens['carrier'] == 'onwind']['generator_id'].astype(str)
    if len(onwind_ids) > 0:
        onwind_ts = pd.DataFrame(
            np.tile(onwind_cf, (len(onwind_ids), 1)).T,
            index=snapshots,
            columns=onwind_ids
        )
        n.generators_t.p_max_pu = pd.concat(
            [n.generators_t.p_max_pu, onwind_ts], axis=1)

    # Offshore wind
    offwind_ids = gens[gens['carrier'] == 'offwind']['generator_id'].astype(str)
    if len(offwind_ids) > 0:
        offwind_ts = pd.DataFrame(
            np.tile(offwind_cf, (len(offwind_ids), 1)).T,
            index=snapshots,
            columns=offwind_ids
        )
        n.generators_t.p_max_pu = pd.concat(
            [n.generators_t.p_max_pu, offwind_ts], axis=1)

    # Must-run (biogas, biomass, waste, run_of_river)
    # Cap at 80% CF but let optimizer decide min dispatch
    for carrier in must_run_carriers:
        c_ids = gens[gens['carrier'] == carrier]['generator_id'].astype(str)
        if len(c_ids) > 0:
            c_ts = pd.DataFrame(
                np.tile(must_run_cf, (len(c_ids), 1)).T,
                index=snapshots,
                columns=c_ids
            )
            n.generators_t.p_max_pu = pd.concat(
                [n.generators_t.p_max_pu, c_ts], axis=1)

    # ── Loads ────────────────────────────────────────────────────────────
    loads = loads.copy()
    load_profile = german_load_profile(hours)

    n.madd('Load',
           loads['load_id'].astype(str),
           bus=loads['bus'].astype(str).values,
           carrier=loads['carrier'].values if 'carrier' in loads.columns else 'AC',
           p_set=loads['p_set'].abs().values)  # Static peak demand

    # Time-varying load: scale each load by the hourly profile
    log.info("Setting time-varying load profiles...")
    load_ids = loads['load_id'].astype(str)
    load_peaks = loads['p_set'].abs().values  # MW peak demand

    load_ts = pd.DataFrame(
        np.outer(load_profile, load_peaks),
        index=snapshots,
        columns=load_ids
    )
    n.loads_t.p_set = load_ts

    # No explicit slack generator needed - LOPF handles power balance.
    # PyPSA automatically selects a slack bus for the linear power flow.

    log.info(f"Network built: {len(n.buses)} buses, {len(n.lines)} lines, "
             f"{len(n.transformers)} transformers, {len(n.generators)} generators, "
             f"{len(n.loads)} loads")

    return n


def check_balance(n):
    """Print generation capacity vs load for each snapshot."""
    hours = np.arange(24)
    load_profile = german_load_profile(hours)

    total_load_peak = n.loads.p_set.sum()
    total_gen_capacity = n.generators.p_nom.sum()

    log.info(f"\nCapacity summary:")
    log.info(f"  Total installed generation: {total_gen_capacity:.0f} MW")
    log.info(f"  Total peak load:            {total_load_peak:.0f} MW")

    # Hourly available generation
    log.info(f"\nHourly balance (MW):")
    log.info(f"  {'Hour':>4s}  {'Load':>8s}  {'Solar':>8s}  {'OnWind':>8s}  {'OffWind':>8s}  {'MustRun':>8s}  {'Disp.Cap':>9s}")

    solar_cap = n.generators[n.generators.carrier == 'solar'].p_nom.sum()
    onwind_cap = n.generators[n.generators.carrier == 'onwind'].p_nom.sum()
    offwind_cap = n.generators[n.generators.carrier == 'offwind'].p_nom.sum()
    must_run_cap = n.generators[n.generators.carrier.isin(
        ['run_of_river', 'biogas', 'biomass', 'waste'])].p_nom.sum()
    disp_cap = n.generators[n.generators.carrier.isin(
        ['gas', 'coal', 'lignite', 'oil', 'other', 'reservoir', 'hydrogen'])].p_nom.sum()

    s_cf = solar_profile(hours)
    w_cf = wind_onshore_profile(hours)
    o_cf = wind_offshore_profile(hours)

    for h in range(24):
        load_h = total_load_peak * load_profile[h]
        solar_h = solar_cap * s_cf[h]
        onwind_h = onwind_cap * w_cf[h]
        offwind_h = offwind_cap * o_cf[h]
        must_h = must_run_cap * 0.8
        log.info(f"  {h:4d}  {load_h:8.0f}  {solar_h:8.0f}  {onwind_h:8.0f}  "
                 f"{offwind_h:8.0f}  {must_h:8.0f}  {disp_cap:9.0f}")


def run_lopf(n):
    """Run linear optimal power flow with unconstrained lines."""
    log.info("\n" + "=" * 60)
    log.info("Running Linear Optimal Power Flow (LOPF)...")
    log.info("  Lines: UNCONSTRAINED (s_max_pu = 1e6)")
    log.info("  Solver: CBC")
    log.info("=" * 60)

    status, condition = n.lopf(
        pyomo=False,
        solver_name='cbc',
    )

    log.info(f"  Status: {status}, Condition: {condition}")
    return status, condition


def analyze_results(n):
    """Analyze and print power flow results."""
    log.info("\n" + "=" * 60)
    log.info("POWER FLOW RESULTS")
    log.info("=" * 60)

    # ── Generation dispatch by carrier ───────────────────────────────────
    gen_p = n.generators_t.p  # (snapshots x generators)
    carriers = n.generators.carrier

    log.info("\nGeneration dispatch by carrier (MW):")
    log.info(f"  {'Carrier':<14s}  {'00:00':>8s}  {'06:00':>8s}  {'12:00':>8s}  "
             f"{'18:00':>8s}  {'22:00':>8s}  {'DailyGWh':>9s}")

    carrier_list = ['solar', 'onwind', 'offwind', 'run_of_river', 'biogas',
                    'biomass', 'waste', 'lignite', 'coal', 'gas', 'oil',
                    'other', 'reservoir', 'hydrogen']

    for carrier in carrier_list:
        gen_ids = n.generators[n.generators.carrier == carrier].index
        if len(gen_ids) == 0:
            continue
        p_carrier = gen_p[gen_ids].sum(axis=1)
        daily_gwh = p_carrier.sum() / 1000  # MWh -> GWh (hourly snapshots)
        hours_show = [0, 6, 12, 18, 22]
        vals = [p_carrier.iloc[h] for h in hours_show]
        log.info(f"  {carrier:<14s}  " +
                 "  ".join(f"{v:8.0f}" for v in vals) +
                 f"  {daily_gwh:9.1f}")

    # Total generation
    total_gen = gen_p.sum(axis=1)
    total_load = n.loads_t.p_set.sum(axis=1) if hasattr(n.loads_t, 'p_set') and len(n.loads_t.p_set) > 0 else n.loads.p_set.sum()
    log.info(f"\n  {'TOTAL GEN':<14s}  " +
             "  ".join(f"{total_gen.iloc[h]:8.0f}" for h in [0, 6, 12, 18, 22]) +
             f"  {total_gen.sum()/1000:9.1f}")
    if isinstance(total_load, pd.Series):
        log.info(f"  {'TOTAL LOAD':<14s}  " +
                 "  ".join(f"{total_load.iloc[h]:8.0f}" for h in [0, 6, 12, 18, 22]) +
                 f"  {total_load.sum()/1000:9.1f}")

    # ── Line loading ─────────────────────────────────────────────────────
    if len(n.lines_t.p0) > 0:
        line_flow = n.lines_t.p0.abs()
        line_loading = line_flow.div(n.lines.s_nom, axis=1) * 100  # percentage

        log.info(f"\nLine loading statistics (% of s_nom):")
        for snap_idx, label in [(12, '12:00 (peak solar)'), (0, '00:00 (night)')]:
            if snap_idx < len(line_loading):
                loading = line_loading.iloc[snap_idx]
                log.info(f"  {label}:")
                log.info(f"    Mean: {loading.mean():.1f}%, Median: {loading.median():.1f}%, "
                         f"Max: {loading.max():.1f}%, >100%: {(loading > 100).sum()} lines")

        # Top 10 most loaded lines (at noon)
        if 12 < len(line_loading):
            noon_loading = line_loading.iloc[12].sort_values(ascending=False)
            log.info(f"\n  Top 10 loaded lines at 12:00:")
            for i, (line_id, pct) in enumerate(noon_loading.head(10).items()):
                flow = line_flow.iloc[12][line_id]
                s_nom = n.lines.loc[line_id, 's_nom']
                v = n.lines.loc[line_id, 'v_nom'] if 'v_nom' in n.lines.columns else '?'
                log.info(f"    {i+1:2d}. Line {line_id}: {flow:.0f}/{s_nom:.0f} MW "
                         f"({pct:.1f}%) [{v} kV]")

    # ── Bus voltage angles (DC power flow) ───────────────────────────────
    if len(n.buses_t.v_ang) > 0:
        v_ang = n.buses_t.v_ang
        noon_ang = v_ang.iloc[12] if 12 < len(v_ang) else v_ang.iloc[0]
        log.info(f"\n  Bus voltage angle spread at noon: "
                 f"{noon_ang.min():.4f} to {noon_ang.max():.4f} rad "
                 f"({np.degrees(noon_ang.min()):.2f}° to {np.degrees(noon_ang.max()):.2f}°)")

    # ── Renewable curtailment ────────────────────────────────────────────
    log.info(f"\nRenewable generation summary:")
    for carrier in ['solar', 'onwind', 'offwind']:
        gen_ids = n.generators[n.generators.carrier == carrier].index
        if len(gen_ids) == 0:
            continue
        p_actual = gen_p[gen_ids].sum(axis=1)
        p_max = (n.generators_t.p_max_pu[gen_ids] * n.generators.loc[gen_ids, 'p_nom']).sum(axis=1)
        curtailment = (p_max - p_actual).clip(lower=0)
        total_potential = p_max.sum() / 1000
        total_actual = p_actual.sum() / 1000
        total_curt = curtailment.sum() / 1000
        log.info(f"  {carrier}: potential={total_potential:.1f} GWh, "
                 f"dispatched={total_actual:.1f} GWh, "
                 f"curtailed={total_curt:.1f} GWh "
                 f"({100*total_curt/max(total_potential,1):.1f}%)")

    # ── System cost ───────────────────────────────────────────────────────
    total_cost = (gen_p * n.generators.marginal_cost).sum().sum()
    log.info(f"\nTotal system cost: {total_cost/1e6:.1f} M€")
    log.info(f"Average cost: {total_cost / (n.loads_t.p_set.sum().sum()):.1f} €/MWh")


def save_results(n):
    """Save network and results."""
    import os
    os.makedirs('/root/egon_2025_project/results', exist_ok=True)

    outfile = '/root/egon_2025_project/results/powerflow_april15.nc'
    n.export_to_netcdf(outfile)
    log.info(f"\nNetwork saved to {outfile}")

    # Save dispatch summary CSV
    gen_p = n.generators_t.p
    carriers = n.generators.carrier
    dispatch = gen_p.T.groupby(carriers).sum().T
    dispatch.index.name = 'snapshot'
    dispatch_file = '/root/egon_2025_project/results/dispatch_april15.csv'
    dispatch.to_csv(dispatch_file)
    log.info(f"Dispatch summary saved to {dispatch_file}")

    # Save line loading CSV
    if len(n.lines_t.p0) > 0:
        loading = n.lines_t.p0.abs().div(n.lines.s_nom, axis=1) * 100
        loading_stats = pd.DataFrame({
            'mean_loading_pct': loading.mean(),
            'max_loading_pct': loading.max(),
            's_nom': n.lines.s_nom,
            'v_nom': n.lines.v_nom if 'v_nom' in n.lines.columns else np.nan,
        })
        loading_file = '/root/egon_2025_project/results/line_loading_april15.csv'
        loading_stats.to_csv(loading_file)
        log.info(f"Line loading saved to {loading_file}")


def main():
    log.info("=" * 60)
    log.info("eGon2025 Power Flow - April 15, 2025")
    log.info("Moderate PV + Wind day, unconstrained lines")
    log.info("=" * 60)

    n = build_network()
    check_balance(n)
    status, condition = run_lopf(n)

    if status != 'ok':
        log.error(f"LOPF failed: {status} / {condition}")
        log.info("Attempting with CBC solver as fallback...")
        status, condition = n.lopf(pyomo=False, solver_name='cbc')
        if status != 'ok':
            log.error(f"CBC also failed: {status} / {condition}")
            sys.exit(1)

    analyze_results(n)
    save_results(n)

    log.info("\nDone!")


if __name__ == '__main__':
    main()

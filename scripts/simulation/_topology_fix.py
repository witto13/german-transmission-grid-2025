"""_topology_fix.py — Apply topology fixes (new EHV/110 transformers, etc.) to an
in-memory PyPSA Network. Used by the corrected DC PF and the two-stage redispatch
without rebuilding the dispatch netCDF.

Reads `data/topology_fixes_transformers.csv` produced by `derive_topology_fixes.py`.
"""
from pathlib import Path
import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

FIX_CSV = Path("/root/egon_2025_project/data/topology_fixes_transformers.csv")
NEW_TRAFO_ID_START = 90000      # avoid clashing with existing trafo ids (≤33000) or PSTs (32000+)


def apply_fixes(n, verbose=True, fix_csv: Path = FIX_CSV):
    """Add the transformer fixes listed in fix_csv to n.transformers in place.

    Transformer impedance is x_pu (per-unit on the transformer's own MVA base), to
    match the model's existing convention. A small distance-proportional adder is
    folded in to represent the EHV connecting line when the matched EHV substation
    is several km away (real 110/EHV transformers sit inside one substation).
    """
    if not fix_csv.exists():
        log.warning(f"No fix CSV at {fix_csv}; skipping topology fixes.")
        return n
    df = pd.read_csv(fix_csv)
    if df.empty:
        return n

    df["low_bus"] = df["low_bus"].astype(str)
    df["ehv_bus"] = df["ehv_bus"].astype(str)

    # Only add fixes where both endpoints exist in this network
    df = df[df["low_bus"].isin(n.buses.index) & df["ehv_bus"].isin(n.buses.index)]
    if df.empty:
        return n

    n_added = 0
    total_mva = 0.0
    for i, r in df.reset_index(drop=True).iterrows():
        tid = str(NEW_TRAFO_ID_START + i)
        if tid in n.transformers.index:
            continue
        # x adder for the ehv-side connecting line (≈0.075 ohm/km at 380, 0.085 at 220);
        # convert to per-unit on the transformer s_nom base.
        v_ehv = float(r["ehv_kv"])
        d_km = float(r["distance_km"])
        x_line_ohm = (0.075 if v_ehv == 380 else 0.085) * d_km
        x_line_pu = x_line_ohm / (v_ehv ** 2 / float(r["s_nom_MVA"]))
        x_pu = float(r["x_pu"]) + x_line_pu      # transformer x + implicit ehv line
        # Use PyPSA's native add() so the index/dtypes are handled correctly and
        # determine_network_topology / find_cycles can include the new trafos.
        n.add("Transformer", tid,
              bus0=str(r["low_bus"]),
              bus1=str(r["ehv_bus"]),
              s_nom=float(r["s_nom_MVA"]),
              x=x_pu,
              r=float(r["r_pu"]),
              model="t",
              tap_ratio=1.0,
              tap_position=0,
              phase_shift=0.0,
              num_parallel=1.0)
        n_added += 1
        total_mva += float(r["s_nom_MVA"])

    if verbose:
        log.info(f"  +{n_added} transformer additions applied ({total_mva:.0f} MVA total).")
    return n

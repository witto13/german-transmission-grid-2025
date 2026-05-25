#!/usr/bin/env python3
"""Compare line loadings: OLD raw-ohm PTDF vs PyPSA correct per-unit PTDF.

Uses PyPSA's native, well-tested PTDF (per-unit, max|PTDF|=1.0) as the correct
reference and shows overloads collapse on the worst snapshots.
"""
import numpy as np
import pypsa
import time
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import factorized

NC = "/root/egon_2025_project/results/dispatch_8760h.nc"
SAMPLE_SNAPS = [7450, 880, 5897, 100]

print("Loading...", flush=True)
n = pypsa.Network(NC)
n.calculate_dependent_values()
n.lines.loc["33300", "x"] = 0.30          # fix dummy connector
n.calculate_dependent_values()
n.determine_network_topology()

bus_ids = list(n.buses.index); bus_idx = {b: i for i, b in enumerate(bus_ids)}
B = len(bus_ids)
inj = np.zeros((len(SAMPLE_SNAPS), B))
p_gen = n.generators_t.p
gb = n.generators.loc[p_gen.columns, "bus"].map(bus_idx).values
for r, t in enumerate(SAMPLE_SNAPS):
    np.add.at(inj[r], gb, p_gen.iloc[t].values)
p_load = n.loads_t.p if (n.loads_t.p is not None and not n.loads_t.p.empty) else n.loads_t.p_set
lb = n.loads.loc[p_load.columns, "bus"].map(bus_idx).values
for r, t in enumerate(SAMPLE_SNAPS):
    np.add.at(inj[r], lb, -p_load.iloc[t].values)
pdis = n.storage_units_t.p_dispatch; pst = n.storage_units_t.p_store
if pdis is not None and not pdis.empty:
    sb = n.storage_units.loc[pdis.columns, "bus"].map(bus_idx).values
    for r, t in enumerate(SAMPLE_SNAPS):
        net = pdis.iloc[t].values - (pst.iloc[t].values if pst is not None and not pst.empty else 0)
        np.add.at(inj[r], sb, net)
print("injection done", flush=True)

# main subnetwork
main, ms = None, 0
for _, row in n.sub_networks.iterrows():
    if len(row.obj.buses_i()) > ms:
        ms = len(row.obj.buses_i()); main = row.obj

# ---------- OLD raw-ohm PTDF (replicates production code) ----------
def old_ptdf():
    sub_buses = list(main.buses_i()); sub_lines = list(main.lines_i())
    sub_trafos = list(main.transformers_i())
    bidx = {b: i for i, b in enumerate(sub_buses)}
    Bn = len(sub_buses); L = len(sub_lines)
    li = n.lines.loc[sub_lines]
    bs = 1.0/li["x"].values.astype(np.float64)
    b0 = np.array([bidx[b] for b in li["bus0"]]); b1 = np.array([bidx[b] for b in li["bus1"]])
    a0 = list(b0); a1 = list(b1); ab = list(bs)
    ti = n.transformers.loc[sub_trafos]
    tx = ti["x"].values.astype(np.float64); tx = np.where(tx > 0, tx, 1.0)
    tb0 = np.array([bidx[b] for b in ti["bus0"]]); tb1 = np.array([bidx[b] for b in ti["bus1"]])
    a0 += tb0.tolist(); a1 += tb1.tolist(); ab += (1.0/tx).tolist()
    a0 = np.asarray(a0); a1 = np.asarray(a1); ab = np.asarray(ab)
    Bbus = csr_matrix((np.concatenate([-ab, -ab, ab, ab]),
                       (np.concatenate([a0, a1, a0, a1]), np.concatenate([a1, a0, a0, a1]))),
                      shape=(Bn, Bn))
    K = csr_matrix((np.concatenate([bs, -bs]),
                    (np.concatenate([np.arange(L), np.arange(L)]), np.concatenate([b0, b1]))),
                   shape=(L, Bn))
    slack = int(np.argmax(np.bincount(np.concatenate([a0, a1]), minlength=Bn)))
    keep = np.array([i for i in range(Bn) if i != slack])
    solve = factorized(Bbus[keep][:, keep].tocsc())
    Kred = K[:, keep].toarray().astype(np.float64)
    rhs = (bs[:, None] * Kred).T
    ptdfT = np.empty_like(rhs)
    for s in range(0, L, 256):
        e = min(s + 256, L)
        ptdfT[:, s:e] = np.column_stack([solve(rhs[:, j]) for j in range(s, e)])
    ptdf = np.zeros((L, Bn)); ptdf[:, keep] = ptdfT.T
    return ptdf, sub_lines, sub_buses

snom = n.lines["s_nom"]

print("\n=== OLD raw-ohm PTDF (production code) ===", flush=True)
ptdf, sl, sb = old_ptdf()
print(f"  max|PTDF|={np.abs(ptdf).max():.2f}", flush=True)
cols = np.array([bus_idx[b] for b in sb]); isub = inj[:, cols].copy()
isub -= isub.sum(axis=1, keepdims=True)/isub.shape[1]
flows = isub @ ptdf.T
sn = snom.loc[sl].values.astype(np.float64); sns = np.where(sn > 0, sn, np.inf)
load = np.abs(flows)/sns
for r, t in enumerate(SAMPLE_SNAPS):
    l = load[r]
    print(f"  snap {t}: max={l.max():7.1f}x  n>1={int((l>1).sum()):5d}  n>2={int((l>2).sum()):4d}  maxflow={np.abs(flows[r]).max():.0f}MW")

# ---------- PyPSA correct per-unit PTDF ----------
print("\n=== PyPSA per-unit PTDF (correct) ===", flush=True)
main.calculate_PTDF()
P = main.PTDF                       # (n_branch, n_bus)
sub_buses = list(main.buses_i())
cols = np.array([bus_idx[b] for b in sub_buses]); isub = inj[:, cols].copy()
isub -= isub.sum(axis=1, keepdims=True)/isub.shape[1]
print(f"  max|PTDF|={np.abs(P).max():.3f}", flush=True)
flows = isub @ P.T                  # (n_samp, n_branch)
# branch order in PTDF = lines then transformers (PyPSA passive_branches order)
br = main.branches()
br_snom = br["s_nom"].values.astype(np.float64)
is_line = np.asarray(br.index.get_level_values(0) == "Line")
sns = np.where(br_snom > 0, br_snom, np.inf)
load = np.abs(flows)/sns
for r, t in enumerate(SAMPLE_SNAPS):
    l = load[r]; ll = l[is_line]
    print(f"  snap {t}: max={l.max():7.1f}x  LINEmax={ll.max():6.1f}x  n>1={int((l>1).sum()):5d}  n>2={int((l>2).sum()):4d}  maxflow={np.abs(flows[r]).max():.0f}MW")

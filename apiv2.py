"""
apiv2.py — Multi-sector SISEPUEDE API v2 (port 8001)
Run: uvicorn apiv2:app --port 8001 --reload
"""
import warnings
warnings.filterwarnings("ignore")

import os, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

import numpy as np
import pandas as pd
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sisepuede"))

import sisepuede.transformers as trf
import sisepuede.transformers.transformers as trfs
from sisepuede.manager.sisepuede_examples import SISEPUEDEExamples

# Try to import individual sector models (none require Julia)
try:
    from sisepuede.models.afolu import AFOLU
    from sisepuede.models.circular_economy import CircularEconomy
    import sisepuede.models.energy_consumption as _mec
    from sisepuede.models.ippu import IPPU
    _SECTOR_MODELS_OK = True
except ImportError as _ie:
    print(f"[v2] Sector model imports unavailable ({_ie}). Non-transport → proxy fallback.")
    _SECTOR_MODELS_OK = False

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="SISEPUEDE Multi-Sector API v2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"], allow_headers=["*"],
)

# ── Sector config ─────────────────────────────────────────────────────────────
TRANSFORMER_PREFIX_TO_SECTOR = {
    "TRNS": "transport",   "TRDE": "transport",
    "AGRC": "agriculture", "FRST": "agriculture", "LNDU": "agriculture",
    "LSMM": "agriculture", "LVST": "agriculture", "SOIL": "agriculture",
    "WASO": "waste",       "WALI": "waste",       "TRWW": "waste",
    "INEN": "energy",      "SCOE": "energy",      "ENTC": "energy",
    "ENFU": "energy",      "FGTV": "energy",      "CCSQ": "energy",
    "IPPU": "industrial",  "PFLO": "cross_sector",
}

SECTOR_META = {
    "transport":    {"label": "Transport",                "icon": "🚛", "color": "#1e7093"},
    "energy":       {"label": "Energy & Buildings",       "icon": "⚡", "color": "#f59e0b"},
    "agriculture":  {"label": "Agriculture & Land Use",   "icon": "🌱", "color": "#10b981"},
    "waste":        {"label": "Waste & Circular Economy", "icon": "♻️",  "color": "#8b5cf6"},
    "industrial":   {"label": "Industrial Processes",     "icon": "🏭", "color": "#ef4444"},
    "cross_sector": {"label": "Cross-Sector",             "icon": "🔗", "color": "#64748b"},
}

# Substring patterns in SISEPUEDE output column names → sector
SECTOR_EMISSION_PREFIXES: Dict[str, List[str]] = {
    "agriculture": ["agrc", "lvst", "lsmm", "soil", "lndu", "pflo"],
    "waste":       ["waso", "wali", "trww"],
    "energy":      ["inen", "scoe", "entc", "fgtv"],
    "industrial":  ["ippu"],
}

SUBSECTOR_LABELS = {
    "agrc": "Crop Agriculture",      "lvst": "Livestock",
    "lsmm": "Manure Management",     "soil": "Soil Emissions",
    "lndu": "Land Use Change",       "pflo": "Forest & Other Land",
    "waso": "Solid Waste",           "wali": "Wastewater",
    "trww": "Industrial Wastewater", "inen": "Industrial Energy",
    "scoe": "Buildings & Combustion","entc": "Energy Technology",
    "fgtv": "Fugitive Emissions",    "ippu": "Industrial Processes",
}

SUBSECTOR_COLORS = {
    "agrc": "#84cc16", "lvst": "#f59e0b", "lsmm": "#f97316", "soil": "#a3a3a3",
    "lndu": "#22c55e", "pflo": "#16a34a", "waso": "#8b5cf6", "wali": "#a78bfa",
    "trww": "#c4b5fd", "inen": "#f59e0b", "scoe": "#fbbf24", "entc": "#fcd34d",
    "fgtv": "#78716c", "ippu": "#ef4444",
}

# GHG Protocol scope classification per non-transport sector.
# Keys must match SECTOR_EMISSION_PREFIXES entries.
# scope1 = direct combustion/process; scope2 = electricity/grid; scope3 = upstream/fugitive
SECTOR_SCOPE_MAP: Dict[str, Dict[str, List[str]]] = {
    "energy": {
        "scope1": ["inen", "scoe"],  # direct stationary combustion: industrial + buildings
        "scope2": ["entc"],          # electricity generation & grid supply
        "scope3": ["fgtv"],          # fugitive/upstream emissions from fuel extraction
    },
    "industrial": {
        "scope1": ["ippu"],  # direct industrial process emissions
        "scope2": [],        # electricity for processes captured in inen (energy sector)
        "scope3": [],        # upstream raw materials not modelled in SISEPUEDE
    },
}

POLICIES_DIR = Path(__file__).parent / "policies"

# ── Transport physics constants ───────────────────────────────────────────────
ED_TJ_PER_LITRE = {
    "diesel": 3.59e-5, "gasoline": 3.44e-5, "kerosene": 3.48e-5,
    "natural_gas": 3.87e-5, "biofuels": 2.94e-5,
    "hydrocarbon_gas_liquids": 2.53e-5, "hydrogen": 8.36e-6,
    "electricity": 3.60e-9, "ammonia": 1.58e-5,
}
_ED_FALLBACK = 3.50e-5

CO2_KG_PER_TJ = {
    "diesel": 74100.0, "gasoline": 69300.0, "kerosene": 71500.0,
    "natural_gas": 56100.0, "biofuels": 0.0,
    "hydrocarbon_gas_liquids": 63100.0, "hydrogen": 0.0,
    "electricity": 0.0, "ammonia": 0.0,
}

GRID_EF_KG_CO2_PER_KWH = {"costa_rica": 0.020, "mexico": 0.454}
_GRID_EF_FALLBACK = 0.400

UPSTREAM_MULTIPLIER = {
    "diesel": 0.20, "gasoline": 0.22, "kerosene": 0.18, "natural_gas": 0.15,
    "biofuels": 0.05, "hydrocarbon_gas_liquids": 0.18, "hydrogen": 0.30,
    "electricity": 0.05, "ammonia": 0.25,
}

# ── Transport emission helpers ────────────────────────────────────────────────
def _detect_transport_modes(df):
    modes = set()
    for col in df.columns:
        if col.startswith("elecfuelefficiency_trns_") and col.endswith("_km_per_kwh"):
            modes.add(col[len("elecfuelefficiency_trns_"):-len("_km_per_kwh")])
    return sorted(modes)

def _detect_transport_fuels(df, modes):
    fuels = {"electricity"}
    for col in df.columns:
        if col.startswith("fuelefficiency_trns_") and col.endswith("_km_per_litre"):
            after = col[len("fuelefficiency_trns_"):-len("_km_per_litre")]
            for mode in modes:
                if after.startswith(mode + "_"):
                    fuels.add(after[len(mode) + 1:])
                    break
    return sorted(fuels)

def _detect_available_gases(df):
    gases = set()
    for col in df.columns:
        if col.startswith("ef_trns_mobile_combustion_") and "_kg_" in col and "_per_tj_" in col:
            try:
                gases.add(col.split("_kg_")[1].split("_per_tj_")[0])
            except IndexError:
                pass
    return sorted(gases)

def _build_activity_proxies(df, modes):
    return {mode: np.ones(len(df)) * 100 for mode in modes}

def _build_efficiency_lookup(df, modes, fuels):
    n = len(df)
    lookup = {}
    for mode in modes:
        for fuel in fuels:
            if fuel == "electricity":
                col, default = f"elecfuelefficiency_trns_{mode}_km_per_kwh", 5.0
            else:
                col, default = f"fuelefficiency_trns_{mode}_{fuel}_km_per_litre", 3.0
            lookup[(mode, fuel)] = (
                np.maximum(df[col].fillna(default).values, 1e-9)
                if col in df.columns else np.full(n, default)
            )
    return lookup

def _build_gas_ef_lookup(df, gas, modes, fuels):
    n = len(df)
    return {
        (mode, fuel): (
            df[f"ef_trns_mobile_combustion_{mode}_kg_{gas}_per_tj_{fuel}"].fillna(0.0).values
            if f"ef_trns_mobile_combustion_{mode}_kg_{gas}_per_tj_{fuel}" in df.columns
            else np.zeros(n)
        )
        for mode in modes for fuel in fuels
    }

def _build_co2_ef_lookup(modes, fuels, n):
    return {(mode, fuel): np.full(n, float(CO2_KG_PER_TJ.get(fuel, 0.0)))
            for mode in modes for fuel in fuels}

def _scope1_by_mode(df, proxies, gas_ef_lookup, eff_lookup, modes, fuels):
    n = len(df); result = {}
    for mode in modes:
        proxy = proxies.get(mode, np.ones(n) * 100)
        em = np.zeros(n)
        for fuel in fuels:
            if fuel == "electricity":
                continue
            frac_col = f"frac_trns_fuelmix_{mode}_{fuel}"
            if frac_col not in df.columns:
                continue
            frac = df[frac_col].fillna(0.0).values
            eff  = eff_lookup.get((mode, fuel), np.full(n, 3.0))
            em  += frac * (proxy / np.maximum(eff, 1e-9)) * ED_TJ_PER_LITRE.get(fuel, _ED_FALLBACK) * gas_ef_lookup.get((mode, fuel), np.zeros(n))
        result[mode] = (em / 1e3).tolist()
    return result

def _scope2_by_mode(df, proxies, eff_lookup, modes, grid_ef):
    n = len(df); result = {}
    for mode in modes:
        proxy = proxies.get(mode, np.ones(n) * 100)
        frac_col = f"frac_trns_fuelmix_{mode}_electricity"
        if frac_col not in df.columns:
            result[mode] = [0.0] * n; continue
        frac = df[frac_col].fillna(0.0).values
        eff  = eff_lookup.get((mode, "electricity"), np.full(n, 5.0))
        result[mode] = (frac * (proxy / np.maximum(eff, 1e-9)) * grid_ef / 1e3).tolist()
    return result

def _scope3_by_mode(df, proxies, eff_lookup, modes, fuels):
    n = len(df); result = {}
    for mode in modes:
        proxy = proxies.get(mode, np.ones(n) * 100)
        em = np.zeros(n)
        for fuel in fuels:
            frac_col = f"frac_trns_fuelmix_{mode}_{fuel}"
            if frac_col not in df.columns:
                continue
            frac = df[frac_col].fillna(0.0).values
            eff  = eff_lookup.get((mode, fuel), np.full(n, 3.0))
            em  += frac * (proxy / np.maximum(eff, 1e-9)) * ED_TJ_PER_LITRE.get(fuel, _ED_FALLBACK) * CO2_KG_PER_TJ.get(fuel, 0.0) * UPSTREAM_MULTIPLIER.get(fuel, 0.15)
        result[mode] = (em / 1e3).tolist()
    return result

def _sum_modes(by_mode, n):
    arr = np.zeros(n)
    for v in by_mode.values():
        arr += np.array(v)
    return arr

def _sum_scope(em_cols: list, df_out, prefixes: list) -> list:
    """Sum all emission columns whose name contains any of the given subsector prefixes."""
    cols = [c for c in em_cols if any(p in c.lower() for p in prefixes)]
    if not cols:
        return [0.0] * len(df_out)
    return [round(float(x), 6) for x in df_out[cols].sum(axis=1).values]

# ── Baseline loading ──────────────────────────────────────────────────────────
MEXICO_CSV = Path(__file__).parent / "mexico_full_input.csv"

def _make_baseline(df, transformers):
    modes   = _detect_transport_modes(df)
    fuels   = _detect_transport_fuels(df, modes)
    n       = len(df)
    proxies = _build_activity_proxies(df, modes)
    eff_lk  = _build_efficiency_lookup(df, modes, fuels)
    sis_gases = _detect_available_gases(df)
    gas_ef  = {g: _build_gas_ef_lookup(df, g, modes, fuels) for g in sis_gases}
    gas_ef["co2"] = _build_co2_ef_lookup(modes, fuels, n)
    return {
        "df": df, "transformers": transformers, "modes": modes, "fuels": fuels,
        "available_gases": sorted({"co2"} | set(sis_gases)),
        "proxies": proxies, "eff_lookup": eff_lk, "gas_ef_lookups": gas_ef,
    }

print("[v2] Loading Costa Rica baseline…")
_examples = SISEPUEDEExamples()
_df_cr    = _examples("input_data_frame")
_trf_cr   = trfs.Transformers({}, df_input=_df_cr)

print("[v2] Loading Mexico baseline…")
_df_mx = pd.read_csv(MEXICO_CSV)
try:
    _trf_mx = trfs.Transformers({}, df_input=_df_mx)
except Exception as exc:
    print(f"  Mexico transformer init skipped: {exc.__class__.__name__}")
    _trf_mx = _trf_cr

BASELINES = {
    "costa_rica": _make_baseline(_df_cr, _trf_cr),
    "mexico":     _make_baseline(_df_mx, _trf_mx),
}

# ── Sector model initialization (Option A: true model.project() per sector) ───
SECTOR_MODELS: Dict[str, object]        = {}
SECTOR_BL_OUTPUTS: Dict[str, Dict]     = {}  # region → sector → {df_out, total, by_sub}
SECTOR_EM_COLS: Dict[str, List[str]]   = {}  # sector → [col_names]

if _SECTOR_MODELS_OK:
    _ma = _examples.model_attributes
    for _sname, _cls, _args in [
        ("agriculture", AFOLU,                  [_ma]),
        ("waste",       CircularEconomy,         [_ma]),
        ("energy",      _mec.EnergyConsumption,  [_ma]),
        ("industrial",  IPPU,                    [_ma]),
    ]:
        try:
            SECTOR_MODELS[_sname] = _cls(*_args)
            print(f"[v2] Initialized {_sname} model")
        except Exception as _e:
            print(f"[v2] {_sname} model init failed: {_e}")

    for _region, _bl in BASELINES.items():
        SECTOR_BL_OUTPUTS[_region] = {}
        for _sname, _model in SECTOR_MODELS.items():
            try:
                print(f"[v2] Computing {_sname} baseline for {_region}…")
                _df_out  = _model.project(_bl["df"])
                _pfxs    = SECTOR_EMISSION_PREFIXES.get(_sname, [])
                _em_cols = [
                    c for c in _df_out.columns
                    if "emission" in c.lower() and any(p in c.lower() for p in _pfxs)
                ]
                if _sname not in SECTOR_EM_COLS:
                    SECTOR_EM_COLS[_sname] = _em_cols
                    print(f"[v2]   {_sname}: {len(_em_cols)} emission cols")
                _total = (_df_out[_em_cols].sum(axis=1).values
                          if _em_cols else np.zeros(len(_df_out)))
                _by_sub = {}
                for _p in _pfxs:
                    _pcols = [c for c in _em_cols if _p in c.lower()]
                    if _pcols:
                        _by_sub[_p] = [round(float(x), 6) for x in _df_out[_pcols].sum(axis=1).values]
                _scope_map = SECTOR_SCOPE_MAP.get(_sname, {})
                _scopes = {
                    sk: _sum_scope(_em_cols, _df_out, spfxs)
                    for sk, spfxs in _scope_map.items()
                }
                # Fine-grained sub-category breakdown (IPPU industrial sub-types)
                _by_detail: dict = {}
                if _sname == "industrial":
                    _ippu_cols = [c for c in _em_cols if "ippu" in c.lower()]
                    _subcat: dict = {}
                    for _c in _ippu_cols:
                        _lc = _c.lower()
                        _pos = _lc.find("ippu_")
                        if _pos >= 0:
                            _token = _lc[_pos + 5:].split("_")[0]
                            if _token:
                                _subcat.setdefault(_token, []).append(_c)
                    for _token, _tcols in _subcat.items():
                        if _tcols:
                            _by_detail[_token] = [round(float(x), 6) for x in _df_out[_tcols].sum(axis=1).values]
                    if _by_detail:
                        print(f"[v2]   {_sname}/{_region}: IPPU sub-cats={sorted(_by_detail)}")
                SECTOR_BL_OUTPUTS[_region][_sname] = {
                    "df_out": _df_out,
                    "total":  [round(float(x), 6) for x in _total.tolist()],
                    "by_sub": _by_sub,
                    **({"by_detail": _by_detail} if _by_detail else {}),
                    **_scopes,
                }
                print(f"[v2]   {_sname}/{_region}: baseline peak={_total.max():.4f}"
                      + (f" scopes={list(_scopes)}" if _scopes else ""))
            except Exception as _e:
                print(f"[v2] {_sname}/{_region} baseline failed: {_e}")

# ── Transformer discovery ─────────────────────────────────────────────────────
def _get_all_transformer_codes() -> Dict[str, str]:
    out = {}
    for code in _trf_cr.all_transformers:
        parts = code.split(":")
        if len(parts) >= 2:
            out[code] = TRANSFORMER_PREFIX_TO_SECTOR.get(parts[1], "other")
    return out

ALL_TRANSFORMER_CODES: Dict[str, str] = _get_all_transformer_codes()

# ── YAML policy loading ───────────────────────────────────────────────────────
def _load_sector_policies(sector: str) -> List[dict]:
    fp = POLICIES_DIR / f"{sector}.yaml"
    if not fp.exists():
        return []
    with open(fp, "r", encoding="utf-8") as f:
        return yaml.safe_load(f).get("policies", [])

def _load_all_policies() -> Dict[str, List[dict]]:
    result = {}
    for yf in POLICIES_DIR.glob("*.yaml"):
        with open(yf, "r", encoding="utf-8") as f:
            result[yf.stem] = yaml.safe_load(f).get("policies", [])
    return result

ALL_POLICIES: Dict[str, List[dict]] = _load_all_policies()

# ── Policy config builder ─────────────────────────────────────────────────────
def _build_policy_config(policy: dict) -> dict:
    params = dict(policy.get("parameters", {}))
    vir = params.get("vec_implementation_ramp")
    if isinstance(vir, dict):
        wl = vir.get("window_logistic", [-8, 8])
        if isinstance(wl, list):
            vir["window_logistic"] = tuple(wl)
        params["vec_implementation_ramp"] = vir
    return {
        "identifiers": {
            "transformation_code": f"TX:V2:{policy['id'].upper()}",
            "transformation_name": policy["name"],
        },
        "transformer": policy["transformer"],
        "parameters":  params,
    }

def _make_years(n: int) -> List[int]:
    return [2015 + round(i * 35 / max(n - 1, 1)) for i in range(n)]

# ── Transport abatement (exact physics, optional detailed scope breakdown) ────
def _compute_transport_abatement(region: str, gas: str, policy_cfg: dict,
                                  detailed: bool = False) -> dict:
    bl         = BASELINES.get(region, BASELINES["costa_rica"])
    df_input   = bl["df"]
    modes      = bl["modes"]; fuels = bl["fuels"]
    proxies    = bl["proxies"]; eff_lk = bl["eff_lookup"]
    gas_ef     = bl["gas_ef_lookups"].get(gas, bl["gas_ef_lookups"]["co2"])
    grid_ef    = GRID_EF_KG_CO2_PER_KWH.get(region, _GRID_EF_FALLBACK)
    n          = len(df_input)

    frozen_gas_ef  = {k: np.full(n, float(v[0])) for k, v in gas_ef.items()}
    df_result      = trf.Transformation(policy_cfg, bl["transformers"])()
    eff_lk_policy  = _build_efficiency_lookup(df_result, modes, fuels)

    bl_s1 = _scope1_by_mode(df_input,  proxies, frozen_gas_ef, eff_lk,        modes, fuels)
    bl_s2 = _scope2_by_mode(df_input,  proxies, eff_lk,        modes, grid_ef)
    bl_s3 = _scope3_by_mode(df_input,  proxies, eff_lk,        modes, fuels)
    po_s1 = _scope1_by_mode(df_result, proxies, gas_ef,        eff_lk_policy, modes, fuels)
    po_s2 = _scope2_by_mode(df_result, proxies, eff_lk_policy, modes, grid_ef)
    po_s3 = _scope3_by_mode(df_result, proxies, eff_lk_policy, modes, fuels)

    bl_arr = _sum_modes(bl_s1, n) + _sum_modes(bl_s2, n) + _sum_modes(bl_s3, n)
    po_arr = _sum_modes(po_s1, n) + _sum_modes(po_s2, n) + _sum_modes(po_s3, n)
    abatement = bl_arr - po_arr
    years     = _make_years(n)
    final_bl  = float(bl_arr[-1]); final_ab = float(abatement[-1])
    pct       = round(max(-100.0, min(100.0, 100.0 * final_ab / final_bl)), 1) if final_bl > 0.01 else 0.0

    out = {
        "years":                  years,
        "baseline":               [round(float(x), 6) for x in bl_arr],
        "policy":                 [round(float(x), 6) for x in po_arr],
        "abatement":              [round(float(x), 6) for x in abatement],
        "final_abatement_tonnes": round(final_ab, 4),
        "final_baseline_tonnes":  round(final_bl, 4),
        "final_reduction_pct":    pct,
        "emission_type":          "exact",
    }
    if detailed:
        out["scope_breakdown"] = {
            "baseline": {
                "scope1": [round(float(x), 6) for x in _sum_modes(bl_s1, n)],
                "scope2": [round(float(x), 6) for x in _sum_modes(bl_s2, n)],
                "scope3": [round(float(x), 6) for x in _sum_modes(bl_s3, n)],
            },
            "policy": {
                "scope1": [round(float(x), 6) for x in _sum_modes(po_s1, n)],
                "scope2": [round(float(x), 6) for x in _sum_modes(po_s2, n)],
                "scope3": [round(float(x), 6) for x in _sum_modes(po_s3, n)],
            },
        }
        out["by_mode"] = {
            "scope1": {m: {"baseline": bl_s1[m], "policy": po_s1[m]} for m in modes},
            "scope2": {m: {"baseline": bl_s2[m], "policy": po_s2[m]} for m in modes},
            "scope3": {m: {"baseline": bl_s3[m], "policy": po_s3[m]} for m in modes},
        }
    return out

# ── True sector abatement (SISEPUEDE sector model.project()) ──────────────────
def _compute_true_sector_abatement(region: str, sector: str, policy_cfg: dict,
                                    detailed: bool = False) -> dict:
    bl_info  = SECTOR_BL_OUTPUTS.get(region, {}).get(sector)
    em_cols  = SECTOR_EM_COLS.get(sector, [])
    model    = SECTOR_MODELS.get(sector)

    if not model or not bl_info or not em_cols:
        return _compute_generic_abatement(region, policy_cfg)

    bl   = BASELINES.get(region, BASELINES["costa_rica"])
    n    = len(bl["df"])
    years = _make_years(n)

    df_policy = trf.Transformation(policy_cfg, bl["transformers"])()
    try:
        df_out_policy = model.project(df_policy)
    except Exception as e:
        print(f"[v2] {sector} model.project failed ({e}), using proxy fallback")
        return _compute_generic_abatement(region, policy_cfg)

    df_out_bl = bl_info["df_out"]
    bl_arr    = df_out_bl[em_cols].sum(axis=1).values.astype(float)

    # Filter to only columns present in policy output too
    shared_cols = [c for c in em_cols if c in df_out_policy.columns]
    po_arr      = df_out_policy[shared_cols].sum(axis=1).values.astype(float) if shared_cols else bl_arr.copy()

    abatement = bl_arr - po_arr
    final_bl  = float(bl_arr[-1]); final_ab = float(abatement[-1])
    pct       = round(max(-100.0, min(100.0, 100.0 * final_ab / final_bl)), 1) if final_bl > 0.01 else 0.0

    out = {
        "years":                  years,
        "baseline":               [round(float(x), 6) for x in bl_arr],
        "policy":                 [round(float(x), 6) for x in po_arr],
        "abatement":              [round(float(x), 6) for x in abatement],
        "final_abatement_tonnes": round(final_ab, 4),
        "final_baseline_tonnes":  round(final_bl, 4),
        "final_reduction_pct":    pct,
        "emission_type":          "exact",
    }
    if detailed:
        prefixes = SECTOR_EMISSION_PREFIXES.get(sector, [])
        categories = []
        for p in prefixes:
            p_cols = [c for c in em_cols if p in c.lower()]
            if not p_cols:
                continue
            bl_sub = df_out_bl[p_cols].sum(axis=1).values.astype(float)
            p_cols_po = [c for c in p_cols if c in df_out_policy.columns]
            po_sub = df_out_policy[p_cols_po].sum(axis=1).values.astype(float) if p_cols_po else bl_sub.copy()
            if abs(bl_sub).max() < 1e-9:
                continue
            categories.append({
                "key":      p,
                "name":     SUBSECTOR_LABELS.get(p, p.upper()),
                "color":    SUBSECTOR_COLORS.get(p, "#64748b"),
                "baseline": [round(float(x), 6) for x in bl_sub],
                "policy":   [round(float(x), 6) for x in po_sub],
            })
        out["categories"] = sorted(categories,
            key=lambda c: abs(np.array(c["baseline"]).sum()), reverse=True)
    return out

# ── Generic proxy fallback ────────────────────────────────────────────────────
def _compute_generic_abatement(region: str, policy_cfg: dict) -> dict:
    bl          = BASELINES.get(region, BASELINES["costa_rica"])
    df_input    = bl["df"]
    n           = len(df_input)
    years       = _make_years(n)
    df_result   = trf.Transformation(policy_cfg, bl["transformers"])()
    changed = []
    for col in df_input.columns:
        if col not in df_result.columns or not np.issubdtype(df_input[col].dtype, np.number):
            continue
        bl_v = df_input[col].fillna(0.0).values.astype(float)
        po_v = df_result[col].fillna(0.0).values.astype(float)
        if np.allclose(bl_v, po_v, atol=1e-10, rtol=1e-8):
            continue
        bl_m = float(np.mean(bl_v)); po_m = float(np.mean(po_v))
        changed.append({"col": col, "bl_mean": round(bl_m, 6), "po_mean": round(po_m, 6),
                        "pct_change": round((po_m - bl_m) / max(abs(bl_m), 1e-9) * 100, 1)})
    if changed:
        avg_abs = float(np.mean([abs(c["pct_change"]) for c in changed]))
        neg     = [c["pct_change"] for c in changed if c["pct_change"] < 0]
        impact  = min(avg_abs, 100.0) * (-1 if neg else 1)
    else:
        impact = 0.0
    ramp = np.linspace(0, impact, n)
    return {
        "years":                  years,
        "baseline":               [100.0] * n,
        "policy":                 [round(float(100 - x), 4) for x in ramp],
        "abatement":              [round(float(x), 4) for x in ramp],
        "final_abatement_tonnes": round(impact, 2),
        "final_baseline_tonnes":  100.0,
        "final_reduction_pct":    round(impact, 1),
        "emission_type":          "proxy",
        "changed_params":         changed[:20],
    }

# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/v2/sectors")
async def list_sectors():
    counts: Dict[str, int] = {}
    for _, sector in ALL_TRANSFORMER_CODES.items():
        counts[sector] = counts.get(sector, 0) + 1
    return {"sectors": [
        {
            "sector":            s,
            "label":             m["label"],
            "icon":              m["icon"],
            "color":             m["color"],
            "transformer_count": counts.get(s, 0),
            "policy_count":      len(ALL_POLICIES.get(s, [])),
            "has_yaml":          bool(ALL_POLICIES.get(s)),
            "has_model":         s == "transport" or s in SECTOR_MODELS,
        }
        for s, m in SECTOR_META.items()
    ]}


@app.get("/v2/sectors/{sector}/policies")
async def list_sector_policies(sector: str):
    policies = _load_sector_policies(sector)
    if not policies:
        raise HTTPException(404, f"No policies for sector '{sector}'")
    meta = SECTOR_META.get(sector, {"label": sector, "color": "#64748b"})
    return {
        "sector":   sector,
        "label":    meta["label"],
        "color":    meta["color"],
        "policies": [{k: v for k, v in p.items() if k != "parameters"} for p in policies],
    }


class BatchRequest(BaseModel):
    region: str = "costa_rica"
    gas:    str = "co2"


@app.post("/v2/sectors/{sector}/run-batch")
async def run_sector_batch(sector: str, request: BatchRequest):
    policies = _load_sector_policies(sector)
    if not policies:
        raise HTTPException(404, f"No policies for sector '{sector}'")
    bl = BASELINES.get(request.region, BASELINES["costa_rica"])
    if request.gas not in bl["available_gases"]:
        request.gas = "co2"
    is_transport  = (sector == "transport")
    has_true_model = (sector in SECTOR_MODELS and
                      request.region in SECTOR_BL_OUTPUTS and
                      sector in SECTOR_BL_OUTPUTS.get(request.region, {}))

    def run_one(policy: dict):
        pid = policy["id"]
        try:
            cfg = _build_policy_config(policy)
            if is_transport:
                out = _compute_transport_abatement(request.region, request.gas, cfg)
            elif has_true_model:
                out = _compute_true_sector_abatement(request.region, sector, cfg)
            else:
                out = _compute_generic_abatement(request.region, cfg)
            out.update({
                "name":           policy["name"],
                "category":       policy.get("category", ""),
                "capex_per_tco2": policy.get("capex_per_tco2", 0),
                "opex_per_tco2":  policy.get("opex_per_tco2", 0),
                "description":    policy.get("description", ""),
            })
            return pid, out
        except Exception as exc:
            return pid, {"error": str(exc)}

    results: dict = {}; errors: dict = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for pid, out in (f.result() for f in as_completed(
                {pool.submit(run_one, p): p["id"] for p in policies})):
            (errors if "error" in out else results)[pid] = out

    emit = "exact" if (is_transport or has_true_model) else "proxy"
    return {
        "sector": sector, "region": request.region, "gas": request.gas,
        "years": _make_years(len(bl["df"])),
        "emission_type": emit, "results": results, "errors": errors,
    }


class PolicyRequest(BaseModel):
    policy_id: str
    region:    str = "costa_rica"
    gas:       str = "co2"


@app.post("/v2/sectors/{sector}/run-policy")
async def run_sector_policy(sector: str, request: PolicyRequest):
    """Run a SINGLE named policy; returns full simulation result for the simulation tab."""
    policies = _load_sector_policies(sector)
    policy   = next((p for p in policies if p["id"] == request.policy_id), None)
    if not policy:
        raise HTTPException(404, f"Policy '{request.policy_id}' not found in sector '{sector}'")
    bl = BASELINES.get(request.region, BASELINES["costa_rica"])
    if request.gas not in bl["available_gases"]:
        request.gas = "co2"
    cfg = _build_policy_config(policy)

    if sector == "transport":
        result = _compute_transport_abatement(request.region, request.gas, cfg, detailed=True)
    elif (sector in SECTOR_MODELS and
          request.region in SECTOR_BL_OUTPUTS and
          sector in SECTOR_BL_OUTPUTS.get(request.region, {})):
        result = _compute_true_sector_abatement(request.region, sector, cfg, detailed=True)
    else:
        result = _compute_generic_abatement(request.region, cfg)

    result["policy_id"]              = request.policy_id
    result["policy_name"]            = policy["name"]
    result["sector"]                 = sector
    result["baseline_total"]         = result.get("baseline", [])
    result["policy_total"]           = result.get("policy", [])
    result["final_reduction_tonnes"] = result.get("final_abatement_tonnes", 0)
    return result


@app.get("/v2/sectors/{sector}/baseline")
async def get_sector_baseline(sector: str, region: str = "costa_rica", gas: str = "co2"):
    """Return sector baseline emission time series."""
    bl    = BASELINES.get(region, BASELINES["costa_rica"])
    n     = len(bl["df"])
    years = _make_years(n)

    if sector == "transport":
        modes   = bl["modes"]; fuels = bl["fuels"]
        proxies = bl["proxies"]; eff_lk = bl["eff_lookup"]
        gas_ef  = bl["gas_ef_lookups"].get(gas, bl["gas_ef_lookups"]["co2"])
        grid_ef = GRID_EF_KG_CO2_PER_KWH.get(region, _GRID_EF_FALLBACK)
        frozen  = {k: np.full(n, float(v[0])) for k, v in gas_ef.items()}
        df_in   = bl["df"]
        s1 = _scope1_by_mode(df_in, proxies, frozen, eff_lk, modes, fuels)
        s2 = _scope2_by_mode(df_in, proxies, eff_lk, modes, grid_ef)
        s3 = _scope3_by_mode(df_in, proxies, eff_lk, modes, fuels)
        total = _sum_modes(s1, n) + _sum_modes(s2, n) + _sum_modes(s3, n)
        return {
            "sector": sector, "region": region, "years": years,
            "emission_type": "exact",
            "total":  [round(float(x), 6) for x in total],
            "scope1": [round(float(x), 6) for x in _sum_modes(s1, n)],
            "scope2": [round(float(x), 6) for x in _sum_modes(s2, n)],
            "scope3": [round(float(x), 6) for x in _sum_modes(s3, n)],
            "by_mode": {"scope1": s1, "scope2": s2, "scope3": s3},
        }

    bl_info = SECTOR_BL_OUTPUTS.get(region, {}).get(sector)
    if bl_info:
        resp = {
            "sector": sector, "region": region, "years": years,
            "emission_type": "exact",
            "total":  bl_info["total"],
            "by_sub": bl_info["by_sub"],
        }
        for sk in ("scope1", "scope2", "scope3"):
            if sk in bl_info:
                resp[sk] = bl_info[sk]
        if "by_detail" in bl_info:
            resp["by_detail"] = bl_info["by_detail"]
        return resp
    raise HTTPException(503, f"Baseline not yet available for '{sector}' / '{region}'")


@app.get("/v2/net-zero/policies")
async def get_net_zero_policies():
    """All policies from ALL sectors for the Net Zero Plan tab."""
    all_pols = []
    for sector, policies in ALL_POLICIES.items():
        meta = SECTOR_META.get(sector, {"label": sector, "color": "#64748b"})
        for p in policies:
            all_pols.append({
                "id":                     p["id"],
                "name":                   p["name"],
                "sector":                 sector,
                "sector_label":           meta["label"],
                "sector_color":           meta["color"],
                "category":               p.get("category", ""),
                "description":            p.get("description", ""),
                "default_capex_per_tco2": p.get("capex_per_tco2", 0),
                "default_opex_per_tco2":  p.get("opex_per_tco2", 0),
            })
    return {"policies": all_pols, "total": len(all_pols)}


@app.post("/v2/net-zero/run-batch")
async def run_net_zero_batch(request: BatchRequest):
    """Run ALL policies across ALL sectors for the Net Zero Plan tab."""
    all_tasks = [(s, p) for s, pols in ALL_POLICIES.items() for p in pols]
    bl_n = len(BASELINES["costa_rica"]["df"])
    years = _make_years(bl_n)

    def run_one(sector, policy):
        pid = f"{sector}::{policy['id']}"
        try:
            bl  = BASELINES.get(request.region, BASELINES["costa_rica"])
            gas = request.gas if request.gas in bl["available_gases"] else "co2"
            cfg = _build_policy_config(policy)
            if sector == "transport":
                out = _compute_transport_abatement(request.region, gas, cfg)
            elif (sector in SECTOR_MODELS and
                  request.region in SECTOR_BL_OUTPUTS and
                  sector in SECTOR_BL_OUTPUTS.get(request.region, {})):
                out = _compute_true_sector_abatement(request.region, sector, cfg)
            else:
                out = _compute_generic_abatement(request.region, cfg)
            meta = SECTOR_META.get(sector, {})
            return pid, {
                "sector":                 sector,
                "sector_label":           meta.get("label", sector),
                "sector_color":           meta.get("color", "#64748b"),
                "name":                   policy["name"],
                "category":               policy.get("category", ""),
                "baseline":               out.get("baseline", []),
                "abatement":              out.get("abatement", []),
                "policy":                 out.get("policy", []),
                "final_abatement_tonnes": out.get("final_abatement_tonnes", 0),
                "final_reduction_pct":    out.get("final_reduction_pct", 0),
                "emission_type":          out.get("emission_type", "proxy"),
                "capex_per_tco2":         policy.get("capex_per_tco2", 0),
                "opex_per_tco2":          policy.get("opex_per_tco2", 0),
            }
        except Exception as e:
            return pid, {"error": str(e)}

    results: dict = {}; errors: dict = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for pid, out in (f.result() for f in as_completed(
                {pool.submit(run_one, s, p): f"{s}::{p['id']}" for s, p in all_tasks})):
            (errors if "error" in out else results)[pid] = out

    return {"region": request.region, "gas": request.gas,
            "years": years, "results": results, "errors": errors}


@app.get("/v2/transformers")
async def list_all_transformers():
    grouped: Dict[str, List[str]] = {}
    for code, sector in ALL_TRANSFORMER_CODES.items():
        grouped.setdefault(sector, []).append(code)
    return {"transformers_by_sector": grouped, "total": len(ALL_TRANSFORMER_CODES)}


@app.get("/v2/health")
async def health():
    return {
        "status": "ok", "version": "2",
        "sectors": list(SECTOR_META.keys()),
        "regions": list(BASELINES.keys()),
        "sector_models_active": list(SECTOR_MODELS.keys()),
        "transformer_count": len(ALL_TRANSFORMER_CODES),
        "policy_files": [f.stem for f in POLICIES_DIR.glob("*.yaml")],
    }

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
    "transport":   ["trns"],   # extracted from energy_consumption df_out
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

# ── Fine-grained category labels & colours (one level below subsector) ─────────
DETAIL_LABELS: Dict[str, str] = {
    # agrc – crop types
    "rice": "Rice", "cereals": "Cereals", "fruits": "Fruits",
    "vegetables_and_vines": "Vegetables & Vines", "sugar_cane": "Sugar Cane",
    "nuts": "Nuts", "pulses": "Pulses", "fibers": "Fibers",
    "tubers": "Tubers", "other_annual": "Other Annual",
    "other_woody_perennial": "Other Woody Perennial",
    "herbs_and_other_perennial_crops": "Herbs & Perennials",
    "bevs_and_spices": "Beverages & Spices",
    # lvst – livestock
    "buffalo": "Buffalo", "cattle_dairy": "Dairy Cattle",
    "cattle_nondairy": "Beef Cattle", "chickens": "Chickens",
    "goats": "Goats", "horses": "Horses", "mules": "Mules",
    "pigs": "Pigs", "sheep": "Sheep",
    # lsmm – manure management systems
    "anaerobic_digester": "Anaerobic Digester", "lagoon": "Lagoon",
    "composting": "Composting", "dry_lot": "Dry Lot",
    "deep_bedding": "Deep Bedding", "biodigester": "Biodigester",
    "solid_storage": "Solid Storage", "pasture": "Pasture Range",
    "daily_spread": "Daily Spread",
    # soil
    "synthetic_fertilizer": "Synthetic Fertilizer",
    "organic_amendments": "Organic Amendments",
    "rice_fields": "Rice Fields", "liming": "Liming", "urea": "Urea",
    # waso – solid waste streams
    "food": "Food Waste", "paper": "Paper", "plastic": "Plastics",
    "glass": "Glass", "metal": "Metal", "nappies": "Nappies",
    "textiles": "Textiles", "wood": "Wood",
    "rubber_leather": "Rubber & Leather",
    "chemical_industrial": "Chemical/Industrial",
    "sludge": "Sludge", "yard": "Yard Waste",
    # wali / trww – wastewater
    "domestic_rural": "Rural Domestic", "domestic_urban": "Urban Domestic",
    "industrial": "Industrial Effluent",
    # lndu – land use categories
    "croplands": "Croplands", "forests": "Forests",
    "grasslands": "Grasslands", "wetlands": "Wetlands",
    "settlements": "Settlements",
    # ippu – industrial process sub-categories
    "cement": "Cement & Lime", "chemicals": "Chemical Industry",
    "metals": "Metal Production", "electronics": "Electronics",
    "hfcs": "HFC Refrigerants", "pfcs": "PFC Gases",
    "n2o": "N₂O Processes", "sf6": "SF₆ Equipment",
}

DETAIL_COLORS: Dict[str, str] = {
    # agrc crops – greens/yellows
    "rice": "#84cc16", "cereals": "#a3e635", "fruits": "#f97316",
    "vegetables_and_vines": "#22c55e", "sugar_cane": "#fbbf24",
    "nuts": "#d97706", "pulses": "#65a30d", "fibers": "#15803d",
    "tubers": "#ca8a04", "other_annual": "#4ade80",
    "other_woody_perennial": "#166534",
    "herbs_and_other_perennial_crops": "#86efac",
    "bevs_and_spices": "#fde68a",
    # lvst – earthy tones
    "buffalo": "#78350f", "cattle_dairy": "#f59e0b",
    "cattle_nondairy": "#b45309", "chickens": "#fcd34d",
    "goats": "#d97706", "horses": "#92400e", "mules": "#a16207",
    "pigs": "#fb923c", "sheep": "#fef08a",
    # lsmm
    "anaerobic_digester": "#06b6d4", "lagoon": "#0891b2",
    "composting": "#65a30d", "dry_lot": "#a3a3a3",
    "deep_bedding": "#78716c", "biodigester": "#22d3ee",
    "solid_storage": "#94a3b8", "pasture": "#4ade80",
    "daily_spread": "#86efac",
    # soil
    "synthetic_fertilizer": "#f43f5e", "organic_amendments": "#fb923c",
    "rice_fields": "#84cc16", "liming": "#cbd5e1", "urea": "#fca5a5",
    # waso
    "food": "#8b5cf6", "paper": "#a78bfa", "plastic": "#6d28d9",
    "glass": "#c4b5fd", "metal": "#94a3b8", "nappies": "#f9a8d4",
    "textiles": "#ec4899", "wood": "#92400e",
    "rubber_leather": "#78350f", "chemical_industrial": "#ef4444",
    "sludge": "#64748b", "yard": "#22c55e",
    # wali / trww
    "domestic_rural": "#818cf8", "domestic_urban": "#6366f1",
    "industrial": "#4338ca",
    # lndu
    "croplands": "#fbbf24", "forests": "#16a34a",
    "grasslands": "#84cc16", "wetlands": "#0ea5e9",
    "settlements": "#94a3b8",
    # ippu
    "cement": "#f97316", "chemicals": "#3b82f6",
    "metals": "#94a3b8", "electronics": "#8b5cf6",
    "hfcs": "#ec4899", "pfcs": "#06b6d4",
    "sf6": "#fbbf24",
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
    "agriculture": {
        "scope1": ["agrc", "lvst", "lsmm", "soil"],  # direct field/livestock/manure/soil
        "scope2": [],                                  # no grid electricity in AFOLU model
        "scope3": ["lndu", "pflo"],                   # land-use change & forestry
    },
    "waste": {
        "scope1": ["waso", "wali"],  # solid waste disposal & liquid wastewater direct
        "scope2": [],
        "scope3": ["trww"],          # industrial wastewater (upstream process)
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

# IPCC AR5 CO2 factors (kg/TJ). Used ONLY for CO2 in Transport.
CO2_KG_PER_TJ = {
    "diesel": 74100.0, "gasoline": 69300.0, "kerosene": 71500.0,
    "natural_gas": 56100.0, "biofuels": 0.0,
    "hydrocarbon_gas_liquids": 63100.0, "hydrogen": 0.0,
    "electricity": 0.0, "ammonia": 0.0,
}

GRID_EF_KG_CO2_PER_KWH = {"costa_rica": 0.020, "mexico": 0.454, "uganda": 0.091}
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
    """Reads CH4/N2O EFs from SISEPUEDE columns."""
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
    """Generates CO2 EFs from Physics Constants."""
    return {(mode, fuel): np.full(n, float(CO2_KG_PER_TJ.get(fuel, 0.0)))
            for mode in modes for fuel in fuels}

def _scope1_by_mode(df, proxies, gas_ef_lookup, eff_lookup, modes, fuels, gas_type="co2"):
    """
    Calculates Scope 1.
    If gas_type is 'co2', gas_ef_lookup should be constants.
    If gas_type is 'ch4'/'n2o', gas_ef_lookup should be from DF columns.
    """
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
            # Use the provided lookup (either constant or column-based)
            ef_val = gas_ef_lookup.get((mode, fuel), np.zeros(n))
            em  += frac * (proxy / np.maximum(eff, 1e-9)) * ED_TJ_PER_LITRE.get(fuel, _ED_FALLBACK) * ef_val
        result[mode] = (em / 1e3).tolist()
    return result

def _scope2_by_mode(df, proxies, eff_lookup, modes, grid_ef, gas_type="co2"):
    """Scope 2 is typically only CO2 (Grid Electricity)."""
    n = len(df); result = {}
    if gas_type != "co2":
        return {m: [0.0] * n for m in modes}
        
    for mode in modes:
        proxy = proxies.get(mode, np.ones(n) * 100)
        frac_col = f"frac_trns_fuelmix_{mode}_electricity"
        if frac_col not in df.columns:
            result[mode] = [0.0] * n; continue
        frac = df[frac_col].fillna(0.0).values
        eff  = eff_lookup.get((mode, "electricity"), np.full(n, 5.0))
        result[mode] = (frac * (proxy / np.maximum(eff, 1e-9)) * grid_ef / 1e3).tolist()
    return result

def _scope3_by_mode(df, proxies, eff_lookup, modes, fuels, gas_type="co2"):
    """Scope 3 is typically only CO2 (Upstream Fuel Chain)."""
    n = len(df); result = {}
    if gas_type != "co2":
        return {m: [0.0] * n for m in modes}

    for mode in modes:
        proxy = proxies.get(mode, np.ones(n) * 100)
        em = np.zeros(n)
        for fuel in fuels:
            frac_col = f"frac_trns_fuelmix_{mode}_{fuel}"
            if frac_col not in df.columns:
                continue
            frac = df[frac_col].fillna(0.0).values
            eff  = eff_lookup.get((mode, fuel), np.full(n, 3.0))
            # Upstream is proportional to CO2 content
            co2_ef = CO2_KG_PER_TJ.get(fuel, 0.0)
            em  += frac * (proxy / np.maximum(eff, 1e-9)) * ED_TJ_PER_LITRE.get(fuel, _ED_FALLBACK) * co2_ef * UPSTREAM_MULTIPLIER.get(fuel, 0.15)
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

# ── Activity-based proportion fallback ───────────────────────────────────────
# Maps subsector prefix → input column prefix that encodes per-category activity
_ACTIVITY_COL_MAP: Dict[str, str] = {
    "agrc": "n_agrc_",
    "lvst": "pop_lvst_",
    "lsmm": "frac_lsmm_",
    "soil": "frac_soil_",
    "waso": "frac_waso_",
    "wali": "frac_wali_",
    "trww": "frac_trww_",
    "lndu": "area_lndu_",
    "inen": "frac_inen_",
}
_ACTIVITY_SKIP = frozenset({"scalar", "initial", "total", "minimum", "maximum",
                             "average", "default", "dummy", "none"})

# Explicit category → input column for subsectors where no simple prefix works
_EXPLICIT_CAT_MAP: Dict[str, Dict[str, str]] = {
    "scoe": {
        "residential":          "consumpinit_scoe_gj_per_hh_residential_heat_energy",
        "commercial_municipal": "consumpinit_scoe_tj_per_mmmgdp_commercial_municipal_heat_energy",
        "other_se":             "consumpinit_scoe_tj_per_mmmgdp_other_se_heat_energy",
    },
    "entc": {
        "pp_coal":       "nemomod_entc_residual_capacity_pp_coal_gw",
        "pp_gas":        "nemomod_entc_residual_capacity_pp_gas_gw",
        "pp_hydropower": "nemomod_entc_residual_capacity_pp_hydropower_gw",
        "pp_solar":      "nemomod_entc_residual_capacity_pp_solar_gw",
        "pp_wind":       "nemomod_entc_residual_capacity_pp_wind_gw",
        "pp_nuclear":    "nemomod_entc_residual_capacity_pp_nuclear_gw",
        "pp_oil":        "nemomod_entc_residual_capacity_pp_oil_gw",
        "pp_biomass":    "nemomod_entc_residual_capacity_pp_biomass_gw",
        "pp_geothermal": "nemomod_entc_residual_capacity_pp_geothermal_gw",
        "pp_biogas":     "nemomod_entc_residual_capacity_pp_biogas_gw",
    },
    # fgtv: use domestic fraction (1 - import_frac) as proxy for local production activity
    "fgtv": {
        "fuel_coal":        "frac_enfu_fuel_demand_imported_pj_fuel_coal",
        "fuel_crude":       "frac_enfu_fuel_demand_imported_pj_fuel_crude",
        "fuel_natural_gas": "frac_enfu_fuel_demand_imported_pj_fuel_natural_gas",
        "fuel_oil":         "frac_enfu_fuel_demand_imported_pj_fuel_oil",
    },
}
# fgtv proxy is import fraction → invert to get domestic production proxy
_INVERT_PROXY = frozenset({"fgtv"})


def _activity_based_detail(df_input, subsector_prefix: str, total_series: list) -> dict:
    """Approximate per-category breakdown from input activity proportions."""
    n = len(total_series)
    total_arr = np.array(total_series, dtype=float)

    # ── Explicit column map ──────────────────────────────────────────────────
    if subsector_prefix in _EXPLICIT_CAT_MAP:
        cats: dict = {}
        invert = subsector_prefix in _INVERT_PROXY
        for cat, col in _EXPLICIT_CAT_MAP[subsector_prefix].items():
            if col not in df_input.columns:
                continue
            vals = df_input[col].fillna(0.0).values.astype(float)
            if invert:
                vals = np.clip(1.0 - vals, 0.0, None)
            if np.any(vals > 0):
                cats[cat] = vals
        if len(cats) < 2:
            return {}
        result: dict = {}
        for yr_i in range(n):
            yr_act = {c: max(0.0, float(v[yr_i])) for c, v in cats.items()}
            yr_total = sum(yr_act.values())
            for cat in cats:
                frac = yr_act[cat] / yr_total if yr_total > 0 else 1.0 / len(cats)
                result.setdefault(cat, []).append(round(float(total_arr[yr_i]) * frac, 6))
        return result if len(result) >= 2 else {}

    # ── Prefix scan fallback ─────────────────────────────────────────────────
    col_prefix = _ACTIVITY_COL_MAP.get(subsector_prefix)
    if not col_prefix:
        return {}
    cats = {}
    for c in df_input.columns:
        if not c.lower().startswith(col_prefix):
            continue
        cat = c[len(col_prefix):]
        if not cat or any(s in cat.lower() for s in _ACTIVITY_SKIP):
            continue
        vals = df_input[c].fillna(0.0).values.astype(float)
        if np.any(vals > 0):
            cats[cat] = vals
    if len(cats) < 2:
        return {}
    result = {}
    for yr_i in range(n):
        yr_act = {cat: max(0.0, float(v[yr_i])) for cat, v in cats.items()}
        yr_total_act = sum(yr_act.values())
        for cat in cats:
            frac = yr_act[cat] / yr_total_act if yr_total_act > 0 else 1.0 / len(cats)
            result.setdefault(cat, []).append(round(float(total_arr[yr_i]) * frac, 6))
    return result if len(result) >= 2 else {}


_GAS_SFXS = frozenset({
    "co2", "ch4", "n2o", "co2e", "sf6", "hfc", "pfc", "hfcs", "pfcs",
    "co2_equivalent", "gwp", "kg", "tonne", "mt", "emission",
})

def _extract_by_detail(em_cols: list, df_out, subsector_prefix: str) -> dict:
    """Extract one time-series per fine-grained category within a subsector."""
    pfx = f"{subsector_prefix}_"
    subcat: dict = {}
    for c in em_cols:
        lc = c.lower()
        pos = lc.find(pfx)
        if pos < 0:
            continue
        parts = lc[pos + len(pfx):].split("_")
        cat_parts: list = []
        for p in parts:
            if p in _GAS_SFXS or p.isdigit():
                break
            cat_parts.append(p)
        if not cat_parts:
            continue
        token = "_".join(cat_parts)
        subcat.setdefault(token, []).append(c)
    result = {
        tok: [round(float(x), 6) for x in df_out[cols].sum(axis=1).values]
        for tok, cols in subcat.items() if cols
    }
    return result if len(result) >= 2 else {}

# ── Baseline loading ──────────────────────────────────────────────────────────
MEXICO_CSV = Path(__file__).parent / "mexico_full_input.csv"
UGANDA_CSV = Path(__file__).parent / "uganda_full_input.csv"

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

print("[v2] Loading Uganda baseline…")
_df_ug = pd.read_csv(UGANDA_CSV)
try:
    _trf_ug = trfs.Transformers({}, df_input=_df_ug)
except Exception as exc:
    print(f"  Uganda transformer init skipped: {exc.__class__.__name__}")
    _trf_ug = _trf_cr

BASELINES = {
    "costa_rica": _make_baseline(_df_cr, _trf_cr),
    "mexico":     _make_baseline(_df_mx, _trf_mx),
    "uganda":     _make_baseline(_df_ug, _trf_ug),
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
                    if "emission" in c.lower()
                    and "subsector_total" not in c.lower()
                    and any(p in c.lower() for p in _pfxs)
                ]
                if _sname not in SECTOR_EM_COLS:
                    SECTOR_EM_COLS[_sname] = _em_cols
                    print(f"[v2]   {_sname}: {len(_em_cols)} emission cols")
                
                # Note: We cache the DF and cols, but we calculate totals dynamically in the endpoint
                # based on the requested gas.
                SECTOR_BL_OUTPUTS[_region][_sname] = {
                    "df_out": _df_out,
                    "prefixes": _pfxs
                }
            except Exception as _e:
                print(f"[v2] {_sname}/{_region} baseline failed: {_e}")

        # ── Extract transport (trns) from energy df_out ──────────────────────
        # EnergyConsumption model includes Transportation (TRNS) subsector.
        # Store it as a separate "transport" entry with real emission values.
        # _energy_bl = SECTOR_BL_OUTPUTS.get(_region, {}).get("energy")
        # if _energy_bl is not None:
        #     try:
        #         _df_energy = _energy_bl["df_out"]
        #         _trns_em_cols = [
        #             c for c in _df_energy.columns
        #             if "emission" in c.lower()
        #             and "subsector_total" not in c.lower()
        #             and "trns" in c.lower()
        #         ]
        #         if _trns_em_cols:
        #             SECTOR_BL_OUTPUTS[_region]["transport"] = {
        #                 "df_out":   _df_energy,
        #                 "prefixes": ["trns"],
        #                 "em_cols":  _trns_em_cols,
        #             }
        #             print(f"[v2] transport/{_region}: {len(_trns_em_cols)} real emission cols from energy model")
        #         else:
        #             print(f"[v2] transport/{_region}: no trns emission cols found in energy df_out")
        #     except Exception as _te:
        #         print(f"[v2] transport/{_region} extraction failed: {_te}")
        # In the startup loop where transport is extracted from energy:
        _energy_bl = SECTOR_BL_OUTPUTS.get(_region, {}).get("energy")
        if _energy_bl is not None:
            try:
                _df_energy = _energy_bl["df_out"]
                # More flexible matching: look for emission columns with transport-related prefixes
                _trns_em_cols = [
                    c for c in _df_energy.columns
                    if "emission" in c.lower()
                    and "subsector_total" not in c.lower()
                    and any(t in c.lower() for t in ["trns", "transport", "aviation", "road_", "rail_", "water_"])
                ]
                if _trns_em_cols:
                    SECTOR_BL_OUTPUTS[_region]["transport"] = {
                        "df_out":   _df_energy,
                        "prefixes": ["trns"],
                        "em_cols":  _trns_em_cols,
                    }
                    print(f"[v2] transport/{_region}: {len(_trns_em_cols)} real emission cols from energy model")
                else:
                    print(f"[v2] transport/{_region}: no trns emission cols found in energy df_out")
                    print(f"[v2] Available emission cols sample: {[c for c in _df_energy.columns if 'emission' in c.lower()][:10]}")
            except Exception as _te:
                print(f"[v2] transport/{_region} extraction failed: {_te}")

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
    
    # Get correct EF lookup based on gas
    if gas == "co2":
        gas_ef = bl["gas_ef_lookups"]["co2"]
    else:
        gas_ef = bl["gas_ef_lookups"].get(gas, {k: np.zeros(len(df_input)) for k in bl["gas_ef_lookups"]["co2"].keys()})
        
    grid_ef    = GRID_EF_KG_CO2_PER_KWH.get(region, _GRID_EF_FALLBACK)
    n          = len(df_input)

    # For baseline, we freeze EFs to t=0 to keep baseline flat
    frozen_gas_ef  = {k: np.full(n, float(v[0])) for k, v in gas_ef.items()}
    
    df_result      = trf.Transformation(policy_cfg, bl["transformers"])()
    eff_lk_policy  = _build_efficiency_lookup(df_result, modes, fuels)

    bl_s1 = _scope1_by_mode(df_input,  proxies, frozen_gas_ef, eff_lk,        modes, fuels, gas)
    bl_s2 = _scope2_by_mode(df_input,  proxies, eff_lk,        modes, grid_ef, gas)
    bl_s3 = _scope3_by_mode(df_input,  proxies, eff_lk,        modes, fuels, gas)
    
    po_s1 = _scope1_by_mode(df_result, proxies, gas_ef,        eff_lk_policy, modes, fuels, gas)
    po_s2 = _scope2_by_mode(df_result, proxies, eff_lk_policy, modes, grid_ef, gas)
    po_s3 = _scope3_by_mode(df_result, proxies, eff_lk_policy, modes, fuels, gas)

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
        bl_sc1 = _sum_modes(bl_s1, n); po_sc1 = _sum_modes(po_s1, n)
        bl_sc2 = _sum_modes(bl_s2, n); po_sc2 = _sum_modes(po_s2, n)
        bl_sc3 = _sum_modes(bl_s3, n); po_sc3 = _sum_modes(po_s3, n)
        out["scope_breakdown"] = {
            "scope1": [round(float(b - p), 6) for b, p in zip(bl_sc1, po_sc1)],
            "scope2": [round(float(b - p), 6) for b, p in zip(bl_sc2, po_sc2)],
            "scope3": [round(float(b - p), 6) for b, p in zip(bl_sc3, po_sc3)],
        }
        out["by_mode"] = {
            "scope1": {m: {"baseline": bl_s1[m], "policy": po_s1[m]} for m in modes},
            "scope2": {m: {"baseline": bl_s2[m], "policy": po_s2[m]} for m in modes},
            "scope3": {m: {"baseline": bl_s3[m], "policy": po_s3[m]} for m in modes},
        }
    return out

# ── True sector abatement (SISEPUEDE sector model.project()) ──────────────────
def _compute_true_sector_abatement(region: str, sector: str, gas: str, policy_cfg: dict,
                                    detailed: bool = False) -> dict:
    bl_info  = SECTOR_BL_OUTPUTS.get(region, {}).get(sector)
    em_cols_all  = SECTOR_EM_COLS.get(sector, [])
    model    = SECTOR_MODELS.get(sector)

    if not model or not bl_info or not em_cols_all:
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
    
    # Filter columns by GAS
    target_gas = gas.lower()
    em_cols_bl = [c for c in em_cols_all if f"_{target_gas}_" in c.lower()]
    em_cols_po = [c for c in df_out_policy.columns if "emission" in c.lower() and f"_{target_gas}_" in c.lower()]

    # Fallback if no specific gas columns found
    if not em_cols_bl: em_cols_bl = [c for c in em_cols_all if "emission" in c.lower()]
    if not em_cols_po: em_cols_po = [c for c in df_out_policy.columns if "emission" in c.lower()]

    bl_arr    = df_out_bl[em_cols_bl].sum(axis=1).values.astype(float)
    po_arr    = df_out_policy[em_cols_po].sum(axis=1).values.astype(float) if em_cols_po else np.zeros(n)

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
            p_cols_bl = [c for c in em_cols_bl if p in c.lower()]
            p_cols_po = [c for c in em_cols_po if p in c.lower()]
            if not p_cols_bl: continue
            
            bl_sub = df_out_bl[p_cols_bl].sum(axis=1).values.astype(float)
            po_sub = df_out_policy[p_cols_po].sum(axis=1).values.astype(float) if p_cols_po else np.zeros(n)
            
            if abs(bl_sub).max() < 1e-9: continue
            
            _label = SUBSECTOR_LABELS.get(p, p.upper())
            categories.append({
                "key":       p,
                "name":      _label,
                "label":     _label,
                "color":     SUBSECTOR_COLORS.get(p, "#64748b"),
                "baseline":  [round(float(x), 6) for x in bl_sub],
                "policy":    [round(float(x), 6) for x in po_sub],
                "abatement": [round(float(b - po), 6) for b, po in zip(bl_sub, po_sub)],
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
# EMISSION SUMMARY (used by NationalEmissionIQ)
# ═══════════════════════════════════════════════════════════════════════════════

def _calc_trend(arr, window: int = 5) -> float:
    """Average YoY % change over last `window` steps."""
    a = list(arr)
    if len(a) < 2:
        return 0.0
    a = a[-window:] if len(a) >= window else a
    changes = [(a[i] - a[i-1]) / a[i-1] * 100 for i in range(1, len(a)) if abs(a[i-1]) > 1e-9]
    return round(float(np.mean(changes)), 2) if changes else 0.0


def _get_emission_summary(region: str) -> dict:
    """
    Pull real SISEPUEDE baseline for every sector and return a compact summary.
    All series values stay in SISEPUEDE-native units (consistent within a region).
    Percentage shares and YoY trends are computed so callers don't need to worry
    about absolute units for comparative analysis.
    """
    bl = BASELINES.get(region, BASELINES["costa_rica"])
    n  = len(bl["df"])
    years = _make_years(n)
    gas = "co2"

    sector_series: dict = {}   # sector → np.array of length n

    # ── Transport (exact scope 1/2/3 physics) ──────────────────────────────────
    try:
        modes  = bl["modes"];  fuels = bl["fuels"]
        prx    = bl["proxies"]; eff   = bl["eff_lookup"]
        frozen = {k: np.full(n, float(v[0])) for k, v in bl["gas_ef_lookups"]["co2"].items()}
        gef    = GRID_EF_KG_CO2_PER_KWH.get(region, _GRID_EF_FALLBACK)
        s1 = _scope1_by_mode(bl["df"], prx, frozen, eff, modes, fuels, gas)
        s2 = _scope2_by_mode(bl["df"], prx, eff, modes, gef, gas)
        s3 = _scope3_by_mode(bl["df"], prx, eff, modes, fuels, gas)
        tr_arr = _sum_modes(s1, n) + _sum_modes(s2, n) + _sum_modes(s3, n)  # tonnes

        # Per-mode breakdown (top modes by final-year value)
        mode_final = {
            m: float(np.array(s1.get(m, [0]*n)) +
                     np.array(s2.get(m, [0]*n)) +
                     np.array(s3.get(m, [0]*n)))[-1]
            for m in modes
        }
        top_modes = sorted(mode_final.items(), key=lambda x: -x[1])[:4]

        sector_series["transport"] = {
            "arr": tr_arr,
            "top_sub": [{"name": m, "val": round(v, 2)} for m, v in top_modes if v > 0],
        }
    except Exception as _e:
        print(f"[iq] transport summary {region}: {_e}")

    # ── Other sectors from pre-computed SISEPUEDE outputs ──────────────────────
    for sec in ["energy", "agriculture", "waste", "industrial"]:
        bl_info = SECTOR_BL_OUTPUTS.get(region, {}).get(sec)
        if not bl_info:
            continue
        try:
            df_out = bl_info["df_out"]
            pfxs   = bl_info["prefixes"]
            # Prefer CO2-only columns; fall back to all emission cols
            all_em = [c for c in df_out.columns
                      if "emission" in c.lower() and "subsector_total" not in c.lower()
                      and any(p in c.lower() for p in pfxs)]
            em_co2 = [c for c in all_em if f"_{gas}_" in c.lower()]
            em_cols = em_co2 if em_co2 else all_em
            if not em_cols:
                continue
            sec_arr = df_out[em_cols].sum(axis=1).values.astype(float)

            top_sub = []
            for pfx in pfxs:
                pc = [c for c in em_cols if pfx in c.lower()]
                if pc:
                    val = float(df_out[pc].sum(axis=1).values[-1])
                    top_sub.append({"name": SUBSECTOR_LABELS.get(pfx, pfx), "val": round(val, 2)})
            top_sub.sort(key=lambda x: -x["val"])

            sector_series[sec] = {"arr": sec_arr, "top_sub": top_sub[:4]}
        except Exception as _e:
            print(f"[iq] {sec} summary {region}: {_e}")

    if not sector_series:
        return {"years": years, "sectors": {}, "grand_total_series": [0.0]*n}

    # ── Grand total + relative shares ──────────────────────────────────────────
    grand = np.zeros(n)
    for s in sector_series.values():
        grand += s["arr"]

    grand_final = float(grand[-1]) if grand[-1] > 0 else 1.0

    out_sectors: dict = {}
    for sec, info in sector_series.items():
        arr = info["arr"]
        final_val = float(arr[-1])
        pct_share = round(final_val / grand_final * 100, 1)
        trend = _calc_trend(arr)
        out_sectors[sec] = {
            "series":    [round(float(x), 4) for x in arr],
            "final_val": round(final_val, 4),
            "pct_share": pct_share,
            "trend_pct": trend,
            "top_sub":   info["top_sub"],
        }

    return {
        "region":            region,
        "years":             years,
        "grand_total_series":[round(float(x), 4) for x in grand],
        "grand_final_val":   round(grand_final, 4),
        "sectors":           out_sectors,
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


@app.get("/v2/emission-summary/{region}")
async def get_emission_summary(region: str):
    """All-sector emission summary from real SISEPUEDE baselines. Used by NationalEmissionIQ."""
    if region not in BASELINES:
        raise HTTPException(404, f"Region '{region}' not found. Valid: {list(BASELINES.keys())}")
    return _get_emission_summary(region)


@app.get("/v2/sectors/{sector}/policies")
async def list_sector_policies(sector: str):
    policies = _load_sector_policies(sector)
    if not policies:
        raise HTTPException(404, f"No policies for sector '{sector}'")
    meta = SECTOR_META.get(sector, {"label": sector, "color": "#64748b"})
    def _fmt(p):
        d = {k: v for k, v in p.items() if k != "parameters"}
        d.setdefault("label", d.get("name", d.get("id", "")))
        return d

    return {
        "sector":   sector,
        "label":    meta["label"],
        "color":    meta["color"],
        "policies": [_fmt(p) for p in policies],
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
                out = _compute_true_sector_abatement(request.region, sector, request.gas, cfg)
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
        result = _compute_true_sector_abatement(request.region, sector, request.gas, cfg, detailed=True)
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
    target_gas = gas.lower()

    if sector == "transport":
        # ── Use real SISEPUEDE energy model output (trns columns) if available ──
        _trns_bl = SECTOR_BL_OUTPUTS.get(region, {}).get("transport")
        if _trns_bl:
            _df_out   = _trns_bl["df_out"]
            _em_cols  = _trns_bl.get("em_cols") or [
                c for c in _df_out.columns
                if "emission" in c.lower() and "trns" in c.lower()
                and "subsector_total" not in c.lower()
            ]
            # Filter to requested gas
            _gas_cols = [c for c in _em_cols if f"_{target_gas}_" in c.lower()]
            if not _gas_cols:
                _gas_cols = _em_cols  # fallback: all emission cols

            _total = _df_out[_gas_cols].sum(axis=1).values.astype(float) if _gas_cols else np.zeros(n)

            # By transport mode subsectors (trns_*)
            _by_sub: dict = {}
            _mode_prefixes = set()
            for c in _gas_cols:
                parts = c.split("_")
                # column format: emission_co2_trns_{mode}_... → extract mode token
                if "trns" in parts:
                    idx = parts.index("trns")
                    if idx + 1 < len(parts):
                        _mode_prefixes.add(parts[idx + 1])
            for _mp in _mode_prefixes:
                _mc = [c for c in _gas_cols if f"_trns_{_mp}" in c]
                if _mc:
                    _by_sub[_mp] = [round(float(x), 6) for x in _df_out[_mc].sum(axis=1).values]

            # Scope breakdown using SECTOR_SCOPE_MAP if available
            _scope_map = SECTOR_SCOPE_MAP.get("transport", {})
            _scopes = {
                sk: _sum_scope(_gas_cols, _df_out, spfxs)
                for sk, spfxs in _scope_map.items() if spfxs
            }

            return {
                "sector": sector, "region": region, "years": years, "gas": target_gas,
                "emission_type": "sisepuede_real",
                "total":  [round(float(x), 6) for x in _total],
                "by_sub": _by_sub,
                **_scopes,
            }

        # ── Fallback: proxy-based calculation (placeholder activity = 100) ──
        modes   = bl["modes"]; fuels = bl["fuels"]
        proxies = bl["proxies"]; eff_lk = bl["eff_lookup"]
        if target_gas == "co2":
            gas_ef = bl["gas_ef_lookups"]["co2"]
        else:
            gas_ef = bl["gas_ef_lookups"].get(target_gas, {k: np.zeros(n) for k in bl["gas_ef_lookups"]["co2"].keys()})
        grid_ef = GRID_EF_KG_CO2_PER_KWH.get(region, _GRID_EF_FALLBACK)
        frozen  = {k: np.full(n, float(v[0])) for k, v in gas_ef.items()}
        df_in   = bl["df"]
        s1 = _scope1_by_mode(df_in, proxies, frozen, eff_lk, modes, fuels, target_gas)
        s2 = _scope2_by_mode(df_in, proxies, eff_lk, modes, grid_ef, target_gas)
        s3 = _scope3_by_mode(df_in, proxies, eff_lk, modes, fuels, target_gas)
        total = _sum_modes(s1, n) + _sum_modes(s2, n) + _sum_modes(s3, n)
        return {
            "sector": sector, "region": region, "years": years, "gas": target_gas,
            "emission_type": "exact",
            "total":  [round(float(x), 6) for x in total],
            "scope1": [round(float(x), 6) for x in _sum_modes(s1, n)],
            "scope2": [round(float(x), 6) for x in _sum_modes(s2, n)],
            "scope3": [round(float(x), 6) for x in _sum_modes(s3, n)],
            "by_mode": {"scope1": s1, "scope2": s2, "scope3": s3},
        }

    bl_info = SECTOR_BL_OUTPUTS.get(region, {}).get(sector)

    # Try on-demand computation if model exists but baseline wasn't pre-cached
    if not bl_info:
        _model = SECTOR_MODELS.get(sector)
        if _model:
            try:
                bl_data = BASELINES.get(region, BASELINES["costa_rica"])
                _df_out  = _model.project(bl_data["df"])
                _pfxs    = SECTOR_EMISSION_PREFIXES.get(sector, [])
                _em_cols = [
                    c for c in _df_out.columns
                    if "emission" in c.lower()
                    and "subsector_total" not in c.lower()
                    and any(p in c.lower() for p in _pfxs)
                ]
                SECTOR_EM_COLS[sector] = _em_cols
                
                # Calculate totals dynamically based on gas
                _gas_em_cols = [c for c in _em_cols if f"_{target_gas}_" in c.lower()]
                if not _gas_em_cols: _gas_em_cols = _em_cols # Fallback

                _total_arr = (_df_out[_gas_em_cols].sum(axis=1).values.astype(float) if _gas_em_cols else np.zeros(n))
                _by_sub: dict = {}
                for _p in _pfxs:
                    _pc = [c for c in _gas_em_cols if _p in c.lower()]
                    if _pc:
                        _by_sub[_p] = [round(float(x), 6) for x in _df_out[_pc].sum(axis=1).values]
                
                _scope_map = SECTOR_SCOPE_MAP.get(sector, {})
                _scopes = {
                    sk: _sum_scope(_gas_em_cols, _df_out, spfxs)
                    for sk, spfxs in _scope_map.items()
                    if spfxs
                }
                _by_detail_od: dict = {}
                for _p in _pfxs:
                    _pc = [c for c in _gas_em_cols if _p in c.lower()]
                    _sub_series = _by_sub.get(_p, [])
                    if _pc:
                        _det = _extract_by_detail(_pc, _df_out, _p)
                        if not _det and _sub_series:
                            _det = _activity_based_detail(bl_data["df"], _p, _sub_series)
                    elif _sub_series:
                        _det = _activity_based_detail(bl_data["df"], _p, _sub_series)
                    else:
                        _det = {}
                    if _det:
                        _by_detail_od[_p] = _det
                        
                bl_info = {"df_out": _df_out,
                           "total":  [round(float(x), 6) for x in _total_arr.tolist()],
                           "by_sub": _by_sub,
                           **({"by_detail": _by_detail_od} if _by_detail_od else {}),
                           **_scopes}
                SECTOR_BL_OUTPUTS.setdefault(region, {})[sector] = bl_info
            except Exception as _ode:
                print(f"[v2] on-demand baseline for {sector}/{region} failed: {_ode}")

    if bl_info:
        _df_out = bl_info["df_out"]
        _pfxs   = bl_info["prefixes"]

        # All individual emission columns (exclude subsector totals to avoid double-counting)
        _all_em = [c for c in _df_out.columns
                   if "emission" in c.lower()
                   and "subsector_total" not in c.lower()
                   and any(p in c.lower() for p in _pfxs)]

        # Filter to the requested gas; "all" keeps every gas column
        _em_cols = [c for c in _all_em if f"_{target_gas}_" in c.lower()] if target_gas != "all" else _all_em
        
        # If filtering resulted in empty list (e.g. asking for N2O but only CO2 exists), fallback to all
        if not _em_cols and target_gas != "all":
             _em_cols = _all_em

        # Recompute totals from gas-filtered columns
        _total_arr = (_df_out[_em_cols].sum(axis=1).values.astype(float)
                      if _em_cols else np.zeros(n))

        _by_sub: dict = {}
        for _p in _pfxs:
            _pc = [c for c in _em_cols if _p in c.lower()]
            if _pc:
                _by_sub[_p] = [round(float(x), 6) for x in _df_out[_pc].sum(axis=1).values]

        _smap  = SECTOR_SCOPE_MAP.get(sector, {})
        _scopes = {sk: _sum_scope(_em_cols, _df_out, spfxs)
                   for sk, spfxs in _smap.items() if spfxs}
        _by_mode_sub = {
            sk: {p: _by_sub[p] for p in spfxs if p in _by_sub}
            for sk, spfxs in _smap.items()
        }

        # by_detail: prefer gas-specific columns (real SISEPUEDE per-gas breakdown),
        # fall back to all-gas proportions only when a subsector has no gas-specific cols.
        _bl_df: "pd.DataFrame" = BASELINES.get(region, BASELINES["costa_rica"])["df"]
        _by_detail: dict = {}
        for _p in _pfxs:
            _sub_total = _by_sub.get(_p, [])
            if not _sub_total:
                continue
            _pc_gas = [c for c in _em_cols if _p in c.lower()]   # gas-specific for this sub
            _pc_all = [c for c in _all_em if _p in c.lower()]     # all-gas fallback
            if _pc_gas:
                _det = _extract_by_detail(_pc_gas, _df_out, _p)
                if not _det:
                    _det = _activity_based_detail(_bl_df, _p, _sub_total)
            elif _pc_all:
                # subsector emits in other gases but not this one — use all-gas proportions
                _det = _extract_by_detail(_pc_all, _df_out, _p)
                if not _det:
                    _det = _activity_based_detail(_bl_df, _p, _sub_total)
            else:
                _det = _activity_based_detail(_bl_df, _p, _sub_total)
            if _det:
                _by_detail[_p] = _det

        resp = {
            "sector": sector, "region": region, "gas": target_gas, "years": years,
            "emission_type": "exact",
            "total":   [round(float(x), 6) for x in _total_arr.tolist()],
            "by_sub":  _by_sub,
            "by_mode": _by_mode_sub,
            **_scopes,
        }
        if _by_detail:
            resp["by_detail"] = _by_detail
        return resp

    # Proxy fallback when no sector model is available
    _proxy_scale = {
        "agriculture": 5_000_000.0,
        "waste":       1_000_000.0,
        "energy":     10_000_000.0,
        "industrial":  1_500_000.0,
    }.get(sector, 1_000_000.0)
    # Approximate gas fraction of total CO2e by sector (IPCC AR6 typical shares)
    _GAS_PROXY_FRAC: Dict[str, Dict[str, float]] = {
        "transport":   {"co2": 0.97, "ch4": 0.01, "n2o": 0.02},
        "energy":      {"co2": 0.72, "ch4": 0.18, "n2o": 0.10},
        "agriculture": {"co2": 0.03, "ch4": 0.55, "n2o": 0.42},
        "waste":       {"co2": 0.03, "ch4": 0.82, "n2o": 0.15},
        "industrial":  {"co2": 0.88, "ch4": 0.04, "n2o": 0.08},
    }
    _gas_frac = _GAS_PROXY_FRAC.get(sector, {}).get(target_gas, 1.0) if target_gas != "all" else 1.0
    _total_proxy = np.linspace(_proxy_scale * 0.8, _proxy_scale, n) * _gas_frac
    _pfxs = SECTOR_EMISSION_PREFIXES.get(sector, [])
    _split = max(len(_pfxs), 1)
    _by_sub_proxy = {
        p: [round(float(x / _split), 6) for x in _total_proxy]
        for p in _pfxs
    }
    # Build proxy scopes and by_mode — split total proportionally by subsector count per scope
    _scope_map   = SECTOR_SCOPE_MAP.get(sector, {})
    _proxy_scopes: dict = {}
    _proxy_by_mode: dict = {}
    if _scope_map:
        _total_mapped = sum(len(v) for v in _scope_map.values() if v)
        _total_mapped = max(_total_mapped, 1)
        for sk, spfxs in _scope_map.items():
            if spfxs:
                frac = len(spfxs) / _total_mapped
                per_sub = frac / len(spfxs)
                _proxy_scopes[sk] = [round(float(x * frac), 6) for x in _total_proxy]
                _proxy_by_mode[sk] = {
                    p: [round(float(x * per_sub), 6) for x in _total_proxy]
                    for p in spfxs
                }
            else:
                _proxy_by_mode[sk] = {}
    # Build by_detail from input proportions even in proxy mode
    _bl_df = BASELINES.get(region, BASELINES["costa_rica"])["df"]
    _proxy_by_detail: dict = {}
    for _p in _pfxs:
        _sub_series = _by_sub_proxy.get(_p, [])
        if _sub_series:
            _det = _activity_based_detail(_bl_df, _p, _sub_series)
            if _det:
                _proxy_by_detail[_p] = _det

    return {
        "sector":        sector,
        "region":        region,
        "gas":           target_gas,
        "years":         years,
        "emission_type": "proxy",
        "total":         [round(float(x), 6) for x in _total_proxy],
        "by_sub":        _by_sub_proxy,
        "by_mode":       _proxy_by_mode,
        **({"by_detail": _proxy_by_detail} if _proxy_by_detail else {}),
        **_proxy_scopes,
    }


@app.get("/v2/debug/columns/{sector}")
async def debug_columns(sector: str, region: str = "costa_rica"):
    """Debug: return actual emission column names and by_detail keys for a sector."""
    em_cols = SECTOR_EM_COLS.get(sector, [])
    bl_info = SECTOR_BL_OUTPUTS.get(region, {}).get(sector, {})
    bl_df   = BASELINES.get(region, BASELINES["costa_rica"])["df"]
    pfxs    = SECTOR_EMISSION_PREFIXES.get(sector, [])

    activity_cols = {}
    for p in pfxs:
        col_prefix = _ACTIVITY_COL_MAP.get(p)
        if col_prefix:
            activity_cols[p] = [c for c in bl_df.columns if c.lower().startswith(col_prefix)]

    return {
        "sector":          sector,
        "region":          region,
        "em_col_count":    len(em_cols),
        "em_cols_sample":  em_cols[:30],
        "by_sub_keys":     list(bl_info.get("by_sub", {}).keys()),
        "by_detail_keys":  {k: list(v.keys()) for k, v in bl_info.get("by_detail", {}).items()},
        "activity_col_map": activity_cols,
        "models_loaded":   list(SECTOR_MODELS.keys()),
    }


@app.get("/v2/net-zero/policies")
async def get_net_zero_policies():
    """All policies from ALL sectors for the Net Zero Plan tab."""
    all_pols = []
    for sector, policies in ALL_POLICIES.items():
        meta = SECTOR_META.get(sector, {"label": sector, "color": "#64748b"})
        for p in policies:
            _label = p.get("label") or p.get("name") or p["id"]
            all_pols.append({
                "id":                     p["id"],
                "name":                   _label,
                "label":                  _label,
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
                out = _compute_true_sector_abatement(request.region, sector, gas, cfg)
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

    # Aggregate baseline (one per sector) and total abatement across all policies
    n_ts = len(years)
    sector_bl: dict = {}
    for pid, out in results.items():
        sec = out.get("sector", "")
        if sec and sec not in sector_bl and out.get("baseline"):
            sector_bl[sec] = np.array(out["baseline"], dtype=float)

    if sector_bl:
        baseline_arr = sum(sector_bl.values())
    else:
        baseline_arr = np.zeros(n_ts)

    total_abat = np.zeros(n_ts)
    for out in results.values():
        ab = out.get("abatement")
        if ab and len(ab) == n_ts:
            total_abat += np.array(ab, dtype=float)

    with_all_arr = np.maximum(0.0, baseline_arr - total_abat)

    policies_list = [
        {
            "id":                     pid.split("::", 1)[-1],
            "label":                  out.get("name", pid),
            "sector":                 out.get("sector", ""),
            "sector_label":           out.get("sector_label", ""),
            "sector_color":           out.get("sector_color", "#64748b"),
            "total_abatement":        out.get("final_abatement_tonnes", 0),
            "default_capex_per_tco2": out.get("capex_per_tco2", 50),
        }
        for pid, out in results.items()
    ]

    return {
        "region":            request.region,
        "gas":               request.gas,
        "years":             years,
        "baseline":          [round(float(x), 6) for x in baseline_arr],
        "with_all_policies": [round(float(x), 6) for x in with_all_arr],
        "policies":          policies_list,
        "results":           results,
        "errors":            errors,
    }


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
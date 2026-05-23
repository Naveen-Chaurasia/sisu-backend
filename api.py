import warnings
warnings.filterwarnings("ignore")

import json
import os
import sys

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic

sys.path.insert(0, os.path.dirname(__file__))
from config import USERS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sisepuede"))

import pandas as pd
import sisepuede.transformers as trf
import sisepuede.transformers.transformers as trfs
from sisepuede.manager.sisepuede_examples import SISEPUEDEExamples

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="SISEPUEDE Policy Simulator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Emissions helpers — fully data-driven from SISEPUEDE columns
# Must be defined before baseline loading so _make_baseline can call them.
# ---------------------------------------------------------------------------

# Energy densities (TJ/litre) and CO2 factors (kg CO2/TJ) — IEA/IPCC physics constants.
# Fuel chemistry does not vary by country, so these are universal.
# CH4 and N2O are NOT here — those come from SISEPUEDE columns (country/mode specific).
ED_TJ_PER_LITRE = {
    "diesel":                  3.59e-5,
    "gasoline":                3.44e-5,
    "kerosene":                3.48e-5,
    "natural_gas":             3.87e-5,
    "biofuels":                2.94e-5,
    "hydrocarbon_gas_liquids": 2.53e-5,
    "hydrogen":                8.36e-6,
    "electricity":             3.60e-9,   # 1 kWh = 3.6e-9 TJ
    "ammonia":                 1.58e-5,
}
_ED_FALLBACK = 3.50e-5

# IPCC AR5 Table A.II.4 — CO2 emission factors (kg CO2/TJ).
# Used only for the "co2" gas channel; CH4/N2O are read from SISEPUEDE columns.
CO2_KG_PER_TJ = {
    "diesel":                  74100.0,
    "gasoline":                69300.0,
    "kerosene":                71500.0,
    "natural_gas":             56100.0,
    "biofuels":                0.0,       # net carbon-neutral
    "hydrocarbon_gas_liquids": 63100.0,   # LPG
    "hydrogen":                0.0,
    "electricity":             0.0,
    "ammonia":                 0.0,
}


def detect_transport_modes(df: pd.DataFrame) -> list:
    """Detect transport modes from elecfuelefficiency column names."""
    modes = set()
    for col in df.columns:
        if col.startswith("elecfuelefficiency_trns_") and col.endswith("_km_per_kwh"):
            modes.add(col[len("elecfuelefficiency_trns_"):-len("_km_per_kwh")])
    return sorted(modes)


def detect_transport_fuels(df: pd.DataFrame, modes: list) -> list:
    """
    Detect transport fuels from fuelefficiency column names.
    Electricity is included via the elecfuelefficiency columns.
    """
    fuels = {"electricity"}
    for col in df.columns:
        if col.startswith("fuelefficiency_trns_") and col.endswith("_km_per_litre"):
            after = col[len("fuelefficiency_trns_"):-len("_km_per_litre")]
            for mode in modes:
                if after.startswith(mode + "_"):
                    fuels.add(after[len(mode) + 1:])
                    break
    return sorted(fuels)


def detect_available_gases(df: pd.DataFrame) -> list:
    """
    Detect gas types that have emission factor columns in df.
    Parses: ef_trns_mobile_combustion_{mode}_kg_{GAS}_per_tj_{fuel}
    """
    gases = set()
    for col in df.columns:
        if col.startswith("ef_trns_mobile_combustion_") and "_kg_" in col and "_per_tj_" in col:
            try:
                gases.add(col.split("_kg_")[1].split("_per_tj_")[0])
            except IndexError:
                pass
    return sorted(gases)


def build_activity_proxies(df_baseline: pd.DataFrame, modes: list) -> dict:
    """
    Flat activity proxy (100) for all modes.
    We don't have actual vehicle-km data in SISEPUEDE input columns, so using avgload
    as a weight causes maritime/rail (avgload ~6000+ t/veh) to dwarf road (avgload ~30 t/veh)
    by 200x, making any road-targeted policy look like 0% on the chart.
    Flat proxy keeps all modes on equal footing so per-mode fuel-switch policies
    produce visible, proportional reductions.
    """
    n = len(df_baseline)
    return {mode: np.ones(n) * 100 for mode in modes}


def build_efficiency_lookup(df_baseline: pd.DataFrame, modes: list, fuels: list) -> dict:
    """
    {(mode, fuel): np.ndarray of km/litre or km/kWh (electricity)}.
    Locked to baseline so SISEPUEDE efficiency side-effects don't distort comparisons.
    """
    n = len(df_baseline)
    lookup = {}
    for mode in modes:
        for fuel in fuels:
            if fuel == "electricity":
                col, default = f"elecfuelefficiency_trns_{mode}_km_per_kwh", 5.0
            else:
                col, default = f"fuelefficiency_trns_{mode}_{fuel}_km_per_litre", 3.0
            if col in df_baseline.columns:
                lookup[(mode, fuel)] = np.maximum(df_baseline[col].fillna(default).values, 1e-9)
            else:
                lookup[(mode, fuel)] = np.full(n, default)
    return lookup


def build_gas_ef_lookup(df_baseline: pd.DataFrame, gas: str,
                        modes: list, fuels: list) -> dict:
    """
    {(mode, fuel): np.ndarray of kg_{gas}/TJ} read from SISEPUEDE columns.
    Used for CH4 and N2O — both are mode/fuel/country specific in SISEPUEDE.
    """
    n = len(df_baseline)
    lookup = {}
    for mode in modes:
        for fuel in fuels:
            col = f"ef_trns_mobile_combustion_{mode}_kg_{gas}_per_tj_{fuel}"
            if col in df_baseline.columns:
                lookup[(mode, fuel)] = df_baseline[col].fillna(0.0).values
            else:
                lookup[(mode, fuel)] = np.zeros(n)
    return lookup


def build_co2_ef_lookup(modes: list, fuels: list, n: int) -> dict:
    """
    {(mode, fuel): np.ndarray of kg_CO2/TJ} from IPCC AR5 constants.
    CO2 is not in SISEPUEDE transport columns — derived from carbon content of fuel.
    The same constant applies to every mode (CO2 depends only on fuel chemistry).
    """
    lookup = {}
    for mode in modes:
        for fuel in fuels:
            lookup[(mode, fuel)] = np.full(n, float(CO2_KG_PER_TJ.get(fuel, 0.0)))
    return lookup


def calculate_gas_emissions(
    df: pd.DataFrame,
    activity_proxies: dict,
    gas_ef_lookup: dict,
    eff_lookup: dict,
    modes: list,
    fuels: list,
) -> np.ndarray:
    """
    Compute emissions of one gas (tonnes) using:
      emissions = sum over (mode,fuel): frac x (proxy/efficiency) x energy_density x EF_gas

    activity_proxies, gas_ef_lookup, eff_lookup are ALL locked to baseline.
    Only frac columns are read from df (these change with policy).
    """
    n = len(df)
    emissions = np.zeros(n)
    for mode in modes:
        proxy = activity_proxies.get(mode, np.ones(n) * 100)
        for fuel in fuels:
            frac_col = f"frac_trns_fuelmix_{mode}_{fuel}"
            if frac_col not in df.columns:
                continue
            frac   = df[frac_col].fillna(0.0).values
            eff    = eff_lookup.get((mode, fuel), np.full(n, 3.0))
            ed     = ED_TJ_PER_LITRE.get(fuel, _ED_FALLBACK)
            gas_ef = gas_ef_lookup.get((mode, fuel), np.zeros(n))
            emissions += frac * (proxy / np.maximum(eff, 1e-9)) * ed * gas_ef
    return emissions / 1e3   # kg → tonnes of this gas


# ---------------------------------------------------------------------------
# Load SISEPUEDE baselines once at startup
# ---------------------------------------------------------------------------
MEXICO_CSV = os.path.join(os.path.dirname(__file__), "mexico_full_input.csv")


def _make_baseline(df: pd.DataFrame, transformers) -> dict:
    modes  = detect_transport_modes(df)
    fuels  = detect_transport_fuels(df, modes)
    n      = len(df)
    proxies    = build_activity_proxies(df, modes)
    eff_lookup = build_efficiency_lookup(df, modes, fuels)

    # CH4/N2O: data-driven from SISEPUEDE columns (country/mode specific)
    sisepuede_gases = detect_available_gases(df)
    gas_ef_lookups  = {gas: build_gas_ef_lookup(df, gas, modes, fuels) for gas in sisepuede_gases}

    # CO2: always available, computed from IPCC carbon-content constants
    gas_ef_lookups["co2"] = build_co2_ef_lookup(modes, fuels, n)

    available_gases = sorted({"co2"} | set(sisepuede_gases))   # co2 always present

    return {
        "df":              df,
        "transformers":    transformers,
        "modes":           modes,
        "fuels":           fuels,
        "available_gases": available_gases,
        "proxies":         proxies,
        "eff_lookup":      eff_lookup,
        "gas_ef_lookups":  gas_ef_lookups,
    }


print("Loading Costa Rica baseline...")
examples       = SISEPUEDEExamples()
df_costa_rica  = examples("input_data_frame")
trf_costa_rica = trfs.Transformers({}, df_input=df_costa_rica)
print(f"  Costa Rica: {df_costa_rica.shape}")

print("Loading Mexico baseline...")
df_mexico = pd.read_csv(MEXICO_CSV)
try:
    trf_mexico = trfs.Transformers({}, df_input=df_mexico)
    print(f"  Mexico: {df_mexico.shape} (full transformers)")
except Exception as e:
    print(f"  Mexico: {df_mexico.shape} (transformer init skipped: {e.__class__.__name__})")
    trf_mexico = trf_costa_rica

BASELINES = {
    "costa_rica": _make_baseline(df_costa_rica, trf_costa_rica),
    "mexico":     _make_baseline(df_mexico,     trf_mexico),
}

# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ---------------------------------------------------------------------------
# Claude system prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEXT = """You are a climate policy modeling assistant for SISEPUEDE, a multi-sector greenhouse gas emission model used by governments and research institutions.

Your task: take a natural-language transport policy description, map it to a valid SISEPUEDE transformation config, call the run_sisepuede_transformation tool, then give a concise quantitative summary of the results.

═══════════════════════════════════════════════════
AVAILABLE TRANSPORT TRANSFORMERS
═══════════════════════════════════════════════════

FUEL SWITCHING
──────────────
• TFR:TRNS:SHIFT_FUEL_MEDIUM_DUTY
  Shift fuel for heavy freight trucks or regional heavy vehicles.
  Required parameters:
    categories           : list from [road_heavy_freight, road_heavy_regional]
    fuels_source         : list e.g. ["fuel_diesel"]
    dict_allocation_fuels_target : dict e.g. {"fuel_electricity": 1.0}  (must sum to 1.0)
    magnitude            : 0.0–1.0  (fraction of fleet affected)
    vec_implementation_ramp : see RAMP section

• TFR:TRNS:SHIFT_FUEL_LIGHT_DUTY
  Electrify private cars / light-duty vehicles.
  Required parameters:
    dict_allocation_fuels_target : {"fuel_electricity": 1.0}
    magnitude            : 0.0–1.0
    vec_implementation_ramp

• TFR:TRNS:SHIFT_FUEL_RAIL
  Electrify rail (freight or passenger).
  Required parameters:
    dict_allocation_fuels_target : {"fuel_electricity": 1.0}
    magnitude            : 0.0–1.0
    vec_implementation_ramp

• TFR:TRNS:SHIFT_FUEL_MARITIME
  Switch maritime/waterborne shipping fuel (default target: hydrogen).
  Required parameters:
    dict_allocation_fuels_target : e.g. {"fuel_hydrogen": 1.0}
    magnitude            : 0.0–1.0
    vec_implementation_ramp

EFFICIENCY IMPROVEMENT
──────────────────────
• TFR:TRNS:INC_EFFICIENCY_NON_ELECTRIC
  Improve fuel economy of non-electric vehicles.
  Required parameters:
    categories : list of modes
    fuels      : list of fuels to improve e.g. ["fuel_diesel"]
    magnitude  : 0.0–0.5  (fractional efficiency gain, e.g. 0.25 = 25% better)
    vec_implementation_ramp

• TFR:TRNS:INC_EFFICIENCY_ELECTRIC
  Improve electric vehicle efficiency (km/kWh).
  Required parameters:
    categories : list of modes
    magnitude  : 0.0–0.5
    vec_implementation_ramp

MODE SHIFT
──────────
• TFR:TRNS:SHIFT_MODE_FREIGHT
  Shift freight from road and aviation to rail.
  Required parameters:
    magnitude  : 0.0–1.0  (fraction of freight shifted to rail)
    vec_implementation_ramp

• TFR:TRNS:SHIFT_MODE_PASSENGER
  Shift private-car trips to transit, cycling, walking.
  Required parameters:
    magnitude  : 0.0–1.0
    vec_implementation_ramp

• TFR:TRNS:SHIFT_MODE_REGIONAL
  Shift regional passenger air/road travel to heavy-duty road/rail.
  Required parameters:
    magnitude  : 0.0–1.0
    vec_implementation_ramp

DEMAND REDUCTION
────────────────
• TFR:TRDE:DEC_DEMAND
  Reduce total transport demand (e.g. remote work, trip consolidation).
  Required parameters:
    magnitude  : 0.0–1.0  (fractional reduction)
    vec_implementation_ramp

═══════════════════════════════════════════════════
TRANSPORT CATEGORIES
═══════════════════════════════════════════════════
road_heavy_freight, road_heavy_regional, road_light, public,
rail_freight, rail_passenger, aviation, water_borne, powered_bikes

═══════════════════════════════════════════════════
AVAILABLE FUELS
═══════════════════════════════════════════════════
fuel_diesel, fuel_electricity, fuel_hydrogen, fuel_biofuels,
fuel_natural_gas, fuel_gasoline, fuel_hydrocarbon_gas_liquids, fuel_kerosene

═══════════════════════════════════════════════════
IMPLEMENTATION RAMP (vec_implementation_ramp)
═══════════════════════════════════════════════════
The model runs from time period 0 (≈2015) to ~35 (≈2050), roughly annual steps.
  tp_0_ramp      : period when ramp begins  (0=2015, 5=2020, 10=2025, 20=2035, 25=2040)
  n_tp_ramp      : null = auto-calculate; or integer for explicit ramp length
  alpha_logistic : 0.0 = symmetric S-curve, >0 = faster start, <0 = slow start
  window_logistic: [-8, 8]  (keep default)

Example for "by 2035 with gradual rollout":
  {"alpha_logistic": 0.0, "n_tp_ramp": null, "tp_0_ramp": 5, "window_logistic": [-8, 8]}

═══════════════════════════════════════════════════
POLICY CONFIG SCHEMA
═══════════════════════════════════════════════════
{
  "identifiers": {
    "transformation_code": "TX:SHORT_CODE",
    "transformation_name": "Human Readable Name"
  },
  "transformer": "TFR:TRNS:...",
  "parameters": {
    ...transformer-specific parameters...
    "magnitude": 0.6,
    "vec_implementation_ramp": {
      "alpha_logistic": 0.0,
      "n_tp_ramp": null,
      "tp_0_ramp": 5,
      "window_logistic": [-8, 8]
    }
  }
}

INSTRUCTIONS
────────────
1. Parse the user's policy: what sector? what change? how much? by when?
2. Choose the best matching transformer.
3. Infer magnitude from phrases like "50% electrified" → 0.5, "fully" → 0.9, "gradually" → 0.5.
4. Infer tp_0_ramp from target year: tp_0_ramp ≈ max(0, target_year - 2015 - 10).
5. Call run_sisepuede_transformation with the config.
6. Summarize results using the sign convention below.

SIGN CONVENTION FOR RESULTS
────────────────────────────
The tool returns `final_reduction_pct` and `final_reduction_tonnes` computed as:
  reduction = baseline_emissions − policy_emissions

• POSITIVE value → policy REDUCES emissions (good, e.g. diesel → hydrogen)
  Display as: "−X%" reduction, "X t saved"
• NEGATIVE value → policy INCREASES emissions (bad, e.g. biofuels → diesel)
  Display as: "+X% increase", "X t added"

When writing the summary table use these column headers:
  Gas | Baseline (2050) | Policy (2050) | Change | Tonnes
And in the Change column:
  • If final_reduction_pct > 0: show "−{pct}% ↓" (emissions went down)
  • If final_reduction_pct < 0: show "+{|pct|}% ↑" (emissions went up)
In the Tonnes column:
  • If final_reduction_tonnes > 0: show "{tonnes} t saved"
  • If final_reduction_tonnes < 0: show "{|tonnes|} t added"

Do NOT flip the sign — use `final_reduction_pct` directly from the tool result.
Do NOT ask clarifying questions — make reasonable assumptions and state them.
"""

# Cached system prompt — passed as list so Anthropic caches it after first call
SYSTEM_PROMPT = [{"type": "text", "text": SYSTEM_PROMPT_TEXT, "cache_control": {"type": "ephemeral"}}]

# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "run_sisepuede_transformation",
        "description": (
            "Execute a SISEPUEDE transport transformation and return per-gas "
            "time-series emissions comparing baseline vs. policy scenario."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "policy_name": {
                    "type": "string",
                    "description": "Short human-readable policy name",
                },
                "policy_config": {
                    "type": "object",
                    "description": "Full SISEPUEDE transformation configuration",
                    "properties": {
                        "identifiers": {
                            "type": "object",
                            "properties": {
                                "transformation_code": {"type": "string"},
                                "transformation_name": {"type": "string"},
                            },
                            "required": ["transformation_code", "transformation_name"],
                        },
                        "transformer": {"type": "string"},
                        "parameters": {"type": "object"},
                    },
                    "required": ["identifiers", "transformer", "parameters"],
                },
            },
            "required": ["policy_name", "policy_config"],
        },
    }
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------
def run_transformation_tool(
    policy_name: str,
    policy_config: dict,
    df_input: pd.DataFrame,
    transformers,
    modes: list,
    fuels: list,
    proxies: dict,
    eff_lookup: dict,
    available_gases: list,
    gas_ef_lookups: dict,
) -> dict:
    try:
        transformation = trf.Transformation(policy_config, transformers)
        df_result = transformation()

        n = len(df_input)

        # Freeze eff and gas_ef at t=0 for the baseline only → flat reference line.
        frozen_eff = {k: np.full(n, float(v[0])) for k, v in eff_lookup.items()}

        per_gas = {}
        for gas in available_gases:
            gas_ef        = gas_ef_lookups[gas]
            frozen_gas_ef = {k: np.full(n, float(v[0])) for k, v in gas_ef.items()}

            baseline_em = calculate_gas_emissions(df_input,  proxies, frozen_gas_ef, frozen_eff, modes, fuels)
            policy_em   = calculate_gas_emissions(df_result, proxies, gas_ef,        eff_lookup,  modes, fuels)

            final_base = float(baseline_em[-1])
            final_pol  = float(policy_em[-1])
            reduction  = final_base - final_pol
            pct = 100.0 * reduction / final_base if final_base > 0 else 0.0

            per_gas[gas] = {
                "baseline":               [round(float(v), 4) for v in baseline_em],
                "policy":                 [round(float(v), 4) for v in policy_em],
                "final_reduction_pct":    round(pct, 1),
                "final_reduction_tonnes": round(reduction, 4),
            }

        n     = len(df_input)
        years = [2015 + round(i * 35 / max(n - 1, 1)) for i in range(n)]

        return {
            "success":         True,
            "policy_name":     policy_name,
            "years":           years,
            "available_gases": available_gases,
            "per_gas":         per_gas,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "policy_name": policy_name}


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/login")
async def login(req: LoginRequest):
    key = req.username.strip().lower()
    if USERS.get(key) == req.password:
        return {"ok": True, "username": req.username.strip()}
    raise HTTPException(status_code=401, detail="Invalid username or password.")


@app.get("/api/available-gases")
async def get_available_gases(region: str = "costa_rica"):
    bl = BASELINES.get(region, BASELINES["costa_rica"])
    available = bl["available_gases"]   # always includes "co2"
    common    = ["co2", "ch4", "n2o"]
    return {
        "region":      region,
        "available":   available,
        "unavailable": [g for g in common if g not in available],
        "note": {
            "co2":  "computed from IPCC AR5 carbon-content constants",
            "ch4":  "read from SISEPUEDE ef_trns_mobile_combustion_* columns",
            "n2o":  "read from SISEPUEDE ef_trns_mobile_combustion_* columns",
        },
    }


class PolicyRequest(BaseModel):
    description: str
    region: str = "costa_rica"


@app.post("/api/run-policy")
async def run_policy(request: PolicyRequest):
    bl = BASELINES.get(request.region, BASELINES["costa_rica"])
    df_input        = bl["df"]
    transformers    = bl["transformers"]
    modes           = bl["modes"]
    fuels           = bl["fuels"]
    proxies         = bl["proxies"]
    eff_lookup      = bl["eff_lookup"]
    available_gases = bl["available_gases"]
    gas_ef_lookups  = bl["gas_ef_lookups"]

    messages = [{"role": "user", "content": request.description}]
    tool_result_data: dict | None = None
    policy_config_used: dict | None = None

    for _ in range(3):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            summary = next(
                (b.text for b in response.content if hasattr(b, "text")),
                "Policy analysis complete.",
            )
            return {"summary": summary, "data": tool_result_data, "policy_config": policy_config_used}

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "run_sisepuede_transformation":
                    policy_config_used = block.input.get("policy_config")
                    result = run_transformation_tool(
                        **block.input,
                        df_input=df_input,
                        transformers=transformers,
                        modes=modes,
                        fuels=fuels,
                        proxies=proxies,
                        eff_lookup=eff_lookup,
                        available_gases=available_gases,
                        gas_ef_lookups=gas_ef_lookups,
                    )
                    tool_result_data = result
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            raise HTTPException(status_code=500, detail=f"Unexpected stop reason: {response.stop_reason}")

    raise HTTPException(status_code=500, detail="Agent loop exceeded max iterations")


class ScopeSimRequest(BaseModel):
    description: str
    region: str = "costa_rica"
    gas: str = "co2"


@app.post("/api/scope-emissions-simulate")
async def scope_emissions_simulate(request: ScopeSimRequest):
    try:
        return await _scope_emissions_simulate_impl(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def _scope_emissions_simulate_impl(request: ScopeSimRequest):
    bl = BASELINES.get(request.region, BASELINES["costa_rica"])
    df_input        = bl["df"]
    transformers    = bl["transformers"]
    modes           = bl["modes"]
    fuels           = bl["fuels"]
    proxies         = bl["proxies"]
    eff_lookup      = bl["eff_lookup"]
    available_gases = bl["available_gases"]
    gas_ef_lookups  = bl["gas_ef_lookups"]

    gas     = request.gas if request.gas in available_gases else "co2"
    grid_ef = GRID_EF_KG_CO2_PER_KWH.get(request.region, _GRID_EF_FALLBACK)

    messages = [{"role": "user", "content": request.description}]
    df_result        = None
    policy_name_used = "Custom Policy"

    for _ in range(3):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "run_sisepuede_transformation":
                    policy_name_used = block.input.get("policy_name", "Custom Policy")
                    try:
                        transformation = trf.Transformation(block.input["policy_config"], transformers)
                        df_result = transformation()
                    except Exception as exc:
                        raise HTTPException(status_code=500, detail=str(exc))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps({"success": True, "policy_name": policy_name_used}),
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            raise HTTPException(status_code=500, detail=f"Unexpected stop reason: {response.stop_reason}")

    if df_result is None:
        raise HTTPException(status_code=500, detail="LLM did not produce a transformation result")

    n = len(df_input)
    years   = [2015 + round(i * 35 / max(n - 1, 1)) for i in range(n)]
    gas_ef  = gas_ef_lookups[gas]

    # Baseline uses baseline eff_lookup; policy uses eff from df_result so
    # efficiency-improvement transformers (which change fuelefficiency_trns_*
    # columns) are correctly captured. For fuel-switch policies the transformer
    # does not touch efficiency columns so eff_lookup_policy == eff_lookup.
    eff_lookup_policy = build_efficiency_lookup(df_result, modes, fuels)

    bl_s1 = _scope1_by_mode(df_input,  proxies, gas_ef, eff_lookup,        modes, fuels)
    bl_s2 = _scope2_by_mode(df_input,  proxies, eff_lookup,        modes, grid_ef) if gas == "co2" else {m: [0.0]*n for m in modes}
    bl_s3 = _scope3_by_mode(df_input,  proxies, eff_lookup,        modes, fuels)   if gas == "co2" else {m: [0.0]*n for m in modes}

    po_s1 = _scope1_by_mode(df_result, proxies, gas_ef, eff_lookup_policy,  modes, fuels)
    po_s2 = _scope2_by_mode(df_result, proxies, eff_lookup_policy, modes, grid_ef) if gas == "co2" else {m: [0.0]*n for m in modes}
    po_s3 = _scope3_by_mode(df_result, proxies, eff_lookup_policy, modes, fuels)   if gas == "co2" else {m: [0.0]*n for m in modes}

    def _total(by_mode):
        arr = np.zeros(n)
        for v in by_mode.values():
            arr += np.array(v)
        return [round(float(x), 6) for x in arr]

    bl_scope = {"scope1": _total(bl_s1), "scope2": _total(bl_s2), "scope3": _total(bl_s3)}
    po_scope = {"scope1": _total(po_s1), "scope2": _total(po_s2), "scope3": _total(po_s3)}
    bl_total = [a + b + c for a, b, c in zip(bl_scope["scope1"], bl_scope["scope2"], bl_scope["scope3"])]
    po_total = [a + b + c for a, b, c in zip(po_scope["scope1"], po_scope["scope2"], po_scope["scope3"])]

    final_bl  = bl_total[-1]
    final_po  = po_total[-1]
    reduction = final_bl - final_po
    if final_bl > 0.01:                                    # meaningful baseline (> 10 kg)
        raw_pct = 100.0 * reduction / final_bl
        pct = round(max(-100.0, min(100.0, raw_pct)), 1)  # cap at ±100 %
    else:
        pct = 0.0

    # Final-year per-mode breakdown for the delta cards
    def _final_by_mode(by_mode_bl, by_mode_po):
        result = {}
        for m in modes:
            bl_val = float(by_mode_bl.get(m, [0])[-1])
            po_val = float(by_mode_po.get(m, [0])[-1])
            if bl_val > 0 or po_val > 0:
                result[m] = {"baseline": round(bl_val, 6), "policy": round(po_val, 6)}
        return result

    # Column-level diff: which SISEPUEDE parameters changed?
    # Map each column to the scope(s) it feeds into
    def _col_scopes(col):
        if col.startswith("frac_trns_fuelmix_"):
            if col.endswith("_electricity"):
                return ["scope2"]
            return ["scope1", "scope3"]
        if col.startswith("elecfuelefficiency_trns_"):
            return ["scope2"]
        if col.startswith("fuelefficiency_trns_"):
            return ["scope1", "scope3"]
        if col.startswith("ef_trns_mobile_combustion_"):
            return ["scope1"]
        if col.startswith("demandscalar_trns_") or col.startswith("modeshift_trns_"):
            return ["scope1", "scope2", "scope3"]
        return ["scope1", "scope2", "scope3"]

    def _col_type(col):
        if col.startswith("frac_trns_fuelmix_"):        return "Fuel Mix Fraction"
        if col.startswith("elecfuelefficiency_trns_"):  return "Electric Efficiency"
        if col.startswith("fuelefficiency_trns_"):      return "Fuel Efficiency"
        if col.startswith("ef_trns_mobile_combustion_"):return "Emission Factor"
        if col.startswith("demandscalar_trns_"):        return "Demand Scalar"
        if col.startswith("modeshift_trns_"):           return "Mode Shift"
        return "Other"

    changed_cols = []
    for col in df_input.columns:
        if col not in df_result.columns:
            continue
        if not np.issubdtype(df_input[col].dtype, np.number):
            continue
        bl_vals = df_input[col].fillna(0.0).values.astype(float)
        po_vals = df_result[col].fillna(0.0).values.astype(float)
        if np.allclose(bl_vals, po_vals, atol=1e-10, rtol=1e-8):
            continue
        bl_mean  = float(np.mean(bl_vals))
        po_mean  = float(np.mean(po_vals))
        bl_final = float(bl_vals[-1])
        po_final = float(po_vals[-1])
        pct      = (po_mean - bl_mean) / max(abs(bl_mean), 1e-9) * 100
        changed_cols.append({
            "col":        col,
            "col_type":   _col_type(col),
            "scopes":     _col_scopes(col),
            "bl_mean":    round(bl_mean,  6),
            "po_mean":    round(po_mean,  6),
            "bl_final":   round(bl_final, 6),
            "po_final":   round(po_final, 6),
            "pct_change": round(pct, 1),
        })
    changed_cols.sort(key=lambda x: (x["col_type"], x["col"]))

    return {
        "policy_name":             policy_name_used,
        "region":                  request.region,
        "gas":                     gas,
        "years":                   years,
        "baseline":                bl_scope,
        "policy":                  po_scope,
        "baseline_total":          bl_total,
        "policy_total":            po_total,
        "final_reduction_pct":     round(pct, 1),
        "final_reduction_tonnes":  round(reduction, 4),
        "by_mode": {
            "scope1": _final_by_mode(bl_s1, po_s1),
            "scope2": _final_by_mode(bl_s2, po_s2),
            "scope3": _final_by_mode(bl_s3, po_s3),
        },
        "changed_cols": changed_cols,
    }


# ---------------------------------------------------------------------------
# Standard policies for Net Zero Plan ( direct SISEPUEDE runs)
# ---------------------------------------------------------------------------
STANDARD_POLICIES = [
    {
        "id": "heavy_freight_elec",
        "name": "Electrify Heavy Freight",
        "sector": "Fuel Switch",
        "description": "70% of heavy-duty trucks switch diesel→electric by 2040",
        "default_capex_per_tco2": 120,
        "default_opex_per_tco2": 18,
        "policy_config": {
            "identifiers": {"transformation_code": "TX:TRNS:HEAVY_ELEC", "transformation_name": "Electrify Heavy Freight"},
            "transformer": "TFR:TRNS:SHIFT_FUEL_MEDIUM_DUTY",
            "parameters": {
                "categories": ["road_heavy_freight", "road_heavy_regional"],
                "fuels_source": ["fuel_diesel"],
                "dict_allocation_fuels_target": {"fuel_electricity": 1.0},
                "magnitude": 0.7,
                "vec_implementation_ramp": {"alpha_logistic": 0.0, "n_tp_ramp": None, "tp_0_ramp": 10, "window_logistic": [-8, 8]},
            },
        },
    },
    {
        "id": "light_vehicles_elec",
        "name": "Electrify Light Vehicles",
        "sector": "Fuel Switch",
        "description": "70% of private cars switch gasoline→electric by 2040",
        "default_capex_per_tco2": 95,
        "default_opex_per_tco2": 10,
        "policy_config": {
            "identifiers": {"transformation_code": "TX:TRNS:LIGHT_ELEC", "transformation_name": "Electrify Light Vehicles"},
            "transformer": "TFR:TRNS:SHIFT_FUEL_LIGHT_DUTY",
            "parameters": {
                "dict_allocation_fuels_target": {"fuel_electricity": 1.0},
                "magnitude": 0.7,
                "vec_implementation_ramp": {"alpha_logistic": 0.0, "n_tp_ramp": None, "tp_0_ramp": 10, "window_logistic": [-8, 8]},
            },
        },
    },
    {
        "id": "rail_electrification",
        "name": "Electrify Rail",
        "sector": "Fuel Switch",
        "description": "80% of rail operations switch to electricity by 2035",
        "default_capex_per_tco2": 60,
        "default_opex_per_tco2": 8,
        "policy_config": {
            "identifiers": {"transformation_code": "TX:TRNS:RAIL_ELEC", "transformation_name": "Electrify Rail"},
            "transformer": "TFR:TRNS:SHIFT_FUEL_RAIL",
            "parameters": {
                "dict_allocation_fuels_target": {"fuel_electricity": 1.0},
                "magnitude": 0.8,
                "vec_implementation_ramp": {"alpha_logistic": 0.0, "n_tp_ramp": None, "tp_0_ramp": 5, "window_logistic": [-8, 8]},
            },
        },
    },
    {
        "id": "maritime_hydrogen",
        "name": "Maritime to Hydrogen",
        "sector": "Fuel Switch",
        "description": "60% of maritime shipping switches diesel→hydrogen by 2045",
        "default_capex_per_tco2": 180,
        "default_opex_per_tco2": 35,
        "policy_config": {
            "identifiers": {"transformation_code": "TX:TRNS:MARITIME_H2", "transformation_name": "Maritime to Hydrogen"},
            "transformer": "TFR:TRNS:SHIFT_FUEL_MARITIME",
            "parameters": {
                "dict_allocation_fuels_target": {"fuel_hydrogen": 1.0},
                "magnitude": 0.6,
                "vec_implementation_ramp": {"alpha_logistic": 0.0, "n_tp_ramp": None, "tp_0_ramp": 15, "window_logistic": [-8, 8]},
            },
        },
    },
    {
        "id": "efficiency_improvement",
        "name": "Improve Fuel Efficiency",
        "sector": "Efficiency",
        "description": "25% efficiency improvement across all non-electric modes by 2035",
        "default_capex_per_tco2": 40,
        "default_opex_per_tco2": 5,
        "policy_config": {
            "identifiers": {"transformation_code": "TX:TRNS:EFF_NONELEC", "transformation_name": "Improve Fuel Efficiency"},
            "transformer": "TFR:TRNS:INC_EFFICIENCY_NON_ELECTRIC",
            "parameters": {
                "categories": ["road_heavy_freight", "road_heavy_regional", "road_light", "public"],
                "fuels": ["fuel_diesel", "fuel_gasoline"],
                "magnitude": 0.25,
                "vec_implementation_ramp": {"alpha_logistic": 0.0, "n_tp_ramp": None, "tp_0_ramp": 5, "window_logistic": [-8, 8]},
            },
        },
    },
    {
        "id": "freight_mode_shift",
        "name": "Freight Modal Shift to Rail",
        "sector": "Mode Shift",
        "description": "50% of road freight shifts to rail by 2035",
        "default_capex_per_tco2": 55,
        "default_opex_per_tco2": 12,
        "policy_config": {
            "identifiers": {"transformation_code": "TX:TRNS:FREIGHT_RAIL", "transformation_name": "Freight Modal Shift to Rail"},
            "transformer": "TFR:TRNS:SHIFT_MODE_FREIGHT",
            "parameters": {
                "magnitude": 0.5,
                "vec_implementation_ramp": {"alpha_logistic": 0.0, "n_tp_ramp": None, "tp_0_ramp": 5, "window_logistic": [-8, 8]},
            },
        },
    },
    {
        "id": "passenger_mode_shift",
        "name": "Passenger Modal Shift",
        "sector": "Mode Shift",
        "description": "40% of private car trips shift to transit/cycling by 2035",
        "default_capex_per_tco2": 35,
        "default_opex_per_tco2": 8,
        "policy_config": {
            "identifiers": {"transformation_code": "TX:TRNS:PASS_SHIFT", "transformation_name": "Passenger Modal Shift"},
            "transformer": "TFR:TRNS:SHIFT_MODE_PASSENGER",
            "parameters": {
                "magnitude": 0.4,
                "vec_implementation_ramp": {"alpha_logistic": 0.0, "n_tp_ramp": None, "tp_0_ramp": 5, "window_logistic": [-8, 8]},
            },
        },
    },
    {
        "id": "demand_reduction",
        "name": "Demand Reduction",
        "sector": "Demand",
        "description": "20% total demand reduction via remote work & trip consolidation by 2030",
        "default_capex_per_tco2": 15,
        "default_opex_per_tco2": 3,
        "policy_config": {
            "identifiers": {"transformation_code": "TX:TRDE:DEC_DEMAND", "transformation_name": "Demand Reduction"},
            "transformer": "TFR:TRDE:DEC_DEMAND",
            "parameters": {
                "magnitude": 0.2,
                "vec_implementation_ramp": {"alpha_logistic": 0.0, "n_tp_ramp": None, "tp_0_ramp": 5, "window_logistic": [-8, 8]},
            },
        },
    },
]


def _compute_policy_abatement(region: str, gas: str, policy_cfg: dict) -> dict:
    """Run one policy directly through SISEPUEDE and return abatement data (no LLM)."""
    bl = BASELINES.get(region, BASELINES["costa_rica"])
    df_input     = bl["df"]
    transformers = bl["transformers"]
    modes        = bl["modes"]
    fuels        = bl["fuels"]
    proxies      = bl["proxies"]
    eff_lookup   = bl["eff_lookup"]
    gas_ef_lookups = bl["gas_ef_lookups"]

    gas_ef  = gas_ef_lookups.get(gas, gas_ef_lookups["co2"])
    grid_ef = GRID_EF_KG_CO2_PER_KWH.get(region, _GRID_EF_FALLBACK)
    n = len(df_input)

    frozen_gas_ef = {k: np.full(n, float(v[0])) for k, v in gas_ef.items()}

    transformation = trf.Transformation(policy_cfg, transformers)
    df_result = transformation()

    bl_s1 = _scope1_by_mode(df_input,  proxies, frozen_gas_ef, eff_lookup, modes, fuels)
    bl_s2 = _scope2_by_mode(df_input,  proxies, eff_lookup,    modes, grid_ef) if gas == "co2" else {m: [0.0]*n for m in modes}
    bl_s3 = _scope3_by_mode(df_input,  proxies, eff_lookup,    modes, fuels)   if gas == "co2" else {m: [0.0]*n for m in modes}

    po_s1 = _scope1_by_mode(df_result, proxies, gas_ef,     eff_lookup, modes, fuels)
    po_s2 = _scope2_by_mode(df_result, proxies, eff_lookup, modes, grid_ef) if gas == "co2" else {m: [0.0]*n for m in modes}
    po_s3 = _scope3_by_mode(df_result, proxies, eff_lookup, modes, fuels)   if gas == "co2" else {m: [0.0]*n for m in modes}

    def _total(by_mode):
        arr = np.zeros(n)
        for v in by_mode.values():
            arr += np.array(v)
        return arr

    bl_arr = _total(bl_s1) + _total(bl_s2) + _total(bl_s3)
    po_arr = _total(po_s1) + _total(po_s2) + _total(po_s3)
    abatement = bl_arr - po_arr

    years = [2015 + round(i * 35 / max(n - 1, 1)) for i in range(n)]
    final_bl = float(bl_arr[-1])
    final_ab = float(abatement[-1])
    pct = round(max(-100.0, min(100.0, 100.0 * final_ab / final_bl)), 1) if final_bl > 0.01 else 0.0

    return {
        "years":                  years,
        "baseline":               [round(float(x), 6) for x in bl_arr],
        "policy":                 [round(float(x), 6) for x in po_arr],
        "abatement":              [round(float(x), 6) for x in abatement],
        "final_abatement_tonnes": round(final_ab, 4),
        "final_baseline_tonnes":  round(final_bl, 4),
        "final_reduction_pct":    pct,
    }


class BatchPoliciesRequest(BaseModel):
    region: str = "costa_rica"
    gas: str = "co2"


@app.get("/api/standard-policies")
async def get_standard_policies():
    return {
        "policies": [
            {k: v for k, v in p.items() if k != "policy_config"}
            for p in STANDARD_POLICIES
        ]
    }


@app.post("/api/run-batch-policies")
async def run_batch_policies(request: BatchPoliciesRequest):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    bl = BASELINES.get(request.region, BASELINES["costa_rica"])
    if request.gas not in bl["available_gases"]:
        raise HTTPException(status_code=400, detail=f"Gas '{request.gas}' not available for region '{request.region}'")

    results = {}
    errors  = {}

    def run_one(policy):
        try:
            return policy["id"], _compute_policy_abatement(request.region, request.gas, policy["policy_config"])
        except Exception as exc:
            return policy["id"], {"error": str(exc)}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(run_one, p): p["id"] for p in STANDARD_POLICIES}
        for future in as_completed(futures):
            pid, outcome = future.result()
            if "error" in outcome:
                errors[pid] = outcome["error"]
            else:
                results[pid] = outcome

    years = [2015 + round(i * 35 / max(len(bl["df"]) - 1, 1)) for i in range(len(bl["df"]))]

    return {
        "region":  request.region,
        "gas":     request.gas,
        "years":   years,
        "results": results,
        "errors":  errors,
    }


@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "regions": list(BASELINES.keys()),
        "costa_rica": {
            "shape":  BASELINES["costa_rica"]["df"].shape,
            "gases":  BASELINES["costa_rica"]["available_gases"],
            "modes":  BASELINES["costa_rica"]["modes"],
            "fuels":  BASELINES["costa_rica"]["fuels"],
        },
        "mexico": {
            "shape":  BASELINES["mexico"]["df"].shape,
            "gases":  BASELINES["mexico"]["available_gases"],
            "modes":  BASELINES["mexico"]["modes"],
            "fuels":  BASELINES["mexico"]["fuels"],
        },
    }


# ---------------------------------------------------------------------------
# Scope 1 / 2 / 3 emissions endpoint
# ---------------------------------------------------------------------------

# Scope 2: grid emission factors (kg CO2/kWh) — country-specific constants.
# Costa Rica grid is ~98% renewable; Mexico grid is fossil-heavy.
GRID_EF_KG_CO2_PER_KWH = {
    "costa_rica": 0.020,
    "mexico":     0.454,
}
_GRID_EF_FALLBACK = 0.400

# Scope 3: upstream fuel-chain emission multipliers (fraction of Scope 1 fuel volume).
# Source: IEA upstream emission intensities, approximate.
UPSTREAM_MULTIPLIER = {
    "diesel":                  0.20,
    "gasoline":                0.22,
    "kerosene":                0.18,
    "natural_gas":             0.15,
    "biofuels":                0.05,
    "hydrocarbon_gas_liquids": 0.18,
    "hydrogen":                0.30,  # grey/blue H2 production
    "electricity":             0.05,
    "ammonia":                 0.25,
}


def _scope1_by_mode(df: pd.DataFrame, proxies: dict, gas_ef_lookup: dict,
                    eff_lookup: dict, modes: list, fuels: list) -> dict:
    """Scope 1: direct combustion emissions per mode (excludes electricity)."""
    n = len(df)
    result = {}
    for mode in modes:
        proxy = proxies.get(mode, np.ones(n) * 100)
        em = np.zeros(n)
        for fuel in fuels:
            if fuel == "electricity":
                continue
            frac_col = f"frac_trns_fuelmix_{mode}_{fuel}"
            if frac_col not in df.columns:
                continue
            frac   = df[frac_col].fillna(0.0).values
            eff    = eff_lookup.get((mode, fuel), np.full(n, 3.0))
            ed     = ED_TJ_PER_LITRE.get(fuel, _ED_FALLBACK)
            gas_ef = gas_ef_lookup.get((mode, fuel), np.zeros(n))
            em    += frac * (proxy / np.maximum(eff, 1e-9)) * ed * gas_ef
        result[mode] = (em / 1e3).tolist()
    return result


def _scope2_by_mode(df: pd.DataFrame, proxies: dict, eff_lookup: dict,
                    modes: list, grid_ef: float) -> dict:
    """Scope 2: indirect emissions from purchased electricity per mode."""
    n = len(df)
    result = {}
    for mode in modes:
        proxy    = proxies.get(mode, np.ones(n) * 100)
        frac_col = f"frac_trns_fuelmix_{mode}_electricity"
        if frac_col not in df.columns:
            result[mode] = [0.0] * n
            continue
        frac = df[frac_col].fillna(0.0).values
        eff  = eff_lookup.get((mode, "electricity"), np.full(n, 5.0))
        # kWh consumed = frac * proxy / eff_kwh_per_km  (proxy/eff = km, *1/eff = kWh)
        # But our eff is km/kWh, so energy = frac * proxy / eff  (kWh units)
        kwh  = frac * (proxy / np.maximum(eff, 1e-9))
        em   = kwh * grid_ef / 1e3   # kg → tonnes
        result[mode] = em.tolist()
    return result


def _scope3_by_mode(df: pd.DataFrame, proxies: dict,
                    eff_lookup: dict, modes: list, fuels: list) -> dict:
    """Scope 3: upstream fuel-chain emissions (multiplier on Scope 1 fuel volumes)."""
    n = len(df)
    result = {}
    for mode in modes:
        proxy = proxies.get(mode, np.ones(n) * 100)
        em = np.zeros(n)
        for fuel in fuels:
            frac_col = f"frac_trns_fuelmix_{mode}_{fuel}"
            if frac_col not in df.columns:
                continue
            frac = df[frac_col].fillna(0.0).values
            eff  = eff_lookup.get((mode, fuel), np.full(n, 3.0))
            mult = UPSTREAM_MULTIPLIER.get(fuel, 0.15)
            # Upstream CO2 proportional to fuel volume consumed × upstream intensity
            # Use CO2 factor for non-electricity fuels as proxy for upstream carbon
            co2_ef = CO2_KG_PER_TJ.get(fuel, 0.0)
            ed     = ED_TJ_PER_LITRE.get(fuel, _ED_FALLBACK)
            em    += frac * (proxy / np.maximum(eff, 1e-9)) * ed * co2_ef * mult
        result[mode] = (em / 1e3).tolist()
    return result


@app.get("/api/scope-emissions")
async def get_scope_emissions(region: str = "costa_rica", gas: str = "co2"):
    bl = BASELINES.get(region, BASELINES["costa_rica"])
    df         = bl["df"]
    modes      = bl["modes"]
    fuels      = bl["fuels"]
    proxies    = bl["proxies"]
    eff_lookup = bl["eff_lookup"]
    gas_ef_lookups = bl["gas_ef_lookups"]
    n = len(df)

    if gas not in bl["available_gases"]:
        raise HTTPException(status_code=400, detail=f"Gas '{gas}' not available for region '{region}'")

    gas_ef    = gas_ef_lookups[gas]
    grid_ef   = GRID_EF_KG_CO2_PER_KWH.get(region, _GRID_EF_FALLBACK)
    years     = [2015 + round(i * 35 / max(n - 1, 1)) for i in range(n)]

    # Freeze at t=0 (same as baseline line in policy view)
    frozen_eff    = {k: np.full(n, float(v[0])) for k, v in eff_lookup.items()}
    frozen_gas_ef = {k: np.full(n, float(v[0])) for k, v in gas_ef.items()}

    s1_by_mode = _scope1_by_mode(df, proxies, frozen_gas_ef, frozen_eff, modes, fuels)
    s2_by_mode = _scope2_by_mode(df, proxies, frozen_eff, modes, grid_ef) if gas == "co2" else {m: [0.0]*n for m in modes}
    s3_by_mode = _scope3_by_mode(df, proxies, frozen_eff, modes, fuels)   if gas == "co2" else {m: [0.0]*n for m in modes}

    def _total(by_mode):
        arr = np.zeros(n)
        for v in by_mode.values():
            arr += np.array(v)
        return [round(float(x), 6) for x in arr]

    def _round_mode(by_mode):
        return {m: [round(float(x), 6) for x in v] for m, v in by_mode.items()}

    return {
        "region":  region,
        "gas":     gas,
        "years":   years,
        "scope1":  {"total": _total(s1_by_mode),  "by_mode": _round_mode(s1_by_mode)},
        "scope2":  {"total": _total(s2_by_mode),  "by_mode": _round_mode(s2_by_mode)},
        "scope3":  {"total": _total(s3_by_mode),  "by_mode": _round_mode(s3_by_mode)},
        "grand_total": [round(float(a+b+c), 6) for a, b, c in zip(
            _total(s1_by_mode), _total(s2_by_mode), _total(s3_by_mode)
        )],
        "mode_labels": {
            "road_heavy_freight":  "Heavy Freight",
            "road_heavy_regional": "Regional Heavy",
            "road_light":          "Light Duty",
            "public":              "Public Transport",
            "rail_freight":        "Rail Freight",
            "rail_passenger":      "Rail Passenger",
            "aviation":            "Aviation",
            "water_borne":         "Maritime",
            "powered_bikes":       "Powered Bikes",
        },
    }

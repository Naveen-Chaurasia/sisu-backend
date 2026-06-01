"""
api_mines.py — FastAPI router for Critical Minerals Investment Modeling.
Mount at /mines in start.py.

DCF engine ports the Excel Calcs sheet exactly:
  Production → Revenue → Opex → EBITDA → Depreciation → Interest →
  EBIT → Tax → Net Income → Capex → FCF → Discounted CF
  → NPV / IRR / MOIC / Payback
"""
import copy
import math
import random
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from mines.mine_data_supabase import get_all_mines, get_mine, upsert_mine, _summary

# Static reference table — not stored in DB
MINERALS_REFERENCE = [
    {"mineral": "Rare Earths",  "market_price": 220,    "price_unit": "$/kg", "avg_ore_grade": 2.0,  "grade_unit": "%",     "avg_recovery_rate": 70},
    {"mineral": "Lithium",      "market_price": 10000,  "price_unit": "$/t",  "avg_ore_grade": 2.0,  "grade_unit": "%",     "avg_recovery_rate": 85},
    {"mineral": "Spodumene",    "market_price": 850,    "price_unit": "$/t",  "avg_ore_grade": 2.0,  "grade_unit": "%",     "avg_recovery_rate": 70},
    {"mineral": "Lepidolite",   "market_price": 400,    "price_unit": "$/t",  "avg_ore_grade": 1.5,  "grade_unit": "%",     "avg_recovery_rate": 60},
    {"mineral": "Copper",       "market_price": 9000,   "price_unit": "$/t",  "avg_ore_grade": 0.08, "grade_unit": "%",     "avg_recovery_rate": 80},
    {"mineral": "Graphite",     "market_price": 500,    "price_unit": "$/t",  "avg_ore_grade": 10.0, "grade_unit": "%",     "avg_recovery_rate": 80},
    {"mineral": "Monazite",     "market_price": 1000,   "price_unit": "$/t",  "avg_ore_grade": 0.5,  "grade_unit": "%",     "avg_recovery_rate": 70},
    {"mineral": "Gold",         "market_price": 3200,   "price_unit": "$/oz", "avg_ore_grade": 0.01, "grade_unit": "%",     "avg_recovery_rate": 90},
    {"mineral": "Tantalite",    "market_price": 150,    "price_unit": "$/lb", "avg_ore_grade": 0.025,"grade_unit": "%",     "avg_recovery_rate": 65},
]

app = FastAPI(title="Mines Investment Modeling API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── IRR (pure Python Newton-Raphson, no numpy_financial needed) ──────────────

def _npv_at_rate(rate: float, cashflows: list) -> float:
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))

def _irr(cashflows: list, guess: float = 0.1, tol: float = 1e-7, maxiter: int = 1000) -> Optional[float]:
    if not any(cf < 0 for cf in cashflows) or not any(cf > 0 for cf in cashflows):
        return None
    r = guess
    for _ in range(maxiter):
        # clamp r so (1+r)^t never overflows
        r = max(-0.999, min(r, 100.0))
        try:
            f  = _npv_at_rate(r, cashflows)
            df = sum(-t * cf / (1 + r) ** (t + 1) for t, cf in enumerate(cashflows))
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(df) < 1e-15:
            break
        r2 = r - f / df
        if abs(r2 - r) < tol:
            return round(r2, 6)
        r = r2
    return round(r, 6)


# ── Core DCF engine ───────────────────────────────────────────────────────────

def _normalize_mine(mine: dict) -> dict:
    """
    Map v1_mines field names → legacy run_dcf field names so the engine
    works with both old synthetic data and new Supabase rows.
    Also synthesizes the 'minerals' list from flat v1_mines commodity fields.
    """
    m = dict(mine)
    # Field aliases (v1_mines → legacy)
    if "ore_reserve"   in m and "total_ore_reserve"      not in m:
        m["total_ore_reserve"]       = m["ore_reserve"]
    if "throughput_pa" in m and "steady_state_throughput" not in m:
        m["steady_state_throughput"] = m["throughput_pa"]
    if "tax_rate"      in m and "corp_income_tax_rate"    not in m:
        m["corp_income_tax_rate"]    = m["tax_rate"]

    # Synthesize minerals list from flat fields if absent
    if not m.get("minerals") and m.get("commodity"):
        m["minerals"] = [{
            "name":  m.get("commodity", "Unknown"),
            "grade": float(m.get("grade") or 0),
            "price": float(m.get("price_base") or 0),
        }]
    return m


def run_dcf(mine: dict) -> dict:
    """
    Compute full year-by-year DCF table and summary metrics.
    Returns dict with 'years' list and 'rows' dict (row_name → list of values).
    Accepts both v1_mines (Supabase) and legacy synthetic mine dicts.
    """
    mine         = _normalize_mine(mine)
    ore_reserve  = mine.get("total_ore_reserve") or 0
    throughput   = mine.get("steady_state_throughput") or 1
    lom          = max(1, round(ore_reserve / throughput)) if ore_reserve and throughput else mine.get("life_of_mine_yr", 15)
    minerals     = mine.get("minerals", [])
    capex        = mine.get("initial_dev_capex", 0)
    opex_ss      = mine.get("total_opex_steady_state", 0)
    opex_esc     = mine.get("opex_escalation_rate", 0.0)
    depr_life    = mine.get("avg_depreciation_years", 15)
    ramp1        = mine.get("ramp_up_y1", 0.40)
    ramp2        = mine.get("ramp_up_y2", 0.75)
    tax_rate     = mine.get("corp_income_tax_rate", 0.32)
    royalty_rate = mine.get("royalty_rate", 0.06)
    debt         = mine.get("debt_funding", 0.0)
    debt_term    = mine.get("debt_term", 10)
    interest_rt  = mine.get("interest_rate", 0.08)
    wacc         = mine.get("wacc", 0.14)
    closure      = mine.get("closure_rehab_cost", 0.0)

    # Year 0 = construction/pre-production; Years 1..LOM = production
    total_years = lom + 1  # 0 to lom inclusive
    years = list(range(total_years))

    rows: Dict[str, List[float]] = {k: [0.0] * total_years for k in [
        "ramp_factor",
        "ore_mined",
        "cumulative_ore_mined",
        "remaining_reserve",
        "gross_revenue",
        "royalties",
        "net_revenue",
        "opex_escalation_factor",
        "operating_costs",
        "ebitda",
        "ebitda_margin",
        "depreciation",
        "interest_expense",
        "ebit",
        "income_tax",
        "net_income",
        "initial_capex",
        "closure_cost",
        "total_capex",
        "fcf",
        "cumulative_cf",
        "discount_factor",
        "discounted_cf",
    ]}

    # Per-mineral rows
    for i, mn in enumerate(minerals):
        rows[f"mineral_{i}_production"] = [0.0] * total_years
        rows[f"mineral_{i}_revenue"]    = [0.0] * total_years

    cum_ore = 0.0
    esc_factor = 1.0

    for t in years:
        # ── Ramp-up ───────────────────────────────────────────────────────────
        if t == 0:
            rf = 0.0
        elif t == 1:
            rf = ramp1
        elif t == 2:
            rf = ramp2
        else:
            rf = 1.0
        rows["ramp_factor"][t] = rf

        ore = throughput * rf
        rows["ore_mined"][t] = ore
        cum_ore += ore
        rows["cumulative_ore_mined"][t] = cum_ore
        rows["remaining_reserve"][t]    = max(0, ore_reserve - cum_ore)

        # ── Revenue ───────────────────────────────────────────────────────────
        gross_rev = 0.0
        for i, mn in enumerate(minerals):
            grade = mn.get("grade", 0)
            price = mn.get("price", 0)
            prod  = (ore * grade) / 1000.0
            rev   = prod * price
            rows[f"mineral_{i}_production"][t] = round(prod, 6)
            rows[f"mineral_{i}_revenue"][t]    = round(rev, 2)
            gross_rev += rev

        rows["gross_revenue"][t] = round(gross_rev, 2)
        royalties = 0.0 if t == 0 else -gross_rev * royalty_rate
        rows["royalties"][t]    = round(royalties, 2)
        rows["net_revenue"][t]  = round(gross_rev + royalties, 2)

        # ── Opex ──────────────────────────────────────────────────────────────
        if t == 0:
            esc_factor = 1.0
            opex = 0.0
        else:
            if t > 1:
                esc_factor *= (1 + opex_esc)
            opex = -opex_ss * esc_factor
        rows["opex_escalation_factor"][t] = round(esc_factor, 6)
        rows["operating_costs"][t]        = round(opex, 2)

        # ── EBITDA ────────────────────────────────────────────────────────────
        ebitda = rows["net_revenue"][t] + opex
        rows["ebitda"][t] = round(ebitda, 2)
        margin = ebitda / gross_rev if gross_rev > 0 else 0.0
        rows["ebitda_margin"][t] = round(margin, 6)

        # ── Below-EBITDA ──────────────────────────────────────────────────────
        depr = (-capex / depr_life) if (1 <= t <= depr_life) else 0.0
        interest = (-debt * interest_rt) if (1 <= t <= debt_term) else 0.0
        rows["depreciation"][t]     = round(depr, 2)
        rows["interest_expense"][t] = round(interest, 2)

        # ── Tax ───────────────────────────────────────────────────────────────
        ebit = ebitda + depr
        rows["ebit"][t] = round(ebit, 2)
        tax = (-ebit * tax_rate) if ebit > 0 else 0.0
        rows["income_tax"][t] = round(tax, 2)
        rows["net_income"][t] = round(ebit + tax, 2)

        # ── Capex ─────────────────────────────────────────────────────────────
        init_cap  = -capex if t == 0 else 0.0
        clos_cost = -closure if t == lom else 0.0
        tot_cap   = init_cap + clos_cost
        rows["initial_capex"][t] = round(init_cap, 2)
        rows["closure_cost"][t]  = round(clos_cost, 2)
        rows["total_capex"][t]   = round(tot_cap, 2)

        # ── FCF ───────────────────────────────────────────────────────────────
        fcf = ebitda + tax + tot_cap
        rows["fcf"][t] = round(fcf, 2)

    # ── Cumulative CF, Discounting ─────────────────────────────────────────────
    cum = 0.0
    for t in years:
        cum += rows["fcf"][t]
        rows["cumulative_cf"][t]   = round(cum, 2)
        disc = 1.0 / (1 + wacc) ** t
        rows["discount_factor"][t] = round(disc, 8)
        rows["discounted_cf"][t]   = round(rows["fcf"][t] * disc, 2)

    # ── Summary Metrics ────────────────────────────────────────────────────────
    npv = round(sum(rows["discounted_cf"]), 2)
    irr = _irr(rows["fcf"])
    if irr is not None:
        irr = round(irr, 4)

    # Payback: first year cumulative CF >= 0
    payback = None
    for t in years[1:]:
        if rows["cumulative_cf"][t] >= 0:
            payback = t
            break

    # MOIC = sum(net_income Y1:YN) / abs(sum(total_capex Y0:YN))
    total_ni  = sum(rows["net_income"][1:])
    total_cap_abs = abs(sum(rows["total_capex"]))
    moic = round(total_ni / total_cap_abs, 2) if total_cap_abs > 0 else None

    total_revenue = round(sum(rows["gross_revenue"]), 2)
    total_fcf_val = round(sum(rows["fcf"]), 2)

    # Total mineral produced (primary mineral, kg)
    total_minerals = 0.0
    for i in range(len(minerals)):
        total_minerals += sum(rows.get(f"mineral_{i}_production", []))
    total_minerals = round(total_minerals, 4)

    # AISC
    total_opex_abs = abs(sum(rows["operating_costs"]))
    aisc = round(total_opex_abs / total_minerals, 2) if total_minerals > 0 else None

    # Unit margin (primary mineral)
    if minerals:
        mn0   = minerals[0]
        price = mn0.get("price", 0)
        vcost = total_opex_abs / max(total_minerals, 1e-9)
        unit_margin_abs = price - vcost
        unit_margin_pct = unit_margin_abs / price if price > 0 else 0.0
    else:
        unit_margin_abs = unit_margin_pct = 0.0

    summary = {
        "npv":                        npv,
        "irr":                        irr,
        "payback_period":             payback,
        "moic":                       moic,
        "total_lom_revenue":          total_revenue,
        "total_lom_fcf":              total_fcf_val,
        "total_lom_minerals_produced":total_minerals,
        "aisc":                       aisc,
        "unit_margin_abs":            round(unit_margin_abs, 2),
        "unit_margin_pct":            round(unit_margin_pct, 4),
        "life_of_mine":               lom,
    }

    # Build per-year row list for the frontend DCF table
    dcf_rows = []
    for t in years:
        dcf_rows.append({
            "year":             t,
            "production_units": rows["ore_mined"][t],
            "production_value": rows["gross_revenue"][t],
            "revenue":          rows["net_revenue"][t],
            "royalty":          rows["royalties"][t],
            "opex":             rows["operating_costs"][t],
            "total_costs":      round(rows["royalties"][t] + rows["operating_costs"][t], 2),
            "ebitda":           rows["ebitda"][t],
            "ebitda_margin":    rows["ebitda_margin"][t],
            "depreciation":     rows["depreciation"][t],
            "interest":         rows["interest_expense"][t],
            "ebit":             rows["ebit"][t],
            "tax":              rows["income_tax"][t],
            "net_income":       rows["net_income"][t],
            "capex":            rows["total_capex"][t],
            "sustaining_capex": 0,
            "fcf":              rows["fcf"][t],
            "discounted_cf":    rows["discounted_cf"][t],
            "cumulative_npv":   rows["cumulative_cf"][t],
        })

    # Frontend-friendly summary (field names match what MineProfile valuation panel reads)
    fe_summary = {
        "npv":            npv,
        "irr":            irr,
        "payback_years":  payback,
        "moic":           moic,
        "aisc_per_unit":  aisc,
        "unit_margin":    round(unit_margin_abs, 2),
        "life_of_mine":   lom,
    }

    # Persist summary metrics back to v1_mines (best-effort — ignore errors)
    mine_id = mine.get("id")
    if mine_id:
        try:
            upsert_mine(mine_id, {
                "npv": npv, "irr": irr, "moic": moic,
            })
        except Exception:
            pass

    return {
        "mine_id":  mine["id"],
        "years":    years,
        "minerals": [mn.get("name", f"Mineral {i}") for i, mn in enumerate(minerals)],
        "rows":     rows,
        "dcf_rows": dcf_rows,
        "summary":  summary,
        "fe_summary": fe_summary,
    }


# ── Monte Carlo ───────────────────────────────────────────────────────────────

def run_monte_carlo(mine: dict, n_runs: int = 500,
                    variation: dict = None) -> dict:
    """
    Perturb 6 inputs and run DCF n_runs times.
    variation = {
      "throughput": 0.10,   # ± fraction
      "grade":      0.10,
      "price":      0.20,
      "capex":      {"low": -0.05, "high": 0.50},
      "opex":       0.15,
      "wacc":       0.05,   # absolute pp
    }
    """
    if variation is None:
        variation = {
            "throughput": 0.10,
            "grade":      0.10,
            "price":      0.20,
            "capex":      {"low": -0.05, "high": 0.50},
            "opex":       0.15,
            "wacc":       0.05,
        }

    results = []
    for _ in range(n_runs):
        m = copy.deepcopy(mine)
        # Throughput ± sym
        tp_v = variation.get("throughput", 0.10)
        m["steady_state_throughput"] *= (1 + random.uniform(-tp_v, tp_v))
        # Grade ± sym per mineral
        gr_v = variation.get("grade", 0.10)
        for mn in m.get("minerals", []):
            mn["grade"] *= (1 + random.uniform(-gr_v, gr_v))
        # Price ± sym per mineral
        pr_v = variation.get("price", 0.20)
        for mn in m.get("minerals", []):
            mn["price"] *= (1 + random.uniform(-pr_v, pr_v))
        # Capex asymmetric
        cap_v = variation.get("capex", {"low": -0.05, "high": 0.50})
        cap_lo = cap_v.get("low", -0.05) if isinstance(cap_v, dict) else -cap_v
        cap_hi = cap_v.get("high", 0.50) if isinstance(cap_v, dict) else cap_v
        m["initial_dev_capex"] *= (1 + random.uniform(cap_lo, cap_hi))
        # Opex ± sym
        op_v = variation.get("opex", 0.15)
        m["total_opex_steady_state"] *= (1 + random.uniform(-op_v, op_v))
        # WACC ± absolute pp
        wacc_v = variation.get("wacc", 0.05)
        m["wacc"] = max(0.01, m["wacc"] + random.uniform(-wacc_v, wacc_v))

        try:
            res = run_dcf(m)["summary"]
            results.append({
                "npv":            res["npv"],
                "irr":            res["irr"],
                "payback_period": res["payback_period"],
                "moic":           res["moic"],
                "unit_margin_pct":res["unit_margin_pct"],
                "total_lom_revenue": res["total_lom_revenue"],
                "total_lom_fcf":     res["total_lom_fcf"],
                "aisc":           res["aisc"],
            })
        except Exception:
            pass

    def _pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        idx = (len(s) - 1) * p / 100.0
        lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
        return round(s[lo] + (s[hi] - s[lo]) * (idx - lo), 2)

    def _stats(key):
        vals = [r[key] for r in results if r.get(key) is not None]
        if not vals:
            return {"mean": None, "median": None, "stdev": None, "min": None, "max": None,
                    "p10": None, "p50": None, "p90": None}
        n = len(vals)
        mean = sum(vals) / n
        sorted_v = sorted(vals)
        median = sorted_v[n // 2] if n % 2 else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2
        variance = sum((v - mean) ** 2 for v in vals) / n
        stdev = math.sqrt(variance)
        return {
            "mean":   round(mean,   2),
            "median": round(median, 2),
            "stdev":  round(stdev,  2),
            "min":    round(min(vals), 2),
            "max":    round(max(vals), 2),
            "p10":    _pct(vals, 10),
            "p50":    _pct(vals, 50),
            "p90":    _pct(vals, 90),
        }

    def _histogram(key, bins: int = 20):
        vals = [r[key] for r in results if r.get(key) is not None]
        if not vals:
            return []
        lo, hi = min(vals), max(vals)
        if lo == hi:
            return [{"bin_start": lo, "bin_end": hi, "count": len(vals)}]
        width = (hi - lo) / bins
        counts = [0] * bins
        for v in vals:
            idx = min(int((v - lo) / width), bins - 1)
            counts[idx] += 1
        return [
            {"bin_start": round(lo + i * width, 2),
             "bin_end":   round(lo + (i + 1) * width, 2),
             "count":     counts[i]}
            for i in range(bins)
        ]

    npv_s  = _stats("npv")
    irr_s  = _stats("irr")
    moic_s = _stats("moic")

    return {
        "n_runs":    len(results),
        # Flat stats with frontend-expected keys
        "stats": {
            "npv_mean":  npv_s["mean"],
            "npv_std":   npv_s["stdev"],
            "npv_p10":   npv_s["p10"],
            "npv_p50":   npv_s["p50"],
            "npv_p90":   npv_s["p90"],
            "irr_p50":   irr_s["p50"],
            "moic_p50":  moic_s["p50"],
        },
        # Raw scenarios for frontend histogram + scatter building
        "scenarios": [
            {"npv": r["npv"], "irr": r["irr"], "moic": r["moic"]}
            for r in results
            if r.get("npv") is not None
        ],
        # Nested stats for completeness
        "stats_nested": {
            "npv":            npv_s,
            "irr":            irr_s,
            "payback_period": _stats("payback_period"),
            "moic":           moic_s,
        },
        "histograms": {
            "npv":            _histogram("npv"),
            "irr":            _histogram("irr"),
        },
        "variation_used":  variation,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/mines/list")
def list_mines():
    return {"mines": get_all_mines()}


@app.get("/mines/minerals-reference")
def minerals_reference():
    return {"minerals": MINERALS_REFERENCE}


@app.get("/mines/{mine_id}")
def get_mine_profile(mine_id: str):
    m = get_mine(mine_id)
    if m is None:
        raise HTTPException(404, f"Mine '{mine_id}' not found")
    return m


class MineCreateRequest(BaseModel):
    mine_name: str
    mine_number: str = ""
    license_number: str = ""
    province: str = "Tete"
    mine_type: str = "Open Pit"
    primary_minerals: str = ""
    prospectivity_notes: str = ""
    total_ore_reserve: float = 0
    reserve_unit: str = "m³"
    steady_state_throughput: float = 0
    minerals: list = []
    initial_dev_capex: float = 0
    total_opex_steady_state: float = 0
    opex_escalation_rate: float = 0
    avg_depreciation_years: int = 15
    ramp_up_y1: float = 0.40
    ramp_up_y2: float = 0.75
    corp_income_tax_rate: float = 0.32
    royalty_rate: float = 0.06
    debt_funding: float = 0
    debt_term: int = 10
    interest_rate: float = 0.08
    wacc: float = 0.14
    closure_rehab_cost: float = 0
    risk_factors: list = []
    environmental_impacts: list = []
    notes: str = ""


@app.post("/mines/")
def create_mine(req: MineCreateRequest):
    data = req.dict()
    data.pop("id", None)           # let Supabase generate UUID
    data["country"] = "Mozambique"
    data["is_user_created"] = True
    new = upsert_mine(None, data)  # None → INSERT, returns saved row
    return new


@app.put("/mines/{mine_id}")
def update_mine(mine_id: str, req: MineCreateRequest):
    if get_mine(mine_id) is None:
        raise HTTPException(404, f"Mine '{mine_id}' not found")
    data = req.dict()
    data["country"] = "Mozambique"
    upsert_mine(mine_id, data)
    return get_mine(mine_id)


class CalcRequest(BaseModel):
    region: str = "mozambique"


@app.post("/mines/{mine_id}/calculate")
def calculate_mine(mine_id: str, req: CalcRequest = None):
    m = get_mine(mine_id)
    if m is None:
        raise HTTPException(404, f"Mine '{mine_id}' not found")
    result = run_dcf(m)
    return {"mine": get_mine(mine_id), "dcf": result}


class MonteCarloRequest(BaseModel):
    n_runs: int = 500
    variation: dict = {
        "throughput": 0.10,
        "grade":      0.10,
        "price":      0.20,
        "capex":      {"low": -0.05, "high": 0.50},
        "opex":       0.15,
        "wacc":       0.05,
    }


@app.post("/mines/{mine_id}/monte-carlo")
def monte_carlo(mine_id: str, req: MonteCarloRequest):
    m = get_mine(mine_id)
    if m is None:
        raise HTTPException(404, f"Mine '{mine_id}' not found")
    # Translate frontend variation keys to backend format
    v = req.variation or {}
    variation = {
        "throughput": v.get("throughput", 0.10),
        "grade":      v.get("grade", 0.10),
        "price":      v.get("price", 0.20),
        "capex":      {"low": -v.get("capex_down", 0.05), "high": v.get("capex_up", 0.50)},
        "opex":       v.get("opex", 0.15),
        "wacc":       v.get("wacc_abs", v.get("wacc", 0.05)),
    }
    return run_monte_carlo(m, n_runs=req.n_runs, variation=variation)


# ── Sensitivity Analysis ──────────────────────────────────────────────────────

SENSITIVITY_PARAMS = [
    {"key": "price",                      "label": "Commodity Price",        "type": "mineral_price"},
    {"key": "grade",                      "label": "Ore Grade",               "type": "mineral_grade"},
    {"key": "initial_dev_capex",          "label": "Initial CAPEX",           "type": "field"},
    {"key": "total_opex_steady_state",    "label": "Operating Costs (OPEX)",  "type": "field"},
    {"key": "wacc",                       "label": "Discount Rate (WACC)",    "type": "field"},
    {"key": "steady_state_throughput",    "label": "Throughput",              "type": "field"},
    {"key": "royalty_rate",               "label": "Royalty Rate",            "type": "field"},
    {"key": "corp_income_tax_rate",       "label": "Corporate Tax Rate",      "type": "field"},
]


@app.get("/mines/{mine_id}/sensitivity")
def sensitivity_analysis(mine_id: str, variation: float = 0.20, metric: str = "npv"):
    m = get_mine(mine_id)
    if m is None:
        raise HTTPException(404, f"Mine '{mine_id}' not found")

    base_summary = run_dcf(copy.deepcopy(m))["summary"]
    base_val = base_summary.get(metric)

    results = []
    for param in SENSITIVITY_PARAMS:
        low_mine  = copy.deepcopy(m)
        high_mine = copy.deepcopy(m)

        if param["type"] == "mineral_price":
            for mn in low_mine.get("minerals", []):
                mn["price"] *= (1 - variation)
            for mn in high_mine.get("minerals", []):
                mn["price"] *= (1 + variation)
        elif param["type"] == "mineral_grade":
            for mn in low_mine.get("minerals", []):
                mn["grade"] *= (1 - variation)
            for mn in high_mine.get("minerals", []):
                mn["grade"] *= (1 + variation)
        else:
            field = param["key"]
            val = m.get(field, 0) or 0
            if val == 0:
                continue
            low_mine[field]  = val * (1 - variation)
            high_mine[field] = val * (1 + variation)

        try:
            low_val  = run_dcf(low_mine)["summary"].get(metric)
            high_val = run_dcf(high_mine)["summary"].get(metric)
        except Exception:
            low_val = high_val = None

        low_change  = round(low_val  - base_val, 6) if (low_val  is not None and base_val is not None) else None
        high_change = round(high_val - base_val, 6) if (high_val is not None and base_val is not None) else None
        range_val   = round(abs((high_val or base_val or 0) - (low_val or base_val or 0)), 6)

        results.append({
            "param":       param["key"],
            "label":       param["label"],
            "base":        base_val,
            "low":         round(low_val,  6) if low_val  is not None else None,
            "high":        round(high_val, 6) if high_val is not None else None,
            "low_change":  low_change,
            "high_change": high_change,
            "range":       range_val,
        })

    results.sort(key=lambda x: x["range"], reverse=True)

    return {
        "mine_id":    mine_id,
        "metric":     metric,
        "base_value": base_val,
        "variation":  variation,
        "parameters": results,
    }


# ── Waterfall / NPV Bridge ────────────────────────────────────────────────────

@app.get("/mines/{mine_id}/waterfall")
def waterfall_chart(mine_id: str):
    m = get_mine(mine_id)
    if m is None:
        raise HTTPException(404, f"Mine '{mine_id}' not found")

    result   = run_dcf(copy.deepcopy(m))
    dcf_rows = result["dcf_rows"]
    summary  = result["summary"]
    npv      = summary["npv"]

    # LOM totals (exclude year 0 for revenue/opex since that's construction)
    gross_revenue = round(sum(r["production_value"] for r in dcf_rows if r["year"] > 0), 2)
    royalties     = round(sum(r["royalty"]          for r in dcf_rows), 2)   # negative
    opex          = round(sum(r["opex"]             for r in dcf_rows), 2)   # negative
    tax           = round(sum(r["tax"]              for r in dcf_rows), 2)   # negative
    capex         = round(sum(r["capex"]            for r in dcf_rows), 2)   # negative
    total_fcf     = round(sum(r["fcf"]              for r in dcf_rows), 2)
    discount_adj  = round(npv - total_fcf, 2)                                # discounting effect

    # Build waterfall steps with (invisible spacer, visible bar) for stacked chart
    steps = []
    running = 0.0

    def add_step(name, raw, kind):
        nonlocal running
        if kind == "positive":
            inv  = max(running, 0)
            val  = raw
            running += raw
        elif kind == "negative":
            running += raw                 # raw is already negative
            inv  = max(running, 0)
            val  = abs(raw)
        elif kind == "subtotal":
            inv  = 0.0
            val  = running
        else:                              # "total" — standalone bar from 0
            inv  = 0.0
            val  = raw
        steps.append({
            "name":     name,
            "invisible": round(inv, 2),
            "value":    round(val, 2),
            "actual":   round(running if kind != "total" else raw, 2),
            "raw":      round(raw if kind != "subtotal" else running, 2),
            "type":     kind,
        })

    add_step("Gross Revenue",  gross_revenue, "positive")
    add_step("Royalties",      royalties,     "negative")
    add_step("Net Revenue",    0,             "subtotal")
    add_step("Oper. Costs",    opex,          "negative")
    add_step("EBITDA",         0,             "subtotal")
    add_step("Income Tax",     tax,           "negative")
    add_step("CAPEX",          capex,         "negative")
    add_step("Total FCF",      0,             "subtotal")
    add_step("Discount Effect",discount_adj,  "negative" if discount_adj < 0 else "positive")

    # Reset running for NPV standalone bar
    add_step("NPV",            npv,           "total")

    # Year-by-year series for the FCF timeline chart
    yearly = [
        {
            "year":          r["year"],
            "fcf":           r["fcf"],
            "discounted_cf": r["discounted_cf"],
            "cumulative_npv":r["cumulative_npv"],
            "revenue":       r["revenue"],
            "opex":          r["opex"],
            "capex":         r["capex"],
        }
        for r in dcf_rows
    ]

    return {
        "mine_id": mine_id,
        "steps":   steps,
        "yearly":  yearly,
        "summary": {
            "gross_revenue": gross_revenue,
            "royalties":     royalties,
            "opex":          opex,
            "tax":           tax,
            "capex":         capex,
            "total_fcf":     total_fcf,
            "discount_adj":  discount_adj,
            "npv":           npv,
            "irr":           summary.get("irr"),
            "life_of_mine":  summary.get("life_of_mine"),
        },
    }

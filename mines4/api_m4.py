"""
api_m4.py — FastAPI for mines4 (schema_v4 / m4_* tables)
==========================================================
Mount at /mines4 in start.py.

Supports:
  • Multiple commodities per mine
  • Bear / Base / Bull / Single scenarios per commodity
  • Per-scenario DCF inputs (price, opex, capex, timing all scenario-level)
  • Mine-level fallbacks for wacc, tax_rate, royalty_rate, ramp-up
  • In-memory simulation (POST /calculate — nothing written to DB)
  • Sensitivity analysis (tornado)
  • Monte Carlo (500 runs, in-memory)
"""
import copy, math, os, random
from typing import Any, Dict, List, Optional
try:
    import numpy_financial as npf
    _HAS_NPF = True
except ImportError:
    _HAS_NPF = False

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from mines4.mask_utils import build_mask_map, mask_mine

SUPABASE_URL = os.environ.get("SUPABASE_URL",
    "https://snbnqwrxvptrfjsecljd.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY",
    os.environ.get("SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNuYm5xd3J4dnB0cmZqc2VjbGpkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3OTg5MzQ4MSwiZXhwIjoyMDk1NDY5NDgxfQ."
    "t92K9HW0uQpCWy08c6CPtGKynHN5ET3ymcfcupNJTO0"))

def sb(): return create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Mines4 API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Helpers ───────────────────────────────────────────────────────────────────

def _raise(msg, code=404): raise HTTPException(status_code=code, detail=msg)
def _data(res): return res.data or []

def _get_mine(mine_id: str):
    rows = _data(sb().table("m4_mines").select("*").eq("id", mine_id).limit(1).execute())
    if not rows: _raise(f"Mine '{mine_id}' not found")
    return rows[0]

def _get_commodities(mine_id: str):
    return _data(sb().table("m4_commodities").select("*").eq("mine_id", mine_id)
                 .order("display_order").execute())

def _get_scenarios(comm_ids: list):
    if not comm_ids: return []
    return _data(sb().table("m4_scenarios").select("*").in_("commodity_id", comm_ids).execute())

def _get_metrics_map(scen_ids: list):
    if not scen_ids: return {}
    rows = _data(sb().table("m4_metrics").select("*").in_("scenario_id", scen_ids).execute())
    return {r["scenario_id"]: r for r in rows}


# ══════════════════════════════════════════════════════════════════════════════
# DCF ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _irr(fcfs: list) -> Optional[float]:
    if not any(f < -0.001 for f in fcfs) or not any(f > 0.001 for f in fcfs):
        return None
    if _HAS_NPF:
        # numpy_financial uses Newton-Raphson from guess=0.1, matching Excel's IRR()
        try:
            r = npf.irr(fcfs)
            if r is None or r != r:  # nan check
                return None
            return float(r)
        except Exception:
            pass
    # Fallback: Newton-Raphson with guess=0.1 (same as Excel), avoids spurious negative roots
    def npv_f(r):
        return sum(cf / (1.0 + r) ** t for t, cf in enumerate(fcfs))
    def dnpv(r):
        return sum(-t * cf / (1.0 + r) ** (t + 1) for t, cf in enumerate(fcfs))
    r = 0.10  # start at 10% like Excel
    for _ in range(100):
        f  = npv_f(r)
        df = dnpv(r)
        if abs(df) < 1e-12: break
        r2 = r - f / df
        if abs(r2 - r) < 1e-8: return r2
        r = r2
    return r if abs(npv_f(r)) < 1.0 else None


def _f(s, k_scen, mine, k_mine=None, default=0.0):
    v = s.get(k_scen)
    if v is not None: return float(v)
    if k_mine: v = mine.get(k_mine)
    if v is not None: return float(v)
    return float(default)


def run_dcf(mine: dict, scen: dict) -> tuple:
    """
    Returns (year_rows: list[dict], metrics: dict).
    Follows exact formula from notes.md:
      FCF = EBITDA + Income_Tax + Total_Capex
      Tax on EBIT only when Taxable_Income (EBIT + Interest) > 0
      LOM = round(ore_reserve / throughput_pa)  if available
      Annual_Prod = (avg_recovered_grade × throughput_pa) / 1000  if grade available
    All year-row values in raw $; metrics npv/capex/lom_fcf/lom_revenue in $M.
    """
    # ── Rates & mine-level inputs ───────────────────────────────────────────────
    wacc         = _f(scen, "wacc",                mine, "wacc",          0.15)
    tax_rate     = float(mine.get("tax_rate")  or 0.32)
    royalty      = _f(scen, "royalty_rate",         mine, "royalty_rate", 0.03)

    # Life of Mine: explicit life_of_mine_yr takes priority; ore/throughput is fallback only
    ore_res  = mine.get("ore_reserve")
    tput     = mine.get("throughput_pa")
    explicit_lom = mine.get("life_of_mine_yr")
    if explicit_lom and int(explicit_lom) > 0:
        lom = int(explicit_lom)
    elif ore_res and tput and float(tput) > 0:
        lom = round(float(ore_res) / float(tput))
        if lom <= 0:
            lom = 15
    else:
        lom = 15

    # ── Price & production ──────────────────────────────────────────────────────
    price0    = _f(scen, "price_base",            None, None, 0.0)
    price_esc = _f(scen, "price_escalation_rate", None, None, 0.0)

    # Annual production: (avg_recovered_grade × throughput_pa) / 1000  if grade given
    avg_grade = _f(scen, "avg_recovered_grade", None, None, 0.0)
    if avg_grade > 0 and tput and float(tput) > 0:
        ann_prod_ss = (avg_grade * float(tput)) / 1000.0   # kg/yr at steady state
    else:
        ann_prod_ss = _f(scen, "annual_production", None, None, 0.0)

    # ── Opex ────────────────────────────────────────────────────────────────────
    opex_ss   = _f(scen, "opex_steady_state",    None, None, 0.0)
    opex_esc  = _f(scen, "opex_escalation_rate", None, None, 0.0)

    # ── Capex ───────────────────────────────────────────────────────────────────
    init_cap  = _f(scen, "initial_capex",        None, None, 0.0)
    sust_cap  = _f(scen, "sustaining_capex_pa",  None, None, 0.0)
    closure   = float(mine.get("closure_rehab_cost") or 0.0)
    capex_yr  = int(scen.get("capex_deployment_year") or 0)
    prod_start= int(scen.get("production_start_year") or 1)

    # ── Ramp-up schedule ────────────────────────────────────────────────────────
    ramps = [
        float(mine.get("ramp_up_y1") or 0.40),
        float(mine.get("ramp_up_y2") or 0.75),
        float(mine.get("ramp_up_y3") or 1.00),
    ]

    # ── Depreciation (straight-line over avg_depreciation_years) ───────────────
    dep_pa_ex = scen.get("depreciation_pa")
    dep_yrs   = int(scen.get("avg_depreciation_years") or mine.get("avg_depreciation_years") or lom) or lom
    dep_pa    = float(dep_pa_ex) if dep_pa_ex is not None else (init_cap / dep_yrs if init_cap > 0 else 0.0)
    dep_start = capex_yr + 1
    dep_end   = dep_start + dep_yrs - 1

    # ── Debt / Interest ─────────────────────────────────────────────────────────
    debt_funding  = float(mine.get("debt_funding")  or 0.0)
    debt_term     = int(mine.get("debt_term")        or 0)
    interest_rate = float(mine.get("interest_rate") or 0.0)
    interest_pa   = debt_funding * interest_rate   # annual interest payment

    # ── Year loop ───────────────────────────────────────────────────────────────
    rows, cum_fcf, cum_ore = [], 0.0, 0.0
    ore_total = float(ore_res) if ore_res else 0.0

    for t in range(lom + 1):
        t_prod = t - prod_start + 1
        if t < prod_start:    rf = 0.0
        elif t_prod <= 3:     rf = ramps[t_prod - 1]
        else:                 rf = 1.0

        # Production schedule
        ore_mined = float(tput) * rf if tput else 0.0
        cum_ore  += ore_mined
        remaining = max(0.0, ore_total - cum_ore)
        prod      = ann_prod_ss * rf   # mineral production (kg) with ramp

        # Revenue
        price   = price0 * ((1 + price_esc) ** max(0, t - prod_start)) if t >= prod_start else price0
        gr      = prod * price                        # Gross Revenue
        roy_amt = gr * royalty                        # Royalties (positive amount)
        net_rev = gr - roy_amt                        # Net Revenue

        # Opex escalation factor: starts at 1.0, multiplies each production year
        t_opex      = max(0, t - prod_start)
        esc_factor  = ((1 + opex_esc) ** t_opex) if t >= prod_start else 1.0
        opex        = -(opex_ss * esc_factor) if t >= prod_start else 0.0

        # EBITDA = Net Revenue + Operating Costs (opex is negative)
        ebitda        = net_rev + opex
        ebitda_margin = round(ebitda / gr, 4) if gr > 0 else 0.0

        # Depreciation: −init_cap / dep_yrs per year (negative)
        dep  = -dep_pa if dep_start <= t <= dep_end else 0.0

        # EBIT = EBITDA + Depreciation
        ebit = ebitda + dep

        # Interest Expense: −debt_funding × interest_rate for years 1..debt_term (negative)
        interest = -interest_pa if (interest_pa > 0 and 1 <= t <= debt_term) else 0.0

        # Taxable Income = EBIT + Interest Expense
        taxable_income = ebit + interest
        # Income Tax: EBIT × −tax_rate if taxable income > 0, else 0 (negative)
        income_tax = (ebit * -tax_rate) if taxable_income > 0 else 0.0

        # Net Income = EBIT + Income Tax
        net_income = ebit + income_tax

        # Capital Expenditure (negative outflows) — track components separately
        init_dev_capex  = -init_cap  if (t == capex_yr and init_cap > 0)    else 0.0
        sustaining_capex= -sust_cap  if (t >= prod_start and sust_cap > 0)  else 0.0
        closure_capex   = -closure   if (t == lom and closure > 0)          else 0.0
        capex = init_dev_capex + sustaining_capex + closure_capex

        # FCF = EBITDA + Income Tax + Total Capex  (income_tax and capex are negative)
        fcf      = ebitda + income_tax + capex
        cum_fcf += fcf

        df    = 1.0 / (1.0 + wacc) ** t
        dcf_v = round(fcf * df, 2)

        rows.append({
            "year":              t,
            # Production schedule
            "ramp_factor":       round(rf, 4),
            "ore_mined":         round(ore_mined, 2),
            "cumulative_ore":    round(cum_ore, 2),
            "remaining_reserve": round(remaining, 2),
            "production":        round(prod, 4),
            # Revenue
            "commodity_price":   round(price, 4),
            "gross_revenue":     round(gr, 2),
            "royalty":           round(-roy_amt, 2),      # negative outflow
            "net_revenue":       round(net_rev, 2),
            "revenue":           round(net_rev, 2),       # frontend alias
            # Operating costs
            "opex_esc_factor":   round(esc_factor, 6),
            "operating_costs":   round(opex, 2),          # negative
            # EBITDA
            "ebitda":            round(ebitda, 2),
            "ebitda_margin":     ebitda_margin,
            # D&A / Interest
            "depreciation":      round(dep, 2),           # negative
            "interest_expense":  round(interest, 2),      # negative
            # EBIT & Tax
            "ebit":              round(ebit, 2),
            "taxable_income":    round(taxable_income, 2),
            "income_tax":        round(income_tax, 2),    # negative
            "tax":               round(income_tax, 2),    # frontend alias
            "nopat":             round(net_income, 2),    # Net Income
            # Capex components (all negative)
            "initial_dev_capex": round(init_dev_capex, 2),
            "sustaining_capex":  round(sustaining_capex, 2),
            "closure_capex":     round(closure_capex, 2),
            "capex":             round(capex, 2),
            # FCF
            "free_cash_flow":    round(fcf, 2),
            "fcf":               round(fcf, 2),           # frontend alias
            "cumulative_fcf":    round(cum_fcf, 2),
            "cumulative_dcf":    round(cum_fcf, 2),       # frontend alias
            "discount_factor":   round(df, 6),
            "discounted_cf":     dcf_v,
            "dcf":               dcf_v,                   # frontend alias
        })

    # ── Summary metrics ─────────────────────────────────────────────────────────
    fcfs    = [r["free_cash_flow"] for r in rows]
    irr     = _irr(fcfs)
    payback = next((r["year"] for r in rows if r["cumulative_fcf"] >= 0 and r["year"] > 0), None)

    # NPV: match Excel convention — Years 0..min(20, lom) only
    # Excel: =NPV(wacc, Y1:Y20) + Y0  (only first 20 years count, post-Y20 FCFs excluded)
    npv_horizon = min(20, lom)
    npv = sum(r["discounted_cf"] for r in rows if r["year"] <= npv_horizon)

    # MOIC: match Excel = SUM(net_income Y1..LOM) / -SUM(capex all years)
    net_income_sum  = sum(r["nopat"] for r in rows if r["year"] > 0)
    total_capex_abs = abs(sum(r["capex"] for r in rows))

    # Total LOM Revenue
    total_lom_revenue = sum(r["gross_revenue"] for r in rows)

    # Total Mineral Produced (all years)
    total_mineral_produced = sum(r["production"] for r in rows)

    # Total Cost per Mineral Unit = -SUM(opex all years) / total_mineral_produced
    total_opex_abs = sum(abs(r["operating_costs"]) for r in rows)
    total_cost_per_unit = (total_opex_abs / total_mineral_produced) if total_mineral_produced > 0 else 0.0

    # Unit Margin at steady state: price - (opex_ss / ann_prod_ss)
    unit_margin_dollar = None
    unit_margin_pct    = None
    if ann_prod_ss > 0 and price0 > 0:
        unit_margin_dollar = price0 - (opex_ss / ann_prod_ss)
        unit_margin_pct    = unit_margin_dollar / price0

    metrics = {
        "npv":                    round(npv / 1e6, 4),
        "irr":                    round(irr, 6) if irr is not None else None,
        "payback":                payback if payback else None,
        "moic":                   round(net_income_sum / total_capex_abs, 4) if total_capex_abs > 0 else 0.0,
        "total_capex":            round(abs(sum(r["capex"] for r in rows)) / 1e6, 4),
        "total_lom_fcf":          round(sum(fcfs) / 1e6, 4),
        "total_lom_revenue":      round(total_lom_revenue / 1e6, 4),
        "total_mineral_produced": round(total_mineral_produced, 2),
        "total_cost_per_unit":    round(total_cost_per_unit, 4) if total_cost_per_unit else None,
        "unit_margin_dollar":     round(unit_margin_dollar, 2) if unit_margin_dollar is not None else None,
        "unit_margin_pct":        round(unit_margin_pct, 6) if unit_margin_pct is not None else None,
        "life_of_mine_yr":        lom,
    }
    return rows, metrics


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── GET /mines/list ───────────────────────────────────────────────────────────
@app.get("/mines/list")
def list_mines():
    client = sb()
    mines     = _data(client.table("m4_mines").select("*").order("mine_name").execute())
    all_comms = _data(client.table("m4_commodities").select("*").execute())
    all_scens = _data(client.table("m4_scenarios").select("*").in_("scenario", ["Base","Single"]).execute())
    scen_ids  = [s["id"] for s in all_scens]
    met_map   = _get_metrics_map(scen_ids)
    comm_map  = {c["id"]: c for c in all_comms}
    mines_map = {m["id"]: m for m in mines}

    # Always recalculate metrics to avoid stale cached values (e.g. wrong IRR)
    for s in all_scens:
        comm = comm_map.get(s["commodity_id"], {})
        mine = mines_map.get(comm.get("mine_id"))
        if not mine:
            continue
        try:
            _, calc_met = run_dcf(mine, s)
            calc_met["scenario_id"] = s["id"]
            client.table("m4_metrics").upsert(calc_met, on_conflict="scenario_id").execute()
            met_map[s["id"]] = calc_met
        except Exception:
            pass

    mine_summary: dict = {}
    for s in all_scens:
        comm = comm_map.get(s["commodity_id"], {})
        mid  = comm.get("mine_id")
        if not mid: continue
        m = met_map.get(s["id"], {})
        if mid not in mine_summary or comm.get("is_primary"):
            mine_summary[mid] = {
                "primary_mineral": comm.get("commodity"),
                "npv":     m.get("npv"),    "irr":  m.get("irr"),
                "moic":    m.get("moic"),   "payback": m.get("payback"),
            }

    mask_map = build_mask_map(client)
    result = []
    for mine in mines:
        comms   = [c for c in all_comms if c["mine_id"] == mine["id"]]
        summary = mine_summary.get(mine["id"], {})
        result.append({**mask_mine(mine, mask_map), "commodities": comms, **summary})
    return {"mines": result}


# ── GET /mines/{mine_id} ──────────────────────────────────────────────────────
@app.get("/mines/{mine_id}")
def get_mine(mine_id: str):
    mine = _get_mine(mine_id)
    comms = _get_commodities(mine_id)
    comm_ids = [c["id"] for c in comms]
    scens = _get_scenarios(comm_ids)
    scen_ids = [s["id"] for s in scens]
    met_map = _get_metrics_map(scen_ids)
    for s in scens:
        s["metrics"] = met_map.get(s["id"])
    scen_by_comm = {}
    for s in scens:
        scen_by_comm.setdefault(s["commodity_id"], []).append(s)
    for c in comms:
        c["scenarios"] = scen_by_comm.get(c["id"], [])
    mask_map = build_mask_map(sb())
    return {**mask_mine(mine, mask_map), "commodities": comms}


# ── PATCH /mines/{mine_id} ────────────────────────────────────────────────────
_MINE_PATCHABLE = {
    "mine_name","license_number","country","province","mine_type","status",
    "ore_reserve","reserve_unit","throughput_pa","throughput_unit","life_of_mine_yr",
    "wacc","tax_rate","royalty_rate","ramp_up_y1","ramp_up_y2","ramp_up_y3",
    "closure_rehab_cost","prospectivity_notes","headline","subtitle",
    "risk_factors","environmental_impacts",
    "lat","lng",
}

@app.patch("/mines/{mine_id}")
def patch_mine(mine_id: str, body: Dict[str, Any] = Body(...)):
    _get_mine(mine_id)
    patch = {k: v for k, v in body.items() if k in _MINE_PATCHABLE}
    if not patch: raise HTTPException(400, "No valid fields")
    client = sb()
    client.table("m4_mines").update(patch).eq("id", mine_id).execute()
    updated = _data(client.table("m4_mines").select("*").eq("id", mine_id).limit(1).execute())[0]
    return mask_mine(updated, build_mask_map(client))


# ── POST /mines ───────────────────────────────────────────────────────────────
@app.post("/mines")
def create_mine(body: Dict[str, Any] = Body(...)):
    import time
    mine_fields = {k: v for k, v in body.items() if k in _MINE_PATCHABLE and v is not None}
    mine_fields.setdefault("mine_name", "New Mine")
    mine_fields.setdefault("license_number", f"DRAFT-{int(time.time())}")
    mine_fields["is_user_created"] = True
    res = sb().table("m4_mines").insert(mine_fields).execute()
    mine_id = res.data[0]["id"]
    for cd in (body.get("commodities") or []):
        _create_commodity(mine_id, cd)
    return get_mine(mine_id)


def _create_commodity(mine_id: str, cd: dict):
    client = sb()
    res = client.table("m4_commodities").insert({
        "mine_id":       mine_id,
        "commodity":     cd.get("commodity", "Unknown"),
        "is_primary":    cd.get("is_primary", False),
        "has_scenarios": cd.get("has_scenarios", False),
        "display_order": cd.get("display_order", 0),
    }).execute()
    comm_id = res.data[0]["id"]
    for sd in (cd.get("scenarios") or [{"scenario": "Single"}]):
        _create_scenario(comm_id, sd)
    return comm_id


def _create_scenario(comm_id: str, sd: dict):
    _SCEN_FIELDS = {
        "scenario","sheet_name","price_base","price_unit","price_escalation_rate",
        "annual_production","production_unit","opex_steady_state","opex_per_unit",
        "opex_escalation_rate","initial_capex","sustaining_capex_pa",
        "capex_deployment_year","depreciation_pa","avg_depreciation_years",
        "production_start_year","wacc","royalty_rate","basis_notes",
    }
    payload = {"commodity_id": comm_id}
    payload.update({k: v for k, v in sd.items() if k in _SCEN_FIELDS and v is not None})
    payload.setdefault("scenario", "Single")
    sb().table("m4_scenarios").insert(payload).execute()


# ── POST /mines/{mine_id}/commodities ─────────────────────────────────────────
@app.post("/mines/{mine_id}/commodities")
def add_commodity(mine_id: str, body: Dict[str, Any] = Body(...)):
    _get_mine(mine_id)
    _create_commodity(mine_id, body)
    return get_mine(mine_id)


# ── DELETE /mines/{mine_id}/commodities/{comm_id} ─────────────────────────────
@app.delete("/mines/{mine_id}/commodities/{comm_id}")
def delete_commodity(mine_id: str, comm_id: str):
    rows = _data(sb().table("m4_commodities").select("id").eq("id", comm_id).eq("mine_id", mine_id).execute())
    if not rows: _raise(f"Commodity not found on mine")
    sb().table("m4_commodities").delete().eq("id", comm_id).execute()
    return {"ok": True}


# ── PATCH /mines/{mine_id}/scenarios/{scen_id} ────────────────────────────────
_SCEN_PATCHABLE = {
    "scenario","sheet_name","price_base","price_unit","price_escalation_rate",
    "annual_production","production_unit","opex_steady_state","opex_per_unit",
    "opex_escalation_rate","initial_capex","sustaining_capex_pa",
    "capex_deployment_year","depreciation_pa","avg_depreciation_years",
    "production_start_year","wacc","royalty_rate","basis_notes",
}

@app.patch("/mines/{mine_id}/scenarios/{scen_id}")
def patch_scenario(mine_id: str, scen_id: str, body: Dict[str, Any] = Body(...)):
    patch = {k: v for k, v in body.items() if k in _SCEN_PATCHABLE}
    if not patch: raise HTTPException(400, "No valid scenario fields")
    sb().table("m4_scenarios").update(patch).eq("id", scen_id).execute()
    return {"ok": True}


# ── DELETE /mines/{mine_id}/scenarios/{scen_id} ───────────────────────────────
@app.delete("/mines/{mine_id}/scenarios/{scen_id}")
def delete_scenario(mine_id: str, scen_id: str):
    client = sb()
    # Verify scenario belongs to this mine via commodity
    scen = _data(client.table("m4_scenarios").select("commodity_id").eq("id", scen_id).limit(1).execute())
    if not scen: _raise("Scenario not found")
    comm = _data(client.table("m4_commodities").select("mine_id").eq("id", scen[0]["commodity_id"]).limit(1).execute())
    if not comm or comm[0]["mine_id"] != mine_id: _raise("Scenario does not belong to mine", 403)
    client.table("m4_metrics").delete().eq("scenario_id", scen_id).execute()
    client.table("m4_dcf_inputs").delete().eq("scenario_id", scen_id).execute()
    client.table("m4_scenarios").delete().eq("id", scen_id).execute()
    return {"ok": True}


# ── DELETE /mines/{mine_id} ───────────────────────────────────────────────────
@app.delete("/mines/{mine_id}")
def delete_mine(mine_id: str):
    client = sb()
    # Cascade: metrics → scenarios → commodities → dcf_inputs → mine
    comms    = _data(client.table("m4_commodities").select("id").eq("mine_id", mine_id).execute())
    comm_ids = [c["id"] for c in comms]
    if comm_ids:
        scens    = _data(client.table("m4_scenarios").select("id").in_("commodity_id", comm_ids).execute())
        scen_ids = [s["id"] for s in scens]
        if scen_ids:
            client.table("m4_metrics").delete().in_("scenario_id", scen_ids).execute()
            client.table("m4_dcf_inputs").delete().in_("scenario_id", scen_ids).execute()
            client.table("m4_scenarios").delete().in_("id", scen_ids).execute()
        client.table("m4_commodities").delete().in_("id", comm_ids).execute()
    client.table("m4_mines").delete().eq("id", mine_id).execute()
    return {"ok": True, "deleted": mine_id}


# ── GET /mines/{mine_id}/dcf/{scen_id} ────────────────────────────────────────
@app.get("/mines/{mine_id}/dcf/{scen_id}")
def get_dcf(mine_id: str, scen_id: str, source: str = "ingested"):
    """
    source='ingested' → return m4_dcf_inputs year rows
    source='calculated' → compute from scalar inputs on-the-fly
    """
    scen_rows = _data(sb().table("m4_scenarios").select("*").eq("id", scen_id).limit(1).execute())
    if not scen_rows: _raise(f"Scenario not found")
    scen = scen_rows[0]

    comm = _data(sb().table("m4_commodities").select("*").eq("id", scen["commodity_id"]).limit(1).execute())
    if not comm or comm[0]["mine_id"] != mine_id: _raise("Scenario does not belong to mine", 403)

    met = _data(sb().table("m4_metrics").select("*").eq("scenario_id", scen_id).limit(1).execute())
    metrics = met[0] if met else None

    if source == "ingested":
        years = _data(sb().table("m4_dcf_inputs").select("*").eq("scenario_id", scen_id).order("year").execute())
        return {"scenario": {**scen, "commodity": comm[0]["commodity"]},
                "metrics": metrics, "years": years, "source": "ingested"}

    # Computed on-the-fly
    mine = _get_mine(mine_id)
    year_rows, calc_metrics = run_dcf(mine, scen)
    return {"scenario": {**scen, "commodity": comm[0]["commodity"]},
            "metrics": calc_metrics, "years": year_rows, "source": "calculated"}


# ── GET /mines/{mine_id}/scenarios ────────────────────────────────────────────
@app.get("/mines/{mine_id}/scenarios")
def get_scenarios(mine_id: str):
    mine  = _get_mine(mine_id)
    comms = _get_commodities(mine_id)
    comm_ids = [c["id"] for c in comms]
    comm_map = {c["id"]: c for c in comms}
    scens = _get_scenarios(comm_ids)
    scen_ids = [s["id"] for s in scens]
    met_map = _get_metrics_map(scen_ids)
    client = sb()

    # Always recalculate metrics from current inputs so cached stale values never surface
    for s in scens:
        try:
            _, calc_met = run_dcf(mine, s)
            calc_met["scenario_id"] = s["id"]
            client.table("m4_metrics").upsert(calc_met, on_conflict="scenario_id").execute()
            met_map[s["id"]] = calc_met
        except Exception:
            pass

    # Return flat list with commodity + metrics merged in
    flat = []
    for s in scens:
        comm = comm_map.get(s["commodity_id"], {})
        m    = met_map.get(s["id"]) or {}
        flat.append({
            **s,
            "commodity":  comm.get("commodity"),
            "is_primary": comm.get("is_primary"),
            "npv":     m.get("npv"),
            "irr":     m.get("irr"),
            "moic":    m.get("moic"),
            "payback": m.get("payback"),
            "total_capex":          m.get("total_capex"),
            "total_lom_fcf":        m.get("total_lom_fcf"),
            "total_lom_revenue":    m.get("total_lom_revenue"),
            "total_mineral_produced": m.get("total_mineral_produced"),
            "total_cost_per_unit":  m.get("total_cost_per_unit"),
            "unit_margin_dollar":   m.get("unit_margin_dollar"),
            "unit_margin_pct":      m.get("unit_margin_pct"),
            "life_of_mine_yr":      m.get("life_of_mine_yr"),
        })
    return {"mine_id": mine_id, "scenarios": flat}


# ── POST /mines/{mine_id}/calculate  (simulation only — nothing to DB) ────────
@app.post("/mines/{mine_id}/calculate")
def calculate_mine(mine_id: str):
    mine  = _get_mine(mine_id)
    comms = _get_commodities(mine_id)
    if not comms: _raise("No commodities found")

    results = []
    for comm in comms:
        scens = _data(sb().table("m4_scenarios").select("*").eq("commodity_id", comm["id"]).execute())
        for scen in scens:
            year_rows, metrics = run_dcf(mine, scen)
            results.append({
                "scenario_id": scen["id"],
                "commodity":   comm["commodity"],
                "scenario":    scen["scenario"],
                "metrics":     metrics,
                "years":       year_rows,
            })
    return {"results": results}


# ── POST /mines/{mine_id}/calculate/{scen_id}  (single scenario) ─────────────
@app.post("/mines/{mine_id}/calculate/{scen_id}")
def calculate_scenario(mine_id: str, scen_id: str):
    mine = _get_mine(mine_id)
    scen_rows = _data(sb().table("m4_scenarios").select("*").eq("id", scen_id).limit(1).execute())
    if not scen_rows: _raise("Scenario not found")
    year_rows, metrics = run_dcf(mine, scen_rows[0])
    return {"scenario_id": scen_id, "metrics": metrics, "years": year_rows}


# ── GET /mines/{mine_id}/sensitivity ─────────────────────────────────────────
_SENS_PARAMS = [
    {"key": "price",   "label": "Commodity Price",      "field": "price"},
    {"key": "opex",    "label": "Operating Costs",      "field": "opex"},
    {"key": "capex",   "label": "Capital Expenditure",  "field": "capex"},
    {"key": "wacc",    "label": "Discount Rate (WACC)", "field": "wacc"},
    {"key": "tax",     "label": "Corporate Tax Rate",   "field": "tax"},
    {"key": "royalty", "label": "Royalty Rate",         "field": "royalty"},
]

def _vary(mine, scen, param_key, delta):
    m, s = copy.deepcopy(mine), copy.deepcopy(scen)
    if param_key == "wacc":
        w = float(s.get("wacc") or m.get("wacc") or 0.15)
        s["wacc"] = max(0.01, w * (1 + delta))
    elif param_key == "price":
        s["price_base"] = (s.get("price_base") or 0) * (1 + delta)
    elif param_key == "opex":
        s["opex_steady_state"] = (s.get("opex_steady_state") or 0) * (1 + delta)
    elif param_key == "capex":
        s["initial_capex"] = (s.get("initial_capex") or 0) * (1 + delta)
    elif param_key == "tax":
        m["tax_rate"] = max(0, float(m.get("tax_rate") or 0.32) * (1 + delta))
    elif param_key == "royalty":
        s["royalty_rate"] = max(0, float(s.get("royalty_rate") or m.get("royalty_rate") or 0.03) * (1 + delta))
    _, met = run_dcf(m, s)
    return met

@app.get("/mines/{mine_id}/sensitivity")
def sensitivity(mine_id: str, variation: float = 0.20, metric: str = "npv"):
    mine  = _get_mine(mine_id)
    comms = _get_commodities(mine_id)
    if not comms: _raise("No commodities")
    primary = next((c for c in comms if c.get("is_primary")), comms[0])
    scens = _data(sb().table("m4_scenarios").select("*").eq("commodity_id", primary["id"])
                  .in_("scenario", ["Base","Single"]).limit(1).execute())
    if not scens:
        scens = _data(sb().table("m4_scenarios").select("*").eq("commodity_id", primary["id"]).limit(1).execute())
    if not scens: _raise("No base scenario for sensitivity")
    scen = scens[0]

    _, base_met = run_dcf(mine, scen)
    base_val = base_met.get(metric)
    if base_val is None: _raise(f"Cannot compute base {metric}")

    results = []
    for p in _SENS_PARAMS:
        try:
            lo_met = _vary(mine, scen, p["key"], -variation)
            hi_met = _vary(mine, scen, p["key"],  variation)
            lo_v, hi_v = lo_met.get(metric), hi_met.get(metric)
            if lo_v is None or hi_v is None: continue
            results.append({
                "param": p["key"], "label": p["label"], "base": base_val,
                "low": round(lo_v, 4), "high": round(hi_v, 4),
                "low_change":  round(lo_v - base_val, 4),
                "high_change": round(hi_v - base_val, 4),
                "range":       round(abs(hi_v - lo_v), 4),
            })
        except: continue
    results.sort(key=lambda x: x["range"], reverse=True)
    sensitivities = [
        {"variable": r["param"], "label": r["label"],
         "low_value": r["low"], "high_value": r["high"],
         "low_change": r["low_change"], "high_change": r["high_change"],
         "swing": r["range"]}
        for r in results
    ]
    return {"mine_id": mine_id, "metric": metric, "base_value": base_val,
            "variation": variation, "sensitivities": sensitivities}


# ── POST /mines/{mine_id}/monte-carlo ─────────────────────────────────────────
@app.post("/mines/{mine_id}/monte-carlo")
def monte_carlo(mine_id: str, body: Dict[str, Any] = Body(default={})):
    mine  = _get_mine(mine_id)
    scenario_id = body.get("scenario_id")
    if scenario_id:
        scens = _data(sb().table("m4_scenarios").select("*").eq("id", scenario_id).limit(1).execute())
    else:
        comms = _get_commodities(mine_id)
        if not comms: _raise("No commodities")
        primary = next((c for c in comms if c.get("is_primary")), comms[0])
        scens = _data(sb().table("m4_scenarios").select("*").eq("commodity_id", primary["id"])
                      .in_("scenario", ["Base","Single"]).limit(1).execute())
        if not scens:
            scens = _data(sb().table("m4_scenarios").select("*").eq("commodity_id", primary["id"]).limit(1).execute())
    if not scens: _raise("No scenario for Monte Carlo")
    base_scen = scens[0]

    n_runs = int(body.get("n_runs", 500))
    vp  = float(body.get("price_std",      0.15))
    vo  = float(body.get("opex_std",       0.10))
    vc  = float(body.get("capex_std",      0.10))
    vpr = float(body.get("production_std", 0.05))
    vw  = float(body.get("wacc_std",       0.02))

    results = []
    for _ in range(n_runs):
        m, s = copy.deepcopy(mine), copy.deepcopy(base_scen)
        s["price_base"]        = max(0.01, (s.get("price_base") or 0)        * (1 + random.gauss(0, vp)))
        s["opex_steady_state"] = max(0,    (s.get("opex_steady_state") or 0) * (1 + random.gauss(0, vo)))
        s["initial_capex"]     = max(0,    (s.get("initial_capex") or 0)     * (1 + random.gauss(0, vc)))
        s["annual_production"] = max(0.01, (s.get("annual_production") or 0) * (1 + random.gauss(0, vpr)))
        base_w = float(s.get("wacc") or m.get("wacc") or 0.15)
        s["wacc"] = max(0.01, base_w + random.gauss(0, vw))
        try:
            _, met = run_dcf(m, s)
            results.append({"npv": met.get("npv"), "irr": met.get("irr"), "moic": met.get("moic")})
        except: pass

    def _stats(key):
        vals = sorted(r[key] for r in results if r.get(key) is not None)
        if not vals: return {}
        n = len(vals)
        mean = sum(vals) / n
        std  = math.sqrt(sum((x - mean)**2 for x in vals) / n)
        def pct(p): i = (n-1)*p/100; lo, hi = int(i), min(int(i)+1,n-1); return vals[lo]+(vals[hi]-vals[lo])*(i-lo)
        return {
            "mean": round(mean, 4), "std": round(std, 4),
            "p10": round(pct(10), 4), "p25": round(pct(25), 4),
            "p50": round(pct(50), 4), "p75": round(pct(75), 4),
            "p90": round(pct(90), 4),
            "min": round(vals[0], 4), "max": round(vals[-1], 4),
        }

    npv_s = _stats("npv")
    irr_s = _stats("irr")
    npv_vals = [r["npv"] for r in results if r.get("npv") is not None]
    irr_vals = [r["irr"] for r in results if r.get("irr") is not None]
    scatter  = [{"x": round(r["irr"] * 100, 3), "y": round(r["npv"], 4)}
                for r in results if r.get("npv") is not None and r.get("irr") is not None][:600]

    return {
        "n_runs": len(results),
        "stats": {
            "npv_mean": npv_s.get("mean"), "npv_std": npv_s.get("std"),
            "npv_p10": npv_s.get("p10"),  "npv_p25": npv_s.get("p25"),
            "npv_p50": npv_s.get("p50"),  "npv_p75": npv_s.get("p75"),
            "npv_p90": npv_s.get("p90"),
            "irr_mean": irr_s.get("mean"), "irr_std": irr_s.get("std"),
            "irr_p10": irr_s.get("p10"),  "irr_p25": irr_s.get("p25"),
            "irr_p50": irr_s.get("p50"),  "irr_p75": irr_s.get("p75"),
            "irr_p90": irr_s.get("p90"),
            "moic_p50": _stats("moic").get("p50"),
        },
        "npv_distribution": npv_vals,
        "irr_distribution": irr_vals,
        "scatter": scatter,
    }


# ── GET /mines/{mine_id}/exec-summary ────────────────────────────────────────
@app.get("/mines/{mine_id}/exec-summary")
def exec_summary(mine_id: str):
    mine  = _get_mine(mine_id)
    comms = _get_commodities(mine_id)
    comm_ids = [c["id"] for c in comms]
    scens = _get_scenarios(comm_ids)
    scen_ids = [s["id"] for s in scens]
    met_map = _get_metrics_map(scen_ids)

    scenario_returns = []
    for s in scens:
        comm = next((c for c in comms if c["id"] == s["commodity_id"]), {})
        m = met_map.get(s["id"], {})
        scenario_returns.append({
            "commodity": comm.get("commodity"), "is_primary": comm.get("is_primary"),
            "scenario_id": s["id"], "scenario": s["scenario"],
            "price_base": s.get("price_base"), "price_unit": s.get("price_unit"),
            "basis_notes": s.get("basis_notes"),
            "npv": m.get("npv"), "irr": m.get("irr"), "payback": m.get("payback"),
            "moic": m.get("moic"), "total_capex": m.get("total_capex"),
        })

    exec_rows = _data(sb().table("m4_exec_rows").select("*").eq("mine_id", mine_id).order("row_index").execute())
    sections: dict = {}
    for r in exec_rows:
        sections.setdefault(r["section"] or "General", []).append(r)

    mask_map = build_mask_map(sb())
    return {"mine": mask_mine(mine, mask_map), "scenario_returns": scenario_returns, "exec_sections": sections}

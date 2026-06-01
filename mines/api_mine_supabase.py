"""
api_mine_supabase.py  v3
========================
FastAPI app for Supabase-backed Investment Modeling UI.
Schema (schema_v2.sql):
  mines, financial_models, mine_commodities, commodity_scenarios,
  scenario_metrics, dcf_years, exec_summary_rows

Mount at /mines2 in start.py.
"""
import os
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from typing import Any, Dict

# ── Supabase client ───────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL",
    "https://snbnqwrxvptrfjsecljd.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY",
    os.environ.get("SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNuYm5xd3J4dnB0cmZqc2VjbGpkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3OTg5MzQ4MSwiZXhwIjoyMDk1NDY5NDgxfQ."
    "t92K9HW0uQpCWy08c6CPtGKynHN5ET3ymcfcupNJTO0"))

# Fresh client per call — avoids httpx "Server disconnected" on idle connections
def sb():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Mines Supabase API v3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _raise(msg, code=404):
    raise HTTPException(status_code=code, detail=msg)

def _data(res):
    return res.data or []

def _get_mine(mine_id: str):
    rows = _data(sb().table("mines").select("*").eq("id", mine_id).limit(1).execute())
    if not rows:
        _raise(f"Mine '{mine_id}' not found")
    return rows[0]

def _get_commodities(mine_id: str):
    return _data(
        sb().table("mine_commodities")
            .select("*")
            .eq("mine_id", mine_id)
            .order("commodity")
            .execute()
    )

def _get_scenarios_for_comms(comm_ids: list):
    if not comm_ids:
        return []
    return _data(
        sb().table("commodity_scenarios")
            .select("*")
            .in_("commodity_id", comm_ids)
            .order("scenario")
            .execute()
    )

def _get_metrics_map(scen_ids: list):
    if not scen_ids:
        return {}
    rows = _data(
        sb().table("scenario_metrics")
            .select("*")
            .in_("scenario_id", scen_ids)
            .execute()
    )
    return {r["scenario_id"]: r for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# GET /mines/list
# Both mines with their commodity list and combined financial model
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/mines/list")
def list_mines():
    client    = sb()
    mines     = _data(client.table("mines").select("*").order("mine_name").execute())
    all_comms = _data(client.table("mine_commodities").select("*").execute())
    all_fms   = _data(client.table("financial_models").select("*").execute())
    fm_map    = {f["mine_id"]: f for f in all_fms}

    # Fetch Base/Single scenario metrics for ALL commodities in bulk
    # Prefer primary commodity but fall back to any commodity that has data
    all_comm_ids = [c["id"] for c in all_comms]
    all_scens, metrics_map, mine_summary = [], {}, {}
    if all_comm_ids:
        all_scens = _data(
            client.table("commodity_scenarios")
                  .select("*")
                  .in_("commodity_id", all_comm_ids)
                  .in_("scenario", ["Base", "Single"])
                  .execute()
        )
        scen_ids = [s["id"] for s in all_scens]
        metrics_map = _get_metrics_map(scen_ids)

        comm_mine = {c["id"]: c for c in all_comms}
        for s in all_scens:
            comm = comm_mine.get(s["commodity_id"], {})
            mid  = comm.get("mine_id")
            if not mid:
                continue
            m = metrics_map.get(s["id"]) or {}
            # Overwrite if: no entry yet, OR this commodity is marked primary
            if mid not in mine_summary or comm.get("is_primary"):
                mine_summary[mid] = {
                    "primary_mineral": comm.get("commodity"),
                    "npv":     m.get("npv"),
                    "irr":     m.get("irr"),
                    "moic":    m.get("moic"),
                    "payback": m.get("payback"),
                }

    result = []
    for mine in mines:
        comms   = [c for c in all_comms if c["mine_id"] == mine["id"]]
        summary = mine_summary.get(mine["id"], {})
        result.append({**mine, "commodities": comms,
                       "financial_model": fm_map.get(mine["id"]), **summary})
    return {"mines": result}


# ─────────────────────────────────────────────────────────────────────────────
# GET /mines/{mine_id}
# Full mine profile: identity + commodities + their scenarios + metrics
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/mines/{mine_id}")
def get_mine(mine_id: str):
    mine = _get_mine(mine_id)
    commodities = _get_commodities(mine_id)
    comm_ids = [c["id"] for c in commodities]
    scenarios = _get_scenarios_for_comms(comm_ids)
    scen_ids = [s["id"] for s in scenarios]
    metrics_map = _get_metrics_map(scen_ids)

    # attach metrics + nest scenarios under commodities
    for s in scenarios:
        s["metrics"] = metrics_map.get(s["id"])
    scen_by_comm = {}
    for s in scenarios:
        scen_by_comm.setdefault(s["commodity_id"], []).append(s)
    for c in commodities:
        c["scenarios"] = scen_by_comm.get(c["id"], [])

    fm = _data(
        sb().table("financial_models")
            .select("*").eq("mine_id", mine_id).limit(1).execute()
    )
    return {**mine, "commodities": commodities, "financial_model": fm[0] if fm else None}


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /mines/{mine_id}
# Update editable mine fields (run ALTER TABLE in Supabase for new columns first)
# ─────────────────────────────────────────────────────────────────────────────
_PATCHABLE = {
    # existing columns
    "mine_name","license_number","country","province","ore_reserve","reserve_unit",
    "throughput_pa","throughput_unit","life_of_mine_yr","concession_area_ha",
    "wacc","tax_rate","headline","subtitle",
    # extended columns (added via ALTER TABLE — see schema_v2_ext.sql)
    "primary_minerals","mine_type","prospectivity_notes","status",
    "ramp_up_y1","ramp_up_y2","ramp_up_y3",
    "initial_dev_capex","total_opex_steady_state","cost_per_ore_m3",
    "variable_cost_per_unit","opex_escalation_rate","avg_depreciation_years",
    "royalty_rate","debt_funding","debt_term","interest_rate","closure_rehab_cost",
    "risk_factors","environmental_impacts",
}

@app.patch("/mines/{mine_id}")
def patch_mine(mine_id: str, body: Dict[str, Any] = Body(...)):
    _get_mine(mine_id)  # 404 if not found
    patch = {k: v for k, v in body.items() if k in _PATCHABLE}
    if not patch:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    sb().table("mines").update(patch).eq("id", mine_id).execute()
    return _get_mine(mine_id)


# ─────────────────────────────────────────────────────────────────────────────
# GET /mines/{mine_id}/exec-summary
# Board memo: mine + exec_summary_rows (sectioned) + scenario returns
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/mines/{mine_id}/exec-summary")
def exec_summary(mine_id: str):
    mine = _get_mine(mine_id)

    exec_rows = _data(
        sb().table("exec_summary_rows")
            .select("*")
            .eq("mine_id", mine_id)
            .order("row_index")
            .execute()
    )

    commodities = _get_commodities(mine_id)
    comm_ids = [c["id"] for c in commodities]
    scenarios = _get_scenarios_for_comms(comm_ids)
    scen_ids = [s["id"] for s in scenarios]
    metrics_map = _get_metrics_map(scen_ids)

    # flat scenario_returns list for the contribution grid
    scenario_returns = []
    for s in scenarios:
        comm = next((c for c in commodities if c["id"] == s["commodity_id"]), {})
        m = metrics_map.get(s["id"]) or {}
        scenario_returns.append({
            "commodity":    comm.get("commodity"),
            "is_primary":   comm.get("is_primary"),
            "has_scenarios":comm.get("has_scenarios"),
            "scenario_id":  s["id"],
            "scenario":     s["scenario"],
            "sheet_name":   s["sheet_name"],
            "price_base":   s["price_base"],
            "price_unit":   s["price_unit"],
            "basis_notes":  s["basis_notes"],
            "npv":          m.get("npv"),
            "irr":          m.get("irr"),
            "payback":      m.get("payback"),
            "moic":         m.get("moic"),
            "total_capex":  m.get("total_capex"),
            "total_lom_fcf":m.get("total_lom_fcf"),
        })

    # group exec_rows by section
    exec_sections: dict = {}
    for r in exec_rows:
        sec = r["section"] or "General"
        exec_sections.setdefault(sec, []).append(r)

    fm = _data(
        sb().table("financial_models")
            .select("*").eq("mine_id", mine_id).limit(1).execute()
    )
    return {
        "mine":             mine,
        "financial_model":  fm[0] if fm else None,
        "exec_sections":    exec_sections,
        "scenario_returns": scenario_returns,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /mines/{mine_id}/scenarios
# Bear/Base/Bull comparison: commodities nested with scenario metrics
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/mines/{mine_id}/scenarios")
def get_scenarios(mine_id: str):
    mine_rows = _data(
        sb().table("mines")
            .select("id,mine_name,license_number,wacc,tax_rate")
            .eq("id", mine_id).limit(1).execute()
    )
    if not mine_rows:
        _raise(f"Mine '{mine_id}' not found")

    commodities = _get_commodities(mine_id)
    comm_ids = [c["id"] for c in commodities]
    scenarios = _get_scenarios_for_comms(comm_ids)
    scen_ids = [s["id"] for s in scenarios]
    metrics_map = _get_metrics_map(scen_ids)

    result = []
    for c in commodities:
        scens = [s for s in scenarios if s["commodity_id"] == c["id"]]
        scen_data = []
        for s in scens:
            m = metrics_map.get(s["id"]) or {}
            scen_data.append({
                "scenario_id":   s["id"],
                "scenario":      s["scenario"],
                "sheet_name":    s["sheet_name"],
                "wacc":          s["wacc"],
                "price_base":    s["price_base"],
                "price_unit":    s["price_unit"],
                "basis_notes":   s["basis_notes"],
                "npv":           m.get("npv"),
                "irr":           m.get("irr"),
                "payback":       m.get("payback"),
                "moic":          m.get("moic"),
                "total_capex":   m.get("total_capex"),
                "total_lom_fcf": m.get("total_lom_fcf"),
            })
        result.append({
            "commodity_id":   c["id"],
            "commodity":      c["commodity"],
            "is_primary":     c["is_primary"],
            "has_scenarios":  c["has_scenarios"],
            "scenarios":      scen_data,
        })

    return {"mine": mine_rows[0], "commodities": result}


# ─────────────────────────────────────────────────────────────────────────────
# GET /mines/{mine_id}/dcf/{scenario_id}
# Full year-by-year DCF for one commodity scenario
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/mines/{mine_id}/dcf/{scenario_id}")
def get_dcf(mine_id: str, scenario_id: str, source: str = "auto"):
    """
    source = 'auto'      → prefer user_calc if it exists, else ingested
    source = 'ingested'  → always return original ingested data
    source = 'user_calc' → return user-calculated data only
    Response includes has_user_calc and active_source so UI can show toggle.
    """
    scen_rows = _data(
        sb().table("commodity_scenarios")
            .select("*").eq("id", scenario_id).limit(1).execute()
    )
    if not scen_rows:
        _raise(f"Scenario '{scenario_id}' not found")
    scen = scen_rows[0]

    comm_rows = _data(
        sb().table("mine_commodities")
            .select("*")
            .eq("id", scen["commodity_id"])
            .eq("mine_id", mine_id)
            .limit(1)
            .execute()
    )
    if not comm_rows:
        _raise(f"Scenario does not belong to mine '{mine_id}'", 403)

    client = sb()

    # Check if user_calc DCF exists
    has_user_calc = bool(_data(
        client.table("dcf_years").select("id")
              .eq("scenario_id", scenario_id).eq("source", "user_calc")
              .limit(1).execute()
    ))

    # Resolve which source to actually serve
    if source == "auto":
        active_source = "user_calc" if has_user_calc else "ingested"
    else:
        active_source = source

    # Fetch years for active source
    years = _data(
        client.table("dcf_years").select("*")
              .eq("scenario_id", scenario_id)
              .eq("source", active_source)
              .order("year").execute()
    )
    # Fallback: if requested source has no rows, return ingested
    if not years and active_source != "ingested":
        active_source = "ingested"
        years = _data(
            client.table("dcf_years").select("*")
                  .eq("scenario_id", scenario_id)
                  .eq("source", "ingested")
                  .order("year").execute()
        )

    # Fetch metrics — prefer user_scenario_metrics when active_source=user_calc
    metrics = None
    if active_source == "user_calc":
        user_met = _data(
            client.table("user_scenario_metrics").select("*")
                  .eq("scenario_id", scenario_id).limit(1).execute()
        )
        metrics = user_met[0] if user_met else None
    if metrics is None:
        orig = _data(
            client.table("scenario_metrics").select("*")
                  .eq("scenario_id", scenario_id).limit(1).execute()
        )
        metrics = orig[0] if orig else None

    return {
        "scenario":       {**scen, "commodity": comm_rows[0]["commodity"]},
        "metrics":        metrics,
        "years":          years,
        "active_source":  active_source,
        "has_user_calc":  has_user_calc,
    }


# ══════════════════════════════════════════════════════════════════════════════
# DCF ENGINE  (used by calculate endpoint for user-created / edited mines)
# ══════════════════════════════════════════════════════════════════════════════

def _calc_irr_py(fcfs: list):
    has_neg = any(f < -0.001 for f in fcfs)
    has_pos = any(f >  0.001 for f in fcfs)
    if not has_neg or not has_pos:
        return None
    def npv_at(r):
        return sum(cf / (1.0 + r) ** t for t, cf in enumerate(fcfs))
    lo, vlo = -0.45, None
    try:
        vlo = npv_at(lo)
        hi = None
        for h in [0.3, 0.8, 1.5, 3.0, 6.0, 12.0]:
            if vlo * npv_at(h) <= 0:
                hi = h; break
        if hi is None:
            return None
        a, b, va = lo, hi, vlo
        for _ in range(80):
            mid = (a + b) / 2
            if b - a < 1e-8: return mid
            vm = npv_at(mid)
            if abs(vm) < 0.01: return mid
            if va * vm <= 0: b = mid
            else: a, va = mid, vm
        return (a + b) / 2
    except Exception:
        return None


def _run_dcf(mine: dict, scenario: dict):
    """
    Compute year-by-year DCF rows and summary metrics.
    Per-scenario fields (schema_v3) take priority over mine-level fallbacks.
    """
    def _f(key_scen, key_mine=None, default=0.0):
        v = scenario.get(key_scen)
        if v is not None:
            return float(v)
        if key_mine:
            v = mine.get(key_mine)
            if v is not None:
                return float(v)
        return float(default)

    wacc          = _f("wacc",                 "wacc",                    0.15)
    tax           = float(mine.get("tax_rate") or 0.32)
    royalty       = _f("royalty_rate",         "royalty_rate",            0.03)
    lom           = int(mine.get("life_of_mine_yr") or 15)
    price0        = _f("price_base",           None,                      0.0)
    price_esc     = _f("price_escalation_rate", None,                     0.0)
    ann_prod      = _f("annual_production",    None,                      0.0)
    dev_cap       = _f("initial_capex",        "initial_dev_capex",       0.0)
    sust_capex    = _f("sustaining_capex_pa",  None,                      0.0)
    opex_ss       = _f("opex",                 "total_opex_steady_state", 0.0)
    opex_esc      = _f("opex_escalation_rate", "opex_escalation_rate",    0.02)
    closure       = float(mine.get("closure_rehab_cost") or 0)
    capex_yr      = int(scenario.get("capex_deployment_year") or 0)
    prod_start    = int(scenario.get("production_start_year") or 1)
    ramps         = [
        float(mine.get("ramp_up_y1") or 0.5),
        float(mine.get("ramp_up_y2") or 0.8),
        float(mine.get("ramp_up_y3") or 1.0),
    ]

    # Depreciation: use explicit dep_pa if set, else spread dev_cap over dep_years
    dep_pa_explicit = scenario.get("depreciation_pa")
    dep_yrs         = max(1, int(mine.get("avg_depreciation_years") or lom))
    dep_pa_computed = dev_cap / dep_yrs if dev_cap > 0 else 0.0
    dep_pa          = float(dep_pa_explicit) if dep_pa_explicit is not None else dep_pa_computed

    rows, cum_fcf = [], 0.0
    for t in range(lom + 1):
        # Production ramp-up (relative to production_start_year)
        t_prod = t - prod_start + 1  # years since production started
        if t < prod_start:
            rf = 0.0
        elif t_prod <= 3:
            rf = ramps[t_prod - 1]
        else:
            rf = 1.0

        price   = price0 * ((1 + price_esc) ** max(0, t - prod_start)) if t >= prod_start else price0
        prod    = ann_prod * rf
        gr      = prod * price
        roy_amt = gr * royalty
        net_rev = gr - roy_amt

        # OPEX escalates from production start
        if t < prod_start:
            opex = 0.0
        else:
            opex = opex_ss * rf * ((1 + opex_esc) ** max(0, t_prod - 1))

        ebitda  = net_rev - opex

        # Depreciation applies from year after capex deployment
        dep_start = capex_yr + 1
        dep = dep_pa if dep_start <= t <= (dep_start + dep_yrs - 1) else 0.0

        ebit    = ebitda - dep
        tax_amt = max(0.0, ebit * tax)

        # CAPEX: initial at capex_yr, sustaining from production start, closure at end
        capex = 0.0
        if t == capex_yr:
            capex -= dev_cap
        if t >= prod_start and sust_capex > 0:
            capex -= sust_capex
        if t == lom and closure > 0:
            capex -= closure

        fcf     = ebit - tax_amt + dep + capex
        cum_fcf += fcf
        df      = 1.0 / (1.0 + wacc) ** t
        rows.append({
            "year": t,
            "production":       round(prod, 4),
            "commodity_price":  round(price, 4),
            "gross_revenue":    round(gr, 2),
            "royalty":          round(roy_amt, 2),
            "net_revenue":      round(net_rev, 2),
            "operating_costs":  round(opex, 2),
            "ebitda":           round(ebitda, 2),
            "ebitda_margin":    round(ebitda / gr if gr > 0 else 0, 4),
            "depreciation":     round(dep, 2),
            "ebit":             round(ebit, 2),
            "income_tax":       round(tax_amt, 2),
            "capex":            round(capex, 2),
            "free_cash_flow":   round(fcf, 2),
            "cumulative_fcf":   round(cum_fcf, 2),
            "discount_factor":  round(df, 6),
            "discounted_cf":    round(fcf * df, 2),
        })

    fcfs    = [r["free_cash_flow"] for r in rows]
    npv_val = sum(r["discounted_cf"] for r in rows)
    irr     = _calc_irr_py(fcfs)
    payback = next((r["year"] for r in rows if r["cumulative_fcf"] >= 0 and r["year"] > 0), None)
    neg_sum = sum(-f for f in fcfs if f < 0) or 1
    pos_sum = sum( f for f in fcfs if f > 0)

    metrics = {
        "npv":           round(npv_val / 1e6, 4),
        "irr":           round(irr, 6) if irr is not None else None,
        "payback":       f"{payback} year(s)" if payback else "Beyond LOM",
        "moic":          round(pos_sum / neg_sum, 4) if neg_sum else None,
        "total_capex":   round(dev_cap / 1e6, 4),
        "total_lom_fcf": round(sum(fcfs) / 1e6, 4),
    }
    return rows, metrics


# ─────────────────────────────────────────────────────────────────────────────
# POST /mines  —  create a new mine (blank or with initial commodities)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/mines")
def create_mine(body: Dict[str, Any] = Body(...)):
    mine_fields = {k: v for k, v in body.items()
                   if k in _PATCHABLE | {"mine_name", "license_number", "country", "province",
                                          "ore_reserve", "reserve_unit", "throughput_pa",
                                          "throughput_unit", "life_of_mine_yr", "wacc", "tax_rate"}
                   and v is not None}
    mine_fields.setdefault("mine_name", "New Mine")
    mine_fields.setdefault("license_number", f"DRAFT-{int(__import__('time').time())}")
    mine_fields["is_user_created"] = True

    result = sb().table("mines").insert(mine_fields).execute()
    new_mine = result.data[0]
    mine_id  = new_mine["id"]

    # Optionally create commodities passed in body
    for comm_data in (body.get("commodities") or []):
        _create_commodity(mine_id, comm_data)

    return _get_mine(mine_id)


def _create_commodity(mine_id: str, comm_data: dict):
    """Insert mine_commodity + commodity_scenario(s) rows."""
    client = sb()
    comm_result = client.table("mine_commodities").insert({
        "mine_id":      mine_id,
        "commodity":    comm_data.get("commodity", "Unknown"),
        "is_primary":   comm_data.get("is_primary", False),
        "has_scenarios": comm_data.get("has_scenarios", False),
    }).execute()
    comm_id = comm_result.data[0]["id"]

    for scen in (comm_data.get("scenarios") or [{"scenario": "Single"}]):
        client.table("commodity_scenarios").insert({
            "commodity_id":        comm_id,
            "scenario":            scen.get("scenario", "Single"),
            "wacc":                scen.get("wacc"),
            "price_base":          scen.get("price_base"),
            "price_unit":          scen.get("price_unit"),
            "annual_production":   scen.get("annual_production"),
            "grade":               scen.get("grade"),
            "grade_unit":          scen.get("grade_unit"),
            "recovery_rate":       scen.get("recovery_rate"),
            "basis_notes":         scen.get("basis_notes"),
        }).execute()
    return comm_id


# ─────────────────────────────────────────────────────────────────────────────
# POST /mines/{mine_id}/commodities  —  add a commodity + scenario
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/mines/{mine_id}/commodities")
def add_commodity(mine_id: str, body: Dict[str, Any] = Body(...)):
    _get_mine(mine_id)
    _create_commodity(mine_id, body)
    return _get_mine(mine_id)


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /mines/{mine_id}/commodities/{comm_id}
# ─────────────────────────────────────────────────────────────────────────────
@app.delete("/mines/{mine_id}/commodities/{comm_id}")
def delete_commodity(mine_id: str, comm_id: str):
    # Verify ownership
    rows = _data(sb().table("mine_commodities").select("id").eq("id", comm_id).eq("mine_id", mine_id).execute())
    if not rows:
        _raise(f"Commodity '{comm_id}' not found on mine '{mine_id}'", 404)
    sb().table("mine_commodities").delete().eq("id", comm_id).execute()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /mines/{mine_id}/scenarios/{scen_id}  —  update price / grade / production
# ─────────────────────────────────────────────────────────────────────────────
_SCEN_PATCHABLE = {
    "wacc","price_base","price_unit","basis_notes","annual_production",
    "grade","grade_unit","recovery_rate","scenario","sheet_name",
    # per-scenario DCF inputs (schema_v3)
    "price_escalation_rate","opex","opex_escalation_rate","initial_capex",
    "sustaining_capex_pa","depreciation_pa","capex_deployment_year",
    "production_start_year","royalty_rate",
}

@app.patch("/mines/{mine_id}/scenarios/{scen_id}")
def patch_scenario(mine_id: str, scen_id: str, body: Dict[str, Any] = Body(...)):
    patch = {k: v for k, v in body.items() if k in _SCEN_PATCHABLE}
    if not patch:
        raise HTTPException(status_code=400, detail="No valid scenario fields")
    sb().table("commodity_scenarios").update(patch).eq("id", scen_id).execute()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# POST /mines/{mine_id}/calculate  —  simulation only, nothing written to DB
# Returns { results: [{ scenario_id, commodity, scenario, metrics, years }] }
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/mines/{mine_id}/calculate")
def calculate_mine(mine_id: str):
    mine  = _get_mine(mine_id)
    comms = _get_commodities(mine_id)
    if not comms:
        _raise("No commodities found — add at least one commodity first")

    results = []
    for comm in comms:
        scens = _data(
            sb().table("commodity_scenarios").select("*")
                .eq("commodity_id", comm["id"]).execute()
        )
        for scen in scens:
            year_rows, metrics = _run_dcf(mine, scen)
            results.append({
                "scenario_id": scen["id"],
                "commodity":   comm["commodity"],
                "scenario":    scen["scenario"],
                "metrics":     metrics,
                "years":       year_rows,
            })

    return {"results": results}


# ── Sensitivity Analysis ──────────────────────────────────────────────────────

_SENS_PARAMS = [
    {"key": "price",    "label": "Commodity Price",      "component": "revenue"},
    {"key": "opex",     "label": "Operating Costs",      "component": "opex"},
    {"key": "capex",    "label": "Capital Expenditure",  "component": "capex"},
    {"key": "wacc",     "label": "Discount Rate (WACC)", "component": "wacc"},
    {"key": "tax",      "label": "Corporate Tax Rate",   "component": "tax"},
    {"key": "royalty",  "label": "Royalty Rate",         "component": "royalty"},
]


def _compute_npv_irr(fcfs: list, wacc: float):
    npv = sum(fcf / (1 + wacc) ** (i + 1) for i, fcf in enumerate(fcfs))
    irr = None
    try:
        def pv(r):
            return sum(fcf / (1 + r) ** (i + 1) for i, fcf in enumerate(fcfs))
        lo, hi = -0.99, 50.0
        if pv(lo) * pv(hi) < 0:
            for _ in range(100):
                mid = (lo + hi) / 2
                if pv(mid) > 0:
                    lo = mid
                else:
                    hi = mid
                if hi - lo < 1e-7:
                    break
            irr = (lo + hi) / 2
    except Exception:
        pass
    return round(npv, 6), irr


def _vary_fcfs(years: list, component: str, delta: float) -> list:
    fcfs = []
    for y in years:
        fcf = y.get("free_cash_flow") or 0
        if component == "revenue":
            fcf += (y.get("net_revenue") or 0) * delta
        elif component == "opex":
            fcf -= abs(y.get("operating_costs") or 0) * delta
        elif component == "capex":
            fcf -= abs(y.get("capex") or 0) * delta
        elif component == "tax":
            fcf -= abs(y.get("income_tax") or 0) * delta
        elif component == "royalty":
            fcf -= abs(y.get("gross_revenue") or 0) * 0.02 * delta
        fcfs.append(fcf)
    return fcfs


@app.get("/mines/{mine_id}/sensitivity")
def sensitivity_analysis(mine_id: str, variation: float = 0.20, metric: str = "npv"):
    mine  = _get_mine(mine_id)
    comms = _get_commodities(mine_id)
    if not comms:
        _raise("No commodities found for this mine")
    primary = next((c for c in comms if c.get("is_primary")), comms[0])

    # Prefer Base or Single scenario
    scens = _data(
        sb().table("commodity_scenarios").select("*")
            .eq("commodity_id", primary["id"])
            .in_("scenario", ["Base", "Single"])
            .limit(1).execute()
    )
    if not scens:
        scens = _data(
            sb().table("commodity_scenarios").select("*")
                .eq("commodity_id", primary["id"])
                .order("scenario").limit(1).execute()
        )
    if not scens:
        _raise("No base scenario available for sensitivity analysis")
    scen = scens[0]

    base_m = _data(
        sb().table("scenario_metrics").select("*")
            .eq("scenario_id", scen["id"]).limit(1).execute()
    )
    base_m   = base_m[0] if base_m else {}
    base_val = base_m.get(metric)
    if base_val is None:
        _raise(f"No base '{metric}' value in scenario_metrics")

    years = _data(
        sb().table("dcf_years").select("*")
            .eq("scenario_id", scen["id"]).order("year").execute()
    )
    if not years:
        _raise("No DCF year data available for sensitivity")

    base_wacc = scen.get("wacc") or mine.get("wacc") or 0.10
    base_fcfs = [y.get("free_cash_flow") or 0 for y in years]

    results = []
    for p in _SENS_PARAMS:
        try:
            if p["component"] == "wacc":
                low_npv,  low_irr  = _compute_npv_irr(base_fcfs, base_wacc * (1 - variation))
                high_npv, high_irr = _compute_npv_irr(base_fcfs, base_wacc * (1 + variation))
            else:
                low_npv,  low_irr  = _compute_npv_irr(_vary_fcfs(years, p["component"], -variation), base_wacc)
                high_npv, high_irr = _compute_npv_irr(_vary_fcfs(years, p["component"],  variation), base_wacc)

            low_val  = low_npv  if metric == "npv" else low_irr
            high_val = high_npv if metric == "npv" else high_irr
            if low_val is None or high_val is None:
                continue

            results.append({
                "param":       p["key"],
                "label":       p["label"],
                "base":        base_val,
                "low":         round(low_val,  6),
                "high":        round(high_val, 6),
                "low_change":  round(low_val  - base_val, 6),
                "high_change": round(high_val - base_val, 6),
                "range":       round(abs(high_val - low_val), 6),
            })
        except Exception:
            continue

    results.sort(key=lambda x: x["range"], reverse=True)
    return {
        "mine_id":    mine_id,
        "metric":     metric,
        "base_value": base_val,
        "variation":  variation,
        "parameters": results,
    }

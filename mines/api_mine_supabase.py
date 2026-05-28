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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client

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
def get_dcf(mine_id: str, scenario_id: str):
    scen_rows = _data(
        sb().table("commodity_scenarios")
            .select("*").eq("id", scenario_id).limit(1).execute()
    )
    if not scen_rows:
        _raise(f"Scenario '{scenario_id}' not found")
    scen = scen_rows[0]

    # verify the scenario's commodity belongs to this mine
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

    metrics = _data(
        sb().table("scenario_metrics")
            .select("*").eq("scenario_id", scenario_id).limit(1).execute()
    )
    years = _data(
        sb().table("dcf_years")
            .select("*").eq("scenario_id", scenario_id).order("year").execute()
    )

    return {
        "scenario": {**scen, "commodity": comm_rows[0]["commodity"]},
        "metrics":  metrics[0] if metrics else None,
        "years":    years,
    }


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

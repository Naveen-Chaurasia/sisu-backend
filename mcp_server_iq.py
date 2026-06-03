"""
mcp_server_iq.py — NationalEmissionIQ MCP Server
Exposes 5 SISEPUEDE tools so any MCP-compatible client (Claude desktop,
Claude Code, or the /chat endpoint) can fetch exactly what it needs.

Run standalone:
    python mcp_server_iq.py            # stdio transport (Claude desktop / Claude Code)
    python mcp_server_iq.py --sse      # SSE transport on port 8002 (HTTP clients)
"""
import sys, json, asyncio
import numpy as np
from mcp.server.fastmcp import FastMCP

from apiv2 import (
    BASELINES, SECTOR_BL_OUTPUTS, SECTOR_MODELS, SECTOR_META,
    SUBSECTOR_LABELS, _make_years, _load_sector_policies,
    _compute_transport_abatement, _compute_true_sector_abatement,
    _compute_generic_abatement, _calc_trend,
    _build_policy_config,
    get_sector_baseline,
)

# Lazy import of the async summary helper — imported at call time to avoid
# circular-import issues when this file is imported by api_emission_iq.
def _summary_iq():
    from api_emission_iq import _get_emission_summary_iq, COUNTRY_LABELS
    return _get_emission_summary_iq, COUNTRY_LABELS

VALID_COUNTRIES = ["costa_rica", "mexico", "uganda"]
REAL_SECTORS    = ["transport", "waste", "industrial"]
COUNTRY_NAMES   = {"costa_rica": "Costa Rica", "mexico": "Mexico", "uganda": "Uganda"}

mcp = FastMCP("EmissionIQ", instructions=(
    "You are EmissionIQ, a climate data analyst. "
    "Use these tools to fetch real SISEPUEDE model data. "
    "Always fetch only what the question needs. "
    "Keep answers under 120 words and bold key numbers."
))


# ── Tool 1: get_sector_data ───────────────────────────────────────────────────

@mcp.tool()
async def get_sector_data(country: str, sector: str) -> str:
    """
    Fetch SISEPUEDE emission data for one country and sector.
    Returns final-year value, YoY trend (%/yr), and top subsectors.
    Use sector='all' to get a ranked breakdown across transport/waste/industrial.

    country: costa_rica | mexico | uganda
    sector:  transport | waste | industrial | all
    """
    if country not in VALID_COUNTRIES:
        return json.dumps({"error": f"unknown country '{country}'"})

    if sector == "all":
        _iq, COUNTRY_LABELS = _summary_iq()
        summary = await _iq(country)
        rows = [
            f"{SECTOR_META.get(s, {}).get('label', s)}: "
            f"final={info['final_val']:.2f}, share={info['pct_share']}%, "
            f"trend={info['trend_pct']:+.1f}%/yr"
            for s, info in sorted(
                summary["sectors"].items(),
                key=lambda x: -x[1]["pct_share"],
            )
        ]
        return json.dumps({
            "country":     COUNTRY_NAMES.get(country, country),
            "sector":      "all",
            "grand_final": round(summary.get("grand_final_val", 0), 2),
            "sectors":     rows,
            "note":        "SISEPUEDE native units",
        })

    if sector not in REAL_SECTORS:
        return json.dumps({"error": f"sector must be one of {REAL_SECTORS} or 'all'"})

    result = await get_sector_baseline(sector, country, "co2")
    total  = result.get("total", [])
    if not total:
        return json.dumps({"error": "no data returned"})

    arr    = np.array(total, dtype=float)
    by_sub = result.get("by_sub", {})
    top    = sorted(
        [{"sub": SUBSECTOR_LABELS.get(k, k), "val": round(float(v[-1]), 2)}
         for k, v in by_sub.items() if v],
        key=lambda x: -x["val"],
    )[:3]

    return json.dumps({
        "country":        COUNTRY_NAMES.get(country, country),
        "sector":         sector,
        "final_val":      round(float(arr[-1]), 2),
        "trend_pct_yr":   round(_calc_trend(arr), 2),
        "top_subsectors": top,
        "note":           "SISEPUEDE native units",
    })


# ── Tool 2: compare_countries ────────────────────────────────────────────────

@mcp.tool()
async def compare_countries(countries: list[str], sector: str) -> str:
    """
    Compare the same sector across 2–3 countries side-by-side.
    Useful for questions like "which country emits more?" or cross-country analysis.

    countries: list of 2–3 from [costa_rica, mexico, uganda]
    sector:    transport | waste | industrial | all
    """
    if not countries or len(countries) > 3:
        return json.dumps({"error": "provide 2 or 3 countries"})

    rows = []
    for c in countries[:3]:
        if c not in VALID_COUNTRIES:
            continue
        if sector == "all":
            _iq, COUNTRY_LABELS = _summary_iq()
            s = await _iq(c)
            dom = max(s["sectors"].items(), key=lambda x: x[1]["pct_share"], default=(None, {}))
            rows.append({
                "country":         COUNTRY_NAMES.get(c, c),
                "grand_final":     round(s.get("grand_final_val", 0), 2),
                "dominant_sector": SECTOR_META.get(dom[0], {}).get("label", dom[0]),
                "dominant_share":  dom[1].get("pct_share", 0),
            })
        else:
            result = await get_sector_baseline(sector, c, "co2")
            total  = result.get("total", [])
            arr    = np.array(total, dtype=float) if total else np.zeros(1)
            rows.append({
                "country":   COUNTRY_NAMES.get(c, c),
                "final_val": round(float(arr[-1]), 2),
                "trend_pct": round(_calc_trend(arr), 2),
            })

    rows.sort(key=lambda x: -x.get("final_val", x.get("grand_final", 0)))
    return json.dumps({
        "sector":     sector,
        "comparison": rows,
        "ranked":     "highest → lowest",
    })


# ── Tool 3: get_trend_analysis ───────────────────────────────────────────────

@mcp.tool()
async def get_trend_analysis(country: str, sector: str) -> str:
    """
    Get a time-series trend for a sector: values at key milestones
    (2015, 2023, 2030, 2040, 2050), overall growth rate, and whether the
    sector is accelerating or decelerating toward 2050.

    country: costa_rica | mexico | uganda
    sector:  transport | waste | industrial
    """
    if country not in VALID_COUNTRIES:
        return json.dumps({"error": f"unknown country '{country}'"})
    if sector not in REAL_SECTORS:
        return json.dumps({"error": f"sector must be one of {REAL_SECTORS}"})

    result = await get_sector_baseline(sector, country, "co2")
    total  = result.get("total", [])
    years  = result.get("years", [])
    if not total or not years:
        return json.dumps({"error": "no data"})

    arr = np.array(total, dtype=float)
    milestones: dict = {}
    for yr in [2015, 2023, 2030, 2040, 2050]:
        idx = min(range(len(years)), key=lambda i: abs(years[i] - yr))
        milestones[str(yr)] = round(float(arr[idx]), 2)

    mid   = len(arr) // 2
    t1    = _calc_trend(arr[:mid])
    t2    = _calc_trend(arr[mid:])
    traj  = "accelerating" if t2 > t1 + 0.1 else ("decelerating" if t2 < t1 - 0.1 else "stable")

    return json.dumps({
        "country":              COUNTRY_NAMES.get(country, country),
        "sector":               sector,
        "milestones":           milestones,
        "overall_trend_pct_yr": round(_calc_trend(arr), 2),
        "trajectory":           traj,
        "note":                 "SISEPUEDE native units",
    })


# ── Tool 4: search_policies ──────────────────────────────────────────────────

@mcp.tool()
async def search_policies(sector: str, keyword: str = "") -> str:
    """
    Search and filter mitigation policies for a sector.
    Use keyword to narrow by type (e.g. 'EV', 'efficiency', 'methane', 'renewable').
    Returns policy IDs, names, categories, and short descriptions.

    sector:  transport | waste | industrial
    keyword: optional filter string
    """
    if sector not in REAL_SECTORS:
        return json.dumps({"error": f"sector must be one of {REAL_SECTORS}"})

    policies = _load_sector_policies(sector) or []
    kw = keyword.lower()
    if kw:
        policies = [
            p for p in policies
            if kw in p.get("name", "").lower()
            or kw in p.get("description", "").lower()
            or kw in p.get("category", "").lower()
        ]

    return json.dumps({
        "sector":   sector,
        "keyword":  keyword or "none",
        "count":    len(policies),
        "policies": [
            {
                "id":       p["id"],
                "name":     p["name"],
                "category": p.get("category", ""),
                "desc":     p.get("description", "")[:80],
            }
            for p in policies[:8]
        ],
    })


# ── Tool 5: run_policy ───────────────────────────────────────────────────────

@mcp.tool()
async def run_policy(country: str, sector: str, policy_id: str) -> str:
    """
    Simulate a single policy and return its emission reduction % vs BAU at end of projection.
    Get policy IDs first with search_policies.

    country:   costa_rica | mexico | uganda
    sector:    transport | waste | industrial
    policy_id: policy identifier from search_policies
    """
    if country not in VALID_COUNTRIES:
        return json.dumps({"error": f"unknown country '{country}'"})
    if sector not in REAL_SECTORS:
        return json.dumps({"error": f"sector must be one of {REAL_SECTORS}"})

    policies = _load_sector_policies(sector) or []
    pol = next((p for p in policies if p["id"] == policy_id), None)
    if not pol:
        return json.dumps({"error": f"policy '{policy_id}' not found — use search_policies to list IDs"})

    cfg = _build_policy_config(pol)
    has_true = (
        sector in SECTOR_MODELS
        and country in SECTOR_BL_OUTPUTS
        and sector in SECTOR_BL_OUTPUTS.get(country, {})
    )

    if sector == "transport":
        out = _compute_transport_abatement(country, "co2", cfg)
    elif has_true:
        out = _compute_true_sector_abatement(country, sector, "co2", cfg)
    else:
        out = _compute_generic_abatement(country, cfg)

    return json.dumps({
        "country":       COUNTRY_NAMES.get(country, country),
        "sector":        sector,
        "policy":        pol["name"],
        "policy_id":     policy_id,
        "reduction_pct": round(float(out.get("final_reduction_pct", 0)), 2),
        "note":          "% reduction vs BAU at end of projection period",
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--sse" in sys.argv:
        # HTTP/SSE transport — for use with Anthropic API mcp_servers parameter
        # or any HTTP-based MCP client
        mcp.run(transport="sse", host="0.0.0.0", port=8002)
    else:
        # stdio transport — for Claude desktop / Claude Code
        mcp.run(transport="stdio")

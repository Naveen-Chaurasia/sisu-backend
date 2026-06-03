"""
api_emission_iq.py — NationalEmissionIQ
Factiq-style investigative analysis powered entirely by real SISEPUEDE baselines.
No hardcoded emission values — all data comes from the engine.
"""
import os, json, math, re
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import numpy as np

# ── Import real SISEPUEDE objects from apiv2 ──────────────────────────────────
from apiv2 import (
    BASELINES, SECTOR_BL_OUTPUTS, SECTOR_MODELS, SECTOR_META,
    SUBSECTOR_LABELS, DETAIL_LABELS, _make_years, _load_sector_policies,
    _compute_transport_abatement, _compute_true_sector_abatement,
    _compute_generic_abatement, _calc_trend,
    _build_policy_config,
    get_sector_baseline,
)

app = FastAPI(title="NationalEmissionIQ")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_client = None

def _get_client():
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=key)
    return _client

# ── Topic → SISEPUEDE sector mapping ─────────────────────────────────────────
TOPIC_TO_SECTOR = {
    "transport": "transport",
    "industry":  "industrial",
    "waste":     "waste",
    "all":       None,
}

COUNTRY_LABELS = {
    "costa_rica": "Costa Rica",
    "mexico":     "Mexico",
    "uganda":     "Uganda",
}

# NDC context (from official NDC filings — not emission numbers, just policy targets)
NDC_CONTEXT = {
    "costa_rica": "NDC target: -55% vs BAU by 2030, net-zero by 2050. 99% renewable electricity grid.",
    "mexico":     "NDC target: -35% unconditional / -70% conditional by 2030. Largest economy in Latin America.",
    "uganda":     "NDC target: -24.7% vs BAU by 2030. 84% hydropower grid. Fastest-growing transport in East Africa.",
}

ANALYSIS_STEPS = [
    "Starting analysis",
    "Loading SISEPUEDE emission data",
    "Mapping 3 investigative angles",
    "Investigating angles in parallel",
    "Generating insight narrative",
    "Analysis complete",
]


# ── Emission summary — same code path as National Emission Report ─────────────

# The 3 sectors with confirmed real (non-proxy) SISEPUEDE data
_REAL_SECTORS = ["transport", "waste", "industrial"]


async def _get_emission_summary_iq(region: str) -> dict:
    """Build sector summary by calling get_sector_baseline for each real sector.
    This is the exact same code path the National Emission Report uses."""
    years = _make_years(len(BASELINES.get(region, BASELINES["costa_rica"])["df"]))
    n = len(years)
    out_raw: dict = {}

    for sec in _REAL_SECTORS:
        try:
            result = await get_sector_baseline(sec, region, "co2")
            total = result.get("total", [])
            if not total:
                continue
            _emit_type = result.get("emission_type", "unknown")

            arr = np.array(total, dtype=float)

            # transport → breakdown by transport mode; others → by subsector prefix or detail
            top_sub: list = []
            if sec == "transport":
                mode_totals: dict = {}
                for scope_key in ("scope1", "scope2", "scope3"):
                    for mode, series in result.get("by_mode", {}).get(scope_key, {}).items():
                        mode_totals[mode] = mode_totals.get(mode, 0) + (
                            float(series[-1]) if series else 0.0
                        )
                top_sub = [
                    {"name": m, "val": round(v, 2)}
                    for m, v in sorted(mode_totals.items(), key=lambda x: -x[1])[:6]
                    if v > 0
                ]
            else:
                by_sub    = result.get("by_sub", {})
                by_detail = result.get("by_detail", {})
                # If only one prefix (e.g. industrial/ippu), go deeper into by_detail
                if len(by_sub) <= 1 and by_detail:
                    for det_dict in by_detail.values():
                        for det_key, series in det_dict.items():
                            val = float(series[-1]) if series else 0.0
                            if val > 0:
                                label = DETAIL_LABELS.get(
                                    det_key,
                                    det_key.replace("_", " ").title(),
                                )
                                top_sub.append({"name": label, "val": round(val, 2)})
                    top_sub.sort(key=lambda x: -x["val"])
                    top_sub = top_sub[:6]
                # Multi-prefix sectors (waste: waso/wali/trww) → use by_sub labels
                if not top_sub:
                    for pfx, series in by_sub.items():
                        val = float(series[-1]) if series else 0.0
                        top_sub.append({"name": SUBSECTOR_LABELS.get(pfx, pfx), "val": round(val, 2)})
                    top_sub.sort(key=lambda x: -x["val"])
                    top_sub = top_sub[:6]

            out_raw[sec] = {"arr": arr, "top_sub": top_sub, "emission_type": _emit_type}
        except Exception as _e:
            print(f"[iq] get_sector_baseline {sec}/{region}: {_e}")

    if not out_raw:
        return {"region": region, "years": years, "sectors": {},
                "grand_total_series": [0.0] * n, "grand_final_val": 0.0}

    grand = np.zeros(n)
    for s in out_raw.values():
        a = s["arr"]
        grand[:len(a)] += a[:n]
    grand_final = float(grand[-1]) if grand[-1] > 0 else 1.0

    sectors: dict = {}
    for sec, info in out_raw.items():
        arr       = info["arr"]
        final_val = float(arr[-1])
        sectors[sec] = {
            "series":       [round(float(x), 4) for x in arr],
            "final_val":    round(final_val, 4),
            "pct_share":    round(final_val / grand_final * 100, 1),
            "trend_pct":    _calc_trend(arr),
            "top_sub":      info["top_sub"],
            "emission_type": info.get("emission_type", "unknown"),
        }

    return {
        "region":             region,
        "years":              years,
        "grand_total_series": [round(float(x), 4) for x in grand],
        "grand_final_val":    round(grand_final, 4),
        "sectors":            sectors,
    }


# ── Context builder ───────────────────────────────────────────────────────────

def _build_context(summary: dict, topic_sector: Optional[str], question: str,
                   policies: list, policy_result: Optional[dict]) -> str:
    country  = summary["region"]
    years    = summary["years"]
    sectors  = summary["sectors"]
    y_start  = years[0]
    y_end    = years[-1]
    # Find the year closest to 2023 (current baseline)
    mid_idx  = min(range(len(years)), key=lambda i: abs(years[i] - 2023))
    y_mid    = years[mid_idx]

    # ── Grand total context ──
    grand    = summary.get("grand_total_series", [])
    g_total  = grand[mid_idx] if len(grand) > mid_idx else 0

    lines = [
        f"Country: {COUNTRY_LABELS.get(country, country)}",
        f"SISEPUEDE baseline range: {y_start}–{y_end} | Reference year: {y_mid}",
        f"Context: {NDC_CONTEXT.get(country, '')}",
        f"Grand total emissions at {y_mid} (SISEPUEDE native units): {g_total:.2f}",
        "",
    ]

    if topic_sector:
        # ── Focused mode: only show the selected sector, skip all others ──
        sec_label = SECTOR_META.get(topic_sector, {}).get("label", topic_sector)
        lines.append(f"=== FOCUS: {sec_label.upper()} SECTOR ONLY ===")
        lines.append(f"IMPORTANT: Your entire analysis must be about the {sec_label} sector only.")
        if topic_sector in sectors:
            info   = sectors[topic_sector]
            pct    = info.get("pct_share", 0)
            trend  = info.get("trend_pct", 0)
            val    = info.get("final_val", 0)
            sign   = "+" if trend >= 0 else ""
            lines.append(f"  Share of total: {pct}%")
            lines.append(f"  Trend: {sign}{trend}%/yr")
            lines.append(f"  Value at {y_mid}: {val:.2f}")
            ser    = info.get("series", [])
            if len(ser) > mid_idx:
                recent = ser[max(0, mid_idx-4):mid_idx+1]
                lines.append(f"  Recent time-series (last 5 steps): {[round(v,2) for v in recent]}")
            lines.append(f"  Sub-component breakdown:")
            for sub in info.get("top_sub", []):
                lines.append(f"    - {sub['name']}: {sub['val']:.2f}")
    else:
        # ── All-sectors mode ──
        lines.append("=== SECTOR BREAKDOWN AT REFERENCE YEAR (% share + YoY trend) ===")
        sorted_secs = sorted(sectors.items(), key=lambda x: -x[1].get("pct_share", 0))
        for sec, info in sorted_secs:
            label  = SECTOR_META.get(sec, {}).get("label", sec)
            pct    = info.get("pct_share", 0)
            trend  = info.get("trend_pct", 0)
            val    = info.get("final_val", 0)
            top    = ", ".join(s["name"] for s in info.get("top_sub", [])[:3])
            sign   = "+" if trend >= 0 else ""
            lines.append(f"  {label}: {pct}% of total | {sign}{trend}%/yr | value={val:.2f} | subsectors: {top}")

    # ── Policies ──
    if policies:
        lines.append("\n=== AVAILABLE POLICIES FOR THIS SECTOR ===")
        for p in policies[:6]:
            lines.append(f"  - {p.get('name','')}: {p.get('description','')[:80]}")

    # ── Policy simulation result ──
    if policy_result and not policy_result.get("error"):
        pct = policy_result.get("final_reduction_pct", 0)
        pname = policy_result.get("policy_name", "top policy")
        lines.append(f"\n=== SISEPUEDE POLICY SIMULATION: {pname} ===")
        lines.append(f"  Abatement at end-year: {pct}% reduction vs BAU")

    lines.append(f"\nUser question: {question}")
    return "\n".join(lines)


# ── Chart data builder ────────────────────────────────────────────────────────

def _build_chart_data(summary: dict, topic_sector: Optional[str],
                      policy_result: Optional[dict]) -> list:
    """Returns per-year rows for the trend line chart."""
    years  = summary["years"]
    # Use focused sector if given, else grand total
    if topic_sector and topic_sector in summary["sectors"]:
        series = summary["sectors"][topic_sector]["series"]
    else:
        series = summary.get("grand_total_series", [0.0] * len(years))

    mid_idx = min(range(len(years)), key=lambda i: abs(years[i] - 2023))

    pol_series = None
    if policy_result and "policy" in policy_result and not policy_result.get("error"):
        pol_series = policy_result["policy"]

    rows = []
    for i, yr in enumerate(years):
        row = {"year": yr}
        val = series[i] if i < len(series) else None
        if val is not None:
            if i <= mid_idx:
                row["historical"] = round(float(val), 4)
                row["bau"] = round(float(val), 4)
            else:
                row["bau"] = round(float(val), 4)
        if pol_series and i < len(pol_series):
            row["policy"] = round(float(pol_series[i]), 4)
        rows.append(row)
    return rows


def _build_sector_chart(summary: dict) -> list:
    """Per-sector breakdown at reference year."""
    years    = summary["years"]
    mid_idx  = min(range(len(years)), key=lambda i: abs(years[i] - 2023))
    rows = []
    for sec, info in summary["sectors"].items():
        label = SECTOR_META.get(sec, {}).get("label", sec)
        ser   = info.get("series", [])
        val   = ser[mid_idx] if len(ser) > mid_idx else info.get("final_val", 0)
        rows.append({
            "sector":    label,
            "val":       round(float(val), 2),
            "pct_share": info.get("pct_share", 0),
            "trend_pct": info.get("trend_pct", 0),
        })
    rows.sort(key=lambda x: -x["val"])
    return rows


# ── Haiku call ────────────────────────────────────────────────────────────────

def _call_haiku(context: str, question: str, topic_label: str) -> dict:
    sector_instruction = (
        f"You MUST analyze ONLY the {topic_label} sector. "
        f"Do NOT mention any other sector in the headline, angles, or narrative. "
        f"Every number you cite must come from the {topic_label} data above.\n"
    ) if topic_label != "All Sectors" else ""

    prompt = (
        f"You are a climate scientist analyzing real SISEPUEDE model outputs.\n"
        f"{sector_instruction}\n"
        f"{context}\n\n"
        "Based ONLY on the data above, return this exact JSON (no markdown wrapper):\n"
        '{"headline":"one bold sentence ≤20 words with specific numbers from the data",'
        '"subtext":"1-2 sentences explaining significance",'
        '"sources":["SISEPUEDE Baseline Model","IPCC AR6","one relevant national source"],'
        '"angles":['
        '{"label":"WHAT","title":"5-8 word title","finding":"key stat from data","detail":"2 sentences using real numbers","confirmed":"4-word verdict"},'
        '{"label":"WHY","title":"5-8 word title","finding":"main driver from data","detail":"2 sentences root cause","confirmed":"4-word verdict"},'
        '{"label":"HOW","title":"5-8 word title","finding":"policy lever or target","detail":"2 sentences on mechanism","confirmed":"4-word verdict"}'
        '],'
        '"narrative":"2-3 paragraphs. Use **bold** for key numbers. Separate paragraphs with \\n\\n. Reference only numbers that appear in the data above."}'
    )
    resp = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json\n"):
            raw = raw[5:]
    return json.loads(raw)


# ── Request / endpoint ────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    country: str
    question: Optional[str] = None
    topic: Optional[str] = None


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    if req.country not in BASELINES:
        raise HTTPException(400, f"Unknown country '{req.country}'. Valid: {list(BASELINES.keys())}")

    country      = req.country
    topic        = req.topic or "all"
    topic_sector = TOPIC_TO_SECTOR.get(topic)
    topic_label  = SECTOR_META.get(topic_sector, {}).get("label", topic) if topic_sector else "All Sectors"
    question     = req.question or f"What are the key emission trends and policy priorities for {COUNTRY_LABELS.get(country, country)}?"
    corrected_question: Optional[str] = None

    # 0. Spell-correct the question before analysis
    if req.question:
        try:
            cr = _get_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system=(
                    "Fix spelling and grammar in the user's question about climate/emissions data. "
                    "Return ONLY the corrected question text — no explanation, no quotes, no punctuation changes beyond spelling. "
                    "If the question needs no correction, return it exactly as-is."
                ),
                messages=[{"role": "user", "content": question}],
            )
            fixed = cr.content[0].text.strip()
            if fixed.lower() != question.lower():
                corrected_question = fixed
                question = fixed
        except Exception as _cr:
            print(f"[iq] analyze spell-correct failed: {_cr}")

    # 1. Pull real SISEPUEDE summary via the same path as National Emission Report
    summary = await _get_emission_summary_iq(country)

    # If the requested sector has no computed data for this country, fall back to all-sectors
    available_sectors = list(summary.get("sectors", {}).keys())
    sector_fallback   = False
    requested_label   = topic_label
    if topic_sector and topic_sector not in summary.get("sectors", {}):
        print(f"[iq] sector '{topic_sector}' not in summary for {country} — falling back to all-sectors")
        sector_fallback = True
        topic_sector    = None
        topic_label     = "All Sectors"

    # 2. Policies for the focused sector
    policies: list = []
    if topic_sector:
        try:
            policies = _load_sector_policies(topic_sector) or []
        except Exception:
            policies = []

    # 3. Run top policy simulation (best effort, non-blocking)
    policy_result: Optional[dict] = None
    if topic_sector and policies:
        try:
            best = policies[0]
            cfg  = _build_policy_config(best)
            if topic_sector == "transport":
                policy_result = _compute_transport_abatement(country, "co2", cfg)
                policy_result["policy_name"] = best.get("name", "")
            elif (topic_sector in SECTOR_MODELS and
                  country in SECTOR_BL_OUTPUTS and
                  topic_sector in SECTOR_BL_OUTPUTS.get(country, {})):
                policy_result = _compute_true_sector_abatement(country, topic_sector, "co2", cfg)
                policy_result["policy_name"] = best.get("name", "")
        except Exception as _pe:
            print(f"[iq] policy sim skipped: {_pe}")

    # 4. Build context from real data
    context = _build_context(summary, topic_sector, question, policies, policy_result)

    # 5. Claude Haiku → narrative
    focus_key = None  # initialised here so it's always defined for the return block
    try:
        analysis = _call_haiku(context, question, topic_label)
    except Exception as _he:
        print(f"[iq] Haiku failed: {_he}")
        sectors = summary.get("sectors", {})
        country_label = COUNTRY_LABELS.get(country, country)
        # Use the selected sector if specified, otherwise fall back to globally largest
        if topic_sector and topic_sector in sectors:
            focus_key  = topic_sector
            focus_info = sectors[topic_sector]
        else:
            focus_entry = max(sectors.items(), key=lambda x: x[1].get("pct_share", 0), default=(None, {}))
            focus_key   = focus_entry[0]
            focus_info  = focus_entry[1]
        focus_name  = SECTOR_META.get(focus_key, {}).get("label", focus_key) if focus_key else "Unknown"
        focus_pct   = focus_info.get("pct_share", 0)
        focus_trend = focus_info.get("trend_pct", 0)
        trend_sign  = "+" if focus_trend >= 0 else ""
        top_subs     = [s['name'] for s in focus_info.get('top_sub', [])[:2]]
        top_subs_str = " and ".join(top_subs) if top_subs else "various subsectors"
        direction    = "declining" if focus_trend < 0 else "growing"
        abs_trend    = abs(focus_trend)
        ndc_text     = NDC_CONTEXT.get(country, '')
        n_policies   = len(policies) if policies else 0

        analysis = {
            "headline": (
                f"{focus_name} accounts for {focus_pct}% of {country_label}'s total emissions "
                f"and is {direction} at {abs_trend}% per year under business-as-usual."
            ),
            "subtext":  f"Based on SISEPUEDE model data. {ndc_text}",
            "sources":  ["SISEPUEDE Baseline Model", "IPCC AR6", "National GHG Inventory"],
            "angles": [
                {"label": "WHAT",
                 "title": f"{focus_name} contributes {focus_pct}% of total emissions",
                 "finding": f"{focus_pct}% of total emissions",
                 "detail": (
                     f"The {focus_name} sector is responsible for {focus_pct}% of {country_label}'s total emissions. "
                     f"The largest contributors are {top_subs_str}."
                 ),
                 "confirmed": "Baseline confirmed"},
                {"label": "WHY",
                 "title": f"Emissions are {direction} at {abs_trend}% per year",
                 "finding": f"{trend_sign}{focus_trend}%/yr trend",
                 "detail": (
                     f"Under a business-as-usual scenario, SISEPUEDE projects {focus_name} emissions to "
                     f"{'decrease' if focus_trend < 0 else 'increase'} by {abs_trend}% per year through 2050. "
                     f"Key drivers include {top_subs_str}."
                 ),
                 "confirmed": "Trend confirmed"},
                {"label": "HOW",
                 "title": f"{n_policies} mitigation {'policy' if n_policies == 1 else 'policies'} available",
                 "finding": f"{n_policies} policies modeled" if n_policies else "Policies available",
                 "detail": (
                     f"{country_label} has {n_policies} modeled mitigation {'option' if n_policies == 1 else 'options'} "
                     f"for the {focus_name} sector. {ndc_text}"
                 ),
                 "confirmed": "Pathway confirmed"},
            ],
            "narrative": (
                f"**{country_label}'s {focus_name}** sector contributes **{focus_pct}% of total national emissions** "
                f"and is currently {direction} at **{abs_trend}% per year** under business-as-usual projections.\n\n"
                f"{ndc_text}"
            ),
        }

    # 6. Build chart data from real SISEPUEDE series
    chart_data   = _build_chart_data(summary, topic_sector, policy_result)
    sector_data  = _build_sector_chart(summary)

    # 7. Subsector breakdown for the focused sector (used in "Breakdown" tab)
    subsector_data: list = []
    if topic_sector and topic_sector in summary["sectors"]:
        top_sub   = summary["sectors"][topic_sector].get("top_sub", [])
        sub_total = sum(s["val"] for s in top_sub) or 1.0
        subsector_data = [
            {
                "name": s["name"],
                "val":  round(s["val"], 2),
                "pct":  round(s["val"] / sub_total * 100, 1),
            }
            for s in top_sub
            if s["val"] > 0
        ]

    # Determine emission_type for the focused sector
    _sector_key = topic_sector or focus_key
    _focus_et   = summary["sectors"].get(_sector_key, {}).get("emission_type", "unknown") if _sector_key else next(
        (v.get("emission_type", "unknown") for v in summary.get("sectors", {}).values()), "unknown"
    )
    _is_real = _focus_et in ("exact", "sisepuede_real")

    return {
        "steps":             ANALYSIS_STEPS,
        "analysis":          analysis,
        "chart_data":        chart_data,
        "sector_data":       sector_data,
        "subsector_data":    subsector_data,
        "country_name":      COUNTRY_LABELS.get(country, country),
        "topic_label":       topic_label,
        "years":             summary["years"],
        "sector_fallback":   sector_fallback,
        "requested_label":   requested_label,
        "available_sectors": [SECTOR_META.get(s, {}).get("label", s) for s in available_sectors],
        "emission_type":       _focus_et,
        "data_is_real":        _is_real,
        "corrected_question":  corrected_question,
        "_context":            context,
    }


# ── Policy batch runner ───────────────────────────────────────────────────────

_POLICY_TRIGGERS = frozenset([
    "polic", "reduc", "abat", "cut", "lower", "decreas", "mitig",
    "intervention", "action", "measure", "what can", "how to", "how can",
    "what should", "recommend", "suggest", "best way",
    # what-if / simulation questions — always show policy cards
    "what if", "if i ", "if we ", "when i ", "if apply",
    "result if", "simulate", "electrif", "deploy", "install",
])

def _is_policy_question(text: str) -> bool:
    tl = text.lower()
    return any(w in tl for w in _POLICY_TRIGGERS)


def _run_policy_batch(country: str, sector: str, top_n: int = 5) -> List[dict]:
    """Run all policies for a sector in parallel, return top N by abatement %."""
    policies = _load_sector_policies(sector) or []
    if not policies:
        return []
    has_true = (sector in SECTOR_MODELS and
                country in SECTOR_BL_OUTPUTS and
                sector in SECTOR_BL_OUTPUTS.get(country, {}))

    def _run_one(p: dict):
        try:
            cfg = _build_policy_config(p)
            if sector == "transport":
                out = _compute_transport_abatement(country, "co2", cfg)
            elif has_true:
                out = _compute_true_sector_abatement(country, sector, "co2", cfg)
            else:
                out = _compute_generic_abatement(country, cfg)
            return {
                "id":            p["id"],
                "name":          p["name"],
                "description":   p.get("description", "")[:120],
                "category":      p.get("category", ""),
                "abatement_pct": round(float(out.get("final_reduction_pct", 0)), 1),
                "baseline":      [round(float(v), 4) for v in out.get("baseline", [])],
                "policy_series": [round(float(v), 4) for v in out.get("policy", [])],
            }
        except Exception:
            return None

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for r in (f.result() for f in as_completed(
                {pool.submit(_run_one, p): p for p in policies})):
            if r and abs(r["abatement_pct"]) > 0.01:
                results.append(r)

    results.sort(key=lambda x: -abs(x["abatement_pct"]))
    return results[:top_n]


def _dominant_sector(country: str) -> str:
    """Return whichever real sector has the most emission columns in the cache.
    Falls back to transport (always computable) when cache is empty."""
    best, best_n = "transport", 0
    for sec in ("waste", "industrial"):
        bl = SECTOR_BL_OUTPUTS.get(country, {}).get(sec, {})
        df = bl.get("df_out")
        if df is not None:
            n = len([c for c in df.columns if "emission" in c.lower()])
            if n > best_n:
                best, best_n = sec, n
    return best


# ── MCP-style tools for agentic /chat ────────────────────────────────────────

_IQ_TOOLS = [
    {
        "name": "get_sector_data",
        "description": (
            "Fetch SISEPUEDE emission data for one country and sector. "
            "Returns final-year value, YoY trend, and top subsectors. "
            "Use sector='all' to get a ranked breakdown across all sectors."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "enum": ["costa_rica", "mexico", "uganda"]},
                "sector":  {"type": "string", "enum": ["transport", "waste", "industrial", "all"]},
            },
            "required": ["country", "sector"],
        },
    },
    {
        "name": "search_policies",
        "description": (
            "Search and filter mitigation policies for a sector. "
            "Use keyword to narrow by type (e.g. 'EV', 'efficiency', 'methane', 'renewable'). "
            "Returns policy IDs needed for run_policy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector":  {"type": "string", "enum": ["transport", "waste", "industrial"]},
                "keyword": {"type": "string", "description": "Optional filter keyword"},
            },
            "required": ["sector"],
        },
    },
    {
        "name": "run_policy",
        "description": (
            "Simulate a single policy and return its emission reduction % vs BAU. "
            "Get policy IDs first with list_policies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country":   {"type": "string", "enum": ["costa_rica", "mexico", "uganda"]},
                "sector":    {"type": "string", "enum": ["transport", "waste", "industrial"]},
                "policy_id": {"type": "string"},
            },
            "required": ["country", "sector", "policy_id"],
        },
    },
    {
        "name": "compare_countries",
        "description": (
            "Compare the same sector across multiple countries side-by-side. "
            "Use this when the user asks which country emits more, "
            "or how countries differ on a specific sector."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "countries": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["costa_rica", "mexico", "uganda"]},
                    "description": "2 or 3 countries to compare",
                },
                "sector": {"type": "string", "enum": ["transport", "waste", "industrial", "all"]},
            },
            "required": ["countries", "sector"],
        },
    },
    {
        "name": "get_trend_analysis",
        "description": (
            "Get a time-series trend for a sector: values at key milestones "
            "(2015, 2023, 2030, 2040, 2050), growth rate, and whether the sector "
            "is accelerating or decelerating. Use for questions about trajectories, "
            "future projections, or whether emissions are improving."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "enum": ["costa_rica", "mexico", "uganda"]},
                "sector":  {"type": "string", "enum": ["transport", "waste", "industrial"]},
            },
            "required": ["country", "sector"],
        },
    },
]


async def _exec_tool(name: str, inp: dict) -> str:
    """Execute an MCP tool call and return a compact JSON string result."""
    try:
        if name == "get_sector_data":
            country = inp["country"]
            sector  = inp.get("sector", "all")
            if sector == "all":
                summary = await _get_emission_summary_iq(country)
                rows = [
                    f"{SECTOR_META.get(s,{}).get('label',s)}: "
                    f"final={info['final_val']:.2f}, share={info['pct_share']}%, "
                    f"trend={info['trend_pct']:+.1f}%/yr"
                    for s, info in sorted(
                        summary["sectors"].items(),
                        key=lambda x: -x[1]["pct_share"]
                    )
                ]
                return json.dumps({
                    "country": country, "sector": "all",
                    "grand_final": round(summary.get("grand_final_val", 0), 2),
                    "sectors": rows,
                    "note": "SISEPUEDE native units",
                })
            else:
                result = await get_sector_baseline(sector, country, "co2")
                total  = result.get("total", [])
                arr    = np.array(total, dtype=float) if total else np.zeros(1)
                by_sub = result.get("by_sub", {})
                top = sorted(
                    [{"sub": SUBSECTOR_LABELS.get(k, k), "val": round(float(v[-1]), 2)}
                     for k, v in by_sub.items() if v],
                    key=lambda x: -x["val"]
                )[:3]
                return json.dumps({
                    "country": country, "sector": sector,
                    "final_val": round(float(arr[-1]), 2),
                    "trend_pct": round(_calc_trend(arr), 2),
                    "top_subsectors": top,
                    "note": "SISEPUEDE native units",
                })

        elif name == "search_policies":
            sector   = inp["sector"]
            keyword  = inp.get("keyword", "").lower()
            policies = _load_sector_policies(sector) or []
            if keyword:
                policies = [
                    p for p in policies
                    if keyword in p.get("name", "").lower()
                    or keyword in p.get("description", "").lower()
                    or keyword in p.get("category", "").lower()
                ]
            return json.dumps({
                "sector":   sector,
                "keyword":  keyword or "none",
                "policies": [
                    {"id": p["id"], "name": p["name"],
                     "category": p.get("category", ""),
                     "desc": p.get("description", "")[:80]}
                    for p in policies[:8]
                ],
            })

        elif name == "run_policy":
            country   = inp["country"]
            sector    = inp["sector"]
            policy_id = inp["policy_id"]
            policies  = _load_sector_policies(sector) or []
            pol = next((p for p in policies if p["id"] == policy_id), None)
            if not pol:
                return json.dumps({"error": f"policy '{policy_id}' not found in {sector}"})
            cfg = _build_policy_config(pol)
            has_true = (sector in SECTOR_MODELS and
                        country in SECTOR_BL_OUTPUTS and
                        sector in SECTOR_BL_OUTPUTS.get(country, {}))
            if sector == "transport":
                out = _compute_transport_abatement(country, "co2", cfg)
            elif has_true:
                out = _compute_true_sector_abatement(country, sector, "co2", cfg)
            else:
                out = _compute_generic_abatement(country, cfg)
            return json.dumps({
                "country": country, "sector": sector, "policy": pol["name"],
                "reduction_pct": round(float(out.get("final_reduction_pct", 0)), 2),
                "note": "% reduction vs BAU at end of projection",
            })

        elif name == "compare_countries":
            countries = inp.get("countries", [])
            sector    = inp.get("sector", "all")
            rows = []
            for c in countries[:3]:
                if sector == "all":
                    s = await _get_emission_summary_iq(c)
                    dominant = max(s["sectors"].items(), key=lambda x: x[1]["pct_share"], default=(None, {}))
                    rows.append({
                        "country":        COUNTRY_LABELS.get(c, c),
                        "grand_final":    round(s.get("grand_final_val", 0), 2),
                        "dominant_sector": SECTOR_META.get(dominant[0], {}).get("label", dominant[0]),
                        "dominant_share": dominant[1].get("pct_share", 0),
                    })
                else:
                    result = await get_sector_baseline(sector, c, "co2")
                    total  = result.get("total", [])
                    arr    = np.array(total, dtype=float) if total else np.zeros(1)
                    rows.append({
                        "country":    COUNTRY_LABELS.get(c, c),
                        "final_val":  round(float(arr[-1]), 2),
                        "trend_pct":  round(_calc_trend(arr), 2),
                    })
            rows.sort(key=lambda x: -x.get("final_val", x.get("grand_final", 0)))
            return json.dumps({"sector": sector, "comparison": rows, "note": "sorted highest→lowest"})

        elif name == "get_trend_analysis":
            country = inp["country"]
            sector  = inp["sector"]
            result  = await get_sector_baseline(sector, country, "co2")
            total   = result.get("total", [])
            years   = result.get("years", [])
            if not total or not years:
                return json.dumps({"error": "no data"})
            arr = np.array(total, dtype=float)
            milestones = [2015, 2023, 2030, 2040, 2050]
            snapshots  = {}
            for yr in milestones:
                idx = min(range(len(years)), key=lambda i: abs(years[i] - yr))
                snapshots[str(yr)] = round(float(arr[idx]), 2)
            # Acceleration: compare first-half trend vs second-half trend
            mid   = len(arr) // 2
            t1    = _calc_trend(arr[:mid])
            t2    = _calc_trend(arr[mid:])
            accel = "accelerating" if t2 > t1 else ("decelerating" if t2 < t1 else "stable")
            return json.dumps({
                "country":    country,
                "sector":     sector,
                "milestones": snapshots,
                "overall_trend_pct_yr": round(_calc_trend(arr), 2),
                "trajectory": accel,
                "note":       "SISEPUEDE native units",
            })

    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({"error": f"unknown tool: {name}"})


# ── Conversational follow-up ──────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str    # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    country: str
    topic: Optional[str] = None
    messages: List[ChatMessage]
    context: Optional[str] = None   # _context from the /analyze response (kept for compat)


@app.post("/chat")
async def chat(req: ChatRequest):
    if req.country not in BASELINES:
        raise HTTPException(400, f"Unknown country '{req.country}'")

    country_label = COUNTRY_LABELS.get(req.country, req.country)
    topic_sector  = TOPIC_TO_SECTOR.get(req.topic or "all")
    topic_label   = SECTOR_META.get(topic_sector, {}).get("label", "All Sectors") if topic_sector else "All Sectors"

    last_user   = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    is_policy_q = _is_policy_question(last_user)

    # Spell-correct the user's question before processing
    corrected_question: Optional[str] = None
    try:
        cr = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=(
                "Fix spelling and grammar in the user's question about climate/emissions data. "
                "Return ONLY the corrected question text — no explanation, no quotes, no punctuation changes beyond spelling. "
                "If the question needs no correction, return it exactly as-is."
            ),
            messages=[{"role": "user", "content": last_user}],
        )
        fixed = cr.content[0].text.strip()
        if fixed.lower() != last_user.lower():
            corrected_question = fixed
            last_user = fixed   # use corrected text in the AI loop
    except Exception as _cr:
        print(f"[iq] spell-correct failed: {_cr}")

    ctx_section = ""
    if req.context:
        ctx_section = (
            "\n\nCURRENT ANALYSIS DATA (SISEPUEDE baseline — use these numbers directly, "
            "do NOT call tools for information already shown here):\n"
            + req.context[:2500]
        )

    system_prompt = (
        f"You are EmissionIQ, a climate analyst. "
        f"The user has already selected: country = {country_label}, sector = {topic_label}. "
        f"ALWAYS use country='{req.country}' and sector='{topic_sector or 'all'}' in every tool call. "
        f"NEVER ask the user which country, sector, amount, or timeframe — these are already known. "
        f"If context is missing, call get_sector_data or get_trend_analysis immediately to fetch it. "
        "Use **bold** for key numbers. Answer in under 120 words. "
        "\n\nPOLICY / WHAT-IF RULES (strictly enforce these):\n"
        "- If the user asks 'what if I reduce/apply/use X' or any policy simulation question: "
        "you MUST call search_policies with a keyword extracted from their question. "
        "Do NOT guess or answer from your own knowledge.\n"
        "- If search_policies returns a matching policy: call run_policy on it, then report "
        "the SISEPUEDE result in 1-2 sentences. Policy cards will be shown automatically.\n"
        "- If search_policies returns NO matching policy (empty or unrelated results): "
        "reply with EXACTLY this and nothing more: "
        "'That specific intervention is not available in SISEPUEDE data for this sector. "
        "Here are the policies I can simulate — select one from the cards below.' "
        "Do NOT estimate outcomes from your own knowledge.\n"
        "- NEVER quote emission reduction numbers that did not come from a run_policy tool result."
        f"{ctx_section}"
        "\n\nAt the very END of your reply (after all text), append exactly one tag on its own line: "
        "[[VIZ:type]] — choose the single best chart type from: "
        "pie (composition/share/breakdown), "
        "hbar (ranking/top-N comparison), "
        "line (time-series/trajectory over years), "
        "milestone (values at 2023/2030/2040/2050), "
        "sector_bar (compare sectors side by side), "
        "radar (multi-dimension sector profile), "
        "none (pure text answer needs no chart). "
        "Pick based on what the user asked. Output ONLY the tag — no explanation."
    )

    msgs: list = [{"role": m.role, "content": m.content} for m in req.messages[-8:]]
    reply = "Sorry, I couldn't generate a response."

    # Track tool calls made during the loop so we can auto-attach policy cards
    tools_called: list = []   # list of (name, input_dict) tuples

    # Agentic loop — Haiku calls tools until done (max 4 rounds)
    for _ in range(4):
        try:
            resp = _get_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=350,
                system=system_prompt,
                tools=_IQ_TOOLS,
                messages=msgs,
            )
        except Exception as e:
            reply = f"Sorry, something went wrong. ({e})"
            break

        if resp.stop_reason == "end_turn":
            texts = [b.text for b in resp.content if hasattr(b, "text")]
            reply = texts[0].strip() if texts else reply
            break

        if resp.stop_reason == "tool_use":
            msgs.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    tools_called.append((block.name, block.input))
                    result_str = await _exec_tool(block.name, block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result_str,
                    })
            if tool_results:
                msgs.append({"role": "user", "content": tool_results})
            continue

        # Any other stop reason → extract whatever text exists
        texts = [b.text for b in resp.content if hasattr(b, "text")]
        reply = texts[0].strip() if texts else reply
        break

    # Extract [[VIZ:type]] tag from reply and strip it from display text
    _VALID_VIZ = {"pie", "hbar", "line", "milestone", "sector_bar", "radar", "none"}
    viz_type = "none"
    viz_match = re.search(r'\[\[VIZ:(\w+)\]\]', reply)
    if viz_match:
        candidate = viz_match.group(1).lower()
        viz_type = candidate if candidate in _VALID_VIZ else "none"
        reply = reply[:viz_match.start()].rstrip()

    # Determine whether to attach policy cards:
    # 1. Explicit policy question (keyword match), OR
    # 2. The AI called search_policies / run_policy during reasoning
    policy_cards: List[dict] = []
    used_sector: Optional[str] = None

    if not used_sector:
        used_sector = topic_sector

    # Detect sector from tool calls even when message had no policy keywords
    for t_name, t_inp in tools_called:
        if t_name in ("search_policies", "run_policy") and t_inp.get("sector"):
            used_sector = t_inp["sector"]
            is_policy_q = True
            break
        if t_name == "get_sector_data" and t_inp.get("sector") not in (None, "all"):
            used_sector = t_inp["sector"]

    if not used_sector:
        used_sector = _dominant_sector(req.country)

    if is_policy_q:
        try:
            policy_cards = _run_policy_batch(req.country, used_sector, top_n=5)
        except Exception as _pe:
            print(f"[iq] policy batch failed: {_pe}")

    # Generate a context-aware follow-up question that guides WHAT → WHY → HOW
    suggested_next: Optional[str] = None
    try:
        q_lower = last_user.lower()
        if any(w in q_lower for w in ["driving", "what is", "account for", "breakdown", "largest", "dominant", "contribut"]):
            direction = (
                "The user just learned WHAT is happening. "
                "Suggest a WHY question — ask why this sub-category is growing or dominant "
                "(e.g. 'Why is cement production driving industrial emissions?'). "
                "Use specific numbers or names from the answer."
            )
        elif any(w in q_lower for w in ["why", "growing", "trend", "increas", "growth", "trajectory"]):
            direction = (
                "The user just learned WHY it is happening. "
                "Suggest a HOW question — ask how emissions can be reduced or what policies exist "
                "(e.g. 'How can we reduce industrial process emissions by 2030?'). "
                "Make it action-oriented."
            )
        else:
            direction = (
                "Suggest a relevant follow-up question that digs deeper into the data just discussed. "
                "Be specific — use numbers or sector names from the answer."
            )

        sq_resp = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=55,
            system=(
                f"The user is exploring {country_label} {topic_label} emissions data. "
                f"{direction} "
                "Reply with ONLY the question itself — max 14 words, no quotes, no period, no intro text."
            ),
            messages=[
                {"role": "user",      "content": last_user},
                {"role": "assistant", "content": reply},
                {"role": "user",      "content": "Suggest the next question:"},
            ],
        )
        suggested_next = sq_resp.content[0].text.strip().strip('"').strip("'").rstrip(".")
    except Exception as _sq:
        print(f"[iq] suggested_next failed: {_sq}")

    return {
        "reply":               reply,
        "has_policies":        bool(policy_cards),
        "policies":            policy_cards,
        "sector":              used_sector,
        "suggested_next":      suggested_next,
        "corrected_question":  corrected_question,
        "viz":                 {"type": viz_type},
    }


# ── Deployment ramp helper ────────────────────────────────────────────────────

def _apply_ramp(baseline: list, policy: list, years: list, ramp_years: int) -> list:
    """Scale the policy emission reduction linearly from 0 → full over ramp_years.
    Ramp starts at the current year (~2023). Before that: unchanged. After ramp: full effect."""
    if ramp_years <= 0 or not baseline or not policy:
        return policy
    start_idx = min(range(len(years)), key=lambda i: abs(years[i] - 2023))
    end_idx   = start_idx + ramp_years
    ramped    = list(policy)
    for i in range(start_idx, min(end_idx, len(policy))):
        factor    = (i - start_idx) / ramp_years          # 0.0 → 1.0
        b         = baseline[i] if i < len(baseline) else 0.0
        p         = policy[i]   if i < len(policy)   else 0.0
        ramped[i] = round(b - (b - p) * factor, 6)
    return ramped


# ── Auto-simulate: keyword-match question → best policy → run ─────────────────

class AutoSimRequest(BaseModel):
    country: str
    sector: str   # transport | waste | industrial
    question: str

@app.post("/auto-simulate")
async def auto_simulate(req: AutoSimRequest):
    """Find the best-matching policy for a natural-language question and simulate it."""
    if req.country not in BASELINES:
        raise HTTPException(400, f"Unknown country '{req.country}'")
    valid = ("transport", "waste", "industrial")
    if req.sector not in valid:
        raise HTTPException(400, f"sector must be one of {valid}")

    all_policies = _load_sector_policies(req.sector) or []
    if not all_policies:
        raise HTTPException(404, "No policies found for this sector")

    # Score each policy by word-overlap with the question
    q_words = set(re.sub(r"[^a-z0-9\s]", " ", req.question.lower()).split())
    stop = {"the", "a", "an", "is", "in", "of", "to", "and", "if", "i", "what",
            "result", "reduce", "apply", "we", "use", "do", "for", "this", "that"}
    q_words -= stop

    def _score(p):
        text = f"{p.get('name','')} {p.get('description','')} {p.get('category','')}".lower()
        t_words = set(re.sub(r"[^a-z0-9\s]", " ", text).split())
        return len(q_words & t_words)

    best = max(all_policies, key=_score)

    # Run simulation
    years    = _make_years(len(BASELINES[req.country]["df"]))
    has_true = (req.sector in SECTOR_MODELS and
                req.country in SECTOR_BL_OUTPUTS and
                req.sector in SECTOR_BL_OUTPUTS.get(req.country, {}))
    cfg = _build_policy_config(best)
    if req.sector == "transport":
        out = _compute_transport_abatement(req.country, "co2", cfg)
    elif has_true:
        out = _compute_true_sector_abatement(req.country, req.sector, "co2", cfg)
    else:
        out = _compute_generic_abatement(req.country, cfg)

    baseline = out.get("baseline", [])
    pol_ser  = out.get("policy",   [])
    b_end    = float(baseline[-1]) if baseline else 1.0
    p_end    = float(pol_ser[-1])  if pol_ser  else b_end
    red_pct  = round((b_end - p_end) / b_end * 100, 2) if b_end else 0.0

    mid_idx = min(range(len(years)), key=lambda i: abs(years[i] - 2023))
    chart_rows = []
    for i, yr in enumerate(years):
        row = {"year": yr, "baseline": round(float(baseline[i]), 4) if i < len(baseline) else None}
        if i < len(pol_ser):
            row[best["id"]] = round(float(pol_ser[i]), 4)
        chart_rows.append(row)

    return {
        "years":      years,
        "chart_rows": chart_rows,
        "policies":   [{"id": best["id"], "name": best["name"], "abatement_pct": round(red_pct, 1)}],
        "mid_idx":    mid_idx,
        "ramp_years": 0,
        "policy_details": {
            "id":          best["id"],
            "name":        best["name"],
            "description": best.get("description", ""),
            "category":    best.get("category", ""),
            "reduction_pct": round(red_pct, 1),
        },
    }


# ── Policy simulation (user-selected) ────────────────────────────────────────

class SimulateRequest(BaseModel):
    country: str
    sector: str
    policy_ids: List[str]
    ramp_years: int          = 0      # global fallback
    target_pct: float        = 100.0  # global fallback
    policy_configs: Optional[dict] = None  # {policy_id: {ramp_years, target_pct}}


@app.post("/simulate-policies")
async def simulate_policies(req: SimulateRequest):
    """Run user-selected policies and return delta chart data."""
    if req.country not in BASELINES:
        raise HTTPException(400, f"Unknown country '{req.country}'")

    all_policies = _load_sector_policies(req.sector) or []
    selected     = [p for p in all_policies if p["id"] in req.policy_ids]
    if not selected:
        raise HTTPException(404, "No matching policies found")

    years    = _make_years(len(BASELINES[req.country]["df"]))
    has_true = (req.sector in SECTOR_MODELS and
                req.country in SECTOR_BL_OUTPUTS and
                req.sector in SECTOR_BL_OUTPUTS.get(req.country, {}))

    def _run_one(p):
        try:
            cfg = _build_policy_config(p)
            if req.sector == "transport":
                out = _compute_transport_abatement(req.country, "co2", cfg)
            elif has_true:
                out = _compute_true_sector_abatement(req.country, req.sector, "co2", cfg)
            else:
                out = _compute_generic_abatement(req.country, cfg)
            return p["id"], out
        except Exception as e:
            return p["id"], {"error": str(e)}

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for pid, out in (f.result() for f in as_completed(
                {pool.submit(_run_one, p): p for p in selected})):
            results[pid] = out

    # Apply per-policy target_pct + ramp (post-process, no apiv2 changes)
    for pid, out in results.items():
        if "policy" not in out or "baseline" not in out:
            continue
        # Per-policy config takes priority over global fallback
        pc         = (req.policy_configs or {}).get(pid, {})
        p_ramp     = int(pc.get("ramp_years", req.ramp_years))
        p_scale    = max(0.0, min(1.0, float(pc.get("target_pct", req.target_pct)) / 100.0))
        bl         = out["baseline"]
        pol        = out["policy"]
        if p_scale < 1.0:
            pol = [round(b - (b - p) * p_scale, 6) for b, p in zip(bl, pol)]
        if p_ramp > 0:
            pol = _apply_ramp(bl, pol, years, p_ramp)
        out["policy"] = pol
        b_end = bl[-1]  if bl  else 1.0
        p_end = pol[-1] if pol else b_end
        out["final_reduction_pct"] = round((b_end - p_end) / b_end * 100, 2) if b_end else 0.0

    # Build chart rows: year, baseline, each policy series
    baseline = []
    for pid, out in results.items():
        if "baseline" in out:
            baseline = out["baseline"]
            break

    # Mid-point index for "current year" split
    mid_idx = min(range(len(years)), key=lambda i: abs(years[i] - 2023))

    chart_rows = []
    for i, yr in enumerate(years):
        row = {"year": yr, "baseline": round(float(baseline[i]), 4) if i < len(baseline) else None}
        for p in selected:
            out = results.get(p["id"], {})
            ser = out.get("policy", [])
            if i < len(ser):
                row[p["id"]] = round(float(ser[i]), 4)
        chart_rows.append(row)

    policy_meta = [
        {
            "id":            p["id"],
            "name":          p["name"],
            "abatement_pct": round(float(results.get(p["id"], {}).get("final_reduction_pct", 0)), 1),
        }
        for p in selected
    ]

    return {
        "years":        years,
        "chart_rows":   chart_rows,
        "policies":     policy_meta,
        "mid_idx":      mid_idx,
        "ramp_years":   req.ramp_years,
    }

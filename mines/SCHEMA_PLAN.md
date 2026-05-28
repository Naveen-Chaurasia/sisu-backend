# Mining Investment Platform — Scalable Schema & Data Ingestion Plan

## Source Files Analysed

| File | Mine | Commodities | Scenarios |
|------|------|-------------|-----------|
| `Mining Investment Model Prototype (4.20.2026).xlsx` | Template (11 mines) | Any | Single |
| `Mine1_12891L_Revised_Benchmarked_Apr2026.xlsx` | Chinaka Resource Mining (License 12891L) | REE + Monazite + Spodumene | Bear / Base / Bull |
| `Complex Release 2 Mine11_9015L_Rebuilt_Benchmarked_Graphite 3 scenarios_Apr2026 (1).xlsx` | M'Gomo Mine (License 9015L) | Gold + Graphite | Gold (single) + Graphite Bear/Base/Bull |

---

## Excel Sheet Inventory

### File 1 — Prototype
| Sheet | Rows × Cols | Purpose |
|-------|-------------|---------|
| Mine_List | 12 × 19 | Portfolio registry — 11 mines, KPIs (NPV, IRR, Payback, MOIC) |
| Mine_Profile | 97 × 10 | Key-value pairs — ore reserve, throughput, ramp-up, ore grades |
| Calcs | 51 × 53 | DCF engine — Year 0-50, all financial line items |
| MonteCarloSample | 109 × 13 | 108 simulation results — NPV, IRR, FCF distributions |
| Minerals_Table | 10 × 4 | Commodity reference — price, grade, recovery rate |
| Data Dictionary | 112 × 12 | Field definitions, validation rules, FK relationships |

### File 2 — Mine1 (Chinaka REE)
| Sheet | Rows × Cols | Purpose |
|-------|-------------|---------|
| Exec Summary | 37 × 6 | Multi-commodity scorecard + scenario range (Bear/Base/Bull) |
| REE DCF | 37 × 24 | Year 0-20 cash flows for REE stream only |
| Monazite DCF | 37 × 24 | Year 0-20 cash flows for Monazite stream |
| Spodumene DCF | 37 × 24 | Year 0-20 cash flows for Spodumene stream |
| Combined DCF | 37 × 24 | Consolidated across all 3 commodities |
| REE Scenario Analysis | 38 × 7 | Bear/Base/Bull vs original model — delta analysis |

**Key findings:**
- Original model NPV $2,687mm revised down to $705mm (−74%) after benchmark correction
- CAPEX revised from $750mm → $1,500mm (+100%)
- REE basket composition (La/Ce vs NdPr) is the largest single NPV driver (±$2.1bn swing)
- JORC status: NOT CONFIRMED — critical financing risk

### File 3 — Mine11 (M'Gomo Gold + Graphite)
| Sheet | Rows × Cols | Purpose |
|-------|-------------|---------|
| Exec Summary | 35 × 6 | Gold (confirmed) + Graphite (3 scenarios) scorecard |
| Gold DCF | 37 × 53 | Year 0-50 cash flows, 226.6 kg/yr alluvial gold |
| Graphite Bear DCF | 39 × 53 | $700/t price, $38mm CAPEX, $380/t OPEX |
| Graphite Base DCF | 39 × 53 | $1,000/t price, $30mm CAPEX, $320/t OPEX |
| Graphite Bull DCF | 39 × 53 | $1,400/t price, $25mm CAPEX, $290/t OPEX |
| Scenario Analysis | 36 × 7 | Benchmark corrections — CAPEX +971%, OPEX +595% vs old model |

**Key findings:**
- Gold: IRR 223.6%, MOIC 318.1x, payback 1 year — de-risked base case
- Graphite: 60% UNCONFIRMED deposit, CAPEX underestimated by ~$27mm (965%) in old model
- OPEX underestimated by $274/t (+600%) vs peer benchmarks (Syrah Balama, NextSource)
- Gold revenue offsets graphite CAPEX timing risk

---

## Generalised Supabase Schema (9 Tables)

```sql
-- ─────────────────────────────────────────────────────────
-- ENUMS
-- ─────────────────────────────────────────────────────────
CREATE TYPE mine_type_enum       AS ENUM ('Open Pit', 'Underground', 'Alluvial', 'In-Situ');
CREATE TYPE mine_status_enum     AS ENUM ('Active', 'Development', 'Exploration', 'Inactive');
CREATE TYPE jorc_status_enum     AS ENUM ('Confirmed', 'Inferred', 'Indicated', 'Not Confirmed');
CREATE TYPE scenario_name_enum   AS ENUM ('Base', 'Bear', 'Bull', 'Custom');
CREATE TYPE risk_level_enum      AS ENUM ('High', 'Medium', 'Low');
CREATE TYPE risk_category_enum   AS ENUM ('JORC', 'Regulatory', 'Price', 'Operational', 'Geochemical', 'Social', 'Environmental', 'Financial');

-- ─────────────────────────────────────────────────────────
-- 1. mines  — master mine registry
-- ─────────────────────────────────────────────────────────
CREATE TABLE mines (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_number           TEXT NOT NULL,
  mine_name             TEXT NOT NULL,
  license_number        TEXT UNIQUE NOT NULL,
  country               TEXT NOT NULL DEFAULT 'Mozambique',
  province              TEXT,
  mine_type             mine_type_enum,
  status                mine_status_enum DEFAULT 'Exploration',
  ore_reserve           NUMERIC,          -- total reserve (tonnes or m³)
  reserve_unit          TEXT,             -- 'Mt', 'm³', 'Mm³'
  throughput_pa         NUMERIC,          -- steady-state annual throughput
  throughput_unit       TEXT,             -- 'tpa', 'm³/yr'
  life_of_mine_years    INT,
  concession_area_ha    NUMERIC,
  jorc_status           jorc_status_enum,
  confidence_pct        NUMERIC,          -- 0-100
  prospectivity_notes   TEXT,
  notes                 TEXT,
  lat                   NUMERIC,
  lng                   NUMERIC,
  created_at            TIMESTAMPTZ DEFAULT now(),
  updated_at            TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────
-- 2. commodities_reference  — master commodity lookup
-- ─────────────────────────────────────────────────────────
CREATE TABLE commodities_reference (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  commodity_name           TEXT UNIQUE NOT NULL,
  market_price             NUMERIC,
  price_unit               TEXT,          -- '$/kg', '$/t', '$/oz'
  avg_ore_grade            NUMERIC,
  grade_unit               TEXT,          -- '%', 'g/t', 'kg/t', 'g/m³'
  avg_recovery_rate_pct    NUMERIC,
  price_escalation_default NUMERIC DEFAULT 0.02,  -- 2% p.a.
  density_t_per_m3         NUMERIC,
  regulatory_notes         TEXT,
  updated_at               TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────
-- 3. mine_commodities  — which commodities a mine produces
-- ─────────────────────────────────────────────────────────
CREATE TABLE mine_commodities (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id                 UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  commodity_name          TEXT NOT NULL,
  ore_grade               NUMERIC,
  grade_unit              TEXT,
  recovery_rate_pct       NUMERIC,
  annual_production_est   NUMERIC,        -- steady-state
  production_unit         TEXT,           -- 'kg/yr', 't/yr'
  is_primary              BOOLEAN DEFAULT false,
  regulatory_notes        TEXT,
  UNIQUE(mine_id, commodity_name)
);

-- ─────────────────────────────────────────────────────────
-- 4. financial_models  — one per mine (can have many scenarios)
-- ─────────────────────────────────────────────────────────
CREATE TABLE financial_models (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id                 UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  model_name              TEXT NOT NULL DEFAULT 'Base Model',
  version                 TEXT DEFAULT 'v1',
  wacc                    NUMERIC NOT NULL DEFAULT 0.14,
  tax_rate                NUMERIC NOT NULL DEFAULT 0.32,
  royalty_rate            NUMERIC NOT NULL DEFAULT 0.06,
  life_of_mine_years      INT,
  initial_capex           NUMERIC,        -- $mm
  sustaining_capex_pa     NUMERIC,        -- $mm/yr
  capex_deployment_year   INT DEFAULT 0,
  closure_cost            NUMERIC,        -- $mm
  debt_funding            NUMERIC DEFAULT 0,
  debt_term_years         INT,
  interest_rate           NUMERIC,
  ramp_up_y1              NUMERIC DEFAULT 0.40,
  ramp_up_y2              NUMERIC DEFAULT 0.75,
  depreciation_years      INT DEFAULT 15,
  currency                TEXT DEFAULT 'USD',
  source_file             TEXT,           -- original xlsx filename
  notes                   TEXT,
  created_at              TIMESTAMPTZ DEFAULT now(),
  updated_at              TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────
-- 5. scenarios  — per commodity × scenario variant
--    One model can have N scenarios (Bear/Base/Bull × commodity)
-- ─────────────────────────────────────────────────────────
CREATE TABLE scenarios (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_id              UUID NOT NULL REFERENCES financial_models(id) ON DELETE CASCADE,
  scenario_name         scenario_name_enum NOT NULL DEFAULT 'Base',
  commodity_name        TEXT NOT NULL,         -- 'Gold', 'REE', 'Graphite', 'Combined'
  price_base            NUMERIC,               -- base year price
  price_unit            TEXT,                  -- '$/kg', '$/t', '$/oz'
  price_escalation_pct  NUMERIC DEFAULT 0.02,  -- p.a.
  capex_override        NUMERIC,               -- $mm, overrides model default
  opex_per_unit         NUMERIC,               -- $/tonne or $/kg
  opex_total_pa         NUMERIC,               -- $mm/yr at steady state
  basis_notes           TEXT,                  -- why this price/cost was chosen
  benchmark_source      TEXT,                  -- e.g. 'Syrah Balama', 'NextSource'
  -- Summary outputs (pre-computed or from ingestion)
  npv                   NUMERIC,               -- $mm
  irr                   NUMERIC,               -- decimal e.g. 0.124
  payback_years         NUMERIC,
  moic                  NUMERIC,
  total_lom_revenue     NUMERIC,               -- $mm
  total_lom_fcf         NUMERIC,               -- $mm
  total_capex           NUMERIC,               -- $mm
  break_even_price      NUMERIC,
  created_at            TIMESTAMPTZ DEFAULT now(),
  UNIQUE(model_id, scenario_name, commodity_name)
);

-- ─────────────────────────────────────────────────────────
-- 6. dcf_years  — year-by-year cash flow (time series core)
-- ─────────────────────────────────────────────────────────
CREATE TABLE dcf_years (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id           UUID NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
  year                  INT NOT NULL,           -- 0, 1, 2 … 50
  ramp_up_factor_pct    NUMERIC,
  ore_mined             NUMERIC,               -- tonnes or m³
  cumulative_ore_mined  NUMERIC,
  remaining_reserve     NUMERIC,
  commodity_produced    NUMERIC,               -- kg or tonnes
  commodity_price       NUMERIC,               -- escalated price that year
  gross_revenue         NUMERIC,               -- $mm
  royalty               NUMERIC,               -- $mm
  net_revenue           NUMERIC,               -- $mm
  operating_costs       NUMERIC,               -- $mm (negative)
  ebitda                NUMERIC,               -- $mm
  ebitda_margin_pct     NUMERIC,
  depreciation          NUMERIC,               -- $mm (negative)
  ebit                  NUMERIC,               -- $mm
  income_tax            NUMERIC,               -- $mm (negative)
  net_income            NUMERIC,               -- $mm
  capex                 NUMERIC,               -- $mm (negative, initial + sustaining)
  free_cash_flow        NUMERIC,               -- $mm
  cumulative_fcf        NUMERIC,               -- $mm
  discount_factor       NUMERIC,               -- 1/(1+wacc)^year
  discounted_cf         NUMERIC,               -- $mm
  UNIQUE(scenario_id, year)
);

-- ─────────────────────────────────────────────────────────
-- 7. monte_carlo_runs  — one run session per model
-- ─────────────────────────────────────────────────────────
CREATE TABLE monte_carlo_runs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_id      UUID NOT NULL REFERENCES financial_models(id) ON DELETE CASCADE,
  n_runs        INT NOT NULL DEFAULT 500,
  variation_pct JSONB,   -- {"price":0.20,"grade":0.10,"capex":0.50,...}
  run_date      TIMESTAMPTZ DEFAULT now(),
  p10_npv       NUMERIC,
  p50_npv       NUMERIC,
  p90_npv       NUMERIC,
  p10_irr       NUMERIC,
  p50_irr       NUMERIC,
  p90_irr       NUMERIC,
  mean_npv      NUMERIC,
  std_npv       NUMERIC
);

-- ─────────────────────────────────────────────────────────
-- 8. monte_carlo_results  — individual simulation rows
-- ─────────────────────────────────────────────────────────
CREATE TABLE monte_carlo_results (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id                  UUID NOT NULL REFERENCES monte_carlo_runs(id) ON DELETE CASCADE,
  simulation_index        INT NOT NULL,
  npv                     NUMERIC,
  irr                     NUMERIC,
  payback_years           NUMERIC,
  moic                    NUMERIC,
  unit_margin             NUMERIC,
  total_lom_revenue       NUMERIC,
  total_lom_fcf           NUMERIC,
  total_mineral_produced  NUMERIC,
  cost_per_unit           NUMERIC
);

-- ─────────────────────────────────────────────────────────
-- 9. risk_factors  — qualitative risk register per mine
-- ─────────────────────────────────────────────────────────
CREATE TABLE risk_factors (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id           UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  name              TEXT NOT NULL,
  category          risk_category_enum,
  level             risk_level_enum,
  probability_label TEXT,    -- 'Probable (40%)', 'Possible (10%)'
  intensity         TEXT,    -- 'High', 'Medium', 'Low'
  duration          TEXT,    -- 'Short Term', 'Long Term'
  notes             TEXT,
  mitigation        TEXT
);

-- ─────────────────────────────────────────────────────────
-- INDEXES for common query patterns
-- ─────────────────────────────────────────────────────────
CREATE INDEX idx_mine_commodities_mine   ON mine_commodities(mine_id);
CREATE INDEX idx_financial_models_mine   ON financial_models(mine_id);
CREATE INDEX idx_scenarios_model         ON scenarios(model_id);
CREATE INDEX idx_dcf_years_scenario      ON dcf_years(scenario_id);
CREATE INDEX idx_dcf_years_year          ON dcf_years(year);
CREATE INDEX idx_mc_results_run          ON monte_carlo_results(run_id);
CREATE INDEX idx_risk_factors_mine       ON risk_factors(mine_id);
```

---

## Data Ingestion Plan

### Script: `backend/mines/ingest_mines.py`

```
File 1 (Prototype)
  ├── Mine_List sheet       → INSERT INTO mines (11 rows)
  ├── Minerals_Table sheet  → INSERT INTO commodities_reference (9 rows)
  └── MonteCarloSample      → INSERT INTO monte_carlo_results (108 rows)
      (linked to a monte_carlo_runs seed row for Mine 11)

File 2 (Mine1 Chinaka REE)
  ├── Exec Summary          → UPDATE mines SET status/notes WHERE license='12891L'
  ├── financial_models      → INSERT (1 model for Chinaka)
  ├── Scenarios × 3 commodities × 3 scenarios = 9 scenario rows
  │     REE Bear, REE Base, REE Bull
  │     Monazite Bear, Monazite Base, Monazite Bull
  │     Spodumene Bear, Spodumene Base, Spodumene Bull
  │     Combined Bear, Combined Base, Combined Bull
  ├── REE DCF / Monazite / Spodumene / Combined sheets
  │     → INSERT INTO dcf_years (20 rows per scenario)
  └── REE Scenario Analysis → populate scenarios.basis_notes, benchmark_source

File 3 (Mine11 M'Gomo Gold + Graphite)
  ├── Exec Summary          → UPDATE mines WHERE license='9015L'
  ├── financial_models      → INSERT (1 model for M'Gomo)
  ├── Scenarios:
  │     Gold Base (1 row)
  │     Graphite Bear, Graphite Base, Graphite Bull (3 rows)
  ├── Gold DCF              → dcf_years (50 rows, year 0-49)
  ├── Graphite Bear/Base/Bull DCF → dcf_years (39 rows each)
  └── Scenario Analysis     → populate benchmark corrections
```

### Ingestion Steps

1. `pip install openpyxl supabase python-dotenv`
2. Create `.env` with `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`
3. Run SQL migration in Supabase SQL Editor (schema above)
4. Run `python ingest_mines.py` — idempotent (upsert by license_number / UNIQUE keys)

---

## Phase Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Supabase project + run SQL migration | TODO |
| 2 | Write + run `ingest_mines.py` | TODO |
| 3 | Replace `mine_data.py` hardcoded dict with Supabase queries | TODO |
| 4 | Add scenario endpoints to `api_mines.py` | TODO |
| 5 | UI: scenario selector, multi-commodity tabs, benchmark panel | TODO |

---

## Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| DCF storage | Pre-computed rows in `dcf_years` | Avoids recalculating 50yr × N scenarios on every API request |
| Scenarios | Separate row per (model × commodity × scenario_name) | Mine11 has Gold + Graphite×3 = 4 scenario rows, all queryable independently |
| Monte Carlo | Raw results stored in DB | Enables SQL percentile queries (PERCENTILE_CONT), avoids re-running in Python |
| Multi-commodity | Aggregation via JOIN at API level | Flexible for combined vs per-commodity views without schema changes |
| Benchmarks | `basis_notes` + `benchmark_source` columns on scenarios | Tracks why a price/cost was revised (audit trail) |
| CAPEX corrections | `capex_override` on scenarios | Original model had Graphite CAPEX at $2.8mm; revised to $30mm — both traceable |

---

## Notable Data Corrections Found in Source Files

| Mine | Field | Original | Revised | Change |
|------|-------|----------|---------|--------|
| Chinaka (Mine1) | NPV | $2,687mm | $705mm | −74% |
| Chinaka (Mine1) | CAPEX | $750mm | $1,500mm | +100% |
| Chinaka (Mine1) | REE Price | $12,500/t | $9,000/t | −28% |
| M'Gomo Graphite | CAPEX | $2.8mm | $30mm (base) | +971% |
| M'Gomo Graphite | OPEX | $46/t | $320/t (base) | +595% |
| M'Gomo Graphite | Annual OPEX | $0.70mm/yr | $4.9mm/yr | +600% |

These corrections must be preserved in the DB — both old and revised values are stored via the `scenarios` table (old model as a separate scenario row, revised as Base/Bear/Bull).

---

## How to Run — Complete Setup Guide

### Prerequisites
- Supabase project created at `https://snbnqwrxvptrfjsecljd.supabase.co`
- Python 3.10+ with pip
- All 3 Excel files present in `backend/mines/`

---

### Step 1 — Create the Database Schema

1. Open **Supabase Dashboard** → **SQL Editor** → **New Query**
2. Paste the full contents of `backend/mines/schema.sql`
3. Click **Run**

This creates:
- 9 tables (`mines`, `commodities_reference`, `mine_commodities`, `financial_models`, `scenarios`, `dcf_years`, `monte_carlo_runs`, `monte_carlo_results`, `risk_factors`)
- All FK constraints, indexes, and check constraints
- 2 views (`v_portfolio`, `v_scenario_comparison`)
- `set_updated_at()` trigger on `mines` and `financial_models`

---

### Step 2 — Install Python Dependencies

```powershell
cd "D:\pythone\RAG Project\sisepuede\Agents\backend"
pip install openpyxl supabase python-dotenv
```

---

### Step 3 — Verify Environment File

Ensure `backend/mines/.env` contains:
```
SUPABASE_URL=https://snbnqwrxvptrfjsecljd.supabase.co
SUPABASE_SERVICE_KEY=<service_role_key>
```
The `.env` file is gitignored and will NOT be pushed to GitHub.

---

### Step 4 — Run the Ingestion Script

```powershell
cd "D:\pythone\RAG Project\sisepuede\Agents\backend\mines"
python ingest_mines.py
```

Expected output:
```
════════════════════════════════════════════════════════════
  Mining Investment Platform — Supabase Ingestion
════════════════════════════════════════════════════════════
  ✓ Found: Mining Investment Model Prototype (4.20.2026).xlsx
  ✓ Found: Mine1_12891L_Revised_Benchmarked_Apr2026.xlsx
  ✓ Found: Complex Release 2 Mine11_9015L_...xlsx

── FILE 1: Prototype ─────────────────────────────────────
  Sheet: Mine_List
    Mine: Chinaka Resource Mining 3 (12891L) → <uuid>
    Mine: M'Gomo Mine (9015L) → <uuid>
    ...11 mines total...
  Sheet: Minerals_Table
    Commodity: Rare Earths
    ...9 commodities...
  Sheet: MonteCarloSample
    → Inserted 108 Monte Carlo result rows

── FILE 2: Mine1 Chinaka REE (12891L) ───────────────────
  Financial model → <uuid>
  Sheet: REE → REE Base
    → Inserted 20 DCF year rows
  Sheet: Monazite → Monazite Base
    → Inserted 20 DCF year rows
  ...
  Scenario: Bear REE
  Scenario: Bull REE

── FILE 3: Mine11 M'Gomo Gold + Graphite (9015L) ─────────
  Financial model → <uuid>
  Sheet: Gold → Gold Base
    → Inserted 50 DCF year rows
  Sheet: Graphite Bear → Graphite Bear
    → Inserted 39 DCF year rows
  ...
  → Inserted 6 risk factors
════════════════════════════════════════════════════════════
  Ingestion complete.
```

---

### Step 5 — Verify in Supabase

Run these queries in SQL Editor to confirm data loaded:

```sql
-- Count rows per table
SELECT 'mines'                AS tbl, COUNT(*) FROM mines
UNION ALL SELECT 'mine_commodities',  COUNT(*) FROM mine_commodities
UNION ALL SELECT 'financial_models',  COUNT(*) FROM financial_models
UNION ALL SELECT 'scenarios',         COUNT(*) FROM scenarios
UNION ALL SELECT 'dcf_years',         COUNT(*) FROM dcf_years
UNION ALL SELECT 'monte_carlo_results',COUNT(*) FROM monte_carlo_results
UNION ALL SELECT 'risk_factors',      COUNT(*) FROM risk_factors;

-- Check portfolio view
SELECT mine_name, primary_commodity, npv, irr, payback_years, moic
FROM v_portfolio ORDER BY npv DESC NULLS LAST;

-- Check scenario comparison
SELECT mine_name, commodity_name, scenario_name, npv, irr, capex_mm
FROM v_scenario_comparison;
```

Expected row counts after ingestion:
| Table | Expected Rows |
|-------|--------------|
| mines | 11 |
| commodities_reference | 9 |
| mine_commodities | ~5 (Chinaka ×3 + M'Gomo ×2) |
| financial_models | 2 (one per detailed mine) |
| scenarios | ~10 (REE Base/Bear/Bull + Monazite/Spodumene/Combined Base + Gold Base + Graphite Bear/Base/Bull) |
| dcf_years | ~250 (20 rows × 4 Chinaka sheets + 50 Gold + 39×3 Graphite) |
| monte_carlo_results | 108 |
| risk_factors | 6 |

---

### Step 6 — Next: API Layer (Phase 3)

After ingestion is verified, update `backend/mines/api_mines.py`:
- Replace hardcoded `MINES` dict lookups with Supabase client queries
- Add `GET /mines/{id}/scenarios` endpoint
- Add `GET /scenarios/{id}/dcf` endpoint (returns pre-computed `dcf_years`)
- Add multi-commodity aggregation endpoint

### Step 7 — Next: UI Layer (Phase 4)

- Mine Registry table pulls live from Supabase via updated API
- Financial Model tab: scenario selector dropdown (Bear / Base / Bull)
- New multi-commodity breakdown panel per mine
- Benchmark comparison panel showing original vs revised CAPEX/OPEX

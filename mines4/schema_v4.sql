-- ════════════════════════════════════════════════════════════════════
-- Mining Investment Platform — schema_v4.sql
-- Handles: prototype single-commodity, Mine1 multi-commodity,
--          mine11 Bear/Base/Bull per commodity
--
-- All tables prefixed m4_ to avoid conflicts.
-- Safe to re-run: uses IF NOT EXISTS.
-- Run in: Supabase Dashboard → SQL Editor → New Query
-- ════════════════════════════════════════════════════════════════════

-- ── 1. m4_mines  (mine identity + mine-level assumptions) ─────────────────────
CREATE TABLE IF NOT EXISTS m4_mines (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_name            TEXT NOT NULL,
  license_number       TEXT UNIQUE,
  country              TEXT DEFAULT 'Mozambique',
  province             TEXT,
  mine_type            TEXT,
  status               TEXT DEFAULT 'Active',
  -- Reserve & production (mine-level)
  ore_reserve          NUMERIC,
  reserve_unit         TEXT DEFAULT 'Mt',
  throughput_pa        NUMERIC,
  throughput_unit      TEXT,
  life_of_mine_yr      INT,
  -- Mine-level financial defaults (each scenario can override)
  wacc                 NUMERIC DEFAULT 0.15,
  tax_rate             NUMERIC DEFAULT 0.32,
  royalty_rate         NUMERIC DEFAULT 0.03,
  -- Ramp-up schedule (fraction of steady-state)
  ramp_up_y1           NUMERIC DEFAULT 0.40,
  ramp_up_y2           NUMERIC DEFAULT 0.75,
  ramp_up_y3           NUMERIC DEFAULT 1.00,
  -- End of life
  closure_rehab_cost   NUMERIC DEFAULT 0,
  -- Debt & interest
  debt_funding         NUMERIC DEFAULT 0,
  debt_term            INT DEFAULT 0,
  interest_rate        NUMERIC DEFAULT 0,
  -- Qualitative
  prospectivity_notes  TEXT,
  headline             TEXT,
  subtitle             TEXT,
  source_file          TEXT,
  risk_factors         JSONB DEFAULT '[]'::jsonb,
  environmental_impacts JSONB DEFAULT '[]'::jsonb,
  is_user_created      BOOLEAN DEFAULT FALSE,
  created_at           TIMESTAMPTZ DEFAULT now()
);

-- ── 2. m4_commodities  (one per commodity per mine) ───────────────────────────
CREATE TABLE IF NOT EXISTS m4_commodities (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id       UUID NOT NULL REFERENCES m4_mines(id) ON DELETE CASCADE,
  commodity     TEXT NOT NULL,        -- 'REE', 'Monazite', 'Spodumene', 'Gold', 'Graphite'
  is_primary    BOOLEAN DEFAULT FALSE,
  has_scenarios BOOLEAN DEFAULT FALSE, -- TRUE when Bear/Base/Bull exist
  display_order INT DEFAULT 0,
  UNIQUE (mine_id, commodity)
);

-- ── 3. m4_scenarios  (one per Bear/Base/Bull/Single per commodity) ────────────
-- All DCF scalar inputs live here. Mine-level fields are fallbacks when NULL.
CREATE TABLE IF NOT EXISTS m4_scenarios (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  commodity_id            UUID NOT NULL REFERENCES m4_commodities(id) ON DELETE CASCADE,
  scenario                TEXT NOT NULL DEFAULT 'Base',  -- 'Base'|'Bear'|'Bull'|'Single'
  sheet_name              TEXT,
  -- Price inputs
  price_base              NUMERIC,
  price_unit              TEXT DEFAULT '$/t',
  -- price_escalation_rate defaults to 0 (fixed price); set explicitly if price escalates
  price_escalation_rate   NUMERIC DEFAULT 0.00,
  -- Production
  annual_production       NUMERIC,
  avg_recovered_grade     NUMERIC,   -- g/m3 or g/t; used to derive annual_production
  production_unit         TEXT DEFAULT 'tonnes',
  -- OPEX (steady-state p.a.)
  opex_steady_state       NUMERIC,
  opex_per_unit           NUMERIC,
  opex_escalation_rate    NUMERIC DEFAULT 0.00,
  -- CAPEX
  initial_capex           NUMERIC,
  sustaining_capex_pa     NUMERIC DEFAULT 0,
  capex_deployment_year   INT DEFAULT 0,
  -- Depreciation
  depreciation_pa         NUMERIC,
  avg_depreciation_years  INT,
  -- Timing
  production_start_year   INT DEFAULT 1,
  -- Scenario-level overrides (NULL → use mine-level default)
  wacc                    NUMERIC,
  royalty_rate            NUMERIC,
  -- Notes
  basis_notes             TEXT,
  UNIQUE (commodity_id, scenario)
);

-- ── 4. m4_dcf_inputs  (ingested year-by-year INPUT rows only) ────────────────
-- Only rows R5, R8, R14, R19, R24 from Excel — NO calculated rows.
-- Used for display reference and to derive scalar inputs.
CREATE TABLE IF NOT EXISTS m4_dcf_inputs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id     UUID NOT NULL REFERENCES m4_scenarios(id) ON DELETE CASCADE,
  year            INT NOT NULL,
  production      NUMERIC,        -- R5
  commodity_price NUMERIC,        -- R8
  operating_costs NUMERIC,        -- R14
  depreciation    NUMERIC,        -- R19
  capex           NUMERIC,        -- R24
  UNIQUE (scenario_id, year)
);

-- ── 5. m4_metrics  (cached computed metrics, written by /calculate) ───────────
CREATE TABLE IF NOT EXISTS m4_metrics (
  scenario_id             UUID PRIMARY KEY REFERENCES m4_scenarios(id) ON DELETE CASCADE,
  npv                     NUMERIC,
  irr                     NUMERIC,
  payback                 TEXT,
  moic                    NUMERIC,
  total_capex             NUMERIC,
  total_lom_fcf           NUMERIC,
  total_lom_revenue       NUMERIC,
  total_mineral_produced  NUMERIC,
  total_cost_per_unit     NUMERIC,
  unit_margin_dollar      NUMERIC,
  unit_margin_pct         NUMERIC,
  life_of_mine_yr         INT,
  calculated_at           TIMESTAMPTZ DEFAULT now()
);

-- ── 6. m4_exec_rows  (qualitative exec summary / board memo rows) ─────────────
CREATE TABLE IF NOT EXISTS m4_exec_rows (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id     UUID NOT NULL REFERENCES m4_mines(id) ON DELETE CASCADE,
  row_index   INT,
  section     TEXT,
  metric      TEXT,
  col_b       TEXT,
  col_c       TEXT,
  col_d       TEXT,
  col_e       TEXT,
  notes       TEXT
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_m4_comm_mine   ON m4_commodities(mine_id);
CREATE INDEX IF NOT EXISTS idx_m4_scen_comm   ON m4_scenarios(commodity_id);
CREATE INDEX IF NOT EXISTS idx_m4_dcf_scen    ON m4_dcf_inputs(scenario_id);
CREATE INDEX IF NOT EXISTS idx_m4_exec_mine   ON m4_exec_rows(mine_id);

-- ── Convenience view: all scenario returns ───────────────────────────────────
CREATE OR REPLACE VIEW v_m4_scenario_returns AS
SELECT
  m.mine_name,
  m.license_number,
  c.commodity,
  c.is_primary,
  s.scenario,
  s.price_base,
  s.price_unit,
  met.npv,
  met.irr,
  met.payback,
  met.moic,
  met.total_capex,
  met.total_lom_fcf,
  s.basis_notes
FROM m4_scenarios s
JOIN m4_commodities c ON c.id = s.commodity_id
JOIN m4_mines m       ON m.id = c.mine_id
LEFT JOIN m4_metrics met ON met.scenario_id = s.id
ORDER BY m.mine_name, c.commodity, s.scenario;



-- m4_mines: debt / interest fields
ALTER TABLE m4_mines ADD COLUMN IF NOT EXISTS debt_funding   NUMERIC DEFAULT 0;
ALTER TABLE m4_mines ADD COLUMN IF NOT EXISTS debt_term      INT DEFAULT 0;
ALTER TABLE m4_mines ADD COLUMN IF NOT EXISTS interest_rate  NUMERIC DEFAULT 0;

-- m4_scenarios: recovered grade
ALTER TABLE m4_scenarios ADD COLUMN IF NOT EXISTS avg_recovered_grade NUMERIC;

-- m4_metrics: new output fields
ALTER TABLE m4_metrics ADD COLUMN IF NOT EXISTS total_lom_revenue      NUMERIC;
ALTER TABLE m4_metrics ADD COLUMN IF NOT EXISTS total_mineral_produced  NUMERIC;
ALTER TABLE m4_metrics ADD COLUMN IF NOT EXISTS total_cost_per_unit     NUMERIC;
ALTER TABLE m4_metrics ADD COLUMN IF NOT EXISTS unit_margin_dollar      NUMERIC;
ALTER TABLE m4_metrics ADD COLUMN IF NOT EXISTS unit_margin_pct         NUMERIC;
ALTER TABLE m4_metrics ADD COLUMN IF NOT EXISTS life_of_mine_yr         INT;

-- Fix: clear cached metrics so run_dcf recalculates with correct price_escalation_rate.
-- Run this once after re-ingesting (ingest now writes correct values per mine).
DELETE FROM m4_metrics;


UPDATE m4_scenarios
SET price_escalation_rate = 0
WHERE commodity_id IN (
  SELECT id FROM m4_commodities WHERE commodity = 'Gold'
);

DELETE FROM m4_metrics;
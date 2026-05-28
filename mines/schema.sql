-- ══════════════════════════════════════════════════════════════════
-- Mining Investment Platform — Supabase Schema
-- Run this in: Supabase Dashboard → SQL Editor → New Query
-- ══════════════════════════════════════════════════════════════════

-- ─────────────────────────────────────────────────────────────────
-- ENUMS
-- ─────────────────────────────────────────────────────────────────
CREATE TYPE mine_type_enum     AS ENUM ('Open Pit','Underground','Alluvial','In-Situ','Mixed');
CREATE TYPE mine_status_enum   AS ENUM ('Active','Development','Exploration','Inactive');
CREATE TYPE jorc_status_enum   AS ENUM ('Confirmed','Inferred','Indicated','Not Confirmed','Sampled');
CREATE TYPE scenario_name_enum AS ENUM ('Base','Bear','Bull','Original','Custom');
CREATE TYPE risk_level_enum    AS ENUM ('High','Medium','Low');
CREATE TYPE risk_cat_enum      AS ENUM ('JORC','Regulatory','Price','Operational','Geochemical','Social','Environmental','Financial','Natural Disaster');

-- ─────────────────────────────────────────────────────────────────
-- 1. mines
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mines (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_number          TEXT NOT NULL,
  mine_name            TEXT NOT NULL,
  license_number       TEXT UNIQUE NOT NULL,
  country              TEXT NOT NULL DEFAULT 'Mozambique',
  province             TEXT,
  mine_type            mine_type_enum,
  status               mine_status_enum DEFAULT 'Exploration',
  ore_reserve          NUMERIC,
  reserve_unit         TEXT,             -- 'Mt','m³','Mm³'
  throughput_pa        NUMERIC,          -- steady-state annual
  throughput_unit      TEXT,             -- 'tpa','m³/yr'
  life_of_mine_years   INT,
  concession_area_ha   NUMERIC,
  jorc_status          jorc_status_enum,
  confidence_pct       NUMERIC CHECK (confidence_pct BETWEEN 0 AND 100),
  prospectivity_notes  TEXT,
  notes                TEXT,
  lat                  NUMERIC,
  lng                  NUMERIC,
  created_at           TIMESTAMPTZ DEFAULT now(),
  updated_at           TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────
-- 2. commodities_reference  (master lookup)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS commodities_reference (
  id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  commodity_name            TEXT UNIQUE NOT NULL,
  market_price              NUMERIC,
  price_unit                TEXT,        -- '$/kg','$/t','$/oz','$/lb'
  avg_ore_grade             NUMERIC,
  grade_unit                TEXT,        -- '%','g/t','kg/t','g/m³'
  avg_recovery_rate_pct     NUMERIC CHECK (avg_recovery_rate_pct BETWEEN 0 AND 100),
  price_escalation_default  NUMERIC DEFAULT 0.02,
  density_t_per_m3          NUMERIC,
  regulatory_notes          TEXT,
  updated_at                TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────
-- 3. mine_commodities  (which commodities a mine produces)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mine_commodities (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id               UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  commodity_name        TEXT NOT NULL,
  ore_grade             NUMERIC,
  grade_unit            TEXT,
  recovery_rate_pct     NUMERIC,
  annual_production_est NUMERIC,
  production_unit       TEXT,            -- 'kg/yr','t/yr'
  is_primary            BOOLEAN DEFAULT FALSE,
  regulatory_notes      TEXT,
  UNIQUE (mine_id, commodity_name)
);

-- ─────────────────────────────────────────────────────────────────
-- 4. financial_models  (one per mine; holds shared assumptions)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS financial_models (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id               UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  model_name            TEXT NOT NULL DEFAULT 'Base Model',
  version               TEXT DEFAULT 'v1',
  wacc                  NUMERIC NOT NULL DEFAULT 0.14,
  tax_rate              NUMERIC NOT NULL DEFAULT 0.32,
  royalty_rate          NUMERIC NOT NULL DEFAULT 0.06,
  life_of_mine_years    INT,
  initial_capex         NUMERIC,         -- $mm total initial
  sustaining_capex_pa   NUMERIC,         -- $mm/yr sustaining
  capex_deployment_year INT DEFAULT 0,   -- year when main capex deployed
  closure_cost          NUMERIC,         -- $mm
  debt_funding          NUMERIC DEFAULT 0,
  debt_term_years       INT,
  interest_rate         NUMERIC,
  ramp_up_y1            NUMERIC DEFAULT 0.40,
  ramp_up_y2            NUMERIC DEFAULT 0.75,
  depreciation_years    INT DEFAULT 15,
  currency              TEXT DEFAULT 'USD',
  source_file           TEXT,
  notes                 TEXT,
  created_at            TIMESTAMPTZ DEFAULT now(),
  updated_at            TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────
-- 5. scenarios  (one row per model × commodity × scenario)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scenarios (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_id              UUID NOT NULL REFERENCES financial_models(id) ON DELETE CASCADE,
  scenario_name         scenario_name_enum NOT NULL DEFAULT 'Base',
  commodity_name        TEXT NOT NULL,
  price_base            NUMERIC,
  price_unit            TEXT,
  price_escalation_pct  NUMERIC DEFAULT 0.02,
  capex_mm              NUMERIC,
  opex_per_unit         NUMERIC,
  opex_total_pa         NUMERIC,
  basis_notes           TEXT,
  benchmark_source      TEXT,
  -- Pre-computed summary outputs
  npv                   NUMERIC,
  irr                   NUMERIC,
  payback_years         NUMERIC,
  moic                  NUMERIC,
  total_lom_revenue     NUMERIC,
  total_lom_fcf         NUMERIC,
  total_capex           NUMERIC,
  break_even_price      NUMERIC,
  created_at            TIMESTAMPTZ DEFAULT now(),
  UNIQUE (model_id, scenario_name, commodity_name)
);

-- ─────────────────────────────────────────────────────────────────
-- 6. dcf_years  (year-by-year cash flow — the time series core)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dcf_years (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id          UUID NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
  year                 INT NOT NULL CHECK (year >= 0),
  ramp_up_factor_pct   NUMERIC,
  ore_mined            NUMERIC,
  cumulative_ore_mined NUMERIC,
  remaining_reserve    NUMERIC,
  commodity_produced   NUMERIC,
  commodity_price      NUMERIC,
  gross_revenue        NUMERIC,          -- $mm
  royalty              NUMERIC,          -- $mm
  net_revenue          NUMERIC,          -- $mm
  operating_costs      NUMERIC,          -- $mm
  ebitda               NUMERIC,          -- $mm
  ebitda_margin_pct    NUMERIC,
  depreciation         NUMERIC,          -- $mm
  ebit                 NUMERIC,          -- $mm
  income_tax           NUMERIC,          -- $mm
  net_income           NUMERIC,          -- $mm
  capex                NUMERIC,          -- $mm
  free_cash_flow       NUMERIC,          -- $mm
  cumulative_fcf       NUMERIC,          -- $mm
  discount_factor      NUMERIC,
  discounted_cf        NUMERIC,          -- $mm
  UNIQUE (scenario_id, year)
);

-- ─────────────────────────────────────────────────────────────────
-- 7. monte_carlo_runs  (one session per model)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS monte_carlo_runs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_id      UUID NOT NULL REFERENCES financial_models(id) ON DELETE CASCADE,
  n_runs        INT NOT NULL DEFAULT 500,
  variation_pct JSONB,   -- {"price":0.20,"grade":0.10,"capex":0.50}
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

-- ─────────────────────────────────────────────────────────────────
-- 8. monte_carlo_results  (individual simulation rows)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS monte_carlo_results (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id                 UUID NOT NULL REFERENCES monte_carlo_runs(id) ON DELETE CASCADE,
  simulation_index       INT NOT NULL,
  npv                    NUMERIC,
  irr                    NUMERIC,
  payback_years          NUMERIC,
  moic                   NUMERIC,
  unit_margin            NUMERIC,
  total_lom_revenue      NUMERIC,
  total_lom_fcf          NUMERIC,
  total_mineral_produced NUMERIC,
  cost_per_unit          NUMERIC
);

-- ─────────────────────────────────────────────────────────────────
-- 9. risk_factors  (qualitative risk register)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS risk_factors (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id           UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  name              TEXT NOT NULL,
  category          risk_cat_enum,
  level             risk_level_enum,
  probability_label TEXT,    -- 'Probable (40%)', 'Possible (10%)'
  intensity         TEXT,
  duration          TEXT,    -- 'Short Term', 'Long Term'
  notes             TEXT,
  mitigation        TEXT
);

-- ─────────────────────────────────────────────────────────────────
-- INDEXES
-- ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_mine_commodities_mine ON mine_commodities(mine_id);
CREATE INDEX IF NOT EXISTS idx_financial_models_mine ON financial_models(mine_id);
CREATE INDEX IF NOT EXISTS idx_scenarios_model        ON scenarios(model_id);
CREATE INDEX IF NOT EXISTS idx_scenarios_name         ON scenarios(scenario_name);
CREATE INDEX IF NOT EXISTS idx_dcf_years_scenario     ON dcf_years(scenario_id);
CREATE INDEX IF NOT EXISTS idx_dcf_years_year         ON dcf_years(year);
CREATE INDEX IF NOT EXISTS idx_mc_results_run         ON monte_carlo_results(run_id);
CREATE INDEX IF NOT EXISTS idx_risk_factors_mine      ON risk_factors(mine_id);

-- ─────────────────────────────────────────────────────────────────
-- UPDATED_AT trigger helper
-- ─────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_mines_updated
  BEFORE UPDATE ON mines
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_models_updated
  BEFORE UPDATE ON financial_models
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ─────────────────────────────────────────────────────────────────
-- USEFUL VIEWS
-- ─────────────────────────────────────────────────────────────────

-- Portfolio summary (one row per mine, best/base NPV)
CREATE OR REPLACE VIEW v_portfolio AS
SELECT
  m.id,
  m.mine_number,
  m.mine_name,
  m.license_number,
  m.province,
  m.mine_type,
  m.status,
  m.ore_reserve,
  m.reserve_unit,
  m.jorc_status,
  m.lat,
  m.lng,
  s.npv,
  s.irr,
  s.payback_years,
  s.moic,
  s.total_lom_revenue,
  s.total_lom_fcf,
  s.commodity_name AS primary_commodity,
  s.scenario_name
FROM mines m
LEFT JOIN financial_models fm ON fm.mine_id = m.id
LEFT JOIN scenarios s ON s.model_id = fm.id AND s.scenario_name = 'Base';

-- Scenario comparison (all scenarios for a given model)
CREATE OR REPLACE VIEW v_scenario_comparison AS
SELECT
  m.mine_name,
  m.license_number,
  s.commodity_name,
  s.scenario_name,
  s.price_base,
  s.price_unit,
  s.capex_mm,
  s.opex_per_unit,
  s.npv,
  s.irr,
  s.payback_years,
  s.moic,
  s.break_even_price,
  s.basis_notes
FROM scenarios s
JOIN financial_models fm ON fm.id = s.model_id
JOIN mines m ON m.id = fm.mine_id
ORDER BY m.mine_name, s.commodity_name, s.scenario_name;

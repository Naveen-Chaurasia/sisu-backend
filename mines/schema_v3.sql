-- ════════════════════════════════════════════════════════════════════
-- Mining Investment Platform — schema_v3.sql
--
-- Additions on top of schema_v2.sql:
--   1. v1_mines table  — flat single-commodity table for prototype model
--   2. ALTER commodity_scenarios — per-scenario DCF input columns
--      (price_escalation_rate, opex, opex_escalation_rate, initial_capex,
--       sustaining_capex_pa, depreciation_pa, capex_deployment_year,
--       production_start_year, royalty_rate)
--
-- Safe to re-run: uses ADD COLUMN IF NOT EXISTS and CREATE TABLE IF NOT EXISTS.
-- Run in: Supabase Dashboard → SQL Editor → New Query
-- ════════════════════════════════════════════════════════════════════


-- ── 1. v1_mines  (flat prototype / single-commodity model) ──────────────────
-- Mirrors the Mine_Profile + Calcs sheet of the prototype workbook.
-- The DCF engine reads directly from this table — no separate scenario rows.
CREATE TABLE IF NOT EXISTS v1_mines (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Identity
  mine_name                TEXT NOT NULL,
  license_number           TEXT UNIQUE,
  country                  TEXT    DEFAULT 'Mozambique',
  province                 TEXT,
  mine_type                TEXT,                        -- 'Open Cast' | 'Underground' | 'Alluvial'
  status                   TEXT    DEFAULT 'Active',   -- 'Active' | 'Development' | 'Exploration'

  -- Commodity
  commodity                TEXT    NOT NULL,            -- e.g. 'Graphite', 'Gold', 'REE'
  price_unit               TEXT    DEFAULT '$/t',

  -- Reserve & throughput
  ore_reserve              NUMERIC,                    -- Mm³ or Mt
  reserve_unit             TEXT    DEFAULT 'Mm³',
  throughput_pa            NUMERIC,                    -- steady-state per year
  throughput_unit          TEXT    DEFAULT 'm³/yr',
  life_of_mine_yr          INT,

  -- Production inputs (from Mine_Profile C-column)
  annual_production        NUMERIC,                    -- t/yr or m³/yr at steady state
  grade                    NUMERIC,                    -- % or g/t
  grade_unit               TEXT    DEFAULT 'g/t',
  recovery_rate            NUMERIC DEFAULT 0.85,

  -- Price
  price_base               NUMERIC,                    -- $/t or $/oz or $/kg
  price_escalation_rate    NUMERIC DEFAULT 0.02,       -- % p.a.

  -- Cost structure
  initial_dev_capex        NUMERIC,                    -- $ (not $M)
  sustaining_capex_pa      NUMERIC DEFAULT 0,          -- $ p.a. after construction
  total_opex_steady_state  NUMERIC,                    -- $ p.a. at full production
  variable_cost_per_unit   NUMERIC,
  opex_escalation_rate     NUMERIC DEFAULT 0.02,
  avg_depreciation_years   INT,

  -- Ramp-up (fraction of steady-state)
  ramp_up_y1               NUMERIC DEFAULT 0.5,
  ramp_up_y2               NUMERIC DEFAULT 0.8,
  ramp_up_y3               NUMERIC DEFAULT 1.0,

  -- Finance
  wacc                     NUMERIC DEFAULT 0.15,
  tax_rate                 NUMERIC DEFAULT 0.32,
  royalty_rate             NUMERIC DEFAULT 0.03,
  debt_funding             NUMERIC DEFAULT 0,
  debt_term                INT     DEFAULT 0,
  interest_rate            NUMERIC DEFAULT 0,

  -- End-of-life
  closure_rehab_cost       NUMERIC DEFAULT 0,

  -- Timing
  capex_deployment_year    INT     DEFAULT 0,          -- year CAPEX is spent (0 = t=0)
  production_start_year    INT     DEFAULT 1,          -- year production starts

  -- Geography
  concession_area_ha       NUMERIC,

  -- Qualitative
  prospectivity_notes      TEXT,
  source_file              TEXT,
  risk_factors             JSONB   DEFAULT '[]'::jsonb,
  environmental_impacts    JSONB   DEFAULT '[]'::jsonb,

  is_user_created          BOOLEAN DEFAULT FALSE,
  created_at               TIMESTAMPTZ DEFAULT now(),

  -- Cached DCF summary (written by /calculate endpoint)
  npv                      NUMERIC,
  irr                      NUMERIC,
  moic                     NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_v1mines_commodity ON v1_mines(commodity);
CREATE INDEX IF NOT EXISTS idx_v1mines_status    ON v1_mines(status);


-- ── 2. commodity_scenarios — per-scenario DCF input columns ─────────────────
-- These allow Bear/Base/Bull scenarios to carry DIFFERENT price, opex, capex,
-- and timing assumptions.  The DCF engine uses these values preferentially and
-- falls back to mine-level fields when NULL.
ALTER TABLE commodity_scenarios
  ADD COLUMN IF NOT EXISTS price_escalation_rate  NUMERIC  DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS opex                   NUMERIC,           -- $ p.a. (overrides mine.total_opex_steady_state)
  ADD COLUMN IF NOT EXISTS opex_escalation_rate   NUMERIC  DEFAULT 0.02,
  ADD COLUMN IF NOT EXISTS initial_capex          NUMERIC,           -- $ (overrides mine.initial_dev_capex)
  ADD COLUMN IF NOT EXISTS sustaining_capex_pa    NUMERIC  DEFAULT 0,
  ADD COLUMN IF NOT EXISTS depreciation_pa        NUMERIC,           -- $ p.a. (overrides computed value)
  ADD COLUMN IF NOT EXISTS capex_deployment_year  INT      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS production_start_year  INT      DEFAULT 1,
  ADD COLUMN IF NOT EXISTS royalty_rate           NUMERIC;           -- overrides mine.royalty_rate when set


-- ── 3. _SCEN_PATCHABLE helper comment ────────────────────────────────────────
-- Update the api_mine_supabase.py _SCEN_PATCHABLE set to include the new fields:
--   price_escalation_rate, opex, opex_escalation_rate, initial_capex,
--   sustaining_capex_pa, depreciation_pa, capex_deployment_year,
--   production_start_year, royalty_rate


-- ── Verify ────────────────────────────────────────────────────────────────────
-- SELECT column_name FROM information_schema.columns WHERE table_name = 'v1_mines' ORDER BY ordinal_position;
-- SELECT column_name FROM information_schema.columns WHERE table_name = 'commodity_scenarios' ORDER BY ordinal_position;

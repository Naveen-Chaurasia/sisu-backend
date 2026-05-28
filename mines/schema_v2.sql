-- ════════════════════════════════════════════════════════════════════
-- Mining Investment Platform — Schema for benchmarked workbook ingestion
-- Tailored to:
--   • Mine1_12891L_Revised_Benchmarked_Apr2026.xlsx
--   • Complex_Release_2_Mine11_9015L_..._Graphite_3_scenarios_Apr2026.xlsx
--
-- Run in: Supabase Dashboard → SQL Editor → New Query
-- Safe to re-run: drops/recreates the objects it owns.
-- ════════════════════════════════════════════════════════════════════

-- ── Clean slate (child → parent order) ──────────────────────────────
DROP TABLE IF EXISTS dcf_years          CASCADE;
DROP TABLE IF EXISTS scenario_metrics   CASCADE;
DROP TABLE IF EXISTS commodity_scenarios CASCADE;
DROP TABLE IF EXISTS mine_commodities   CASCADE;
DROP TABLE IF EXISTS exec_summary_rows  CASCADE;
DROP TABLE IF EXISTS financial_models   CASCADE;
DROP TABLE IF EXISTS mines              CASCADE;

DROP TYPE IF EXISTS scenario_enum CASCADE;

CREATE TYPE scenario_enum AS ENUM ('Base','Bear','Bull','Single');
-- 'Single' = commodity has no Bear/Bull split (e.g. REE, Monazite, Gold).
-- Graphite uses Bear/Base/Bull; REE basket scenarios live on the model row.

-- ── 1. mines ────────────────────────────────────────────────────────
CREATE TABLE mines (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_name       TEXT NOT NULL,
  license_number  TEXT UNIQUE NOT NULL,
  country         TEXT DEFAULT 'Mozambique',
  province        TEXT,
  ore_reserve     NUMERIC,           -- e.g. 50 (Mt) or 31.89 (Mm³)
  reserve_unit    TEXT,              -- 'Mt' | 'm³' | 'Mm³'
  throughput_pa   NUMERIC,
  throughput_unit TEXT,              -- 'Mtpa' | 'm³/yr'
  life_of_mine_yr INT,
  concession_area_ha NUMERIC,
  wacc            NUMERIC,           -- 0.10 | 0.15
  tax_rate        NUMERIC,           -- 0.30 | 0.32  (IRPC)
  source_file     TEXT,
  headline        TEXT,              -- row-1 banner from Exec Summary
  subtitle        TEXT,              -- row-2 banner
  created_at      TIMESTAMPTZ DEFAULT now()
);

-- ── 2. financial_models (one per mine; the benchmarked model) ───────
CREATE TABLE financial_models (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id         UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  model_name      TEXT DEFAULT 'Revised & Benchmarked',
  wacc            NUMERIC,
  tax_rate        NUMERIC,
  currency        TEXT DEFAULT 'USD',
  -- combined headline metrics (base case) when present in Exec Summary
  combined_npv    NUMERIC,
  combined_irr    NUMERIC,
  combined_payback TEXT,
  combined_moic   NUMERIC,
  source_file     TEXT,
  created_at      TIMESTAMPTZ DEFAULT now()
);

-- ── 3. mine_commodities (REE / Monazite / Spodumene / Gold / Graphite) ─
CREATE TABLE mine_commodities (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id       UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  commodity     TEXT NOT NULL,       -- 'REE','Monazite','Spodumene','Gold','Graphite'
  is_primary    BOOLEAN DEFAULT FALSE,
  has_scenarios BOOLEAN DEFAULT FALSE,  -- TRUE only for Graphite (Bear/Base/Bull sheets)
  UNIQUE (mine_id, commodity)
);

-- ── 4. commodity_scenarios (one DCF sheet → one row) ────────────────
-- REE/Monazite/Spodumene/Gold each produce ONE row (scenario='Single' or 'Base').
-- Graphite produces THREE rows (Bear, Base, Bull).
CREATE TABLE commodity_scenarios (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  commodity_id  UUID NOT NULL REFERENCES mine_commodities(id) ON DELETE CASCADE,
  scenario      scenario_enum NOT NULL DEFAULT 'Single',
  sheet_name    TEXT,                -- exact source sheet, e.g. 'Graphite Bear'
  wacc          NUMERIC,
  price_base    NUMERIC,
  price_unit    TEXT,                -- '$/t conc','$/kg','$/t'
  basis_notes   TEXT,                -- benchmark line from row 2 / row 39
  UNIQUE (commodity_id, scenario)
);

-- ── 5. scenario_metrics (the R32–R37 summary block per sheet) ───────
CREATE TABLE scenario_metrics (
  scenario_id     UUID PRIMARY KEY REFERENCES commodity_scenarios(id) ON DELETE CASCADE,
  npv             NUMERIC,           -- $mm
  irr             NUMERIC,           -- fraction, e.g. 0.166  (NULL if N/A)
  payback         TEXT,              -- '7 year(s)' | 'Beyond LOM'
  moic            NUMERIC,
  total_capex     NUMERIC,           -- $mm
  total_lom_fcf   NUMERIC            -- $mm
);

-- ── 6. dcf_years (year-by-year time series, the model core) ─────────
CREATE TABLE dcf_years (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id     UUID NOT NULL REFERENCES commodity_scenarios(id) ON DELETE CASCADE,
  year            INT NOT NULL,          -- 0 .. life_of_mine
  production      NUMERIC,               -- R5 (tonnes/kg/t conc)
  commodity_price NUMERIC,               -- R8
  gross_revenue   NUMERIC,               -- R9
  royalty         NUMERIC,               -- R10
  net_revenue     NUMERIC,               -- R11
  operating_costs NUMERIC,               -- R14
  ebitda          NUMERIC,               -- R15
  ebitda_margin   NUMERIC,               -- R16 (fraction)
  depreciation    NUMERIC,               -- R19
  ebit            NUMERIC,               -- R20
  income_tax      NUMERIC,               -- R21
  capex           NUMERIC,               -- R24
  free_cash_flow  NUMERIC,               -- R25
  cumulative_fcf  NUMERIC,               -- R26
  discount_factor NUMERIC,               -- R27
  discounted_cf   NUMERIC,               -- R28
  UNIQUE (scenario_id, year)
);

-- ── 7. exec_summary_rows (verbatim board-memo rows incl. benchmark notes) ─
-- Keeps the qualitative narrative: assumptions, actions, risks, "vs original".
CREATE TABLE exec_summary_rows (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mine_id     UUID NOT NULL REFERENCES mines(id) ON DELETE CASCADE,
  row_index   INT,
  section     TEXT,        -- nearest preceding numbered section header
  metric      TEXT,        -- col A
  col_b       TEXT,        -- e.g. REE / Gold
  col_c       TEXT,        -- Monazite / Graphite Bear
  col_d       TEXT,        -- Spodumene / Graphite Base
  col_e       TEXT,        -- Combined / Graphite Bull
  notes       TEXT         -- col F: basis / benchmark / vs original
);

-- ── Indexes ─────────────────────────────────────────────────────────
CREATE INDEX idx_fm_mine        ON financial_models(mine_id);
CREATE INDEX idx_mc_mine        ON mine_commodities(mine_id);
CREATE INDEX idx_cs_commodity   ON commodity_scenarios(commodity_id);
CREATE INDEX idx_dcf_scenario   ON dcf_years(scenario_id);
CREATE INDEX idx_dcf_year       ON dcf_years(year);
CREATE INDEX idx_es_mine        ON exec_summary_rows(mine_id);

-- ── Convenience view: all scenario returns in one place ─────────────
CREATE OR REPLACE VIEW v_scenario_returns AS
SELECT
  m.mine_name,
  m.license_number,
  c.commodity,
  s.scenario,
  s.price_base,
  s.price_unit,
  sm.npv,
  sm.irr,
  sm.payback,
  sm.moic,
  sm.total_capex,
  sm.total_lom_fcf,
  s.basis_notes
FROM commodity_scenarios s
JOIN mine_commodities c ON c.id = s.commodity_id
JOIN mines m            ON m.id = c.mine_id
LEFT JOIN scenario_metrics sm ON sm.scenario_id = s.id
ORDER BY m.mine_name, c.commodity, s.scenario;
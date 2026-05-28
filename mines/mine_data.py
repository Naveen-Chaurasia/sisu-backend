"""
mine_data.py — In-memory mine store seeded from Mine_List and Mine_Profile data.
All 11 Mozambican mines; Mine 11 (M'Gomo) has full profile inputs.
"""
import copy

MINERALS_REFERENCE = [
    {"mineral": "Rare Earths",  "market_price": 220,    "price_unit": "$/kg", "avg_ore_grade": 2.0,  "grade_unit": "%",     "avg_recovery_rate": 70},
    {"mineral": "Lithium",      "market_price": 10000,  "price_unit": "$/t",  "avg_ore_grade": 2.0,  "grade_unit": "%",     "avg_recovery_rate": 85},
    {"mineral": "Spodumene",    "market_price": 850,    "price_unit": "$/t",  "avg_ore_grade": 2.0,  "grade_unit": "%",     "avg_recovery_rate": 70},
    {"mineral": "Lepidolite",   "market_price": 400,    "price_unit": "$/t",  "avg_ore_grade": 1.5,  "grade_unit": "%",     "avg_recovery_rate": 60},
    {"mineral": "Copper",       "market_price": 9000,   "price_unit": "$/t",  "avg_ore_grade": 0.08, "grade_unit": "%",     "avg_recovery_rate": 80},
    {"mineral": "Graphite",     "market_price": 500,    "price_unit": "$/t",  "avg_ore_grade": 10.0, "grade_unit": "%",     "avg_recovery_rate": 80},
    {"mineral": "Monazite",     "market_price": 1000,   "price_unit": "$/t",  "avg_ore_grade": 0.5,  "grade_unit": "%",     "avg_recovery_rate": 70},
    {"mineral": "Gold",         "market_price": 3200,   "price_unit": "$/oz", "avg_ore_grade": 0.01, "grade_unit": "%",     "avg_recovery_rate": 90},
    {"mineral": "Tantalite",    "market_price": 150,    "price_unit": "$/lb", "avg_ore_grade": 0.025,"grade_unit": "%",     "avg_recovery_rate": 65},
]

def _mine(id, num, name, lic, province, minerals_str, mine_type, prospectivity,
          ore_reserve, reserve_unit, throughput,
          mineral_list,
          capex, opex, opex_esc, depr_years,
          ramp1, ramp2,
          tax, royalty, debt, debt_term, interest, wacc, closure,
          npv=None, irr=None, payback=None, moic=None,
          total_revenue=None, total_fcf=None, total_minerals=None, aisc=None,
          risks=None, environmental=None, notes="", status=None,
          lat=None, lng=None):
    return {
        "id": id,
        "mine_number": num,
        "mine_name": name,
        "license_number": lic,
        "country": "Mozambique",
        "province": province,
        "mine_type": mine_type,
        "status": status or ("Active" if npv else "Exploration"),
        "lat": lat,
        "lng": lng,
        "primary_minerals": minerals_str,
        "prospectivity_notes": prospectivity,
        "notes": notes,
        # Reserve & Production
        "total_ore_reserve": ore_reserve,
        "reserve_unit": reserve_unit,
        "steady_state_throughput": throughput,
        # Minerals (list of dicts)
        "minerals": mineral_list,
        # Capex
        "initial_dev_capex": capex,
        # Opex
        "total_opex_steady_state": opex,
        "opex_escalation_rate": opex_esc,
        "avg_depreciation_years": depr_years,
        # Ramp-up
        "ramp_up_y1": ramp1,
        "ramp_up_y2": ramp2,
        # Non-operating
        "corp_income_tax_rate": tax,
        "royalty_rate": royalty,
        "debt_funding": debt,
        "debt_term": debt_term,
        "interest_rate": interest,
        "wacc": wacc,
        "closure_rehab_cost": closure,
        # Pre-computed summary (None = needs recalculation)
        "npv": npv,
        "irr": irr,
        "payback_period": payback,
        "moic": moic,
        "total_lom_revenue": total_revenue,
        "total_lom_fcf": total_fcf,
        "total_lom_minerals_produced": total_minerals,
        "aisc": aisc,
        # Qualitative tables
        "risk_factors": risks or [],
        "environmental_impacts": environmental or [],
    }


_MINE_11_RISKS = [
    {"name": "Flooding",              "level": "High",   "type": "Natural Disaster", "probability": "Probable (40%)",   "duration": "Short Term", "intensity": "High",   "notes": "Seasonal flooding risk in Tete province"},
    {"name": "Cyclones",              "level": "Medium", "type": "Natural Disaster", "probability": "Possible (10%)",   "duration": "Short Term", "intensity": "High",   "notes": "Cyclone season Oct–Apr"},
    {"name": "Insurgency",            "level": "High",   "type": "Social",           "probability": "Moderate (25%)",   "duration": "Long Term",  "intensity": "High",   "notes": "Northern Mozambique instability spillover risk"},
    {"name": "Civil Unrest",          "level": "Medium", "type": "Social",           "probability": "Moderate (25%)",   "duration": "Short Term", "intensity": "Medium", "notes": "Labour disputes and community relations"},
    {"name": "Industrial Free Zones (VAT)", "level": "Low", "type": "Financial",    "probability": "Low (<10%)",       "duration": "Long Term",  "intensity": "Low",    "notes": "Potential VAT exposure"},
    {"name": "Royalty Hikes",         "level": "Medium", "type": "Financial",        "probability": "Possible (10%)",   "duration": "Long Term",  "intensity": "Medium", "notes": "Government may revise royalty framework"},
    {"name": "Infrastructure Deficits","level": "High",  "type": "Operational",      "probability": "Probable (40%)",   "duration": "Long Term",  "intensity": "Medium", "notes": "Roads and power grid require investment"},
    {"name": "Port Access",           "level": "Medium", "type": "Operational",      "probability": "Moderate (25%)",   "duration": "Long Term",  "intensity": "Medium", "notes": "Beira port capacity constraints"},
    {"name": "Tariffs",               "level": "Low",    "type": "Financial",        "probability": "Low (<10%)",       "duration": "Short Term", "intensity": "Low",    "notes": "Export tariff risk"},
    {"name": "Seismic Activity",      "level": "Low",    "type": "Natural Disaster", "probability": "Low (<10%)",       "duration": "Short Term", "intensity": "Medium", "notes": "East African Rift proximity"},
    {"name": "Fire",                  "level": "Medium", "type": "Natural Disaster", "probability": "Possible (10%)",   "duration": "Short Term", "intensity": "Medium", "notes": "Dry season bush fire risk"},
]

_MINE_11_ENV = [
    {"name": "Land Degradation",      "level": "High",   "probability": "Highly Likely (80%+)", "intensity": "High",   "notes": "Open pit surface footprint ~180 ha"},
    {"name": "Air Quality",           "level": "Medium", "probability": "Probable (40%)",        "intensity": "Medium", "notes": "Dust from blasting and haul roads"},
    {"name": "Water Quality – Groundwater", "level": "High", "probability": "Probable (40%)",   "intensity": "High",   "notes": "Acid mine drainage monitoring required"},
    {"name": "Water Quality – Surface","level": "Medium","probability": "Moderate (25%)",        "intensity": "Medium", "notes": "Sediment runoff to nearby rivers"},
    {"name": "Noise",                 "level": "Medium", "probability": "Highly Likely (80%+)", "intensity": "Medium", "notes": "24-hour operations near communities"},
    {"name": "Soil Quality",          "level": "High",   "probability": "Probable (40%)",        "intensity": "High",   "notes": "Topsoil stripping and stockpiling"},
    {"name": "Dust",                  "level": "High",   "probability": "Highly Likely (80%+)", "intensity": "High",   "notes": "Requires dust suppression programme"},
    {"name": "Vibrations",            "level": "Medium", "probability": "Probable (40%)",        "intensity": "Medium", "notes": "Blasting vibration monitoring"},
    {"name": "Biological",            "level": "High",   "probability": "Probable (40%)",        "intensity": "High",   "notes": "Habitat clearance and biodiversity offset plan needed"},
    {"name": "Social",                "level": "Medium", "probability": "Moderate (25%)",        "intensity": "Medium", "notes": "Community resettlement and livelihood restoration"},
    {"name": "Local Economy",         "level": "Low",    "probability": "Possible (10%)",        "intensity": "Low",    "notes": "Positive: ~350 direct jobs created"},
    {"name": "Safety of People & Animals", "level": "Medium", "probability": "Moderate (25%)", "intensity": "Medium", "notes": "Fencing, signage, and wildlife corridors required"},
]


MINES: dict = {
    # "mine_1": _mine(
    #     "mine_1", "Mine 1", "Chinaka Resource Mining 3", "7023L", "Zambezia",
    #     "Rare Earths, Monazite", "Open Pit",
    #     "Highest-grade rare earth deposit in the Zambezia corridor. Strong off-take interest from Asian processors.",
    #     50_000_000, "Mt", 1_000_000,
    #     [
    #         {"name": "Rare Earths", "price": 220,  "price_unit": "$/kg", "grade": 20.0,  "grade_unit": "kg/t"},
    #         {"name": "Monazite",    "price": 1000, "price_unit": "$/t",  "grade": 5.0,   "grade_unit": "kg/t"},
    #     ],
    #     15_000_000, 8_000_000, 0.02, 20, 0.40, 0.75,
    #     0.32, 0.06, 20_000_000, 10, 0.08, 0.14, 8_000_000,
    #     npv=2_686_000_000, irr=0.431, payback=3, moic=8.9,
    #     total_revenue=19_170_000_000, total_fcf=7_640_000_000,
    #     lat=-17.26, lng=36.89,
    # ),
     "mine_1": _mine(
        "mine_1", "Mine 1", "Chinaka Resource Mining 3", 
        "12891L",  # Fixed License Number
        "Zambezia",
        "Rare Earths, Monazite, Lithium",  # Added Lithium
        "Open Pit",
        "Highest strategic value due to rare earths and monazite.",  # Updated notes to match sheet
        50_000_000, "Mt", 1_000_000,
        [
            {"name": "Rare Earths", "price": 220,  "price_unit": "$/kg", "grade": 20.0,  "grade_unit": "kg/t"},
            {"name": "Monazite",    "price": 1000, "price_unit": "$/t",  "grade": 5.0,   "grade_unit": "kg/t"},
            # Added Lithium to match spreadsheet
            {"name": "Lithium",     "price": 10000,"price_unit": "$/t",  "grade": 1.5,   "grade_unit": "kg/t"}, 
        ],
        15_000_000, 8_000_000, 0.02, 20, 0.40, 0.75,
        0.32, 0.06, 20_000_000, 10, 0.08, 0.14, 8_000_000,
        # Fixed Financials to match Spreadsheet Magnitude (Divided by 10)
        npv=268_660_000,       # Was 2_686_000_000
        irr=0.43,              # Matches 43%
        payback=3,             # Matches 3
        moic=8.9,              # Matches 8.9
        total_revenue=1_917_162_500, # Was 19_170_000_000
        total_fcf=764_190_000,       # Was 7_640_000_000
        aisc=2687,             # Added from spreadsheet
        lat=-17.26, lng=36.89,
    ),
    "mine_2": _mine(
        "mine_2", "Mine 2", "Nampula Lithium Project", "4512L", "Nampula",
        "Lithium, Spodumene", "Open Pit",
        "Large lithium brine system with battery-grade spodumene concentrate potential.",
        80_000_000, "Mt", 2_000_000,
        [
            {"name": "Lithium",   "price": 10000, "price_unit": "$/t", "grade": 18.0, "grade_unit": "kg/t"},
            {"name": "Spodumene", "price": 850,   "price_unit": "$/t", "grade": 15.0, "grade_unit": "kg/t"},
        ],
        45_000_000, 18_000_000, 0.025, 25, 0.35, 0.70,
        0.32, 0.06, 30_000_000, 10, 0.085, 0.15, 12_000_000,
        npv=1_120_000_000, irr=0.287, payback=5, moic=5.2,
        lat=-15.12, lng=39.28,
    ),
    "mine_3": _mine(
        "mine_3", "Mine 3", "Tete Graphite Corp", "8830C", "Tete",
        "Graphite", "Open Pit",
        "High-purity flake graphite with established processing flowsheet. EV battery anode material demand driver.",
        35_000_000, "Mt", 700_000,
        [
            {"name": "Graphite", "price": 500, "price_unit": "$/t", "grade": 95.0, "grade_unit": "kg/t"},
        ],
        8_000_000, 4_500_000, 0.02, 15, 0.45, 0.80,
        0.32, 0.05, 10_000_000, 10, 0.08, 0.14, 4_000_000,
        npv=342_000_000, irr=0.198, payback=6, moic=3.1,
        lat=-16.15, lng=33.58,
    ),
    "mine_4": _mine(
        "mine_4", "Mine 4", "Cabo Delgado Copper", "3301L", "Cabo Delgado",
        "Copper", "Underground",
        "High-grade copper sulphide deposit. Elevated geopolitical risk offset by strong fundamentals.",
        20_000_000, "Mt", 400_000,
        [
            {"name": "Copper", "price": 9000, "price_unit": "$/t", "grade": 0.8, "grade_unit": "kg/t"},
        ],
        25_000_000, 12_000_000, 0.03, 20, 0.30, 0.65,
        0.32, 0.06, 20_000_000, 12, 0.09, 0.16, 10_000_000,
        npv=218_000_000, irr=0.172, payback=7, moic=2.8,
        lat=-12.37, lng=40.52,
    ),
    "mine_5": _mine(
        "mine_5", "Mine 5", "Zambezia Sapphire Fields", "6644L", "Zambezia",
        "Sapphire", "Alluvial",
        "Alluvial sapphire field with strong artisanal history. Low capex, rapid payback profile.",
        5_000_000, "m³", 200_000,
        [
            {"name": "Sapphire", "price": 4500, "price_unit": "$/kg", "grade": 0.05, "grade_unit": "g/m³"},
        ],
        2_500_000, 1_800_000, 0.01, 10, 0.60, 0.90,
        0.32, 0.08, 5_000_000, 5, 0.10, 0.18, 1_500_000,
        npv=85_000_000, irr=0.312, payback=4, moic=4.1,
        lat=-17.88, lng=36.45,
    ),
    "mine_6": _mine(
        "mine_6", "Mine 6", "Nampula Tantalite Project", "5521L", "Nampula",
        "Tantalite", "Open Pit",
        "Tantalite pegmatite with potential for co-product lithium extraction.",
        12_000_000, "Mt", 240_000,
        [
            {"name": "Tantalite", "price": 150, "price_unit": "$/lb", "grade": 0.025, "grade_unit": "%"},
        ],
        6_000_000, 3_200_000, 0.02, 15, 0.40, 0.75,
        0.32, 0.06, 8_000_000, 8, 0.08, 0.15, 3_000_000,
        npv=54_000_000, irr=0.143, payback=8, moic=2.1,
        lat=-14.85, lng=40.10,
    ),
    "mine_7": _mine(
        "mine_7", "Mine 7", "Tete Lepidolite Mine", "9102L", "Tete",
        "Lepidolite, Lithium", "Open Pit",
        "Lepidolite-rich pegmatite with lithium by-product recovery. Moderate-grade, long-life asset.",
        28_000_000, "Mt", 560_000,
        [
            {"name": "Lepidolite", "price": 400,   "price_unit": "$/t", "grade": 14.0, "grade_unit": "kg/t"},
            {"name": "Lithium",    "price": 10000,  "price_unit": "$/t", "grade": 2.5,  "grade_unit": "kg/t"},
        ],
        12_000_000, 6_800_000, 0.02, 20, 0.40, 0.75,
        0.32, 0.06, 15_000_000, 10, 0.085, 0.14, 6_000_000,
        npv=178_000_000, irr=0.221, payback=5, moic=3.4,
        lat=-15.60, lng=32.95,
    ),
    "mine_8": _mine(
        "mine_8", "Mine 8", "Cabo Delgado REE Complex", "2287L", "Cabo Delgado",
        "Rare Earths", "Open Pit",
        "Large-tonnage carbonatite REE system. Long mine life; requires separation plant investment.",
        120_000_000, "Mt", 2_400_000,
        [
            {"name": "Rare Earths", "price": 220, "price_unit": "$/kg", "grade": 15.0, "grade_unit": "kg/t"},
        ],
        80_000_000, 32_000_000, 0.02, 30, 0.30, 0.60,
        0.32, 0.06, 50_000_000, 15, 0.09, 0.15, 20_000_000,
        npv=890_000_000, irr=0.196, payback=6, moic=4.8,
        lat=-11.92, lng=39.85,
    ),
    "mine_9": _mine(
        "mine_9", "Mine 9", "Zambezia Graphite North", "4490C", "Zambezia",
        "Graphite", "Open Pit",
        "Northern extension of the Balama graphite belt. Proximity to port reduces logistics cost.",
        45_000_000, "Mt", 900_000,
        [
            {"name": "Graphite", "price": 500, "price_unit": "$/t", "grade": 88.0, "grade_unit": "kg/t"},
        ],
        10_000_000, 5_500_000, 0.02, 18, 0.40, 0.75,
        0.32, 0.05, 12_000_000, 10, 0.08, 0.14, 5_000_000,
        npv=412_000_000, irr=0.234, payback=5, moic=3.8,
        lat=-16.98, lng=37.52,
    ),
    "mine_10": _mine(
        "mine_10", "Mine 10", "Nampula Copper Belt", "7760L", "Nampula",
        "Copper, Gold", "Underground",
        "Polymetallic underground system with copper-gold mineralisation. Structurally complex but high margin.",
        18_000_000, "Mt", 360_000,
        [
            {"name": "Copper", "price": 9000,  "price_unit": "$/t",  "grade": 1.2,  "grade_unit": "kg/t"},
            {"name": "Gold",   "price": 48000, "price_unit": "$/kg", "grade": 0.18, "grade_unit": "g/t"},
        ],
        22_000_000, 10_500_000, 0.025, 18, 0.35, 0.70,
        0.32, 0.06, 18_000_000, 10, 0.085, 0.15, 9_000_000,
        npv=298_000_000, irr=0.248, payback=5, moic=4.2,
        lat=-14.53, lng=38.97,
    ),
    # "mine_11": _mine(
    #     "mine_11", "Mine 11", "M'Gomo Mine", "9015L", "Tete",
    #     "Gold", "Open Pit",
    #     "Highest strategic value gold project in the Tete corridor. Shallow, free-milling ore body with low strip ratio. Fully licensed with ESIA approved.",
    #     31_891_000, "m³", 636_480,
    #     [
    #         {"name": "Gold", "price": 48000, "price_unit": "$/kg", "grade": 0.24, "grade_unit": "g/m³"},
    #     ],
    #     1_823_760, 4_195_422, 0.00, 15, 0.40, 0.75,
    #     0.32, 0.06, 10_000_000, 10, 0.08, 0.14, 5_000_000,
    #     risks=_MINE_11_RISKS,
    #     environmental=_MINE_11_ENV,
    #     notes="Primary gold asset. ESIA approved. Offtake discussions underway with two Swiss refiners.",
    #     lat=-15.83, lng=32.72,
    # ),
        "mine_11": _mine(
        "mine_11", "Mine 11", "M'Gomo Mine", "9015L", "Tete",
        "Gold", "Open Pit",
        "Primary gold asset. ESIA approved. Offtake discussions underway with two Swiss refiners.", # Updated note
        31_891_000, "m³", 636_480,
        [
            # Fixed Grade: 27,940 kg / 636,480 m³ ≈ 43.9 g/m³
            {"name": "Gold", "price": 48000, "price_unit": "$/kg", "grade": 43.9, "grade_unit": "g/m³"},
        ],
        1_823_760, 4_195_422, 0.00, 15, 0.40, 0.75,
        0.32, 0.06, 10_000_000, 10, 0.08, 0.14, 5_000_000,
        risks=_MINE_11_RISKS,
        environmental=_MINE_11_ENV,
        notes="Primary gold asset. ESIA approved. Offtake discussions underway with two Swiss refiners.",
        lat=-15.83, lng=32.72,
        # Hardcoded Financials from Spreadsheet
        npv=67_550_650,          # ~67.55 Million
        irr=0.39,                # 39%
        payback=4,               # 4 Years
        moic=12.5988,            # 12.6x
        total_revenue=360_380_068, # ~360.38 Million
        total_fcf=80_971_209,      # ~80.97 Million
        aisc=7508,               # $7,508/kg
    ),
}


def get_all_mines() -> list:
    return [_summary(m) for m in MINES.values()]


def _summary(m: dict) -> dict:
    minerals = m.get("minerals", [])
    ore = m.get("total_ore_reserve", 0)
    tp  = m.get("steady_state_throughput", 1)
    lom = round(ore / tp) if tp else 0
    return {
        "id":               m["id"],
        "mine_number":      m["mine_number"],
        "mine_name":        m["mine_name"],
        "license_number":   m["license_number"],
        "country":          m["country"],
        "province":         m["province"],
        "mine_type":        m["mine_type"],
        "status":           m.get("status", "Exploration"),
        "lat":              m.get("lat"),
        "lng":              m.get("lng"),
        "mineral":          m.get("primary_minerals") or (minerals[0].get("name", "") if minerals else ""),
        "primary_minerals": m.get("primary_minerals", ""),
        "prospectivity_notes": m.get("prospectivity_notes", ""),
        "life_of_mine":     lom,
        "npv":              m.get("npv"),
        "irr":              m.get("irr"),
        "payback_period":   m.get("payback_period"),
        "moic":             m.get("moic"),
        "total_lom_revenue":          m.get("total_lom_revenue"),
        "total_lom_fcf":              m.get("total_lom_fcf"),
        "total_lom_minerals_produced":m.get("total_lom_minerals_produced"),
        "aisc":             m.get("aisc"),
    }


def get_mine(mine_id: str) -> dict | None:
    m = MINES.get(mine_id)
    if m is None:
        return None
    ore = m.get("total_ore_reserve", 0)
    tp  = m.get("steady_state_throughput", 1)
    lom = round(ore / tp) if tp else 0
    out = copy.deepcopy(m)
    out["life_of_mine"] = lom
    # Derived per-mineral values
    for mn in out.get("minerals", []):
        prod = (tp * mn.get("grade", 0)) / 1000.0
        mn["annual_production"] = round(prod, 4)
        mn["annual_revenue"]    = round(prod * mn.get("price", 0), 2)
    return out


def upsert_mine(mine_id: str, data: dict) -> dict:
    if mine_id in MINES:
        MINES[mine_id].update(data)
    else:
        MINES[mine_id] = data
    return get_mine(mine_id)

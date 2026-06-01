"""
mask_utils.py — display masking for Mines4 API responses.
Replaces mine_name, license_number, country with anonymised labels
(Mine 1 / License1 / Country1 …) ordered by created_at so each mine
always gets the same number.  Real data stays unchanged in the DB.
"""
from typing import Dict

# Coordinates keyed by original license number (mirrors MineMap4.jsx MINE_COORDS)
_COORDS_BY_LICENSE: Dict[str, dict] = {
    "12891L": {"lat": -17.0, "lng": 36.8},
    "9015L":  {"lat": -15.5, "lng": 32.5},
    "1234L":  {"lat": -13.8, "lng": 35.2},
    "5678L":  {"lat": -16.2, "lng": 33.6},
    "9101L":  {"lat": -14.4, "lng": 34.1},
}

# Fallback coords keyed by lowercased original mine name (mirrors MineMap4.jsx MINE_COORDS_BY_NAME)
_COORDS_BY_NAME: Dict[str, dict] = {
    "m'gomo":  {"lat": -15.2, "lng": 33.6},
    "mgomo":   {"lat": -15.2, "lng": 33.6},
    "mine c3": {"lat": -14.8, "lng": 38.4},
    "mine g":  {"lat": -13.5, "lng": 35.8},
    "mine a":  {"lat": -16.8, "lng": 35.1},
    "mine b":  {"lat": -17.4, "lng": 36.2},
    "mine d":  {"lat": -12.9, "lng": 39.1},
    "mine e":  {"lat": -15.9, "lng": 32.8},
    "mine f":  {"lat": -14.1, "lng": 34.6},
}


def _resolve_coords(mine: dict) -> dict:
    """Return {lat, lng} from original (pre-mask) license or name, or {} if unknown."""
    if mine.get("lat") is not None and mine.get("lng") is not None:
        return {}  # already present in DB row
    lic = (mine.get("license_number") or "").strip()
    if lic in _COORDS_BY_LICENSE:
        return _COORDS_BY_LICENSE[lic]
    name = (mine.get("mine_name") or "").lower().strip()
    for k, v in _COORDS_BY_NAME.items():
        if name == k or name.startswith(k):
            return v
    return {}


def build_mask_map(sb_client) -> Dict[str, dict]:
    rows = (
        sb_client.table("m4_mines")
        .select("id")
        .order("created_at")
        .execute()
        .data or []
    )
    return {
        row["id"]: {
            "mine_name":      f"Mine {i + 1}",
            "license_number": f"License{i + 1}",
            "country":        f"Country{i + 1}",
        }
        for i, row in enumerate(rows)
    }


def mask_mine(mine: dict, mask_map: Dict[str, dict]) -> dict:
    """Overlay masked fields onto a mine dict; leaves all other fields intact.
    Also injects lat/lng from original license/name so the map can still plot the mine."""
    coords = _resolve_coords(mine)
    return {**mine, **coords, **(mask_map.get(mine.get("id"), {}))}

"""
mask_utils.py — display masking for Mines4 API responses.
Replaces mine_name, license_number, country with anonymised labels
(Mine 1 / License1 / Country1 …) ordered by created_at so each mine
always gets the same number.  Real data stays unchanged in the DB.
"""
from typing import Dict


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
    """Overlay masked fields onto a mine dict; leaves all other fields intact."""
    return {**mine, **(mask_map.get(mine.get("id"), {}))}

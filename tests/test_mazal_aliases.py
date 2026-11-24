"""Conservative J9U matching and integration with the rarity build."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_data
import build_mazal_aliases as mazal
from normalize import build_reversed_pair_map, load_aliases, structural_clean
from collections import Counter


def row(name, qid, j9u, pref):
    return {k: {"value": v} for k, v in {"q": name, "qid": qid, "j9u": j9u, "pref": pref}.items()}


def test_groups_by_identifier_and_preserves_reviewed_mappings():
    names = ["משה מונטפיורי", "משה מונטיפיורי", "השר משה מונטיפיורי", "מונטיפיורי"]
    rows = [row(n, "Q1", "98701", "משה מונטיפיורי") for n in names]
    aliases, sources = mazal.build_aliases(names, rows, {"משה מונטיפיורי": "משה מונטיפיורי"})
    assert aliases["משה מונטפיורי"] == "משה מונטיפיורי"
    assert aliases["השר משה מונטיפיורי"] == "משה מונטיפיורי"
    assert "מונטיפיורי" not in aliases
    assert sources[0]["j9u"] == "98701"
    assert sources[0]["qid"] == "Q1"


def test_ambiguous_and_conflicting_groups_are_not_merged():
    names = ["אדם ראשון", "אדם שני", "שם אחר", "שם נוסף"]
    rows = [row("אדם ראשון", "Q1", "1", "אדם ראשון"),
            row("אדם שני", "Q1", "1", "אדם ראשון"),
            row("אדם שני", "Q2", "2", "אדם שני"),
            row("שם אחר", "Q3", "3", "שם אחר"),
            row("שם נוסף", "Q3", "3", "שם אחר")]
    existing = {"שם אחר": "זהות א", "שם נוסף": "זהות ב"}
    assert mazal.build_aliases(names, rows, existing) == (existing, [])
    # A shared display label, or multiple IDs for the same QID, cannot be used.
    rows = [row("אדם ראשון", "Q1", "1", "אדם ראשון"),
            row("אדם שני", "Q1", "4", "אדם ראשון"),
            row("שם אחר", "Q2", "2", "אדם ראשון"),
            row("שם נוסף", "Q2", "2", "אדם ראשון")]
    assert mazal.build_aliases(names, rows, {}) == ({}, [])


def test_failed_request_does_not_return_partial_results(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr(mazal.requests, "get", fail)
    monkeypatch.setattr(mazal.time, "sleep", lambda _: None)
    import pytest
    with pytest.raises(RuntimeError, match="no aliases written"):
        mazal.sparql("query")


def test_reversal_respects_distinct_explicit_identities():
    counts = Counter({"אדם ראשון": 2, "ראשון אדם": 1})
    aliases = {"אדם ראשון": "אדם ראשון", "ראשון אדם": "ראשון אדם"}
    assert build_reversed_pair_map(counts, aliases) == {}


def test_montefiore_aliases_affect_locality_counts(monkeypatch, tmp_path):
    aliases = load_aliases(ROOT / "data" / "mazal_aliases.json")
    assert aliases[structural_clean("רחוב משה מונטפיורי")] == "משה מונטיפיורי"
    assert "מונטיפיורי" not in aliases
    streets = [
        {"סמל_ישוב": 1, "שם_ישוב": "א", "סמל_רחוב": 1, "שם_רחוב": "משה מונטיפיורי"},
        {"סמל_ישוב": 1, "שם_ישוב": "א", "סמל_רחוב": 2, "שם_רחוב": "השר משה מונטיפיורי"},
        {"סמל_ישוב": 2, "שם_ישוב": "ב", "סמל_רחוב": 3, "שם_רחוב": "משה מונטפיורי"},
        {"סמל_ישוב": 2, "שם_ישוב": "ב", "סמל_רחוב": 4, "שם_רחוב": "מונטפיורי משה"},
    ]
    cbs = [{"סמל יישוב": i, "שם מעמד מונציפאלי": "עירייה", "דת יישוב": 1,
            "שנת ייסוד": 1900, "קואורדינטות": 220976632344} for i in (1, 2)]
    monkeypatch.setattr(build_data, "fetch_all", lambda rid: streets if rid == build_data.STREETS_ID else cbs)
    monkeypatch.setattr(build_data, "OUT", tmp_path)
    monkeypatch.setattr(build_data, "MIN_STREETS", 1)
    build_data.main()
    places = json.loads((tmp_path / "data.json").read_text())
    assert len(places) == 2
    assert all(p["streets"] == 1 and p["pure_exclusive"] == 0 for p in places)
    assert all(p["sample_common"][0] == {"name": "משה מונטיפיורי", "n": 2} for p in places)

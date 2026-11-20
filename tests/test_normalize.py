"""Unit and data-integrity tests for israel-street-rarity."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from normalize import (  # noqa: E402
    build_reversed_pair_map,
    classify_sector,
    is_numeric,
    is_placeholder_street,
    load_aliases,
    structural_clean,
    unify_punctuation,
)


def test_placeholder_street_detection():
    assert is_placeholder_street(9000)
    assert is_placeholder_street("9000")
    assert not is_placeholder_street(9477)
    assert not is_placeholder_street(101)
    assert not is_placeholder_street(None)


def test_is_numeric():
    assert is_numeric("123")
    assert is_numeric("רחוב 45")
    assert is_numeric("רח 200")
    assert is_numeric("רח רח 270")
    assert is_numeric("כביש 31")
    assert is_numeric("סמ 1")
    assert is_numeric("ש 116")
    assert not is_numeric("הרצל")
    assert not is_numeric("השקד 1")


def test_structural_clean_prefixes_and_punctuation():
    assert structural_clean("משעול הרקפת") == "הרקפת"
    assert structural_clean("מבוא הזית") == "הזית"
    assert structural_clean("סמ האלה") == "האלה"
    assert structural_clean("סמ' השקדיה") == "השקדיה"
    assert structural_clean("שדרת הברושים") == "הברושים"
    assert structural_clean("שד' הציונות") == "הציונות"
    assert structural_clean("רחבת אביר") == "אביר"
    assert structural_clean("נתיב האורנים") == "האורנים"
    assert structural_clean("מעגל החצב") == "החצב"
    assert structural_clean("ש הותיקים") == "הותיקים"
    # Period initial "ש. שלום" must NOT be stripped as neighborhood "ש "
    assert structural_clean("ש. שלום") == "ש. שלום"
    # Niqqud, dashes, quotes unified
    assert structural_clean("עָמוֹס") == "עמוס"
    assert structural_clean('דוד בן־גוריון') == "דוד בן גוריון"
    assert structural_clean('רמב"ם') == "רמב״ם"
    # Definite article ה is preserved
    assert structural_clean("הזית") == "הזית"
    # Single-char leftovers dropped
    assert structural_clean("רח א") == ""
    assert structural_clean("רח ו") == ""


def test_reversed_pair_canonicalization():
    counts = Counter({
        "בגין מנחם": 31,
        "מנחם בגין": 15,
        "דוד המלך": 28,
        "המלך דוד": 7,
        "שניאור זלמן": 10,
        "זלמן שניאור": 1,
        "בית גן": 1,
        "גן בית": 1,
    })
    aliases = {"מנחם בגין": "מנחם בגין", "בגין מנחם": "מנחם בגין"}
    rev = build_reversed_pair_map(counts, aliases)
    assert rev.get("בגין מנחם") == "מנחם בגין"
    assert rev.get("המלך דוד") == "דוד המלך"
    # Different people & non-person constructs must NOT be merged
    assert "שניאור זלמן" not in rev and "זלמן שניאור" not in rev
    assert "בית גן" not in rev and "גן בית" not in rev


def test_classify_sector_cbs():
    assert classify_sector(1, 0, 50000) == ("יהודי", 0.0)
    assert classify_sector(2, 15259, 15263) == ("ערבי", 100.0)
    assert classify_sector(4, 391388, 980674) == ("מעורב", 39.9)
    # Fallback when religion code missing
    assert classify_sector(None, 800, 1000) == ("ערבי", 80.0)
    assert classify_sector(None, 250, 1000) == ("מעורב", 25.0)
    assert classify_sector(None, 10, 1000) == ("יהודי", 1.0)


def test_aliases_hygiene():
    aliases = load_aliases(ROOT / "data" / "mazal_aliases.json")
    assert len(aliases) > 200
    # Infamous false merges from old Wikidata pull must NEVER return
    for bad in ("צבר", "הצבר", "ירמיהו", "יעל", "דניאל", "אהוד מנור", "המהנדס", "אשוח", "הגל", "המפל", "שני", "נועה", "נטע", "עוז", "אילן", "שמעון מזרחי", "אלי מויאל"):
        assert bad not in aliases, f"Unsafe alias key {bad!r} should not be in mazal_aliases.json"
    # Key corrections
    assert aliases.get("טרומפלדור") == "יוסף טרומפלדור"
    assert aliases.get("פינסקר") == "יהודה לייב פינסקר"
    assert aliases.get("ויצמן") == "חיים ויצמן"
    assert aliases.get("עזר וייצמן") == "עזר ויצמן"
    # No NLI catalog commas/years or un-unified punctuation
    for k, v in aliases.items():
        assert "," not in v, f"Catalog-style comma in alias value: {v!r}"
        assert k == unify_punctuation(k)
        assert v == unify_punctuation(v)


def test_generated_data_integrity_and_sector_fixes():
    data_path = ROOT / "data.json"
    if not data_path.exists():
        return
    data = json.loads(data_path.read_text(encoding="utf-8"))
    by_name = {d["name"]: d for d in data}
    assert len(data) >= 180

    # Mathematical invariants for every single place
    for d in data:
        assert d["streets"] >= 5, d["name"]
        assert 0 <= d["pure_exclusive"] <= d["streets"], d["name"]
        assert d["rarity_sum_log"] <= d["streets"] + 0.15, f"sum_log > streets in {d['name']}"
        assert d["rarity_sum_sqrt"] <= d["streets"] + 0.15, f"sum_sqrt > streets in {d['name']}"
        assert 0.0 <= d["rarity_mean_log"] <= 100.0, d["name"]
        assert 0.0 <= d["rarity_mean_sqrt"] <= 100.0, d["name"]
        assert d["sector"] in {"יהודי", "ערבי", "מעורב"}, d["name"]

    # Verify previously misclassified Arab/Druze/Circassian places are now "ערבי"
    for name in ("מעלה עירון", "כפר כמא", "ג'דיידה-מכר", "בענה", "נחף", "עספיא", "אבו גוש", "אבו סנאן", "ירכא", "בית ג'ן", "תל שבע", "ערערה", "ג'לג'וליה"):
        if name in by_name:
            assert by_name[name]["sector"] == "ערבי", f"{name} should be ערבי"
            assert by_name[name]["is_arab"] is True

    # Verify official CBS mixed cities are now "מעורב"
    for name in ("ירושלים", "חיפה", "לוד", "רמלה", "עכו", "נוף הגליל", "מעלות-תרשיחא"):
        if name in by_name:
            assert by_name[name]["sector"] == "מעורב", f"{name} should be מעורב"
            assert by_name[name]["is_mixed"] is True

    # Verify Jewish cities are "יהודי"
    for name in ("ראשון לציון", "פתח תקווה", "באר שבע", "טירת כרמל", "מודיעין עילית", "הרצליה"):
        if name in by_name:
            assert by_name[name]["sector"] == "יהודי", f"{name} should be יהודי"
            assert by_name[name]["is_arab"] is False

#!/usr/bin/env python3
"""Shared normalization, CBS sector classification, and alias helpers."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

# Hebrew niqqud / cantillation marks (intentionally excluding U+05BE maqaf ־)
_NIQQUD_RE = re.compile(r"[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]")

# Road / neighborhood prefixes stripped from street names (longest first)
_ROAD_PREFIX_RE = re.compile(
    r"^(?:"
    r"רחוב|רח[׳.]?|"
    r"שדרות|שדרת|שד[׳.]?|"
    r"דרך|"
    r"סמטת|סמטה|סמ[׳.]?|"
    r"כיכר|ככר|רחבת|"
    r"מעלה|מעלות|משעול|מבוא|נתיב|מעגל|שביל|טיילת|"
    r"שכונת|שכונה|שכ[׳.]?|"
    r"ש"  # plain "ש " (neighborhood abbreviation); intentionally NOT "ש." (e.g. "ש. שלום")
    r")\s+"
)

_NUMERIC_STREET_RE = re.compile(
    r"^(?:(?:רחוב|רח[׳.]?|שדרות|שדרת|שד[׳.]?|דרך|כביש|"
    r"סמטת|סמטה|סמ[׳.]?|שכונת|שכונה|שכ[׳.]?|ש)\s+)*\d+\s*$"
)

# Two-word pairs that must NOT be auto-merged by reversing word order
_NO_REVERSE_PAIRS: frozenset[frozenset[str]] = frozenset({
    frozenset({"שניאור", "זלמן"}),  # Rabbi Shneur Zalman vs poet Zalman Shneur
    frozenset({"גן", "בית"}),
})

# First/second tokens indicating geographic/construct phrases rather than "First Last" person names
_NON_PERSON_CONSTRUCT_WORDS: frozenset[str] = frozenset({
    "בית", "גן", "כפר", "עין", "תל", "נחל", "הר", "קרית", "קריית",
    "נווה", "נוה", "רמת", "גבעת", "שער", "באר", "אל", "אבו", "אם", "בני",
    "עמק", "חוף", "יער", "אזור", "מרכז", "פארק", "מתחם", "מבצע", "חטיבת",
})

_TITLE_PREFIX_WORDS: frozenset[str] = frozenset({"הרב", "רבי", "רב", "רבן", "רבנו", "הנשיא", "הברון"})
_TITLE_SUFFIX_WORDS: frozenset[str] = frozenset({"המלך", "המלכה", "הנביא", "הנביאה", "המשוררת", "הכהן", "הגדול", "גאון"})

# Population Authority sentinel street code for "no street name / locality itself"
PLACEHOLDER_STREET_CODE = 9000


def unify_punctuation(text: str) -> str:
    """Strip Hebrew niqqud, unify geresh/gershayim, replace dashes with spaces."""
    s = _NIQQUD_RE.sub("", text.strip())
    for ch in ("'", "`", "’", "‘", "´", "׳"):
        s = s.replace(ch, "׳")
    for ch in ('"', "”", "“", "״"):
        s = s.replace(ch, "״")
    for dash in ("-", "–", "—", "־", "−"):
        s = s.replace(dash, " ")
    return re.sub(r"\s+", " ", s).strip()


def is_placeholder_street(street_code: Any) -> bool:
    """Return True for Population Authority sentinel street code 9000 (locality name itself)."""
    try:
        return int(float(street_code)) == PLACEHOLDER_STREET_CODE
    except (TypeError, ValueError):
        return False


def is_numeric(name: Any) -> bool:
    """Return True if street name is purely numeric (optionally preceded by a road prefix)."""
    if not isinstance(name, str):
        return True
    s = unify_punctuation(name)
    if not s:
        return True
    return bool(_NUMERIC_STREET_RE.match(s))


def structural_clean(name: Any) -> str:
    """Safe structural cleanup — no automatic definite-article (ה) stripping."""
    if not isinstance(name, str):
        return ""
    s = unify_punctuation(name)
    if not s:
        return ""

    # Strip road/neighborhood prefix (up to twice for nested forms like "רח רח 270")
    for _ in range(2):
        m = _ROAD_PREFIX_RE.match(s)
        if m and len(s) > m.end():
            s = s[m.end():].strip()
        else:
            break

    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*\(.*\)\s*$", "", s).strip()
    s = re.sub(r"\s+סמט(?:ה|ת)?\s*\d+\s*$", "", s).strip()
    s = re.sub(r"\s+סמ[׳.]?\s*\d+\s*$", "", s).strip()
    s = re.sub(r"\s+סמ\d+\s*$", "", s).strip()
    s = re.sub(r"\s+\d{1,3}\s*$", "", s).strip()

    # Drop empty, pure digits, or single-letter leftovers (e.g. "רח א", "רח ו")
    if not s or re.fullmatch(r"\d+", s) or len(s) < 2:
        return ""
    return s


def load_aliases(path: Path) -> dict[str, str]:
    """Load variant -> preferred alias map, normalized to match structural_clean()."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for k, v in raw.items():
        if str(k).startswith("_"):
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        ck = unify_punctuation(str(k))
        cv = unify_punctuation(v)
        if ck and cv:
            out[ck] = cv
    return out


def _choose_canonical_pair(a: str, ca: int, b: str, cb: int, aliases: dict[str, str]) -> str:
    """Choose canonical display form for a reversed two-word pair (a, b)."""
    if a in aliases and aliases[a] != a:
        return aliases[a]
    if b in aliases and aliases[b] != b:
        return aliases[b]
    pa, pb = a.split(), b.split()
    # Prefer natural Hebrew title order: "הרב X", "X המלך", "X הנביא", "X המלכה"
    a_natural = pa[0] in _TITLE_PREFIX_WORDS or pa[1] in _TITLE_SUFFIX_WORDS
    b_natural = pb[0] in _TITLE_PREFIX_WORDS or pb[1] in _TITLE_SUFFIX_WORDS
    if a_natural and not b_natural:
        return a
    if b_natural and not a_natural:
        return b
    if ca != cb:
        return a if ca > cb else b
    return min(a, b)


def build_reversed_pair_map(
    counts: Counter[str],
    aliases: dict[str, str] | None = None,
) -> dict[str, str]:
    """Detect two-word 'First Last' <-> 'Last First' pairs present in the dataset."""
    aliases = aliases or {}
    seen: set[tuple[str, str]] = set()
    mapping: dict[str, str] = {}
    for name, cnt in counts.items():
        parts = name.split()
        if len(parts) != 2 or parts[0] == parts[1]:
            continue
        if parts[0] in _NON_PERSON_CONSTRUCT_WORDS or parts[1] in _NON_PERSON_CONSTRUCT_WORDS:
            continue
        if frozenset(parts) in _NO_REVERSE_PAIRS:
            continue
        rev = f"{parts[1]} {parts[0]}"
        if rev not in counts:
            continue
        key = (min(name, rev), max(name, rev))
        if key in seen:
            continue
        seen.add(key)
        canon = _choose_canonical_pair(name, cnt, rev, counts[rev], aliases)
        if name != canon:
            mapping[name] = canon
        if rev != canon:
            mapping[rev] = canon
    return mapping


def classify_sector(
    religion_code: Any,
    arab_pop: Any = None,
    total_israelis: Any = None,
) -> tuple[str, float | None]:
    """Classify locality sector using official CBS 'דת יישוב' and demographic counts.

    CBS 'דת יישוב' codes:
      1 -> יהודי (Jewish)
      2 -> ערבי (Non-Jewish: Arab, Druze, Circassian per CBS)
      4 -> מעורב (Mixed Jewish-Arab city)

    Returns (sector_label, arab_pct_or_None).
    """
    arab_pct: float | None = None
    try:
        tot = float(total_israelis)
        if tot > 0:
            ar = 0.0 if arab_pop is None or (isinstance(arab_pop, float) and arab_pop != arab_pop) else float(arab_pop)
            arab_pct = round(max(0.0, min(100.0, ar / tot * 100.0)), 1)
    except (TypeError, ValueError):
        arab_pct = None

    rel: int | None = None
    try:
        if religion_code is not None and not (isinstance(religion_code, float) and religion_code != religion_code):
            rel = int(float(religion_code))
    except (TypeError, ValueError):
        rel = None

    if rel == 1:
        return "יהודי", arab_pct
    if rel == 2:
        return "ערבי", arab_pct
    if rel == 4:
        return "מעורב", arab_pct

    # Fallback purely from CBS demographic percentage if religion code is missing
    if arab_pct is not None:
        if arab_pct >= 50.0:
            return "ערבי", arab_pct
        if arab_pct >= 10.0:
            return "מעורב", arab_pct
        return "יהודי", arab_pct

    return "אחר", None

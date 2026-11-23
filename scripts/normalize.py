#!/usr/bin/env python3
"""Shared normalization, CBS sector classification, and alias helpers."""
from __future__ import annotations

import json
import math
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


def norm_idf_weight(n_places: Any, corpus_localities: int) -> float:
    """Normalized IDF weight for a street name, in the closed interval [0, 1].

    Formula: log(N / n) / log(N), where:
      N = corpus_localities — number of distinct localities in the cleaned corpus
      n = n_places — number of localities in which the name appears

    A name unique to a single locality (n = 1) scores 1.0 (100%); a name present
    in every locality (n = N) scores 0.0. Base of the logarithm is irrelevant.
    """
    try:
        n = int(n_places)
    except (TypeError, ValueError):
        return 1.0
    N = int(corpus_localities)
    if N <= 1 or n <= 0:
        return 1.0
    n = min(max(n, 1), N)
    return math.log(N / n) / math.log(N)


def itm_to_wgs84(coord_value: Any) -> tuple[float, float] | None:
    """Convert CBS 12-digit Israel Transverse Mercator (EPSG:2039) coordinate to WGS84 (lat, lon).

    CBS encodes ITM coordinates as a 12-digit integer: 6-digit Easting + 6-digit Northing (meters).
    """
    if coord_value is None:
        return None
    try:
        if isinstance(coord_value, float) and math.isnan(coord_value):
            return None
        s = str(int(round(float(coord_value)))).strip()
    except (TypeError, ValueError):
        return None

    if len(s) != 12 or not s.isdigit():
        return None

    east = float(s[:6])
    north = float(s[6:])

    # EPSG:2039 (Israel 1993 / Israeli TM Grid) on GRS80
    a = 6378137.0
    f = 1.0 / 298.257222101
    e2 = 2.0 * f - f * f
    e4 = e2 * e2
    e6 = e4 * e2
    ep2 = e2 / (1.0 - e2)

    lat0 = math.radians(31.73439361111111)
    lon0 = math.radians(35.20451694444444)
    k0 = 1.0000067
    fe = 219529.584
    fn = 626907.390

    c0 = 1.0 - e2 / 4.0 - 3.0 * e4 / 64.0 - 5.0 * e6 / 256.0
    c2 = 3.0 * e2 / 8.0 + 3.0 * e4 / 32.0 + 45.0 * e6 / 1024.0
    c4 = 15.0 * e4 / 256.0 + 45.0 * e6 / 1024.0
    c6 = 35.0 * e6 / 3072.0

    m0 = a * (c0 * lat0 - c2 * math.sin(2.0 * lat0) + c4 * math.sin(4.0 * lat0) - c6 * math.sin(6.0 * lat0))
    m = m0 + (north - fn) / k0
    mu = m / (a * c0)

    e1 = (1.0 - math.sqrt(1.0 - e2)) / (1.0 + math.sqrt(1.0 - e2))
    phi1 = (
        mu
        + (3.0 * e1 / 2.0 - 27.0 * (e1 ** 3) / 32.0) * math.sin(2.0 * mu)
        + (21.0 * (e1 ** 2) / 16.0 - 55.0 * (e1 ** 4) / 32.0) * math.sin(4.0 * mu)
        + (151.0 * (e1 ** 3) / 96.0) * math.sin(6.0 * mu)
        + (1097.0 * (e1 ** 4) / 512.0) * math.sin(8.0 * mu)
    )

    sin_p1 = math.sin(phi1)
    cos_p1 = math.cos(phi1)
    tan_p1 = math.tan(phi1)
    n1 = a / math.sqrt(1.0 - e2 * sin_p1 * sin_p1)
    r1 = a * (1.0 - e2) / ((1.0 - e2 * sin_p1 * sin_p1) ** 1.5)
    t1 = tan_p1 * tan_p1
    c1 = ep2 * cos_p1 * cos_p1
    d = (east - fe) / (n1 * k0)

    lat_itm = phi1 - (n1 * tan_p1 / r1) * (
        d * d / 2.0
        - (5.0 + 3.0 * t1 + 10.0 * c1 - 4.0 * c1 * c1 - 9.0 * ep2) * (d ** 4) / 24.0
        + (61.0 + 90.0 * t1 + 298.0 * c1 + 45.0 * t1 * t1 - 252.0 * ep2 - 3.0 * c1 * c1) * (d ** 6) / 720.0
    )
    lon_itm = lon0 + (
        d
        - (1.0 + 2.0 * t1 + c1) * (d ** 3) / 6.0
        + (5.0 - 2.0 * c1 + 28.0 * t1 - 3.0 * c1 * c1 + 8.0 * ep2 + 24.0 * t1 * t1) * (d ** 5) / 120.0
    ) / cos_p1

    # 3-parameter geocentric shift Israel 1993 -> WGS84
    sin_lat, cos_lat = math.sin(lat_itm), math.cos(lat_itm)
    sin_lon, cos_lon = math.sin(lon_itm), math.cos(lon_itm)
    v = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    x = v * cos_lat * cos_lon - 24.0024
    y = v * cos_lat * sin_lon - 17.1032
    z = v * (1.0 - e2) * sin_lat - 17.8444

    # WGS84 inverse ECEF -> geodetic
    a_w = 6378137.0
    f_w = 1.0 / 298.257223563
    e2_w = 2.0 * f_w - f_w * f_w
    p = math.hypot(x, y)
    lon_w = math.atan2(y, x)
    lat_w = math.atan2(z, p * (1.0 - e2_w))
    for _ in range(5):
        v_w = a_w / math.sqrt(1.0 - e2_w * math.sin(lat_w) ** 2)
        lat_w = math.atan2(z + e2_w * v_w * math.sin(lat_w), p)

    lat_deg = round(math.degrees(lat_w), 5)
    lon_deg = round(math.degrees(lon_w), 5)
    if not (29.0 <= lat_deg <= 33.6 and 34.0 <= lon_deg <= 36.0):
        return None
    return lat_deg, lon_deg

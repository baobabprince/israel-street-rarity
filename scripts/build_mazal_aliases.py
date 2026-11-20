#!/usr/bin/env python3
"""
Build data/mazal_aliases.json by resolving structurally-cleaned street names
against Wikidata entities that have a National Library of Israel J9U ID (P8189),
i.e. linked to מז"ל / Mazal authority records, with strict anti-false-merge guards.

Usage:
  python scripts/build_mazal_aliases.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

try:
    import pandas as pd
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "pandas", "-q"])
    import pandas as pd
    import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from normalize import is_numeric, is_placeholder_street, structural_clean, unify_punctuation  # noqa: E402

OUT = SCRIPT_DIR.parent
if not (OUT / "index.html").exists():
    OUT = Path(".")

STREETS_ID = "9ad3862c-8391-4b2f-84a4-2d4c68625f4b"
API = "https://data.gov.il/api/3/action/datastore_search"
SPARQL = "https://query.wikidata.org/sparql"
UA = {
    "User-Agent": "israel-street-rarity/2.0 (https://github.com/baobabprince/israel-street-rarity; research)",
    "Accept": "application/sparql-results+json",
}
BATCH = 40
SLEEP = 1.2

# Single-word common Hebrew words, plants, nature terms, first names, or ambiguous surnames
# that must NEVER be auto-merged via Wikidata altLabel lookup.
BLOCKED_KEYS: frozenset[str] = frozenset({
    "אביאל", "אדיר", "אהבת ישראל", "אור הגנוז", "אזולאי", "אילאיל", "אילן",
    "אלי מויאל", "אליעזר", "אלעזר", "אם כל חי", "ארם", "אשוח", "בלשן",
    "בן אשר", "בן יוסף", "בת עין", "גולד", "גור אריה", "דוד כהן", "דורות ראשונים",
    "דיכטר", "דינה", "דניאל", "דסקל", "דקלה", "הגל", "הטייס", "המהנדס",
    "המפל", "המשביר", "הנוקם", "הנזיר", "הסולם", "העופרים", "העורב", "הצבר",
    "הר שלום", "זית רענן", "חבצלת השרון", "חיי עולם", "חיים", "חנניה",
    "יאיר", "יהושפט", "יואב", "יובל", "יוסף", "יעל", "יעקב", "ירמיהו",
    "לוי", "לימור", "מנדלסון", "מנגר", "מנחם", "מנצור", "נועה", "נחלת בנימין",
    "נחמני", "נטע", "ניסים", "נתן", "עוז", "עזמות", "עמנואל שפר", "עפר", "עופר",
    "ערוגות הבושם", "פארן", "פרי חדש", "פרי מגדים", "פרנקו", "צבי", "צבר",
    "צל הרים", "צמחי היהודים", "רב כהנא", "רוז", "רחל", "רייך", "רמי",
    "שאול", "שבח", "שלומית", "שמואל ירחינאי", "שמעון מזרחי", "שני", "שער המלך",
    "תפארת ישראל", "אבקת רוכל", "אהוד מנור", "אודי מנור",
})

# High-confidence curated overrides for prominent Israeli street figures
CURATED_OVERRIDES: dict[str, str] = {
    "הרצל": "בנימין זאב הרצל",
    "בן גוריון": "דוד בן גוריון",
    "דוד בן גוריון": "דוד בן גוריון",
    "בן גוריון דוד": "דוד בן גוריון",
    "ז׳בוטינסקי": "זאב ז׳בוטינסקי",
    "ז׳בוטינסקי זאב": "זאב ז׳בוטינסקי",
    "זאב ז׳בוטינסקי": "זאב ז׳בוטינסקי",
    "ביאליק": "חיים נחמן ביאליק",
    "חיים נחמן ביאליק": "חיים נחמן ביאליק",
    "טרומפלדור": "יוסף טרומפלדור",
    "יוסף טרומפלדור": "יוסף טרומפלדור",
    "ויצמן": "חיים ויצמן",
    "וייצמן": "חיים ויצמן",
    "חיים ויצמן": "חיים ויצמן",
    "חיים וייצמן": "חיים ויצמן",
    "ויצמן חיים": "חיים ויצמן",
    "וייצמן חיים": "חיים ויצמן",
    "הנשיא ויצמן": "חיים ויצמן",
    "הנשיא וייצמן": "חיים ויצמן",
    "עזר ויצמן": "עזר ויצמן",
    "עזר וייצמן": "עזר ויצמן",
    "בגין": "מנחם בגין",
    "מנחם בגין": "מנחם בגין",
    "בגין מנחם": "מנחם בגין",
    "רבין": "יצחק רבין",
    "יצחק רבין": "יצחק רבין",
    "רבין יצחק": "יצחק רבין",
    "בן צבי": "יצחק בן צבי",
    "יצחק בן צבי": "יצחק בן צבי",
    "בן צבי יצחק": "יצחק בן צבי",
    "בן יהודה": "אליעזר בן יהודה",
    "אליעזר בן יהודה": "אליעזר בן יהודה",
    "בן יהודה אליעזר": "אליעזר בן יהודה",
    "כצנלסון": "ברל כצנלסון",
    "ברל כצנלסון": "ברל כצנלסון",
    "כצנלסון ברל": "ברל כצנלסון",
    "עגנון": "שמואל יוסף עגנון",
    "ש״י עגנון": "שמואל יוסף עגנון",
    "עגנון ש״י": "שמואל יוסף עגנון",
    "שמואל יוסף עגנון": "שמואל יוסף עגנון",
    "פינסקר": "יהודה לייב פינסקר",
    "יהודה לייב פינסקר": "יהודה לייב פינסקר",
    "נורדאו": "מקס נורדאו",
    "נורדאו מקס": "מקס נורדאו",
    "מקס נורדאו": "מקס נורדאו",
    "אוסישקין": "מנחם אוסישקין",
    "אוסישקין מנחם": "מנחם אוסישקין",
    "מנחם אוסישקין": "מנחם אוסישקין",
    "רוטשילד": "הברון רוטשילד",
    "הברון רוטשילד": "הברון רוטשילד",
    "בנימין אדמונד ג׳יימס דה רוטשילד": "הברון רוטשילד",
    "עקיבא": "רבי עקיבא",
    "רבי עקיבא": "רבי עקיבא",
    "רבי עקיבא בן יוסף": "רבי עקיבא",
    "הרדב״ז": "רדב״ז",
    "רדב״ז": "רדב״ז",
    "רלב״ח": "לוי אבן חביב",
    # Well-known verified nicknames / spelling variants
    "דדו": "דוד אלעזר",
    "דוד אלעזר": "דוד אלעזר",
    "רפול": "רפאל איתן",
    "רפאל איתן": "רפאל איתן",
    "אנטק": "יצחק צוקרמן",
    "יצחק צוקרמן": "יצחק צוקרמן",
    "מוטה גור": "מרדכי גור",
    "מרדכי גור": "מרדכי גור",
    "יוני נתניהו": "יונתן נתניהו",
    "יונתן נתניהו": "יונתן נתניהו",
    "אריק שרון": "אריאל שרון",
    "אריאל שרון": "אריאל שרון",
    "אנילביץ": "מרדכי אנילביץ׳",
    "מרדכי אנילביץ׳": "מרדכי אנילביץ׳",
    "שינקין": "מנחם שינקין",
    "שיינקין": "מנחם שינקין",
    "מנחם שינקין": "מנחם שינקין",
    "לילינבלום": "משה לייב ליליינבלום",
    "משה לייב ליליינבלום": "משה לייב ליליינבלום",
    "מנדלי": "מנדלי מוכר ספרים",
    "מנדלי מוכר ספרים": "מנדלי מוכר ספרים",
    "גלילאו": "גלילאו גליליי",
    "גלילאו גליליי": "גלילאו גליליי",
    "נפוליאון": "נפוליאון בונפרטה",
    "נפוליאון בונפרטה": "נפוליאון בונפרטה",
    "גאנדי": "מהאטמה גנדי",
    "מהאטמה גנדי": "מהאטמה גנדי",
    "פיקסו": "פבלו פיקאסו",
    "פבלו פיקאסו": "פבלו פיקאסו",
    "גטה": "יוהאן וולפגנג פון גתה",
    "יוהאן וולפגנג פון גתה": "יוהאן וולפגנג פון גתה",
    "רמבם": "רמב״ם",
    "הרמב״ם": "רמב״ם",
    "רמב״ם": "רמב״ם",
    "אלראזי": "אל ראזי",
    "אל ראזי": "אל ראזי",
    "אלבירוני": "אל בירוני",
    "אל בירוני": "אל בירוני",
    "אלפאראבי": "אל פאראבי",
    "אלפראבי": "אל פאראבי",
    "אל פאראבי": "אל פאראבי",
    "הזז": "חיים הזז",
    "חיים הזז": "חיים הזז",
}

# Reject NLI catalog-style labels like "להט, ציון בן רצון, 1947-"
_CATALOG_LABEL_RE = re.compile(r",|\d{4}")


def is_safe_alias(variant: str, preferred: str) -> bool:
    """Validate a candidate Wikidata alias mapping before accepting it."""
    v = unify_punctuation(variant)
    p = unify_punctuation(preferred)
    if not v or not p or v in BLOCKED_KEYS or p in BLOCKED_KEYS:
        return False
    if _CATALOG_LABEL_RE.search(p):
        return False
    if v == p:
        return True
    v_tokens = v.split()
    p_tokens = p.split()
    # For single-word variants, only allow if it's an acronym (contains ״) or matches the surname/last token
    if len(v_tokens) == 1:
        if "״" in v:
            return True
        if v == p_tokens[-1] and len(v) >= 4:
            return True
        return False
    return True


def fetch_all(rid: str, limit: int = 32000) -> list[dict]:
    recs: list[dict] = []
    offset = 0
    while True:
        r = requests.get(API, params={"resource_id": rid, "limit": limit, "offset": offset}, timeout=120)
        r.raise_for_status()
        batch = r.json()["result"]["records"]
        if not batch:
            break
        recs.extend(batch)
        offset += len(batch)
        print(f"  streets {len(recs)}", flush=True)
        if len(batch) < limit:
            break
    return recs


def sparql(query: str) -> list[dict]:
    for attempt in range(5):
        try:
            r = requests.get(SPARQL, params={"query": query, "format": "json"}, headers=UA, timeout=180)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["results"]["bindings"]
        except Exception as e:
            print(f"  SPARQL retry {attempt + 1}: {e}", flush=True)
            time.sleep(3 * (attempt + 1))
    return []


def escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def resolve_batch(names: list[str]) -> dict[str, str]:
    """Map street form -> preferred Hebrew label for UNAMBIGUOUS J9U-linked humans."""
    if not names:
        return {}
    values = " ".join(f'"{escape_literal(n)}"@he' for n in names)
    q = f"""
    SELECT ?q ?pref ?qid WHERE {{
      VALUES ?q {{ {values} }}
      ?item wdt:P8189 ?j9u; wdt:P31 wd:Q5.
      {{ ?item rdfs:label ?q }} UNION {{ ?item skos:altLabel ?q }}
      ?item rdfs:label ?pref. FILTER(LANG(?pref) = "he")
      BIND(STRAFTER(STR(?item), "entity/") AS ?qid)
    }}
    """
    rows = sparql(q)
    hits: dict[str, dict[str, str]] = defaultdict(dict)
    for b in rows:
        qv = unify_punctuation(b["q"]["value"])
        pref = unify_punctuation(b["pref"]["value"])
        qid = b.get("qid", {}).get("value", "")
        if qv and pref and qid:
            hits[qv][qid] = pref

    out: dict[str, str] = {}
    for name, qid_map in hits.items():
        # Skip ambiguous names matching multiple distinct Wikidata humans
        if len(qid_map) != 1:
            continue
        pref = next(iter(qid_map.values()))
        if is_safe_alias(name, pref):
            out[name] = pref
    return out


def main() -> None:
    print("Fetching street names…")
    raw = pd.DataFrame(fetch_all(STREETS_ID))
    raw = raw[~raw["סמל_רחוב"].apply(is_placeholder_street)]
    names = (
        raw.loc[~raw["שם_רחוב"].apply(is_numeric), "שם_רחוב"]
        .map(structural_clean)
        .loc[lambda s: s.str.len() >= 2]
        .unique()
        .tolist()
    )
    print(f"Unique structural names: {len(names)}")

    resolved: dict[str, str] = {}
    for i in range(0, len(names), BATCH):
        batch = names[i : i + BATCH]
        print(f"SPARQL batch {i // BATCH + 1}/{(len(names) + BATCH - 1) // BATCH} ({len(batch)} names)…", flush=True)
        resolved.update(resolve_batch(batch))
        print(f"  hits so far: {len(resolved)}", flush=True)
        time.sleep(SLEEP)

    # Apply curated overrides on top of Wikidata resolution
    for k, v in CURATED_OVERRIDES.items():
        resolved[unify_punctuation(k)] = unify_punctuation(v)

    by_pref: dict[str, set[str]] = defaultdict(set)
    for v, p in resolved.items():
        if v in BLOCKED_KEYS and v not in CURATED_OVERRIDES:
            continue
        by_pref[p].add(v)
        by_pref[p].add(p)

    final: dict[str, str] = {
        "_comment": 'Auto-built from Wikidata entities with NLI J9U (P8189≈מז"ל) + curated overrides. Punctuation-normalized variant -> preferred Hebrew form.',
        "_source": "wikidata J9U P8189 humans (unambiguous + curated)",
        "_generated": time.strftime("%Y-%m-%d"),
    }
    for pref, variants in sorted(by_pref.items()):
        if len(variants) <= 1:
            continue
        for v in sorted(variants):
            final[v] = pref

    path = OUT / "data" / "mazal_aliases.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n_maps = sum(1 for k in final if not k.startswith("_"))
    print(f"Wrote {path} with {n_maps} alias entries")


if __name__ == "__main__":
    main()

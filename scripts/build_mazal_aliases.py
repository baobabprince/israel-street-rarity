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

from normalize import is_numeric, is_placeholder_street, load_aliases, structural_clean, unify_punctuation  # noqa: E402

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
    "משה מונטיפיורי": "משה מונטיפיורי",
    "משה מונטפיורי": "משה מונטיפיורי",
    "משה חיים מונטיפיורי": "משה מונטיפיורי",
    "השר משה מונטיפיורי": "משה מונטיפיורי",
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
    raise RuntimeError("Wikidata SPARQL request failed after retries; no aliases written")


def escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def resolve_batch(names: list[str]) -> list[dict]:
    """Retrieve all exact Hebrew label/altLabel matches, including their J9U IDs."""
    if not names:
        return []
    values = " ".join(f'"{escape_literal(n)}"@he' for n in names)
    q = f"""
    SELECT ?q ?pref ?qid ?j9u WHERE {{
      VALUES ?q {{ {values} }}
      ?item wdt:P8189 ?j9u; wdt:P31 wd:Q5.
      {{ ?item rdfs:label ?q }} UNION {{ ?item skos:altLabel ?q }}
      ?item rdfs:label ?pref. FILTER(LANG(?pref) = "he")
      BIND(STRAFTER(STR(?item), "entity/") AS ?qid)
    }}
    """
    return sparql(q)


def build_aliases(names: list[str], rows: list[dict], existing: dict[str, str]):
    """Add conservative, J9U-backed groups without changing audited mappings.

    Only exact matches to one Wikidata human AND one J9U are eligible. A name
    is not necessarily a person in a street context; exclude single words, and
    only add groups containing multiple observed street forms. The original
    reviewed map always wins, including over a conflicting preferred label.
    """
    names = set(names)
    hits = defaultdict(set)
    labels = defaultdict(set)
    for row in rows:
        try:
            name = unify_punctuation(row["q"]["value"])
            pref = unify_punctuation(row["pref"]["value"])
            qid, j9u = row["qid"]["value"], row["j9u"]["value"]
        except (KeyError, TypeError):
            raise ValueError("Unexpected Wikidata response; no aliases written") from None
        if name not in names or not re.fullmatch(r"Q[1-9][0-9]*", qid) or not re.fullmatch(r"[0-9]+", j9u):
            continue
        hits[name].add((qid, j9u))
        labels[(qid, j9u)].add(pref)

    # A QID with several J9Us (or vice versa) is not a safe identity.
    qids, ids = defaultdict(set), defaultdict(set)
    for pairs in hits.values():
        for qid, j9u in pairs:
            qids[qid].add(j9u)
            ids[j9u].add(qid)
    groups = defaultdict(set)
    for name, pairs in hits.items():
        if len(pairs) != 1 or len(name.split()) < 2 or name in BLOCKED_KEYS:
            continue
        qid, j9u = next(iter(pairs))
        if len(qids[qid]) == len(ids[j9u]) == 1:
            groups[(qid, j9u)].add(name)

    # Different identities may happen to have the same Hebrew preferred label.
    label_owners = defaultdict(set)
    for identity, prefs in labels.items():
        for pref in prefs:
            label_owners[pref].add(identity)

    result = dict(existing)
    provenance = []
    for (qid, j9u), variants in sorted(groups.items()):
        prefs = labels[(qid, j9u)]
        if len(variants) < 2 or len(prefs) != 1:
            continue
        pref = next(iter(prefs))
        if (len(label_owners[pref]) != 1 or not pref or pref in BLOCKED_KEYS
                or _CATALOG_LABEL_RE.search(pref) or structural_clean(pref) != pref):
            continue
        # A preferred label in the corpus must itself be a unique match.
        if pref in names and hits.get(pref) != {(qid, j9u)}:
            continue
        if any(not is_safe_alias(v, pref) for v in variants):
            continue
        # Never overwrite an audited mapping or absorb an audited identity.
        anchors = {existing[v] for v in variants if v in existing}
        if pref in existing:
            anchors.add(existing[pref])
        if len(anchors) > 1:
            continue
        if not anchors and pref in existing.values():
            continue
        canonical = next(iter(anchors), pref)
        # Avoid redirecting a preferred name belonging to a different reviewed group.
        if canonical in existing and existing[canonical] != canonical:
            continue
        added = sorted(v for v in variants if v not in existing and v != canonical)
        if not added:
            continue
        for v in added:
            result[v] = canonical
        result.setdefault(canonical, canonical)
        provenance.append({"j9u": j9u, "qid": qid, "preferred": pref,
                           "canonical": canonical, "added": added})
    return result, provenance


def main() -> None:
    print("Fetching street names…")
    raw = pd.DataFrame(fetch_all(STREETS_ID))
    raw = raw[~raw["סמל_רחוב"].apply(is_placeholder_street)]
    names = sorted(
        raw.loc[~raw["שם_רחוב"].apply(is_numeric), "שם_רחוב"]
        .map(structural_clean).loc[lambda s: s.str.len() >= 2].unique().tolist()
    )
    print(f"Unique structural names: {len(names)}")
    rows = []
    for i in range(0, len(names), BATCH):
        print(f"SPARQL batch {i // BATCH + 1}/{(len(names) + BATCH - 1) // BATCH}…", flush=True)
        rows.extend(resolve_batch(names[i:i + BATCH]))
        time.sleep(SLEEP)

    path = OUT / "data" / "mazal_aliases.json"
    if not path.exists():
        raise FileNotFoundError("Reviewed alias map is missing; refusing to replace it")
    existing = load_aliases(path)
    # Curated overrides are explicit, never inferred from the Wikidata lookup.
    for key, value in CURATED_OVERRIDES.items():
        key, value = unify_punctuation(key), unify_punctuation(value)
        if key in existing and existing[key] != value:
            raise ValueError(f"Curated override conflicts with reviewed alias: {key}")
        existing[key] = value
    result, provenance = build_aliases(names, rows, existing)
    final = {
        "_comment": "Reviewed aliases plus conservative exact Wikidata matches with J9U IDs; see mazal_aliases_provenance.json.",
        "_source": "Wikidata J9U P8189 humans + reviewed overrides (not the NLI API)",
        "_generated": time.strftime("%Y-%m-%d"),
        **dict(sorted(result.items())),
    }
    # Keep existing provenance so future builds don't lose evidence for older additions.
    provenance_path = OUT / "data" / "mazal_aliases_provenance.json"
    previous = json.loads(provenance_path.read_text(encoding="utf-8")) if provenance_path.exists() else []
    provenance_path.write_text(json.dumps(previous + provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(result)} aliases; {sum(len(p['added']) for p in provenance)} new J9U-backed variants")


if __name__ == "__main__":
    main()

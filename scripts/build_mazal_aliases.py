#!/usr/bin/env python3
"""
Build data/mazal_aliases.json by resolving structurally-cleaned street names
against Wikidata entities that have a National Library of Israel J9U ID (P8189),
i.e. linked to מז\"ל / Mazal authority records.

One-time / occasional job — can take a while (batched SPARQL).
Usage:
  python scripts/build_mazal_aliases.py
"""
from __future__ import annotations
import json, re, sys, time
from pathlib import Path
from collections import defaultdict

try:
    import requests, pandas as pd
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "pandas", "-q"])
    import requests, pandas as pd

OUT = Path(__file__).resolve().parent.parent
if not (OUT / "index.html").exists():
    OUT = Path(".")
STREETS_ID = "9ad3862c-8391-4b2f-84a4-2d4c68625f4b"
API = "https://data.gov.il/api/3/action/datastore_search"
SPARQL = "https://query.wikidata.org/sparql"
UA = {"User-Agent": "israel-street-rarity/1.0 (https://github.com/baobabprince/israel-street-rarity; research)", "Accept": "application/sparql-results+json"}
BATCH = 40
SLEEP = 1.2

def fetch_all(rid, limit=32000):
    recs, offset = [], 0
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

def is_numeric(name):
    if not isinstance(name, str):
        return True
    s = name.strip()
    return bool(re.match(r"^(רח|רח'|רח\.|רחוב)?\s*\d+\s*$", s) or re.match(r"^\d+\s*$", s))

def structural_clean(name):
    if not isinstance(name, str):
        return ""
    s = name.strip()
    s = s.replace("'", "׳").replace("`", "׳").replace("'", "׳")
    for dash in ("-", "–", "—", "־", "−"):
        s = s.replace(dash, " ")
    s = re.sub(r"\s+", " ", s).strip()
    for p in [
        r"^רחוב\s+", r"^רח׳\s+", r"^רח\.\s+", r"^רח\s+",
        r"^שדרות\s+", r"^שד׳\s+", r"^שד\.\s+", r"^שד\s+",
        r"^דרך\s+", r"^סמטת\s+", r"^סמטה\s+", r"^כיכר\s+", r"^ככר\s+",
        r"^מעלה\s+", r"^שביל\s+", r"^טיילת\s+",
        r"^שכונת\s+", r"^שכונה\s+", r"^שכ׳\s+", r"^שכ\.\s+", r"^שכ\s+",
    ]:
        s = re.sub(p, "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*\(.*\)\s*$", "", s).strip()
    s = re.sub(r"\s+סמט(?:ה|ת)?\s*\d+\s*$", "", s).strip()
    s = re.sub(r"\s+סמ׳?\s*\d+\s*$", "", s).strip()
    s = re.sub(r"\s+סמ\d+\s*$", "", s).strip()
    s = re.sub(r"\s+\d{1,3}\s*$", "", s).strip()
    if re.fullmatch(r"\d+", s):
        return ""
    return s

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
            print(f"  SPARQL retry {attempt+1}: {e}", flush=True)
            time.sleep(3 * (attempt + 1))
    return []

def escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

def resolve_batch(names: list[str]) -> dict[str, str]:
    """Map street form -> preferred Hebrew label for J9U-linked humans."""
    if not names:
        return {}
    values = " ".join(f'"{escape_literal(n)}"@he' for n in names)
    # Match label or altLabel; require J9U (P8189) and human (Q5)
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
    # street form -> list of (pref, qid)
    hits: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for b in rows:
        qv = b["q"]["value"]
        pref = b["pref"]["value"]
        qid = b.get("qid", {}).get("value", "")
        hits[qv].append((pref, qid))

    out = {}
    for name, opts in hits.items():
        # prefer the most common preferred label; break ties by shorter label
        counts: dict[str, int] = defaultdict(int)
        for pref, _ in opts:
            counts[pref] += 1
        best = sorted(counts.items(), key=lambda x: (-x[1], len(x[0])))[0][0]
        if best != name:
            out[name] = best
        else:
            # still record identity if other forms exist under same entity — handled globally later
            out[name] = best
    return out

def main():
    print("Fetching street names…")
    raw = pd.DataFrame(fetch_all(STREETS_ID))
    names = (
        raw.loc[~raw["שם_רחוב"].apply(is_numeric), "שם_רחוב"]
        .map(structural_clean)
        .loc[lambda s: s.str.len() > 0]
        .unique()
        .tolist()
    )
    # Skip very short tokens (1 char) — noise
    names = [n for n in names if len(n) >= 2]
    print(f"Unique structural names: {len(names)}")

    # Only resolve names that look somewhat like person/place phrases:
    # at least one Hebrew letter, not pure common tree/flower single words optional — resolve all, Wikidata filters
    resolved: dict[str, str] = {}
    for i in range(0, len(names), BATCH):
        batch = names[i : i + BATCH]
        print(f"SPARQL batch {i // BATCH + 1}/{(len(names) + BATCH - 1) // BATCH} ({len(batch)} names)…", flush=True)
        part = resolve_batch(batch)
        resolved.update(part)
        print(f"  hits so far: {len(resolved)}", flush=True)
        time.sleep(SLEEP)

    # Collapse: all street forms that share the same preferred label map to it;
    # also map preferred -> preferred for stability
    aliases = {"_comment": "Auto-built from Wikidata entities with NLI J9U (P8189≈מז\"ל). variant -> preferred Hebrew label."}
    for variant, pref in resolved.items():
        aliases[variant] = pref
        aliases[pref] = pref

    # Drop identity-only entries that add nothing (optional keep for documentation)
    # Keep only cases where variant differs OR multiple variants share pref
    by_pref: dict[str, set[str]] = defaultdict(set)
    for v, p in list(aliases.items()):
        if v.startswith("_"):
            continue
        by_pref[p].add(v)
    final = {"_comment": aliases["_comment"], "_source": "wikidata J9U P8189 humans", "_generated": time.strftime("%Y-%m-%d")}
    for pref, variants in by_pref.items():
        if len(variants) == 1 and pref in variants:
            continue  # singleton, no merge value
        for v in variants:
            final[v] = pref

    path = OUT / "data" / "mazal_aliases.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    n_maps = sum(1 for k in final if not k.startswith("_"))
    print(f"Wrote {path} with {n_maps} alias entries ({len(by_pref)} identity clusters with merges)")

if __name__ == "__main__":
    main()

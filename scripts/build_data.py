#!/usr/bin/env python3
"""Fetch Israel streets + CBS demographics, normalize, compute rarity scores, write outputs."""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "pandas", "numpy", "-q"])
    import numpy as np
    import pandas as pd
    import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from normalize import (  # noqa: E402
    build_reversed_pair_map,
    classify_sector,
    is_numeric,
    is_placeholder_street,
    itm_to_wgs84,
    load_aliases,
    norm_idf_weight,
    structural_clean,
)

OUT = SCRIPT_DIR.parent
if not (OUT / "index.html").exists():
    OUT = Path(".")

STREETS_ID = "9ad3862c-8391-4b2f-84a4-2d4c68625f4b"
CBS_ID = "d47a54ff-87f0-44b3-b33a-f284c0c38e5a"
API = "https://data.gov.il/api/3/action/datastore_search"
INCLUDED_STATUS = {"עירייה", "מועצה מקומית"}
MIN_STREETS = 5
ALIASES_PATH = OUT / "data" / "mazal_aliases.json"


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
        print(f"  {len(recs)}", flush=True)
        if len(batch) < limit:
            break
    return recs


def kind_from_status(status) -> str:
    s = "" if status is None or (isinstance(status, float) and np.isnan(status)) else str(status).strip()
    if s == "עירייה":
        return "עיר"
    if s == "מועצה מקומית":
        return "מועצה מקומית"
    return "יישוב"


def pick_col(df: pd.DataFrame, *predicates) -> str | None:
    for pred in predicates:
        for c in df.columns:
            if pred(str(c)):
                return str(c)
    return None


def main() -> None:
    aliases = load_aliases(ALIASES_PATH)
    print(f"Loaded {len(aliases)} Mazal-style aliases")

    print("Fetching streets...")
    raw = pd.DataFrame(fetch_all(STREETS_ID))
    rows_raw = len(raw)
    unique_raw = int(raw["שם_רחוב"].nunique()) if "שם_רחוב" in raw.columns else 0

    raw["סמל_ישוב"] = pd.to_numeric(raw["סמל_ישוב"], errors="coerce")
    raw["שם_ישוב"] = raw["שם_ישוב"].astype(str).str.strip()

    # 1. Remove Population Authority placeholder rows (סמל_רחוב == 9000, locality name itself)
    placeholder_mask = raw["סמל_רחוב"].apply(is_placeholder_street)
    rows_placeholder_9000 = int(placeholder_mask.sum())
    no_placeholder = raw[~placeholder_mask].copy()

    # 2. Remove numeric-only street names
    after_num = no_placeholder[~no_placeholder["שם_רחוב"].apply(is_numeric)].copy()
    rows_after_numeric = len(after_num)
    unique_after_numeric = int(after_num["שם_רחוב"].nunique())

    # 3. Structural clean + alias resolution + two-word reversed-name canonicalization
    after_num["clean"] = after_num["שם_רחוב"].apply(structural_clean)
    df = after_num[after_num["clean"].str.len() > 0].copy()

    # Pass 1: exact alias lookup
    df["norm"] = df["clean"].map(lambda s: aliases.get(s, s))

    # Pass 2: merge two-word reversed person names ("בגין מנחם" <-> "מנחם בגין")
    reversed_map = build_reversed_pair_map(Counter(df["norm"]), aliases)
    print(f"Detected {len(reversed_map)} reversed two-word person-name variants")
    df["norm"] = df["norm"].map(lambda s: reversed_map.get(s, s))

    # Pass 3: second alias pass in case reversal unlocked an alias (e.g. "עגנון ש״י" -> "ש״י עגנון" -> "שמואל יוסף עגנון")
    df["norm"] = df["norm"].map(lambda s: aliases.get(s, s))

    rows_clean_before_dedup = len(df)

    # 4. Deduplicate (סמל_ישוב, norm) so duplicate raw variants inside the same city
    #    (e.g. "הגרנות" + "שכ הגרנות") are counted once per city!
    df_dedup = df.drop_duplicates(subset=["סמל_ישוב", "norm"], keep="first").copy()
    rows_dedup = len(df_dedup)
    rows_dups_removed = rows_clean_before_dedup - rows_dedup
    unique_norm = int(df_dedup["norm"].nunique())
    print(
        f"raw={rows_raw} placeholder_9000={rows_placeholder_9000} after_numeric={rows_after_numeric} "
        f"clean={rows_clean_before_dedup} dedup={rows_dedup} (removed {rows_dups_removed} intra-city dups) "
        f"unique_norm={unique_norm}"
    )

    # 5. Compute nationwide prevalence & rarity weights across ALL localities
    nloc = df_dedup.groupby("norm")["סמל_ישוב"].nunique()
    df_dedup["n"] = df_dedup["norm"].map(nloc)
    df_dedup["w_log"] = 1.0 / np.log2(1.0 + df_dedup["n"])
    df_dedup["w_sqrt"] = 1.0 / np.sqrt(df_dedup["n"].astype(float))
    # Normalized IDF: log(N / n) / log(N) ∈ [0,1], where N = localities in the corpus.
    # Unique name (n=1) -> 1.0; name present in every locality (n=N) -> 0.0.
    corpus_localities = int(df_dedup["סמל_ישוב"].nunique())
    if corpus_localities > 1:
        log_corpus = np.log(corpus_localities)
        df_dedup["w_idf"] = np.log(corpus_localities / df_dedup["n"].clip(lower=1).astype(float)) / log_corpus
        df_dedup["w_idf"] = df_dedup["w_idf"].clip(lower=0.0, upper=1.0)
    else:
        df_dedup["w_idf"] = 1.0
    print(f"Corpus localities (N for NORM IDF) = {corpus_localities}")

    pure_exclusive_names = int((nloc == 1).sum())

    places_by_norm = (
        df_dedup.groupby("norm")["שם_ישוב"]
        .apply(lambda s: sorted({str(x) for x in s if pd.notna(x)}))
        .to_dict()
    )

    # Top 25 most widespread street names in Israel
    top_shared_df = (
        df_dedup.groupby("norm")
        .agg(n=("סמל_ישוב", "nunique"))
        .sort_values("n", ascending=False)
        .head(25)
        .reset_index()
    )
    top_shared = [
        {
            "name": str(r["norm"]),
            "n_places": int(r["n"]),
            "weight_log": round(float(1.0 / np.log2(1 + int(r["n"]))), 3),
            "weight_sqrt": round(float(1.0 / np.sqrt(int(r["n"]))), 3),
            "weight_norm_idf": round(float(norm_idf_weight(int(r["n"]), corpus_localities)), 3),
        }
        for _, r in top_shared_df.iterrows()
    ]

    # Sample examples for the interactive showcase
    name_places = (
        df_dedup.groupby("norm")
        .agg(n=("סמל_ישוב", "nunique"), place=("שם_ישוב", "first"), raw=("שם_רחוב", "first"))
        .reset_index()
    )
    n_unique = int((name_places["n"] == 1).sum())
    n_mid = int(((name_places["n"] >= 2) & (name_places["n"] <= 15)).sum())
    n_common = int((name_places["n"] >= 50).sum())
    unique_pool = name_places[name_places["n"] == 1].sample(n=min(80, n_unique), random_state=42)
    mid_pool = name_places[(name_places["n"] >= 2) & (name_places["n"] <= 15)].sample(n=min(60, n_mid), random_state=42)
    common_pool = name_places[name_places["n"] >= 50].sample(n=min(40, n_common), random_state=42)
    examples = []
    for _, row in pd.concat([unique_pool, mid_pool, common_pool]).iterrows():
        n = int(row["n"])
        places_list = places_by_norm.get(row["norm"], [str(row["place"])])
        examples.append({
            "name": str(row["norm"]),
            "raw": str(row["raw"]),
            "n_places": n,
            "example_place": str(row["place"]),
            "places": places_list[:12],
            "weight_log": round(float(1.0 / np.log2(1 + n)), 3),
            "weight_sqrt": round(float(1.0 / np.sqrt(n)), 3),
            "weight_norm_idf": round(float(norm_idf_weight(n, corpus_localities)), 3),
            "unique": n == 1,
        })
    random.Random(42).shuffle(examples)
    (OUT / "examples.json").write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(examples)} street examples")

    # 6. Per-city aggregation (on deduplicated per-city streets!)
    per = df_dedup.groupby(["סמל_ישוב", "שם_ישוב"]).agg(
        streets=("norm", "count"),
        pure_exclusive=("n", lambda x: int((x == 1).sum())),
        rarity_sum_log=("w_log", "sum"),
        rarity_mean_log=("w_log", "mean"),
        rarity_sum_sqrt=("w_sqrt", "sum"),
        rarity_mean_sqrt=("w_sqrt", "mean"),
        rarity_sum_idf=("w_idf", "sum"),
        rarity_mean_idf=("w_idf", "mean"),
    ).reset_index()

    per["exclusive_pct"] = (per["pure_exclusive"] / per["streets"] * 100.0).round(1)
    per["rarity_mean_log"] = (per["rarity_mean_log"] * 100.0).round(1)
    per["rarity_sum_log"] = per["rarity_sum_log"].round(1)
    per["rarity_mean_sqrt"] = (per["rarity_mean_sqrt"] * 100.0).round(1)
    per["rarity_sum_sqrt"] = per["rarity_sum_sqrt"].round(1)
    per["rarity_mean_idf"] = (per["rarity_mean_idf"] * 100.0).round(1)
    per["rarity_sum_idf"] = per["rarity_sum_idf"].round(1)

    # Sample unique & most-common streets per locality for the city detail inspector
    city_samples: dict[int, tuple[list[str], list[dict]]] = {}
    for code, grp in df_dedup.groupby("סמל_ישוב"):
        if pd.isna(code):
            continue
        uniq_names = sorted(grp.loc[grp["n"] == 1, "norm"].tolist())
        # Deterministic diverse sample of up to 8 unique streets
        if len(uniq_names) > 8:
            rng = random.Random(int(code))
            uniq_sample = sorted(rng.sample(uniq_names, 8))
        else:
            uniq_sample = uniq_names
        common_rows = grp[grp["n"] > 1].sort_values(["n", "norm"], ascending=[False, True]).head(8)
        common_sample = [{"name": str(r["norm"]), "n": int(r["n"])} for _, r in common_rows.iterrows()]
        city_samples[int(code)] = (uniq_sample, common_sample)

    # 7. Fetch CBS demographics and merge by official locality code (סמל יישוב)
    print("Fetching CBS...")
    cbs = pd.DataFrame(fetch_all(CBS_ID))
    cbs["סמל יישוב"] = pd.to_numeric(cbs["סמל יישוב"], errors="coerce")

    pop_col = pick_col(cbs, lambda c: "אוכלוס" in c and "סך" in c)
    israelis_col = pick_col(cbs, lambda c: "ישראלים" in c and "סך" in c)
    arab_pop_col = pick_col(cbs, lambda c: "ערבים" in c)
    rel_col = pick_col(cbs, lambda c: "דת" in c and "יישוב" in c, lambda c: "דת" in c)
    coord_col = pick_col(cbs, lambda c: "קואורדינט" in c)

    cbs["pop"] = pd.to_numeric(cbs[pop_col], errors="coerce") if pop_col else np.nan
    cbs["israelis"] = pd.to_numeric(cbs[israelis_col], errors="coerce") if israelis_col else cbs["pop"]
    cbs["arab_pop"] = pd.to_numeric(cbs[arab_pop_col], errors="coerce") if arab_pop_col else np.nan
    cbs["religion"] = pd.to_numeric(cbs[rel_col], errors="coerce") if rel_col else np.nan
    cbs["coords"] = cbs[coord_col] if coord_col else None
    cbs["year"] = pd.to_numeric(cbs.get("שנת ייסוד"), errors="coerce")
    cbs["district"] = cbs.get("שם מחוז")
    cbs["status"] = cbs.get("שם מעמד מונציפאלי")

    m = per.merge(
        cbs[["סמל יישוב", "pop", "israelis", "arab_pop", "religion", "coords", "year", "district", "status"]],
        left_on="סמל_ישוב",
        right_on="סמל יישוב",
        how="left",
    )

    places = m[m["status"].isin(INCLUDED_STATUS)].copy()
    places_before_min = len(places)
    places = places[places["streets"] >= MIN_STREETS].copy()
    places_dropped = places_before_min - len(places)
    print(f"places status filter={places_before_min}, after min {MIN_STREETS} streets={len(places)} (dropped {places_dropped})")

    out: list[dict] = []
    for _, r in places.iterrows():
        st = str(r["status"]).strip() if pd.notna(r["status"]) else None
        sector, arab_pct = classify_sector(r["religion"], r["arab_pop"], r["israelis"])
        latlon = itm_to_wgs84(r["coords"])
        lat, lon = latlon if latlon else (None, None)
        code_int = int(r["סמל_ישוב"])
        uniq_sample, common_sample = city_samples.get(code_int, ([], []))

        streets_n = int(r["streets"])
        excl_n = int(r["pure_exclusive"])
        sum_log = float(r["rarity_sum_log"])
        sum_sqrt = float(r["rarity_sum_sqrt"])
        mean_log = float(r["rarity_mean_log"])
        mean_sqrt = float(r["rarity_mean_sqrt"])
        sum_idf = float(r["rarity_sum_idf"])
        mean_idf = float(r["rarity_mean_idf"])

        # Mathematical invariants (guard against regressions)
        assert 0 <= excl_n <= streets_n, f"pure_exclusive > streets for {r['שם_ישוב']}"
        assert sum_log <= streets_n + 0.15, f"rarity_sum_log > streets for {r['שם_ישוב']}"
        assert sum_sqrt <= streets_n + 0.15, f"rarity_sum_sqrt > streets for {r['שם_ישוב']}"
        assert sum_idf <= streets_n + 0.15, f"rarity_sum_idf > streets for {r['שם_ישוב']}"
        assert 0.0 <= mean_log <= 100.0, f"rarity_mean_log out of range for {r['שם_ישוב']}"
        assert 0.0 <= mean_sqrt <= 100.0, f"rarity_mean_sqrt out of range for {r['שם_ישוב']}"
        assert 0.0 <= mean_idf <= 100.0, f"rarity_mean_idf out of range for {r['שם_ישוב']}"

        out.append({
            "code": code_int,
            "name": str(r["שם_ישוב"]).strip(),
            "district": str(r["district"]).strip() if pd.notna(r["district"]) else None,
            "status": st,
            "kind": kind_from_status(st),
            "streets": streets_n,
            "pure_exclusive": excl_n,
            "exclusive_pct": float(r["exclusive_pct"]),
            "rarity_mean_log": mean_log,
            "rarity_sum_log": sum_log,
            "rarity_mean_sqrt": mean_sqrt,
            "rarity_sum_sqrt": sum_sqrt,
            "rarity_mean_norm_idf": mean_idf,
            "rarity_sum_norm_idf": sum_idf,
            "population": int(r["pop"]) if pd.notna(r["pop"]) else 0,
            "founded": int(r["year"]) if pd.notna(r["year"]) else None,
            "lat": lat,
            "lon": lon,
            "sector": sector,
            "arab_pct": arab_pct,
            "is_arab": sector == "ערבי",
            "is_mixed": sector == "מעורב",
            "sample_unique": uniq_sample,
            "sample_common": common_sample,
        })

    out.sort(key=lambda x: (-x["rarity_mean_sqrt"], -x["streets"], x["name"]))
    (OUT / "data.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "data.min.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    sector_counts = dict(Counter(x["sector"] for x in out))
    meta = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "rows_raw": rows_raw,
        "unique_raw": unique_raw,
        "rows_placeholder_9000": rows_placeholder_9000,
        "rows_after_numeric_filter": rows_after_numeric,
        "unique_after_numeric_filter": unique_after_numeric,
        "rows_after_normalize": rows_dedup,
        "rows_intra_city_dups_removed": rows_dups_removed,
        "unique_after_normalize": unique_norm,
        "pure_exclusive_names": pure_exclusive_names,
        "places_in_ranking": len(out),
        "sector_counts": sector_counts,
        "min_streets": MIN_STREETS,
        "places_dropped_lt_min_streets": int(places_dropped),
        "mazal_aliases_count": len(aliases),
        "reversed_pairs_merged": len(reversed_map),
        "idf_corpus_localities": corpus_localities,
        "sector_source": "CBS official דת יישוב (1=יהודי, 2=ערבי כולל דרוזים וצ׳רקסים, 4=מעורב)",
        "normalize_mode": "structural + dedup per city + reversed person names + cleaned mazal_aliases (no auto definite-article strip)",
        "top_shared": top_shared,
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(out)} places", Counter(x["kind"] for x in out), "sectors:", sector_counts)


if __name__ == "__main__":
    main()

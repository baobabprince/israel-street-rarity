#!/usr/bin/env python3
"""Fetch Israel streets, normalize, compute rarity scores, write data.json"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

try:
    import requests, numpy as np, pandas as pd
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "pandas", "numpy", "-q"])
    import requests, numpy as np, pandas as pd

OUT = Path(__file__).resolve().parent.parent
if not (OUT / "index.html").exists():
    OUT = Path(".")
STREETS_ID = "9ad3862c-8391-4b2f-84a4-2d4c68625f4b"
CBS_ID = "d47a54ff-87f0-44b3-b33a-f284c0c38e5a"
API = "https://data.gov.il/api/3/action/datastore_search"

def fetch_all(rid, limit=32000):
    recs, offset = [], 0
    while True:
        r = requests.get(API, params={"resource_id": rid, "limit": limit, "offset": offset}, timeout=120)
        r.raise_for_status()
        batch = r.json()["result"]["records"]
        if not batch: break
        recs.extend(batch)
        offset += len(batch)
        print(f"  {len(recs)}", flush=True)
        if len(batch) < limit: break
    return recs

def is_numeric(name):
    if not isinstance(name, str): return True
    s = name.strip()
    return bool(re.match(r"^(רח|רח'|רח\.|רחוב)?\s*\d+\s*$", s) or re.match(r"^\d+\s*$", s))

def normalize(name):
    if not isinstance(name, str): return ""
    s = name.strip()
    for p in [r"^רחוב\s+", r"^רח'\s+", r"^רח\.\s+", r"^רח\s+", r"^שדרות\s+", r"^שד['׳']\s+", r"^שד\.\s+", r"^שד\s+",
              r"^דרך\s+", r"^סמטת\s+", r"^סמטה\s+", r"^כיכר\s+", r"^ככר\s+", r"^מעלה\s+", r"^שביל\s+", r"^טיילת\s+"]:
        s = re.sub(p, "", s)
    s = s.replace("'", "׳").replace("`", "׳")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*\(.*\)\s*$", "", s).strip()
    if re.search(r"ז[׳']?בוטינסקי|זבוטינסקי", s): return "ז'בוטינסקי"
    if re.search(r"(בנימין זאב )?הרצל$", s) or s in ("הרצל", "בנימין זאב הרצל"): return "הרצל"
    if "ויצמן" in s or "וייצמן" in s: return "ויצמן"
    if re.search(r"בן[- ]?גוריון", s): return "בן גוריון"
    if "בגין" in s: return "בגין"
    if "טרומפלדור" in s: return "טרומפלדור"
    if "ארלוזורוב" in s: return "ארלוזורוב"
    if "ביאליק" in s: return "ביאליק"
    if "אחד העם" in s: return "אחד העם"
    if "רוטשילד" in s: return "רוטשילד"
    if "דיזנגוף" in s: return "דיזנגוף"
    if "אלנבי" in s: return "אלנבי"
    if len(s) > 2 and s.startswith("ה"): s = s[1:]
    return s

ARAB = {"נצרת","שפרעם","אום אל-פחם","טמרה","טירה","כפר קרע","כפר קאסם","טייבה","מגאר","סח'נין","סחנין","באקה אל-גרביה","עראבה","רהט","קלנסווה","דאלית אל-כרמל","עוספיה","כפר כנא","כפר מנדא","אעבלין"}

def main():
    print("Fetching streets...")
    df = pd.DataFrame(fetch_all(STREETS_ID))
    df["סמל_ישוב"] = pd.to_numeric(df["סמל_ישוב"], errors="coerce")
    df = df[~df["שם_רחוב"].apply(is_numeric)].copy()
    df["norm"] = df["שם_רחוב"].apply(normalize)
    df = df[df["norm"].str.len() > 0]
    print(f"clean rows={len(df)} unique={df['norm'].nunique()}")
    nloc = df.groupby("norm")["סמל_ישוב"].nunique()
    df["n"] = df["norm"].map(nloc)
    df["w"] = 1.0 / np.log2(1 + df["n"])
    per = df.groupby(["סמל_ישוב", "שם_ישוב"]).agg(
        streets=("norm", "nunique"),
        pure_exclusive=("n", lambda x: int((x == 1).sum())),
        rarity_sum_log=("w", "sum"),
        rarity_mean_log=("w", "mean"),
    ).reset_index()
    per["rarity_mean_log"] = (per["rarity_mean_log"] * 100).round(1)
    per["rarity_sum_log"] = per["rarity_sum_log"].round(1)
    print("Fetching CBS...")
    cbs = pd.DataFrame(fetch_all(CBS_ID))
    cbs["סמל יישוב"] = pd.to_numeric(cbs["סמל יישוב"], errors="coerce")
    pop_col = next((c for c in cbs.columns if "אוכלוס" in str(c) and "סך" in str(c)), None)
    cbs["pop"] = pd.to_numeric(cbs[pop_col], errors="coerce") if pop_col else np.nan
    cbs["year"] = pd.to_numeric(cbs.get("שנת ייסוד"), errors="coerce")
    cbs["district"] = cbs.get("שם מחוז")
    cbs["status"] = cbs.get("שם מעמד מונציפאלי")
    m = per.merge(cbs[["סמל יישוב", "pop", "year", "district", "status"]], left_on="סמל_ישוב", right_on="סמל יישוב", how="left")
    cities = m[m["status"] == "עירייה"].copy()
    cities["is_arab"] = cities["שם_ישוב"].isin(ARAB) | cities["שם_ישוב"].str.contains(r"אל-|אום |טמרה|טירה|נצרת|שפרעם|רהט|טייבה|מגאר|סח|עראבה|קלנסווה|באקה", na=False)
    cities.loc[cities["שם_ישוב"].isin(["מודיעין עילית", "ביתר עילית", "אריאל", "אלעד"]), "is_arab"] = False
    out = []
    for _, r in cities.iterrows():
        out.append({
            "name": r["שם_ישוב"],
            "district": r["district"] if pd.notna(r["district"]) else None,
            "streets": int(r["streets"]) if pd.notna(r["streets"]) else 0,
            "pure_exclusive": int(r["pure_exclusive"]) if pd.notna(r["pure_exclusive"]) else 0,
            "rarity_mean_log": float(r["rarity_mean_log"]) if pd.notna(r["rarity_mean_log"]) else None,
            "rarity_sum_log": float(r["rarity_sum_log"]) if pd.notna(r["rarity_sum_log"]) else None,
            "population": int(r["pop"]) if pd.notna(r["pop"]) else 0,
            "founded": int(r["year"]) if pd.notna(r["year"]) else None,
            "is_arab": bool(r["is_arab"]),
        })
    out.sort(key=lambda x: -(x["rarity_mean_log"] or 0))
    (OUT / "data.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "data.min.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(out)} cities")

if __name__ == "__main__":
    main()

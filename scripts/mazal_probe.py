"""Probe documented NLI searches without exposing the API key."""
import json
import os
from pathlib import Path
import requests

key = os.environ.get("MAZAL_API_KEY", "")
if not key:
    print("::error::MAZAL_API_KEY is missing")
    raise SystemExit(1)
results = []
for label, params in [
    ("identity", {"query": "any,contains,מונטיפיורי", "material_type": "Identity"}),
    ("books", {"query": "title,exact,Jerusalem"}),
]:
    try:
        response = requests.get(
            "https://api.nli.org.il/openlibrary/search",
            params={"api_key": key, **params, "output_format": "json", "items_per_page": 3, "result_page": 1},
            headers={"Accept": "application/json"}, timeout=(20, 60), allow_redirects=False)
    except requests.RequestException:
        print(f"::error::{label}: network error (details withheld)")
        continue
    print(f"::notice::{label}: HTTP {response.status_code}; Errors header: {bool(response.headers.get('Errors'))}")
    if response.status_code != 200 or response.headers.get("Errors"):
        continue
    try:
        data = response.json()
    except ValueError:
        print(f"::error::{label}: non-JSON response")
        continue
    if label == "identity":
        text = json.dumps(data, ensure_ascii=False, indent=2).replace(key, "[REDACTED]")
        Path("mazal-probe.json").write_text(text + "\n", encoding="utf-8")
        results.append(label)
if not results:
    print("::error::No usable Identity response")
    raise SystemExit(1)

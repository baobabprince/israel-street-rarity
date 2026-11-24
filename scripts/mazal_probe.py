"""Probe documented NLI searches without exposing the API key."""
import json
import os
from pathlib import Path
import requests

key = os.environ.get("MAZAL_API_KEY", "")
if not key:
    print("::error::MAZAL_API_KEY is missing")
    raise SystemExit(1)
try:
    values = requests.get("https://api.nli.org.il/openlibrary/values/getMaterialTypes",
                          params={"api_key": key}, timeout=(20, 60), allow_redirects=False)
    if values.status_code == 200:
        try:
            value_json = values.json()
            print(f"::notice::getMaterialTypes: HTTP 200; Identity mentioned: {'identity' in json.dumps(value_json).lower()}")
        except ValueError:
            print("::notice::getMaterialTypes: HTTP 200, not JSON")
    else:
        print(f"::notice::getMaterialTypes: HTTP {values.status_code}")
except requests.RequestException:
    print("::notice::getMaterialTypes: network error")
results = []
for label, params in [
    ("identity_he", {"query": "any,contains,מונטיפיורי", "material_type": "Identity"}),
    ("identity_en", {"query": "any,contains,Montefiore", "material_type": "Identity"}),
    ("identity_title", {"query": "title,contains,Montefiore", "material_type": "Identity"}),
    ("identity_lower", {"query": "title,contains,Montefiore", "material_type": "identity"}),
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
    if response.headers.get("Errors"):
        try:
            errors = json.loads(response.headers["Errors"]).get("Errors", [])
            codes = [str(e.get("code", "?")) for e in errors if isinstance(e, dict)]
            details = [str(e.get("description", ""))[:300].replace(key, "[REDACTED]") for e in errors if isinstance(e, dict)]
            print(f"::notice::{label}: error codes {','.join(codes)}; details {'; '.join(details)}")
        except (ValueError, AttributeError):
            print(f"::notice::{label}: unparseable Errors header")
    if response.status_code != 200 or response.headers.get("Errors"):
        continue
    try:
        data = response.json()
    except ValueError:
        print(f"::error::{label}: non-JSON response")
        continue
    if label.startswith("identity"):
        text = json.dumps(data, ensure_ascii=False, indent=2).replace(key, "[REDACTED]")
        Path(f"mazal-probe-{label}.json").write_text(text + "\n", encoding="utf-8")
        results.append(label)
if not results:
    print("::error::No usable Identity response")
    raise SystemExit(1)

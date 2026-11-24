"""Probe the documented NLI Identity search without logging credentials."""
import json
import os
from pathlib import Path
import requests

key = os.environ.get("MAZAL_API_KEY", "")
if not key:
    raise SystemExit("MAZAL_API_KEY is missing")
try:
    response = requests.get(
        "https://api.nli.org.il/openlibrary/search",
        params={"api_key": key, "query": "any,contains,מונטיפיורי",
                "material_type": "Identity", "output_format": "json",
                "items_per_page": 3, "result_page": 1},
        headers={"Accept": "application/json"}, timeout=(20, 60), allow_redirects=False)
except requests.RequestException:
    raise SystemExit("NLI request failed (details omitted to protect the API key)") from None
print(f"NLI status: {response.status_code}; errors header: {bool(response.headers.get('Errors'))}")
if response.status_code != 200 or response.headers.get("Errors"):
    raise SystemExit("Identity search request failed")
try:
    data = response.json()
except ValueError:
    raise SystemExit("Identity search returned non-JSON") from None
# Only one small, public search sample; mask any accidental key occurrence.
text = json.dumps(data, ensure_ascii=False, indent=2).replace(key, "[REDACTED]")
Path("mazal-probe.json").write_text(text + "\n", encoding="utf-8")
print("Saved redacted Identity response for schema inspection")

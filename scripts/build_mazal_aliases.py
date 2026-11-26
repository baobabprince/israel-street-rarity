#!/usr/bin/env python3
"""
Build data/mazal_aliases.json by resolving structurally-cleaned Israeli street names
against the National Library of Israel's MAZAL (מז״ל) authority records (J9U / 987007...):

  Stage 1: Discover J9U authority IDs via Wikidata SPARQL (wdt:P8189) using exact,
           honorific-stripped, reversed-order, and spelling-normalized lookup keys,
           with sitelink-based disambiguation for multi-word names.
  Stage 2: Fetch LIVE MARC21 Authority XML directly from NLI's MAZAL endpoint
           (https://iiif.nli.org.il/IIIFv21/marc/authority/{j9u}) to extract official
           Hebrew preferred headings (100) and ALL Hebrew see-from variant forms (400/700),
           then intersect them against the full Israeli street corpus.
  Stage 3: Enrich remaining unresolved person / honorific / spelling-variant candidates
           via VIAF J9U SRU (local.sources any "j9u") -> NLI MAZAL MARC21, and optionally
           via NLI's Open Library Search API (api.nli.org.il/openlibrary/search) when
           NLI_API_KEY / --nli-api-key-file is supplied (leveraging NLI's internal MAZAL
           synonym expansion on subject/creator queries, since material_type=Identity
           is ignored by NLI's Open Library Search API).
  Stage 4: Corpus-wide unambiguous surname & honorific-prefix linking with strict
           anti-collision and anti-false-merge guards.

Usage:
  python scripts/build_mazal_aliases.py
  python scripts/build_mazal_aliases.py --nli-api-key-file /path/to/key --audit-out .cache/audit.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

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

from normalize import (  # noqa: E402
    _NON_PERSON_CONSTRUCT_WORDS,
    _NO_REVERSE_PAIRS,
    is_numeric,
    is_placeholder_street,
    load_aliases,
    make_street_cleaner,
    structural_clean,
    unify_punctuation,
)

OUT = SCRIPT_DIR.parent
if not (OUT / "index.html").exists():
    OUT = Path(".")

STREETS_ID = "9ad3862c-8391-4b2f-84a4-2d4c68625f4b"
DATA_GOV_API = "https://data.gov.il/api/3/action/datastore_search"
SPARQL_URL = "https://query.wikidata.org/sparql"
NLI_MARC_AUTH_URL = "https://iiif.nli.org.il/IIIFv21/marc/authority/{j9u}"
NLI_SEARCH_URL = "https://api.nli.org.il/openlibrary/search"
VIAF_SRU_URL = "https://viaf.org/viaf/search"

UA_SPARQL = {
    "User-Agent": "israel-street-rarity/3.0 (https://github.com/baobabprince/israel-street-rarity; research)",
    "Accept": "application/sparql-results+json",
}
UA_HTTP = {
    "User-Agent": "israel-street-rarity/3.0 (https://github.com/baobabprince/israel-street-rarity; research)",
}
MARC_NS = "{http://www.loc.gov/MARC21/slim}"

SPARQL_BATCH = 200
SPARQL_SLEEP = 0.25
MARC_WORKERS = 5

# Single-word common Hebrew words, plants, nature terms, first names, or ambiguous surnames
# that must NEVER be auto-merged via Wikidata/MAZAL lookup.
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
    # Additional common Hebrew nouns / nature / first names / places / ambiguous surnames
    "אלון", "אורן", "ארז", "דקל", "זית", "תמר", "שקד", "רימון", "גפן", "הדס",
    "אלה", "ברוש", "חרוב", "תאנה", "שיטה", "דולב", "רותם", "ירדן", "כרמל",
    "גליל", "נגב", "שרון", "חרמון", "תבור", "גלבוע", "ציון", "מוריה", "יהודה",
    "שמעון", "ראובן", "בנימין", "אברהם", "יצחק", "משה", "אהרן", "אהרון",
    "דוד", "שלמה", "שמואל", "אליהו", "ישעיהו", "יחזקאל", "הושע",
    "יואל", "מיכה", "נחום", "צפניה", "חגי", "זכריה", "מלאכי",
    "מאיר", "ספיר", "שפירא", "כהן", "אשכנזי", "מזרחי", "ביטון", "אדרי",
    "דהן", "פרץ", "פרידמן", "שלום", "ישראל", "עזרא", "נחמיה", "אסתר",
    "מרים", "שרה", "רבקה", "לאה", "דבורה", "יהושע", "גדעון", "שמשון",
    "אבשלום", "יונתן", "עמנואל", "מיכאל", "גבריאל", "רפאל", "אוריאל",
    "ברק", "לביא", "אריה", "נמר", "אילת", "צפריר", "דרור", "יגאל", "עידו",
    "איתן", "אסף", "עומר", "אלעד", "אופיר", "אביב", "סתיו", "חורף", "קיץ",
    "מנוח", "שושנה", "ורד", "נרקיס", "כלנית", "רקפת", "איריס", "יסמין",
    "סביון", "כליל", "מרגלית", "פנינה", "אודם", "ברקת", "שוהם", "לשם",
    "ענבר", "אחלמה", "ישפה", "טופז", "אלמוג", "שחף", "נשר", "עגור", "זמיר",
    "סנונית", "דוכיפת", "שלדג", "ינשוף", "צופית", "חוגלה", "חסידה",
    "הרצוג", "בארי", "עוזיאל", "מימון", "סלע", "גולן", "אשכול", "רמון",
    "שדה", "גבע", "צור", "פלג", "מעיין", "מעין", "נחל", "אגם", "כוכב",
    "שמש", "ירח", "גשם", "טל", "רוח", "אור", "זוהר", "נוגה", "הדר", "גיל",
    "שיר", "זמר", "רננה", "שמחה", "תקווה", "תקוה", "אמונה", "גאולה", "חירות",
    "שלווה", "אחווה", "רעות", "ידידות", "אהבה", "חסד", "צדק", "אמת",
    # Audited false-merge guards (places / living people / ultra-common ambiguous names / pseudonyms)
    "ישורון", "אלמגור", "תדמור", "אורגד", "כרמון", "שטיין", "מטלון", "ברודי",
    "תרדיון", "גרשום", "ישמעאל", "טרפון", "נתניהו", "בן אליעזר", "בן זאב",
    "חיים כהן", "כהן חיים", "יעקב כהן", "כהן יעקב", "דוד דהן", "רבי דוד דהן",
    "רב יוסף הכהן", "השופט חיים כהן", "רז אריה", "משה עזרא", "צור יעקב", "עטר חיים",
    "יוסף הכהן", "אהרון כהן", "יהל״ם", "צפירה", "בן אריה",
})

_RABBINIC_TITLES: frozenset[str] = frozenset({
    "הרב", "רבי", "רב", "רבן", "רבנו", "רבינו", "הגאון", "האדמו״ר", "אדמו״ר",
})

_NON_SURNAME_PREFIXES: frozenset[str] = frozenset({
    "הרב", "רבי", "רב", "רבן", "רבנו", "רבינו", "הגאון", "האדמו״ר", "אדמו״ר",
    "הנשיא", "הברון", "המשורר", "המשוררת", "המלך", "המלכה", "הנביא",
})

# Honorific prefixes stripped ONLY when generating lookup keys or matching a verified entity
TITLE_PREFIX_RE = re.compile(
    r"^(?:"
    r"הרב|רבי|רב|רבן|רבנו|רבינו|הגאון|האדמו״ר|אדמו״ר|הרבנית|"
    r"ד״ר|דוקטור|פרופ׳|פרופ|פרופסור|"
    r"הנשיא|הברון|ברון|סר|דון|"
    r"השופט|השר|האלוף|אלוף|רא״ל|תא״ל|אל״ם|אל״מ|סא״ל|רס״ן|סרן|"
    r"המשורר|המשוררת|הסופר|הצייר|מר"
    r")\s+"
)

# High-confidence curated overrides for prominent Israeli street figures
CURATED_OVERRIDES: dict[str, str] = {
    "הרצל": "בנימין זאב הרצל",
    "בן גוריון": "דוד בן גוריון",
    "דוד בן גוריון": "דוד בן גוריון",
    "בן גוריון דוד": "דוד בן גוריון",
    "ז׳בוטינסקי": "זאב ז׳בוטינסקי",
    "זבוטינסקי": "זאב ז׳בוטינסקי",
    "ז׳בוטינסקי זאב": "זאב ז׳בוטינסקי",
    "זאב ז׳בוטינסקי": "זאב ז׳בוטינסקי",
    "זאב זבוטינסקי": "זאב ז׳בוטינסקי",
    "ביאליק": "חיים נחמן ביאליק",
    "חיים נחמן ביאליק": "חיים נחמן ביאליק",
    "ח.נ. ביאליק": "חיים נחמן ביאליק",
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
    "הנשיא יצחק בן צבי": "יצחק בן צבי",
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
    # Well-known verified nicknames / spelling variants / prominent MAZAL figures
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
    "הנשיא הרצוג": "חיים הרצוג",
    "הנשיא הרצוג חיים": "חיים הרצוג",
    "הרצוג חיים": "חיים הרצוג",
    "חיים הרצוג": "חיים הרצוג",
    "הרב הרצוג": "יצחק אייזיק הלוי הרצוג",
    "הרב עוזיאל": "בן ציון מאיר חי עוזיאל",
    "בן ציון מאיר חי עוזיאל": "בן ציון מאיר חי עוזיאל",
    "הרב מימון": "יהודה לייב מימון",
    "יהודה לייב מימון": "יהודה לייב מימון",
    "הרב מוהליבר": "שמואל מוהליבר",
    "מוהליבר": "שמואל מוהליבר",
    "שמואל מוהליבר": "שמואל מוהליבר",
    "הרב קלישר": "צבי הירש קלישר",
    "קלישר": "צבי הירש קלישר",
    "צבי הירש קלישר": "צבי הירש קלישר",
    "הרב עובדיה יוסף": "עובדיה יוסף",
    "עובדיה יוסף": "עובדיה יוסף",
    "אלנבי": "אדמונד אלנבי",
    "אדמונד אלנבי": "אדמונד אלנבי",
    "מוצקין": "ליאו מוצקין",
    "מוצקין אריה": "ליאו מוצקין",
    "ליאו מוצקין": "ליאו מוצקין",
    "שפינוזה": "ברוך שפינוזה",
    "ברוך שפינוזה": "ברוך שפינוזה",
    "הרב צבי יהודה": "צבי יהודה קוק",
    "צבי יהודה קוק": "צבי יהודה קוק",
    "הר״ן": "ר״ן",
    "ר״ן": "ר״ן",
    "לב שמחה": "ישראל אלתר",
    "ישראל אלתר": "ישראל אלתר",
    "שאגת אריה": "אריה לייב שלוסברג",
    "אריה לייב שלוסברג": "אריה לייב שלוסברג",
    # Verified MAZAL non-human proper-noun entities (military, Zionist orgs, wars, historical events)
    "צה״ל": "צבא הגנה לישראל",
    "צהל": "צבא הגנה לישראל",
    "צבא הגנה לישראל": "צבא הגנה לישראל",
    "צבא ההגנה לישראל": "צבא הגנה לישראל",
    "אצ״ל": "ארגון צבאי לאומי",
    "האצ״ל": "ארגון צבאי לאומי",
    "ארגון צבאי לאומי": "ארגון צבאי לאומי",
    "הארגון הצבאי הלאומי": "ארגון צבאי לאומי",
    "לח״י": "לוחמי חירות ישראל",
    "הלח״י": "לוחמי חירות ישראל",
    "לוחמי חרות ישראל": "לוחמי חירות ישראל",
    "לוחמי חירות ישראל": "לוחמי חירות ישראל",
    "פלמ״ח": "פלמ״ח",
    "הפלמ״ח": "פלמ״ח",
    "הפלמח": "פלמ״ח",
    "פלוגות המחץ": "פלמ״ח",
    "קק״ל": "קרן קיימת לישראל",
    "קרן קימת": "קרן קיימת לישראל",
    "קרן קיימת": "קרן קיימת לישראל",
    "קרן קימת לישראל": "קרן קיימת לישראל",
    "קרן קיימת לישראל": "קרן קיימת לישראל",
    "הקרן הקיימת": "קרן קיימת לישראל",
    "הקרן הקיימת לישראל": "קרן קיימת לישראל",
    "מבצע קדש": "מלחמת סיני",
    "מלחמת סיני": "מלחמת סיני",
    "מבצע יונתן": "מבצע אנטבה",
    "מבצע יהונתן": "מבצע אנטבה",
    "מבצע אנטבה": "מבצע אנטבה",
    "מלחמת השחרור": "מלחמת העצמאות",
    "מלחמת הקוממיות": "מלחמת העצמאות",
    "מלחמת העצמאות": "מלחמת העצמאות",
    "יציאת אירופה": "אקסודוס",
    "אקסודוס": "אקסודוס",
    "חיבת ציון": "חובבי ציון",
    "חובבי ציון": "חובבי ציון",
    "הגדוד העברי": "הגדודים העבריים",
    "הגדודים העבריים": "הגדודים העבריים",
    "מורדי הגיטאות": "מורדי הגטאות",
    "מורדי הגטאות": "מורדי הגטאות",
    "חג האילנות": "ט״ו בשבט",
    "ט״ו בשבט": "ט״ו בשבט",
    "י״א חללי מינכן": "י״א חללי מינכן",
    "י״א הספורטאים": "י״א חללי מינכן",
    "טבח הספורטאים באולימפיאדת מינכן": "י״א חללי מינכן",
    "הר כרמל": "הכרמל",
    "הר הכרמל": "הכרמל",
    "הכרמל": "הכרמל",
    "ילדי טהרן": "ילדי טהראן",
    "ילדי טהראן": "ילדי טהראן",
}

# Pure ktiv-male-chaser (יי/י, וו/ו) homograph pairs that have DIFFERENT meanings in Hebrew
_KTIV_HOMOGRAPHS: frozenset[str] = frozenset({
    "השיירה", "השירה",
    "שיטים", "שייטים",
    "השניים", "השנים",
    "החייל", "החיל",
    "קוורץ", "קורץ",
    "לוין", "לווין",
    "ביירות", "בירות",
    "כוכב", "כווכב",
})

_SAFE_NODOT_ACRONYMS: frozenset[str] = frozenset({
    "הפלמח", "פלמח", "נילי", "בילו", "קקל", "הקקל", "אצל", "האצל", "צהל",
    "ויצו", "נעמת", "מפם", "פיקא", "קליה", "ייטב", "דובב",
})

_NONHUMAN_BLOCKED_VARIANTS: frozenset[str] = frozenset({
    "מע״ר", "ר״ם", "א״ב", "אפקים", "גרניום", "מודיעין", "בירות",
    "להקת חלב ודבש", "עם כלביא", "בית הבחירה", "יפה נוף", "העמק",
    "אייר", "איה", "האור", "עציון", "עכא", "כוכב הים", "עירייה",
    "ניצה", "שחם", "תאשור", "לבנדר", "הלבנה", "סהר", "נרד",
    "גבעת רמב״ם", "עין חי", "בית חזון", "קריה נאמנה", "נח״ל נמרוד",
    "הנחל", "האגם", "האילן", "החיל",
})

_GEO_PREFIXES: frozenset[str] = frozenset({
    "קרית", "קריית", "רמת", "נווה", "נוה", "גבעת", "תל", "כפר",
    "עין", "באר", "נחל", "עמק", "הר", "מבצע", "חטיבת",
})

_INST_PREFIXES: frozenset[str] = frozenset({
    "מכון", "ישיבת", "ישיבות", "מועצה", "בית", "קניון", "תיכון",
    "המכללה", "חסידות", "שכונת",
})

# Reject NLI catalog-style labels like "להט, ציון בן רצון, 1947-"
_CATALOG_LABEL_RE = re.compile(r",|\d{4}")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_PATRONYMIC_RE = re.compile(r"\s+(?:בן|בת|בר)\s+\S+(?:\s+\S+)?$")


def is_safe_alias(variant: str, preferred: str, allow_unique_surname: bool = False) -> bool:
    """Validate a candidate alias mapping before accepting it."""
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
    if len(v_tokens) == 1:
        if "״" in v and len(v) >= 3:
            return True
        # Never treat the last token of a title-led name (e.g. "רבי טרפון", "רבנו גרשום") as a surname
        if p_tokens[0] in _NON_SURNAME_PREFIXES:
            return False
        if allow_unique_surname and len(v) >= 5 and v == p_tokens[-1]:
            return True
        if v == p_tokens[-1] and len(v) >= 4:
            return True
        return False
    return True


def is_deceased_marc(marc: dict[str, Any] | None) -> bool:
    """Return True if a MAZAL MARC21 authority record has a death date (046 $g or 100 $d -YYYY)."""
    if not marc:
        return False
    if marc.get("death_date"):
        return True
    for entry in marc.get("h100", []) + marc.get("h400", []):
        d = entry.get("d", "")
        # Matches "1873-1934", "נפטר 1940", "-1927", etc. (NOT open-ended "1939-")
        if re.search(r"-\s*\d{3,4}", d) or "נפטר" in d or "נפטרה" in d:
            return True
    return False


def to_ascii_quotes(s: str) -> str:
    """Convert Hebrew geresh/gershayim back to ASCII quotes for Wikidata label lookup."""
    return s.replace("״", '"').replace("׳", "'")


def strip_title(name: str) -> tuple[str | None, str]:
    """Strip a single leading honorific title if present. Returns (title, rest)."""
    m = TITLE_PREFIX_RE.match(name)
    if not m:
        return None, name
    return m.group(0).strip(), name[m.end():].strip()


def reverse_two_word(name: str) -> str | None:
    """Return reversed 'B A' for safe two-word person names, else None."""
    parts = name.split()
    if len(parts) != 2 or parts[0] == parts[1]:
        return None
    if parts[0] in _NON_PERSON_CONSTRUCT_WORDS or parts[1] in _NON_PERSON_CONSTRUCT_WORDS:
        return None
    if frozenset(parts) in _NO_REVERSE_PAIRS:
        return None
    return f"{parts[1]} {parts[0]}"


def spelling_skeleton(s: str) -> str:
    """Collapse minor Hebrew ktiv/geresh differences for candidate grouping."""
    s = unify_punctuation(s)
    s = s.replace("׳", "").replace("״", "")
    s = re.sub(r"^איבן\s+", "אבן ", s)
    s = s.replace("יי", "י").replace("וו", "ו")
    return s


def marc_heading_to_variants(raw_a: str, raw_c: str = "") -> tuple[set[str], set[str]]:
    """Convert a MARC21 100/400 Hebrew heading into (full_variants, surname_candidates).

    Examples:
      'הרצל, בנימין זאב,' -> full={'בנימין זאב הרצל', 'הרצל בנימין זאב'}, surnames={'הרצל'}
      'קארו, יוסף בן אפרים,' -> full={'יוסף בן אפרים קארו', 'יוסף קארו', 'קארו יוסף'}, surnames={'קארו'}
      'אחד העם,' -> full={'אחד העם'}, surnames=set()
      'בית יוסף (קארו)' -> full={'בית יוסף'}, surnames=set()
    """
    full: set[str] = set()
    surnames: set[str] = set()
    text = raw_a.strip().rstrip(",;:. ")
    if not text:
        return full, surnames

    # Remove parenthetical qualifiers like "(קארו)" or "(הראשון)"
    text_no_paren = _PAREN_RE.sub("", text).strip().rstrip(", ")
    if not text_no_paren:
        return full, surnames

    if "," in text_no_paren:
        parts = [p.strip().rstrip(", ") for p in text_no_paren.split(",", 1)]
        last, first = parts[0], parts[1]
        last_u = unify_punctuation(last)
        first_u = unify_punctuation(first)
        if last_u and first_u:
            # Natural order & inverted order
            full.add(unify_punctuation(f"{first_u} {last_u}"))
            full.add(unify_punctuation(f"{last_u} {first_u}"))
            # Patronymic-stripped form (e.g., "יוסף בן אפרים" -> "יוסף")
            first_short = _PATRONYMIC_RE.sub("", first_u).strip()
            if first_short and first_short != first_u:
                full.add(unify_punctuation(f"{first_short} {last_u}"))
                full.add(unify_punctuation(f"{last_u} {first_short}"))
            # Surname candidate (single token only)
            if len(last_u.split()) == 1 and len(last_u) >= 5:
                surnames.add(last_u)
        elif last_u:
            full.add(last_u)
    else:
        u = unify_punctuation(text_no_paren)
        if u:
            full.add(u)
            if raw_c:
                c_u = unify_punctuation(_PAREN_RE.sub("", raw_c).strip().rstrip(", "))
                if c_u:
                    full.add(unify_punctuation(f"{u} {c_u}"))
                    full.add(unify_punctuation(f"{c_u} {u}"))

    # Filter out single-letter initials like "נ.ס." or "א. גינצבערג"
    clean_full = {
        v for v in full
        if len(v) >= 3 and not _CATALOG_LABEL_RE.search(v)
    }
    return clean_full, surnames


def fetch_all_streets(rid: str = STREETS_ID, limit: int = 32000) -> list[dict]:
    recs: list[dict] = []
    offset = 0
    while True:
        r = requests.get(DATA_GOV_API, params={"resource_id": rid, "limit": limit, "offset": offset}, timeout=120)
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


def sparql_query(query: str) -> list[dict] | None:
    for attempt in range(5):
        try:
            r = requests.post(SPARQL_URL, data={"query": query, "format": "json"}, headers=UA_SPARQL, timeout=180)
            if r.status_code in (429, 502, 503, 504):
                time.sleep(4 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["results"]["bindings"]
        except Exception as e:
            print(f"  SPARQL retry {attempt + 1}: {e}", flush=True)
            time.sleep(3 * (attempt + 1))
    return None


def escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def resolve_wikidata_batch(names: list[str]) -> dict[str, list[dict[str, Any]]] | None:
    """Resolve Hebrew names to Wikidata humans with NLI J9U ID (P8189) + sitelinks."""
    if not names:
        return {}
    values = " ".join(f'"{escape_literal(n)}"@he' for n in names)
    q = f"""
    SELECT ?q ?qid ?j9u ?pref ?sitelinks WHERE {{
      VALUES ?q {{ {values} }}
      {{ ?item rdfs:label ?q }} UNION {{ ?item skos:altLabel ?q }}
      ?item wdt:P8189 ?j9u; wdt:P31 wd:Q5.
      ?item rdfs:label ?pref. FILTER(LANG(?pref) = "he")
      OPTIONAL {{ ?item wikibase:sitelinks ?sitelinks. }}
      BIND(STRAFTER(STR(?item), "entity/") AS ?qid)
    }}
    """
    rows = sparql_query(q)
    if rows is None:
        return None
    by_q: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for b in rows:
        qv_raw = b["q"]["value"]
        qv = unify_punctuation(qv_raw)
        qid = b.get("qid", {}).get("value", "")
        j9u = b.get("j9u", {}).get("value", "")
        pref = unify_punctuation(b["pref"]["value"])
        sl = int(b.get("sitelinks", {}).get("value", "0") or "0")
        if qv and qid and j9u and pref:
            for key in {qv_raw, qv}:
                prev = by_q[key].get(qid)
                if not prev or (j9u.startswith("987007") and not prev["j9u"].startswith("987007")):
                    by_q[key][qid] = {"qid": qid, "j9u": j9u, "pref": pref, "sitelinks": sl}

    return {
        k: sorted(v.values(), key=lambda x: -x["sitelinks"])
        for k, v in by_q.items()
    }


def marc_nonhuman_to_variants(tag: str, subfields: list[tuple[str, str]]) -> set[str]:
    """Extract clean Hebrew street-name variants from MARC21 110/111/130/150/151/410/411/430/450/451."""
    variants: set[str] = set()
    by_code: dict[str, list[str]] = defaultdict(list)
    for code, val in subfields:
        if val and val.strip():
            by_code[code].append(val.strip())

    # If a 150/151/450/451 has subject subdivisions ($x/$y/$z), NEVER emit bare $a (e.g. "ישראל")!
    # Instead emit the chronological/topical subdivision if it stands alone (e.g. "מלחמת העצמאות, 1948-1949").
    if tag in ("150", "151", "450", "451") and (by_code.get("x") or by_code.get("y") or by_code.get("z")):
        for sub in by_code.get("y", []):
            s = re.sub(r",?\s*\d{3,4}(?:-\d{0,4})?\.?$", "", sub).strip().rstrip(",;:. ")
            s = _PAREN_RE.sub("", s).strip()
            u = unify_punctuation(s)
            if len(u) >= 3 and len(u.split()) >= 2 and not _CATALOG_LABEL_RE.search(u):
                variants.add(u)
        return variants

    # For corporate bodies (110/410) with subordinate units ($b), e.g. "ישראל. $b צה"ל. $b חטיבת גולני"
    if tag in ("110", "410") and by_code.get("b"):
        leaf = by_code["b"][-1]
        s = _PAREN_RE.sub("", leaf).strip().rstrip(",;:. ")
        s = re.sub(r",?\s*\d{3,4}(?:-\d{0,4})?\.?$", "", s).strip()
        u = unify_punctuation(s)
        # Skip generic sub-departments like "הלשכה הראשית", "צבא", "מחלקה", "ועד"
        if u and len(u) >= 3 and not _CATALOG_LABEL_RE.search(u):
            if "״" in u or len(u.split()) >= 2:
                variants.add(u)
        return variants

    # Standard $a heading
    for raw_a in by_code.get("a", []):
        s = _PAREN_RE.sub("", raw_a).strip().rstrip(",;:. ")
        s = re.sub(r",?\s*\d{3,4}(?:-\d{0,4})?\.?$", "", s).strip().rstrip(", ")
        if not s or "," in s:
            continue
        u = unify_punctuation(s)
        if len(u) >= 3 and not _CATALOG_LABEL_RE.search(u):
            variants.add(u)
    return variants


def resolve_wikidata_nonhuman_batch(names: list[str]) -> dict[str, list[dict[str, Any]]] | None:
    """Resolve Hebrew names to non-human Wikidata entities with NLI J9U ID (P8189) + sitelinks."""
    if not names:
        return {}
    values = " ".join(f'"{escape_literal(n)}"@he' for n in names)
    q = f"""
    SELECT ?q ?qid ?j9u ?pref ?sitelinks WHERE {{
      VALUES ?q {{ {values} }}
      {{ ?item rdfs:label ?q }} UNION {{ ?item skos:altLabel ?q }}
      ?item wdt:P8189 ?j9u.
      FILTER NOT EXISTS {{ ?item wdt:P31 wd:Q5 }}
      ?item rdfs:label ?pref. FILTER(LANG(?pref) = "he")
      OPTIONAL {{ ?item wikibase:sitelinks ?sitelinks. }}
      BIND(STRAFTER(STR(?item), "entity/") AS ?qid)
    }}
    """
    rows = sparql_query(q)
    if rows is None:
        return None
    by_q: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for b in rows:
        qv_raw = b["q"]["value"]
        qv = unify_punctuation(qv_raw)
        qid = b.get("qid", {}).get("value", "")
        j9u = b.get("j9u", {}).get("value", "")
        pref = unify_punctuation(b["pref"]["value"])
        sl = int(b.get("sitelinks", {}).get("value", "0") or "0")
        if qv and qid and j9u and pref:
            for key in {qv_raw, qv}:
                prev = by_q[key].get(qid)
                if not prev or (j9u.startswith("987007") and not prev["j9u"].startswith("987007")):
                    by_q[key][qid] = {"qid": qid, "j9u": j9u, "pref": pref, "sitelinks": sl}

    return {
        k: sorted(v.values(), key=lambda x: -x["sitelinks"])
        for k, v in by_q.items()
    }


def _edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


def clean_nonhuman_label(label: str) -> str:
    """Clean Wikidata/MAZAL non-human display label (strip parentheticals and decorative quotes)."""
    s = _PAREN_RE.sub("", label).strip()
    if s.count("׳") == 2:
        s = s.replace("׳", "").strip()
    return unify_punctuation(s)


def _is_plural_or_taxon_pair(v: str, p: str) -> bool:
    """Return True if two single-word Hebrew nouns differ only by singular/plural or biological family suffix."""
    if len(v.split()) == 1 and len(p.split()) == 1:
        for a, b in ((v, p), (p, v)):
            if a + "ים" == b or a + "ות" == b or a + "יים" == b or a + "אים" == b:
                return True
            if a.endswith("ה") and (a[:-1] + "ות" == b or a[:-1] + "יות" == b or a[:-1] + "ים" == b):
                return True
            if a.endswith("ית") and a[:-2] + "יות" == b:
                return True
            if a.endswith("י") and (a + "ים" == b or a + "ות" == b):
                return True
            if (b.endswith("יים") or b.endswith("אים")) and not (a.endswith("יים") or a.endswith("אים")):
                return True
    return False


def _norm_content_tokens(s: str) -> set[str]:
    out: set[str] = set()
    for t in spelling_skeleton(s).split():
        if t.startswith("ה") and len(t) >= 4:
            t = t[1:]
        if len(t) >= 2:
            out.add(t)
    return out


def is_safe_nonhuman_alias(variant: str, preferred: str, source: str = "") -> bool:
    """High-precision validator for non-human MAZAL entity mappings (orgs, units, events, places)."""
    v = unify_punctuation(variant)
    p = clean_nonhuman_label(preferred)
    if not v or not p or v in BLOCKED_KEYS or p in BLOCKED_KEYS or v in _NONHUMAN_BLOCKED_VARIANTS:
        return False
    if _CATALOG_LABEL_RE.search(p) or re.search(r"[A-Za-z]", p):
        return False
    if v == p:
        return True
    # Never strip definite article ה on bare single-word non-acronym common nouns (e.g. הזית <-> זית)
    if len(v.split()) == 1 and len(p.split()) == 1 and "״" not in v and "״" not in p:
        if v == "ה" + p or p == "ה" + v:
            return False
    if _is_plural_or_taxon_pair(v, p):
        return False

    # Gershayim-less official acronyms: only allow explicitly safe non-common-word acronyms
    if source == "mazal_nonhuman_acronym":
        return v in _SAFE_NODOT_ACRONYMS

    sv, sp = spelling_skeleton(v), spelling_skeleton(p)

    # Type 1: Official acronym (with ״) <-> full entity name or spelling variant
    if "״" in v:
        return len(v) >= 3
    if "״" in p:
        # When ״ is only in preferred (not variant), require skeleton/token relation so unrelated places never merge
        if sv == sp or sv == sp.lstrip("ה") or ("ה" + sv == sp):
            return True
        if v in _SAFE_NODOT_ACRONYMS:
            return True
        vt, pt = _norm_content_tokens(v), _norm_content_tokens(p)
        return bool(vt and pt and (len(vt & pt) / max(len(vt), len(pt)) >= 0.5))

    # Type 2a: Exact spelling skeleton match (ktiv male/chaser, double yod/vav, geresh)
    if sv == sp:
        return True

    # Type 2b: Single-word orthographic / transliteration variant (edit distance <= 1, len >= 5, same initial)
    if len(v.split()) == 1 and len(p.split()) == 1:
        return len(v) >= 5 and len(p) >= 5 and v[0] == p[0] and _edit_distance(sv, sp) <= 1

    # Type 2c: Multi-word overlap >= 65% with institutional & geographic prefix guards
    if len(v.split()) == 1 and len(p.split()) >= 2:
        return False
    vt, pt = _norm_content_tokens(v), _norm_content_tokens(p)
    if not vt or not pt:
        return False
    if len(vt & pt) / max(len(vt), len(pt)) < 0.65:
        return False
    vp0, pp0 = v.split()[0], p.split()[0]
    if pp0 in _INST_PREFIXES and vp0 not in _INST_PREFIXES:
        return False
    if vp0 in _GEO_PREFIXES and pp0 in _GEO_PREFIXES and spelling_skeleton(vp0) == spelling_skeleton(pp0):
        vr = _norm_content_tokens(" ".join(v.split()[1:]))
        pr = _norm_content_tokens(" ".join(p.split()[1:]))
        if not (vr & pr):
            return False
    return True


def fetch_mazal_marc(j9u: str) -> dict[str, Any] | None:
    """Fetch and parse a live MARC21 Authority XML record from NLI MAZAL (both 100/400 humans & 110/111/130/150/151 non-humans).

    Returns {'not_found': True} on HTTP 404, parsed dict on 200, or None on transient failure.
    """
    url = NLI_MARC_AUTH_URL.format(j9u=j9u)
    for attempt in range(4):
        try:
            r = requests.get(url, headers=UA_HTTP, timeout=20)
            if r.status_code == 404:
                return {"j9u": j9u, "not_found": True, "h100": [], "h400": [], "h700": [], "other_variants": []}
            if r.status_code in (429, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            root = ET.fromstring(r.text)
            h100: list[dict[str, str]] = []
            h400: list[dict[str, str]] = []
            h700: list[dict[str, str]] = []
            other_1xx: list[str] = []
            other_4xx: list[str] = []
            wikidata_id: str | None = None
            viaf_id: str | None = None
            death_date: str | None = None
            for df in root.findall(f".//{MARC_NS}datafield"):
                tag = df.attrib.get("tag")
                sf_list = [(sf.attrib.get("code") or "", sf.text or "") for sf in df]
                sfs = dict(sf_list)
                if tag == "024":
                    src = sfs.get("2", "").lower()
                    if src == "wikidata" and sfs.get("a"):
                        wikidata_id = sfs["a"].strip()
                    elif src == "viaf" and sfs.get("a"):
                        viaf_id = sfs["a"].strip()
                elif tag == "046" and sfs.get("g"):
                    death_date = sfs["g"].strip()
                elif tag in ("100", "400", "700") and sfs.get("9") == "heb" and sfs.get("a"):
                    entry = {"a": sfs["a"], "c": sfs.get("c", ""), "d": sfs.get("d", "")}
                    if tag == "100":
                        h100.append(entry)
                    elif tag == "400":
                        h400.append(entry)
                    else:
                        h700.append(entry)
                elif tag in ("110", "111", "130", "150", "151") and sfs.get("9") == "heb":
                    other_1xx.extend(sorted(marc_nonhuman_to_variants(tag, sf_list)))
                elif tag in ("410", "411", "430", "450", "451") and sfs.get("9") == "heb":
                    other_4xx.extend(sorted(marc_nonhuman_to_variants(tag, sf_list)))
            return {
                "j9u": j9u,
                "wikidata": wikidata_id,
                "viaf": viaf_id,
                "death_date": death_date,
                "h100": h100,
                "h400": h400,
                "h700": h700,
                "other_1xx": list(dict.fromkeys(other_1xx)),
                "other_4xx": list(dict.fromkeys(other_4xx)),
            }
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None


def viaf_lookup_j9u(name: str) -> list[str]:
    """Query VIAF SRU restricted to NLI (j9u) personal names; return unique J9U IDs."""
    cql = f'local.personalNames all "{name}" and local.sources any "j9u"'
    for attempt in range(2):
        try:
            r = requests.get(
                VIAF_SRU_URL,
                params={"query": cql, "httpAccept": "application/json", "maximumRecords": "3"},
                headers=UA_HTTP,
                timeout=10,
            )
            if r.status_code in (429, 502, 503):
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            n_rec_raw = data.get("searchRetrieveResponse", {}).get("numberOfRecords", 0)
            n_rec = int(n_rec_raw.get("content", 0)) if isinstance(n_rec_raw, dict) else int(n_rec_raw or 0)
            if n_rec == 0 or n_rec > 5:
                return []
            ids = list(dict.fromkeys(re.findall(r"J9U\|(\d+)", r.text)))
            return ids[:3]
        except Exception:
            time.sleep(0.8 * (attempt + 1))
    return []


def nli_api_expand_heading(name: str, api_key: str) -> list[str]:
    """Use NLI Open Library Search API's internal MAZAL synonym expansion on subject queries.

    Returns canonical Hebrew 'משפחה, פרטי' headings extracted from dc:subject / dc:creator.
    """
    headings: list[str] = []
    for field in ("subject", "creator"):
        try:
            r = requests.get(
                NLI_SEARCH_URL,
                params={
                    "api_key": api_key,
                    "query": f"{field},contains,{name}",
                    "items_per_page": "3",
                },
                headers=UA_HTTP,
                timeout=8,
            )
            if r.status_code != 200:
                continue
            arr = r.json()
            if not isinstance(arr, list):
                continue
            for item in arr:
                for dc_key in ("http://purl.org/dc/elements/1.1/subject", "http://purl.org/dc/elements/1.1/creator"):
                    for val_obj in item.get(dc_key, []):
                        val = val_obj.get("@value", "")
                        for part in re.split(r";|\$\$Q", val):
                            part = part.strip()
                            if re.search(r"[\u0590-\u05FF]", part) and ("," in part or re.search(r"\d{4}", part)):
                                clean = re.sub(r",?\s*\d{3,4}-\d{0,4}.*$", "", part).strip()
                                clean = re.sub(r"\s+(?:מחבר|עורך|לכבודו)$", "", clean).strip()
                                if clean:
                                    headings.append(clean)
            if headings:
                break
        except Exception:
            continue
    return list(dict.fromkeys(headings))


def choose_preferred_label(entity: dict[str, Any]) -> str:
    """Choose clean Hebrew canonical display label for a MAZAL entity."""
    wd_pref = entity.get("wd_pref", "")
    if wd_pref and not _CATALOG_LABEL_RE.search(wd_pref):
        return unify_punctuation(wd_pref)
    marc = entity.get("marc") or {}
    for entry in marc.get("h100", []):
        fulls, _ = marc_heading_to_variants(entry["a"], entry.get("c", ""))
        # Prefer natural "First Last" form (first generated)
        a = _PAREN_RE.sub("", entry["a"]).strip().rstrip(", ")
        if "," in a:
            last, first = [x.strip() for x in a.split(",", 1)]
            first_short = _PATRONYMIC_RE.sub("", unify_punctuation(first)).strip()
            cand = unify_punctuation(f"{first_short or first} {last}")
            if cand and not _CATALOG_LABEL_RE.search(cand):
                return cand
        elif fulls:
            return sorted(fulls, key=len)[0]
    return ""


def load_cache(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"wikidata": {}, "marc": {}, "viaf": {}, "nli_api": {}}


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build data/mazal_aliases.json using NLI MAZAL authority records.")
    parser.add_argument("--cache", type=Path, default=OUT / ".cache" / "mazal_cache.json")
    parser.add_argument("--nli-api-key-file", type=Path, default=None, help="File containing NLI api_key (or set NLI_API_KEY env var)")
    parser.add_argument("--audit-out", type=Path, default=None, help="Write detailed JSON audit of all resolved aliases")
    parser.add_argument("--dry-run", action="store_true", help="Do not overwrite data/mazal_aliases.json")
    parser.add_argument("--skip-viaf", action="store_true", help="Skip Stage 3 VIAF/NLI fallback lookups")
    args = parser.parse_args()

    nli_api_key = os.environ.get("NLI_API_KEY", "").strip()
    if args.nli_api_key_file and args.nli_api_key_file.exists():
        nli_api_key = args.nli_api_key_file.read_text(encoding="utf-8").strip()

    cache = load_cache(args.cache)
    cache.setdefault("wikidata", {})
    cache.setdefault("wikidata_nonhuman", {})
    cache.setdefault("marc", {})
    cache.setdefault("viaf", {})
    cache.setdefault("nli_api", {})

    print("1. Fetching Israeli street names from data.gov.il…", flush=True)
    raw = pd.DataFrame(fetch_all_streets(STREETS_ID))
    raw = raw[~raw["סמל_רחוב"].apply(is_placeholder_street)]
    raw_valid = raw[~raw["שם_רחוב"].apply(is_numeric)].copy()
    # Same alias-aware cleaner as build_data.py, seeded from the current alias map, so names like
    # "אהרון (רוזנפלד)" are looked up as "אהרון רוזנפלד" rather than the bare "אהרון".
    raw_valid["clean"] = raw_valid["שם_רחוב"].map(make_street_cleaner(load_aliases(OUT / "data" / "mazal_aliases.json")))
    raw_valid = raw_valid[raw_valid["clean"].str.len() >= 2]

    # Count distinct localities per cleaned street name
    city_counts: Counter[str] = Counter(
        raw_valid.drop_duplicates(subset=["סמל_ישוב", "clean"])["clean"]
    )
    corpus_names: set[str] = set(city_counts.keys())
    print(f"   Unique cleaned street names: {len(corpus_names)}", flush=True)

    # Build expanded lookup keys -> original corpus street names
    lookup_to_orig: dict[str, set[str]] = defaultdict(set)

    def add_lookup(lk: str, orig: str) -> None:
        lookup_to_orig[lk].add(orig)
        # Wikidata Hebrew labels/altLabels frequently use ASCII " and ' instead of ״ and ׳
        aq = to_ascii_quotes(lk)
        if aq != lk:
            lookup_to_orig[aq].add(orig)

    for n in sorted(corpus_names):
        add_lookup(n, n)
        title, rest = strip_title(n)
        if title and len(rest.split()) >= 2:
            add_lookup(rest, n)
            rev_rest = reverse_two_word(rest)
            if rev_rest:
                add_lookup(rev_rest, n)
        rev = reverse_two_word(n)
        if rev:
            add_lookup(rev, n)
        if n.startswith("איבן "):
            add_lookup("אבן " + n[5:], n)

    # Stage 1: Wikidata P8189 (J9U) SPARQL lookup (cached!)
    all_lookups = sorted(lookup_to_orig.keys())
    missing_wd = [k for k in all_lookups if k not in cache["wikidata"]]
    print(f"2. Wikidata P8189 (J9U) lookup: {len(all_lookups)} keys ({len(missing_wd)} uncached)…", flush=True)
    if missing_wd:
        total_batches = (len(missing_wd) + SPARQL_BATCH - 1) // SPARQL_BATCH
        for i in range(0, len(missing_wd), SPARQL_BATCH):
            batch = missing_wd[i : i + SPARQL_BATCH]
            res = resolve_wikidata_batch(batch)
            if res is not None:
                for k in batch:
                    cache["wikidata"][k] = res.get(k, [])
            if (i // SPARQL_BATCH + 1) % 20 == 0 or i + SPARQL_BATCH >= len(missing_wd):
                print(f"   SPARQL batch {i // SPARQL_BATCH + 1}/{total_batches} done", flush=True)
                save_cache(args.cache, cache)
            time.sleep(SPARQL_SLEEP)
        save_cache(args.cache, cache)

    # Disambiguate Wikidata hits into j9u entities
    entities: dict[str, dict[str, Any]] = {}  # j9u -> {qid, j9u, wd_pref, sitelinks, matched_corpus}
    corpus_to_j9u: dict[str, set[str]] = defaultdict(set)
    match_provenance: dict[str, str] = {}

    for lk, orig_names in lookup_to_orig.items():
        cands = cache["wikidata"].get(lk, [])
        if not cands:
            continue
        chosen = None
        if len(cands) == 1:
            chosen = cands[0]
        elif len(lk.split()) >= 2 or "״" in lk:
            # Sitelink dominance disambiguation for multi-word names / acronyms
            top, second = cands[0], cands[1]
            if top["sitelinks"] >= 15 and top["sitelinks"] >= 4 * max(1, second["sitelinks"]):
                chosen = top
        if not chosen:
            continue

        j9u = chosen["j9u"]
        ent = entities.setdefault(j9u, {
            "j9u": j9u,
            "qid": chosen["qid"],
            "wd_pref": chosen["pref"],
            "sitelinks": chosen["sitelinks"],
            "marc": None,
        })
        if chosen["sitelinks"] > ent["sitelinks"]:
            ent["sitelinks"] = chosen["sitelinks"]
            ent["wd_pref"] = chosen["pref"]

        for orig in orig_names:
            if orig in BLOCKED_KEYS:
                continue
            # Single-word lookup only accepted if safe
            if len(orig.split()) == 1 and not is_safe_alias(orig, chosen["pref"]):
                continue
            corpus_to_j9u[orig].add(j9u)
            match_provenance.setdefault(orig, "wikidata_j9u")

    print(f"   Discovered {len(entities)} unique MAZAL J9U entities from Wikidata", flush=True)

    # Stage 2: Fetch LIVE MARC21 Authority XML from NLI MAZAL for every discovered J9U!
    j9u_list = sorted(entities.keys())
    missing_marc = [j for j in j9u_list if cache["marc"].get(j) is None]
    print(f"3. Fetching live NLI MAZAL MARC21 authority records: {len(j9u_list)} total ({len(missing_marc)} uncached/retry)…", flush=True)
    if missing_marc:
        done_cnt = 0
        with ThreadPoolExecutor(max_workers=MARC_WORKERS) as ex:
            futs = {ex.submit(fetch_mazal_marc, j): j for j in missing_marc}
            for fut in as_completed(futs):
                j = futs[fut]
                res = fut.result()
                if res is not None:
                    cache["marc"][j] = res
                done_cnt += 1
                if done_cnt % 100 == 0 or done_cnt == len(missing_marc):
                    print(f"   MAZAL MARC fetched {done_cnt}/{len(missing_marc)}", flush=True)
                    save_cache(args.cache, cache)
        save_cache(args.cache, cache)

    for j9u, ent in entities.items():
        ent["marc"] = cache["marc"].get(j9u)

    # Index ALL MAZAL 100/400/700 Hebrew variants against the Israeli street corpus!
    # Also build corpus lookup by spelling skeleton for multi-word exact-skeleton matching.
    corpus_by_skel: dict[str, set[str]] = defaultdict(set)
    for n in corpus_names:
        corpus_by_skel[spelling_skeleton(n)].add(n)

    # Also build honorific-stripped index: base (>=2 words) -> set of corpus names
    corpus_by_stripped: dict[str, set[str]] = defaultdict(set)
    for n in corpus_names:
        title, rest = strip_title(n)
        if title and len(rest.split()) >= 2:
            corpus_by_stripped[rest].add(n)
            corpus_by_stripped[spelling_skeleton(rest)].add(n)

    mazal_variant_to_j9us: dict[str, set[str]] = defaultdict(set)
    mazal_surname_to_j9us: dict[str, set[str]] = defaultdict(set)

    for j9u, ent in entities.items():
        marc = ent.get("marc")
        if not marc:
            continue
        for entry in marc.get("h100", []) + marc.get("h400", []) + marc.get("h700", []):
            fulls, surnames = marc_heading_to_variants(entry["a"], entry.get("c", ""))
            for v in fulls:
                mazal_variant_to_j9us[v].add(j9u)
            for sn in surnames:
                mazal_surname_to_j9us[sn].add(j9u)
        # Also include Wikidata preferred label surname
        pref = choose_preferred_label(ent)
        if pref:
            mazal_variant_to_j9us[pref].add(j9u)
            ptoks = pref.split()
            if len(ptoks) >= 2 and len(ptoks[-1]) >= 5:
                mazal_surname_to_j9us[ptoks[-1]].add(j9u)

    def is_ambiguous_in_wikidata(name: str) -> bool:
        """Return True if name (or its reversed/stripped form) matches >=2 Wikidata humans without a dominant winner."""
        forms = {name, to_ascii_quotes(name)}
        rev = reverse_two_word(name)
        if rev:
            forms.update({rev, to_ascii_quotes(rev)})
        _, rest = strip_title(name)
        if rest != name and len(rest.split()) >= 2:
            forms.update({rest, to_ascii_quotes(rest)})
            rev_r = reverse_two_word(rest)
            if rev_r:
                forms.update({rev_r, to_ascii_quotes(rev_r)})
        for f in forms:
            cands = cache["wikidata"].get(f, [])
            if len(cands) >= 2:
                top, second = cands[0], cands[1]
                if not (top["sitelinks"] >= 15 and top["sitelinks"] >= 4 * max(1, second["sitelinks"])):
                    return True
        return False

    def is_safe_marc_variant(variant: str, preferred: str) -> bool:
        """Extra hygiene guards specifically for MAZAL 400 see-from headings."""
        if not is_safe_alias(variant, preferred):
            return False
        if is_ambiguous_in_wikidata(variant):
            return False
        # Reject two-word patronymic pseudonyms ("בן X" / "בת X" / "בר X") with no token overlap with preferred
        vt = variant.split()
        pt = set(preferred.split())
        if len(vt) == 2 and vt[0] in ("בן", "בת", "בר") and vt[1] not in pt:
            return False
        return True

    # Link corpus names that uniquely match a MAZAL 100/400 variant (exact, skeleton, or title-prefixed!)
    mazal_hits = 0
    for variant, jset in mazal_variant_to_j9us.items():
        if len(jset) != 1:
            continue
        j9u = next(iter(jset))
        pref = choose_preferred_label(entities[j9u])
        if not pref:
            continue
        # 1. Exact corpus match
        if variant in corpus_names and variant not in BLOCKED_KEYS:
            if is_safe_marc_variant(variant, pref):
                if j9u not in corpus_to_j9u[variant]:
                    corpus_to_j9u[variant].add(j9u)
                    match_provenance.setdefault(variant, "mazal_marc_400")
                    mazal_hits += 1
        # 2. Honorific-prefixed corpus match (for >=2-word MAZAL variants)
        if len(variant.split()) >= 2 and not is_ambiguous_in_wikidata(variant):
            for orig in corpus_by_stripped.get(variant, ()):
                if orig not in BLOCKED_KEYS and not is_ambiguous_in_wikidata(orig) and j9u not in corpus_to_j9u[orig]:
                    corpus_to_j9u[orig].add(j9u)
                    match_provenance.setdefault(orig, "mazal_marc_title")
                    mazal_hits += 1
            # 3. Multi-word spelling skeleton match (e.g. "אבן ח׳לדון" <-> "אבן חלדון" <-> "איבן ח׳לדון")
            skel = spelling_skeleton(variant)
            if len(skel) >= 6:
                for orig in corpus_by_skel.get(skel, ()):
                    if (
                        len(orig.split()) >= 2
                        and orig not in BLOCKED_KEYS
                        and is_safe_marc_variant(orig, pref)
                        and j9u not in corpus_to_j9u[orig]
                    ):
                        corpus_to_j9u[orig].add(j9u)
                        match_provenance.setdefault(orig, "mazal_marc_spelling")
                        mazal_hits += 1
                for orig in corpus_by_stripped.get(skel, ()):
                    if orig not in BLOCKED_KEYS and not is_ambiguous_in_wikidata(orig) and j9u not in corpus_to_j9u[orig]:
                        corpus_to_j9u[orig].add(j9u)
                        match_provenance.setdefault(orig, "mazal_marc_title_spelling")
                        mazal_hits += 1

    print(f"   MAZAL MARC21 100/400 intersection added {mazal_hits} corpus links", flush=True)

    # Stage 3: VIAF J9U SRU + optional NLI API fallback strictly on person-titled unresolved candidates
    if not args.skip_viaf:
        unresolved_candidates: list[str] = []
        for n in sorted(corpus_names):
            if n in corpus_to_j9u or n in BLOCKED_KEYS:
                continue
            title, rest = strip_title(n)
            if title and len(rest.split()) >= 2 and city_counts[n] >= 2:
                unresolved_candidates.append(n)
            elif title and len(rest.split()) == 1 and len(rest) >= 4 and city_counts[n] >= 3:
                unresolved_candidates.append(n)

        unresolved_candidates = unresolved_candidates[:200]
        print(f"4. Stage 3 VIAF-J9U / NLI fallback on {len(unresolved_candidates)} person-titled unresolved candidates…", flush=True)

        query_map: dict[str, str] = {}
        for cand in unresolved_candidates:
            title, rest = strip_title(cand)
            query_map[cand] = rest if (title and len(rest.split()) >= 2) else cand

        uncached_viaf = sorted({q for q in query_map.values() if q not in cache["viaf"]})
        if uncached_viaf:
            done_v = 0
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = {ex.submit(viaf_lookup_j9u, q): q for q in uncached_viaf}
                for fut in as_completed(futs):
                    q = futs[fut]
                    cache["viaf"][q] = fut.result()
                    done_v += 1
                    if done_v % 40 == 0 or done_v == len(uncached_viaf):
                        print(f"   VIAF J9U checked {done_v}/{len(uncached_viaf)}", flush=True)
            save_cache(args.cache, cache)

        new_j9u_to_fetch = sorted({
            cache["viaf"][q][0]
            for q in query_map.values()
            if len(cache["viaf"].get(q, [])) == 1 and cache["viaf"][q][0] not in cache["marc"]
        })
        if new_j9u_to_fetch:
            with ThreadPoolExecutor(max_workers=MARC_WORKERS) as ex:
                futs = {ex.submit(fetch_mazal_marc, j): j for j in new_j9u_to_fetch}
                for fut in as_completed(futs):
                    cache["marc"][futs[fut]] = fut.result()
            save_cache(args.cache, cache)

        viaf_new_j9us: set[str] = set()
        nli_cands: list[tuple[str, str]] = []
        for cand, query_str in query_map.items():
            j9us = cache["viaf"].get(query_str, [])
            if len(j9us) == 1:
                j9u = j9us[0]
                marc = cache["marc"].get(j9u)
                if marc and marc.get("h100"):
                    if j9u not in entities:
                        entities[j9u] = {
                            "j9u": j9u,
                            "qid": marc.get("wikidata") or "",
                            "wd_pref": "",
                            "sitelinks": 0,
                            "marc": marc,
                        }
                        viaf_new_j9us.add(j9u)
            elif nli_api_key:
                nli_cands.append((cand, query_str))

        uncached_nli = sorted({q for _, q in nli_cands if q not in cache["nli_api"]})
        if nli_api_key and uncached_nli:
            done_n = 0
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(nli_api_expand_heading, q, nli_api_key): q for q in uncached_nli}
                for fut in as_completed(futs):
                    cache["nli_api"][futs[fut]] = fut.result()
                    done_n += 1
                    if done_n % 30 == 0 or done_n == len(uncached_nli):
                        print(f"   NLI API checked {done_n}/{len(uncached_nli)}", flush=True)
            save_cache(args.cache, cache)

        nli_hits = 0
        for cand, query_str in nli_cands:
            _, rest = strip_title(cand)
            rest_toks = {spelling_skeleton(t) for t in rest.split() if len(t) >= 3}
            for h in cache["nli_api"].get(query_str, []):
                fulls, surnames = marc_heading_to_variants(h)
                h_toks = {spelling_skeleton(s) for s in surnames} | {
                    spelling_skeleton(t) for fv in fulls for t in fv.split() if len(t) >= 3
                }
                if not (rest_toks & h_toks):
                    continue
                for fv in fulls:
                    jset = mazal_variant_to_j9us.get(fv)
                    if jset and len(jset) == 1:
                        j9u = next(iter(jset))
                        pref = choose_preferred_label(entities[j9u])
                        if pref and not is_ambiguous_in_wikidata(cand):
                            corpus_to_j9u[cand].add(j9u)
                            match_provenance.setdefault(cand, "nli_api_synonym")
                            nli_hits += 1
                            break
        if nli_hits:
            print(f"   NLI Search API synonym expansion matched {nli_hits} verified person-titled candidates", flush=True)

        viaf_hits = 0
        for j9u in viaf_new_j9us:
            ent = entities[j9u]
            marc = ent.get("marc") or {}
            pref = choose_preferred_label(ent)
            if not pref:
                continue
            for entry in marc.get("h100", []) + marc.get("h400", []) + marc.get("h700", []):
                fulls, _ = marc_heading_to_variants(entry["a"], entry.get("c", ""))
                for v in fulls:
                    if v in corpus_names and v not in BLOCKED_KEYS and is_safe_marc_variant(v, pref):
                        corpus_to_j9u[v].add(j9u)
                        match_provenance.setdefault(v, "viaf_j9u_marc")
                        viaf_hits += 1
                    if len(v.split()) >= 2 and not is_ambiguous_in_wikidata(v):
                        for orig in corpus_by_stripped.get(v, ()):
                            if orig not in BLOCKED_KEYS and not is_ambiguous_in_wikidata(orig):
                                corpus_to_j9u[orig].add(j9u)
                                match_provenance.setdefault(orig, "viaf_j9u_marc_title")
                                viaf_hits += 1
                        skel = spelling_skeleton(v)
                        if len(skel) >= 6:
                            for orig in corpus_by_skel.get(skel, ()):
                                if len(orig.split()) >= 2 and orig not in BLOCKED_KEYS and not is_ambiguous_in_wikidata(orig):
                                    corpus_to_j9u[orig].add(j9u)
                                    match_provenance.setdefault(orig, "viaf_j9u_marc_spelling")
                                    viaf_hits += 1
        print(f"   VIAF-J9U discovered {len(viaf_new_j9us)} new MAZAL records -> {viaf_hits} corpus links", flush=True)

    # Stage 4: Corpus-wide surname collision table (guards BOTH Stage 1 & Stage 4 single-word surnames!)
    surname_to_distinct_people: dict[str, set[str]] = defaultdict(set)
    for n in corpus_names:
        title, rest = strip_title(n)
        toks = rest.split()
        if len(toks) < 2:
            continue
        if toks[0] in _NON_PERSON_CONSTRUCT_WORDS:
            continue
        sn = toks[-1]
        if len(sn) < 4:
            continue
        jset = corpus_to_j9u.get(n)
        if jset and len(jset) == 1:
            surname_to_distinct_people[sn].add("J9U:" + next(iter(jset)))
        else:
            surname_to_distinct_people[sn].add("RAW:" + spelling_skeleton(rest))

    surname_hits = 0
    for sn, jset in mazal_surname_to_j9us.items():
        if sn not in corpus_names or sn in BLOCKED_KEYS or sn in corpus_to_j9u:
            continue
        if len(sn) < 5 or sn.startswith("ה"):
            continue
        active_j9us = {j for j in jset if any(j in js for js in corpus_to_j9u.values())}
        if len(active_j9us) != 1:
            continue
        j9u = next(iter(active_j9us))
        ent = entities[j9u]
        # Only auto-link bare surnames for deceased historical figures with prominence
        if not is_deceased_marc(ent.get("marc")):
            continue
        if ent.get("sitelinks", 0) < 10 and city_counts.get(sn, 0) < 3:
            continue
        distinct_people = surname_to_distinct_people.get(sn, set())
        if distinct_people != {f"J9U:{j9u}"}:
            continue
        pref = choose_preferred_label(ent)
        if pref and is_safe_alias(sn, pref, allow_unique_surname=True):
            corpus_to_j9u[sn].add(j9u)
            match_provenance[sn] = "mazal_unique_surname"
            surname_hits += 1
    print(f"5. Corpus-safe unique surname linking added {surname_hits} links", flush=True)

    # Assemble resolved map: only keep unambiguous corpus_name -> single j9u
    resolved: dict[str, str] = {}
    audit_rows: list[dict[str, Any]] = []
    for name, jset in sorted(corpus_to_j9u.items()):
        if len(jset) != 1:
            continue
        j9u = next(iter(jset))
        ent = entities[j9u]
        pref = choose_preferred_label(ent)
        if not pref or not is_safe_alias(name, pref, allow_unique_surname=(match_provenance.get(name) == "mazal_unique_surname")):
            continue
        # Guard ALL single-word non-acronym surnames against corpus-wide multi-person collisions (e.g. נתניהו, הרצוג)
        if len(name.split()) == 1 and "״" not in name and name != pref:
            distinct_people = surname_to_distinct_people.get(name, set())
            if len(distinct_people) > 1 or (len(distinct_people) == 1 and distinct_people != {f"J9U:{j9u}"}):
                continue
        # Guard rabbinic-titled street names from matching living/non-historical people
        title, _ = strip_title(name)
        if title in _RABBINIC_TITLES and name != pref and not is_deceased_marc(ent.get("marc")):
            if not cache["wikidata"].get(name) and not cache["wikidata"].get(to_ascii_quotes(name)):
                continue
        resolved[name] = pref
        audit_rows.append({
            "variant": name,
            "preferred": pref,
            "j9u": j9u,
            "cities": city_counts.get(name, 0),
            "source": match_provenance.get(name, "unknown"),
        })

    # Stage 6: Non-human MAZAL authorities (organizations, military units, places, wars, events, works)
    nh_lookup_to_orig: dict[str, set[str]] = defaultdict(set)

    def add_nh_lookup(lk: str, orig: str) -> None:
        nh_lookup_to_orig[lk].add(orig)
        aq = to_ascii_quotes(lk)
        if aq != lk:
            nh_lookup_to_orig[aq].add(orig)

    for n in sorted(corpus_names):
        if n in resolved or n in BLOCKED_KEYS:
            continue
        add_nh_lookup(n, n)
        # Allow definite-article stripping for lookup on acronyms ("הפלמ״ח" -> "פלמ״ח") & multi-word orgs/events
        if n.startswith("ה") and len(n) >= 4 and ("״" in n or len(n.split()) >= 2):
            add_nh_lookup(n[1:], n)

    nh_all_lookups = sorted(nh_lookup_to_orig.keys())
    missing_nh = [k for k in nh_all_lookups if k not in cache["wikidata_nonhuman"]]
    print(f"6. Non-human MAZAL (P8189) lookup: {len(nh_all_lookups)} keys ({len(missing_nh)} uncached)…", flush=True)
    if missing_nh:
        total_b = (len(missing_nh) + SPARQL_BATCH - 1) // SPARQL_BATCH
        for i in range(0, len(missing_nh), SPARQL_BATCH):
            batch = missing_nh[i : i + SPARQL_BATCH]
            res = resolve_wikidata_nonhuman_batch(batch)
            if res is not None:
                for k in batch:
                    cache["wikidata_nonhuman"][k] = res.get(k, [])
            if (i // SPARQL_BATCH + 1) % 20 == 0 or i + SPARQL_BATCH >= len(missing_nh):
                print(f"   Non-human SPARQL batch {i // SPARQL_BATCH + 1}/{total_b} done", flush=True)
                save_cache(args.cache, cache)
            time.sleep(SPARQL_SLEEP)
        save_cache(args.cache, cache)

    nh_entities: dict[str, dict[str, Any]] = {}
    nh_corpus_to_j9u: dict[str, set[str]] = defaultdict(set)
    nh_provenance: dict[str, str] = {}

    for lk, orig_names in nh_lookup_to_orig.items():
        cands = cache["wikidata_nonhuman"].get(lk, [])
        if not cands:
            continue
        chosen = None
        if len(cands) == 1:
            chosen = cands[0]
        else:
            top, second = cands[0], cands[1]
            if top["sitelinks"] >= 15 and top["sitelinks"] >= 4 * max(1, second["sitelinks"]):
                chosen = top
        if not chosen:
            continue
        j9u = chosen["j9u"]
        ent = nh_entities.setdefault(j9u, {
            "j9u": j9u,
            "qid": chosen["qid"],
            "wd_pref": chosen["pref"],
            "sitelinks": chosen["sitelinks"],
            "marc": None,
        })
        if chosen["sitelinks"] > ent["sitelinks"]:
            ent["sitelinks"] = chosen["sitelinks"]
            ent["wd_pref"] = chosen["pref"]
        for orig in orig_names:
            if orig in resolved or orig in BLOCKED_KEYS:
                continue
            if is_safe_nonhuman_alias(orig, chosen["pref"]):
                nh_corpus_to_j9u[orig].add(j9u)
                nh_provenance.setdefault(orig, "mazal_nonhuman_wd")

    print(f"   Discovered {len(nh_entities)} non-human MAZAL J9U entities", flush=True)

    # Fetch live MAZAL MARC21 (110/111/130/150/151 + 4xx) for non-human entities
    nh_j9us = sorted(nh_entities.keys())
    missing_nh_marc = [
        j for j in nh_j9us
        if cache["marc"].get(j) is None or "other_1xx" not in cache["marc"].get(j, {})
    ]
    print(f"   Fetching live MAZAL MARC21 for non-humans: {len(nh_j9us)} total ({len(missing_nh_marc)} uncached)…", flush=True)
    if missing_nh_marc:
        done_nh = 0
        with ThreadPoolExecutor(max_workers=MARC_WORKERS) as ex:
            futs = {ex.submit(fetch_mazal_marc, j): j for j in missing_nh_marc}
            for fut in as_completed(futs):
                j = futs[fut]
                res = fut.result()
                if res is not None:
                    cache["marc"][j] = res
                done_nh += 1
                if done_nh % 100 == 0 or done_nh == len(missing_nh_marc):
                    print(f"   Non-human MAZAL MARC fetched {done_nh}/{len(missing_nh_marc)}", flush=True)
                    save_cache(args.cache, cache)
        save_cache(args.cache, cache)

    nh_variant_to_j9us: dict[str, set[str]] = defaultdict(set)
    nh_acronym_skels: dict[str, set[str]] = defaultdict(set)
    for j9u, ent in nh_entities.items():
        marc = cache["marc"].get(j9u) or {}
        ent["marc"] = marc
        variants = set(marc.get("other_1xx", [])) | set(marc.get("other_4xx", []))
        if ent["wd_pref"]:
            variants.add(ent["wd_pref"])
        for v in list(variants):
            nh_variant_to_j9us[v].add(j9u)
            if "״" in v:
                # Allow definite-article + gershayim-less variants of official MAZAL acronyms (e.g. פלמ״ח -> הפלמ״ח, הפלמח, פלמח)
                bare = v[1:] if v.startswith("ה") else v
                for form in (bare, "ה" + bare):
                    nh_variant_to_j9us[form].add(j9u)
                    skel = form.replace("״", "").replace("׳", "")
                    if len(skel) >= 4:
                        nh_acronym_skels[skel].add(j9u)

    def is_ambiguous_nonhuman(name: str) -> bool:
        for f in {name, to_ascii_quotes(name)}:
            cands = cache["wikidata_nonhuman"].get(f, [])
            if len(cands) >= 2:
                top, second = cands[0], cands[1]
                if not (top["sitelinks"] >= 15 and top["sitelinks"] >= 4 * max(1, second["sitelinks"])):
                    return True
        return False

    nh_marc_hits = 0
    for variant, jset in nh_variant_to_j9us.items():
        if len(jset) != 1:
            continue
        j9u = next(iter(jset))
        pref = nh_entities[j9u]["wd_pref"]
        if not pref or _CATALOG_LABEL_RE.search(pref):
            continue
        # Exact corpus match
        if variant in corpus_names and variant not in resolved and variant not in BLOCKED_KEYS:
            if not is_ambiguous_nonhuman(variant) and is_safe_nonhuman_alias(variant, pref):
                if j9u not in nh_corpus_to_j9u[variant]:
                    nh_corpus_to_j9u[variant].add(j9u)
                    nh_provenance.setdefault(variant, "mazal_nonhuman_marc")
                    nh_marc_hits += 1
        # Multi-word spelling skeleton match (e.g. "קרן קימת לישראל" <-> "קרן קיימת לישראל", "עליית הנוער" <-> "עלית הנוער")
        if len(variant.split()) >= 2:
            skel = spelling_skeleton(variant)
            if len(skel) >= 6:
                for orig in corpus_by_skel.get(skel, ()):
                    if (
                        len(orig.split()) >= 2
                        and orig not in resolved
                        and orig not in BLOCKED_KEYS
                        and not is_ambiguous_nonhuman(orig)
                        and is_safe_nonhuman_alias(orig, pref)
                        and j9u not in nh_corpus_to_j9u[orig]
                    ):
                        nh_corpus_to_j9u[orig].add(j9u)
                        nh_provenance.setdefault(orig, "mazal_nonhuman_spelling")
                        nh_marc_hits += 1

    # Gershayim-less official MAZAL acronyms (e.g. "הפלמח" -> "פלמ״ח", "נילי" -> "ניל״י", "בילו" -> "ביל״ו")
    for skel, jset in nh_acronym_skels.items():
        if len(jset) != 1 or skel not in corpus_names or skel in resolved or skel in BLOCKED_KEYS:
            continue
        j9u = next(iter(jset))
        pref = nh_entities[j9u]["wd_pref"]
        if pref and not _CATALOG_LABEL_RE.search(pref) and not is_ambiguous_nonhuman(skel):
            if j9u not in nh_corpus_to_j9u[skel]:
                nh_corpus_to_j9u[skel].add(j9u)
                nh_provenance.setdefault(skel, "mazal_nonhuman_acronym")
                nh_marc_hits += 1

    print(f"   Non-human MAZAL MARC21 intersection added {nh_marc_hits} corpus links", flush=True)

    for name, jset in sorted(nh_corpus_to_j9u.items()):
        if len(jset) != 1 or name in resolved:
            continue
        j9u = next(iter(jset))
        pref = clean_nonhuman_label(nh_entities[j9u]["wd_pref"])
        if not pref or _CATALOG_LABEL_RE.search(pref):
            continue
        src = nh_provenance.get(name, "mazal_nonhuman_wd")
        if not is_safe_nonhuman_alias(name, pref, source=src):
            continue
        resolved[name] = pref
        audit_rows.append({
            "variant": name,
            "preferred": pref,
            "j9u": j9u,
            "cities": city_counts.get(name, 0),
            "source": src,
        })

    # Stage 7: Pure Hebrew ktiv-male-chaser (יי/י, וו/ו) unification for remaining corpus duplicates
    ktiv_groups: dict[str, list[str]] = defaultdict(list)
    for n in sorted(corpus_names):
        if len(n) < 4 or "״" in n or n in _KTIV_HOMOGRAPHS:
            continue
        kkey = n.replace("יי", "י").replace("וו", "ו")
        ktiv_groups[kkey].append(n)

    ktiv_hits = 0
    for kkey, members in sorted(ktiv_groups.items()):
        if len(members) < 2:
            continue
        # Only consider groups where every member has len >= 4 and none is a known homograph
        if any(m in _KTIV_HOMOGRAPHS for m in members):
            continue
        # If any member already resolved to a canonical entity, check if all resolved members agree
        existing_targets = {resolved[m] for m in members if m in resolved}
        if len(existing_targets) > 1:
            continue
        if len(existing_targets) == 1:
            canon = next(iter(existing_targets))
        else:
            # Prefer the highest-frequency form in the Israeli street corpus
            canon = max(members, key=lambda x: (city_counts.get(x, 0), len(x), x))
        for m in members:
            if m in resolved or m == canon:
                continue
            if m in BLOCKED_KEYS and m not in CURATED_OVERRIDES:
                continue
            resolved[m] = canon
            audit_rows.append({
                "variant": m,
                "preferred": canon,
                "j9u": "",
                "cities": city_counts.get(m, 0),
                "source": "ktiv_male_chaser",
            })
            ktiv_hits += 1
    print(f"7. Pure Hebrew ktiv-male-chaser (יי/י, וו/ו) unification added {ktiv_hits} links", flush=True)

    # Apply curated overrides on top (highest authority)
    for k, v in CURATED_OVERRIDES.items():
        uk = unify_punctuation(k)
        uv = unify_punctuation(v)
        resolved[uk] = uv

    # Unify chains (if A -> B and B -> C via curated override, make A -> C)
    for k in list(resolved.keys()):
        target = resolved[k]
        if target in CURATED_OVERRIDES:
            resolved[k] = unify_punctuation(CURATED_OVERRIDES[target])

    by_pref: dict[str, set[str]] = defaultdict(set)
    for v, p in resolved.items():
        if v in BLOCKED_KEYS and v not in CURATED_OVERRIDES:
            continue
        by_pref[p].add(v)
        by_pref[p].add(p)

    final: dict[str, str] = {
        "_comment": 'Auto-built from NLI MAZAL (מז״ל) MARC21 authority records (iiif.nli.org.il/IIIFv21/marc/authority/{J9U}) + Wikidata P8189 + VIAF J9U + curated overrides. Punctuation-normalized variant -> preferred Hebrew form.',
        "_source": "NLI MAZAL MARC21 authorities + Wikidata P8189 + VIAF J9U + curated",
        "_generated": time.strftime("%Y-%m-%d"),
    }
    for pref, variants in sorted(by_pref.items()):
        if len(variants) <= 1:
            continue
        for v in sorted(variants):
            final[v] = pref

    n_maps = sum(1 for k in final if not k.startswith("_"))
    n_entities = len({v for k, v in final.items() if not k.startswith("_")})
    print(f"Result: {n_maps} alias entries across {n_entities} canonical entities", flush=True)

    if args.audit_out:
        args.audit_out.parent.mkdir(parents=True, exist_ok=True)
        # Keep only rows that ended up in a multi-variant group
        kept_keys = {k for k in final if not k.startswith("_")}
        kept_audit = [r for r in audit_rows if r["variant"] in kept_keys and r["variant"] != r["preferred"]]
        args.audit_out.write_text(json.dumps(kept_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote audit ({len(kept_audit)} non-identity mappings) to {args.audit_out}", flush=True)

    if not args.dry_run:
        path = OUT / "data" / "mazal_aliases.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {path}", flush=True)


if __name__ == "__main__":
    main()

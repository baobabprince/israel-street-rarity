# נדירות שמות רחובות בישראל

דשבורד אינטראקטיבי + צינור נתונים אוטומטי.

## אתר (GitHub Pages)
https://baobabprince.github.io/israel-street-rarity/

## הרצת עדכון נתונים
1. לכו ל-**Actions** → **Update street rarity data** → **Run workflow**
2. הסקריפט מוריד את רשימת הרחובות מ-data.gov.il, מנרמל, מחשב מדדי נדירות, וכותב `data.json`
3. אחרי שה-Action מסתיים — רעננו את האתר

## הרצה מקומית
```bash
pip install requests pandas numpy
python scripts/build_data.py
```

## מבנה
- `scripts/build_data.py` — הורדה + נרמול + מדדי נדירות
- `.github/workflows/update-data.yml` — Action חודשי + ידני
- `data.json` — פלט לדשבורד
- `index.html` — דשבורד (Chart.js)

## מתודולוגיה
- הסרת רחובות מספריים בלבד
- נרמול: קידומות, ה׳ הידיעה, שמות אישים
- משקל: `1/log2(1+n)` כאשר n = מספר יישובים עם אותו שם

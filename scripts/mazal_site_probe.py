"""Inspect public Mazal frontend paths; never send the NLI API key to the site."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import requests

class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.urls.append(attrs['src'])

try:
    base = 'https://www.nli.org.il/he/search?projectName=NLI'
    response = requests.get(base, timeout=30)
    print(f'::notice::public search page HTTP {response.status_code}')
    response.raise_for_status()
    parser = Scripts()
    parser.feed(response.text)
    scripts = [urljoin(base, u) for u in parser.urls if urlsplit(urljoin(base, u)).hostname == 'www.nli.org.il']
    print(f'::notice::local JavaScript assets: {len(scripts)}')
    found = set()
    for url in scripts[:40]:
        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200 or len(r.content) > 10_000_000:
                continue
            # Only print string literals that look like paths and mention the authority interface.
            for match in re.findall(r'''["']([^"'\\\r\n]{1,200}(?:authorit|mazal)[^"'\\\r\n]{0,200})["']''', r.text, re.I):
                if '/' in match and len(match) < 200:
                    found.add(match)
        except requests.RequestException:
            continue
    for path in sorted(found)[:40]:
        print('::notice::frontend path: ' + path.replace('%', '%25').replace('\r',' ').replace('\n',' '))
    print(f'::notice::matching public frontend strings: {len(found)}')
except requests.RequestException:
    print('::error::public search inspection failed')
    raise SystemExit(1) from None

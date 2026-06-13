import urllib.request
import urllib.parse
import re

url = "https://html.duckduckgo.com/html/?q=test"
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req, timeout=10) as response:
        html = response.read().decode('utf-8')
    with open('ddg.html', 'w', encoding='utf-8') as f:
        f.write(html)
except Exception as e:
    print(e)

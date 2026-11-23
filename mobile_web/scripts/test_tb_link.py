import requests, re
r = requests.get('https://m.tb.cn/h.R7w6AIX', timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
for m in re.finditer(r'<script.*?</script>', r.text, re.DOTALL):
    print('SCRIPT:', m.group(0)[:300])

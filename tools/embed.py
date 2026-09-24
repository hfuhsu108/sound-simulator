# 把 tmp/processed 的 26 個 wav 以 base64 寫進 index.html 的 sampleData 區塊（裁示 22）
#
# 為什麼用腳本而不是 Edit 工具：1.46 MB 的 base64 無法手寫，屬確定性批次轉換。
# 替換用精確錨點，且只動 <script id="sampleData"> 這一個區塊；
# 跑完會印出「區塊外的位元組是否完全相同」供核對。
import base64, json, os, sys

ROOT = r'C:\Users\Hope\Desktop\CLAUDE工作區\simulation\sound-sim'
PROC = os.path.join(ROOT, 'tmp', 'processed')
HTML = os.path.join(ROOT, 'index.html')
OPEN_TAG = '<script id="sampleData">'
CLOSE_TAG = '</script>'

meta = json.load(open(os.path.join(PROC, 'meta.json'), encoding='utf-8'))

# index.html 只用得到這幾個欄位；frames／bytes／flatten／boost 是處理紀錄，留在 meta.json 就好
slim = [{'inst': r['inst'], 'note': r['note'], 'midi': r['midi'],
         'sr': r['sr'], 'loopStart': r['loopStart'], 'loopEnd': r['loopEnd']}
        for r in meta]

data = {}
for r in meta:
    p = os.path.join(PROC, r['inst'], r['note'] + '.wav')
    with open(p, 'rb') as f:
        data[r['inst'] + '/' + r['note']] = base64.b64encode(f.read()).decode('ascii')

payload = 'window.SOUND_SAMPLES = ' + json.dumps(
    {'meta': slim, 'data': data}, ensure_ascii=False, separators=(',', ':')) + ';'

# base64 字元集是 A-Za-z0-9+/=，不可能產生 '<'，所以不會提前關掉 script 標籤。
# 仍然斷言一次：這是把資料塞進 HTML 時唯一會靜默毀掉整頁的事。
assert '<' not in payload.split('"data":', 1)[1], '資料段出現 "<"，會截斷 script 標籤'

src = open(HTML, encoding='utf-8').read()
i = src.find(OPEN_TAG)
if i < 0:
    sys.exit('找不到錨點 ' + OPEN_TAG)
j = src.find(CLOSE_TAG, i)
if j < 0:
    sys.exit('錨點後找不到 </script>')
head, tail = src[:i + len(OPEN_TAG)], src[j:]
out = head + payload + tail

# 區塊外的位元組必須一字不差
old_head, old_tail = src[:i + len(OPEN_TAG)], src[j:]
same = (head == old_head and tail == old_tail)

open(HTML, 'w', encoding='utf-8', newline='').write(out)

print('sample 數        ', len(slim))
print('base64 總長      %.2f MB' % (sum(len(v) for v in data.values()) / 1048576))
print('index.html       %.0f KB → %.2f MB' % (len(src) / 1024, len(out) / 1048576))
print('區塊外位元組相同  ', same)
print('<script 標籤數    ', out.count('<script'), '／</script>', out.count('</script>'))
print('舊資料被完整取代  ', out.count('window.SOUND_SAMPLES = ') == 1)

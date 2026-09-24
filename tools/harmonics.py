# 從「實際會播出去的」sample 量出諧波的振幅與相位
# 用途：波形顯示改成合成，但合成的參數全部來自真實錄音，不是手調的
# 只用標準庫（本機無 numpy）。不做 FFT——基頻已知，直接算那些頻率上的 DFT 更準也更簡單。
import wave, json, os, array, math

OUT   = r'C:\Users\Hope\Desktop\CLAUDE工作區\simulation\sound-sim\tmp\processed'
NH    = 20        # 取前 20 個諧波
CYCLES= 24        # 分析窗口取整數個週期，避免頻譜洩漏

meta = json.load(open(os.path.join(OUT,'meta.json'), encoding='utf-8'))

def load(path):
    w = wave.open(path,'rb')
    a = array.array('h'); a.frombytes(w.readframes(w.getnframes())); w.close()
    return a

def dft_at(x, sr, freq):
    """算單一頻率上的 DFT。k 不必是整數，所以不受 FFT 頻率格點限制。"""
    w = 2*math.pi*freq/sr
    re = im = 0.0
    for i in range(len(x)):
        re += x[i]*math.cos(w*i)
        im -= x[i]*math.sin(w*i)
    n = len(x)
    return 2*math.sqrt(re*re+im*im)/n, math.atan2(im, re)

rows = {}
for r in meta:
    p = os.path.join(OUT, r['inst'], r['note']+'.wav')
    a = load(p)
    sr, f0 = r['sr'], r['f0']
    ls, le = r['loopStart'], r['loopEnd']

    per = sr/f0
    span = int(round(CYCLES*per))

    def analyse(start):
        s = max(0, min(start, len(a)-span))
        seg = a[s:s+span]
        if len(seg) < span:
            return None
        amps, phs = [], []
        for n in range(1, NH+1):
            f = f0*n
            if f > sr/2*0.95:            # 超過奈奎斯特就填 0
                amps.append(0.0); phs.append(0.0); continue
            m, ph = dft_at(seg, sr, f)
            amps.append(m); phs.append(ph)
        mx = max(amps) or 1.0
        return {
            'amps':   [round(v/mx, 4) for v in amps],
            'phases': [round(v, 4) for v in phs],
            'fundamentalRank': sorted(amps, reverse=True).index(amps[0]) + 1,
            # 有效諧波數：振幅 ≥ 最大值 5% 的個數，用來看音色豐不豐富
            'nSig': sum(1 for v in amps if v >= 0.05*mx),
        }

    # 兩個取樣位置：起音後 0.15 秒（高次諧波還在）vs 循環區中間（已衰減）
    early = analyse(int(0.15*sr))
    late  = analyse((ls+le)//2 - span//2)
    if not early or not late:
        continue
    rows.setdefault(r['inst'], []).append({
        'note': r['note'], 'midi': r['midi'], 'f0': r['f0'],
        'early': early, 'late': late,
        'amps': late['amps'], 'phases': late['phases'],
        'fundamentalRank': late['fundamentalRank'],
    })

for k in rows:
    rows[k].sort(key=lambda x: x['midi'])
with open(os.path.join(OUT,'harmonics.json'),'w',encoding='utf-8') as fh:
    json.dump(rows, fh, ensure_ascii=False, indent=1)

NAME={'flute':'長笛','clarinet':'單簧管','violin':'小提琴','trumpet':'小號','piano':'鋼琴'}
print('                起音後 0.15 秒          循環區（約 0.8 秒）')
print('樂器      音名  基音名次 有效諧波   基音名次 有效諧波   起音處前 8 諧波')
for k in ['flute','clarinet','violin','trumpet','piano']:
    for r in rows.get(k,[]):
        e, l = r['early'], r['late']
        print('%-9s %-5s %6d %7d %10d %7d    %s' % (
            NAME[k], r['note'], e['fundamentalRank'], e['nSig'],
            l['fundamentalRank'], l['nSig'],
            ' '.join('%.2f'%v for v in e['amps'][:8])))
print()
print('「基音名次」＝基音在 20 個諧波中的振幅排名；1 = 基音最強（波形週期才看得準）。')
print('「有效諧波」＝振幅 ≥ 最大值 5% 的諧波個數；越多代表音色越豐富、波形越複雜。')
print('輸出：', os.path.join(OUT,'harmonics.json'))

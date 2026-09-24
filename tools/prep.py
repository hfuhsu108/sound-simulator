# 把下載的樂器 wav 處理成可內嵌的短 sample
# 只用 Python 標準庫（本機無 ffmpeg / sox / numpy / audioop）
import wave, glob, os, json, array, math

BASE = r'C:\Users\Hope\Desktop\CLAUDE工作區\simulation\sound-sim\tmp\samples'
OUT  = r'C:\Users\Hope\Desktop\CLAUDE工作區\simulation\sound-sim\tmp\processed'
SR_OUT   = 22050      # 降取樣目標：可表現到 11 kHz，對基頻 262–659 Hz 的樂器夠用
KEEP_SEC = 1.00       # 每個 sample 保留的長度
LOOP_MIN = 0.60       # 循環區只在這之後找（確保起音已經過去）
LOOP_SEC = 0.25       # 循環區目標長度
PEAK     = 0.90       # 正規化目標峰值（留 headroom 給多鍵同按）
# 裁示 31「折衷補償」：鋼琴這類打擊式樂器的循環區已經衰減到別的樂器的 1/10，
# 學生按下去會以為程式壞了。把循環區補到 SUSTAIN_RMS，起音那一下維持原樣。
SUSTAIN_RMS  = 0.20   # 循環區目標 RMS（其他樂器本來就 0.207–0.580，不會被動到）
SUSTAIN_CAP  = 0.85   # 補償後循環區峰值上限，絕不削波
# 增益上限只是防呆（真正的限制是 SUSTAIN_CAP）。先前設 4.0 讓 E5 只補到 0.110，
# 反而使同一個樂器的高低音差兩倍——那是新的不一致，比「衰減感」更該避免。
# 衰減感來自「起音峰值 0.9 vs 持續 0.2」這個 4.5 倍落差，與本上限無關。
BOOST_MAX    = 8.0

PC = {'C':0,'Cs':1,'D':2,'Ds':3,'E':4,'F':5,'Fs':6,'G':7,'Gs':8,'A':9,'As':10,'B':11}

def note_to_midi(name):
    # 檔名如 C4 / As4 / Ds4 / Fs4
    i = len(name)
    while i > 0 and (name[i-1].isdigit()):
        i -= 1
    pc, octv = name[:i], int(name[i:])
    return (octv + 1) * 12 + PC[pc]

def midi_to_freq(m):
    return 440.0 * (2.0 ** ((m - 69) / 12.0))

def load_mono16(path):
    w = wave.open(path, 'rb')
    assert w.getnchannels() == 1 and w.getsampwidth() == 2
    sr = w.getframerate()
    a = array.array('h')
    a.frombytes(w.readframes(w.getnframes()))
    w.close()
    return a, sr

def find_onset(a, thresh_ratio=0.02):
    pk = max(abs(v) for v in a)
    th = pk * thresh_ratio
    for i, v in enumerate(a):
        if abs(v) > th:
            return i
    return 0

def downsample2(a):
    # 44100 -> 22050：相鄰兩點平均（兼作簡易低通），再抽取
    out = array.array('h', bytes(len(a) // 2 * 2))
    for i in range(len(a) // 2):
        out[i] = (a[2*i] + a[2*i+1]) // 2
    return out

def normalize(a, target=PEAK):
    pk = max(abs(v) for v in a)
    if pk == 0:
        return a
    g = target * 32767.0 / pk
    for i in range(len(a)):
        v = int(a[i] * g)
        a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return a

def rising_zero_crossings(a, lo, hi):
    out = []
    for i in range(lo, hi - 1):
        if a[i] <= 0 < a[i+1]:
            out.append(i)
    return out

XFADE_SEC = 0.006   # 循環接點的交叉淡化長度（6 ms）

def find_loop(a, sr, f0):
    """在 sustain 區找一段首尾波形最吻合的循環區間。
    以基頻整數週期為基準微調，用平方差挑最佳。
    註：長笛與小提琴的錄音帶 vibrato，波形本身持續在變，
    不存在完美吻合的循環點——所以搜尋之後一律再做一次 crossfade。"""
    per = sr / f0
    n_per = max(1, int(round(LOOP_SEC * f0)))
    base_len = int(round(n_per * per))
    lo = int(LOOP_MIN * sr)
    hi = len(a) - base_len - 8
    if hi <= lo:
        return 0, len(a)
    # start 候選放寬到 0.25 秒範圍（原本只有 0.08 秒、6 個候選）
    starts = rising_zero_crossings(a, lo, min(lo + int(0.25 * sr), hi))[:20]
    if not starts:
        starts = [lo]
    win = 128
    span = int(base_len * 0.04)          # 長度微調範圍放寬到 ±4%
    best = None
    for s in starts:
        for d in range(-span, span + 1, 2):
            e = s + base_len + d
            if e + win >= len(a) or e <= s:
                continue
            err = 0
            for k in range(0, win, 2):   # 隔點取樣，速度加倍、判別力足夠
                diff = a[s + k] - a[e + k]
                err += diff * diff
            if best is None or err < best[0]:
                best = (err, s, e)
    if best is None:
        return 0, len(a)
    return best[1], best[2]

def flatten_loop_envelope(a, ls, le, sr, f0):
    """把循環區內的音量壓平。
    鋼琴這類打擊式樂器在循環區內仍持續衰減，循環跳回起點時音量會突然變大。
    這裡只施加隨時間變化的「增益」，波形的形狀（＝音色）完全不動。
    對應 PROJECT_PLAN §8 既有決定：七種樂器一律 sustain 型包絡，讓學生定得住看。"""
    seg = max(1, int(sr / f0) * 4)          # 以 4 個週期為一段量 RMS
    if le - ls < seg * 4:
        return a, 1.0
    def seg_rms(i0):
        s = 0
        for i in range(i0, min(i0 + seg, le)):
            s += a[i] * a[i]
        return math.sqrt(s / seg)
    r0, r1 = seg_rms(ls), seg_rms(le - seg)
    if r0 <= 0 or r1 <= 0:
        return a, 1.0
    ratio = r0 / r1
    if ratio < 1.05:                         # 幾乎沒衰減就不動它
        return a, 1.0
    n = le - ls
    for k in range(n):
        g = 1.0 + (ratio - 1.0) * (k / (n - 1))
        v = int(a[ls + k] * g)
        a[ls + k] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return a, ratio

ENV_W = 441           # 包絡量測窗，20 ms @ 22050

def boost_sustain(a, ls, le):
    """把持續段托住，不讓它一路衰減到聽不見（裁示 31「折衷補償」）。

    做法是**逐點壓縮**，不是整段乘同一個數。先量 20 ms 短時 RMS 包絡，取它的
    累積最小值當「音量已經降到哪」（累積最小值天生單調不增，不受顫音與拍頻的來回
    波動干擾），目標包絡則是 max(該值, SUSTAIN_RMS)：起音段還沒降到門檻，增益恆為 1、
    峰值完全不動；降到門檻之後才被托住。**目標包絡由兩個單調不增的量取 max 再取 min，
    所以結果保證不會出現「越放越大聲」**——先前用線性斜坡補償時，補償速度追過了指數
    衰減，C4／C5／E5 的包絡實測各有 4／6／8 段回升，聽起來會是「叮…然後又變大聲」。

    必須排在 normalize 之後：normalize 依**全檔峰值**縮放，而鋼琴的峰值在起音那一下，
    先拉持續段再 normalize 等於白拉——這正是先前鋼琴補不上去的原因。
    其他樂器的持續段本來就不低於 SUSTAIN_RMS，增益全程為 1，不會被動到。"""
    n = len(a)
    if le - ls <= 0 or n < ENV_W * 4:
        return a, 1.0
    # 先看**循環區**（實際會被循環播放的那一段）夠不夠大聲，夠就整個不動。
    # 本函式的授權範圍是裁示 31 的「鋼琴太小聲」，不是去修別的樂器的起音過渡段——
    # 少了這道閘，長笛 A4、小號 As4／C4、小提琴 E4 這 4 檔的過渡段也會被托高（實測）。
    s = 0
    for i in range(ls, le):
        s += a[i] * a[i]
    if math.sqrt(s / (le - ls)) / 32768.0 >= SUSTAIN_RMS:
        return a, 1.0
    nb = (n + ENV_W - 1) // ENV_W
    env = [0.0] * nb
    bpk = [0.0] * nb
    for b in range(nb):
        i0, i1 = b * ENV_W, min((b + 1) * ENV_W, n)
        s = 0
        p = 0
        for j in range(i0, i1):
            s += a[j] * a[j]
            if abs(a[j]) > p:
                p = abs(a[j])
        env[b] = math.sqrt(s / (i1 - i0)) / 32768.0
        bpk[b] = p / 32768.0
    if max(env) <= 0:
        return a, 1.0
    # 累積最小值必須**從起音峰值那一塊起算**。從第 0 塊起算的話，起音前那段近似靜音
    # 會讓累積最小值一開始就趨近 0，於是門檻全程生效、連持續型樂器都被拉高——
    # 實測徵狀是本來不該動的 20 個非鋼琴檔 md5 全變了。
    pkb = max(range(nb), key=lambda b: env[b])
    run = [env[b] for b in range(nb)]
    m = env[pkb]
    for b in range(pkb, nb):
        if env[b] < m:
            m = env[b]
        run[b] = m
    g = [1.0] * nb
    for b in range(pkb, nb):
        if env[b] <= 1e-9:
            continue
        gi = max(run[b], SUSTAIN_RMS) / env[b]
        g[b] = min(max(gi, 1.0), BOOST_MAX, SUSTAIN_CAP / max(bpk[b], 1e-9))
        if g[b] < 1.0:
            g[b] = 1.0
    if max(g) <= 1.0:
        return a, 1.0
    # 只處理到 loopEnd：之後那段播放時用不到（循環在 loopEnd 就跳回），動它沒有意義
    n = min(n, le)
    # 逐樣本線性內插到區塊中心，避免區塊邊界出現增益階梯（那在波形上是一根假的不連續）
    for i in range(n):
        x = i / ENV_W - 0.5
        b0 = int(math.floor(x))
        t = x - b0
        ga = g[min(max(b0, 0), nb - 1)]
        gb = g[min(max(b0 + 1, 0), nb - 1)]
        v = int(a[i] * (ga + (gb - ga) * t))
        a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    lo, hi = ls // ENV_W, max(ls // ENV_W + 1, le // ENV_W)
    return a, sum(g[lo:hi]) / max(1, hi - lo)

def crossfade_loop(a, ls, le, sr):
    """把循環結束前的一小段與循環起點前的一小段交叉淡化，
    讓 le → ls 的跳接在波形上連續。這是 sampler 的標準做法，
    只動接點前 6 ms，不改變樂器本身的波形內容。"""
    n = int(XFADE_SEC * sr)
    if ls < n or le - n <= ls:
        return a
    for k in range(n):
        t = k / (n - 1) if n > 1 else 1.0
        i = le - n + k
        j = ls - n + k
        v = int(a[i] * (1.0 - t) + a[j] * t)
        a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return a

os.makedirs(OUT, exist_ok=True)
rows = []
for f in sorted(glob.glob(os.path.join(BASE, '*', '*.wav'))):
    inst = os.path.basename(os.path.dirname(f))
    note = os.path.splitext(os.path.basename(f))[0]
    midi = note_to_midi(note)
    f0 = midi_to_freq(midi)

    a, sr = load_mono16(f)
    on = find_onset(a)
    on = max(0, on - int(0.005 * sr))              # 起音前留 5 ms
    a = a[on: on + int(KEEP_SEC * sr)]
    if sr == 44100:
        a = downsample2(a)
        sr = 22050
    a = normalize(a)
    ls, le = find_loop(a, sr, f0)
    a, flat = flatten_loop_envelope(a, ls, le, sr, f0)
    if flat > 1.0:
        a = normalize(a)          # 壓平後峰值可能改變，重新正規化保住「換樂器不改變響度」
    a, boost = boost_sustain(a, ls, le)   # 必須排在 normalize 之後，理由見函式註解
    a = crossfade_loop(a, ls, le, sr)

    od = os.path.join(OUT, inst)
    os.makedirs(od, exist_ok=True)
    op = os.path.join(od, note + '.wav')
    w = wave.open(op, 'wb')
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(a.tobytes())
    w.close()

    rows.append({'inst': inst, 'note': note, 'midi': midi, 'f0': round(f0, 2),
                 'sr': sr, 'frames': len(a), 'loopStart': ls, 'loopEnd': le,
                 'loopCycles': round((le - ls) / (sr / f0), 2),
                 'flatten': round(flat, 3),
                 'boost': round(boost, 3),
                 'bytes': os.path.getsize(op)})

with open(os.path.join(OUT, 'meta.json'), 'w', encoding='utf-8') as fh:
    json.dump(rows, fh, ensure_ascii=False, indent=1)

tot = sum(r['bytes'] for r in rows)
print('處理完成', len(rows), '檔')
print('總計 %.2f MB → base64 約 %.2f MB' % (tot/1048576, tot*4/3/1048576))
print()
print('樂器      音名  MIDI  基頻Hz   樣本數  循環起點 循環長度 循環週期數  KB')
for r in rows:
    print('%-9s %-5s %4d %8.2f %7d %8d %8d %9.2f %6.1f' % (
        r['inst'], r['note'], r['midi'], r['f0'], r['frames'],
        r['loopStart'], r['loopEnd']-r['loopStart'], r['loopCycles'], r['bytes']/1024))

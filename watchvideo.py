#!/usr/bin/env python3
"""看视频：抓字幕 + 抽帧拼成印相样片，默认两样一起。都不留视频文件。

    watchvideo <url>                    # 字幕(txt) + 印相样片(格数按时长自动定)
    watchvideo <url> --no-frames        # 只要字幕（播客/讲座/访谈）
    watchvideo <url> -z 2:15            # 把某个时间点抽成大图看清楚
    watchvideo <url> -F 20              # 样片显式给格数
    watchvideo <url> --list             # 先看有哪些字幕轨
    watchvideo <url> -l en -f srt        # 指定语种 / 要时间轴
    watchvideo <url> -H                 # 字幕换成读画面上的硬字幕（方言/无字幕轨/自动字幕不准）+ 样片
    watchvideo <url> -H -f srt          # 同上，字幕带时间轴
    watchvideo <url> -H --ocr vision    # 两行字幕 / 多行花字：换 macOS Vision
    watchvideo <url> --keep-ads         # B 站默认剃掉恰饭段（社区标注），不想剃就加这个

字幕和画面各自会漏东西：字幕漏掉画面里引用的图，画面漏掉说了什么，
而 ASR 还会听错专名（《牛来》被听成"牛奶"）。要紧的视频两条都跑。

大多数站走 yt-dlp。小红书是例外：它的视频确实带 srt 字幕轨
（sns-subtitle-s2.xhscdn.com），但 yt-dlp 的 xiaohongshu extractor
里完全没有字幕代码，只会回一句 "has no subtitles"——那不是证据，
所以这里自己从页面 JSON 里解析。

硬字幕（-H）：先抽 24 帧自动找字幕在画面哪一条，再逐句识别。默认引擎 PP-OCRv6 tiny
（模型放 $WATCHVIDEO_MODELS，默认 ~/.cache/watchvideo/models；4.4MB，快且准，只认单行）；Vision 是系统自带的后备，会吞"的"。
23 分钟视频约 20 秒。字幕带找错了用 --sub-region 0.25 手动指定底部比例。

B 站的 AI 字幕必须登录。刚在浏览器登录完要等几秒再跑：
Chromium 系 cookie 落盘有延迟，在那之前抽到的是旧登录态，
B 站会回一句同样误导人的 "Subtitles are only available when logged in"。
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')


def srt_to_txt(path: Path) -> Path:
    """去掉序号和时间轴，只留正文——喂给模型读的时候省 token。

    YouTube 的自动字幕是滚动式的：每个 cue 的首行重复上一个 cue 的尾行。
    所以逐行收集再去掉连续重复，而不是把 cue 内多行拼成一行，
    否则同一句会连着出现两三次。
    """
    lines = []
    for block in path.read_text(encoding='utf-8').split('\n\n'):
        for l in block.strip().splitlines():
            l = l.strip()
            if not l or l.isdigit() or '-->' in l:
                continue
            if lines and l == lines[-1]:
                continue
            lines.append(l)
    dest = path.with_suffix('.txt')
    dest.write_text('\n'.join(lines), encoding='utf-8')
    path.unlink()
    return dest


def landed(dest: Path, detail: str):
    """产物落地那一行。**打绝对路径，不是 dest.name**（20260827 立卷）：
    OUTDIR 默认是 '.'，但 $WATCHVIDEO_OUT 会把落点悄悄挪走——只印一个光秃文件名，
    使用者会理所当然去 cwd 找，然后找不着。一个"看起来是相对路径"的输出，
    配上一个静默改目的地的环境变量，正好凑成一个陷阱。
    终端里的一行事实胜过文档里的一节；顺带在多数终端里还能直接点开。"""
    print(f'  -> {dest}  ({detail})')


# 带时间的字幕（20261029）：样片每格配「那一刻在说什么」、字幕里的视觉提示词强制加帧，都要它。
# 默认输出是 txt，srt 转完就删了——所以在删之前先解析进这里。只认第一份（多语种时先到先得）。
CUES = []   # [(开始秒, 结束秒, 文本)]


def _parse_srt(path: Path):
    out = []
    for block in path.read_text(encoding='utf-8', errors='replace').replace('\r', '').split('\n\n'):
        ls = [l.strip() for l in block.strip().splitlines()]
        tl = next((i for i, l in enumerate(ls) if '-->' in l), None)
        if tl is None:
            continue
        m = re.findall(r'(\d+):(\d+):(\d+)[,.](\d+)', ls[tl])
        if len(m) < 2:
            continue
        sec = lambda g: int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 10 ** len(g[3])
        text = re.sub(r'<[^>]+>|\{[^}]*\}', '', ' '.join(ls[tl + 1:])).strip()
        if text and not (out and out[-1][2] == text):   # YouTube 滚动字幕：连续同句只留一条
            out.append((sec(m[0]), sec(m[1]), text))
    return out


def report(path: Path, fmt: str):
    if not CUES and path.suffix == '.srt':
        try:
            CUES.extend(_parse_srt(path))
        except OSError:
            pass
    out = srt_to_txt(path) if fmt == 'txt' else path
    n = len(out.read_text(encoding='utf-8').strip().splitlines())
    landed(out, f'{n} 行')


# ---------- 小红书 ----------

# 浏览器数据目录（macOS）。只用来**分辨"读不到"的原因**，不用来找 cookie 本身
# ——找 cookie 的活儿归 yt-dlp。
_BROWSER_DIRS = {
    'chrome':  '~/Library/Application Support/Google/Chrome',
    'edge':    '~/Library/Application Support/Microsoft Edge',
    'brave':   '~/Library/Application Support/BraveSoftware/Brave-Browser',
    'chromium': '~/Library/Application Support/Chromium',
    'vivaldi': '~/Library/Application Support/Vivaldi',
    'firefox': '~/Library/Application Support/Firefox',
    'safari':  '~/Library/Safari',
}


def _host_app():
    """往上找到装着这个终端的 .app。
    ⚠️ TCC 授权是**按 app 发的，不是按"终端"发的**，所以提示里必须指名道姓：
    在 A 终端里授过权、这次却在 B 终端里跑，是很常见的情况；这时只说
    "给终端开权限"，读的人会理解成"我早开过了"，然后往别的方向查半天。"""
    try:
        pid = os.getpid()
        for _ in range(10):
            out = subprocess.run(['ps', '-o', 'ppid=,comm=', '-p', str(pid)],
                                 capture_output=True, text=True).stdout.strip()
            if not out:
                return None
            parts = out.split(None, 1)
            if len(parts) < 2:
                return None
            # ⚠️ 排掉 framework 里那层 .app：Homebrew 的解释器路径长这样——
            # `…/python@3.14/…/Python.framework/Versions/3.14/Resources/Python.app/
            # Contents/MacOS/Python`，不排的话第一步就匹配上，回一个 "Python"
            # 当成终端名（20260827 实测踩到）。真正的终端是最外层那个 .app。
            path = parts[1]
            m = re.search(r'/([^/]+)\.app/Contents/MacOS/', path)
            if m and '.framework/' not in path and '/Frameworks/' not in path:
                return m.group(1)
            pid = int(parts[0])
    except Exception:
        pass
    return None


def cookie_advice(browser):
    """借不到 cookie 时，分辨到底是哪一种"借不到"，回一句能照着做的话。

    ⚠️ yt-dlp 那句 `could not find <browser> cookies database in "<path>"` **会骗人**
    （20260827 立卷）：macOS 的 TCC 挡的是**列目录**，不是文件本身——
    `ls "<dir>"` 报 Operation not permitted，可 `ls "<dir>/Default/Cookies"` 好好的，
    库就在那儿、浏览器正开着。yt-dlp 靠枚举去找库，被挡住就报"找不到"，
    读的人会以为浏览器没装或没登录过，往错的方向查半天。
    ⚠️ 这个授权**会被 macOS 大版本升级重置**，所以"以前好好的"不是反证。"""
    d = _BROWSER_DIRS.get(browser)
    if not d or sys.platform != 'darwin':
        return None
    path = os.path.expanduser(d)
    try:
        os.listdir(path)
        return None                      # 列得动，那就是真没有 cookie / 没登录
    except PermissionError:
        app = _host_app()
        who = f'「{app}」' if app else '这个终端 app'
        return (f'  真正拦住的是 macOS 的隐私授权，不是"没有 cookie 库"：'
                f'库就在 {path} 里，但{who}没权限列它。\n'
                f'  去「系统设置 → 隐私与安全性 → 完全磁盘访问权限」把{who}加进去。\n'
                f'  ⚠️ 授权是**按 app 发的**：别的终端授过权不算数，换个终端就得重来；'
                f'而且 macOS 大版本升级会把已有的授权重置掉。')
    except FileNotFoundError:
        return f'  {browser} 的数据目录不存在（{path}），是不是没装/没跑过？'


def _cookie_header(browser, workdir, domain):
    if browser == 'none':
        return None
    jar = workdir / '.cookies.txt'
    subprocess.run(['yt-dlp', '--cookies-from-browser', browser,
                    '--cookies', str(jar), '--skip-download', '--simulate',
                    f'https://www.{domain}/'],
                   capture_output=True, check=False)
    if not jar.exists():
        return None
    pairs = {}
    for line in jar.read_text().splitlines():
        p = line.split('\t')
        if len(p) >= 7 and domain in p[0]:
            pairs[p[5]] = p[6]
    jar.unlink()
    return '; '.join(f'{k}={v}' for k, v in pairs.items()) or None


def _get(url, referer, cookie=None):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Referer': referer,
        **({'Cookie': cookie} if cookie else {})})
    return urllib.request.urlopen(req, timeout=30).read()


def _json_block(s, key):
    """从页面里抠出 "key":{...} 这一整块，靠括号配对定边界。"""
    i = s.find(f'"{key}":')
    if i < 0:
        return None
    j = s.index('{', i)
    depth = 0
    for k in range(j, len(s)):
        if s[k] == '{':
            depth += 1
        elif s[k] == '}':
            depth -= 1
            if depth == 0:
                return s[j:k + 1]
    return None


def expand_xhs(url):
    """展开 xhslink 短链，再把 /login?redirectPath=… 拆回真地址。

    xsec_token 在查询串里，丢了就取不到页面。小红书现在还会把匿名请求先打到
    登录页，真地址连同 token 一起被包进 redirectPath —— 不拆出来，后面拿到的
    全是登录页。"""
    if 'xhslink' in url:
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        url = urllib.request.urlopen(req, timeout=30).geturl()
    if not re.search(r'xiaohongshu\.com|xhslink', url):
        return url          # 别的站原样放行：这函数在 main 里对所有 url 都会走一遍
    for _ in range(3):
        nxt = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get('redirectPath')
        if not nxt or not nxt[0].lower().startswith('http'):
            break
        url = nxt[0]
    return re.sub(r'^http://', 'https://', url)


def xiaohongshu(url, lang, fmt, outdir, browser, list_only):
    url = expand_xhs(url)

    # 先不带 cookie 抓：小红书对登录态会返回另一套页面，里面根本没有字幕数据。
    # 匿名页反而带全 subtitles，所以匿名优先，拿不到再拿登录态兜底。
    blk = page = None
    for cookie in (None, _cookie_header(browser, outdir, 'xiaohongshu.com')):
        page = _get(url, 'https://www.xiaohongshu.com/', cookie).decode('utf-8', 'replace')
        # 只还原斜杠/&/引号的转义，别整篇 unicode_escape——那会毁掉中文
        t = page.replace('\\u002F', '/').replace('\\u0026', '&').replace('\\"', '"')
        blk = _json_block(t, 'subtitles')
        if blk:
            break
    if not blk:
        # ⚠️ 20260831 这里原来是 sys.exit，那会**直接终止进程**，main() 里
        #    "字幕挂了还要接着抓帧"那一段永远跑不到。20260830 改默认时我在 main()
        #    的注释里写明了这个场景（"小红书就是这样：yt-dlp 看不见它的字幕轨"），
        #    但没实现——**真正的退出点在更下层，而我只测了有字幕的视频。**
        #    改成 return，把返回码交回 main()，让它决定还要不要抓帧。
        print('这条笔记没有字幕轨（图文笔记、或视频没生成字幕）', file=sys.stderr)
        return 1
    subs = json.loads(blk)

    if list_only:
        print('可用字幕轨: ' + ', '.join(subs))
        return 0

    m = re.search(r'<title[^>]*>(.*?)</title>', page, re.S)
    title = re.sub(r'\s*-\s*小红书\s*$', '', html.unescape(m.group(1)).strip()) if m else 'xhs'
    title = re.sub(r'[/:\\]', '_', title)[:80]

    if lang:
        if lang not in subs:
            # 同上：不许在 handler 里 exit，返回码交给 main()
            print(f'没有 {lang} 这条轨，有的是: {", ".join(subs)}', file=sys.stderr)
            return 1
        picked = {lang: subs[lang]}
    else:
        # 自动挡：中文站看的基本是中文内容，默认就取中文轨
        key = next((k for k in ('zh-CN', 'zh-Hans', 'source') if k in subs),
                   next(iter(subs)))
        picked = {key: subs[key]}

    for lg, items in picked.items():
        dest = outdir / f'{title}.{lg}.srt'
        # URL 带 sign 和过期时间，每次都得现抓，存不住
        dest.write_bytes(_get(items[0]['url'], 'https://www.xiaohongshu.com/'))
        report(dest, fmt)
    return 0


# ---------- 其余站点 ----------

ZH_SITES = r'bilibili\.com|acfun\.cn'
# 小红书对登录态返回的是另一套残缺页面：既没有 subtitles 块，也没有可下载的
# video format。匿名反而拿得到全的，所以这些站一律匿名优先、登录态只作兜底。
ANON_FIRST = r'xiaohongshu\.com|xhslink'
# 默认写当前目录；想固定去处就设 WATCHVIDEO_OUT
OUTDIR = os.environ.get('WATCHVIDEO_OUT') or '.'


# ── B 站去广告（20261018）──────────────────────────────────────────────────
# 拉回来的 B 站视频连恰饭段一起拉回来了。yt-dlp 的 --sponsorblock 只认 YouTube，
# B 站这边用社区版 BilibiliSponsorBlock（「B站空降助手」那个库，bsbsb.top），
# 接口照抄 SponsorBlock。只剃字幕和样片里落在广告段的部分，视频本来就不留。
# ⚠️ 没人标过 ≠ 没广告：查不到要明说（0829 那条——只给一半会让人以为完整了）。
BSB_API = 'https://www.bsbsb.top/api/skipSegments'
AD_CATS = {'sponsor': '恰饭', 'selfpromo': '自推'}   # 片头片尾、三连提醒不算广告，不剃
KEEP_ADS = False
_ads_cache = {}


def _bili_get(url, timeout=8):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://www.bilibili.com/'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read()


def _bili_cid(bv, url):
    """分 P 视频每 P 一个 cid，标注是按 cid 记的。没带 ?p= 就是第 1 P。"""
    m = re.search(r'[?&]p=(\d+)', url)
    page = int(m.group(1)) if m else 1
    try:
        _, body = _bili_get(f'https://api.bilibili.com/x/web-interface/view?bvid={bv}')
        pages = json.loads(body)['data']['pages']
        return str(pages[page - 1]['cid'])
    except Exception:
        return None


def bili_ads(url):
    """返回这条 B 站视频要剃掉的 [(起, 止, 类别)]；不是 B 站或查不到就是空表。同一条只查一次。"""
    if KEEP_ADS or not re.search(r'bilibili\.com|b23\.tv', url):
        return []
    if url in _ads_cache:
        return _ads_cache[url]
    real = url
    m = re.search(r'BV[0-9A-Za-z]{10}', url)
    if not m and 'b23.tv' in url:
        try:
            real = _bili_get(url)[0]
            m = re.search(r'BV[0-9A-Za-z]{10}', real)
        except Exception:
            pass
    segs, note = [], None
    if not m:
        note = '认不出 BV 号，广告没剃'
    else:
        bv = m.group(0)
        q = urllib.parse.urlencode({'videoID': bv, 'categories': json.dumps(list(AD_CATS))})
        try:
            data = json.loads(_bili_get(f'{BSB_API}?{q}')[1])
        except urllib.error.HTTPError as e:
            data = [] if e.code == 404 else None      # SponsorBlock 系没标注时回 404
        except Exception:
            data = None
        if data is None:
            note = '查广告库失败（bsbsb.top 连不上），广告没剃'
        else:
            if {d.get('cid') for d in data} - {None}:
                cid = _bili_cid(bv, real)
                if cid:
                    data = [d for d in data if str(d.get('cid')) == cid]
                elif len({d.get('cid') for d in data}) > 1:
                    data, note = [], '分 P 视频认不出是哪一 P，广告没剃'
            segs = sorted((d['segment'][0], d['segment'][1], d['category']) for d in data
                          if d.get('actionType', 'skip') == 'skip' and d['segment'][1] > d['segment'][0])
            if not segs and not note:
                note = '空降助手里没人标过这条的广告段，没剃（不代表没有）'
    if segs:
        mmss = lambda t: f'{int(t) // 60}:{int(t) % 60:02d}'
        desc = '、'.join(f'{mmss(a)}–{mmss(b)} {AD_CATS.get(c, c)}' for a, b, c in segs)
        print(f'  去广告：字幕和样片里剃掉 {len(segs)} 段（{desc}）')
    else:
        print(f'  去广告：{note}')
    _ads_cache[url] = segs
    return segs


def _in_ads(t, ads):
    return any(a <= t < b for a, b, _ in ads)


def _cut_srt(path, ads):
    """删掉中点落在广告段里的字幕句，序号重排。"""
    def sec(x):
        h, m, rest = x.strip().replace('.', ',').split(':')
        s, ms = rest.split(',')
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    keep = []
    for block in path.read_text(encoding='utf-8').strip().split('\n\n'):
        lines = block.strip().splitlines()
        ts = next((l for l in lines if '-->' in l), None)
        if ts:
            a, b = ts.split('-->')
            if _in_ads((sec(a) + sec(b.split()[0])) / 2, ads):
                continue
            lines = lines[lines.index(ts):]
        keep.append(lines)
    path.write_text('\n\n'.join(f'{i}\n' + '\n'.join(l) for i, l in enumerate(keep, 1)) + '\n',
                    encoding='utf-8')


def _squeeze(ads, dur):
    """把广告段挖掉后的「正片时间轴」：返回 (正片总长, 切点换算, 正片时刻→原片时刻)。
    样片在正片轴上均匀取格，格子就不会浪费在广告上，标签仍是原片时刻（-z 要用）。"""
    iv = []
    for a, b, _ in ads:
        a, b = max(0.0, a), min(dur, b)
        if b <= a:
            continue
        if iv and a <= iv[-1][1]:
            iv[-1][1] = max(iv[-1][1], b)
        else:
            iv.append([a, b])

    def real(t):
        for a, b in iv:
            if t >= a:
                t += b - a
            else:
                break
        return t

    def squeeze_cuts(cuts):
        return [c - sum(min(b, c) - a for a, b in iv if a < c)
                for c in cuts if not any(a <= c < b for a, b in iv)]

    return dur - sum(b - a for a, b in iv), squeeze_cuts, real


def _pick(tracks, zh_first, native=None):
    """挑一条最合意的轨：中文站优先中文轨，其余站优先原生语言轨。"""
    zh = [r'^ai-zh$', r'^zh(-|$)']
    nat = [r'-orig$']
    if native:
        nat.append(rf'^{re.escape(native.split("-")[0])}(-|$)')
    for pat in (zh + nat if zh_first else nat + zh):
        hit = [t for t in tracks if re.search(pat, t)]
        if hit:
            return hit[0]
    return None


def _tracks(url, browser):
    """拿到可用轨名。B 站的 danmaku、直播回放的 live_chat 不算字幕，剔掉。"""
    # 必须带 --write-subs：InfoExtractor.extract_subtitles() 只在
    # writesubtitles/listsubtitles 为真时才真去取，裸 -J 会返回空 subtitles。
    # -J 本身是 simulate，不会真写文件。
    cmd = ['yt-dlp', '--skip-download', '--write-subs', '-J']
    r = subprocess.run(
        (cmd + ['--cookies-from-browser', browser] if browser != 'none' else cmd) + [url],
        capture_output=True, text=True)
    if r.returncode and browser != 'none' and 'cookies' in (r.stderr or '').lower():
        # cookie 罐读不出来（浏览器没装/从没跑过/正被锁着）**不是"没有字幕轨"**。
        # 不降级的话这里回空表，调用方就据此宣布"没找到任何字幕轨"——
        # 把锅扣给了站点。多数站的字幕根本不需要登录，先拿到能拿的。
        r = subprocess.run(cmd + [url], capture_output=True, text=True)
    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        return [], None
    # 一条推挂多段视频时 -J 回的是 playlist：顶层 subtitles 是空的，轨全在
    # entries 里。只看顶层就会把「有 en 轨」报成「没找到任何字幕轨」。
    nodes = [info] + [e for e in (info.get('entries') or []) if isinstance(e, dict)]
    keys = [k for n in nodes
            for k in list(n.get('subtitles') or {}) + list(n.get('automatic_captions') or {})]
    tracks = [k for k in dict.fromkeys(keys) if k not in ('live_chat', 'danmaku')]
    return tracks, next((n['language'] for n in nodes if n.get('language')), None)


def generic(url, lang, fmt, outdir, browser, list_only):
    base = ['yt-dlp', '--skip-download']
    if browser != 'none':
        base += ['--cookies-from-browser', browser]
    if list_only:
        return subprocess.run(base + ['--list-subs', url]).returncode

    warned = []

    def attempt(langs):
        before = set(outdir.glob('*.srt'))
        tail = ['--write-subs', '--write-auto-subs', '--convert-subs', 'srt',
                '--sub-langs', langs,
                '-o', str(outdir / '%(title)s.%(ext)s'), url]
        rc = subprocess.run(base + tail).returncode
        if rc and browser != 'none' and not warned:
            # 借不到 cookie 不该拖垮整次抓取——真正需要登录的只有 B 站那几家。
            # 降级重试一次，并且**明说降级了**：否则最后那句"没抓到字幕轨"
            # 就成了假结论，把锅扣给站点，而真凶是读不到 cookie 罐。
            # 至于罐子为什么读不到，交给 cookie_advice() 去分辨——最常见的那种
            # 原因，浏览器的报错本身就是错的。
            warned.append(1)
            print(f'  借 {browser} 的 cookie 失败了，改成不带 cookie 再试一次'
                  f'（要登录才给字幕的站点这次会拿不到）。', file=sys.stderr)
            advice = cookie_advice(browser)
            if advice:
                print(advice, file=sys.stderr)
            rc = subprocess.run(['yt-dlp', '--skip-download'] + tail).returncode
        return rc, sorted(set(outdir.glob('*.srt')) - before)

    if lang:
        rc, written = attempt(lang)
    else:
        # 自动挡：中文站默认中文轨；其余站默认原生轨——YouTube 把真 ASR
        # 标成 <lang>-orig，其余 150+ 条都是机翻，上 all 会一次落一百多个文件。
        zh_first = bool(re.search(ZH_SITES, url))
        rc, written = attempt('ai-zh' if zh_first else '.*-orig')
        if not written:
            tracks, native = _tracks(url, browser)
            if not tracks:
                print('没找到任何字幕轨。', file=sys.stderr)
                return rc or 1
            pick = _pick(tracks, zh_first, native)
            if pick:
                rc, written = attempt(pick)
            elif len(tracks) > 12:
                print(f'有 {len(tracks)} 条轨且挑不出中文/原生轨，不替你全下。'
                      f'\n用 -l 指定，前几条是: {", ".join(tracks[:12])} ...',
                      file=sys.stderr)
                return 1
            else:
                rc, written = attempt(','.join(tracks))

    if not written:
        print('没抓到字幕轨。先跑 --list 看有没有；'
              'B 站字幕要登录：加 --browser chrome（或 edge/safari），'
              '刚登完还要等几秒让 cookie 落盘。', file=sys.stderr)
        return rc or 1
    ads = bili_ads(url)
    for f in written:
        if ads:
            _cut_srt(f, ads)
        report(f, fmt)
    return rc


# ---------- 印相样片（contact sheet）----------

def _font():
    """样片标签用的字体。ImageMagick 在有些机器上找不到默认字体
    （报 unable to read font ''），所以显式挑一个；实在没有就不传
    -font，交回给 ImageMagick 自己的 fontconfig。"""
    # 20261029：标签开始印台词了，中文要有字形——Arial Unicode 打头（Arial 本身没有汉字，会印成方框）
    for f in ('/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
              '/System/Library/Fonts/Supplemental/Arial.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
              '/usr/share/fonts/dejavu/DejaVuSans.ttf',
              '/usr/share/fonts/TTF/DejaVuSans.ttf',
              'C:/Windows/Fonts/arial.ttf'):
        if os.path.exists(f):
            return f
    return None
# 抽帧不需要音轨。必须允许 video-only：B 站只提供分离流，
# 裸 worst[...] 只匹配音视频合并的流，在那里会直接 "format is not available"。
FMT = 'wv*[height>=480]/wv*/worst/best'


def _probe(path):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
         'stream=width,height', '-show_entries', 'format=duration',
         '-of', 'default=nw=1:nk=1', str(path)],
        capture_output=True, text=True).stdout.split()
    w, h, dur = int(out[0]), int(out[1]), float(out[2])
    return w, h, dur


def _scene_cuts(path, threshold=0.3):
    """场景切点。整片扫一遍，不用 -frames:v 截断——截断只会留下开头的切点，
    长片的尾巴全丢。154 秒的片子这步只要 1.6 秒，值。"""
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-f', 'lavfi', '-i',
         f'movie={path},select=gt(scene\\,{threshold})',
         '-show_entries', 'frame=pts_time', '-of', 'csv=p=0'],
        capture_output=True, text=True)
    # 20260908 修：csv=p=0 的每行可能带尾逗号（ffprobe 多输出一个空字段），
    # 直接 float() 会炸在 '481.050000,' 上。剥掉逗号再转，转不动的跳过。
    out = []
    for x in r.stdout.split():
        x = x.strip().rstrip(",")
        if not x: continue
        try: out.append(float(x))
        except ValueError: pass
    return out


def _pick_times(cuts, dur, n):
    """时间轴均匀分 n 段，每段落到最近的真实镜头切点上。

    不按切点序号均匀取——快切开场会让切点堆在前 10 秒，
    按序号取样会把大半格子花在开头，后面几分钟只剩一两格。
    """
    if len(cuts) < max(4, n // 3):     # 基本静止的片子，场景检测没意义
        return [dur * (i + 0.5) / n for i in range(n)]
    # 先合并挨得太近的切点（20260828）：镜头**内部**的快速运镜会让 scene detect
    # 连报几刀——实测 Roman 那片 0:48.0 和 0:48.3 报了两刀，其实是同一个镜头里
    # 探测器阵列在移动。不合并的话"每个切点最多一帧"就变成了同一镜头抽两帧。
    # ⚠️ 老版本靠 `abs(c - picked[-1]) <= 0.5` 挡这个，改成"切点不复用"时别把它一起丢了。
    merged = []
    for c in sorted(cuts):
        if not merged or c - merged[-1] > MIN_SHOT:
            merged.append(c)
    picked = []
    used = set()
    for i in range(n):
        mid = dur * (i + 0.5) / n
        # ⚠️ 切点**不复用**（20260828）。老版本撞车时退回段中点，而段中点往往还在
        #    同一个镜头里——于是同一镜头抽出两帧，样片上两格连标签都一样（那对狼，
        #    实测 PHASH 7.56，感知上差得远，去重根本抓不住）。改成挑"最近的、还没
        #    被用过的切点"：一个切点开一个镜头，不复用就等于每镜头最多一帧，
        #    确定性的，不靠阈值。切点真用光了才退回段中点（保住格数，同老版本用意）。
        avail = [t for t in merged if t not in used]
        if avail:
            c = min(avail, key=lambda t: abs(t - mid))
            used.add(c)
            # 让开切点那一帧本身：正好压在刀口上会抽到叠化/黑场的过渡帧
            c = min(c + CUT_LEAD, dur - TAIL_PAD)
        else:
            c = mid
        picked.append(c)
    return picked


def _grid(n, w, h, target=1560, budget=1568):
    """选列数：不留空格 > 缩放后每格尽量大。

    写死"横屏 4 列、竖屏 6 列"会排出 8 格挤成 6+2 这种断行。
    也不能只按总高卡上限：竖屏 6 格排成 6×1 每格才 260px，
    换 3×2 虽然超出 budget 会被整体缩掉，缩完每格仍有 440px——
    模型看到的是缩放后的像素，所以直接按那个数选。
    """
    ar = h / w
    best = None
    for c in range(1, min(8, n) + 1):
        cell = (target // c) & ~1          # 偶数宽，免得 ffmpeg 缩放挑剔
        rows = -(-n // c)
        tw, th = c * cell, rows * cell * ar
        eff = cell * min(1.0, budget / max(tw, th))
        # 先看清楚，再谈整齐：空格数最多也就 cols-1 个（最后一行缺几格），
        # 而把"零空格"设成绝对优先会让质数格数塌成 1 列——17 格排成 1×17、
        # 每格只剩 164px。宁可最后一行空两格。
        score = (-eff, c * rows - n)
        if best is None or score < best[0]:
            best = (score, c, cell, rows)
    return best[1], best[2], best[3]


MAX_CELLS = 40


MIN_CELL_PX = 350


def _eff_px(n, w, h, budget=1568):
    """一格在模型眼前的实际像素——样片超出 budget 会被整体缩掉，缩完才算数。"""
    cols, cell, rows = _grid(n, w, h)
    return cell * min(1.0, budget / max(cols * cell, rows * cell * h / w))


def _page_size(w, h):
    """一页放几格。格子太小就只能看出"有什么"、看不出"是什么"，宁可多出一张。

    实测每格的实际像素：横屏 16~28 格都稳定在 390px，32 格掉到 348，35 格以上只剩
    312；竖屏格子占地更高，16 格就掉到 220。所以按缩放后的像素卡，而不是按格数——
    同样 20 格，横屏 390px、竖屏只有 220px。
    """
    for n in range(MAX_CELLS, 5, -1):
        if _eff_px(n, w, h) >= MIN_CELL_PX:
            return n
    return 6


def _auto_grids(dur, n_cuts=0):
    """格数按时长打底，镜头密的片子按镜头数给够。

    只看时长会漏掉一大半：一条 172 秒的预告片有 35 个镜头，按时长只给 12 格，
    结果连出品方 logo 那一格都没抽到（黑场抽到了，logo 亮起的下一格没抽到），
    骑乘、沼泽、熔岩、深海全不见。反过来 20 秒的单镜头视频给 12 格也是浪费，
    后几格必然是同一个动作的复读。所以两者取大。

    上限 MAX_CELLS：再多每格就掉到 200px 出头，看不清了。
    """
    if dur <= 15:
        base = 6
    elif dur <= 45:
        base = 8
    elif dur <= 180:
        base = 12
    else:
        base = 16
    return max(base, min(n_cuts, MAX_CELLS)) if n_cuts else base


CUT_LEAD = 0.3      # 切点后让开这么久再抽（躲过渡帧）
MIN_SHOT = 1.0      # 比这更近的两刀当成同一个镜头（运镜误报）
TAIL_PAD = 0.5      # 抽帧最晚只到片尾前这么久。
                    # ⚠️ 别缩到 0.05：贴着 EOF seek，ffmpeg 抽不出帧，
                    # 报 "Could not open encoder before EOF" 并往 stderr 吐一堆，
                    # 那一格白丢（20260828 实测，B 站那条 13:53 的片子撞上了）。
BLANK_STD = 10.0    # 上 70% 灰度标准差低于它 = 纯色/黑屏。
                    # ⚠️ 别往上调：实测真黑屏是 5.2，而**昏暗但有内容**的真画面
                    # （夜戏荒原、暗色 UI）低到 17.5。20 会把它们一起误杀，
                    # 10 卡在这条 5.2→17.5 的空档中间。
PRE_CUT = 8.0       # 粗筛：签名距离超过它就不必再跑 PHASH 了


def _flatness(path):
    """画面有多"平"。**只看上 70%，避开烧死的字幕带**。

    实测（20260828 那张 20 格样片）：纯黑帧连字幕一起算标准差 19.9，跟真画面
    贴得很近；避开字幕带之后黑帧是 5.2、真画面 44 —— 差一个数量级，随便画条线都行。
    量不出来就返回一个大数，宁可漏判也别误杀。
    """
    r = subprocess.run(['magick', str(path), '-gravity', 'North',
                        '-crop', '100x70%+0+0', '+repage', '-colorspace', 'Gray',
                        '-format', '%[fx:standard_deviation*255]', 'info:'],
                       capture_output=True, text=True)
    try:
        return float((r.stdout or '').strip())
    except ValueError:
        return 999.0


def _sig(path):
    """8×8 灰度缩略图，只当**粗筛**签名用，不当判据。

    它跟 PHASH 不同序（实测有一对签名距离 0.50 而 PHASH 2.21，另一对 0.61 / 0.24），
    所以不能拿它判重复。但它足够挡掉"明显不同"的：20 格样片的 190 对里，
    10% 分位就已经是 21.3，取阈值 8 只放行 3% 进 PHASH——
    两两比对从 190 次子进程（15 秒）降到 6 次（0.5 秒），而真重复那对距离 0.61，
    离阈值有 13 倍余量。
    """
    r = subprocess.run(['magick', str(path), '-colorspace', 'Gray',
                        '-resize', '8x8!', '-depth', '8', 'txt:'],
                       capture_output=True, text=True)
    return [int(m) for m in re.findall(r'gray\((\d+)\)', r.stdout or '')]


def _sig_dist(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0      # 签名没拿到就别筛，让它进 PHASH
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _drop_dupes(made, keep, must=frozenset()):
    """按感知哈希顺序去重，只清掉近乎全等的帧。

    阈值取得保守（PHASH 0.35）是因为像素/感知距离只能回答"画面变了没有"，
    回答不了"意思重复了没有"：水面视频里波光让每帧的距离都很高，而室内固定
    机位上真实的表情变化距离却很低——两条真实样本的中位数几乎一样。
    所以这里只负责扔掉肉眼全等的，剩下的冗余交给 _auto_grids 用格数去控。
    """
    kept, sigs = [], []
    for t, q in made:
        sig = _sig(q)
        dup = False
        # ⚠️ 跟**每一张**已留下的比，不是只跟上一张（20260828）。老版本只比
        #    kept[-1]，隔得远的重复穿不过去：实测 0:28 和 4:45 是同一张图，
        #    PHASH 0.24 —— **本来就在 0.35 阈值以下**，只是中间隔了十几帧，
        #    从来没被拿来比过。阈值一个字没改，改的是比谁。
        #    先用 _sig 粗筛，别把 O(n²) 次 compare 真的跑出来。
        for ks, (_, kq) in zip(sigs, kept):
            if _sig_dist(sig, ks) > PRE_CUT:
                continue
            r = subprocess.run(['compare', '-metric', 'PHASH', str(kq),
                                str(q), 'null:'], capture_output=True, text=True)
            try:
                if float((r.stderr or '0').split()[0]) < 0.35:
                    dup = True
                    break
            except (ValueError, IndexError):
                pass
        if dup:
            q.unlink(missing_ok=True)
            continue
        kept.append((t, q))
        sigs.append(sig)
    n_kept = len(kept)
    if n_kept <= keep:
        return kept, n_kept
    # 20261029：字幕里说了「看这里 / 这张图」的那几帧（must）不参与均匀删减，名额先给它们，剩下的再均匀取。
    #   重复的照样会在上面被去掉——跟已留的一样，就说明画面已经在了。
    forced = set([i for i, (_, q) in enumerate(kept) if str(q) in must][:keep])
    # 20261029 改：不再均匀删。一张一张删「跟留下的里面最像的那张」——同一个大头复读六遍的先走，
    #   弹出来的截图、换了的页跟谁都不像，留下来。
    #   ⚠️ 时间覆盖底线：全片切成 keep//4 段，每段至少留一张——不然片头花哨快切会把名额吃光，后面安静的部分一张不剩。
    t0, t1 = kept[0][0], kept[-1][0]
    nb = max(2, keep // 4)
    bin_of = [min(nb - 1, int((t - t0) / max(t1 - t0, 1e-6) * nb)) for t, _ in kept]
    alive = set(range(n_kept))
    while len(alive) > keep:
        best, bd = None, None
        for i in alive:
            if i in forced or sum(1 for j in alive if bin_of[j] == bin_of[i]) <= 1:
                continue
            d = min(_sig_dist(sigs[i], sigs[j]) for j in alive if j != i)
            if bd is None or d < bd:
                best, bd = i, d
        if best is None:   # 剩下的全是受保护的，按均匀删收尾
            rest = sorted(alive - forced)
            best = rest[len(rest) // 2] if rest else sorted(alive)[-1]
        alive.discard(best)
    return [kept[i] for i in sorted(alive)], n_kept


def _fetch_video(url, tmp, browser):
    """下载视频到 tmp 并返回路径。只在这里处理站点差异，别在调用处各写一遍。"""
    # 20261024：本地已有的片子（film load 拉下来的整部电影）直接用，不再下一遍。
    #   读本地片子的硬字幕时没想起 -H，另写了一个糙版——该做的是让这里认本地文件。
    if os.path.isfile(url):
        return Path(url)
    order = ['none', browser] if re.search(ANON_FIRST, url) else [browser, 'none']
    err = ['']
    for b in dict.fromkeys(order):
        cmd = ['yt-dlp', '-f', FMT, '--no-playlist',
               '-o', str(tmp / '%(title)s.%(ext)s')]
        if b != 'none':
            cmd += ['--cookies-from-browser', b]
        r = subprocess.run(cmd + [url], capture_output=True, text=True)
        if r.returncode == 0:
            break
        err = (r.stderr or '').strip().splitlines()[-1:] or ['']
    else:
        sys.exit(f'视频下载失败：{err[0][:200]}')

    vids = [f for f in tmp.iterdir()
            if f.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.flv')]
    if not vids:
        sys.exit('下载完成但没找到视频文件')
    return max(vids, key=lambda f: f.stat().st_size)


def _parse_time(text):
    """'135' / '2:15' / '1:02:03' -> 秒"""
    try:
        parts = [float(x) for x in text.strip().split(':')]
    except ValueError:
        sys.exit(f'看不懂的时间点: {text}')
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def _label(t, dur):
    """长片也保留一位小数（20260828）。

    以前是 `int(t)`，截断到整秒。而选帧是**落在镜头切点上**的，切点很少落在整秒——
    19.9 秒那一帧标成 `0:19`，照着标签敲 `-z 0:19` 会 seek 到 19.0、落在上一个镜头里，
    于是"样片上是望远镜、单抽出来是火箭"。标签是给人抄回去用的，就得抄得回去。
    """
    return f'{t:.1f}s' if dur < 60 else f'{int(t) // 60:d}:{t % 60:04.1f}'


def _montage(tiles, cols, rows, cell, dest):
    args = []
    for lab, path in tiles:
        args += ['-label', lab, str(path)]
    font = _font()
    subprocess.run(['montage', *(['-font', font] if font else []),
                    '-background', '#1b1b1b', '-fill', '#f0f0f0',
                    '-pointsize', '16', '-tile', f'{cols}x{rows}',
                    '-geometry', f'{cell}x+5+5', *args, str(dest)], check=True)


def zoom(url, outdir, spec, browser):
    """把指定时间点抽成大图——样片挑出可疑的格子之后用这个看清楚。"""
    for exe in ('ffmpeg', 'ffprobe', 'montage', 'magick', 'compare'):
        if not shutil.which(exe):
            sys.exit(f'缺 {exe}（montage/magick/compare 来自 imagemagick: '
                     f'brew install imagemagick）')

    times = [_parse_time(x) for x in spec.split(',') if x.strip()]
    if not times:
        sys.exit('没给时间点，例如 -z 2:15 或 -z 2:11,2:13,2:15')

    tmp = Path(tempfile.mkdtemp(prefix='zoom-'))
    try:
        video = _fetch_video(url, tmp, browser)
        w, h, dur = _probe(video)
        over = [t for t in times if t > dur]
        if over:
            sys.exit(f'时间点超出片长（{int(dur) // 60}:{int(dur) % 60:02d}）: '
                     + ', '.join(f'{t:g}s' for t in over))

        # 单帧就给足分辨率；多帧仍按缩放后的有效大小排版
        cols, cell, rows = (1, 1400, 1) if len(times) == 1 else _grid(len(times), w, h)
        tiles = []
        for i, t in enumerate(sorted(times)):
            out = tmp / f'{i:03d}.jpg'
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(t), '-i', str(video),
                            '-frames:v', '1', '-vf', f'scale={cell}:-2', str(out)], check=False)
            if out.exists():
                tiles.append((_label(t, dur), out))
        if not tiles:
            sys.exit('一帧都没抽出来')

        first = f'{int(times[0]) // 60:02d}m{int(times[0]) % 60:02d}s'
        name = first if len(tiles) == 1 else f'{first}+{len(tiles) - 1}'
        dest = outdir / f'{re.sub(r"[/:\\]", "_", video.stem)[:70]}.zoom-{name}.jpg'
        _montage(tiles, cols, rows, cell, dest)
        landed(dest, f'{len(tiles)} 帧 / {cols}×{rows}，每格 {cell}px')
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def subs_hint(url, browser):
    """样片/大图跑完，提一句这条视频还有没有字幕。
    本文件开篇那句忠告是「字幕和画面各自会漏东西，要紧的视频两条都跑」——
    可跑完一条从不提醒另一条，那句忠告就落不了地。查不着就闭嘴，别成为故障源。"""
    if re.search(r'xiaohongshu\.com|xhslink', url):
        # yt-dlp 的 xiaohongshu extractor 里根本没有字幕代码，它说"没有"不是证据
        # （见本文件开篇）。这里不去猜有没有，只把路指出来。
        print('  小红书的字幕轨 yt-dlp 看不见，要文字就再跑一次不带 -F。')
        return
    try:
        tracks, native = _tracks(url, browser)
    except Exception:
        return
    if tracks:
        # ⚠️ 别把前几条轨名摊出来：YouTube 一条视频挂着 150+ 条**机翻**轨
        # （真 ASR 只有 <lang>-orig 那条），列出来的多半是 `ab`/`aa`/`en-uYU-…`
        # 这种噪音，读的人只会更慌。复用真正跑的时候那套挑轨逻辑，直接说会挑哪条。
        pick = _pick(tracks, bool(re.search(ZH_SITES, url)), native)
        if pick:
            print(f'  另有 {len(tracks)} 条字幕轨（会挑 {pick}），'
                  f'要文字就再跑一次不带 -F。')
        else:
            print(f'  另有 {len(tracks)} 条字幕轨，但挑不出中文/原生轨；'
                  f'要文字就用 -l 指定一条，或先跑 --list 看看。')
    elif re.search(r'bilibili\.com', url) and browser == 'none':
        # 空手查 B 站永远是空的（AI 字幕要登录，见本文件开篇），
        # 和小红书那条同一个道理：**查不到不等于没有**，别让沉默冒充结论。
        print('  B 站的 AI 字幕要登录才看得见，当前 --browser none 查不出来；'
              '要文字就加 --browser chrome 再跑一次不带 -F。')


# 字幕里的「视觉提示词」（20261029）：说到这些就是在指画面，那一刻强制抽一帧。
VISUAL_CUE = re.compile(r'(看这里|看这个|看一下|你看|大家看|注意看|请看|来看|我们看|如图|这张图|这个图|图上|图里|画面|屏幕上|界面上|右上角|左上角|右下角|左下角|'
                        r'演示|展示一下|给大家看|look at|as you can see|you can see|see here|right here|on (?:the )?screen|'
                        r'this (?:chart|graph|image|picture|screen|slide))', re.I)


def _change_events(video, dur, k):
    """画面突然变了的时刻（20261029）。
    起因：一条口播视频是真人出镜 + 不停弹出论文截图 / 表格，背景从头到尾不换，scene 只认出 1 个镜头，
    16 格里 6 格是同一个大头，截图全靠均匀取样碰运气。截图弹出来 = 画面一大块突然变了。
    每 0.5 秒一张 64×36 灰度图跟上一张比，比前后 10 秒的中位数高一大截就算一次；取变化后 0.7 秒（弹出动画走完）。
    返回变化最大的 ≤k 个，按时间排。"""
    W, H, fps = 64, 36, 2
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-vf', f'fps={fps},scale={W}:{H},format=gray',
                          '-f', 'rawvideo', 'pipe:1'], capture_output=True).stdout
    n = W * H
    fr = [raw[i * n:(i + 1) * n] for i in range(len(raw) // n)]
    if len(fr) < 4:
        return []
    d = [0.0] + [sum(abs(a - b) for a, b in zip(fr[i - 1], fr[i])) / (n * 255) for i in range(1, len(fr))]
    ev = []
    for i in range(1, len(d)):
        win = sorted(d[max(1, i - 20):i + 21])
        med = win[len(win) // 2]
        t = i / fps + .7
        if d[i] > max(.035, med * 3) and t < dur - TAIL_PAD and (not ev or t - ev[-1][0] > 1.5):
            ev.append((t, d[i]))
    return sorted(t for t, _ in sorted(ev, key=lambda x: -x[1])[:k])


def cue_at(t, back=4.0):
    """t 那一刻在说的那句；正在说的优先，否则取 back 秒内刚说完的。"""
    hit = ''
    for a, b, x in CUES:
        if a > t:
            break
        if a <= t <= b + .3 or t - b <= back:
            hit = x
    return hit


def _wrap(text, px, size=16, lines=2):
    """按格子宽度折成 ≤lines 行，超了加省略号。汉字算一个字宽，拉丁算半个多一点。"""
    per = max(8, int(px / size) - 1)
    out, cur, w = [], '', 0.0
    for ch in text:
        cw = 1.0 if ord(ch) > 0x2e80 else .56
        if w + cw > per:
            out.append(cur); cur, w = '', 0.0
            if len(out) == lines:
                out[-1] = out[-1][:-1] + '…'
                return '\n'.join(out)
        cur += ch; w += cw
    out.append(cur)
    return '\n'.join(x for x in out if x)


def contact_sheet(url, outdir, n_frames, browser):
    for exe in ('ffmpeg', 'ffprobe', 'montage', 'magick', 'compare'):
        if not shutil.which(exe):
            sys.exit(f'缺 {exe}（montage/magick/compare 来自 imagemagick: '
                     f'brew install imagemagick）')

    tmp = Path(tempfile.mkdtemp(prefix='sheet-'))
    try:
        video = _fetch_video(url, tmp, browser)

        w, h, dur = _probe(video)
        cuts = _scene_cuts(video)
        want = n_frames if n_frames > 0 else _auto_grids(dur, len(cuts))
        ads = bili_ads(url)
        if ads:
            main_dur, squeeze_cuts, real = _squeeze(ads, dur)
            times = [real(t) for t in _pick_times(squeeze_cuts(cuts), main_dur, want * 2)]
        else:
            times = _pick_times(cuts, dur, want * 2)
        # 字幕里指了画面的时刻：句子中点强制加一帧（3 秒内的算一处，最多占一半格子）
        forced_t = []
        for a_, b_, x in CUES:
            if VISUAL_CUE.search(x):
                m = min((a_ + b_) / 2, dur - TAIL_PAD)
                if not forced_t or m - forced_t[-1] > 3:
                    forced_t.append(m)
        forced_t = forced_t[:max(1, want // 2)]
        # 画面突然变了的时刻（截图弹出、换页）也进候选；最后挑格时「最像别人的先删」会帮它们留下来
        events = [t for t in _change_events(video, dur, want) if not (ads and _in_ads(t, ads))]
        cand = sorted(times + forced_t + events)
        times = [t for i, t in enumerate(cand) if i == 0 or t - cand[i - 1] > .5 or t in forced_t]

        # 每格宽度按版式反推，让样片长边落在 ~1560px：再大模型也会缩掉
        cell = ((1560 // (4 if w >= h else 6)) & ~1)   # 候选帧先按保守宽度抽
        fdir = tmp / 'f'
        fdir.mkdir()
        made, blank, must = [], [], set()
        for i, t in enumerate(times):
            # 文件名用序号保证唯一：短片每格不到一秒，按秒命名会撞名，
            # 而 ffmpeg 不加 -y 遇到同名会停在覆盖确认上，白丢一格
            out = fdir / f'{i:03d}.jpg'
            # 抽到纯黑/纯色就往后挪一点重抽（20260828）：讲解片里整段黑底配字幕
            # 很常见，那种格子在样片上就是一块白扔掉的黑。挪不出来才认。
            at, flat = t, 0.0
            for step in (0.0, 0.6, 1.4, 2.5):
                at = min(t + step, dur - TAIL_PAD)
                subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(at),
                                '-i', str(video), '-frames:v', '1',
                                '-vf', f'scale={cell}:-2', str(out)], check=False)
                if not out.exists():
                    break
                flat = _flatness(out)
                if flat >= BLANK_STD:
                    break
            # at 始终是**真正抽出这一帧的时刻**——挪了就得跟着挪，
            # 否则标签又和画面对不上（正是这次要修的那类毛病）
            if out.exists():
                (made if flat >= BLANK_STD else blank).append((at, out))
                if t in forced_t:
                    must.add(str(out))
        # 整片都是黑底字幕的极端情况：宁可给黑格也别给空样片
        if not made:
            made = blank

        # 按真实时间排序，不靠文件名字母序：超过 10 分钟后 "10m12s" 会排到
        # "1m57s" 前面，样片的时间线就乱了（补零也只是把问题推到 100 分钟）
        made = [(t, q) for t, q in sorted(made, key=lambda x: x[0]) if q.exists()]
        n_cand = len(made)
        shots, n_uniq = _drop_dupes(made, want, must)
        if not shots:
            sys.exit('一帧都没抽出来')
        title = re.sub(r'[/:\\]', '_', video.stem)
        mins = f'{int(dur) // 60}:{int(dur) % 60:02d}'
        # 没检出切点不是出错，是这片子本来就一个镜头拍到底
        shot_note = f'{len(cuts)} 个镜头' if cuts else '单镜头'
        print(f'  {title}  全片 {mins}，{shot_note}，'
              f'候选 {n_cand} → 去重剩 {n_uniq} → 取 {len(shots)}')

        # 格子太小就读不出材质，超了宁可多出一张
        per_page = _page_size(w, h)
        n_pages = -(-len(shots) // per_page)
        chunk = -(-len(shots) // n_pages)   # 均分，免得最后一页只剩两三格
        for i in range(n_pages):
            page = shots[i * chunk:(i + 1) * chunk]
            if not page:
                break
            cols, cell, rows = _grid(len(page), w, h)
            suffix = '' if n_pages == 1 else f'-{i + 1}'
            dest = outdir / f'{title}.sheet{suffix}.jpg'
            # 标签跟文件名解耦；短片精确到 0.1 秒，长片 m:ss 就够
            # 20261029：标签下面再印那一刻在说的话（折两行），格子和台词一眼对上
            _montage([(_label(t, dur) + ('\n' + _wrap(cue_at(t), cell) if cue_at(t) else ''), q) for t, q in page],
                     cols, rows, cell, dest)
            landed(dest, f'{len(page)} 格 / {cols}×{rows}，'
                        f'每格 {_eff_px(len(page), w, h):.0f}px')
        # 格子 ↔ 台词的文字版：读文字比在小图上辨字省事；★ = 字幕里指了画面、强制加的那几帧
        if CUES:
            print('  格子台词（★ 字幕里指了画面）：')
            for t, q in shots:
                print(f"    {'★' if str(q) in must else ' '} {_label(t, dur):>7}  {cue_at(t) or '—'}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 硬字幕 OCR（20261008）──────────────────────────────────────────────────
# 起因：方言视频——B 站 ai-zh 听不懂方言，又没有字幕轨，
# 可 UP 主烧进画面的字是人听懂了打的，是准的。字已经在画面上，只差读出来。
# 做法：只看底部一条 → 相邻帧比差异，字幕变了才算新的一句 → 每句取中间一帧 →
# macOS 自带 Vision 识别（visionocr，同目录 swift 编的）→ 连续相同的合并 → txt/srt。
# 同类的 pickcut.cn/ocr 闭源、源码里写明禁止复制抓取，这里只用公开思路，自己搭。
VISIONOCR = Path(__file__).resolve().parent / 'visionocr'
# 20261008 下午：拿 pickcut 用的 PP-OCRv6 tiny（4.4MB，Apache 2.0），拉下来对照——
# 69 条字幕 1.6s vs Vision 4.4s，而且 Vision 系统性吞"的"（"心心念念的老秦人的坟头"→"心心念念老秦人坟头"），
# PP 25 条只错一个字。改成默认用 PP；它只认单行，两行字幕/多行花字时 --ocr vision 切回去。
# 模型目录：$WATCHVIDEO_MODELS，默认 ~/.cache/watchvideo/models
MODELS = Path(os.environ.get('WATCHVIDEO_MODELS') or Path.home() / '.cache/watchvideo/models').expanduser()
PPOCR_PY = MODELS / '.venv/bin/python'
PPOCR = Path(__file__).resolve().parent / 'ppocr_rec.py'
PPOCR_DET = MODELS / 'ppocrv6_tiny_det/inference.onnx'


def _find_band(video, tmp, dur, n=24, pp=True):
    """先找字幕在哪（pickcut 也是这么干的：先框位置，再只认那一条）。
    均匀抽 n 帧整图 → Vision 找框 → 只留水平居中的框（排除角落水印/台标）→
    按框的垂直中线分桶，剔掉固定文字后有字帧数最多的那一档就是字幕带。返回 (y0, y1, x0, x1)，0–1，原点左上；找不到 None。"""
    d = tmp / 'probe'; d.mkdir(exist_ok=True)
    for k in range(n):
        t = dur * (k + 0.5) / n
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-ss', f'{t:.2f}', '-i', str(video),
                        '-frames:v', '1', '-vf', 'scale=960:-2', str(d / f'{k:02d}.jpg')], check=False)
    # 1107：找框跟着认字的引擎走——PP 装着就用 PP 的检测+识别（ppocr_rec.py --boxes，输出格式同 visionocr），
    #   不再非得有 Vision；-H 因此不再只能在 macOS 上跑。
    cmd = [str(PPOCR_PY), str(PPOCR), '--boxes'] if pp else [str(VISIONOCR), '--boxes']
    r = subprocess.run(cmd + [str(x) for x in sorted(d.glob('*.jpg'))], capture_output=True, text=True)
    hits = {}   # 桶 → [(帧, y0, y1, x0, x1, 去掉数字标点后的字)]
    anywhere = set()   # (字, 帧)，不管在画面哪儿——水印多半在角落，居中过滤会把它漏掉（比如角落里的频道名）
    for line in r.stdout.splitlines():
        o = json.loads(line)
        for x, y, w, h, txt in o['boxes']:
            core = re.sub(r'[\W\d_]', '', txt)
            if len(core) >= 3:
                anywhere.add((core, o['file']))
            if len(core) < 2 or abs(x + w / 2 - 0.5) > 0.2:
                continue
            y0, y1 = 1 - (y + h), 1 - y               # 换成原点左上
            hits.setdefault(round((y0 + y1) / 2 * 25), []).append((o['file'], y0, y1, x, x + w, core))
    if not hits:
        return None
    # 20261018 改打分。以前数「几帧有字」，可比分栏/台标/标题卡/UI 每帧都在，天然比字幕多
    #（1009 原神解说锁到 50–65% 的对话框、1018 LOL 锁到顶上比分栏）。现在两步：
    # ① 剥掉数字标点后，同一串字出现在 ≥1/4 帧里的算固定文字，剔掉（比分 13vs7→vs、标题、台标）；
    # ② 剩下的按**有字的帧数**算，不按句子数——原神那条有整屏的书页，一帧十几行每行都不同，
    #    按句子数书页赢；但书页只出现在部分画面里，字幕几乎贯穿全片（22/24 帧）。
    # 相邻两档合起来算（字幕高度可能正好压在档界上），帧取并集，所以多行文字叠不出分来。
    from collections import Counter
    seen = Counter(c for c, _ in {(p[5], p[0]) for v in hits.values() for p in v})
    static = {c for c, k in seen.items() if k >= max(3, n // 4)}
    wm = Counter(c for c, _ in anywhere)
    BAND_STATIC.clear(); BAND_STATIC.update(c for c, k in wm.items() if k >= max(3, n // 4))   # 给 _strip_watermark 用
    live = {k: [p for p in v if p[5] not in static] for k, v in hits.items()}

    def frames(k):
        return {p[0] for p in live.get(k, []) + live.get(k + 1, [])}
    best = max(live, key=lambda k: (len(frames(k)), k))   # 平手取靠下的
    if len(frames(best)) < max(3, n // 6):     # 不到 1/6 的帧有在变的字，多半不是字幕
        return None
    pts = live.get(best, []) + live.get(best + 1, [])
    pad = 0.015
    y0 = max(0, min(p[1] for p in pts) - pad); y1 = min(1, max(p[2] for p in pts) + pad)
    # x 范围只给"比较"用，不给 OCR 用：抽到的那几帧字幕可能都短，按它们框宽度裁 OCR 图，
    # 长句头尾会被切掉（实测漏 91 句）；但整条宽度拿来比，两边背景一动就切出一堆假段（OCR 86 秒）。
    x0 = max(0, min(p[3] for p in pts) - 0.03); x1 = min(1, max(p[4] for p in pts) + 0.03)
    return y0, y1, x0, x1


def _strip_frames(video, tmp, fps, band):
    """一遍解码出两路：小灰度图字节（比差异）+ 原尺寸 jpg（给 OCR）。
    20261008 第二版：以前两遍 ffmpeg、写四千张 png；现在 split 一次解码两路输出，jpg 小得多。"""
    y0, y1, x0, x1 = band
    crop = f"crop=iw:ih*{y1-y0:.4f}:0:ih*{y0:.4f}"                    # OCR：整条宽度
    mid = f"crop=iw*{x1-x0:.4f}:ih:iw*{x0:.4f}:0"                     # 比较：只看中间
    W, H = 384, 48   # 20261008：160×24 时等长同位的两句白字缩完亮点几乎重合，被并成一句（漏 40 句）
    d = tmp / 'strip'; d.mkdir(exist_ok=True)
    raw = subprocess.run(['ffmpeg', '-loglevel', 'error', '-i', str(video), '-filter_complex',
                          f'[0:v]fps={fps},{crop},split[a][b];[a]{mid},scale={W}:{H},format=gray[g]',
                          '-map', '[g]', '-f', 'rawvideo', 'pipe:1',
                          '-map', '[b]', '-q:v', '3', str(d / '%05d.jpg')],
                         capture_output=True).stdout
    n = W * H
    sigs = [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]
    pngs = sorted(d.glob('*.jpg'))
    m = min(len(sigs), len(pngs))
    return sigs[:m], pngs[:m], W


# 亮点掩码：bytes.translate 把每个像素变成 0/1，再整个转成大整数——
# 交并就是 & | 和 bit_count，全在 C 里跑。第一版逐像素 Python 循环，比较一段 23 分钟视频要几十秒。
_BRIGHT = bytes(1 if i > 190 else 0 for i in range(256))


def _mask(sig):
    return int.from_bytes(sig.translate(_BRIGHT), 'big')


def _ink(mask, n):
    """这一条里白字占比（‰）。"""
    return 1000 * mask.bit_count() / n


def _changed(A, B):
    """字有没有换：亮点的 Jaccard 距离 0–1。只比白字，背景画面动不算换句。"""
    u = (A | B).bit_count()
    return 0.0 if not u else 1 - (A & B).bit_count() / u


def _fmt_ts(t, srt=True):
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',') if srt else f'{h:d}:{m:02d}:{int(s):02d}'


# 20261103 一条户外动物视频一堆单字乱码：PP 只认字不找字，没字幕的镜头里给它草地/泥/水，
# 它也硬吐一两个字（m、一、口、Y）；有字幕时又会把字幕带两头的小人图标、半截水印认成字粘上去
#（「中体且极度自律公」「就是味道不大对華装」）。量了 246 句：硬吐的整句把握度 <0.4；粘上去的怪字
# 单字把握度几乎都 <0.35，句子里真字基本 >0.9。以前的视频几乎每个镜头都有字幕，所以没暴露。
PP_EDGE_MIN = 0.3    # 句子两头单字把握度低于它就剪掉（1104 试过 0.7：麦克风「具」0.58 是剪掉了，但真字「惩罚」的「罚」、「可能」的「可」也在 0.3~0.7，误伤更多，退回）
PP_GAP_K = 3         # 两头的字和相邻字的间距 > 中位间距×它（且 ≥6 步）就剪：背景里的东西（头发→「大」、窗框→「三」）隔着一大段空白
PP_LINE_MIN = 0.5    # 剪完整句平均把握度低于它就整句扔掉


def _clean_pp(text, pc, pt=None):
    """按 PP 的单字把握度剪两头怪字、扔掉硬吐出来的整句。剩下有效字不到 2 个也扔（单个「一」「、」）。
    pt（每个字在 CTC 时间轴上的位置≈横坐标）在的话，先按间距剪：OCR 喂的是整条宽度的字幕带，
    短字幕两边的背景（旁听席的头发、窗框）会被认成字粘在两头，它们和正文之间隔着一大段空白。"""
    if pt and len(pt) == len(text) == len(pc) and len(pt) >= 4:
        items = list(zip(text, pc, pt))
        gaps = sorted(b[2] - a[2] for a, b in zip(items, items[1:]))
        lim = max(6, PP_GAP_K * gaps[len(gaps) // 2])
        while len(items) > 2 and items[1][2] - items[0][2] > lim:
            items.pop(0)
        while len(items) > 2 and items[-1][2] - items[-2][2] > lim:
            items.pop()
        text = "".join(c for c, _, _ in items); pc = [p for _, p, _ in items]
    chars = list(zip(text, pc))
    while chars and (chars[0][1] < PP_EDGE_MIN or not chars[0][0].strip()):
        chars.pop(0)
    while chars and (chars[-1][1] < PP_EDGE_MIN or not chars[-1][0].strip()):
        chars.pop()
    if not chars or sum(p for _, p in chars) / len(chars) < PP_LINE_MIN:
        return ''
    # 两头孤立的标点/连字符也剪（「，、它是…」「一-再加两条」的「-」），句中的不动
    t = re.sub(r'^[\s，、。：；,.:;\-—…·]+|[\s，、：；,:;\-—·]+$', '', ''.join(c for c, _ in chars))
    return t if len(re.sub(r'[\W\d_]', '', t)) >= 2 else ''


BAND_STATIC = set()   # _find_band 抽 24 帧整图时、在 ≥1/4 帧里都出现的字（水印/台标/比分栏），已剥掉数字标点


def _strip_watermark(out):
    """画面里飘的水印（「小鱼动物园」）偶尔进字幕带，认得还很准，单字把握度剪不掉。
    只删 _find_band 在整图里见过的固定文字。⚠️ 别改回「字幕里重复 ≥3 次就当水印」：
    1103 第一版这么干，在游戏解说上把「一只小小虫」「尤克里里」「这一波我们」这些口头禅删了一大片。"""
    # 水印常被认掉头尾一个字（「小鱼动物」），所以水印的 4 字以上片段也算
    marks = sorted({w[i:j] for w in BAND_STATIC for i in range(len(w)) for j in range(i + 4, len(w) + 1)},
                   key=len, reverse=True)
    res = []
    for a, b, t in out:
        for m in marks:
            t = t.replace(m, '')
        t = t.strip()
        if len(re.sub(r'[\W\d_]', '', t)) >= 2:
            res.append([a, b, t])
    return res


def _drop_foreign(out):
    """中文字幕的片子里，一个汉字都没有的句子是画面上的英文（文档、论文列表、台标），不是字幕。1107 加：
    lindsay 1411 句里 26 句全是这种（ucnornis×8、Selected Publications…），另三条 B 站片子 0~2 句，一句真字幕都没有。
    九成以上的句子带汉字才动手，英文字幕的片子不受影响。"""
    cjk = lambda t: re.search(r'[\u4e00-\u9fff]', t)
    if not out or sum(1 for x in out if cjk(x[2])) < 0.9 * len(out):
        return out
    return [x for x in out if cjk(x[2])]


LONG_SEG_S = 4.0    # 一段超过这么久多半是几句被并成一段了（见 _units）
SUB_EVERY_S = 1.2   # 长段里每隔多久多认一帧


def _units(a, b, fps):
    """一段 [a, b] 帧拆成要认的单元 [(起帧, 止帧, 认哪一帧)]。
    1107：亮背景（浅色画面、白底文档、白墙）上，「亮点」大半是背景，换一句字幕只动其中一小块，
    _changed 判成没换句，几句并成一段、只认中间一帧——lindsay 4:42 一段 6 秒吞了三句，13 句漏认里 8 句是这样。
    一句字幕一般 2~3 秒，超过 LONG_SEG_S 的段每隔 SUB_EVERY_S 多认一帧；认出来一样的，后面连续合并会并掉。"""
    n = b - a + 1
    if n <= LONG_SEG_S * fps:
        return [(a, b, (a + b) // 2)]
    step = max(1, int(SUB_EVERY_S * fps))
    mids = list(range(a + step // 2, b + 1, step))
    edges = [a] + [(x + y) // 2 for x, y in zip(mids, mids[1:])] + [b + 1]
    return [(edges[i], edges[i + 1] - 1, m) for i, m in enumerate(mids)]


def hardsubs(url, outdir, browser, fmt, fps=3, region=None, ink_min=3, change=0.3, engine='pp'):
    use_pp = engine == 'pp' and PPOCR_PY.exists() and PPOCR.exists() and PPOCR_DET.exists()
    if engine == 'pp' and not use_pp:
        print(f'PP-OCR 环境不全（{MODELS}，见 README「装 -H」），退回 Vision', file=sys.stderr)
    if not use_pp and not VISIONOCR.exists():
        sys.exit(f'缺 {VISIONOCR}：cd {VISIONOCR.parent} && swiftc -O visionocr.swift -o visionocr'
                 f'（或者装 PP-OCR，见 README「装 -H」）')
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        import time; T = [time.time()]
        def lap(name): T.append(time.time()); print(f'  [{name} {T[-1]-T[-2]:.1f}s]', file=sys.stderr)
        print('下载视频…', file=sys.stderr)
        video = _fetch_video(url, tmp, browser); lap('下载')
        if region:
            band = (1 - region, 1.0, 0.0, 1.0)
        else:
            band = _find_band(video, tmp, _probe(video)[2] or 60, pp=use_pp)
            if band:
                print(f'字幕带：高度 {band[0]:.0%}–{band[1]:.0%}，宽 {band[2]:.0%}–{band[3]:.0%}', file=sys.stderr)
            else:
                band = (0.78, 1.0, 0.0, 1.0)
                print('没自动找到字幕位置，按底部 22% 来（不对就 --sub-region）', file=sys.stderr)
        lap('找位置')
        sigs, pngs, W = _strip_frames(video, tmp, fps, band); lap('解码')
        if not sigs:
            print('✗ 硬字幕：没抽到帧', file=sys.stderr); return 1
        N = len(sigs[0]); masks = [_mask(x) for x in sigs]
        # 切段：有字的连续帧、相邻变化小的算同一句
        segs, cur = [], None
        for i, mk in enumerate(masks):
            has = _ink(mk, N) >= ink_min
            if not has:
                if cur: segs.append(cur); cur = None
                continue
            if cur and _changed(masks[cur[1]], mk) < change:
                cur[1] = i
            else:
                if cur: segs.append(cur)
                cur = [i, i]
        if cur: segs.append(cur)
        if not segs:
            print('✗ 硬字幕：字幕带里没看到字（位置不对？试 --sub-region 0.35 指定底部比例）', file=sys.stderr)
            return 1
        lap('切段')
        units = [u for a, b in segs for u in _units(a, b, fps)]
        mids = [pngs[m] for _, _, m in units]
        print(f'{len(sigs)} 帧 → {len(segs)} 段候选（长段拆开共认 {len(units)} 帧），OCR 中…', file=sys.stderr)
        texts = {}
        # 1104：PP 那条先用检测模型找字幕框再认（ppocr_rec.py --det），不再整条宽度喂识别。
        # （1107 起检测模型缺了就整个退回 Vision，不再「整条喂识别」那条半残的路。）
        cmd = [str(PPOCR_PY), str(PPOCR), '--det'] if use_pp else [str(VISIONOCR)]   # 1107：检测模型现在是 PP 的必备件（找位置也靠它）
        for k in range(0, len(mids), 500):
            r = subprocess.run(cmd + [str(x) for x in mids[k:k + 500]],
                               capture_output=True, text=True)
            for line in r.stdout.splitlines():
                o = json.loads(line)
                texts[o['file']] = _clean_pp(o['text'], o['pc'], o.get('pt')) if 'pc' in o else o['text'].strip()
        lap('OCR')
        # 连续近似的合并（同一句被认成"的"有无两版），留最长那版；空的扔掉
        import difflib
        out = []
        for (a, b, _), mp in zip(units, mids):
            t = texts.get(str(mp), '')
            if not t: continue
            if out and difflib.SequenceMatcher(None, out[-1][2], t).ratio() >= 0.75:
                out[-1][1] = (b + 1) / fps
                if len(t) > len(out[-1][2]): out[-1][2] = t
            else:
                out.append([a / fps, (b + 1) / fps, t])
        out = _strip_watermark(out)
        out = _drop_foreign(out)
        title = re.sub(r'[\\/:*?"<>|]', '_', video.stem)[:80]
    ads = bili_ads(url)
    out = [x for x in out if not _in_ads((x[0] + x[1]) / 2, ads)]
    if not CUES:
        CUES.extend((a, b, t) for a, b, t in out)
    dest = outdir / f'{title}.硬字幕.{fmt}'
    with open(dest, 'w') as f:
        if fmt == 'srt':
            for i, (a, b, t) in enumerate(out, 1):
                f.write(f'{i}\n{_fmt_ts(a)} --> {_fmt_ts(b)}\n{t}\n\n')
        else:
            f.write('\n'.join(t for _, _, t in out) + '\n')
    print(f'✓ 硬字幕 {len(out)} 句 → {dest}')
    return 0


def main():
    p = argparse.ArgumentParser(description='\n\n'.join(__doc__.split('\n\n')[:2]),
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('url')
    p.add_argument('-l', '--lang', default=None, help='如 ai-zh / zh-CN / en')
    p.add_argument('-f', '--format', default='txt', choices=['txt', 'srt'])
    p.add_argument('-o', '--outdir', default=OUTDIR,
                   help='输出目录（默认 $WATCHVIDEO_OUT，没设就是当前目录；'
                        '现在是 %(default)s）')
    p.add_argument('--browser', default=os.environ.get('WATCHVIDEO_BROWSER', 'none'),
                   help='借哪个浏览器的 cookie: chrome/edge/safari/firefox/none'
                        '（B 站字幕必须登录，默认 none 在那里会拿不到）')
    p.add_argument('--list', action='store_true')
    # 20261008：没有字幕轨 / 自动字幕听不懂方言时，读画面上烧进去的字
    p.add_argument('-H', '--hardsub', action='store_true',
                   help='字幕那一半改成读烧在画面里的硬字幕（自动找位置 + OCR）；帧照抓，不要加 --no-frames')
    p.add_argument('--sub-region', type=float, default=None,
                   help='手动指定硬字幕在画面底部多大比例里（默认自动找位置）')
    p.add_argument('--ocr', default='pp', choices=['pp', 'vision'],
                   help='硬字幕识别引擎：pp=PP-OCRv6 tiny（默认，快且准，只认单行）/ vision=macOS 自带（多行）')
    # ⚠️ 20260829 改默认：以前 -F 是「抓帧**且不抓字幕**」，两者互斥。
    #    暴露问题的是一条几乎没有语音的视频——只拿到六行字幕，**就以为已经看完了**，
    #    其实画面里还有一整组镜头没看见。
    #    毛病不在多敲一次，在于**只给一半会让人以为完整了**。所以默认两个一起。
    p.add_argument('-F', '--frames', nargs='?', type=int, const=0, default=None,
                   metavar='N', help='印相样片的格数；省略则按时长自动定（帧本来就默认抓）')
    p.add_argument('--no-frames', action='store_true',
                   help='只要字幕，不抓帧。纯语音的东西（播客/讲座/访谈）抓帧是白抓，还慢')
    p.add_argument('-z', '--zoom', metavar='T[,T...]',
                   help='把指定时间点抽成大图，如 -z 2:15 或 -z 2:11,2:13,2:15')
    p.add_argument('--keep-ads', action='store_true',
                   help='B 站默认按空降助手的标注剃掉恰饭/自推段（字幕和样片都剃）；加这个就不剃')
    a = p.parse_args()
    global KEEP_ADS
    KEEP_ADS = a.keep_ads

    outdir = Path(a.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    expand = expand_xhs

    # -z 是「样片看过了，要看某一秒」——精确抽大图，独立动作，不顺带字幕
    if a.zoom:
        url = expand(a.url)
        rc = zoom(url, outdir, a.zoom, a.browser)
        if rc == 0:
            subs_hint(url, a.browser)
        return rc

    # 20261008：-H 第一版写成"不抓帧"，正好违反 0829 那条——只给一半会让人以为看完了。
    #   -H 只是把"字幕"那一半换成读画面上的字，帧照抓；不要帧就 --no-frames。
    if a.hardsub:
        rc_sub = hardsubs(expand(a.url), outdir, a.browser, a.format, region=a.sub_region, engine=a.ocr)
    else:
        handler = xiaohongshu if re.search(r'xiaohongshu\.com|xhslink', a.url) else generic
        rc_sub = handler(a.url, a.lang, a.format, outdir, a.browser, a.list)
    # --list 是查询动作（列有哪些字幕轨），不该顺带下一整个视频来抓帧
    if a.no_frames or a.list:
        return rc_sub

    rc_frame = contact_sheet(expand(a.url), outdir, a.frames if a.frames is not None else 0,
                             a.browser)
    # 字幕抓不到但帧抓到了也算有收成（小红书就是这样：yt-dlp 看不见它的字幕轨），
    # 反之亦然。两边都塌了才算失败。
    return 0 if (rc_sub == 0 or rc_frame == 0) else (rc_sub or rc_frame)


if __name__ == '__main__':
    sys.exit(main())

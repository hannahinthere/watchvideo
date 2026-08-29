#!/usr/bin/env python3
"""看视频：抓字幕，或抽帧拼成印相样片。都不留视频文件。

    watchvideo <url>                    # 抓字幕，默认 txt
    watchvideo <url> -F                 # 印相样片，格数按时长自动定
    watchvideo <url> -z 2:15            # 把某个时间点抽成大图看清楚
    watchvideo <url> -F 20              # 显式给格数
    watchvideo <url> --list             # 先看有哪些字幕轨
    watchvideo <url> -l en -f srt        # 指定语种 / 要时间轴

字幕和画面各自会漏东西：字幕漏掉画面里引用的图，画面漏掉说了什么，
而 ASR 还会听错专名（《牛来》被听成"牛奶"）。要紧的视频两条都跑。

大多数站走 yt-dlp。小红书是例外：它的视频确实带 srt 字幕轨
（sns-subtitle-s2.xhscdn.com），但 yt-dlp 的 xiaohongshu extractor
里完全没有字幕代码，只会回一句 "has no subtitles"——那不是证据，
所以这里自己从页面 JSON 里解析。

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


def report(path: Path, fmt: str):
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


def xiaohongshu(url, lang, fmt, outdir, browser, list_only):
    # 短链要先展开，xsec_token 在查询串里，丢了就取不到页面
    if 'xhslink' in url:
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        url = urllib.request.urlopen(req, timeout=30).geturl()

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
        sys.exit('这条笔记没有字幕轨（图文笔记、或视频没生成字幕）')
    subs = json.loads(blk)

    if list_only:
        print('可用字幕轨: ' + ', '.join(subs))
        return 0

    m = re.search(r'<title[^>]*>(.*?)</title>', page, re.S)
    title = re.sub(r'\s*-\s*小红书\s*$', '', html.unescape(m.group(1)).strip()) if m else 'xhs'
    title = re.sub(r'[/:\\]', '_', title)[:80]

    if lang:
        if lang not in subs:
            sys.exit(f'没有 {lang} 这条轨，有的是: {", ".join(subs)}')
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
    keys = list(info.get('subtitles') or {}) + list(info.get('automatic_captions') or {})
    tracks = [k for k in dict.fromkeys(keys) if k not in ('live_chat', 'danmaku')]
    return tracks, info.get('language')


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
    for f in written:
        report(f, fmt)
    return rc


# ---------- 印相样片（contact sheet）----------

def _font():
    """样片标签用的字体。ImageMagick 在有些机器上找不到默认字体
    （报 unable to read font ''），所以显式挑一个；实在没有就不传
    -font，交回给 ImageMagick 自己的 fontconfig。"""
    for f in ('/System/Library/Fonts/Supplemental/Arial.ttf',
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
    return [float(x) for x in r.stdout.split() if x.strip()]


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


def _drop_dupes(made, keep):
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
    idx = [round(i * (n_kept - 1) / (keep - 1)) for i in range(keep)]
    return [kept[i] for i in sorted(set(idx))], n_kept


def _fetch_video(url, tmp, browser):
    """下载视频到 tmp 并返回路径。只在这里处理站点差异，别在调用处各写一遍。"""
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
        times = _pick_times(cuts, dur, want * 2)

        # 每格宽度按版式反推，让样片长边落在 ~1560px：再大模型也会缩掉
        cell = ((1560 // (4 if w >= h else 6)) & ~1)   # 候选帧先按保守宽度抽
        fdir = tmp / 'f'
        fdir.mkdir()
        made, blank = [], []
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
        # 整片都是黑底字幕的极端情况：宁可给黑格也别给空样片
        if not made:
            made = blank

        # 按真实时间排序，不靠文件名字母序：超过 10 分钟后 "10m12s" 会排到
        # "1m57s" 前面，样片的时间线就乱了（补零也只是把问题推到 100 分钟）
        made = [(t, q) for t, q in sorted(made, key=lambda x: x[0]) if q.exists()]
        n_cand = len(made)
        shots, n_uniq = _drop_dupes(made, want)
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
            _montage([(_label(t, dur), q) for t, q in page], cols, rows, cell, dest)
            landed(dest, f'{len(page)} 格 / {cols}×{rows}，'
                        f'每格 {_eff_px(len(page), w, h):.0f}px')
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    p = argparse.ArgumentParser()
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
    # ⚠️ 20260829 她点单改默认：以前 -F 是「抓帧**且不抓字幕**」，两者互斥。
    #    暴露问题的是当晚那个水獭视频——我跑完拿到六行字幕，**以为已经看完了**，
    #    是她说"还有抓帧呢"我才知道那八格存在。
    #    毛病不在多敲一次，在于**只给一半会让人以为完整了**。所以默认两个一起。
    p.add_argument('-F', '--frames', nargs='?', type=int, const=0, default=None,
                   metavar='N', help='印相样片的格数；省略则按时长自动定（帧本来就默认抓）')
    p.add_argument('--no-frames', action='store_true',
                   help='只要字幕，不抓帧。纯语音的东西（播客/讲座/访谈）抓帧是白抓，还慢')
    p.add_argument('-z', '--zoom', metavar='T[,T...]',
                   help='把指定时间点抽成大图，如 -z 2:15 或 -z 2:11,2:13,2:15')
    a = p.parse_args()

    outdir = Path(a.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    def expand(u):
        # xhslink 短链不展开的话 xsec_token 会丢
        if 'xhslink' not in u:
            return u
        req = urllib.request.Request(u, headers={'User-Agent': UA})
        return urllib.request.urlopen(req, timeout=30).geturl()

    # -z 是「样片看过了，要看某一秒」——精确抽大图，独立动作，不顺带字幕
    if a.zoom:
        url = expand(a.url)
        rc = zoom(url, outdir, a.zoom, a.browser)
        if rc == 0:
            subs_hint(url, a.browser)
        return rc

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

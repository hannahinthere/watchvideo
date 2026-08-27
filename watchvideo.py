#!/usr/bin/env python3
"""看视频：抓字幕，或抽帧拼成印相样片。都不留视频文件。

    watchvideo <url>                    # 抓字幕，默认 txt
    watchvideo <url> -F                 # 印相样片，格数按时长自动定
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


def report(path: Path, fmt: str):
    out = srt_to_txt(path) if fmt == 'txt' else path
    n = len(out.read_text(encoding='utf-8').strip().splitlines())
    print(f'  -> {out.name}  ({n} 行)')


# ---------- 小红书 ----------

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
    if browser != 'none':
        cmd += ['--cookies-from-browser', browser]
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

    def attempt(langs):
        before = set(outdir.glob('*.srt'))
        rc = subprocess.run(base + [
            '--write-subs', '--write-auto-subs', '--convert-subs', 'srt',
            '--sub-langs', langs,
            '-o', str(outdir / '%(title)s.%(ext)s'), url]).returncode
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
    picked = []
    for i in range(n):
        mid = dur * (i + 0.5) / n
        c = min(cuts, key=lambda t: abs(t - mid))
        # 这一段没有属于自己的镜头（撞上了前一格选中的切点）就退回段中点，
        # 而不是把这一格丢掉——否则格数会随片子的剪辑风格莫名其妙地变少
        if picked and abs(c - picked[-1]) <= 0.5:
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
        score = (c * rows - n, -eff)
        if best is None or score < best[0]:
            best = (score, c, cell, rows)
    return best[1], best[2], best[3]


def _auto_grids(dur):
    """格数按时长走。短片就是没那么多信息——20 秒的单镜头视频给 12 格，
    后面几格必然是同一个动作的复读，再好的选帧也变不出新东西。"""
    if dur <= 15:
        return 6
    if dur <= 45:
        return 8
    if dur <= 180:
        return 12
    return 16


def _drop_dupes(made, keep):
    """按感知哈希顺序去重，只清掉近乎全等的帧。

    阈值取得保守（PHASH 0.35）是因为像素/感知距离只能回答"画面变了没有"，
    回答不了"意思重复了没有"：水面视频里波光让每帧的距离都很高，而室内固定
    机位上真实的表情变化距离却很低——两条真实样本的中位数几乎一样。
    所以这里只负责扔掉肉眼全等的，剩下的冗余交给 _auto_grids 用格数去控。
    """
    kept = []
    for t, q in made:
        if kept:
            r = subprocess.run(['compare', '-metric', 'PHASH', str(kept[-1][1]),
                                str(q), 'null:'], capture_output=True, text=True)
            try:
                if float((r.stderr or '0').split()[0]) < 0.35:
                    q.unlink(missing_ok=True)
                    continue
            except (ValueError, IndexError):
                pass
        kept.append((t, q))
    if len(kept) <= keep:
        return kept
    idx = [round(i * (len(kept) - 1) / (keep - 1)) for i in range(keep)]
    return [kept[i] for i in sorted(set(idx))]


def contact_sheet(url, outdir, n_frames, browser):
    for exe in ('ffmpeg', 'ffprobe', 'montage'):
        if not shutil.which(exe):
            sys.exit(f'缺 {exe}（montage 来自 imagemagick: brew install imagemagick）')

    tmp = Path(tempfile.mkdtemp(prefix='sheet-'))
    try:
        order = ['none', browser] if re.search(ANON_FIRST, url) else [browser, 'none']
        err = ''
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
        video = max(vids, key=lambda f: f.stat().st_size)

        w, h, dur = _probe(video)
        want = n_frames if n_frames > 0 else _auto_grids(dur)
        times = _pick_times(_scene_cuts(video), dur, want * 2)

        # 每格宽度按版式反推，让样片长边落在 ~1560px：再大模型也会缩掉
        cell = ((1560 // (4 if w >= h else 6)) & ~1)   # 候选帧先按保守宽度抽
        fdir = tmp / 'f'
        fdir.mkdir()
        made = []
        for i, t in enumerate(times):
            # 文件名用序号保证唯一：短片每格不到一秒，按秒命名会撞名，
            # 而 ffmpeg 不加 -y 遇到同名会停在覆盖确认上，白丢一格
            out = fdir / f'{i:03d}.jpg'
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(t), '-i', str(video),
                            '-frames:v', '1', '-vf', f'scale={cell}:-2',
                            str(out)], check=False)
            made.append((t, out))

        # 按真实时间排序，不靠文件名字母序：超过 10 分钟后 "10m12s" 会排到
        # "1m57s" 前面，样片的时间线就乱了（补零也只是把问题推到 100 分钟）
        made = [(t, q) for t, q in sorted(made, key=lambda x: x[0]) if q.exists()]
        shots = _drop_dupes(made, want)
        if not shots:
            sys.exit('一帧都没抽出来')
        cols, cell, rows = _grid(len(shots), w, h)
        title = re.sub(r'[/:\\]', '_', video.stem)
        dest = outdir / f'{title}.sheet.jpg'
        # 标签逐个指定，跟文件名解耦；短片精确到 0.1 秒，长片 m:ss 就够
        tiles = []
        for t, q in shots:
            lab = f'{t:.1f}s' if dur < 60 else f'{int(t) // 60:d}:{int(t) % 60:02d}'
            tiles += ['-label', lab, str(q)]
        font = _font()
        subprocess.run(['montage', *(['-font', font] if font else []),
                        '-background', '#1b1b1b', '-fill', '#f0f0f0',
                        '-pointsize', '16', '-tile', f'{cols}x{rows}',
                        '-geometry', f'{cell}x+5+5',
                        *tiles, str(dest)], check=True)
        mins = f'{int(dur) // 60}:{int(dur) % 60:02d}'
        print(f'  -> {dest.name}  ({len(shots)} 格 / {cols}×{rows}，全片 {mins}）')
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('url')
    p.add_argument('-l', '--lang', default=None, help='如 ai-zh / zh-CN / en')
    p.add_argument('-f', '--format', default='txt', choices=['txt', 'srt'])
    p.add_argument('-o', '--outdir', default=OUTDIR)
    p.add_argument('--browser', default=os.environ.get('WATCHVIDEO_BROWSER', 'none'),
                   help='借哪个浏览器的 cookie: chrome/edge/safari/firefox/none'
                        '（B 站字幕必须登录，默认 none 在那里会拿不到）')
    p.add_argument('--list', action='store_true')
    p.add_argument('-F', '--frames', nargs='?', type=int, const=0, default=None,
                   metavar='N', help='拼印相样片，不抓字幕；N 省略则按时长自动定格数')
    a = p.parse_args()

    outdir = Path(a.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    if a.frames is not None:   # 0 = 按时长自动定格数，不能用真值判断
        url = a.url
        if 'xhslink' in url:   # 短链不展开的话 xsec_token 会丢
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            url = urllib.request.urlopen(req, timeout=30).geturl()
        return contact_sheet(url, outdir, a.frames, a.browser)

    handler = xiaohongshu if re.search(r'xiaohongshu\.com|xhslink', a.url) else generic
    return handler(a.url, a.lang, a.format, outdir, a.browser, a.list)


if __name__ == '__main__':
    sys.exit(main())

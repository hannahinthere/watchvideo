#!/usr/bin/env python3
"""PP-OCRv6 tiny 识别（只认字、不找字），给 watchvideo -H 做对照实验（20261008）。
    $WATCHVIDEO_MODELS/.venv/bin/python ppocr_rec.py [--det] a.jpg b.jpg ...   → 每张一行 JSON {"file","text","conf","pc"}
    pc = 每个字的把握度（1103 加：watchvideo 拿它剪掉句子两头粘上的怪字）
    pt = 每个字在 CTC 时间轴上的位置（1104 加：≈横坐标，字幕两头隔着大空当的字是背景里的东西）
预处理照 PaddleOCR RecResizeImg：高 48，按比例缩宽，(x/255-0.5)/0.5，BGR，CHW。后处理 CTC：
argmax → 去连续重复 → 去 blank(0)。字典 = ["blank"] + yml 里的 character_dict (+ 空格)。"""
import os, sys, json, math
from pathlib import Path
import numpy as np, yaml, onnxruntime as ort
from PIL import Image

# 模型和 .venv 在 $WATCHVIDEO_MODELS（默认 ~/.cache/watchvideo/models，二进制不进 git），装法见 README
MODELS = Path(os.environ.get("WATCHVIDEO_MODELS") or Path.home() / ".cache/watchvideo/models").expanduser()   # 同 watchvideo.py
D = MODELS / "ppocrv6_tiny_rec"
cfg = yaml.safe_load(open(D / "inference.yml"))
post = cfg["PostProcess"]
chars = ["blank"] + list(post["character_dict"])
if post.get("use_space_char", True):
    chars.append(" ")
sess = ort.InferenceSession(str(D / "inference.onnx"), providers=["CPUExecutionProvider"])
inp = sess.get_inputs()[0].name


def prep(path, H=48):
    im = path if isinstance(path, Image.Image) else Image.open(path).convert("RGB")
    w, h = im.size
    nw = max(16, min(3200, math.ceil(H * w / h)))
    im = im.resize((nw, H), Image.BILINEAR)
    a = np.asarray(im, dtype=np.float32)[:, :, ::-1]          # RGB→BGR
    a = (a / 255.0 - 0.5) / 0.5
    return a.transpose(2, 0, 1)[None]


def decode(logits):
    idx = logits.argmax(-1); prob = logits.max(-1)
    out, ps, pt, last = [], [], [], -1
    for t, (i, p) in enumerate(zip(idx, prob)):
        if i != last and i != 0:
            out.append(chars[i] if i < len(chars) else ""); ps.append(p); pt.append(t)
        last = i
    return "".join(out), float(np.mean(ps)) if ps else 0.0, [round(float(p), 3) for p in ps], pt


# ─── 检测（1104 加，--det 打开）────────────────────────────────────────────────
# 以前整条字幕带直接喂识别：短字幕两边的背景（麦克风→「具」、旁听席头发→「大木」、
# 白底文档上的英文）跟着被认进来，文档上的字幕还会整句认不出被扔掉（Clancy 案 22:31 丢 4 句）。
# pickcut 同一个识别模型却干净，差在它每帧先用检测模型找字幕框。这里补上 PP-OCRv6 tiny det：
# 概率图 → 连通块 → 以「横跨中线、字最高」的块为种子，把同一行相邻的块并进来 → 外扩（DB 预测的是缩进去的核）→ 裁下来识别。
# 只认一行：双行字幕只拿到中线那行（原来整条喂进去双行也认不好，不算退步）。
DET = MODELS / "ppocrv6_tiny_det" / "inference.onnx"
_det = None


def _prob(im):
    global _det
    if _det is None:
        _det = ort.InferenceSession(str(DET), providers=["CPUExecutionProvider"])
    w, h = im.size
    W = max(32, int(round(w / 32)) * 32); H = max(32, int(math.ceil(h / 32)) * 32)
    a = np.asarray(im.resize((W, H), Image.BILINEAR), dtype=np.float32)[:, :, ::-1] / 255.0
    a = (a - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
    p = _det.run(None, {_det.get_inputs()[0].name: a.transpose(2, 0, 1)[None].astype(np.float32)})[0][0, 0]
    return np.asarray(Image.fromarray((p * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)) / 255.0


def _components(m):
    """行程编码连通域（上下相邻的行程算连通）。没有 cv2/scipy，纯 numpy+并查集。返回 [x0,y0,x1,y1,面积]。"""
    parent, boxes, prev = [], [], []
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for y in range(m.shape[0]):
        d = np.diff(np.concatenate([[0], m[y].astype(np.int8), [0]]))
        cur = []
        for a, b in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
            lab = len(parent); parent.append(lab); boxes.append([a, y, b, y + 1, b - a])
            for pa, pb, pl in prev:
                if pa < b and a < pb:
                    ra, rb = find(lab), find(pl)
                    if ra != rb: parent[ra] = rb
            cur.append((a, b, lab))
        prev = cur
    agg = {}
    for i, (x0, y0, x1, y1, ar) in enumerate(boxes):
        r = find(i); g = agg.get(r)
        agg[r] = [x0, y0, x1, y1, ar] if g is None else [min(g[0], x0), min(g[1], y0), max(g[2], x1), max(g[3], y1), g[4] + ar]
    return list(agg.values())


def subtitle_box(im, thr=0.3):
    w, h = im.size
    cs = [c for c in _components(_prob(im) > thr) if c[4] >= 20]
    if not cs:
        return None
    cx = w / 2
    cross = [c for c in cs if c[0] <= cx <= c[2]]
    # 种子：横跨中线的块里字最高的（字幕字号通常比画面里的文档/招牌大）；没有横跨的就取离中线最近的
    seed = max(cross, key=lambda c: (c[3] - c[1], c[4])) if cross else \
        min(cs, key=lambda c: min(abs(c[0] - cx), abs(c[2] - cx)))
    hh = seed[3] - seed[1]
    line, changed = [seed], True
    while changed:
        changed = False
        x0 = min(c[0] for c in line); x1 = max(c[2] for c in line)
        y0 = min(c[1] for c in line); y1 = max(c[3] for c in line)
        for c in cs:
            if c in line:
                continue
            ch = c[3] - c[1]
            inside = c[1] >= y0 - 0.25 * hh and c[3] <= y1 + 0.25 * hh      # 标点（引号/顿号）矮，但落在这一行里
            ov = min(y1, c[3]) - max(y0, c[1])
            gap = max(c[0] - x1, x0 - c[2])
            if gap < 1.2 * hh and ch <= 1.6 * hh and (inside or ov >= 0.5 * min(y1 - y0, ch)):
                line.append(c); changed = True
    x0 = min(c[0] for c in line); x1 = max(c[2] for c in line)
    y0 = min(c[1] for c in line); y1 = max(c[3] for c in line)
    e = int(0.6 * (y1 - y0)) + 2
    return [int(max(0, x0 - e)), int(max(0, y0 - e // 2)), int(min(w, x1 + e)), int(min(h, y1 + e // 2))]


use_det = "--det" in sys.argv[1:] and DET.exists()
for f in [a for a in sys.argv[1:] if a != "--det"]:
    box = None
    if use_det:
        im = Image.open(f).convert("RGB")
        box = subtitle_box(im)
        if box is None:
            print(json.dumps({"file": f, "text": "", "conf": 0.0, "pc": [], "pt": [], "box": None}, ensure_ascii=False))
            continue
        x = prep(im.crop(box))
    else:
        x = prep(f)
    y = sess.run(None, {inp: x})[0][0]
    t, c, pc, pt = decode(y)
    print(json.dumps({"file": f, "text": t, "conf": c, "pc": pc, "pt": pt, "box": box}, ensure_ascii=False))

// visionocr · 用 macOS 自带的 Vision 框架识别图片里的文字（20261008，给 watchvideo 硬字幕用）
// 不装任何东西：相册"拷贝图中文字"同一套引擎，中文准。
//   visionocr a.png b.png ...          → 每张一行 JSON：{"file": ..., "text": ..., "conf": ...}
//   visionocr --boxes a.png b.png ...  → 每张一行 JSON：{"file": ..., "boxes": [[x,y,w,h,"字"],...]}
//     （坐标 0–1，原点左下——Vision 的习惯；给找字幕位置用）
import Foundation
import Vision
import AppKit

func recognize(_ path: String) -> (String, Float) {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return ("", 0) }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    req.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    // 先按行（中线高度，差不到半个字高算同一行）上到下，同一行再左到右。
    // 只按 minY 排时，一行被拆成左右两块会倒过来："是非功过，留给后人评说"→"留给后人评说 是非功过，"
    let obs = (req.results ?? []).sorted { a, b in
        let h = min(a.boundingBox.height, b.boundingBox.height) / 2
        if abs(a.boundingBox.midY - b.boundingBox.midY) > h { return a.boundingBox.midY > b.boundingBox.midY }
        return a.boundingBox.minX < b.boundingBox.minX
    }
    var lines: [String] = []; var confs: [Float] = []
    for o in obs { if let c = o.topCandidates(1).first { lines.append(c.string); confs.append(c.confidence) } }
    return (lines.joined(separator: " "), confs.isEmpty ? 0 : confs.reduce(0, +) / Float(confs.count))
}

func boxes(_ path: String) -> [[Any]] {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return [] }
    let req = VNRecognizeTextRequest()
    // ⚠️ 20261018：以前用 .fast——fast 只认拉丁字母，中文字幕要么出乱码要么根本不出框，
    //    找字幕带时候选里全是比分/台标的数字。24 帧换 accurate 多花几秒，换来的是中文能进候选。
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = false
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    return (req.results ?? []).compactMap { o in
        guard let c = o.topCandidates(1).first else { return nil }
        let b = o.boundingBox
        return [b.minX, b.minY, b.width, b.height, c.string]
    }
}

var args = Array(CommandLine.arguments.dropFirst())
if args.first == "--boxes" {
    for path in args.dropFirst() {
        let obj: [String: Any] = ["file": path, "boxes": boxes(path)]
        if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) { print(s) }
    }
    exit(0)
}

for path in args {
    let (t, c) = recognize(path)
    let obj: [String: Any] = ["file": path, "text": t, "conf": c]
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) { print(s) }
}

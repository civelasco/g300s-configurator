// Draws the app icon and writes an .iconset folder:  swift make_icon.swift <out.iconset>
import AppKit

func draw(_ size: CGFloat) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = size / 1024
    let ctx = NSGraphicsContext.current!.cgContext
    ctx.scaleBy(x: s, y: s)

    // Background tile (Apple's icon grid: 824pt square, ~185pt corner radius).
    let tile = NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824), xRadius: 185, yRadius: 185)
    NSGradient(starting: NSColor(calibratedRed: 0.20, green: 0.22, blue: 0.27, alpha: 1),
               ending: NSColor(calibratedRed: 0.07, green: 0.08, blue: 0.10, alpha: 1))!.draw(in: tile, angle: -90)

    tile.addClip()  // keep the glow inside the tile

    // Mouse body (front at the top). y grows upwards in AppKit.
    let body = NSBezierPath()
    body.move(to: NSPoint(x: 512, y: 830))
    body.curve(to: NSPoint(x: 352, y: 640), controlPoint1: NSPoint(x: 400, y: 830), controlPoint2: NSPoint(x: 356, y: 760))
    body.curve(to: NSPoint(x: 330, y: 330), controlPoint1: NSPoint(x: 348, y: 520), controlPoint2: NSPoint(x: 318, y: 420))
    body.curve(to: NSPoint(x: 512, y: 190), controlPoint1: NSPoint(x: 342, y: 240), controlPoint2: NSPoint(x: 430, y: 190))
    body.curve(to: NSPoint(x: 694, y: 330), controlPoint1: NSPoint(x: 594, y: 190), controlPoint2: NSPoint(x: 682, y: 240))
    body.curve(to: NSPoint(x: 672, y: 640), controlPoint1: NSPoint(x: 706, y: 420), controlPoint2: NSPoint(x: 676, y: 520))
    body.curve(to: NSPoint(x: 512, y: 830), controlPoint1: NSPoint(x: 668, y: 760), controlPoint2: NSPoint(x: 624, y: 830))
    body.close()

    // Glow (the profile light).
    ctx.saveGState()
    ctx.setShadow(offset: .zero, blur: 70, color: NSColor(calibratedRed: 0.12, green: 0.53, blue: 1, alpha: 0.9).cgColor)
    NSColor(calibratedRed: 0.12, green: 0.53, blue: 1, alpha: 1).setStroke()
    body.lineWidth = 16
    body.stroke()
    ctx.restoreGState()
    NSGradient(starting: NSColor(calibratedWhite: 0.30, alpha: 1), ending: NSColor(calibratedWhite: 0.14, alpha: 1))!.draw(in: body, angle: -90)

    // Button split and wheel.
    NSColor(calibratedWhite: 0.05, alpha: 1).setStroke()
    let split = NSBezierPath()
    split.move(to: NSPoint(x: 512, y: 830)); split.line(to: NSPoint(x: 512, y: 560))
    split.move(to: NSPoint(x: 356, y: 560)); split.line(to: NSPoint(x: 668, y: 560))
    split.lineWidth = 10
    split.stroke()
    NSColor(calibratedWhite: 0.55, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 488, y: 660, width: 48, height: 110), xRadius: 24, yRadius: 24).fill()
    NSColor(calibratedRed: 0.12, green: 0.53, blue: 1, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 492, y: 600, width: 40, height: 40), xRadius: 10, yRadius: 10).fill()

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

let out = CommandLine.arguments[1]
try? FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let name = scale == 1 ? "icon_\(base)x\(base).png" : "icon_\(base)x\(base)@2x.png"
        let data = draw(CGFloat(base * scale)).representation(using: .png, properties: [:])!
        try! data.write(to: URL(fileURLWithPath: "\(out)/\(name)"))
    }
}
print("icons written")

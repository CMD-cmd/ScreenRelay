import AppKit

// Original vector artwork: Screen Relay. Coordinates use a 1024-point canvas.
let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let iconset = output.appendingPathComponent("AppIcon.iconset", isDirectory: true)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255,
            green: CGFloat((hex >> 8) & 255) / 255, blue: CGFloat(hex & 255) / 255, alpha: alpha)
}
func round(_ rect: NSRect, _ radius: CGFloat) -> NSBezierPath {
    NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
}
func gradient(_ path: NSBezierPath, _ start: UInt32, _ end: UInt32, _ angle: CGFloat = 90) {
    NSGradient(starting: color(start), ending: color(end))!.draw(in: path, angle: angle)
}
func artwork() {
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow(); shadow.shadowColor = color(0x06152C, 0.24)
    shadow.shadowBlurRadius = 22; shadow.shadowOffset = NSSize(width: 0, height: -10); shadow.set()
    gradient(round(NSRect(x: 64, y: 64, width: 896, height: 896), 202), 0x0A1731, 0x244A79)
    NSGraphicsContext.restoreGraphicsState()
    let rim = round(NSRect(x: 66, y: 66, width: 892, height: 892), 200)
    color(0xD8EBFF, 0.12).setStroke(); rim.lineWidth = 2; rim.stroke()

    // Display, stand and base form one clear silhouette at small sizes.
    gradient(round(NSRect(x: 372, y: 256, width: 92, height: 140), 22), 0x829EBE, 0xDDEBF8)
    gradient(round(NSRect(x: 284, y: 242, width: 270, height: 38), 19), 0x8DA9C9, 0xDFEDF9)
    NSGraphicsContext.saveGraphicsState()
    let screenShadow = NSShadow(); screenShadow.shadowColor = color(0x020D20, 0.36)
    screenShadow.shadowBlurRadius = 26; screenShadow.shadowOffset = NSSize(width: 0, height: -12); screenShadow.set()
    gradient(round(NSRect(x: 164, y: 354, width: 602, height: 408), 46), 0x95B5D7, 0xF3F9FF)
    NSGraphicsContext.restoreGraphicsState()
    gradient(round(NSRect(x: 187, y: 392, width: 556, height: 346), 26), 0x10284A, 0x2D6888)
    color(0xE5F5FF, 0.4).setFill(); NSBezierPath(ovalIn: NSRect(x: 459, y: 367, width: 9, height: 9)).fill()

    // A tablet in front of the display gives the remote destination priority.
    NSGraphicsContext.saveGraphicsState()
    let tabletShadow = NSShadow(); tabletShadow.shadowColor = color(0x020B1D, 0.45)
    tabletShadow.shadowBlurRadius = 28; tabletShadow.shadowOffset = NSSize(width: -12, height: -14); tabletShadow.set()
    gradient(round(NSRect(x: 548, y: 218, width: 310, height: 432), 48), 0xBAD4E9, 0xF7FCFF)
    NSGraphicsContext.restoreGraphicsState()
    gradient(round(NSRect(x: 567, y: 242, width: 272, height: 384), 30), 0x124967, 0x2F8BA4)
    color(0x0D324D, 0.8).setFill(); NSBezierPath(ovalIn: NSRect(x: 699, y: 631, width: 8, height: 8)).fill()
    color(0xC8F8FF, 0.6).setFill(); round(NSRect(x: 671, y: 252, width: 64, height: 5), 2.5).fill()

    // One continuous relay line, with a destination arrow that survives 32 px.
    let relay = NSBezierPath(); relay.move(to: NSPoint(x: 328, y: 548))
    relay.curve(to: NSPoint(x: 700, y: 428), controlPoint1: NSPoint(x: 496, y: 548), controlPoint2: NSPoint(x: 700, y: 658))
    relay.lineWidth = 28; relay.lineCapStyle = .round; relay.lineJoinStyle = .round
    color(0x75F4ED).setStroke(); relay.stroke()
    color(0xD7FFFA).setFill(); NSBezierPath(ovalIn: NSRect(x: 309, y: 529, width: 38, height: 38)).fill()
    let arrow = NSBezierPath(); arrow.move(to: NSPoint(x: 661, y: 468))
    arrow.line(to: NSPoint(x: 700, y: 425)); arrow.line(to: NSPoint(x: 739, y: 468))
    arrow.lineWidth = 26; arrow.lineCapStyle = .round; arrow.lineJoinStyle = .round
    color(0x91FFF5).setStroke(); arrow.stroke()
}
func render(_ size: Int) -> NSBitmapImageRep {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    NSGraphicsContext.current!.imageInterpolation = .high
    NSGraphicsContext.current!.cgContext.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    artwork(); NSGraphicsContext.restoreGraphicsState()
    return bitmap
}
for size in [16, 32, 128, 256, 512] {
    for retina in [false, true] {
        let pixels = size * (retina ? 2 : 1)
        let name = "icon_\(size)x\(size)\(retina ? "@2x" : "").png"
        try render(pixels).representation(using: .png, properties: [:])!.write(to: iconset.appendingPathComponent(name))
    }
}
try render(1024).representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent("AppIcon-1024.png"))
print("Screen Relay: 10 iconset representations and 1024 px preview rendered")

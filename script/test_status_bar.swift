import AppKit

@main
struct StatusBarTests {
    static func main() throws {
        var checks = 0
        func check(_ condition: Bool, _ message: String) { precondition(condition, message); checks += 1 }
        func status(_ mode: String, _ extra: [String: Any] = [:]) -> [String: Any] {
            ["mode": mode, "timestamp": 1000.0].merging(extra) { _, new in new }
        }
        func resolve(_ mode: String, _ connection: String = "connected", _ extra: [String: Any] = [:],
                     trusted: Bool = true, sleeping: Bool = false) -> StatusBarPresentation {
            StatusBarPresentation.resolve(connection: connection, trusted: trusted, sleeping: sleeping,
                status: status(mode, extra), now: 1000)
        }
        check(resolve("ACTIVE").kind == .remote, "active verified connection")
        check(resolve("ARMED", "disconnected").kind == .standby, "enabled standby")
        check(resolve("ARMED").kind == .switching, "connected is not yet active")
        check(resolve("ACTIVE", "disconnected").kind == .switching, "disconnect grace is not idle")
        check(resolve("ACTIVE", "unknown").kind == .checking, "uncertain active connection")
        check(resolve("ARMED", "unknown").kind == .checking, "uncertain waiting connection")
        check(resolve("ACTIVE", "connected", ["waiting_for": "virtual display mode"]).kind == .switching, "mode change is a transition")
        check(resolve("ARMED", "unknown", ["waiting_for": "trusted UU connection"]).kind == .checking, "unknown wait is not a screen change")
        check(resolve("RESTORING").kind == .switching, "restoration has transition icon")
        check(resolve("RESTORING", "connected", ["last_error": "Remote disconnected", "next_retry": 0.0]).kind == .switching, "normal restoration reason is not an alert")
        check(resolve("RESTORING", "connected", ["last_error": "Physical unavailable", "next_retry": 1100.0]).kind == .attention, "failed recovery backoff requires attention")
        for category in ["manual", "manual_stop"] {
            check(resolve("PAUSED", "connected", ["last_error": "Controller stopped", "recovery_context": ["category": category]]).kind == .paused, "manual pause is not a fault")
        }
        check(resolve("PAUSED").kind == .paused, "normal paused")
        check(resolve("DISABLED").kind == .paused, "not yet enabled")
        check(resolve("PAUSED", "connected", ["last_error": "Identity mismatch", "recovery_context": ["category": "safety_fault"]]).kind == .attention, "safety fault is visible")
        check(resolve("ACTIVE", trusted: false).kind == .attention, "permission loss")
        check(resolve("ACTIVE", trusted: false, sleeping: true).kind == .sleeping, "sleep is distinct from permission loss")
        check(resolve("UNKNOWN_MODE").kind == .checking, "unknown automation mode")
        check(resolve("ACTIVE", "connected", ["timestamp": 909.0]).kind == .attention, "stale controller cannot look healthy")
        check(resolve("ACTIVE", "connected", ["timestamp": 1006.0]).kind == .attention, "future controller time rejected")
        check(resolve("ACTIVE", "connected", ["timestamp": Double.nan]).kind == .attention, "invalid timestamp rejected")
        check(StatusBarPresentation.resolve(connection: "connected", trusted: true, sleeping: false, status: nil).kind == .checking, "startup waits for controller")
        func renderPNGData(_ image: NSImage) -> (Data, NSBitmapImageRep) {
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 40, pixelsHigh: 36,
                bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            NSGraphicsContext.current!.cgContext.scaleBy(x: 2, y: 2)
            image.draw(in: NSRect(x: 0, y: 0, width: 20, height: 18))
            NSGraphicsContext.restoreGraphicsState()
            return (rep.representation(using: .png, properties: [:])!, rep)
        }
        func alphaSum(_ rep: NSBitmapImageRep, xRange: ClosedRange<Int>, yRange: ClosedRange<Int>) -> Int {
            var total = 0
            for y in yRange {
                for x in xRange {
                    total += Int((rep.colorAt(x: x, y: y)?.alphaComponent ?? 0) * 255)
                }
            }
            return total
        }
        var renderedBitmaps: [StatusBarPresentation.Kind: NSBitmapImageRep] = [:]
        var uniquePNGs = Set<Data>()
        for kind in StatusBarPresentation.Kind.allCases {
            let presentation = StatusBarPresentation(kind: kind, detail: "状态详情")
            guard let img = presentation.image() else {
                preconditionFailure("status bar icon missing: \(kind)")
            }
            check(img.size == NSSize(width: 20, height: 18), "unified menu bar canvas size: \(kind)")
            check(img.isTemplate == true, "template supports light and dark menu bars")
            check(presentation.tooltip.contains(presentation.label) && presentation.tooltip.contains("状态详情"), "text available without opening menu")
            let (png, rep) = renderPNGData(img)
            renderedBitmaps[kind] = rep
            uniquePNGs.insert(png)
        }
        check(uniquePNGs.count == StatusBarPresentation.Kind.allCases.count, "every status bar state renders a distinct icon")
        // Verify remote screen interior is solid-illuminated while standby is light/open (never inverted)
        let remoteInterior = alphaSum(renderedBitmaps[.remote]!, xRange: 10...30, yRange: 10...22)
        let standbyInterior = alphaSum(renderedBitmaps[.standby]!, xRange: 10...30, yRange: 10...22)
        check(remoteInterior > standbyInterior * 3, "remote screen is visibly illuminated compared to standby")
        // Verify paused and sleeping dim the outer display frame compared to active states
        let standbyTopBezel = alphaSum(renderedBitmaps[.standby]!, xRange: 12...28, yRange: 5...7)
        let pausedTopBezel = alphaSum(renderedBitmaps[.paused]!, xRange: 12...28, yRange: 5...7)
        let sleepingTopBezel = alphaSum(renderedBitmaps[.sleeping]!, xRange: 12...28, yRange: 5...7)
        check(standbyTopBezel > pausedTopBezel && standbyTopBezel > sleepingTopBezel, "paused and sleeping dim the display bezel")
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("status.json")
        var observed: [String] = []
        let observer = StatusFileObserver(directory: folder) { observed.append((try? String(contentsOf: file, encoding: .utf8)) ?? "missing") }
        check(observer != nil, "native directory observer starts")
        func wait(until predicate: () -> Bool, timeout: Double = 2) -> Bool {
            let end = Date().addingTimeInterval(timeout)
            while !predicate(), Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
            return predicate()
        }
        try Data("ARMED".utf8).write(to: file, options: .atomic)
        check(wait { observed.last == "ARMED" }, "first atomic status write triggers event")
        try Data("ACTIVE".utf8).write(to: file, options: .atomic)
        check(wait { observed.last == "ACTIVE" }, "second atomic replacement stays observed")
        for index in 0..<20 { try Data("burst-\(index)".utf8).write(to: file, options: .atomic) }
        check(wait { observed.last == "burst-19" }, "burst resolves latest status")
        let idleCount = observed.count
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        check(observed.count == idleCount, "stable directory does not poll")
        observer?.stop(); observer?.stop()
        try Data("STOPPED".utf8).write(to: file, options: .atomic)
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        check(observed.count == idleCount, "stopped observer never calls back")
        check(StatusFileObserver(directory: folder.appendingPathComponent("missing"), onChange: {}) == nil, "missing directory fails without a retry loop")
        if let index = CommandLine.arguments.firstIndex(of: "--preview"), index + 1 < CommandLine.arguments.count {
            let path = CommandLine.arguments[index + 1]
            let kinds = StatusBarPresentation.Kind.allCases
            let names = ["待机", "远程中", "切换中", "应用中", "已暂停", "确认中", "需要处理", "睡眠"]
            let canvasWidth = kinds.count * 100
            let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: canvasWidth * 2, pixelsHigh: 260,
                bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
            NSGraphicsContext.current!.cgContext.scaleBy(x: 2, y: 2)
            NSColor(calibratedWhite: 0.96, alpha: 1).setFill(); NSRect(x: 0, y: 0, width: canvasWidth, height: 130).fill()
            for (index, kind) in kinds.enumerated() {
                let x = CGFloat(index) * 100
                let image = StatusBarPresentation(kind: kind, detail: "").image()!
                image.draw(in: NSRect(x: x + 36, y: 65, width: 28, height: 25))
                let text = names[index] as NSString
                let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.black]
                let width = text.size(withAttributes: attributes).width
                text.draw(at: NSPoint(x: x + (100 - width) / 2, y: 32), withAttributes: attributes)
            }
            NSGraphicsContext.restoreGraphicsState()
            try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
        }
        print("StatusBar: \(checks) checks passed")
    }
}

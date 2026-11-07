import AppKit

struct StatusBarPresentation: Equatable {
    enum Kind: String, CaseIterable { case standby, remote, switching, applying, paused, checking, attention, sleeping }
    let kind: Kind
    let detail: String
    var symbol: String {
        switch kind {
        case .standby: return "display"
        case .remote: return "checkmark.rectangle"
        case .switching: return "arrow.triangle.2.circlepath"
        case .applying: return "hourglass"
        case .paused: return "pause.circle"
        case .checking: return "questionmark.circle"
        case .attention: return "exclamationmark.triangle"
        case .sleeping: return "moon.zzz"
        }
    }
    var label: String {
        switch kind {
        case .standby: return "等待远程连接"
        case .remote: return "远程中"
        case .switching: return "正在切换屏幕"
        case .applying: return "正在应用设置"
        case .paused: return "自动接力已暂停"
        case .checking: return "正在确认状态"
        case .attention: return "需要处理"
        case .sleeping: return "Mac 睡眠中"
        }
    }
    var tooltip: String { "屏幕接力：\(label)\n\(detail)" }
    func image() -> NSImage? { Self.icons[kind] }

    private static let icons: [Kind: NSImage] = Dictionary(uniqueKeysWithValues: Kind.allCases.map { kind in
        (kind, renderIcon(for: kind))
    })

    private static func renderIcon(for kind: Kind) -> NSImage {
        let size = NSSize(width: 20, height: 18)
        let image = NSImage(size: size, flipped: false) { _ in
            guard let ctx = NSGraphicsContext.current?.cgContext else { return false }
            ctx.setShouldAntialias(true)
            ctx.setAllowsAntialiasing(true)

            func ink(_ alpha: CGFloat) -> NSColor { NSColor(calibratedWhite: 0, alpha: alpha) }

            let frameAlpha: CGFloat
            switch kind {
            case .paused, .sleeping: frameAlpha = 0.42
            case .checking: frameAlpha = 0.78
            case .standby: frameAlpha = 0.90
            case .remote, .switching, .applying, .attention: frameAlpha = 1.0
            }

            func drawDisplayFrame(alpha: CGFloat, filledScreen: Bool = false, innerTint: CGFloat = 0) {
                if innerTint > 0 {
                    let tintRect = NSRect(x: 3.5, y: 6.25, width: 13.0, height: 8.0)
                    let tintPath = NSBezierPath(roundedRect: tintRect, xRadius: 1.1, yRadius: 1.1)
                    ink(innerTint).setFill()
                    tintPath.fill()
                }

                ctx.saveGState()
                ctx.setAlpha(alpha)
                ctx.beginTransparencyLayer(auxiliaryInfo: nil)

                let screenStrokeRect = NSRect(x: 2.75, y: 5.5, width: 14.5, height: 9.5)
                let screenPath = NSBezierPath(roundedRect: screenStrokeRect, xRadius: 2.15, yRadius: 2.15)
                screenPath.lineWidth = 1.5
                NSColor.black.setStroke()
                screenPath.stroke()

                if filledScreen {
                    let fillRect = NSRect(x: 3.0, y: 5.75, width: 14.0, height: 9.0)
                    let fillPath = NSBezierPath(roundedRect: fillRect, xRadius: 1.5, yRadius: 1.5)
                    NSColor.black.setFill()
                    fillPath.fill()
                }

                NSColor.black.setFill()
                let neck = NSBezierPath()
                neck.move(to: NSPoint(x: 8.7, y: 4.9))
                neck.line(to: NSPoint(x: 11.3, y: 4.9))
                neck.line(to: NSPoint(x: 11.7, y: 2.6))
                neck.line(to: NSPoint(x: 8.3, y: 2.6))
                neck.close()
                neck.fill()

                let base = NSBezierPath(roundedRect: NSRect(x: 6.25, y: 1.5, width: 7.5, height: 1.3), xRadius: 0.65, yRadius: 0.65)
                base.fill()

                ctx.endTransparencyLayer()
                ctx.restoreGState()
            }

            func punchOut(_ drawBlock: () -> Void) {
                ctx.saveGState()
                ctx.setBlendMode(.clear)
                NSColor.black.setFill()
                NSColor.black.setStroke()
                drawBlock()
                ctx.restoreGState()
            }

            switch kind {
            case .standby:
                drawDisplayFrame(alpha: frameAlpha, filledScreen: false, innerTint: 0.14)

            case .remote:
                drawDisplayFrame(alpha: 1.0, filledScreen: true)
                punchOut {
                    let check = NSBezierPath()
                    check.move(to: NSPoint(x: 6.85, y: 10.1))
                    check.line(to: NSPoint(x: 9.15, y: 7.8))
                    check.line(to: NSPoint(x: 13.35, y: 12.2))
                    check.lineWidth = 1.85
                    check.lineCapStyle = .round
                    check.lineJoinStyle = .round
                    check.stroke()
                }

            case .switching:
                drawDisplayFrame(alpha: frameAlpha, filledScreen: false)
                ink(1.0).setStroke()
                let top = NSBezierPath()
                top.move(to: NSPoint(x: 6.2, y: 11.8))
                top.line(to: NSPoint(x: 13.5, y: 11.8))
                top.move(to: NSPoint(x: 11.8, y: 13.1))
                top.line(to: NSPoint(x: 13.6, y: 11.8))
                top.line(to: NSPoint(x: 11.8, y: 10.5))
                top.lineWidth = 1.3
                top.lineCapStyle = .round
                top.lineJoinStyle = .round
                top.stroke()

                let bot = NSBezierPath()
                bot.move(to: NSPoint(x: 13.8, y: 8.5))
                bot.line(to: NSPoint(x: 6.5, y: 8.5))
                bot.move(to: NSPoint(x: 8.2, y: 9.8))
                bot.line(to: NSPoint(x: 6.4, y: 8.5))
                bot.line(to: NSPoint(x: 8.2, y: 7.2))
                bot.lineWidth = 1.3
                bot.lineCapStyle = .round
                bot.lineJoinStyle = .round
                bot.stroke()

            case .applying:
                drawDisplayFrame(alpha: frameAlpha, filledScreen: false)
                ink(0.95).setStroke()
                ink(1.0).setFill()
                let track1 = NSBezierPath()
                track1.move(to: NSPoint(x: 6.1, y: 11.75))
                track1.line(to: NSPoint(x: 9.7, y: 11.75))
                track1.lineWidth = 1.35
                track1.lineCapStyle = .round
                track1.stroke()
                NSBezierPath(ovalIn: NSRect(x: 12.2 - 1.4, y: 11.75 - 1.4, width: 2.8, height: 2.8)).fill()

                NSBezierPath(ovalIn: NSRect(x: 7.8 - 1.4, y: 8.55 - 1.4, width: 2.8, height: 2.8)).fill()
                let track2 = NSBezierPath()
                track2.move(to: NSPoint(x: 10.3, y: 8.55))
                track2.line(to: NSPoint(x: 13.9, y: 8.55))
                track2.lineWidth = 1.35
                track2.lineCapStyle = .round
                track2.stroke()

            case .paused:
                drawDisplayFrame(alpha: frameAlpha, filledScreen: false)
                ink(0.92).setFill()
                NSBezierPath(roundedRect: NSRect(x: 7.55, y: 7.85, width: 1.75, height: 4.8), xRadius: 0.75, yRadius: 0.75).fill()
                NSBezierPath(roundedRect: NSRect(x: 10.7, y: 7.85, width: 1.75, height: 4.8), xRadius: 0.75, yRadius: 0.75).fill()

            case .checking:
                drawDisplayFrame(alpha: frameAlpha, filledScreen: false)
                ink(0.95).setFill()
                for cx in [6.75, 10.0, 13.25] {
                    NSBezierPath(ovalIn: NSRect(x: cx - 1.05, y: 9.2, width: 2.1, height: 2.1)).fill()
                }

            case .attention:
                drawDisplayFrame(alpha: 0.90, filledScreen: false)
                let cx: CGFloat = 15.45
                let topY: CGFloat = 11.85
                let botY: CGFloat = 3.95
                let halfW: CGFloat = 4.25
                punchOut {
                    ctx.beginPath()
                    let pTop = CGPoint(x: cx, y: topY + 1.15)
                    let pLeft = CGPoint(x: cx - halfW - 1.0, y: botY - 0.85)
                    let pRight = CGPoint(x: cx + halfW + 1.0, y: botY - 0.85)
                    ctx.move(to: CGPoint(x: cx, y: botY - 0.85))
                    ctx.addArc(tangent1End: pLeft, tangent2End: pTop, radius: 1.4)
                    ctx.addArc(tangent1End: pTop, tangent2End: pRight, radius: 1.4)
                    ctx.addArc(tangent1End: pRight, tangent2End: pLeft, radius: 1.4)
                    ctx.closePath()
                    ctx.fillPath()
                }
                ctx.saveGState()
                ink(1.0).setFill()
                let pTop = CGPoint(x: cx, y: topY)
                let pLeft = CGPoint(x: cx - halfW, y: botY)
                let pRight = CGPoint(x: cx + halfW, y: botY)
                ctx.beginPath()
                ctx.move(to: CGPoint(x: cx, y: botY))
                ctx.addArc(tangent1End: pLeft, tangent2End: pTop, radius: 1.0)
                ctx.addArc(tangent1End: pTop, tangent2End: pRight, radius: 1.0)
                ctx.addArc(tangent1End: pRight, tangent2End: pLeft, radius: 1.0)
                ctx.closePath()
                ctx.fillPath()
                ctx.restoreGState()
                punchOut {
                    NSBezierPath(roundedRect: NSRect(x: cx - 0.62, y: 6.75, width: 1.24, height: 2.7), xRadius: 0.62, yRadius: 0.62).fill()
                    NSBezierPath(ovalIn: NSRect(x: cx - 0.68, y: 4.9, width: 1.36, height: 1.36)).fill()
                }

            case .sleeping:
                drawDisplayFrame(alpha: frameAlpha, filledScreen: false)
                let moonImg = NSImage(size: size, flipped: false) { _ in
                    guard let mctx = NSGraphicsContext.current?.cgContext else { return false }
                    NSColor(calibratedWhite: 0, alpha: 0.92).setFill()
                    NSBezierPath(ovalIn: NSRect(x: 7.75, y: 7.6, width: 5.1, height: 5.1)).fill()
                    mctx.setBlendMode(.clear)
                    NSColor.black.setFill()
                    NSBezierPath(ovalIn: NSRect(x: 9.4, y: 8.45, width: 4.7, height: 4.7)).fill()
                    return true
                }
                moonImg.draw(in: NSRect(origin: .zero, size: size))
            }
            return true
        }
        image.accessibilityDescription = label(for: kind)
        image.isTemplate = true
        return image
    }

    private static func label(for kind: Kind) -> String {
        StatusBarPresentation(kind: kind, detail: "").label
    }
    static func resolve(connection: String, trusted: Bool, sleeping: Bool,
                        status: [String: Any]?, now: TimeInterval = Date().timeIntervalSince1970,
                        storage: StorageHealthSnapshot? = nil) -> Self {
        if sleeping { return Self(kind: .sleeping, detail: "唤醒后自动重新确认连接。") }
        if !trusted { return Self(kind: .attention, detail: "需要重新授权辅助功能。") }
        if let storage, storage.phase == "pressure" { return Self(kind: .attention, detail: storage.message) }
        guard let status else { return Self(kind: .checking, detail: "正在等待后台控制器。") }
        if status["backend_unavailable"] as? Bool == true {
            return Self(kind: .checking, detail: "后台正在重新启动，暂时保留当前画面。")
        }
        guard let stamp = status["timestamp"] as? Double, stamp.isFinite,
              now - stamp >= -5, now - stamp <= 90 else {
            return Self(kind: .attention, detail: "后台状态已过期，请点开查看。")
        }
        let mode = status["mode"] as? String ?? ""
        let error = status["last_error"] as? String ?? ""
        let category = (status["recovery_context"] as? [String: Any])?["category"] as? String ?? ""
        if status["exit_after_restore"] is String {
            let pending = status["exit_recovery_pending"] as? [String: Any]
            if pending?["cursor"] as? Bool == true {
                return Self(kind: .attention, detail: "光标还原尚未完成，正在重试；完成后会退出。")
            }
            return Self(kind: .switching, detail: "正在恢复显示器和光标，完成后会退出。")
        }
        if mode == "RESTORING" {
            if !error.isEmpty, (status["next_retry"] as? Double ?? 0) > now {
                return Self(kind: .attention, detail: "实体屏恢复尚未完成，正在等待重试。")
            }
            return Self(kind: .switching, detail: "正在恢复实体屏。")
        }
        if let error = status["configuration_error"] as? String, !error.isEmpty {
            return Self(kind: .attention, detail: StatusCopy.localize(error) ?? error)
        }
        if status["cursor_error"] is String {
            return Self(kind: .attention, detail: "远程光标调整尚未完成，正在重试；当前屏幕保持不变。")
        }
        if mode == "PAUSED" || mode == "DISABLED" {
            if !error.isEmpty, !["manual", "manual_stop"].contains(category) {
                return Self(kind: .attention, detail: "自动接力因异常暂停，请点开查看。")
            }
            return Self(kind: .paused, detail: "打开“自动接力”后，连接 UU 即自动切屏。")
        }
        guard mode == "ARMED" || mode == "ACTIVE" else {
            return Self(kind: .checking, detail: "正在确认后台状态。")
        }
        if let waiting = status["waiting_for"] as? String, !waiting.isEmpty,
           waiting != "trusted UU connection" {
            return Self(kind: .switching, detail: "正在调整或核验屏幕。")
        }
        let context = status["recovery_context"] as? [String: Any] ?? [:]
        let progress = ["manual", "manual_stop", "remote_disconnected", "sleep", "profile_change", "process_restart", "device_change"]
            .contains(category) && error == context["reason"] as? String
        if !error.isEmpty && !progress {
            return Self(kind: .attention, detail: StatusCopy.localize(error) ?? error)
        }
        if status["operation_pending"] as? Bool == true || status["cursor_pending"] as? Bool == true {
            return Self(kind: .applying, detail: status["cursor_pending"] as? Bool == true
                        ? "正在应用远程光标，等待实际执行完成。" : "设置已提交，正在等待后台完成。")
        }
        if connection == "unknown" { return Self(kind: .checking, detail: "正在确认 UU 连接，暂时保留当前桌面。") }
        if mode == "ACTIVE" && connection == "connected" {
            return Self(kind: .remote, detail: "正在使用设备虚拟屏。")
        }
        if mode == "ARMED" && connection == "disconnected" {
            return Self(kind: .standby, detail: "连接 UU 后自动切换到设备虚拟屏。")
        }
        if connection == "connected" || connection == "disconnected" {
            return Self(kind: .switching, detail: connection == "connected" ? "正在切换到设备虚拟屏。" : "连接已断开，准备恢复实体屏。")
        }
        return Self(kind: .checking, detail: "正在确认 UU 连接。")
    }
}

import AppKit
import SwiftUI

/// Restrained visual language: native controls everywhere, brand colour only on the device stage.
enum Brand {
    static let deep = Color(red: 0x0A / 255, green: 0x17 / 255, blue: 0x31 / 255)
    static let mid = Color(red: 0x24 / 255, green: 0x4A / 255, blue: 0x79 / 255)
    static let cyan = Color(red: 0x75 / 255, green: 0xF4 / 255, blue: 0xED / 255)
    static var stage: LinearGradient {
        LinearGradient(colors: [mid, deep], startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

/// Text drawn on neutral surfaces needs more contrast than decorative status dots.
enum TextInk {
    static let connected = Color(nsColor: NSColor(name: "ScreenRelayConnectedText") { appearance in
        if appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua {
            return NSColor(srgbRed: 0x5B / 255, green: 0xD4 / 255, blue: 0x7A / 255, alpha: 1)
        }
        return NSColor(srgbRed: 0x17 / 255, green: 0x6A / 255, blue: 0x35 / 255, alpha: 1)
    })
    static let neutral = Color(nsColor: .labelColor)
    static let hint = neutral.opacity(0.8)
}

enum Format {
    /// Plain digits: SwiftUI's Int interpolation would localize 1280 as "1,280".
    static func size(_ width: Int, _ height: Int) -> String { "\(String(width)) × \(String(height))" }
    static func size(_ resolution: String) -> String { resolution.replacingOccurrences(of: "x", with: " × ") }
    static func number(_ value: Double) -> String {
        value.rounded() == value ? String(Int(value)) : String(value)
    }
    static func scale(_ value: Double) -> String { number(value) + "×" }
}

extension StatusBarPresentation {
    var tint: Color {
        switch kind {
        case .remote: return .green
        case .switching, .applying: return .accentColor
        case .attention: return .orange
        case .standby, .checking, .paused, .sleeping: return .secondary
        }
    }
}

struct StatusPill: View {
    let label: String
    let tint: Color
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(tint).frame(width: 7, height: 7)
            Text(label).font(.callout).foregroundStyle(.primary)
        }
        .padding(.horizontal, 10).padding(.vertical, 4)
        .background(.quaternary.opacity(0.6), in: Capsule())
        .accessibilityElement(children: .combine)
    }
}

/// Catalog hardware and schematic opening geometry, shared by both native previews.
enum DeviceHardware: Equatable {
    case generic, tabletLongCamera, tabletShortCamera, tabletHomeButton, phoneHomeButton
    case wideNotch, compactNotch, dynamicIsland
    enum OpeningEdge: Equatable { case top, leading, bottom, trailing }

    static func forDescriptor(_ kind: String?, catalogID: String?) -> DeviceHardware {
        guard let kind else { return forCatalog(catalogID) }
        switch kind {
        case "tablet_long_camera": return .tabletLongCamera
        case "tablet_short_camera": return .tabletShortCamera
        case "tablet_home_button": return .tabletHomeButton
        case "phone_home_button": return .phoneHomeButton
        case "wide_notch": return .wideNotch
        case "compact_notch": return .compactNotch
        case "dynamic_island": return .dynamicIsland
        default: return .generic
        }
    }

    static func forCatalog(_ id: String?) -> DeviceHardware {
        switch id {
        case "ipad-pro-13-m4", "ipad-pro-11-m4", "ipad-air-13-m2": return .tabletLongCamera
        case "ipad-mini-a17-pro": return .tabletShortCamera
        case "iphone-12", "iphone-12-mini", "iphone-12-pro", "iphone-12-pro-max": return .wideNotch
        case "iphone-13", "iphone-13-mini", "iphone-13-pro", "iphone-13-pro-max",
             "iphone-14", "iphone-14-plus", "iphone-16e": return .compactNotch
        case "iphone-14-pro", "iphone-14-pro-max",
             "iphone-15", "iphone-15-plus", "iphone-15-pro", "iphone-15-pro-max",
             "iphone-16", "iphone-16-plus", "iphone-16-pro", "iphone-16-pro-max",
             "iphone-17", "iphone-17-pro", "iphone-17-pro-max", "iphone-air": return .dynamicIsland
        default: return .generic
        }
    }

    var hasHomeButton: Bool { self == .tabletHomeButton || self == .phoneHomeButton }
    var isCamera: Bool { self == .tabletLongCamera || self == .tabletShortCamera || hasHomeButton }

    /// Extend the body along the screen's short edges, retaining the physical display ratio.
    func bodySize(forDisplay display: CGSize) -> CGSize {
        guard hasHomeButton else { return display }
        let short = min(display.width, display.height)
        let landscape = display.width >= display.height
        return CGSize(width: display.width + short * (landscape ? 0.32 : 0.12),
                      height: display.height + short * (landscape ? 0.12 : 0.32))
    }

    func homeDisplayRect(in body: CGSize, landscape: Bool) -> CGRect? {
        guard hasHomeButton, body.width > 0, body.height > 0 else { return nil }
        let short = min(body.width, body.height) / 1.12
        return CGRect(origin: .zero, size: body)
            .insetBy(dx: short * (landscape ? 0.16 : 0.06),
                     dy: short * (landscape ? 0.06 : 0.16))
    }

    func homeButtonEdge(landscape: Bool) -> OpeningEdge? {
        hasHomeButton ? (landscape ? .trailing : .bottom) : nil
    }

    func homeButtonRect(in size: CGSize, landscape: Bool) -> CGRect? {
        guard hasHomeButton, size.width > 0, size.height > 0 else { return nil }
        let short = min(size.width, size.height) / 1.12
        let diameter = short * 0.09
        let center = short * 0.08
        return landscape
            ? CGRect(x: size.width - center - diameter / 2, y: (size.height - diameter) / 2,
                     width: diameter, height: diameter)
            : CGRect(x: (size.width - diameter) / 2, y: size.height - center - diameter / 2,
                     width: diameter, height: diameter)
    }
    func openingEdge(landscape: Bool) -> OpeningEdge? {
        switch self {
        case .generic: return nil
        case .tabletLongCamera: return landscape ? .top : .leading
        case .tabletShortCamera, .tabletHomeButton, .phoneHomeButton, .wideNotch, .compactNotch, .dynamicIsland:
            return landscape ? .leading : .top
        }
    }

    /// Phone geometry uses the full display; tablet geometry uses the outer bezel frame.
    func openingRect(in size: CGSize, landscape: Bool) -> CGRect? {
        guard size.width > 0, size.height > 0, let edge = openingEdge(landscape: landscape) else { return nil }
        let short = min(size.width, size.height)
        let span: CGFloat
        let depth: CGFloat
        let gap: CGFloat
        switch self {
        case .generic: return nil
        case .tabletLongCamera, .tabletShortCamera:
            span = max(1.5, short * 0.022); depth = span; gap = short * 0.022
        case .tabletHomeButton, .phoneHomeButton:
            span = short * 0.022; depth = span; gap = short * 0.06
        case .wideNotch: span = short * 0.43; depth = short * 0.075; gap = 0
        case .compactNotch: span = short * 0.32; depth = short * 0.075; gap = 0
        case .dynamicIsland: span = short * 0.29; depth = short * 0.075; gap = short * 0.025
        }
        return edge == .top
            ? CGRect(x: (size.width - span) / 2, y: gap, width: span, height: depth)
            : CGRect(x: gap, y: (size.height - span) / 2, width: depth, height: span)
    }
}

extension WorkbenchDevice {
    var frameHardware: DeviceHardware { DeviceHardware.forDescriptor(hardwareKind, catalogID: catalogID ?? id) }
}

extension WorkbenchDraft {
    var frameHardware: DeviceHardware { DeviceHardware.forDescriptor(hardwareKind, catalogID: catalogID) }
}

/// Physical device proportions with an optional centered desktop canvas.
struct DeviceFrame<Screen: View>: View {
    enum Style { case solid, draft, ghost }
    let width: Int
    let height: Int
    var style: Style = .solid
    var fit: CGSize
    var canvasWidth: Int? = nil
    var canvasHeight: Int? = nil
    var hardware: DeviceHardware = .generic
    @ViewBuilder var screen: () -> Screen

    var body: some View {
        let bodySize = hardware.bodySize(forDisplay: CGSize(width: max(width, 1), height: max(height, 1)))
        let ratio = bodySize.width / bodySize.height
        let h = min(fit.height, fit.width / ratio)
        let w = h * ratio
        let bezel = max(5, min(w, h) * 0.055)
        let interiorScale = max(0.1, 1 - bezel * 2 / max(min(w, h), 1))
        let homeDisplay = hardware.homeDisplayRect(in: CGSize(width: w, height: h), landscape: width >= height)
        let innerWidth = max(1, homeDisplay?.width ?? w * interiorScale)
        let innerHeight = max(1, homeDisplay?.height ?? h * interiorScale)
        let canvasRatio = CGFloat(max(canvasWidth ?? width, 1)) / CGFloat(max(canvasHeight ?? height, 1))
        let desktopHeight = min(innerHeight, innerWidth / canvasRatio)
        let desktopWidth = desktopHeight * canvasRatio
        ZStack {
            switch style {
            case .solid:
                RoundedRectangle(cornerRadius: bezel * 2.2, style: .continuous)
                    .fill(Color.black.opacity(0.55))
                    .overlay(RoundedRectangle(cornerRadius: bezel * 2.2, style: .continuous)
                        .strokeBorder(Color.white.opacity(0.28), lineWidth: 1))
            case .draft:
                RoundedRectangle(cornerRadius: bezel * 2.2, style: .continuous)
                    .strokeBorder(Brand.cyan, style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
            case .ghost:
                RoundedRectangle(cornerRadius: bezel * 2.2, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.22), lineWidth: 1)
            }
            if style == .ghost, canvasWidth != nil, canvasHeight != nil {
                Rectangle()
                    .strokeBorder(Color.white.opacity(0.28), lineWidth: 1)
                    .frame(width: desktopWidth, height: desktopHeight)
            }
            if style != .ghost {
                ZStack {
                    Color.black
                    Rectangle()
                        .fill(LinearGradient(colors: [Color.white.opacity(0.16), Color.white.opacity(0.06)],
                                             startPoint: .top, endPoint: .bottom))
                        .frame(width: desktopWidth, height: desktopHeight)
                    screen()
                        .frame(width: desktopWidth, height: desktopHeight)
                        .clipped()
                    if !hardware.isCamera {
                        hardwareOpening(in: CGSize(width: innerWidth, height: innerHeight))
                    }
                }
                .frame(width: innerWidth, height: innerHeight)
                .clipShape(RoundedRectangle(cornerRadius: bezel * 1.2, style: .continuous))
                if hardware.isCamera { hardwareOpening(in: CGSize(width: w, height: h)) }
                if let rect = hardware.homeButtonRect(in: CGSize(width: w, height: h), landscape: width >= height) {
                    Circle()
                        .fill(Color.black.opacity(0.7))
                        .overlay(Circle().strokeBorder(Color.white.opacity(0.4), lineWidth: 0.8))
                        .frame(width: rect.width, height: rect.height)
                        .position(x: rect.midX, y: rect.midY)
                        .frame(width: w, height: h)
                        .accessibilityHidden(true)
                }
            }
        }
        .frame(width: w, height: h)
    }

    @ViewBuilder private func hardwareOpening(in size: CGSize) -> some View {
        if let rect = hardware.openingRect(in: size, landscape: width >= height) {
            RoundedRectangle(cornerRadius: hardware.isCamera || hardware == .dynamicIsland
                             ? min(rect.width, rect.height) / 2 : min(rect.width, rect.height) * 0.35,
                             style: .continuous)
                .fill(hardware.isCamera ? Color.white.opacity(0.4) : Color.black)
                .overlay {
                    if hardware == .dynamicIsland {
                        RoundedRectangle(cornerRadius: min(rect.width, rect.height) / 2)
                            .strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5)
                    }
                }
                .frame(width: rect.width, height: rect.height)
                .position(x: rect.midX, y: rect.midY)
                .frame(width: size.width, height: size.height)
                .accessibilityHidden(true)
        }
    }
}

/// Display-size choice in the style of macOS Displays: larger text on the left, more space on the right.
struct ScaleTilePicker: View {
    struct Option: Identifiable, Equatable {
        let resolution: String
        let width: Int
        let height: Int
        let zoomPercent: Double
        var id: String { resolution }
    }
    let options: [Option]
    @Binding var selection: String
    var compact = false

    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: compact ? 6 : 10) {
                ForEach(Array(options.enumerated()), id: \.element.id) { index, option in
                    tile(option, index: index)
                }
            }
            HStack {
                Text("更大字体"); Spacer(); Text("更多空间")
            }
            .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func tile(_ option: Option, index: Int) -> some View {
        let selected = option.resolution == selection
        let ratio = CGFloat(option.width) / CGFloat(max(option.height, 1))
        let tileHeight: CGFloat = 40
        let tileWidth = min(compact ? 44 : 64, tileHeight * ratio)
        let fontSize = CGFloat(16 - index * 2)
        return Button { selection = option.resolution } label: {
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(selected ? Color.accentColor.opacity(0.14) : Color.secondary.opacity(0.08))
                .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .strokeBorder(selected ? Color.accentColor : Color.secondary.opacity(0.25),
                                  lineWidth: selected ? 2 : 1))
                .overlay(Text("Aa").font(.system(size: max(8, fontSize), weight: .medium))
                    .lineLimit(1).minimumScaleFactor(0.5)
                    .foregroundStyle(selected ? Color.accentColor : .secondary))
                .frame(width: tileWidth, height: min(tileHeight, tileWidth / ratio))
                .frame(maxWidth: .infinity)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(option.zoomPercent > 0.05
              ? "\(Format.size(option.resolution)) · 界面放大 \(String(format: "%.1f", option.zoomPercent))%"
              : Format.size(option.resolution))
        .accessibilityLabel(Format.size(option.resolution))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

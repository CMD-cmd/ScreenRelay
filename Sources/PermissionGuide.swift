import AppKit
import SwiftUI

/// Owns only the permission guide window. The injected model owns detection.
final class PermissionGuideWindowController: NSWindowController, NSWindowDelegate {
    private let model: AccessibilityPermissionModel
    private static let collapsedSize = NSSize(width: 520, height: 412)
    private static let expandedSize = NSSize(width: 520, height: 556)

    init(model: AccessibilityPermissionModel, expandTroubleshooting: Bool = false) {
        self.model = model
        let initialSize = expandTroubleshooting ? Self.expandedSize : Self.collapsedSize
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: initialSize),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "辅助功能设置"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 480, height: 390)
        super.init(window: window)
        let hosting = NSHostingController(rootView: PermissionGuideView(
            model: model,
            expandTroubleshooting: expandTroubleshooting,
            onToggleTroubleshooting: { [weak self] expanded in
                self?.resizeForTroubleshooting(expanded: expanded)
            },
            onDone: { [weak self] in
                self?.window?.performClose(nil)
            }
        ))
        window.contentViewController = hosting
        window.setContentSize(initialSize)
        window.delegate = self
        window.center()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func show() {
        model.beginConfirmation()
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        window?.makeFirstResponder(nil)
    }

    private func resizeForTroubleshooting(expanded: Bool) {
        guard let window else { return }
        let targetHeight = expanded ? Self.expandedSize.height : Self.collapsedSize.height
        let currentFrame = window.frame
        let contentRect = window.contentRect(forFrameRect: currentFrame)
        let delta = targetHeight - contentRect.height
        guard abs(delta) > 1 else { return }
        var nextContent = contentRect
        nextContent.size.height = targetHeight
        var nextFrame = window.frameRect(forContentRect: nextContent)
        nextFrame.origin.y = currentFrame.origin.y - (nextFrame.height - currentFrame.height)
        window.setFrame(nextFrame, display: true, animate: true)
    }

    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow, closing === window else { return }
        model.endConfirmation()
    }
}

struct PermissionGuideView: View {
    @ObservedObject var model: AccessibilityPermissionModel
    let onToggleTroubleshooting: (Bool) -> Void
    let onDone: () -> Void
    @State private var showTroubleshooting: Bool

    init(
        model: AccessibilityPermissionModel,
        expandTroubleshooting: Bool = false,
        onToggleTroubleshooting: @escaping (Bool) -> Void = { _ in },
        onDone: @escaping () -> Void
    ) {
        self.model = model
        self.onToggleTroubleshooting = onToggleTroubleshooting
        self.onDone = onDone
        _showTroubleshooting = State(initialValue: expandTroubleshooting)
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    header
                    stepsCard
                    statusCard
                    troubleshootingCard
                }
                .padding(.horizontal, 24)
                .padding(.top, 20)
                .padding(.bottom, 16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            footer
        }
        .foregroundStyle(.primary)
        .frame(minWidth: 480, idealWidth: 520, minHeight: 390)
        .onChange(of: showTroubleshooting) { _, expanded in
            onToggleTroubleshooting(expanded)
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 14) {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(headerTint.opacity(0.12))
                    .overlay(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(headerTint.opacity(0.22), lineWidth: 1)
                    )
                Image(systemName: headerSymbol)
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(headerTint)
            }
            .frame(width: 40, height: 40)
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 3) {
                Text("允许辅助功能访问")
                    .font(.system(size: 17, weight: .semibold))
                Text("用于读取 UU 的连接状态")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer(minLength: 8)

            StatusPill(label: badgeLabel, tint: badgeTint)
        }
    }

    private var stepsCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                stepBadge(1)
                VStack(alignment: .leading, spacing: 2) {
                    Text("打开辅助功能设置")
                        .font(.body.weight(.medium))
                    Text("系统设置 › 隐私与安全性 › 辅助功能")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 12)
                Button("打开辅助功能设置", action: model.openSettings)
                    .controlSize(.regular)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)

            Divider().padding(.leading, 48)

            HStack(alignment: .center, spacing: 12) {
                stepBadge(2)
                VStack(alignment: .leading, spacing: 2) {
                    Text("开启“屏幕接力 / UUDisplayAutomation”的开关")
                        .font(.body.weight(.medium))
                        .fixedSize(horizontal: false, vertical: true)
                    Text("在列表中找到当前应用并打开右侧权限开关")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)

            Divider().padding(.leading, 48)

            HStack(alignment: .center, spacing: 12) {
                stepBadge(3)
                VStack(alignment: .leading, spacing: 2) {
                    Text("返回后自动确认")
                        .font(.body.weight(.medium))
                    Text("授权后自动检查，必要时自动刷新检测服务")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.08), lineWidth: 1)
        )
    }

    private func stepBadge(_ index: Int) -> some View {
        ZStack {
            Circle()
                .fill(isGranted ? Color.green.opacity(0.15) : Color.accentColor.opacity(0.12))
            if isGranted {
                Image(systemName: "checkmark")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(TextInk.connected)
            } else {
                Text("\(index)")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(Color.accentColor)
            }
        }
        .frame(width: 22, height: 22)
        .accessibilityHidden(true)
    }

    private var statusCard: some View {
        HStack(alignment: .top, spacing: 10) {
            if isBusy {
                ProgressView()
                    .controlSize(.small)
                    .padding(.top, 2)
                    .accessibilityHidden(true)
            } else {
                Image(systemName: statusSymbol)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(statusIconTint)
                    .padding(.top, 1)
                    .accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(statusTitle)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(isGranted ? TextInk.connected : Color.primary)
                if !model.detail.isEmpty {
                    Text(model.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(statusBackground, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(statusBorder, lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
    }

    private var troubleshootingCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.16)) {
                    showTroubleshooting.toggle()
                }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "questionmark.circle")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(.secondary)
                    Text("开关已开但仍未通过？")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.primary)
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(showTroubleshooting ? 90 : 0))
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if showTroubleshooting {
                Divider().padding(.horizontal, 14)
                VStack(alignment: .leading, spacing: 9) {
                    Text("在辅助功能列表中选中旧条目，点“−”移除，再点“＋”添加当前应用并开启开关。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    HStack(alignment: .center, spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("当前应用")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                            Text(model.applicationURL.path)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 8)
                        Button("在 Finder 中显示应用", action: model.revealApplication)
                            .controlSize(.small)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 7, style: .continuous))

                    Text("如系统要求验证身份，请在系统窗口中完成。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor).opacity(0.65))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.07), lineWidth: 1)
        )
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Spacer()
            if isGranted {
                Button("完成", action: onDone)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            } else {
                Button("我已开启，重新检测", action: model.recheck)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(isBusy)
            }
        }
        .padding(.horizontal, 24)
        .frame(height: 50)
        .background(.bar)
    }

    private var isBusy: Bool {
        switch model.phase {
        case .checking, .refreshing: return true
        case .waiting, .granted, .issue: return false
        }
    }

    private var isGranted: Bool {
        if case .granted = model.phase { return true }
        return false
    }

    private var headerSymbol: String {
        switch model.phase {
        case .granted: return "checkmark.shield.fill"
        case .issue: return "exclamationmark.shield.fill"
        case .waiting, .checking, .refreshing: return "hand.raised.fill"
        }
    }

    private var headerTint: Color {
        switch model.phase {
        case .granted: return .green
        case .issue: return .orange
        case .waiting, .checking, .refreshing: return .accentColor
        }
    }

    private var badgeLabel: String {
        switch model.phase {
        case .checking: return "正在检查"
        case .waiting: return "待授权"
        case .refreshing: return "正在刷新"
        case .granted: return "已授权"
        case .issue: return "需处理"
        }
    }

    private var badgeTint: Color {
        switch model.phase {
        case .granted: return .green
        case .checking, .refreshing: return .accentColor
        case .waiting, .issue: return .orange
        }
    }

    private var statusTitle: String {
        switch model.phase {
        case .checking: return "正在检查授权…"
        case .waiting: return "等待开启辅助功能"
        case .refreshing: return "正在刷新检测服务…"
        case .granted: return "辅助功能已授权"
        case .issue: return "暂时无法确认授权"
        }
    }

    private var statusSymbol: String {
        switch model.phase {
        case .granted: return "checkmark.circle.fill"
        case .issue: return "exclamationmark.triangle.fill"
        case .waiting: return "lock.circle"
        case .checking, .refreshing: return "magnifyingglass"
        }
    }

    private var statusIconTint: Color {
        switch model.phase {
        case .granted: return TextInk.connected
        case .issue: return .orange
        case .waiting, .checking, .refreshing: return .secondary
        }
    }

    private var statusBackground: Color {
        switch model.phase {
        case .granted: return Color.green.opacity(0.10)
        case .issue: return Color.orange.opacity(0.10)
        case .waiting, .checking, .refreshing: return Color.secondary.opacity(0.08)
        }
    }

    private var statusBorder: Color {
        switch model.phase {
        case .granted: return Color.green.opacity(0.24)
        case .issue: return Color.orange.opacity(0.26)
        case .waiting, .checking, .refreshing: return Color.primary.opacity(0.06)
        }
    }
}

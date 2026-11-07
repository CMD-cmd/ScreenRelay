import SwiftUI

/// Uses the same grouped form and semantic colors as the workbench's general settings.
struct OfficeCursorSetupSection: View {
    @ObservedObject var model: OfficeCursorSetupModel
    @State private var sheet: OfficeCursorSetupSheet?

    var body: some View {
        Section {
            ForEach(model.statuses) { status in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(status.host.name).font(.headline)
                        Spacer(minLength: 8)
                        Text(status.title).foregroundStyle(.secondary).multilineTextAlignment(.trailing)
                    }
                    Text(status.detail).font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if status.needsOfficeQuitBeforeInstall {
                        Text("安装或更新前，请保存文稿并退出 \(status.host.name)。")
                            .font(.callout).fixedSize(horizontal: false, vertical: true)
                    }
                    ViewThatFits(in: .horizontal) {
                        HStack { hostActions(status) }
                        VStack(alignment: .leading, spacing: 8) { hostActions(status) }
                    }
                }
                .padding(.vertical, 4)
                .accessibilityElement(children: .contain)
            }
            if let error = model.error {
                Label {
                    Text(error).foregroundStyle(.primary).fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
            }
            HStack {
                if model.isBusy {
                    ProgressView().controlSize(.small)
                    Text("正在准备 Office 光标…").font(.callout).foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)
                Button("刷新状态") { model.refresh() }.disabled(model.isBusy)
            }
        } header: {
            Text("Office 光标")
        } footer: {
            Text("在 Excel 和 PowerPoint 中使用当前设备的光标大小。安装后，请分别在 Office 中启用加载项。")
        }
        .sheet(item: $sheet) { destination in
            switch destination {
            case .install(let host):
                OfficeCursorInstallSheet(model: model, host: host) { sheet = .guide(host) }
            case .guide(let host):
                OfficeCursorEnableSheet(model: model, host: host)
            }
        }
    }

    @ViewBuilder
    private func hostActions(_ status: OfficeCursorSetupStatus) -> some View {
        if status.canInstall {
            Button(status.phase == .recoveryNeeded ? "恢复安装…" : (status.hasFiles ? "更新…" : "安装…")) { sheet = .install(status.host) }
                .disabled(model.isBusy)
                .accessibilityLabel("\(status.hasFiles ? "更新" : "安装") \(status.host.name) 光标")
        }
        if status.hasFiles {
            Button("启用指引…") { sheet = .guide(status.host) }
                .accessibilityLabel("\(status.host.name) 加载项启用指引")
        }
    }
}

private enum OfficeCursorSetupSheet: Identifiable {
    case install(OfficeCursorHost), guide(OfficeCursorHost)
    var id: String {
        switch self {
        case .install(let host): return "install-" + host.id
        case .guide(let host): return "guide-" + host.id
        }
    }
}

private struct OfficeCursorInstallSheet: View {
    @ObservedObject var model: OfficeCursorSetupModel
    let host: OfficeCursorHost
    let installed: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var attempted = false

    private var status: OfficeCursorSetupStatus? { model.statuses.first { $0.host == host } }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("\(status?.phase == .recoveryNeeded ? "恢复安装" : (status?.hasFiles == true ? "更新" : "安装")) \(host.name) 光标")
                .font(.title2.bold())
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("此加载项会在 \(host.name) 内运行本机光标代码，让光标跟随当前设备设置的倍率。")
                    Text("安装会保留 Office 的全局宏安全设置。启用加载项时，请根据 Office 的提示确认权限。")
                    if status?.isRunning == true {
                        Label("请先保存文稿并退出 \(host.name)，然后重新检查。", systemImage: "exclamationmark.triangle")
                        Button("重新检查") { model.refresh() }.disabled(model.isBusy)
                    }
                    if attempted, let error = model.error {
                        Text(error).foregroundStyle(.primary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(model.isBusy)
                Spacer()
                if model.isBusy { ProgressView().controlSize(.small) }
                Button(status?.phase == .recoveryNeeded ? "恢复并准备加载项" : (status?.hasFiles == true ? "更新加载项" : "安装加载项")) {
                    attempted = true
                    Task { @MainActor in
                        await model.install(host)
                        if model.error == nil, model.statuses.first(where: { $0.host == host })?.hasFiles == true {
                            installed()
                        }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(model.isBusy || status?.isRunning != false || status?.canInstall != true)
            }
        }
        .padding(24)
        .frame(minWidth: 340, idealWidth: 480, maxWidth: 560, minHeight: 300, idealHeight: 360)
        .onAppear { model.refresh() }
    }
}

private struct OfficeCursorEnableSheet: View {
    @ObservedObject var model: OfficeCursorSetupModel
    let host: OfficeCursorHost
    @Environment(\.dismiss) private var dismiss
    private var status: OfficeCursorSetupStatus? { model.statuses.first { $0.host == host } }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("在 \(host.name) 中启用光标").font(.title2.bold())
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    guideStep(1, "打开 \(host.name)。")
                    guideStep(2, host == .excel
                        ? "在菜单栏选择「工具 → Excel 加载项…」（Tools → Excel Add-ins）。"
                        : "在菜单栏选择「工具 → PowerPoint 加载项…」（Tools → PowerPoint Add-ins）。")
                    guideStep(3, "点击「浏览…」或「添加…」（Browse / Add），选择 \(host.addinName)。")
                    Text("在文件选择窗口按 ⌘⇧G，粘贴下方复制的完整路径，再按回车即可定位文件。")
                        .font(.callout).foregroundStyle(.secondary)
                    ViewThatFits(in: .horizontal) {
                        HStack { fileActions }
                        VStack(alignment: .leading, spacing: 8) { fileActions }
                    }
                    guideStep(4, "勾选并启用加载项，按 Office 提示确认权限，然后返回这里检查状态。")
                    Text("Office 可能在重新打开或更新后再次询问权限，请按当次提示处理。")
                        .font(.callout).foregroundStyle(.secondary)
                    if let status {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(status.title).font(.headline)
                            Text(status.detail).foregroundStyle(.secondary)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("打开 \(host.name)") { model.openOffice(host) }
                    .disabled(status?.phase == .missingApp)
                Button("重新检查") { model.refresh() }.disabled(model.isBusy)
                Spacer()
                Button(status?.hasLoadedClient == true ? "完成" : "稍后设置") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(24)
        .frame(minWidth: 340, idealWidth: 500, maxWidth: 560, minHeight: 400, idealHeight: 530)
        .onAppear { model.refresh() }
    }

    @ViewBuilder private var fileActions: some View {
        Button("在 Finder 中显示") { model.reveal(host) }
        Button("复制完整路径") { model.copyPath(host) }
    }

    private func guideStep(_ number: Int, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text("\(number). ").monospacedDigit().foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text(text).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

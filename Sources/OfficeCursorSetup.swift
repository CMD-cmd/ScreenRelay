import AppKit
import Foundation
import CoreFoundation
import Combine

@MainActor
final class OfficeCursorSetupModel: ObservableObject {
    @Published private(set) var statuses: [OfficeCursorSetupStatus] = []
    @Published private(set) var isBusy = false
    @Published private(set) var error: String?

    private let installer: OfficeCursorInstaller?
    private let previewMode: Bool
    private let workspace: NSWorkspace
    private var workspaceObservers: [NSObjectProtocol] = []
    private var replyObserver: NSObjectProtocol?
    private var directoryObserver: StatusFileObserver?
    private let directory: URL?
    private let onPrepared: () -> Void
    private var clients: [OfficeCursorHost: OfficeCursorClientStatus] = [:]
    private var pendingRequest: String?
    private var users = 0
    private var runningEpochs: [OfficeCursorHost: OfficeCursorProcessEpoch] = [:]
    private var baselineRequests: [OfficeCursorHost: OfficeCursorProcessEpoch] = [:]

    private init(installer: OfficeCursorInstaller?, directory: URL?, preview: Bool, onPrepared: @escaping () -> Void = {}) {
        self.installer = installer; self.directory = directory; previewMode = preview
        self.onPrepared = onPrepared
        workspace = .shared
        reload()
    }
    static func live(directory: URL, onPrepared: @escaping () -> Void = {}) -> OfficeCursorSetupModel {
        let office = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Microsoft/UUDisplayAutomation", isDirectory: true)
        let payload = (Bundle.main.resourceURL ?? Bundle.main.bundleURL).appendingPathComponent("OfficeCursor", isDirectory: true)
        return .init(installer: OfficeCursorInstaller(directory: directory, officeDirectory: office, payloadDirectory: payload),
                     directory: directory, preview: false, onPrepared: onPrepared)
    }
    static func preview() -> OfficeCursorSetupModel { .init(installer: nil, directory: nil, preview: true) }

    func begin() {
        users += 1
        guard !previewMode, users == 1 else { return }
        replyObserver = DistributedNotificationCenter.default().addObserver(forName: OfficeCursorClientStatus.reply,
            object: nil, queue: .main) { [weak self] note in
                let object = note.object
                Task { @MainActor in self?.receive(object) }
            }
        for name in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            workspaceObservers.append(workspace.notificationCenter.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.refresh() }
            })
        }
        if let directory {
            directoryObserver = StatusFileObserver(directory: directory) { [weak self] in
                Task { @MainActor in self?.reload() }
            }
        }
        refresh()
    }
    func end() {
        users = max(0, users - 1)
        guard users == 0 else { return }
        if let replyObserver { DistributedNotificationCenter.default().removeObserver(replyObserver) }
        replyObserver = nil
        for item in workspaceObservers { workspace.notificationCenter.removeObserver(item) }
        workspaceObservers = []; directoryObserver?.stop(); directoryObserver = nil
        clients = [:]; runningEpochs = [:]; baselineRequests = [:]; pendingRequest = nil
    }
    func refresh() {
        guard !previewMode else { return }
        clients = [:]
        baselineRequests = [:]
        reload()
        guard users > 0 else { return }
        let nonce = UUID().uuidString; pendingRequest = nonce
        DistributedNotificationCenter.default().postNotificationName(OfficeCursorClientStatus.probe,
            object: nonce, userInfo: nil, deliverImmediately: true)
    }
    private func running(_ host: OfficeCursorHost) -> NSRunningApplication? {
        workspace.runningApplications.first { $0.bundleIdentifier == host.bundleID && !$0.isTerminated }
    }
    private func epoch(_ app: NSRunningApplication?) -> OfficeCursorProcessEpoch? {
        guard let app, let launched = app.launchDate else { return nil }
        return .init(pid: app.processIdentifier, launchedAt: launched.timeIntervalSince1970)
    }
    private func policyBlocked(_ host: OfficeCursorHost) -> Bool {
        for domain in ["com.microsoft.office", host.bundleID] {
            var valid = DarwinBoolean(false)
            let disabled = CFPreferencesGetAppBooleanValue("DisableVisualBasicExternalDylibs" as CFString, domain as CFString, &valid)
            if valid.boolValue && disabled { return true }
            let allDisabled = CFPreferencesGetAppBooleanValue("VisualBasicEntirelyDisabled" as CFString, domain as CFString, &valid)
            if valid.boolValue && allDisabled { return true }
        }
        return false
    }
    private func receive(_ object: Any?) {
        guard users > 0, let client = OfficeCursorClientStatus(object: object, now: Date().timeIntervalSince1970),
              let current = epoch(running(client.host)) else { return }
        if runningEpochs[client.host] != current { clients[client.host] = nil }
        runningEpochs[client.host] = current
        guard client.belongs(to: current, pendingRequest: pendingRequest, previous: clients[client.host]) else {
            // A module may load after the initial probe. Its first announcement
            // requests one fresh baseline for that epoch, rather than being trusted.
            if client.requestID.isEmpty, clients[client.host] == nil,
               client.pid == current.pid, client.timestamp >= current.launchedAt,
               baselineRequests[client.host] != current {
                baselineRequests[client.host] = current
                let nonce = pendingRequest ?? UUID().uuidString; pendingRequest = nonce
                DistributedNotificationCenter.default().postNotificationName(OfficeCursorClientStatus.probe,
                    object: nonce, userInfo: nil, deliverImmediately: true)
            }
            return
        }
        clients[client.host] = client
        reload()
    }
    private func reload() {
        if previewMode {
            statuses = [
                .init(host: .excel, phase: .notInstalled, title: "待安装", detail: "安装光标模块，再在 Excel 中启用加载项。",
                      appVersion: "16.114.1", isRunning: false, hasFiles: false, canInstall: true, scale: nil),
                .init(host: .powerPoint, phase: .prepared, title: "等待 Office 启用", detail: "文件已准备，完成启用后会自动确认加载状态。",
                      appVersion: "16.114.1", isRunning: false, hasFiles: true, canInstall: false, scale: nil)
            ]; return
        }
        guard let installer else { return }
        let payload = Result { try installer.payloadManifest() }
        let recovery = Result { try installer.pendingHost() }
        statuses = OfficeCursorHost.allCases.map { host in
            let appURL = workspace.urlForApplication(withBundleIdentifier: host.bundleID)
            let app = running(host)
            let current = epoch(app)
            if runningEpochs[host] != current { clients[host] = nil }
            runningEpochs[host] = current
            let version = appURL.flatMap { Bundle(url: $0)?.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String }
            func row(_ phase: OfficeCursorSetupPhase, _ title: String, _ detail: String,
                     files: Bool = false, install: Bool = false, scale: Double? = nil) -> OfficeCursorSetupStatus {
                .init(host: host, phase: phase, title: title, detail: detail, appVersion: version,
                      isRunning: app != nil, hasFiles: files, canInstall: install, scale: scale)
            }
            guard appURL != nil else { return row(.missingApp, "未检测到应用", "安装 \(host.name) 后，再准备光标模块。") }
            if policyBlocked(host) { return row(.blocked, "受 Office 策略限制", "当前策略禁止宏或外部本机库，请由设备管理员确认可用设置。") }
            if case .failure(let error) = recovery { return row(.failed, "恢复记录需要检查", localize(error)) }
            if case .success(let pending?) = recovery {
                let stopped = OfficeCursorHost.allCases.allSatisfy { running($0) == nil }
                return row(.recoveryNeeded, "安装未完成", "请保存并退出 Excel 和 PowerPoint，再恢复 \(pending.name) 的安装。", install: stopped)
            }
            let record: OfficeCursorInstallRecord?
            do { record = try installer.installed(host) }
            catch { return row(.failed, "安装记录需要检查", localize(error)) }
            let hasFiles = record?.filesValid == true
            if case .failure = payload { return row(.failed, "安装文件不可用", "请使用包含完整 Office 光标模块的屏幕接力安装包。", files: hasFiles) }
            let expected = try! payload.get()
            guard let record else { return row(.notInstalled, "待安装", "安装光标模块，再在 \(host.name) 中启用加载项。", install: app == nil) }
            guard record.filesValid else { return row(.failed, "文件需要检查", "已记录的模块文件已改变；请保留现有文件并检查安装记录。") }
            let expectedAddin = (expected.raw["hosts"] as? [String: [String: Any]])?[host.bundleID]?["sha256"] as? String
            if record.legacy || record.version != expected.version || record.moduleVersion != expected.moduleVersion
                || record.librarySHA256 != expected.librarySHA256 || record.addinSHA256 != expectedAddin {
                return row(record.legacy ? .legacy : .outdated, "可更新光标模块", "更新后可在这里自动确认 \(host.name) 的加载状态。", files: true, install: app == nil)
            }
            guard let client = clients[host], client.pid == app?.processIdentifier,
                  client.moduleVersion == expected.moduleVersion,
                  client.libraryURL.resolvingSymlinksInPath() == record.libraryURL.resolvingSymlinksInPath() else {
                if app == nil {
                    return row(.prepared, "文件已准备 · 应用未运行", "打开 \(host.name) 后会确认加载状态；首次使用可按指引启用加载项。", files: true)
                }
                return row(.waitingForOffice, "等待 Office 启用", "文件已准备，请按指引添加并启用加载项。", files: true)
            }
            if client.conflict || client.error == "hook_conflict" {
                return row(.failed, "需要关闭旧加载项", "请在 \(host.name) 中保留一份屏幕接力加载项，保存文稿后退出并重开。", files: true)
            }
            if !client.error.isEmpty { return row(.failed, "本机模块未启动", "当前 Office 或系统接口未能启用光标模块，请检查 Office 提示。", files: true) }
            guard client.installed else { return row(.prepared, "等待 Office 启用", "加载项已停用；可按指引重新启用。", files: true) }
            return row(client.enabled ? .active : .idle, client.enabled ? "已生效 · \(client.scale.formatted())×" : "已加载 · 当前未放大",
                       client.enabled ? "\(host.name) 已响应，使用当前设备的光标倍率。" : "\(host.name) 已响应，光标倍率随当前设备和连接状态调整。", files: true, scale: client.scale)
        }
    }
    func install(_ host: OfficeCursorHost) async {
        guard !isBusy else { return }
        error = nil
        if previewMode {
            statuses = statuses.map { row in
                guard row.host == host else { return row }
                return .init(host: host, phase: .prepared, title: "等待 Office 启用", detail: "文件已准备，请按指引完成启用。",
                             appVersion: row.appVersion, isRunning: false, hasFiles: true, canInstall: false, scale: nil)
            }; return
        }
        guard let installer else { return }
        guard !policyBlocked(host) else { error = "当前 Office 策略禁止此本机模块。"; return }
        isBusy = true; defer { isBusy = false; refresh() }
        do {
            if try installer.pendingHost() != nil {
                guard OfficeCursorHost.allCases.allSatisfy({ running($0) == nil }) else {
                    throw OfficeCursorInstallError.invalid("请先保存并退出 Excel 和 PowerPoint，再恢复未完成的安装。")
                }
                try installer.recoverPending()
            }
            _ = try installer.prepare(host, isOfficeRunning: { self.running(host) != nil })
            onPrepared()
        } catch { self.error = localize(error) }
    }
    func openOffice(_ host: OfficeCursorHost) {
        guard !previewMode, let url = workspace.urlForApplication(withBundleIdentifier: host.bundleID) else { return }
        workspace.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }
    func reveal(_ host: OfficeCursorHost) {
        guard !previewMode, let record = try? installer?.installed(host) else { return }
        workspace.activateFileViewerSelecting([record.addinURL])
    }
    func copyPath(_ host: OfficeCursorHost) {
        guard !previewMode, let record = try? installer?.installed(host) else { return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(record.addinURL.path, forType: .string)
    }
    private func localize(_ error: Error) -> String {
        let nsError = error as NSError
        if (nsError.domain == NSCocoaErrorDomain && nsError.code == CocoaError.fileWriteOutOfSpace.rawValue)
            || (nsError.domain == NSPOSIXErrorDomain && [Int(ENOSPC), Int(EDQUOT)].contains(nsError.code)) {
            return "空间不足，Office 模块尚未安装完成。腾出空间后可重试；若显示恢复安装，请先退出 Excel 和 PowerPoint。"
        }
        let message = error.localizedDescription
        if message.contains("Quit ") || message.contains("Office launched") { return "请先保存并退出 Office，再重试安装。" }
        if message.contains("checksum") || message.contains("manifest") || message.contains("payload") { return "安装文件校验未通过，请重新安装屏幕接力的完整安装包。" }
        if message.contains("unowned") || message.contains("Unsafe") || message.contains("changed") { return "目标位置已有其他文件或文件已改变，安装已停止；现有文件已保留。" }
        return "Office 光标安装未完成：\(message)"
    }
}

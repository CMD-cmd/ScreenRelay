import AppKit
import ApplicationServices
import Combine

/// Checks only the macOS authorization flag while the guide is open. UU itself
/// remains event-driven, and a fresh process is used only for a stale grant.
final class AccessibilityPermissionModel: ObservableObject {
    enum Phase { case checking, waiting, refreshing, granted, issue }
    @Published private(set) var phase: Phase = .checking
    @Published private(set) var detail = "正在检查当前应用的辅助功能权限…"
    let applicationURL: URL
    let directory: URL
    let build: String
    let ownerPID: Int
    let ownerStartedAt: Double
    private let trust: () -> Bool
    private let clock: () -> Double
    private let schedulePolling: Bool
    private var timer: Timer?
    private var deadline = 0.0
    private var visitedSettings = false
    private var probes = 0
    private var restartUsed = false
    private(set) var isPresenting = false
    private(set) var pending: PermissionTicket?
    private var expectedProbePID: Int?
    var onObservation: (Bool, Bool) -> Void = { _, _ in }
    var onRelaunch: (PermissionTicket) -> Bool = { _ in false }
    var onCancelRelaunch: () -> Void = {}
    var requestSystemSettings: () -> Void = {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }
    var revealCurrentApplication: (() -> Void)?
    /// The completion must describe the application actually returned by LaunchServices.
    var launchProbe: (PermissionTicket, @escaping (Int, URL?, Error?) -> Void) -> Void

    init(directory: URL, applicationURL: URL = Bundle.main.bundleURL,
         build: String = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "",
         ownerPID: Int = Int(getpid()), ownerStartedAt: Double = PermissionProcessIdentity.startedAt(Int(getpid())) ?? 0,
         trust: @escaping () -> Bool = { AXIsProcessTrusted() },
         clock: @escaping () -> Double = { Date().timeIntervalSince1970 }, schedulePolling: Bool = true) {
        self.directory = directory; self.applicationURL = applicationURL.standardizedFileURL.resolvingSymlinksInPath()
        self.build = build; self.ownerPID = ownerPID; self.ownerStartedAt = ownerStartedAt
        self.trust = trust; self.clock = clock; self.schedulePolling = schedulePolling
        launchProbe = { ticket, completion in
            let config = NSWorkspace.OpenConfiguration()
            config.activates = false; config.createsNewApplicationInstance = true
            config.arguments = ["--permission-probe", ticket.nonce]
            NSWorkspace.shared.openApplication(at: applicationURL, configuration: config) { app, error in
                DispatchQueue.main.async { completion(Int(app?.processIdentifier ?? 0), app?.bundleURL, error) }
            }
        }
    }
    deinit { timer?.invalidate() }

    func beginConfirmation() {
        if !isPresenting { probes = 0; visitedSettings = false; restartUsed = false }
        isPresenting = true; deadline = clock() + 180
        if pending != nil { startTimerIfNeeded(); return }
        phase = .checking
        check(forceReset: true)
        startTimerIfNeeded()
    }
    func endConfirmation() {
        isPresenting = false; stopTimer(); cancelPending()
    }
    func openSettings() {
        if !isPresenting { beginConfirmation() }
        visitedSettings = false
        requestSystemSettings()
    }
    func revealApplication() {
        if let revealCurrentApplication { revealCurrentApplication() }
        else { NSWorkspace.shared.activateFileViewerSelecting([applicationURL]) }
    }
    func settingsActivated() { if isPresenting { visitedSettings = true } }
    func applicationActivated() {
        check(forceReset: isPresenting && visitedSettings && phase != .granted)
        if isPresenting, phase != .granted {
            if clock() >= deadline { deadline = clock() + 180 }
            startTimerIfNeeded()
            if visitedSettings { requestFreshCheck() }
        }
    }
    func recheck() {
        if !isPresenting { beginConfirmation() }
        deadline = clock() + 180
        check(forceReset: true)
        if phase != .granted { requestFreshCheck(); startTimerIfNeeded() }
    }
    /// The regular heartbeat and surface-open events call this without starting a timer.
    func check(forceReset: Bool = false) {
        let granted = trust()
        // The detector's last published signal, not a second cached Boolean,
        // decides whether AX objects need rebuilding.
        onObservation(granted, forceReset)
        if granted {
            cancelPending()
            phase = .granted
            detail = "辅助功能权限已生效。连接检测已重新建立，自动接力会继续按原来的开启或暂停状态运行。"
            stopTimer()
        } else if pending == nil, phase != .issue {
            phase = .waiting
            detail = "请在系统设置中开启当前应用的辅助功能开关。完成后会自动确认。"
        }
    }
    func poll() {
        guard isPresenting else { stopTimer(); return }
        check()
        guard phase != .granted else { return }
        if let ticket = pending {
            if phase != .refreshing, ticket.phase == .probing, let pid = expectedProbePID,
               let report = PermissionHandoff.report(directory: directory, nonce: ticket.nonce),
               PermissionHandoff.accepts(report, for: ticket, expectedPID: pid, now: clock()) {
                if report.trusted {
                    var accepted = ticket; accepted.probePID = pid; pending = accepted
                    phase = .refreshing
                    detail = "系统已允许当前版本访问辅助功能。正在安全刷新检测服务，设备配置和未应用的更改会保留。"
                    if !onRelaunch(accepted), phase != .issue {
                        fail("暂时无法安全刷新检测服务。请稍后点击重新检测。")
                    }
                } else {
                    cancelPending()
                    phase = .issue
                    detail = "系统尚未允许当前版本访问辅助功能。请确认开关对应的是当前应用；开关已开仍未通过时，可展开下方排障步骤。"
                }
            } else if clock() - ticket.createdAt > (phase == .refreshing ? 49 : 12) {
                fail("检测服务暂未完成刷新，已保留当前桌面和配置。请稍后重新检测。")
            }
        }
        if clock() >= deadline {
            cancelPending(); phase = .issue
            detail = "还没有检测到权限开启。完成系统设置后，可点击重新检测。"
            stopTimer()
        }
    }
    private func startTimerIfNeeded() {
        guard schedulePolling, isPresenting, phase != .granted, timer == nil else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.poll() }
    }
    private func stopTimer() { timer?.invalidate(); timer = nil }
    var polling: Bool { timer != nil }
    private func requestFreshCheck() {
        guard !trust(), pending == nil, !restartUsed else { return }
        guard ownerStartedAt.isFinite, ownerStartedAt > 0 else {
            phase = .issue; detail = "暂时无法确认检测服务身份，请稍后重新检测。"; return
        }
        guard probes < 2 else {
            phase = .issue; detail = "本轮仍未确认授权。请确认系统设置中的当前应用开关，再重新打开此引导。"; return
        }
        probes += 1
        let ticket = PermissionTicket(nonce: UUID().uuidString, ownerPID: ownerPID, ownerStartedAt: ownerStartedAt,
            bundlePath: applicationURL.path, build: build, createdAt: clock(), phase: .probing,
            probePID: nil, workbenchState: nil, showWorkbench: false)
        do { try PermissionHandoff.write(ticket, directory: directory) }
        catch { fail("无法写入刷新记录：\(error.localizedDescription)。请检查磁盘空间后重试。"); return }
        pending = ticket; expectedProbePID = nil; phase = .checking
        detail = "正在用当前应用的新检测进程确认系统授权…"
        launchProbe(ticket) { [weak self] pid, url, error in
            guard let self, self.pending?.nonce == ticket.nonce else { return }
            guard error == nil, pid > 1, pid != self.ownerPID,
                  url?.standardizedFileURL.resolvingSymlinksInPath() == self.applicationURL else {
                self.fail("暂时无法启动权限检测，请稍后重试。"); return
            }
            self.expectedProbePID = pid
            self.poll()
        }
    }
    func markReady(workbenchState: Data?, showWorkbench: Bool) -> Bool {
        guard var ticket = pending, phase == .refreshing, let pid = expectedProbePID,
              let app = NSRunningApplication(processIdentifier: pid_t(pid)), !app.isTerminated,
              app.bundleURL?.standardizedFileURL.resolvingSymlinksInPath() == applicationURL else { return false }
        ticket.phase = .ready; ticket.probePID = pid
        ticket.workbenchState = workbenchState; ticket.showWorkbench = showWorkbench
        do { try PermissionHandoff.write(ticket, directory: directory); pending = ticket; return true }
        catch { fail("刷新记录未保存，已保留当前检测服务：\(error.localizedDescription)"); return false }
    }
    func resumed(_ ticket: PermissionTicket) {
        restartUsed = true; isPresenting = true; deadline = clock() + 180
        PermissionHandoff.remove(directory: directory, nonce: ticket.nonce)
        check(forceReset: true)
        if phase != .granted {
            phase = .issue
            detail = "检测服务已刷新，但系统尚未确认授权。请确认当前应用的辅助功能开关后重新检测。"
            startTimerIfNeeded()
        }
    }
    func cancelPending() {
        guard var ticket = pending else { return }
        pending = nil; expectedProbePID = nil
        ticket.phase = .cancelled
        try? PermissionHandoff.write(ticket, directory: directory)
        PermissionHandoff.remove(directory: directory, nonce: ticket.nonce)
        onCancelRelaunch()
    }
    func fail(_ message: String) {
        cancelPending(); phase = .issue; detail = message
    }
}

/// Launched by LaunchServices as another instance of the same installed bundle.
/// It never publishes session evidence or starts a controller before safe takeover.
enum PermissionProbeAgent {
    static func run(nonce: String, directory: URL, applicationURL: URL, build: String,
                    checkTrust: () -> Bool = { AXIsProcessTrusted() }) -> PermissionTicket? {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        // Complete the LaunchServices launch handshake before waiting for the
        // owner, so its openApplication completion can identify this process.
        app.finishLaunching()
        let applicationURL = applicationURL.standardizedFileURL.resolvingSymlinksInPath()
        guard let ticket = PermissionHandoff.ticket(directory: directory, nonce: nonce),
              PermissionHandoff.valid(ticket, bundlePath: applicationURL.path, build: build, now: Date().timeIntervalSince1970),
              ticket.phase == .probing, ticket.ownerPID != Int(getpid()),
              ticket.ownerPID <= Int(Int32.max),
              let owner = NSRunningApplication(processIdentifier: pid_t(ticket.ownerPID)),
              owner.bundleURL?.standardizedFileURL.resolvingSymlinksInPath() == applicationURL,
              let started = PermissionProcessIdentity.startedAt(ticket.ownerPID),
              abs(started - ticket.ownerStartedAt) < 0.1 else { return nil }
        let report = PermissionProbeReport(nonce: nonce, pid: Int(getpid()), trusted: checkTrust(),
            bundlePath: applicationURL.path, build: build, timestamp: Date().timeIntervalSince1970)
        do { try PermissionHandoff.write(report, directory: directory) } catch { return nil }
        if !report.trusted {
            RunLoop.current.run(until: Date().addingTimeInterval(1))
            return nil
        }
        var exited = false, cancelled = false
        let process = DispatchSource.makeProcessSource(identifier: pid_t(ticket.ownerPID), eventMask: .exit, queue: .main)
        process.setEventHandler { exited = true }; process.resume()
        guard let observer = StatusFileObserver(directory: directory, onChange: {
            if PermissionHandoff.ticket(directory: directory, nonce: nonce)?.phase == .cancelled ||
                PermissionHandoff.ticket(directory: directory, nonce: nonce) == nil { cancelled = true }
        }) else { process.cancel(); return nil }
        let limit = Date().addingTimeInterval(55)
        while !exited, !cancelled, Date() < limit {
            RunLoop.current.run(until: min(limit, Date().addingTimeInterval(1)))
            if owner.isTerminated { exited = true }
        }
        process.cancel(); observer.stop()
        guard exited, !cancelled, let ready = PermissionHandoff.ticket(directory: directory, nonce: nonce),
              PermissionHandoff.valid(ready, bundlePath: applicationURL.path, build: build, now: Date().timeIntervalSince1970),
              ready.phase == .ready, ready.probePID == Int(getpid()), ready.ownerPID == ticket.ownerPID,
              ready.ownerStartedAt == ticket.ownerStartedAt else { return nil }
        return ready
    }
}

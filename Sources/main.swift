import AppKit
import ApplicationServices
import Carbon
import Darwin
import OSLog
import SwiftUI

let sessionLogger = Logger(subsystem: "local.codex.UUDisplayAutomation", category: "Session")
let openWorkbenchNotification = Notification.Name("local.codex.UUDisplayAutomation.OpenWorkbench")
let refreshSessionNotification = Notification.Name("local.codex.UUDisplayAutomation.RefreshSession")
let quitAndRestoreNotification = Notification.Name("local.codex.UUDisplayAutomation.QuitAndRestore")
let permissionGuideNotification = Notification.Name("local.codex.UUDisplayAutomation.PermissionGuide")

// AX remote-control row recognition adapted from UUyc-companion:
// https://github.com/aoyamashou/UUyc-companion (MIT, Copyright (c) 2026 AoyamaShou).
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

let supportURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/UUDisplayAutomation", isDirectory: true)
struct Signal: Codable {
    var timestamp = Date().timeIntervalSince1970
    var state: String
    var trusted: Bool
    var overlay: Bool
    var controllers: [String]
    var reason: String
    var sleeping: Bool
    var event = "session"
    var event_id: UInt64 = 0
    var display_revision: UInt64 = 0
    var detector_pid = getpid()
    var run_id = UUID().uuidString
    var ax_reads = 0
    var notifications = 0
    var heartbeats = 0
    var fallback_reads = 0
    var observer_supported = false
    var activeControllerCount: Int? = nil
    var axCachedWindows = false
    var uuPID: pid_t? = nil
    var session_observed_at: Double? = nil
    var session_negative_verified = false
    var ui_hidden: Bool? = nil
    var ui_minimized: Bool? = nil
    var ui_observable: Bool? = nil
    var session_read_ms: Double? = nil
}
func atomicJSON<T: Encodable>(_ value: T, name: String) {
    do {
        try FileManager.default.createDirectory(at: supportURL, withIntermediateDirectories: true)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(value).write(to: supportURL.appendingPathComponent(name), options: .atomic)
    } catch { NSLog("UUDisplayAutomation: cannot write %@: %@", name, error.localizedDescription) }
}
struct Command: Encodable { let id = UUID().uuidString; let action: String }
func wakeController() {
    // A controller restart can close its reader between open and write.
    _ = Darwin.signal(SIGPIPE, SIG_IGN)
    let fd = open(supportURL.appendingPathComponent("wake.fifo").path, O_WRONLY | O_NONBLOCK)
    if fd >= 0 { var byte: UInt8 = 1; _ = Darwin.write(fd, &byte, 1); close(fd) }
}
func commandPayload(_ payload: [String: Any]) throws {
    var value = payload
    if value["id"] == nil { value["id"] = UUID().uuidString }
    let manual = ["pause", "recover"].contains(value["action"] as? String ?? "")
    if !manual, let storage = StorageHealthCenter.shared.active() {
        throw NSError(domain: "UUDisplayAutomation", code: 28, userInfo: [NSLocalizedDescriptionKey: storage.message])
    }
    if !manual, value["action"] as? String != "enable",
       ControllerSupervisor.status()?["exit_after_restore"] is String {
        throw NSError(domain: "UUDisplayAutomation", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "正在恢复并退出，请等待完成后再修改设备配置。"])
    }
    let url = supportURL.appendingPathComponent("command.json")
    let previous = (try? Data(contentsOf: url)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    do {
        try FileManager.default.createDirectory(at: supportURL, withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
    } catch {
        let failure = error as NSError
        let underlying = failure.userInfo[NSUnderlyingErrorKey] as? NSError
        let full = (failure.domain == NSPOSIXErrorDomain && [Int(ENOSPC), Int(EDQUOT)].contains(failure.code)) ||
            (failure.domain == NSCocoaErrorDomain && failure.code == NSFileWriteOutOfSpaceError) ||
            (underlying?.domain == NSPOSIXErrorDomain && [Int(ENOSPC), Int(EDQUOT)].contains(underlying?.code ?? 0))
        guard manual && full else { throw error }
        value["previous_command_id"] = previous?["id"] ?? NSNull()
        let message = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        var framed = Data("\nUU_DISPLAY_COMMAND ".utf8); framed.append(message); framed.append(0x0a)
        _ = Darwin.signal(SIGPIPE, SIG_IGN)
        let fd = open(supportURL.appendingPathComponent("wake.fifo").path, O_WRONLY | O_NONBLOCK)
        guard fd >= 0 else { throw error }
        defer { close(fd) }
        let sent = framed.withUnsafeBytes { raw in Darwin.write(fd, raw.baseAddress, raw.count) }
        guard sent == framed.count else { throw error }
    }
    wakeController()
}
func command(_ action: String) {
    do { try commandPayload(["action": action]) }
    catch { NSLog("Unable to send automation command: %@", error.localizedDescription) }
}

struct AXSnapshot {
    var names: [String]?
    var reason: String
    var observed: [AXUIElement] = []
    var activeControllerCount: Int? = nil
    var usedCachedWindows = false
    var pid: pid_t? = nil
    var observedAt = Date().timeIntervalSince1970
    var negativeVerified = false
    var hidden: Bool? = nil
    var minimized: Bool? = nil
    var observable = false
    var readMilliseconds: Double? = nil
    var hasControllers: Bool { SessionEvidence.hasControllers(names: names, count: activeControllerCount) }
}
enum Reader {
    // Element references only: every read walks their current AX attributes.
    static var windowPID: pid_t = 0
    static var lastWindows: [AXUIElement] = []
    struct ControllerTable {
        let pid: pid_t
        let window: AXUIElement
        let table: AXUIElement
        let structure: [AXUIElement]
    }
    static var controllerTable: ControllerTable?
    static var sessionWindow: AXUIElement?
    static var homeDisconnect = HomeDisconnectConfirmation()
    /// Called only on the serial reader queue, including after authorization changes.
    static func invalidateAuthorizationCache() {
        windowPID = 0; lastWindows = []; controllerTable = nil; sessionWindow = nil; homeDisconnect.reset()
    }

    static func liveControllerTable(_ cached: ControllerTable, observed: inout [AXUIElement]) -> SessionEvidence? {
        guard attr(cached.window, kAXRoleAttribute) as? String == kAXWindowRole,
              let role = attr(cached.table, kAXRoleAttribute) as? String,
              [kAXTableRole, kAXOutlineRole].contains(role),
              let count = rowCount(cached.table), count >= 0, count <= 32 else { return nil }
        var rowsValue: AnyObject?
        guard AXUIElementCopyAttributeValue(cached.table, kAXRowsAttribute as CFString, &rowsValue) == .success,
              let rows = rowsValue as? [AXUIElement], rows.count == count else { return nil }
        let deadline = Date().addingTimeInterval(0.8)
        var failed = false
        func texts(_ element: AXUIElement, depth: Int = 0) -> [String] {
            guard depth < 10, Date() < deadline,
                  let role = attr(element, kAXRoleAttribute) as? String else { failed = true; return [] }
            if role == kAXStaticTextRole {
                guard let value = attr(element, kAXValueAttribute) as? String else { failed = true; return [] }
                return [value.trimmingCharacters(in: .whitespacesAndNewlines)]
            }
            var children: AnyObject?
            let error = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children)
            if error == .noValue || error == .attributeUnsupported { return [] }
            guard error == .success, let values = children as? [AXUIElement], values.count < 128 else { failed = true; return [] }
            return values.flatMap { texts($0, depth: depth + 1) }
        }
        let rowTexts = rows.map { texts($0) }
        observed += cached.structure + [cached.window, cached.table] + rows
        return SessionEvidence.cachedControllerTable(rowTexts: rowTexts, rowCount: count, valid: !failed)
    }
    static func overlay() -> Bool {
        let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        return windows.contains { ($0[kCGWindowOwnerName as String] as? String) == "UURemoteServer" }
    }
    static func attr(_ element: AXUIElement, _ key: String) -> AnyObject? {
        var value: AnyObject?
        return AXUIElementCopyAttributeValue(element, key as CFString, &value) == .success ? value : nil
    }
    static func rowCount(_ element: AXUIElement) -> Int? {
        var count: CFIndex = 0
        return AXUIElementGetAttributeValueCount(element, kAXRowsAttribute as CFString, &count) == .success ? count : nil
    }
    static func controllers() -> AXSnapshot {
        let started = Date()
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.netease.uuremote").first else {
            windowPID = 0; lastWindows = []; controllerTable = nil; sessionWindow = nil; homeDisconnect.reset()
            return AXSnapshot(names: [], reason: "uu_not_running", activeControllerCount: 0,
                              negativeVerified: true)
        }
        let pid = app.processIdentifier
        if windowPID != pid {
            windowPID = pid; lastWindows = []; controllerTable = nil; sessionWindow = nil; homeDisconnect.reset()
        }
        guard AXIsProcessTrusted() else {
            homeDisconnect.reset(); return AXSnapshot(reason: "accessibility_permission_required")
        }
        let root = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(root, 0.3)
        var windowValue: AnyObject?
        let windowResult = AXUIElementCopyAttributeValue(root, kAXWindowsAttribute as CFString, &windowValue)
        let liveWindows = windowValue as? [AXUIElement] ?? []
        let cached = liveWindows.isEmpty && !lastWindows.isEmpty
        if !liveWindows.isEmpty {
            // A transient dialog must not evict the still-live main window.
            var merged = liveWindows
            for old in lastWindows where attr(old, kAXRoleAttribute) != nil {
                if !merged.contains(where: { CFEqual($0, old) }) { merged.append(old) }
            }
            lastWindows = Array(merged.prefix(8))
        }
        let windows = liveWindows.isEmpty ? lastWindows : liveWindows
        let hidden = app.isHidden
        let minimized = !windows.isEmpty && windows.allSatisfy { (attr($0, kAXMinimizedAttribute) as? Bool) == true }
        let observable = !hidden && !minimized && !liveWindows.isEmpty
        var observed = [root] + windows
        var emptyTableEvidence: SessionEvidence?
        if let cachedTable = controllerTable, cachedTable.pid == pid,
           let evidence = liveControllerTable(cachedTable, observed: &observed) {
            if (evidence.activeControllerCount ?? 0) > 0 {
                homeDisconnect.reset()
                return AXSnapshot(names: evidence.names, reason: evidence.reason, observed: observed,
                                  activeControllerCount: evidence.activeControllerCount, usedCachedWindows: cached,
                                  pid: pid, hidden: hidden, minimized: minimized, observable: observable,
                                  readMilliseconds: Date().timeIntervalSince(started) * 1000)
            }
            emptyTableEvidence = evidence
            controllerTable = nil // Never let an old empty table mask a newly created session table.
        }
        guard !windows.isEmpty else {
            homeDisconnect.reset()
            return AXSnapshot(names: emptyTableEvidence?.names,
                              reason: emptyTableEvidence?.reason ?? "uu_windows_unavailable", observed: observed,
                              activeControllerCount: emptyTableEvidence?.activeControllerCount, pid: pid,
                              negativeVerified: emptyTableEvidence != nil, hidden: hidden, minimized: minimized)
        }
        let deadline = Date().addingTimeInterval(1.5)
        var visited: [CFHashCode: [AXUIElement]] = [:]
        var count = 0
        var incomplete = windowResult != .success && windowResult != .noValue
        var names: [String] = []; var windowTexts: [[String]] = []
        var currentWindowTexts: [String] = []
        var completeMainWindow = false
        var completeKnownHome = false
        var tableCandidates: [(element: AXUIElement, names: [String])] = []
        func walk(_ element: AXUIElement, _ depth: Int, _ inRow: Bool, _ texts: inout [String], _ parentTable: AXUIElement? = nil) {
            guard depth < 25, count < 1500, Date() < deadline else { incomplete = true; return }
            let hash = CFHash(element)
            if visited[hash, default: []].contains(where: { CFEqual($0, element) }) { return }
            visited[hash, default: []].append(element)
            count += 1
            guard let role = attr(element, kAXRoleAttribute) as? String else { incomplete = true; return }
            let row = role == kAXRowRole
            let table = [kAXTableRole, kAXOutlineRole].contains(role) ? element : parentTable
            // Keep the surrounding containers observed even when UU removes
            // its entire controller table at disconnect.
            if [kAXTableRole, kAXOutlineRole, kAXGroupRole, kAXSplitGroupRole, kAXScrollAreaRole].contains(role) { observed.append(element) }
            var rowTexts: [String] = []
            if role == kAXStaticTextRole {
                var value: AnyObject?
                let result = AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value)
                if result != .success { incomplete = true }
                if let value = value as? String {
                    let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !text.isEmpty {
                        if inRow { texts.append(text) }
                        else { currentWindowTexts.append(text) }
                    }
                } else if result == .success { incomplete = true }
            }
            var childValue: AnyObject?
            let childResult = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &childValue)
            let container = [kAXWindowRole, kAXTableRole, kAXOutlineRole, kAXGroupRole,
                             kAXSplitGroupRole, kAXScrollAreaRole, kAXRowRole, kAXCellRole].contains(role)
            if childResult != .success && (container || (childResult != .noValue && childResult != .attributeUnsupported)) { incomplete = true }
            if childResult == .success, !(childValue is [AXUIElement]) { incomplete = true }
            for child in (childValue as? [AXUIElement]) ?? [] {
                if row { walk(child, depth + 1, true, &rowTexts, table) }
                else { walk(child, depth + 1, inRow, &texts, table) }
            }
            if row, rowTexts.contains(where: { $0.contains("远程控制") }) {
                // Some AX versions expose separate cells, others combine the entire row.
                let name = SessionEvidence.controllerName(rowTexts)
                if let name {
                    if !names.contains(name) { names.append(name) }
                    if let table {
                        if let index = tableCandidates.firstIndex(where: { CFEqual($0.element, table) }) {
                            tableCandidates[index].names.append(name)
                        } else { tableCandidates.append((element: table, names: [name])) }
                    }
                }
                else { incomplete = true }
            }
        }
        for window in windows {
            currentWindowTexts = []
            tableCandidates = []
            var texts: [String] = []
            walk(window, 0, false, &texts)
            windowTexts.append(currentWindowTexts)
            // Cache references only after an explicit LOCAL controlled-session
            // context and an unambiguous controller table were read together.
            let local = SessionEvidence.classify(windowTexts: [currentWindowTexts], controllerNames: [],
                                                incomplete: true, completeMainWindow: false)
            if (local.activeControllerCount ?? 0) > 0 { sessionWindow = window }
            let candidates = tableCandidates.filter { !$0.names.isEmpty && Set($0.names).count == $0.names.count }
            if (local.activeControllerCount ?? 0) > 0, candidates.count == 1,
               let match = candidates.first, match.names.count == local.activeControllerCount {
                controllerTable = ControllerTable(pid: pid, window: window, table: match.element,
                                                  structure: observed)
            }
            if attr(window, kAXRoleAttribute) as? String == kAXWindowRole,
               liveWindows.contains(where: { CFEqual($0, window) }),
               Set(["我的设备", "全部设备", "远程协助"]).isSubset(of: Set(currentWindowTexts)) {
                if !hidden, (attr(window, kAXMinimizedAttribute) as? Bool) != true { completeMainWindow = true }
                if let sessionWindow, CFEqual(sessionWindow, window) { completeKnownHome = true }
            }
        }
        var evidence = SessionEvidence.classify(windowTexts: windowTexts, controllerNames: names,
                                                incomplete: incomplete, completeMainWindow: completeMainWindow,
                                                allowHomeNegative: observable)
        let fullHome = SessionEvidence.classify(windowTexts: windowTexts, controllerNames: names,
                                               incomplete: incomplete, completeMainWindow: completeKnownHome)
        let homeCandidate = fullHome.reason == "ax_complete_home_no_local_control"
        let homeConfirmed = homeDisconnect.observe(completeKnownHome: homeCandidate,
                                                    now: Date().timeIntervalSince1970)
        if !observable && homeCandidate {
            evidence = homeConfirmed ? fullHome : SessionEvidence(reason: "ax_home_disconnect_confirmation_pending")
        }
        if names.isEmpty, (evidence.activeControllerCount ?? 0) == 0, let emptyTableEvidence {
            evidence = emptyTableEvidence
        }
        let negativeVerified = evidence.names?.isEmpty == true &&
            (evidence.reason == "ax_cached_controller_table_empty" || observable || homeConfirmed)
        return AXSnapshot(names: evidence.names, reason: evidence.reason, observed: observed,
                          activeControllerCount: evidence.activeControllerCount,
                          usedCachedWindows: cached, pid: pid, negativeVerified: negativeVerified,
                          hidden: hidden, minimized: minimized, observable: observable,
                          readMilliseconds: Date().timeIntervalSince(started) * 1000)
    }
    static func signal(snapshot: AXSnapshot?, sleeping: Bool = false) -> Signal {
        let trusted = AXIsProcessTrusted(); let overlay = overlay()
        let names = snapshot?.names ?? []
        let stopped = snapshot?.reason == "uu_not_running"
        let state = sleeping ? "unknown" : (stopped ? "disconnected" : ((overlay || !names.isEmpty || (snapshot?.activeControllerCount ?? 0) > 0) ? "connected" : (snapshot?.names != nil ? "disconnected" : "unknown")))
        let reason = sleeping ? "system_sleeping" : (stopped ? "uu_not_running" : (overlay ? "uu_overlay_present" : (snapshot?.reason ?? (trusted ? "ax_read_pending" : "accessibility_permission_required"))))
        var value = Signal(state: state, trusted: trusted, overlay: overlay, controllers: names, reason: reason, sleeping: sleeping)
        value.activeControllerCount = snapshot?.activeControllerCount
        value.axCachedWindows = snapshot?.usedCachedWindows ?? false
        value.uuPID = snapshot?.pid
        value.session_observed_at = snapshot?.observedAt
        value.session_negative_verified = snapshot?.negativeVerified ?? false
        value.ui_hidden = snapshot?.hidden; value.ui_minimized = snapshot?.minimized
        value.ui_observable = snapshot?.observable
        value.session_read_ms = snapshot?.readMilliseconds
        return value
    }
}

// One native supervisor owns the controller; its lock prevents overlap across restarts.
final class ControllerSupervisor {
    var child: Process?; var adoptedPID: pid_t?; var failures = 0; var launching = false; var stopped = false
    var controllerSeenAt = ProcessInfo.processInfo.systemUptime
    var retry: DispatchWorkItem?
    let queue = DispatchQueue(label: "local.codex.UUDisplayAutomation.supervisor", qos: .utility)
    fileprivate var statusReader: () -> [String: Any]? = { ControllerSupervisor.status() }
    fileprivate var stateReader: () -> [String: Any]? = { ControllerSupervisor.record("state.json") }
    fileprivate var cursorReader: () -> [String: Any]? = { ControllerSupervisor.record("cursor-profile-state.json") }
    fileprivate var ownerReader: (() -> pid_t?)?
    fileprivate var terminateAdopted: (pid_t) -> Void = { kill($0, SIGTERM) }
    fileprivate var processLauncher: (Process) throws -> Void = { try $0.run() }
    var requestedExitID: String?
    var requestedExitAt: TimeInterval?
    var requestedExitAllowsResume = false
    private var launchGeneration = 0
    private var terminateOnStop = true
    var controllerPID: pid_t? { child?.isRunning == true ? child?.processIdentifier : adoptedPID }
    func expectedExitCompleted() -> Bool {
        guard let requestedExitID, let requestedExitAt else { return false }
        return ExitHandoff.isComplete(state: stateReader(), status: statusReader(), cursor: cursorReader(),
                                       commandID: requestedExitID, requestedAt: requestedExitAt,
                                       allowResume: requestedExitAllowsResume)
    }
    static func status() -> [String: Any]? {
        record("status.json")
    }
    static func record(_ name: String) -> [String: Any]? {
        guard let data = try? Data(contentsOf: supportURL.appendingPathComponent(name)) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
    func matchesController(_ number: Int) -> Bool {
        guard number > 1, number <= Int(Int32.max), kill(pid_t(number), 0) == 0 else { return false }
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-p", String(number), "-o", "command="]
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return false }
        let output = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        process.waitUntilExit()
        return output.contains(supportURL.appendingPathComponent("controller.py").path) && output.contains("--event-driven")
    }
    func existingPID() -> pid_t? {
        if let ownerReader { return ownerReader() }
        if let data = try? Data(contentsOf: supportURL.appendingPathComponent("supervisor-child.json")),
           let record = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let number = (record["pid"] as? Int) ?? Int(record["pid"] as? String ?? ""), matchesController(number) {
            return pid_t(number)
        }
        // The PID file itself may have failed to update on a full disk. Adopt
        // the verified lock owner instead of spawning a competing controller.
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        process.arguments = ["-t", supportURL.appendingPathComponent("controller.lock").path]
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return nil }
        let output = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        process.waitUntilExit()
        return output.split(whereSeparator: \.isWhitespace).compactMap { Int($0) }
            .first(where: matchesController).map { pid_t($0) }
    }
    func ensureRunning() {
        guard !stopped, !launching, child?.isRunning != true, !expectedExitCompleted() else { return }
        launching = true
        launchGeneration += 1
        let generation = launchGeneration
        queue.async { [self] in
            if let pid = self.existingPID() {
                DispatchQueue.main.async {
                    guard !self.stopped, self.launchGeneration == generation else {
                        self.launching = false
                        if self.terminateOnStop { self.terminateAdopted(pid) }
                        return
                    }
                    self.adoptedPID = pid; self.launching = false
                    self.controllerSeenAt = ProcessInfo.processInfo.systemUptime
                    if let pressure = StorageHealthCenter.shared.active(), pressure.pid != pid {
                        StorageHealthCenter.shared.clear()
                    }
                }
                return
            }
            let mayLaunch = DispatchQueue.main.sync { () -> Bool in
                guard !self.stopped, self.launchGeneration == generation else {
                    self.launching = false
                    return false
                }
                return true
            }
            guard mayLaunch else { return }
            let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = [supportURL.appendingPathComponent("controller.py").path, "--event-driven", "--directory", supportURL.path]
            for name in ["controller.stdout.log", "controller.stderr.log"] {
                let url = supportURL.appendingPathComponent(name)
                if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
            }
            let out = try? FileHandle(forWritingTo: supportURL.appendingPathComponent("controller.stdout.log"))
            let err = try? FileHandle(forWritingTo: supportURL.appendingPathComponent("controller.stderr.log"))
            _ = try? out?.seekToEnd(); _ = try? err?.seekToEnd()
            process.standardOutput = out ?? FileHandle.nullDevice; process.standardError = err ?? FileHandle.nullDevice
            process.terminationHandler = { [weak self] ended in
                try? out?.close(); try? err?.close()
                DispatchQueue.main.async {
                    guard let self, self.launchGeneration == generation else { return }
                    self.child = nil; self.launching = false
                    StorageHealthCenter.shared.clear()
                    let expected = self.expectedExitCompleted()
                    if !self.stopped && !expected { self.scheduleRestart() }
                }
            }
            do {
                try self.processLauncher(process)
                atomicJSON(["pid": String(process.processIdentifier), "start_token": UUID().uuidString, "started_at": String(Date().timeIntervalSince1970)], name: "supervisor-child.json")
                DispatchQueue.main.async {
                    guard !self.stopped, self.launchGeneration == generation else {
                        self.launching = false
                        if self.terminateOnStop, process.isRunning { process.terminate() }
                        return
                    }
                    self.child = process; self.adoptedPID = nil; self.launching = false
                    self.controllerSeenAt = ProcessInfo.processInfo.systemUptime
                    if let pressure = StorageHealthCenter.shared.active(), pressure.pid != process.processIdentifier {
                        StorageHealthCenter.shared.clear()
                    }
                }
            } catch {
                try? out?.close(); try? err?.close()
                DispatchQueue.main.async {
                    guard self.launchGeneration == generation else { return }
                    self.launching = false; self.scheduleRestart()
                }
            }
        }
    }
    func scheduleRestart() {
        guard retry == nil, !stopped else { return }
        failures += 1
        let work = DispatchWorkItem { [weak self] in self?.retry = nil; self?.ensureRunning() }
        retry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + min(60, pow(2, Double(min(failures, 6)))), execute: work)
    }
    func healthCheck() {
        guard !stopped else { return }
        let age = Date().timeIntervalSince1970 - (statusReader()?["timestamp"] as? Double ?? 0)
        if let pid = controllerPID, let pressure = StorageHealthCenter.shared.active(),
           pressure.pid == pid, kill(pid, 0) == 0 { return }
        if age < 90, let pid = controllerPID,
           (statusReader()?["controller_pid"] as? Int) == Int(pid) { failures = 0 }
        let graceEnded = ProcessInfo.processInfo.systemUptime - controllerSeenAt >= 60
        if let child, child.isRunning {
            if age > 120 && graceEnded { child.terminate() } // no SIGKILL
        } else if retry == nil {
            if let pid = adoptedPID, kill(pid, 0) == 0 {
                if age > 120 && graceEnded {
                    queue.async {
                        guard self.existingPID() == pid else { return }
                        DispatchQueue.main.async {
                            // A pressure heartbeat can arrive while the owner
                            // check is queued. Re-evaluate it at action time.
                            guard self.controllerPID == pid,
                                  ProcessInfo.processInfo.systemUptime - self.controllerSeenAt >= 60,
                                  Date().timeIntervalSince1970 - (self.statusReader()?["timestamp"] as? Double ?? 0) > 120,
                                  StorageHealthCenter.shared.active()?.pid != pid else { return }
                            self.terminateAdopted(pid)
                        }
                    }
                }
            } else {
                adoptedPID = nil; StorageHealthCenter.shared.clear()
                if !expectedExitCompleted() { ensureRunning() }
            }
        }
    }
    func stop(terminateController: Bool = true) {
        terminateOnStop = stopped ? (terminateOnStop || terminateController) : terminateController
        stopped = true; launchGeneration += 1; retry?.cancel(); retry = nil
        guard terminateOnStop else { return }
        if child?.isRunning == true { child?.terminate() }
        if let pid = adoptedPID {
            queue.async { if self.existingPID() == pid { self.terminateAdopted(pid) } }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var started = false
    var item: NSStatusItem!; var timer: Timer?; var sleeping = false
    var snapshot: AXSnapshot?; var inFlight = false; var dirty = false
    var debounce: DispatchWorkItem?; var observer: AXObserver?; var observed: [AXUIElement] = []
    var rowCounts: [CFHashCode: Int] = [:]
    var structuralRetry: DispatchWorkItem?
    var observedPID: pid_t = 0; var fallbackRequired = true; var lastRead = Date.distantPast
    var fallbackReads = 0
    var axReads = 0; var notifications = 0; var heartbeats = 0; var eventID: UInt64 = 0; var displayRevision: UInt64 = 0
    let runID = UUID().uuidString
    let queue = DispatchQueue(label: "local.codex.UUDisplayAutomation.reader", qos: .utility)
    var signal = Reader.signal(snapshot: nil)
    var hotkey: EventHotKeyRef?
    let supervisor = ControllerSupervisor()
    var quitting = false
    var terminationApproved = false
    var quitProgressCheck: (() -> Void)?
    var quitWindowDeadline: DispatchWorkItem?
    var statusObserver: StatusFileObserver?
    var statusPresentation: StatusBarPresentation?
    var workbench: WorkbenchWindowController?
    var popover: StatusPopoverController?
    var lastSessionRefresh = Date.distantPast
    var terminationSignals: [DispatchSourceSignal] = []
    var authorizationGeneration: UInt64 = 0
    var permissionGuide: PermissionGuideWindowController?
    var permissionTakeover: PermissionTicket?
    var permissionRefreshInProgress = false
    var permissionDraftState: Data?
    var permissionWorkbenchWasVisible = false
    lazy var permissionModel: AccessibilityPermissionModel = {
        let model = AccessibilityPermissionModel(directory: supportURL)
        model.onObservation = { [weak self] trusted, force in self?.authorizationObserved(trusted, force: force) }
        model.onCancelRelaunch = { [weak self] in self?.cancelPermissionRefresh() }
        model.onRelaunch = { [weak self] _ in
            guard let self, !self.quitting, !self.sleeping, StorageHealthCenter.shared.active() == nil else { return false }
            do { self.permissionDraftState = try self.workbench?.model.permissionDraftState() }
            catch {
                self.permissionModel.fail("请先保存或还原工作台中的未应用更改，再刷新检测服务。")
                return false
            }
            self.permissionWorkbenchWasVisible = self.workbench?.window?.isVisible == true
            self.permissionRefreshInProgress = true
            self.beginQuit(preserveIntent: true)
            if !self.quitting { self.permissionRefreshInProgress = false; return false }
            return true
        }
        return model
    }()
    func applicationDidFinishLaunching(_ notification: Notification) {
        guard !started else { return }; started = true
        NSApp.setActivationPolicy(.accessory)
        var startingSignal = signal
        startingSignal.detector_pid = getpid(); startingSignal.run_id = runID
        startingSignal.timestamp = Date().timeIntervalSince1970
        NativeStatusSignal.shared.update(startingSignal, directory: supportURL)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        refreshStatusIcon()
        statusObserver = StatusFileObserver(directory: supportURL) { [weak self] in
            self?.refreshStatusIcon(); self?.quitProgressCheck?()
        }
        item.button?.target = self
        item.button?.action = #selector(statusItemClicked(_:))
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        popover = StatusPopoverController(directory: supportURL, actions: PopoverActions(
            openWorkbench: { [weak self] in self?.openWorkbench() },
            requestPermission: { [weak self] in self?.permission() },
            quit: { [weak self] in self?.quit() },
            startupEnabled: { try? LoginAtStartup().isEnabled() },
            setStartup: { try LoginAtStartup().setEnabled($0) }), sendCommand: commandPayload)
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(self, selector: #selector(willSleep), name: NSWorkspace.willSleepNotification, object: nil)
        center.addObserver(self, selector: #selector(didWake), name: NSWorkspace.didWakeNotification, object: nil)
        center.addObserver(self, selector: #selector(appChanged), name: NSWorkspace.didLaunchApplicationNotification, object: nil)
        center.addObserver(self, selector: #selector(appChanged), name: NSWorkspace.didTerminateApplicationNotification, object: nil)
        center.addObserver(self, selector: #selector(appChanged), name: NSWorkspace.didHideApplicationNotification, object: nil)
        center.addObserver(self, selector: #selector(appChanged), name: NSWorkspace.didUnhideApplicationNotification, object: nil)
        center.addObserver(self, selector: #selector(permissionApplicationActivated(_:)), name: NSWorkspace.didActivateApplicationNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(permissionSurfaceActivated),
            name: NSApplication.didBecomeActiveNotification, object: nil)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(refreshSessionRequested),
            name: refreshSessionNotification, object: nil, suspensionBehavior: .deliverImmediately)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(storageHealthReceived),
            name: StorageHealthCenter.distributedNotification, object: nil, suspensionBehavior: .deliverImmediately)
        NotificationCenter.default.addObserver(self, selector: #selector(storageHealthUpdated),
            name: StorageHealthCenter.didChange, object: nil)
        CGDisplayRegisterReconfigurationCallback({ _, flags, context in
            guard !flags.contains(.beginConfigurationFlag), let context else { return }
            let owner = Unmanaged<AppDelegate>.fromOpaque(context).takeUnretainedValue()
            DispatchQueue.main.async { owner.publish(event: "display", force: true); owner.requestRead() }
        }, Unmanaged.passUnretained(self).toOpaque())
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            guard let event else { return OSStatus(eventNotHandledErr) }
            var key = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil, MemoryLayout<EventHotKeyID>.size, nil, &key)
            if key.signature == 0x55554441 && key.id == 1 { command("recover"); return noErr }
            return OSStatus(eventNotHandledErr)
        }, 1, [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))], nil, nil)
        RegisterEventHotKey(UInt32(kVK_ANSI_9), UInt32(controlKey | optionKey | cmdKey), EventHotKeyID(signature: 0x55554441, id: 1), GetApplicationEventTarget(), 0, &hotkey)
        supervisor.ensureRunning()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            guard let self else { return }
            if self.quitting { self.supervisor.healthCheck(); return }
            self.heartbeats += 1
            self.permissionModel.check()
            self.publish(event: "heartbeat", force: true) // cached: never reads AX or window list
            self.supervisor.healthCheck()
            // Unsupported row notifications get a deliberately rare safety fallback.
            if self.fallbackRequired && Date().timeIntervalSince(self.lastRead) >= 60 { self.fallbackReads += 1; self.requestRead() }
        }
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(openWorkbench),
            name: openWorkbenchNotification, object: nil, suspensionBehavior: .deliverImmediately)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(permission),
            name: permissionGuideNotification, object: nil, suspensionBehavior: .deliverImmediately)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(quitRequested(_:)),
            name: quitAndRestoreNotification, object: nil, suspensionBehavior: .deliverImmediately)
        for number in [SIGTERM, SIGINT] {
            Darwin.signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { [weak self] in
                guard let self else { return }
                if self.permissionRefreshInProgress { self.permissionModel.endConfirmation() }
                self.beginQuit(preserveIntent: number == SIGTERM)
            }
            source.resume(); terminationSignals.append(source)
        }
        requestRead()
        if let ticket = permissionTakeover {
            permissionModel.resumed(ticket)
            if ticket.showWorkbench || ticket.workbenchState != nil {
                ensureWorkbench()
                if let data = ticket.workbenchState { workbench?.model.restorePermissionDraftState(data) }
                if ticket.showWorkbench { workbench?.show() }
            }
            permission()
        } else if !AXIsProcessTrusted() {
            let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "unknown"
            if UserDefaults.standard.string(forKey: "PermissionGuidePresentedBuild") != version {
                UserDefaults.standard.set(version, forKey: "PermissionGuidePresentedBuild")
                permission()
            }
        }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openWorkbench(); return true
    }
    @objc func openWorkbench() {
        permissionModel.check()
        ensureWorkbench()
        workbench?.show()
    }
    private func ensureWorkbench() {
        if workbench == nil {
            workbench = WorkbenchWindowController(directory: supportURL, actions: WorkbenchActions(
                openStatusFolder: { [weak self] in self?.openFolder() },
                requestPermission: { [weak self] in self?.permission() },
                startupEnabled: { try? LoginAtStartup().isEnabled() },
                setStartup: { try LoginAtStartup().setEnabled($0) },
                officeSetup: MainActor.assumeIsolated { OfficeCursorSetupModel.live(directory: supportURL, onPrepared: wakeController) }), sendCommand: commandPayload)
        }
    }
    /// Left click opens the panel; right click or Control-click shows the classic menu.
    @objc func statusItemClicked(_ sender: NSStatusBarButton) {
        permissionModel.check()
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            popover?.close()
            let menu = NSMenu(); menu.delegate = self
            item.menu = menu
            sender.performClick(nil) // tracks the menu modally
            item.menu = nil
        } else {
            popover?.toggle(relativeTo: sender)
        }
    }
    func bindObserver(_ elements: [AXUIElement]) {
        let pid = NSRunningApplication.runningApplications(withBundleIdentifier: "com.netease.uuremote").first?.processIdentifier ?? 0
        if let observer { CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes) }
        observer = nil; observed = []; rowCounts = [:]; observedPID = pid
        guard pid != 0 else { fallbackRequired = false; return }
        var result: AXObserver?
        guard AXObserverCreate(pid, { source, element, name, context in
            guard let context else { return }
            let owner = Unmanaged<AppDelegate>.fromOpaque(context).takeUnretainedValue()
            DispatchQueue.main.async {
                guard let current = owner.observer, CFEqual(current, source) else { return }
                owner.notifications += 1
                if name as String == kAXCreatedNotification {
                    let role = Reader.attr(element, kAXRoleAttribute) as? String
                    // UU can announce only the newly created group/text, not
                    // the table nested inside it. Waiting must accept those.
                    if owner.snapshot?.hasControllers == true {
                        guard role == kAXWindowRole || role == kAXTableRole || role == kAXOutlineRole || role == kAXRowRole else { return }
                    }
                }
                if name as String == kAXUIElementDestroyedNotification {
                    guard owner.observed.contains(where: { CFEqual($0, element) }) else { return }
                }
                if name as String == kAXLayoutChangedNotification {
                    let hash = CFHash(element)
                    if let count = Reader.rowCount(element) {
                        guard owner.rowCounts[hash] != count else { return }
                        owner.rowCounts[hash] = count
                    } else {
                        // The empty screen has no AXRows object. Its surviving
                        // containers provide the reconnect event. Ignore these
                        // layouts once connected because UU updates its clock.
                        guard owner.snapshot?.hasControllers != true else { return }
                    }
                }
                owner.requestRead()
                if owner.snapshot?.hasControllers != true {
                    owner.retryAfterStructureAppears()
                }
            }
        }, &result) == .success, let result else { fallbackRequired = true; return }
        observer = result; observed = elements
        var rowSupported = false
        for element in elements {
            if let count = Reader.rowCount(element) { rowCounts[CFHash(element)] = count }
            for name in [kAXRowCountChangedNotification, kAXCreatedNotification, kAXUIElementDestroyedNotification, kAXWindowCreatedNotification, kAXWindowMiniaturizedNotification, kAXWindowDeminiaturizedNotification] {
                let error = AXObserverAddNotification(result, element, name as CFString, Unmanaged.passUnretained(self).toOpaque())
                if name == kAXRowCountChangedNotification && Reader.rowCount(element) != nil && (error == .success || error == .notificationAlreadyRegistered) { rowSupported = true }
            }
            // Root/window/group layout subscriptions survive removal of the
            // controller table. Row-count subscriptions alone cannot reconnect.
            _ = AXObserverAddNotification(result, element, kAXLayoutChangedNotification as CFString, Unmanaged.passUnretained(self).toOpaque())
        }
        fallbackRequired = !rowSupported
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(result), .commonModes)
    }
    func retryAfterStructureAppears() {
        guard structuralRetry == nil else { return }
        // One bounded settling check per event burst, never a repeating timer.
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }; self.structuralRetry = nil
            if self.snapshot?.hasControllers != true { self.requestRead() }
        }
        structuralRetry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6, execute: work)
    }
    func requestRead() {
        guard !sleeping, !quitting else { return }
        dirty = true
        guard !inFlight, debounce == nil else { return }
        let work = DispatchWorkItem { [weak self] in self?.debounce = nil; self?.readSession() }
        debounce = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: work)
    }
    @objc func refreshSessionRequested(_ notification: Notification) {
        guard Date().timeIntervalSince(lastSessionRefresh) >= 1 else { return }
        lastSessionRefresh = Date(); requestRead()
    }
    @objc func storageHealthReceived(_ notification: Notification) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { self.storageHealthReceived(notification) }
            return
        }
        guard let pid = supervisor.controllerPID, let text = notification.object as? String,
              text.utf8.count <= 8192, let data = text.data(using: .utf8),
              let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        _ = StorageHealthCenter.shared.accept(payload, expectedPID: pid)
    }
    @objc func storageHealthUpdated(_ notification: Notification) { refreshStatusIcon() }
    func readSession() {
        guard !inFlight, !sleeping, !quitting else { return }
        dirty = false; inFlight = true; axReads += 1; lastRead = Date()
        let generation = authorizationGeneration
        queue.async {
            let snapshot = Reader.controllers()
            let value = Reader.signal(snapshot: snapshot)
            DispatchQueue.main.async {
                self.inFlight = false
                guard !self.sleeping, !self.quitting else { return }
                guard generation == self.authorizationGeneration else { self.requestRead(); return }
                self.snapshot = snapshot
                self.bindObserver(snapshot.observed)
                self.publish(value: value, event: "session")
                if snapshot.reason == "ax_home_disconnect_confirmation_pending" { self.retryAfterStructureAppears() }
                if self.dirty { self.requestRead() }
            }
        }
    }
    func publish(value: Signal? = nil, event: String, force: Bool = false) {
        var value = value ?? signal
        if event == "display" { displayRevision += 1 }
        value.sleeping = sleeping
        if sleeping { value.state = "unknown"; value.reason = "system_sleeping" }
        let meaningfulChange = value.activeControllerCount != signal.activeControllerCount || value.axCachedWindows != signal.axCachedWindows || value.uuPID != signal.uuPID || value.state != signal.state || value.trusted != signal.trusted || value.overlay != signal.overlay || value.controllers != signal.controllers || value.reason != signal.reason || value.sleeping != signal.sleeping || value.ui_hidden != signal.ui_hidden || value.ui_minimized != signal.ui_minimized || value.ui_observable != signal.ui_observable
        let changed = meaningfulChange || value.session_observed_at != signal.session_observed_at
        guard force || changed || eventID == 0 else { return }
        if meaningfulChange {
            sessionLogger.info("Session state=\(value.state, privacy: .public) reason=\(value.reason, privacy: .public) hidden=\(value.ui_hidden ?? false) minimized=\(value.ui_minimized ?? false) negativeVerified=\(value.session_negative_verified)")
        }
        eventID += 1; value.timestamp = Date().timeIntervalSince1970
        value.event = event; value.event_id = eventID; value.detector_pid = getpid(); value.run_id = runID
        value.display_revision = displayRevision
        value.ax_reads = axReads; value.notifications = notifications; value.heartbeats = heartbeats
        value.fallback_reads = fallbackReads; value.observer_supported = !fallbackRequired
        signal = value
        NativeStatusSignal.shared.update(value, directory: supportURL)
        atomicJSON(value, name: "signal.json"); wakeController()
        refreshStatusIcon()
    }
    func refreshStatusIcon() {
        let presentation = RuntimeStatusSnapshot.read(directory: supportURL).presentation()
        guard presentation != statusPresentation, let button = item?.button else { return }
        statusPresentation = presentation
        button.title = ""; button.imagePosition = .imageOnly; button.image = presentation.image()
        button.toolTip = presentation.tooltip
        button.setAccessibilityLabel("屏幕接力：\(presentation.label)")
        NSLog("UUDisplayAutomation status icon: %@", presentation.kind.rawValue)
    }
    @objc func appChanged(_ notification: Notification) {
        guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
              app.bundleIdentifier == "com.netease.uuremote" || app.localizedName == "UURemoteServer" else { return }
        requestRead()
    }
    @objc func permissionApplicationActivated(_ notification: Notification) {
        guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else { return }
        if app.bundleIdentifier == "com.apple.systempreferences" { permissionModel.settingsActivated() }
        if app.processIdentifier == getpid() { permissionModel.applicationActivated() }
    }
    @objc func permissionSurfaceActivated() { permissionModel.applicationActivated() }
    func authorizationObserved(_ trusted: Bool, force: Bool) {
        guard force || trusted != signal.trusted else { return }
        authorizationGeneration &+= 1
        debounce?.cancel(); debounce = nil; structuralRetry?.cancel(); structuralRetry = nil
        if let observer { CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes) }
        observer = nil; observed = []; rowCounts = [:]; observedPID = 0; fallbackRequired = true
        snapshot = nil
        queue.async { Reader.invalidateAuthorizationCache() }
        var value = signal
        value.trusted = trusted; value.state = "unknown"; value.controllers = []
        value.activeControllerCount = nil; value.session_observed_at = nil; value.session_negative_verified = false
        value.reason = trusted ? "ax_read_pending" : "accessibility_permission_required"
        publish(value: value, event: "session", force: true)
        if trusted { requestRead() }
    }
    func cancelPermissionRefresh() {
        guard permissionRefreshInProgress else { return }
        permissionRefreshInProgress = false; permissionDraftState = nil
        quitting = false; quitProgressCheck = nil; quitWindowDeadline?.cancel()
        supervisor.requestedExitID = nil; supervisor.requestedExitAt = nil
        supervisor.ensureRunning()
    }
    @objc func willSleep() { sleeping = true; publish(event: "session", force: true) }
    @objc func didWake() {
        sleeping = false; permissionModel.check(); requestRead(); quitProgressCheck?(); supervisor.healthCheck()
    }
    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        func add(_ title: String, _ action: Selector? = nil) { let entry = NSMenuItem(title: title, action: action, keyEquivalent: ""); entry.target = self; entry.isEnabled = action != nil; menu.addItem(entry) }
        let snapshot = RuntimeStatusSnapshot.read(directory: supportURL)
        let presentation = snapshot.presentation()
        add("屏幕接力 · \(presentation.label)")
        if presentation.kind == .attention {
            add(presentation.detail)
        }
        menu.addItem(.separator())
        let automation = NSMenuItem(title: "自动接力", action: #selector(toggleAutomation), keyEquivalent: "")
        automation.target = self
        automation.state = snapshot.enabled.map { $0 ? .on : .off } ?? .mixed
        automation.isEnabled = snapshot.ready
        menu.addItem(automation)
        add("紧急恢复并暂停  ⌃⌥⌘9", #selector(recover))
        let sizeMenu = NSMenu(title: "远程光标大小")
        let profiles = snapshot.workbench?["profiles"] as? [[String: Any]] ?? []
        let profileID = snapshot.activeID ?? snapshot.workbench?["selected_id"] as? String
        let selectedScale = profiles.first(where: { $0["id"] as? String == profileID })?["cursor_scale"] as? Double
        for (index, scale) in CursorSizeMenu.scales.enumerated() {
            let entry = NSMenuItem(title: CursorSizeMenu.title(for: scale), action: #selector(setCursorSize(_:)), keyEquivalent: "")
            entry.target = self; entry.tag = CursorSizeMenu.firstTag + index
            entry.state = selectedScale == scale ? .on : .off
            entry.isEnabled = snapshot.ready && snapshot.pendingID == nil && snapshot.mode != "RESTORING" && StorageHealthCenter.shared.active() == nil
            sizeMenu.addItem(entry)
        }
        let sizeItem = NSMenuItem(title: "远程光标大小", action: nil, keyEquivalent: "")
        sizeItem.submenu = sizeMenu; menu.addItem(sizeItem)
        menu.addItem(.separator())
        add("设备工作台…", #selector(openWorkbench))
        let startup = NSMenuItem(title: "开机自启", action: #selector(toggleStartup), keyEquivalent: "")
        startup.target = self; startup.tag = LoginAtStartup.menuTag
        if let enabled = try? LoginAtStartup().isEnabled() { startup.state = enabled ? .on : .off }
        else { startup.isEnabled = false }
        menu.addItem(startup)
        if !snapshot.trusted { add("授权辅助功能…", #selector(permission)) }
        menu.addItem(.separator())
        add("帮助", #selector(help)); add("退出并恢复", #selector(quit))
    }
    @objc func enable() { command("enable") }
    @objc func pause() { command("pause") }
    @objc func toggleAutomation() {
        command(ControllerSupervisor.status()?["desired_enabled"] as? Bool == true ? "pause" : "enable")
    }
    @objc func recover() { command("recover") }
    @objc func toggleStartup() {
        do {
            let startup = LoginAtStartup()
            try startup.setEnabled(!startup.isEnabled())
        } catch {
            let alert = NSAlert(); alert.messageText = "未能更改开机自启"
            alert.informativeText = error.localizedDescription; alert.runModal()
        }
    }
    @objc func setCursorSize(_ sender: NSMenuItem) {
        guard let scale = CursorSizeMenu.scale(forTag: sender.tag) else { return }
        do {
            try commandPayload(["action": "profile_cursor", "scale": scale])
        } catch {
            let alert = NSAlert(); alert.messageText = "未能保存光标大小"
            alert.informativeText = "请稍后重试。"; alert.runModal()
        }
    }
    @objc func permission() {
        if permissionGuide == nil { permissionGuide = PermissionGuideWindowController(model: permissionModel) }
        permissionGuide?.show()
    }
    @objc func openFolder() { NSWorkspace.shared.open(supportURL) }
    @objc func help() {
        let alert = NSAlert(); alert.messageText = StatusCopy.product
        alert.informativeText = "UU 远程连接时，自动把 Mac 桌面接力到设备专用的虚拟屏；断开后恢复实体屏。\n\n辅助功能权限仅用于读取 UU 的连接状态。\n紧急恢复并暂停：⌃⌥⌘9"
        alert.addButton(withTitle: "好")
        alert.addButton(withTitle: "打开状态文件夹")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertSecondButtonReturn { openFolder() }
    }
    @objc func quit() {
        if permissionRefreshInProgress { permissionModel.endConfirmation() }
        beginQuit()
    }
    @objc func quitRequested(_ notification: Notification) {
        guard let id = notification.object as? String, !id.isEmpty, id.utf8.count <= 200 else { return }
        if permissionRefreshInProgress { permissionModel.endConfirmation() }
        beginQuit(commandID: id)
    }
    func beginQuit(commandID: String = UUID().uuidString, preserveIntent: Bool = false) {
        guard !quitting else { return }
        if !permissionRefreshInProgress { permissionModel.endConfirmation() }
        func dictionary(_ name: String) -> [String: Any]? {
            guard let data = try? Data(contentsOf: supportURL.appendingPathComponent(name)) else { return nil }
            return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        }
        let previousCommand = dictionary("command.json")
        let previousID = previousCommand?["id"] as? String
        let preserveIntent = ExitHandoff.preserveEnabledIntent(requested: preserveIntent, previousCommand: previousCommand)
        let requestedAt = Date().timeIntervalSince1970
        do {
            try commandPayload(["id": commandID, "action": "recover", "exit_after_restore": true,
                                "preserve_enabled_after_exit": preserveIntent,
                                "previous_command_id": previousID as Any? ?? NSNull()])
        } catch {
            let alert = NSAlert(); alert.messageText = "未能请求恢复"
            alert.informativeText = error.localizedDescription
            alert.runModal(); return
        }
        quitting = true; terminationApproved = false; popover?.close(); supervisor.requestedExitID = commandID
        supervisor.requestedExitAt = requestedAt; supervisor.requestedExitAllowsResume = preserveIntent
        supervisor.ensureRunning()
        func waitForRestore() {
            let latest = dictionary("command.json")
            let laterAction = latest?["action"] as? String ?? ""
            let ordinaryManual = ["pause", "recover"].contains(laterAction) && latest?["exit_after_restore"] as? Bool != true
            if ordinaryManual, latest?["id"] as? String != commandID, latest?["id"] as? String != previousID {
                if permissionRefreshInProgress { permissionModel.cancelPending() }
                else {
                    quitting = false; quitProgressCheck = nil; quitWindowDeadline?.cancel()
                    supervisor.requestedExitID = nil; supervisor.requestedExitAt = nil; supervisor.ensureRunning()
                }
                return
            }
            if permissionRefreshInProgress, laterAction == "enable",
               latest?["id"] as? String != commandID, latest?["id"] as? String != previousID {
                permissionModel.cancelPending()
                return
            }
            if latest?["action"] as? String == "enable", latest?["id"] as? String != commandID,
               latest?["id"] as? String != previousID {
                quitting = false; quitProgressCheck = nil; quitWindowDeadline?.cancel()
                supervisor.requestedExitID = nil; supervisor.requestedExitAt = nil; supervisor.ensureRunning()
                return
            }
            if ExitHandoff.isComplete(state: dictionary("state.json"), status: dictionary("status.json"),
                                      cursor: dictionary("cursor-profile-state.json"), commandID: commandID,
                                      requestedAt: requestedAt, allowResume: preserveIntent) {
                if permissionRefreshInProgress {
                    guard !sleeping else { return }
                    guard permissionModel.markReady(workbenchState: permissionDraftState,
                                                    showWorkbench: permissionWorkbenchWasVisible) else {
                        permissionModel.fail("检测服务暂时无法接管，当前服务已保留。请稍后重新检测。")
                        return
                    }
                }
                terminationApproved = true; quitProgressCheck = nil; quitWindowDeadline?.cancel()
                supervisor.stop(terminateController: false)
                NSApp.terminate(nil)
            }
        }
        quitProgressCheck = waitForRestore
        let hideWindow = DispatchWorkItem { [weak self] in
            guard let self, self.quitting, !self.terminationApproved else { return }
            // Keep the supervisor resident during recovery, including after a
            // later backend crash. File events, not repeated polling, finish Quit.
            self.workbench?.close(); self.popover?.close(); self.refreshStatusIcon()
        }
        quitWindowDeadline = hideWindow
        DispatchQueue.main.asyncAfter(deadline: .now() + 20, execute: hideWindow)
        waitForRestore()
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminationApproved { return .terminateNow }
        let event = NSAppleEventManager.shared().currentAppleEvent
        let reason = (event?.paramDescriptor(forKeyword: AEKeyword(kAEQuitReason)) ??
                      event?.attributeDescriptor(forKeyword: AEKeyword(kAEQuitReason)))?.enumCodeValue
        let systemExit = reason.map { [AEKeyword(kAEShutDown), AEKeyword(kAERestart),
                                      AEKeyword(kAEReallyLogOut), AEKeyword(kAELogOut),
                                      AEKeyword(kAEQuitAll)].contains($0) } ?? false
        if quitting {
            if permissionRefreshInProgress { permissionModel.endConfirmation(); beginQuit(preserveIntent: systemExit) }
            return .terminateCancel
        }
        beginQuit(preserveIntent: systemExit)
        return .terminateCancel
    }
    func applicationWillTerminate(_ notification: Notification) {
        statusObserver?.stop()
        if !quitting { command("recover") }
        atomicJSON(Signal(state: "unknown", trusted: AXIsProcessTrusted(), overlay: false, controllers: [], reason: "detector_stopped", sleeping: false), name: "signal.json")
    }
}
// A dedicated LaunchServices role supplies fresh-process authorization evidence.
// It enters the normal single-instance path only after the old owner has safely exited.
let permissionBootstrap: PermissionTicket? = {
    guard let index = CommandLine.arguments.firstIndex(of: "--permission-probe") else { return nil }
    guard index + 1 < CommandLine.arguments.count else { exit(2) }
    let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""
    guard let ticket = PermissionProbeAgent.run(nonce: CommandLine.arguments[index + 1], directory: supportURL,
                                               applicationURL: Bundle.main.bundleURL, build: build) else { exit(0) }
    return ticket
}()
if CommandLine.arguments.contains("--storage-supervisor-probe") {
    // Only synthetic callbacks and this short-lived process PID are used.
    // No controller, AX, file writes, or actual termination is performed.
    let supervisor = ControllerSupervisor(); let pid = getpid()
    supervisor.adoptedPID = pid
    supervisor.controllerSeenAt = ProcessInfo.processInfo.systemUptime - 61
    supervisor.statusReader = { ["timestamp": 0.0] }
    supervisor.ownerReader = { pid }
    var terminations = 0
    supervisor.terminateAdopted = { _ in terminations += 1 }
    let gate = DispatchSemaphore(value: 0)
    supervisor.queue.async { gate.wait() }
    supervisor.healthCheck()
    let payload: [String: Any] = ["version": 1, "pid": Int(pid), "phase": "pressure",
        "free_bytes": 0, "retry_after": 60, "reason": "space", "recovery_pending": true, "mode": "RESTORING"]
    precondition(StorageHealthCenter.shared.accept(payload, expectedPID: pid))
    var drained = false
    supervisor.queue.async { DispatchQueue.main.async { drained = true } }
    gate.signal()
    let deadline = Date().addingTimeInterval(3)
    while !drained, Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
    precondition(drained && terminations == 0, "Fresh storage health must invalidate a queued termination")
    StorageHealthCenter.shared.clear()
    supervisor.healthCheck()
    let retryDeadline = Date().addingTimeInterval(3)
    while terminations == 0, Date() < retryDeadline { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
    precondition(terminations == 1, "A genuinely stale controller remains recoverable")
    print("Storage supervisor: queued stale checks respect a later healthy pressure heartbeat")
} else if let index = CommandLine.arguments.firstIndex(of: "--report-storage-health") {
    guard index + 1 < CommandLine.arguments.count else { exit(2) }
    let text = CommandLine.arguments[index + 1]
    guard text.utf8.count <= 8192, let data = text.data(using: .utf8),
          let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          StorageHealthSnapshot.decode(payload) != nil else { exit(2) }
    DistributedNotificationCenter.default().postNotificationName(StorageHealthCenter.distributedNotification,
        object: text, userInfo: nil, deliverImmediately: true)
} else if CommandLine.arguments.contains("--status-icon-probe") {
    let presentation = RuntimeStatusSnapshot.read(directory: supportURL).presentation()
    let data = try! JSONSerialization.data(withJSONObject: ["kind": presentation.kind.rawValue,
        "symbol": presentation.symbol, "label": presentation.label, "tooltip": presentation.tooltip], options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
} else if CommandLine.arguments.contains("--menu-probe") {
    _ = NSApplication.shared
    let owner = AppDelegate(); let menu = NSMenu()
    owner.menuWillOpen(menu)
    func describe(_ menu: NSMenu) -> [[String: Any]] {
        menu.items.map { item in
            var value: [String: Any] = ["title": item.title, "enabled": item.isEnabled,
                "tag": item.tag, "checked": item.state == .on]
            if let submenu = item.submenu { value["items"] = describe(submenu) }
            return value
        }
    }
    if let data = try? JSONSerialization.data(withJSONObject: describe(menu), options: [.sortedKeys]) {
        print(String(decoding: data, as: UTF8.self))
    }
} else if CommandLine.arguments.contains("--observer-probe") {
    let snapshot = Reader.controllers()
    var registrations: [[String: Any]] = []; var observer: AXObserver?
    var callbacks = 0
    if let pid = NSRunningApplication.runningApplications(withBundleIdentifier: "com.netease.uuremote").first?.processIdentifier {
        let created = AXObserverCreate(pid, { _, element, name, context in
            let role = Reader.attr(element, kAXRoleAttribute) as? String ?? "unavailable"
            fputs("observer-probe: \(name) role=\(role)\n", stderr)
            if let context { context.assumingMemoryBound(to: Int.self).pointee += 1 }
        }, &observer)
        if created == .success, let observer {
            withUnsafeMutablePointer(to: &callbacks) { pointer in
                for element in snapshot.observed {
                    for name in [kAXRowCountChangedNotification, kAXCreatedNotification, kAXUIElementDestroyedNotification, kAXWindowCreatedNotification] {
                        let result = AXObserverAddNotification(observer, element, name as CFString, pointer)
                        registrations.append(["notification": name, "result": result.rawValue, "role": Reader.attr(element, kAXRoleAttribute) as? String ?? "?", "row_count": Reader.rowCount(element) ?? -1])
                    }
                }
                CFRunLoopAddSource(CFRunLoopGetCurrent(), AXObserverGetRunLoopSource(observer), .defaultMode)
                CFRunLoopRunInMode(.defaultMode, 5, false)
                CFRunLoopRemoveSource(CFRunLoopGetCurrent(), AXObserverGetRunLoopSource(observer), .defaultMode)
            }
        }
    }
    let report: [String: Any] = ["trusted": AXIsProcessTrusted(), "reason": snapshot.reason, "elements": snapshot.observed.count, "registrations": registrations, "callbacks": callbacks]
    if let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]) { print(String(decoding: data, as: UTF8.self)) }
} else if CommandLine.arguments.contains("--permission-guide-preview"),
          let index = CommandLine.arguments.firstIndex(of: "--permission-guide-preview"), index + 1 < CommandLine.arguments.count {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let model = AccessibilityPermissionModel(directory: URL(fileURLWithPath: CommandLine.arguments[index + 1]),
        trust: { CommandLine.arguments.contains("--preview-granted") }, schedulePolling: false)
    model.requestSystemSettings = {}; model.revealCurrentApplication = {}
    model.launchProbe = { _, completion in completion(0, nil, CocoaError(.featureUnsupported)) }
    let window = PermissionGuideWindowController(model: model, expandTroubleshooting: CommandLine.arguments.contains("--preview-troubleshooting"))
    window.window?.title = "授权引导预览"
    window.window?.level = .floating
    window.window?.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
    if CommandLine.arguments.contains("--preview-dark") { window.window?.appearance = NSAppearance(named: .darkAqua) }
    window.show()
    if CommandLine.arguments.contains("--preview-issue") { model.fail("系统尚未允许当前版本访问辅助功能，请按下方步骤检查当前应用。") }
    DispatchQueue.main.async {
        NSApp.activate(ignoringOtherApps: true)
        window.window?.makeKeyAndOrderFront(nil)
        window.window?.makeFirstResponder(nil)
    }
    app.run()
} else if CommandLine.arguments.contains("--workbench-preview"),
          let index = CommandLine.arguments.firstIndex(of: "--workbench-preview"), index + 1 < CommandLine.arguments.count {
    // Isolated UI acceptance mode: no detector, supervisor, permission or display access.
    let directory = URL(fileURLWithPath: CommandLine.arguments[index + 1], isDirectory: true)
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    if CommandLine.arguments.contains("--preview-dark") {
        app.appearance = NSAppearance(named: .darkAqua)
    } else if CommandLine.arguments.contains("--preview-light") {
        app.appearance = NSAppearance(named: .aqua)
    }
    let officeSetupPreview = CommandLine.arguments.contains("--office-setup-preview")
    let previewActions = WorkbenchActions(officeSetup: MainActor.assumeIsolated { officeSetupPreview ? .preview() : nil })
    let window = WorkbenchWindowController(directory: directory, actions: previewActions,
                                           initialGeneralPage: officeSetupPreview) { payload in
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        try data.write(to: directory.appendingPathComponent("preview-command.json"), options: .atomic)
    }
    window.show()
    if let i = CommandLine.arguments.firstIndex(of: "--preview-size"), i + 1 < CommandLine.arguments.count {
        let parts = CommandLine.arguments[i + 1].split(separator: "x").compactMap { Double($0) }
        if parts.count == 2, parts.allSatisfy({ $0.isFinite && (300...2400).contains($0) }) {
            window.window?.minSize = NSSize(width: 300, height: 300)
            window.window?.setContentSize(NSSize(width: parts[0], height: parts[1]))
            window.window?.center()
        }
    }
    window.window?.level = .floating
    window.window?.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
    app.run()
} else if CommandLine.arguments.contains("--popover-preview"),
          let index = CommandLine.arguments.firstIndex(of: "--popover-preview"), index + 1 < CommandLine.arguments.count {
    // Isolated panel preview in an ordinary window: no detector, supervisor or display access.
    let directory = URL(fileURLWithPath: CommandLine.arguments[index + 1], isDirectory: true)
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let model = WorkbenchModel(directory: directory) { payload in
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        try data.write(to: directory.appendingPathComponent("preview-command.json"), options: .atomic)
    }
    let window = NSWindow(contentViewController: NSHostingController(rootView: StatusPopoverView(model: model, actions: PopoverActions())))
    window.title = "面板预览"; window.center(); window.makeKeyAndOrderFront(nil)
    app.activate(ignoringOtherApps: true)
    app.run()
} else if let index = CommandLine.arguments.firstIndex(of: "--request-quit") {
    let id = index + 1 < CommandLine.arguments.count ? CommandLine.arguments[index + 1] : UUID().uuidString
    guard !id.isEmpty, id.utf8.count <= 200 else { exit(2) }
    DistributedNotificationCenter.default().postNotificationName(quitAndRestoreNotification,
        object: id, userInfo: nil, deliverImmediately: true)
} else if CommandLine.arguments.contains("--open-workbench") {
    DistributedNotificationCenter.default().postNotificationName(openWorkbenchNotification,
        object: nil, userInfo: nil, deliverImmediately: true)
} else if CommandLine.arguments.contains("--request-session-refresh") {
    DistributedNotificationCenter.default().postNotificationName(refreshSessionNotification,
        object: nil, userInfo: nil, deliverImmediately: true)
} else if CommandLine.arguments.contains("--probe") {
    let value = Reader.signal(snapshot: Reader.controllers())
    let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
    if let json = try? encoder.encode(value) { print(String(decoding: json, as: UTF8.self)) }
} else if CommandLine.arguments.contains("--request-permission") {
    DistributedNotificationCenter.default().postNotificationName(permissionGuideNotification,
        object: nil, userInfo: nil, deliverImmediately: true)
} else {
    // Hold an advisory lock for the process lifetime; duplicate launches perform no commands.
    try? FileManager.default.createDirectory(at: supportURL, withIntermediateDirectories: true)
    let lockFD = open(supportURL.appendingPathComponent("detector.lock").path, O_CREAT | O_RDWR | O_CLOEXEC, S_IRUSR | S_IWUSR)
    guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { exit(0) }
    let app = NSApplication.shared
    let delegate = AppDelegate(); delegate.permissionTakeover = permissionBootstrap
    app.delegate = delegate
    if permissionBootstrap != nil {
        DispatchQueue.main.async { delegate.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification)) }
    }
    app.run()
}

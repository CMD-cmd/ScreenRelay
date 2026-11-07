import Foundation
import CoreFoundation
import Darwin

/// The detector can publish in memory even when a full disk prevents signal.json.
/// This cache contains only the current host's signal and creates no observer or timer.
final class NativeStatusSignal {
    static let shared = NativeStatusSignal()
    static let didChange = Notification.Name("local.codex.UUDisplayAutomation.NativeStatusChanged")
    private var directory: URL?
    private var value: [String: Any]?
    func update<T: Encodable>(_ signal: T, directory: URL) {
        guard let data = try? JSONEncoder().encode(signal),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        self.directory = directory.standardizedFileURL; value = object
        NotificationCenter.default.post(name: Self.didChange, object: self)
    }
    func current(in directory: URL) -> [String: Any]? {
        self.directory == directory.standardizedFileURL ? value : nil
    }
}

/// All surfaces use status.json for live execution and the same policy result
/// for connectivity. workbench-status supplies saved settings and receipts.
struct RuntimeStatusSnapshot {
    let status: [String: Any]?
    let workbench: [String: Any]?
    let connection: String
    let trusted: Bool
    let sleeping: Bool
    let controllers: [String]
    let ready: Bool
    let operational: Bool
    let pendingID: String?
    let error: String?
    let expiry: TimeInterval?

    var mode: String { status?["mode"] as? String ?? "" }
    var enabled: Bool? { status?["desired_enabled"] as? Bool }
    var activeID: String? { mode == "ACTIVE" ? status?["active_profile_id"] as? String : nil }

    func presentation(now: TimeInterval = Date().timeIntervalSince1970,
                      storage: StorageHealthSnapshot? = StorageHealthCenter.shared.active()) -> StatusBarPresentation {
        StatusBarPresentation.resolve(connection: connection, trusted: trusted, sleeping: sleeping,
                                      status: status, now: now, storage: storage)
    }

    static func record(_ name: String, in directory: URL) -> [String: Any]? {
        guard let data = try? Data(contentsOf: directory.appendingPathComponent(name)) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    static func read(directory: URL, now: TimeInterval = Date().timeIntervalSince1970) -> Self {
        let status = record("status.json", in: directory)
        let pid = integer(status?["controller_pid"])
        return resolve(status: status, workbench: record("workbench-status.json", in: directory),
            raw: NativeStatusSignal.shared.current(in: directory) ?? record("signal.json", in: directory),
            effective: record("effective-session.json", in: directory),
            cursor: record("cursor-profile-state.json", in: directory),
            cursorConfig: record("cursor-profile-config.json", in: directory),
            receipts: recordArray("command-results.json", in: directory), now: now,
            backendAlive: pid.map { $0 > 1 && $0 <= Int(Int32.max) && kill(pid_t($0), 0) == 0 } ?? false)
    }

    private static func recordArray(_ name: String, in directory: URL) -> [[String: Any]] {
        guard let data = try? Data(contentsOf: directory.appendingPathComponent(name)) else { return [] }
        return (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] ?? []
    }
    private static func number(_ value: Any?) -> Double? {
        guard let value = value as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
              value.doubleValue.isFinite else { return nil }
        return value.doubleValue
    }
    private static func integer(_ value: Any?) -> Int? {
        guard let value = number(value), value.rounded() == value,
              value > 0, value < Double(Int.max) else { return nil }
        return Int(value)
    }
    private static func fresh(_ value: Any?, _ now: Double) -> Bool {
        guard let stamp = number(value), now.isFinite else { return false }
        return 0 <= now - stamp && now - stamp < 90
    }
    private static func sameGeneration(_ a: [String: Any], _ b: [String: Any]) -> Bool {
        guard let run = a["run_id"] as? String, !run.isEmpty, run == b["run_id"] as? String,
              let detector = integer(a["detector_pid"]), detector == integer(b["detector_pid"]),
              let uu = integer(a["uuPID"]), uu == integer(b["uuPID"]) else { return false }
        for key in ["event_id", "display_revision"] where a[key] != nil {
            guard let value = number(a[key]), value == number(b[key]) else { return false }
        }
        return true
    }

    static func resolve(status original: [String: Any]?, workbench: [String: Any]?,
                        raw: [String: Any]?, effective: [String: Any]?,
                        cursor: [String: Any]? = nil, cursorConfig: [String: Any]? = nil,
                        receipts: [[String: Any]] = [], now: Double,
                        backendAlive: Bool = true) -> Self {
        var status = original
        let statusFresh = fresh(status?["timestamp"], now) && integer(status?["controller_pid"]) != nil
        if !backendAlive, statusFresh { status?["backend_unavailable"] = true }
        let compatible = (workbench?["version"] as? Int == 1
            && fresh(workbench?["timestamp"], now)
            && integer(workbench?["controller_pid"]) == integer(status?["controller_pid"]))
        let rows = workbench?["profiles"] as? [[String: Any]]
        let configurationValid = compatible && rows?.allSatisfy({ row in
            row["id"] is String && row["label"] is String && integer(row["native_width"]) != nil
                && integer(row["native_height"]) != nil && number(row["ppi"]) != nil
        }) == true
        if compatible && !configurationValid { status?["configuration_error"] = "设备配置状态暂时无法读取，已保留当前草稿。" }
        let raw = raw ?? [:]
        let rawFresh = fresh(raw["timestamp"], now)
        let trusted = raw["trusted"] as? Bool ?? true
        let sleeping = raw["sleeping"] as? Bool ?? false
        var connection = "unknown"
        var controllers: [String] = []
        let verifiedNegative = raw["state"] as? String == "disconnected" &&
            (raw["reason"] as? String == "uu_not_running" ||
             (raw["session_negative_verified"] as? Bool == true &&
              ["ax_complete_home_no_local_control", "ax_cached_controller_table_empty"].contains(raw["reason"] as? String ?? "")))
        if rawFresh, trusted, !sleeping {
            if verifiedNegative { connection = "disconnected" }
            else if let effective, sameGeneration(raw, effective),
                    integer(effective["policy_controller_pid"]) == integer(status?["controller_pid"]),
                    fresh(effective["native_timestamp"] ?? effective["raw_timestamp"] ?? effective["timestamp"], now),
                    fresh(effective["policy_evaluated_at"], now),
                    effective["trusted"] as? Bool == true, effective["sleeping"] as? Bool == false {
                let state = effective["state"] as? String ?? "unknown"
                if ["connected", "disconnected"].contains(state) { connection = state }
                controllers = effective["controllers"] as? [String] ?? []
            }
        }
        let latest = workbench?["last_result"] as? [String: Any]
        var pendingID: String?
        if compatible, latest?["status"] as? String == "applying", let id = latest?["id"] as? String,
           !receipts.contains(where: { $0["id"] as? String == id && ["ok", "error"].contains($0["status"] as? String ?? "") }) {
            pendingID = id; status?["operation_pending"] = true
        }
        let ordinary = ["manual", "manual_stop", "remote_disconnected", "sleep", "profile_change", "process_restart", "device_change"]
        let context = status?["recovery_context"] as? [String: Any] ?? [:]
        let engineError = status?["last_error"] as? String
        let progress = ordinary.contains(context["category"] as? String ?? "") && engineError == context["reason"] as? String
        var error = progress ? nil : engineError
        if let issue = status?["configuration_error"] as? String { error = issue }
        if compatible, let configurationError = workbench?["configuration_error"] as? String, !configurationError.isEmpty {
            status?["configuration_error"] = configurationError
            error = configurationError
        }
        if status?["mode"] as? String == "ACTIVE", let cursor {
            let cursorState = cursor["status"] as? String ?? ""
            if let fault = cursor["error"] as? String, !fault.isEmpty {
                status?["cursor_error"] = fault
                error = "远程光标调整尚未完成，正在重试；当前屏幕保持不变。"
            } else if let cursorConfig {
                let desired = cursor["desired_profile"] as? [String: Any]
                let enabled = cursorConfig["enabled"] as? Bool == true
                if (enabled && (cursorState != "active" || number(desired?["scale"]) != number(cursorConfig["scale"]))) ||
                    (!enabled && !["idle", "restored"].contains(cursorState)) {
                    status?["cursor_pending"] = true
                }
            }
        }
        let deadlines = [original?["timestamp"], raw["timestamp"], workbench?["timestamp"]].compactMap(number)
            .map { $0 + 90.01 }.filter { $0 > now }
        return Self(status: status, workbench: workbench, connection: connection, trusted: trusted,
                    sleeping: sleeping, controllers: controllers,
                    ready: statusFresh && backendAlive && configurationValid,
                    operational: statusFresh && backendAlive,
                    pendingID: pendingID, error: error, expiry: deadlines.min())
    }
}

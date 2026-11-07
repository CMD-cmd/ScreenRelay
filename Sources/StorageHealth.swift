import Foundation
import CoreFoundation

/// Storage pressure arrives in memory because status files may be unwritable.
struct StorageHealthSnapshot {
    let pid: pid_t
    let phase: String
    let freeBytes: Int64?
    let retryAfter: TimeInterval
    let recoveryPending: Bool
    let mode: String
    let reason: String

    static func decode(_ payload: [String: Any]) -> Self? {
        guard integer(payload["version"]) == 1,
              let process = integer(payload["pid"]), process > 1, process <= Int64(Int32.max),
              let phase = payload["phase"] as? String, ["pressure", "healthy"].contains(phase),
              let reason = payload["reason"] as? String, ["space", "quota"].contains(reason),
              let retry = payload["retry_after"] as? NSNumber,
              CFGetTypeID(retry) != CFBooleanGetTypeID(), retry.doubleValue.isFinite, retry.doubleValue >= 0,
              let pending = payload["recovery_pending"] as? NSNumber,
              CFGetTypeID(pending) == CFBooleanGetTypeID(),
              let mode = payload["mode"] as? String,
              let free = payload["free_bytes"] else { return nil }
        let freeBytes: Int64?
        if free is NSNull { freeBytes = nil }
        else {
            guard let value = integer(free), value >= 0 else { return nil }
            freeBytes = value
        }
        return Self(pid: pid_t(process), phase: phase, freeBytes: freeBytes,
                    retryAfter: retry.doubleValue, recoveryPending: pending.boolValue, mode: mode, reason: reason)
    }

    private static func integer(_ value: Any?) -> Int64? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              ["c", "C", "s", "S", "i", "I", "l", "L", "q", "Q"].contains(String(cString: number.objCType)) else { return nil }
        return Int64(number.stringValue)
    }

    var message: String {
        let problem = reason == "quota" ? "磁盘配额已满" : "磁盘空间不足"
        let available: String
        if let freeBytes {
            let units: [(Double, String)] = [(1_073_741_824, "GB"), (1_048_576, "MB"), (1024, "KB")]
            if let unit = units.first(where: { Double(freeBytes) >= $0.0 }) {
                available = String(format: "（可用 %.1f %@）", Double(freeBytes) / unit.0, unit.1)
            } else { available = "（可用 \(freeBytes) B）" }
        } else { available = "" }
        return "\(problem)\(available)，已停止新的切屏。现有恢复记录保留；清理空间后自动继续。"
    }
}

/// Main-thread owned; neither file access nor a polling timer is needed.
final class StorageHealthCenter {
    static let shared = StorageHealthCenter()
    static let distributedNotification = Notification.Name("local.codex.UUDisplayAutomation.StorageHealth")
    static let didChange = Notification.Name("local.codex.UUDisplayAutomation.StorageHealthChanged")
    private var snapshot: StorageHealthSnapshot?
    private var receivedAt: TimeInterval?

    @discardableResult
    func accept(_ payload: [String: Any], expectedPID: pid_t,
                now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> Bool {
        guard now.isFinite, now >= 0, let decoded = StorageHealthSnapshot.decode(payload),
              expectedPID > 1, decoded.pid == expectedPID,
              receivedAt.map({ now >= $0 }) ?? true else { return false }
        snapshot = decoded.phase == "pressure" ? decoded : nil
        receivedAt = now
        NotificationCenter.default.post(name: Self.didChange, object: self)
        return true
    }

    func active(now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> StorageHealthSnapshot? {
        guard now.isFinite, now >= 0, let receivedAt, now >= receivedAt,
              now - receivedAt <= 120, let snapshot, snapshot.phase == "pressure" else { return nil }
        return snapshot
    }

    func clear() {
        snapshot = nil
        receivedAt = nil
        NotificationCenter.default.post(name: Self.didChange, object: self)
    }
}

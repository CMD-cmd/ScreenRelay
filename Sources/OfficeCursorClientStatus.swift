import Foundation
import CoreFoundation

struct OfficeCursorClientStatus {
    let host: OfficeCursorHost
    let pid: Int32
    let installed: Bool
    let enabled: Bool
    let conflict: Bool
    let scale: Double
    let moduleVersion: Int
    let libraryURL: URL
    let timestamp: Double
    let requestID: String
    let error: String

    static let probe = Notification.Name("local.codex.UUDisplayAutomation.OfficeCursorClientProbe")
    static let reply = Notification.Name("local.codex.UUDisplayAutomation.OfficeCursorClientStatus")

    init?(object: Any?, now: Double) {
        guard let string = object as? String, let data = string.data(using: .utf8), data.count <= 16 * 1024,
              let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let protocolNumber = Self.number(raw["protocol"]), protocolNumber.doubleValue == 2,
              let name = raw["host"] as? String,
              let host = OfficeCursorHost.allCases.first(where: { $0.bundleID == name }),
              let pid = Self.number(raw["pid"]), pid.doubleValue.rounded() == pid.doubleValue,
              pid.doubleValue > 1, pid.doubleValue <= Double(Int32.max),
              let installed = Self.boolean(raw["installed"]), let enabled = Self.boolean(raw["enabled"]),
              let conflict = Self.boolean(raw["conflict"]),
              let scale = Self.number(raw["scale"]), [1, 1.5, 2, 2.5, 3, 3.5, 4].contains(scale.doubleValue),
              let version = Self.number(raw["module_version"]), version.doubleValue.rounded() == version.doubleValue,
              version.intValue > 0, version.intValue <= 1000,
              let timestamp = Self.number(raw["timestamp"]), timestamp.doubleValue.isFinite,
              now - timestamp.doubleValue >= -5, now - timestamp.doubleValue <= 15,
              let path = raw["library_path"] as? String, path.hasPrefix("/"), !path.contains("\0"), path.utf8.count <= 8192,
              let request = raw["request_id"] as? String, request.utf8.count <= 128,
              let error = raw["error"] as? String, error.utf8.count <= 200 else { return nil }
        self.host = host; self.pid = pid.int32Value; self.installed = installed; self.enabled = enabled
        self.conflict = conflict; self.scale = scale.doubleValue; self.moduleVersion = version.intValue
        libraryURL = URL(fileURLWithPath: path).standardizedFileURL
        self.timestamp = timestamp.doubleValue; requestID = request; self.error = error
    }

    /// A fresh probe establishes the baseline for each actual Office process.
    /// Subsequent unsolicited reports can then update it without a timer.
    func belongs(to epoch: OfficeCursorProcessEpoch, pendingRequest: String?, previous: Self?) -> Bool {
        guard pid == epoch.pid, timestamp >= epoch.launchedAt,
              previous.map({ timestamp >= $0.timestamp }) ?? true else { return false }
        if requestID.isEmpty { return previous != nil }
        return requestID == pendingRequest
    }

    private static func boolean(_ value: Any?) -> Bool? {
        guard let value = value as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID() else { return nil }
        return value.boolValue
    }
    private static func number(_ value: Any?) -> NSNumber? {
        guard let value = value as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite else { return nil }
        return value
    }
}

struct OfficeCursorProcessEpoch: Equatable {
    let pid: Int32
    let launchedAt: TimeInterval
}

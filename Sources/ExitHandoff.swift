import Foundation
import CoreFoundation

/// An old PAUSED snapshot is not proof that this exit request has recovered.
enum ExitHandoff {
    /// A newer durable pause must survive a system exit before the controller
    /// has had a chance to consume the single-slot command mailbox.
    static func preserveEnabledIntent(requested: Bool, previousCommand: [String: Any]?) -> Bool {
        let action = previousCommand?["action"] as? String ?? ""
        let manualPause = ["pause", "recover"].contains(action) && boolean(previousCommand?["exit_after_restore"]) != true
        return requested && !manualPause
    }
    static func isComplete(state: [String: Any]?, status: [String: Any]?, cursor: [String: Any]?,
                           commandID: String, requestedAt: TimeInterval, allowResume: Bool = false,
                           now: TimeInterval = Date().timeIntervalSince1970) -> Bool {
        guard let state, let status, let cursor, !commandID.isEmpty,
              let completed = state["exit_completed"] as? [String: Any],
              let published = status["exit_completed"] as? [String: Any],
              completed["id"] as? String == commandID, published["id"] as? String == commandID,
              state["last_command"] as? String == commandID, status["last_command"] as? String == commandID,
              let stamp = completed["timestamp"] as? Double, stamp.isFinite,
              let updated = status["timestamp"] as? Double, updated.isFinite,
              stamp >= requestedAt, stamp <= now, updated >= stamp, updated <= now,
              now - updated <= 90,
              let pid = completed["controller_pid"] as? Int, pid > 1,
              published["controller_pid"] as? Int == pid, status["controller_pid"] as? Int == pid,
              (state["journal"] == nil || state["journal"] is NSNull),
              (status["journal"] == nil || status["journal"] is NSNull),
              boolean(status["recovery_pending"]) == false,
              state["exit_after_restore"] == nil || state["exit_after_restore"] is NSNull else { return false }
        let resumed = boolean(completed["resume_enabled"]) ?? false
        guard !resumed || allowResume,
              boolean(state["desired_enabled"]) == resumed, boolean(status["desired_enabled"]) == resumed,
              state["mode"] as? String == (resumed ? "ARMED" : "PAUSED"),
              status["mode"] as? String == (resumed ? "ARMED" : "PAUSED") else { return false }
        guard let pending = cursor["pending_originals"] as? [Any], pending.isEmpty,
                  ["idle", "restored"].contains(cursor["status"] as? String ?? ""),
                  cursor["error"] == nil || cursor["error"] is NSNull,
                  cursor["desired_profile"] == nil || cursor["desired_profile"] is NSNull,
              cursor["original_profile"] == nil || cursor["original_profile"] is NSNull else { return false }
        return true
    }

    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
}

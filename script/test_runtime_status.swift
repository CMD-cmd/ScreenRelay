import AppKit

@main struct RuntimeStatusTests {
    static func main() {
        var checks = 0
        func check(_ condition: Bool, _ message: String) { precondition(condition, message); checks += 1 }
        let raw: [String: Any] = ["timestamp": 1000.0, "state": "unknown", "trusted": true, "sleeping": false,
            "run_id": "generation", "detector_pid": 100, "uuPID": 200, "event_id": 10, "display_revision": 3,
            "reason": "uu_ui_hidden"]
        let effective = raw.merging(["state": "connected", "raw_timestamp": 1000.0,
            "policy_evaluated_at": 1000.0, "session_held": true, "session_observed_at": 1.0,
            "controllers": ["iPad"], "policy_controller_pid": 300]) { _, new in new }
        let status: [String: Any] = ["mode": "ACTIVE", "timestamp": 1000.0, "controller_pid": 300,
                                    "desired_enabled": true, "active_profile_id": "ipad"]
        let profile: [String: Any] = ["id": "ipad", "label": "iPad", "native_width": 2752, "native_height": 2064, "ppi": 264.0]
        let workbench: [String: Any] = ["version": 1, "timestamp": 1000.0, "controller_pid": 300,
            "profiles": [profile], "mode": "PAUSED", "desired_enabled": false, "error": NSNull()]
        func resolve(_ native: [String: Any] = raw, _ policy: [String: Any]? = effective,
                     _ state: [String: Any] = status, _ config: [String: Any] = workbench,
                     alive: Bool = true) -> RuntimeStatusSnapshot {
            RuntimeStatusSnapshot.resolve(status: state, workbench: config, raw: native,
                                          effective: policy, now: 1000, backendAlive: alive)
        }
        func kind(_ value: RuntimeStatusSnapshot) -> StatusBarPresentation.Kind { value.presentation(now: 1000, storage: nil).kind }
        check(kind(resolve()) == .remote, "hidden continuity uses backend policy")
        check(resolve().enabled == true && resolve().mode == "ACTIVE", "live status wins over older workbench mode and intent")
        check(resolve().controllers == ["iPad"], "effective identity stays with connection")
        for reason in ["uu_ui_hidden", "uu_ui_minimized", "ax_snapshot_incomplete"] {
            check(kind(resolve(raw.merging(["reason": reason]) { _, n in n })) == .remote, reason)
        }
        var probe = effective
        probe["session_held"] = false; probe["native_timestamp"] = 1000.0; probe["session_verification"] = ["verified_at": 1000.0]
        check(kind(resolve(raw, probe)) == .remote, "one-shot verification uses native heartbeat")
        for field in ["run_id", "detector_pid", "uuPID", "event_id", "display_revision"] {
            var changed = raw; changed[field] = field == "run_id" ? "new generation" : 999
            check(kind(resolve(changed)) == .checking, "old effective rejected: " + field)
        }
        var missingPID = raw; missingPID.removeValue(forKey: "uuPID")
        check(kind(resolve(missingPID)) == .checking, "incomplete identity cannot hold a prior connection")
        check(kind(resolve(raw, effective.merging(["policy_controller_pid": 299]) { _, n in n })) == .checking,
              "prior backend's held connection cannot cross a restart")
        for reason in ["ax_complete_home_no_local_control", "ax_cached_controller_table_empty", "uu_not_running"] {
            let negative = raw.merging(["state": "disconnected", "reason": reason, "session_negative_verified": true]) { _, n in n }
            check(resolve(negative).connection == "disconnected", "verified negative takes precedence")
        }
        check(kind(resolve(raw.merging(["trusted": false]) { _, n in n })) == .attention, "permission loss is immediate")
        check(kind(resolve(raw.merging(["sleeping": true]) { _, n in n })) == .sleeping, "sleep is immediate")
        for stamp in [910.0, 1001.0, Double.nan] {
            var stale = effective; stale["raw_timestamp"] = stamp
            check(kind(resolve(raw, stale)) == .checking, "bad native heartbeat rejected")
            stale = effective; stale["policy_evaluated_at"] = stamp
            check(kind(resolve(raw, stale)) == .checking, "bad policy timestamp rejected")
        }
        let oldPositive = raw.merging(["state": "connected", "session_observed_at": 1.0]) { _, n in n }
        let unknown = effective.merging(["state": "unknown"]) { _, n in n }
        check(kind(resolve(oldPositive, unknown)) == .checking, "never fall back to an old raw positive")
        check(kind(resolve(raw, nil)) == .checking, "new events wait for their own policy result")
        for mode in ["PAUSED", "ARMED", "RESTORING"] {
            let state = status.merging(["mode": mode, "desired_enabled": mode == "ARMED"]) { _, n in n }
            let expected: StatusBarPresentation.Kind = mode == "PAUSED" ? .paused : .switching
            check(kind(resolve(raw, effective, state)) == expected, "live mode " + mode)
        }
        check(kind(resolve(raw, effective, status.merging(["waiting_for": "physical displays online"]) { _, n in n })) == .switching,
              "intermediate execution heartbeat is visible")
        check(kind(resolve(alive: false)) == .checking && !resolve(alive: false).ready, "dead backend cannot remain remote")
        check(!resolve(raw, effective, status.merging(["timestamp": 909.0]) { _, n in n }).ready, "stale live status disables commands")
        check(!resolve(raw, effective, status, workbench.merging(["controller_pid": 299]) { _, n in n }).ready, "old process configuration is not actionable")
        let applying = workbench.merging(["last_result": ["id": "pending", "status": "applying"]]) { _, n in n }
        check(kind(resolve(raw, effective, status, applying)) == .applying, "operation shared across surfaces")
        let terminal = RuntimeStatusSnapshot.resolve(status: status, workbench: applying, raw: raw, effective: effective,
            receipts: [["id": "pending", "status": "ok"]], now: 1000)
        check(terminal.pendingID == nil && kind(terminal) == .remote, "terminal result wins over a delayed projection")
        let cursor = ["status": "active", "desired_profile": ["scale": 2.0]] as [String: Any]
        let cursorConfig: [String: Any] = ["enabled": true, "scale": 3.0]
        let pendingCursor = RuntimeStatusSnapshot.resolve(status: status, workbench: workbench, raw: raw,
            effective: effective, cursor: cursor, cursorConfig: cursorConfig, now: 1000)
        check(kind(pendingCursor) == .applying, "saved cursor size is not yet actual completion")
        let failedCursor = RuntimeStatusSnapshot.resolve(status: status, workbench: workbench, raw: raw,
            effective: effective, cursor: ["status": "recovery_pending", "error": "helper failed"], now: 1000)
        check(kind(failedCursor) == .attention, "cursor failure is visible everywhere")
        check(kind(resolve(raw, effective, status, workbench.merging(["configuration_error": "configuration unavailable"]) { _, n in n })) == .attention,
              "configuration fault has one status")
        let clearedFault = workbench.merging(["error": "old engine failure", "status_detail": ["last_error": "old engine failure"]]) { _, n in n }
        check(kind(resolve(raw, effective, status, clearedFault)) == .remote,
              "a delayed workbench error cannot revive an engine fault already cleared in status")
        let activeCursorFault = RuntimeStatusSnapshot.resolve(status: status, workbench: workbench, raw: raw,
            effective: effective, cursor: ["status": "active", "error": "new profile validation failed", "desired_profile": ["scale": 3]],
            cursorConfig: cursorConfig, now: 1000)
        check(kind(activeCursorFault) == .attention, "active cursor can still reject the newly requested size")
        print("Runtime status: \(checks) checks passed")
    }
}

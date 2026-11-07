import AppKit

@main struct WorkbenchStateTests {
    static func main() throws {
        var checks = 0
        func check(_ condition: Bool, _ message: String) { precondition(condition, message); checks += 1 }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        var now = Date().timeIntervalSince1970
        let pid = Int(getpid())
        func write(_ name: String, _ value: [String: Any]) throws {
            try JSONSerialization.data(withJSONObject: value).write(to: directory.appendingPathComponent(name), options: .atomic)
        }
        var status: [String: Any] = ["mode": "ACTIVE", "timestamp": now, "controller_pid": pid,
            "desired_enabled": true, "active_profile_id": "ipad"]
        var workbench: [String: Any] = ["version": 1, "mode": "ACTIVE", "timestamp": now, "controller_pid": pid,
            "desired_enabled": true, "selected_id": "ipad", "active_profile_id": "ipad", "automatic": true,
            "profiles": [["id": "ipad", "label": "iPad", "native_width": 2752, "native_height": 2064,
                "ppi": 264.0, "resolution": "1280x960", "orientation": "landscape", "cursor_scale": 3.0]]]
        var raw: [String: Any] = ["timestamp": now, "state": "unknown", "reason": "uu_ui_minimized", "trusted": true,
            "sleeping": false, "run_id": "run", "detector_pid": pid, "uuPID": 200, "event_id": 1, "display_revision": 1]
        let effective = raw.merging(["state": "connected", "raw_timestamp": now, "policy_evaluated_at": now,
            "session_held": true, "controllers": ["iPad"], "policy_controller_pid": pid]) { _, n in n }
        try write("status.json", status); try write("workbench-status.json", workbench)
        try write("signal.json", raw); try write("effective-session.json", effective)
        var commands: [[String: Any]] = []
        let model = WorkbenchModel(directory: directory, clock: { now }, sendCommand: { commands.append($0) })
        defer { model.suspend() }
        check(model.statusReady && model.connected && model.presentation.kind == .remote, "initial policy matches native header")
        model.update { $0.label = "unsaved device name" }
        check(model.dirty, "draft retained")
        model.update { $0.width = "unfinished width" }
        let handoff = try model.permissionDraftState()
        let replacement = WorkbenchModel(directory: directory, clock: { now }, sendCommand: { _ in
            preconditionFailure("restoring drafts must not send any command")
        })
        replacement.suspend()
        replacement.restorePermissionDraftState(handoff)
        check(replacement.dirty && replacement.draft.label == "unsaved device name" && replacement.draft.width == "unfinished width",
              "closed replacement preserves incomplete unapplied draft without applying it")
        replacement.restorePermissionDraftState(Data("{}".utf8))
        check(replacement.draft.width == "unfinished width", "invalid handoff leaves current draft untouched")
        replacement.addDevice(); replacement.update { $0.label = "incomplete new device" }
        let unsavedID = replacement.selection
        let newDeviceHandoff = try replacement.permissionDraftState()
        let next = WorkbenchModel(directory: directory, clock: { now }, sendCommand: { _ in
            preconditionFailure("new device draft must remain unapplied")
        })
        next.suspend(); next.restorePermissionDraftState(newDeviceHandoff)
        check(next.selection == unsavedID && next.draft.label == "incomplete new device" && next.unsavedKeys.contains(unsavedID),
              "unsaved new device survives service takeover")
        model.update { $0.width = "2752" }
        status["mode"] = "RESTORING"; status["desired_enabled"] = false
        try write("status.json", status); model.reload()
        check(model.mode == "RESTORING" && !model.enabled, "status changes before workbench projection")
        check(model.presentation.kind == .switching && model.busy && !model.canApply, "restoration is visible and serialized")
        check(model.draft.label == "unsaved device name", "intermediate publication preserves unsaved input")
        status["mode"] = "ARMED"; status["desired_enabled"] = true
        try write("status.json", status); model.reload()
        model.core("enable")
        check(model.pendingID != nil && commands.count == 1, "request waits for its receipt")
        raw["trusted"] = false; try write("signal.json", raw); model.reload()
        check(model.presentation.kind == .attention && model.statusText == model.presentation.label, "pending request cannot hide lost permission")
        raw["sleeping"] = true; try write("signal.json", raw); model.reload()
        check(model.presentation.kind == .sleeping && model.statusText == model.presentation.label, "pending request cannot hide sleep")
        raw["trusted"] = true; raw["sleeping"] = false; try write("signal.json", raw)
        workbench["results"] = [["id": model.pendingID!, "status": "error", "message": "injected failure"]]
        try write("workbench-status.json", workbench); model.reload()
        check(model.pendingID == nil && model.failure != nil, "failed request settles without optimistic success")
        check(model.currentDevice?.cursor == 3 && model.draft.label == "unsaved device name", "saved cursor and editable draft stay separate")
        now += 91; model.reload()
        check(!model.statusReady && !model.canApply && !model.connected && model.currentDevice == nil, "stale state is not actionable or live")
        check(model.presentation.kind == .attention, "stale snapshot is visible")
        try Data("{".utf8).write(to: directory.appendingPathComponent("status.json"), options: .atomic)
        model.reload()
        check(!model.statusReady && model.presentation.kind != .remote, "corrupt state cannot preserve remote label")
        try FileManager.default.removeItem(at: directory.appendingPathComponent("workbench-status.json")); model.reload()
        check(!model.statusReady && model.draft.label == "unsaved device name", "missing configuration preserves draft and blocks submit")
        let rejected = model.setCursor(4)
        check(!rejected && commands.count == 1, "stale cursor request is not sent")
        now = Date().timeIntervalSince1970
        status["timestamp"] = now; workbench["timestamp"] = now
        try write("status.json", status); try write("workbench-status.json", workbench); model.reload()
        let failedSender = WorkbenchModel(directory: directory, clock: { now }, sendCommand: { _ in throw CocoaError(.fileWriteOutOfSpace) })
        check(!failedSender.setCursor(4) && failedSender.pendingID == nil && failedSender.currentDevice?.cursor == 3,
              "send failure returns saved scale for the slider")
        failedSender.suspend()
        model.suspend(); check(!model.observing, "closed workbench releases observation")
        model.resume(); check(model.observing, "reopen takes a fresh snapshot")
        model.suspend()
        let nearExpiry = Date().timeIntervalSince1970 - 89.90
        status["timestamp"] = nearExpiry; workbench["timestamp"] = nearExpiry; raw["timestamp"] = nearExpiry
        try write("status.json", status); try write("workbench-status.json", workbench); try write("signal.json", raw)
        let expiring = WorkbenchModel(directory: directory, sendCommand: { _ in preconditionFailure("expiry must not send a command") })
        check(expiring.statusReady, "freshness boundary starts usable")
        let deadline = Date().addingTimeInterval(1)
        while expiring.statusReady, Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
        check(!expiring.statusReady && expiring.presentation.kind == .attention,
              "one-shot deadline invalidates an unchanged file without polling")
        expiring.suspend()
        print("Workbench state: \(checks) checks passed")
    }
}

// Isolated UI-model contract test. Uses only a fresh temporary directory and a recording sender.
import Foundation

@main
struct WorkbenchTests {
    static func main() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("workbench-test-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        var row: [String: Any] = ["id": "tablet", "label": "Tablet", "native_width": 2752,
            "native_height": 2064, "ppi": 264, "orientation": "landscape", "resolution": "1280x960", "cursor_scale": 3]
        let mini: [String: Any] = ["id": "mini", "label": "iPad mini", "native_width": 2266,
            "native_height": 1488, "ppi": 326]
        var status: [String: Any] = ["version": 1, "mode": "ARMED", "desired_enabled": true,
            "timestamp": Date().timeIntervalSince1970, "connection": "disconnected", "trusted": true,
            "controllers": [], "automatic": true, "selected_id": "tablet", "profiles": [row],
            "catalog": [mini], "presets": ["tablet": ["1376x1032", "1280x960"]],
            "identity": ["status": "none", "source": "name"]]
        func writeStatus() throws {
            let data = try JSONSerialization.data(withJSONObject: status)
            try data.write(to: directory.appendingPathComponent("workbench-status.json"), options: .atomic)
        }
        try writeStatus()
        var commands: [[String: Any]] = []
        let model = WorkbenchModel(directory: directory) { commands.append($0) }
        assert(model.statusReady && !model.dirty && !model.connected)
        assert(model.presentation.kind == .standby && model.observing)
        // The window shares the menu-bar resolver, including faults it previously could not see.
        for (mode, connection, kind) in [("ACTIVE", "connected", StatusBarPresentation.Kind.remote), ("PAUSED", "disconnected", .paused),
                                         ("DISABLED", "disconnected", .paused), ("RESTORING", "disconnected", .switching)] {
            status["mode"] = mode; status["connection"] = connection; try writeStatus(); model.reload()
            assert(model.presentation.kind == kind, "\(mode) resolves to \(kind)")
            assert(model.busy == (mode == "RESTORING"))
        }
        status["mode"] = "PAUSED"; status["status_detail"] = ["last_error": "Detector stale, untrusted, or unknown",
                                                              "recovery_context": ["category": "detector_unavailable"]]
        try writeStatus(); model.reload()
        assert(model.presentation.kind == .attention && model.banner != nil, "safety pause is visible in the window")
        status["status_detail"] = [:]; status["mode"] = "ACTIVE"; status["connection"] = "connected"; status["trusted"] = false
        try writeStatus(); model.reload()
        assert(model.presentation.kind == .attention && model.banner?.needsPermission == true && model.banner?.warning == true, "permission loss is visible")
        status["trusted"] = true; status["timestamp"] = Date().timeIntervalSince1970 - 600
        try writeStatus(); model.reload()
        assert(model.presentation.kind == .attention, "stale backend cannot look healthy")
        status["timestamp"] = Date().timeIntervalSince1970
        status["status_detail"] = ["exit_after_restore": "quit",
                                    "exit_recovery_pending": ["cursor": true]]
        try writeStatus(); model.reload()
        let beforeQuitCommands = commands.count
        model.apply()
        assert(model.busy && !model.canApply && commands.count == beforeQuitCommands,
               "shutdown recovery blocks draft submission")
        assert(model.presentation.kind == .attention && model.presentation.detail.contains("光标还原"),
               "incomplete cursor restoration remains visible")
        status["status_detail"] = [:]; try writeStatus(); model.reload()
        status["mode"] = "ARMED"; status["connection"] = "disconnected"; try writeStatus(); model.reload()
        // Display-size tiles: ascending width, zoom relative to the native half resolution.
        let tiles = model.scaleOptions
        assert(tiles.map(\.resolution) == ["1280x960", "1376x1032"], "tiles sorted by width")
        assert(abs(tiles[0].zoomPercent - 7.5) < 0.01 && abs(tiles[1].zoomPercent) < 0.01)
        assert(Format.size(1280, 960) == "1280 × 960" && Format.number(264) == "264" && Format.scale(2.5) == "2.5×")
        assert(model.draft.ppi == "264", "PPI shown without a trailing .0")
        assert(model.currentDevice?.id == "tablet")
        model.update { $0.rotate(to: "portrait") }
        assert(model.draft.resolution == "960x1280")
        assert(model.draft.validation == nil && model.dirty && model.canApply)
        assert(model.pendingChanges == ["竖屏", "960 × 1280"] && !model.requiresReconnect && model.showsChangeBar)
        model.addDevice()
        let newID = model.selection
        assert(model.draft.validation != nil && !model.canApply)
        model.selectCatalog("mini")
        assert(model.draft.cursor == 3.5 && model.draft.validation == nil)
        model.selection = "tablet"
        assert(model.draft.orientation == "portrait")
        model.reload()
        assert(model.draft.orientation == "portrait", "status refresh must preserve edits")
        model.apply()
        assert(commands.count == 1 && model.busy)
        let requestID = commands[0]["id"] as! String
        assert(commands[0]["profile_id"] as? String == "tablet")
        assert((commands[0]["draft"] as! [String: Any])["virtual"] == nil)
        model.apply()
        assert(commands.count == 1, "duplicate application must be blocked")
        model.core("recover")
        assert(commands.count == 2 && commands[1]["action"] as? String == "recover")
        status["last_result"] = ["id": "old-receipt", "status": "ok", "message": "Old result"]
        try writeStatus(); model.reload()
        assert(model.pendingID == requestID, "old receipts must not finish a new request")
        let recoveryID = commands[1]["id"] as! String
        status["results"] = [
            ["id": requestID, "status": "applying", "message": "Applying"],
            ["id": requestID, "status": "error", "message": "Fixture error"],
            ["id": recoveryID, "status": "ok", "message": "Recovered"]]
        status["last_result"] = ["id": recoveryID, "status": "ok", "message": "Recovered"]
        try writeStatus(); model.reload()
        assert(model.pendingID == nil && model.draft.orientation == "portrait" && model.dirty)
        assert(model.message == "Recovered", "recovery receipt is independently consumed")
        status["last_result"] = ["id": recoveryID, "status": "error", "message": "Stale recovery"]
        try writeStatus(); model.reload()
        assert(model.message == "Recovered", "consumed receipts must not change feedback")
        model.apply()
        let retryID = commands.last!["id"] as! String
        row["orientation"] = "portrait"; row["resolution"] = "960x1280"
        status["profiles"] = [row]; status["presets"] = ["tablet": ["1032x1376", "960x1280"]]
        status["results"] = [["id": retryID, "status": "ok", "message": "Saved for next connection"]]
        status["last_result"] = ["id": retryID, "status": "error", "message": "Superseded receipt"]
        try writeStatus(); model.reload()
        assert(!model.dirty && model.pendingID == nil && model.drafts[newID] != nil)
        assert(model.message == "Saved for next connection", "history terminal receipt takes priority")
        status["mode"] = "ACTIVE"; status["active_profile_id"] = "tablet"
        status["controllers"] = ["My iPad"]; status["binding_available"] = true
        let savedBinding: [String: Any] = ["key":"myipad", "name":"My iPad", "profile_id":"tablet"]
        status["bindings"] = [savedBinding]; status["current_binding"] = savedBinding
        try writeStatus(); model.reload()
        assert(model.selectedBindings.count == 1 && !model.canBindCurrent && !model.dirty)
        assert(!model.previewIsDraft && model.applyTitle == "应用显示设置")
        model.update { $0.rotate(to: "landscape") }
        assert(model.requiresReconnect, "live orientation change warns about the brief reconnect")
        model.discard()
        let reopened = WorkbenchModel(directory: directory) { _ in }
        assert(reopened.selectedBindings.first?.name == "My iPad", "saved binding survives window reopening")
        var second = row; second["id"] = "second"; second["label"] = "另一台设备"
        status["profiles"] = [row, second]; try writeStatus(); model.reload(); model.selection = "second"
        assert(model.applyTitle == "切换到此设备配置" && model.canBindCurrent)
        assert(model.draftStatus.contains("尚未切换"))
        status["controllers"] = ["My iPad", "Another iPad"]
        try writeStatus(); model.reload()
        assert(!model.canApply && model.draftStatus.contains("多台控制端"))
        status["controllers"] = ["My iPad"]; try writeStatus(); model.reload()
        let request = model.bindingRequest()!
        assert(request.profileID == "second" && request.expectedProfileID == "tablet")
        model.update { $0.cursor = 2.5 }
        model.submitBinding(request)
        let bindCommand = commands.last!; let bindID = bindCommand["id"] as! String
        assert(bindCommand["action"] as? String == "profile_bind" && bindCommand["draft"] == nil)
        status["results"] = [["id":bindID,"status":"error","message":"Binding changed"]]
        try writeStatus(); model.reload()
        assert(model.draft.cursor == 2.5 && model.dirty && model.selectedBindings.isEmpty)
        model.selection = "tablet"
        let unbind = model.unbindingRequest(model.selectedBindings[0])
        assert(unbind.name == "myipad" && unbind.profileID == "tablet")
        status["error"] = "Temporary backend error"; try writeStatus(); model.reload()
        assert(model.backendError == "Temporary backend error" && model.feedback == StatusCopy.localize("Temporary backend error"),
               "raw backend text is kept for diagnostics and localized for display")
        status.removeValue(forKey: "error"); status["mode"] = "ARMED"; try writeStatus(); model.reload()
        assert(!model.connected && model.feedback != "Temporary backend error")
        assert(model.applyTitle.contains("下次连接"))
        model.addDevice(); let cancelled = model.selection; model.discard()
        assert(model.drafts[cancelled] == nil && model.selection == "tablet")
        var custom = WorkbenchDraft()
        custom.width = "2420"; custom.height = "1668"; custom.ppi = "264"; custom.resolution = "1210x834"
        assert(custom.validation == nil)
        custom.resolution = "1280x960"
        assert(custom.validation != nil)
        custom.width = "64"; custom.height = "64"; custom.resolution = "15x15"
        assert(custom.validation != nil)
        custom.resolution = "16x16"; custom.cursor = 1.1
        assert(custom.validation != nil)
        custom.cursor = 1.5
        assert(custom.validation == nil)
        // A closed window stops observing; status writes then cost nothing.
        model.suspend()
        assert(!model.observing)
        status["mode"] = "PAUSED"; try writeStatus()
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        assert(model.mode == "ARMED", "suspended model ignores status writes")
        model.resume()
        assert(model.observing && model.mode == "PAUSED", "resume re-reads current status")
        assert(StatusCopy.localize("Remote disconnected") == "远程已断开，已恢复实体屏。")
        assert(StatusCopy.localize("已保存") == "已保存" && StatusCopy.localize("") == nil)
        print("Workbench model contract checks passed")
    }
}

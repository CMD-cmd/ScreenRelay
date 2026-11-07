import Foundation

@main
struct ExitHandoffTests {
    static func main() {
        var checks = 0
        func check(_ condition: Bool, _ message: String) { precondition(condition, message); checks += 1 }
        var completed: [String: Any] = ["id": "quit", "timestamp": 100.0, "controller_pid": 123, "resume_enabled": false]
        var state: [String: Any] = ["mode": "PAUSED", "desired_enabled": false, "journal": NSNull(),
                                    "last_command": "quit", "exit_completed": completed]
        var status = state
        status["timestamp"] = 101.0; status["controller_pid"] = 123; status["recovery_pending"] = false
        var cursor: [String: Any] = ["status": "restored", "pending_originals": [], "desired_profile": NSNull(),
                                     "original_profile": NSNull(), "error": NSNull()]
        func ready(_ allowResume: Bool = false) -> Bool {
            ExitHandoff.isComplete(state: state, status: status, cursor: cursor,
                                   commandID: "quit", requestedAt: 99, allowResume: allowResume, now: 101)
        }
        check(ready(), "fresh matching recovery is complete")
        let savedState = state, savedStatus = status, savedCursor = cursor
        for change: [String: Any] in [["mode": "ACTIVE"], ["journal": ["physical": [:]]],
                                     ["last_command": "old"], ["desired_enabled": true],
                                     ["exit_after_restore": "quit"]] {
            state = savedState.merging(change) { _, new in new }
            check(!ready(), "incomplete durable state must not authorize exit")
        }
        state = savedState
        for change: [String: Any] in [["timestamp": 0.0], ["timestamp": 102.0], ["timestamp": 99.0],
                                     ["controller_pid": 456], ["last_command": "old"], ["mode": "ACTIVE"],
                                     ["journal": ["physical": [:]]], ["desired_enabled": 0],
                                     ["recovery_pending": true]] {
            status = savedStatus.merging(change) { _, new in new }
            check(!ready(), "stale or split status must not authorize exit")
        }
        status = savedStatus
        for change: [String: Any] in [["status": "restoring"], ["pending_originals": [["name": "ArrowS"]]],
                                     ["desired_profile": ["scale": 3]], ["original_profile": ["scale": 1]],
                                     ["error": "restore failed"]] {
            cursor = savedCursor.merging(change) { _, new in new }
            check(!ready(), "cursor recovery must finish first")
        }
        cursor = savedCursor
        check(!ExitHandoff.isComplete(state: state, status: status, cursor: nil,
                                      commandID: "quit", requestedAt: 99, now: 101), "missing cursor evidence")
        completed["id"] = "old"; state["exit_completed"] = completed
        check(!ready(), "an older successful exit is not this request")
        state = savedState
        completed = savedState["exit_completed"] as! [String: Any]
        completed["resume_enabled"] = true
        state["exit_completed"] = completed; state["mode"] = "ARMED"; state["desired_enabled"] = true
        status = state; status["timestamp"] = 101.0; status["controller_pid"] = 123; status["recovery_pending"] = false
        check(!ready(), "user Quit cannot secretly preserve enabled")
        check(ready(true), "system exit can preserve enabled after recovery")
        check(!ExitHandoff.preserveEnabledIntent(requested: true, previousCommand: ["action": "pause"]),
              "unconsumed manual pause takes priority over a system exit")
        check(!ExitHandoff.preserveEnabledIntent(requested: true, previousCommand: ["action": "recover"]),
              "unconsumed manual recovery cannot be overwritten with preserve intent")
        check(ExitHandoff.preserveEnabledIntent(requested: true, previousCommand: ["action": "recover", "exit_after_restore": true]),
              "consecutive intentional system exits keep preserve semantics")
        check(ExitHandoff.preserveEnabledIntent(requested: true, previousCommand: ["action": "enable"]),
              "explicit enable can survive a later system exit")
        check(!ExitHandoff.preserveEnabledIntent(requested: false, previousCommand: ["action": "enable"]),
              "ordinary quit always remains off")
        print("Exit handoff: \(checks) checks passed")
    }
}

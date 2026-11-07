import Foundation
import Darwin

// Isolated listener and synthetic PID: the actual application rejects these
// messages because they do not come from its owned controller. No AX or files.
@main
struct StorageIPCTests {
    static func main() throws {
        guard CommandLine.arguments.count == 2 else { exit(2) }
        let executable = URL(fileURLWithPath: CommandLine.arguments[1])
        let center = DistributedNotificationCenter.default()
        let state = StorageHealthCenter()
        let ownPID = getpid()
        var received = 0
        let observer = center.addObserver(forName: StorageHealthCenter.distributedNotification,
            object: nil, queue: .main) { notification in
                guard let text = notification.object as? String, let data = text.data(using: .utf8),
                      let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                      state.accept(payload, expectedPID: ownPID) else { return }
                received += 1
            }
        defer { center.removeObserver(observer) }
        func send(_ phase: String) throws {
            let payload: [String: Any] = ["version": 1, "pid": Int(ownPID), "phase": phase,
                "free_bytes": 0, "retry_after": 60, "reason": "space", "recovery_pending": true,
                "mode": "RESTORING"]
            let data = try JSONSerialization.data(withJSONObject: payload)
            let process = Process(); process.executableURL = executable
            process.arguments = ["--report-storage-health", String(decoding: data, as: UTF8.self)]
            process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
            try process.run()
            let deadline = Date().addingTimeInterval(5)
            let expected = received + 1
            while received < expected, Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
            process.waitUntilExit()
            precondition(process.terminationStatus == 0 && received == expected, "Health notification must be delivered")
        }
        try send("pressure")
        precondition(state.active()?.message.contains("磁盘空间不足") == true)
        let warning = StatusBarPresentation.resolve(connection: "connected", trusted: true, sleeping: false,
            status: ["mode": "ACTIVE", "timestamp": 0], storage: state.active())
        precondition(warning.kind == .attention && warning.detail.contains("磁盘空间不足"))
        try send("healthy")
        precondition(state.active() == nil)
        print("Storage IPC: pressure and recovery delivered without state files")
    }
}

import Foundation

@main
struct OfficeCursorClientTests {
    static var assertions = 0
    static let now = 2_000_000_000.0
    static func expect(_ result: @autoclosure () -> Bool, _ description: String) {
        assertions += 1
        precondition(result(), description)
    }
    static func packet(_ changes: [String: Any] = [:]) -> String {
        var raw: [String: Any] = ["protocol": 2, "host": "com.microsoft.Excel", "pid": 100,
            "installed": true, "enabled": true, "conflict": false, "scale": 3,
            "module_version": 2, "library_path": "/Users/另一用户 🧪/Library/Application Support/Microsoft/UUDisplayAutomation/Excel/libUUNativeCursor.dylib",
            "timestamp": now, "request_id": "fresh-probe", "error": ""]
        raw.merge(changes) { _, new in new }
        return String(data: try! JSONSerialization.data(withJSONObject: raw), encoding: .utf8)!
    }
    static func parse(_ changes: [String: Any] = [:]) -> OfficeCursorClientStatus? {
        OfficeCursorClientStatus(object: packet(changes), now: now)
    }
    static func main() {
        let good = parse()!
        expect(good.host == .excel && good.scale == 3 && good.enabled, "valid current module response")
        expect(good.libraryURL.path.contains("另一用户 🧪"), "Unicode installation location")
        expect(parse(["host": "com.microsoft.Powerpoint"])?.host == .powerPoint, "PowerPoint host")
        for values: [String: Any] in [
            ["protocol": true], ["protocol": 2.9], ["protocol": 1], ["pid": true],
            ["pid": 0], ["pid": 1], ["pid": 1.5], ["pid": Double(Int32.max) + 1],
            ["installed": 1], ["enabled": "true"], ["conflict": 0], ["scale": true],
            ["scale": 9], ["scale": 2.75], ["module_version": true], ["module_version": 2.1],
            ["module_version": 0], ["module_version": 1001], ["host": "com.microsoft.Word"],
            ["timestamp": now - 16], ["timestamp": now + 6], ["timestamp": true],
            ["library_path": "relative/lib.dylib"], ["library_path": "/tmp/unsafe\0path"],
            ["library_path": "/" + String(repeating: "x", count: 8192)],
            ["request_id": String(repeating: "x", count: 129)],
            ["error": String(repeating: "x", count: 201)]
        ] { expect(parse(values) == nil, "malformed or stale packet refused: \(values.keys.first!)") }
        expect(OfficeCursorClientStatus(object: String(repeating: "x", count: 16_385), now: now) == nil, "bounded packet size")
        expect(OfficeCursorClientStatus(object: "[]", now: now) == nil, "object required")
        for scale in [1.0, 1.5, 2, 2.5, 3, 3.5, 4] {
            expect(parse(["scale": scale])?.scale == scale, "supported scale \(scale)")
        }
        let epoch = OfficeCursorProcessEpoch(pid: 100, launchedAt: now - 1)
        expect(good.belongs(to: epoch, pendingRequest: "fresh-probe", previous: nil), "fresh baseline")
        expect(!good.belongs(to: epoch, pendingRequest: "old-probe", previous: nil), "wrong nonce")
        expect(!good.belongs(to: epoch, pendingRequest: nil, previous: nil), "unsolicited baseline refused")
        expect(!good.belongs(to: .init(pid: 101, launchedAt: now - 1), pendingRequest: "fresh-probe", previous: nil), "old PID refused")
        expect(!good.belongs(to: .init(pid: 100, launchedAt: now + 0.1), pendingRequest: "fresh-probe", previous: nil), "same PID reused by new epoch refused")
        let delta = parse(["request_id": "", "enabled": false, "timestamp": now + 1])!
        expect(!delta.belongs(to: epoch, pendingRequest: "fresh-probe", previous: nil), "delta before baseline refused")
        expect(delta.belongs(to: epoch, pendingRequest: "fresh-probe", previous: good), "same epoch delta accepted")
        expect(!good.belongs(to: epoch, pendingRequest: "fresh-probe", previous: delta), "out-of-order status refused")
        expect(parse(["request_id": "old-probe"])!.belongs(to: epoch, pendingRequest: "fresh-probe", previous: good) == false, "old probe cannot replace baseline")
        let idle = parse(["enabled": false, "scale": 1])!
        expect(idle.installed && !idle.enabled, "original size remains a loaded module")
        func status(_ phase: OfficeCursorSetupPhase, running: Bool = true) -> OfficeCursorSetupStatus {
            .init(host: .excel, phase: phase, title: "", detail: "", isRunning: running,
                  hasFiles: false, canInstall: false)
        }
        for phase: OfficeCursorSetupPhase in [.notInstalled, .legacy, .outdated] {
            expect(status(phase).needsOfficeQuitBeforeInstall, "file preparation requires a stopped host")
            expect(!status(phase, running: false).needsOfficeQuitBeforeInstall, "stopped host needs no quit prompt")
        }
        for phase: OfficeCursorSetupPhase in [.prepared, .waitingForOffice, .active, .idle] {
            expect(!status(phase).needsOfficeQuitBeforeInstall, "enabling an already prepared add-in needs Office open")
        }
        expect(status(.active).hasLoadedClient && status(.idle).hasLoadedClient, "only current client evidence completes setup")
        expect(!status(.prepared).hasLoadedClient && !status(.waitingForOffice).hasLoadedClient, "files alone keep setup pending")
        print("Office client status: \(assertions) assertions passed")
    }
}

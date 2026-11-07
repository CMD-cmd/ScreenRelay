#!/usr/bin/env python3
"""Compile the actual supervisor class with isolated ownership/launch stubs."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
source = (ROOT / 'Sources/main.swift').read_text()
supervisor = source.split('final class ControllerSupervisor {', 1)[1].split('final class AppDelegate:', 1)[0]
supervisor = 'final class ControllerSupervisor {' + supervisor
harness = r'''
import Foundation
import Darwin
let supportURL = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
func atomicJSON<T: Encodable>(_ value: T, name: String) { preconditionFailure("Unexpected persistent write") }
'''
harness += supervisor
harness += r'''
@main struct SupervisorLifecycleTests {
    static func drain(_ done: () -> Bool) {
        let deadline = Date().addingTimeInterval(2)
        while !done(), Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.005)) }
        precondition(done(), "asynchronous ownership handoff did not finish")
    }
    static func main() {
        let adopted = ControllerSupervisor()
        let started = DispatchSemaphore(value: 0), gate = DispatchSemaphore(value: 0)
        var terminations = 0
        adopted.ownerReader = { started.signal(); gate.wait(); return 12345 }
        adopted.terminateAdopted = { _ in terminations += 1 }
        adopted.ensureRunning(); precondition(started.wait(timeout: .now() + 2) == .success)
        adopted.stop(); gate.signal()
        drain { !adopted.launching }
        precondition(adopted.stopped && adopted.adoptedPID == nil && terminations == 1,
                     "stop-before-adopt must terminate the verified late owner")

        let launching = ControllerSupervisor()
        let lookup = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        var launches = 0
        launching.ownerReader = { lookup.signal(); release.wait(); return nil }
        launching.processLauncher = { _ in launches += 1; throw NSError(domain: "Test", code: 1) }
        launching.ensureRunning(); precondition(lookup.wait(timeout: .now() + 2) == .success)
        launching.stop(); release.signal()
        drain { !launching.launching }
        precondition(launching.stopped && launches == 0 && launching.child == nil,
                     "stop-before-launch must not start a new controller")

        let recovery = ControllerSupervisor()
        let looking = DispatchSemaphore(value: 0), finish = DispatchSemaphore(value: 0)
        var interrupted = 0
        recovery.ownerReader = { looking.signal(); finish.wait(); return 12345 }
        recovery.terminateAdopted = { _ in interrupted += 1 }
        recovery.ensureRunning(); precondition(looking.wait(timeout: .now() + 2) == .success)
        recovery.stop(terminateController: false); finish.signal()
        drain { !recovery.launching }
        precondition(interrupted == 0, "recovery-only owner must survive UI exit")
        let completed = ControllerSupervisor()
        let now = Date().timeIntervalSince1970
        completed.requestedExitID = "quit"
        completed.requestedExitAt = now - 1
        let receipt: [String: Any] = ["id": "quit", "controller_pid": 12345, "timestamp": now, "resume_enabled": false]
        var finalState: [String: Any] = ["mode": "PAUSED", "desired_enabled": false, "journal": NSNull(),
                                       "last_command": "quit", "exit_completed": receipt]
        let finalStatus = finalState.merging(["controller_pid": 12345, "timestamp": now, "recovery_pending": false]) { _, new in new }
        completed.stateReader = { finalState }
        completed.statusReader = { finalStatus }
        completed.cursorReader = { ["status": "restored", "pending_originals": [], "desired_profile": NSNull()] }
        completed.ownerReader = { preconditionFailure("A completed quit must not relaunch or adopt") }
        completed.ensureRunning()
        precondition(!completed.launching && completed.child == nil,
                     "completed adopted exits must not respawn at a heartbeat boundary")
        finalState["exit_after_restore"] = "quit"
        finalState.removeValue(forKey: "exit_completed")
        precondition(!completed.expectedExitCompleted(),
                     "a completed status cannot suppress recovery when state is still pending")
        print("Supervisor lifecycle: five ownership, cancellation and split-exit checks passed")
    }
}
'''
with tempfile.TemporaryDirectory(prefix='uudisplay-supervisor-test-') as temporary:
    directory = Path(temporary)
    swift = directory / 'Harness.swift'
    swift.write_text(harness)
    binary = directory / 'supervisor-tests'
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library', str(swift),
                    str(ROOT / 'Sources/StorageHealth.swift'), str(ROOT / 'Sources/ExitHandoff.swift'),
                    '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)

import AppKit

/// Isolated bundle fixture: no controller, UU, display changes or TCC prompts.
/// The injected grant tests the real LaunchServices/process handoff protocol.
@main struct PermissionLaunchServicesFixture {
    final class Delegate: NSObject, NSApplicationDelegate {
        let directory: URL
        let ticket: PermissionTicket
        var started = false
        init(_ directory: URL, _ ticket: PermissionTicket) { self.directory = directory; self.ticket = ticket }
        func applicationDidFinishLaunching(_ notification: Notification) {
            guard !started else { return }; started = true
            let result: [String: Any] = ["takeover_pid": Int(getpid()), "owner_pid": ticket.ownerPID,
                "draft_preserved": ticket.workbenchState == Data("unsaved draft".utf8),
                "closed_workbench": !ticket.showWorkbench, "startup_count": 1]
            try! JSONSerialization.data(withJSONObject: result).write(to: directory.appendingPathComponent("takeover.json"), options: .atomic)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { exit(0) }
        }
    }
    static func main() throws {
        let args = CommandLine.arguments
        let directory = URL(fileURLWithPath: args.last!, isDirectory: true)
        let url = Bundle.main.bundleURL
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        if let index = args.firstIndex(of: "--fixture-probe") {
            let ticket = PermissionHandoff.ticket(directory: directory, nonce: args[index + 1])
            let owner = ticket.flatMap { NSRunningApplication(processIdentifier: pid_t($0.ownerPID)) }
            let diagnostic: [String: Any] = ["args": args, "url": url.path,
                "owner_url": owner?.bundleURL?.path ?? "nil", "owner_birth": ticket.flatMap { PermissionProcessIdentity.startedAt($0.ownerPID) } ?? 0,
                "ticket_birth": ticket?.ownerStartedAt ?? 0, "valid": ticket.map {
                    PermissionHandoff.valid($0, bundlePath: url.path, build: "fixture", now: Date().timeIntervalSince1970)
                } ?? false]
            try JSONSerialization.data(withJSONObject: diagnostic).write(to: directory.appendingPathComponent("probe-diagnostic.json"), options: .atomic)
            guard let ticket = PermissionProbeAgent.run(nonce: args[index + 1], directory: directory,
                applicationURL: url, build: "fixture", checkTrust: { true }) else { exit(2) }
            let delegate = Delegate(directory, ticket)
            app.delegate = delegate
            DispatchQueue.main.async { delegate.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification)) }
            app.run()
        } else {
            let ticket = PermissionTicket(nonce: UUID().uuidString, ownerPID: Int(getpid()),
                ownerStartedAt: PermissionProcessIdentity.startedAt(Int(getpid()))!, bundlePath: url.path,
                build: "fixture", createdAt: Date().timeIntervalSince1970, phase: .probing,
                probePID: nil, workbenchState: nil, showWorkbench: false)
            try PermissionHandoff.write(ticket, directory: directory)
            let config = NSWorkspace.OpenConfiguration()
            config.activates = false; config.createsNewApplicationInstance = true
            config.arguments = ["--fixture-probe", ticket.nonce, directory.path]
            DispatchQueue.main.async {
              NSWorkspace.shared.openApplication(at: url, configuration: config) { child, error in
                DispatchQueue.main.async {
                    guard error == nil, let child, child.processIdentifier != getpid(), child.bundleURL == url else { exit(3) }
                    let callback: [String: Any] = ["timestamp": Date().timeIntervalSince1970, "owner_pid": Int(getpid()),
                        "probe_pid": Int(child.processIdentifier), "birth": PermissionProcessIdentity.startedAt(Int(getpid())) ?? 0]
                    try! JSONSerialization.data(withJSONObject: callback).write(to: directory.appendingPathComponent("callback.json"), options: .atomic)
                    var polls = 0
                    _ = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { timer in
                        polls += 1
                        if let report = PermissionHandoff.report(directory: directory, nonce: ticket.nonce),
                           PermissionHandoff.accepts(report, for: ticket, expectedPID: Int(child.processIdentifier), now: Date().timeIntervalSince1970) {
                            timer.invalidate()
                            var ready = ticket; ready.phase = .ready; ready.probePID = Int(child.processIdentifier)
                            ready.workbenchState = Data("unsaved draft".utf8)
                            try! PermissionHandoff.write(ready, directory: directory)
                            let result: [String: Any] = ["owner_pid": Int(getpid()), "probe_pid": Int(child.processIdentifier),
                                "callback_before_owner_exit": true, "probe_report_matches_launchservices": report.trusted]
                            try! JSONSerialization.data(withJSONObject: result).write(to: directory.appendingPathComponent("owner.json"), options: .atomic)
                            exit(0)
                        }
                        if polls > 200 { exit(4) }
                    }
                }
              }
              DispatchQueue.main.asyncAfter(deadline: .now() + 15) { exit(5) }
            }
            app.run()
        }
    }
}

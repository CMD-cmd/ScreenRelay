import AppKit

/// Bounded, read-only acceptance probe for our own Office client protocol.
@main
struct OfficeCursorClientProbe {
    static func main() {
        let arguments = CommandLine.arguments
        guard arguments.count == 3, let host = OfficeCursorHost(rawValue: arguments[1]),
              let seconds = Double(arguments[2]), (1...180).contains(seconds) else { exit(2) }
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let nonce = UUID().uuidString
        var askedEpochs: Set<String> = []
        let center = DistributedNotificationCenter.default()
        func probe() { center.postNotificationName(OfficeCursorClientStatus.probe, object: nonce, userInfo: nil, deliverImmediately: true) }
        let token = center.addObserver(forName: OfficeCursorClientStatus.reply, object: nil, queue: .main) { note in
            MainActor.assumeIsolated {
            guard let client = OfficeCursorClientStatus(object: note.object, now: Date().timeIntervalSince1970),
                  client.host == host,
                  let process = NSWorkspace.shared.runningApplications.first(where: {
                      $0.bundleIdentifier == host.bundleID && $0.processIdentifier == client.pid && !$0.isTerminated
                  }), let launched = process.launchDate else { return }
            let epoch = OfficeCursorProcessEpoch(pid: client.pid, launchedAt: launched.timeIntervalSince1970)
            if client.belongs(to: epoch, pendingRequest: nonce, previous: nil) {
                print(note.object as! String); fflush(stdout); exit(0)
            }
            let key = "\(epoch.pid)-\(epoch.launchedAt)"
            if client.requestID.isEmpty, client.timestamp >= epoch.launchedAt, !askedEpochs.contains(key) {
                askedEpochs.insert(key); probe()
            }
            }
        }
        _ = token
        DispatchQueue.main.async { probe() }
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { exit(3) }
        app.run()
    }
}

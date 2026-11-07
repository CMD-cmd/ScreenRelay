import AppKit

@main struct AccessibilityPermissionTests {
    static func main() throws {
        var checks = 0
        func check(_ value: Bool, _ message: String) { precondition(value, message); checks += 1 }
        let fm = FileManager.default
        let directory = fm.temporaryDirectory.appendingPathComponent("permission-model-" + UUID().uuidString)
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: directory) }
        var now = Date().timeIntervalSince1970
        var trusted = false
        let url = URL(fileURLWithPath: "/Applications/PermissionFixture.app")
        let model = AccessibilityPermissionModel(directory: directory, applicationURL: url, build: "test",
            ownerPID: 120, ownerStartedAt: now - 100, trust: { trusted }, clock: { now }, schedulePolling: false)
        var observations: [(Bool, Bool)] = []
        var launches: [PermissionTicket] = []
        var completion: ((Int, URL?, Error?) -> Void)?
        var relaunches = 0, cancellations = 0, settings = 0
        model.onObservation = { observations.append(($0, $1)) }
        model.launchProbe = { launches.append($0); completion = $1 }
        model.onRelaunch = { _ in relaunches += 1; return true }
        model.onCancelRelaunch = { cancellations += 1 }
        model.requestSystemSettings = { settings += 1 }
        model.check()
        check(model.phase == .waiting && !model.polling && launches.isEmpty, "background checks do not start polling or probes")
        model.beginConfirmation()
        check(model.isPresenting && observations.last?.1 == true, "opening guide immediately rebuilds current authorization observation")
        trusted = true; model.poll()
        check(model.phase == .granted && observations.last?.0 == true && relaunches == 0, "live permission is recognized without restart")
        model.check()
        check(observations.count == 4, "each check publishes truth even if local grant did not change")
        model.endConfirmation(); trusted = false; model.beginConfirmation(); model.openSettings()
        check(settings == 1 && launches.isEmpty, "settings action does not prematurely launch a replacement")
        model.settingsActivated(); model.applicationActivated()
        check(launches.count == 1 && model.pending != nil, "return from settings starts one bounded fresh check")
        model.applicationActivated(); model.recheck()
        check(launches.count == 1, "repeated activation cannot duplicate a pending check")
        let ticket = model.pending!
        var report = PermissionProbeReport(nonce: ticket.nonce, pid: 240, trusted: true,
            bundlePath: url.path, build: "test", timestamp: now)
        try PermissionHandoff.write(report, directory: directory)
        completion?(241, url, nil)
        check(relaunches == 0 && model.phase != .refreshing, "report must match PID returned by LaunchServices")
        report.pid = 241; try PermissionHandoff.write(report, directory: directory); model.poll()
        check(relaunches == 1 && model.phase == .refreshing && model.pending?.probePID == 241, "fresh grant initiates exactly one safe takeover")
        model.poll(); model.poll()
        check(relaunches == 1, "same report cannot repeatedly initiate takeover")
        trusted = true; model.poll()
        check(model.phase == .granted && model.pending == nil && cancellations == 1,
              "resident grant arriving during handoff cancels replacement and keeps current process")
        check(PermissionHandoff.ticket(directory: directory, nonce: ticket.nonce) == nil, "successful resident check cleans only its handoff files")
        model.endConfirmation(); trusted = false; model.beginConfirmation(); model.recheck()
        let cancelledNonce = model.pending!.nonce
        model.endConfirmation()
        check(!model.isPresenting && model.pending == nil && cancellations == 2, "closing guide cancels pending takeover")
        completion?(240, url, nil)
        check(model.pending == nil && relaunches == 1, "late launch completion cannot revive cancelled request")
        check(PermissionHandoff.ticket(directory: directory, nonce: cancelledNonce) == nil, "cancelled nonce removed")
        model.beginConfirmation(); model.recheck()
        let denied = model.pending!
        try PermissionHandoff.write(PermissionProbeReport(nonce: denied.nonce, pid: 240, trusted: false,
            bundlePath: url.path, build: "test", timestamp: now), directory: directory)
        completion?(240, url, nil)
        check(model.phase == .issue && model.pending == nil && relaunches == 1, "ungranted fresh process cannot restart app")
        model.recheck(); let expired = model.pending!
        now += 13; model.poll()
        check(model.phase == .issue && model.pending == nil, "bounded probe timeout retains resident")
        check(PermissionHandoff.ticket(directory: directory, nonce: expired.nonce) == nil, "timeout invalidates token")
        model.recheck()
        check(model.pending == nil && model.phase == .issue, "at most two fresh checks per guide session")
        model.endConfirmation(); model.beginConfirmation(); now += 181; model.poll()
        check(model.phase == .issue && model.pending == nil && !model.polling, "guide timeout stops confirmation work")
        model.resumed(ticket); model.recheck()
        check(model.pending == nil && model.phase == .issue, "takeover cannot cause a self-restarting loop")
        model.endConfirmation()
        let unavailableIdentity = AccessibilityPermissionModel(directory: directory, applicationURL: url, build: "test",
            ownerStartedAt: 0, trust: { false }, schedulePolling: false)
        unavailableIdentity.launchProbe = { _, _ in preconditionFailure("unknown identity must not launch") }
        unavailableIdentity.recheck()
        check(unavailableIdentity.phase == .issue && unavailableIdentity.pending == nil, "unverified owner birth fails safely")
        let currentBirth = PermissionProcessIdentity.startedAt(Int(getpid()))
        check(currentBirth != nil && currentBirth! <= Date().timeIntervalSince1970, "kernel process identity available")
        check(PermissionProcessIdentity.startedAt(0) == nil && PermissionProcessIdentity.startedAt(Int.max) == nil,
              "invalid PID never queried or narrowed unsafely")
        let timed = AccessibilityPermissionModel(directory: directory, trust: { false })
        timed.beginConfirmation(); check(timed.polling, "timer exists only for active confirmation")
        timed.endConfirmation(); check(!timed.polling, "closing guide invalidates timer")
        timed.check(); check(!timed.polling, "background heartbeat cannot restart permission polling")
        print("Accessibility permission: \(checks) checks passed")
    }
}

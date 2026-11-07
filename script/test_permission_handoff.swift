import Foundation
import Darwin

@main
struct PermissionHandoffTests {
    static func main() throws {
        var checks = 0
        func check(_ condition: Bool, _ message: String) {
            precondition(condition, message)
            checks += 1
        }
        let fm = FileManager.default
        let directory = fm.temporaryDirectory.appendingPathComponent("permission-handoff-tests-\(UUID().uuidString)")
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: directory) }
        let original = PermissionTicket(nonce: UUID().uuidString, ownerPID: 123,
            ownerStartedAt: 800, bundlePath: "/Applications/UUDisplayAutomation.app", build: "123",
            createdAt: 995, phase: .probing, probePID: nil,
            workbenchState: Data([1, 2, 3]), showWorkbench: true)
        let originalReport = PermissionProbeReport(nonce: original.nonce, pid: 456, trusted: true,
            bundlePath: original.bundlePath, build: original.build, timestamp: 999)
        func valid(_ value: PermissionTicket, at time: Double = 1_000) -> Bool {
            PermissionHandoff.valid(value, bundlePath: original.bundlePath, build: original.build, now: time)
        }
        func accepts(_ value: PermissionProbeReport, ticket: PermissionTicket? = nil,
                     pid: Int = 456, at time: Double = 1_000) -> Bool {
            PermissionHandoff.accepts(value, for: ticket ?? original, expectedPID: pid, now: time)
        }
        check(valid(original), "normal ticket")
        check(accepts(originalReport), "normal report")
        var ticket = original
        for invalid in ["", "not-a-uuid", "../../unrelated", original.nonce + "/../x", original.nonce + "\0"] {
            ticket = original; ticket.nonce = invalid
            check(!valid(ticket), "reject invalid ticket nonce")
            check((try? PermissionHandoff.ticketURL(directory: directory, nonce: invalid)) == nil, "reject unsafe ticket filename")
            check((try? PermissionHandoff.reportURL(directory: directory, nonce: invalid)) == nil, "reject unsafe report filename")
            check(PermissionHandoff.ticket(directory: directory, nonce: invalid) == nil, "reject unsafe reads")
            do { try PermissionHandoff.write(ticket, directory: directory); check(false, "unsafe write succeeded") }
            catch { check(true, "unsafe write rejected") }
            PermissionHandoff.remove(directory: directory, nonce: invalid)
        }
        for pid in [-1, 0, 1] {
            ticket = original; ticket.ownerPID = pid
            check(!valid(ticket), "reject invalid owner PID")
            check(!accepts(originalReport, pid: pid), "reject invalid expected PID")
        }
        for time in [-1.0, 0, .nan, .infinity, -.infinity] {
            ticket = original; ticket.ownerStartedAt = time
            check(!valid(ticket), "reject invalid owner launch time")
        }
        for time in [1_001.0, 879, .nan, .infinity, -.infinity] {
            ticket = original; ticket.createdAt = time
            check(!valid(ticket), "reject future expired nonfinite ticket")
        }
        ticket = original; ticket.createdAt = 880
        check(valid(ticket), "ticket lifetime inclusive boundary")
        check(!valid(original, at: .nan), "reject nonfinite current time")
        check(!valid(original, at: .infinity), "reject infinite current time")
        ticket = original; ticket.build = "other"
        check(!valid(ticket), "reject wrong build")
        ticket.build = ""
        check(!valid(ticket), "reject empty build")
        ticket = original; ticket.bundlePath = "/Applications/Other.app"
        check(!valid(ticket), "reject wrong path")
        ticket.bundlePath = "Applications/UUDisplayAutomation.app"
        check(!valid(ticket), "reject relative path")
        ticket.bundlePath = "/Applications/UUDisplayAutomation.app/../UUDisplayAutomation.app"
        check(valid(ticket), "standardize equivalent path")
        ticket = original; ticket.workbenchState = Data(repeating: 0, count: 512 * 1024)
        check(valid(ticket), "workbench size inclusive boundary")
        ticket.workbenchState!.append(0)
        check(!valid(ticket), "reject oversized workbench")
        for phase in [PermissionTicket.Phase.ready, .cancelled] {
            ticket = original; ticket.phase = phase
            check(!accepts(originalReport, ticket: ticket), "only probing phase accepts reports")
        }
        ticket = original; ticket.createdAt = 800
        check(!accepts(originalReport, ticket: ticket), "reject expired owning ticket")
        ticket = original; ticket.ownerPID = 456
        check(!accepts(originalReport, ticket: ticket), "reject owner as probe")
        check(!accepts(originalReport, pid: 789), "reject previous probe PID")
        var report = originalReport
        report.pid = 123
        check(!accepts(report), "reject old owner report")
        report = originalReport; report.nonce = UUID().uuidString
        check(!accepts(report), "reject unrelated report nonce")
        report = originalReport; report.build = "other"
        check(!accepts(report), "reject report build mismatch")
        report = originalReport; report.bundlePath = "/Applications/Other.app"
        check(!accepts(report), "reject report path mismatch")
        for time in [1_001.0, 994, .nan, .infinity, -.infinity] {
            report = originalReport; report.timestamp = time
            check(!accepts(report), "reject invalid report timestamp")
        }
        ticket = original; ticket.createdAt = 980
        report = originalReport; report.timestamp = 989
        check(!accepts(report, ticket: ticket), "reject report older than ten seconds")
        report.timestamp = 990
        check(accepts(report, ticket: ticket), "report freshness inclusive boundary")
        report = originalReport; report.trusted = false
        check(accepts(report), "authentic negative result is still a valid report")

        try PermissionHandoff.write(original, directory: directory)
        try PermissionHandoff.write(originalReport, directory: directory)
        let ticketURL = try PermissionHandoff.ticketURL(directory: directory, nonce: original.nonce)
        let reportURL = try PermissionHandoff.reportURL(directory: directory, nonce: original.nonce)
        check(ticketURL.lastPathComponent == "permission-refresh-\(original.nonce).json", "ticket filename")
        check(reportURL.lastPathComponent == "permission-refresh-\(original.nonce).result.json", "report filename")
        check(PermissionHandoff.ticket(directory: directory, nonce: original.nonce) == original, "ticket serialization round trip")
        check(PermissionHandoff.report(directory: directory, nonce: original.nonce) == originalReport, "report serialization round trip")
        for url in [ticketURL, reportURL] {
            let attributes = try fm.attributesOfItem(atPath: url.path)
            check((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600, "private handoff permissions")
        }
        ticket = original; ticket.phase = .ready; ticket.probePID = 456; ticket.showWorkbench = false; ticket.workbenchState = nil
        try PermissionHandoff.write(ticket, directory: directory)
        check(PermissionHandoff.ticket(directory: directory, nonce: original.nonce) == ticket, "atomic overwrite and optional round trip")
        for phase in [PermissionTicket.Phase.probing, .ready, .cancelled] {
            ticket.phase = phase
            check(try JSONDecoder().decode(PermissionTicket.self, from: JSONEncoder().encode(ticket)) == ticket, "phase round trip")
        }
        try Data("broken json".utf8).write(to: ticketURL)
        check(PermissionHandoff.ticket(directory: directory, nonce: original.nonce) == nil, "reject corrupt JSON")
        ticket = original; ticket.nonce = UUID().uuidString
        try JSONEncoder().encode(ticket).write(to: ticketURL)
        check(PermissionHandoff.ticket(directory: directory, nonce: original.nonce) == nil, "reject stored nonce mismatch")
        try Data(repeating: 32, count: 2 * 1024 * 1024 + 1).write(to: ticketURL)
        check(PermissionHandoff.ticket(directory: directory, nonce: original.nonce) == nil, "reject oversized file")
        ticket = original; ticket.workbenchState = Data(repeating: 0, count: 2 * 1024 * 1024)
        do { try PermissionHandoff.write(ticket, directory: directory); check(false, "oversized encoded file written") }
        catch { check(true, "oversized encoded file rejected") }
        let unrelated = directory.appendingPathComponent("unrelated.json")
        let payload = try JSONEncoder().encode(original)
        try payload.write(to: unrelated)
        try fm.removeItem(at: ticketURL)
        try fm.createSymbolicLink(at: ticketURL, withDestinationURL: unrelated)
        check(PermissionHandoff.ticket(directory: directory, nonce: original.nonce) == nil, "reject symlink ticket")
        try fm.removeItem(at: reportURL)
        try fm.createSymbolicLink(at: reportURL, withDestinationURL: unrelated)
        check(PermissionHandoff.report(directory: directory, nonce: original.nonce) == nil, "reject symlink report")
        PermissionHandoff.remove(directory: directory, nonce: original.nonce)
        check(try Data(contentsOf: unrelated) == payload, "cleanup preserves symlink target")
        check(!fm.fileExists(atPath: ticketURL.path) && !fm.fileExists(atPath: reportURL.path), "cleanup removes only named handoff files")
        try fm.createDirectory(at: ticketURL, withIntermediateDirectories: false)
        check(PermissionHandoff.ticket(directory: directory, nonce: original.nonce) == nil, "reject directory read")
        PermissionHandoff.remove(directory: directory, nonce: original.nonce)
        check(fm.fileExists(atPath: ticketURL.path), "cleanup never recursively removes directories")
        try fm.removeItem(at: ticketURL)
        check(mkfifo(ticketURL.path, 0o600) == 0, "create FIFO fixture")
        check(PermissionHandoff.ticket(directory: directory, nonce: original.nonce) == nil, "reject FIFO without blocking")
        PermissionHandoff.remove(directory: directory, nonce: original.nonce)
        check(try fm.contentsOfDirectory(atPath: directory.path) == ["unrelated.json"], "no leftover temporary files or unrelated deletion")
        PermissionHandoff.remove(directory: directory, nonce: "../unrelated")
        check(fm.fileExists(atPath: unrelated.path), "invalid nonce cleanup has no effect")
        print("Permission handoff: \(checks) checks passed")
    }
}

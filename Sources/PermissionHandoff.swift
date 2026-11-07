import Foundation
import Darwin

struct PermissionTicket: Codable, Equatable {
    enum Phase: String, Codable { case probing, ready, cancelled }
    var nonce: String
    var ownerPID: Int
    var ownerStartedAt: Double
    var bundlePath: String
    var build: String
    var createdAt: Double
    var phase: Phase
    var probePID: Int?
    var workbenchState: Data?
    var showWorkbench: Bool
}

struct PermissionProbeReport: Codable, Equatable {
    var nonce: String
    var pid: Int
    var trusted: Bool
    var bundlePath: String
    var build: String
    var timestamp: Double
}

/// Short-lived process handoff evidence. A valid report does not establish the
/// resident process's Accessibility authorization; the resident must check it.
enum PermissionHandoff {
    private static let maximumFileSize = 2 * 1024 * 1024
    private static let maximumWorkbenchSize = 512 * 1024

    static func ticketURL(directory: URL, nonce: String) throws -> URL {
        directory.appendingPathComponent(try filename(nonce: nonce, report: false))
    }

    static func reportURL(directory: URL, nonce: String) throws -> URL {
        directory.appendingPathComponent(try filename(nonce: nonce, report: true))
    }

    static func write(_ ticket: PermissionTicket, directory: URL) throws {
        try write(JSONEncoder().encode(ticket), directory: directory,
                  filename: filename(nonce: ticket.nonce, report: false))
    }

    static func write(_ report: PermissionProbeReport, directory: URL) throws {
        try write(JSONEncoder().encode(report), directory: directory,
                  filename: filename(nonce: report.nonce, report: true))
    }

    static func ticket(directory: URL, nonce: String) -> PermissionTicket? {
        guard let name = try? filename(nonce: nonce, report: false),
              let data = read(directory: directory, filename: name),
              let value = try? JSONDecoder().decode(PermissionTicket.self, from: data),
              value.nonce == nonce else { return nil }
        return value
    }

    static func report(directory: URL, nonce: String) -> PermissionProbeReport? {
        guard let name = try? filename(nonce: nonce, report: true),
              let data = read(directory: directory, filename: name),
              let value = try? JSONDecoder().decode(PermissionProbeReport.self, from: data),
              value.nonce == nonce else { return nil }
        return value
    }

    static func remove(directory: URL, nonce: String) {
        guard let ticket = try? filename(nonce: nonce, report: false),
              let report = try? filename(nonce: nonce, report: true) else { return }
        let descriptor = open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard descriptor >= 0 else { return }
        defer { close(descriptor) }
        // unlinkat does not follow final-component symlinks or remove directories.
        _ = unlinkat(descriptor, ticket, 0)
        _ = unlinkat(descriptor, report, 0)
        _ = fsync(descriptor)
    }

    static func valid(_ ticket: PermissionTicket, bundlePath: String, build: String, now: Double) -> Bool {
        guard parsedNonce(ticket.nonce) != nil,
              ticket.ownerPID > 1, ticket.ownerStartedAt.isFinite, ticket.ownerStartedAt > 0,
              now.isFinite, ticket.createdAt.isFinite, ticket.createdAt <= now,
              now - ticket.createdAt <= 120,
              let actualPath = normalizedPath(ticket.bundlePath),
              let expectedPath = normalizedPath(bundlePath), actualPath == expectedPath,
              !ticket.build.isEmpty, !build.isEmpty, ticket.build == build,
              (ticket.workbenchState?.count ?? 0) <= maximumWorkbenchSize else { return false }
        return true
    }

    static func accepts(_ report: PermissionProbeReport, for ticket: PermissionTicket,
                        expectedPID: Int, now: Double) -> Bool {
        guard ticket.phase == .probing,
              valid(ticket, bundlePath: ticket.bundlePath, build: ticket.build, now: now),
              expectedPID > 1, expectedPID != ticket.ownerPID, report.pid == expectedPID,
              report.nonce == ticket.nonce,
              let reportPath = normalizedPath(report.bundlePath),
              let ticketPath = normalizedPath(ticket.bundlePath), reportPath == ticketPath,
              report.build == ticket.build,
              report.timestamp.isFinite, report.timestamp <= now,
              report.timestamp >= ticket.createdAt, now - report.timestamp <= 10 else { return false }
        return true
    }

    private static func normalizedPath(_ path: String) -> String? {
        guard path.hasPrefix("/"), !path.utf8.contains(0) else { return nil }
        return URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath().path
    }

    private static func filename(nonce: String, report: Bool) throws -> String {
        guard let uuid = parsedNonce(nonce) else { throw failure(EINVAL) }
        return "permission-refresh-\(uuid.uuidString)\(report ? ".result" : "").json"
    }

    private static func parsedNonce(_ nonce: String) -> UUID? {
        // Foundation's parser can accept an embedded NUL suffix. Require the
        // complete supplied string to be the UUID, allowing either letter case.
        guard nonce.utf8.count == 36, let uuid = UUID(uuidString: nonce),
              uuid.uuidString.lowercased() == nonce.lowercased() else { return nil }
        return uuid
    }

    private static func failure(_ code: Int32 = errno) -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(code))
    }

    private static func write(_ data: Data, directory: URL, filename: String) throws {
        guard data.count <= maximumFileSize else { throw failure(EFBIG) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let parent = open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard parent >= 0 else { throw failure() }
        defer { close(parent) }
        let temporary = ".\(filename).\(UUID().uuidString).tmp"
        let descriptor = openat(parent, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { throw failure() }
        defer { close(descriptor); _ = unlinkat(parent, temporary, 0) }
        // Enforce exact permissions even under an unusually restrictive umask.
        guard fchmod(descriptor, 0o600) == 0 else { throw failure() }
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count < 0 { if errno == EINTR { continue }; throw failure() }
                guard count > 0 else { throw failure(EIO) }
                offset += count
            }
        }
        guard fsync(descriptor) == 0 else { throw failure() }
        guard renameat(parent, temporary, parent, filename) == 0 else { throw failure() }
        // Persist the rename as well as the file contents.
        guard fsync(parent) == 0 else { throw failure() }
    }

    private static func read(directory: URL, filename: String) -> Data? {
        let parent = open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard parent >= 0 else { return nil }
        defer { close(parent) }
        let descriptor = openat(parent, filename, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }
        var info = stat()
        guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_size >= 0, info.st_size <= maximumFileSize else { return nil }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 16 * 1024)
        while true {
            let count = buffer.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress!, $0.count) }
            if count < 0 { if errno == EINTR { continue }; return nil }
            if count == 0 { return data }
            guard data.count <= maximumFileSize - count else { return nil }
            data.append(contentsOf: buffer.prefix(count))
        }
    }
}

import Foundation
import CryptoKit
import Darwin
import CoreFoundation

struct OfficeCursorPayloadManifest {
    let version: String
    let moduleVersion: Int
    let librarySHA256: String
    let raw: [String: Any]
}

struct OfficeCursorInstallRecord {
    let host: OfficeCursorHost
    let addinURL: URL
    let libraryURL: URL
    let version: String?
    let moduleVersion: Int?
    let legacy: Bool
    let filesValid: Bool
    let addinSHA256: String
    let librarySHA256: String
}

enum OfficeCursorInstallError: LocalizedError {
    case invalid(String)
    var errorDescription: String? { if case .invalid(let message) = self { return message }; return nil }
}

/// Copies the signed app's bundled payload only. Loading and runtime verification belong to Office.
final class OfficeCursorInstaller {
    private let directory: URL
    private let officeDirectory: URL
    private let payloadDirectory: URL
    private let fm = FileManager.default
    /// Test seam. Throwing simulates an interrupted transaction; recovery is explicit or on next prepare.
    var faultInjector: ((String) throws -> Void)?
    private var markerURL: URL { directory.appendingPathComponent("office-cursor-addon.json") }
    private var journalURL: URL { directory.appendingPathComponent("office-cursor-install.json") }
    private let libraryName = "libUUNativeCursor.dylib"

    init(directory: URL, officeDirectory: URL, payloadDirectory: URL) {
        self.directory = directory.standardizedFileURL
        self.officeDirectory = officeDirectory.standardizedFileURL
        self.payloadDirectory = payloadDirectory.standardizedFileURL
    }

    private func fail(_ message: String) -> OfficeCursorInstallError { .invalid(message) }
    private func ioFailure(_ message: String) -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(errno), userInfo: [NSLocalizedDescriptionKey: message])
    }
    private func exists(_ url: URL) -> Bool { (try? fm.attributesOfItem(atPath: url.path)) != nil }
    private func check(_ url: URL, directory: Bool = false, owned: Bool = true) throws {
        let a = try fm.attributesOfItem(atPath: url.path)
        guard a[.type] as? FileAttributeType == (directory ? .typeDirectory : .typeRegular),
              !owned || (a[.ownerAccountID] as? NSNumber)?.uint32Value == getuid() else {
            throw fail("Unsafe file or directory: \(url.lastPathComponent)")
        }
    }
    private func makeDirectory(_ url: URL) throws {
        if exists(url) { try check(url, directory: true); return }
        let parent = url.deletingLastPathComponent()
        if !exists(parent) { try makeDirectory(parent) }
        else { try check(parent, directory: true, owned: false) }
        try fm.createDirectory(at: url, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        try check(url, directory: true)
        try syncDirectory(parent)
    }
    private func read(_ url: URL, limit: Int = 128 * 1024, owned: Bool = true) throws -> Data {
        try check(url, owned: owned)
        let size = (try fm.attributesOfItem(atPath: url.path)[.size] as? NSNumber)?.intValue ?? Int.max
        guard size <= limit else { throw fail("File exceeds size limit: \(url.lastPathComponent)") }
        let data = try Data(contentsOf: url, options: [.mappedIfSafe])
        guard data.count <= limit else { throw fail("File exceeds size limit") }
        return data
    }
    private func json(_ url: URL, owned: Bool = true) throws -> [String: Any] {
        guard let value = try JSONSerialization.jsonObject(with: read(url, owned: owned)) as? [String: Any] else { throw fail("Invalid JSON object") }
        return value
    }
    private func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    private func digest(_ url: URL, owned: Bool = true) throws -> String { hash(try read(url, limit: 64 * 1024 * 1024, owned: owned)) }
    private func validHash(_ value: Any?) -> String? {
        guard let s = value as? String, s.count == 64, s.allSatisfy({ $0.isASCII && ($0.isNumber || ("a"..."f").contains(String($0))) }) else { return nil }
        return s
    }
    // JSON booleans bridge to NSNumber/Int too; decimal/exponent tokens are not integer fields.
    private func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              !["f", "d"].contains(String(cString: number.objCType)) else { return nil }
        return Int(number.stringValue)
    }
    private func syncDirectory(_ url: URL) throws {
        try check(url, directory: true, owned: false)
        let fd = open(url.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard fd >= 0 else { throw ioFailure("Cannot open installation directory for synchronization") }
        defer { close(fd) }
        guard fsync(fd) == 0 else { throw ioFailure("Cannot synchronize installation directory") }
    }
    /// A private, exclusive temp file is flushed before rename; directory metadata is flushed after.
    /// The journal uses this same path, and is durable before any destination is replaced.
    private func durableWrite(_ data: Data, to url: URL, step: String? = nil) throws {
        if exists(url) { try check(url) }
        let parent = url.deletingLastPathComponent()
        try check(parent, directory: true)
        let temp = parent.appendingPathComponent(".office-cursor-\(UUID().uuidString).tmp")
        let fd = open(temp.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw ioFailure("Cannot stage Office installation file") }
        defer { close(fd); try? fm.removeItem(at: temp) }
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw ioFailure("Cannot write Office installation file") }
                offset += count
            }
        }
        guard fsync(fd) == 0 else { throw ioFailure("Cannot flush Office installation file") }
        // F_FULLFSYNC requests physical media flushing on macOS; fsync above remains required.
        if fcntl(fd, F_FULLFSYNC) != 0 && errno != EINVAL && errno != ENOTSUP {
            throw ioFailure("Cannot synchronize Office installation file")
        }
        try faultInjector?("staged:\(step ?? url.lastPathComponent)")
        if exists(url) { try check(url) }
        guard rename(temp.path, url.path) == 0 else { throw ioFailure("Cannot atomically replace Office installation file") }
        try syncDirectory(parent)
    }
    private func durableRemove(_ url: URL) throws {
        try check(url)
        try fm.removeItem(at: url)
        try syncDirectory(url.deletingLastPathComponent())
    }
    private func writeJSON(_ value: [String: Any], to url: URL) throws {
        try durableWrite(JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]), to: url)
    }
    private func clearRuntimeProofs(_ entry: inout [String: Any]) {
        for key in Array(entry.keys) where ["loaded", "active", "loaded_at", "verified_at", "verified"].contains(key)
            || key.hasPrefix("verified_") || key.hasSuffix("_verified") {
            entry.removeValue(forKey: key)
        }
    }
    private func locked<T>(_ body: () throws -> T) throws -> T {
        try makeDirectory(directory)
        let url = directory.appendingPathComponent("office-cursor-install.lock")
        let fd = open(url.path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw fail("Cannot acquire installer lock") }
        defer { close(fd) }
        try check(url)
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { throw fail("Another Office cursor installation is in progress") }
        defer { flock(fd, LOCK_UN) }
        return try body()
    }

    func payloadManifest() throws -> OfficeCursorPayloadManifest {
        try check(payloadDirectory, directory: true, owned: false)
        let raw = try json(payloadDirectory.appendingPathComponent("manifest.json"), owned: false)
        guard integer(raw["schema"]) == 1, let version = raw["version"] as? String,
              !version.isEmpty, version.utf8.count <= 80,
              let module = integer(raw["module_version"]), module > 0,
              raw["library"] as? String == libraryName,
              let libraryHash = validHash(raw["library_sha256"]),
              let hosts = raw["hosts"] as? [String: [String: Any]] else { throw fail("Invalid Office payload manifest") }
        guard try digest(payloadDirectory.appendingPathComponent(libraryName), owned: false) == libraryHash else { throw fail("Office library checksum mismatch") }
        for host in OfficeCursorHost.allCases {
            guard let item = hosts[host.bundleID], item["file"] as? String == host.addinName,
                  item["resource"] as? String == "\(host.directoryName)/\(host.addinName)",
                  let expected = validHash(item["sha256"]) else { throw fail("Invalid Office host payload") }
            let folder = payloadDirectory.appendingPathComponent(host.directoryName)
            try check(folder, directory: true, owned: false)
            guard try digest(folder.appendingPathComponent(host.addinName), owned: false) == expected else { throw fail("Office add-in checksum mismatch") }
        }
        return .init(version: version, moduleVersion: module, librarySHA256: libraryHash, raw: raw)
    }

    private func marker() throws -> [String: Any] { exists(markerURL) ? try json(markerURL) : [:] }
    private func record(_ host: OfficeCursorHost, marker: [String: Any]) throws -> OfficeCursorInstallRecord? {
        let hosts = marker["hosts"] as? [String: [String: Any]] ?? [:]
        let entry = hosts[host.bundleID] ?? (host == .excel ? marker : [:])
        guard entry["installed"] as? Bool == true,
              let addin = entry["add_in_path"] as? String,
              let library = entry["library_path"] as? String,
              let addinHash = validHash(entry["add_in_sha256"]),
              let libraryHash = validHash(entry["library_sha256"]) else { return nil }
        let hostDir = officeDirectory.appendingPathComponent(host.directoryName)
        let excelLegacy = host == .excel && addin == officeDirectory.appendingPathComponent(host.addinName).path && library == officeDirectory.appendingPathComponent(libraryName).path
        let oldLibraryName = URL(fileURLWithPath: library).lastPathComponent
        let powerpointLegacy = host == .powerPoint && addin == hostDir.appendingPathComponent(host.addinName).path && library == hostDir.appendingPathComponent(oldLibraryName).path && oldLibraryName.range(of: "^libUUPowerPointCursor-[a-f0-9]{12}\\.dylib$", options: .regularExpression) != nil
        let legacyLocation = excelLegacy || powerpointLegacy
        let legacy = legacyLocation && entry["payload_version"] == nil
        // Paths from marker data never authorize arbitrary locations.
        guard legacyLocation || (addin == hostDir.appendingPathComponent(host.addinName).path && library == hostDir.appendingPathComponent(libraryName).path) else { return nil }
        let a = URL(fileURLWithPath: addin), l = URL(fileURLWithPath: library)
        var valid = false
        do {
            try check(officeDirectory, directory: true)
            if !excelLegacy { try check(hostDir, directory: true) }
            valid = try digest(a) == addinHash && digest(l) == libraryHash
        } catch { valid = false }
        return .init(host: host, addinURL: a, libraryURL: l, version: entry["payload_version"] as? String,
                     moduleVersion: integer(entry["module_version"]), legacy: legacy, filesValid: valid,
                     addinSHA256: addinHash, librarySHA256: libraryHash)
    }
    func installed(_ host: OfficeCursorHost) throws -> OfficeCursorInstallRecord? { try record(host, marker: marker()) }

    func prepare(_ host: OfficeCursorHost, isOfficeRunning: () -> Bool) throws -> OfficeCursorInstallRecord {
        try locked {
            if let pending = try pendingHost() {
                guard pending == host else { throw fail("Another Office host has a pending installation; quit both Office apps before recovery") }
                guard !isOfficeRunning() else { throw fail("Quit Office before recovering its cursor installation") }
            }
            try recoverUnlocked()
            let manifest = try payloadManifest() // Validate every resource before touching destinations.
            var mark = try marker()
            let existing = try record(host, marker: mark)
            let payloadHosts = manifest.raw["hosts"] as! [String: [String: Any]]
            if let existing, !existing.legacy, existing.filesValid, existing.version == manifest.version,
               existing.moduleVersion == manifest.moduleVersion,
               try digest(existing.libraryURL) == manifest.librarySHA256,
               try digest(existing.addinURL) == payloadHosts[host.bundleID]?["sha256"] as? String { return existing }
            guard !isOfficeRunning() else { throw fail("Quit \(host.name) before preparing its cursor add-in") }
            if let existing, !existing.filesValid { throw fail("Recorded Office add-in files have changed; refusing replacement") }
            if host == .excel && exists(officeDirectory.appendingPathComponent(host.addinName)) {
                guard existing?.filesValid == true, existing?.addinURL == officeDirectory.appendingPathComponent(host.addinName) else { throw fail("An unowned legacy Excel add-in occupies the registered location") }
            }
            try makeDirectory(officeDirectory)
            let reusesRoot = host == .excel && existing?.filesValid == true && existing?.addinURL == officeDirectory.appendingPathComponent(host.addinName)
            let hostDir = reusesRoot ? officeDirectory : officeDirectory.appendingPathComponent(host.directoryName)
            try makeDirectory(hostDir)
            let targets = [hostDir.appendingPathComponent(host.addinName), hostDir.appendingPathComponent(libraryName)]
            let items = manifest.raw["hosts"] as! [String: [String: Any]]
            let hashes = [items[host.bundleID]!["sha256"] as! String, manifest.librarySHA256]
            let sources = [payloadDirectory.appendingPathComponent("\(host.directoryName)/\(host.addinName)"), payloadDirectory.appendingPathComponent(libraryName)]
            for target in targets where exists(target) {
                guard let existing, existing.filesValid,
                      target == existing.addinURL || target == existing.libraryURL else { throw fail("An unowned Office add-in file occupies the destination") }
                try check(target)
            }
            let transaction = UUID().uuidString
            let backup = directory.appendingPathComponent("backups/office-cursor/\(transaction)")
            try makeDirectory(backup)
            var changes: [[String: Any]] = []
            for (index, target) in targets.enumerated() {
                var change: [String: Any] = ["name": target.lastPathComponent, "new_hash": hashes[index], "existed": exists(target)]
                if exists(target) {
                    change["old_hash"] = try digest(target)
                    try durableWrite(read(target, limit: 64 * 1024 * 1024), to: backup.appendingPathComponent(target.lastPathComponent), step: "backup:\(target.lastPathComponent)")
                }
                changes.append(change)
            }
            var journal: [String: Any] = ["schema": 1, "host": host.rawValue, "location": reusesRoot ? "root" : "host", "transaction": transaction, "changes": changes]
            if exists(markerURL) {
                try durableWrite(read(markerURL), to: backup.appendingPathComponent("marker.json"), step: "backup:marker.json")
                journal["marker_old_hash"] = try digest(markerURL)
            }
            var hosts = mark["hosts"] as? [String: [String: Any]] ?? [:]
            var entry = hosts[host.bundleID] ?? [:]
            entry.merge(["installed": true, "phase": "prepared", "payload_version": manifest.version,
                         "module_version": manifest.moduleVersion, "add_in_path": targets[0].path,
                         "library_path": targets[1].path, "add_in_sha256": hashes[0], "library_sha256": hashes[1],
                         "installed_at": ISO8601DateFormatter().string(from: Date())]) { _, new in new }
            // A previous runtime acknowledgement does not attest newly copied bytes.
            clearRuntimeProofs(&entry)
            clearRuntimeProofs(&mark)
            hosts[host.bundleID] = entry
            mark["hosts"] = hosts; mark["installed"] = true
            let markerBytes = try JSONSerialization.data(withJSONObject: mark, options: [.prettyPrinted, .sortedKeys])
            journal["marker_new_hash"] = hash(markerBytes)
            try writeJSON(journal, to: journalURL)
            try faultInjector?("journal")
            do {
                for index in targets.indices {
                    guard !isOfficeRunning() else { throw fail("Office started during installation") }
                    try check(officeDirectory, directory: true)
                    try check(hostDir, directory: true)
                    // Recheck ownership immediately before replacement.
                    if exists(targets[index]) {
                        guard let old = changes[index]["old_hash"] as? String, try digest(targets[index]) == old else { throw fail("Office add-in changed during installation") }
                    } else if changes[index]["existed"] as? Bool == true { throw fail("Office add-in disappeared during installation") }
                    try durableWrite(read(sources[index], limit: 64 * 1024 * 1024, owned: false), to: targets[index])
                    guard try digest(targets[index]) == hashes[index] else { throw fail("Installed payload checksum mismatch") }
                    try faultInjector?("file\(index)")
                }
                guard !isOfficeRunning() else { throw fail("Office started before installation completed") }
                if let old = journal["marker_old_hash"] as? String {
                    guard exists(markerURL), try digest(markerURL) == old else { throw fail("Office marker changed during installation") }
                } else if exists(markerURL) { throw fail("Office marker appeared during installation") }
                try durableWrite(markerBytes, to: markerURL)
                try faultInjector?("marker")
                try recoverUnlocked()
                return try record(host, marker: mark)!
            } catch {
                // Preserve journal and backups if a third party changed a target.
                if !isOfficeRunning() { try? recoverUnlocked() }
                throw error
            }
        }
    }

    func pendingHost() throws -> OfficeCursorHost? {
        guard exists(journalURL) else { return nil }
        let journal = try json(journalURL)
        guard integer(journal["schema"]) == 1, let name = journal["host"] as? String,
              let host = OfficeCursorHost(rawValue: name) else { throw fail("Invalid Office installation recovery journal") }
        return host
    }

    /// Caller must confirm both Office hosts have stopped before explicit recovery.
    func recoverPending() throws { try locked { try recoverUnlocked() } }
    private func recoverUnlocked() throws {
        guard exists(journalURL) else { return }
        let journal = try json(journalURL)
        guard integer(journal["schema"]) == 1, let hostName = journal["host"] as? String,
              let host = OfficeCursorHost(rawValue: hostName), let transaction = journal["transaction"] as? String,
              UUID(uuidString: transaction) != nil, let changes = journal["changes"] as? [[String: Any]], changes.count == 2,
              let markerNew = validHash(journal["marker_new_hash"]) else { throw fail("Invalid Office installation recovery journal") }
        guard let location = journal["location"] as? String, location == "host" || (location == "root" && host == .excel) else { throw fail("Invalid recovery location") }
        let hostDir = location == "root" ? officeDirectory : officeDirectory.appendingPathComponent(host.directoryName)
        try check(officeDirectory, directory: true); try check(hostDir, directory: true)
        let backup = directory.appendingPathComponent("backups/office-cursor/\(transaction)")
        try check(directory.appendingPathComponent("backups"), directory: true)
        try check(directory.appendingPathComponent("backups/office-cursor"), directory: true)
        try check(backup, directory: true)
        let names = [host.addinName, libraryName]
        for index in changes.indices {
            guard changes[index]["name"] as? String == names[index], validHash(changes[index]["new_hash"]) != nil,
                  changes[index]["existed"] is Bool else { throw fail("Invalid recovery file record") }
            if changes[index]["existed"] as? Bool == true {
                guard let old = validHash(changes[index]["old_hash"]), try digest(backup.appendingPathComponent(names[index])) == old else { throw fail("Recovery backup checksum mismatch") }
            }
        }
        let committed = try exists(markerURL) && digest(markerURL) == markerNew
        if committed {
            for index in changes.indices {
                guard try digest(hostDir.appendingPathComponent(names[index])) == changes[index]["new_hash"] as? String else { throw fail("Committed Office payload changed; preserving recovery evidence") }
            }
        } else {
            // Check every target and marker before restoring any of them.
            if let old = validHash(journal["marker_old_hash"]) {
                guard exists(markerURL), try digest(markerURL) == old,
                      try digest(backup.appendingPathComponent("marker.json")) == old else { throw fail("Office marker changed; preserving recovery evidence") }
            } else if exists(markerURL) { throw fail("Unrecognized Office marker; preserving recovery evidence") }
            for index in changes.indices {
                let target = hostDir.appendingPathComponent(names[index])
                if exists(target) {
                    let current = try digest(target)
                    guard current == changes[index]["new_hash"] as? String || current == changes[index]["old_hash"] as? String else { throw fail("Office file changed; preserving recovery evidence") }
                } else if changes[index]["existed"] as? Bool == true { throw fail("Owned Office file missing; preserving recovery evidence") }
            }
            for index in changes.indices {
                let target = hostDir.appendingPathComponent(names[index])
                if changes[index]["existed"] as? Bool == true {
                    try durableWrite(read(backup.appendingPathComponent(names[index]), limit: 64 * 1024 * 1024), to: target)
                } else if exists(target) { try durableRemove(target) }
            }
        }
        try durableRemove(journalURL)
    }
}

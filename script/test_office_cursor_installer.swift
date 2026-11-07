import Foundation
import CryptoKit

@main
struct OfficeCursorInstallerTests {
    static let fm = FileManager.default
    static var assertions = 0
    static func expect(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        assertions += 1
        if try !condition() { throw NSError(domain: message, code: 1) }
    }
    static func refuses(_ message: String, _ body: () throws -> Void) throws {
        var failed = false
        do { try body() } catch { failed = true }
        try expect(failed, message)
    }
    static func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    static func writeJSON(_ object: [String: Any], _ url: URL) throws {
        try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]).write(to: url)
    }
    static func json(_ url: URL) throws -> [String: Any] { try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any] }
    final class Fixture {
        let root: URL, state: URL, office: URL, payload: URL
        var installer: OfficeCursorInstaller { OfficeCursorInstaller(directory: state, officeDirectory: office, payloadDirectory: payload) }
        init() throws {
            root = fm.temporaryDirectory.appendingPathComponent("office-installer-\(UUID().uuidString)/另一用户 🧪/Library/Application Support")
            state = root.appendingPathComponent("Screen Relay")
            office = root.appendingPathComponent("Microsoft/UUDisplayAutomation")
            payload = root.appendingPathComponent("Bundled Payload")
            try fm.createDirectory(at: state, withIntermediateDirectories: true)
            try fm.createDirectory(at: payload, withIntermediateDirectories: true)
            try build("2.0.0")
        }
        deinit { try? fm.removeItem(at: root.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()) }
        func build(_ version: String) throws {
            let lib = Data("library \(version)".utf8)
            try lib.write(to: payload.appendingPathComponent("libUUNativeCursor.dylib"))
            var hosts: [String: Any] = [:]
            for host in OfficeCursorHost.allCases {
                let folder = payload.appendingPathComponent(host.directoryName)
                try fm.createDirectory(at: folder, withIntermediateDirectories: true)
                let data = Data("addin \(host.rawValue) \(version)".utf8)
                try data.write(to: folder.appendingPathComponent(host.addinName))
                hosts[host.bundleID] = ["file": host.addinName, "resource": "\(host.directoryName)/\(host.addinName)", "sha256": hash(data)]
            }
            try writeJSON(["schema": 1, "version": version, "module_version": 2, "library": "libUUNativeCursor.dylib", "library_sha256": hash(lib), "hosts": hosts], payload.appendingPathComponent("manifest.json"))
        }
        var marker: URL { state.appendingPathComponent("office-cursor-addon.json") }
        func legacy(_ host: OfficeCursorHost) throws -> (URL, URL) {
            let folder = host == .excel ? office : office.appendingPathComponent(host.directoryName)
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
            let a = folder.appendingPathComponent(host.addinName)
            let l = folder.appendingPathComponent(host == .excel ? "libUUNativeCursor.dylib" : "libUUPowerPointCursor-012345abcdef.dylib")
            let ad = Data("legacy-addin".utf8), ld = Data("legacy-library".utf8)
            try ad.write(to: a); try ld.write(to: l)
            let entry: [String: Any] = ["installed": true, "add_in_path": a.path, "library_path": l.path, "add_in_sha256": hash(ad), "library_sha256": hash(ld)]
            var marker = host == .excel ? entry : [:]
            marker["hosts"] = [host.bundleID: entry]
            try writeJSON(marker, self.marker)
            return (a, l)
        }
    }
    static func main() throws {
        do {
            let f = try Fixture(), i = f.installer
            try expect(try i.payloadManifest().moduleVersion == 2, "manifest module")
            let a = try i.prepare(.excel, isOfficeRunning: { false })
            try expect(a.filesValid && !a.legacy && a.version == "2.0.0", "first install")
            try expect(a.addinURL.path.contains("另一用户 🧪"), "portable Unicode user path")
            let before = try Data(contentsOf: f.marker)
            _ = try i.prepare(.excel, isOfficeRunning: { true })
            try expect(try Data(contentsOf: f.marker) == before, "idempotence even running")
            var marker = try json(f.marker)
            marker["custom"] = ["retained": true]
            var hosts = marker["hosts"] as! [String: Any]
            hosts["other.app"] = ["sentinel": 42]
            marker["hosts"] = hosts
            try writeJSON(marker, f.marker)
            let p = try i.prepare(.powerPoint, isOfficeRunning: { false })
            try expect(p.filesValid, "PowerPoint install")
            let result = try json(f.marker)
            try expect((result["custom"] as? [String: Bool])?["retained"] == true, "unknown marker keys")
            try expect(((result["hosts"] as? [String: [String: Any]])?["other.app"]?["sentinel"] as? Int) == 42, "other host preserved")
            try f.build("2.1.0")
            try refuses("running update refusal") { _ = try i.prepare(.excel, isOfficeRunning: { true }) }
            try expect(try i.installed(.excel)?.version == "2.0.0", "running refusal preserves old")
            _ = try i.prepare(.excel, isOfficeRunning: { false })
            try expect(try i.installed(.excel)?.version == "2.1.0", "owned update")
            try expect(fm.fileExists(atPath: f.state.appendingPathComponent("backups/office-cursor").path), "backups retained")
        }
        for host in OfficeCursorHost.allCases {
            let f = try Fixture(), i = f.installer
            let old = try f.legacy(host)
            try expect(try i.installed(host)?.legacy == true, "legacy adopted")
            _ = try i.prepare(host, isOfficeRunning: { false })
            try expect(try i.installed(host)?.filesValid == true, "legacy upgraded")
            try expect(fm.fileExists(atPath: old.1.path), "legacy library retained")
            if host == .excel {
                try expect(try i.installed(host)?.addinURL == old.0, "registered legacy addin path retained")
                try expect(try i.installed(host)?.libraryURL == old.1, "registered legacy library path retained")
                try expect(try i.installed(host)?.legacy == false, "legacy upgraded state")
            }
        }
        do {
            let f = try Fixture(), i = f.installer
            let folder = f.office.appendingPathComponent("Excel")
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
            let target = folder.appendingPathComponent(OfficeCursorHost.excel.addinName)
            try Data("user".utf8).write(to: target)
            try refuses("unknown target refusal") { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
            try expect(try Data(contentsOf: target) == Data("user".utf8), "unknown target preserved")
            try fm.removeItem(at: target)
            let external = f.root.appendingPathComponent("external")
            try Data("external".utf8).write(to: external)
            try fm.createSymbolicLink(at: target, withDestinationURL: external)
            try refuses("symlink refusal") { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
            try expect(try Data(contentsOf: external) == Data("external".utf8), "symlink target untouched")
        }
        do {
            let f = try Fixture(), i = f.installer
            let resource = f.payload.appendingPathComponent("PowerPoint/UUDisplayPowerPointCursor.ppam")
            try Data("corrupt".utf8).write(to: resource)
            try refuses("all resources validated") { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
            try expect(!fm.fileExists(atPath: f.office.path), "no destination writes for corrupt payload")
            try fm.removeItem(at: resource)
            try refuses("missing payload") { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
        }
        do {
            let f = try Fixture(), i = f.installer
            i.faultInjector = { step in if step == "journal" { throw NSError(domain: "simulated crash", code: 1) } }
            try refuses("crash simulation") { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
            try expect(fm.fileExists(atPath: f.state.appendingPathComponent("office-cursor-install.json").path), "pending journal")
            try expect(try i.pendingHost() == .excel, "pending host exposed")
            try refuses("cross-host pending recovery refusal") { _ = try i.prepare(.powerPoint, isOfficeRunning: { false }) }
            try refuses("running pending recovery refusal") { _ = try i.prepare(.excel, isOfficeRunning: { true }) }
            try f.installer.recoverPending()
            try expect(!fm.fileExists(atPath: f.marker.path), "recover uncommitted first install")
            i.faultInjector = nil
            let old = try i.prepare(.excel, isOfficeRunning: { false })
            let before = try Data(contentsOf: old.addinURL), marker = try Data(contentsOf: f.marker)
            try f.build("2.2.0")
            i.faultInjector = { step in if step == "file0" { throw NSError(domain: "write failure", code: 1) } }
            try refuses("partial failure") { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
            try expect(try Data(contentsOf: old.addinURL) == before, "partial write restored")
            try expect(try Data(contentsOf: f.marker) == marker, "partial failure marker unchanged")
            i.faultInjector = nil
            _ = try i.prepare(.excel, isOfficeRunning: { false })
            try expect(try i.installed(.excel)?.version == "2.2.0", "retry succeeds")
        }
        do {
            let f = try Fixture(), i = f.installer
            let old = try i.prepare(.excel, isOfficeRunning: { false })
            let before = try Data(contentsOf: old.addinURL)
            try f.build("2.3.0")
            var running = false
            i.faultInjector = { step in if step == "file0" { running = true } }
            try refuses("host launching mid-install") { _ = try i.prepare(.excel, isOfficeRunning: { running }) }
            try expect(try i.pendingHost() == .excel, "running host leaves pending recovery")
            running = false
            try i.recoverPending()
            try expect(try Data(contentsOf: old.addinURL) == before, "closed-host recovery restores originals")
        }
        do {
            let f = try Fixture(), i = f.installer
            let old = try i.prepare(.excel, isOfficeRunning: { false })
            try f.build("2.4.0")
            i.faultInjector = { step in
                if step == "file0" {
                    try Data("concurrent user change".utf8).write(to: old.addinURL)
                    throw NSError(domain: "concurrent mutation", code: 1)
                }
            }
            try refuses("concurrent replacement") { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
            try refuses("recovery refuses changed target") { try i.recoverPending() }
            try expect(try Data(contentsOf: old.addinURL) == Data("concurrent user change".utf8), "recovery preserves third-party changes")
            try expect(try i.pendingHost() == .excel, "ambiguous recovery journal retained")
        }
        for field in ["schema", "module_version"] {
            for token in ["true", "false", "1.0", "1.5", "1e0"] {
                let f = try Fixture()
                let path = f.payload.appendingPathComponent("manifest.json")
                var text = try String(contentsOf: path, encoding: .utf8)
                let pattern = "\"" + field + "\":" + "[0-9]+"
                text = text.replacingOccurrences(of: pattern, with: "\"" + field + "\":" + token, options: .regularExpression)
                try Data(text.utf8).write(to: path)
                try refuses("strict integer " + field + " " + token) { _ = try f.installer.payloadManifest() }
            }
        }
        do {
            let f = try Fixture(), i = f.installer
            let paths = [f.payload.appendingPathComponent("manifest.json"), f.payload.appendingPathComponent("libUUNativeCursor.dylib")]
                + OfficeCursorHost.allCases.map { f.payload.appendingPathComponent("\($0.directoryName)/\($0.addinName)") }
            for path in paths { try fm.setAttributes([.posixPermissions: 0o444], ofItemAtPath: path.path) }
            for host in OfficeCursorHost.allCases { try fm.setAttributes([.posixPermissions: 0o555], ofItemAtPath: f.payload.appendingPathComponent(host.directoryName).path) }
            try fm.setAttributes([.posixPermissions: 0o555], ofItemAtPath: f.payload.path)
            defer {
                try? fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: f.payload.path)
                for host in OfficeCursorHost.allCases { try? fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: f.payload.appendingPathComponent(host.directoryName).path) }
            }
            try expect(try i.prepare(.excel, isOfficeRunning: { false }).filesValid, "read-only app-bundled payload")
            try expect(try i.prepare(.powerPoint, isOfficeRunning: { false }).filesValid, "read-only payload second host")
        }
        do {
            let f = try Fixture(), i = f.installer
            _ = try i.prepare(.excel, isOfficeRunning: { false })
            var marker = try json(f.marker)
            var hosts = marker["hosts"] as! [String: [String: Any]]
            let proofKeys = ["loaded", "verified", "verified_in_addin_manager", "restart_autoload_verified", "profile_enable_disable_verified", "verified_at"]
            for key in proofKeys { hosts[OfficeCursorHost.excel.bundleID]![key] = true; marker[key] = true }
            hosts[OfficeCursorHost.excel.bundleID]!["custom_note"] = "keep"
            hosts[OfficeCursorHost.powerPoint.bundleID] = ["restart_autoload_verified": true]
            marker["hosts"] = hosts
            try writeJSON(marker, f.marker)
            try f.build("3.0.0")
            _ = try i.prepare(.excel, isOfficeRunning: { false })
            let result = try json(f.marker)
            let resultHosts = result["hosts"] as! [String: [String: Any]]
            for key in proofKeys {
                try expect(result[key] == nil, "clear top runtime proof " + key)
                try expect(resultHosts[OfficeCursorHost.excel.bundleID]![key] == nil, "clear host runtime proof " + key)
            }
            try expect(resultHosts[OfficeCursorHost.excel.bundleID]!["custom_note"] as? String == "keep", "retain unknown host metadata")
            try expect(resultHosts[OfficeCursorHost.powerPoint.bundleID]!["restart_autoload_verified"] as? Bool == true, "other host proof preserved")
        }
        for failureStep in ["staged:backup:UUDisplayNativeCursor.xlam", "staged:backup:marker.json", "staged:office-cursor-install.json", "staged:UUDisplayNativeCursor.xlam", "staged:libUUNativeCursor.dylib", "staged:office-cursor-addon.json", "marker"] {
            let f = try Fixture(), i = f.installer
            let old = try i.prepare(.excel, isOfficeRunning: { false })
            let oldAddin = try Data(contentsOf: old.addinURL), oldLibrary = try Data(contentsOf: old.libraryURL)
            let oldMarker = try Data(contentsOf: f.marker)
            try f.build("3.1.0")
            var reached = false
            i.faultInjector = { step in
                if step == failureStep && !reached {
                    reached = true
                    throw NSError(domain: NSPOSIXErrorDomain, code: Int(ENOSPC))
                }
            }
            try refuses("staging failure " + failureStep) { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
            try expect(reached, "injection reached " + failureStep)
            i.faultInjector = nil
            try i.recoverPending()
            if failureStep == "marker" {
                try expect(try i.installed(.excel)?.version == "3.1.0", "committed marker journal cleaned without reverting")
            } else {
                try expect(try Data(contentsOf: old.addinURL) == oldAddin, "durable addin rollback " + failureStep)
                try expect(try Data(contentsOf: old.libraryURL) == oldLibrary, "durable library rollback " + failureStep)
                try expect(try Data(contentsOf: f.marker) == oldMarker, "durable marker rollback " + failureStep)
            }
            try expect(try i.pendingHost() == nil, "durable journal cleaned " + failureStep)
        }
        do {
            let f = try Fixture(), i = f.installer
            let old = try i.prepare(.excel, isOfficeRunning: { false })
            let oldAddin = try Data(contentsOf: old.addinURL), oldLibrary = try Data(contentsOf: old.libraryURL)
            let oldMarker = try Data(contentsOf: f.marker)
            try f.build("3.2.0")
            // Recreate an abrupt precommit exit after both replacements; bypass ordinary catch recovery.
            i.faultInjector = { step in
                if step == "journal" {
                    try Data(contentsOf: f.payload.appendingPathComponent("Excel/UUDisplayNativeCursor.xlam")).write(to: old.addinURL)
                    try Data(contentsOf: f.payload.appendingPathComponent("libUUNativeCursor.dylib")).write(to: old.libraryURL)
                    throw NSError(domain: "abrupt exit fixture", code: 1)
                }
            }
            try refuses("abrupt precommit exit") { _ = try i.prepare(.excel, isOfficeRunning: { false }) }
            i.faultInjector = { step in
                if step == "staged:libUUNativeCursor.dylib" { throw NSError(domain: NSPOSIXErrorDomain, code: Int(ENOSPC)) }
            }
            try refuses("disk full during rollback") { try i.recoverPending() }
            try expect(try i.pendingHost() == .excel, "failed rollback retains journal")
            try expect(try Data(contentsOf: f.marker) == oldMarker, "failed rollback retains original marker")
            i.faultInjector = nil
            try i.recoverPending()
            try expect(try Data(contentsOf: old.addinURL) == oldAddin, "retry rollback restores addin")
            try expect(try Data(contentsOf: old.libraryURL) == oldLibrary, "retry rollback restores library")
            try expect(try i.pendingHost() == nil, "retry rollback completes")
        }
        print("OfficeCursorInstaller: \(assertions) assertions passed")
    }
}

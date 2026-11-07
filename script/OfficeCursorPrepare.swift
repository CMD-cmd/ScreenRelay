import AppKit

/// Runs the same installer as the workbench, for bounded integration acceptance.
@main
struct OfficeCursorPrepare {
    static func main() throws {
        let args = CommandLine.arguments
        guard args.count == 5, let host = OfficeCursorHost(rawValue: args[4]) else { exit(2) }
        let installer = OfficeCursorInstaller(directory: URL(fileURLWithPath: args[1]),
            officeDirectory: URL(fileURLWithPath: args[2]), payloadDirectory: URL(fileURLWithPath: args[3]))
        let record = try installer.prepare(host) {
            NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == host.bundleID && !$0.isTerminated }
        }
        let packet: [String: Any] = ["host": host.bundleID, "addin": record.addinURL.path,
            "library": record.libraryURL.path, "version": record.version ?? "", "files_valid": record.filesValid]
        print(String(data: try JSONSerialization.data(withJSONObject: packet, options: [.sortedKeys]), encoding: .utf8)!)
    }
}

import AppKit

/// Read-only integration acceptance of the workbench's actual live model.
@main @MainActor
struct OfficeCursorSetupProbe {
    static func main() {
        guard CommandLine.arguments.count == 2 else { exit(2) }
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let model = OfficeCursorSetupModel.live(directory: URL(fileURLWithPath: CommandLine.arguments[1]))
        model.begin()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            let rows = model.statuses.map { row -> [String: Any] in
                ["host": row.host.bundleID, "phase": row.phase.rawValue, "title": row.title,
                 "running": row.isRunning, "has_files": row.hasFiles, "can_install": row.canInstall,
                 "loaded": row.hasLoadedClient, "detail": row.detail]
            }
            let data = try! JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
            print(String(data: data, encoding: .utf8)!)
            let loaded = model.statuses.allSatisfy(\.hasLoadedClient)
            model.end()
            exit(loaded ? 0 : 3)
        }
        app.run()
    }
}

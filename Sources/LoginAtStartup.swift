import Foundation
import Darwin

/// Controls the existing login service without unloading it or stopping a session.
struct LoginAtStartup {
    static let didChange = Notification.Name("local.codex.UUDisplayAutomation.StartupChanged")
    static let label = "local.codex.uu-display"
    static let menuTag = 7200
    let agentURL: URL
    let executableURL: URL
    let supportURL: URL
    let userID: uid_t
    var run: ([String]) throws -> String = LoginAtStartup.launchctl

    init(home: URL = FileManager.default.homeDirectoryForCurrentUser,
         userID: uid_t = getuid(), run: @escaping ([String]) throws -> String = LoginAtStartup.launchctl) {
        agentURL = home.appendingPathComponent("Library/LaunchAgents/\(Self.label).plist")
        executableURL = home.appendingPathComponent("Applications/UUDisplayAutomation.app/Contents/MacOS/UUDisplayAutomation")
        supportURL = home.appendingPathComponent("Library/Application Support/UUDisplayAutomation")
        self.userID = userID; self.run = run
    }

    enum StartupError: LocalizedError {
        case invalidAgent, invalidResponse, commandFailed, verificationFailed
        var errorDescription: String? {
            switch self {
            case .invalidAgent: return "现有启动项与本应用不匹配，未修改。"
            case .invalidResponse: return "暂时无法读取系统的自启设置，请稍后重试。"
            case .commandFailed: return "系统未能保存自启设置，请稍后重试。"
            case .verificationFailed: return "自启设置尚未生效，请稍后重试。"
            }
        }
    }

    static func launchctl(_ arguments: [String]) throws -> String {
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        let output = Pipe(); process.standardOutput = output; process.standardError = FileHandle.nullDevice
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile(); process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw StartupError.commandFailed }
        return String(decoding: data, as: UTF8.self)
    }

    static func disabledOverride(in output: String) throws -> Bool? {
        let text = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.hasPrefix("disabled services = {"), text.hasSuffix("}") else { throw StartupError.invalidResponse }
        let key = NSRegularExpression.escapedPattern(for: label)
        let expression = try NSRegularExpression(pattern: "^\\s*\"\(key)\"\\s*=>\\s*(disabled|enabled|true|false)\\s*$")
        let lines = text.components(separatedBy: .newlines).filter { $0.contains("\"\(label)\"") }
        guard lines.count <= 1 else { throw StartupError.invalidResponse }
        guard let line = lines.first else { return nil }
        let range = NSRange(line.startIndex..., in: line)
        guard let match = expression.firstMatch(in: line, range: range),
              let valueRange = Range(match.range(at: 1), in: line) else { throw StartupError.invalidResponse }
        return ["disabled", "true"].contains(String(line[valueRange]))
    }

    private func existingAgent() throws -> [String: Any]? {
        guard FileManager.default.fileExists(atPath: agentURL.path) else { return nil }
        guard let object = try PropertyListSerialization.propertyList(from: Data(contentsOf: agentURL), format: nil) as? [String: Any],
              object["Label"] as? String == Self.label,
              object["ProgramArguments"] as? [String] == [executableURL.path],
              object["RunAtLoad"] as? Bool == true else { throw StartupError.invalidAgent }
        return object
    }

    func isEnabled() throws -> Bool {
        guard try existingAgent() != nil else { return false }
        let disabled = try Self.disabledOverride(in: run(["print-disabled", "gui/\(userID)"]))
        return disabled != true
    }

    func setEnabled(_ enabled: Bool) throws {
        var createdData: Data?
        if try existingAgent() == nil {
            guard enabled else { return }
            let agent: [String: Any] = [
                "Label": Self.label, "ProgramArguments": [executableURL.path],
                "AssociatedBundleIdentifiers": ["local.codex.UUDisplayAutomation"],
                "LimitLoadToSessionType": "Aqua", "ProcessType": "Interactive",
                "KeepAlive": ["SuccessfulExit": false], "RunAtLoad": true, "ThrottleInterval": 10,
                "StandardOutPath": supportURL.appendingPathComponent("supervisor.stdout.log").path,
                "StandardErrorPath": supportURL.appendingPathComponent("supervisor.stderr.log").path]
            let data = try PropertyListSerialization.data(fromPropertyList: agent, format: .xml, options: 0)
            try FileManager.default.createDirectory(at: agentURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: agentURL, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: agentURL.path)
            createdData = data
        }
        // These persistent overrides do not boot out or restart the running app.
        do {
            _ = try run([enabled ? "enable" : "disable", "gui/\(userID)/\(Self.label)"])
            guard try isEnabled() == enabled else { throw StartupError.verificationFailed }
            NotificationCenter.default.post(name: Self.didChange, object: nil)
        } catch {
            if let createdData, (try? Data(contentsOf: agentURL)) == createdData {
                try? FileManager.default.removeItem(at: agentURL)
            }
            throw error
        }
    }
}

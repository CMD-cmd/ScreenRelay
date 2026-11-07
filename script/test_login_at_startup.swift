import Foundation

@main
struct LoginAtStartupTests {
    static func main() throws {
        if CommandLine.arguments.contains("--live-probe") {
            print("enabled=\(try LoginAtStartup().isEnabled())"); return
        }
        if let action = CommandLine.arguments.last, ["--live-enable", "--live-disable"].contains(action) {
            try LoginAtStartup().setEnabled(action == "--live-enable")
            print("enabled=\(try LoginAtStartup().isEnabled())"); return
        }
        var checks = 0
        func check(_ condition: Bool, _ message: String) {
            precondition(condition, message); checks += 1
        }
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        var disabled: Bool?; var commands: [[String]] = []
        var failWrite = false; var failRead = false
        let startup = LoginAtStartup(home: home, userID: 501) { args in
            commands.append(args)
            if args.first == "print-disabled" {
                if failRead { throw LoginAtStartup.StartupError.commandFailed }
                return "disabled services = {\n\(disabled.map { "\"local.codex.uu-display\" => \($0 ? "disabled" : "enabled")" } ?? "")\n}"
            }
            if failWrite { throw LoginAtStartup.StartupError.commandFailed }
            check(args.count == 2 && args[1] == "gui/501/local.codex.uu-display", "only owned service can change")
            check(["enable", "disable"].contains(args[0]), "never unload, stop or bootstrap")
            disabled = args[0] == "disable"; return ""
        }
        check(try startup.isEnabled() == false, "missing agent means off")
        check(commands.isEmpty, "missing agent is read without system commands")
        try startup.setEnabled(true); check(try startup.isEnabled(), "enable creates one exact service")
        let original = try Data(contentsOf: startup.agentURL)
        let plist = try PropertyListSerialization.propertyList(from: original, format: nil) as! [String: Any]
        check(plist["RunAtLoad"] as? Bool == true, "loaded at login")
        check(plist["ProgramArguments"] as? [String] == [startup.executableURL.path], "starts native app directly")
        check(plist["AssociatedBundleIdentifiers"] as? [String] == ["local.codex.UUDisplayAutomation"], "owned background item identity")
        try startup.setEnabled(false); check(try !startup.isEnabled(), "persistent disable reflected")
        check(try Data(contentsOf: startup.agentURL) == original, "toggle does not rewrite existing job")
        try startup.setEnabled(true); check(try startup.isEnabled(), "re-enable reflected")
        disabled = nil; check(try startup.isEnabled(), "absent override uses installed RunAtLoad")
        failWrite = true
        do { try startup.setEnabled(false); preconditionFailure("failed mutation accepted") } catch { checks += 1 }
        check(try startup.isEnabled(), "failed toggle retains effective state")
        failWrite = false; failRead = true
        do { _ = try startup.isEnabled(); preconditionFailure("failed read accepted") } catch { checks += 1 }
        failRead = false
        let newInstall = LoginAtStartup(home: home.appendingPathComponent("new-install"), userID: 501, run: startup.run)
        failWrite = true
        do { try newInstall.setEnabled(true); preconditionFailure("failed new registration accepted") } catch { checks += 1 }
        check(!FileManager.default.fileExists(atPath: newInstall.agentURL.path), "failed new registration removes only its own newly created file")
        failWrite = false
        for token in ["disabled", "true", "enabled", "false"] {
            let actual = try LoginAtStartup.disabledOverride(in: "disabled services = {\n \"local.codex.uu-display\" => \(token)\n}")
            check(actual == ["disabled", "true"].contains(token), "supported system output \(token)")
        }
        for bad in ["", "unavailable", "disabled services = {\n\"local.codex.uu-display\" => mystery\n}", "disabled services = {\n\"local.codex.uu-display\" => true\n\"local.codex.uu-display\" => false\n}"] {
            do { _ = try LoginAtStartup.disabledOverride(in: bad); preconditionFailure("malformed output accepted") } catch { checks += 1 }
        }
        var foreign = plist; foreign["ProgramArguments"] = ["/another/app"]
        try PropertyListSerialization.data(fromPropertyList: foreign, format: .xml, options: 0).write(to: startup.agentURL)
        let count = commands.count
        do { try startup.setEnabled(true); preconditionFailure("foreign job replaced") } catch { checks += 1 }
        check(commands.count == count, "foreign job never triggers launchctl mutation")
        print("LoginAtStartup: \(checks) checks passed")
    }
}

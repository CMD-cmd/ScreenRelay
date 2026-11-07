import Foundation

@main
struct CursorSizeMenuTests {
    static func main() throws {
        var checks = 0
        func check(_ value: @autoclosure () -> Bool, _ description: String) {
            precondition(value(), description); checks += 1
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let configURL = directory.appendingPathComponent("cursor-profile-config.json")
        check(CursorSizeMenu.currentScale(at: configURL) == 3, "missing config defaults to three")
        check(CursorSizeMenu.scales == [1, 1.5, 2, 2.5, 3, 3.5, 4], "exact supported choices")
        for (index, scale) in CursorSizeMenu.scales.enumerated() {
            check(CursorSizeMenu.scale(forTag: CursorSizeMenu.firstTag + index) == scale, "tag identifies exact whitelist choice")
            try CursorSizeMenu.write(scale: scale, to: configURL)
            check(CursorSizeMenu.currentScale(at: configURL) == scale, "read current actual selection")
            let data = try Data(contentsOf: configURL)
            let config = try JSONSerialization.jsonObject(with: data) as! [String: Any]
            check(Set(config.keys) == Set(["enabled", "scale", "scope"]), "minimal config fields")
            check(config["enabled"] as? Bool == (scale != 1), "original size disables profile")
            check(config["scope"] as? String == "all_supported", "one setting applies to all supported shapes")
        }
        for tag in [0, CursorSizeMenu.firstTag - 1, CursorSizeMenu.firstTag + CursorSizeMenu.scales.count, Int.min, Int.max] {
            check(CursorSizeMenu.scale(forTag: tag) == nil, "unrecognized action tag rejected")
        }
        let previous = try Data(contentsOf: configURL)
        for scale in [Double.nan, Double.infinity, -Double.infinity, 0, 1.1, 4.5] {
            do {
                try CursorSizeMenu.write(scale: scale, to: configURL)
                preconditionFailure("invalid scale accepted")
            } catch { checks += 1 }
            let retained = try Data(contentsOf: configURL)
            check(retained == previous, "invalid input preserves previous config")
        }
        try Data(#"{"enabled":false,"scale":3}"#.utf8).write(to: configURL)
        check(CursorSizeMenu.currentScale(at: configURL) == 1, "disabled actual configuration selects original")
        for invalid in [#"{"enabled":true,"scale":true}"#, #"{"enabled":true,"scale":4.5}"#, "not JSON"] {
            try Data(invalid.utf8).write(to: configURL)
            check(CursorSizeMenu.currentScale(at: configURL) == nil, "invalid actual config does not claim a selection")
        }
        let unavailable = directory.appendingPathComponent("target-directory", isDirectory: true)
        try FileManager.default.createDirectory(at: unavailable, withIntermediateDirectories: true)
        let sentinel = unavailable.appendingPathComponent("existing")
        try previous.write(to: sentinel)
        do {
            try CursorSizeMenu.write(scale: 2, to: unavailable)
            preconditionFailure("directory replacement should fail")
        } catch { checks += 1 }
        let retained = try Data(contentsOf: sentinel)
        check(retained == previous, "failed atomic replacement preserves existing content")
        check(CursorSizeMenu.title(for: 1) == "原始 1 倍", "original title")
        check(CursorSizeMenu.title(for: 1.5) == "1.5 倍", "fractional title")
        print("CursorSizeMenu: \(checks) checks passed")
    }
}

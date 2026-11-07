import Foundation

/// The menu and the controller share one size configuration for supported cursors.
enum CursorSizeMenu {
    static let scales: [Double] = [1, 1.5, 2, 2.5, 3, 3.5, 4]
    static let defaultScale: Double = 3
    static let firstTag = 7100

    private struct Configuration: Codable {
        var enabled: Bool
        var scale: Double
        var scope: String?
    }

    enum ConfigurationError: Error { case unsupportedScale }

    static func scale(forTag tag: Int) -> Double? {
        guard tag >= firstTag, tag < firstTag + scales.count else { return nil }
        return scales[tag - firstTag]
    }

    static func title(for scale: Double) -> String {
        if scale == 1 { return "原始 1 倍" }
        let number = scale.rounded() == scale ? String(Int(scale)) : String(scale)
        return "\(number) 倍"
    }

    static func currentScale(at url: URL) -> Double? {
        guard FileManager.default.fileExists(atPath: url.path) else { return defaultScale }
        guard let data = try? Data(contentsOf: url),
              let config = try? JSONDecoder().decode(Configuration.self, from: data) else { return nil }
        if !config.enabled { return 1 }
        guard config.scale.isFinite, scales.contains(config.scale) else { return nil }
        return config.scale
    }

    static func write(scale: Double, to url: URL) throws {
        guard scale.isFinite, scales.contains(scale) else { throw ConfigurationError.unsupportedScale }
        let config = Configuration(enabled: scale != 1, scale: scale, scope: "all_supported")
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(config)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
    }
}

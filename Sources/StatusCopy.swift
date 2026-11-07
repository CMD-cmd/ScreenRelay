import Foundation

/// User-facing wording for backend diagnostics. Raw text stays available in Diagnostics.
enum StatusCopy {
    static let product = "屏幕接力"

    /// Ordered: the first matching fragment wins.
    private static let rules: [(String, String)] = [
        ("; recovery:", "实体屏恢复未完成，正在重试。"),
        ("Detector stale", "连接检测暂时失效，已恢复实体屏；检测恢复后自动接力。"),
        ("heartbeat gap", "后台计时中断，已恢复实体屏。"),
        ("Native session generation", "UU 会话已变化，已恢复实体屏。"),
        ("System sleep", "Mac 进入睡眠，已恢复实体屏。"),
        ("Remote disconnected", "远程已断开，已恢复实体屏。"),
        ("Controller stopped", "后台已重启，已恢复实体屏。"),
        ("Manual ", "已手动恢复并暂停。"),
        ("No physical output", "没有可用的实体屏，暂时保留虚拟屏。"),
        ("Physical", "实体屏恢复未完成，正在重试。"),
        ("Timed out waiting", "屏幕切换超时，已保持安全状态。"),
        ("irror", "检测到镜像显示，已暂停自动接力。"),
        ("Target", "虚拟屏核验未通过，已恢复实体屏。"),
        ("dentity", "显示器身份核验未通过，已保持安全状态。"),
        ("UUID", "显示器身份核验未通过，已保持安全状态。"),
        ("selector", "显示器尚未就绪，已保持安全状态。"),
        ("BetterDisplay", "暂时无法与 BetterDisplay 通信。"),
        ("Invalid command", "命令无效。"),
    ]

    static func localize(_ raw: String?) -> String? {
        guard let text = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return nil }
        let lower = text.lowercased()
        if lower.contains("edquot") || lower.contains("disk quota exceeded") {
            return "磁盘配额已满，已停止新的切屏。现有恢复记录保留；清理空间后自动继续。"
        }
        if lower.contains("enospc") || lower.contains("no space left") {
            return "磁盘空间不足，已停止新的切屏。现有恢复记录保留；清理空间后自动继续。"
        }
        if text.unicodeScalars.contains(where: { (0x4E00...0x9FFF).contains($0.value) }) { return text }
        for (fragment, message) in rules where text.contains(fragment) { return message }
        return "后台遇到问题，屏幕已保持在安全状态。"
    }

    static func modeLabel(_ mode: String) -> String {
        switch mode.uppercased() {
        case "ACTIVE": return "使用设备虚拟屏"
        case "ARMED": return "等待远程连接"
        case "RESTORING": return "正在恢复实体屏"
        case "PAUSED": return "已暂停"
        case "DISABLED": return "尚未启用"
        default: return mode.isEmpty ? "—" : mode
        }
    }

    static func connectionLabel(_ state: String) -> String {
        ["connected": "已连接", "disconnected": "未连接", "unknown": "正在确认"][state] ?? state
    }
}

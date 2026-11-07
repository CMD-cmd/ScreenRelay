import Foundation

/// Two complete reads of the same known session window, separated in time.
/// UI read failures and positive session evidence cancel the candidate.
struct HomeDisconnectConfirmation {
    private var firstRead: Double?
    mutating func reset() { firstRead = nil }
    mutating func observe(completeKnownHome: Bool, now: Double) -> Bool {
        guard completeKnownHome, now.isFinite else { reset(); return false }
        guard let firstRead, now >= firstRead else { self.firstRead = now; return false }
        return now - firstRead >= 0.6
    }
}

/// Classifies freshly read AX content. Contains no AX access or cached session state.
struct SessionEvidence {
    var names: [String]?
    var activeControllerCount: Int?
    var reason: String

    static func hasControllers(names: [String]?, count: Int?) -> Bool {
        !(names ?? []).isEmpty || (count ?? 0) > 0
    }

    static func classify(windowTexts: [[String]], controllerNames: [String],
                         incomplete: Bool, completeMainWindow: Bool,
                         allowHomeNegative: Bool = true) -> SessionEvidence {
        let names = Array(Set(controllerNames)).sorted()
        let compact = windowTexts.map { $0.joined(separator: " ").filter { !$0.isWhitespace } }
        var sidebarCount = 0
        var hasLocalContext = false
        let pattern = #"本机被控中.{0,120}?当前有([0-9]+)台设备正在控制此电脑"#
        let expression = try! NSRegularExpression(pattern: pattern)
        for text in compact {
            if text.contains("本机被控中") { hasLocalContext = true }
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            for match in expression.matches(in: text, range: range) {
                if let digits = Range(match.range(at: 1), in: text),
                   let count = Int(text[digits]), count > 0 {
                    sidebarCount = max(sidebarCount, count)
                }
            }
        }
        if !names.isEmpty || sidebarCount > 0 {
            return SessionEvidence(names: names, activeControllerCount: max(names.count, sidebarCount),
                                   reason: sidebarCount > 0 ? "ax_local_control_count" : "ax_controller_row")
        }
        if hasLocalContext {
            return SessionEvidence(reason: "ax_local_control_count_unreadable")
        }
        if incomplete { return SessionEvidence(reason: "ax_snapshot_incomplete") }
        let navigation = Set(["我的设备", "全部设备", "远程协助"])
        let completeNavigation = windowTexts.contains { navigation.isSubset(of: Set($0)) }
        if allowHomeNegative && completeMainWindow && completeNavigation {
            return SessionEvidence(names: [], activeControllerCount: 0,
                                   reason: "ax_complete_home_no_local_control")
        }
        return SessionEvidence(reason: "uu_main_content_unavailable")
    }

    static func controllerName(_ texts: [String]) -> String? {
        guard texts.contains(where: { $0.contains("远程控制") }) else { return nil }
        let prefix = texts.compactMap { text -> String? in
            guard let marker = text.range(of: "远程控制") else { return nil }
            let value = String(text[..<marker.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
            return value.isEmpty ? nil : value
        }.first
        return prefix ?? texts.first { !$0.contains("远程控制") && !$0.hasPrefix("连接时长") && !$0.isEmpty }
    }

    // A successful read of the same previously verified inbound table is
    // stronger than absence of a sidebar in a hidden/partially rendered window.
    static func cachedControllerTable(rowTexts: [[String]], rowCount: Int?, valid: Bool) -> SessionEvidence? {
        guard valid, let count = rowCount, count >= 0, count <= 32, rowTexts.count == count else { return nil }
        if count == 0 {
            return SessionEvidence(names: [], activeControllerCount: 0, reason: "ax_cached_controller_table_empty")
        }
        let names = rowTexts.compactMap(controllerName)
        guard names.count == count, Set(names).count == count else { return nil }
        return SessionEvidence(names: names.sorted(), activeControllerCount: count, reason: "ax_cached_controller_table")
    }
}

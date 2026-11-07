import Foundation

@main
struct SessionEvidenceTests {
    static func main() {
        var checked = 0
        func check(_ condition: @autoclosure () -> Bool, _ description: String) {
            precondition(condition(), description)
            checked += 1
        }
        func classify(_ texts: [String], names: [String] = [], incomplete: Bool = false,
                      complete: Bool = true) -> SessionEvidence {
            SessionEvidence.classify(windowTexts: [texts], controllerNames: names,
                                     incomplete: incomplete, completeMainWindow: complete)
        }
        let home = ["我的设备", "全部设备", "远程协助", "收藏设备", "开始协助"]
        let sidebar = classify(home + ["本机被控中", "当前有 1 台设备正在控制此电脑"])
        check(sidebar.activeControllerCount == 1 && sidebar.names == [], "sidebar one is positive without details")
        check(sidebar.reason == "ax_local_control_count", "sidebar reason")
        check(SessionEvidence.hasControllers(names: [], count: 1), "sidebar-only positive suppresses clock layout reads")
        check(SessionEvidence.hasControllers(names: ["iPad"], count: nil), "named controller suppresses structural retries")
        check(!SessionEvidence.hasControllers(names: [], count: 0), "empty session permits reconnect structure events")
        check(!SessionEvidence.hasControllers(names: nil, count: nil), "unknown UI permits bounded structure settling")
        var confirmation = HomeDisconnectConfirmation()
        check(!confirmation.observe(completeKnownHome: true, now: 1000), "first hidden home read is insufficient")
        check(!confirmation.observe(completeKnownHome: true, now: 1000.3), "same event burst cannot confirm disconnect")
        check(confirmation.observe(completeKnownHome: true, now: 1000.7), "second complete known home read confirms removal")
        check(!confirmation.observe(completeKnownHome: false, now: 1001), "positive or incomplete read cancels candidate")
        check(!confirmation.observe(completeKnownHome: true, now: 1002), "new candidate needs independent settling")
        check(!confirmation.observe(completeKnownHome: true, now: 999), "clock reversal cannot confirm")
        check(!confirmation.observe(completeKnownHome: true, now: .nan), "invalid clock cannot confirm")
        let table = classify([], names: ["controller A"])
        check(table.names == ["controller A"] && table.activeControllerCount == 1, "controller table positive")
        check(classify(home, incomplete: true).names == nil, "incomplete empty tree unknown")
        check(classify(["我的设备"]).names == nil, "single home label is insufficient")
        check(classify(home + ["本机被控中"]).names == nil, "unreadable local count unknown")
        check(classify(home + ["本机被控中", "当前有零台设备正在控制此电脑"]).names == nil, "non-numeric local count unknown")
        check(classify(home + ["本机被控中", "当前有0台设备正在控制此电脑"]).names == nil, "zero banner not negative evidence")
        let empty = classify(home)
        check(empty.names == [] && empty.activeControllerCount == 0, "complete home negative evidence")
        check(empty.reason == "ax_complete_home_no_local_control", "distinct negative reason")
        check(classify(home, complete: false).names == nil, "main window boundary required")
        let multiple = classify(["本机被控中 当前有3台设备正在控制此电脑"], names: ["A", "B"])
        check(multiple.activeControllerCount == 3 && multiple.names?.count == 2, "preserve multiple count and actual names")
        check(classify(home + ["远程控制", "当前有1台设备正在控制此电脑"]).activeControllerCount == 0, "remote action text is not local context")
        check(classify(["本机被控中 当前有2台设备正在控制此电脑"], incomplete: true).activeControllerCount == 2, "explicit positive survives partial unrelated tree")
        let split = SessionEvidence.classify(windowTexts: [["本机被控中"], ["当前有1台设备正在控制此电脑"]], controllerNames: [], incomplete: false, completeMainWindow: true)
        check(split.names == nil, "never pair text across window boundaries")
        let scatteredHome = SessionEvidence.classify(windowTexts: [["我的设备"], ["全部设备", "远程协助"]], controllerNames: [], incomplete: false, completeMainWindow: true)
        check(scatteredHome.names == nil, "never combine home navigation across windows")
        let hiddenHome = SessionEvidence.classify(windowTexts: [home], controllerNames: [], incomplete: false,
                                                  completeMainWindow: true, allowHomeNegative: false)
        check(hiddenHome.names == nil, "hidden or minimized home cannot prove disconnect")
        check(SessionEvidence.cachedControllerTable(rowTexts: [], rowCount: 0, valid: true)?.activeControllerCount == 0,
              "known live inbound table with zero rows is explicit negative")
        check(SessionEvidence.cachedControllerTable(rowTexts: [], rowCount: nil, valid: true) == nil,
              "failed row read never becomes zero")
        check(SessionEvidence.cachedControllerTable(rowTexts: [], rowCount: 0, valid: false) == nil,
              "destroyed table never becomes disconnect")
        check(SessionEvidence.cachedControllerTable(rowTexts: [], rowCount: 1, valid: true) == nil,
              "partial row list never becomes disconnect")
        let liveRow = SessionEvidence.cachedControllerTable(rowTexts: [["iPad 远程控制 连接时长 00:10"]], rowCount: 1, valid: true)
        check(liveRow?.names == ["iPad"], "combined live inbound row parsed")
        check(SessionEvidence.cachedControllerTable(rowTexts: [["iPad", "远程控制", "连接时长 00:10"], ["Mac", "远程控制"]],
                                                    rowCount: 2, valid: true)?.activeControllerCount == 2,
              "multiple peers remain connected")
        check(SessionEvidence.cachedControllerTable(rowTexts: [["iPad", "其他状态"]], rowCount: 1, valid: true) == nil,
              "unrecognized row schema fails closed")
        print("SessionEvidence: \(checked) checks passed")
    }
}

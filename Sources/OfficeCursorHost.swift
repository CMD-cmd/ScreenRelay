import Foundation

enum OfficeCursorHost: String, CaseIterable, Identifiable, Codable {
    case excel
    case powerPoint

    var id: String { rawValue }
    var name: String { self == .excel ? "Excel" : "PowerPoint" }
    var bundleID: String { self == .excel ? "com.microsoft.Excel" : "com.microsoft.Powerpoint" }
    var addinName: String { self == .excel ? "UUDisplayNativeCursor.xlam" : "UUDisplayPowerPointCursor.ppam" }
    var directoryName: String { name }
}

enum OfficeCursorSetupPhase: String {
    case missingApp, notInstalled, legacy, prepared, waitingForOffice
    case active, idle, outdated, blocked, failed, recoveryNeeded
}

struct OfficeCursorSetupStatus: Identifiable {
    let host: OfficeCursorHost
    var phase: OfficeCursorSetupPhase
    var title: String
    var detail: String
    var appVersion: String?
    var isRunning: Bool
    var hasFiles: Bool
    var canInstall: Bool
    var scale: Double?
    var id: String { host.id }
    var hasLoadedClient: Bool { phase == .active || phase == .idle }
    var needsOfficeQuitBeforeInstall: Bool { isRunning && [.notInstalled, .legacy, .outdated].contains(phase) }
}

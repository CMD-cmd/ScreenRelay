import Foundation
import Darwin

/// Kernel birth time also covers login services launched directly by launchd,
/// for which NSRunningApplication.launchDate is not guaranteed to exist.
enum PermissionProcessIdentity {
    static func startedAt(_ pid: Int) -> Double? {
        guard pid > 1, pid <= Int(Int32.max) else { return nil }
        var info = proc_bsdinfo()
        let expected = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(Int32(pid), PROC_PIDTBSDINFO, 0, &info, expected) == expected else { return nil }
        return Double(info.pbi_start_tvsec) + Double(info.pbi_start_tvusec) / 1_000_000
    }
}

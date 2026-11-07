import Foundation

@main
struct StorageHealthTests {
    static func main() throws {
        var checks = 0
        func check(_ condition: Bool, _ message: String) { precondition(condition, message); checks += 1 }
        let base: [String: Any] = ["version": 1, "pid": 123, "phase": "pressure", "free_bytes": 1024,
            "retry_after": 5.0, "reason": "space", "recovery_pending": true, "mode": "RESTORING"]
        func payload(_ changes: [String: Any]) -> [String: Any] { base.merging(changes) { _, new in new } }
        let decoded = StorageHealthSnapshot.decode(base)!
        check(decoded.pid == 123 && decoded.freeBytes == 1024 && decoded.retryAfter == 5, "native decoding")
        check(decoded.recoveryPending && decoded.mode == "RESTORING", "recovery metadata preserved")
        let json = try JSONSerialization.data(withJSONObject: base)
        check(StorageHealthSnapshot.decode(try JSONSerialization.jsonObject(with: json) as! [String: Any]) != nil, "JSON numeric decoding")
        check(StorageHealthSnapshot.decode(payload(["free_bytes": NSNull()]))?.freeBytes == nil, "unknown capacity")
        let invalid: [[String: Any]] = [
            ["version": 2], ["version": true], ["pid": 1], ["pid": 123.5], ["pid": true],
            ["pid": Int64(Int32.max) + 1], ["phase": "other"], ["reason": "other"],
            ["free_bytes": -1], ["free_bytes": 1.5], ["free_bytes": true], ["free_bytes": "0"],
            ["retry_after": -1], ["retry_after": Double.infinity], ["retry_after": Double.nan],
            ["retry_after": true], ["recovery_pending": 1], ["mode": 1]
        ]
        for change in invalid { check(StorageHealthSnapshot.decode(payload(change)) == nil, "reject malformed field \(change.keys)") }
        for key in base.keys {
            var missing = base; missing.removeValue(forKey: key)
            check(StorageHealthSnapshot.decode(missing) == nil, "required field \(key)")
        }
        check(decoded.message.contains("磁盘空间不足") && decoded.message.contains("1.0 KB"), "explicit pressure and concise space")
        check(decoded.message.contains("停止新的切屏") && decoded.message.contains("现有恢复记录保留") && decoded.message.contains("清理空间后自动继续"), "action and retention copy")
        check(!decoded.message.contains("已恢复") && !decoded.message.contains("恢复完成"), "never claims physical recovery")
        check(StorageHealthSnapshot.decode(payload(["reason": "quota"]))!.message.contains("配额已满"), "quota copy")
        let center = StorageHealthCenter()
        var notifications = 0
        let observer = NotificationCenter.default.addObserver(forName: StorageHealthCenter.didChange, object: center, queue: nil) { _ in notifications += 1 }
        defer { NotificationCenter.default.removeObserver(observer) }
        check(!center.accept(base, expectedPID: 124, now: 10), "reject wrong controller")
        check(!center.accept(base, expectedPID: 123, now: -1), "reject negative clock")
        check(!center.accept(base, expectedPID: 123, now: .nan), "reject invalid clock")
        check(center.accept(base, expectedPID: 123, now: 10), "accept controller pressure")
        check(center.active(now: 130) != nil && center.active(now: 130.001) == nil, "120 second TTL boundary")
        check(center.active(now: 9) == nil && center.active(now: -1) == nil && center.active(now: .infinity) == nil, "reject future receipt and invalid query time")
        check(!center.accept(base, expectedPID: 123, now: 9), "reject backwards receipt clock")
        check(center.accept(base, expectedPID: 123, now: 20), "legal repeat refresh")
        check(notifications == 2, "notify every legal update only")
        let staleStatus: [String: Any] = ["timestamp": 0.0, "mode": "ACTIVE"]
        func presentation(storage: StorageHealthSnapshot?, trusted: Bool = true, sleeping: Bool = false) -> StatusBarPresentation {
            StatusBarPresentation.resolve(connection: "connected", trusted: trusted, sleeping: sleeping,
                status: staleStatus, now: 1000, storage: storage)
        }
        check(presentation(storage: center.active(now: 21)).kind == .attention && presentation(storage: center.active(now: 21)).detail == decoded.message, "pressure survives stale disk status")
        check(presentation(storage: decoded, trusted: false).detail.contains("辅助功能"), "permission takes priority")
        check(presentation(storage: decoded, sleeping: true).kind == .sleeping, "sleep takes priority")
        check(center.accept(payload(["phase": "healthy"]), expectedPID: 123, now: 30), "accept healthy")
        check(center.active(now: 30) == nil && notifications == 3, "healthy clears and notifies")
        let normal = StatusBarPresentation.resolve(connection: "connected", trusted: true, sleeping: false,
            status: ["timestamp": 1000.0, "mode": "ACTIVE"], now: 1000, storage: center.active(now: 30))
        check(normal.kind == .remote, "normal presentation returns")
        check(center.accept(base, expectedPID: 123, now: 40), "pressure can recur")
        center.clear()
        check(center.active(now: 40) == nil && notifications == 5, "clear removes state and notifies")
        for raw in ["ENOSPC; recovery: Physical failed", "no space left on device", "中文 ENOSPC"] {
            check(StatusCopy.localize(raw)?.contains("磁盘空间不足") == true, "space errors precede generic recovery")
        }
        for raw in ["EDQUOT; recovery: Physical failed", "disk quota exceeded"] {
            check(StatusCopy.localize(raw)?.contains("配额已满") == true, "quota errors precede generic recovery")
        }
        print("StorageHealth: \(checks) checks passed")
    }
}

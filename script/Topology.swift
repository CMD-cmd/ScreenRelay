import Foundation
import CoreGraphics
import ColorSync

// Read-only native display positions; no accessibility or screen capture access.
var ids = [CGDirectDisplayID](repeating: 0, count: 64)
var count: UInt32 = 0
guard CGGetOnlineDisplayList(UInt32(ids.count), &ids, &count) == .success else { exit(1) }
if CommandLine.arguments.count > 1 {
    guard CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--modes",
          let id = UInt32(CommandLine.arguments[2]), ids.prefix(Int(count)).contains(id) else { exit(2) }
    let options = [kCGDisplayShowDuplicateLowResolutionModes: true] as CFDictionary
    let modes = (CGDisplayCopyAllDisplayModes(id, options) as? [CGDisplayMode] ?? []).map { mode -> [String: Any] in
        ["width": mode.width, "height": mode.height, "pixelWidth": mode.pixelWidth,
         "pixelHeight": mode.pixelHeight, "refreshRate": mode.refreshRate, "modeID": mode.ioDisplayModeID]
    }
    let value: [String: Any] = ["displayID": id, "vendor": CGDisplayVendorNumber(id),
        "model": CGDisplayModelNumber(id), "serial": CGDisplaySerialNumber(id), "modes": modes,
        "uuid": CGDisplayCreateUUIDFromDisplayID(id).map { CFUUIDCreateString(nil, $0.takeRetainedValue()) as String } ?? ""]
    print(String(decoding: try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), as: UTF8.self))
    exit(0)
}
let displays: [[String: Any]] = ids.prefix(Int(count)).map { id in
    let bounds = CGDisplayBounds(id)
    let mode = CGDisplayCopyDisplayMode(id)
    return ["displayID": id, "originX": Int(bounds.origin.x), "originY": Int(bounds.origin.y),
            "width": Int(bounds.width), "height": Int(bounds.height),
            "pixelWidth": mode?.pixelWidth ?? 0, "pixelHeight": mode?.pixelHeight ?? 0,
            "refreshRate": mode?.refreshRate ?? 0, "rotation": CGDisplayRotation(id),
            "main": CGDisplayIsMain(id) != 0, "builtin": CGDisplayIsBuiltin(id) != 0,
            "uuid": CGDisplayCreateUUIDFromDisplayID(id).map { CFUUIDCreateString(nil, $0.takeRetainedValue()) as String } ?? "",
            "vendor": CGDisplayVendorNumber(id), "model": CGDisplayModelNumber(id),
            "serial": CGDisplaySerialNumber(id), "mirrored": CGDisplayIsInMirrorSet(id) != 0,
            "mode_identifier": mode?.ioDisplayModeID ?? 0]
}
let data = try JSONSerialization.data(withJSONObject: displays, options: [.sortedKeys])
print(String(decoding: data, as: UTF8.self))

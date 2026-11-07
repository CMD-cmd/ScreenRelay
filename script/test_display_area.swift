import AppKit
import SwiftUI

private struct HardwareGallery: View {
    let catalog: [WorkbenchDevice]
    struct Item {
        let title: String
        let id: String
        let landscape: Bool
        var safe = false
    }
    private let items = [
        Item(title:"iPad Pro M4 · 横屏",id:"ipad-pro-13-m4",landscape:true),
        Item(title:"iPad Pro M4 · 竖屏",id:"ipad-pro-13-m4",landscape:false),
        Item(title:"iPad mini A17 Pro · 横屏",id:"ipad-mini-a17-pro",landscape:true),
        Item(title:"iPad mini A17 Pro · 竖屏",id:"ipad-mini-a17-pro",landscape:false),
        Item(title:"iPhone 16 Pro Max · 完整屏幕",id:"iphone-16-pro-max",landscape:true),
        Item(title:"iPhone 16 Pro Max · 每边 7%",id:"iphone-16-pro-max",landscape:true,safe:true),
        Item(title:"iPhone 16 Pro Max · 完整屏幕",id:"iphone-16-pro-max",landscape:false),
        Item(title:"iPhone 16 Pro Max · 每边 7%",id:"iphone-16-pro-max",landscape:false,safe:true),
        Item(title:"iPhone 12 · 横屏",id:"iphone-12",landscape:true),
        Item(title:"iPhone 12 · 竖屏",id:"iphone-12",landscape:false),
        Item(title:"iPhone 13 · 横屏",id:"iphone-13",landscape:true),
        Item(title:"iPhone 16e · 竖屏",id:"iphone-16e",landscape:false),
        Item(title:"iPhone SE · 横屏",id:"iphone-se-3",landscape:true),
        Item(title:"iPhone SE · 竖屏",id:"iphone-se-3",landscape:false),
        Item(title:"iPad 第 9 代 · 横屏",id:"ipad-9",landscape:true),
        Item(title:"iPad 第 9 代 · 竖屏",id:"ipad-9",landscape:false)
    ]
    var body: some View {
        VStack(alignment:.leading,spacing:16) {
            Text("屏幕接力 · 设备外观与显示范围").font(.system(size:24,weight:.semibold))
            LazyVGrid(columns:Array(repeating:GridItem(.fixed(280)),count:4),spacing:14) {
                ForEach(Array(items.enumerated()),id:\.offset) { _, item in
                    VStack(spacing:10) {
                        let device = catalog.first { $0.id == item.id }
                        let long = device?.width ?? 2868
                        let short = device?.height ?? 1320
                        let w = item.landscape ? long : short
                        let h = item.landscape ? short : long
                        let safeLong = item.safe ? Int(floor(Double(long) * 0.86 / 2 + 0.5)) * 2 : long
                        DeviceFrame(width:w,height:h,fit:CGSize(width:240,height:145),
                            canvasWidth:item.landscape ? safeLong : short,
                            canvasHeight:item.landscape ? short : safeLong,
                            hardware:device?.frameHardware ?? .generic) {
                                Text("HiDPI").font(.system(size:10,weight:.semibold)).foregroundStyle(.white)
                            }.frame(width:240,height:145)
                        Text(item.title).font(.system(size:12,weight:.medium)).foregroundStyle(.white).lineLimit(1)
                    }
                    .frame(width:280,height:190).background(Brand.stage,in:RoundedRectangle(cornerRadius:12))
                }
            }
        }.padding(22).background(Color(nsColor:.windowBackgroundColor))
    }
}

@main @MainActor struct DisplayAreaTests {
    static func main() throws {
        var checks = 0
        func check(_ result: Bool, _ message: String) {
            if !result { fputs("FAILED: \(message)\n", stderr); exit(1) }; checks += 1
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
        let rows = fixture["cases"] as! [[String: Any]]
        let model = WorkbenchModel(directory: directory, sendCommand: { _ in preconditionFailure("geometry edits stay in the draft") })
        defer { model.suspend() }
        model.catalog = (fixture["catalog"] as! [[String: Any]]).compactMap(WorkbenchDevice.init)
        model.addDevice()
        for row in rows {
            let raw = row["profile"] as! [String: Any]
            let device = WorkbenchDevice(raw)!
            model.update { $0 = WorkbenchDraft(device) }
            let expectedCanvas = row["canvas"] as! [Int]
            check(model.draft.canvasPixels?.0 == expectedCanvas[0] && model.draft.canvasPixels?.1 == expectedCanvas[1], "Swift/Python canvas agrees: \(device.label)")
            check(model.draft.validation == nil, "valid canvas and effective resolution: \(device.label)")
            check(model.resolutions() == row["presets"] as! [String], "Swift/Python preset parity: \(device.label) \(device.orientation)")
            let payload = model.draft.payload
            check(payload["native_width"] as? Int == device.width && payload["native_height"] as? Int == device.height, "native specification retained")
            check(payload["cursor_scale"] as? Double == device.cursor, "cursor retained")
            let encoded = try JSONEncoder().encode(model.draft)
            check(try JSONDecoder().decode(WorkbenchDraft.self, from: encoded) == model.draft, "draft handoff retains independent area settings")
        }
        model.update { $0.orientation = "landscape" }; model.selectCatalog("iphone-16-pro-max")
        model.update { $0.resolution = "1434x660"; $0.cursor = 2.5 }
        let original = model.draft
        check(original.displayAreaMode == "full", "old profile defaults full")
        model.update { $0.setDisplayAreaMode("safe") }
        check(model.draft.resolution == "1233x660" && model.draft.displayAreaInset == 7, "default safe area retains short edge")
        model.update { $0.setDisplayAreaInset(10) }
        check(model.draft.resolution == "1147x660", "margin adjustment preserves text scale")
        model.update { $0.rotate(to: "portrait") }
        check(model.draft.displayAreaMode == "full" && model.draft.resolution == "660x1434", "portrait has independent full setting")
        model.update { $0.setDisplayAreaMode("safe"); $0.setDisplayAreaInset(8.5) }
        check(model.draft.resolution == "660x1190" && model.draft.validation == nil, "portrait mode uses its own percentage")
        model.update { $0.rotate(to: "landscape") }
        check(model.draft.displayAreaInset == 10 && model.draft.resolution == "1147x660", "landscape preference remembered")
        model.update { $0.setDisplayAreaMode("full") }
        check(model.draft.resolution == original.resolution && model.draft.cursor == original.cursor, "full range restores font scale and cursor")
        let last = model.draft
        model.update { $0.setDisplayAreaInset(Double.nan); $0.setDisplayAreaInset(5.1); $0.setDisplayAreaMode("unknown") }
        check(model.draft == last, "invalid control values rejected before geometry work")
        let oldData = try JSONSerialization.data(withJSONObject: ["label":"old", "catalogID":"", "width":"2752", "height":"2064", "ppi":"264", "orientation":"landscape", "resolution":"1280x960", "cursor":3.0])
        let restored = try JSONDecoder().decode(WorkbenchDraft.self, from: oldData)
        check(restored.displayAreaMode == "full" && restored.resolution == "1280x960", "pre-feature authorization draft decodes")
        for malformed: [String: Any] in [["landscape":["mode":true]], ["landscape":["mode":2]],
            ["portrait":["inset_percent":true]], ["portrait":["inset_percent":"7"]], ["portrait":["mode":NSNull()]]] {
            check(DisplayAreaSettings(malformed) == nil, "malformed status uses backend-equivalent validation")
        }
        var square = WorkbenchDraft()
        square.width = "3000"; square.height = "2500"; square.ppi = "300"; square.resolution = "1500x1250"
        square.setDisplayAreaMode("safe"); square.rotate(to: "portrait")
        square.setDisplayAreaMode("safe"); square.setDisplayAreaInset(12); square.rotate(to: "landscape")
        check(square.validation == "请减小竖屏留白，使桌面保持纵向。", "both saved directions are validated")
        model.selectCatalog("ipad-pro-11-m4"); model.update { $0.resolution = "1016x701"; $0.setDisplayAreaInset(0); $0.setDisplayAreaMode("safe") }
        check(model.draft.resolution == "1016x701", "zero margin preserves exact selected logical size")
        check(DeviceHardware.forCatalog("ipad-pro-13-m4") == .tabletLongCamera, "M4 Pro camera on long edge")
        check(DeviceHardware.forCatalog("ipad-mini-a17-pro") == .tabletShortCamera, "A17 mini camera on short edge")
        check(DeviceHardware.forCatalog("iphone-16-pro-max") == .dynamicIsland, "iPhone Pro displays island")
        check(DeviceHardware.forCatalog("iphone-16e") == .compactNotch, "16e displays notch")
        check(DeviceHardware.forCatalog("iphone-12-mini") == .wideNotch, "12 mini displays wide notch")
        check(DeviceHardware.forCatalog("custom") == .generic, "custom hardware uses neutral frame")
        check(DeviceHardware.forDescriptor("tablet_long_camera",catalogID:"ipad-mini-a17-pro") == .tabletLongCamera, "catalog metadata supplies hardware")
        check(DeviceHardware.forDescriptor("unknown",catalogID:"iphone-16-pro-max") == .generic, "unknown descriptor remains neutral")
        for hardware in [DeviceHardware.tabletLongCamera, .tabletShortCamera, .tabletHomeButton, .phoneHomeButton, .wideNotch, .compactNotch, .dynamicIsland] {
            for landscape in [true, false] {
                let size = landscape ? CGSize(width:220, height:100) : CGSize(width:100, height:220)
                let rect = hardware.openingRect(in:size,landscape:landscape)!
                check(CGRect(origin:.zero,size:size).contains(rect), "opening remains inside physical frame")
                if hardware == .tabletLongCamera {
                    check(hardware.openingEdge(landscape:landscape) == (landscape ? .top : .leading), "long camera rotates with device")
                } else {
                    check(hardware.openingEdge(landscape:landscape) == (landscape ? .leading : .top), "short opening rotates with device")
                }
                if hardware.hasHomeButton {
                    let body = hardware.bodySize(forDisplay:size)
                    let display = hardware.homeDisplayRect(in:body,landscape:landscape)!
                    check(abs(display.width/display.height-size.width/size.height) < 0.00001,"Home bezel preserves screen proportions")
                    let button = hardware.homeButtonRect(in:body,landscape:landscape)!
                    check(CGRect(origin:.zero,size:body).contains(button),"Home button stays in body")
                    check(!display.intersects(button),"Home button stays outside desktop")
                    check(hardware.homeButtonEdge(landscape:landscape) == (landscape ? .trailing : .bottom),"Home button rotates opposite camera")
                }
            }
        }
        model.selectCatalog("iphone-se-3")
        check(model.draft.frameHardware == .phoneHomeButton && !model.draft.supportsDisplayArea,"Home iPhone uses matching frame and streamlined picture options")
        model.selectCatalog("iphone-duo")
        check(model.draft.displayVariants?.count == 2 && model.draft.displayVariant == "outer","multi-panel device starts with published outer panel")
        let duoCursor = model.draft.cursor
        model.selectDisplayVariant("inner")
        check(model.draft.width == "2670" && model.draft.height == "1878" && model.draft.ppi == "430","verified panel specs selected together")
        check(model.draft.validation == nil && model.draft.cursor == duoCursor,"panel switch preserves cursor and valid HiDPI")
        check(model.resolutions() == ["1335x939","1202x845","1068x751","993x699","890x626"],"inner panel derives its own scale choices")
        check(model.draft.payload["display_variant"] as? String == "inner","panel selection submitted with geometry")
        model.selectDisplayVariant("outer")
        check(model.draft.resolution == "814x559","panel roundtrip retains the exact selected preset")
        model.selectCatalog("")
        model.update { $0.width = "3000"; $0.height = "2000"; $0.ppi = "300"; $0.resolution = "1200x800" }
        check(model.draft.displayVariants == nil && model.draft.displayVariant == nil && model.draft.frameHardware == .generic,"custom selection clears catalog metadata")
        check(model.draft.validation == nil && model.draft.payload["display_variant"] == nil,"custom draft remains valid after multi-panel selection")
        print("Display area native: \(checks) checks passed")
        if CommandLine.arguments.count > 2 {
            let renderer = ImageRenderer(content:HardwareGallery(catalog:model.catalog).environment(\.colorScheme,.light))
            renderer.scale = 2
            guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
                  let bitmap = NSBitmapImageRep(data:tiff), let png = bitmap.representation(using:.png,properties:[:]) else {
                throw CocoaError(.fileWriteUnknown)
            }
            try png.write(to:URL(fileURLWithPath:CommandLine.arguments[2]))
            print("Hardware gallery rendered")
        }
    }
}

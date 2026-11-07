import AppKit

@main struct IPhoneWorkbenchTests {
    static func main() throws {
        var checks=0
        func check(_ value: Bool, _ message: String) { precondition(value,message); checks += 1 }
        let directory=FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let data=try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let rows=try JSONSerialization.jsonObject(with: data) as! [[String:Any]]
        let model=WorkbenchModel(directory:directory,sendCommand:{ _ in preconditionFailure("model selection is only a draft") })
        defer { model.suspend() }
        model.catalog=rows.compactMap(WorkbenchDevice.init)
        let legacyMini=WorkbenchDevice(["id":"ipad-mini-a17-pro","label":"iPad mini (A17 Pro)",
            "native_width":2266,"native_height":1488,"ppi":326.0])!
        check(legacyMini.cursor==3.5 && legacyMini.presetResolutions.isEmpty,"old catalog retains PPI-based cursor fallback")
        check(NSImage(systemSymbolName:"iphone.gen3",accessibilityDescription:nil) != nil,"portrait phone symbol exists")
        model.addDevice()
        for item in model.catalog {
            for orientation in ["landscape","portrait"] {
                model.update { $0.orientation=orientation }
                model.selectCatalog(item.id)
                let expected=orientation==item.orientation ? item.resolution : item.resolution.split(separator:"x").reversed().joined(separator:"x")
                check(model.draft.resolution==expected && model.draft.validation==nil,"catalog default matches backend for \(item.id) \(orientation)")
                let modes=orientation==item.orientation ? item.presetResolutions : item.presetResolutions.map{$0.split(separator:"x").reversed().joined(separator:"x")}
                check(model.resolutions()==modes,"new device uses server presets for \(item.id)")
                check(model.draft.cursor==item.cursor,"cursor default matches backend")
                if item.isPhone { check(NSImage(systemSymbolName:item.symbol,accessibilityDescription:nil) != nil,"phone symbol exists") }
            }
        }
        model.update { $0.orientation="landscape" }; model.selectCatalog("iphone-16-pro-max")
        check(model.draft.resolution=="1147x528" && model.draft.cursor==4,"iPhone16ProMax balanced default")
        model.update { $0.rotate(to:"portrait") }
        check(model.draft.resolution=="528x1147" && model.draft.validation==nil,"rotation retains selected size")
        let saved=try model.permissionDraftState()
        let replacement=WorkbenchModel(directory:directory,sendCommand:{ _ in preconditionFailure("draft restore cannot apply") })
        replacement.restorePermissionDraftState(saved); replacement.suspend()
        check(replacement.draft.catalogID=="iphone-16-pro-max" && replacement.draft.resolution=="528x1147","phone draft survives authorization handoff")
        for visible in [CGRect(x:0,y:24,width:1147,height:450),CGRect(x:0,y:24,width:440,height:908),
                        CGRect(x:0,y:24,width:780,height:336),CGRect(x:-1920,y:0,width:1920,height:1056)] {
            for frame in [CGRect(x:1600,y:400,width:940,height:700),CGRect(x:-5000,y:-3000,width:1100,height:900)] {
                let fitted=WorkbenchWindowLayout.fitting(frame,into:visible)
                check(visible.contains(fitted),"whole window remains within visible desktop")
                let minimum=WorkbenchWindowLayout.minimumSize(visible:visible.size)
                check(fitted.width>=minimum.width && fitted.height>=minimum.height,"minimum bounds also fit phone")
            }
        }
        let desktop=CGRect(x:20,y:40,width:940,height:700)
        check(WorkbenchWindowLayout.fitting(desktop,into:CGRect(x:0,y:0,width:1920,height:1080))==desktop,"desktop window does not move unnecessarily")
        print("iPhone workbench: \(checks) checks passed")
    }
}

import Foundation

@main struct RelayPolicyTests {
    static func main() {
        var checks=0
        func check(_ value:Bool,_ message:String) { precondition(value,message);checks+=1 }
        var status:[String:Any]=["mode":"ACTIVE","desired_enabled":true,"timestamp":100.0]
        var signal:[String:Any]=["state":"connected","trusted":true,"timestamp":100.0]
        var config:[String:Any]=["enabled":true,"scope":"all_supported","scale":3.0]
        func packet(_ now:Double=100)->OfficeCursorPacket { OfficeCursorPacket.make(status:status,signal:signal,config:config,now:now) }
        check(packet()==OfficeCursorPacket(enabled:true,scale:3,expires:185),"known current profile")
        for value in [1.5,2,2.5,3,3.5,4] { config["scale"]=value;check(packet().scale==value,"all supported multipliers") }
        for value:Any in [true,1,0,4.5,"3",Double.nan,Double.infinity] {
            config["scale"]=value;check(!packet().enabled,"reject unsafe multiplier")
        }
        config["scale"]=3.0
        for mode in ["RESTORING","PAUSED","ARMED","DISABLED","SLEEPING"] {
            status["mode"]=mode;check(!packet().enabled,"only active remote session can enable")
        }
        status["mode"]="ACTIVE"
        check(!packet(185).enabled,"one shot expiry fails closed")
        check(packet(184.9).expires==185,"requests cannot extend a lease")
        signal["trusted"]=false;check(!packet().enabled,"permission loss disables")
        signal["trusted"]=1;check(!packet().enabled,"numeric truth not trusted")
        signal["trusted"]=true;signal["state"]="unknown";check(!packet().enabled,"unknown connection disables")
        signal["state"]="connected";signal["sleeping"]=true;check(!packet().enabled,"sleep disables")
        signal["sleeping"]=false;status["desired_enabled"]=false;check(!packet().enabled,"manual pause disables")
        check(!OfficeCursorPacket.make(status:nil,signal:nil,config:nil,now:100).enabled,"missing configuration disables")
        let raw:[String:Any]=["state":"unknown","trusted":true,"timestamp":100.0]
        let held:[String:Any]=["state":"connected","trusted":true,"timestamp":100.0,"session_policy_version":1,"session_source":"held_observation"]
        check(OfficeCursorPacket.sessionSignal(raw:raw,effective:held,policyInstalled:true)?["state"] as? String == "connected","installed policy uses held evidence")
        check(OfficeCursorPacket.sessionSignal(raw:signal,effective:nil,policyInstalled:true)==nil,"missing effective source fails closed")
        check(OfficeCursorPacket.sessionSignal(raw:signal,effective:raw,policyInstalled:true)==nil,"effective source must be explicit")
        check(OfficeCursorPacket.sessionSignal(raw:raw,effective:held,policyInstalled:false)?["state"] as? String == "unknown","legacy unknown remains unknown")
        let directory=FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try! FileManager.default.createDirectory(at:directory,withIntermediateDirectories:true)
        defer { try? FileManager.default.removeItem(at:directory) }
        let relay=OfficeCursorRelay(directory:directory,parentPID:getpid())
        relay.persist(OfficeCursorPacket(enabled:true,scale:3,expires:185),now:100)
        relay.persist(OfficeCursorPacket(enabled:false,scale:1,expires:186),now:101)
        let saved=try! JSONSerialization.jsonObject(with:Data(contentsOf:directory.appendingPathComponent("office-cursor-relay-status.json"))) as! [String:Any]
        check(saved["enabled"] as? Bool == false,"exit overwrites enabled status")
        check(saved["scale"] as? Double == 1,"exit restores recorded Office size")
        check(saved["timestamp"] as? Double == 101,"exit records fresh state")
        print("Office relay policy: \(checks) assertions passed")
    }
}

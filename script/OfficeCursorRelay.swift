import Foundation
import Darwin

struct OfficeCursorPacket: Equatable {
    let enabled: Bool
    let scale: Double
    let expires: Double
    var object: String {
        let data = try! JSONSerialization.data(withJSONObject: ["enabled":enabled,"scale":scale,"expires":expires],options:[.sortedKeys])
        return String(decoding:data,as:UTF8.self)
    }
    static func sessionSignal(raw:[String:Any]?, effective:[String:Any]?, policyInstalled:Bool) -> [String:Any]? {
        guard policyInstalled else { return raw }
        guard let effective=effective,
              effective["session_policy_version"] as? Int == 1,
              let source=effective["session_source"] as? String,
              ["native_observation","probe","held_observation","verified_negative"].contains(source) else { return nil }
        return effective
    }
    static func make(status:[String:Any]?, signal:[String:Any]?, config:[String:Any]?, now:Double) -> Self {
        func yes(_ value:Any?) -> Bool {
            guard let number=value as? NSNumber, CFGetTypeID(number)==CFBooleanGetTypeID() else { return false }
            return number.boolValue
        }
        let disabled=Self(enabled:false,scale:1,expires:now+85)
        guard status?["mode"] as? String == "ACTIVE", yes(status?["desired_enabled"]),
              signal?["state"] as? String == "connected", yes(signal?["trusted"]), !yes(signal?["sleeping"]),
              yes(config?["enabled"]), config?["scope"] as? String == "all_supported",
              let number=config?["scale"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              [1.5,2,2.5,3,3.5,4].contains(number.doubleValue),
              let statusTime=status?["timestamp"] as? Double, let signalTime=signal?["timestamp"] as? Double,
              statusTime.isFinite,signalTime.isFinite,
              now-statusTime >= -5, now-signalTime >= -5 else { return disabled }
        let deadline=min(statusTime,signalTime)+85
        guard deadline>now,deadline-now<=90 else { return disabled }
        return Self(enabled:true,scale:number.doubleValue,expires:deadline)
    }
}

final class OfficeCursorRelay: NSObject {
    static let profile="local.codex.UUDisplayAutomation.OfficeCursorProfile"
    static let request=profile+"Request"
    let directory:URL
    let parentPID:pid_t
    var directoryObserver:StatusFileObserver?
    var parentObserver:DispatchSourceProcess?
    var signals:[DispatchSourceSignal]=[]
    var expiry:DispatchWorkItem?
    var lastInput:[Data]?
    var lastRequest=0.0
    var stopping=false
    init(directory:URL,parentPID:pid_t) { self.directory=directory;self.parentPID=parentPID }
    func input(_ name:String)->Data {
        guard let data=try? Data(contentsOf:directory.appendingPathComponent(name)), data.count<256*1024 else { return Data() }
        return data
    }
    func dictionary(_ data:Data)->[String:Any]? { (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] }
    func post(_ packet:OfficeCursorPacket) {
        DistributedNotificationCenter.default().postNotificationName(Notification.Name(Self.profile),object:packet.object,userInfo:nil,deliverImmediately:true)
    }
    @objc func requested(_ note:Notification) {
        let now=ProcessInfo.processInfo.systemUptime
        guard now-lastRequest>=0.1 else { return };lastRequest=now
        refresh(force:true)
    }
    func refresh(force:Bool=false) {
        guard !stopping else { return }
        let inputs=[input("status.json"),input("signal.json"),input("cursor-profile-config.json"),input("effective-session.json"),input("session-policy.json")]
        guard force || inputs != lastInput else { return };lastInput=inputs
        let now=Date().timeIntervalSince1970
        let policyInstalled=FileManager.default.fileExists(atPath:directory.appendingPathComponent("session-policy.json").path)
        let session=OfficeCursorPacket.sessionSignal(raw:dictionary(inputs[1]),effective:dictionary(inputs[3]),policyInstalled:policyInstalled)
        let packet=OfficeCursorPacket.make(status:dictionary(inputs[0]),signal:session,config:dictionary(inputs[2]),now:now)
        post(packet)
        expiry?.cancel();expiry=nil
        if packet.enabled {
            let work=DispatchWorkItem { [weak self] in self?.refresh(force:true) };expiry=work
            DispatchQueue.main.asyncAfter(deadline:.now()+max(0,packet.expires-now),execute:work)
        }
        persist(packet, now:now)
    }
    func persist(_ packet:OfficeCursorPacket, now:Double) {
        let status:[String:Any]=["pid":getpid(),"parent_pid":parentPID,"enabled":packet.enabled,"scale":packet.scale,"expires":packet.expires,"timestamp":now]
        if let data=try? JSONSerialization.data(withJSONObject:status,options:[.sortedKeys]) {
            try? data.write(to:directory.appendingPathComponent("office-cursor-relay-status.json"),options:.atomic)
        }
    }
    func start() {
        guard parentPID>1,getppid()==parentPID,kill(parentPID,0)==0 else { stop();return }
        DistributedNotificationCenter.default().addObserver(self,selector:#selector(requested(_:)),name:Notification.Name(Self.request),object:nil,suspensionBehavior:.deliverImmediately)
        guard let observer=StatusFileObserver(directory:directory,onChange:{ [weak self] in self?.refresh() }) else { stop();return }
        directoryObserver=observer
        let parent=DispatchSource.makeProcessSource(identifier:parentPID,eventMask:.exit,queue:.main)
        parent.setEventHandler { [weak self] in self?.stop() };parent.resume();parentObserver=parent
        guard getppid()==parentPID,kill(parentPID,0)==0 else { stop();return }
        for value in [SIGTERM,SIGINT,SIGHUP] {
            signal(value,SIG_IGN)
            let source=DispatchSource.makeSignalSource(signal:value,queue:.main)
            source.setEventHandler { [weak self] in self?.stop() };source.resume();signals.append(source)
        }
        refresh(force:true)
    }
    func stop() {
        guard !stopping else { return };stopping=true
        expiry?.cancel();directoryObserver?.stop();parentObserver?.cancel()
        let now=Date().timeIntervalSince1970
        let disabled=OfficeCursorPacket(enabled:false,scale:1,expires:now+85)
        post(disabled);persist(disabled,now:now)
        DistributedNotificationCenter.default().removeObserver(self)
        exit(0)
    }
}

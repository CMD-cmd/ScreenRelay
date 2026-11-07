import Foundation
import Darwin

@main struct OfficeCursorRelayMain {
    static func main() {
        let parentPID=getppid(),args=CommandLine.arguments
        guard parentPID>1,let index=args.firstIndex(of:"--directory"),index+1<args.count else { exit(2) }
        let directory=URL(fileURLWithPath:args[index+1])
        let fd=open(directory.appendingPathComponent("office-cursor-relay.lock").path,O_CREAT|O_RDWR,0o600)
        guard fd>=0,flock(fd,LOCK_EX|LOCK_NB)==0 else { exit(4) }
        let relay=OfficeCursorRelay(directory:directory,parentPID:parentPID)
        relay.start()
        if let index=args.firstIndex(of:"--seconds"),index+1<args.count,let seconds=Double(args[index+1]),seconds>0,seconds<=600 {
            DispatchQueue.main.asyncAfter(deadline:.now()+seconds) { relay.stop() }
        }
        CFRunLoopRun()
    }
}

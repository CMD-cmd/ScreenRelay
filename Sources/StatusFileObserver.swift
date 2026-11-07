import Foundation
import Darwin

/// Watches the directory so atomic replacement of status.json never loses observation.
final class StatusFileObserver {
    private var source: DispatchSourceFileSystemObject?
    private var pending: DispatchWorkItem?
    init?(directory: URL, onChange: @escaping () -> Void) {
        let descriptor = open(directory.path, O_EVTONLY)
        guard descriptor >= 0 else { return nil }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: descriptor,
            eventMask: [.write, .rename, .delete, .revoke], queue: .main)
        self.source = source
        source.setCancelHandler { close(descriptor) }
        source.setEventHandler { [weak self] in
            guard let self else { return }
            self.pending?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.source != nil else { return }
                self.pending = nil; onChange()
            }
            self.pending = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: work)
        }
        source.resume()
    }
    func stop() { pending?.cancel(); pending = nil; source?.cancel(); source = nil }
    deinit { stop() }
}

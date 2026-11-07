import Foundation

/// Full-window bounds, including native titlebar, confined to a visible desktop.
enum WorkbenchWindowLayout {
    static func minimumSize(visible: CGSize) -> CGSize {
        CGSize(width: min(520, visible.width), height: min(360, visible.height))
    }
    static func fitting(_ frame: CGRect, into visible: CGRect) -> CGRect {
        let minimum = minimumSize(visible: visible.size)
        let width = min(visible.width, max(minimum.width, frame.width))
        let height = min(visible.height, max(minimum.height, frame.height))
        return CGRect(x: max(visible.minX, min(frame.minX, visible.maxX - width)),
                      y: max(visible.minY, min(frame.maxY - height, visible.maxY - height)),
                      width: width, height: height)
    }
}

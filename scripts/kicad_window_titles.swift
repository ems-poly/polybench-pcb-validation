import CoreGraphics
import Foundation
let ws = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as! [[String: Any]]
for w in ws {
    let o = (w[kCGWindowOwnerName as String] as? String) ?? ""
    if o.lowercased().contains("kicad") || o.lowercased().contains("eeschema") || o.lowercased().contains("pcbnew") {
        print(o, w[kCGWindowNumber as String] ?? "", w[kCGWindowName as String] ?? "-", w[kCGWindowIsOnscreen as String] ?? "")
    }
}

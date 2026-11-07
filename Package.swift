// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "UUDisplayAutomation", platforms: [.macOS(.v13)], products: [.executable(name: "UUDisplayAutomation", targets: ["UUDisplayAutomation"])], targets: [.executableTarget(name: "UUDisplayAutomation", path: "Sources", linkerSettings: [.linkedFramework("AppKit"), .linkedFramework("ApplicationServices"), .linkedFramework("Carbon")])])

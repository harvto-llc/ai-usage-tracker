// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "UsageMenuBar",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/steipete/SweetCookieKit", from: "0.4.0"),
    ],
    targets: [
        .executableTarget(
            name: "UsageMenuBar",
            dependencies: [
                .product(name: "SweetCookieKit", package: "SweetCookieKit"),
            ],
            path: "Sources",
            linkerSettings: [
                .unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist", "-Xlinker", "Info.plist"]),
            ]
        ),
        .testTarget(
            name: "UsageMenuBarTests",
            dependencies: ["UsageMenuBar"],
            path: "Tests"
        ),
    ]
)

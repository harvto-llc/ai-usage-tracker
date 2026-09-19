import AppKit
import Foundation

/// Owns the bundled backend (`Contents/Resources/backend/<arch>/aicur-backend supervise`).
///
/// The supervisor child runs the API on 127.0.0.1 and the collector loop, and restarts either
/// if it dies. This controller restarts the supervisor itself if it dies, and stops it when the
/// app quits (including on SIGTERM). If the app is force-quit, the supervisor notices that its
/// parent is gone and stops its children on its own.
///
/// A source install has no bundled backend; then this controller does nothing and the app talks
/// to whatever launchd started, exactly as before.
@MainActor
final class BackendController {
    static let shared = BackendController()

    private var process: Process?
    private var stopping = false
    private var restartDelay: TimeInterval = 1
    private var startedAt = Date.distantPast
    private var sigtermSource: DispatchSourceSignal?

    static var bundledExecutable: URL? {
        #if arch(arm64)
        let arch = "arm64"
        #else
        let arch = "x86_64"
        #endif
        guard let url = Bundle.main.resourceURL?
            .appendingPathComponent("backend", isDirectory: true)
            .appendingPathComponent(arch, isDirectory: true)
            .appendingPathComponent("aicur-backend"),
            FileManager.default.isExecutableFile(atPath: url.path)
        else { return nil }
        return url
    }

    /// Turn SIGTERM into a normal quit so applicationWillTerminate stops the backend.
    func installTerminationSignalHandler() {
        guard sigtermSource == nil else { return }
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler {
            NSApplication.shared.terminate(nil)
        }
        source.resume()
        sigtermSource = source
    }

    func start() {
        guard process == nil, !stopping else { return }
        guard let executable = Self.bundledExecutable else {
            NSLog("ai-cur: no bundled backend in this build; using an externally started API")
            return
        }
        let child = Process()
        child.executableURL = executable
        child.arguments = ["supervise"]
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = Self.supervisorLog()
        child.standardError = child.standardOutput
        child.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            Task { @MainActor in
                self?.supervisorExited(status: status)
            }
        }
        do {
            try child.run()
            process = child
            startedAt = Date()
        } catch {
            NSLog("ai-cur: could not start backend: \(error.localizedDescription)")
            scheduleRestart()
        }
    }

    /// Stop the supervisor and wait for it. The supervisor gives its own children a 5 s grace,
    /// so wait a little longer than that before escalating to SIGKILL.
    func stop() {
        stopping = true
        guard let child = process else { return }
        process = nil
        guard child.isRunning else { return }
        child.terminate()
        let deadline = Date().addingTimeInterval(7)
        while child.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if child.isRunning {
            kill(child.processIdentifier, SIGKILL)
        }
    }

    private func supervisorExited(status: Int32) {
        process = nil
        guard !stopping else { return }
        NSLog("ai-cur: backend supervisor exited with status \(status); restarting")
        if Date().timeIntervalSince(startedAt) > 60 {
            restartDelay = 1
        }
        scheduleRestart()
    }

    private func scheduleRestart() {
        let delay = restartDelay
        restartDelay = min(restartDelay * 2, 30)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            Task { @MainActor in
                self?.start()
            }
        }
    }

    private static func supervisorLog() -> Any {
        // Honour HOME like the Python backend does (Path.home()), so both log to one place.
        let home = ProcessInfo.processInfo.environment["HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser
        let logs = home.appendingPathComponent(".usage-tracker/logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let file = logs.appendingPathComponent("supervisor.log")
        if !FileManager.default.fileExists(atPath: file.path) {
            FileManager.default.createFile(atPath: file.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: file) else { return FileHandle.nullDevice }
        handle.seekToEndOfFile()
        return handle
    }
}

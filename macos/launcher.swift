// Launcher for "Roku Remote.app": runs roku_gui.py as a child process.
//
// Why a compiled stub instead of launching python directly: macOS privacy
// prompts (Local Network for reaching the TV, Microphone/Speech for
// dictation) are attributed to the *responsible app*. A python started by
// launchd has none, so LAN access fails with "No route to host" and no
// prompt is ever shown. Children inherit this bundle as their responsible
// app, so macOS asks once on behalf of "Roku Remote" instead.
//
// Python and script paths come from Info.plist (RokuPython, RokuScript),
// written by install-gui-macos.sh.
import Foundation

let info = Bundle.main.infoDictionary ?? [:]
guard let python = info["RokuPython"] as? String,
      let script = info["RokuScript"] as? String else {
    FileHandle.standardError.write("Info.plist is missing RokuPython/RokuScript\n".data(using: .utf8)!)
    exit(1)
}

let child = Process()
child.executableURL = URL(fileURLWithPath: python)
child.arguments = [script] + CommandLine.arguments.dropFirst().filter { !$0.hasPrefix("-psn_") }
var env = ProcessInfo.processInfo.environment
env["PYTHONUNBUFFERED"] = "1"
env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
child.environment = env

for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { child.terminate() }
    source.resume()
    _ = Unmanaged.passRetained(source)  // keep alive for process lifetime
}
child.terminationHandler = { exit($0.terminationStatus) }

do {
    try child.run()
} catch {
    FileHandle.standardError.write("failed to start \(python): \(error)\n".data(using: .utf8)!)
    exit(1)
}
dispatchMain()

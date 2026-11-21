import Foundation

struct BackendRecovery {
    private var readyOwnedProcess: Int32?
    private var lastAutomaticRestart: TimeInterval?

    mutating func observedReady(processID: Int32?, ownedProcessID: Int32?) {
        readyOwnedProcess = processID == ownedProcessID ? ownedProcessID : nil
    }

    static func shouldKeepChecking(ownedProcessRunning: Bool?, hasConnected: Bool) -> Bool {
        // A reused service may restart under its own supervisor. Keep probing
        // without taking ownership. An exited child follows the restart policy.
        ownedProcessRunning ?? hasConnected
    }

    mutating func shouldRestart(afterExit processID: Int32, uptime: TimeInterval) -> Bool {
        guard readyOwnedProcess == processID else { return false }
        readyOwnedProcess = nil
        if let last = lastAutomaticRestart, uptime - last < 60 { return false }
        lastAutomaticRestart = uptime
        return true
    }
}

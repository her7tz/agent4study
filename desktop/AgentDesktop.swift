import Cocoa
import WebKit
import UniformTypeIdentifiers
import Darwin

// The app owns only processes it launches. An already running workspace service
// remains alive when this window or the application closes.
private struct DesktopConfiguration {
    let project: URL
    let python: URL
    let port: Int

    var output: URL { project.appendingPathComponent("summaries", isDirectory: true) }
    var data: URL { project.appendingPathComponent(".local", isDirectory: true) }
    var home: URL { URL(string: "http://127.0.0.1:\(port)/#chat")! }
    var status: URL { URL(string: "http://127.0.0.1:\(port)/api/status")! }

    static func load() throws -> DesktopConfiguration {
        let info = Bundle.main.infoDictionary ?? [:]
        guard let root = info["AgentProjectRoot"] as? String, root.hasPrefix("/"),
              let executable = info["AgentPythonExecutable"] as? String,
              executable.hasPrefix("/") else {
            throw DesktopError.message("桌面应用缺少工作空间配置，请重新构建应用。")
        }
        let port = (info["AgentBackendPort"] as? NSNumber)?.intValue ?? 8765
        guard (1024...65535).contains(port) else {
            throw DesktopError.message("本地服务端口配置无效，请重新构建应用。")
        }
        let project = URL(fileURLWithPath: root, isDirectory: true).standardizedFileURL.resolvingSymlinksInPath()
        let python = URL(fileURLWithPath: executable).standardizedFileURL
        guard FileManager.default.fileExists(atPath: project.appendingPathComponent("agent.py").path),
              FileManager.default.isExecutableFile(atPath: python.path) else {
            throw DesktopError.message("找不到工作空间或 Python 运行环境。请先完成项目的运行环境配置，再重新构建应用。")
        }
        return DesktopConfiguration(project: project, python: python, port: port)
    }
}

private enum DesktopError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let value): return value }
    }
}

private enum ProbeResult {
    case ready
    case unavailable
    case failure(String)
}

private final class DownloadRecord {
    let download: WKDownload
    var destination: URL?
    var temporaryDirectory: URL?
    var temporaryFile: URL?
    init(_ download: WKDownload) { self.download = download }
}

private struct DownloadDestinationRequest {
    let download: WKDownload
    let filename: String
    let completion: (URL?) -> Void
}

@MainActor
private final class AgentDesktop: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var configuration: DesktopConfiguration?
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var connectionLabel = NSTextField(labelWithString: "正在连接本地工作空间…")
    private var messageLabel = NSTextField(wrappingLabelWithString: "")
    private var spinner = NSProgressIndicator()
    private var retryButton: NSButton!
    private var overlay = NSView()
    private var backend: Process?
    private var backendLog: FileHandle?
    private var pollingTimer: Timer?
    private var probeInFlight = false
    private var connectionGeneration = 0
    private var startupDeadline: Date?
    private var hasLoadedPage = false
    private var quitting = false
    private var downloads: [ObjectIdentifier: DownloadRecord] = [:]
    private var destinationRequests: [DownloadDestinationRequest] = []
    private var savePanelActive = false
    private var activeSavePanel: NSSavePanel?
    private let backendQueue = DispatchQueue(label: "agent.desktop.backend", qos: .utility)

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.applicationIconImage = makeAppIcon()
        buildMenus()
        buildWindow()
        buildStatusItem()
        showWindow(nil)
        do {
            configuration = try DesktopConfiguration.load()
            connect(nil)
        } catch {
            showConnectionFailure(error.localizedDescription)
        }
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Agent 工作台"
        window.minSize = NSSize(width: 760, height: 580)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("AgentDesktopMainWindow")
        let container = NSView()
        window.contentView = container

        let webConfiguration = WKWebViewConfiguration()
        webConfiguration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: webConfiguration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(webView)
        overlay.translatesAutoresizingMaskIntoConstraints = false
        overlay.wantsLayer = true
        overlay.layer?.backgroundColor = NSColor(calibratedRed: 0.96, green: 0.97, blue: 0.95, alpha: 1).cgColor
        container.addSubview(overlay)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            webView.topAnchor.constraint(equalTo: container.topAnchor),
            webView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            overlay.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            overlay.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            overlay.topAnchor.constraint(equalTo: container.topAnchor),
            overlay.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        let title = NSTextField(labelWithString: "Agent 工作台")
        title.font = .systemFont(ofSize: 28, weight: .semibold)
        title.textColor = NSColor(calibratedRed: 0.14, green: 0.34, blue: 0.27, alpha: 1)
        connectionLabel.font = .systemFont(ofSize: 16, weight: .medium)
        connectionLabel.alignment = .center
        messageLabel.alignment = .center
        messageLabel.textColor = .secondaryLabelColor
        messageLabel.font = .systemFont(ofSize: 13)
        messageLabel.maximumNumberOfLines = 0
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.isDisplayedWhenStopped = false
        retryButton = NSButton(title: "重新连接", target: self, action: #selector(connect(_:)))
        retryButton.bezelStyle = .rounded
        let stack = NSStackView(views: [title, spinner, connectionLabel, messageLabel, retryButton])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        overlay.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
            stack.widthAnchor.constraint(equalToConstant: 490),
            messageLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
    }

    private func buildMenus() {
        let mainMenu = NSMenu()
        let appMenu = NSMenu()
        let appRoot = NSMenuItem()
        appRoot.submenu = appMenu
        mainMenu.addItem(appRoot)
        appMenu.addItem(withTitle: "关于 Agent 工作台", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let services = NSMenu()
        let servicesItem = NSMenuItem(title: "服务", action: nil, keyEquivalent: "")
        servicesItem.submenu = services
        appMenu.addItem(servicesItem)
        NSApp.servicesMenu = services
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 Agent 工作台", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "隐藏其他应用", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "显示全部", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 Agent 工作台", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let edit = NSMenu(title: "编辑")
        for (title, selector, key) in [("撤销", "undo:", "z"), ("重做", "redo:", "Z"),
                                        ("剪切", "cut:", "x"), ("复制", "copy:", "c"),
                                        ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: NSSelectorFromString(selector), keyEquivalent: key)
        }
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        editItem.submenu = edit
        mainMenu.addItem(editItem)
        let view = NSMenu(title: "显示")
        let contents = view.addItem(withTitle: "打开内容保存目录", action: #selector(openContents(_:)), keyEquivalent: "")
        contents.target = self
        let refresh = view.addItem(withTitle: "刷新工作台", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        refresh.target = self
        let viewItem = NSMenuItem(title: "显示", action: nil, keyEquivalent: "")
        viewItem.submenu = view
        mainMenu.addItem(viewItem)
        let windows = NSMenu(title: "窗口")
        let close = windows.addItem(withTitle: "关闭窗口，保持后台运行", action: #selector(hideWindow(_:)), keyEquivalent: "w")
        close.target = self
        windows.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        let reveal = windows.addItem(withTitle: "显示工作台", action: #selector(showWindow(_:)), keyEquivalent: "")
        reveal.target = self
        let windowsItem = NSMenuItem(title: "窗口", action: nil, keyEquivalent: "")
        windowsItem.submenu = windows
        mainMenu.addItem(windowsItem)
        NSApp.windowsMenu = windows
        NSApp.mainMenu = mainMenu
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let image = NSImage(systemSymbolName: "waveform", accessibilityDescription: "Agent 工作台")
        image?.isTemplate = true
        statusItem.button?.image = image
        let menu = NSMenu()
        let show = menu.addItem(withTitle: "显示主窗口", action: #selector(showWindow(_:)), keyEquivalent: "")
        show.target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 Agent 工作台", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        statusItem.menu = menu
        statusItem.button?.toolTip = "Agent 工作台 · 正在连接"
    }

    @objc private func showWindow(_ sender: Any?) {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func hideWindow(_ sender: Any?) { window?.orderOut(nil) }

    @objc private func openContents(_ sender: Any?) {
        if let output = configuration?.output { NSWorkspace.shared.open(output) }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow(nil)
        return true
    }

    @objc private func reloadPage(_ sender: Any?) {
        if overlay.isHidden { webView.reload() } else { connect(nil) }
    }

    @objc private func connect(_ sender: Any?) {
        guard !quitting else { return }
        if configuration == nil {
            do { configuration = try DesktopConfiguration.load() }
            catch { showConnectionFailure(error.localizedDescription); return }
        }
        pollingTimer?.invalidate()
        connectionGeneration += 1
        probeInFlight = false
        startupDeadline = nil
        showConnecting("正在连接本地工作空间…", detail: "关闭窗口后，Agent 与定时任务会继续在后台运行。")
        probe(allowLaunch: backend == nil || !(backend?.isRunning ?? false))
    }

    private func showConnecting(_ title: String, detail: String) {
        overlay.isHidden = false
        connectionLabel.stringValue = title
        messageLabel.stringValue = detail
        retryButton.isHidden = true
        spinner.startAnimation(nil)
        statusItem.button?.toolTip = "Agent 工作台 · 正在连接"
    }

    private func showConnectionFailure(_ message: String) {
        pollingTimer?.invalidate()
        pollingTimer = nil
        overlay.isHidden = false
        spinner.stopAnimation(nil)
        connectionLabel.stringValue = "暂时无法连接工作空间"
        messageLabel.stringValue = message
        retryButton.isHidden = false
        statusItem.button?.toolTip = "Agent 工作台 · 连接异常"
    }

    private func probe(allowLaunch: Bool = false) {
        guard let config = configuration, !probeInFlight, !quitting else { return }
        probeInFlight = true
        let generation = connectionGeneration
        var request = URLRequest(url: config.status)
        request.timeoutInterval = 12
        request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let result: ProbeResult
            if let error = error as NSError? {
                if error.domain == NSURLErrorDomain && error.code == NSURLErrorCannotConnectToHost {
                    result = .unavailable
                } else {
                    result = .failure("本地服务暂时没有响应，请稍后重新连接。")
                }
            } else if let http = response as? HTTPURLResponse, http.statusCode == 200,
                      let data = data,
                      let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let output = payload["output_dir"] as? String {
                let actual = URL(fileURLWithPath: output).standardizedFileURL.resolvingSymlinksInPath()
                let expected = config.output.standardizedFileURL.resolvingSymlinksInPath()
                result = actual == expected ? .ready : .failure("端口 \(config.port) 正在运行其他工作空间。请先退出其他工作空间的服务，再重新连接。")
            } else {
                result = .failure("端口 \(config.port) 的服务不是当前 Agent 工作空间。请检查正在运行的本地服务。")
            }
            DispatchQueue.main.async {
                guard let self = self, self.connectionGeneration == generation, !self.quitting else { return }
                self.probeInFlight = false
                self.handleProbe(result, allowLaunch: allowLaunch)
            }
        }.resume()
    }

    private func handleProbe(_ result: ProbeResult, allowLaunch: Bool) {
        switch result {
        case .ready:
            startupDeadline = nil
            overlay.isHidden = true
            spinner.stopAnimation(nil)
            statusItem.button?.toolTip = "Agent 工作台 · 本地已连接"
            if !hasLoadedPage {
                hasLoadedPage = true
                webView.load(URLRequest(url: configuration!.home))
            }
            startPolling(interval: 10)
        case .unavailable:
            if allowLaunch {
                startBackend()
            } else if let deadline = startupDeadline, Date() < deadline,
                      backend?.isRunning == true {
                startPolling(interval: 1)
            } else {
                showConnectionFailure("本地服务已停止。点击重新连接可重新启动；详细信息记录在工作空间的 .local/desktop-backend.log。")
            }
        case .failure(let message):
            if let deadline = startupDeadline, Date() < deadline, backend?.isRunning == true {
                startPolling(interval: 2)
            } else {
                showConnectionFailure(message)
            }
        }
    }

    private func startPolling(interval: TimeInterval) {
        if let existing = pollingTimer, existing.timeInterval == interval { return }
        pollingTimer?.invalidate()
        pollingTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.probe() }
        }
    }

    private func startBackend() {
        guard let config = configuration, !quitting else { return }
        showConnecting("正在启动本地 Agent…", detail: "任务与结果保存在这个工作空间。首次连接可能需要稍等片刻。")
        let process = Process()
        process.executableURL = config.python
        process.arguments = ["-u", config.project.appendingPathComponent("agent.py").path, "web", "--port", String(config.port)]
        process.currentDirectoryURL = config.project
        var environment = ProcessInfo.processInfo.environment
        environment["MINIMAL_AGENT_HOME"] = config.data.path
        environment["MINIMAL_AGENT_OUTPUT"] = config.output.path
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PATH"] = [config.python.deletingLastPathComponent().path,
                               config.project.appendingPathComponent(".local/tools").path,
                               "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
                               environment["PATH"] ?? ""].joined(separator: ":")
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        let generation = connectionGeneration
        backend = process
        process.terminationHandler = { [weak self] terminated in
            DispatchQueue.main.async {
                guard let self = self, self.backend === terminated else { return }
                self.backendLog?.closeFile()
                self.backendLog = nil
                if !self.quitting {
                    self.showConnectionFailure("本地服务已退出。点击重新连接可再次启动；详细信息记录在 .local/desktop-backend.log。")
                }
            }
        }
        backendQueue.async { [weak self] in
            do {
                try FileManager.default.createDirectory(at: config.data, withIntermediateDirectories: true,
                                                        attributes: [.posixPermissions: 0o700])
                let log = config.data.appendingPathComponent("desktop-backend.log")
                if !FileManager.default.fileExists(atPath: log.path) {
                    guard FileManager.default.createFile(atPath: log.path, contents: nil,
                                                         attributes: [.posixPermissions: 0o600]) else {
                        throw DesktopError.message("无法创建本地服务日志。")
                    }
                }
                let handle = try FileHandle(forWritingTo: log)
                try handle.seekToEnd()
                process.standardOutput = handle
                process.standardError = handle
                do { try process.run() }
                catch { try? handle.close(); throw error }
                DispatchQueue.main.async {
                    guard let self = self else { process.interrupt(); return }
                    self.backendLog = handle
                    guard !self.quitting, self.connectionGeneration == generation else {
                        if process.isRunning { process.interrupt() }
                        return
                    }
                    self.startupDeadline = Date().addingTimeInterval(45)
                    self.startPolling(interval: 1)
                    self.probe()
                }
            } catch {
                DispatchQueue.main.async {
                    guard let self = self, self.connectionGeneration == generation, !self.quitting else { return }
                    self.backend = nil
                    self.showConnectionFailure("无法启动本地服务：\(error.localizedDescription)")
                }
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateLater }
        quitting = true
        pollingTimer?.invalidate()
        activeSavePanel?.cancel(nil)
        for request in destinationRequests { request.completion(nil) }
        destinationRequests.removeAll()
        for record in downloads.values { record.download.cancel { _ in } }
        guard let process = backend else { return .terminateNow }
        // Serializing behind process startup closes the launch/quit race without
        // waiting on the UI thread. A foreign process is never signalled here.
        backendQueue.async {
            if process.isRunning { process.interrupt() }
            let deadline = Date().addingTimeInterval(3)
            while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
            if process.isRunning { process.terminate() }
            let finalDeadline = Date().addingTimeInterval(2)
            while process.isRunning && Date() < finalDeadline { Thread.sleep(forTimeInterval: 0.05) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            DispatchQueue.main.async { sender.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    private func isWorkspaceURL(_ url: URL) -> Bool {
        guard let config = configuration else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == config.port
    }

    private func isWorkspaceBlob(_ url: URL) -> Bool {
        guard url.scheme == "blob", let embedded = URL(string: String(url.absoluteString.dropFirst(5))) else { return false }
        return isWorkspaceURL(embedded)
    }

    private func openExternal(_ url: URL) {
        if ["https", "http", "mailto"].contains(url.scheme?.lowercased() ?? "") { NSWorkspace.shared.open(url) }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if (isWorkspaceURL(url) || isWorkspaceBlob(url)) && navigationAction.shouldPerformDownload {
            decisionHandler(.download)
        } else if isWorkspaceURL(url) {
            if navigationAction.targetFrame == nil {
                decisionHandler(.cancel)
                webView.load(navigationAction.request)
            } else {
                decisionHandler(.allow)
            }
        } else {
            decisionHandler(.cancel)
            if navigationAction.navigationType == .linkActivated { openExternal(url) }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let url = navigationResponse.response.url,
              isWorkspaceURL(url) || isWorkspaceBlob(url) else { decisionHandler(.cancel); return }
        let disposition = (navigationResponse.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if !navigationResponse.canShowMIMEType || disposition.lowercased().hasPrefix("attachment") {
            decisionHandler(.download)
        } else { decisionHandler(.allow) }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        if isWorkspaceURL(url) { webView.load(navigationAction.request) }
        else { openExternal(url) }
        return nil
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        guard let url = frame.request.url, isWorkspaceURL(url), let window = webView.window else {
            completionHandler(nil); return
        }
        let panel = NSOpenPanel()
        panel.title = "选择论文 PDF"
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.pdf]
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled { return }
        hasLoadedPage = false
        showConnectionFailure("工作台页面未能加载，请重新连接本地服务。")
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled { return }
        hasLoadedPage = false
        showConnectionFailure("工作台页面加载中断，请重新连接。")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        hasLoadedPage = false
        connect(nil)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        register(download)
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        register(download)
    }

    private func register(_ download: WKDownload) {
        downloads[ObjectIdentifier(download)] = DownloadRecord(download)
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        guard !quitting else { completionHandler(nil); return }
        let filename = URL(fileURLWithPath: suggestedFilename).lastPathComponent
        destinationRequests.append(DownloadDestinationRequest(download: download,
            filename: filename.isEmpty ? "Agent 笔记.html" : filename, completion: completionHandler))
        presentNextSavePanel()
    }

    private func presentNextSavePanel() {
        guard !savePanelActive, !destinationRequests.isEmpty, !quitting else { return }
        savePanelActive = true
        let request = destinationRequests.removeFirst()
        let panel = NSSavePanel()
        activeSavePanel = panel
        panel.title = "保存笔记"
        panel.nameFieldStringValue = request.filename
        panel.canCreateDirectories = true
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        showWindow(nil)
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self = self else { request.completion(nil); return }
            self.activeSavePanel = nil
            self.savePanelActive = false
            if response == .OK, let destination = panel.url, !self.quitting,
               let record = self.downloads[ObjectIdentifier(request.download)] {
                do {
                    let folder = destination.deletingLastPathComponent().appendingPathComponent(".agent-download-\(UUID().uuidString)", isDirectory: true)
                    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false,
                                                            attributes: [.posixPermissions: 0o700])
                    record.destination = destination
                    record.temporaryDirectory = folder
                    record.temporaryFile = folder.appendingPathComponent(destination.lastPathComponent)
                    request.completion(record.temporaryFile)
                } catch {
                    request.completion(nil)
                    self.showDownloadError("无法保存到所选位置：\(error.localizedDescription)")
                }
            } else {
                request.completion(nil)
                self.downloads.removeValue(forKey: ObjectIdentifier(request.download))
            }
            self.presentNextSavePanel()
        }
    }

    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse,
                  newRequest request: URLRequest, decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        decisionHandler(request.url.map(isWorkspaceURL) == true ? .allow : .cancel)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let record = downloads.removeValue(forKey: ObjectIdentifier(download)),
              let source = record.temporaryFile, let destination = record.destination else { return }
        do {
            if FileManager.default.fileExists(atPath: destination.path) {
                _ = try FileManager.default.replaceItemAt(destination, withItemAt: source)
            } else {
                try FileManager.default.moveItem(at: source, to: destination)
            }
            if let folder = record.temporaryDirectory { try? FileManager.default.removeItem(at: folder) }
            statusItem.button?.toolTip = "Agent 工作台 · 笔记已保存"
        } catch {
            // Keep the completed bytes for recovery if the final move fails.
            showDownloadError("笔记下载完成，但无法移动到所选位置。文件保留在：\(source.path)\n\(error.localizedDescription)")
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let record = downloads.removeValue(forKey: ObjectIdentifier(download)), let folder = record.temporaryDirectory {
            try? FileManager.default.removeItem(at: folder)
        }
        let nsError = error as NSError
        if quitting || (nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled) { return }
        showDownloadError("笔记未能保存，请重新下载。\n\(error.localizedDescription)")
    }

    private func showDownloadError(_ message: String) {
        guard !quitting else { return }
        let alert = NSAlert()
        alert.messageText = "保存笔记遇到问题"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "知道了")
        showWindow(nil)
        alert.beginSheetModal(for: window)
    }

}

private func makeAppIcon() -> NSImage {
        NSImage(size: NSSize(width: 512, height: 512), flipped: false) { rect in
            NSColor(calibratedRed: 0.14, green: 0.34, blue: 0.27, alpha: 1).setFill()
            NSBezierPath(roundedRect: rect.insetBy(dx: 32, dy: 32), xRadius: 100, yRadius: 100).fill()
            NSColor(calibratedRed: 0.91, green: 0.95, blue: 0.86, alpha: 1).setFill()
            let heights: [CGFloat] = [128, 222, 176, 112]
            for index in 0..<heights.count {
                let height = heights[index]
                NSBezierPath(roundedRect: NSRect(x: 138 + CGFloat(index) * 62, y: (512 - height) / 2,
                                                width: 34, height: height), xRadius: 17, yRadius: 17).fill()
            }
            return true
        }
    }
@main
private struct DesktopMain {
@MainActor static func main() throws {
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--write-icon" {
    let directory = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let icon = makeAppIcon()
    for size in [16, 32, 128, 256, 512] {
        for scale in [1, 2] {
            let pixels = size * scale
            let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                                          bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                          isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
            icon.draw(in: NSRect(x: 0, y: 0, width: pixels, height: pixels))
            NSGraphicsContext.restoreGraphicsState()
            let suffix = scale == 2 ? "@2x" : ""
            try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
        }
    }
    exit(0)
}
let application = NSApplication.shared
let applicationDelegate = AgentDesktop()
application.delegate = applicationDelegate
application.run()
}
}

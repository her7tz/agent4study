import Cocoa
import WebKit
import Security

// The workspace bridge is registered only on the local workbench web view.
// Account pages get no script bridge, API token, password reader, or cookie export API.
final class AccountBrowserManager: NSObject, WKScriptMessageHandler {
    let base: URL
    let dataRoot: URL
    var browsers: [String: AccountBrowser] = [:]
    init(base: URL, dataRoot: URL) { self.base = base; self.dataRoot = dataRoot }

    func request(_ path: String, method: String = "GET", body: Data? = nil,
                 headers: [String: String] = [:], completion: @escaping (Result<[String: Any], Error>) -> Void) {
        URLSession.shared.dataTask(with: base.appendingPathComponent("api/status")) { data, _, error in
            guard error == nil, let data = data, let status = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let token = status["csrf_token"] as? String else {
                DispatchQueue.main.async { completion(.failure(NSError(domain: "Agent", code: 1, userInfo: [NSLocalizedDescriptionKey: "本地服务未连接"]))) }; return
            }
            var request = URLRequest(url: self.base.appendingPathComponent(path))
            request.httpMethod = method; request.httpBody = body; request.timeoutInterval = 60
            request.setValue(token, forHTTPHeaderField: "X-Agent-Token")
            request.setValue(self.base.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")), forHTTPHeaderField: "Origin")
            for (key, value) in headers { request.setValue(value, forHTTPHeaderField: key) }
            URLSession.shared.dataTask(with: request) { data, response, error in
                let value = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
                let code = (response as? HTTPURLResponse)?.statusCode ?? 500
                DispatchQueue.main.async {
                    if let error = error { completion(.failure(error)) }
                    else if !(200...299).contains(code) { completion(.failure(NSError(domain: "Agent", code: code, userInfo: [NSLocalizedDescriptionKey: value["error"] as? String ?? "本地操作未完成"]))) }
                    else { completion(.success(value)) }
                }
            }.resume()
        }.resume()
    }

    func state(_ id: String, _ value: String) {
        let body = try? JSONSerialization.data(withJSONObject: ["status": value])
        request("api/site-sessions/\(id)/state", method: "POST", body: body, headers: ["Content-Type": "application/json"]) { _ in }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, let origin = message.frameInfo.request.url,
              origin.scheme == base.scheme, origin.host == base.host, origin.port == base.port,
              let payload = message.body as? [String: String], let id = payload["session_id"],
              UUID(uuidString: Self.uuidString(id)) != nil else { return }
        if payload["action"] == "clear_account" { clear(id); return }
        guard payload["action"] == "open_account" else { return }
        request("api/site-sessions/\(id)") { result in
            switch result {
            case .failure(let error): self.alert(error.localizedDescription)
            case .success(let value):
                guard let session = value["session"] as? [String: Any], let address = session["url"] as? String,
                      let url = URL(string: address), AccountBrowser.allowed(url),
                      !["cleared", "clear_pending"].contains(session["status"] as? String ?? "") else {
                    self.alert("会话已清除或网址不可访问，请重新发起登录。"); return
                }
                if let existing = self.browsers[id] { existing.show(); return }
                let browser = AccountBrowser(manager: self, id: id, url: url,
                    automatic: session["automatic"] as? Bool ?? false)
                self.browsers[id] = browser; browser.show()
            }
        }
    }

    static func uuidString(_ id: String) -> String {
        guard id.count == 32, id.allSatisfy({ $0.isHexDigit }) else { return "" }
        let chars = Array(id)
        return [String(chars[0..<8]), String(chars[8..<12]), String(chars[12..<16]), String(chars[16..<20]), String(chars[20..<32])].joined(separator: "-")
    }

    static func store(_ id: String) -> WKWebsiteDataStore {
        if #available(macOS 14.0, *), let uuid = UUID(uuidString: uuidString(id)) { return WKWebsiteDataStore(forIdentifier: uuid) }
        return .nonPersistent()
    }

    func clear(_ id: String) {
        state(id, "clear_pending")
        let browser = browsers.removeValue(forKey: id); browser?.clearing = true
        browser?.web.stopLoading()
        if let browser = browser { for popup in browser.popups { popup.close() }; browser.popups.removeAll(); browser.window.orderOut(nil) }
        let store = browser?.web.configuration.websiteDataStore ?? Self.store(id)
        store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast) {
            DispatchQueue.main.async { browser?.window.close(); self.state(id, "cleared") }
        }
        AccountBrowser.clearCookies(id)
    }

    func shutdown() {
        for browser in browsers.values { browser.saveSession(); browser.window.orderOut(nil) }
    }

    func alert(_ message: String) {
        let alert = NSAlert(); alert.messageText = "账号与论文材料"; alert.informativeText = message
        alert.addButton(withTitle: "知道了"); alert.runModal()
    }
}

final class AccountBrowser: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, NSWindowDelegate {
    weak var manager: AccountBrowserManager?
    let id: String
    let automatic: Bool
    var web: WKWebView!
    var window: NSWindow!
    var status = NSTextField(wrappingLabelWithString: "请自行完成登录、验证码或学校认证。")
    var address = NSTextField(labelWithString: "")
    var pdfURL: URL?
    var requestedDownload = false
    var clearing = false
    var downloadFiles: [ObjectIdentifier: URL] = [:]
    var popups: [NSWindow] = []

    init(manager: AccountBrowserManager, id: String, url: URL, automatic: Bool) {
        self.manager = manager; self.id = id; self.automatic = automatic
        super.init()
        let configuration = WKWebViewConfiguration(); configuration.websiteDataStore = AccountBrowserManager.store(id)
        web = WKWebView(frame: .zero, configuration: configuration); web.navigationDelegate = self; web.uiDelegate = self
        web.translatesAutoresizingMaskIntoConstraints = false
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1080, height: 780), styleMask: [.titled,.closable,.resizable,.miniaturizable], backing: .buffered, defer: false)
        window.title = "论文网站登录 · \(url.host ?? "")"; window.isReleasedWhenClosed = false; window.delegate = self; window.center()
        let back = NSButton(title: "返回", target: self, action: #selector(backPage))
        let save = NSButton(title: "完成登录，保存会话", target: self, action: #selector(finishLogin))
        let acquire = NSButton(title: automatic ? "读取页面提供的 PDF" : "导入当前 PDF", target: self, action: #selector(acquirePDF))
        let clear = NSButton(title: "退出并清除会话", target: self, action: #selector(clearSession))
        let toolbar = NSStackView(views: [back,save,acquire,clear]); toolbar.spacing = 12
        let root = NSStackView(views: [toolbar,address,status,web]); root.orientation = .vertical; root.alignment = .leading; root.spacing = 8; root.edgeInsets = NSEdgeInsets(top: 12,left: 16,bottom: 12,right: 16)
        root.translatesAutoresizingMaskIntoConstraints = false; window.contentView = NSView(); window.contentView!.addSubview(root)
        NSLayoutConstraint.activate([root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),root.topAnchor.constraint(equalTo: window.contentView!.topAnchor),root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),web.widthAnchor.constraint(equalTo: root.widthAnchor,constant: -32),web.heightAnchor.constraint(greaterThanOrEqualToConstant: 500),status.widthAnchor.constraint(equalTo: web.widthAnchor)])
        status.stringValue = automatic ? "完成认证后，可读取页面提供的 PDF。密码和会话信息不会发送给模型。" : "此站点采用人工下载。请登录后打开论文 PDF，再点击“导入当前 PDF”，或在网站点击下载。"
        restoreCookies { self.web.load(URLRequest(url: url)) }
    }

    static func allowed(_ url: URL) -> Bool {
        guard ["http","https"].contains(url.scheme?.lowercased() ?? ""), let host = url.host?.lowercased(), url.user == nil, url.password == nil else { return false }
        if host.contains(":") && (host.hasPrefix("::") || host.hasPrefix("0:")) { return false }
        if !host.contains(".") && !host.contains(":") { return false }
        if host == "localhost" || host == "::1" || host == "0.0.0.0" || host.hasSuffix(".local") || host.hasPrefix("127.") || host.hasPrefix("10.") || host.hasPrefix("192.168.") || host.hasPrefix("169.254.") || host.hasPrefix("fc") && host.contains(":") || host.hasPrefix("fd") && host.contains(":") || host.hasPrefix("fe80:") { return false }
        let parts = host.split(separator: "."); if parts.count == 4, parts[0] == "172", let second = Int(parts[1]), (16...31).contains(second) { return false }
        return url.port == nil || url.port == 80 || url.port == 443
    }

    func show() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func backPage() { if web.canGoBack { web.goBack() } }
    @objc func finishLogin() {
        saveSession { saved in
            if saved { self.manager?.state(self.id,"session_saved"); self.status.stringValue = "会话已保存在本机。全文权限由网站判断；可继续下载或关闭窗口返回对话。" }
            else { self.status.stringValue = "会话未能持久保存；当前窗口仍可继续使用，关闭后可能需要重新登录。" }
        }
    }
    @objc func clearSession() { manager?.clear(id) }
    @objc func acquirePDF() {
        if let url = pdfURL { requestedDownload = true; web.load(URLRequest(url: url)); return }
        guard automatic else { status.stringValue = "请先在网站中打开或下载论文 PDF，再导入。也可将下载的 PDF 附加到对话中的原任务。"; return }
        let script = "(() => { const m=document.querySelector('meta[name=\"citation_pdf_url\"]'); if(m&&m.content)return new URL(m.content,location.href).href; const a=[...document.querySelectorAll('a[href]')].find(a=>/\\.pdf(?:[?#]|$)|\\/pdf[/?]/i.test(a.href)); return a?a.href:null; })()"
        web.evaluateJavaScript(script) { value, _ in
            guard let address = value as? String, let url = URL(string: address), Self.allowed(url) else { self.status.stringValue = "没有找到可读取的 PDF 链接，请在网站手动打开 PDF 或上传原文。"; return }
            self.requestedDownload = true; self.web.load(URLRequest(url: url))
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy)->Void) {
        guard let url = navigationAction.request.url, Self.allowed(url) || url.absoluteString == "about:blank" else { decisionHandler(.cancel); return }
        if navigationAction.shouldPerformDownload { requestedDownload = true; decisionHandler(.download) } else { decisionHandler(.allow) }
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy)->Void) {
        guard let url = navigationResponse.response.url, Self.allowed(url) else { decisionHandler(.cancel); return }
        if let response = navigationResponse.response as? HTTPURLResponse, [401,403].contains(response.statusCode) { manager?.state(id,"needs_login"); status.stringValue = "网站要求登录、验证或额外访问权限，请在窗口中处理。" }
        if navigationResponse.response.mimeType == "application/pdf" {
            pdfURL = url
            if requestedDownload { decisionHandler(.download); return }
            status.stringValue = "当前已打开 PDF。点击“导入当前 PDF”即可继续总结。"
        }
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .cancel)
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { address.stringValue = "当前站点：" + (webView.url?.host ?? ""); saveSession() }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { status.stringValue = "网页未能加载，请重试或手动上传 PDF。" }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures)->WKWebView? {
        guard let url = navigationAction.request.url, Self.allowed(url) || url.absoluteString == "about:blank" else { return nil }
        let child = WKWebView(frame: .zero, configuration: configuration); child.navigationDelegate = self; child.uiDelegate = self
        let popup = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 760,height: 680),styleMask: [.titled,.closable,.resizable],backing: .buffered,defer: false)
        popup.title = "网站认证"; popup.isReleasedWhenClosed = false; popup.contentView = child; popups.append(popup); popup.makeKeyAndOrderFront(nil); return child
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping(URL?)->Void) {
        guard response.mimeType == "application/pdf", let root = manager?.dataRoot else { status.stringValue = "未取得 PDF，下载可能是登录或验证页面。"; manager?.state(id,"access_failed"); completionHandler(nil); return }
        let directory = root.appendingPathComponent("account-downloads", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: directory,withIntermediateDirectories: true,attributes: [.posixPermissions:0o700])
            let file = directory.appendingPathComponent(UUID().uuidString + ".pdf"); downloadFiles[ObjectIdentifier(download)] = file; status.stringValue = "正在下载 PDF，完成后自动导入原任务…"; completionHandler(file)
        } catch { status.stringValue = "无法保存下载，请手动上传 PDF。"; completionHandler(nil) }
    }
    func downloadDidFinish(_ download: WKDownload) {
        requestedDownload = false
        guard let file = downloadFiles.removeValue(forKey: ObjectIdentifier(download)) else { return }
        guard let manager = manager, !clearing else { try? FileManager.default.removeItem(at: file); return }
        do {
            defer { try? FileManager.default.removeItem(at: file) }
            let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
            guard let size = attributes[.size] as? NSNumber, size.intValue <= 20*1024*1024 else { status.stringValue = "PDF 超过 20 MB，请提供较小材料。"; return }
            try FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:file.path)
            let data = try Data(contentsOf: file)
            manager.request("api/site-sessions/\(id)/import",method:"POST",body:data,headers:["Content-Type":"application/pdf","X-File-Name":"paper.pdf"]) { result in
                switch result {
                case .success: self.status.stringValue = "PDF 已导入，原任务将继续总结。可以关闭此窗口返回对话。"; self.saveSession()
                case .failure(let error): self.status.stringValue = error.localizedDescription; manager.state(self.id,"access_failed")
                }
            }
        } catch { status.stringValue = "PDF 导入失败，请在对话中手动上传。"; manager.state(id,"access_failed") }
    }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        requestedDownload = false
        if let file = downloadFiles.removeValue(forKey: ObjectIdentifier(download)) { try? FileManager.default.removeItem(at: file) }
        if !clearing { status.stringValue = "下载未完成，请确认权限并重试。"; manager?.state(id,"access_failed") }
    }
    func windowWillClose(_ notification: Notification) { if !clearing { saveSession() }; for popup in popups { popup.close() }; popups.removeAll() }

    private static func key(_ id: String) -> [String: Any] { return [kSecClass as String:kSecClassGenericPassword,kSecAttrService as String:"local.agent.workbench.site-session",kSecAttrAccount as String:id] }
    static func clearCookies(_ id: String) { SecItemDelete(key(id) as CFDictionary) }
    func saveSession(_ completion: ((Bool)->Void)? = nil) {
        guard !clearing else { completion?(false); return }
        web.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
            guard !self.clearing else { return }
            let values = cookies.map { cookie -> [String:Any] in
                var value: [String:Any] = ["name":cookie.name,"value":cookie.value,"domain":cookie.domain,"path":cookie.path,"secure":cookie.isSecure]
                if let expires = cookie.expiresDate { value["expires"] = expires.timeIntervalSince1970 }; return value
            }
            guard let data = try? JSONSerialization.data(withJSONObject: values) else { return }
            var query = Self.key(self.id); query[kSecValueData as String] = data
            let status = SecItemUpdate(Self.key(self.id) as CFDictionary,[kSecValueData as String:data] as CFDictionary)
            var saved = status == errSecSuccess
            if status == errSecItemNotFound { query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly; saved = SecItemAdd(query as CFDictionary,nil) == errSecSuccess }
            if #available(macOS 14.0, *) { saved = true } // The dedicated website store persists independently.
            completion?(saved)
        }
    }
    func restoreCookies(_ completion: @escaping()->Void) {
        var query = Self.key(id); query[kSecReturnData as String] = true; query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary,&result) == errSecSuccess, let data = result as? Data,
              let values = try? JSONSerialization.jsonObject(with: data) as? [[String:Any]] else { completion(); return }
        let group = DispatchGroup()
        for value in values {
            guard let name=value["name"] as? String, let text=value["value"] as? String,let domain=value["domain"] as? String,let path=value["path"] as? String else { continue }
            var properties: [HTTPCookiePropertyKey:Any] = [.name:name,.value:text,.domain:domain,.path:path,.secure:(value["secure"] as? Bool ?? false) ? "TRUE":"FALSE"]
            if let expires=value["expires"] as? Double { guard expires>Date().timeIntervalSince1970 else { continue }; properties[.expires]=Date(timeIntervalSince1970:expires) }
            if let cookie=HTTPCookie(properties:properties) { group.enter(); web.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) { group.leave() } }
        }
        group.notify(queue:.main,execute:completion)
    }
}

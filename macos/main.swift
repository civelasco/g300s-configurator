// G300s Configurator - native Mac window around the configurator page.
//
// Starts g300_gui.py (installed by macos/build.sh, see resourceDir()) on a free local port and
// shows it in its own window, so the app opens from the Dock / Launchpad like any other app.

import Cocoa
import WebKit
import UniformTypeIdentifiers
import IOKit.hid

let python = "/usr/bin/python3"

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var serverURL: URL?
    var output = ""
    var quitting = false

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "saveFile")
        config.userContentController.add(self, name: "app")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // no white flash in dark mode

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 980, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "G300s Configurator"
        window.minSize = NSSize(width: 420, height: 480)
        window.contentView = webView
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("MainWindow")
        window.makeKeyAndOrderFront(nil)

        webView.loadHTMLString(message("Starting…", detail: nil), baseURL: nil)
        requestMouseAccess()
        startServer()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting || serverURL == nil { return .terminateNow }
        // Ask the page whether there are unsaved changes.
        webView.evaluateJavaScript("(typeof edits !== 'undefined' && edits && dirty().length) ? 1 : 0") { result, _ in
            var ok = true
            if (result as? Int) == 1 {
                let alert = NSAlert()
                alert.messageText = "You have changes that aren't saved to the mouse."
                alert.informativeText = "If you quit now, they'll be lost."
                alert.addButton(withTitle: "Cancel")
                alert.addButton(withTitle: "Quit Anyway")
                ok = alert.runModal() == .alertSecondButtonReturn
            }
            self.quitting = ok
            NSApp.reply(toApplicationShouldTerminate: ok)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        server?.terminate()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        NSApp.terminate(nil)  // runs the unsaved-changes check
        return false
    }

    // MARK: permission

    // Talking to the mouse needs Input Monitoring. Asking here shows macOS's standard prompt
    // the first time, and puts this app in the Settings list so it can be switched on.
    func requestMouseAccess() {
        let access = IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)
        let name = access == kIOHIDAccessTypeGranted ? "granted" : access == kIOHIDAccessTypeDenied ? "denied" : "not decided"
        NSLog("G300s: Input Monitoring access is %@", name)
        if access != kIOHIDAccessTypeGranted {
            _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
        }
    }

    func openInputMonitoringSettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")!)
    }

    // Permission changes only apply to a fresh process, so restart ourselves.
    func relaunch() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "sleep 1; /usr/bin/open \"$0\"", Bundle.main.bundlePath]
        try? p.run()
        quitting = true
        NSApp.terminate(nil)
    }

    // MARK: python server

    // The Python/HTML files live in Application Support (installed by build_app.sh) rather than
    // inside the app, so updating them doesn't change the app's signature and macOS keeps the
    // Input Monitoring permission. Falls back to copies in the bundle if present.
    func resourceDir() -> URL? {
        let support = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/G300s Configurator")
        if FileManager.default.fileExists(atPath: support.appendingPathComponent("g300_gui.py").path) { return support }
        return Bundle.main.resourceURL
    }

    func startServer() {
        guard let res = resourceDir() else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-u", res.appendingPathComponent("g300_gui.py").path,
                       "--port", "0", "--no-browser", "--parent-pid", String(getpid())]
        p.currentDirectoryURL = res
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            DispatchQueue.main.async { self?.gotOutput(String(decoding: data, as: UTF8.self)) }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.quitting else { return }
                self.webView.loadHTMLString(self.message("The configurator stopped unexpectedly.", detail: self.output), baseURL: nil)
            }
        }
        do {
            try p.run()
            server = p
        } catch {
            webView.loadHTMLString(message("Couldn't start Python (\(python)).",
                                           detail: "Install Apple's command line tools with:  xcode-select --install\n\n\(error)"),
                                   baseURL: nil)
        }
    }

    func gotOutput(_ text: String) {
        output += text
        guard serverURL == nil,
              let r = output.range(of: #"http://127\.0\.0\.1:\d+/"#, options: .regularExpression),
              let url = URL(string: String(output[r])) else { return }
        serverURL = url
        webView.load(URLRequest(url: url))
    }

    // MARK: web view

    // Only our local page loads inside the window; anything else opens in the browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, let host = url.host, host != "127.0.0.1", url.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // <input type="file"> for "Load a backup file…"
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.json]
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    // "Save a backup file…" posts the JSON here.
    func userContentController(_ controller: WKUserContentController, didReceive msg: WKScriptMessage) {
        if msg.name == "app" {
            switch msg.body as? String {
            case "openSettings": openInputMonitoringSettings()
            case "relaunch": relaunch()
            default: break
            }
            return
        }
        guard msg.name == "saveFile", let body = msg.body as? [String: Any],
              let name = body["name"] as? String, let text = body["text"] as? String else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = name
        panel.allowedContentTypes = [.json]
        panel.beginSheetModal(for: window) { r in
            guard r == .OK, let url = panel.url else { return }
            do {
                try text.write(to: url, atomically: true, encoding: .utf8)
                self.webView.evaluateJavaScript("toast('Backup saved.')")
            } catch {
                NSAlert(error: error).beginSheetModal(for: self.window)
            }
        }
    }

    func message(_ title: String, detail: String?) -> String {
        let esc = { (s: String) in s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;") }
        return """
        <meta name="color-scheme" content="light dark">
        <body style="font:15px -apple-system;display:grid;place-items:center;height:90vh;margin:0;color:#888">
        <div style="max-width:560px;text-align:center"><p style="font-size:17px">\(esc(title))</p>
        \(detail.map { "<pre style='text-align:left;white-space:pre-wrap;font:12px Menlo'>\(esc($0))</pre>" } ?? "")</div></body>
        """
    }

    // MARK: menu

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About G300s Configurator", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide G300s Configurator", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit G300s Configurator", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    @objc func reload() {
        if let url = serverURL { webView.load(URLRequest(url: url)) }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

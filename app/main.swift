// Local Models Hub, the menu-bar app: a client of the gateway's HTTP endpoints, nothing more. It reads
// GET /hub/snapshot, shows the state as a template icon and a menu (app/Glance.swift), switches this Mac's and each
// peer's local models off and on through the endpoints the dashboard uses, and notifies an instance left unloaded after
// a crash and a peer unreachable for more than a minute. Built on this Mac by `localmodels app install`.
//
//   --dump <snapshot.json>      print the menu a snapshot gives (no window, no network)
//   --render-icons <folder>     write each icon state as PNG, on light and dark menu-bar grounds
//   --unregister-login          remove the login item (localmodels app uninstall)
import AppKit
import ServiceManagement
import UserNotifications

let info = Bundle.main.infoDictionary ?? [:]
let hubURL = URL(string: (info["LMHubURL"] as? String) ?? "http://localhost:1235")!
let cliPath = (info["LMHubCLI"] as? String) ?? "/opt/homebrew/bin/localmodels"

// ---------------------------------------------------------------- the icon: the chip mark as a template image
func makeIcon(_ icon: Icon, attention: Bool) -> NSImage {
    let img = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
        NSColor.black.setStroke(); NSColor.black.setFill()
        let c = CGPoint(x: 9, y: 9)
        let pins = NSBezierPath(); pins.lineWidth = 1.25; pins.lineCapStyle = .round
        for o in [-2.55, 0, 2.55] {                       // three pins a side, as on the mark
            for (dx, dy) in [(0.0, 1.0), (0.0, -1.0), (1.0, 0.0), (-1.0, 0.0)] {
                let a = CGPoint(x: c.x + (dx == 0 ? o : dx * 5.35), y: c.y + (dy == 0 ? o : dy * 5.35))
                let b = CGPoint(x: c.x + (dx == 0 ? o : dx * 6.95), y: c.y + (dy == 0 ? o : dy * 6.95))
                pins.move(to: a); pins.line(to: b)
            }
        }
        pins.stroke()
        if icon == .serving {                             // serving: the die filled
            NSBezierPath(ovalIn: NSRect(x: c.x - 4.4, y: c.y - 4.4, width: 8.8, height: 8.8)).fill()
        } else {                                          // off, down or nothing loaded: the die outlined
            let ring = NSBezierPath(ovalIn: NSRect(x: c.x - 3.8, y: c.y - 3.8, width: 7.6, height: 7.6)); ring.lineWidth = 1.2; ring.stroke()
        }
        if attention {                                    // needs attention: a dot at the corner, cut clear of the pins
            let ctx = NSGraphicsContext.current
            ctx?.compositingOperation = .clear
            NSBezierPath(ovalIn: NSRect(x: 15.4 - 3.1, y: 15.4 - 3.1, width: 6.2, height: 6.2)).fill()
            ctx?.compositingOperation = .sourceOver
            NSBezierPath(ovalIn: NSRect(x: 15.4 - 2.1, y: 15.4 - 2.1, width: 4.2, height: 4.2)).fill()
        }
        return true
    }
    img.isTemplate = true
    return img
}

// ---------------------------------------------------------------- talking to the gateway
func request(_ path: String, body: [String: Any]? = nil, timeout: Double = 5, done: @escaping (Int, [String: Any]?) -> Void) {
    var r = URLRequest(url: hubURL.appendingPathComponent(path), timeoutInterval: timeout)
    r.cachePolicy = .reloadIgnoringLocalCacheData
    if let body = body {
        r.httpMethod = "POST"
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try? JSONSerialization.data(withJSONObject: body)
    }
    URLSession.shared.dataTask(with: r) { data, resp, _ in
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        DispatchQueue.main.async { done(code, json) }
    }.resume()
}

@discardableResult
func ask(_ title: String, _ text: String, ok: String, cancel: String? = "Cancel") -> Bool {
    NSApp.activate(ignoringOtherApps: true)
    let a = NSAlert(); a.messageText = title; a.informativeText = text
    a.addButton(withTitle: ok); if let cancel = cancel { a.addButton(withTitle: cancel) }
    return a.runModal() == .alertFirstButtonReturn
}

// ---------------------------------------------------------------- the app
final class Hub: NSObject, NSApplicationDelegate, NSMenuDelegate, UNUserNotificationCenterDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    let menu = NSMenu()
    var last = glanceDown()
    var seen = Set<String>(), baseline = true, failures = 0, menuOpen = false, polling = false

    func applicationDidFinishLaunching(_ n: Notification) {
        menu.delegate = self; menu.autoenablesItems = false
        item.menu = menu
        UNUserNotificationCenter.current().delegate = self
        show(last); poll()
    }

    func poll() {
        if polling { return }
        polling = true
        request("hub/snapshot") { code, json in
            self.polling = false
            if code == 200, let json = json {
                self.failures = 0
                let g = glance(json)
                self.notify(fresh(g.events, seen: &self.seen, baseline: self.baseline)); self.baseline = false
                self.show(g)
            } else { self.failures += 1; self.show(glanceDown(port: hubURL.port ?? 1235)) }
            let wait = self.menuOpen ? 2.0 : self.failures > 0 ? min(30.0, 5.0 * pow(2.0, Double(self.failures - 1))) : 5.0
            DispatchQueue.main.asyncAfter(deadline: .now() + wait) { self.poll() }
        }
    }

    func show(_ g: Glance) {
        last = g
        item.button?.image = makeIcon(g.icon, attention: g.attention)
        item.button?.setAccessibilityLabel(g.accessibilityLabel)
        item.button?.toolTip = g.accessibilityLabel
        menu.removeAllItems()
        for l in g.lines {
            if l.separatorBefore { menu.addItem(.separator()) }
            let m = NSMenuItem(title: l.title, action: l.action == .none ? nil : #selector(act(_:)), keyEquivalent: "")
            m.target = self; m.isEnabled = l.action != .none; m.representedObject = Box(l.action)
            if l.action != .none && l.action != .openDashboard && l.action != .startGateway { m.indentationLevel = 1 }
            menu.addItem(m)
        }
        menu.addItem(.separator())
        let dash = NSMenuItem(title: "Open dashboard", action: #selector(openDashboard), keyEquivalent: "d"); dash.target = self
        let login = NSMenuItem(title: "Launch at login", action: #selector(toggleLogin), keyEquivalent: ""); login.target = self
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(dash); menu.addItem(login); menu.addItem(.separator())
        let ver = NSMenuItem(title: "Local Models Hub" + (g.version.isEmpty ? "" : " \(g.version)") + " · Haedo Labs", action: nil, keyEquivalent: ""); ver.isEnabled = false
        menu.addItem(ver)
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }

    func menuWillOpen(_ m: NSMenu) { menuOpen = true; poll() }
    func menuDidClose(_ m: NSMenu) { menuOpen = false }

    @objc func openDashboard() { NSWorkspace.shared.open(hubURL) }

    @objc func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() } else { try SMAppService.mainApp.register() }
        } catch { ask("Launch at login not changed", error.localizedDescription, ok: "OK", cancel: nil) }
        show(last)
    }

    @objc func act(_ sender: NSMenuItem) {
        guard let action = (sender.representedObject as? Box)?.action else { return }
        switch action {
        case .none: break
        case .openDashboard: openDashboard()
        case .startGateway:
            let p = Process(); p.executableURL = URL(fileURLWithPath: cliPath); p.arguments = ["start"]
            p.terminationHandler = { _ in DispatchQueue.main.async { self.poll() } }
            do { try p.run() } catch { ask("Gateway not started", "\(cliPath) start: \(error.localizedDescription)", ok: "OK", cancel: nil) }
        case .local(let on):
            if !on && !ask("Switch this Mac's local models off?",
                           "Every instance is unloaded once the requests in flight finish; until turned on, requests here get 503 and nothing is loaded.",
                           ok: "Switch off") { return }
            switchModels(on ? "hub/on" : "hub/off", [:], who: "this Mac's local models", on: on)
        case .peer(let name, let on):
            if !on && !ask("Switch peer \(name)'s local models off?",
                           "It unloads every instance once its requests in flight finish; until turned on, this gateway routes nothing there.",
                           ok: "Switch off") { return }
            switchModels(on ? "hub/link/on" : "hub/link/off", ["peer": name], who: "peer \(name)'s local models", on: on)
        }
    }

    func switchModels(_ path: String, _ body: [String: Any], who: String, on: Bool) {
        request(path, body: body, timeout: 20) { code, j in
            if code == 409, let blockers = j?["blockers"] as? [String], !blockers.isEmpty {
                if ask("In use", blockers.joined(separator: "\n") + "\n\nSwitch \(who) off anyway?", ok: "Switch off anyway") {
                    var forced = body; forced["force"] = true
                    request(path, body: forced, timeout: 20) { c2, j2 in
                        if !(200..<300).contains(c2) { self.failed(who, on, c2, j2) }
                        self.poll()
                    }
                }
            } else if !(200..<300).contains(code) { self.failed(who, on, code, j) }
            self.poll()
        }
    }

    func failed(_ who: String, _ on: Bool, _ code: Int, _ j: [String: Any]?) {
        ask("Nothing switched \(on ? "on" : "off")", (j?["error"] as? String) ?? (code == 0 ? "The gateway did not answer." : "HTTP \(code)") + " (\(who))",
            ok: "OK", cancel: nil)
    }

    // ---- notifications: asked for on the first event, never at launch
    func notify(_ events: [Event]) {
        guard !events.isEmpty else { return }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            for e in events {
                let c = UNMutableNotificationContent(); c.title = e.title; c.body = e.body
                center.add(UNNotificationRequest(identifier: e.key, content: c, trigger: nil))
            }
        }
    }
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) { done([.banner, .list]) }
    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) { openDashboard(); done() }
}

final class Box: NSObject { let action: Action; init(_ a: Action) { action = a } }

// ---------------------------------------------------------------- the command line
let args = CommandLine.arguments
if let i = args.firstIndex(of: "--dump"), i + 1 < args.count {
    guard let data = FileManager.default.contents(atPath: args[i + 1]),
          let snap = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { print("not a snapshot: \(args[i + 1])"); exit(2) }
    let g = glance(snap)
    print("icon: \(g.icon == .serving ? "serving" : "idle")\(g.attention ? " + attention" : "") · \(g.accessibilityLabel)")
    for l in g.lines { if l.separatorBefore { print("  ---") }; print("  " + l.title + (l.action == .none ? "" : "   [\(l.action)]")) }
    for e in g.events { print("  event: \(e.title): \(e.body)") }
    exit(0)
}
if let i = args.firstIndex(of: "--render-icons"), i + 1 < args.count {
    let dir = URL(fileURLWithPath: args[i + 1]); try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    for (name, icon, att) in [("serving", Icon.serving, false), ("idle", Icon.idle, false), ("serving-attention", Icon.serving, true), ("idle-attention", Icon.idle, true)] {
        for (ground, bg, fg) in [("light", NSColor(white: 0.93, alpha: 1), NSColor.black), ("dark", NSColor(white: 0.16, alpha: 1), NSColor.white)] {
            let px = 18 * 4, rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4,
                                                    hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            rep.size = NSSize(width: 18, height: 18)
            NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            bg.setFill(); NSRect(x: 0, y: 0, width: 18, height: 18).fill()
            let tinted = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { r in   // as the menu bar tints a template image
                makeIcon(icon, attention: att).draw(in: r); fg.set(); r.fill(using: .sourceIn); return true }
            tinted.draw(in: NSRect(x: 0, y: 0, width: 18, height: 18))
            NSGraphicsContext.restoreGraphicsState()
            try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent("\(name)-\(ground).png"))
        }
    }
    print("icons written to \(dir.path)"); exit(0)
}
if args.contains("--unregister-login") { try? SMAppService.mainApp.unregister(); exit(0) }

let app = NSApplication.shared
let hub = Hub()
app.delegate = hub
app.setActivationPolicy(.accessory)
app.run()

// Local Models Hub, the menu-bar app: a client of the gateway's HTTP endpoints, nothing more. It reads
// GET /hub/snapshot, shows the state as a template icon and a menu (app/Glance.swift), switches this Mac's and each
// peer's local models off and on through the endpoints the dashboard uses, and notifies an instance left unloaded after
// a crash and a peer unreachable for more than a minute. It also draws a section for each extension: a program that
// wrote a manifest in the state folder's extensions/ (GUIDE.md "Extensions") and serves its menu as JSON; the app acts
// for it only on its own origin or with a verb its manifest lists. The menu is kept in place across polls (app/Menu.swift).
// Built on this Mac by `localmodels app install`.
//
//   --dump <snapshot.json> [--extensions <pages.json>]   print the menu a snapshot gives, with extensions
//                               ([{"manifest", "menu", "down_since"}]; no window, no network)
//   --render-icons <folder>     write each icon state as PNG, on light and dark menu-bar grounds
//   --unregister-login          remove the login item (localmodels app uninstall)
import AppKit
import ServiceManagement
import UserNotifications

let info = Bundle.main.infoDictionary ?? [:]
let hubURL = URL(string: (info["LMHubURL"] as? String) ?? "http://localhost:1235")!
let cliPath = (info["LMHubCLI"] as? String) ?? "/opt/homebrew/bin/localmodels"
let stateDir = (info["LMHubState"] as? String) ?? (NSHomeDirectory() + "/Library/Application Support/LocalModels")
let extensionsDir = stateDir + "/extensions"

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

/// One request to an extension (its menu, or one of its POSTs), by full URL.
let extensionSession = URLSession(configuration: .ephemeral, delegate: ExtensionRedirect(), delegateQueue: nil)
func fetch(_ url: String, token: (header: String, value: String)? = nil, body: [String: String]? = nil, timeout: Double = 5,
           done: @escaping (Int, [String: Any]?) -> Void) {
    guard originOf(url) != nil, let u = URL(string: url) else { done(0, nil); return }
    var r = URLRequest(url: u, timeoutInterval: timeout)
    r.cachePolicy = .reloadIgnoringLocalCacheData
    if let body = body {
        r.httpMethod = "POST"
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let t = token { r.setValue(t.value, forHTTPHeaderField: t.header) }
        r.httpBody = try? JSONSerialization.data(withJSONObject: body)
    }
    extensionSession.dataTask(with: r) { data, resp, _ in
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        DispatchQueue.main.async { done(code, json) }
    }.resume()
}

/// The extensions this Mac has: every sound manifest in the state folder's extensions/, by file name.
func readExtensions() -> [Extension] {
    let names = ((try? FileManager.default.contentsOfDirectory(atPath: extensionsDir)) ?? []).filter { $0.hasSuffix(".json") }.sorted()
    var manifests: [(String, [String: Any])] = [], counts: [String: Int] = [:]
    for name in names {
        guard let data = FileManager.default.contents(atPath: extensionsDir + "/" + name),
              let m = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let id = m["id"] as? String else { continue }
        counts[id, default: 0] += 1
        manifests.append((name, m))
    }
    return manifests.compactMap { name, m in
        guard let e = readManifest(m), name == e.id + ".json", counts[e.id] == 1 else { return nil }
        return e
    }
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
    var seen = Set<String>(), baseline = true, failures = 0, menuOpen = false, polling = false, face = ""
    var exts: [Extension] = [], extDown: [String: Double] = [:], extLast: [String: (menu: [String: Any], at: Double)] = [:]
    let grace = 15.0                                      // an extension's menu missed for less than this: its last one shows
    lazy var keeper = MenuKeeper(target: self, selector: #selector(act(_:)))   // the menu, kept in place (app/Menu.swift)

    func applicationDidFinishLaunching(_ n: Notification) {
        menu.delegate = self; menu.autoenablesItems = false
        item.menu = menu
        UNUserNotificationCenter.current().delegate = self
        show(last); poll()
    }

    func poll() {
        if polling { return }
        polling = true
        exts = readExtensions()
        var pages = exts.map { ExtensionPage(ext: $0, menu: nil) }
        var snap: [String: Any]? = nil
        let group = DispatchGroup()
        group.enter()
        request("hub/snapshot") { code, json in if code == 200 { snap = json }; group.leave() }
        for (i, e) in exts.enumerated() {
            group.enter()
            fetch(e.menu, timeout: 3) { code, json in
                let now = Date().timeIntervalSince1970
                if code == 200, let json = json { pages[i].menu = json; self.extDown[e.id] = nil; self.extLast[e.id] = (json, now) }
                else {
                    let since = self.extDown[e.id] ?? now; self.extDown[e.id] = since
                    if let l = self.extLast[e.id], now - l.at < self.grace { pages[i].menu = l.menu }   // a moment's miss: no flip to "not answering"
                    else { pages[i].downSince = since }
                }
                group.leave()
            }
        }
        group.notify(queue: .main) {
            self.polling = false
            let now = Date().timeIntervalSince1970
            let g: Glance
            if let json = snap { self.failures = 0; g = glance(json, now: now, extensions: pages) }
            else { self.failures += 1; g = glanceDown(port: hubURL.port ?? 1235, extensions: pages, now: now) }
            self.notify(fresh(g.events, seen: &self.seen, baseline: self.baseline)); self.baseline = false
            self.show(g)
            let wait = self.menuOpen ? 2.0 : self.failures > 0 ? min(30.0, 5.0 * pow(2.0, Double(self.failures - 1))) : 5.0
            DispatchQueue.main.asyncAfter(deadline: .now() + wait) { self.poll() }
        }
    }

    func show(_ g: Glance) {
        last = g
        let look = "\(g.icon) \(g.attention)"             // the icon redrawn only when its state changes
        if look != face { item.button?.image = makeIcon(g.icon, attention: g.attention); face = look }
        if item.button?.toolTip != g.accessibilityLabel { item.button?.setAccessibilityLabel(g.accessibilityLabel); item.button?.toolTip = g.accessibilityLabel }
        keeper.sync(menu, specs(g.lines) + [          // every poll (every 2 s while open) changes only what changed
            Spec(),
            Spec(line: Line(title: "Open dashboard", action: .openDashboard, indent: 0), key: "d"),
            Spec(line: Line(title: "Launch at login", action: .toggleLogin, indent: 0), on: SMAppService.mainApp.status == .enabled),
            Spec(),
            .about(version: g.version),
            Spec(line: Line(title: "Quit", action: .quit, indent: 0), key: "q"),
        ])
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
        case .openMaker: NSWorkspace.shared.open(URL(string: "https://haedolabs.com/")!)
        case .toggleLogin: toggleLogin()
        case .quit: NSApp.terminate(nil)
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
        case .extOpen(_, let url):
            if let u = URL(string: url) { NSWorkspace.shared.open(u) }
        case .extPost(let id, let path, let body, let confirm, let input, let reveal):
            guard let e = exts.first(where: { $0.id == id }) else { return }
            if !confirm.isEmpty && !ask(confirm, e.label, ok: "OK") { return }
            var b = body
            if let input = input {
                guard let text = askText(input.prompt, e.label, placeholder: input.placeholder) else { return }
                b[input.field] = text
            }
            extPost(e, path, b, title: sender.title, reveal: reveal)
        case .extRun(let id, let verb, let confirm):
            guard let e = exts.first(where: { $0.id == id }), e.verbs.contains(verb), !e.command.isEmpty else { return }
            if !confirm.isEmpty && !ask(confirm, e.label, ok: "OK") { return }
            extRun(e, verb)
        }
    }

    // ---- extensions: a POST on their own origin with their token, or their command with one of their verbs
    func askText(_ title: String, _ text: String, placeholder: String) -> String? {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert(); a.messageText = title; a.informativeText = text
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24)); field.placeholderString = placeholder
        a.accessoryView = field; a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = field
        guard a.runModal() == .alertFirstButtonReturn else { return nil }
        let t = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }

    func extPost(_ e: Extension, _ path: String, _ body: [String: String], title: String, reveal: Bool) {
        let token = e.token.isEmpty ? nil : (try? String(contentsOfFile: e.token, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        fetch(e.origin + path, token: token.map { (e.tokenHeader, $0) }, body: body, timeout: 120) { code, j in
            let ok = (200..<300).contains(code) && ((j?["ok"] as? Bool) ?? true)
            if ok {
                if reveal, let p = j?["path"] as? String, FileManager.default.fileExists(atPath: p) {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)])
                }
                if let words = j?["message"] as? String, !words.isEmpty {
                    self.notify([Event(key: "ext:\(e.id):done:\(Date().timeIntervalSince1970)", title: e.label, body: words, ext: e.id)])
                }
            } else {
                let problems = (j?["problems"] as? [String]) ?? []
                ask("\(title): not done", problems.isEmpty ? ((j?["error"] as? String) ?? (code == 0 ? "\(e.label) did not answer." : "HTTP \(code)"))
                    : problems.prefix(8).joined(separator: "\n"), ok: "OK", cancel: nil)
            }
            self.poll()
        }
    }

    func extRun(_ e: Extension, _ verb: String) {
        let p = Process(); p.executableURL = URL(fileURLWithPath: e.command[0]); p.arguments = Array(e.command.dropFirst()) + [verb]
        var env = ProcessInfo.processInfo.environment; for (k, v) in e.env { env[k] = v }; p.environment = env
        do {
            try ExtensionOutput().run(p) { status, words in
                DispatchQueue.main.async {
                    if status != 0 { ask("\(e.label): \(verb) did not finish", words.trimmingCharacters(in: .whitespacesAndNewlines), ok: "OK", cancel: nil) }
                    self.poll()
                }
            }
        } catch { ask("\(e.label): \(verb) did not start", "\(e.command[0]): \(error.localizedDescription)", ok: "OK", cancel: nil) }
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
                let c = UNMutableNotificationContent(); c.title = e.title; c.body = e.body; c.userInfo = ["ext": e.ext]
                center.add(UNNotificationRequest(identifier: e.key, content: c, trigger: nil))
            }
        }
    }
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) { done([.banner, .list]) }
    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let id = (r.notification.request.content.userInfo["ext"] as? String) ?? ""
        DispatchQueue.main.async {                        // an extension's event opens its page; the hub's, the dashboard
            if let e = self.exts.first(where: { $0.id == id }), let u = URL(string: e.open.isEmpty ? e.origin : e.open) { NSWorkspace.shared.open(u) }
            else { self.openDashboard() }
        }
        done()
    }
}

// ---------------------------------------------------------------- the command line
let args = CommandLine.arguments
if let i = args.firstIndex(of: "--dump"), i + 1 < args.count {
    guard let data = FileManager.default.contents(atPath: args[i + 1]),
          let snap = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { print("not a snapshot: \(args[i + 1])"); exit(2) }
    var pages: [ExtensionPage] = []
    if let j = args.firstIndex(of: "--extensions"), j + 1 < args.count,
       let d = FileManager.default.contents(atPath: args[j + 1]), let raw = try? JSONSerialization.jsonObject(with: d) as? [[String: Any]] {
        for r in raw {
            guard let m = r["manifest"], let md = try? JSONSerialization.data(withJSONObject: m), let e = readExtension(md) else { print("  (a manifest not sound: skipped)"); continue }
            pages.append(ExtensionPage(ext: e, menu: r["menu"] as? [String: Any], downSince: (r["down_since"] as? NSNumber)?.doubleValue))
        }
    }
    let g = glance(snap, extensions: pages)
    print("icon: \(g.icon == .serving ? "serving" : "idle")\(g.attention ? " + attention" : "") · \(g.accessibilityLabel)")
    func dump(_ l: Line, _ depth: Int) {                  // a submenu one tab in; each line at its drawn indentation
        print(String(repeating: "\t", count: depth) + "  " + String(repeating: "    ", count: drawnIndent(l)) + l.title
              + (l.children.isEmpty ? "" : "  ▸") + (l.action == .none ? "" : "   [\(l.action)]"))
        for k in l.children { if k.separatorBefore { print(String(repeating: "\t", count: depth + 1) + "  ---") }; dump(k, depth + 1) }
    }
    for l in g.lines { if l.separatorBefore { print("  ---") }; dump(l, 0) }
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

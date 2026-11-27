// Glance: the gateway's snapshot (GET /hub/snapshot) read into what the menu-bar app shows, with no AppKit, so a
// plain runner can test it (app/tests). The icon state, the menu's lines and their actions, and the events worth a
// notification all come from here, and so does each extension's section: its manifest read (localmodels/extensions.py,
// GUIDE.md "Extensions") and the menu it serves drawn, with every action held to its own origin and its own verbs.
import Foundation

enum Icon: Equatable { case serving, idle }            // serving: some slot can take a request; idle: off, down or none loaded

enum Action: Equatable {
    case none, openDashboard, openMaker, startGateway, toggleLogin, quit
    case local(on: Bool)                                // POST /hub/on or /hub/off
    case peer(name: String, on: Bool)                   // POST /hub/link/on or /hub/link/off with {"peer": name}
    case extOpen(ext: String, url: String)              // an extension's page, on its own origin, in the browser
    case extPost(ext: String, path: String, body: [String: String], confirm: String, input: Input?, reveal: Bool)
    case extRun(ext: String, verb: String, confirm: String)   // the extension's command, with one of its manifest's verbs
}

/// A line of text an action asks for before it posts (a note, a name), sent as `field` in the body.
struct Input: Equatable {
    var prompt: String
    var field: String
    var placeholder = ""
}

struct Line: Equatable {
    var title: String
    var action: Action = .none
    var separatorBefore = false
    var indent: Int? = nil                              // nil: the hub's own rule (actions indented under their host)
    var children: [Line] = []                           // a submenu
    var tooltip = ""                                    // a title cut to fit: the whole of it
}

/// The indentation a line is drawn at: as it says, else the hub's own rule (an action indented under its host).
func drawnIndent(_ l: Line) -> Int {
    l.indent ?? ([.none, .openDashboard, .startGateway].contains(l.action) ? 0 : 1)
}

/// What a menu item is, for keeping it across updates: a separator, a line, or a line with a submenu.
enum Kind: Equatable { case separator, line, submenu }

/// The items an update keeps in place: the longest runs of the same kinds at the start and at the end of the menu. A kept
/// item is set where it is (its title, its action) instead of being replaced, so a submenu open under the pointer stays
/// open and the highlight stays put; only the items between the two runs are removed and inserted.
func keep(_ old: [Kind], _ new: [Kind]) -> (head: Int, tail: Int) {
    var head = 0
    while head < old.count && head < new.count && old[head] == new[head] { head += 1 }
    var tail = 0
    while tail < old.count - head && tail < new.count - head && old[old.count - 1 - tail] == new[new.count - 1 - tail] { tail += 1 }
    return (head, tail)
}

struct Event: Equatable {
    var key: String                                     // one notification per key, ever
    var title: String
    var body: String
    var ext = ""                                        // an extension's event: a click opens its page
}

// ---------------------------------------------------------------- extensions (GUIDE.md "Extensions", protocol 1)
/// One extension, as its manifest (<state>/extensions/<id>.json) describes it; only a sound manifest becomes one.
struct Extension: Equatable {
    var id: String
    var name: String
    var title = ""
    var version = ""
    var menu: String                                    // GET: its menu, as JSON
    var open = ""                                       // its page
    var token = ""                                      // the file holding the token its POSTs carry
    var tokenHeader = "X-Extension-Token"
    var command: [String] = []
    var env: [String: String] = [:]
    var verbs: [String] = []

    var label: String { title.isEmpty ? name : "\(name) — \(title)" }
    var origin: String { originOf(menu) ?? "" }
}

/// An extension as found on this round: the menu it served, or nil when it did not answer (and since when).
struct ExtensionPage {
    var ext: Extension
    var menu: [String: Any]?
    var downSince: Double? = nil
}

func originOf(_ url: String) -> String? {
    guard let u = URLComponents(string: url), u.scheme == "http", let host = u.host, host == "localhost" || host == "127.0.0.1" else { return nil }
    return "http://\(host)" + (u.port.map { ":\($0)" } ?? "")
}

/// A manifest read the way localmodels/extensions.py reads it; nil unless it is sound enough to draw and to act for.
func readExtension(_ data: Data?) -> Extension? {
    guard let data = data, let m = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          (m["extension"] as? NSNumber)?.intValue == 1,
          let id = m["id"] as? String, id.range(of: "^[a-z0-9][a-z0-9._-]{0,63}$", options: .regularExpression) != nil,
          let name = m["name"] as? String, !name.trimmingCharacters(in: .whitespaces).isEmpty,
          let menu = m["menu"] as? String, let home = originOf(menu) else { return nil }
    var e = Extension(id: id, name: name, menu: menu)
    e.title = (m["title"] as? String) ?? ""
    e.version = (m["version"] as? String) ?? ""
    if let open = m["open"] as? String, originOf(open) == home { e.open = open }
    if let token = m["token"] as? String, token.hasPrefix("/") { e.token = token }
    if let h = m["token_header"] as? String, h.range(of: "^X-[A-Za-z0-9-]{1,60}$", options: .regularExpression) != nil { e.tokenHeader = h }
    if let run = m["run"] as? [String: Any], let cmd = run["command"] as? [String], let first = cmd.first, first.hasPrefix("/"),
       let verbs = run["verbs"] as? [String] {
        e.command = cmd
        e.env = (run["env"] as? [String: String]) ?? [:]
        e.verbs = verbs.filter { $0.range(of: "^[a-z][a-z0-9-]{0,31}$", options: .regularExpression) != nil }
    }
    return e
}

/// One action of an extension's menu, held to the protocol: a page or a POST on its own origin, or one of its verbs.
/// Anything else is no action (the line is drawn, but does nothing).
func extensionAction(_ v: Any?, _ e: Extension) -> Action {
    guard let a = v as? [String: Any] else { return .none }
    let kinds = ["open", "post", "run"].filter { a[$0] != nil }
    guard kinds.count == 1 else { return .none }
    let confirm = (a["confirm"] as? String) ?? ""
    switch kinds[0] {
    case "open":
        guard let to = a["open"] as? String else { return .none }
        if to.hasPrefix("/") && !to.hasPrefix("//") { return .extOpen(ext: e.id, url: e.origin + to) }
        return originOf(to) == e.origin ? .extOpen(ext: e.id, url: to) : .none
    case "post":
        guard let path = a["post"] as? String, path.hasPrefix("/"), !path.hasPrefix("//") else { return .none }
        var input: Input? = nil
        if let i = a["input"] as? [String: Any] {
            guard let prompt = i["prompt"] as? String, let field = i["field"] as? String, !field.isEmpty else { return .none }
            input = Input(prompt: prompt, field: field, placeholder: (i["placeholder"] as? String) ?? "")
        }
        return .extPost(ext: e.id, path: path, body: (a["body"] as? [String: String]) ?? [:], confirm: confirm, input: input,
                        reveal: (a["reveal"] as? Bool) ?? false)
    default:
        guard let verb = a["run"] as? String, e.verbs.contains(verb) else { return .none }
        return .extRun(ext: e.id, verb: verb, confirm: confirm)
    }
}

let sectionLines = 12                                  // an extension's own lines drawn in the menu itself; the rest under "More"
let titleLength = 72                                   // an extension's longer titles are cut to this, the whole in the tooltip

/// A title cut to about `n` characters in the middle, at spaces where it can be, so its end stays (it often holds the
/// state: "4 of 13 · next: Stems"); the whole title becomes the tooltip.
func fit(_ s: String, _ n: Int = titleLength) -> (title: String, tooltip: String) {
    guard s.count > n else { return (s, "") }
    let keepHead = n * 11 / 20, keepTail = n - keepHead - 3
    var head = String(s.prefix(keepHead)), tail = String(s.suffix(keepTail))
    if let sp = head.lastIndex(of: " "), head.distance(from: head.startIndex, to: sp) > keepHead / 2 { head = String(head[..<sp]) }
    if let sp = tail.firstIndex(of: " "), tail.distance(from: sp, to: tail.endIndex) > keepTail / 2 { tail = String(tail[tail.index(after: sp)...]) }
    return (head.trimmingCharacters(in: .whitespaces) + " … " + tail.trimmingCharacters(in: .whitespaces), s)
}

/// An extension's lines, read as served: titles (cut to fit), actions held to the protocol, indents, separators, and
/// submenus at most three deep; at most 200 lines in all.
func extensionLines(_ v: Any?, _ e: Extension, depth: Int = 1, budget: inout Int) -> [Line] {
    var out: [Line] = []
    for x in (v as? [[String: Any]]) ?? [] {
        guard budget > 0 else { break }
        guard let title = x["title"] as? String, !title.trimmingCharacters(in: .whitespaces).isEmpty else { continue }
        budget -= 1
        let (shown, whole) = fit(title)
        var l = Line(title: shown, action: extensionAction(x["action"], e), separatorBefore: (x["separator"] as? Bool) ?? false,
                     indent: min(3, max(0, (x["indent"] as? NSNumber)?.intValue ?? 0)), tooltip: whole)
        if depth < 3 { l.children = extensionLines(x["children"], e, depth: depth + 1, budget: &budget) }
        out.append(l)
    }
    return out
}

/// One extension's section, in the menu itself (no submenu to open first), drawn as the hub draws a host: its name, then
/// indented under it its page (what a person opens most), what needs the person (its alerts), where it stands (the
/// summary), and its own lines (one block: a separator it asks for shows inside its submenus only; past 12 lines, the
/// rest under "More"). Also its events, keys of its own, and whether it needs attention. Not answering: the name says
/// so, with Start when its manifest lists the verb.
func extensionSection(_ p: ExtensionPage, now: Double) -> (lines: [Line], events: [Event], attention: Bool) {
    let e = p.ext
    guard let menu = p.menu else {
        var out = [Line(title: "\(e.label) — not answering", separatorBefore: true, indent: 0)]
        if e.verbs.contains("start") { out.append(Line(title: "Start \(e.name)", action: .extRun(ext: e.id, verb: "start", confirm: ""), indent: 1)) }
        out.append(Line(title: "No menu at " + e.menu, indent: 1))
        var events: [Event] = []
        if let since = p.downSince, now - since > 60 {
            events.append(Event(key: "ext:\(e.id):down:\(Int(since))", title: "\(e.label) not answering",
                                body: "Since \(clockText(since)): \(e.menu)", ext: e.id))
        }
        return (out, events, true)
    }
    guard (menu["menu"] as? NSNumber)?.intValue == 1 else {
        return ([Line(title: "\(e.label) — its menu is not protocol 1", separatorBefore: true, indent: 0)], [], false)
    }
    var budget = 200
    var out = [Line(title: e.label, separatorBefore: true, indent: 0)]
    if !e.open.isEmpty { out.append(Line(title: "Open \(e.name)", action: .extOpen(ext: e.id, url: e.open), indent: 1)) }
    let alerts = extensionLines(menu["alerts"], e, depth: 3, budget: &budget).map { Line(title: $0.title, action: $0.action, indent: 1, tooltip: $0.tooltip) }
    out += alerts
    if let s = menu["summary"] as? String, !s.trimmingCharacters(in: .whitespaces).isEmpty {
        let (shown, whole) = fit(s)
        out.append(Line(title: shown, indent: 1, tooltip: whole))
    }
    var own = extensionLines(menu["lines"], e, budget: &budget).map { l -> Line in
        var l = l; l.separatorBefore = false; l.indent = min(4, (l.indent ?? 0) + 1); return l
    }
    if own.count > sectionLines {
        let more = own[(sectionLines - 1)...].map { l -> Line in var l = l; l.indent = max(0, (l.indent ?? 1) - 1); return l }
        own = Array(own[..<(sectionLines - 1)]) + [Line(title: "More", indent: 1, children: more)]
    }
    out += own
    let events = ((menu["events"] as? [[String: Any]]) ?? []).compactMap { x -> Event? in
        guard let key = x["key"] as? String, !key.isEmpty, let title = x["title"] as? String, !title.isEmpty else { return nil }
        return Event(key: "ext:\(e.id):\(key)", title: title, body: (x["body"] as? String) ?? "", ext: e.id)
    }
    return (out, events, !alerts.isEmpty)
}

/// The extensions' sections added to a glance, after the hosts, each in the order of its manifest's file name.
func withExtensions(_ g: Glance, _ pages: [ExtensionPage], now: Double) -> Glance {
    var g = g
    for p in pages {
        let s = extensionSection(p, now: now)
        g.lines += s.lines; g.events += s.events; g.attention = g.attention || s.attention
    }
    return g
}

struct Glance {
    var gatewayUp = false
    var version = ""
    var icon = Icon.idle
    var attention = false
    var summary = ""                                    // the status line, and the accessibility label's core
    var lines: [Line] = []
    var events: [Event] = []

    var accessibilityLabel: String {
        "Local models: " + summary + (attention ? "; needs attention" : "")
    }
}

func clockText(_ t: Double) -> String {
    let f = DateFormatter()
    f.timeStyle = .short
    f.dateStyle = .none
    return f.string(from: Date(timeIntervalSince1970: t))
}

private func num(_ v: Any?) -> Double? { (v as? NSNumber)?.doubleValue }
private func int(_ v: Any?) -> Int { Int(num(v) ?? 0) }
private func str(_ v: Any?) -> String { (v as? String) ?? "" }
private func dict(_ v: Any?) -> [String: Any] { (v as? [String: Any]) ?? [:] }
private func list(_ v: Any?) -> [[String: Any]] { (v as? [[String: Any]]) ?? [] }

/// The gateway did not answer: the icon shows idle with attention, and the menu offers to start it. The extensions
/// still show: they are not the gateway's.
func glanceDown(port: Int = 1235, extensions: [ExtensionPage] = [], now: Double = Date().timeIntervalSince1970) -> Glance {
    var g = Glance()
    g.attention = true
    g.summary = "gateway not answering on localhost:\(port)"
    g.lines = [Line(title: "Gateway not answering on localhost:\(port)"), Line(title: "Start gateway", action: .startGateway)]
    return withExtensions(g, extensions, now: now)
}

/// A snapshot read into a glance, with each extension's section. `now` is the time to measure "unreachable for more than
/// a minute" against.
func glance(_ snap: [String: Any], now: Double = Date().timeIntervalSince1970, extensions: [ExtensionPage] = []) -> Glance {
    withExtensions(hubGlance(snap, now: now), extensions, now: now)
}

func hubGlance(_ snap: [String: Any], now: Double) -> Glance {
    var g = Glance()
    g.gatewayUp = true
    g.version = str(dict(snap["about"])["version"])
    let door = dict(snap["door"]), queue = dict(door["queue"])
    let queued = int(queue["people"]) + int(queue["others"])
    let mt = snap["maintenance"] as? [String: Any]
    let off = (mt?["off"] as? Bool) ?? false
    let inMaintenance = !off && str(mt?["phase"]) == "on"
    let peersOnly = off && ((mt?["peers"] as? Bool) ?? false)       // this Mac's models off, every request sent to the peers
    let refusing = str(mt?["phase"]) == "on" && !peersOnly          // every request gets 503: no slot anywhere takes one

    // this Mac's slots
    let fams = list(dict(snap["lmstudio"])["families"])
    let localBusy = off ? 0 : fams.reduce(0) { $0 + int($1["busy"]) }, localSlots = off ? 0 : fams.reduce(0) { $0 + int($1["at_once"]) }
    var busy = localBusy, slots = localSlots

    // the hosts: this Mac, then each peer
    var hosts: [Line] = []
    let here = peersOnly ? "This Mac — local models off since \(clockText(num(mt?["start"]) ?? now)); requests go to peers"
        : off ? "This Mac — local models switched off since \(clockText(num(mt?["start"]) ?? now))"
        : inMaintenance ? "This Mac — in maintenance" + (mt?["end"] is NSNumber ? " until \(clockText(num(mt?["end"])!))" : "")
        : localSlots > 0 ? "This Mac — \(localBusy)/\(localSlots) active" : "This Mac — no instance loaded"
    hosts.append(Line(title: here, separatorBefore: true))
    if !inMaintenance { hosts.append(Line(title: off ? "Turn local models on" : "Turn local models off", action: .local(on: off))) }

    var alerts: [String] = []
    for p in list(snap["peers"]) {
        let name = str(p["name"]), state = str(p["state"]), pOff = (p["off"] as? Bool) ?? false
        var b = 0, s = 0
        for (_, f) in dict(p["families"]) {
            let f = dict(f)
            if (f["match"] as? Bool) == true { b += int(f["busy"]); s += int(f["slots"]) }
        }
        let since = num(p["since"])
        var title: String, action = Action.none
        switch state {
        case "up":
            if !refusing { busy += b; slots += s }
            title = s > 0 ? "\(name) — \(b)/\(s) active" : "\(name) — no matching instance loaded"
            action = .peer(name: name, on: false)
        case "maintenance" where pOff:
            title = "\(name) — local models switched off"; action = .peer(name: name, on: true)
        case "maintenance":
            title = "\(name) — in maintenance"
        case "down":
            title = "\(name) — unreachable" + (since.map { " since \(clockText($0))" } ?? "")
            alerts.append("Peer \(name) unreachable" + (since.map { " since \(clockText($0))" } ?? ""))
        case "refused":
            title = "\(name) — auth failed (peer-token)"; alerts.append("Peer \(name): auth failed (peer-token mismatch)")
        default:
            title = "\(name) — not polled yet"
        }
        hosts.append(Line(title: title, separatorBefore: true))
        if action != .none { hosts.append(Line(title: pOff ? "Turn \(name) on" : "Turn \(name) off", action: action)) }
        if state == "down", let since = since, now - since > 60 {
            g.events.append(Event(key: "peer:\(name):\(Int(since))", title: "Peer \(name) unreachable",
                                  body: "Since \(clockText(since)): \(str(p["why"]))"))
        }
    }

    // what needs a person
    for d in list(dict(snap["crashes"])["down"]) {
        let fam = str(d["family"]), at = num(d["crashed"]) ?? now, left = str(d["state"]) == "left down"
        alerts.append("\(fam) crashed at \(clockText(at))" + (left ? "; left unloaded" : "; reloading"))
        if left {
            g.events.append(Event(key: "crash:\(fam):\(Int(at))", title: "Instance left unloaded",
                                  body: "\(fam) crashed at \(clockText(at)): \(str(d["why"]))"))
        }
    }
    for x in list(dict(snap["crashes"])["stalled"]) {               // an engine that stopped while still listed (watchdog.py)
        alerts.append("\(str(x["copy"])) stalled at \(clockText(num(x["at"]) ?? now)); reloading")
    }
    if let e = snap["errors"] as? [String: Any], int(e["count"]) > 0 {
        alerts.append("\(int(e["count"])) error\(int(e["count"]) == 1 ? "" : "s") recorded in the last \(int(e["minutes"])) min")
    }
    if let w = snap["settings_error"] as? String, !w.isEmpty { alerts.append("Settings file not taken: \(w)") }
    if let w = snap["write_problem"] as? String, !w.isEmpty { alerts.append("Records not written: \(w)") }

    g.icon = slots > 0 ? .serving : .idle
    g.attention = !alerts.isEmpty
    g.summary = slots > 0 ? "\(busy) of \(slots) slots active" + (peersOnly ? " on peers" : "") + (queued > 0 ? " · \(queued) queued" : "")
        : peersOnly ? "local models off; no peer reachable" : off ? "local models switched off" : inMaintenance ? "in maintenance" : "no instance loaded"
    g.lines = [Line(title: g.summary.prefix(1).uppercased() + g.summary.dropFirst())]
        + alerts.map { Line(title: $0, action: .openDashboard) }
        + hosts
    return g
}

/// Events not notified before. The first glance after launch only records what is already there.
func fresh(_ events: [Event], seen: inout Set<String>, baseline: Bool) -> [Event] {
    let new = events.filter { !seen.contains($0.key) }
    for e in events { seen.insert(e.key) }
    return baseline ? [] : new
}

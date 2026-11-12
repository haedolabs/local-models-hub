// Glance: the gateway's snapshot (GET /hub/snapshot) read into what the menu-bar app shows, with no AppKit, so a
// plain runner can test it (app/tests). The icon state, the menu's lines and their actions, and the events worth a
// notification all come from here.
import Foundation

enum Icon: Equatable { case serving, idle }            // serving: some slot can take a request; idle: off, down or none loaded

enum Action: Equatable {
    case none, openDashboard, startGateway
    case local(on: Bool)                                // POST /hub/on or /hub/off
    case peer(name: String, on: Bool)                   // POST /hub/link/on or /hub/link/off with {"peer": name}
}

struct Line: Equatable {
    var title: String
    var action: Action = .none
    var separatorBefore = false
}

struct Event: Equatable {
    var key: String                                     // one notification per key, ever
    var title: String
    var body: String
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

/// The gateway did not answer: the icon shows idle with attention, and the menu offers to start it.
func glanceDown(port: Int = 1235) -> Glance {
    var g = Glance()
    g.attention = true
    g.summary = "gateway not answering on localhost:\(port)"
    g.lines = [Line(title: "Gateway not answering on localhost:\(port)"), Line(title: "Start gateway", action: .startGateway)]
    return g
}

/// A snapshot read into a glance. `now` is the time to measure "unreachable for more than a minute" against.
func glance(_ snap: [String: Any], now: Double = Date().timeIntervalSince1970) -> Glance {
    var g = Glance()
    g.gatewayUp = true
    g.version = str(dict(snap["about"])["version"])
    let door = dict(snap["door"]), queue = dict(door["queue"])
    let queued = int(queue["people"]) + int(queue["others"])
    let mt = snap["maintenance"] as? [String: Any]
    let off = (mt?["off"] as? Bool) ?? false
    let inMaintenance = !off && str(mt?["phase"]) == "on"

    // this Mac's slots
    let fams = list(dict(snap["lmstudio"])["families"])
    let localBusy = fams.reduce(0) { $0 + int($1["busy"]) }, localSlots = fams.reduce(0) { $0 + int($1["at_once"]) }
    var busy = localBusy, slots = localSlots

    // the hosts: this Mac, then each peer
    var hosts: [Line] = []
    let here = off ? "This Mac — local models switched off since \(clockText(num(mt?["start"]) ?? now))"
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
            busy += b; slots += s
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
    if let e = snap["errors"] as? [String: Any], int(e["count"]) > 0 {
        alerts.append("\(int(e["count"])) error\(int(e["count"]) == 1 ? "" : "s") recorded in the last \(int(e["minutes"])) min")
    }
    if let w = snap["settings_error"] as? String, !w.isEmpty { alerts.append("Settings file not taken: \(w)") }
    if let w = snap["write_problem"] as? String, !w.isEmpty { alerts.append("Records not written: \(w)") }

    g.icon = slots > 0 ? .serving : .idle
    g.attention = !alerts.isEmpty
    g.summary = slots > 0 ? "\(busy) of \(slots) slots active" + (queued > 0 ? " · \(queued) queued" : "")
        : off ? "local models switched off" : "no instance loaded"
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

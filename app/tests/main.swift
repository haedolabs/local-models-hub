// The menu-bar app's state logic (app/Glance.swift) against fixture snapshots: the icon, the menu's lines and actions,
// and the notifications. Built and run by `localmodels check` when the Command Line Tools are present:
//   swiftc app/Glance.swift app/tests/main.swift -o <tmp> && <tmp>
import Foundation

var results: [Bool] = []
func t(_ name: String, _ good: Bool, _ note: @autoclosure () -> String = "") {
    results.append(good)
    print("  \(good ? "ok  " : "FAIL") \(name)" + (good ? "" : ": \(note())"))
}

func snap(_ json: String) -> [String: Any] {
    try! JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
}

let now = 1_800_000_000.0
let fam = #"{"id": "test/family", "busy": 3, "at_once": 8}"#
let peerUp = #"{"name": "m4pro", "state": "up", "off": false, "since": 1799999000, "families": {"test/family": {"match": true, "busy": 1, "slots": 4}}}"#

print("menu-bar app: the snapshot read into the icon, the menu and the notifications")

let busy = glance(snap(#"{"about": {"version": "1.2.3"}, "door": {"up": true, "queue": {"people": 1, "others": 1}}, "maintenance": null,"#
    + #""lmstudio": {"families": [\#(fam)]}, "peers": [\#(peerUp)], "crashes": {"down": []}, "errors": null}"#), now: now)
t("busy: this Mac's and the peer's slots counted, the queue said", busy.summary == "4 of 12 slots active · 2 queued", busy.summary)
t("busy: the icon serves, nothing needs attention", busy.icon == .serving && !busy.attention)
t("busy: this Mac and the peer each with their switch",
  busy.lines.contains(Line(title: "Turn local models off", action: .local(on: false)))
  && busy.lines.contains(Line(title: "Turn m4pro off", action: .peer(name: "m4pro", on: false))), "\(busy.lines.map(\.title))")
t("busy: the version is read", busy.version == "1.2.3")
t("the accessibility label says the state in words", busy.accessibilityLabel == "Local models: 4 of 12 slots active · 2 queued")

let idle = glance(snap(#"{"door": {"queue": {}}, "maintenance": null, "lmstudio": {"families": [{"id": "f", "busy": 0, "at_once": 4}]}, "peers": [], "crashes": {"down": []}}"#), now: now)
t("idle: serving, no queue words", idle.icon == .serving && idle.summary == "0 of 4 slots active", idle.summary)

let off = glance(snap(#"{"door": {"queue": {}}, "maintenance": {"off": true, "phase": "on", "start": 1799990000}, "lmstudio": {"families": []}, "peers": [], "crashes": {"down": []}}"#), now: now)
t("switched off: the icon is idle and says so", off.icon == .idle && off.summary == "local models switched off", off.summary)
t("switched off: the switch turns them on", off.lines.contains(Line(title: "Turn local models on", action: .local(on: true))), "\(off.lines.map(\.title))")
t("switched off: this Mac's line says since when", off.lines.contains { $0.title.hasPrefix("This Mac — local models switched off since ") })

let viaPeer = glance(snap(#"{"door": {"queue": {}}, "maintenance": {"off": true, "phase": "on", "start": 1799990000}, "lmstudio": {"families": []}, "peers": [\#(peerUp)], "crashes": {"down": []}}"#), now: now)
t("this Mac off, the peer up: still serving through the peer", viaPeer.icon == .serving && viaPeer.summary == "1 of 4 slots active", viaPeer.summary)

let window = glance(snap(#"{"door": {"queue": {}}, "maintenance": {"off": false, "phase": "on", "start": 1799990000, "end": 1800001000}, "lmstudio": {"families": []}, "peers": [], "crashes": {"down": []}}"#), now: now)
t("in a maintenance window: no switch for this Mac (the window ends it)", !window.lines.contains { if case .local = $0.action { return true }; return false }, "\(window.lines.map(\.title))")

let peerDown = #"{"name": "m4pro", "state": "down", "since": 1799999000, "why": "no route", "families": {}}"#
let down = glance(snap(#"{"door": {"queue": {}}, "maintenance": null, "lmstudio": {"families": [\#(fam)]}, "peers": [\#(peerDown)], "crashes": {"down": []}}"#), now: now)
t("a peer down: an alert, attention, and no switch for it", down.attention && down.lines.contains { $0.title.hasPrefix("Peer m4pro unreachable") }
  && !down.lines.contains { if case .peer = $0.action { return true }; return false }, "\(down.lines.map(\.title))")
t("a peer down for more than a minute: one notification event", down.events.count == 1 && down.events[0].key == "peer:m4pro:1799999000", "\(down.events)")
let justDown = glance(snap(#"{"door": {"queue": {}}, "maintenance": null, "lmstudio": {"families": []}, "peers": [\#(peerDown)], "crashes": {"down": []}}"#), now: 1_799_999_030)
t("a peer down for 30 s: no notification yet", justDown.events.isEmpty)

let peerOff = #"{"name": "m4pro", "state": "maintenance", "off": true, "families": {}}"#
let pOff = glance(snap(#"{"door": {"queue": {}}, "maintenance": null, "lmstudio": {"families": [\#(fam)]}, "peers": [\#(peerOff)], "crashes": {"down": []}}"#), now: now)
t("a peer switched off: not an alert; its switch turns it on", !pOff.attention && pOff.lines.contains(Line(title: "Turn m4pro on", action: .peer(name: "m4pro", on: true))), "\(pOff.lines.map(\.title))")

let crash = glance(snap(#"{"door": {"queue": {}}, "maintenance": null, "lmstudio": {"families": [\#(fam)]}, "peers": [], "crashes": {"down": [{"family": "test/family", "crashed": 1799999500, "state": "left down", "why": "out of memory"}, {"family": "other", "crashed": 1799999600, "state": "loading again"}]}}"#), now: now)
t("a crash left unloaded and one reloading: both alerts, attention", crash.attention && crash.lines.filter { $0.action == .openDashboard }.count == 2, "\(crash.lines.map(\.title))")
t("...and only the one left unloaded is a notification", crash.events.map(\.key) == ["crash:test/family:1799999500"], "\(crash.events)")

var seen = Set<String>()
t("the first glance after launch notifies nothing (a baseline)", fresh(crash.events, seen: &seen, baseline: true).isEmpty)
t("the same event later: not notified again", fresh(crash.events, seen: &seen, baseline: false).isEmpty)
t("a new event: notified once", fresh(down.events, seen: &seen, baseline: false).count == 1 && fresh(down.events, seen: &seen, baseline: false).isEmpty)

let errs = glance(snap(#"{"door": {"queue": {}}, "maintenance": null, "lmstudio": {"families": []}, "peers": [], "crashes": {"down": []}, "errors": {"count": 2, "minutes": 60}, "settings_error": "families: not an object"}"#), now: now)
t("recorded errors and a settings error: alerts", errs.attention && errs.lines.contains { $0.title == "2 errors recorded in the last 60 min" }
  && errs.lines.contains { $0.title.hasPrefix("Settings file not taken") }, "\(errs.lines.map(\.title))")

let gone = glanceDown()
t("the gateway down: idle with attention, and a way to start it", gone.icon == .idle && gone.attention && gone.lines.contains(Line(title: "Start gateway", action: .startGateway)))

let partial = glance(snap(#"{}"#), now: now)
t("a snapshot missing every field: read without a crash", partial.gatewayUp && partial.icon == .idle)

print("\(results.filter { $0 }.count) of \(results.count) ok")
exit(results.allSatisfy { $0 } ? 0 : 1)

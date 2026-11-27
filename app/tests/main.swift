// The menu-bar app's state logic (app/Glance.swift) against fixture snapshots: the icon, the menu's lines and actions,
// and the notifications; its menu across polls (app/Menu.swift); and extension redirects and child output
// (app/ExtensionIO.swift). Built and run by `localmodels check` when the Command Line Tools are present:
//   swiftc app/Glance.swift app/Menu.swift app/ExtensionIO.swift app/tests/main.swift -o <tmp> && <tmp>
import AppKit

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
let peerUp = #"{"name": "m4pro", "state": "up", "off": false, "since": 1799999000, "families": {"test/family": {"match": true, "busy": 1, "slots": 4}, "test/embed": {"kind": "embedding", "match": true, "busy": 1, "slots": 1}}}"#
let peerDown0 = #"{"name": "m4pro", "state": "down", "since": 1799999000, "why": "no route", "families": {}}"#

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

let refused = glance(snap(#"{"door": {"queue": {}}, "maintenance": {"off": true, "phase": "on", "start": 1799990000}, "lmstudio": {"families": [\#(fam)]}, "peers": [\#(peerUp)], "crashes": {"down": []}}"#), now: now)
t("switched off with a peer up: every request gets 503, so nothing serves (the peer's slots are not counted)", refused.icon == .idle && refused.summary == "local models switched off", refused.summary)
let viaPeer = glance(snap(#"{"door": {"queue": {}}, "maintenance": {"off": true, "peers": true, "phase": "on", "start": 1799990000}, "lmstudio": {"families": [\#(fam)]}, "peers": [\#(peerUp)], "crashes": {"down": []}}"#), now: now)
t("switched off to peers only, the peer up: serving through the peer alone, this Mac's instances not counted", viaPeer.icon == .serving && viaPeer.summary == "1 of 4 slots active on peers", viaPeer.summary)
t("peers only: this Mac's line says so, and its switch turns the local models on", viaPeer.lines.contains { $0.title.hasPrefix("This Mac — local models off since ") && $0.title.hasSuffix("requests go to peers") }
  && viaPeer.lines.contains(Line(title: "Turn local models on", action: .local(on: true))), "\(viaPeer.lines.map(\.title))")
let alone = glance(snap(#"{"door": {"queue": {}}, "maintenance": {"off": true, "peers": true, "phase": "on", "start": 1799990000}, "lmstudio": {"families": []}, "peers": [\#(peerDown0)], "crashes": {"down": []}}"#), now: now)
t("peers only with no peer reachable: idle, and says why", alone.icon == .idle && alone.summary == "local models off; no peer reachable", alone.summary)

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

let stalledSnap = glance(snap(#"{"door": {"queue": {}}, "maintenance": null, "lmstudio": {"families": [\#(fam)]}, "peers": [], "crashes": {"down": [], "stalled": [{"copy": "test/family", "at": 1799999500, "why": "stalled: test"}]}}"#), now: now)
t("a stalled instance: an alert that says it is being reloaded, attention", stalledSnap.attention && stalledSnap.lines.contains { $0.title.hasPrefix("test/family stalled at ") && $0.title.hasSuffix("; reloading") }, "\(stalledSnap.lines.map(\.title))")

let errs = glance(snap(#"{"door": {"queue": {}}, "maintenance": null, "lmstudio": {"families": []}, "peers": [], "crashes": {"down": []}, "errors": {"count": 2, "minutes": 60}, "settings_error": "families: not an object"}"#), now: now)
t("recorded errors and a settings error: alerts", errs.attention && errs.lines.contains { $0.title == "2 errors recorded in the last 60 min" }
  && errs.lines.contains { $0.title.hasPrefix("Settings file not taken") }, "\(errs.lines.map(\.title))")

let gone = glanceDown()
t("the gateway down: idle with attention, and a way to start it", gone.icon == .idle && gone.attention && gone.lines.contains(Line(title: "Start gateway", action: .startGateway)))

let partial = glance(snap(#"{}"#), now: now)
t("a snapshot missing every field: read without a crash", partial.gatewayUp && partial.icon == .idle)

// ---------------------------------------------------------------- extensions (GUIDE.md "Extensions"; a made-up one)
print("menu-bar app: extensions, each in its own section, every action held to its own origin and verbs")

func manifest(_ extra: String = "") -> Data {
    Data((#"{"extension": 1, "id": "example.first", "name": "Example Tool", "title": "First Project", "version": "1.0.0", "menu": "http://localhost:9411/api/menu","#
          + #" "open": "http://localhost:9411", "token": "/tmp/example.token", "token_header": "X-Example-Token","#
          + #" "run": {"command": ["/usr/bin/true", "example"], "env": {"EXAMPLE_PROJECT": "/tmp/first"}, "verbs": ["start", "restart"]}\#(extra)}"#).utf8)
}
let ex = readExtension(manifest())
t("a sound manifest is read", ex != nil && ex!.label == "Example Tool — First Project" && ex!.origin == "http://localhost:9411" && ex!.verbs == ["start", "restart"], "\(String(describing: ex))")
t("...its token header and command as given", ex?.tokenHeader == "X-Example-Token" && ex?.command == ["/usr/bin/true", "example"] && ex?.env == ["EXAMPLE_PROJECT": "/tmp/first"])
t("refused: another protocol version", readExtension(Data(#"{"extension": 2, "id": "a.b", "name": "A", "menu": "http://localhost:1/m"}"#.utf8)) == nil)
t("refused: a menu on another machine", readExtension(Data(#"{"extension": 1, "id": "a.b", "name": "A", "menu": "http://example.com/m"}"#.utf8)) == nil)
t("refused: an id with capitals", readExtension(Data(#"{"extension": 1, "id": "A.b", "name": "A", "menu": "http://localhost:1/m"}"#.utf8)) == nil)
t("refused: not JSON", readExtension(Data("nope".utf8)) == nil && readExtension(nil) == nil)
let offsite = readExtension(Data(#"{"extension": 1, "id": "a.b", "name": "A", "menu": "http://localhost:1/m", "open": "http://example.com/"}"#.utf8))
t("a manifest with a page on another origin is refused", offsite == nil)
let fixtureFile = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("manifest-fixtures.json")
let manifestFixtures = try JSONSerialization.jsonObject(with: Data(contentsOf: fixtureFile)) as! [[String: Any]]
for fixture in manifestFixtures {
    var base: [String: Any] = ["extension": 1, "id": "fixture", "name": "Fixture", "menu": "http://localhost/menu"]
    base.merge(fixture["fields"] as! [String: Any]) { _, field in field }
    t("shared manifest: \(fixture["name"]!)", (readManifest(base) != nil) == (fixture["valid"] as! Bool))
}

let redirects = ExtensionRedirect()
let session = URLSession(configuration: .ephemeral, delegate: redirects, delegateQueue: nil)
let startURL = URL(string: "http://localhost:9411/start")!
for method in ["GET", "POST"] {
    var initial = URLRequest(url: startURL)
    initial.httpMethod = method
    let task = session.dataTask(with: initial)
    let response = HTTPURLResponse(url: startURL, statusCode: 307, httpVersion: nil, headerFields: nil)!
    for port in [9411, 9412] {
        var next = initial; next.url = URL(string: "http://localhost:\(port)/next")
        redirects.urlSession(session, task: task, willPerformHTTPRedirection: response, newRequest: next) { follow in
            t("\(method) follows a redirect only on the extension's original port", (follow != nil) == (port == 9411))
        }
    }
    task.cancel()
}
session.invalidateAndCancel()

let child = Process(), finished = DispatchSemaphore(value: 0)
child.executableURL = URL(fileURLWithPath: "/bin/sh")
child.arguments = ["-c", "head -c 262144 /dev/zero; echo finished >&2"]
var tail = "", status: Int32 = -1
try ExtensionOutput().run(child) { code, words in status = code; tail = words; finished.signal() }
let exited = finished.wait(timeout: .now() + 10) == .success
if !exited && child.isRunning { child.terminate() }
t("a verbose extension exits and its final diagnostic fits in 4 KiB", exited && status == 0 && tail.utf8.count == 4096 && tail.hasSuffix("finished\n"))

let menuJSON = #"{"menu": 1, "summary": "3 of 8 items ready", "alerts": [{"title": "Something is due tomorrow", "action": {"open": "/due"}}],"#
    + #" "lines": [{"title": "A status line"}, {"title": "Open the report", "action": {"open": "/report"}, "indent": 1},"#
    + #" {"title": "Export it", "action": {"post": "/api/export", "reveal": true}, "indent": 1},"#
    + #" {"title": "Leave a note…", "action": {"post": "/api/note", "body": {"via": "menu"}, "input": {"prompt": "A note", "field": "text", "placeholder": "the note"}}},"#
    + #" {"title": "Items", "children": [{"title": "First item", "action": {"open": "/items/1"}}]},"#
    + #" {"title": "Off to another site", "action": {"open": "http://example.com/"}}, {"title": "Uninstall it", "action": {"run": "uninstall"}},"#
    + #" {"title": "Restart it", "action": {"run": "restart", "confirm": "Restart the example?"}}],"#
    + #" "events": [{"key": "done:42", "title": "Job 42 finished", "body": "all good"}]}"#
let hubSnap = snap(#"{"about": {"version": "1.2.3"}, "door": {"up": true, "queue": {}}, "maintenance": null, "lmstudio": {"families": [\#(fam)]}, "peers": [], "crashes": {"down": []}}"#)
let withExt = glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: snap(menuJSON))])
let at = withExt.lines.firstIndex { $0.title == "Example Tool — First Project" } ?? -1
let sec = at >= 0 ? Array(withExt.lines[at...]) : []
t("its section: in the menu itself (no submenu to open first), after the hosts, under its name as a host is",
  at > 0 && sec[0] == Line(title: "Example Tool — First Project", separatorBefore: true, indent: 0)
  && withExt.lines[..<at].contains { $0.title == "Turn local models off" }, "\(withExt.lines.map(\.title))")
t("...its page first under the name", sec.count > 1 && sec[1] == Line(title: "Open Example Tool", action: .extOpen(ext: "example.first", url: "http://localhost:9411"), indent: 1), "\(sec.map(\.title))")
t("...then its alert, in its own section (not the hub's list), with its action",
  sec.count > 2 && sec[2] == Line(title: "Something is due tomorrow", action: .extOpen(ext: "example.first", url: "http://localhost:9411/due"), indent: 1)
  && !withExt.lines[..<at].contains { $0.title.contains("due tomorrow") }, "\(sec.map(\.title))")
t("...then the summary", sec.count > 3 && sec[3] == Line(title: "3 of 8 items ready", indent: 1), "\(sec.map(\.title))")
t("...its alert lights the dot", withExt.attention)
let kids = Array(sec.dropFirst(4))
t("...its lines indented under the name, one deeper than served", kids.first == Line(title: "A status line", indent: 1)
  && kids.contains(Line(title: "Open the report", action: .extOpen(ext: "example.first", url: "http://localhost:9411/report"), indent: 2)), "\(kids.map { "\($0.title) \($0.indent ?? -1)" })")
t("...a POST that shows its file", kids.contains(Line(title: "Export it", action: .extPost(ext: "example.first", path: "/api/export", body: [:], confirm: "", input: nil, reveal: true), indent: 2)))
t("...a POST that asks for a line first", kids.contains(Line(title: "Leave a note…", action: .extPost(ext: "example.first", path: "/api/note", body: ["via": "menu"], confirm: "",
                                                                                                  input: Input(prompt: "A note", field: "text", placeholder: "the note"), reveal: false), indent: 1)))
t("...a submenu of its own", kids.first { $0.title == "Items" }?.children == [Line(title: "First item", action: .extOpen(ext: "example.first", url: "http://localhost:9411/items/1"), indent: 0)])
t("...a page on another site: drawn, but no action", kids.first { $0.title == "Off to another site" }?.action == Action.none)
t("...a verb the manifest does not list: no action", kids.first { $0.title == "Uninstall it" }?.action == Action.none)
t("...a listed verb, with its question", kids.first { $0.title == "Restart it" }?.action == .extRun(ext: "example.first", verb: "restart", confirm: "Restart the example?"))
t("...one block: no separator inside the section itself", !sec.dropFirst().contains { $0.separatorBefore } && withExt.lines.last?.title == "Restart it", "\(sec.map(\.title))")
t("its event: a key of its own, and a click opens its page", withExt.events == [Event(key: "ext:example.first:done:42", title: "Job 42 finished", body: "all good", ext: "example.first")], "\(withExt.events)")
t("the hub's own lines and state are untouched by it", withExt.icon == .serving && withExt.summary == hubGlance(hubSnap, now: now).summary
  && Array(withExt.lines[..<max(0, at)]) == hubGlance(hubSnap, now: now).lines)

var longMenu = #"{"menu": 1, "lines": ["#
longMenu += (0..<20).map { #"{"title": "line \#($0)", "separator": \#($0 == 3 ? "true" : "false")}"# }.joined(separator: ",") + "]}"
let longSec = Array(glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: snap(longMenu))]).lines.drop { $0.title != "Example Tool — First Project" })
let more = longSec.last
t("past 12 lines of its own, the rest under More (at their served indent)", longSec.count == 1 + 1 + 12 && more?.title == "More"
  && more?.children.count == 9 && more?.children.first == Line(title: "line 11", indent: 0), "\(longSec.map(\.title)) \(more?.children.map(\.title) ?? [])")

let song = "Like a Stone (Live At Queen Elizabeth Theatre, Toronto, ON April 20, 2011) — 1 of 13 · next: Stems"
let cut = fit(song)
t("a long title: cut in the middle at spaces, its end (the state) kept, the whole as the tooltip",
  cut.title.count <= titleLength && cut.title.hasPrefix("Like a Stone") && cut.title.hasSuffix("1 of 13 · next: Stems") && cut.title.contains(" … ") && cut.tooltip == song, cut.title)
t("a title that fits: as is, no tooltip", fit("Open the report") == ("Open the report", ""))
let wide = glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: snap(#"{"menu": 1, "lines": [{"title": "Songs", "children": [{"title": "\#(song)"}]}]}"#))])
let cutKid = wide.lines.last?.children.first
t("...and so in a submenu", cutKid?.title == cut.title && cutKid?.tooltip == song, "\(String(describing: cutKid))")

let extDown = glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: nil, downSince: now - 90)])
let downAt = extDown.lines.firstIndex { $0.title == "Example Tool — First Project — not answering" } ?? -1
t("an extension not answering: its name says so, attention lit", downAt > 0 && extDown.attention, "\(extDown.lines.map(\.title))")
t("...first under it, a way to start it (a listed verb)", downAt > 0 && extDown.lines[downAt + 1] == Line(title: "Start Example Tool", action: .extRun(ext: "example.first", verb: "start", confirm: ""), indent: 1),
  "\(extDown.lines.map(\.title))")
t("...and where it was asked", extDown.lines.last == Line(title: "No menu at http://localhost:9411/api/menu", indent: 1))
t("...down for a minute and a half: one notification", extDown.events.map(\.key) == ["ext:example.first:down:\(Int(now - 90))"], "\(extDown.events)")
t("...down for twenty seconds: none yet", glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: nil, downSince: now - 20)]).events.isEmpty)
let noStart = readExtension(Data(#"{"extension": 1, "id": "a.b", "name": "A", "menu": "http://localhost:1/m"}"#.utf8))!
t("...with no start verb: no Start line", !glance(hubSnap, now: now, extensions: [ExtensionPage(ext: noStart, menu: nil)]).lines.contains { $0.title.hasPrefix("Start") })

let v2 = glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: snap(#"{"menu": 2, "lines": [{"title": "x"}]}"#))])
t("a menu of another protocol version: said, and nothing drawn from it", v2.lines.last?.title == "Example Tool — First Project — its menu is not protocol 1" && v2.lines.last?.children.isEmpty == true)
let deep = glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: snap(#"{"menu": 1, "lines": [{"title": "a", "children": [{"title": "b", "children": [{"title": "c", "children": [{"title": "d"}]}]}]}]}"#))])
t("submenus stop at three deep", deep.lines.last?.children.first?.children.first?.title == "c" && deep.lines.last?.children.first?.children.first?.children.isEmpty == true,
  "\(String(describing: deep.lines.last))")
var many = #"{"menu": 1, "lines": ["#
many += (0..<250).map { #"{"title": "line \#($0)"}"# }.joined(separator: ",") + "]}"
let manySec = Array(glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: snap(many))]).lines.drop { $0.title != "Example Tool — First Project" })
t("at most 200 lines are drawn", manySec.count - 3 + (manySec.last?.children.count ?? 0) == 200, "\(manySec.count) \(manySec.last?.children.count ?? 0)")

let downHub = glanceDown(extensions: [ExtensionPage(ext: ex!, menu: snap(menuJSON))], now: now)
t("the gateway down: the extensions still show", downHub.lines.contains { $0.title == "Example Tool — First Project" } && downHub.lines.contains(Line(title: "Start gateway", action: .startGateway)), "\(downHub.lines.map(\.title))")
t("no extensions: the menu is the hub's alone", glance(hubSnap, now: now).lines == hubGlance(hubSnap, now: now).lines)

// ---------------------------------------------------------------- the menu kept in place across updates
print("menu-bar app: an update keeps the items that line up, so an open submenu stays open")
let K = { (s: String) -> [Kind] in s.map { $0 == "-" ? Kind.separator : $0 == ">" ? Kind.submenu : Kind.line } }
t("nothing changed in shape: every item kept", keep(K("l-l>l-ll"), K("l-l>l-ll")) == (8, 0))
t("an alert added at the top: the rest kept, the open submenu with it", keep(K("l-l>l-ll"), K("ll-l>l-ll")) == (1, 7))
t("a line gone from a section: kept on both sides", keep(K("l-ll>-ll"), K("l-l>-ll")) == (3, 4))
t("a line that grew a submenu: only it replaced", keep(K("l-lll-ll"), K("l-l>l-ll")) == (3, 4))
t("never more kept than either has", { let k = keep(K("ll"), K("lll")); return k.head + k.tail <= 2 }())
t("an empty menu: everything new", keep([], K("l-l")) == (0, 0))
let kinds = { (g: Glance) -> [Kind] in g.lines.flatMap { l -> [Kind] in (l.separatorBefore ? [Kind.separator] : []) + [l.children.isEmpty ? .line : .submenu] } }
var later = snap(menuJSON); later["summary"] = "4 of 8 items ready"
let then = glance(hubSnap, now: now, extensions: [ExtensionPage(ext: ex!, menu: snap(menuJSON))])
let after = glance(hubSnap, now: now + 2, extensions: [ExtensionPage(ext: ex!, menu: later)])
t("a new summary two seconds later: the same shape, every item kept and retitled in place",
  keep(kinds(then), kinds(after)) == (kinds(then).count, 0) && then.lines != after.lines)

print("menu-bar app: the same with real menu items (app/Menu.swift): the same objects from one poll to the next")
final class Sink: NSObject { @objc func act(_ s: NSMenuItem) {} }
let sink = Sink(), keeper = MenuKeeper(target: sink, selector: #selector(Sink.act(_:)))
let real = NSMenu(); real.autoenablesItems = false
let footer = [Spec(), Spec(line: Line(title: "Open dashboard", action: .openDashboard, indent: 0), key: "d"),
              Spec(line: Line(title: "Launch at login", action: .toggleLogin, indent: 0), on: true)]
keeper.sync(real, specs(then.lines) + footer)
let ids = { (m: NSMenu) -> [ObjectIdentifier] in m.items.map(ObjectIdentifier.init) }
let item = { (title: String) -> NSMenuItem? in real.items.first { $0.title == title } }
t("drawn: one item per line and per separator, each at its indent", real.items.count == specs(then.lines).count + 3
  && real.items.filter(\.isSeparatorItem).count == then.lines.filter(\.separatorBefore).count + 1
  && item("Open the report")?.indentationLevel == 2 && item("Example Tool — First Project")?.indentationLevel == 0, "\(real.items.map(\.title))")
t("...an action: sent to the app, carried with the item", item("Open Example Tool")?.action == #selector(Sink.act(_:)) && item("Open Example Tool")?.target === sink
  && (item("Open Example Tool")?.representedObject as? Box)?.action == .extOpen(ext: "example.first", url: "http://localhost:9411") && item("Open Example Tool")?.isEnabled == true)
t("...a line with none: shown, not enabled", item("3 of 8 items ready")?.isEnabled == false && item("3 of 8 items ready")?.action == nil)
t("...the footer's key and check mark", item("Open dashboard")?.keyEquivalent == "d" && item("Launch at login")?.state == .on)
let before = ids(real), sub = item("Items")?.submenu, subIds = sub.map(ids) ?? []
keeper.sync(real, specs(after.lines) + footer)
t("the next poll, its summary changed: every item the same object, the summary retitled in place",
  ids(real) == before && item("4 of 8 items ready") != nil && item("3 of 8 items ready") == nil, "\(real.items.map(\.title))")
t("...the submenu the same object, and its items (a submenu open under the pointer stays open)", sub != nil && item("Items")?.submenu === sub && ids(sub!) == subIds)
let crashed = snap(#"{"about": {"version": "1.2.3"}, "door": {"up": true, "queue": {}}, "maintenance": null, "lmstudio": {"families": [\#(fam)]}, "peers": [],"#
                   + #" "crashes": {"down": [{"family": "test/family", "crashed": 1799999500, "state": "loading again"}]}}"#)
keeper.sync(real, specs(glance(crashed, now: now, extensions: [ExtensionPage(ext: ex!, menu: later)]).lines) + footer)
t("a hub alert added at the top: one item inserted, every item after it the same object, the submenu with them",
  real.items.count == before.count + 1 && real.items[1].title.hasPrefix("test/family crashed") && Array(ids(real).dropFirst(2)) == Array(before.dropFirst(1))
  && item("Items")?.submenu === sub, "\(real.items.map(\.title))")
keeper.sync(real, specs(after.lines) + footer)
t("...and gone again: the item removed, the rest kept", ids(real) == before)

print("\(results.filter { $0 }.count) of \(results.count) ok")
exit(results.allSatisfy { $0 } ? 0 : 1)

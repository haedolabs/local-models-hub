// Menu: the menu-bar app's menu kept in place (AppKit). Each update sets only what changed, on the items already there
// (keep() in Glance.swift says which items stay), so a submenu open under the pointer stays open, the highlight stays
// put, and nothing redraws that did not change. app/tests checks it with real menu items.
import AppKit

/// One item of the menu: a separator, or a line with what only the app adds (a key equivalent, a check mark).
struct Spec {
    var line: Line? = nil                                 // nil: a separator
    var key = ""
    var on = false
    var kind: Kind { line == nil ? .separator : line!.children.isEmpty ? .line : .submenu }
}

/// A glance's lines as menu items, each separator an item of its own.
func specs(_ lines: [Line]) -> [Spec] { lines.flatMap { $0.separatorBefore ? [Spec(), Spec(line: $0)] : [Spec(line: $0)] } }

/// A menu item's action, carried as its representedObject.
final class Box: NSObject { let action: Action; init(_ a: Action) { action = a } }

/// Brings a menu to the items wanted, in place; every item with an action sends `selector` to `target`.
final class MenuKeeper {
    weak var target: AnyObject?
    let selector: Selector
    init(target: AnyObject, selector: Selector) { self.target = target; self.selector = selector }

    func sync(_ menu: NSMenu, _ want: [Spec]) {
        let have = menu.items.map { $0.isSeparatorItem ? Kind.separator : $0.submenu == nil ? Kind.line : Kind.submenu }
        let (head, tail) = keep(have, want.map(\.kind))
        for _ in head..<(have.count - tail) { menu.removeItem(at: head) }
        for (i, w) in want[head..<(want.count - tail)].enumerated() { menu.insertItem(make(w), at: head + i) }
        for (i, w) in want.enumerated() where i < head || i >= want.count - tail { set(menu.items[i], w) }
    }

    func make(_ w: Spec) -> NSMenuItem {
        guard w.line != nil else { return .separator() }
        let m = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        if w.kind == .submenu { let sub = NSMenu(); sub.autoenablesItems = false; m.submenu = sub }
        set(m, w)
        return m
    }

    /// One item set to its line, each property only when it differs (setting one redraws the item).
    func set(_ m: NSMenuItem, _ w: Spec) {
        guard let l = w.line else { return }
        if m.title != l.title { m.title = l.title }
        let tip: String? = l.tooltip.isEmpty ? nil : l.tooltip
        if m.toolTip != tip { m.toolTip = tip }
        if (m.representedObject as? Box)?.action != l.action { m.representedObject = Box(l.action) }
        let sel: Selector? = l.action == .none ? nil : selector
        if m.action != sel { m.action = sel }
        if m.target !== target { m.target = target }
        let enabled = l.action != .none || !l.children.isEmpty
        if m.isEnabled != enabled { m.isEnabled = enabled }
        if m.indentationLevel != drawnIndent(l) { m.indentationLevel = drawnIndent(l) }
        if m.keyEquivalent != w.key { m.keyEquivalent = w.key }
        let state: NSControl.StateValue = w.on ? .on : .off
        if m.state != state { m.state = state }
        if let sub = m.submenu { sync(sub, specs(l.children)) }
    }
}

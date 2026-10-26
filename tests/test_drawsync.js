// Drawing Sync JS tests - runs the SHIPPED logic, not a copy.
// It slices the marked DrawSync pure-logic block out of
// app/static/app.js and evaluates it, then asserts the v1 rules:
// create/edit/delete propagate to same-symbol panes only, symbol change
// hides/restores, reload dedupes, no echo loop, Trade Taker implemented
// copies refuse Implement, toggle OFF disables all propagation, and
// pre-existing unsynced drawings are never touched.
// Run: node tests/test_drawsync.js  (exit 0 = all pass)
"use strict";
const fs = require("fs");
const path = require("path");

const appJs = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "app.js"), "utf8");
const START = "/* === DrawSync pure logic (extracted by tests) === */";
const END = "/* === end DrawSync pure logic === */";
const i0 = appJs.indexOf(START), i1 = appJs.indexOf(END);
if (i0 < 0 || i1 < 0 || i1 <= i0) {
  console.error("FAIL: DrawSync pure-logic markers not found in app.js");
  process.exit(1);
}
const block = appJs.slice(i0 + START.length, i1);
const DrawSync = new Function(block + "\nreturn DrawSync;")();

let failures = 0;
function check(name, cond, extra) {
  if (cond) console.log("ok - " + name);
  else { failures++; console.log("FAIL - " + name + (extra !== undefined ? " (got " + JSON.stringify(extra) + ")" : "")); }
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function pane(id, symbol, drawings) {
  return { id: id, state: { symbol: symbol, drawings: drawings || [] } };
}
function trend(syncId, sym, p2p) {
  return { id: syncId, syncId: syncId, syncSymbol: sym, type: "trend",
           p1: { t: 100, p: 2700 }, p2: { t: 200, p: p2p || 2710 },
           style: { color: "#ff0000", width: 2 } };
}
function unsynced(id) {
  return { id: id, type: "hline", p1: { t: 50, p: 2690 }, p2: null };
}

// 1. create propagates to same-symbol panes only (renko pane included)
(function () {
  DrawSync.enabled = true;
  const a = pane(0, "XAUUSD"), b = pane(1, "XAUUSD"), c = pane(2, "EURUSD");
  const r = pane(3, "XAUUSD"); r.mode = "renko"; // same symbol, renko: included
  const panes = [a, b, c, r];
  const d = trend("d1", "XAUUSD");
  a.state.drawings.push(d);
  const touched = DrawSync.onCreate(panes, a, d);
  check("create touches same-symbol panes only",
    eq(touched.map(function (p) { return p.id; }), [1, 3]), touched.map(function (p) { return p.id; }));
  check("create copies geometry to peer", b.state.drawings.length === 1 &&
    eq(b.state.drawings[0].p2, { t: 200, p: 2710 }) && b.state.drawings[0].syncId === "d1");
  check("create copies to renko pane", r.state.drawings.length === 1 && r.state.drawings[0].syncId === "d1");
  check("create never touches different-symbol pane", c.state.drawings.length === 0);
  check("create copy is a deep copy, not a shared reference",
    b.state.drawings[0] !== d && b.state.drawings[0].p1 !== d.p1);
})();

// 2. create never doubles a peer that already has the drawing
(function () {
  DrawSync.enabled = true;
  const a = pane(0, "XAUUSD"), b = pane(1, "XAUUSD");
  const d = trend("d1", "XAUUSD");
  a.state.drawings.push(d);
  b.state.drawings.push(DrawSync.clone(d));
  const touched = DrawSync.onCreate([a, b], a, d);
  check("create skips peer that already has it", touched.length === 0 && b.state.drawings.length === 1);
})();

// 3. edit propagates geometry + style in place; unsynced untouched
(function () {
  DrawSync.enabled = true;
  const a = pane(0, "XAUUSD"), b = pane(1, "XAUUSD");
  const d = trend("d1", "XAUUSD");
  a.state.drawings.push(d);
  DrawSync.onCreate([a, b], a, d);
  const peerCopy = b.state.drawings[0];
  const local = unsynced("u1");
  b.state.drawings.push(local);
  d.p2 = { t: 300, p: 2720 }; d.style = { color: "#00ff00", width: 3 };
  const touched = DrawSync.onEdit([a, b], a, d);
  check("edit touches peer with the copy", touched.length === 1 && touched[0] === b);
  check("edit updates peer geometry + style", eq(peerCopy.p2, { t: 300, p: 2720 }) &&
    eq(peerCopy.style, { color: "#00ff00", width: 3 }));
  check("edit keeps peer object identity (selection/drag survive)", b.state.drawings[0] === peerCopy);
  check("edit keeps peer local id + sync identity", peerCopy.id === "d1" && peerCopy.syncId === "d1");
  check("edit never touches unsynced drawings", eq(local, unsynced("u1")));
})();

// 4. delete propagates, including a hidden copy on a different-symbol pane
(function () {
  DrawSync.enabled = true;
  const a = pane(0, "XAUUSD"), b = pane(1, "XAUUSD"), c = pane(2, "EURUSD");
  const d = trend("d1", "XAUUSD");
  a.state.drawings.push(d);
  DrawSync.onCreate([a, b], a, d);
  c.state.drawings.push(DrawSync.clone(d)); // hidden X copy held while on EURUSD
  c.state.drawings.push(unsynced("u9"));
  const touched = DrawSync.onDelete([a, b, c], a, "d1");
  check("delete removes peer copy", b.state.drawings.length === 0);
  check("delete removes hidden copy on other-symbol pane", c.state.drawings.length === 1 &&
    c.state.drawings[0].id === "u9");
  check("delete touches both peers", touched.length === 2);
  check("delete with no syncId is a no-op", DrawSync.onDelete([a, b], a, undefined).length === 0);
})();

// 5. symbol change hides/restores + pulls drawings created while away
(function () {
  DrawSync.enabled = true;
  const d = trend("d1", "XAUUSD");
  check("synced drawing hidden on other symbol", DrawSync.isVisible(d, "EURUSD") === false);
  check("synced drawing visible on its symbol", DrawSync.isVisible(d, "XAUUSD") === true);
  check("unsynced drawing always visible", DrawSync.isVisible(unsynced("u1"), "EURUSD") === true);
  const a = pane(0, "EURUSD"); // pane returning to XAUUSD, missing d
  const b = pane(1, "XAUUSD", [DrawSync.clone(d)]);
  const touched = DrawSync.onSymbolChange([a, b], a, "XAUUSD");
  check("symbol change pulls missing synced drawing", a.state.drawings.length === 1 &&
    a.state.drawings[0].syncId === "d1");
  check("symbol change reports the pane as touched", touched.length === 1 && touched[0] === a);
  const touched2 = DrawSync.onSymbolChange([a, b], a, "XAUUSD");
  check("symbol change never doubles on repeat", touched2.length === 0 && a.state.drawings.length === 1);
})();

// 6. reload dedupes (no doubles), unsynced kept
(function () {
  const d = trend("d1", "XAUUSD");
  const out = DrawSync.dedupe([DrawSync.clone(d), unsynced("u1"), DrawSync.clone(d),
    DrawSync.clone(d), unsynced("u2")]);
  check("dedupe keeps first copy only", out.length === 3 &&
    out.filter(function (x) { return x.syncId === "d1"; }).length === 1);
  check("dedupe keeps every unsynced drawing", out.filter(function (x) { return !x.syncId; }).length === 2);
})();

// 7. no echo loop: re-firing propagation from a peer copy is a no-op / converges
(function () {
  DrawSync.enabled = true;
  const a = pane(0, "XAUUSD"), b = pane(1, "XAUUSD");
  const panes = [a, b];
  const d = trend("d1", "XAUUSD");
  a.state.drawings.push(d);
  DrawSync.onCreate(panes, a, d); // real wiring: peers updated by state surgery only
  const echo = DrawSync.onCreate(panes, b, b.state.drawings[0]); // hypothetical re-fire
  check("echo re-fire creates nothing", echo.length === 0 &&
    a.state.drawings.length === 1 && b.state.drawings.length === 1);
  d.p2 = { t: 400, p: 2730 };
  DrawSync.onEdit(panes, a, d);
  const back = DrawSync.onEdit(panes, b, b.state.drawings[0]); // echo back
  check("edit echo converges to identical state",
    eq(a.state.drawings[0].p2, { t: 400, p: 2730 }) && eq(b.state.drawings[0].p2, { t: 400, p: 2730 }) &&
    eq(a.state.drawings[0], b.state.drawings[0]));
  check("edit echo touches only the peer that holds a copy", back.length === 1 && back[0] === a);
})();

// 8. Trade Taker: implemented state + ticket propagate, so copies refuse Implement
(function () {
  DrawSync.enabled = true;
  const a = pane(0, "XAUUSD"), b = pane(1, "XAUUSD");
  const tt = { id: "tt1", syncId: "tt1", syncSymbol: "XAUUSD", type: "tradetaker",
               p1: { t: 100, p: 2700 }, p2: { t: 100, p: 2696 }, p3: { t: 100, p: 2704 },
               risk: 20, qty: 0.05, qtyRisk: 20, orderType: "market",
               implemented: false, ticket: null };
  a.state.drawings.push(tt);
  DrawSync.onCreate([a, b], a, tt);
  tt.implemented = true; tt.ticket = 12345678; // order placed in pane A
  DrawSync.onEdit([a, b], a, tt);
  const copy = b.state.drawings[0];
  check("implemented state propagates to copy", copy.implemented === true);
  check("ticket propagates to copy", copy.ticket === 12345678);
  // TradeTaker.implement's shipped guard is `if (!d || d.implemented) ... "Already live"`.
  // A copy carrying implemented=true therefore refuses a second order.
  check("implemented copy trips the shipped Already-live guard", copy.implemented === true);
  // post-Implement drag: TP (p3) moves on a copy, geometry follows, order state untouched
  copy.p3 = { t: 100, p: 2708 };
  DrawSync.onEdit([a, b], b, copy);
  check("post-Implement drag updates drawing on peers", eq(a.state.drawings[0].p3, { t: 100, p: 2708 }));
  check("post-Implement drag never clears implemented/ticket",
    a.state.drawings[0].implemented === true && a.state.drawings[0].ticket === 12345678);
})();

// 9. toggle OFF disables all propagation (panes behave exactly as before)
(function () {
  DrawSync.enabled = false;
  const a = pane(0, "XAUUSD"), b = pane(1, "XAUUSD");
  const d = trend("d1", "XAUUSD");
  a.state.drawings.push(d);
  check("toggle OFF blocks create", DrawSync.onCreate([a, b], a, d).length === 0 &&
    b.state.drawings.length === 0);
  b.state.drawings.push(DrawSync.clone(d));
  d.p2 = { t: 300, p: 2720 };
  check("toggle OFF blocks edit", DrawSync.onEdit([a, b], a, d).length === 0 &&
    eq(b.state.drawings[0].p2, { t: 200, p: 2710 }));
  check("toggle OFF blocks delete", DrawSync.onDelete([a, b], a, "d1").length === 0 &&
    b.state.drawings.length === 1);
  const c = pane(2, "EURUSD");
  check("toggle OFF blocks symbol-change pull", DrawSync.onSymbolChange([a, b, c], c, "XAUUSD").length === 0 &&
    c.state.drawings.length === 0);
  DrawSync.enabled = true;
})();

// 10. migration: pre-existing per-pane drawings (no syncId) are never synced
(function () {
  DrawSync.enabled = true;
  const a = pane(0, "XAUUSD", [unsynced("u1")]), b = pane(1, "XAUUSD", [unsynced("u2")]);
  const panes = [a, b];
  check("unsynced create is not propagated",
    DrawSync.onCreate(panes, a, a.state.drawings[0]).length === 0 && b.state.drawings.length === 1);
  check("unsynced edit is not propagated",
    DrawSync.onEdit(panes, a, a.state.drawings[0]).length === 0);
  check("unsynced drawings survive untouched", a.state.drawings.length === 1 && b.state.drawings.length === 1);
})();

if (failures) { console.error(failures + " FAILURES"); process.exit(1); }
console.log("all DrawSync checks passed");

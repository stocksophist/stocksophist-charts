// Trade Taker JS regression tests - runs the SHIPPED logic, not a copy.
// It slices the marked TradeTaker pure-logic block out of
// app/static/app.js and evaluates it, then asserts the user's rules:
// direction, order type, the qty floor rule (20/4 and 23/4 -> 0.05),
// RR math, TP side validation, too-small-risk block.
// Run: node tests/test_v344_tradetaker.js  (exit 0 = all pass)
"use strict";
const fs = require("fs");
const path = require("path");

const appJs = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "app.js"), "utf8");
const START = "/* === TradeTaker pure logic (extracted by tests) === */";
const END = "/* === end TradeTaker pure logic === */";
const i0 = appJs.indexOf(START), i1 = appJs.indexOf(END);
if (i0 < 0 || i1 < 0 || i1 <= i0) {
  console.error("FAIL: TradeTaker pure-logic markers not found in app.js");
  process.exit(1);
}
const block = appJs.slice(i0 + START.length, i1);
const TradeTaker = new Function(block + "\nreturn TradeTaker;")();

let failures = 0;
function check(name, cond, extra) {
  if (cond) console.log("ok - " + name);
  else { failures++; console.log("FAIL - " + name + (extra !== undefined ? " (got " + JSON.stringify(extra) + ")" : "")); }
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// direction
check("direction buy", TradeTaker.directionOf(2700, 2696) === "buy");
check("direction sell", TradeTaker.directionOf(2700, 2704) === "sell");
check("direction none at equal", TradeTaker.directionOf(2700, 2700) === null);
// order type
check("buy near ask -> market",
  TradeTaker.decideOrderType("buy", 2700.05, 2699.95, 2700.05, 0.01, 0).type === "market");
check("buy below ask -> buy_limit",
  TradeTaker.decideOrderType("buy", 2690, 2699.95, 2700.05, 0.01, 0).type === "buy_limit");
check("buy above ask -> buy_stop",
  TradeTaker.decideOrderType("buy", 2710, 2699.95, 2700.05, 0.01, 0).type === "buy_stop");
check("sell above bid -> sell_limit",
  TradeTaker.decideOrderType("sell", 2710, 2699.95, 2700.05, 0.01, 0).type === "sell_limit");
check("sell below bid -> sell_stop",
  TradeTaker.decideOrderType("sell", 2690, 2699.95, 2700.05, 0.01, 0).type === "sell_stop");
check("stops level forces market",
  TradeTaker.decideOrderType("buy", 2700.15, 2699.95, 2700.05, 0.01, 20).type === "market");
check("no quote -> null type",
  TradeTaker.decideOrderType("buy", 2700, null, null, 0.01, 0).type === null);
// qty floor - the user's own examples ($4.00 SL distance)
const q20 = TradeTaker.calcQty(20, 2700, 2696, 0.01, 1.0);
check("risk 20 / 4-pt SL -> 0.05", eq([q20.qty, q20.qtyRisk], [0.05, 20]), q20);
const q23 = TradeTaker.calcQty(23, 2700, 2696, 0.01, 1.0);
check("risk 23 / 4-pt SL -> 0.05 (not 0.06)", eq([q23.qty, q23.qtyRisk], [0.05, 20]), q23);
const qSmall = TradeTaker.calcQty(3, 2700, 2696, 0.01, 1.0);
check("risk 3 blocked", qSmall.qty === null && /too small/.test(qSmall.error), qSmall);
let neverAbove = true;
[1, 4, 4.5, 7.99, 8, 11.5, 100, 1000].forEach(function (r) {
  const q = TradeTaker.calcQty(r, 2700, 2696, 0.01, 1.0);
  if (q.qty !== null && q.qtyRisk > r) neverAbove = false;
});
check("qty risk never exceeds budget", neverAbove);
// RR
check("RR 2.0 buy", TradeTaker.calcRR(2700, 2696, 2708) === 2.0);
check("RR 2.0 sell", TradeTaker.calcRR(2700, 2704, 2692) === 2.0);
// plan
const p = TradeTaker.plan(2700, 2696, 2708, 20, { bid: 2699.95, ask: 2700.05 },
  { tickSize: 0.01, tickValue: 1.0, point: 0.01, stopsLevel: 0 });
check("plan ok", p.ok && p.direction === "buy" && p.qty === 0.05 && p.rr === 2.0 && p.orderType === "market", p);
const badTp = TradeTaker.plan(2700, 2696, 2690, 20, { bid: 2699.95, ask: 2700.05 },
  { tickSize: 0.01, tickValue: 1.0, point: 0.01, stopsLevel: 0 });
check("plan rejects wrong-side TP", !badTp.ok && /profit side/.test(badTp.message), badTp);
const noQuote = TradeTaker.plan(2700, 2696, 2708, 20, null,
  { tickSize: 0.01, tickValue: 1.0, point: 0.01, stopsLevel: 0 });
check("plan without quote still computes qty", noQuote.ok && noQuote.qty === 0.05 && noQuote.orderType === null, noQuote);

if (failures) { console.log(failures + " FAILURES"); process.exit(1); }
console.log("all TradeTaker JS checks passed");

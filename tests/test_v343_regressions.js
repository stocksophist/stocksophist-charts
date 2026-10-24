// v3.4.43 regression checks (Node, no deps, no browser).
//
// These tests extract the SHIPPED function source from app/static/app.js
// (brace-matched, not a copy) and run it against stub chart/series objects
// that mimic lightweight-charts' exact-match timeToCoordinate behaviour
// (null unless the time exactly equals a bar time - see the library's
// timeToIndex with findNearest=false).
//
// Run: node tests/test_v343_regressions.js   (from the repo root)
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const appJs = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "app.js"), "utf8");

// Extract a brace-delimited block starting at `startIdx` (position of the
// opening brace). Returns the source INCLUDING the braces.
function extractBlock(src, startIdx) {
  let depth = 0, i = startIdx, inStr = null, esc = false;
  for (; i < src.length; i++) {
    const c = src[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === inStr) inStr = null;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") { inStr = c; continue; }
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) return src.slice(startIdx, i + 1); }
  }
  throw new Error("unbalanced braces");
}

function extractTopFunction(src, name) {
  const idx = src.indexOf("function " + name + "(");
  assert(idx >= 0, name + " not found");
  const brace = src.indexOf("{", idx);
  return src.slice(idx, brace) + extractBlock(src, brace);
}

function extractMethod(src, name) {
  const marker = "ChartPane.prototype." + name + " = function";
  const idx = src.indexOf(marker);
  assert(idx >= 0, marker + " not found");
  const brace = src.indexOf("{", idx);
  // methods end with "};" - extractBlock returns "{...}", then expect ";"
  return "ChartPane.prototype." + name + " = function" +
    src.slice(src.indexOf("(", idx), brace) + extractBlock(src, brace) + ";";
}

// ---- build the harness: a fake ChartPane with the real methods ----
const ChartPane = { prototype: {} };
let replayCenterRange; // strict-mode eval cannot leak a declaration, so bind it
const harnessSrc = [
  extractTopFunction(appJs, "replayCenterRange")
    .replace(/^function replayCenterRange/, "replayCenterRange = function"),
  extractMethod(appJs, "timeX"),
  extractMethod(appJs, "_interpX"),
  extractMethod(appJs, "priceY"),
  extractMethod(appJs, "px"),
].join("\n");
eval(harnessSrc); // defines replayCenterRange + ChartPane.prototype.* in scope
// candles-mode renkoSlot is the identity (mode !== "renko" path)
ChartPane.prototype.renkoSlot = function (t) { return t; };

function makePane(barTimes) {
  let libCalls = 0, priceCalls = 0;
  const exact = new Set(barTimes);
  const pane = Object.create(ChartPane.prototype);
  pane.seriesData = barTimes.map((t, i) => ({ time: t, close: 100 + i }));
  pane._pxCacheX = {}; pane._pxCacheY = {};
  pane.chart = {
    timeScale: () => ({
      // mimic the library: exact match or null
      timeToCoordinate: (t) => { libCalls++; return exact.has(t) ? t * 10 : null; },
    }),
  };
  pane.series = { priceToCoordinate: (p) => { priceCalls++; return p * 2; } };
  pane.counters = () => ({ libCalls, priceCalls });
  return pane;
}

// ---- Bug 1: cross-timeframe drawing times ---------------------------
// 1m-grid bars; a drawing plotted on a 3m grid has times like 1090
// (not a 1m bar) and times older/newer than the loaded window.
{
  const pane = makePane([1000, 1060, 1120, 1180, 1240]);
  // exact bar time: library fast path, unchanged behaviour
  assert.strictEqual(pane.timeX(1120), 11200);
  // between bars (3m-grid time on a 1m series): OLD code returned null
  // (library exact-match), NEW code interpolates: halfway 1060->1120
  assert.strictEqual(pane.timeX(1090), 10900);
  // before the first bar: extrapolate with edge spacing (60s -> 600px)
  assert.strictEqual(pane.timeX(940), 9400);
  // after the last bar: extrapolate
  assert.strictEqual(pane.timeX(1300), 13000);
  // px() combines both and never returns null for a mappable time
  assert.deepStrictEqual(pane.px(1090, 50), { x: 10900, y: 100 });
  // fewer than 2 bars: cannot interpolate, honest null
  const thin = makePane([1000]);
  assert.strictEqual(thin.timeX(1090), null);
  console.log("bug1 timeX interpolation/extrapolation: OK");
}
// ---- Bug 1 + Bug 3: per-frame memo collapses repeated lookups -------
{
  const pane = makePane([1000, 1060, 1120, 1180, 1240]);
  pane.timeX(1090); pane.timeX(1090); pane.timeX(1090);
  pane.px(1090, 50); pane.px(1090, 50);
  const c = pane.counters();
  // first timeX(1090): 1 exact-miss + 2 bracketing-bar lookups = 3;
  // the other 4 calls are served from the per-frame memo = 0 extra.
  // price 50 looked up once despite 2 px() calls.
  assert.strictEqual(c.libCalls, 3, "libCalls=" + c.libCalls);
  assert.strictEqual(c.priceCalls, 1, "priceCalls=" + c.priceCalls);
  // control: same 5 calls with the memo disabled cost 5 x 3 = 15
  const nocache = makePane([1000, 1060, 1120, 1180, 1240]);
  nocache._pxCacheX = null; nocache._pxCacheY = null;
  nocache.timeX(1090); nocache.timeX(1090); nocache.timeX(1090);
  nocache.px(1090, 50); nocache.px(1090, 50);
  assert.strictEqual(nocache.counters().libCalls, 15);
  assert.strictEqual(nocache.counters().priceCalls, 2);
  console.log("per-frame memo (zoom cost): OK", JSON.stringify(c));
}
// ---- Bug 2: replay centering ----------------------------------------
{
  // normal case: last bar exactly centered in a 178-wide window
  let r = replayCenterRange(199, 89);
  assert.deepStrictEqual(r, { from: 110, to: 288 });
  assert.strictEqual((r.from + r.to) / 2, 199);
  // old behaviour for comparison: {from: 30, to: 207} put bar at 93% right
  const old = { from: Math.max(0, 200 - 170), to: 199 + 8 };
  assert.strictEqual((199 - old.from) / (old.to - old.from) > 0.9, true);
  // early replay: left edge clamps at 0, bar as close to center as possible
  r = replayCenterRange(50, 89);
  assert.deepStrictEqual(r, { from: 0, to: 139 });
  r = replayCenterRange(0, 89);
  assert.deepStrictEqual(r, { from: 0, to: 89 });
  console.log("bug2 replayCenterRange: OK");
}
console.log("ALL v3.4.43 REGRESSION CHECKS PASSED");

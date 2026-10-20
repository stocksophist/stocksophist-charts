# Development

## Running the test suite

The test suite is `app/test_indicators.py` — `unittest`, no network.
Run it from the `app/` directory:

```sh
cd app
python -m unittest test_indicators -v
```

It covers textbook indicator formulas (hand-computed values, independent
of the module under test) plus the indicator catalogue (factory defaults,
overlay flags, settings-API pure helpers). The suite covers only the
standard indicators.

There is no CI. Run the tests before every release, and run a live boot
test (start the server, check `/api/version`, `/api/indicators`, and
one chart load) before shipping.

## Running the server in dev

```sh
python app/server.py
```

Useful environment variables for dev (see
[docs/CONFIGURATION.md](CONFIGURATION.md) for the full list):

```sh
STOCKSOPHIST_CHART_PORT=5001 STOCKSOPHIST_NO_BROWSER=1 python app/server.py
```

- The server binds `127.0.0.1` on the configured port (default 5000).
- On Windows it opens the chart in the browser automatically unless
  `STOCKSOPHIST_NO_BROWSER=1`.
- `STOCKSOPHIST_NO_AUTO_MT5=1` skips the MT5 auto-connect at startup
  (useful on machines without MT5).
- The server writes runtime files to the data directory
  (`C:\stocksophist\cache` on Windows,
  `~/.stocksophist_chart_cache` elsewhere), never into the code tree.

## Adding a new indicator

An indicator is one Python file in `app/indicators/` (built-in) or a
script in the data directory's `indicators/custom/` folder (user
script). Every indicator module must define exactly three things:

```python
META = {
    "id": "my_indicator",          # 2-40 chars: lowercase letters, digits, _
    "name": "My Indicator",
    "author": "StockSophist",
    "description": "One-line description. Reference only - never signals or alerts.",
    "overlay": False,             # True = draws on the price chart, False = own pane
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int",
     "default": 14, "min": 2, "max": 100, "group": "Inputs"},
    {"key": "color", "label": "Line color", "type": "color",
     "default": "#2962FF", "group": "Style"},
    # Input types: int, float, bool, color, options (with an "options" list).
    # "group" is "Inputs" or "Style" (other groups are ignored by the UI).
]

def compute(ctx):
    bars = ctx["bars"]            # pandas DataFrame: time, open, high, low, close
    inp = ctx["inputs"]           # merged settings: factory defaults + saved overrides
    # ... compute ...
    return {"lines": [...], "dashboard": None, "sweeps": [], "values": {}}
```

Rules for `compute(ctx)`:

- `ctx` also provides `symbol`, `tf`, `minutes`, and `get_bars(tf)`
  for fetching another timeframe's bars.
- Return `lines`: a list of segments, each
  `{"t0", "t1", "p0", "p1", "color", "width", "style", "label",
  "label_size"}`. Use `"style": "Solid" | "Dashed" | "Dotted"`.
- Return `dashboard`: `None`, or a dashboard dict
  `{"title", "rows": [{"text", "color"}], "position", "size", "bg",
  "fg"}` for the latest-value box.
- Return `sweeps`: a list (empty for standard indicators).
- Return `values`: a dict of the latest computed values, e.g.
  `{"my_indicator14": 42.0}`.
- Compute on closed bars only. Never signal, never alert, never trade.
  Return `{"lines": [], "dashboard": None, "sweeps": [], "values": {}}`
  when there are no bars.
- Keep the module self-contained: no imports from other indicator files,
  no network, no file access, no global state. Use only the standard
  library + pandas.

Validation (in `indicators/__init__.py`):

- `META` must be a dict with `id` and `name`. `META id` must be 2–40
  chars: lowercase letters, digits, underscore.
- `INPUTS` must be a list. Each entry needs `key`, `label`, `type`,
  `default`; `options` entries need an `options` list.
- `compute` must be callable.
- A module that fails validation is reported in the catalogue's
  `errors` list (`GET /api/indicators`) instead of breaking the app.
- For user scripts added via `POST /api/indicators/add`, the script's
  `META id` must match the requested `id`.

To add a test for the new indicator, follow the pattern in
`app/test_indicators.py`: build a context with `_ind_ctx([...])`,
call `compute`, and assert hand-computed values (computed independently
of the module under test).

## Comment and style rules

These rules keep the public codebase clean. They apply to every file
in the repository, including comments, docstrings, and user-visible
strings.

- **English only.** Comments, docstrings, log messages, error strings,
  and UI text are English. No Hinglish, no other languages.
- **No personal data.** No names, emails, phones, tokens, channel IDs,
  account numbers, or machine-specific paths. The only path-like values
  allowed are generic install paths (`C:\stocksophist\cache`,
  `~/.stocksophist_chart_cache`) and the project's own layout.
- **No history notes.** Do not write changelogs, version notes, or
  "previously / used to / no longer" narratives in comments. Describe
  what the code does now, in the present tense.
- **No removed-feature references.** Do not mention features that were
  removed (AI, custom signal indicators, webhook relay, or any
  brand/integration that no longer exists). If a comment would need to
  explain a removal, delete the comment instead.
- **No secrets.** Never commit tokens, passwords, or keys. Never log
  them. Never print them in error messages.
- **Docstrings describe behavior.** A module docstring says what the
  module does and its safety rules (if any). A function docstring says
  what it returns and what it refuses. Keep them short and factual.

## Making a release ZIP

A release is a versioned folder + ZIP containing the app, not the
runtime state:

**Goes in:**

- `app/` — the full application tree (server, feeds, indicators,
  static UI, tests).
- `requirements.txt`
- `start_chart.bat`, `stop_chart.bat`

**Stays out (never in a release ZIP):**

- `settings.json` — user settings (lives in the data directory).
- `*.db` — databases (tick vault, history DB, backups).
- `*.log`, `*.tmp` — logs and temp files.
- `__pycache__/`, `.pytest_cache/` — bytecode and test caches.
- `*.md` inside `app/` — no markdown files in the app tree.
- Any file under the data directory (settings, alerts, caches,
  `ticks.db`, tones, indicator cache). The ZIP is code only.
- Tokens, passwords, keys, channel IDs, personal data — never in any
  file that ships.

**Release checklist:**

1. Bump the version in `server.py` (`VERSION`) and in the frontend
   (`APP_VERSION` in `app.js`) to the same value. A mismatch shows a
   version-mismatch banner.
2. Run the test suite: `python -m unittest test_indicators -v` from
   `app/`.
3. Boot-test from the ZIP (extract to a fresh folder, fake HOME, start
   the server): check `/api/version`, `/api/indicators` (17 ids, no
   errors), the `/` page title, and that removed endpoints return 404.
4. Verify the ZIP contains no `.md` in `app/`, no `settings.json`, no
   `.db`, no logs, no `__pycache__`, and an empty `app/data/` (if the
   folder exists at all).
5. Name the ZIP with the new version. Never overwrite an old release.

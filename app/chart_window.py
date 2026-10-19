"""StockSophist Charts - dedicated lightweight window.

Runs the chart server as a SEPARATE process and shows it in a small
dedicated browser window (pywebview, backed by Windows WebView2 - no
Chrome needed, no browser-cache mismatch banners, no extra tabs).
Falls back to the system browser if the embedded window cannot start
(e.g. WebView2 missing).

the server is a subprocess, not an in-process thread. A hung
MT5 call inside the server cannot freeze the whole window (shared
process); the window stays responsive no matter what the server
is doing - exactly like the system-browser version.

Usage: python app\\chart_window.py (or start_chart_window.bat)
"""
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(APP_DIR, "server.py")

PORT = int(os.environ.get("STOCKSOPHIST_CHART_PORT", "5000") or 5000)
URL = "http://127.0.0.1:%d/" % PORT


def _wait_for_server(proc, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            print("[chart] server process exited (code %s) - see the "
                  "server output above." % proc.poll(), flush=True)
            return False
        try:
            with urllib.request.urlopen(URL + "api/version",
                                        timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    # The server gets its own process: its PID file keeps stop_chart.bat
    # working, and STOCKSOPHIST_NO_BROWSER stops it opening a stray browser tab.
    env = dict(os.environ)
    env["STOCKSOPHIST_NO_BROWSER"] = "1"
    try:
        with open(SERVER_PY, "rb") as f:
            pass
    except OSError:
        print("[chart] cannot find %s" % SERVER_PY, flush=True)
        return 1
    # Show which chart version this window serves.
    version = "?"
    try:
        with open(SERVER_PY, "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line.startswith("VERSION"):
                    version = line.split('"')[1]
                    break
    except Exception:
        pass
    proc = subprocess.Popen(
        [sys.executable, SERVER_PY],
        cwd=os.path.dirname(APP_DIR),
        env=env,
    )
    if not _wait_for_server(proc):
        print("[chart] server did not answer on %s - opening system "
              "browser anyway." % URL, flush=True)
        try:
            webbrowser.open(URL)
        finally:
            if proc.poll() is None:
                proc.terminate()
        return 1
    print("[chart] server up - opening dedicated chart window.", flush=True)
    try:
        import webview
    except Exception as e:
        print("[chart] embedded window unavailable (%s); using system "
              "browser instead." % e, flush=True)
        webbrowser.open(URL)
        # Keep serving until the user closes this console / stops the server.
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return 0
    try:
        webview.create_window(
            "StockSophist Charts v%s" % version, URL,
            width=1440, height=900, resizable=True)
        webview.start()
    except Exception as e:
        print("[chart] embedded window failed (%s); using system browser."
              % e, flush=True)
        webbrowser.open(URL)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return 0
    finally:
        # Closing the chart window stops the server too.
        if proc.poll() is None:
            proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())

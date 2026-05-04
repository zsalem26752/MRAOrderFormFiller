"""
dashboard.py — MR. AWNINGS · Order Form Filler Dashboard

Three tabs in one place:
  • Overview      — run history, next scheduled runs, Run Now button
  • Live Run      — watch the agent work in real time with live log streaming
                    + side-by-side invoice (Zoho) and filled order form PDFs
  • Past Runs     — browse saved runs, see which orders were filled/failed

Usage:
    python3 dashboard.py
Then open: http://127.0.0.1:5003
"""

import os, json, sys, time, threading, webbrowser, subprocess
from datetime import datetime, timedelta
from flask import Flask, Response, jsonify, request, render_template_string, send_file

try:
    from zoneinfo import ZoneInfo
    SCHED_TZ = ZoneInfo("America/New_York")
except ImportError:
    import pytz
    SCHED_TZ = pytz.timezone("America/New_York")

app = Flask(__name__)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH  = os.path.join(BASE_DIR, "run_history.json")
AGENT_PATH    = os.path.join(BASE_DIR, "agent.py")
SCHED_HOURS   = [9, 21]

sys.path.insert(0, BASE_DIR)

# ── Live run state ─────────────────────────────────────────────────────────────
_live_lock     = threading.Lock()
_live_events   = []      # list of {"type": str, "data": dict}
_live_active   = False
_live_pdfs     = {}      # po_number → {"invoice_pdf": bytes|None, "form_pdf_path": str|None}
_live_zoho     = None    # ZohoClient instance (created at run time)


def _live_push(event_type: str, data: dict):
    with _live_lock:
        _live_events.append({"type": event_type, "data": data})


def _event_sink(event_type: str, data: dict):
    """Called by agent.run_agent() for every event during a live run."""
    global _live_pdfs, _live_zoho

    _live_push(event_type, data)

    # When a PDF is successfully filled, try to pre-fetch the Zoho invoice PDF
    if event_type == "pdf_ready":
        po  = data.get("po_number")
        pdf_path = data.get("pdf_path")
        if po not in _live_pdfs:
            _live_pdfs[po] = {"invoice_pdf": None, "form_pdf_path": None}
        _live_pdfs[po]["form_pdf_path"] = pdf_path

        # Fetch Zoho invoice PDF in background
        if _live_zoho and po:
            def _fetch_invoice(po_num):
                try:
                    pdf_bytes = _live_zoho.get_invoice_pdf(po_num)
                    with _live_lock:
                        if po_num in _live_pdfs:
                            _live_pdfs[po_num]["invoice_pdf"] = pdf_bytes
                    _live_push("invoice_pdf_ready", {"po_number": po_num})
                except Exception:
                    pass
            threading.Thread(target=_fetch_invoice, args=(po,), daemon=True).start()


def _run_live(source: str = "manual"):
    global _live_active, _live_events, _live_pdfs, _live_zoho

    with _live_lock:
        _live_active = True
        _live_events = []
        _live_pdfs   = {}

    try:
        from config import Config
        from zoho_client import ZohoClient

        try:
            cfg = Config()
            _live_zoho = ZohoClient(cfg)
        except Exception:
            _live_zoho = None

        import agent
        agent.run_agent(source=source, event_sink=_event_sink)
    finally:
        with _live_lock:
            _live_active = False
            _live_zoho   = None


# ── History helpers ────────────────────────────────────────────────────────────

def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH) as f:
            raw = json.load(f)
    except Exception:
        return []
    normalised = []
    for entry in raw:
        raw_filled = entry.get("filled") or []
        raw_failed = entry.get("failed") or []

        filled = []
        for item in raw_filled:
            if isinstance(item, str):
                filled.append({"name": item, "model": "", "pdf_path": ""})
            else:
                filled.append({
                    "name":     item.get("name", ""),
                    "model":    item.get("model", ""),
                    "pdf_path": item.get("pdf_path", ""),
                })

        failed = []
        for item in raw_failed:
            if isinstance(item, dict):
                reason = item.get("reason") or item.get("reasons", [""])
                if isinstance(reason, list):
                    reason = "; ".join(reason)
                failed.append({
                    "name":   item.get("name", ""),
                    "reason": reason,
                })
            elif isinstance(item, str):
                failed.append({"name": item, "reason": ""})

        normalised.append({
            "timestamp": entry.get("timestamp", ""),
            "source":    entry.get("source", "cron"),
            "filled":    filled,
            "failed":    failed,
            "note":      entry.get("note", ""),
        })
    return normalised


def next_scheduled_runs(count=3):
    now = datetime.now(SCHED_TZ)
    results = []
    for day_offset in range(8):
        for hour in sorted(SCHED_HOURS):
            dt = now.replace(hour=hour, minute=0, second=0, microsecond=0) \
                 + timedelta(days=day_offset)
            if dt > now:
                results.append(dt.isoformat())
                if len(results) == count:
                    return results
    return results


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/history")
def api_history():
    return jsonify(load_history())

@app.route("/api/schedule")
def api_schedule():
    return jsonify(next_scheduled_runs(3))

@app.route("/api/status")
def api_status():
    return jsonify({"running": _live_active})

@app.route("/api/run", methods=["POST"])
def api_run():
    global _live_active
    with _live_lock:
        if _live_active:
            return jsonify({"ok": False, "message": "Agent is already running"}), 409
    threading.Thread(target=_run_live, args=("manual",), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/events")
def api_events():
    """SSE stream of live run events."""
    def generate():
        sent = 0
        while True:
            with _live_lock:
                events = list(_live_events)
                active = _live_active
            while sent < len(events):
                ev = events[sent]
                sent += 1
                yield "event: {}\ndata: {}\n\n".format(ev["type"], json.dumps(ev["data"]))
            # End stream once run is done and all events sent
            if not active and sent >= len(events):
                break
            time.sleep(0.25)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/live/pdfs")
def api_live_pdfs():
    """Return list of PO numbers that have PDFs ready."""
    with _live_lock:
        return jsonify({
            po: {
                "has_invoice": bool(info.get("invoice_pdf")),
                "has_form":    bool(info.get("form_pdf_path") and os.path.exists(info["form_pdf_path"])),
                "form_path":   info.get("form_pdf_path", ""),
            }
            for po, info in _live_pdfs.items()
        })

@app.route("/pdf/invoice/<po>")
def pdf_invoice(po):
    with _live_lock:
        pdf = _live_pdfs.get(po, {}).get("invoice_pdf")
    if not pdf:
        return "Invoice PDF not yet available", 404
    return Response(pdf, mimetype="application/pdf")

@app.route("/pdf/form/<path:encoded_path>")
def pdf_form(encoded_path):
    # encoded_path is URL-encoded actual filesystem path; use po to look it up
    return "Use /pdf/form/po/<po> instead", 400

@app.route("/pdf/form/po/<po>")
def pdf_form_po(po):
    with _live_lock:
        path = _live_pdfs.get(po, {}).get("form_pdf_path")
    if not path or not os.path.exists(path):
        return "Order form PDF not found", 404
    with open(path, "rb") as f:
        return Response(f.read(), mimetype="application/pdf")

@app.route("/pdf/history/<int:run_idx>/<int:order_idx>")
def pdf_history_form(run_idx, order_idx):
    """Serve a filled PDF from a past run by its path stored in run_history.json."""
    history = load_history()
    if run_idx >= len(history):
        return "Run not found", 404
    run = history[run_idx]
    filled = run.get("filled", [])
    if order_idx >= len(filled):
        return "Order not found", 404
    path = filled[order_idx].get("pdf_path", "")
    if not path or not os.path.exists(path):
        return "PDF file not found on disk", 404
    with open(path, "rb") as f:
        return Response(f.read(), mimetype="application/pdf")


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR. AWNINGS · Order Form Filler</title>
<style>
:root {
  --bg:      #0f1117;
  --surface: #1a1d27;
  --surface2:#20253a;
  --border:  #2a2d3a;
  --accent:  #4f8ef7;
  --pass:    #22c55e;
  --fail:    #ef4444;
  --flag:    #f59e0b;
  --muted:   #8892a4;
  --text:    #e2e8f0;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg); color: var(--text);
  min-height: 100vh;
}
body.live-mode  { height: 100vh; overflow: hidden; display: flex; flex-direction: column; }
body.past-mode  { height: 100vh; overflow: hidden; display: flex; flex-direction: column; }

/* ── Header ── */
header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 20px; background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0; z-index: 20; position: relative;
}
.header-left { display: flex; align-items: center; gap: 14px; }
header h1 { font-size: 14px; font-weight: 700; letter-spacing: .03em; white-space: nowrap; }
header h1 em { color: var(--accent); font-style: normal; }
#status-indicator {
  display: flex; align-items: center; gap: 7px;
  font-size: 11px; font-weight: 600; letter-spacing: .07em;
  text-transform: uppercase; color: var(--muted);
}
#status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--muted); flex-shrink: 0; transition: background .3s;
}
#status-dot.running {
  background: var(--pass);
  animation: pulse-ring 1.4s ease-out infinite;
}
@keyframes pulse-ring {
  0%   { box-shadow: 0 0 0 0 rgba(34,197,94,.55); }
  70%  { box-shadow: 0 0 0 7px rgba(34,197,94,0); }
  100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
}

/* ── Tabs ── */
#tab-bar {
  display: flex; padding: 0 20px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  gap: 0; flex-shrink: 0;
}
.tab-btn {
  padding: 10px 18px; font-size: 12px; font-weight: 600;
  color: var(--muted); border: none; background: transparent;
  cursor: pointer; border-bottom: 2px solid transparent;
  letter-spacing: .04em; transition: color .15s, border-color .15s;
  margin-bottom: -1px;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }

/* ── Overview tab ── */
#tab-overview { display: block; overflow-y: auto; }
#tab-live     { display: none; flex: 1; flex-direction: column; overflow: hidden; }
#tab-past     { display: none; flex: 1; overflow: hidden; }
body.live-mode #tab-live     { display: flex; }
body.live-mode #tab-overview { display: none; }
body.live-mode #tab-past     { display: none; }
body.past-mode #tab-past     { display: flex; }
body.past-mode #tab-overview { display: none; }
body.past-mode #tab-live     { display: none; }

/* Control panel */
#control-panel {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; padding: 14px 20px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.panel-label {
  font-size: 10px; font-weight: 600; letter-spacing: .09em;
  text-transform: uppercase; color: var(--muted); margin-bottom: 10px;
}
#schedule-cards { display: flex; gap: 10px; flex-wrap: wrap; }
.sched-card {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 16px; min-width: 120px;
}
.sched-card-day       { font-size: 10px; color: var(--muted); margin-bottom: 2px; }
.sched-card-time      { font-size: 14px; font-weight: 700; }
.sched-card-countdown { font-size: 11px; color: var(--accent); margin-top: 3px; }
#run-btn {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 20px; border-radius: 8px; border: none; cursor: pointer;
  font-size: 13px; font-weight: 600;
  background: var(--accent); color: #fff;
  transition: opacity .15s, background .15s;
  white-space: nowrap; align-self: flex-end;
}
#run-btn:disabled { opacity: .5; cursor: not-allowed; }
#run-btn:not(:disabled):hover { background: #3d7ee8; }
.spinner-sm {
  width: 13px; height: 13px; border: 2px solid rgba(255,255,255,.3);
  border-top-color: #fff; border-radius: 50%;
  animation: spin .7s linear infinite; flex-shrink: 0;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Stats bar */
#stats-bar {
  display: flex; gap: 12px; padding: 14px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface); flex-wrap: wrap;
}
.stat {
  display: flex; flex-direction: column; align-items: center;
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 20px; min-width: 90px;
}
.stat-val { font-size: 22px; font-weight: 700; }
.stat-lbl { font-size: 10px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); margin-top: 2px; }
.stat-val.c-pass  { color: var(--pass); }
.stat-val.c-fail  { color: var(--fail); }
.stat-val.c-total { color: var(--accent); }

/* Run history */
#list-toolbar {
  display: none; align-items: center; gap: 8px;
  padding: 10px 20px 0;
}
#list-toolbar.visible { display: flex; }
.list-ctrl-btn {
  font-size: 11px; font-weight: 600; padding: 5px 12px;
  border-radius: 6px; border: 1px solid var(--border);
  background: var(--surface); color: var(--muted);
  cursor: pointer; letter-spacing: .03em;
  transition: color .15s, border-color .15s, background .15s;
}
.list-ctrl-btn:hover { color: var(--accent); border-color: var(--accent); background: rgba(79,142,247,.07); }
#list { padding: 20px; display: flex; flex-direction: column; gap: 10px; }
.run {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden; transition: border-color .15s;
}
.run:hover { border-color: var(--accent); }
.run-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 11px 16px; cursor: pointer; gap: 12px; user-select: none;
}
.run-left { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 0; }
.run-time { font-size: 13px; font-weight: 600; white-space: nowrap; }
.run-date { font-size: 10px; color: var(--muted); margin-top: 1px; }
.source-badge {
  font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em;
  padding: 2px 8px; border-radius: 20px; white-space: nowrap; flex-shrink: 0;
}
.source-cron   { background: rgba(79,142,247,.15); color: var(--accent); border: 1px solid rgba(79,142,247,.3); }
.source-manual { background: rgba(245,158,11,.12); color: var(--flag);   border: 1px solid rgba(245,158,11,.3); }
.run-summary { display: flex; gap: 8px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }
.pill {
  font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 20px;
  display: flex; align-items: center; gap: 4px;
}
.pill-pass  { background: rgba(34,197,94,.12);  color: var(--pass);  border: 1px solid rgba(34,197,94,.2); }
.pill-fail  { background: rgba(239,68,68,.12);  color: var(--fail);  border: 1px solid rgba(239,68,68,.2); }
.pill-empty { background: rgba(136,146,164,.1); color: var(--muted); border: 1px solid rgba(136,146,164,.2); }
.chevron { font-size: 11px; color: var(--muted); transition: transform .2s; flex-shrink: 0; }
.run.open .chevron { transform: rotate(90deg); }
.run-body {
  display: none; border-top: 1px solid var(--border); padding: 14px 16px;
  background: var(--bg);
}
.run.open .run-body { display: block; }
.section-label {
  font-size: 10px; text-transform: uppercase; letter-spacing: .07em;
  color: var(--muted); margin-bottom: 6px; margin-top: 12px;
}
.section-label:first-child { margin-top: 0; }
.order-row {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 6px 10px; border-radius: 6px; font-size: 12px;
  background: var(--surface2); margin-bottom: 4px;
}
.order-icon { flex-shrink: 0; margin-top: 1px; }
.order-name { font-weight: 500; }
.order-meta { color: var(--muted); font-size: 11px; margin-top: 2px; }
#ov-empty {
  text-align: center; padding: 60px 24px; color: var(--muted); font-size: 14px; display: none;
}

/* ── Live Run tab ── */
#lr-idle {
  flex: 1; display: flex; align-items: center; justify-content: center;
  flex-direction: column; gap: 16px; color: var(--muted);
}
#lr-idle h3 { color: var(--text); font-size: 16px; }
#lr-idle-run-btn {
  padding: 10px 28px; border-radius: 8px; border: none; cursor: pointer;
  font-size: 14px; font-weight: 600; background: var(--accent); color: #fff;
  transition: background .15s; margin-top: 4px;
}
#lr-idle-run-btn:hover { background: #3d7ee8; }

#lr-main { display: none; flex: 1; overflow: hidden; flex-direction: column; }
#lr-inner { display: flex; flex: 1; overflow: hidden; }

/* PDF sidebar — list of filled forms */
#lr-sidebar {
  width: 200px; flex-shrink: 0; border-right: 1px solid var(--border);
  background: var(--surface); display: flex; flex-direction: column; overflow: hidden;
}
#lr-sidebar h2 {
  font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); padding: 11px 14px 8px;
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#lr-pdf-list { overflow-y: auto; flex: 1; }
.lr-pdf-item {
  padding: 9px 14px; border-bottom: 1px solid var(--border);
  font-size: 12px; line-height: 1.4; cursor: pointer;
  transition: background .1s;
}
.lr-pdf-item:hover { background: rgba(79,142,247,.06); }
.lr-pdf-item.active { background: rgba(79,142,247,.12); border-left: 3px solid var(--accent); }
.lr-pdf-name   { font-weight: 500; margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lr-pdf-model  { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--accent); }
.lr-pdf-status { font-size: 10px; color: var(--muted); margin-top: 2px; }

/* PDF viewer area */
#lr-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
#lr-viewers {
  display: flex; flex: 1; overflow: hidden;
  border-bottom: 1px solid var(--border);
}
.pane {
  flex: 1; display: flex; flex-direction: column;
  border-right: 1px solid var(--border); position: relative; overflow: hidden;
}
.pane:last-child { border-right: none; }
.pane-label {
  padding: 6px 14px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em;
  color: var(--muted); background: var(--surface);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
  display: flex; justify-content: space-between; align-items: center;
}
.pane-label span { font-weight: 400; color: var(--text); font-size: 11px; text-transform: none; letter-spacing: 0; }
.pane iframe { flex: 1; border: none; background: #fff; width: 100%; height: 100%; }
.pane-ph {
  flex: 1; display: flex; align-items: center;
  justify-content: center; color: var(--muted); font-size: 13px;
  text-align: center; padding: 20px;
}

/* Log panel */
#lr-log-panel {
  height: 240px; display: flex; flex-direction: column;
  background: var(--surface); flex-shrink: 0; overflow: hidden;
}
#lr-log-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 16px; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#lr-log-header h3 {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .07em; color: var(--muted);
}
#lr-run-summary {
  font-size: 11px; padding: 3px 12px; border-radius: 20px; display: none;
}
#lr-run-summary.done-ok   { display:inline-block; background:rgba(34,197,94,.15);  color:var(--pass); }
#lr-run-summary.done-warn { display:inline-block; background:rgba(245,158,11,.15); color:var(--flag); }
#lr-run-summary.done-fail { display:inline-block; background:rgba(239,68,68,.15);  color:var(--fail); }
#lr-statusbar {
  padding: 5px 16px; font-size: 11px; color: var(--muted);
  background: var(--bg); border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 8px; flex-shrink: 0;
}
.spinner {
  width: 11px; height: 11px; border: 2px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%;
  animation: spin .7s linear infinite; flex-shrink: 0;
}
#log-box {
  flex: 1; min-height: 0; overflow-y: auto; padding: 4px 14px 6px;
  font-size: 10.5px; font-family: "SF Mono", "Fira Code", "Menlo", monospace;
  line-height: 1.6; background: var(--bg); border-top: 1px solid var(--border);
}
#log-box::-webkit-scrollbar { width: 6px; }
#log-box::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.log-line { white-space: pre-wrap; }
.log-line.info    { color: var(--muted); }
.log-line.success { color: var(--pass); }
.log-line.warn    { color: var(--flag); }
.log-line.fail    { color: var(--fail); }
.log-line.error   { color: var(--fail); font-weight: 600; }
.log-line.detail  { color: #566070; }
.log-line.status  { color: var(--accent); font-style: italic; }

/* Resize handles */
.rh-h {
  width: 5px; flex-shrink: 0; cursor: col-resize;
  background: var(--border); transition: background .15s; position: relative; z-index: 10;
}
.rh-h:hover, .rh-h.rh-active { background: var(--accent); }
.rh-v {
  height: 5px; flex-shrink: 0; cursor: row-resize;
  background: var(--border); transition: background .15s; position: relative; z-index: 10;
}
.rh-v:hover, .rh-v.rh-active { background: var(--accent); }

/* ── Past Runs tab ── */
#pr-layout { display: flex; flex: 1; overflow: hidden; }
#pr-sidebar {
  width: 230px; flex-shrink: 0; border-right: 1px solid var(--border);
  background: var(--surface); display: flex; flex-direction: column; overflow: hidden;
}
#pr-sidebar h2 {
  font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); padding: 11px 14px 8px;
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#pr-run-list { overflow-y: auto; flex: 1; }
.pr-run-item {
  padding: 10px 14px; border-bottom: 1px solid var(--border);
  cursor: pointer; transition: background .1s;
}
.pr-run-item:hover { background: rgba(79,142,247,.06); }
.pr-run-item.active { background: rgba(79,142,247,.12); border-left: 3px solid var(--accent); }
.pr-run-time  { font-size: 12px; font-weight: 600; margin-bottom: 2px; }
.pr-run-date  { font-size: 10px; color: var(--muted); margin-bottom: 5px; }
.pr-run-pills { display: flex; gap: 4px; flex-wrap: wrap; }
.pr-pill {
  font-size: 9px; font-weight: 600; padding: 2px 6px; border-radius: 20px;
}
.pr-pill-pass  { background: rgba(34,197,94,.12);  color: var(--pass);  border: 1px solid rgba(34,197,94,.2); }
.pr-pill-fail  { background: rgba(239,68,68,.12);  color: var(--fail);  border: 1px solid rgba(239,68,68,.2); }
.pr-pill-empty { background: rgba(136,146,164,.1); color: var(--muted); border: 1px solid rgba(136,146,164,.2); }

/* Past Runs content */
#pr-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
#pr-empty-state {
  flex: 1; display: flex; align-items: center; justify-content: center;
  flex-direction: column; gap: 10px; color: var(--muted);
}
#pr-empty-state h3 { color: var(--text); font-size: 16px; }
#pr-run-detail { display: none; flex: 1; overflow: hidden; flex-direction: row; }
#pr-order-sidebar {
  width: 220px; flex-shrink: 0; border-right: 1px solid var(--border);
  background: var(--surface); display: flex; flex-direction: column; overflow: hidden;
}
#pr-order-sidebar h2 {
  font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); padding: 11px 14px 8px;
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#pr-order-list { overflow-y: auto; flex: 1; }
.pr-order-item {
  padding: 9px 14px; border-bottom: 1px solid var(--border);
  cursor: pointer; font-size: 12px; line-height: 1.4; transition: background .1s;
}
.pr-order-item:hover { background: rgba(79,142,247,.06); }
.pr-order-item.active { background: rgba(79,142,247,.12); border-left: 3px solid var(--accent); }
.pr-order-name  { font-weight: 500; margin-bottom: 2px; }
.pr-order-meta  { font-size: 10px; color: var(--muted); }
.pr-order-badge { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
.pr-order-badge.filled  { color: var(--pass); }
.pr-order-badge.failed  { color: var(--fail); }
#pr-detail { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
#pr-detail-header {
  padding: 10px 18px; background: var(--surface);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#pr-detail-title { font-size: 14px; font-weight: 600; margin-bottom: 2px; }
#pr-detail-sub   { font-size: 11px; color: var(--muted); }
#pr-viewers {
  display: flex; flex: 1; overflow: hidden;
}
#pr-notice {
  flex: 1; display: flex; align-items: center; justify-content: center;
  flex-direction: column; gap: 8px; color: var(--muted); font-size: 13px;
  background: var(--bg); text-align: center; padding: 20px;
}

/* Toast */
#toast {
  position: fixed; bottom: 24px; right: 24px; z-index: 200;
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 10px; padding: 11px 18px; font-size: 13px;
  opacity: 0; transition: opacity .25s; pointer-events: none;
}
#toast.show { opacity: 1; }
#toast.toast-err { border-color: rgba(239,68,68,.4); color: var(--fail); }
#toast.toast-ok  { border-color: rgba(34,197,94,.3);  color: var(--pass); }
</style>
</head>
<body>

<header>
  <div class="header-left">
    <h1>&#128221; MR. AWNINGS &middot; <em>Order Form Filler</em></h1>
  </div>
  <div id="status-indicator">
    <div id="status-dot"></div>
    <span id="status-text">IDLE</span>
  </div>
</header>

<div id="tab-bar">
  <button class="tab-btn active" id="tab-ov-btn" onclick="switchTab('overview')">Overview</button>
  <button class="tab-btn"        id="tab-lv-btn" onclick="switchTab('live')">&#9654; Live Run</button>
  <button class="tab-btn"        id="tab-pr-btn" onclick="switchTab('past')">&#128337; Past Runs</button>
</div>

<!-- ═══════════════ OVERVIEW TAB ═══════════════ -->
<div id="tab-overview">
  <div id="control-panel">
    <div>
      <div class="panel-label">Next Scheduled Runs</div>
      <div id="schedule-cards"><span style="color:var(--muted);font-size:12px">Loading…</span></div>
    </div>
    <button id="run-btn" onclick="runNow()">&#9654; Run Now</button>
  </div>

  <div id="stats-bar">
    <div class="stat"><div class="stat-val c-total" id="s-total">—</div><div class="stat-lbl">Total Runs</div></div>
    <div class="stat"><div class="stat-val c-pass"  id="s-pass">—</div><div class="stat-lbl">Forms Filled</div></div>
    <div class="stat"><div class="stat-val c-fail"  id="s-fail">—</div><div class="stat-lbl">Failed</div></div>
  </div>

  <div id="list-toolbar">
    <button class="list-ctrl-btn" onclick="expandAll()">&#9660; Expand All</button>
    <button class="list-ctrl-btn" onclick="collapseAll()">&#9650; Collapse All</button>
  </div>
  <div id="list"></div>
  <div id="ov-empty">No run history yet. Hit <strong>Run Now</strong> to kick off the first run.</div>
</div>

<!-- ═══════════════ LIVE RUN TAB ═══════════════ -->
<div id="tab-live">

  <div id="lr-idle">
    <h3>No active run</h3>
    <p>Start a manual run to watch the agent work in real time.</p>
    <button id="lr-idle-run-btn" onclick="runNow()">&#9654; Run Now</button>
  </div>

  <div id="lr-main">
    <div id="lr-inner">

      <!-- PDF list sidebar -->
      <aside id="lr-sidebar">
        <h2>Filled Forms <span id="lr-pdf-count" style="font-weight:400;text-transform:none;letter-spacing:0"></span></h2>
        <div id="lr-pdf-list"></div>
      </aside>

      <div class="rh-h" id="rh-lr-sidebar"></div>

      <div id="lr-content">
        <div id="lr-viewers">
          <div class="pane" id="pane-invoice">
            <div class="pane-label">Zoho Invoice <span id="lbl-invoice"></span></div>
            <div class="pane-ph" id="ph-invoice">Select a filled form from the sidebar to view the source invoice.</div>
          </div>
          <div class="rh-h" id="rh-lr-panes"></div>
          <div class="pane" id="pane-form">
            <div class="pane-label">Filled Order Form <span id="lbl-form"></span></div>
            <div class="pane-ph" id="ph-form">Filled forms will appear here as the agent completes them.</div>
          </div>
        </div>
        <div class="rh-v" id="rh-lr-log"></div>
        <div id="lr-log-panel">
          <div id="lr-log-header">
            <h3>Agent Log</h3>
            <div id="lr-run-summary"></div>
          </div>
          <div id="lr-statusbar">
            <div class="spinner" id="lr-spin" style="display:none"></div>
            <span id="lr-status-msg">Idle — waiting for a run to start.</span>
          </div>
          <div id="log-box"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════ PAST RUNS TAB ═══════════════ -->
<div id="tab-past">
  <div id="pr-layout">
    <aside id="pr-sidebar">
      <h2>Past Runs</h2>
      <div id="pr-run-list"></div>
    </aside>
    <div id="pr-content">
      <div id="pr-empty-state">
        <h3>Select a run</h3>
        <p>Choose a past run from the left to review its filled forms.</p>
      </div>
      <div id="pr-run-detail">
        <aside id="pr-order-sidebar">
          <h2>Orders</h2>
          <div id="pr-order-list"></div>
        </aside>
        <div id="pr-detail">
          <div id="pr-detail-header">
            <div id="pr-detail-title">—</div>
            <div id="pr-detail-sub"></div>
          </div>
          <div id="pr-viewers">
            <div class="pane" id="pr-pane">
              <div class="pane-label">Filled Order Form</div>
              <div class="pane-ph" id="pr-ph">Select an order from the left.</div>
            </div>
          </div>
        </div>
        <div id="pr-notice" style="display:none"></div>
      </div>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
// ── Shared state ────────────────────────────────────────────────────────────
var _activeTab   = "overview";
var _isRunning   = false;
var _schedData   = [];
var _countdownId = null;
var _histData    = [];
var _lrSSE       = null;
var _lrPdfs      = {};        // po → {has_invoice, has_form, item_name, model}
var _lrSelected  = null;      // currently selected PO in live sidebar
var _prHistory   = [];
var _prSelRun    = -1;
var _prSelOrder  = -1;

// ── Tabs ────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  _activeTab = tab;
  document.getElementById("tab-ov-btn").className = "tab-btn" + (tab === "overview" ? " active" : "");
  document.getElementById("tab-lv-btn").className = "tab-btn" + (tab === "live"     ? " active" : "");
  document.getElementById("tab-pr-btn").className = "tab-btn" + (tab === "past"     ? " active" : "");
  document.body.className = tab === "live" ? "live-mode" : tab === "past" ? "past-mode" : "";
  if (tab === "past") { renderPastRunsSidebar(); }
}

// ── Status polling ──────────────────────────────────────────────────────────
function pollStatus() {
  fetch("/api/status").then(r => r.json()).then(d => {
    var was = _isRunning;
    _isRunning = d.running;
    setStatusDot(_isRunning);
    document.getElementById("run-btn").disabled   = _isRunning;
    if (_isRunning) {
      document.getElementById("run-btn").innerHTML =
        '<div class="spinner-sm"></div> Running…';
    } else {
      document.getElementById("run-btn").innerHTML = "&#9654; Run Now";
      if (was && !_isRunning) {
        // run just finished — reload history
        loadHistory();
      }
    }
  });
  setTimeout(pollStatus, 2000);
}

function setStatusDot(running) {
  var dot  = document.getElementById("status-dot");
  var txt  = document.getElementById("status-text");
  dot.className = running ? "running" : "";
  txt.textContent = running ? "RUNNING" : "IDLE";
}

// ── Run Now ─────────────────────────────────────────────────────────────────
function runNow() {
  if (_isRunning) return;
  fetch("/api/run", {method:"POST"}).then(r => r.json()).then(d => {
    if (!d.ok) { showToast(d.message || "Failed to start run", true); return; }
    showToast("Run started", false);
    // Switch to live tab and attach SSE
    switchTab("live");
    startLiveRun();
  });
}

// ── Schedule cards ──────────────────────────────────────────────────────────
function loadSchedule() {
  fetch("/api/schedule").then(r => r.json()).then(data => {
    _schedData = data;
    renderScheduleCards();
    if (_countdownId) clearInterval(_countdownId);
    _countdownId = setInterval(renderScheduleCards, 30000);
  });
}

function renderScheduleCards() {
  var el = document.getElementById("schedule-cards");
  if (!_schedData.length) { el.innerHTML = '<span style="color:var(--muted);font-size:12px">None scheduled</span>'; return; }
  var now = new Date();
  el.innerHTML = _schedData.map(function(iso) {
    var dt   = new Date(iso);
    var diff = Math.round((dt - now) / 60000);
    var hrs  = Math.floor(diff / 60);
    var mins = diff % 60;
    var countdown = diff < 60 ? "in " + diff + "m" :
                    "in " + hrs + "h " + (mins > 0 ? mins + "m" : "");
    var days  = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
    var day   = days[dt.getDay()];
    var time  = dt.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
    return '<div class="sched-card"><div class="sched-card-day">' + day + '</div>' +
           '<div class="sched-card-time">' + time + '</div>' +
           '<div class="sched-card-countdown">' + countdown + '</div></div>';
  }).join("");
}

// ── History ──────────────────────────────────────────────────────────────────
function loadHistory() {
  fetch("/api/history").then(r => r.json()).then(data => {
    _histData  = data;
    _prHistory = data;
    renderHistory(data);
    renderStats(data);
  });
}

function renderStats(data) {
  var totalRuns  = data.length;
  var totalFill  = data.reduce(function(a, r) { return a + (r.filled || []).length; }, 0);
  var totalFail  = data.reduce(function(a, r) { return a + (r.failed || []).length; }, 0);
  document.getElementById("s-total").textContent = totalRuns;
  document.getElementById("s-pass").textContent  = totalFill;
  document.getElementById("s-fail").textContent  = totalFail;
}

function renderHistory(data) {
  var list = document.getElementById("list");
  var empty = document.getElementById("ov-empty");
  var toolbar = document.getElementById("list-toolbar");
  if (!data.length) {
    list.innerHTML = ""; empty.style.display = "block"; toolbar.className = "";
    return;
  }
  empty.style.display = "none"; toolbar.className = "visible";
  list.innerHTML = data.map(function(run, i) {
    var dt   = new Date(run.timestamp);
    var time = dt.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
    var date = dt.toLocaleDateString([], {month:"short", day:"numeric", year:"numeric"});
    var src  = run.source === "manual" ? "manual" : "cron";
    var nf   = (run.filled || []).length;
    var nd   = (run.failed || []).length;

    var pillsHtml = "";
    if (nf) pillsHtml += '<span class="pill pill-pass">&#10003; ' + nf + ' filled</span>';
    if (nd) pillsHtml += '<span class="pill pill-fail">&#10005; ' + nd + ' failed</span>';
    if (!nf && !nd) pillsHtml = '<span class="pill pill-empty">' + (run.note || "Empty run") + '</span>';

    var bodyHtml = "";
    if (nf) {
      bodyHtml += '<div class="section-label">Filled Forms</div>';
      (run.filled || []).forEach(function(o) {
        var model = o.model ? o.model.charAt(0).toUpperCase() + o.model.slice(1) : "";
        bodyHtml += '<div class="order-row"><div class="order-icon">&#10003;</div>' +
          '<div><div class="order-name">' + esc(o.name) + '</div>' +
          (model ? '<div class="order-meta">' + esc(model) + '</div>' : '') +
          '</div></div>';
      });
    }
    if (nd) {
      bodyHtml += '<div class="section-label">Failed</div>';
      (run.failed || []).forEach(function(o) {
        bodyHtml += '<div class="order-row"><div class="order-icon" style="color:var(--fail)">&#10005;</div>' +
          '<div><div class="order-name">' + esc(o.name) + '</div>' +
          (o.reason ? '<div class="order-meta" style="color:var(--fail)">' + esc(o.reason) + '</div>' : '') +
          '</div></div>';
      });
    }
    if (!bodyHtml && run.note) {
      bodyHtml = '<div style="color:var(--muted);font-size:12px">' + esc(run.note) + '</div>';
    }

    return '<div class="run" id="run-' + i + '">' +
      '<div class="run-header" onclick="toggleRun(' + i + ')">' +
        '<div class="run-left">' +
          '<div><div class="run-time">' + time + '</div><div class="run-date">' + date + '</div></div>' +
          '<span class="source-badge source-' + src + '">' + src + '</span>' +
        '</div>' +
        '<div class="run-summary">' + pillsHtml + '</div>' +
        '<span class="chevron">&#9658;</span>' +
      '</div>' +
      '<div class="run-body">' + bodyHtml + '</div>' +
    '</div>';
  }).join("");
}

function toggleRun(i) {
  var el = document.getElementById("run-" + i);
  if (el) el.classList.toggle("open");
}
function expandAll()  { document.querySelectorAll(".run").forEach(function(r) { r.classList.add("open"); }); }
function collapseAll(){ document.querySelectorAll(".run").forEach(function(r) { r.classList.remove("open"); }); }

// ── Live Run ─────────────────────────────────────────────────────────────────
function startLiveRun() {
  _lrPdfs = {};
  _lrSelected = null;
  document.getElementById("lr-idle").style.display = "none";
  document.getElementById("lr-main").style.display = "flex";
  document.getElementById("lr-pdf-list").innerHTML = "";
  document.getElementById("lr-pdf-count").textContent = "";
  document.getElementById("log-box").innerHTML = "";
  document.getElementById("lr-run-summary").className = "";
  document.getElementById("lr-run-summary").style.display = "none";
  showLrStatus("Connecting to agent…", true);
  resetLrPanes();

  if (_lrSSE) { _lrSSE.close(); }
  _lrSSE = new EventSource("/api/events");

  _lrSSE.addEventListener("log", function(e) {
    var d = JSON.parse(e.data);
    appendLog(d.msg, d.level || "info");
  });

  _lrSSE.addEventListener("status", function(e) {
    var d = JSON.parse(e.data);
    showLrStatus(d.msg, true);
    appendLog(d.msg, "status");
  });

  _lrSSE.addEventListener("pdf_ready", function(e) {
    var d = JSON.parse(e.data);
    _lrPdfs[d.po_number] = {
      has_invoice: false,
      has_form:    true,
      item_name:   d.item_name || "",
      model:       d.model     || "",
      task_name:   d.task_name || "",
    };
    renderLrPdfList();
    // Auto-select first PDF
    if (!_lrSelected) selectLrPdf(d.po_number);
  });

  _lrSSE.addEventListener("invoice_pdf_ready", function(e) {
    var d = JSON.parse(e.data);
    if (_lrPdfs[d.po_number]) {
      _lrPdfs[d.po_number].has_invoice = true;
      renderLrPdfList();
      if (_lrSelected === d.po_number) loadInvoicePdf(d.po_number);
    }
  });

  _lrSSE.addEventListener("complete", function(e) {
    var d = JSON.parse(e.data);
    _lrSSE.close(); _lrSSE = null;
    showLrStatus("Run complete.", false);
    var nf = (d.filled || []).length;
    var nd = (d.failed || []).length;
    var sumEl = document.getElementById("lr-run-summary");
    if (!nd) {
      sumEl.textContent  = "&#10003; " + nf + " form(s) filled";
      sumEl.className    = "done-ok";
    } else if (nf) {
      sumEl.textContent  = nf + " filled, " + nd + " failed";
      sumEl.className    = "done-warn";
    } else {
      sumEl.textContent  = nd + " failed";
      sumEl.className    = "done-fail";
    }
    sumEl.style.display = "inline-block";
    loadHistory();
  });

  _lrSSE.onerror = function() {
    if (_lrSSE) { _lrSSE.close(); _lrSSE = null; }
    showLrStatus("Stream closed.", false);
  };
}

function showLrStatus(msg, spinning) {
  document.getElementById("lr-status-msg").textContent = msg;
  document.getElementById("lr-spin").style.display = spinning ? "inline-block" : "none";
}

function appendLog(msg, level) {
  var box  = document.getElementById("log-box");
  var line = document.createElement("div");
  line.className = "log-line " + (level || "info");
  line.textContent = msg;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function renderLrPdfList() {
  var list  = document.getElementById("lr-pdf-list");
  var count = document.getElementById("lr-pdf-count");
  var pos   = Object.keys(_lrPdfs);
  count.textContent = "(" + pos.length + ")";
  list.innerHTML = pos.map(function(po) {
    var p   = _lrPdfs[po];
    var active = _lrSelected === po ? " active" : "";
    var invStatus = p.has_invoice ? "Invoice PDF ready" : "Invoice PDF loading…";
    return '<div class="lr-pdf-item' + active + '" onclick="selectLrPdf(\'' + po + '\')">' +
      '<div class="lr-pdf-name">' + esc(p.task_name || po) + '</div>' +
      '<div class="lr-pdf-model">' + esc(p.model) + '</div>' +
      '<div class="lr-pdf-status">PO #' + esc(po) + ' &middot; ' + invStatus + '</div>' +
    '</div>';
  }).join("");
}

function selectLrPdf(po) {
  _lrSelected = po;
  renderLrPdfList();
  var p = _lrPdfs[po];
  if (!p) return;

  // Invoice pane
  document.getElementById("lbl-invoice").textContent = "PO #" + po;
  if (p.has_invoice) {
    loadInvoicePdf(po);
  } else {
    var ph = document.getElementById("ph-invoice");
    ph.style.display = "flex";
    ph.textContent   = "Invoice PDF is loading…";
    var iframe = document.getElementById("iframe-invoice");
    if (iframe) iframe.remove();
  }

  // Form pane
  document.getElementById("lbl-form").textContent = p.model ? p.model.charAt(0).toUpperCase() + p.model.slice(1) + " Order Form" : "Order Form";
  loadFormPdf(po);
}

function loadInvoicePdf(po) {
  var pane = document.getElementById("pane-invoice");
  var ph   = document.getElementById("ph-invoice");
  ph.style.display = "none";
  var existing = document.getElementById("iframe-invoice");
  if (existing) existing.remove();
  var iframe = document.createElement("iframe");
  iframe.id  = "iframe-invoice";
  iframe.src = "/pdf/invoice/" + po + "?t=" + Date.now();
  pane.appendChild(iframe);
}

function loadFormPdf(po) {
  var pane = document.getElementById("pane-form");
  var ph   = document.getElementById("ph-form");
  ph.style.display = "none";
  var existing = document.getElementById("iframe-form");
  if (existing) existing.remove();
  var iframe = document.createElement("iframe");
  iframe.id  = "iframe-form";
  iframe.src = "/pdf/form/po/" + po + "?t=" + Date.now();
  pane.appendChild(iframe);
}

function resetLrPanes() {
  var pInv = document.getElementById("pane-invoice");
  var pFrm = document.getElementById("pane-form");
  ["iframe-invoice", "iframe-form"].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.remove();
  });
  document.getElementById("ph-invoice").style.display = "flex";
  document.getElementById("ph-form").style.display    = "flex";
  document.getElementById("ph-invoice").textContent   = "Select a filled form from the sidebar to view the source invoice.";
  document.getElementById("ph-form").textContent      = "Filled forms will appear here as the agent completes them.";
  document.getElementById("lbl-invoice").textContent  = "";
  document.getElementById("lbl-form").textContent     = "";
}

// ── Past Runs ────────────────────────────────────────────────────────────────
function renderPastRunsSidebar() {
  var list = document.getElementById("pr-run-list");
  if (!_prHistory.length) {
    list.innerHTML = '<div style="padding:14px;color:var(--muted);font-size:12px">No history yet.</div>';
    return;
  }
  list.innerHTML = _prHistory.map(function(run, i) {
    var dt   = new Date(run.timestamp);
    var time = dt.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
    var date = dt.toLocaleDateString([], {month:"short", day:"numeric"});
    var nf   = (run.filled || []).length;
    var nd   = (run.failed || []).length;
    var pills = "";
    if (nf) pills += '<span class="pr-pill pr-pill-pass">' + nf + ' filled</span>';
    if (nd) pills += '<span class="pr-pill pr-pill-fail">' + nd + ' failed</span>';
    if (!nf && !nd) pills = '<span class="pr-pill pr-pill-empty">empty</span>';
    var active = _prSelRun === i ? " active" : "";
    return '<div class="pr-run-item' + active + '" onclick="selectPrRun(' + i + ')">' +
      '<div class="pr-run-time">' + time + '</div>' +
      '<div class="pr-run-date">' + date + '</div>' +
      '<div class="pr-run-pills">' + pills + '</div>' +
    '</div>';
  }).join("");
}

function selectPrRun(i) {
  _prSelRun   = i;
  _prSelOrder = -1;
  renderPastRunsSidebar();
  var run = _prHistory[i];
  if (!run) return;

  document.getElementById("pr-empty-state").style.display  = "none";
  document.getElementById("pr-run-detail").style.display   = "flex";

  // Populate order list
  var all = (run.filled || []).map(function(o) { return {o: o, type: "filled"}; })
    .concat((run.failed || []).map(function(o) { return {o: o, type: "failed"}; }));

  var olist = document.getElementById("pr-order-list");
  olist.innerHTML = all.map(function(item, j) {
    var badge = item.type === "filled"
      ? '<div class="pr-order-badge filled">&#10003; Filled</div>'
      : '<div class="pr-order-badge failed">&#10005; Failed</div>';
    var model = item.o.model ? item.o.model.charAt(0).toUpperCase() + item.o.model.slice(1) : "";
    return '<div class="pr-order-item" onclick="selectPrOrder(' + i + ',' + j + ')">' +
      '<div class="pr-order-name">' + esc(item.o.name) + '</div>' +
      (model ? '<div class="pr-order-meta">' + esc(model) + '</div>' : '') +
      badge +
    '</div>';
  }).join("");

  // Reset detail panel
  document.getElementById("pr-detail-title").textContent = formatRunTime(run.timestamp);
  document.getElementById("pr-detail-sub").textContent   = run.source + " run · " +
    (run.filled || []).length + " filled, " + (run.failed || []).length + " failed";
  showPrNotice("Select an order from the left.");
}

function selectPrOrder(runIdx, orderIdx) {
  _prSelOrder = orderIdx;
  var run = _prHistory[runIdx];
  if (!run) return;

  // Highlight
  document.querySelectorAll(".pr-order-item").forEach(function(el, j) {
    el.classList.toggle("active", j === orderIdx);
  });

  var all = (run.filled || []).map(function(o) { return {o: o, type: "filled"}; })
    .concat((run.failed || []).map(function(o) { return {o: o, type: "failed"}; }));
  var item = all[orderIdx];
  if (!item) return;

  if (item.type === "failed") {
    showPrNotice("&#10005; " + (item.o.reason || "This order failed — no PDF to show."));
    return;
  }

  // Show the filled PDF
  hidePrNotice();
  var pane = document.getElementById("pr-pane");
  var ph   = document.getElementById("pr-ph");
  ph.style.display = "none";
  var existing = document.getElementById("iframe-pr");
  if (existing) existing.remove();

  // Compute indices
  var filledIdx = runIdx;
  var orderInFilled = (run.filled || []).indexOf(item.o);

  var iframe = document.createElement("iframe");
  iframe.id  = "iframe-pr";
  iframe.src = "/pdf/history/" + runIdx + "/" + orderInFilled + "?t=" + Date.now();
  iframe.style.flex   = "1";
  iframe.style.border = "none";
  iframe.style.width  = "100%";
  iframe.style.height = "100%";
  pane.appendChild(iframe);
}

function showPrNotice(msg) {
  var pane    = document.getElementById("pr-pane");
  var ph      = document.getElementById("pr-ph");
  var iframe  = document.getElementById("iframe-pr");
  if (iframe) iframe.remove();
  ph.style.display  = "flex";
  ph.innerHTML      = msg;
}

function hidePrNotice() {
  // handled by selectPrOrder directly
}

function formatRunTime(iso) {
  var dt = new Date(iso);
  return dt.toLocaleDateString([], {weekday:"short", month:"short", day:"numeric"}) +
         " at " + dt.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
}

// ── Resize handles ───────────────────────────────────────────────────────────
function initResize(handleId, getA, getB, prop, minA, minB) {
  var handle = document.getElementById(handleId);
  if (!handle) return;
  handle.addEventListener("mousedown", function(e) {
    e.preventDefault();
    handle.classList.add("rh-active");
    var startX = e.clientX, startY = e.clientY;
    var aEl = getA(), bEl = getB();
    var startASize = prop === "width" ? aEl.offsetWidth : aEl.offsetHeight;
    var startBSize = prop === "width" ? bEl.offsetWidth : bEl.offsetHeight;
    function onMove(e2) {
      var delta = prop === "width" ? e2.clientX - startX : e2.clientY - startY;
      var newA  = Math.max(minA, startASize + delta);
      var newB  = Math.max(minB, startBSize - delta);
      aEl.style.flex = "none";
      bEl.style.flex = "none";
      aEl.style[prop] = newA + "px";
      bEl.style[prop] = newB + "px";
    }
    function onUp() {
      handle.classList.remove("rh-active");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup",   onUp);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup",   onUp);
  });
}

// ── Utilities ────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

var _toastTimer = null;
function showToast(msg, isErr) {
  var el = document.getElementById("toast");
  el.textContent = msg;
  el.className   = "show " + (isErr ? "toast-err" : "toast-ok");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function() { el.className = ""; }, 3000);
}

// ── Init ─────────────────────────────────────────────────────────────────────
window.addEventListener("load", function() {
  loadSchedule();
  loadHistory();
  pollStatus();

  // Resize handles
  initResize("rh-lr-sidebar",
    function() { return document.getElementById("lr-sidebar"); },
    function() { return document.getElementById("lr-content"); },
    "width", 120, 300
  );
  initResize("rh-lr-panes",
    function() { return document.getElementById("pane-invoice"); },
    function() { return document.getElementById("pane-form"); },
    "width", 200, 200
  );
  initResize("rh-lr-log",
    function() { return document.getElementById("lr-viewers"); },
    function() { return document.getElementById("lr-log-panel"); },
    "height", 200, 100
  );

  // If agent is already running when page loads, auto-show live tab
  fetch("/api/status").then(r => r.json()).then(d => {
    if (d.running) {
      switchTab("live");
      startLiveRun();
    }
  });
});
</script>
</body>
</html>
"""

def _start_scheduler():
    """Start the APScheduler in a background daemon thread (used when running on Railway)."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import agent as _agent

    tz = os.environ.get("TIMEZONE", "America/New_York")
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(_agent.run_agent, CronTrigger(hour="9,21", minute=0), args=["cron"])
    sched.start()
    return sched


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5003))
    host = os.environ.get("HOST", "0.0.0.0")

    # When AUTO_SCHEDULER=1 (set by Railway), run the cron scheduler in-process
    if os.environ.get("AUTO_SCHEDULER", "").strip() == "1":
        _start_scheduler()

    # Only open a browser when running locally
    if host in ("127.0.0.1", "localhost"):
        webbrowser.open(f"http://127.0.0.1:{port}")

    app.run(host=host, port=port, debug=False, threaded=True)

"""
agent.py — Mr. Awnings Order Form Filler Agent

Workflow:
  1. Poll ClickUp for tasks with status "GIVEN TO JAY".
  2. For each task, extract PO number(s) from the 'PO #' custom field.
  3. Fetch the full Zoho invoice for each PO.
  4. Fill the correct Sunesta order form (Sunesta / Sunstyle / Sunlight)
     using data from the invoice, powered by Claude AI.
  5. Save filled PDF to the Dropbox output folder (date-stamped subfolder).
  6. Update the ClickUp task status to "Sent to Zack" so the verifier picks it up.
  7. Post a Slack summary of filled / failed orders.

Run modes:
  python3 agent.py --now    → run once immediately (no dashboard)
  python3 agent.py          → run on schedule (9 AM & 9 PM ET daily)

The dashboard (dashboard.py) calls run_agent() directly with an event_sink
callback so it can stream live log events via SSE.
"""

import os
import json
import logging
import threading
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config import Config
from clickup_client import ClickUpClient
from dropbox_client import DropboxClient
from zoho_client import ZohoClient
from slack_notifier import SlackNotifier
from form_filler import process_invoice

# ── File-level logger (also used when run via CLI) ────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("agent.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Live-run state (used by dashboard SSE stream) ─────────────────────────────
_run_lock         = threading.Lock()
_run_events       = []          # list of {"type": str, "data": dict}
_run_active       = False       # True while a run is in progress
_stop_requested   = False       # Set to True to cancel after current task
_resume_event     = threading.Event()  # Used in "one at a time" mode to gate between tasks


def _push(event_type: str, data: dict, sink=None):
    """Append an SSE event to the shared buffer and optionally call sink(type, data)."""
    with _run_lock:
        _run_events.append({"type": event_type, "data": data})
    if sink:
        try:
            sink(event_type, data)
        except Exception:
            pass


def is_running() -> bool:
    return _run_active


def request_stop():
    """Ask the agent to stop after it finishes the current task."""
    global _stop_requested
    with _run_lock:
        _stop_requested = True
    _resume_event.set()  # unblock any pause so the stop is seen immediately


def resume_run():
    """Resume the agent after a between-task pause (one-at-a-time mode)."""
    _resume_event.set()


def get_events() -> list:
    with _run_lock:
        return list(_run_events)


def prompt_job_options() -> dict:
    """Ask the user at job start how to handle wind sensors and LEDs."""
    print()
    print("─" * 50)
    print("  JOB OPTIONS — answer before processing begins")
    print("─" * 50)

    ws = input("  Wind sensors — order WITH the awning? [y/N]: ").strip().lower()
    wind_sensor_stock = ws not in ("y", "yes")

    led = input("  LED lights   — order WITH the awning? [y/N]: ").strip().lower()
    led_stock = led not in ("y", "yes")

    print()
    print(f"  Wind sensors: {'ORDER with awning' if not wind_sensor_stock else 'from STOCK'}")
    print(f"  LED lights:   {'ORDER with awning' if not led_stock else 'from STOCK'}")
    print("─" * 50)
    print()

    return {"wind_sensor_stock": wind_sensor_stock, "led_stock": led_stock}


def run_agent(source: str = "cron", event_sink=None, job_options: dict = None):
    """
    Main agent entry-point.

    event_sink — optional callable(event_type: str, data: dict) called for
                 every log/status event so callers (dashboard SSE) can stream
                 events in real time.
    """
    global _run_active, _run_events, _stop_requested

    if job_options is None:
        job_options = {"wind_sensor_stock": True, "led_stock": True}

    _resume_event.clear()

    with _run_lock:
        if _run_active:
            return  # don't allow concurrent runs
        _run_active      = True
        _run_events      = []
        _stop_requested  = False

    def emit(msg: str, level: str = "info"):
        _push("log", {"msg": msg, "level": level}, sink=event_sink)
        log.info(msg)

    def status(msg: str):
        _push("status", {"msg": msg}, sink=event_sink)
        log.info(msg)

    try:
        log.info("=" * 60)
        emit(f"Form Filler Agent run started — {datetime.now().strftime('%Y-%m-%d %H:%M')}", "info")

        try:
            config = Config()
        except KeyError as e:
            emit(f"Missing required config value: {e}. Check your .env file.", "error")
            _push("complete", {"filled": [], "failed": [], "note": f"Config error: {e}"}, sink=event_sink)
            _write_run_log([], [], source=source, note=f"Config error: {e}")
            return

        clickup = ClickUpClient(config)
        zoho    = ZohoClient(config)
        slack   = SlackNotifier(config)
        dropbox = DropboxClient(config) if config.DROPBOX_REFRESH_TOKEN else None
        if dropbox:
            emit("Dropbox storage enabled (API mode).", "info")
        else:
            emit("Dropbox not configured — saving PDFs to local filesystem.", "info")

        status(f"Fetching ClickUp tasks with status: '{config.TRIGGER_STATUS}'")
        try:
            tasks = clickup.get_tasks_by_status(config.TRIGGER_STATUS)
        except Exception as e:
            msg = f"ClickUp fetch failed: {e}"
            emit(msg, "error")
            slack.error(msg)
            _push("complete", {"filled": [], "failed": [], "note": msg}, sink=event_sink)
            _write_run_log([], [], source=source, note=msg)
            return

        if not tasks:
            emit("No tasks found in trigger status. Nothing to do.", "warn")
            _push("complete", {"filled": [], "failed": [], "note": "No tasks in queue"}, sink=event_sink)
            _write_run_log([], [], source=source, note="No tasks in queue")
            log.info("=" * 60)
            return

        emit(f"Found {len(tasks)} task(s) to process.", "info")

        filled_orders = []
        failed_orders = []

        for task_idx, task in enumerate(tasks):
            # Check for stop request before starting each new task
            with _run_lock:
                should_stop = _stop_requested
            if should_stop:
                emit("⛔ Stop requested — halting after current task.", "warn")
                _push("stopped", {}, sink=event_sink)
                break

            task_id   = task["id"]
            task_name = task["name"]
            emit(f"── Task: {task_name} ({task_id})", "info")

            po_numbers = clickup.get_po_numbers(task)
            if not po_numbers:
                reason = "No PO # value found on this ClickUp task."
                emit(f"  SKIPPED — {reason}", "warn")
                failed_orders.append({"name": task_name, "reason": reason})
                try:
                    clickup.add_comment(task_id, f"Form Filler: {reason}")
                except Exception:
                    pass
                continue

            task_all_ok = True
            task_filled = []

            for po_number in po_numbers:
                label = f"{task_name} (PO #{po_number})"
                emit(f"  Processing PO #{po_number}…", "info")

                try:
                    # 1. Fetch invoice
                    status(f"[{po_number}] Fetching invoice from Zoho…")
                    invoice = zoho.get_full_invoice(po_number)
                    if not invoice:
                        raise RuntimeError(f"Invoice #{po_number} not found in Zoho.")
                    if not invoice.get("awning_items"):
                        raise RuntimeError(
                            f"No awning line items found in invoice #{po_number}. "
                            "Check that the invoice contains Sunesta/Sunstyle/Sunlight items."
                        )

                    customer = invoice.get("customer_name", "")
                    emit(f"  Invoice: {po_number} — {customer} ({len(invoice['awning_items'])} awning item(s))", "success")
                    for item in invoice["awning_items"]:
                        emit(f"    • {item.get('name', '?')}", "detail")

                    # 2. Fill forms
                    status(f"[{po_number}] Filling {len(invoice['awning_items'])} form(s) with Claude AI…")
                    results = process_invoice(
                        invoice=invoice,
                        po_number=po_number,
                        forms_folder=config.SUNESTA_FORMS_FOLDER,
                        output_folder=config.FILLED_FORMS_FOLDER,
                        anthropic_api_key=config.ANTHROPIC_API_KEY,
                        dropbox_client=dropbox,
                        job_options=job_options,
                    )

                    # 3. Handle results
                    for r in results:
                        if r.success:
                            emit(f"  ✅ {r.model.title()} form filled → {os.path.basename(r.pdf_path)}", "success")
                            _push("pdf_ready", {
                                "po_number":  po_number,
                                "model":      r.model,
                                "pdf_path":   r.pdf_path,
                                "item_name":  r.item_name,
                                "task_name":  task_name,
                            }, sink=event_sink)
                            task_filled.append(r)
                            filled_orders.append({
                                "name":     label,
                                "model":    r.model,
                                "pdf_path": r.pdf_path,
                            })
                        else:
                            emit(f"  ❌ {r.item_name}: {r.error}", "fail")
                            task_all_ok = False
                            failed_orders.append({"name": label, "reason": r.error})
                            try:
                                clickup.add_comment(
                                    task_id,
                                    f"Form Filler: Could not fill form for {r.item_name}:\n{r.error}",
                                )
                            except Exception:
                                pass

                except Exception as e:
                    emit(f"  ERROR: {e}", "error")
                    log.error(f"  ERROR: {e}", exc_info=True)
                    task_all_ok = False
                    failed_orders.append({"name": label, "reason": str(e)})
                    try:
                        clickup.add_comment(task_id, f"Form Filler error on PO #{po_number}: {e}")
                    except Exception:
                        pass

            # Log filled forms to ClickUp as a comment (status move is manual)
            if task_all_ok and task_filled:
                try:
                    models = ", ".join(sorted({r.model.title() for r in task_filled}))
                    clickup.add_comment(
                        task_id,
                        f"Form Filler: {len(task_filled)} {models} form(s) filled and saved to Dropbox.",
                    )
                except Exception:
                    pass

            # In one-at-a-time mode, pause between tasks and wait for user to continue
            has_more_tasks = task_idx < len(tasks) - 1
            if job_options.get("pause_between_tasks") and has_more_tasks:
                emit("⏸ Paused — review the filled form, then click Continue.", "info")
                _push("task_complete_pause", {"remaining": len(tasks) - task_idx - 1}, sink=event_sink)
                _resume_event.clear()
                _resume_event.wait()  # blocks until resume_run() or request_stop() is called
                with _run_lock:
                    if _stop_requested:
                        emit("⛔ Stop requested during pause.", "warn")
                        _push("stopped", {}, sink=event_sink)
                        break

        # Write run log now so dashboard stats are up to date whether run
        # completed normally or was stopped partway through.
        _write_run_log(filled_orders, failed_orders, source=source)

        # Slack summaries
        if filled_orders:
            slack.filled_summary(filled_orders)
        if failed_orders:
            slack.failed_summary(failed_orders)

        emit(
            f"Run complete | Filled: {len(filled_orders)} | Failed: {len(failed_orders)}",
            "success" if not failed_orders else "warn",
        )
        _push("complete", {"filled": filled_orders, "failed": failed_orders}, sink=event_sink)
        log.info("=" * 60)

    finally:
        with _run_lock:
            _run_active      = False
            _stop_requested  = False


def _write_run_log(filled: list, failed: list, source: str = "cron", note: str = ""):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "source":    source,
        "filled":    filled,
        "failed":    failed,
    }
    if note:
        entry["note"] = note

    history_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_history.json")
    history = []
    if os.path.exists(history_path):
        try:
            with open(history_path) as f:
                history = json.load(f)
        except Exception:
            pass

    history.insert(0, entry)
    with open(history_path, "w") as f:
        json.dump(history[:100], f, indent=2)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        log.info("Running agent immediately (--now flag)…")
        opts = prompt_job_options()
        run_agent(source="manual", job_options=opts)
    else:
        log.info("Order Form Filler Agent starting…")
        log.info("Scheduled: 9:00 AM and 9:00 PM daily (America/New_York)")
        log.info("To run immediately: python3 agent.py --now")
        log.info("Press Ctrl+C to stop.")
        scheduler = BlockingScheduler(timezone="America/New_York")
        scheduler.add_job(run_agent, CronTrigger(hour="9,21", minute=0))
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Agent stopped.")

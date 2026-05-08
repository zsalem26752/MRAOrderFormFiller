"""
run_test.py — Step-by-step end-to-end test (DRY RUN).

Processes ALL tasks in the 'GIVEN TO JAY' queue:
  1. Fetches ClickUp tasks
  2. For each task: shows PO #(s), fetches Zoho invoice, fills PDF(s) with Claude
  3. Saves filled PDFs locally (or uploads to Dropbox if token is set)
  4. Shows a summary of filled fields and the output path

DOES NOT update ClickUp statuses or post Slack messages.
Pauses after each task so you can inspect the output.
"""

import os
import sys
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

# ── colour helpers ─────────────────────────────────────────────────────────────
def c(text, code): return f"\033[{code}m{text}\033[0m"
def bold(t):   return c(t, "1")
def green(t):  return c(t, "32")
def yellow(t): return c(t, "33")
def red(t):    return c(t, "31")
def cyan(t):   return c(t, "36")
def dim(t):    return c(t, "2")

def section(title):
    print()
    print(bold(cyan(f"{'─' * 55}")))
    print(bold(cyan(f"  {title}")))
    print(bold(cyan(f"{'─' * 55}")))

def pause(prompt="Press ENTER to continue…"):
    input(f"\n{dim(prompt)}")


# ── main ───────────────────────────────────────────────────────────────────────
def main():
    from config import Config
    from clickup_client import ClickUpClient
    from dropbox_client import DropboxClient
    from zoho_client import ZohoClient
    from form_filler import process_invoice

    print()
    print(bold("=" * 55))
    print(bold("  MRA ORDER FORM FILLER — STEP-BY-STEP TEST"))
    print(bold("  ⚠  DRY RUN: ClickUp statuses will NOT be updated"))
    print(bold("=" * 55))

    # ── 1. Load config ─────────────────────────────────────────────────────────
    section("STEP 1 — Load Config")
    try:
        config = Config()
        print(f"  ClickUp list : {config.CLICKUP_LIST_ID}")
        print(f"  Trigger status: {config.TRIGGER_STATUS}")
        print(f"  Next status   : {config.NEXT_STATUS}  (will NOT be applied)")
        print(f"  Forms folder  : {config.SUNESTA_FORMS_FOLDER}")
        print(f"  Output folder : {config.FILLED_FORMS_FOLDER}")
        dbx_mode = "Dropbox" if config.DROPBOX_ACCESS_TOKEN else "Local filesystem"
        print(f"  Storage mode  : {dbx_mode}")
        print(green("  Config loaded OK"))
    except KeyError as e:
        print(red(f"  MISSING env var: {e}"))
        sys.exit(1)

    dropbox = DropboxClient(config) if config.DROPBOX_ACCESS_TOKEN else None

    pause()

    # ── Job options (wind sensor + LED) ────────────────────────────────────────
    section("JOB OPTIONS")
    ws = input("  Wind sensors — order WITH the awning? [y/N]: ").strip().lower()
    wind_sensor_stock = ws not in ("y", "yes")
    led = input("  LED lights   — order WITH the awning? [y/N]: ").strip().lower()
    led_stock = led not in ("y", "yes")
    job_options = {"wind_sensor_stock": wind_sensor_stock, "led_stock": led_stock}
    print()
    print(f"  Wind sensors : {'ORDER with awning' if not wind_sensor_stock else 'from STOCK (skip)'}")
    print(f"  LED lights   : {'ORDER with awning' if not led_stock else 'from STOCK (skip)'}")

    pause()

    # ── 2. Fetch ClickUp tasks ─────────────────────────────────────────────────
    section("STEP 2 — Fetch ClickUp Tasks")
    clickup = ClickUpClient(config)
    tasks = clickup.get_tasks_by_status(config.TRIGGER_STATUS)
    print(f"  Found {bold(str(len(tasks)))} task(s) with status '{config.TRIGGER_STATUS}':")
    for i, t in enumerate(tasks):
        po_raw = next(
            (f.get("value", "") for f in t.get("custom_fields", [])
             if f.get("name", "").strip().lower() == "po #"),
            ""
        )
        print(f"  [{i+1}] {t['name']}  —  PO #: {po_raw or dim('(not set)')}")

    pause()

    # ── 3. Process each task ───────────────────────────────────────────────────
    zoho = ZohoClient(config)
    all_filled  = []
    all_failed  = []

    for task_idx, task in enumerate(tasks):
        task_id   = task["id"]
        task_name = task["name"]

        section(f"TASK {task_idx + 1}/{len(tasks)} — {task_name}")

        # PO numbers
        po_numbers = clickup.get_po_numbers(task)
        if not po_numbers:
            print(yellow(f"  ⚠ No PO # on this task — skipping"))
            all_failed.append({"name": task_name, "reason": "No PO # field"})
            pause()
            continue

        print(f"  PO number(s): {bold(', '.join(po_numbers))}")

        for po_number in po_numbers:
            print()
            print(bold(f"  ── PO #{po_number}"))

            # ── Fetch invoice ──────────────────────────────────────────────────
            print(f"  Fetching Zoho invoice…", end=" ", flush=True)
            try:
                invoice = zoho.get_full_invoice(po_number)
            except Exception as e:
                print(red(f"FAILED — {e}"))
                import traceback; traceback.print_exc()
                all_failed.append({"name": task_name, "reason": str(e)})
                continue

            if not invoice:
                print(red(f"FAILED — invoice #{po_number} not found in Zoho"))
                all_failed.append({"name": task_name, "reason": "Invoice not found"})
                continue

            if not isinstance(invoice, dict):
                print(red(f"FAILED — unexpected type from Zoho: {type(invoice)} → {repr(invoice)[:200]}"))
                all_failed.append({"name": task_name, "reason": f"Bad Zoho response type: {type(invoice)}"})
                continue

            print(green("OK"))

            # Show invoice details
            print(f"  Customer  : {bold(invoice.get('customer_name', '?'))}")
            print(f"  Ship to   : {invoice.get('shipping_street','')}, "
                  f"{invoice.get('shipping_city','')}, "
                  f"{invoice.get('shipping_state','')} "
                  f"{invoice.get('shipping_zip','')}")
            print(f"  Phone     : {invoice.get('phone','')}")
            items = invoice.get("awning_items", [])
            if not items:
                print(red("  ⚠ No awning line items found — skipping form fill"))
                all_failed.append({"name": task_name, "reason": "No awning items"})
                continue

            print(f"  Awning items ({len(items)}):")
            for item in items:
                print(f"    • {item.get('name','?')}")
                if item.get("description"):
                    # print first 120 chars of description
                    desc = item["description"][:120].replace("\n", " ")
                    print(f"      {dim(desc)}")

            pause(f"  Press ENTER to fill {len(items)} form(s) with Claude AI…")

            # ── Fill forms ─────────────────────────────────────────────────────
            print(f"  Calling Claude AI to parse specs and fill PDF(s)…")
            try:
                results = process_invoice(
                    invoice=invoice,
                    po_number=po_number,
                    forms_folder=config.SUNESTA_FORMS_FOLDER,
                    output_folder=config.FILLED_FORMS_FOLDER,
                    anthropic_api_key=config.ANTHROPIC_API_KEY,
                    dropbox_client=dropbox,
                    job_options=job_options,
                )
            except Exception as e:
                print(red(f"  process_invoice crashed: {e}"))
                import traceback; traceback.print_exc()
                all_failed.append({"name": task_name, "reason": str(e)})
                continue

            # ── Show results ───────────────────────────────────────────────────
            for r in results:
                if r.success:
                    print(green(f"  ✅ {r.model.title()} form filled"))
                    print(f"     File : {bold(os.path.basename(r.pdf_path))}")
                    print(f"     Path : {dim(r.pdf_path)}")
                    all_filled.append({"name": f"{task_name} (PO #{po_number})",
                                       "model": r.model, "pdf_path": r.pdf_path})
                else:
                    print(red(f"  ❌ {r.item_name}: {r.error}"))
                    all_failed.append({"name": f"{task_name} (PO #{po_number})",
                                       "reason": r.error})

        pause(f"  Task done. Press ENTER for next task…")

    # ── 4. Final summary ───────────────────────────────────────────────────────
    section("FINAL SUMMARY")
    print(f"  Filled : {green(str(len(all_filled)))}")
    for o in all_filled:
        print(f"    ✅ {o['name']}  [{o['model'].title()}]")
        print(f"       {dim(o['pdf_path'])}")

    print(f"  Failed : {red(str(len(all_failed))) if all_failed else green('0')}")
    for o in all_failed:
        print(f"    ❌ {o['name']}  — {o['reason']}")

    print()
    if not all_failed:
        print(bold(green("All tasks processed successfully. ✓")))
    else:
        print(bold(yellow(f"Done with {len(all_failed)} failure(s).")))
    print()


if __name__ == "__main__":
    main()

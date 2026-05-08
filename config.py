"""config.py — loads all settings from environment / .env file."""
import os
from dotenv import load_dotenv

load_dotenv(override=True)

# Base directory of this file — used for relative path defaults
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    # ── ClickUp ──────────────────────────────────────────────────────────────
    CLICKUP_API_KEY = os.environ["CLICKUP_API_KEY"]
    CLICKUP_LIST_ID = os.environ["CLICKUP_LIST_ID"]
    TRIGGER_STATUS  = os.environ.get("TRIGGER_STATUS", "GIVEN TO JAY")
    NEXT_STATUS     = os.environ.get("NEXT_STATUS",    "Sent to Zack")

    # ── Sunesta form templates (read-only blank PDFs) ─────────────────────────
    # On Railway: bundled under ./sunesta_forms/ in the repo.
    # Override via SUNESTA_FORMS_FOLDER env var if needed.
    SUNESTA_FORMS_FOLDER = os.environ.get(
        "SUNESTA_FORMS_FOLDER",
        os.path.join(_BASE_DIR, "sunesta_forms"),
    )

    # ── Output: where filled PDFs are saved ───────────────────────────────────
    # On Railway this is ephemeral local storage (./output/).
    # Point FILLED_FORMS_FOLDER at a mounted volume or cloud path for persistence.
    FILLED_FORMS_FOLDER = os.environ.get(
        "FILLED_FORMS_FOLDER",
        os.path.join(_BASE_DIR, "output"),
    )

    # ── Zoho Invoice ─────────────────────────────────────────────────────────
    ZOHO_CLIENT_ID       = os.environ["ZOHO_CLIENT_ID"]
    ZOHO_CLIENT_SECRET   = os.environ["ZOHO_CLIENT_SECRET"]
    ZOHO_REFRESH_TOKEN   = os.environ["ZOHO_REFRESH_TOKEN"]
    ZOHO_ORGANIZATION_ID = os.environ["ZOHO_ORGANIZATION_ID"]
    ZOHO_REGION          = os.environ.get("ZOHO_REGION", "com")

    # ── Anthropic ────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

    # ── Slack ────────────────────────────────────────────────────────────────
    SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
    SLACK_CHANNEL     = os.environ.get("SLACK_CHANNEL",     "")

    # ── Dropbox (optional — enables cloud storage on Railway) ────────────────────
    # If set, filled PDFs are uploaded to Dropbox instead of local filesystem.
    # Leave unset for local dev (files saved to FILLED_FORMS_FOLDER as before).
    DROPBOX_ACCESS_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
    DROPBOX_FOLDER       = os.environ.get("DROPBOX_FOLDER", "/MRA Order Forms")

    # ── Scheduler ────────────────────────────────────────────────────────────
    TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")

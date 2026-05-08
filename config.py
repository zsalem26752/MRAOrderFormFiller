"""config.py — loads all settings from environment / .env file."""
import os
from dotenv import load_dotenv

load_dotenv(override=True)


class Config:
    def __init__(self):
        # ── ClickUp ──────────────────────────────────────────────────────────
        self.CLICKUP_API_KEY = os.environ["CLICKUP_API_KEY"]
        self.CLICKUP_LIST_ID = os.environ["CLICKUP_LIST_ID"]
        self.TRIGGER_STATUS  = os.environ.get("TRIGGER_STATUS", "GIVEN TO JAY")
        self.NEXT_STATUS     = os.environ.get("NEXT_STATUS",    "Sent to Zack")

        # ── Sunesta form templates (read-only blank PDFs) ─────────────────────
        _base = os.path.dirname(os.path.abspath(__file__))
        self.SUNESTA_FORMS_FOLDER = os.environ.get(
            "SUNESTA_FORMS_FOLDER",
            os.path.join(_base, "sunesta_forms"),
        )

        # ── Output: where filled PDFs are saved (local mode only) ─────────────
        self.FILLED_FORMS_FOLDER = os.environ.get(
            "FILLED_FORMS_FOLDER",
            os.path.join(_base, "output"),
        )

        # ── Zoho Invoice ──────────────────────────────────────────────────────
        self.ZOHO_CLIENT_ID       = os.environ["ZOHO_CLIENT_ID"]
        self.ZOHO_CLIENT_SECRET   = os.environ["ZOHO_CLIENT_SECRET"]
        self.ZOHO_REFRESH_TOKEN   = os.environ["ZOHO_REFRESH_TOKEN"]
        self.ZOHO_ORGANIZATION_ID = os.environ["ZOHO_ORGANIZATION_ID"]
        self.ZOHO_REGION          = os.environ.get("ZOHO_REGION", "com")

        # ── Anthropic ─────────────────────────────────────────────────────────
        self.ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

        # ── Slack ─────────────────────────────────────────────────────────────
        self.SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
        self.SLACK_CHANNEL     = os.environ.get("SLACK_CHANNEL",     "")

        # ── Dropbox (OAuth2 refresh token — shared with Order Verifier) ───────
        # On Railway: DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
        # are set as shared env vars across both agents in the project.
        # When all three are present, the DropboxClient uses the API.
        # When absent (local dev without creds), it falls back to local filesystem.
        self.DROPBOX_APP_KEY       = os.environ.get("DROPBOX_APP_KEY",       "")
        self.DROPBOX_APP_SECRET    = os.environ.get("DROPBOX_APP_SECRET",    "")
        self.DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN", "")

        # Root of the local Dropbox folder (used to strip prefix when building API paths)
        self.DROPBOX_LOCAL_ROOT = os.environ.get(
            "DROPBOX_LOCAL_ROOT",
            "/Users/zsalem/Mr Awnings Dropbox",
        )

        # The Dropbox folder (or local path) where filled forms are saved.
        # On Railway: an API path like "/Mr Awnings/01.01 - SUNESTA/03.00 - 2026 ORDERS/2026 FILLED FORMS"
        # Locally:    a full path like "/Users/zsalem/Mr Awnings Dropbox/Mr Awnings/..."
        self.DROPBOX_FOLDER = os.environ.get(
            "DROPBOX_FOLDER",
            os.environ.get(
                "FILLED_FORMS_FOLDER",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"),
            ),
        )

        # ── Scheduler ─────────────────────────────────────────────────────────
        self.TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")

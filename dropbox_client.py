"""dropbox_client.py — Upload / download filled order-form PDFs.

Two modes depending on whether Dropbox API credentials are set:

  API mode  (Railway / any server)
    Requires: DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
    Uses the official Dropbox SDK (OAuth2 refresh token — never expires).

  Local mode  (Mac with Dropbox desktop app)
    Falls back to plain filesystem access when API creds are absent.
    No extra credentials needed — Dropbox must be synced locally.
"""

import os
import logging
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError

log = logging.getLogger(__name__)


class DropboxClient:
    def __init__(self, config):
        self.config = config
        self._dbx   = None

        if self._api_enabled:
            try:
                import dropbox
                self._dbx = dropbox.Dropbox(
                    oauth2_refresh_token=config.DROPBOX_REFRESH_TOKEN,
                    app_key=config.DROPBOX_APP_KEY,
                    app_secret=config.DROPBOX_APP_SECRET,
                )
                # For Dropbox Business/Team accounts, switch to the team root namespace
                # so paths like /Mr Awnings/... resolve correctly.
                try:
                    team_client = self._dbx.with_path_root(
                        dropbox.common.PathRoot.namespace_id(
                            self._dbx.users_get_current_account().root_info.root_namespace_id
                        )
                    )
                    self._dbx = team_client
                    log.info("Dropbox: team namespace root applied")
                except Exception as _ns_err:
                    log.info(f"Dropbox: not a team account or namespace unavailable ({_ns_err}), using personal root")

                log.info("Dropbox: using API mode")
            except ImportError:
                log.error("dropbox package not installed. Run: pip install dropbox")
                raise
        else:
            log.info("Dropbox: using local filesystem mode (no API creds set)")

    # ── Public methods ────────────────────────────────────────────────────────

    def upload(self, pdf_bytes: bytes, path: str) -> str:
        """Upload bytes to Dropbox (or save to local disk).

        `path` is either:
          - An API path  ("/Mr Awnings/.../filename.pdf")     when API mode
          - A local path ("/Users/.../Dropbox/.../filename.pdf") when local mode

        Returns the path that was used (for storage in run history).
        """
        if self._api_enabled:
            api_path = self._api_path(path)
            self._dbx.files_upload(pdf_bytes, api_path, mode=WriteMode.overwrite)
            log.info(f"  Dropbox upload OK: {api_path}")
            return api_path
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(pdf_bytes)
            log.info(f"  Saved locally: {path}")
            return path

    def download(self, path: str) -> bytes:
        """Download a file from Dropbox (or read from local disk)."""
        if self._api_enabled:
            api_path = self._api_path(path)
            _, response = self._dbx.files_download(api_path)
            return response.content
        else:
            with open(path, "rb") as f:
                return f.read()

    def build_path(self, subfolder: str, filename: str) -> str:
        """Return the full destination path for a filled PDF.

        In API mode:   <DROPBOX_FOLDER_as_api_path>/<subfolder>/<filename>
        In local mode: <DROPBOX_FOLDER>/<subfolder>/<filename>
        """
        base = self.config.DROPBOX_FOLDER.rstrip("/")
        if self._api_enabled:
            base = self._api_path(base)
        return f"{base}/{subfolder}/{filename}"

    def test_connection(self) -> dict:
        """Verify credentials by fetching account info. Returns name + email."""
        if self._api_enabled:
            account = self._dbx.users_get_current_account()
            return {"name": account.name.display_name, "email": account.email}
        return {"name": "(local mode)", "email": ""}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def _api_enabled(self) -> bool:
        return bool(getattr(self.config, "DROPBOX_REFRESH_TOKEN", ""))

    def _api_path(self, local_path: str) -> str:
        """Convert a local Dropbox sync path to a Dropbox API path.

        e.g.  /Users/zsalem/Mr Awnings Dropbox/Mr Awnings/Orders
              → /Mr Awnings/Orders

        If the path doesn't start with DROPBOX_LOCAL_ROOT it's assumed to
        already be an API path and is returned as-is.
        """
        root = getattr(self.config, "DROPBOX_LOCAL_ROOT", "").rstrip("/")
        if root and local_path.startswith(root):
            api_path = local_path[len(root):]
            if not api_path.startswith("/"):
                api_path = "/" + api_path
            return api_path
        return local_path

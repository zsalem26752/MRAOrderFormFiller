"""dropbox_client.py — Dropbox API wrapper for storing filled PDFs in the cloud.

Used automatically when DROPBOX_ACCESS_TOKEN is set in the environment.
Falls back to local filesystem storage when the token is absent.
"""

import logging
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError

log = logging.getLogger(__name__)


class DropboxClient:
    def __init__(self, config):
        self._token  = config.DROPBOX_ACCESS_TOKEN
        self._folder = config.DROPBOX_FOLDER.rstrip("/")
        self._dbx    = dropbox.Dropbox(self._token)

    def upload(self, pdf_bytes: bytes, dropbox_path: str) -> str:
        """Upload bytes to Dropbox at dropbox_path, overwriting if it exists.

        Returns the Dropbox path on success.
        """
        self._dbx.files_upload(pdf_bytes, dropbox_path, mode=WriteMode.overwrite)
        log.info(f"  Dropbox upload OK: {dropbox_path}")
        return dropbox_path

    def download(self, dropbox_path: str) -> bytes:
        """Download a file from Dropbox and return its bytes."""
        _, response = self._dbx.files_download(dropbox_path)
        return response.content

    def create_folder(self, dropbox_path: str) -> None:
        """Create a folder at dropbox_path (no-op if it already exists)."""
        try:
            self._dbx.files_create_folder_v2(dropbox_path)
            log.info(f"  Dropbox folder created: {dropbox_path}")
        except ApiError as e:
            # folder_conflict means it already exists — that's fine
            if e.error.is_path() and e.error.get_path().is_conflict():
                pass
            else:
                raise

    def build_path(self, subfolder: str, filename: str) -> str:
        """Return a full Dropbox path: {DROPBOX_FOLDER}/{subfolder}/{filename}."""
        return f"{self._folder}/{subfolder}/{filename}"

    def test_connection(self) -> dict:
        """Verify the token is valid by calling account info. Returns account info dict."""
        account = self._dbx.users_get_current_account()
        return {
            "name":  account.name.display_name,
            "email": account.email,
        }

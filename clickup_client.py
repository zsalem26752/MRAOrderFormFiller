"""clickup_client.py — ClickUp REST API wrapper."""

import logging
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.clickup.com/api/v2"


class ClickUpClient:
    def __init__(self, config):
        self.config  = config
        self.headers = {
            "Authorization": config.CLICKUP_API_KEY,
            "Content-Type":  "application/json",
        }

    # ── Public methods ────────────────────────────────────────────────────────

    def get_tasks_by_status(self, status: str) -> list:
        """Return all open tasks in the configured list that match `status`."""
        url    = f"{BASE_URL}/list/{self.config.CLICKUP_LIST_ID}/task"
        params = {"statuses[]": status, "include_closed": "false", "page": 0}
        tasks  = []
        while True:
            resp = self._get(url, params=params)
            page = resp.get("tasks", [])
            tasks.extend(page)
            if not resp.get("last_page", True):
                params["page"] += 1
            else:
                break
        return tasks

    def get_po_numbers(self, task: dict) -> list[str]:
        """Extract PO # values from the task's custom fields."""
        for field in task.get("custom_fields", []):
            if field.get("name", "").strip().lower() == "po #":
                raw   = field.get("value", "") or ""
                parts = [p.strip() for p in raw.split(",") if p.strip()]
                if parts:
                    log.info(f"  PO #(s) found: {parts}")
                    return parts
                else:
                    log.warning(f"  'PO #' field is empty for task: {task['name']}")
                    return []
        log.warning(f"  No 'PO #' custom field found on task: {task['name']}")
        return []

    def update_status(self, task_id: str, status: str) -> dict:
        """Update a task's status."""
        url  = f"{BASE_URL}/task/{task_id}"
        resp = self._put(url, json={"status": status})
        log.info(f"  ClickUp status updated: {task_id} → '{status}'")
        return resp

    def add_comment(self, task_id: str, comment_text: str) -> dict:
        """Post a comment to a task."""
        url = f"{BASE_URL}/task/{task_id}/comment"
        return self._post(url, json={"comment_text": comment_text})

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get(self, url, **kwargs):
        kwargs.setdefault("timeout", 30)
        r = requests.get(url, headers=self.headers, **kwargs)
        self._raise(r)
        return r.json()

    def _post(self, url, **kwargs):
        kwargs.setdefault("timeout", 30)
        r = requests.post(url, headers=self.headers, **kwargs)
        self._raise(r)
        return r.json()

    def _put(self, url, **kwargs):
        kwargs.setdefault("timeout", 30)
        r = requests.put(url, headers=self.headers, **kwargs)
        self._raise(r)
        return r.json()

    @staticmethod
    def _raise(response):
        try:
            response.raise_for_status()
        except requests.HTTPError:
            log.error(f"ClickUp API error {response.status_code}: {response.text[:300]}")
            raise

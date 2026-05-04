"""slack_notifier.py — Slack notifications for the Order Form Filler agent."""

import logging
import requests

log = logging.getLogger(__name__)


class SlackNotifier:
    def __init__(self, config):
        self.webhook = config.SLACK_WEBHOOK_URL
        self.channel = config.SLACK_CHANNEL

    def filled_summary(self, results: list[dict]):
        """
        Post a success summary after forms are filled.
        Each result dict: {"name": str, "model": str, "pdf_path": str}
        """
        lines = []
        for r in results:
            lines.append(f"✅ *{r['name']}* — {r['model'].title()} form filled")
        self._send(
            text=f"*Order forms filled and sent to verification ({len(results)})*\n\n" + "\n".join(lines),
            color="#00e5a0",
        )

    def failed_summary(self, failures: list[dict]):
        """
        Post a failure summary.
        Each failure dict: {"name": str, "reason": str}
        """
        lines = []
        for f in failures:
            lines.append(f"❌ *{f['name']}*\n      {f['reason']}")
        self._send(
            text=f"*Form filler — orders requiring attention ({len(failures)})*\n\n" + "\n".join(lines),
            color="#ff4d6d",
        )

    def error(self, message: str):
        self._send(text=f"🚨 *Form Filler error:* {message}", color="#ff4d6d")

    def _send(self, text: str, color: str = "#4a8cff"):
        if not self.webhook:
            log.debug("Slack webhook not configured — skipping.")
            return
        payload: dict = {
            "attachments": [{
                "color":      color,
                "text":       text,
                "mrkdwn_in":  ["text"],
                "footer":     "Mr. Awnings · Order Form Filler",
            }]
        }
        if self.channel:
            payload["channel"] = self.channel
        try:
            resp = requests.post(self.webhook, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"Slack notification failed (non-fatal): {e}")

"""zoho_client.py — Zoho Invoice API integration.

Uses OAuth2 refresh-token flow to stay authenticated.
Provides get_full_invoice() which returns customer info + all awning line items.
"""

import logging
import requests
from typing import Optional

log = logging.getLogger(__name__)

# Exact awning product names (lowercased, straight quotes) that should produce a
# filled order form. Anything not in this set is ignored — no blocklist needed.
# Note: Zoho sometimes uses curly/smart quotes; _normalize_name() handles that.
_AWNING_NAMES = {
    'sunesta "sunesta" motorized awning',
    'sunesta "sunstyle" motorized awning',
    'sunesta "sunlight" motorized awning',
    'sunesta "sunlite" motorized awning',   # alternate spelling used on some invoices
}


def _normalize_name(name: str) -> str:
    """Lowercase and replace curly/smart quotes with straight quotes."""
    return (
        name.lower()
        .replace('“', '"').replace('”', '"')  # " "
        .replace('‘', "'").replace('’', "'")  # ' '
    )


class ZohoClient:
    def __init__(self, config):
        self.config       = config
        self.access_token = None
        self._refresh()

    # ── Public methods ────────────────────────────────────────────────────────

    def get_full_invoice(self, po_number: str) -> Optional[dict]:
        """
        Return a structured dict with all invoice data needed to fill a form:
        {
          "invoice_number": str,
          "customer_name":  str,
          "billing_street": str,
          "billing_city":   str,
          "billing_state":  str,
          "billing_zip":    str,
          "shipping_street": str,
          "shipping_city":   str,
          "shipping_state":  str,
          "shipping_zip":    str,
          "phone":           str,
          "awning_items": [
              {
                "name":        str,
                "description": str,
                "quantity":    int,
              },
              ...
          ]
        }
        Returns None if the invoice is not found.
        """
        po = po_number.strip()
        invoice_id = (
            self._search_by_invoice_number(po)
            or self._search_by_invoice_number(_strip_suffix(po))
            or self._search_by_reference(po)
        )
        if not invoice_id:
            log.warning(f"  No Zoho invoice found for PO #: {po}")
            return None

        data    = self._get(f"/invoices/{invoice_id}")
        invoice = data.get("invoice", {})

        # ── Customer / address info ──────────────────────────────────────────
        billing  = invoice.get("billing_address",  {})
        shipping = invoice.get("shipping_address", {})

        # Zoho address fields vary — try common key names
        def _street(addr: dict) -> str:
            return addr.get("address", "") or addr.get("street", "") or ""

        result = {
            "invoice_number":  invoice.get("invoice_number", po),
            "customer_name":   invoice.get("customer_name", ""),
            "billing_street":  _street(billing),
            "billing_city":    billing.get("city",    ""),
            "billing_state":   billing.get("state",   ""),
            "billing_zip":     billing.get("zip",     "") or billing.get("zip_code", ""),
            "shipping_street": _street(shipping),
            "shipping_city":   shipping.get("city",   ""),
            "shipping_state":  shipping.get("state",  ""),
            "shipping_zip":    shipping.get("zip",    "") or shipping.get("zip_code", ""),
            "phone":           (invoice.get("contact_persons") or [{}])[0].get("phone", "")
                               if isinstance((invoice.get("contact_persons") or [None])[0], dict)
                               else invoice.get("phone", "") or "",
            "awning_items": [],
        }

        # ── Filter awning line items ─────────────────────────────────────────
        # Only the three known Sunesta product names produce an order form.
        # Everything else (disclaimers, installation, tax, freight, etc.) is ignored.
        for item in invoice.get("line_items", []):
            name = item.get("name", "") or ""
            desc = item.get("description", "") or ""

            if _normalize_name(name.strip()) not in _AWNING_NAMES:
                log.info(f"  Skipping non-awning item: {name[:60]}")
                continue

            result["awning_items"].append({
                "name":        name,
                "description": desc,
                "quantity":    int(item.get("quantity", 1) or 1),
            })
            log.info(f"  Found awning item: {name[:80]}")

        log.info(f"  Invoice {po}: {len(result['awning_items'])} awning item(s) found.")
        return result

    def get_invoice_pdf(self, po_number: str) -> bytes:
        """Download the Zoho invoice as a PDF and return raw bytes."""
        po = po_number.strip()
        invoice_id = (
            self._search_by_invoice_number(po)
            or self._search_by_invoice_number(_strip_suffix(po))
            or self._search_by_reference(po)
        )
        if not invoice_id:
            raise RuntimeError(f"Invoice #{po} not found in Zoho.")
        url  = f"{self._base()}/invoices/{invoice_id}?accept=pdf"
        resp = requests.get(url, headers=self._headers, timeout=30)
        if resp.status_code == 401:
            self._refresh()
            resp = requests.get(url, headers=self._headers, timeout=30)
        resp.raise_for_status()
        return resp.content

    # ── Private helpers ───────────────────────────────────────────────────────

    def _refresh(self):
        region = self.config.ZOHO_REGION
        url    = f"https://accounts.zoho.{region}/oauth/v2/token"
        resp   = requests.post(url, params={
            "grant_type":    "refresh_token",
            "client_id":     self.config.ZOHO_CLIENT_ID,
            "client_secret": self.config.ZOHO_CLIENT_SECRET,
            "refresh_token": self.config.ZOHO_REFRESH_TOKEN,
        }, timeout=30)
        resp.raise_for_status()
        self.access_token = resp.json()["access_token"]
        log.debug("Zoho access token refreshed.")

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Zoho-oauthtoken {self.access_token}",
            "X-com-zoho-invoice-organizationid": self.config.ZOHO_ORGANIZATION_ID,
        }

    def _base(self) -> str:
        return f"https://www.zohoapis.{self.config.ZOHO_REGION}/invoice/v3"

    def _get(self, path: str, params: dict = None) -> dict:
        url  = f"{self._base()}{path}"
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        if resp.status_code == 401:
            self._refresh()
            resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _search_by_invoice_number(self, invoice_number: str) -> Optional[str]:
        if not invoice_number:
            return None
        data     = self._get("/invoices", {"invoice_number": invoice_number})
        invoices = data.get("invoices", [])
        if invoices:
            log.info(f"  Zoho: matched invoice_number '{invoice_number}' → {invoices[0]['invoice_id']}")
            return invoices[0]["invoice_id"]
        return None

    def _search_by_reference(self, ref: str) -> Optional[str]:
        if not ref:
            return None
        data     = self._get("/invoices", {"reference_number": ref})
        invoices = data.get("invoices", [])
        if invoices:
            log.info(f"  Zoho: matched reference_number '{ref}' → {invoices[0]['invoice_id']}")
            return invoices[0]["invoice_id"]
        return None


def _strip_suffix(po: str) -> str:
    if "-" in po:
        base = po.rsplit("-", 1)[0]
        if base.isdigit():
            return base
    return po

"""
form_filler.py — Core order-form filling logic.

Workflow for each awning line item:
  1. Detect the model (Sunesta / Sunstyle / Sunlight) from the invoice text.
  2. Call Claude to parse all specs from the invoice name + description into JSON.
  3. Map those specs to the correct PDF form-field names for the detected model.
  4. Fill the blank PDF template using pypdf and save to the output folder.

Returns a list of FillResult objects — one per awning item filled.
"""

import io
import os
import re
import json
import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import anthropic
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, create_string_object

log = logging.getLogger(__name__)

# ── Form template filenames ───────────────────────────────────────────────────
FORM_FILES = {
    "sunesta":  "Sunesta Sunesta.pdf",
    "sunstyle": "Sunstyle.pdf",
    "sunlight": "Sunlight.pdf",
}

# ── Row-prefix constants (exactly as they appear in the PDFs) ─────────────────
_SE = "Part  Qty Description Comments Retail Price Each Discoun t  Width Projection Extended Retail Total  1"
_SS = "Part  Qty Description Comments Retail Price Each Discount  Width Projection Extended Retail Total  1"
_SL = "Extended Retail Total  Description Comments Retail Price Each Discount  Width Projection Part  Qty 1"

# ── Wind sensor part numbers by frame color ───────────────────────────────────
# Sunesta field: 'Sun Sensor' (text, qty) — fill with "1"
# Sunstyle field: 'Sun Sensor Check Box' (checkbox) + text field for qty
WIND_SENSOR_BY_COLOR = {
    "white": "304326",
    "clay":  "304326",
    "beige": "304326",
    "brown": "304326",
    "black": "304326",
}
# All colors use 304326 for now; key separate to allow future per-color split.

# ── Bracket count by width ────────────────────────────────────────────────────
def _bracket_count(width_ft: float) -> int:
    if width_ft <= 9:   return 2
    if width_ft <= 10:  return 3
    if width_ft <= 16:  return 4
    if width_ft <= 17:  return 5
    if width_ft <= 21:  return 6
    if width_ft <= 22:  return 7
    return 8


# ── FIELD_MAP ─────────────────────────────────────────────────────────────────
#
# Keys are logical slot names used by build_field_values().
# Values are the EXACT PDF field names from the form.
#
# Column suffixes for Row1:
#   (none)/_1 = Part #   _2 = Qty   _4 = Description   _5 = Comments
#   _6 = Retail   _7 = Discount   _8 = Width   _9 = Projection
#   _10 = Extended Retail   _11 = Total

FIELD_MAP = {

    # ── SUNESTA SUNESTA ───────────────────────────────────────────────────────
    "sunesta": {
        # ── Header ────────────────────────────────────────────────────────────
        "salesperson":   "SALESPERSON",
        "date":          "Order Form Date",
        "number":        "Unit Number",
        "page":          "Unit Number Box 2",
        "of":            "Total Units",
        "customer":      "Customer Number",
        "po":            "PO Number",
        "phone":         "Bill To Phone",

        # ── Row 1 — main awning line ───────────────────────────────────────
        "row1_width": "Width",
        "row1_proj":  "Projection/Depth",
        "row1_qty":   "Awning QTY",

        # ── Case type ─────────────────────────────────────────────────────
        "cb_standard":  "NO Smart Case Check Box",
        "cb_smartcase": "Smart Case Check Box 1",

        # ── SmartCase accessory ───────────────────────────────────────────
        "sc_check":   "Smart Case Check Box 2",
        "sc_qty":     "Smart Case QTY",

        # ── Smart Hood ───────────────────────────────────────────────────
        "hood_check": "Smart Hood Check Box 1",
        "hood_qty":   "Smart Hood QTY 1",

        # ── Smart Drop ───────────────────────────────────────────────────
        "smartdrop_check":       "Smart Drop Check Box",
        "smartdrop_qty":         "Smart Drop QTY",
        "smartdrop_motor_check": "Smart Drop Motor Check Box",
        "smartdrop_motor_qty":   "Smart Drop Motor QTY",

        # ── Frame color ───────────────────────────────────────────────────
        "cb_clay":   "Clay Frame Color",
        "cb_black":  "Black Frame Color",
        "cb_brown":  "Brown Frame Color",
        "cb_white":  "White Frame Color",
        "cb_beige":  "Beige Frame Color",

        # ── Drive side ────────────────────────────────────────────────────
        "drive_left_text": "Left",
        "cb_drive_left":   "Left Motor Check Box",
        "cb_drive_right":  "Right Motor Check Box",

        # ── Bracket type ──────────────────────────────────────────────────
        "cb_wall_brkt": "Wall Brackets Check Box",
        "wall_brkt_qty": "Wall Bracket QTY",
        "cb_ceil_brkt": "Soffit Bracket Check Box",
        "ceil_brkt_qty": "Soffit Bracket QTY",
        "cb_roof_brkt": "Roof Bracket Check Box",
        "roof_brkt_qty": "Roof Bracket QTY",

        # ── Motor ─────────────────────────────────────────────────────────
        "motor_check": "Motor Check Box",
        "motor_qty":   "Motor QTY",

        # ── Manual crank (non-motorized awning) ───────────────────────────
        "manual_crank_check": "Manual Crank Check Box",
        "manual_crank_qty":   "Manual Crank QTY",

        # ── Motion/Wind sensor ────────────────────────────────────────────
        "wind_sensor_check": "Motion/Wind Sensor Check Box",
        "wind_sensor_qty":   "Motion/Wind Sensor QTY",
        "cb_sensor_black":   "Black Motion/Wind Sensor QTY",
        "cb_sensor_white":   "White Motion/Wind Sensor QTY",
        "cb_sensor_beige":   "Beige Motion/Wind Sensor QTY",

        # ── Tahoma/MyLink ─────────────────────────────────────────────────
        "tahoma_check": "Tahoma/MyLink Check Box",
        "tahoma_qty":   "Tahoma/MyLink QTY",

        # ── Hand crank lengths ────────────────────────────────────────────
        "hcrank_check": "4'7\" Hand Crank Check Box",
        "hcrank_qty":   "4'7\" Hand Crank QTY",

        # ── LED ───────────────────────────────────────────────────────────
        "led_check": "LED Lights Check Box",
        "led_qty":   "LED Lights QTY",

        # ── Fabric ───────────────────────────────────────────────────────
        "deck_fabric":      "Fabric # Box 1",
        "valance_fabric":   "Fabric # Box 2",
        "smartdrop_fabric": "Smart Drop Fabric #",

        # ── Valance ───────────────────────────────────────────────────────
        "cb_valance_6":  "6\" Valance Check Box",
        "cb_valance_9":  "9\" Valance Check Box",
        "cb_valance_11": "11\" Valance Check Box",
        "valance_style":   "Valance Style Number",
        "valance_binding": "Valance Binding",

        # ── Notes ─────────────────────────────────────────────────────────
        "notes": "NOTES",
    },

    # ── SUNSTYLE ──────────────────────────────────────────────────────────────
    "sunstyle": {
        # ── Header ────────────────────────────────────────────────────────────
        "salesperson":   "SALESPERSON",
        "date":          "Order Form Date",
        "number":        "Unit Number",
        "page":          "Unit Number Box 2",
        "of":            "Total Units",
        "customer":      "Customer Number",
        "po":            "PO Number",

        # ── Row 1 — main awning line ──────────────────────────────────────────
        "row1_width": "Width",
        "row1_proj":  "Projection/Depth",
        "row1_qty":   "Part  Qty Description Comments Retail Price Each Discount  Width Projection Extended Retail Total  1Row1_4",

        # ── Case type ─────────────────────────────────────────────────────────
        "cb_standard":  "No Smart Case Check Box",
        "cb_smartcase": "Smart Case Check Box 1",

        # ── SmartCase accessory row ───────────────────────────────────────────
        "sc_check":   "Smart Case Check Box 2",
        "sc_qty":     "Smart Case QTY",

        # ── Frame color ───────────────────────────────────────────────────────
        "cb_clay":   "Clay Arms Check Box",
        "cb_brown":  "Brown Arms Check Box",
        "cb_white":  "White Arms Check Box",
        "cb_beige":  "Beige Arms Check Box",

        # ── Drive side ────────────────────────────────────────────────────────
        "cb_drive_left":  "Left Motor Check Box",
        "cb_drive_right": "Right Motor Check Box",

        # ── Bracket type ──────────────────────────────────────────────────────
        "cb_wall_brkt":  "Wall Bracket Check Box",
        "wall_brkt_qty": "14048",
        "cb_ceil_brkt":  "Soffit Bracket Check Box",
        "ceil_brkt_qty": "14049",
        "cb_roof_brkt":  "Roof Bracket check box",
        "roof_brkt_qty": "11237",

        # ── Wall bracket for hood ─────────────────────────────────────────────
        "cb_wall_brkt_hood": "Wall Bracket for Hood Check Box",
        "wall_brkt_hood_qty": "11164",

        # ── Motor (standard / no SmartCase) ──────────────────────────────────
        "motor_check":     "NO Smart Case Motor Check Box",
        "motor_qty":       "NO Smart Case Motor Check Box QTY",

        # ── Motor (SmartCase) ─────────────────────────────────────────────────
        "sc_motor_check":  "Smart Case Motor Check Box",
        "sc_motor_qty":    "Smart Case Motor Check Box QTY",

        # ── Smart Hood ────────────────────────────────────────────────────────
        "hood_check":   "Smart Hood Check Box 1",
        "hood_qty":     "Smart Hood Check Box 1 QTY",
        "hood_check_2": "Smart Hood Check Box 2",
        "hood_qty_2":   "Smart Hood Check Box 2 QTY",
        "hood_check_3": "Smart Hood Check Box 3",
        "hood_qty_3":   "Smart Hood Check Box 3 QTY",
        "hood_check_roof": "Smart Hood Check Box for ROOF Brackets",
        "hood_qty_roof":   "Smart Hood Check Box for ROOF Brackets QTY",

        # ── Smart Drop ────────────────────────────────────────────────────────
        "smartdrop_check":       "Smart Drop Check Box",
        "smartdrop_qty":         "Smart Drop Check Box QTY",
        "smartdrop_motor_check": "Smart Drop Motor Check Box",
        "smartdrop_motor_qty":   "Smart Drop Motor QTY",

        # ── Motion/Wind sensor ────────────────────────────────────────────────
        "wind_sensor_check": "Motion/Wind Sensor Check Box",
        "wind_sensor_qty":   "Motion/Wind Sensor QTY",
        "cb_sensor_black":   "Black Motion/Wind Sensor QTY",
        "cb_sensor_white":   "White Motion/Wind Sensor QTY",
        "cb_sensor_beige":   "Beige Motion/Wind Sensor QTY",

        # ── Hand crank ────────────────────────────────────────────────────────
        "hcrank_check": "4'7\" Hand Crank Check Box",
        "hcrank_qty":   "4'7\" Hand Crank Check Box QTY",

        # ── LED ───────────────────────────────────────────────────────────────
        "led_check": "LED Lights Check Box",
        "led_qty":   "LED Lights QTY",

        # ── Tahoma/MyLink ─────────────────────────────────────────────────────
        "tahoma_check": "Tahoma/MyLink Check Box",
        "tahoma_qty":   "Tahoma/MyLink QTY",

        # ── SmartTilt ─────────────────────────────────────────────────────────
        "cb_smarttilt": "Smart Tilt Check Box",
        "smarttilt_qty": "Smart Tilt QTY",

        # ── Fabric ────────────────────────────────────────────────────────────
        "deck_fabric":      "Fabric # Box 1",
        "valance_fabric":   "Fabric # Box 2",
        "smartdrop_fabric": "Smart Drop Fabric #",

        # ── Valance ───────────────────────────────────────────────────────────
        "cb_valance_6":    "6\" Valance Check Box",
        "cb_valance_9":    "9\" Valance Check Box",
        "cb_valance_11":   "11\" Valance Check Box",
        "valance_style":   "Valance Style Number",
        "valance_binding": "Valance Binding",

        # ── Notes ─────────────────────────────────────────────────────────────
        "notes": "NOTES",
    },

    # ── SUNLIGHT ──────────────────────────────────────────────────────────────
    "sunlight": {
        # ── Header ────────────────────────────────────────────────────────────
        "salesperson":   "SALESPERSON",
        "date":          "DATE",
        "number":        "NUMBER",
        "page":          "PAGE",
        "of":            "OF",
        "customer":      "CUST",
        "po":            "PO",

        # ── Row 1 — main awning line ──────────────────────────────────────────
        "row1_width": "Width",
        "row1_proj":  "Projection/Width",
        "row1_qty":   "Awning QTY",

        # ── Frame color ───────────────────────────────────────────────────────
        "cb_clay":   "Clay Frame Color Check Box",
        "cb_white":  "White Frame Color Check Box",
        "cb_beige":  "Beige Frame Color Check Box",

        # ── Drive side ────────────────────────────────────────────────────────
        "cb_drive_left":  "Left Motor Side Check Box",
        "cb_drive_right": "Right Motor Side Check Box",

        # ── Bracket type ──────────────────────────────────────────────────────
        "cb_wall_brkt":  "Wall Bracket Check Box",
        "wall_brkt_qty": "Wall Bracket QTY",
        "cb_ceil_brkt":  "Soffit Bracket Check Box",
        "ceil_brkt_qty": "Soffit Bracket QTY",
        "cb_roof_brkt":  "Roof Bracket Check Box",
        "roof_brkt_qty": "Roof Bracket QTY",

        # ── Motor ─────────────────────────────────────────────────────────────
        "motor_check": "Motor Check Box",
        "motor_qty":   "Motor QTY",

        # ── Manual crank (non-motorized) ──────────────────────────────────────
        "manual_crank_check": "Manual Crank Check Box",
        "manual_crank_qty":   "Manual Crank QTY",

        # ── Hand crank ────────────────────────────────────────────────────────
        "hcrank_check": "4'7\" Crank Check Box",
        "hcrank_qty":   "4'7\" Crank QTY",

        # ── Hood ──────────────────────────────────────────────────────────────
        "hood_check":   "Hood Check Box 1",
        "hood_qty":     "Hood QTY",
        "hood_check_2": "Hood Check Box 2",
        "hood_check_3": "Hood Check Box 3",
        "hood_end_cover_qty": "Hood End Cover QTY",
        "hood_bracket_qty":   "Hood Bracket QTY",

        # ── Motion/Wind sensor ────────────────────────────────────────────────
        "wind_sensor_check": "Motion/Wind Sensor Check Box",
        "wind_sensor_qty":   "Motion/Wind Sensor QTY",
        "cb_sensor_white":   "White Motion/Wind Sensor QTY",
        "cb_sensor_beige":   "Beige Motion/Wind Sensor QTY",

        # ── LED ───────────────────────────────────────────────────────────────
        "led_check": "LED Lights Check Box",
        "led_qty":   "LED Lights QTY",

        # ── Tahoma/MyLink ─────────────────────────────────────────────────────
        "tahoma_check": "Tahoma/MyLink Check Box",
        "tahoma_qty":   "Tahoma/MyLink QTY",

        # ── SmartTilt ─────────────────────────────────────────────────────────
        "cb_smarttilt":  "Smart Tilt Check Box",
        "smarttilt_qty": "Smart Tilt QTY",

        # ── Fabric ────────────────────────────────────────────────────────────
        "deck_fabric":    "Fabric # Box 1",
        "valance_fabric": "Fabric # Box 2",

        # ── Valance ───────────────────────────────────────────────────────────
        "cb_valance_6":    "6\" Valance Check Box",
        "cb_valance_9":    "9\" Valance Check Box",
        "cb_valance_11":   "11\" Valance Check Box",
        "valance_style":   "Valance Style Number",
        "valance_binding": "Valance Binding #",

        # ── Notes ─────────────────────────────────────────────────────────────
        "notes": "NOTES",
    },
}


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class FillResult:
    po_number:  str
    model:      str
    pdf_path:   str
    item_name:  str
    success:    bool = True
    error:      str  = ""


# ── Model detection ───────────────────────────────────────────────────────────

def detect_model(name: str, description: str) -> Optional[str]:
    text = (name + " " + description).lower()
    if "sunstyle" in text:
        return "sunstyle"
    if "sunlight" in text or "sunlite" in text:
        return "sunlight"
    if "sunesta" in text:
        return "sunesta"
    return None


# ── Dimension parser ──────────────────────────────────────────────────────────

def _width_to_ft(width_str: str) -> float:
    """Convert '20\'6"' or '20.5' to decimal feet."""
    if not width_str:
        return 0.0
    m = re.match(r"(\d+)'(\d+)", str(width_str))
    if m:
        return int(m.group(1)) + int(m.group(2)) / 12
    m = re.match(r"(\d+\.?\d*)", str(width_str))
    if m:
        return float(m.group(1))
    return 0.0


# ── Claude AI spec extraction ─────────────────────────────────────────────────

def parse_specs_with_claude(line_item: dict, model: str, anthropic_api_key: str) -> dict:
    """Call Claude to extract structured specs from an invoice line item."""
    client = anthropic.Anthropic(api_key=anthropic_api_key)

    prompt = f"""You are parsing a Sunesta awning order from a Zoho invoice line item.
The model is: {model.upper()}

Line item name: {line_item.get('name', '')}
Line item description:
{line_item.get('description', '')}

Extract every specification and return ONLY a valid JSON object with these exact keys:
{{
  "width":              "<feet'inches\"> e.g. \\"20'0\\"",
  "projection":         "<feet'inches\"> e.g. \\"13'0\\"",
  "frame_color":        "<Clay|Black|Brown|White|Beige>",
  "drive_side":         "<Left|Right|TBD>",
  "motorized":          <true|false>,
  "smart_case":         <true|false>,
  "smart_hood":         <true|false>,
  "smartdrop":          <true|false>,
  "smartdrop_motorized":<true|false>,
  "smarttilt":          <true|false>,
  "motion_sensor":      <true|false>,
  "led_lights":         <true|false>,
  "wind_sensor":        <true|false>,
  "hand_crank":         <true|false>,
  "bracket_type":       "<wall|ceiling|roof|gear|coupled|rafter|null>",
  "quantity":           <integer, default 1>,
  "deck_fabric":        "<fabric number or null>",
  "valance_fabric":     "<fabric number or null>",
  "smartdrop_fabric":   "<fabric number or null>",
  "valance_style":      "<style number integer, e.g. 5, or null>",
  "valance_height":     "<9 or 11 — the drop height in inches, or null>",
  "valance_binding":    "<MATCH|CONTRAST|null>",
  "remote_color":       "<Black|White|Beige|null>"
}}

Rules:
- Width and projection: parse the FIRST dimensions line e.g. "20' Wide X 13' Deep" → width="20'0\\"", projection="13'0\\""
- smart_case: true if the description says "Smart Case: YES" or "SmartCase: YES"
- smart_hood: true ONLY if the description explicitly says "Smart Hood: YES" or "Smart Hood.*YES". Do NOT set smart_hood based on "Drop Arm" — "Drop Arm (Smart Fold)" is a completely separate accessory and must never map to smart_hood.
- smartdrop: true if description says "Smart Drop.*YES" or "Drop Screen.*YES"
- smartdrop_motorized: true if "Motorized Drop Screen.*YES"
- wind_sensor: true if "Wireless Wind Sensor.*YES"
- led_lights: true if "LED Lights.*YES"
- hand_crank: true only if NOT motorized (manual awning)
- drive_side: if the motor side is "TBD", "N/A", blank, or unknown → return "TBD". Never guess or default to Left or Right.
- bracket_type: look for "Bracket Type:" line, e.g. "WALL" → "wall"
- quantity: from the invoice line item quantity field, default 1
- valance_style: the STYLE NUMBER (integer) from the valance line, e.g. "Valance #: 5 - Straight Hem, 9\\"" → 5. Not the drop height.
- valance_height: the drop height in inches — either 9 or 11. E.g. "9\\"" → 9. Return as integer string "9" or "11".
- valance_binding: if valance style is 2 → "MATCH". Otherwise look for explicit binding info; null if not specified.
- Return ONLY the JSON — no explanation, no markdown fences."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=900,
        messages=[{"role": "user", "content": prompt}],
        timeout=60.0,
    )
    text = resp.content[0].text.strip()

    if "```" in text:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        else:
            text = text.split("```")[1].split("```")[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"Claude returned invalid JSON: {e}\nRaw: {text[:500]}")
        raise RuntimeError(f"Failed to parse Claude response as JSON: {e}")


# ── Field value builder ───────────────────────────────────────────────────────

def build_field_values(
    specs: dict,
    invoice: dict,
    po_number: str,
    model: str,
    item_index: int,
    total_items: int,
    job_options: dict,
) -> tuple[dict, dict]:
    """
    Translate parsed specs + customer info into:
      texts:  {pdf_field_name: str_value}
      checks: {pdf_field_name: bool}

    job_options: dict with keys:
      "wind_sensor_stock": bool  — True = from stock (skip), False = order with awning
      "led_stock":         bool  — True = from stock (skip), False = order with awning
    """
    fm     = FIELD_MAP[model]
    texts  = {}
    checks = {}
    today  = datetime.now().strftime("%m/%d/%Y")

    def set_text(slot: str, value: str):
        if slot in fm and value:
            texts[fm[slot]] = str(value)

    def set_check(slot: str, value: bool):
        if slot in fm:
            checks[fm[slot]] = bool(value)

    # ── Computed values ───────────────────────────────────────────────────────
    width      = specs.get("width", "") or ""
    projection = specs.get("projection", "") or ""
    width_ft   = _width_to_ft(width)
    color      = (specs.get("frame_color") or "").strip().lower()
    drive_side = (specs.get("drive_side") or "").strip().upper()
    if drive_side == "TBD" or drive_side == "":
        raise ValueError(
            "Motor side is TBD or missing — cannot fill order form. "
            "Please confirm the motor side with the customer and update the invoice before reprocessing."
        )
    is_left    = drive_side == "LEFT"
    smart_case = bool(specs.get("smart_case"))
    # Smart Hood and Smart Case are mutually exclusive — Smart Case takes priority
    smart_hood = bool(specs.get("smart_hood")) and not smart_case
    motorized  = bool(specs.get("motorized", True))
    quantity   = int(specs.get("quantity") or 1)
    bracket    = (specs.get("bracket_type") or "").strip().lower()
    bracket_qty = _bracket_count(width_ft)

    # ── Header ────────────────────────────────────────────────────────────────
    set_text("salesperson", "Jay")
    set_text("date",        today)
    set_text("customer",    "11782")      # always 11782 (rule #3)

    # PO field: invoice# - LastName [-n if multiple items] (rule #4)
    last_name = invoice.get("customer_name", "").split()[-1] if invoice.get("customer_name") else ""
    if total_items > 1:
        po_text = f"{po_number} - {last_name} [{item_index + 1}]"
    else:
        po_text = f"{po_number} - {last_name}"
    set_text("po", po_text)

    # NUMBER / PAGE / OF — only fill when multiple awnings for same PO (rule #5)
    if total_items > 1:
        set_text("number", po_number)
        set_text("page",   str(item_index + 1))
        set_text("of",     str(total_items))

    # BILL TO / SHIP TO / ADDRESS / CITY / STATE / ZIP are pre-filled in the
    # template — do not touch them. Only fill PHONE if present.
    set_text("phone", invoice.get("phone", ""))

    # ── Row 1 — main awning line ──────────────────────────────────────────────
    set_text("row1_width", width)
    set_text("row1_proj",  projection)
    set_text("row1_qty",   str(quantity))

    # ── Frame color ───────────────────────────────────────────────────────────
    set_check("cb_clay",  color == "clay")
    set_check("cb_black", color == "black")
    set_check("cb_brown", color == "brown")
    set_check("cb_white", color == "white")
    set_check("cb_beige", color == "beige")

    # ── Drive side ────────────────────────────────────────────────────────────
    if model == "sunesta" and is_left and "drive_left_text" in fm:
        texts[fm["drive_left_text"]] = "X"
    set_check("cb_drive_left",  is_left)
    set_check("cb_drive_right", not is_left)

    # ── Case type (top header checkboxes) ────────────────────────────────────
    set_check("cb_standard",  not smart_case)
    set_check("cb_smartcase", smart_case)

    # ── Bracket type + qty ────────────────────────────────────────────────────
    is_wall     = bracket == "wall"
    is_ceil     = bracket in ("ceiling", "soffit")
    is_roof     = bracket == "roof"
    is_gear     = bracket == "gear"
    is_coupled  = bracket == "coupled"
    is_rafter   = bracket == "rafter"

    set_check("cb_wall_brkt", is_wall)
    set_check("cb_ceil_brkt", is_ceil)
    set_check("cb_roof_brkt", is_roof)
    if "cb_gear_brkt"   in fm: set_check("cb_gear_brkt",   is_gear)
    if "cb_coupled"     in fm: set_check("cb_coupled",     is_coupled)
    if "cb_rafter_brkt" in fm: set_check("cb_rafter_brkt", is_rafter)

    # Write bracket qty into the matching qty text field
    if is_wall and "wall_brkt_qty" in fm:
        set_text("wall_brkt_qty", str(bracket_qty))
    elif is_ceil and "ceil_brkt_qty" in fm:
        set_text("ceil_brkt_qty", str(bracket_qty))
    elif is_roof and "roof_brkt_qty" in fm:
        set_text("roof_brkt_qty", str(bracket_qty))

    # ── Motor ─────────────────────────────────────────────────────────────────
    if motorized:
        if model == "sunstyle":
            # Sunstyle has separate motor rows for standard vs SmartCase
            if smart_case:
                set_check("sc_motor_check", True)
                set_text("sc_motor_qty", "1")
            else:
                set_check("motor_check", True)
                set_text("motor_qty", "1")
        else:
            set_check("motor_check", True)
            set_text("motor_qty", "1")
    else:
        # Non-motorized: manual crank
        if "manual_crank_check" in fm:
            set_check("manual_crank_check", True)
            set_text("manual_crank_qty", "1")

    # ── 4'7" Hand Crank — always included on every order ─────────────────────
    if "hcrank_check" in fm:
        set_check("hcrank_check", True)
        set_text("hcrank_qty", "1")

    # ── Smart Case accessory row (row 8 in the body) ─────────────────────────
    # sc_check and sc_qty fill the SmartCase part row; cb_smartcase fills the
    # top header checkbox. Both must be set when smart_case is True.
    if smart_case:
        set_check("sc_check", True)
        set_text("sc_qty", "1")

    # ── Smart Hood ────────────────────────────────────────────────────────────
    if smart_hood:
        if "hood_check" in fm:
            set_check("hood_check", True)
        elif "cb_hood" in fm:
            set_check("cb_hood", True)
        set_text("hood_qty", "1")

    # ── Smart Drop ────────────────────────────────────────────────────────────
    smartdrop           = bool(specs.get("smartdrop"))
    smartdrop_motorized = bool(specs.get("smartdrop_motorized"))
    if smartdrop:
        if smartdrop_motorized:
            set_check("smartdrop_motor_check", True)
            set_text("smartdrop_motor_qty", "1")
        else:
            set_check("smartdrop_check", True)
            set_text("smartdrop_qty", "1")

    # ── Wind / Motion sensor (rule #12) ──────────────────────────────────────
    wind_from_invoice   = bool(specs.get("wind_sensor"))
    motion_from_invoice = bool(specs.get("motion_sensor"))
    order_wind          = not job_options.get("wind_sensor_stock", True)

    if (wind_from_invoice or motion_from_invoice) and order_wind:
        set_check("wind_sensor_check", True)
        set_text("wind_sensor_qty", "1")
        # Sensor color is determined by frame color:
        #   white  → white sensor
        #   beige  → beige sensor
        #   clay   → beige sensor
        #   black  → black sensor
        #   brown  → black sensor (default for any other color)
        sensor_color = {
            "white": "white",
            "beige": "beige",
            "clay":  "beige",
            "black": "black",
        }.get(color, "black")
        set_check("cb_sensor_black", sensor_color == "black")
        set_check("cb_sensor_white", sensor_color == "white")
        set_check("cb_sensor_beige", sensor_color == "beige")

    # ── Hand crank lengths (non-motorized, separate from manual crank) ────────
    # ── LED (rule #16) ────────────────────────────────────────────────────────
    led       = bool(specs.get("led_lights"))
    order_led = not job_options.get("led_stock", True)

    if led and order_led:
        set_check("led_check", True)
        set_text("led_qty", "1")
    # If led_stock=True (from stock), omit entirely — no checkbox, no qty

    # ── SmartTilt ────────────────────────────────────────────────────────────
    if bool(specs.get("smarttilt")):
        if "cb_smarttilt" in fm:
            set_check("cb_smarttilt", True)
        if "smarttilt_qty" in fm:
            set_text("smarttilt_qty", "1")

    # ── Fabric ────────────────────────────────────────────────────────────────
    # deck_fabric → Fabric # Box 1, valance_fabric → Fabric # Box 2.
    # If the invoice only lists one fabric number, use it for both boxes.
    deck_fab    = specs.get("deck_fabric")    or ""
    valance_fab = specs.get("valance_fabric") or deck_fab   # fall back to deck fabric
    set_text("deck_fabric",    deck_fab)
    set_text("valance_fabric", valance_fab)
    if smartdrop:
        set_text("smartdrop_fabric", specs.get("smartdrop_fabric") or "")

    # ── Valance ───────────────────────────────────────────────────────────────
    # valance_style  = the style NUMBER (e.g. 5) → goes in Valance Style Number text field
    # valance_height = the drop HEIGHT in inches (9 or 11) → drives the height checkbox
    # valance_binding: if style == "2" → always "MATCH"; otherwise use extracted value
    v_style   = str(specs.get("valance_style")  or "").strip()
    v_height  = str(specs.get("valance_height") or "").strip()
    v_binding = str(specs.get("valance_binding") or "").strip()

    # Both style and height are required — fail clearly if either is missing
    if not v_style:
        raise ValueError(
            "Valance style number is missing — cannot fill order form. "
            "Please confirm the valance style with the customer and update the invoice before reprocessing."
        )
    if not v_height:
        raise ValueError(
            "Valance height (9\" or 11\") is missing — cannot fill order form. "
            "Please confirm the valance height with the customer and update the invoice before reprocessing."
        )

    # Style number → text field
    if v_style:
        set_text("valance_style", v_style)

    # Binding: style 2 always requires MATCH
    if v_style == "2":
        v_binding = "MATCH"
    if v_binding:
        set_text("valance_binding", v_binding)

    # Height checkboxes — driven by valance_height (9 or 11 for Sunesta)
    if "cb_valance_6"  in fm: set_check("cb_valance_6",  v_height == "6")
    if "cb_valance_8"  in fm: set_check("cb_valance_8",  v_height == "8")
    if "cb_valance_9"  in fm: set_check("cb_valance_9",  v_height == "9")
    if "cb_valance_10" in fm: set_check("cb_valance_10", v_height == "10")
    if "cb_valance_11" in fm: set_check("cb_valance_11", v_height == "11")

    return texts, checks


# ── PDF writer ────────────────────────────────────────────────────────────────

def fill_pdf(template_path: str, text_fields: dict, checkbox_fields: dict) -> bytes:
    """Fill template and return filled PDF as bytes.

    checkbox_fields values are booleans. For /Btn fields we set /V and /AS.
    For /Tx fields that were named as checkboxes (common in some Acrobat forms),
    we write "✓" for True and "" for False via the text-field path.
    """
    reader = PdfReader(template_path)

    # Build a map of field_name → /FT so we can route correctly
    tx_check_fields = {}   # /Tx fields that appear in checkbox_fields
    btn_check_fields = {}  # /Btn fields that appear in checkbox_fields

    raw_fields = reader.get_fields() or {}
    for name, field in raw_fields.items():
        if name in checkbox_fields:
            ft = field.get("/FT", "")
            if str(ft) == "/Btn":
                btn_check_fields[name] = checkbox_fields[name]
            else:
                # Treat as text: "X" when True, blank when False
                if checkbox_fields[name]:
                    tx_check_fields[name] = "X"
                else:
                    tx_check_fields[name] = ""

    # Merge tx-style checks into text_fields
    merged_texts = {**text_fields, **tx_check_fields}

    writer = PdfWriter()
    writer.clone_reader_document_root(reader)

    for page in writer.pages:
        writer.update_page_form_field_values(page, merged_texts, auto_regenerate=False)

    for page in writer.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot_ref in annots:
            try:
                annot = annot_ref.get_object()
            except Exception:
                continue
            if annot.get("/FT") != "/Btn":
                continue
            raw_name = annot.get("/T")
            if raw_name is None:
                continue
            field_name = str(raw_name)
            if field_name not in btn_check_fields:
                continue

            # Determine the correct on-state name from the field's /AP dict.
            # Some Acrobat-created fields use /On or /on instead of /Yes.
            on_state = "/Yes"  # safe default
            try:
                ap = annot.get("/AP")
                if ap:
                    ap_obj = ap.get_object() if hasattr(ap, "get_object") else ap
                    n_dict = ap_obj.get("/N") if ap_obj else None
                    if n_dict:
                        n_obj = n_dict.get_object() if hasattr(n_dict, "get_object") else n_dict
                        for key in n_obj.keys():
                            if str(key).lower() != "/off":
                                on_state = str(key)
                                break
            except Exception:
                pass

            if btn_check_fields[field_name]:
                annot.update({NameObject("/V"): NameObject(on_state), NameObject("/AS"): NameObject(on_state)})
            else:
                annot.update({NameObject("/V"): NameObject("/Off"), NameObject("/AS"): NameObject("/Off")})

    # Fix font size for Width and Projection fields — default auto-size (0) can
    # overflow the box for large values like 20'0". Force 17pt across all pages.
    _FIXED_FONT_FIELDS = {"Width", "Projection"}
    _FIXED_DA = "/Helv 17 Tf 0 g"
    for page in writer.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot_ref in annots:
            try:
                annot = annot_ref.get_object()
            except Exception:
                continue
            if str(annot.get("/T", "")) in _FIXED_FONT_FIELDS:
                annot[NameObject("/DA")] = create_string_object(_FIXED_DA)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── Orchestrator ──────────────────────────────────────────────────────────────

def process_invoice(
    invoice: dict,
    po_number: str,
    forms_folder: str,
    output_folder: str,
    anthropic_api_key: str,
    dropbox_client=None,
    job_options: dict = None,
) -> list[FillResult]:
    """
    Fill one PDF per awning line item in `invoice`.

    job_options: {
      "wind_sensor_stock": bool,  # True = stock (don't order), False = order w/ awning
      "led_stock":         bool,  # True = stock (don't order), False = order w/ awning
    }

    If dropbox_client is provided, PDFs are uploaded to Dropbox.
    Otherwise saved to output_folder on local disk.
    """
    if job_options is None:
        job_options = {"wind_sensor_stock": True, "led_stock": True}

    results     = []
    today_dir   = datetime.now().strftime("%Y-%m-%d")
    awning_items = invoice.get("awning_items", [])
    total_items  = len(awning_items)

    for idx, item in enumerate(awning_items):
        name = item.get("name", f"Item {idx + 1}")
        log.info(f"  Processing item: {name[:80]}")

        # 1. Detect model
        model = detect_model(item.get("name", ""), item.get("description", ""))
        if not model:
            log.warning(f"  Could not detect model for item: {name[:60]}")
            results.append(FillResult(
                po_number=po_number, model="unknown", pdf_path="", item_name=name,
                success=False, error="Could not determine model (Sunesta/Sunstyle/Sunlight)",
            ))
            continue

        log.info(f"  Detected model: {model}")

        # 2. Parse specs with Claude
        try:
            specs = parse_specs_with_claude(item, model, anthropic_api_key)
            log.info(f"  Specs: {json.dumps(specs)}")
        except Exception as e:
            log.error(f"  Claude parsing failed: {e}")
            results.append(FillResult(
                po_number=po_number, model=model, pdf_path="", item_name=name,
                success=False, error=f"Claude parsing failed: {e}",
            ))
            continue

        # 3. Build field values
        try:
            text_fields, checkbox_fields = build_field_values(
                specs, invoice, po_number, model, idx, total_items, job_options
            )
        except Exception as e:
            log.error(f"  Field mapping failed: {e}", exc_info=True)
            results.append(FillResult(
                po_number=po_number, model=model, pdf_path="", item_name=name,
                success=False, error=f"Field mapping failed: {e}",
            ))
            continue

        # 4. Fill the PDF
        template_path = os.path.join(forms_folder, FORM_FILES[model])
        if not os.path.exists(template_path):
            err = f"Template not found: {template_path}"
            log.error(f"  {err}")
            results.append(FillResult(
                po_number=po_number, model=model, pdf_path="", item_name=name,
                success=False, error=err,
            ))
            continue

        # Output filename: PO# - LastName [-n].pdf
        last_name = invoice.get("customer_name", "Customer").split()[-1].replace("/", "-")
        if total_items > 1:
            filename = f"{po_number} - {last_name} [{idx + 1}].pdf"
        else:
            filename = f"{po_number} - {last_name}.pdf"

        try:
            pdf_bytes = fill_pdf(template_path, text_fields, checkbox_fields)
        except Exception as e:
            log.error(f"  PDF fill failed: {e}", exc_info=True)
            results.append(FillResult(
                po_number=po_number, model=model, pdf_path="", item_name=name,
                success=False, error=f"PDF fill failed: {e}",
            ))
            continue

        # 5. Store
        try:
            if dropbox_client:
                dropbox_path = dropbox_client.build_path(today_dir, filename)
                saved_path   = dropbox_client.upload(pdf_bytes, dropbox_path)
                log.info(f"  Uploaded to Dropbox: {saved_path}")
            else:
                dest_dir  = os.path.join(output_folder, today_dir)
                dest_path = os.path.join(dest_dir, filename)
                os.makedirs(dest_dir, exist_ok=True)
                with open(dest_path, "wb") as f:
                    f.write(pdf_bytes)
                saved_path = dest_path
                log.info(f"  Saved locally: {saved_path}")
        except Exception as e:
            log.error(f"  Storage failed: {e}", exc_info=True)
            results.append(FillResult(
                po_number=po_number, model=model, pdf_path="", item_name=name,
                success=False, error=f"Storage failed: {e}",
            ))
            continue

        results.append(FillResult(
            po_number=po_number, model=model, pdf_path=saved_path,
            item_name=name, success=True,
        ))

    return results

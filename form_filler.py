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
import shutil
import logging
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import anthropic
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, BooleanObject

log = logging.getLogger(__name__)

# ── Form template filenames ───────────────────────────────────────────────────
FORM_FILES = {
    "sunesta":  "Sunesta Sunesta.pdf",
    "sunstyle": "Sunstyle.pdf",
    "sunlight": "Sunlight.pdf",
}

# ── PDF field name mappings per model ─────────────────────────────────────────
#
# Keys are logical slot names used by the mapper.
# Values are the exact PDF field names (as read by read_pdf_fields).
#
# NOTE: "Row1" columns correspond to:
#   (no suffix) = col 1 (Part #)   _2 = Qty   _4 = Description   _5 = Comments
#   _6 = Retail Price   _7 = Discount   _8 = Width   _9 = Projection
#   _10 = Extended Retail   _11 = Total

_SUNESTA_PREFIX = "Part  Qty Description Comments Retail Price Each Discoun t  Width Projection Extended Retail Total  1"
_SUNSTYLE_PREFIX = "Part  Qty Description Comments Retail Price Each Discount  Width Projection Extended Retail Total  1"
_SUNLIGHT_PREFIX = "Extended Retail Total  Description Comments Retail Price Each Discount  Width Projection Part  Qty 1"

FIELD_MAP = {

    # ── SUNESTA SUNESTA ───────────────────────────────────────────────────────
    "sunesta": {
        # Header
        "salesperson":      "SALESPERSON",
        "date":             "DATE",
        "invoice_number":   "NUMBER",
        "page":             "PAGE",
        "of":               "OF",
        "customer":         "CUST",
        "po":               "PO",
        "bill_to":          "BILL TO",
        "ship_to":          "SHIP TO",
        "address_bill":     "ADDRESS",
        "address_ship":     "ADDRESS_2",
        "city_bill":        "CITY",
        "state_bill":       "STATE",
        "zip_bill":         "ZIP",
        "city_ship":        "CITY_2",
        "state_ship":       "STATE_2",
        "zip_ship":         "ZIP_2",
        "phone":            "PHONE",
        # Main product row (Row 1)
        "qty":              _SUNESTA_PREFIX + "Row1_2",
        "description":      _SUNESTA_PREFIX + "Row1_4",
        "comments":         _SUNESTA_PREFIX + "Row1_5",
        "width":            _SUNESTA_PREFIX + "Row1_8",
        "projection":       _SUNESTA_PREFIX + "Row1_9",
        # Case type (checkboxes)
        "cb_standard":      "Standard  12103",
        "cb_smartcase":     "SmartCase  12403",
        # Frame color (checkboxes)
        "cb_clay":          "Clay",
        "cb_black":         "Black",
        "cb_brown":         "Brown",
        "cb_white":         "White",
        "cb_beige":         "Beige",
        # Drive side
        "drive_left_text":  "Left",        # text field — set to "✓"
        "cb_drive_left":    "left",         # checkbox
        "cb_drive_right":   "Right",        # checkbox
        # Bracket type (checkboxes)
        "cb_wall_brkt":     "wall brkt",
        "cb_ceil_brkt":     "ceilingl brkt",
        "cb_gear_brkt":     "gear only",
        "cb_coupled":       "coupled",
        "cb_roof_brkt":     "roof brkt",
        # Motor rows — put qty "1" in the row next to the correct part number
        # Standard motors
        "motor_std_s_qty":  _SUNESTA_PREFIX + "Row11",   # 303049, 7–26 ft
        "motor_std_l_qty":  _SUNESTA_PREFIX + "Row12",   # 303099, 27–40 ft
        # Override / SmartCase-compatible motors
        "motor_ovr_s_qty":  _SUNESTA_PREFIX + "Row13",   # 303280, 7–26 ft
        "motor_ovr_l_qty":  _SUNESTA_PREFIX + "Row14",   # 303074, 27–40 ft
        # Remote color (checkboxes)
        "cb_remote_black":  "Black Remote",
        "cb_remote_white":  "White Remote",
        "cb_remote_beige":  "Beige Remote",
        # Motion sensor / LED (qty text fields)
        "motion_sensor_qty": "Motion Sensor",
        "led_kit_qty":        "LED Light Kit",
        # SmartCase accessory qty
        "smartcase_qty":      "SmartCase 8",
        # Fabric
        "deck_fabric":        "Deck Fabric NumberRow1",
        "valance_fabric":     "Valance Fabric NoRow1",
        "smartdrop_fabric":   "SmartDrop Fabric NoRow1",
        # Valance
        "valance_style":      "Style6 8 10",
        "valance_binding":    "Valance Binding6 8 10",
        # Notes
        "notes":              "NOTES",
    },

    # ── SUNSTYLE ──────────────────────────────────────────────────────────────
    "sunstyle": {
        # Header
        "salesperson":      "SALESPERSON",
        "date":             "DATE",
        "invoice_number":   "NUMBER",
        "page":             "PAGE",
        "of":               "OF",
        "customer":         "CUST",
        "po":               "PO",
        "bill_to":          "BILL TO",
        "ship_to":          "SHIP TO",
        "address_bill":     "ADDRESS",
        "address_ship":     "ADDRESS_2",
        "city_bill":        "CITY",
        "state_bill":       "STATE",
        "zip_bill":         "ZIP",
        "city_ship":        "CITY_2",
        "state_ship":       "STATE_2",
        "zip_ship":         "ZIP_2",
        "phone":            "PHONE",
        # Main product row
        "qty":              _SUNSTYLE_PREFIX + "Row1_2",
        "description":      _SUNSTYLE_PREFIX + "Row1_4",
        "comments":         _SUNSTYLE_PREFIX + "Row1_5",
        "width":            _SUNSTYLE_PREFIX + "Row1_8",
        "projection":       _SUNSTYLE_PREFIX + "Row1_9",
        # Case type
        "cb_standard":      "Standard  12803",
        "cb_smartcase":     "SmartCase  12903",
        # Frame color
        "cb_clay":          "Clay Arms Check Box",
        "cb_brown":         "Brown Arms Check Box",
        "cb_white":         "White Arms Check Box",
        "cb_beige":         "Beige Arms Check Box",
        # Drive side (both checkboxes for Sunstyle)
        "cb_drive_left":    "Left",
        "cb_drive_right":   "Right",
        # Bracket type
        "cb_wall_brkt":     "Wall Bracket Check Box",
        "cb_ceil_brkt":     "Ceiling Bracket Kit Check Box",
        "cb_gear_brkt":     "Gear (standard) check box",
        "cb_coupled":       "Coupled Unit Check Box",
        "cb_roof_brkt":     "Roof Bracket Kit check box",
        "cb_rafter_brkt":   "Rafter Bracket",
        # Motor rows
        "motor_std_s_qty":  _SUNSTYLE_PREFIX + "Row3_3",   # 303049, 7–26 ft
        "motor_std_l_qty":  _SUNSTYLE_PREFIX + "Row8",     # 303099, 27–40 ft
        "motor_ovr_s_qty":  _SUNSTYLE_PREFIX + "Row3_4",   # 303280, 7–26 ft (standard motorized)
        "motor_sc_s_qty":   _SUNSTYLE_PREFIX + "Row3_5",   # 303281, 7–26 ft (SmartCase)
        "motor_ovr_l_qty":  _SUNSTYLE_PREFIX + "Row3_6",   # 303074, 27–40 ft
        # SmartCase / Motorized checkboxes
        "cb_motor_standard": "Standard Wireless Motor check Box",
        "cb_motor_motor":    "Motor Check Box",
        "cb_smartcase_mtr":  "Override Motor Smartcase check box",
        # Remote
        "cb_remote_black":  "*3 Black",
        "cb_remote_white":  "*5 White",
        "cb_remote_beige":  "*7 Beige",
        # Accessories (checkboxes)
        "cb_motion_sensor": "Motion Sensor check box",
        "cb_led_lights":    "LED Light Kit check box",
        "cb_smarttilt":     "Smarttilt check box",
        "cb_smartdrop":     "SmartDrop Gear Standard Check Box",
        "cb_hood":          "hood check box",
        # Fabric
        "deck_fabric":      "Deck Fabric NumberRow1",
        "valance_fabric":   "Valance Fabric NoRow1",
        "smartdrop_fabric": "SmartDrop Fabric NoRow1",
        # Valance
        "valance_style":    "Style6 8 10",
        "valance_binding":  "Valance Binding6 8 10",
        # Notes
        "notes":            "NOTES",
    },

    # ── SUNLIGHT ──────────────────────────────────────────────────────────────
    "sunlight": {
        # Header
        "salesperson":      "SALESPERSON",
        "date":             "DATE",
        "invoice_number":   "NUMBER",
        "page":             "PAGE",
        "of":               "OF",
        "customer":         "CUST",
        "po":               "PO",
        "bill_to":          "BILL TO",
        "ship_to":          "SHIP TO",
        "address_bill":     "ADDRESS",
        "address_ship":     "ADDRESS_2",
        "city_bill":        "CITY",
        "state_bill":       "STATE",
        "zip_bill":         "ZIP",
        "city_ship":        "CITY_2",
        "state_ship":       "STATE_2",
        "zip_ship":         "ZIP_2",
        "phone":            "PHONE",
        # Main product row
        "qty":              _SUNLIGHT_PREFIX + "Row1_2",
        "description":      _SUNLIGHT_PREFIX + "Row1_4",
        "comments":         _SUNLIGHT_PREFIX + "Row1_5",
        "width":            _SUNLIGHT_PREFIX + "Row1_8",
        "projection":       _SUNLIGHT_PREFIX + "Row1_9",
        # Frame color (Sunlight arm colors)
        "cb_clay":          "Clay Arms",
        "cb_white":         "White arms",
        "cb_beige":         "Beige Arms",
        # Drive side
        "cb_drive_left":    "Left",
        "cb_drive_right":   "Right",
        # Bracket type
        "cb_wall_brkt":     "Wall Bracket Kit 14*048",
        "cb_ceil_brkt":     "Ceiling Bracket Kit 14*049",
        "cb_gear_brkt":     "Gear (standard)",
        "cb_coupled":       "Coupled Unit Check Box",
        "cb_roof_brkt":     "Roff Bracket Kit 11*237",
        "cb_rafter_brkt":   "Rafter Bracket 11*168",
        # Motor rows (Sunlight uses different part numbers)
        "motor_std_qty":    _SUNLIGHT_PREFIX + "Row3_4",   # 303047
        "motor_ovr_qty":    _SUNLIGHT_PREFIX + "Row3_5",   # 303279
        # Motor checkboxes
        "cb_motor_std":     "Standard Motor Check Box",
        "cb_motor_ovr":     "Manual Override Motor Check Box",
        # Accessories
        "cb_motion_sensor": "Motion Sensor Checkbox",
        "cb_led_lights":    "Led Light Kit Check Box",
        "cb_smarttilt":     "SmartTilt check box",
        "cb_hood":          "hood check box",
        # Fabric
        "deck_fabric":      "Deck Fabric NumberRow1",
        "valance_fabric":   "Valance Fabric NumberRow1",
        # Valance
        "valance_style":    "Style6 8 10",
        "valance_binding":  "Valance Binding6 8 10",
        # Notes
        "notes":            "NOTES",
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
    """Detect awning model from line item text. Returns 'sunesta', 'sunstyle', or 'sunlight'."""
    text = (name + " " + description).lower()
    if "sunstyle" in text:
        return "sunstyle"
    if "sunlight" in text or "sunlite" in text:
        return "sunlight"
    if "sunesta" in text:
        return "sunesta"
    return None


# ── Claude AI spec extraction ─────────────────────────────────────────────────

def parse_specs_with_claude(
    line_item: dict,
    model: str,
    anthropic_api_key: str,
) -> dict:
    """
    Send the invoice line item to Claude and get back structured JSON specs.

    Returns a dict like:
    {
      "width":           "20'6\"",
      "projection":      "10'0\"",
      "frame_color":     "Clay",          # Clay | Black | Brown | White | Beige
      "drive_side":      "Left",          # Left | Right
      "smart_case":      false,           # bool
      "deck_fabric":     "4955-0000",
      "valance_fabric":  "4955-0000",
      "valance_style":   "8",             # "6" | "8" | "10" | null
      "valance_binding": "MATCH",         # "MATCH" | "CONTRAST" | null
      "smartdrop":       false,
      "smartdrop_fabric": null,
      "smarttilt":       false,
      "motion_sensor":   false,
      "led_lights":      false,
      "hood":            false,
      "bracket_type":    "wall",          # wall|ceiling|roof|gear|coupled|rafter|null
      "remote_channels": 5,               # 1|4|5|null
      "remote_color":    "White",         # Black|White|Beige|null
      "motorized":       true             # relevant for Sunlight
    }
    """
    client = anthropic.Anthropic(api_key=anthropic_api_key)

    prompt = f"""You are parsing a Sunesta awning order from a Zoho invoice line item.
The model is: {model.upper()}

Line item name: {line_item.get('name', '')}
Line item description:
{line_item.get('description', '')}

Extract every specification and return ONLY a valid JSON object with these exact keys:
{{
  "width":           "<feet'inches\\">  e.g. \\"20'6\\"\\",
  "projection":      "<feet'inches\\">  e.g. \\"10'0\\"\\",
  "frame_color":     "<Clay|Black|Brown|White|Beige>",
  "drive_side":      "<Left|Right>",
  "smart_case":      <true|false>,
  "deck_fabric":     "<fabric number or null>",
  "valance_fabric":  "<fabric number or null>",
  "valance_style":   "<6|8|10|null>",
  "valance_binding": "<MATCH|CONTRAST|null>",
  "smartdrop":       <true|false>,
  "smartdrop_fabric": "<fabric number or null>",
  "smarttilt":       <true|false>,
  "motion_sensor":   <true|false>,
  "led_lights":      <true|false>,
  "hood":            <true|false>,
  "bracket_type":    "<wall|ceiling|roof|gear|coupled|rafter|null>",
  "remote_channels": <1|4|5|null>,
  "remote_color":    "<Black|White|Beige|null>",
  "motorized":       <true|false>
}}

Rules:
- If a value is not mentioned, use null (for strings) or false (for booleans).
- For Sunesta, smart_case is an add-on accessory — mark true only if explicitly mentioned.
- For Sunstyle, smart_case refers to the enclosure type (standard vs SmartCase hood).
- For Sunlight, motorized means it has a motor (vs hand-crank manual).
- Return ONLY the JSON — no explanation, no markdown fences."""

    resp = client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()

    # Strip any accidental markdown fences
    if "```" in text:
        text = text.split("```")[1] if "```json" not in text else text.split("```json")[1]
        text = text.split("```")[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"Claude returned invalid JSON: {e}\nRaw: {text[:500]}")
        raise RuntimeError(f"Failed to parse Claude response as JSON: {e}")


# ── Width helper ──────────────────────────────────────────────────────────────

def _width_to_ft(width_str: str) -> float:
    """Convert '20\'6"' or '246' style width to decimal feet."""
    if not width_str:
        return 0.0
    m = re.match(r"(\d+)'(\d+)", width_str)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 12
    m = re.match(r"(\d+\.?\d*)", width_str)
    if m:
        return float(m.group(1))
    return 0.0


# ── Field value builder ───────────────────────────────────────────────────────

def build_field_values(
    specs: dict,
    invoice: dict,
    po_number: str,
    model: str,
    item_index: int = 0,
) -> tuple[dict, dict]:
    """
    Translate parsed specs + customer info into two dicts:
      - text_fields:     {pdf_field_name: str_value}
      - checkbox_fields: {pdf_field_name: bool}

    Uses FIELD_MAP[model] to resolve logical slot → actual PDF field name.
    """
    fm      = FIELD_MAP[model]
    texts   = {}
    checks  = {}
    today   = datetime.now().strftime("%m/%d/%Y")

    def set_text(slot: str, value: str):
        if slot in fm and value:
            texts[fm[slot]] = str(value)

    def set_check(slot: str, value: bool):
        if slot in fm:
            checks[fm[slot]] = bool(value)

    # ── Header ────────────────────────────────────────────────────────────────
    set_text("salesperson",    "MR. AWNINGS")
    set_text("date",           today)
    set_text("invoice_number", invoice.get("invoice_number", ""))
    set_text("page",           str(item_index + 1))
    set_text("of",             "1")
    set_text("customer",       invoice.get("customer_name", ""))
    set_text("po",             po_number)
    set_text("bill_to",        invoice.get("customer_name", ""))
    set_text("ship_to",        invoice.get("customer_name", ""))
    set_text("address_bill",   invoice.get("billing_street",  ""))
    set_text("address_ship",   invoice.get("shipping_street", "") or invoice.get("billing_street", ""))
    set_text("city_bill",      invoice.get("billing_city",    ""))
    set_text("state_bill",     invoice.get("billing_state",   ""))
    set_text("zip_bill",       invoice.get("billing_zip",     ""))
    set_text("city_ship",      invoice.get("shipping_city",   "") or invoice.get("billing_city", ""))
    set_text("state_ship",     invoice.get("shipping_state",  "") or invoice.get("billing_state", ""))
    set_text("zip_ship",       invoice.get("shipping_zip",    "") or invoice.get("billing_zip", ""))
    set_text("phone",          invoice.get("phone",           ""))

    # ── Main product row ──────────────────────────────────────────────────────
    width      = specs.get("width", "")
    projection = specs.get("projection", "")
    desc_line  = f"{model.upper()} {width} X {projection}".strip()
    set_text("qty",         "1")
    set_text("description", desc_line)
    set_text("width",       width)
    set_text("projection",  projection)

    # ── Frame color ───────────────────────────────────────────────────────────
    color = (specs.get("frame_color") or "").strip().lower()
    set_check("cb_clay",  color == "clay")
    set_check("cb_black", color == "black")
    set_check("cb_brown", color == "brown")
    set_check("cb_white", color == "white")
    set_check("cb_beige", color == "beige")

    # ── Drive side ────────────────────────────────────────────────────────────
    is_left = (specs.get("drive_side") or "").strip().lower() == "left"
    if model == "sunesta":
        # Sunesta has a text field AND a checkbox for Left
        if is_left:
            texts[fm["drive_left_text"]] = "✓"
        set_check("cb_drive_left",  is_left)
        set_check("cb_drive_right", not is_left)
    else:
        set_check("cb_drive_left",  is_left)
        set_check("cb_drive_right", not is_left)

    # ── Case type ─────────────────────────────────────────────────────────────
    smart_case = bool(specs.get("smart_case"))
    set_check("cb_standard",  not smart_case)
    set_check("cb_smartcase", smart_case)
    if smart_case and model == "sunesta":
        set_text("smartcase_qty", "1")

    # ── Bracket type ─────────────────────────────────────────────────────────
    bracket = (specs.get("bracket_type") or "").strip().lower()
    set_check("cb_wall_brkt",   bracket == "wall")
    set_check("cb_ceil_brkt",   bracket == "ceiling")
    set_check("cb_gear_brkt",   bracket == "gear")
    set_check("cb_coupled",     bracket == "coupled")
    set_check("cb_roof_brkt",   bracket == "roof")
    if "cb_rafter_brkt" in fm:
        set_check("cb_rafter_brkt", bracket == "rafter")

    # ── Motor selection ───────────────────────────────────────────────────────
    width_ft  = _width_to_ft(width)
    is_short  = width_ft <= 26.0   # 7–26 ft range
    motorized = bool(specs.get("motorized", True))  # default True for Sunesta/Sunstyle

    if model == "sunesta":
        # Standard vs SmartCase-compatible motor, then size
        if smart_case:
            set_text("motor_ovr_s_qty", "1" if is_short else "")
            set_text("motor_ovr_l_qty", "1" if not is_short else "")
        else:
            set_text("motor_std_s_qty", "1" if is_short else "")
            set_text("motor_std_l_qty", "1" if not is_short else "")

    elif model == "sunstyle":
        if smart_case:
            set_text("motor_sc_s_qty",  "1" if is_short else "")
            set_text("motor_ovr_l_qty", "1" if not is_short else "")
        else:
            set_text("motor_std_s_qty", "1" if is_short else "")
            set_text("motor_std_l_qty", "1" if not is_short else "")
        # Motor type checkboxes
        set_check("cb_motor_standard", not smart_case)
        set_check("cb_motor_motor",    not smart_case)
        set_check("cb_smartcase_mtr",  smart_case)

    elif model == "sunlight":
        if motorized:
            set_text("motor_std_qty", "1" if not smart_case else "")
            set_text("motor_ovr_qty", "1" if smart_case else "")
            set_check("cb_motor_std", not smart_case)
            set_check("cb_motor_ovr", smart_case)

    # ── Remote ───────────────────────────────────────────────────────────────
    remote_color = (specs.get("remote_color") or "").strip().lower()
    set_check("cb_remote_black", remote_color == "black")
    set_check("cb_remote_white", remote_color == "white")
    set_check("cb_remote_beige", remote_color == "beige")

    # ── Accessories ───────────────────────────────────────────────────────────
    motion = bool(specs.get("motion_sensor"))
    led    = bool(specs.get("led_lights"))
    stilt  = bool(specs.get("smarttilt"))
    hood   = bool(specs.get("hood"))
    sdrop  = bool(specs.get("smartdrop"))

    if model == "sunesta":
        # Sunesta uses qty text fields for motion sensor and LED
        set_text("motion_sensor_qty", "1" if motion else "")
        set_text("led_kit_qty",        "1" if led    else "")
    else:
        set_check("cb_motion_sensor", motion)
        set_check("cb_led_lights",    led)

    if "cb_smarttilt" in fm:
        set_check("cb_smarttilt", stilt)
    if "cb_hood" in fm:
        set_check("cb_hood", hood)
    if "cb_smartdrop" in fm:
        set_check("cb_smartdrop", sdrop)

    # ── Fabric ────────────────────────────────────────────────────────────────
    set_text("deck_fabric",      specs.get("deck_fabric")      or "")
    set_text("valance_fabric",   specs.get("valance_fabric")   or "")
    set_text("smartdrop_fabric", specs.get("smartdrop_fabric") or "")

    # ── Valance ───────────────────────────────────────────────────────────────
    v_style   = specs.get("valance_style")   or ""
    v_binding = specs.get("valance_binding") or ""
    set_text("valance_style",   str(v_style))
    set_text("valance_binding", v_binding)

    # For Sunstyle valance height checkboxes (6", 8", 10")
    if model == "sunstyle" and v_style in ("6", "8", "10"):
        if "6" in fm:
            checks[fm.get("6",  "")] = v_style == "6"
        if "8" in fm:
            checks[fm.get("8",  "")] = v_style == "8"
        if "10" in fm:
            checks[fm.get("10", "")] = v_style == "10"

    return texts, checks


# ── PDF writer ────────────────────────────────────────────────────────────────

def fill_pdf(
    template_path: str,
    text_fields: dict,
    checkbox_fields: dict,
) -> bytes:
    """
    Fill `template_path` with the provided field values and return the PDF bytes.

    text_fields:     {field_name: str_value}
    checkbox_fields: {field_name: True/False}
    """
    reader = PdfReader(template_path)
    writer = PdfWriter()
    writer.clone_reader_document_root(reader)

    # Fill text fields — works across all pages
    for page in writer.pages:
        writer.update_page_form_field_values(
            page,
            text_fields,
            auto_regenerate=False,
        )

    # Fill checkboxes — must iterate annotations
    for page in writer.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot_ref in annots:
            try:
                annot = annot_ref.get_object()
            except Exception:
                continue
            # Only process button (checkbox) fields
            if annot.get("/FT") != "/Btn":
                continue
            raw_name = annot.get("/T")
            if raw_name is None:
                continue
            field_name = str(raw_name)
            if field_name not in checkbox_fields:
                continue
            if checkbox_fields[field_name]:
                annot.update({
                    NameObject("/V"):  NameObject("/Yes"),
                    NameObject("/AS"): NameObject("/Yes"),
                })
            else:
                annot.update({
                    NameObject("/V"):  NameObject("/Off"),
                    NameObject("/AS"): NameObject("/Off"),
                })

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
) -> list[FillResult]:
    """
    Fill one PDF per awning line item in `invoice`.

    If dropbox_client is provided, filled PDFs are uploaded to Dropbox and
    pdf_path in each FillResult will be a Dropbox path.
    Otherwise, PDFs are saved to output_folder on the local filesystem.

    Returns a list of FillResult objects (one per awning item).
    """
    results   = []
    today_dir = datetime.now().strftime("%Y-%m-%d")

    for idx, item in enumerate(invoice.get("awning_items", [])):
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
                specs, invoice, po_number, model, idx
            )
        except Exception as e:
            log.error(f"  Field mapping failed: {e}")
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

        # Build output filename
        customer  = invoice.get("customer_name", "Customer").replace("/", "-")
        item_tag  = f" ({idx + 1})" if len(invoice.get("awning_items", [])) > 1 else ""
        filename  = f"{po_number} - {customer} - {model.title()}{item_tag} Order Form.pdf"

        try:
            pdf_bytes = fill_pdf(template_path, text_fields, checkbox_fields)
        except Exception as e:
            log.error(f"  PDF fill failed: {e}", exc_info=True)
            results.append(FillResult(
                po_number=po_number, model=model, pdf_path="", item_name=name,
                success=False, error=f"PDF fill failed: {e}",
            ))
            continue

        # Store the filled PDF — Dropbox if token is configured, local disk otherwise
        try:
            if dropbox_client:
                dropbox_path = dropbox_client.build_path(today_dir, filename)
                saved_path   = dropbox_client.upload(pdf_bytes, dropbox_path)
                log.info(f"  Filled form uploaded to Dropbox: {saved_path}")
            else:
                dest_dir  = os.path.join(output_folder, today_dir)
                dest_path = os.path.join(dest_dir, filename)
                os.makedirs(dest_dir, exist_ok=True)
                with open(dest_path, "wb") as f:
                    f.write(pdf_bytes)
                saved_path = dest_path
                log.info(f"  Filled form saved locally: {saved_path}")
        except Exception as e:
            log.error(f"  Storage failed: {e}", exc_info=True)
            results.append(FillResult(
                po_number=po_number, model=model, pdf_path="", item_name=name,
                success=False, error=f"Storage failed: {e}",
            ))
            continue

        results.append(FillResult(
            po_number=po_number,
            model=model,
            pdf_path=saved_path,
            item_name=name,
            success=True,
        ))

    return results

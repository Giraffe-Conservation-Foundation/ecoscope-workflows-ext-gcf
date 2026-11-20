"""
Event Upload rules -- form (schema) handling and CSV row checking.

Copied from TwigaTools modules/event_upload/schema.py (2026-11-20). Keep the
two in step: pure functions, no Streamlit/ecoscope imports.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd

# Columns every event has, whatever its type (not part of the form).
STANDARD_COLUMNS = [
    # (column, required, what it is)
    ("event_datetime", True, "When it happened, e.g. 2026-11-20 14:30 or 20/11/2026 14:30 (day first); uses the time zone you choose when uploading"),
    ("latitude", False, "Decimal degrees, e.g. -19.4321"),
    ("longitude", False, "Decimal degrees, e.g. 15.9876"),
    ("notes", False, "Free-text note added to the event"),
]
STANDARD_NAMES = {c for c, _, _ in STANDARD_COLUMNS}

TRUE_WORDS = {"true", "yes", "y", "1", "t"}
FALSE_WORDS = {"false", "no", "n", "0", "f"}
MULTI_SEP = ";"


@dataclass
class Field:
    path: tuple            # nested keys inside event_details, e.g. ("group", "size")
    column: str            # CSV header (keys joined with ".")
    title: str
    kind: str              # text | number | integer | boolean | datetime | choice | multichoice
    required: bool = False
    choices: dict = field(default_factory=dict)   # stored value -> label
    description: str = ""


@dataclass
class SkippedField:
    column: str
    title: str
    reason: str


# ======== Reading the form ========

def _resolve(node, doc: dict, depth: int = 0) -> dict:
    """Follow local $ref pointers ('#/$defs/X', '#/definitions/X')."""
    while isinstance(node, dict) and "$ref" in node and depth < 10:
        ref = str(node["$ref"])
        if not ref.startswith("#/"):
            break
        target = doc
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        merged = dict(target) if isinstance(target, dict) else {}
        merged.update({k: v for k, v in node.items() if k != "$ref"})
        node, depth = merged, depth + 1
    return node if isinstance(node, dict) else {}


def inline_remote_refs(node, loader, depth: int = 0):
    """Replace {"$ref": "/api/v2.0/schemas/choices.json?field=..."} (EarthRanger
    v2 dropdowns keep their options in separate choice lists) with what
    loader(ref) returns. Refs the loader can't read are left in place."""
    if depth > 30:
        return node
    if isinstance(node, list):
        return [inline_remote_refs(x, loader, depth + 1) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and not ref.startswith("#"):
        loaded = loader(ref)
        if isinstance(loaded, dict) and loaded:
            merged = dict(loaded)
            merged.update({k: v for k, v in node.items() if k != "$ref"})
            return merged
        return node
    return {k: inline_remote_refs(v, loader, depth + 1) for k, v in node.items()}


def _has_remote_ref(node, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str) and not node["$ref"].startswith("#"):
            return True
        return any(_has_remote_ref(v, depth + 1) for k, v in node.items() if k in ("anyOf", "oneOf", "allOf", "items"))
    if isinstance(node, list):
        return any(_has_remote_ref(x, depth + 1) for x in node)
    return False


def _props(node, doc: dict) -> tuple[dict, set]:
    """(properties, required keys) of an object schema incl. allOf/anyOf/oneOf parts."""
    node = _resolve(node, doc)
    props = dict(node.get("properties") or {})
    required = set(node.get("required") or []) if isinstance(node.get("required"), list) else set()
    for combo in ("allOf", "anyOf", "oneOf"):
        for part in node.get(combo) or []:
            p, r = _props(part, doc)
            props.update(p)
            required |= r
    return props, required


def _choices(prop: dict, doc: dict) -> dict:
    """{stored value: label} for a dropdown field, or {} if it isn't one."""
    prop = _resolve(prop, doc)
    out = {}
    enum = prop.get("enum")
    names = prop.get("enumNames")
    if isinstance(enum, list):
        if isinstance(names, dict):
            out = {str(v): str(names.get(v, names.get(str(v), v))) for v in enum}
        elif isinstance(names, list) and len(names) == len(enum):
            out = {str(v): str(n) for v, n in zip(enum, names)}
        else:
            out = {str(v): str(v) for v in enum}
    for key in ("anyOf", "oneOf"):
        for opt in prop.get(key, []) or []:
            opt = _resolve(opt, doc)
            if "const" in opt:
                out[str(opt["const"])] = str(opt.get("title", opt["const"]))
            elif opt:
                out.update(_choices(opt, doc))
    return {k: v for k, v in out.items() if k not in ("None", "")}


def _type_of(prop: dict) -> str:
    t = prop.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    return str(t or "")


def schema_fields(schema: dict) -> tuple[list[Field], list[SkippedField]]:
    """Every fillable field in the form, plus the ones a CSV can't fill."""
    fields: list[Field] = []
    skipped: list[SkippedField] = []
    if not isinstance(schema, dict):
        return fields, skipped
    doc = schema

    def walk(node, prefix: tuple, title_prefix: str, depth: int):
        props, required = _props(node, doc)
        for key, raw in props.items():
            prop = _resolve(raw, doc)
            if not isinstance(prop, dict):
                continue
            if prop.get("readOnly") or prop.get("deprecated") or prop.get("isHidden"):
                continue
            path = prefix + (str(key),)
            column = ".".join(path)
            title = f"{title_prefix}{prop.get('title') or key}"
            kind = _type_of(prop)
            choices = _choices(prop, doc)
            desc = str(prop.get("description") or "")
            if kind == "object" and depth < 2 and (prop.get("properties") or prop.get("allOf")):
                walk(prop, path, f"{title} › ", depth + 1)
                continue
            if kind == "array":
                items = _resolve(prop.get("items") or {}, doc)
                item_choices = _choices(items, doc)
                if item_choices:
                    fields.append(Field(path, column, title, "multichoice", key in required, item_choices, desc))
                elif _has_remote_ref(items) or _has_remote_ref(prop):
                    fields.append(Field(path, column, title, "multichoice_unknown", key in required, {}, desc))
                elif _type_of(items) in ("string", "number", "integer", ""):
                    fields.append(Field(path, column, title, "multitext", key in required, {}, desc))
                else:
                    skipped.append(SkippedField(column, title, "a list of entries (e.g. one per animal) — can't be filled from a flat CSV"))
                continue
            if kind == "object":
                skipped.append(SkippedField(column, title, "a nested group too deep for a CSV column"))
                continue
            if choices:
                fields.append(Field(path, column, title, "choice", key in required, choices, desc))
            elif _has_remote_ref(prop):
                fields.append(Field(path, column, title, "choice_unknown", key in required, {}, desc))
            elif kind == "boolean":
                fields.append(Field(path, column, title, "boolean", key in required, {}, desc))
            elif kind in ("number", "integer"):
                fields.append(Field(path, column, title, kind, key in required, {}, desc))
            elif kind == "string" and str(prop.get("format", "")) in ("date-time", "date"):
                fields.append(Field(path, column, title, "datetime", key in required, {}, desc))
            else:
                fields.append(Field(path, column, title, "text", key in required, {}, desc))

    walk(schema, (), "", 0)
    # A form field must not clash with a standard column name
    for f in fields:
        if f.column in STANDARD_NAMES:
            f.column = f"details.{f.column}"
    return fields, skipped


# ======== Template and field guide ========

_KIND_HINT = {
    "text": "Free text",
    "multitext": f"Free text, several values allowed (separate with '{MULTI_SEP}')",
    "number": "Number",
    "integer": "Whole number",
    "boolean": "Yes / No",
    "datetime": "Date/time, e.g. 2026-11-20 14:30",
    "choice": "Dropdown — enter ONE of the allowed values (value or label)",
    "multichoice": f"Multi-select dropdown — enter one or more allowed values, separated by '{MULTI_SEP}'",
    "choice_unknown": "Dropdown — options couldn't be read from EarthRanger; enter the stored value exactly as in EarthRanger",
    "multichoice_unknown": f"Multi-select dropdown — options couldn't be read from EarthRanger; enter stored values separated by '{MULTI_SEP}'",
}
_KIND_TYPE = {
    "text": "Free text", "multitext": "Free text (list)", "number": "Number", "integer": "Whole number",
    "boolean": "Yes / No", "datetime": "Date/time", "choice": "Dropdown", "multichoice": "Multi-select dropdown",
    "choice_unknown": "Dropdown", "multichoice_unknown": "Multi-select dropdown",
}


def field_guide(fields: list[Field]) -> pd.DataFrame:
    std_type = {"event_datetime": "Date/time", "latitude": "Number", "longitude": "Number", "notes": "Free text"}
    rows = [{"Column": c, "Label": c.replace("_", " ").capitalize(), "Field type": std_type.get(c, "Free text"),
             "Required": "Yes" if req else "", "What to enter": hint, "Allowed values": ""}
            for c, req, hint in STANDARD_COLUMNS]
    for f in fields:
        allowed = ""
        if f.choices:
            allowed = " | ".join(v if v == lbl else f"{v} ({lbl})" for v, lbl in f.choices.items())
        rows.append({"Column": f.column, "Label": f.title, "Field type": _KIND_TYPE.get(f.kind, "Free text"),
                     "Required": "Yes" if f.required else "",
                     "What to enter": _KIND_HINT.get(f.kind, "Text") + (f" — {f.description}" if f.description else ""),
                     "Allowed values": allowed})
    return pd.DataFrame(rows)


def template_csv(fields: list[Field]) -> str:
    cols = [c for c, _, _ in STANDARD_COLUMNS] + [f.column for f in fields]
    return pd.DataFrame(columns=cols).to_csv(index=False)


# ======== Checking rows ========

def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return str(v).strip() == "" or str(v).strip().lower() in ("nan", "none", "null")


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def match_columns(headers: list[str], fields: list[Field]) -> tuple[dict, list[str]]:
    """{csv header: field column or standard column}; headers not recognised.
    A header can be the column name or the field's label (case-insensitive)."""
    lookup = {}
    for c, _, _ in STANDARD_COLUMNS:
        lookup[_norm(c)] = c
    for f in fields:
        lookup.setdefault(_norm(f.title), f.column)
        lookup[_norm(f.column)] = f.column
        lookup.setdefault(_norm(f.path[-1]), f.column)
    mapping, unknown = {}, []
    for h in headers:
        target = lookup.get(_norm(h))
        if target and target not in mapping.values():
            mapping[h] = target
        else:
            unknown.append(h)
    return mapping, unknown


def parse_time(raw, tz_name: str) -> str:
    s = str(raw).strip()
    # Slashed dates are read day-first (20/11/2026), as Excel writes them in
    # most of Africa; ISO dates (2026-11-20) are unambiguous.
    ts = pd.to_datetime(s, dayfirst=bool(re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}", s)))
    if ts.tzinfo is None:
        ts = ts.tz_localize(ZoneInfo(tz_name))
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _pick_choice(raw: str, choices: dict) -> str | None:
    s = str(raw).strip()
    if s in choices:
        return s
    by_value = {_norm(k): k for k in choices}
    by_label = {_norm(v): k for k, v in choices.items()}
    return by_value.get(_norm(s)) or by_label.get(_norm(s))


def _convert(f: Field, raw, tz_name: str):
    s = str(raw).strip()
    if f.kind == "text":
        return s
    if f.kind == "choice_unknown":
        return s
    if f.kind in ("multitext", "multichoice_unknown"):
        return [p.strip() for p in s.split(MULTI_SEP) if p.strip()]
    if f.kind in ("number", "integer"):
        try:
            num = float(s.replace(",", "."))
        except ValueError:
            raise ValueError(f"'{s}' isn't a number") from None
        if f.kind == "integer":
            if num != int(num):
                raise ValueError(f"'{s}' isn't a whole number")
            return int(num)
        return num
    if f.kind == "boolean":
        if s.lower() in TRUE_WORDS:
            return True
        if s.lower() in FALSE_WORDS:
            return False
        raise ValueError(f"'{s}' isn't yes/no")
    if f.kind == "datetime":
        return parse_time(s, tz_name)
    if f.kind == "choice":
        v = _pick_choice(s, f.choices)
        if v is None:
            raise ValueError(f"'{s}' isn't one of the allowed values")
        return v
    if f.kind == "multichoice":
        out = []
        for part in [p for p in s.split(MULTI_SEP) if p.strip()]:
            v = _pick_choice(part, f.choices)
            if v is None:
                raise ValueError(f"'{part.strip()}' isn't one of the allowed values")
            out.append(v)
        return out
    return s


def _set(d: dict, path: tuple, value):
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def build_events(df: pd.DataFrame, mapping: dict, fields: list[Field], event_type: str,
                 default_title: str, tz_name: str) -> list[dict]:
    """One result per row: {'row', 'ok', 'errors': [...], 'payload': {...}}."""
    by_col = {f.column: f for f in fields}
    rev = {v: k for k, v in mapping.items()}   # target column -> csv header
    out = []
    for i, rec in enumerate(df.to_dict("records")):
        errors = []

        def val(col):
            h = rev.get(col)
            return None if h is None or _blank(rec.get(h)) else rec.get(h)

        payload = {"event_type": event_type, "event_details": {}}
        raw_t = val("event_datetime")
        if raw_t is None:
            errors.append("event_datetime is missing")
        else:
            try:
                payload["time"] = parse_time(raw_t, tz_name)
            except Exception:
                errors.append(f"event_datetime '{raw_t}' isn't a date/time")

        lat, lon = val("latitude"), val("longitude")
        if (lat is None) != (lon is None):
            errors.append("latitude and longitude must both be filled (or both blank)")
        elif lat is not None:
            try:
                la, lo = float(str(lat).replace(",", ".")), float(str(lon).replace(",", "."))
                if not (-90 <= la <= 90 and -180 <= lo <= 180):
                    raise ValueError
                if la == 0 and lo == 0:
                    errors.append("location is 0, 0 — leave both blank if unknown")
                payload["location"] = {"latitude": la, "longitude": lo}
            except ValueError:
                errors.append(f"location '{lat}, {lon}' isn't valid decimal degrees")

        payload["title"] = default_title   # always the event type's name
        note = val("notes")
        if note is not None:
            payload["notes"] = [{"text": str(note).strip()}]

        for col, f in by_col.items():
            raw = val(col)
            if raw is None:
                if f.required:
                    errors.append(f"{f.column} ({f.title}) is required")
                continue
            try:
                _set(payload["event_details"], f.path, _convert(f, raw, tz_name))
            except Exception as e:
                errors.append(f"{f.column}: {e}")

        out.append({"row": i + 2, "ok": not errors, "errors": errors, "payload": payload})
    return out


def schema_from_response(data) -> dict:
    """Dig the JSON schema out of whatever an EarthRanger schema endpoint returned."""
    for _ in range(4):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return {}
        if not isinstance(data, dict):
            return {}
        if "properties" in data and isinstance(data["properties"], dict):
            return data
        for key in ("data", "schema", "json"):
            if key in data and isinstance(data[key], (dict, str)):
                data = data[key]
                break
        else:
            return {}
    return data if isinstance(data, dict) and "properties" in data else {}


def choices_to_schema(data) -> dict:
    """Normalise a choice-list reply (JSON schema piece, or EarthRanger choice
    rows [{value, display}]) into {"anyOf": [{"const", "title"}]}; {} if unreadable."""
    for _ in range(3):
        if isinstance(data, dict) and "data" in data and not any(k in data for k in ("anyOf", "oneOf", "enum")):
            data = data["data"]
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            data = data["results"]
    if isinstance(data, dict) and any(k in data for k in ("anyOf", "oneOf", "enum")):
        return data
    if isinstance(data, list):
        opts = []
        for r in data:
            if isinstance(r, dict) and "const" in r:
                opts.append({"const": r["const"], "title": r.get("title", r["const"])})
            elif isinstance(r, dict) and r.get("value") is not None:
                if r.get("is_active") is False:
                    continue
                opts.append({"const": r["value"], "title": r.get("display") or r["value"]})
        return {"anyOf": opts} if opts else {}
    return {}

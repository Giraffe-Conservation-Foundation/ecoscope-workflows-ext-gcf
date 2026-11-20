"""
gcf_event_upload -- bulk-create EarthRanger events of any event type from a CSV.

Ecoscope Desktop version of the TwigaTools "Event Upload" open module. One
task, three actions chosen in the workflow form:

  Make template     write <event_type>_template.csv and <event_type>_field_guide.csv
                    (built from the event type's own form, v1 or v2) to the results folder
  Check file only   (default) read the filled CSV, check every row, write
                    <event_type>_problems.csv -- nothing is sent to EarthRanger
  Check and import  as above, then create the rows that passed and write
                    <event_type>_import_results.csv (CSV row -> new serial number)

Returns a short Markdown summary for a text widget on the results dashboard.
Registered as a task in __init__.py (top-level name, like the other GCF tasks).
The row-checking rules live in _event_upload_rules.py (shared with TwigaTools).
"""


import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from pydantic import Field

try:  # the EarthRanger connection type tasks receive (named connection -> client)
    from ecoscope.platform.connections import EarthRangerClient
except ImportError:  # older layouts
    from ecoscope.platform.annotations import EarthRangerClient  # type: ignore

from ._event_upload_rules import (
    build_events, choices_to_schema, field_guide, inline_remote_refs, match_columns,
    schema_fields, schema_from_response, template_csv,
)

MAX_ROWS = 2000
_WORKERS = 4
ACTIONS = ("Make template", "Check file only", "Check and import")


# ======== EarthRanger helpers ========

def _server(client) -> str:
    root = str(getattr(client, "service_root", "") or getattr(client, "server", "") or "").rstrip("/")
    server = root.rsplit("/api/", 1)[0] if "/api/" in root else root
    if server and not server.startswith("http"):
        server = "https://" + server
    return server


def _headers(client) -> dict:
    try:
        return dict(client.auth_headers())
    except Exception:
        token = getattr(client, "token", None)
        return {"Authorization": f"Bearer {token}"} if token else {}


def _get_all(url: str, headers: dict, params: dict | None = None, max_pages: int = 50) -> list:
    out = []
    for _ in range(max_pages):
        resp = requests.get(url, params=params, headers=headers, timeout=120)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, list):
            out.extend(data)
            break
        out.extend(data.get("results", []) if isinstance(data, dict) else [])
        url, params = (data.get("next") if isinstance(data, dict) else None), None
        if not url:
            break
    return out


def _find_event_type(server: str, headers: dict, value: str) -> dict:
    """The event type record (v2 first, then v1) matching value or display name."""
    wanted = value.strip().lower()
    for version, url in (("v2", f"{server}/api/v2.0/activity/eventtypes/"),
                         ("v1", f"{server}/api/v1.0/activity/events/eventtypes")):
        try:
            for r in _get_all(url, headers):
                if not isinstance(r, dict):
                    continue
                if wanted in (str(r.get("value", "")).lower(), str(r.get("display", "")).strip().lower()):
                    return {**r, "_version": version}
        except Exception:
            continue
    return {}


def _load_schema(server: str, headers: dict, value: str, version: str) -> dict:
    urls = [(f"{server}/api/v2.0/activity/eventtypes/{value}/schema", None),
            (f"{server}/api/v2.0/activity/eventtypes/{value}", {"include_schema": "true"}),
            (f"{server}/api/v1.0/activity/events/schema/eventtype/{value}", None)]
    if version == "v1":
        urls = urls[2:] + urls[:2]
    for url, params in urls:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            if resp.status_code == 200 and (schema := schema_from_response(resp.json())):
                return schema
        except Exception:
            continue
    return {}


def _choice_loader(server: str, headers: dict):
    memo: dict = {}
    all_choices: dict | None = None

    def load(ref: str) -> dict:
        nonlocal all_choices
        if ref in memo:
            return memo[ref]
        url = ref if ref.startswith("http") else f"{server}/{ref.lstrip('/')}"
        result = {}
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            if resp.status_code == 200:
                result = choices_to_schema(resp.json())
        except Exception:
            pass
        if not result:
            field = parse_qs(urlparse(ref).query).get("field", [""])[0]
            if field:
                if all_choices is None:
                    all_choices = {}
                    try:
                        for r in _get_all(f"{server}/api/v1.0/choices/", headers, {"page_size": 1000}):
                            if isinstance(r, dict) and r.get("field"):
                                all_choices.setdefault(str(r["field"]), []).append(r)
                    except Exception:
                        pass
                rows = sorted(all_choices.get(field, []), key=lambda r: r.get("ordernum") or 0)
                result = choices_to_schema(rows)
        memo[ref] = result
        return result

    return load


def _post_one(url: str, headers: dict, item: dict) -> dict:
    try:
        resp = requests.post(url, json=item["payload"], headers=headers, timeout=60)
        if resp.status_code >= 400:
            msg = resp.text[:300]
            try:
                body = resp.json()
                msg = str(body.get("status", {}).get("detail") or body.get("data") or body)[:300]
            except Exception:
                pass
            return {"row": item["row"], "status": "failed", "serial_number": None,
                    "event_id": None, "error": f"HTTP {resp.status_code}: {msg}"}
        body = resp.json()
        data = body.get("data", body) if isinstance(body, dict) else {}
        return {"row": item["row"], "status": "created", "serial_number": data.get("serial_number"),
                "event_id": data.get("id"), "error": ""}
    except Exception as e:
        return {"row": item["row"], "status": "failed", "serial_number": None,
                "event_id": None, "error": str(e)[:300]}


# ======== Output ========

def _write(root_path: str, filename: str, text: str) -> str:
    try:
        from ecoscope.platform.serde import _persist_text  # handles local paths and URLs
        return _persist_text(text, root_path, filename)
    except ImportError:
        root = root_path[len("file://"):] if root_path.startswith("file://") else root_path
        path = Path(root) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)


def _read_csv(csv_path: str) -> pd.DataFrame:
    p = csv_path.strip().strip('"').strip("'")
    if p.startswith("file://"):
        p = p[len("file://"):]
    path = Path(p).expanduser()
    if not path.is_file():
        raise ValueError(f"Can't find the CSV file '{csv_path}'. Paste the full path to the file, "
                         "e.g. C:\\Users\\me\\Documents\\sightings.csv")
    raw = path.read_bytes()
    return pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False, encoding="utf-8-sig")


# ======== Task ========

def gcf_event_upload(
    client: EarthRangerClient,
    event_type: Annotated[
        str,
        Field(description=(
            "The event type to upload. Enter its value (e.g. giraffe_survey_encounter_nam) "
            "or its name as shown in EarthRanger."
        )),
    ],
    action: Annotated[
        Literal["Make template", "Check file only", "Check and import"],
        Field(description=(
            "Make template: save a blank CSV template and field guide for this event type. "
            "Check file only: check your filled CSV and list any problems (nothing is sent). "
            "Check and import: check the CSV, then create the rows that passed in EarthRanger."
        )),
    ] = "Check file only",
    csv_path: Annotated[
        str,
        Field(description=(
            "Full path to your filled CSV on this computer (not needed for Make template), "
            "e.g. C:\\Users\\me\\Documents\\sightings.csv"
        )),
    ] = "",
    time_zone: Annotated[
        str,
        Field(description=(
            "Time zone of the times in your CSV, e.g. Africa/Johannesburg, Africa/Windhoek, "
            "Africa/Nairobi or UTC. Times with their own zone (ending in Z or +02:00) keep it."
        )),
    ] = "UTC",
    root_path: Annotated[str, Field(description="Results folder", exclude=True)] = ".",
) -> Annotated[str, Field(description="Markdown summary of the run")]:
    """Make a CSV template for any EarthRanger event type, or check and import a filled one."""
    if action not in ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(ACTIONS)}")
    server, headers = _server(client), _headers(client)
    if not server:
        raise ValueError("Couldn't work out the EarthRanger server from the connection.")

    et = _find_event_type(server, headers, event_type)
    if not et:
        raise ValueError(f"Event type '{event_type}' wasn't found on {server}, or this account "
                         "can't see it. Check the value in EarthRanger.")
    value, display, version = str(et["value"]), str(et.get("display") or et["value"]), et["_version"]

    schema = _load_schema(server, headers, value, version)
    schema = inline_remote_refs(schema, _choice_loader(server, headers))
    fields, skipped = schema_fields(schema)
    lines = [f"### Event Upload — {display} (`{value}`)", ""]

    if action == "Make template":
        _write(root_path, f"{value}_template.csv", template_csv(fields))
        _write(root_path, f"{value}_field_guide.csv", field_guide(fields).to_csv(index=False))
        n_drop = sum(f.kind in ("choice", "multichoice", "choice_unknown", "multichoice_unknown") for f in fields)
        lines += [
            f"Saved **{value}_template.csv** and **{value}_field_guide.csv** to the results folder.",
            "",
            f"- {len(fields)} form field(s), {n_drop} of them dropdowns. Only **event_datetime** is always required.",
            "- The field guide gives each field's type and the allowed dropdown values.",
            "- Fill one row per event, then run again with **Check file only**.",
        ]
        if not schema:
            lines.append("- EarthRanger didn't return a form for this type: only the standard columns are included.")
        if skipped:
            lines.append("- Can't be filled from a CSV (left empty): "
                         + "; ".join(f"{s.title} ({s.reason})" for s in skipped))
        unknown = [f.title for f in fields if f.kind.endswith("_unknown")]
        if unknown:
            lines.append("- Dropdown options couldn't be read for: " + ", ".join(unknown)
                         + " — enter values exactly as EarthRanger stores them.")
        return "\n".join(lines)

    # ── Check (and import) ──
    if not csv_path.strip():
        raise ValueError("Enter the path to your filled CSV in 'csv_path'.")
    df = _read_csv(csv_path)
    df = df[~df.apply(lambda r: all(str(v).strip() == "" for v in r), axis=1)]
    if df.empty:
        raise ValueError("The CSV has no rows.")
    if len(df) > MAX_ROWS:
        raise ValueError(f"The CSV has {len(df)} rows; the limit is {MAX_ROWS} per run. Please split it.")
    mapping, unknown_cols = match_columns(list(df.columns), fields)
    if "event_datetime" not in mapping.values():
        raise ValueError("The CSV needs an event_datetime column. Start from the template (Make template).")

    results = build_events(df, mapping, fields, value, display, time_zone)
    good = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    problems = pd.DataFrame([{"csv_row": r["row"], "problem": p} for r in bad for p in r["errors"]],
                            columns=["csv_row", "problem"])
    _write(root_path, f"{value}_problems.csv", problems.to_csv(index=False))

    lines += [f"- Rows in file: **{len(results)}**",
              f"- Ready to import: **{len(good)}**",
              f"- With problems: **{len(bad)}** (see **{value}_problems.csv**; row numbers match the "
              "spreadsheet, header = row 1)"]
    if unknown_cols:
        lines.append("- Columns ignored (not in the form): " + ", ".join(unknown_cols))

    if action == "Check file only":
        lines += ["", "Nothing was sent to EarthRanger. When the file is ready, run again with "
                  "**Check and import** (only rows without problems are imported)."]
        return "\n".join(lines)

    if not good:
        lines += ["", "No rows passed the checks, so nothing was imported."]
        return "\n".join(lines)

    url = f"{server}/api/v1.0/activity/events/"
    post_headers = {**headers, "Content-Type": "application/json"}
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        res = list(pool.map(lambda it: _post_one(url, post_headers, it), good))
    res_df = pd.DataFrame(res).sort_values("row").rename(columns={"row": "csv_row"})
    _write(root_path, f"{value}_import_results.csv", res_df.to_csv(index=False))
    created = int((res_df["status"] == "created").sum())
    lines += ["", f"**Created {created} event(s)** on {server}."]
    if created < len(res_df):
        lines.append(f"**{len(res_df) - created} failed** — see the error column in **{value}_import_results.csv**.")
    lines.append(f"Serial numbers for every row are in **{value}_import_results.csv**. "
                 "Don't run Check and import on the same file again, or the events will be duplicated.")
    return "\n".join(lines)

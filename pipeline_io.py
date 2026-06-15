"""
Shared pipeline I/O helpers.

The workflow uses compressed JSON Lines for local handoffs instead of Excel.
JSONL keeps arrays and nested objects intact, streams well in CI, and avoids
spreadsheet type coercion for IDs, timestamps, and list/struct columns.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is installed in CI, this is defensive.
    np = None


OUTPUT_DIR = Path(os.getenv("PIPELINE_OUTPUT_DIR", "output"))
REGEX_MATCHES_PATH = OUTPUT_DIR / "regex_matches.jsonl.gz"
UNMATCHED_TICKETS_PATH = OUTPUT_DIR / "unmatched_tickets.jsonl.gz"
REQUEST_INTENT_RESULTS_PATH = OUTPUT_DIR / "request_intent_results.jsonl.gz"
WORKFLOW_RUN_ID_PATH = OUTPUT_DIR / "workflow_run_id.txt"
WORKFLOW_RUN_ID_ENV = "WORKFLOW_RUN_ID"


def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _is_missing_scalar(value: Any) -> bool:
    if value is None:
        return True

    if isinstance(value, float) and math.isnan(value):
        return True

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _native_number(value: Any) -> Any:
    if np is not None:
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            if math.isnan(float(value)):
                return None
            return float(value)
        if isinstance(value, np.bool_):
            return bool(value)
    return value


def normalize_json_value(value: Any) -> Any:
    """Convert pandas/numpy/datetime values into JSON-safe Python values."""

    value = _native_number(value)

    if isinstance(value, Mapping):
        return {
            str(key): normalize_json_value(inner_value)
            for key, inner_value in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [normalize_json_value(item) for item in value]

    if _is_missing_scalar(value):
        return None

    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        if value.tzinfo is None:
            value = value.tz_localize(timezone.utc)
        return value.isoformat()

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    return value


def write_records(records: Iterable[Mapping[str, Any]], path: str | Path) -> Path:
    """Write records as compressed JSON Lines."""

    ensure_output_dir()
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with gzip.open(output_path, "wt", encoding="utf-8") as file:
        for record in records:
            json.dump(
                normalize_json_value(record),
                file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            file.write("\n")

    return output_path


def read_records(path: str | Path) -> list[dict[str, Any]]:
    input_path = Path(path)
    if not input_path.exists():
        return []

    records: list[dict[str, Any]] = []
    with gzip.open(input_path, "rt", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    return records


def write_dataframe(df: pd.DataFrame, path: str | Path) -> Path:
    return write_records(df.to_dict(orient="records"), path)


def read_dataframe(path: str | Path) -> pd.DataFrame:
    return pd.DataFrame(read_records(path))


def _id_part(value: Any) -> str:
    value = _native_number(value)

    if _is_missing_scalar(value):
        return ""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


def build_request_id(zendesk_ticket_id: Any, request_number: Any) -> str | None:
    """Build the immutable request key used by the BigQuery history table."""

    ticket_id = _id_part(zendesk_ticket_id)
    request_no = _id_part(request_number)

    if not ticket_id or not request_no:
        return None

    return f"{ticket_id}_{request_no}"


def get_workflow_run_id() -> str:
    """
    Return the workflow GUID for this run.

    GitHub Actions sets WORKFLOW_RUN_ID in the workflow file.  Local runs use a
    persisted file so separate script invocations in the same working directory
    still share one GUID.
    """

    env_value = os.getenv(WORKFLOW_RUN_ID_ENV, "").strip()
    if env_value:
        return env_value

    ensure_output_dir()

    if WORKFLOW_RUN_ID_PATH.exists():
        value = WORKFLOW_RUN_ID_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value

    value = str(uuid.uuid4())
    WORKFLOW_RUN_ID_PATH.write_text(value, encoding="utf-8")
    return value

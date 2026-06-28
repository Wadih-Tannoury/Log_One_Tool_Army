"""
ticket_fetcher.py

Fetch active Zendesk requests into the BigQuery active-ticket table and expose
BigQuery history logging helpers used by the final response-generation stage.

The history logging logic intentionally lives here so there is no separate
history-logging script.  Importing this module is safe: Zendesk fetching only
runs when this file is executed as a script.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlparse

import pandas as pd
import requests
from requests.auth import HTTPBasicAuth

from pipeline_io import (
    REQUEST_INTENT_RESULTS_PATH,
    build_request_id,
    get_workflow_run_id,
    read_dataframe,
)
from customs_rules import (
    CARRIER_EMAIL_DOMAINS,
    VALID_TICKET_CATEGORIES,
    carrier_code_from_email,
    classify_ticket_category_from_content,
    email_matches_any_carrier_domain,
    normalize_email,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

PROJECT_ID = os.getenv("PROJECT_ID", "tlg-business-intelligence-prd")

CONFIG_TABLE = os.getenv(
    "LOG_CONFIG_TABLE",
    "tlg-business-intelligence-prd.til.log_one_tool_army_config",
)

TARGET_TABLE = os.getenv(
    "ACTIVE_TICKETS_TABLE",
    "tlg-business-intelligence-prd.til.log_one_tool_army_active_tickets",
)

HISTORY_TABLE = os.getenv(
    "LOG_HISTORY_TABLE",
    "tlg-business-intelligence-prd.til.log_one_tool_army_logs_history",
)

BQ_STORAGE = False

ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "thelevelgroup")
ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL", "beatrice.bettini@thelevelgroup.com")

ZENDESK_RESPONSE_SUBMISSION_ENV = "SUBMIT_ZENDESK_RESPONSES"
# Safety default: never submit public ticket replies unless the workflow/env flag
# explicitly opts in with true/1/yes/y/on.
DEFAULT_SUBMIT_ZENDESK_RESPONSES = False
ZENDESK_STATUS_AFTER_REPLY_ENV = "ZENDESK_STATUS_AFTER_REPLY"
DEFAULT_ZENDESK_STATUS_AFTER_REPLY = "solved"

ACTIVE_TICKET_COLUMNS = [
    "ingestion_timestamp",
    "request_submission_timestamp",
    "ticket_submission_timestamp",
    "request_id",
    "zendesk_ticket_id",
    "request_number",
    "requester_email",
    "subject",
    "request_body",
    "ticket_category",
    "extracted_tracking_number",
    "shipment_order_number",
    "shipment_tracking_number",
    "return_tracking_number",
    "shipment_carrier_code",
    "return_carrier_code",
    "tracking_not_found_in_shipping_platform_shipments",
]

HISTORY_COLUMNS = [
    "ingestion_timestamp",
    "request_submission_timestamp",
    "ticket_submission_timestamp",
    "workflow_run_id",
    "request_run_id",
    "zendesk_ticket_id",
    "request_number",
    "request_id",
    "requester_email",
    "subject",
    "request_body",
    "cleaned_request_body",
    "ticket_category",
    "extracted_tracking_number",
    "shipment_order_number",
    "shipment_tracking_number",
    "return_tracking_number",
    "notes",
    "human_intervention_required",
    "regex_request_types",
    "regex_requested_data",
    "regex_confidence",
    "matched_spans",
    "request_language",
    "language_confidence",
    "language_notes",
    "llm_was_used",
    "llm_confidence",
    "requested_data",
    "draft_response",
    "final_response",
]

VALID_CATEGORIES = list(VALID_TICKET_CATEGORIES)
# Zendesk statuses the workflow should process. The previous broad query
# (-closed/-solved) also included hold tickets, which can be very large and
# are intentionally not considered active for this workflow.
ACTIVE_ZENDESK_STATUSES = ("new", "open", "pending")

LLM_ENGINE_PREFIXES = ("llm",)
LLM_ENGINE_NAMES = {
    "regex_llm_disagreement_guard",
}


# ============================================================================
# CLIENT AND AUTH HELPERS
# ============================================================================


def bigquery_client():
    """Create a BigQuery client from BI_BIGQUERY_CREDS."""

    from google.cloud import bigquery
    from google.oauth2 import service_account

    bq_credentials = json.loads(os.environ["BI_BIGQUERY_CREDS"])
    credentials = service_account.Credentials.from_service_account_info(
        bq_credentials
    )

    client = bigquery.Client(project=PROJECT_ID, credentials=credentials)
    return client, bigquery


def zendesk_auth() -> HTTPBasicAuth:
    zendesk_config = json.loads(os.environ["ZENDESK_API_CREDENTIALS"])
    zendesk_api_token = zendesk_config["ZENDESK_API_TOKEN"]

    return HTTPBasicAuth(
        f"{ZENDESK_EMAIL}/token",
        zendesk_api_token,
    )


def zendesk_base_url() -> str:
    return f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"


# ============================================================================
# ACTIVE TABLE HELPERS
# ============================================================================


def active_tickets_schema(bigquery):
    return [
        bigquery.SchemaField("ingestion_timestamp", "TIMESTAMP"),
        bigquery.SchemaField("request_submission_timestamp", "TIMESTAMP"),
        bigquery.SchemaField("ticket_submission_timestamp", "TIMESTAMP"),
        bigquery.SchemaField("request_id", "STRING"),
        bigquery.SchemaField("zendesk_ticket_id", "INT64"),
        bigquery.SchemaField("request_number", "INT64"),
        bigquery.SchemaField("requester_email", "STRING"),
        bigquery.SchemaField("subject", "STRING"),
        bigquery.SchemaField("request_body", "STRING"),
        bigquery.SchemaField("ticket_category", "STRING"),
        bigquery.SchemaField("extracted_tracking_number", "STRING"),
        bigquery.SchemaField("shipment_order_number", "STRING"),
        bigquery.SchemaField("shipment_tracking_number", "STRING"),
        bigquery.SchemaField("return_tracking_number", "STRING"),
        bigquery.SchemaField("shipment_carrier_code", "STRING"),
        bigquery.SchemaField("return_carrier_code", "STRING"),
        bigquery.SchemaField(
            "tracking_not_found_in_shipping_platform_shipments",
            "BOOL",
        ),
    ]


def empty_active_tickets_table(client) -> None:
    client.query(
        f"""
        CREATE OR REPLACE TABLE `{TARGET_TABLE}` (
            ingestion_timestamp TIMESTAMP,
            request_submission_timestamp TIMESTAMP,
            ticket_submission_timestamp TIMESTAMP,
            request_id STRING,
            zendesk_ticket_id INT64,
            request_number INT64,
            requester_email STRING,
            subject STRING,
            request_body STRING,
            ticket_category STRING,
            extracted_tracking_number STRING,
            shipment_order_number STRING,
            shipment_tracking_number STRING,
            return_tracking_number STRING,
            shipment_carrier_code STRING,
            return_carrier_code STRING,
            tracking_not_found_in_shipping_platform_shipments BOOL
        )
        """
    ).result()


def replace_active_tickets(df: pd.DataFrame, client=None, bigquery=None) -> None:
    if client is None or bigquery is None:
        client, bigquery = bigquery_client()

    df = df.reindex(columns=ACTIVE_TICKET_COLUMNS)

    if df.empty:
        empty_active_tickets_table(client)
        print("Successfully replaced active ticket table with 0 rows.")
        return

    for timestamp_column in [
        "ingestion_timestamp",
        "request_submission_timestamp",
        "ticket_submission_timestamp",
    ]:
        df[timestamp_column] = pd.to_datetime(
            df[timestamp_column],
            utc=True,
            errors="coerce",
        )

    job_config = bigquery.LoadJobConfig(
        schema=active_tickets_schema(bigquery),
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    load_job = client.load_table_from_dataframe(
        df,
        TARGET_TABLE,
        job_config=job_config,
    )
    load_job.result()

    print(f"Successfully replaced active ticket table with {len(df)} rows.")


def _existing_request_ids(client, bigquery, request_ids: Iterable[str]) -> set[str]:
    request_ids = sorted({str(request_id) for request_id in request_ids if request_id})
    if not request_ids:
        return set()

    query = f"""
    SELECT request_id
    FROM `{HISTORY_TABLE}`
    WHERE request_id IN UNNEST(@request_ids)
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter(
                "request_ids",
                "STRING",
                request_ids,
            )
        ]
    )

    return {
        row["request_id"]
        for row in client.query(query, job_config=job_config).result()
    }


def existing_history_request_ids(
    request_ids: Iterable[str],
    client=None,
    bigquery=None,
) -> set[str]:
    if client is None or bigquery is None:
        client, bigquery = bigquery_client()
    return _existing_request_ids(client, bigquery, request_ids)


# ============================================================================
# HISTORY TABLE LOGGING HELPERS
# ============================================================================


def _is_missing(value: Any) -> bool:
    if value is None:
        return True

    if isinstance(value, float) and math.isnan(value):
        return True

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _to_int(value: Any) -> int | None:
    if _is_missing(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _to_str(value: Any) -> str | None:
    if _is_missing(value):
        return None
    return str(value)


def _to_timestamp(value: Any) -> str | None:
    if _is_missing(value):
        return None

    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None

    return timestamp.to_pydatetime().isoformat().replace("+00:00", "Z")


def _parse_collection(value: Any) -> list[Any]:
    if _is_missing(value):
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, tuple):
        return list(value)

    if isinstance(value, set):
        return list(value)

    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw.lower() in {"nan", "none", "null", "n/a", "na"}:
            return []

        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(raw)
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, tuple):
                    return list(parsed)
                if isinstance(parsed, set):
                    return list(parsed)
                if _is_missing(parsed):
                    return []
                return [parsed]
            except Exception:
                continue

        return [raw]

    return [value]


def _string_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in _parse_collection(value):
        if _is_missing(item):
            continue
        item_str = str(item).strip()
        if item_str and item_str not in result:
            result.append(item_str)
    return result


def _matched_spans(value: Any) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []

    for item in _parse_collection(value):
        if not isinstance(item, Mapping):
            continue

        span = {
            "request_type": _to_str(item.get("request_type")),
            "span": _to_str(item.get("span")),
            "start": _to_int(item.get("start")),
            "end_pos": _to_int(item.get("end_pos", item.get("end"))),
        }
        spans.append(span)

    return spans


def _infer_llm_was_used(row: Mapping[str, Any]) -> bool:
    if "llm_was_used" in row and not _is_missing(row.get("llm_was_used")):
        return bool(_to_bool(row.get("llm_was_used")))

    engine = str(row.get("engine", "") or "").strip().lower()
    return engine.startswith(LLM_ENGINE_PREFIXES) or engine in LLM_ENGINE_NAMES


def history_schema(bigquery):
    return [
        bigquery.SchemaField("ingestion_timestamp", "TIMESTAMP"),
        bigquery.SchemaField("request_submission_timestamp", "TIMESTAMP"),
        bigquery.SchemaField("ticket_submission_timestamp", "TIMESTAMP"),
        bigquery.SchemaField("workflow_run_id", "STRING"),
        bigquery.SchemaField("request_run_id", "STRING"),
        bigquery.SchemaField("zendesk_ticket_id", "INT64"),
        bigquery.SchemaField("request_number", "INT64"),
        bigquery.SchemaField("request_id", "STRING"),
        bigquery.SchemaField("requester_email", "STRING"),
        bigquery.SchemaField("subject", "STRING"),
        bigquery.SchemaField("request_body", "STRING"),
        bigquery.SchemaField("cleaned_request_body", "STRING"),
        bigquery.SchemaField("ticket_category", "STRING"),
        bigquery.SchemaField("extracted_tracking_number", "STRING"),
        bigquery.SchemaField("shipment_order_number", "STRING"),
        bigquery.SchemaField("shipment_tracking_number", "STRING"),
        bigquery.SchemaField("return_tracking_number", "STRING"),
        bigquery.SchemaField("notes", "STRING"),
        bigquery.SchemaField("human_intervention_required", "BOOL"),
        bigquery.SchemaField("regex_request_types", "STRING", mode="REPEATED"),
        bigquery.SchemaField("regex_requested_data", "STRING", mode="REPEATED"),
        bigquery.SchemaField("regex_confidence", "FLOAT64"),
        bigquery.SchemaField(
            "matched_spans",
            "RECORD",
            mode="REPEATED",
            fields=[
                bigquery.SchemaField("request_type", "STRING"),
                bigquery.SchemaField("span", "STRING"),
                bigquery.SchemaField("start", "INT64"),
                bigquery.SchemaField("end_pos", "INT64"),
            ],
        ),
        bigquery.SchemaField("request_language", "STRING"),
        bigquery.SchemaField("language_confidence", "FLOAT64"),
        bigquery.SchemaField("language_notes", "STRING"),
        bigquery.SchemaField("llm_was_used", "BOOL"),
        bigquery.SchemaField("llm_confidence", "FLOAT64"),
        bigquery.SchemaField("requested_data", "STRING", mode="REPEATED"),
        bigquery.SchemaField("draft_response", "STRING"),
        bigquery.SchemaField("final_response", "STRING"),
    ]


def _ensure_history_schema(client) -> None:
    """Apply additive history-table schema changes required by this version."""

    client.query(
        f"""
        ALTER TABLE `{HISTORY_TABLE}`
        ADD COLUMN IF NOT EXISTS final_response STRING
        """
    ).result()


def _target_history_schema(client):
    """
    Use the live destination schema for history appends.

    This keeps logging independent of the physical column order in BigQuery and
    allows future nullable columns to exist in the table even when this pipeline
    does not populate them yet.
    """

    _ensure_history_schema(client)

    table = client.get_table(HISTORY_TABLE)
    actual_columns = {field.name for field in table.schema}
    missing_columns = [column for column in HISTORY_COLUMNS if column not in actual_columns]

    if missing_columns:
        raise RuntimeError(
            "BigQuery history table is missing expected columns: "
            + ", ".join(missing_columns)
        )

    return table.schema


def prepare_history_row(
    row: Mapping[str, Any],
    *,
    workflow_run_id: str,
    ingestion_timestamp: str,
) -> dict[str, Any]:
    zendesk_ticket_id = _to_int(row.get("zendesk_ticket_id"))
    request_number = _to_int(row.get("request_number"))
    request_id = _to_str(row.get("request_id")) or build_request_id(
        zendesk_ticket_id,
        request_number,
    )

    llm_was_used = _infer_llm_was_used(row)
    regex_confidence = _to_float(row.get("regex_confidence"))
    llm_confidence = _to_float(row.get("llm_confidence"))

    # Backward-compatible fallbacks for older local handoff files that only
    # contained a generic confidence column.  For regex-only rows the generic
    # value belongs to regex_confidence; for LLM rows it belongs to llm_confidence.
    if regex_confidence is None and not llm_was_used:
        regex_confidence = _to_float(row.get("confidence"))

    if llm_confidence is None and llm_was_used:
        llm_confidence = _to_float(row.get("confidence"))

    prepared = {
        "ingestion_timestamp": ingestion_timestamp,
        "request_submission_timestamp": _to_timestamp(
            row.get("request_submission_timestamp")
        ),
        "ticket_submission_timestamp": _to_timestamp(
            row.get("ticket_submission_timestamp")
        ),
        "workflow_run_id": workflow_run_id,
        "request_run_id": str(uuid.uuid4()),
        "zendesk_ticket_id": zendesk_ticket_id,
        "request_number": request_number,
        "request_id": request_id,
        "requester_email": _to_str(row.get("requester_email")),
        "subject": _to_str(row.get("subject")),
        "request_body": _to_str(row.get("request_body")),
        "cleaned_request_body": _to_str(row.get("cleaned_request_body")),
        "ticket_category": _to_str(row.get("ticket_category")),
        "extracted_tracking_number": _to_str(row.get("extracted_tracking_number")),
        "shipment_order_number": _to_str(row.get("shipment_order_number")),
        "shipment_tracking_number": _to_str(row.get("shipment_tracking_number")),
        "return_tracking_number": _to_str(row.get("return_tracking_number")),
        "notes": _to_str(row.get("notes")),
        "human_intervention_required": bool(
            _to_bool(row.get("human_intervention_required"))
        ),
        "regex_request_types": _string_list(row.get("regex_request_types")),
        "regex_requested_data": _string_list(row.get("regex_requested_data")),
        "regex_confidence": regex_confidence,
        "matched_spans": _matched_spans(row.get("matched_spans")),
        "request_language": _to_str(row.get("request_language")),
        "language_confidence": _to_float(row.get("language_confidence")),
        "language_notes": _to_str(row.get("language_notes")),
        "llm_was_used": llm_was_used,
        "llm_confidence": llm_confidence,
        "requested_data": _string_list(row.get("requested_data")),
        "draft_response": _to_str(row.get("draft_response")),
        "final_response": _to_str(row.get("final_response")),
    }

    return {column: prepared.get(column) for column in HISTORY_COLUMNS}


def append_history_rows(
    rows: Iterable[Mapping[str, Any]] | pd.DataFrame,
    *,
    return_inserted_request_ids: bool = False,
) -> int | tuple[int, list[str]]:
    """Append final workflow rows to the BigQuery history table."""

    def _return(count: int, inserted_request_ids: list[str] | None = None):
        if return_inserted_request_ids:
            return count, list(inserted_request_ids or [])
        return count

    if isinstance(rows, pd.DataFrame):
        raw_rows = rows.to_dict(orient="records")
    else:
        raw_rows = list(rows)

    if not raw_rows:
        print("No final rows to log to BigQuery history.")
        return _return(0)

    workflow_run_id = get_workflow_run_id()
    ingestion_timestamp = datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )

    prepared_rows = [
        prepare_history_row(
            row,
            workflow_run_id=workflow_run_id,
            ingestion_timestamp=ingestion_timestamp,
        )
        for row in raw_rows
    ]
    prepared_rows = [row for row in prepared_rows if row.get("request_id")]

    unique_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in prepared_rows:
        request_id = row["request_id"]
        if request_id in seen_ids:
            continue
        seen_ids.add(request_id)
        unique_rows.append(row)

    if not unique_rows:
        print("No rows with a valid request_id to log to BigQuery history.")
        return _return(0)

    client, bigquery = bigquery_client()
    target_schema = _target_history_schema(client)

    existing_ids = _existing_request_ids(
        client,
        bigquery,
        [row["request_id"] for row in unique_rows],
    )

    rows_to_insert = [
        row for row in unique_rows if row["request_id"] not in existing_ids
    ]

    skipped_count = len(unique_rows) - len(rows_to_insert)
    if skipped_count:
        print(f"Skipped {skipped_count} rows already present in {HISTORY_TABLE}.")

    if not rows_to_insert:
        print("No new rows to append to BigQuery history.")
        return _return(0)

    job_config = bigquery.LoadJobConfig(
        schema=target_schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )

    load_job = client.load_table_from_json(
        rows_to_insert,
        HISTORY_TABLE,
        job_config=job_config,
    )
    load_job.result()

    print(
        f"Logged {len(rows_to_insert)} rows to {HISTORY_TABLE} "
        f"with workflow_run_id={workflow_run_id}."
    )

    return _return(
        len(rows_to_insert),
        [str(row["request_id"]) for row in rows_to_insert if row.get("request_id")],
    )


def new_history_request_ids(rows: Iterable[Mapping[str, Any]] | pd.DataFrame) -> list[str]:
    """Return request_ids that are not already present in the history table."""

    if isinstance(rows, pd.DataFrame):
        raw_rows = rows.to_dict(orient="records")
    else:
        raw_rows = list(rows)

    if not raw_rows:
        return []

    workflow_run_id = get_workflow_run_id()
    ingestion_timestamp = datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    prepared_rows = [
        prepare_history_row(
            row,
            workflow_run_id=workflow_run_id,
            ingestion_timestamp=ingestion_timestamp,
        )
        for row in raw_rows
    ]

    request_ids: list[str] = []
    for row in prepared_rows:
        request_id = row.get("request_id")
        if request_id and request_id not in request_ids:
            request_ids.append(str(request_id))

    if not request_ids:
        return []

    client, bigquery = bigquery_client()
    _target_history_schema(client)
    existing_ids = _existing_request_ids(client, bigquery, request_ids)
    return [request_id for request_id in request_ids if request_id not in existing_ids]


def append_history_from_file(path=REQUEST_INTENT_RESULTS_PATH) -> int:
    """Convenience CLI helper to append output/request_intent_results.jsonl.gz."""

    df = read_dataframe(path)
    return append_history_rows(df)


# ============================================================================
# ZENDESK RESPONSE HELPERS
# ============================================================================


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def zendesk_response_submission_enabled() -> bool:
    """Return True only when public Zendesk replies are explicitly enabled."""

    return _env_bool(
        ZENDESK_RESPONSE_SUBMISSION_ENV,
        DEFAULT_SUBMIT_ZENDESK_RESPONSES,
    )


def zendesk_status_after_reply() -> str:
    """Return the ticket status to set when posting an automatic Zendesk reply."""

    status = str(
        os.getenv(
            ZENDESK_STATUS_AFTER_REPLY_ENV,
            DEFAULT_ZENDESK_STATUS_AFTER_REPLY,
        )
        or ""
    ).strip().lower()
    if not status:
        return ""

    valid_statuses = {"new", "open", "pending", "hold", "solved", "closed"}
    if status not in valid_statuses:
        raise ValueError(
            f"Unsupported {ZENDESK_STATUS_AFTER_REPLY_ENV}={status!r}; "
            "use one of: new, open, pending, hold, solved, closed"
        )
    return status


def _response_is_blank(value: Any) -> bool:
    if _is_missing(value):
        return True
    return str(value).strip().lower() in {"", "nan", "none", "null", "n/a", "na"}


def _attachment_paths(value: Any) -> list[str]:
    paths: list[str] = []
    for item in _parse_collection(value):
        if _is_missing(item):
            continue
        path = str(item).strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def upload_zendesk_attachment(
    path: str | Path,
    *,
    auth: HTTPBasicAuth | None = None,
    base_url: str | None = None,
) -> str:
    """Upload one local file to Zendesk and return its upload token."""

    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"Zendesk attachment not found: {file_path}")

    auth = auth or zendesk_auth()
    base_url = (base_url or zendesk_base_url()).rstrip("/")
    url = f"{base_url}/uploads.json?filename={quote(file_path.name)}"

    with file_path.open("rb") as file_obj:
        response = requests.post(
            url,
            auth=auth,
            data=file_obj,
            headers={"Content-Type": "application/pdf"},
            timeout=60,
        )
    response.raise_for_status()
    token = str(response.json().get("upload", {}).get("token", "") or "").strip()
    if not token:
        raise RuntimeError(f"Zendesk upload did not return a token for {file_path}")
    return token


def _normalize_comment_body(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\r\n", "\n")).strip()


def zendesk_public_comment_exists(
    ticket_id: int,
    body: str,
    *,
    auth: HTTPBasicAuth | None = None,
    base_url: str | None = None,
) -> bool:
    """Return True when the same public comment body is already on the ticket."""

    auth = auth or zendesk_auth()
    base_url = (base_url or zendesk_base_url()).rstrip("/")
    wanted_body = _normalize_comment_body(body)
    if not wanted_body:
        return False

    for comment in get_ticket_comments(ticket_id, auth=auth, base_url=base_url):
        if not comment.get("public", True):
            continue
        for body_key in ("body", "plain_body"):
            if _normalize_comment_body(comment.get(body_key)) == wanted_body:
                return True
    return False


def submit_ticket_response(
    ticket_id: int,
    body: str,
    *,
    attachment_paths: Iterable[str] = (),
    auth: HTTPBasicAuth | None = None,
    base_url: str | None = None,
) -> bool:
    """Submit one public Zendesk ticket comment with optional uploaded files."""

    if _response_is_blank(body):
        raise ValueError("Cannot submit an empty Zendesk response body")

    auth = auth or zendesk_auth()
    base_url = (base_url or zendesk_base_url()).rstrip("/")

    if _env_bool("ZENDESK_SKIP_DUPLICATE_FINAL_RESPONSE", True) and zendesk_public_comment_exists(
        ticket_id,
        body,
        auth=auth,
        base_url=base_url,
    ):
        print(f"Skipped Zendesk ticket {ticket_id}: final_response is already present.")
        return False

    upload_tokens = [
        upload_zendesk_attachment(path, auth=auth, base_url=base_url)
        for path in attachment_paths
    ]

    comment: dict[str, Any] = {
        "body": str(body).strip(),
        "public": True,
    }
    if upload_tokens:
        comment["uploads"] = upload_tokens

    ticket_update: dict[str, Any] = {"comment": comment}
    status_after_reply = zendesk_status_after_reply()
    if status_after_reply:
        ticket_update["status"] = status_after_reply

    response = requests.put(
        f"{base_url}/tickets/{int(ticket_id)}.json",
        auth=auth,
        json={"ticket": ticket_update},
        timeout=60,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError:
        if status_after_reply == "closed" and response.status_code in {400, 422}:
            ticket_update["status"] = "solved"
            fallback_response = requests.put(
                f"{base_url}/tickets/{int(ticket_id)}.json",
                auth=auth,
                json={"ticket": ticket_update},
                timeout=60,
            )
            fallback_response.raise_for_status()
            print(
                f"Zendesk ticket {ticket_id}: status 'closed' was rejected; "
                "submitted automatic reply with status 'solved' instead."
            )
        else:
            raise
    return True


def submit_final_responses(rows: Iterable[Mapping[str, Any]] | pd.DataFrame) -> int:
    """Submit non-empty final_response values to Zendesk when enabled.

    Human-intervention rows and blank final responses are intentionally skipped.
    The actual public Zendesk side effect is controlled by the
    SUBMIT_ZENDESK_RESPONSES environment/workflow flag. Set it to true to
    submit; false or an unset value logs only and performs no ticket update.
    """

    if isinstance(rows, pd.DataFrame):
        raw_rows = rows.to_dict(orient="records")
    else:
        raw_rows = list(rows)

    response_rows = []
    for row in raw_rows:
        if bool(_to_bool(row.get("human_intervention_required"))):
            continue
        final_response = row.get("final_response")
        if _response_is_blank(final_response):
            continue
        ticket_id = _to_int(row.get("zendesk_ticket_id"))
        if ticket_id is None:
            raise ValueError(
                "Cannot submit Zendesk response without zendesk_ticket_id "
                f"for request_id={row.get('request_id')}"
            )
        response_rows.append((ticket_id, str(final_response).strip(), row))

    if not response_rows:
        print("No Zendesk final responses to submit.")
        return 0

    if not zendesk_response_submission_enabled():
        print(
            f"{ZENDESK_RESPONSE_SUBMISSION_ENV}=false; skipped "
            f"{len(response_rows)} Zendesk response(s)."
        )
        return 0

    auth = zendesk_auth()
    base_url = zendesk_base_url()
    submitted = 0

    for ticket_id, final_response, row in response_rows:
        attachment_paths = _attachment_paths(row.get("zendesk_attachment_paths"))
        if submit_ticket_response(
            ticket_id,
            final_response,
            attachment_paths=attachment_paths,
            auth=auth,
            base_url=base_url,
        ):
            submitted += 1

    print(f"Submitted {submitted} Zendesk final response(s).")
    return submitted


# ============================================================================
# ZENDESK FETCH HELPERS
# ============================================================================


def load_requester_configuration(client, bigquery) -> dict[str, str]:
    """Load exact requester-email to ticket_category overrides from BigQuery.

    The fetcher no longer limits Zendesk retrieval to these emails. They are
    still authoritative: when a carrier-domain requester exists in this config
    table, its ticket_category is taken from BigQuery exactly as before.
    """

    print("Loading requester configuration table...")

    config_query = f"""
    SELECT DISTINCT
        LOWER(TRIM(requester_email)) AS requester_email,
        ticket_category
    FROM `{CONFIG_TABLE}`
    WHERE ticket_category IN UNNEST(@categories)
      AND requester_email IS NOT NULL
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter(
                "categories",
                "STRING",
                VALID_CATEGORIES,
            )
        ]
    )

    config_df = (
        client.query(config_query, job_config=job_config)
        .to_dataframe(create_bqstorage_client=BQ_STORAGE)
    )

    if config_df.empty:
        raise RuntimeError("No requester emails found in configuration table.")

    email_to_category: dict[str, str] = {}
    for _, row in config_df.iterrows():
        requester_email = normalize_email(row.get("requester_email"))
        ticket_category = str(row.get("ticket_category") or "").strip()
        if requester_email and ticket_category:
            email_to_category[requester_email] = ticket_category

    print(
        f"Loaded {len(email_to_category)} exact requester-email category overrides."
    )
    return email_to_category


def get_ticket_comments(ticket_id, *, auth, base_url: str) -> list[dict[str, Any]]:
    """Retrieve all comments for a Zendesk ticket."""

    comments: list[dict[str, Any]] = []
    url = f"{base_url}/tickets/{ticket_id}/comments.json"

    while url:
        response = requests.get(url, auth=auth, timeout=60)
        response.raise_for_status()

        payload = response.json()
        comments.extend(payload.get("comments", []))
        url = payload.get("next_page")

    return comments


def get_zendesk_user_email(
    user_id: object,
    *,
    auth,
    base_url: str,
    cache: dict[int, str] | None = None,
) -> str:
    """Return a Zendesk user's normalized email address, cached per run."""

    if user_id is None:
        return ""

    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        return ""

    if cache is not None and user_id_int in cache:
        return cache[user_id_int]

    response = requests.get(
        f"{base_url}/users/{user_id_int}.json",
        auth=auth,
        timeout=60,
    )
    response.raise_for_status()
    requester_email = normalize_email(response.json().get("user", {}).get("email"))

    if cache is not None:
        cache[user_id_int] = requester_email

    return requester_email


def requester_email_from_ticket(
    ticket: Mapping[str, Any],
    *,
    auth,
    base_url: str,
    user_email_cache: dict[int, str],
) -> str:
    """Extract the requester email used for domain filtering and config lookup.

    Zendesk search results sometimes expose the email in via.source.from.address;
    when that is absent or not a carrier-domain address, fall back to the
    requester user record.
    """

    via_email = normalize_email(
        ticket.get("via", {})
        .get("source", {})
        .get("from", {})
        .get("address", "")
    )
    if email_matches_any_carrier_domain(via_email):
        return via_email

    requester_email = get_zendesk_user_email(
        ticket.get("requester_id"),
        auth=auth,
        base_url=base_url,
        cache=user_email_cache,
    )
    if requester_email:
        return requester_email

    return via_email


def extract_tracking_number(subject, description, carrier_code):
    """Extract the most likely tracking/AWB number for the requester carrier."""

    subject = subject or ""
    description = description or ""
    carrier_code = str(carrier_code or "").strip().upper()

    patterns_by_carrier = {
        "FEDEX": [
            r"\b\d{12}\b",
            r"\b\d{15}\b",
            r"\b(?:\d{20}|\d{22})\b",
        ],
        "UPS": [
            r"\b1Z[0-9A-Z]{16}\b",
            r"\bW\d{10}\b",
        ],
        "DHL": [
            r"\bJJD\d{12,24}\b",
            r"\b\d{10}\b",
        ],
    }

    def matches(pattern):
        return re.findall(
            pattern,
            f"{subject}\n{description}",
            flags=re.IGNORECASE,
        )

    if carrier_code in patterns_by_carrier:
        values: list[str] = []
        for pattern in patterns_by_carrier[carrier_code]:
            values.extend(matches(pattern))
        return next(iter(dict.fromkeys(values)), "N/A")

    # Conservative fallback for malformed/missing requester emails.
    values = []
    for patterns in patterns_by_carrier.values():
        for pattern in patterns:
            values.extend(matches(pattern))
    return next(iter(dict.fromkeys(values)), "N/A")


def _active_zendesk_search_query(
    *,
    status: str,
    include_type_term: bool = False,
) -> str:
    """Build the Zendesk query for one active mail-ticket status.

    The fetcher runs one API-level search per active status: new, open, and
    pending. This is intentionally narrower than the previous
    ``-status:closed -status:solved`` query, which also returned ``hold``
    tickets and could make the Zendesk fetch unnecessarily slow.

    The query does not constrain requester emails or dates: Zendesk does not
    provide a reliable domain wildcard filter for this workflow, so the Python
    layer still filters requester emails to UPS/DHL/FedEx domains. Set
    ZENDESK_ACTIVE_TICKET_LOOKBACK_DAYS only if the Zendesk instance needs an
    operational safety window.

    The main fetch path uses Zendesk's cursor-based Search Export endpoint.
    That endpoint requires the object type in the separate filter[type]
    parameter and rejects type:ticket inside the query string, so the default
    query excludes the type term. include_type_term exists only for backward-
    compatible tests/debugging against the regular search endpoint.
    """

    normalized_status = str(status or "").strip().lower()
    if normalized_status not in ACTIVE_ZENDESK_STATUSES:
        raise ValueError(
            f"Unsupported Zendesk active status {status!r}; use one of: "
            + ", ".join(ACTIVE_ZENDESK_STATUSES)
        )

    search_terms = []
    if include_type_term:
        search_terms.append("type:ticket")

    search_terms.extend(
        [
            f"status:{normalized_status}",
            "via:mail",
        ]
    )

    lookback_days_raw = str(os.getenv("ZENDESK_ACTIVE_TICKET_LOOKBACK_DAYS", "") or "").strip()
    if lookback_days_raw:
        try:
            lookback_days = int(lookback_days_raw)
        except ValueError as exc:
            raise ValueError(
                "ZENDESK_ACTIVE_TICKET_LOOKBACK_DAYS must be an integer number of days"
            ) from exc
        if lookback_days > 0:
            start_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            search_terms.append(f"created>={start_date.strftime('%Y-%m-%d')}")

    return " ".join(search_terms)


def _zendesk_search_export_url(
    base_url: str,
    query: str,
    *,
    after_cursor: str | None = None,
) -> str:
    """Return a cursor-paginated Search Export URL for tickets."""

    page_size_raw = str(os.getenv("ZENDESK_SEARCH_EXPORT_PAGE_SIZE", "100") or "100").strip()
    try:
        page_size = int(page_size_raw)
    except ValueError as exc:
        raise ValueError("ZENDESK_SEARCH_EXPORT_PAGE_SIZE must be an integer") from exc

    # Zendesk allows up to 1000 records per page on this endpoint, but its own
    # docs recommend 100 for stability with large datasets.
    page_size = min(max(page_size, 1), 1000)

    params = {
        "query": query,
        "filter[type]": "ticket",
        "page[size]": str(page_size),
    }
    if after_cursor:
        params["page[after]"] = after_cursor

    return f"{base_url}/search/export.json?{urlencode(params)}"


def _zendesk_meta_has_more(meta: Mapping[str, Any]) -> bool:
    value = meta.get("has_more")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _absolute_zendesk_url(base_url: str, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        parsed_base = urlparse(base_url)
        if parsed_base.scheme and parsed_base.netloc:
            return f"{parsed_base.scheme}://{parsed_base.netloc}{url}"
    return url


def iter_zendesk_search_export_tickets(
    *,
    auth,
    base_url: str,
    query: str,
) -> Iterable[list[dict[str, Any]]]:
    """Yield pages from Zendesk's cursor-paginated Search Export endpoint.

    The regular /search.json endpoint is offset-paginated and fails with HTTP
    422 when page 11 is requested at 100 results/page. Search Export avoids
    that 1,000-result ceiling. The caller still keeps queries narrow by passing
    exactly one active status at a time before applying the UPS/DHL/FedEx
    requester-domain filter locally.
    """

    base_url = base_url.rstrip("/")
    url = _zendesk_search_export_url(base_url, query)
    page = 1

    while url:
        response = requests.get(url, auth=auth, timeout=60)
        response.raise_for_status()
        payload = response.json()

        tickets = payload.get("results", [])
        print(
            f"Found {len(tickets)} ticket(s) on Zendesk Search Export page {page} "
            f"for query: {query!r}."
        )
        yield tickets

        links = payload.get("links") or {}
        meta = payload.get("meta") or {}
        next_url = links.get("next")
        if _zendesk_meta_has_more(meta):
            if next_url:
                url = _absolute_zendesk_url(base_url, str(next_url))
            elif meta.get("after_cursor"):
                url = _zendesk_search_export_url(
                    base_url,
                    query,
                    after_cursor=str(meta["after_cursor"]),
                )
            else:
                raise RuntimeError(
                    "Zendesk Search Export indicated more results but did not "
                    "return links.next or meta.after_cursor."
                )
            page += 1
        else:
            url = None


def fetch_ticket_rows(
    *,
    client,
    bigquery,
    auth,
    base_url: str,
    email_to_category: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Fetch new/open/pending Zendesk tickets from UPS/DHL/FedEx domains.

    Exact requester emails found in the BigQuery config table keep their
    configured ticket_category. Carrier-domain requester emails not in config
    are classified from the current requester message content.
    """

    rows: list[dict[str, Any]] = []
    seen_ticket_ids: set[int] = set()
    user_email_cache: dict[int, str] = {}
    skipped_non_carrier = 0
    skipped_unexpected_status = 0

    print(
        "Fetching Zendesk mail tickets with API-level status filters: "
        + ", ".join(ACTIVE_ZENDESK_STATUSES)
    )
    print(
        "Requester domains are still filtered locally after Zendesk returns "
        "the status-filtered tickets: " + ", ".join(CARRIER_EMAIL_DOMAINS)
    )

    for zendesk_status in ACTIVE_ZENDESK_STATUSES:
        query = _active_zendesk_search_query(status=zendesk_status)
        print(f"Fetching Zendesk {zendesk_status!r} mail tickets with query: {query!r}")

        for tickets in iter_zendesk_search_export_tickets(
            auth=auth,
            base_url=base_url,
            query=query,
        ):
            for ticket in tickets:
                try:
                    ticket_id = int(ticket["id"])
                    if ticket_id in seen_ticket_ids:
                        continue
                    seen_ticket_ids.add(ticket_id)

                    ticket_status = str(ticket.get("status") or "").strip().lower()
                    if ticket_status not in ACTIVE_ZENDESK_STATUSES:
                        skipped_unexpected_status += 1
                        continue

                    requester_id = ticket.get("requester_id")
                    requester_email = requester_email_from_ticket(
                        ticket,
                        auth=auth,
                        base_url=base_url,
                        user_email_cache=user_email_cache,
                    )

                    if not email_matches_any_carrier_domain(requester_email):
                        skipped_non_carrier += 1
                        continue

                    configured_category = email_to_category.get(requester_email)
                    carrier = carrier_code_from_email(requester_email)

                    comments = get_ticket_comments(
                        ticket_id,
                        auth=auth,
                        base_url=base_url,
                    )

                    requester_comment_counter = 0
                    requester_requests = []

                    for idx, comment in enumerate(comments):
                        if comment.get("author_id") != requester_id:
                            continue

                        requester_comment_counter += 1
                        requester_requests.append(
                            {
                                "request_number": requester_comment_counter,
                                "body": comment.get("body", ""),
                                "comment_index": idx,
                                "request_submission_timestamp": comment.get("created_at"),
                            }
                        )

                    active_requests = []

                    for idx in range(len(comments) - 1, -1, -1):
                        comment = comments[idx]

                        # Ignore internal notes.
                        if not comment.get("public", True):
                            continue

                        if comment.get("author_id") == requester_id:
                            request = next(
                                (
                                    candidate
                                    for candidate in requester_requests
                                    if candidate["comment_index"] == idx
                                ),
                                None,
                            )

                            if request:
                                active_requests.append(request)
                        else:
                            # First public reply after the latest requester block.
                            break

                    active_requests.reverse()

                    if not active_requests:
                        continue

                    subject = ticket.get("subject", "")
                    description = ticket.get("description", "")

                    for request in active_requests:
                        request_body = request["body"]
                        ticket_category = configured_category or classify_ticket_category_from_content(
                            subject=subject,
                            request_body=request_body,
                            requester_email=requester_email,
                        )
                        tracking_number = extract_tracking_number(
                            subject,
                            f"{description}\n{request_body}",
                            carrier,
                        )

                        rows.append(
                            {
                                "ingestion_timestamp": datetime.now(timezone.utc),
                                "request_id": build_request_id(
                                    ticket_id,
                                    request["request_number"],
                                ),
                                "request_submission_timestamp": request.get(
                                    "request_submission_timestamp"
                                ),
                                "ticket_submission_timestamp": ticket.get("created_at"),
                                "zendesk_ticket_id": ticket_id,
                                "requester_email": requester_email,
                                "subject": subject,
                                "request_body": request_body,
                                "request_number": request["request_number"],
                                "ticket_category": ticket_category,
                                "extracted_tracking_number": tracking_number,
                            }
                        )

                except Exception as exc:
                    print(f"Error processing ticket {ticket.get('id')}: {str(exc)}")

    print(
        f"Fetched {len(rows)} active requester message(s) from carrier-domain tickets; "
        f"skipped {skipped_non_carrier} non-carrier ticket(s) and "
        f"{skipped_unexpected_status} ticket(s) with unexpected statuses."
    )
    return rows


def enrich_with_shipment_numbers(df: pd.DataFrame, client, bigquery) -> pd.DataFrame:
    tracking_numbers = [
        str(x).strip()
        for x in df["extracted_tracking_number"].dropna().unique()
        if str(x).strip() and str(x).strip().upper() != "N/A"
    ]

    shipment_map = {}

    if tracking_numbers:
        shipment_query = """
        WITH matched AS (

            SELECT DISTINCT
                shipment_order_number,
                tracking_number

            FROM `tlg-business-intelligence-prd.bi.shipping_platform_shipments`

            WHERE tracking_number IN UNNEST(@tracking_numbers)
        )

        SELECT
            matched.tracking_number AS source_tracking_number,
            matched.shipment_order_number,
            s.tracking_number,
            s.is_return,
            s.carrier_code

        FROM matched

        JOIN `tlg-business-intelligence-prd.bi.shipping_platform_shipments` s
          ON matched.shipment_order_number = s.shipment_order_number
        """

        shipment_df = (
            client.query(
                shipment_query,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ArrayQueryParameter(
                            "tracking_numbers",
                            "STRING",
                            tracking_numbers,
                        )
                    ]
                ),
            )
            .to_dataframe(create_bqstorage_client=BQ_STORAGE)
        )

        for tracking in shipment_df["source_tracking_number"].unique():
            subset = shipment_df[shipment_df["source_tracking_number"] == tracking]
            shipment_order_number = subset.iloc[0]["shipment_order_number"]

            shipment_tracking = None
            return_tracking = None
            shipment_carrier_code = None
            return_carrier_code = None

            for _, row in subset.iterrows():
                is_return = bool(row["is_return"])
                if is_return:
                    return_tracking = row["tracking_number"]
                    return_carrier_code = row.get("carrier_code")
                else:
                    shipment_tracking = row["tracking_number"]
                    shipment_carrier_code = row.get("carrier_code")

            shipment_map[tracking] = {
                "shipment_order_number": shipment_order_number,
                "shipment_tracking_number": shipment_tracking,
                "return_tracking_number": return_tracking,
                "shipment_carrier_code": shipment_carrier_code,
                "return_carrier_code": return_carrier_code,
            }

    df = df.copy()
    normalized_tracking = df["extracted_tracking_number"].map(
        lambda x: str(x).strip() if pd.notna(x) else ""
    )
    df["shipment_order_number"] = df["extracted_tracking_number"].map(
        lambda x: shipment_map.get(str(x).strip(), {}).get("shipment_order_number")
    )
    df["shipment_tracking_number"] = df["extracted_tracking_number"].map(
        lambda x: shipment_map.get(str(x).strip(), {}).get("shipment_tracking_number")
    )
    df["return_tracking_number"] = df["extracted_tracking_number"].map(
        lambda x: shipment_map.get(str(x).strip(), {}).get("return_tracking_number")
    )
    df["shipment_carrier_code"] = df["extracted_tracking_number"].map(
        lambda x: shipment_map.get(str(x).strip(), {}).get("shipment_carrier_code")
    )
    df["return_carrier_code"] = df["extracted_tracking_number"].map(
        lambda x: shipment_map.get(str(x).strip(), {}).get("return_carrier_code")
    )
    df["tracking_not_found_in_shipping_platform_shipments"] = normalized_tracking.map(
        lambda x: bool(x and x.upper() != "N/A" and x not in shipment_map)
    )

    return df


def fetch_tickets_to_active_table() -> int:
    client, bigquery = bigquery_client()
    email_to_category = load_requester_configuration(
        client,
        bigquery,
    )

    rows = fetch_ticket_rows(
        client=client,
        bigquery=bigquery,
        auth=zendesk_auth(),
        base_url=zendesk_base_url(),
        email_to_category=email_to_category,
    )

    if not rows:
        print("No records found. Clearing active ticket table.")
        replace_active_tickets(
            pd.DataFrame(columns=ACTIVE_TICKET_COLUMNS),
            client,
            bigquery,
        )
        return 0

    df = pd.DataFrame(rows)

    df["request_id"] = df.apply(
        lambda row: row.get("request_id")
        or build_request_id(
            row.get("zendesk_ticket_id"),
            row.get("request_number"),
        ),
        axis=1,
    )

    df.drop_duplicates(subset=["request_id"], inplace=True)

    existing_ids = existing_history_request_ids(
        df["request_id"].dropna().unique().tolist(),
        client,
        bigquery,
    )

    if existing_ids:
        before_count = len(df)
        df = df[~df["request_id"].isin(existing_ids)].copy()
        print(
            f"Skipped {before_count - len(df)} requests already present in "
            f"{HISTORY_TABLE}."
        )

    if df.empty:
        print("No new requests to process after history de-duplication.")
        replace_active_tickets(
            pd.DataFrame(columns=ACTIVE_TICKET_COLUMNS),
            client,
            bigquery,
        )
        return 0

    df = enrich_with_shipment_numbers(df, client, bigquery)

    print(f"Replacing active ticket table with {len(df)} new requests...")
    replace_active_tickets(df, client, bigquery)
    print("Process completed.")

    return len(df)


# ============================================================================
# CLI
# ============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch active Zendesk requests into BigQuery or append final "
            "request results to the BigQuery history table."
        )
    )
    parser.add_argument(
        "--append-history",
        action="store_true",
        help=(
            "Append output/request_intent_results.jsonl.gz to the BigQuery "
            "history table instead of fetching Zendesk tickets."
        ),
    )
    parser.add_argument(
        "--history-path",
        default=str(REQUEST_INTENT_RESULTS_PATH),
        help="Path to the request_intent_results JSONL.GZ handoff file.",
    )

    args = parser.parse_args()

    if args.append_history:
        logged_rows = append_history_from_file(args.history_path)
        print(f"Logged {logged_rows} new rows from {args.history_path}.")
        return

    fetch_tickets_to_active_table()


if __name__ == "__main__":
    main()

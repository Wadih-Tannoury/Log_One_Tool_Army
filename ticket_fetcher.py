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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    is_no_action_carrier_notification,
    is_noreply_requester_email,
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
ZENDESK_FETCH_CONFIGURED_REQUESTERS_ENV = "ZENDESK_FETCH_CONFIGURED_REQUESTERS"
ZENDESK_FETCH_CARRIER_DOMAIN_TERMS_ENV = "ZENDESK_FETCH_CARRIER_DOMAIN_TERMS"
ZENDESK_BROAD_ACTIVE_MAIL_FALLBACK_ENV = "ZENDESK_BROAD_ACTIVE_MAIL_FALLBACK"
ZENDESK_CONFIG_REQUESTER_QUERY_BATCH_SIZE_ENV = "ZENDESK_CONFIG_REQUESTER_QUERY_BATCH_SIZE"
DEFAULT_ZENDESK_CONFIG_REQUESTER_QUERY_BATCH_SIZE = 45
ZENDESK_FETCH_STRATEGY_ENV = "ZENDESK_FETCH_STRATEGY"
DEFAULT_ZENDESK_FETCH_STRATEGY = "broad_active"
ZENDESK_SEARCH_EXPORT_PAGE_SIZE_ENV = "ZENDESK_SEARCH_EXPORT_PAGE_SIZE"
DEFAULT_ZENDESK_SEARCH_EXPORT_PAGE_SIZE = 500
ZENDESK_USER_FETCH_BATCH_SIZE_ENV = "ZENDESK_USER_FETCH_BATCH_SIZE"
DEFAULT_ZENDESK_USER_FETCH_BATCH_SIZE = 100
ZENDESK_COMMENT_FETCH_WORKERS_ENV = "ZENDESK_COMMENT_FETCH_WORKERS"
DEFAULT_ZENDESK_COMMENT_FETCH_WORKERS = 20
ZENDESK_MAX_RETRIES_ENV = "ZENDESK_MAX_RETRIES"
DEFAULT_ZENDESK_MAX_RETRIES = 5

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


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = str(os.getenv(name, str(default)) or "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def zendesk_get(
    url: str,
    *,
    auth,
    session: requests.Session | None = None,
    params: Mapping[str, Any] | None = None,
    timeout: int = 60,
) -> requests.Response:
    """GET wrapper with retry handling for Zendesk rate limits/errors."""

    http = session or requests
    max_retries = _env_int(
        ZENDESK_MAX_RETRIES_ENV,
        DEFAULT_ZENDESK_MAX_RETRIES,
        minimum=1,
        maximum=10,
    )
    last_response: requests.Response | None = None

    for attempt in range(1, max_retries + 1):
        response = http.get(
            url,
            auth=auth,
            params=params,
            timeout=timeout,
        )
        last_response = response

        if response.status_code == 429 and attempt < max_retries:
            retry_after_raw = response.headers.get("Retry-After", "60")
            try:
                retry_after = int(float(retry_after_raw))
            except ValueError:
                retry_after = 60
            sleep_seconds = max(retry_after + 1, 1)
            print(
                "Rate limited by Zendesk. "
                f"Sleeping for {sleep_seconds} seconds before retrying."
            )
            time.sleep(sleep_seconds)
            continue

        if response.status_code in {500, 502, 503, 504} and attempt < max_retries:
            sleep_seconds = min(2 ** attempt, 30)
            print(
                f"Transient Zendesk error {response.status_code}. "
                f"Retrying in {sleep_seconds} seconds."
            )
            time.sleep(sleep_seconds)
            continue

        response.raise_for_status()
        return response

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError("Zendesk request failed without a response.")


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

    if status == "closed":
        print(
            f"{ZENDESK_STATUS_AFTER_REPLY_ENV}=closed would make the ticket "
            "non-editable/non-reopenable; using 'solved' instead."
        )
        status = "solved"

    valid_statuses = {"new", "open", "pending", "hold", "solved"}
    if status not in valid_statuses:
        raise ValueError(
            f"Unsupported {ZENDESK_STATUS_AFTER_REPLY_ENV}={status!r}; "
            "use one of: new, open, pending, hold, solved"
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


_IMAGE_LINK_LINE_RE = re.compile(r"\bimage\s*:", re.IGNORECASE)


def _strip_links_from_line(line: str) -> str:
    line = re.sub(
        r":\s*\[[^\]\n]+\]\([^\)\n]+\)",
        "",
        line,
        flags=re.IGNORECASE,
    )
    line = re.sub(
        r":\s*(?:https?://\S+|generated_documents/\S+|/[^\s:]+/generated_documents/\S+)",
        "",
        line,
        flags=re.IGNORECASE,
    )
    line = re.sub(
        r"\[[^\]\n]+\]\([^\)\n]+\)",
        "",
        line,
        flags=re.IGNORECASE,
    )
    line = re.sub(r"https?://\S+", "", line, flags=re.IGNORECASE)
    line = re.sub(
        r"(?:generated_documents/\S+|/[^\s:]+/generated_documents/\S+)",
        "",
        line,
        flags=re.IGNORECASE,
    )
    return line


def strip_links_from_public_response(value: Any) -> str:
    """Defense-in-depth guard: public Zendesk replies must not contain links.

    Returned-item image links (lines containing "Image:") are kept intact so
    the customer-facing reply still shows what was confirmed as returned.
    """

    sanitized = str(value or "")
    lines = sanitized.split("\n")
    processed_lines = [
        line if _IMAGE_LINK_LINE_RE.search(line) else _strip_links_from_line(line)
        for line in lines
    ]
    lines = [re.sub(r"[ \t]+$", "", line) for line in processed_lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


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



_ZENDESK_FIELD_CACHE={}
def _zendesk_ticket_field(field_id:int,auth,base_url):
    if field_id in _ZENDESK_FIELD_CACHE:
        return _ZENDESK_FIELD_CACHE[field_id]
    r=requests.get(f"{base_url}/ticket_fields/{field_id}.json",auth=auth,timeout=60)
    r.raise_for_status()
    fld=r.json()["ticket_field"]
    _ZENDESK_FIELD_CACHE[field_id]=fld
    return fld

def _zendesk_field_value(field_id:int,display_name:str,auth,base_url):
    fld=_zendesk_ticket_field(field_id,auth,base_url)
    for opt in fld.get("custom_field_options",[]):
        if str(opt.get("name","")).strip().lower()==str(display_name).strip().lower():
            return opt.get("value")
    return fld.get("custom_field_options",[{}])[0].get("value")
def _brand_from_country_tag(tag: str) -> str:
    """Extract the brand code from a country tag like 'country_dj_us' -> 'DJ'."""
    # Tags are always: country_{brand}_{iso2}
    parts = str(tag or "").split("_")
    # parts[0] = "country", parts[1] = brand, parts[2] = iso2
    if len(parts) >= 3:
        return parts[1].upper()
    return ""


def submit_ticket_response(
    ticket_id: int,
    body: str,
    *,
    attachment_paths: Iterable[str] = (),
    reason_of_contact: str = 'Altro',
    order_note: str = '-',
    country_tag: str | None = None,
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
    channel_value=_zendesk_field_value(360000381300,"online",auth,base_url)
    reason_value=_zendesk_field_value(23910471,reason_of_contact,auth,base_url)
    custom_fields: list[dict[str, Any]] = [
        {"id": 360000381300, "value": channel_value},
        {"id": 23910471, "value": reason_value},
        {"id": 36951465, "value": order_note or "-"},
    ]

    # Populate the per-brand "Country <BRAND>" field when the country tag was
    # resolved from the GET_FULL_ORDER shippingAddress (e.g. "country_dj_us").
    # The tag encodes both the brand and the ISO country code so we can derive
    # the Zendesk field id from the brand prefix without an extra API call.
    if country_tag:
        brand_code = _brand_from_country_tag(country_tag)
        from customs_rules import country_ticket_field_id_for_brand
        field_id = country_ticket_field_id_for_brand(brand_code)
        if field_id:
            custom_fields.append({"id": field_id, "value": country_tag})

    ticket_update["custom_fields"] = custom_fields

    response = requests.put(
        f"{base_url}/tickets/{int(ticket_id)}.json",
        auth=auth,
        json={"ticket": ticket_update},
        timeout=60,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        error_detail = _zendesk_error_detail(response)

        # Zendesk rejects ANY update (comment or status) to a ticket whose
        # current status is already "closed" -- this is unrelated to the
        # status we're trying to set, so retrying with a different status
        # value will not help. Surface this as a clear, specific failure
        # instead of a bare "422 Client Error".
        if response.status_code == 422 and _is_zendesk_closed_ticket_error(error_detail):
            raise ZendeskTicketClosedError(
                f"Zendesk ticket {ticket_id} is closed and cannot be updated "
                f"via the API. Skipping this ticket. Zendesk said: {error_detail}"
            ) from exc

        raise requests.HTTPError(
            f"{exc} | Zendesk ticket {ticket_id} update failed. "
            f"Zendesk error detail: {error_detail}",
            response=response,
        ) from exc
    return True


class ZendeskTicketClosedError(RuntimeError):
    """Raised when Zendesk refuses an update because the ticket is closed."""


def _zendesk_error_detail(response: requests.Response) -> str:
    """Best-effort extraction of Zendesk's JSON error body for diagnostics."""

    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:500]
    return json.dumps(payload)[:1000]


def _is_zendesk_closed_ticket_error(error_detail: str) -> bool:
    """Detect Zendesk's "ticket is closed" validation error from its body."""

    lowered = error_detail.lower()
    return "closed" in lowered and (
        "ticketblockederror" in lowered
        or "prevents ticket update" in lowered
        or "not valid for ticket update" in lowered
    )


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
        if is_noreply_requester_email(row.get("requester_email")):
            continue
        if bool(_to_bool(row.get("human_intervention_required"))):
            continue
        request_text = "\n".join(
            str(row.get(column) or "")
            for column in ("subject", "cleaned_request_body", "request_body")
        )
        if is_no_action_carrier_notification(request_text):
            continue
        final_response = strip_links_from_public_response(row.get("final_response"))
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
    failed: list[tuple[int, str]] = []

    for ticket_id, final_response, row in response_rows:
        attachment_paths = _attachment_paths(row.get("zendesk_attachment_paths"))
        try:
            if submit_ticket_response(
                ticket_id,
                final_response,
                attachment_paths=attachment_paths,
                reason_of_contact=str(row.get('reason_of_contact') or row.get('contact_reason') or row.get('zendesk_reason_of_contact') or 'Altro'),
                order_note=str(row.get('shipment_order_number') or row.get('order_number') or '-'),
                country_tag=row.get('zendesk_country_tag') or None,
                auth=auth,
                base_url=base_url,
            ):
                submitted += 1
        except ZendeskTicketClosedError as exc:
            # Already-closed tickets can never be fixed by retrying this
            # same request, so log it and keep going with the rest of the
            # batch instead of aborting every remaining submission.
            print(f"Skipped Zendesk ticket {ticket_id}: {exc}")
            failed.append((ticket_id, str(exc)))
        except Exception as exc:  # noqa: BLE001 - intentionally broad: one
            # bad ticket (network blip, validation error, etc.) must not
            # prevent the rest of the batch from being submitted.
            print(f"Failed to submit Zendesk ticket {ticket_id}: {exc}")
            failed.append((ticket_id, str(exc)))

    print(f"Submitted {submitted} Zendesk final response(s).")

    if failed:
        failed_ids = ", ".join(str(ticket_id) for ticket_id, _ in failed)
        print(
            f"WARNING: {len(failed)} Zendesk response(s) failed to submit "
            f"(ticket id(s): {failed_ids}). See messages above for details."
        )
        raise RuntimeError(
            f"{len(failed)} of {len(response_rows)} Zendesk response(s) failed "
            f"to submit after submitting {submitted} successfully. "
            f"Failed ticket id(s): {failed_ids}."
        )

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


def get_ticket_comments(
    ticket_id,
    *,
    auth,
    base_url: str,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Retrieve all comments for a Zendesk ticket."""

    comments: list[dict[str, Any]] = []
    url = f"{base_url}/tickets/{ticket_id}/comments.json"

    while url:
        response = zendesk_get(url, auth=auth, session=session, timeout=60)

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
    session: requests.Session | None = None,
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

    response = zendesk_get(
        f"{base_url}/users/{user_id_int}.json",
        auth=auth,
        session=session,
        timeout=60,
    )
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
    session: requests.Session | None = None,
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
        session=session,
    )
    if requester_email:
        return requester_email

    return via_email


def _zendesk_user_fetch_batch_size() -> int:
    return _env_int(
        ZENDESK_USER_FETCH_BATCH_SIZE_ENV,
        DEFAULT_ZENDESK_USER_FETCH_BATCH_SIZE,
        minimum=1,
        maximum=100,
    )


def get_zendesk_users_by_ids(
    user_ids: Iterable[object],
    *,
    auth,
    base_url: str,
    cache: dict[int, str],
    session: requests.Session | None = None,
) -> dict[int, str]:
    """Batch-load Zendesk requester emails into cache using users/show_many."""

    clean_user_ids: list[int] = []
    for user_id in user_ids:
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            continue
        if user_id_int not in cache and user_id_int not in clean_user_ids:
            clean_user_ids.append(user_id_int)

    if not clean_user_ids:
        return cache

    base_url = base_url.rstrip("/")
    batch_size = _zendesk_user_fetch_batch_size()

    for batch in _chunked(clean_user_ids, batch_size):
        response = zendesk_get(
            f"{base_url}/users/show_many.json",
            auth=auth,
            session=session,
            params={"ids": ",".join(str(user_id) for user_id in batch)},
            timeout=60,
        )
        payload = response.json()
        returned_ids: set[int] = set()

        for user in payload.get("users", []):
            try:
                user_id_int = int(user.get("id"))
            except (TypeError, ValueError):
                continue
            returned_ids.add(user_id_int)
            cache[user_id_int] = normalize_email(user.get("email"))

        for missing_user_id in set(batch) - returned_ids:
            cache.setdefault(missing_user_id, "")

    return cache


def _ticket_via_source_email(ticket: Mapping[str, Any]) -> str:
    return normalize_email(
        ticket.get("via", {})
        .get("source", {})
        .get("from", {})
        .get("address", "")
    )


def requester_email_from_ticket_cache(
    ticket: Mapping[str, Any],
    *,
    user_email_cache: Mapping[int, str],
) -> str:
    """Return requester email from via.source first, then batched user cache."""

    via_email = _ticket_via_source_email(ticket)
    if email_matches_any_carrier_domain(via_email):
        return via_email

    try:
        requester_id = int(ticket.get("requester_id"))
    except (TypeError, ValueError):
        requester_id = None

    requester_email = user_email_cache.get(requester_id, "") if requester_id else ""
    return requester_email or via_email


def _zendesk_comment_fetch_workers(ticket_count: int) -> int:
    if ticket_count <= 0:
        return 0
    configured = _env_int(
        ZENDESK_COMMENT_FETCH_WORKERS_ENV,
        DEFAULT_ZENDESK_COMMENT_FETCH_WORKERS,
        minimum=1,
        maximum=50,
    )
    return min(configured, ticket_count)


def get_comments_for_ticket(
    ticket: Mapping[str, Any],
    *,
    auth,
    base_url: str,
) -> dict[str, Any]:
    """Fetch comments for one ticket; safe for ThreadPoolExecutor workers."""

    ticket_id = ticket.get("id")
    try:
        with requests.Session() as session:
            comments = get_ticket_comments(
                ticket_id,
                auth=auth,
                base_url=base_url,
                session=session,
            )
        return {"ticket_id": int(ticket_id), "comments": comments, "error": None}
    except Exception as exc:
        return {"ticket_id": ticket_id, "comments": [], "error": str(exc)}


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


def _dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    """Return non-empty strings once, preserving first-seen order."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _chunked(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _configured_carrier_requester_emails(
    email_to_category: Mapping[str, str],
) -> list[str]:
    """Return configured requester emails that belong to the carrier domains."""

    return _dedupe_preserve_order(
        normalize_email(email)
        for email in email_to_category.keys()
        if email_matches_any_carrier_domain(normalize_email(email))
    )


def _zendesk_config_requester_query_batch_size() -> int:
    raw = str(
        os.getenv(
            ZENDESK_CONFIG_REQUESTER_QUERY_BATCH_SIZE_ENV,
            str(DEFAULT_ZENDESK_CONFIG_REQUESTER_QUERY_BATCH_SIZE),
        )
        or ""
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ZENDESK_CONFIG_REQUESTER_QUERY_BATCH_SIZE_ENV} must be an integer"
        ) from exc

    # Keep the generated query comfortably below Zendesk's documented
    # 64-word search-query limit, while still batching the 64 known requesters
    # into only a couple of API calls.
    return min(max(value, 1), 50)


def _active_zendesk_search_query(
    *,
    status: str | None = None,
    statuses: Iterable[str] | None = None,
    requester_emails: Iterable[str] = (),
    domain_term: str | None = None,
    include_type_term: bool = False,
) -> str:
    """Build a narrow Zendesk Search Export query for active mail tickets.

    By default the query includes all workflow-active statuses at API level:
    ``status:new status:open status:pending``. Zendesk treats repeated values
    for the same property as OR matches, so this fetches only those three
    statuses and does not fetch solved/closed tickets for local filtering.

    The main fetch path uses Zendesk's cursor-based Search Export endpoint.
    That endpoint requires the object type in the separate filter[type]
    parameter and rejects type:ticket inside the query string, so the default
    query excludes the type term. include_type_term exists only for backward-
    compatible tests/debugging against the regular search endpoint.
    """

    if status is not None and statuses is not None:
        raise ValueError("Pass either status or statuses, not both")

    raw_statuses = [status] if status is not None else list(statuses or ACTIVE_ZENDESK_STATUSES)
    normalized_statuses: list[str] = []
    for raw_status in raw_statuses:
        normalized_status = str(raw_status or "").strip().lower()
        if normalized_status not in ACTIVE_ZENDESK_STATUSES:
            raise ValueError(
                f"Unsupported Zendesk active status {raw_status!r}; use one of: "
                + ", ".join(ACTIVE_ZENDESK_STATUSES)
            )
        if normalized_status not in normalized_statuses:
            normalized_statuses.append(normalized_status)

    search_terms = []
    if include_type_term:
        search_terms.append("type:ticket")

    search_terms.extend(f"status:{value}" for value in normalized_statuses)
    search_terms.append("via:mail")

    requester_terms = _dedupe_preserve_order(
        normalize_email(email) for email in requester_emails
    )
    search_terms.extend(f"requester:{email}" for email in requester_terms)

    normalized_domain = str(domain_term or "").strip().lower().lstrip("@")
    if normalized_domain:
        search_terms.append(normalized_domain)

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


def _zendesk_fetch_query_specs(
    email_to_category: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Build Zendesk Search Export queries for this run.

    Default strategy is one broad active-mail query:
    ``status:new status:open status:pending via:mail``. It is normally faster
    than several domain/full-text searches because requester emails are loaded
    in batched ``users/show_many`` calls and comments are fetched only for the
    matching carrier-domain tickets.

    Set ``ZENDESK_FETCH_STRATEGY=narrow`` only for diagnostics if you need the
    older configured-requester/domain-term search behavior.
    """

    strategy = str(
        os.getenv(ZENDESK_FETCH_STRATEGY_ENV, DEFAULT_ZENDESK_FETCH_STRATEGY)
        or DEFAULT_ZENDESK_FETCH_STRATEGY
    ).strip().lower()

    if strategy in {"", "broad", "broad_active", "single", "single_active"}:
        return [
            (
                "active mail tickets",
                _active_zendesk_search_query(),
            )
        ]

    if strategy not in {"narrow", "domain", "domain_terms"}:
        raise ValueError(
            f"Unsupported {ZENDESK_FETCH_STRATEGY_ENV}={strategy!r}; "
            "use broad_active or narrow"
        )

    specs: list[tuple[str, str]] = []

    if _env_bool(ZENDESK_FETCH_CONFIGURED_REQUESTERS_ENV, True):
        configured_emails = _configured_carrier_requester_emails(email_to_category)
        batches = list(
            _chunked(
                configured_emails,
                _zendesk_config_requester_query_batch_size(),
            )
        )
        for index, batch in enumerate(batches, start=1):
            label = f"configured carrier requesters batch {index}/{len(batches)}"
            specs.append(
                (
                    label,
                    _active_zendesk_search_query(requester_emails=batch),
                )
            )

    if _env_bool(ZENDESK_FETCH_CARRIER_DOMAIN_TERMS_ENV, True):
        for domain in CARRIER_EMAIL_DOMAINS:
            specs.append(
                (
                    f"carrier domain term {domain}",
                    _active_zendesk_search_query(domain_term=domain),
                )
            )

    if _env_bool(ZENDESK_BROAD_ACTIVE_MAIL_FALLBACK_ENV, False):
        specs.append(
            (
                "optional broad active-mail fallback",
                _active_zendesk_search_query(),
            )
        )

    deduped_specs: list[tuple[str, str]] = []
    seen_queries: set[str] = set()
    for label, query in specs:
        if query in seen_queries:
            continue
        seen_queries.add(query)
        deduped_specs.append((label, query))

    if not deduped_specs:
        raise RuntimeError(
            "No Zendesk fetch queries were generated. Enable at least one of "
            f"{ZENDESK_FETCH_CONFIGURED_REQUESTERS_ENV}, "
            f"{ZENDESK_FETCH_CARRIER_DOMAIN_TERMS_ENV}, or "
            f"{ZENDESK_BROAD_ACTIVE_MAIL_FALLBACK_ENV}."
        )

    return deduped_specs


def _zendesk_search_export_url(
    base_url: str,
    query: str,
    *,
    after_cursor: str | None = None,
) -> str:
    """Return a cursor-paginated Search Export URL for tickets."""

    page_size = _env_int(
        ZENDESK_SEARCH_EXPORT_PAGE_SIZE_ENV,
        DEFAULT_ZENDESK_SEARCH_EXPORT_PAGE_SIZE,
        minimum=1,
        maximum=1000,
    )

    # Zendesk allows up to 1000 records per page. Active-ticket searches are not
    # archived-ticket exports, so the workflow defaults to 500 to reduce page
    # round trips while keeping an env override available.

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
    label: str | None = None,
    session: requests.Session | None = None,
) -> Iterable[list[dict[str, Any]]]:
    """Yield pages from Zendesk's cursor-paginated Search Export endpoint.

    The regular /search.json endpoint is offset-paginated and fails with HTTP
    422 when page 11 is requested at 100 results/page. Search Export avoids
    that 1,000-result ceiling. The caller now keeps queries narrow before
    local validation by including active statuses and carrier/requester terms
    in the Zendesk query itself.
    """

    base_url = base_url.rstrip("/")
    url = _zendesk_search_export_url(base_url, query)
    page = 1
    query_label = label or query

    while url:
        response = zendesk_get(url, auth=auth, session=session, timeout=60)
        payload = response.json()

        tickets = payload.get("results", [])
        print(
            f"Found {len(tickets)} ticket(s) on Zendesk Search Export page {page} "
            f"for {query_label!r}."
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

    The slow network operations are deliberately batched/parallelized:
    requester users are loaded with users/show_many, and comments are fetched
    concurrently only for tickets whose requester email matches a carrier
    domain. Exact requester emails found in BigQuery keep their configured
    ticket_category; unconfigured carrier-domain requesters are classified from
    the current requester message.
    """

    rows: list[dict[str, Any]] = []
    seen_ticket_ids: set[int] = set()
    user_email_cache: dict[int, str] = {}
    skipped_non_carrier = 0
    skipped_unexpected_status = 0
    comment_error_count = 0

    query_specs = _zendesk_fetch_query_specs(email_to_category)

    print(
        "Fetching Zendesk mail tickets with API-level active-status filters: "
        + ", ".join(ACTIVE_ZENDESK_STATUSES)
    )
    print(
        "Default fetch strategy uses a single active-mail Zendesk query, then "
        "batch-loads requester users and filters carrier domains locally: "
        + ", ".join(CARRIER_EMAIL_DOMAINS)
    )
    print(
        f"Prepared {len(query_specs)} Zendesk Search Export query/queries "
        f"using {ZENDESK_FETCH_STRATEGY_ENV}="
        f"{os.getenv(ZENDESK_FETCH_STRATEGY_ENV, DEFAULT_ZENDESK_FETCH_STRATEGY)}."
    )

    with requests.Session() as session:
        for query_label, query in query_specs:
            print(f"Fetching {query_label} with query: {query!r}")

            for tickets in iter_zendesk_search_export_tickets(
                auth=auth,
                base_url=base_url,
                query=query,
                label=query_label,
                session=session,
            ):
                candidate_tickets: list[dict[str, Any]] = []

                for ticket in tickets:
                    try:
                        ticket_id = int(ticket["id"])
                    except Exception:
                        print(f"Skipped Zendesk result without numeric id: {ticket!r}")
                        continue

                    if ticket_id in seen_ticket_ids:
                        continue
                    seen_ticket_ids.add(ticket_id)

                    ticket_status = str(ticket.get("status") or "").strip().lower()
                    if ticket_status not in ACTIVE_ZENDESK_STATUSES:
                        skipped_unexpected_status += 1
                        continue

                    candidate_tickets.append(ticket)

                if not candidate_tickets:
                    continue

                # Zendesk search results normally have requester_id but not a
                # normalized requester email. Batch-load only the user records
                # needed for tickets whose via.source email is not already a
                # carrier-domain address.
                user_ids_to_load = []
                for ticket in candidate_tickets:
                    if email_matches_any_carrier_domain(_ticket_via_source_email(ticket)):
                        continue
                    requester_id = ticket.get("requester_id")
                    try:
                        requester_id_int = int(requester_id)
                    except (TypeError, ValueError):
                        continue
                    if requester_id_int not in user_email_cache:
                        user_ids_to_load.append(requester_id_int)

                get_zendesk_users_by_ids(
                    user_ids_to_load,
                    auth=auth,
                    base_url=base_url,
                    cache=user_email_cache,
                    session=session,
                )

                matching_tickets: list[dict[str, Any]] = []
                requester_email_by_ticket_id: dict[int, str] = {}

                for ticket in candidate_tickets:
                    ticket_id = int(ticket["id"])
                    requester_email = requester_email_from_ticket_cache(
                        ticket,
                        user_email_cache=user_email_cache,
                    )
                    if not email_matches_any_carrier_domain(requester_email):
                        skipped_non_carrier += 1
                        continue

                    matching_tickets.append(ticket)
                    requester_email_by_ticket_id[ticket_id] = requester_email

                print(
                    f"Matched {len(matching_tickets)} carrier-domain ticket(s) "
                    f"from {len(candidate_tickets)} active candidate ticket(s) "
                    "on this page."
                )

                if not matching_tickets:
                    continue

                ticket_comment_map: dict[int, list[dict[str, Any]]] = {}
                workers = _zendesk_comment_fetch_workers(len(matching_tickets))

                if workers <= 1:
                    comment_results = [
                        get_comments_for_ticket(
                            ticket,
                            auth=auth,
                            base_url=base_url,
                        )
                        for ticket in matching_tickets
                    ]
                else:
                    comment_results = []
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = [
                            executor.submit(
                                get_comments_for_ticket,
                                ticket,
                                auth=auth,
                                base_url=base_url,
                            )
                            for ticket in matching_tickets
                        ]
                        for future in as_completed(futures):
                            comment_results.append(future.result())

                for result in comment_results:
                    if result.get("error"):
                        comment_error_count += 1
                        print(
                            f"Error fetching comments for ticket "
                            f"{result.get('ticket_id')}: {result.get('error')}"
                        )
                        continue
                    try:
                        result_ticket_id = int(result["ticket_id"])
                    except (TypeError, ValueError):
                        continue
                    ticket_comment_map[result_ticket_id] = result.get("comments", [])

                for ticket in matching_tickets:
                    try:
                        ticket_id = int(ticket["id"])
                        requester_id = ticket.get("requester_id")
                        requester_email = requester_email_by_ticket_id.get(ticket_id, "")
                        configured_category = email_to_category.get(requester_email)
                        carrier = carrier_code_from_email(requester_email)
                        comments = ticket_comment_map.get(ticket_id, [])
                        if not comments:
                            continue

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
        f"skipped {skipped_non_carrier} non-carrier ticket(s), "
        f"{skipped_unexpected_status} ticket(s) with unexpected statuses, and "
        f"{comment_error_count} ticket(s) with comment-fetch errors."
    )
    return rows


def enrich_with_shipment_numbers(df: pd.DataFrame, client, bigquery) -> pd.DataFrame:
    tracking_numbers = []

    for value in df["extracted_tracking_number"].dropna():
        for tracking in re.split(r"[;,]", str(value)):
            tracking = tracking.strip()
            if tracking and tracking.upper() != "N/A":
                tracking_numbers.append(tracking)

    tracking_numbers = list(dict.fromkeys(tracking_numbers))

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
    def enrich_tracking_field(value, field):
    results = []

    for tracking in re.split(r"[;,]", str(value or "")):
        tracking = tracking.strip()
        if not tracking:
            continue

        mapped = shipment_map.get(tracking, {}).get(field)
        if mapped and mapped not in results:
            results.append(mapped)

    return ";".join(results) if results else None


    df["shipment_order_number"] = df["extracted_tracking_number"].apply(
        lambda x: enrich_tracking_field(x, "shipment_order_number")
    )

    df["shipment_tracking_number"] = df["extracted_tracking_number"].apply(
        lambda x: enrich_tracking_field(x, "shipment_tracking_number")
    )

    df["return_tracking_number"] = df["extracted_tracking_number"].apply(
        lambda x: enrich_tracking_field(x, "return_tracking_number")
    )

    df["shipment_carrier_code"] = df["extracted_tracking_number"].apply(
        lambda x: enrich_tracking_field(x, "shipment_carrier_code")
    )

    df["return_carrier_code"] = df["extracted_tracking_number"].apply(
        lambda x: enrich_tracking_field(x, "return_carrier_code")
    )

    df["tracking_not_found_in_shipping_platform_shipments"] = df[
        "extracted_tracking_number"
    ].apply(
        lambda value: any(
            tracking.strip()
            and tracking.strip().upper() != "N/A"
            and tracking.strip() not in shipment_map
            for tracking in re.split(r"[;,]", str(value or ""))
        )
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

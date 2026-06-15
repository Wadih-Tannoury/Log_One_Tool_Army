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
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import pandas as pd
import requests
from requests.auth import HTTPBasicAuth

from pipeline_io import (
    REQUEST_INTENT_RESULTS_PATH,
    build_request_id,
    get_workflow_run_id,
    read_dataframe,
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
    "requested_data",
    "confidence",
    "llm_confidence",
    "notes",
    "human_intervention_required",
    "regex_request_types",
    "regex_requested_data",
    "matched_spans",
    "request_language",
    "language_confidence",
    "language_notes",
    "llm_was_used",
    "draft_response",
]

VALID_CATEGORIES = [
    "Order Customs Clearance",
    "Pending Order Release",
    "Returns Customs Clearance",
]

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
            return_tracking_number STRING
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
        bigquery.SchemaField("requested_data", "STRING", mode="REPEATED"),
        bigquery.SchemaField("confidence", "FLOAT64"),
        bigquery.SchemaField("llm_confidence", "FLOAT64"),
        bigquery.SchemaField("notes", "STRING"),
        bigquery.SchemaField("human_intervention_required", "BOOL"),
        bigquery.SchemaField("regex_request_types", "STRING", mode="REPEATED"),
        bigquery.SchemaField("regex_requested_data", "STRING", mode="REPEATED"),
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
        bigquery.SchemaField("draft_response", "STRING"),
    ]


def _target_history_schema(client):
    """
    Use the live destination schema for history appends.

    This keeps logging independent of the physical column order in BigQuery and
    allows future nullable columns to exist in the table even when this pipeline
    does not populate them yet.
    """

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
    llm_confidence = _to_float(row.get("llm_confidence"))

    # Backward-compatible fallback for older local handoff files that only
    # contain the generic confidence column.
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
        "requested_data": _string_list(row.get("requested_data")),
        "confidence": _to_float(row.get("confidence")),
        "llm_confidence": llm_confidence,
        "notes": _to_str(row.get("notes")),
        "human_intervention_required": bool(
            _to_bool(row.get("human_intervention_required"))
        ),
        "regex_request_types": _string_list(row.get("regex_request_types")),
        "regex_requested_data": _string_list(row.get("regex_requested_data")),
        "matched_spans": _matched_spans(row.get("matched_spans")),
        "request_language": _to_str(row.get("request_language")),
        "language_confidence": _to_float(row.get("language_confidence")),
        "language_notes": _to_str(row.get("language_notes")),
        "llm_was_used": llm_was_used,
        "draft_response": _to_str(row.get("draft_response")),
    }

    return {column: prepared.get(column) for column in HISTORY_COLUMNS}


def append_history_rows(rows: Iterable[Mapping[str, Any]] | pd.DataFrame) -> int:
    """Append final workflow rows to the BigQuery history table."""

    if isinstance(rows, pd.DataFrame):
        raw_rows = rows.to_dict(orient="records")
    else:
        raw_rows = list(rows)

    if not raw_rows:
        print("No final rows to log to BigQuery history.")
        return 0

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
        return 0

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
        return 0

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

    return len(rows_to_insert)


def append_history_from_file(path=REQUEST_INTENT_RESULTS_PATH) -> int:
    """Convenience CLI helper to append output/request_intent_results.jsonl.gz."""

    df = read_dataframe(path)
    return append_history_rows(df)


# ============================================================================
# ZENDESK FETCH HELPERS
# ============================================================================


def load_requester_configuration(client, bigquery) -> tuple[dict[str, str], list[str]]:
    print("Loading configuration table...")

    config_query = f"""
    SELECT DISTINCT
        requester_email,
        ticket_category
    FROM `{CONFIG_TABLE}`
    WHERE ticket_category IN UNNEST(@categories)
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

    email_to_category = dict(
        zip(
            config_df["requester_email"],
            config_df["ticket_category"],
        )
    )

    requester_emails = list(email_to_category.keys())
    print(f"Loaded {len(requester_emails)} requester emails.")

    return email_to_category, requester_emails


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


def extract_tracking_number(subject, description, carrier_code):
    subject = subject or ""
    description = description or ""

    fedex_express_pattern = r"\b\d{12}\b"
    fedex_ground_pattern = r"\b\d{15}\b"
    fedex_smartpost_pattern = r"\b\d{20}\b|\b\d{22}\b"

    ups_pattern = r"\b1Z[\da-z]{16}\b"
    ups_alt_pattern = r"W\d{10}"

    dhl_pattern = r"\b\d{10}\b"

    def matches(pattern):
        return (
            re.findall(pattern, subject, flags=re.IGNORECASE)
            or re.findall(pattern, description, flags=re.IGNORECASE)
            or []
        )

    if carrier_code == "FEDEX":
        values = (
            matches(fedex_express_pattern)
            + matches(fedex_ground_pattern)
            + matches(fedex_smartpost_pattern)
        )
        return next(iter(dict.fromkeys(values)), "N/A")

    if carrier_code == "UPS":
        values = matches(ups_pattern) + matches(ups_alt_pattern)
        return next(iter(dict.fromkeys(values)), "N/A")

    if carrier_code == "DHL":
        values = matches(dhl_pattern)
        return next(iter(dict.fromkeys(values)), "N/A")

    return "N/A"


def fetch_ticket_rows(
    *,
    client,
    bigquery,
    auth,
    base_url: str,
    email_to_category: Mapping[str, str],
    requester_emails: Iterable[str],
) -> list[dict[str, Any]]:
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=90)

    start_date_str = start_date.strftime("%Y-%m-%d")
    end_date_str = end_date.strftime("%Y-%m-%d")

    rows: list[dict[str, Any]] = []

    for requester_email in requester_emails:
        print(f"Processing requester: {requester_email}")
        page = 1

        while True:
            search_query = (
                f"type:ticket "
                f"-status:closed "
                f"-status:solved "
                f"via:mail "
                f"requester:{requester_email} "
                f"created>={start_date_str} "
                f"created<={end_date_str}"
            )

            url = f"{base_url}/search.json?query={quote(search_query)}&page={page}"

            response = requests.get(url, auth=auth, timeout=60)
            response.raise_for_status()

            payload = response.json()
            tickets = payload.get("results", [])

            if not tickets:
                break

            print(f"Found {len(tickets)} tickets on page {page}")

            for ticket in tickets:
                try:
                    ticket_id = ticket["id"]
                    requester_id = ticket.get("requester_id")

                    comments = get_ticket_comments(
                        ticket_id,
                        auth=auth,
                        base_url=base_url,
                    )

                    requester_comment_counter = 0
                    requester_requests = []

                    for idx, comment in enumerate(comments):
                        if comment["author_id"] != requester_id:
                            continue

                        requester_comment_counter += 1
                        requester_requests.append(
                            {
                                "request_number": requester_comment_counter,
                                "body": comment.get("body", ""),
                                "comment_index": idx,
                                "request_submission_timestamp": comment.get(
                                    "created_at"
                                ),
                            }
                        )

                    active_requests = []

                    for idx in range(len(comments) - 1, -1, -1):
                        comment = comments[idx]

                        # Ignore internal notes.
                        if not comment.get("public", True):
                            continue

                        if comment["author_id"] == requester_id:
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

                    sender_email = (
                        ticket.get("via", {})
                        .get("source", {})
                        .get("from", {})
                        .get("address", "")
                    )

                    carrier = ""
                    if "@" in sender_email:
                        carrier = sender_email.split("@")[1].split(".")[0].upper()

                    tracking_number = extract_tracking_number(
                        ticket.get("subject", ""),
                        ticket.get("description", ""),
                        carrier,
                    )

                    for request in active_requests:
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
                                "ticket_submission_timestamp": ticket.get(
                                    "created_at"
                                ),
                                "zendesk_ticket_id": ticket_id,
                                "requester_email": requester_email,
                                "subject": ticket.get("subject", ""),
                                "request_body": request["body"],
                                "request_number": request["request_number"],
                                "ticket_category": email_to_category[requester_email],
                                "extracted_tracking_number": tracking_number,
                            }
                        )

                except Exception as exc:
                    print(
                        f"Error processing ticket {ticket.get('id')}: {str(exc)}"
                    )

            if not payload.get("next_page"):
                break

            page += 1

    return rows


def enrich_with_shipment_numbers(df: pd.DataFrame, client, bigquery) -> pd.DataFrame:
    tracking_numbers = [
        x for x in df["extracted_tracking_number"].dropna().unique() if x != "N/A"
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
            s.is_return

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

            for _, row in subset.iterrows():
                if row["is_return"]:
                    return_tracking = row["tracking_number"]
                else:
                    shipment_tracking = row["tracking_number"]

            shipment_map[tracking] = {
                "shipment_order_number": shipment_order_number,
                "shipment_tracking_number": shipment_tracking,
                "return_tracking_number": return_tracking,
            }

    df = df.copy()
    df["shipment_order_number"] = df["extracted_tracking_number"].map(
        lambda x: shipment_map.get(x, {}).get("shipment_order_number")
    )
    df["shipment_tracking_number"] = df["extracted_tracking_number"].map(
        lambda x: shipment_map.get(x, {}).get("shipment_tracking_number")
    )
    df["return_tracking_number"] = df["extracted_tracking_number"].map(
        lambda x: shipment_map.get(x, {}).get("return_tracking_number")
    )

    return df


def fetch_tickets_to_active_table() -> int:
    client, bigquery = bigquery_client()
    email_to_category, requester_emails = load_requester_configuration(
        client,
        bigquery,
    )

    rows = fetch_ticket_rows(
        client=client,
        bigquery=bigquery,
        auth=zendesk_auth(),
        base_url=zendesk_base_url(),
        email_to_category=email_to_category,
        requester_emails=requester_emails,
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

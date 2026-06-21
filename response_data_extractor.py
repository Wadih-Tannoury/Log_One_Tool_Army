"""
response_data_extractor.py

GET_FULL_ORDER API enrichment and PDF document generation for draft responses.

This module is intentionally the single place where response_generator.py gets
API-backed response data and generated customs documents:

- GET_FULL_ORDER client authentication and payload fetching;
- shipment-specific extraction from GET_FULL_ORDER responses;
- LOA / authorization-letter PDF generation;
- POA / power-of-attorney PDF generation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import fitz
import requests
from requests.auth import HTTPBasicAuth

from customs_rules import (
    PLACEHOLDER,
    UPS_BROKERAGE_EMAIL,
    extract_ups_code,
    first_available_value,
    normalize_email,
    normalize_requested_data,
)
from pipeline_io import REQUEST_INTENT_RESULTS_PATH, read_dataframe, write_dataframe

DEFAULT_BASE_URL = "https://zelda.thelevelgroup.com/return/api/v1"
CREDENTIALS_ENV = "GET_FULL_ORDER_API_CREDENTIALS"
BASE_URL_ENV = "GET_FULL_ORDER_API_BASE_URL"
AUTH_MODE_ENV = "GET_FULL_ORDER_AUTH_MODE"
TOKEN_URL_ENV = "GET_FULL_ORDER_API_TOKEN_URL"
TIMEOUT_ENV = "GET_FULL_ORDER_API_TIMEOUT_SECONDS"

FULL_ORDER_RESPONSE_COLUMNS = {
    "return_proforma_invoice": "full_order_return_proforma_invoice",
    "commercial_invoice": "full_order_commercial_invoice",
    "customer_email": "full_order_customer_email",
    "customer_phone": "full_order_customer_phone",
    "shipped_at": "full_order_shipped_at",
    "api_error": "full_order_api_error",
}

FULL_ORDER_DATA_KEYS = {
    "return_proforma_invoice",
    "commercial_invoice",
    "customer_email",
    "customer_phone",
}
FULL_ORDER_LOOKUP_KEYS = FULL_ORDER_DATA_KEYS | {"authorization_letter"}
DOCUMENT_DATA_KEYS = {"authorization_letter", "power_of_attorney"}

DOCUMENT_RESPONSE_COLUMNS = {
    "authorization_letter": "generated_authorization_letter_path",
    "power_of_attorney": "generated_power_of_attorney_path",
    "document_error": "generated_document_error",
}

REQUESTED_DATA_ALIASES = {
    "ups_account": "ups_account_number",
    "ups_account_code": "ups_account_number",
    "ups_code": "ups_account_number",
    "invoice": "commercial_invoice",
    "commercial_invoice_required": "commercial_invoice",
    "invoice_correction": "corrected_invoice",
    "declaration_of_intent": "dichiarazione_di_libera_esportazione",
}

REQUESTED_DATA_SOURCE_COLUMNS = (
    "requested_data",
    "regex_requested_data",
    "regex_request_types",
    "request_types",
    "standard_reply_requested_data",
)

TEMPLATE_DIR = Path(os.getenv("PDF_TEMPLATE_DIR", "templates/pdf"))
LOA_TEMPLATE_PATH = Path(
    os.getenv(
        "LOA_TEMPLATE_PATH",
        str(TEMPLATE_DIR / "authorization_letter_template.pdf"),
    )
)
POA_TEMPLATE_PATH = Path(
    os.getenv(
        "POA_TEMPLATE_PATH",
        str(TEMPLATE_DIR / "power_of_attorney_template.pdf"),
    )
)
GENERATED_DOCUMENTS_DIR = Path(os.getenv("GENERATED_DOCUMENTS_DIR", "generated_documents"))
DOCUMENT_RESPONSE_STYLE = os.getenv("DOCUMENT_RESPONSE_STYLE", "markdown").strip().lower()
DOCUMENT_PUBLIC_BASE_URL = os.getenv("DOCUMENT_PUBLIC_BASE_URL", "").strip().rstrip("/")


def _join_public_base_url(base_url: str, relative_path: str) -> str:
    if not base_url:
        return relative_path
    return f"{base_url}/{quote(relative_path, safe='/')}"


class DocumentGenerationError(RuntimeError):
    """Raised when a requested PDF cannot be generated safely."""


@dataclass(frozen=True)
class FullOrderCredentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_environment(cls) -> "FullOrderCredentials | None":
        raw_credentials = os.getenv(CREDENTIALS_ENV, "").strip()
        if not raw_credentials:
            return None

        try:
            payload = json.loads(raw_credentials)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{CREDENTIALS_ENV} must be a JSON object with client_id and client_secret"
            ) from exc

        client_id = str(payload.get("client_id", "") or "").strip()
        client_secret = str(payload.get("client_secret", "") or "").strip()
        if not client_id or not client_secret:
            raise ValueError(
                f"{CREDENTIALS_ENV} must contain non-empty client_id and client_secret"
            )

        return cls(client_id=client_id, client_secret=client_secret)


class GetFullOrderClient:
    """Small requests-based client for Zelda's GET_FULL_ORDER endpoint."""

    def __init__(
        self,
        *,
        credentials: FullOrderCredentials | None = None,
        base_url: str | None = None,
        auth_mode: str | None = None,
        token_url: str | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
    ):
        self.credentials = credentials if credentials is not None else FullOrderCredentials.from_environment()
        self.base_url = (base_url or os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        self.auth_mode = (auth_mode or os.getenv(AUTH_MODE_ENV) or "auto").strip().lower()
        self.token_url = (token_url or os.getenv(TOKEN_URL_ENV) or "").strip()
        self.timeout = timeout if timeout is not None else float(os.getenv(TIMEOUT_ENV, "60"))
        self.session = session or requests.Session()
        self._access_token: str | None = None

    @property
    def is_configured(self) -> bool:
        return self.credentials is not None

    def order_url(self, shipment_order_number: object) -> str:
        brand = brand_from_shipment_order_number(shipment_order_number)
        order_number = order_number_from_shipment_order_number(shipment_order_number)
        if not brand or not order_number:
            raise ValueError(f"Invalid shipment_order_number: {shipment_order_number!r}")

        return (
            f"{self.base_url}/brands/{quote(brand, safe='')}/orders/"
            f"{quote(order_number, safe='')}"
        )

    def get_order_payload(self, shipment_order_number: object) -> dict[str, Any]:
        if not self.credentials:
            raise RuntimeError(f"Missing {CREDENTIALS_ENV}")

        url = self.order_url(shipment_order_number)
        last_auth_error: requests.HTTPError | None = None

        for request_kwargs in self._auth_request_kwargs():
            response = self.session.get(url, timeout=self.timeout, **request_kwargs)

            if response.status_code in {401, 403} and self.auth_mode == "auto":
                last_auth_error = _http_error(response)
                continue

            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("GET_FULL_ORDER API response was not a JSON object")
            return payload

        if last_auth_error is not None:
            raise last_auth_error

        raise RuntimeError("No usable GET_FULL_ORDER authentication strategy was configured")

    def _auth_request_kwargs(self) -> list[dict[str, Any]]:
        if not self.credentials:
            return [{}]

        mode = self.auth_mode
        if mode == "auto":
            strategies = []
            if self.token_url:
                strategies.append("oauth2")
            strategies.extend(["headers", "basic"])
        else:
            strategies = [mode]

        return [self._strategy_kwargs(strategy) for strategy in strategies]

    def _strategy_kwargs(self, strategy: str) -> dict[str, Any]:
        assert self.credentials is not None

        if strategy == "headers":
            return {
                "headers": {
                    "Accept": "application/json",
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret,
                    "X-Client-Id": self.credentials.client_id,
                    "X-Client-Secret": self.credentials.client_secret,
                }
            }

        if strategy == "basic":
            return {
                "headers": {"Accept": "application/json"},
                "auth": HTTPBasicAuth(
                    self.credentials.client_id,
                    self.credentials.client_secret,
                ),
            }

        if strategy == "oauth2":
            return {
                "headers": {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._get_access_token()}",
                }
            }

        if strategy in {"none", "noauth"}:
            return {"headers": {"Accept": "application/json"}}

        raise ValueError(
            f"Unsupported {AUTH_MODE_ENV}={strategy!r}; use auto, headers, basic, oauth2, or none"
        )

    def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        if not self.token_url:
            raise ValueError(f"{TOKEN_URL_ENV} is required when {AUTH_MODE_ENV}=oauth2")
        if not self.credentials:
            raise RuntimeError(f"Missing {CREDENTIALS_ENV}")

        response = self.session.post(
            self.token_url,
            data={"grant_type": "client_credentials"},
            auth=HTTPBasicAuth(
                self.credentials.client_id,
                self.credentials.client_secret,
            ),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token", "") or "").strip()
        if not token:
            raise ValueError("OAuth2 token response did not contain access_token")
        self._access_token = token
        return token


def _http_error(response: requests.Response) -> requests.HTTPError:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc
    return requests.HTTPError(f"HTTP {response.status_code}", response=response)


def is_blank(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text.lower() in {"", "nan", "none", "null", "n/a", "na", PLACEHOLDER.lower()}


def safe_filename(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = text.strip("._-")
    return text or "document"


def normalize_data_key(value: object) -> str:
    key = str(value or "").strip()
    return REQUESTED_DATA_ALIASES.get(key, key)


def normalize_requested_data_with_aliases(value: object) -> list[str]:
    cleaned: list[str] = []
    for item in normalize_requested_data(value):
        key = normalize_data_key(item)
        if key and key not in cleaned:
            cleaned.append(key)
    return cleaned


def requested_data_keys_from_row(row: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for column in REQUESTED_DATA_SOURCE_COLUMNS:
        for key in normalize_requested_data_with_aliases(row.get(column)):
            if key and key not in keys:
                keys.append(key)
    return keys


def _email_domain(email: object) -> str:
    normalized = normalize_email(email)
    if "@" not in normalized:
        return ""
    return normalized.rsplit("@", 1)[-1]


def is_ups_requester_email(email: object) -> bool:
    normalized = normalize_email(email)
    domain = _email_domain(email)
    return normalized == UPS_BROKERAGE_EMAIL or domain == "ups.com" or domain.endswith(".ups.com")


def row_needs_full_order_lookup(
    row: Mapping[str, Any],
    requested_data: list[str] | None = None,
) -> bool:
    requested_data = requested_data if requested_data is not None else requested_data_keys_from_row(row)
    requested_set = set(requested_data)

    if requested_set & FULL_ORDER_LOOKUP_KEYS:
        return True

    # The standard UPS-account draft includes an LOA.  The LOA export date comes
    # from GET_FULL_ORDER.shipments[].shippedAt, so the lookup is still needed.
    return requested_data == ["ups_account_number"] and is_ups_requester_email(row.get("requester_email"))


def row_needs_document_generation(
    row: Mapping[str, Any],
    requested_data: list[str] | None = None,
) -> bool:
    requested_data = requested_data if requested_data is not None else requested_data_keys_from_row(row)
    requested_set = set(requested_data)

    if requested_set & DOCUMENT_DATA_KEYS:
        return True

    # The standard UPS-account reply includes the generated LOA even when
    # authorization_letter was not explicitly requested as standalone data.
    return requested_data == ["ups_account_number"] and is_ups_requester_email(row.get("requester_email"))


def _initialize_response_data_columns(df):
    for column in FULL_ORDER_RESPONSE_COLUMNS.values():
        if column not in df.columns:
            df[column] = None
    for column in DOCUMENT_RESPONSE_COLUMNS.values():
        if column not in df.columns:
            df[column] = None
    return df


def row_has_full_order_attempt(row: Mapping[str, Any]) -> bool:
    return any(not is_blank(row.get(column)) for column in FULL_ORDER_RESPONSE_COLUMNS.values())


def enrich_dataframe_with_full_order_data(
    df,
    *,
    client: GetFullOrderClient | None = None,
    fetch_all: bool = False,
    skip_existing: bool = True,
):
    """Enrich request rows with GET_FULL_ORDER values and return the DataFrame.

    This function is used by the standalone workflow step.  It writes API data
    back to the local handoff file before response_generator.py builds the final
    draft, making response_data_extractor.py an explicit part of the pipeline.
    """

    if df.empty:
        return df

    df = _initialize_response_data_columns(df.copy())
    requested_data_by_index = {
        index: requested_data_keys_from_row(row)
        for index, row in df.iterrows()
    }

    lookup_indices = []
    for index, row in df.iterrows():
        if skip_existing and row_has_full_order_attempt(row):
            continue
        if fetch_all:
            needs_lookup = not is_blank(row.get("shipment_order_number"))
        else:
            needs_lookup = row_needs_full_order_lookup(row, requested_data_by_index[index])
        if needs_lookup:
            lookup_indices.append(index)

    if not lookup_indices:
        print("No rows need GET_FULL_ORDER enrichment.")
        return df

    try:
        api_client = client or GetFullOrderClient()
    except Exception as exc:
        print(f"WARNING: GET_FULL_ORDER client could not be configured: {exc}")
        for index in lookup_indices:
            df.at[index, FULL_ORDER_RESPONSE_COLUMNS["api_error"]] = str(exc)
        return df

    if not api_client.is_configured:
        message = "Missing GET_FULL_ORDER_API_CREDENTIALS"
        print(f"WARNING: {message}; API-backed response data will be unavailable.")
        for index in lookup_indices:
            df.at[index, FULL_ORDER_RESPONSE_COLUMNS["api_error"]] = message
        return df

    cache: dict[str, dict[str, Any]] = {}

    for index in lookup_indices:
        shipment_order_number = df.at[index, "shipment_order_number"] if "shipment_order_number" in df.columns else None
        if is_blank(shipment_order_number):
            df.at[index, FULL_ORDER_RESPONSE_COLUMNS["api_error"]] = "Missing shipment_order_number for GET_FULL_ORDER lookup"
            continue

        cache_key = str(shipment_order_number).strip().upper()
        if cache_key not in cache:
            try:
                cache[cache_key] = fetch_shipment_order_data(
                    shipment_order_number,
                    client=api_client,
                )
            except Exception as exc:
                print(
                    "WARNING: GET_FULL_ORDER lookup failed for "
                    f"shipment_order_number={shipment_order_number}: {exc}"
                )
                cache[cache_key] = {FULL_ORDER_RESPONSE_COLUMNS["api_error"]: str(exc)}

        for column, value in cache[cache_key].items():
            if column not in df.columns:
                df[column] = None
            df.at[index, column] = value

    print(f"GET_FULL_ORDER enrichment attempted for {len(lookup_indices)} row(s).")
    return df


def generate_documents_for_dataframe(df, *, force: bool = False):
    if df.empty:
        return df

    df = _initialize_response_data_columns(df.copy())
    generated = 0

    for index, row in df.iterrows():
        requested_data = requested_data_keys_from_row(row)
        if not row_needs_document_generation(row, requested_data):
            continue

        needs_loa = "authorization_letter" in requested_data or (
            requested_data == ["ups_account_number"] and is_ups_requester_email(row.get("requester_email"))
        )
        needs_poa = "power_of_attorney" in requested_data

        if needs_loa:
            column = DOCUMENT_RESPONSE_COLUMNS["authorization_letter"]
            existing_path = row.get(column)
            if force or is_blank(existing_path) or not document_path_exists(existing_path):
                try:
                    row_data = df.loc[index].to_dict()
                    df.at[index, column] = generate_authorization_letter(row_data)
                    generated += 1
                except Exception as exc:
                    message = f"authorization_letter: {exc}"
                    print(f"WARNING: Could not generate authorization_letter for row {index}: {exc}")
                    df.at[index, DOCUMENT_RESPONSE_COLUMNS["document_error"]] = message

        if needs_poa:
            column = DOCUMENT_RESPONSE_COLUMNS["power_of_attorney"]
            existing_path = row.get(column)
            if force or is_blank(existing_path) or not document_path_exists(existing_path):
                try:
                    row_data = df.loc[index].to_dict()
                    df.at[index, column] = generate_power_of_attorney(row_data)
                    generated += 1
                except Exception as exc:
                    message = f"power_of_attorney: {exc}"
                    existing_error = df.at[index, DOCUMENT_RESPONSE_COLUMNS["document_error"]]
                    if not is_blank(existing_error):
                        message = f"{existing_error}; {message}"
                    print(f"WARNING: Could not generate power_of_attorney for row {index}: {exc}")
                    df.at[index, DOCUMENT_RESPONSE_COLUMNS["document_error"]] = message

    print(f"Generated {generated} PDF document(s).")
    return df


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich request-intent rows with GET_FULL_ORDER data and generated PDF documents."
    )
    parser.add_argument(
        "--input",
        default=str(REQUEST_INTENT_RESULTS_PATH),
        help="Compressed JSONL handoff to read. Defaults to output/request_intent_results.jsonl.gz.",
    )
    parser.add_argument(
        "--output",
        default=str(REQUEST_INTENT_RESULTS_PATH),
        help="Compressed JSONL handoff to write. Defaults to the same request-intent file.",
    )
    parser.add_argument(
        "--fetch-all",
        action="store_true",
        help="Call GET_FULL_ORDER for every row with a shipment_order_number, not only rows whose requested_data needs it.",
    )
    parser.add_argument(
        "--skip-documents",
        action="store_true",
        help="Skip LOA/POA PDF generation.",
    )
    parser.add_argument(
        "--force-documents",
        action="store_true",
        help="Regenerate LOA/POA PDFs even when generated paths already exist in the handoff.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    df = read_dataframe(args.input)

    if df.empty:
        print("No request-intent rows found. Nothing to enrich.")
        write_dataframe(df, args.output)
        return

    df = enrich_dataframe_with_full_order_data(df, fetch_all=args.fetch_all)
    if not args.skip_documents:
        df = generate_documents_for_dataframe(df, force=args.force_documents)

    write_dataframe(df, args.output)
    print(f"Response data extraction completed for {len(df)} row(s). Wrote {args.output}.")


def brand_from_shipment_order_number(shipment_order_number: object) -> str:
    value = str(shipment_order_number or "").strip().upper()
    return value[:2] if len(value) >= 2 else ""


def order_number_from_shipment_order_number(shipment_order_number: object) -> str:
    """
    Convert a shipment_order_number into the order number used by the API.

    Example:
        DG-EUA01663254 -> DG-EU-01663254
    """

    value = str(shipment_order_number or "").strip().upper()
    if len(value) >= 6:
        return f"{value[:5]}-{value[6:]}"
    return value


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def _get_any(mapping: Mapping[str, Any] | None, names: Iterable[str]) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for name in names:
        if name in mapping:
            return mapping.get(name)
    return None


def _find_first_key(value: Any, target_key: str) -> Any:
    if isinstance(value, Mapping):
        if target_key in value:
            return value[target_key]
        for child in value.values():
            found = _find_first_key(child, target_key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first_key(child, target_key)
            if found is not None:
                return found
    return None


def _nested_get(mapping: Mapping[str, Any] | None, *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _shipment_order_value(shipment: Mapping[str, Any]) -> str:
    value = _get_any(
        shipment,
        (
            "shipmentOrderNumber",
            "shipment_order_number",
            "shipmentOrderNo",
            "shipment_order_no",
        ),
    )
    return str(value or "").strip().upper()


def _shipments_from_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    shipments = payload.get("shipments")
    if shipments is None and isinstance(payload.get("order"), Mapping):
        shipments = payload["order"].get("shipments")
    if shipments is None:
        shipments = _find_first_key(payload, "shipments")

    if not isinstance(shipments, list):
        return []

    return [shipment for shipment in shipments if isinstance(shipment, Mapping)]


def shipment_block_from_order_payload(
    payload: Mapping[str, Any],
    shipment_order_number: object,
) -> Mapping[str, Any] | None:
    wanted = str(shipment_order_number or "").strip().upper()
    if not wanted:
        return None

    for shipment in _shipments_from_payload(payload):
        if _shipment_order_value(shipment) == wanted:
            return shipment

    return None


def _invoice_documents_from_shipment(shipment: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    invoice_documents = _nested_get(shipment, "erpDocuments", "invoiceDocuments")
    if not isinstance(invoice_documents, list):
        return []
    return [document for document in invoice_documents if isinstance(document, Mapping)]


def select_invoice_document_link(
    invoice_documents: Iterable[Mapping[str, Any]],
    document_type: str,
) -> str | None:
    wanted_document_type = str(document_type or "").strip().upper()
    matches = [
        document
        for document in invoice_documents
        if str(document.get("documentType", "") or "").strip().upper() == wanted_document_type
        and not is_blank(document.get("documentLink"))
    ]

    if not matches:
        return None

    intercompany_matches = [
        document
        for document in matches
        if _truthy(document.get("intercompanyDocument"))
    ]

    selected = intercompany_matches[0] if intercompany_matches else matches[0]
    return str(selected.get("documentLink") or "").strip() or None


def _customer_from_payload_or_shipment(
    payload: Mapping[str, Any],
    shipment: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    customer = _get_any(shipment, ("customer",))
    if isinstance(customer, Mapping):
        return customer

    customer = payload.get("customer")
    if isinstance(customer, Mapping):
        return customer

    order = payload.get("order")
    if isinstance(order, Mapping) and isinstance(order.get("customer"), Mapping):
        return order["customer"]

    customer = _find_first_key(payload, "customer")
    if isinstance(customer, Mapping):
        return customer

    return {}


def _shipped_at_from_shipment(shipment: Mapping[str, Any] | None) -> str | None:
    # The current GET_FULL_ORDER payload uses camelCase shippedAt.  Keep the
    # snake_case and legacy alternatives to avoid breaking older fixtures.
    value = _get_any(shipment, ("shippedAt", "shipped_at", "shippedDate", "shipmentDate"))
    if is_blank(value):
        return None
    return str(value).strip()


def extract_shipment_order_data(
    payload: Mapping[str, Any],
    shipment_order_number: object,
) -> dict[str, Any]:
    """Extract only the current shipment's response fields from a full order."""

    shipment = shipment_block_from_order_payload(payload, shipment_order_number)
    if shipment is None:
        return {
            FULL_ORDER_RESPONSE_COLUMNS["api_error"]: (
                "Shipment block not found in GET_FULL_ORDER response"
            )
        }

    invoice_documents = _invoice_documents_from_shipment(shipment)
    customer = _customer_from_payload_or_shipment(payload, shipment)

    return {
        FULL_ORDER_RESPONSE_COLUMNS["return_proforma_invoice"]: select_invoice_document_link(
            invoice_documents,
            "RPI",
        ),
        FULL_ORDER_RESPONSE_COLUMNS["commercial_invoice"]: select_invoice_document_link(
            invoice_documents,
            "INV",
        ),
        FULL_ORDER_RESPONSE_COLUMNS["customer_email"]: (
            None if is_blank(customer.get("email")) else str(customer.get("email")).strip()
        ),
        FULL_ORDER_RESPONSE_COLUMNS["customer_phone"]: (
            None
            if is_blank(customer.get("customerNumber"))
            else str(customer.get("customerNumber")).strip()
        ),
        FULL_ORDER_RESPONSE_COLUMNS["shipped_at"]: _shipped_at_from_shipment(shipment),
        FULL_ORDER_RESPONSE_COLUMNS["api_error"]: None,
    }


def fetch_shipment_order_data(
    shipment_order_number: object,
    *,
    client: GetFullOrderClient | None = None,
) -> dict[str, Any]:
    client = client or GetFullOrderClient()
    payload = client.get_order_payload(shipment_order_number)
    return extract_shipment_order_data(payload, shipment_order_number)


def tracking_number_for_documents(row: Mapping[str, Any]) -> str:
    return first_available_value(
        row.get("extracted_tracking_number"),
        row.get("shipment_tracking_number"),
        default="",
    )


def ups_account_number_for_documents(row: Mapping[str, Any]) -> str:
    ups_code = extract_ups_code(
        row.get("shipment_tracking_number"),
        row.get("extracted_tracking_number"),
        row.get("return_tracking_number"),
    )
    if ups_code:
        return ups_code

    # Some carrier emails or file names include an extra trailing check digit or
    # punctuation around an otherwise valid UPS tracking number. The account code
    # is still the 6 characters immediately after the leading 1Z. This fallback is
    # limited to LOA generation and does not alter the existing response logic for
    # ups_account_number.
    for value in (
        row.get("shipment_tracking_number"),
        row.get("extracted_tracking_number"),
        row.get("return_tracking_number"),
    ):
        text = str(value or "").strip().upper()
        match = re.search(r"1Z([0-9A-Z]{6})", text)
        if match:
            return match.group(1)

    return ""


def _today() -> date:
    override = os.getenv("AGENT_TODAY", "").strip()
    if override:
        parsed = datetime.fromisoformat(override.replace("Z", "+00:00"))
        return parsed.date()
    return datetime.now(timezone.utc).date()


def today_ddmmyyyy() -> str:
    return _today().strftime("%d/%m/%Y")


def today_mmddyyyy() -> str:
    return _today().strftime("%m/%d/%Y")


def format_api_datetime(value: object, output_format: str) -> str:
    if is_blank(value):
        return ""

    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DocumentGenerationError(f"Cannot parse API shippedAt value: {raw!r}") from exc

    return parsed.strftime(output_format)


def document_local_path(path: str | Path | None) -> Path | None:
    if not path:
        return None

    text = str(path).strip()
    if not text or re.match(r"^[a-z][a-z0-9+.-]*://", text, flags=re.IGNORECASE):
        return None

    return Path(text)


def document_path_exists(path: str | Path | None) -> bool:
    local_path = document_local_path(path)
    return bool(local_path and local_path.exists() and local_path.is_file())


def document_reference(path: str | Path | None) -> str:
    if not path:
        return PLACEHOLDER

    text = str(path).strip()
    if not text:
        return PLACEHOLDER

    if re.match(r"^[a-z][a-z0-9+.-]*://", text, flags=re.IGNORECASE):
        href = text
        display_path = Path(text)
    else:
        path_obj = Path(text)
        if path_obj.is_absolute():
            try:
                relative_path = path_obj.relative_to(Path.cwd()).as_posix()
            except ValueError:
                relative_path = path_obj.as_posix()
                href = relative_path
            else:
                href = _join_public_base_url(DOCUMENT_PUBLIC_BASE_URL, relative_path)
        else:
            relative_path = path_obj.as_posix()
            href = _join_public_base_url(DOCUMENT_PUBLIC_BASE_URL, relative_path)
        display_path = Path(relative_path)

    if DOCUMENT_RESPONSE_STYLE == "plain":
        return href

    return f"[{display_path.name}]({href})"


def _require_template(path: Path, label: str) -> None:
    if not path.exists():
        raise DocumentGenerationError(f"{label} template not found: {path}")


def _output_path(document_type: str, tracking_number: str) -> Path:
    output_dir = GENERATED_DOCUMENTS_DIR / document_type
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{safe_filename(tracking_number)}.pdf"


def _first_rect(page: fitz.Page, text: str, *, occurrence: int = 0) -> fitz.Rect:
    rects = page.search_for(text)
    if not rects:
        raise DocumentGenerationError(f"Could not locate LOA template label: {text}")
    return rects[occurrence]


def _replace_value_after_label(
    page: fitz.Page,
    label: str,
    value: str,
    *,
    occurrence: int = 0,
    cover_width: float = 170,
    font_size: float = 12,
    x_padding: float = 4,
) -> None:
    label_rect = _first_rect(page, label, occurrence=occurrence)
    x0 = label_rect.x1 + x_padding
    y0 = label_rect.y0 - 1
    y1 = label_rect.y1 + 2
    value_width = fitz.get_text_length(value, fontname="helv", fontsize=font_size) + 10
    x1 = min(page.rect.width - 36, x0 + max(cover_width, value_width))
    cover_rect = fitz.Rect(x0, y0, x1, y1)
    page.add_redact_annot(cover_rect, fill=(1, 1, 1))
    page.apply_redactions()
    page.insert_text(
        fitz.Point(x0, label_rect.y1 - 1.5),
        value,
        fontname="helv",
        fontsize=font_size,
        color=(0, 0, 0),
        overlay=True,
    )


def generate_authorization_letter(row: Mapping[str, Any]) -> str:
    """
    Generate a filled LOA / authorization letter PDF and return its relative path.

    The generated file name is the extracted tracking number, stored under the
    authorization_letter subfolder to avoid collisions with other document types.
    """

    _require_template(LOA_TEMPLATE_PATH, "LOA")

    tracking_number = tracking_number_for_documents(row)
    ups_account = ups_account_number_for_documents(row)
    shipped_at = row.get("full_order_shipped_at") or row.get("shippedAt") or row.get("shipped_at")

    if is_blank(tracking_number):
        raise DocumentGenerationError("Cannot generate LOA without extracted tracking number")
    if is_blank(ups_account):
        raise DocumentGenerationError("Cannot generate LOA without UPS account number")
    if is_blank(shipped_at):
        raise DocumentGenerationError("Cannot generate LOA without full_order_shipped_at")

    export_date = format_api_datetime(shipped_at, "%d/%m/%Y")
    today = today_ddmmyyyy()
    output_path = _output_path("authorization_letter", tracking_number)

    doc = fitz.open(LOA_TEMPLATE_PATH)
    page = doc[0]

    _replace_value_after_label(
        page,
        "UPS Account number:",
        ups_account,
        cover_width=120,
    )
    _replace_value_after_label(
        page,
        "Tracking number(s):",
        tracking_number,
        cover_width=210,
    )
    _replace_value_after_label(
        page,
        "Export date:",
        export_date,
        cover_width=110,
    )
    _replace_value_after_label(
        page,
        "to our UPS account:",
        ups_account,
        cover_width=95,
    )
    _replace_value_after_label(
        page,
        "Date:",
        today,
        occurrence=-1,
        cover_width=110,
    )

    doc.save(output_path, deflate=True, garbage=4)
    doc.close()
    return output_path.as_posix()


def generate_power_of_attorney(row: Mapping[str, Any]) -> str:
    """Generate a filled POA PDF and return its relative path."""

    _require_template(POA_TEMPLATE_PATH, "POA")

    tracking_number = tracking_number_for_documents(row)
    if is_blank(tracking_number):
        raise DocumentGenerationError("Cannot generate POA without extracted tracking number")

    output_path = _output_path("power_of_attorney", tracking_number)
    today = today_mmddyyyy()

    doc = fitz.open(POA_TEMPLATE_PATH)
    updated_fields = set()

    for page in doc:
        widgets = page.widgets() or []
        for widget in widgets:
            if widget.field_name == "Tracking Number":
                widget.field_value = tracking_number
                widget.update()
                updated_fields.add(widget.field_name)
            elif widget.field_name == "Date":
                widget.field_value = today
                widget.update()
                updated_fields.add(widget.field_name)

    missing_fields = {"Tracking Number", "Date"} - updated_fields
    if missing_fields:
        doc.close()
        raise DocumentGenerationError(
            "POA template is missing expected form fields: " + ", ".join(sorted(missing_fields))
        )

    doc.save(output_path, deflate=True, garbage=4)
    doc.close()
    return output_path.as_posix()


def document_value_for_response(document_path: str | None) -> str:
    return document_reference(document_path)


if __name__ == "__main__":
    main()

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
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import fitz
import requests
from requests.auth import HTTPBasicAuth

from customs_rules import (
    PLACEHOLDER,
    UPS_BROKERAGE_EMAIL,
    extract_ups_code,
    collapse_document_embedded_requested_data,
    first_available_value,
    is_noreply_requester_email,
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
INVOICE_DOWNLOAD_TIMEOUT_ENV = "INVOICE_PDF_DOWNLOAD_TIMEOUT_SECONDS"
INVOICE_DOWNLOAD_USER_AGENT_ENV = "INVOICE_PDF_DOWNLOAD_USER_AGENT"
INVOICE_DOWNLOAD_MAX_ATTEMPTS_ENV = "INVOICE_PDF_DOWNLOAD_MAX_ATTEMPTS"

FULL_ORDER_RESPONSE_COLUMNS = {
    "return_proforma_invoice": "full_order_return_proforma_invoice",
    "commercial_invoice": "full_order_commercial_invoice",
    "returned_items_confirmation": "full_order_returned_items",
    "customer_email": "full_order_customer_email",
    "customer_phone": "full_order_customer_phone",
    "customer_name": "full_order_customer_name",
    "shipping_address": "full_order_shipping_address",
    "shipped_at": "full_order_shipped_at",
    "api_error": "full_order_api_error",
}

FULL_ORDER_DATA_KEYS = {
    "return_proforma_invoice",
    "commercial_invoice",
    "returned_items_confirmation",
    "customer_email",
    "customer_phone",
    "customer_name",
    "shipping_address",
}
FULL_ORDER_LOOKUP_KEYS = FULL_ORDER_DATA_KEYS | {"authorization_letter"}
DOCUMENT_DATA_KEYS = {"authorization_letter", "power_of_attorney"}
INVOICE_DOCUMENT_DATA_KEYS = {"return_proforma_invoice", "commercial_invoice"}

DOCUMENT_RESPONSE_COLUMNS = {
    "authorization_letter": "generated_authorization_letter_path",
    "power_of_attorney": "generated_power_of_attorney_path",
    "return_proforma_invoice": "generated_return_proforma_invoice_path",
    "commercial_invoice": "generated_commercial_invoice_path",
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


def _row_request_text(row: Mapping[str, Any]) -> str:
    return "\n".join(
        str(value or "")
        for value in (
            row.get("subject", ""),
            row.get("cleaned_request_body", ""),
            row.get("request_body", ""),
        )
        if not is_blank(value)
    )


def requested_data_keys_from_row(row: Mapping[str, Any]) -> list[str]:
    """Return the effective requested_data keys for API/doc retrieval.

    The canonical ``requested_data`` column is the source of truth for what the
    response should provide.  Regex and standard-reply columns are only fallbacks
    for older/incomplete rows where the canonical field is empty.

    This prevents downloading both the commercial invoice and the return
    proforma invoice when one of the non-canonical helper columns still contains
    the other document type.
    """

    def _collapsed_unique_keys(value: Any) -> list[str]:
        keys: list[str] = []
        collapsed_keys = collapse_document_embedded_requested_data(
            value,
            ticket_category=row.get("ticket_category", ""),
            request_number=row.get("request_number", 1),
            requester_email=row.get("requester_email", ""),
            request_text=_row_request_text(row),
        )
        for key in collapsed_keys:
            key = normalize_data_key(key)
            if key and key not in keys:
                keys.append(key)
        return keys

    canonical_keys = _collapsed_unique_keys(row.get("requested_data"))
    if canonical_keys:
        return canonical_keys

    fallback_keys: list[str] = []
    for column in REQUESTED_DATA_SOURCE_COLUMNS:
        if column == "requested_data":
            continue
        for key in _collapsed_unique_keys(row.get(column)):
            if key and key not in fallback_keys:
                fallback_keys.append(key)
    return fallback_keys


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
    if is_noreply_requester_email(row.get("requester_email")):
        return False

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
    if is_noreply_requester_email(row.get("requester_email")):
        return False

    requested_data = requested_data if requested_data is not None else requested_data_keys_from_row(row)
    requested_set = set(requested_data)

    if requested_set & (DOCUMENT_DATA_KEYS | INVOICE_DOCUMENT_DATA_KEYS):
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


def _append_document_error(df, index: object, message: str) -> None:
    error_column = DOCUMENT_RESPONSE_COLUMNS["document_error"]
    existing_error = df.at[index, error_column] if error_column in df.columns else None
    if not is_blank(existing_error):
        message = f"{existing_error}; {message}"
    df.at[index, error_column] = message


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

        for invoice_key in sorted(set(requested_data) & INVOICE_DOCUMENT_DATA_KEYS):
            column = DOCUMENT_RESPONSE_COLUMNS[invoice_key]
            existing_path = row.get(column)
            if not force and not is_blank(existing_path) and document_path_exists(existing_path):
                continue

            source_column = FULL_ORDER_RESPONSE_COLUMNS[invoice_key]
            source_link = row.get(source_column)
            if is_blank(source_link):
                # The full-order missing-value check will block the automatic reply.
                continue

            try:
                row_data = df.loc[index].to_dict()
                df.at[index, column] = download_invoice_pdf(
                    source_link,
                    invoice_key,
                    row=row_data,
                )
                generated += 1
            except Exception as exc:
                message = f"{invoice_key}: {exc}"
                print(f"WARNING: Could not download {invoice_key} PDF for row {index}: {exc}")
                _append_document_error(df, index, message)

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
                    _append_document_error(df, index, message)

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
                    print(f"WARNING: Could not generate power_of_attorney for row {index}: {exc}")
                    _append_document_error(df, index, message)

    print(f"Generated or downloaded {generated} PDF document(s).")
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


def _normalize_shipment_order_number(shipment_order_number: object) -> str:
    return str(shipment_order_number or "").strip().upper()


def brand_from_shipment_order_number(shipment_order_number: object) -> str:
    value = _normalize_shipment_order_number(shipment_order_number)
    return value[:2] if len(value) >= 2 else ""


def order_number_from_shipment_order_number(shipment_order_number: object) -> str:
    """
    Convert a shipment_order_number into the order number used by GET_FULL_ORDER.

    Examples:
        DG-EUA01663254 -> DG-EU-01663254
        DG-EUB01614772 -> DG-EU-01614772
        DG-EUC01669099 -> DG-EU-01669099
        DG-USA11590412 -> DG-US-11590412
        DG-USB11591156 -> DG-US-11591156
        DG-USC11589641 -> DG-US-11589641
        DG-EU-01663254 -> DG-EU-01663254
        DG-US-11590412 -> DG-US-11590412
        DG-EUF01663254 -> DG-EUF01663254
        DG-USF11590412 -> DG-USF11590412
    """

    value = _normalize_shipment_order_number(shipment_order_number)
    if not value:
        return value

    # Already in the GET_FULL_ORDER URL shape: keep it unchanged.
    if "-EU-" in value or "-US-" in value:
        return value

    # EUF/USF are distinct order-number families and must not be rewritten to
    # EU-/US-. Match them only as the market code immediately after the brand
    # prefix, so values such as USB/USC/EUB/EUC still go through conversion.
    if re.match(r"^[A-Z0-9]{2}-(?:EUF|USF).+$", value):
        return value

    # Historical shipping-platform values use EU?/US? in the shipment block but
    # GET_FULL_ORDER expects EU-/US- in the order URL. Only EUF/USF bypass this
    # rewrite; for example USB, USC, EUB, and EUC all become US-/EU-.
    match = re.match(r"^([A-Z0-9]{2}-(?:EU|US))([A-Z])(.+)$", value)
    if match:
        region_prefix, family_code, suffix = match.groups()
        if family_code != "F":
            return f"{region_prefix}-{suffix}"

    return value


def shipment_order_number_for_shipment_block_lookup(shipment_order_number: object) -> str:
    """Return the shipmentOrderNumber shape used inside GET_FULL_ORDER payloads."""
    value = _normalize_shipment_order_number(shipment_order_number)
    if "-EU-" in value:
        return value.replace("-EU-", "-EUA", 1)
    if "-US-" in value:
        return value.replace("-US-", "-USA", 1)
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
    wanted = shipment_order_number_for_shipment_block_lookup(shipment_order_number)
    if not wanted:
        return None

    for shipment in _shipments_from_payload(payload):
        current = shipment_order_number_for_shipment_block_lookup(
            _shipment_order_value(shipment)
        )
        if current == wanted:
            return shipment

    return None


def _invoice_documents_from_shipment(shipment: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    invoice_documents = _nested_get(shipment, "erpDocuments", "invoiceDocuments")
    if not isinstance(invoice_documents, list):
        return []
    return [document for document in invoice_documents if isinstance(document, Mapping)]


def _items_from_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = payload.get("items")
    if items is None and isinstance(payload.get("order"), Mapping):
        items = payload["order"].get("items")
    if items is None:
        items = _find_first_key(payload, "items")

    if not isinstance(items, list):
        return []

    return [item for item in items if isinstance(item, Mapping)]


def _clean_item_field(value: object) -> str:
    if is_blank(value):
        return ""
    return str(value).strip()


def extract_returned_items(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    returned_items: list[dict[str, str]] = []

    for item in _items_from_payload(payload):
        returned_item = {
            "sku": _clean_item_field(_get_any(item, ("sku", "SKU"))),
            "productName": _clean_item_field(
                _get_any(item, ("productName", "product_name", "name", "productTitle"))
            ),
            "imageUrl": _clean_item_field(
                _get_any(item, ("imageUrl", "image_url", "imageURL", "image", "imageLink"))
            ),
        }
        if any(returned_item.values()):
            returned_items.append(returned_item)

    return returned_items


def format_returned_items(returned_items: Iterable[Mapping[str, str]]) -> str | None:
    lines: list[str] = []

    for item in returned_items:
        parts = []
        sku = _clean_item_field(item.get("sku"))
        product_name = _clean_item_field(item.get("productName"))
        image_url = _clean_item_field(item.get("imageUrl"))

        if sku:
            parts.append(f"SKU: {sku}")
        if product_name:
            parts.append(f"Product: {product_name}")
        if image_url:
            parts.append(f"Image: {image_url}")

        if parts:
            lines.append("  - " + "; ".join(parts))

    return "\n".join(lines) if lines else None


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


def _shipping_address_from_payload_or_shipment(
    payload: Mapping[str, Any],
    shipment: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    shipping_address = _get_any(shipment, ("shippingAddress", "shipping_address"))
    if isinstance(shipping_address, Mapping):
        return shipping_address

    shipping_address = _get_any(payload, ("shippingAddress", "shipping_address"))
    if isinstance(shipping_address, Mapping):
        return shipping_address

    order = payload.get("order")
    if isinstance(order, Mapping):
        shipping_address = _get_any(order, ("shippingAddress", "shipping_address"))
        if isinstance(shipping_address, Mapping):
            return shipping_address

    shipping_address = _find_first_key(payload, "shippingAddress")
    if isinstance(shipping_address, Mapping):
        return shipping_address

    shipping_address = _find_first_key(payload, "shipping_address")
    if isinstance(shipping_address, Mapping):
        return shipping_address

    return {}


def _format_customer_name(shipping_address: Mapping[str, Any]) -> str | None:
    parts = [
        _clean_item_field(_get_any(shipping_address, ("name", "firstName", "first_name"))),
        _clean_item_field(_get_any(shipping_address, ("surName", "surname", "lastName", "last_name"))),
    ]
    value = " ".join(part for part in parts if part).strip()
    return value or None


def _format_shipping_address(shipping_address: Mapping[str, Any]) -> str | None:
    parts = [
        _clean_item_field(_get_any(shipping_address, ("addressLine1", "address_line_1", "address1"))),
        _clean_item_field(_get_any(shipping_address, ("zip", "postalCode", "postal_code"))),
        _clean_item_field(_get_any(shipping_address, ("stateOrProvince", "state_or_province", "state", "province"))),
        _clean_item_field(_get_any(shipping_address, ("country", "countryCode", "country_code"))),
        _clean_item_field(_get_any(shipping_address, ("cityOrTown", "city_or_town", "city", "town"))),
    ]
    value = ", ".join(part for part in parts if part).strip()
    return value or None


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
    shipping_address = _shipping_address_from_payload_or_shipment(payload, shipment)

    return {
        FULL_ORDER_RESPONSE_COLUMNS["return_proforma_invoice"]: select_invoice_document_link(
            invoice_documents,
            "RPI",
        ),
        FULL_ORDER_RESPONSE_COLUMNS["commercial_invoice"]: select_invoice_document_link(
            invoice_documents,
            "INV",
        ),
        FULL_ORDER_RESPONSE_COLUMNS["returned_items_confirmation"]: format_returned_items(
            extract_returned_items(payload)
        ),
        FULL_ORDER_RESPONSE_COLUMNS["customer_email"]: (
            None if is_blank(customer.get("email")) else str(customer.get("email")).strip()
        ),
        FULL_ORDER_RESPONSE_COLUMNS["customer_phone"]: (
            None
            if is_blank(_nested_get(shipping_address, "phoneNumbers", "home"))
            else str(_nested_get(shipping_address, "phoneNumbers", "home")).strip()
        ),
        FULL_ORDER_RESPONSE_COLUMNS["customer_name"]: _format_customer_name(shipping_address),
        FULL_ORDER_RESPONSE_COLUMNS["shipping_address"]: _format_shipping_address(shipping_address),
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


def is_url(value: object) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", str(value or "").strip(), flags=re.IGNORECASE))


def _invoice_filename_from_link(source_link: str, data_key: str, row: Mapping[str, Any] | None = None) -> str:
    row = row or {}
    parsed = urlparse(source_link)
    query_values = parse_qs(parsed.query)

    candidate = ""
    for query_key in ("link", "filename", "file", "name"):
        values = query_values.get(query_key)
        if values:
            candidate = values[0]
            break

    if not candidate:
        candidate = Path(unquote(parsed.path)).name

    candidate = unquote(str(candidate or "")).replace("\\", "/").rsplit("/", 1)[-1]
    if not candidate or candidate.lower().endswith((".aspx", ".ashx", ".php", ".html", ".htm")):
        fallback_parts = [
            data_key,
            row.get("shipment_order_number"),
            row.get("extracted_tracking_number"),
            row.get("shipment_tracking_number"),
            row.get("request_id"),
        ]
        candidate = "_".join(str(part).strip() for part in fallback_parts if not is_blank(part))

    filename = safe_filename(candidate)
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return filename


def _invoice_output_path(source_link: str, data_key: str, row: Mapping[str, Any] | None = None) -> Path:
    output_dir = GENERATED_DOCUMENTS_DIR / "invoice"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / _invoice_filename_from_link(source_link, data_key, row)


def _download_timeout() -> float:
    return float(os.getenv(INVOICE_DOWNLOAD_TIMEOUT_ENV, os.getenv(TIMEOUT_ENV, "60")))


def _download_max_attempts() -> int:
    try:
        return max(1, int(os.getenv(INVOICE_DOWNLOAD_MAX_ATTEMPTS_ENV, "12")))
    except ValueError:
        return 12


def _invoice_download_headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf,application/octet-stream,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": os.getenv(
            INVOICE_DOWNLOAD_USER_AGENT_ENV,
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ),
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _looks_like_pdf(sample: bytes, content_type: object) -> bool:
    return b"%PDF" in sample or "pdf" in str(content_type or "").lower()


def _decode_response_bytes(content: bytes, response: requests.Response | None = None) -> str:
    """Decode already-downloaded response bytes without touching response.content.

    The invoice downloader streams responses to a temp file first. After a streamed
    response has been consumed, requests.Response.apparent_encoding tries to read
    response.content again and raises "The content for this response was already
    consumed". Keep decoding based only on headers plus safe fallbacks.
    """

    encodings: list[str] = []
    if response is not None:
        if response.encoding:
            encodings.append(response.encoding)

        content_type = str(response.headers.get("Content-Type", "") or "")
        charset_match = re.search(r"charset=([A-Za-z0-9_.:-]+)", content_type, flags=re.IGNORECASE)
        if charset_match:
            encodings.append(charset_match.group(1))

    encodings.extend(["utf-8", "utf-8-sig", "latin-1"])

    tried: set[str] = set()
    for encoding in encodings:
        normalized = str(encoding or "").strip().lower()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        try:
            return content.decode(encoding, errors="replace")
        except LookupError:
            continue
    return content.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class InvoiceDownloadAttempt:
    """A concrete HTTP request to try while resolving an invoice PDF."""

    url: str
    method: str = "GET"
    data: tuple[tuple[str, str], ...] = ()
    referer: str | None = None

    def key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        normalized_url = self.url.split("#", 1)[0]
        return (self.method.upper(), normalized_url, self.data)

    def summary(self) -> str:
        method = self.method.upper()
        return self.url if method == "GET" else f"{method} {self.url}"


class _InvoiceHtmlParser(HTMLParser):
    """Extract links and ASP.NET form fields from a DocOpen HTML wrapper."""

    DOWNLOAD_ATTRIBUTES = {"href", "src", "data", "action", "formaction"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.forms: list[dict[str, Any]] = []
        self._current_form: dict[str, Any] | None = None

    @staticmethod
    def _attrs_to_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(name).lower(): "" if value is None else str(value) for name, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        attr_map = self._attrs_to_dict(attrs)

        if tag_name == "form":
            self._current_form = {
                "action": attr_map.get("action", ""),
                "method": attr_map.get("method", "post"),
                "fields": [],
            }
            self.forms.append(self._current_form)

        for attr_name in self.DOWNLOAD_ATTRIBUTES:
            value = attr_map.get(attr_name)
            if value:
                self.links.append(value)

        if tag_name == "meta" and attr_map.get("http-equiv", "").lower() == "refresh":
            content = attr_map.get("content", "")
            match = re.search(r"url\s*=\s*([^;]+)$", content, flags=re.IGNORECASE)
            if match:
                self.links.append(match.group(1).strip())

        if tag_name == "input" and self._current_form is not None:
            name = attr_map.get("name", "")
            if name:
                fields = self._current_form.setdefault("fields", [])
                fields.append((name, attr_map.get("value", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self._current_form = None


def _clean_invoice_candidate(candidate: str) -> str:
    cleaned = html_unescape(str(candidate or "")).strip()
    # JavaScript-generated attributes can arrive escaped as \"...\". Strip only
    # wrapping quote/backslash noise, not meaningful URL characters inside the value.
    for _ in range(3):
        stripped = cleaned.strip().strip('"\'')
        stripped = stripped.lstrip("\\").strip().strip('"\'')
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = cleaned.replace("&amp;", "&").strip()
    return cleaned


def _append_invoice_candidate(
    candidates: list[InvoiceDownloadAttempt],
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]],
    candidate: str,
    *,
    base_url: str,
    method: str = "GET",
    data: Iterable[tuple[str, str]] | Mapping[str, Any] | None = None,
    referer: str | None = None,
) -> None:
    cleaned = _clean_invoice_candidate(candidate)
    if not cleaned:
        return

    lower_cleaned = cleaned.lower()
    if lower_cleaned.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return

    # Avoid malformed values produced by regex scans over quoted JavaScript such as
    # '"javascript:__doPostBack(...)'. Those are browser actions, not URLs.
    if "javascript:" in lower_cleaned[:40]:
        return

    absolute_url = urljoin(base_url, cleaned).split("#", 1)[0]
    if not is_url(absolute_url):
        return

    if isinstance(data, Mapping):
        normalized_data = tuple((str(key), "" if value is None else str(value)) for key, value in data.items())
    else:
        normalized_data = tuple((str(key), "" if value is None else str(value)) for key, value in (data or ()))

    attempt = InvoiceDownloadAttempt(
        url=absolute_url,
        method=method.upper(),
        data=normalized_data,
        referer=referer,
    )
    key = attempt.key()
    if key not in seen:
        seen.add(key)
        candidates.append(attempt)


def _parse_invoice_html(html: str) -> tuple[str, _InvoiceHtmlParser]:
    text = html_unescape(html or "").replace("\\/", "/")
    parser = _InvoiceHtmlParser()
    try:
        parser.feed(text)
    except Exception:
        # HTMLParser is best-effort; keep regex fallbacks available even for bad HTML.
        pass
    return text, parser


def _aspnet_postback_attempts_from_html(
    text: str,
    parser: _InvoiceHtmlParser,
    base_url: str,
) -> list[InvoiceDownloadAttempt]:
    """Build POST attempts for ASP.NET WebForms __doPostBack download links."""

    postbacks: list[InvoiceDownloadAttempt] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    forms = parser.forms or [{"action": "", "method": "post", "fields": []}]

    for match in re.finditer(
        r"__doPostBack\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)",
        text,
        flags=re.IGNORECASE,
    ):
        event_target = html_unescape(match.group(1)).strip()
        event_argument = html_unescape(match.group(2)).strip()
        if not event_target:
            continue

        for form in forms:
            fields = [
                (str(name), "" if value is None else str(value))
                for name, value in form.get("fields", [])
                if str(name) not in {"__EVENTTARGET", "__EVENTARGUMENT"}
            ]
            fields.extend([
                ("__EVENTTARGET", event_target),
                ("__EVENTARGUMENT", event_argument),
            ])
            action = str(form.get("action") or "").strip() or base_url
            _append_invoice_candidate(
                postbacks,
                seen,
                action,
                base_url=base_url,
                method="POST",
                data=fields,
                referer=base_url,
            )

    return postbacks


def _invoice_candidates_from_html(html: str, base_url: str) -> list[InvoiceDownloadAttempt]:
    """Return browser-download targets embedded in a DocOpen.aspx HTML wrapper."""

    if not html:
        return []

    text, parser = _parse_invoice_html(html)
    candidates: list[InvoiceDownloadAttempt] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()

    # First emulate ASP.NET WebForms download links. DocOpen.aspx pages commonly
    # render a LinkButton whose href is javascript:__doPostBack(...); Chrome then
    # posts the hidden __VIEWSTATE/__EVENTVALIDATION fields back to DocOpen.aspx.
    for postback_attempt in _aspnet_postback_attempts_from_html(text, parser, base_url):
        key = postback_attempt.key()
        if key not in seen:
            seen.add(key)
            candidates.append(postback_attempt)

    # Then follow real URLs exposed in normal HTML attributes.
    for raw_link in parser.links:
        _append_invoice_candidate(
            candidates,
            seen,
            raw_link,
            base_url=base_url,
            referer=base_url,
        )

    # JavaScript redirects that Chrome follows but requests does not execute.
    redirect_patterns = [
        r"(?:window\.)?(?:location(?:\.href)?|document\.location)\s*=\s*['\"]([^'\"]+)['\"]",
        r"(?:window\.open|location\.assign|location\.replace)\s*\(\s*['\"]([^'\"]+)['\"]",
        r"http-equiv\s*=\s*['\"]?refresh['\"]?[^>]+content\s*=\s*['\"][^'\"]*?url=([^'\"]+)",
        r"content\s*=\s*['\"][^'\"]*?url=([^'\"]+)",
        r"(https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?)",
        # Relative PDF URLs with an explicit path marker. This intentionally does
        # not match a bare invoice filename inside a DocOpen.aspx?link=... query,
        # because the valid entry point is DocOpen.aspx, not /<filename>.pdf.
        r"((?:/|\./|\.\./)[A-Za-z0-9_./%=&?+-]+?\.pdf(?:\?[^\s\"'<>]*)?)",
    ]
    for pattern in redirect_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            raw_candidate = match.group(1).strip().rstrip(";,)>\"'")
            _append_invoice_candidate(
                candidates,
                seen,
                raw_candidate,
                base_url=base_url,
                referer=base_url,
            )

    parsed = urlparse(base_url)
    query_values = parse_qs(parsed.query)
    link_values = query_values.get("link") or query_values.get("filename") or query_values.get("file")
    if link_values:
        filename = str(link_values[0] or "").strip()
        if filename:
            quoted_filename = quote(filename, safe="/._-%")
            current_name = Path(parsed.path).name.lower()
            for replacement in (
                "DocDownload.aspx",
                "DocFile.aspx",
                "FileDownload.aspx",
                "Download.aspx",
                "DownloadFile.aspx",
                "GetFile.aspx",
            ):
                if current_name == replacement.lower():
                    continue
                candidate_url = parsed._replace(
                    path=str(Path(parsed.path).with_name(replacement)),
                    query=f"link={quoted_filename}",
                    fragment="",
                ).geturl()
                _append_invoice_candidate(
                    candidates,
                    seen,
                    candidate_url,
                    base_url=base_url,
                    referer=base_url,
                )

    return candidates


def _download_invoice_candidate(
    requester: requests.Session,
    attempt: InvoiceDownloadAttempt,
    output_path: Path,
) -> tuple[bool, str, str, list[InvoiceDownloadAttempt]]:
    """Try one HTTP request. Return (saved_pdf, content_type, final_url, html_candidates)."""

    response: requests.Response | None = None
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    sample = b""
    bytes_written = 0

    try:
        response = requester.request(
            attempt.method.upper(),
            attempt.url,
            data=list(attempt.data) if attempt.data else None,
            stream=True,
            timeout=_download_timeout(),
            allow_redirects=True,
            headers=_invoice_download_headers(attempt.referer),
        )
        response.raise_for_status()

        with temp_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                if len(sample) < 4096:
                    sample = (sample + chunk)[:4096]
                bytes_written += len(chunk)
                file.write(chunk)

        if bytes_written == 0:
            raise DocumentGenerationError(f"Downloaded invoice PDF was empty: {attempt.url}")

        content_type = str(response.headers.get("Content-Type", "") or "").lower()
        final_url = str(response.url or attempt.url)
        if _looks_like_pdf(sample, content_type):
            temp_path.replace(output_path)
            return True, content_type, final_url, []

        content = temp_path.read_bytes()
        html_candidates: list[InvoiceDownloadAttempt] = []
        if "html" in content_type or b"<html" in sample.lower() or b"<script" in sample.lower():
            html = _decode_response_bytes(content, response)
            html_candidates = _invoice_candidates_from_html(html, final_url)

        if temp_path.exists():
            temp_path.unlink()
        return False, content_type, final_url, html_candidates
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    finally:
        if response is not None:
            response.close()


def download_invoice_pdf(
    source_link: object,
    data_key: str,
    *,
    row: Mapping[str, Any] | None = None,
    session: requests.Session | None = None,
) -> str:
    """Download an invoice PDF link into generated_documents/invoice.

    Some DocOpen.aspx links return an HTML wrapper that Chrome executes before
    downloading the real PDF. This function follows regular redirects, emulates
    ASP.NET __doPostBack links, and follows PDF/redirect URLs found in that wrapper.
    """

    source = str(source_link or "").strip()
    if is_blank(source):
        raise DocumentGenerationError(f"Missing source link for {data_key}")

    if not is_url(source):
        if document_path_exists(source):
            return str(source)
        raise DocumentGenerationError(f"Invoice source is not a downloadable URL: {source}")

    output_path = _invoice_output_path(source, data_key, row)
    if output_path.exists() and output_path.is_file() and output_path.stat().st_size > 0:
        return output_path.as_posix()

    requester = session or requests.Session()
    initial_attempt = InvoiceDownloadAttempt(url=source, method="GET")
    candidates: list[InvoiceDownloadAttempt] = [initial_attempt]
    seen = {initial_attempt.key()}
    last_content_type = "unknown"
    last_url = source
    last_error = ""
    attempted: list[str] = []
    max_attempts = _download_max_attempts()

    try:
        while candidates and len(attempted) < max_attempts:
            attempt = candidates.pop(0)
            attempted.append(attempt.summary())
            try:
                saved, content_type, final_url, html_candidates = _download_invoice_candidate(
                    requester,
                    attempt,
                    output_path,
                )
            except Exception as exc:
                last_error = str(exc)
                last_url = attempt.url
                continue

            last_content_type = content_type or "unknown"
            last_url = final_url or attempt.url
            if saved:
                return output_path.as_posix()

            for html_candidate in html_candidates:
                key = html_candidate.key()
                if key not in seen:
                    seen.add(key)
                    candidates.append(html_candidate)

        attempted_summary = ", ".join(attempted[:4])
        if len(attempted) > 4:
            attempted_summary += f", ... ({len(attempted)} total)"
        error_detail = f"; last error: {last_error}" if last_error else ""
        raise DocumentGenerationError(
            "Downloaded invoice content did not resolve to a PDF "
            f"(last Content-Type: {last_content_type}; last URL: {last_url}; "
            f"attempted: {attempted_summary}; max attempts: {max_attempts}{error_detail})"
        )
    finally:
        if session is None and isinstance(requester, requests.Session):
            requester.close()

def document_reference(path: str | Path | None) -> str:
    if not path:
        return PLACEHOLDER

    text = str(path).strip()
    if not text:
        return PLACEHOLDER

    if re.match(r"^[a-z][a-z0-9+.-]*://", text, flags=re.IGNORECASE):
        href = text
        parsed = urlparse(text)
        query_values = parse_qs(parsed.query)
        display_name = ""
        for query_key in ("link", "filename", "file", "name"):
            values = query_values.get(query_key)
            if values:
                display_name = values[0]
                break
        if not display_name:
            display_name = Path(unquote(parsed.path)).name or text
        display_name = unquote(str(display_name or "")).replace("\\", "/").rsplit("/", 1)[-1]
        display_path = Path(display_name or text)
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

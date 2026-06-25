"""
response_generator.py

Deterministic draft-response generator.
Reads requested_data from output/request_intent_results.jsonl.gz, builds draft responses,
and appends the final rows to the BigQuery history table through ticket_fetcher.

No LLM is used here. The dictionary-detected request_language column
produced by intent_detection.py is used to choose the response language.

The templates and safety branches are intentionally aligned with
prompts/response_builder.md.
"""

import os
import re

import pandas as pd

from response_data_extractor import (
    DOCUMENT_RESPONSE_COLUMNS,
    DocumentGenerationError,
    FULL_ORDER_DATA_KEYS,
    FULL_ORDER_LOOKUP_KEYS,
    FULL_ORDER_RESPONSE_COLUMNS,
    INVOICE_DOCUMENT_DATA_KEYS,
    GetFullOrderClient,
    document_path_exists,
    document_value_for_response,
    fetch_shipment_order_data,
    generate_authorization_letter,
    generate_documents_for_dataframe,
    generate_power_of_attorney,
    tracking_number_for_documents,
    ups_account_number_for_documents,
)

from ticket_fetcher import (
    append_history_rows,
    new_history_request_ids,
    submit_final_responses,
    zendesk_response_submission_enabled,
)
from pipeline_io import REQUEST_INTENT_RESULTS_PATH, build_request_id, read_dataframe
from customs_rules import (
    FEDEX_BROKERAGE_EMAIL,
    HUMAN_INTERVENTION_REQUIRED,
    PLACEHOLDER,
    UNKNOWN_REQUEST,
    UPS_BROKERAGE_EMAIL,
    detect_language_with_dictionary,
    extract_ups_code,
    first_available_value,
    is_customer_refused_return_request,
    is_returns_customs_clearance,
    normalize_email,
    normalize_language,
    normalize_request_number,
    normalize_requested_data,
)

INPUT_PATH = REQUEST_INTENT_RESULTS_PATH
AUTO_ADD_UPS_MIN_CONFIDENCE = float(os.getenv("AUTO_ADD_UPS_MIN_CONFIDENCE", "0.90"))

DHL_BROKERAGE_EMAIL = os.getenv("DHL_BROKERAGE_EMAIL", "kamil.it@dhl.com")
SUPPRESSED_RESPONSE_DATA_KEYS = {"previously_requested_documentation"}

FULL_ORDER_SHIPPED_AT_COLUMN = FULL_ORDER_RESPONSE_COLUMNS["shipped_at"]
FULL_ORDER_API_ERROR_COLUMN = FULL_ORDER_RESPONSE_COLUMNS["api_error"]
DOCUMENT_GENERATION_ERROR_COLUMN = DOCUMENT_RESPONSE_COLUMNS["document_error"]

# Keep this local to response generation so the generator remains safe even if
# upstream regex/LLM output still contains older aliases.
REQUESTED_DATA_ALIASES = {
    "ups_account": "ups_account_number",
    "ups_account_code": "ups_account_number",
    "ups_code": "ups_account_number",
    "invoice": "commercial_invoice",
    "commercial_invoice_required": "commercial_invoice",
    "invoice_correction": "corrected_invoice",
    "declaration_of_intent": "dichiarazione_di_libera_esportazione",
}

DOCUMENT_EMBEDDED_REQUESTED_DATA = {
    "tax_information",
    "country_of_origin",
    "product_description",
}

RPI_DOCUMENT_EMBEDDED_REQUESTED_DATA = {
    "customs_description",
    "importer_details",
}

FIRST_REQUEST_RPI_RESPONSE_EMBEDDED_REQUESTED_DATA = {
    "corrected_invoice",
    "invoice_correction",
    "value_confirmation",
}

RPI_EMBEDDED_CONTACT_REQUESTED_DATA = {
    "shipping_address",
    "customer_email",
    "customer_phone",
}

FIRST_REQUEST_COMMERCIAL_INVOICE_EMBEDDED_REQUESTED_DATA = {
    "dichiarazione_di_libera_esportazione",
}

FIRST_REQUEST_IGNORED_REQUESTED_DATA = {
    "eori_number",
}

FIRST_REQUEST_SHIPMENT_INSTRUCTIONS_REQUESTED_DATA = {
    "shipment_instructions",
}

ALWAYS_HUMAN_INTERVENTION_REQUESTED_DATA = {
    "address_translation",
    "exporter_ein",
    "address_correction",
}

AFTER_FIRST_REQUEST_HUMAN_INTERVENTION_REQUESTED_DATA = (
    FIRST_REQUEST_RPI_RESPONSE_EMBEDDED_REQUESTED_DATA
    | FIRST_REQUEST_COMMERCIAL_INVOICE_EMBEDDED_REQUESTED_DATA
    | FIRST_REQUEST_IGNORED_REQUESTED_DATA
    | FIRST_REQUEST_SHIPMENT_INSTRUCTIONS_REQUESTED_DATA
)

DOCUMENT_ATTACHMENT_DATA_KEYS = set(INVOICE_DOCUMENT_DATA_KEYS) | {
    "authorization_letter",
    "power_of_attorney",
}

RETURN_PROFORMA_CONTEXT_RE = re.compile(
    r"\b(?:rpi|pri|return\s+proforma|return\s+invoice|fattura\s+(?:di\s+)?reso|"
    r"proforma\s+(?:di\s+)?reso|reintroduzione\s+in\s+franchigia|reso|rientr)\b",
    re.IGNORECASE,
)

PLATFORM_HANDOFF_RE = re.compile(
    r"(?:"
    r"(?:siete\s+pregati|vi\s+preghiamo|la\s+preghiamo|please|kindly)"
    r"[\s\S]{0,160}"
    r"(?:inviar(?:ci|e)|fornir(?:ci|e)|inserire|caricare|upload|send|provide)"
    r"[\s\S]{0,160}"
    r"(?:informazioni|istruzioni|information|instructions)"
    r"[\s\S]{0,160}"
    r"(?:portale\s+)?fedex\s+support\s+hub|"
    r"(?:portale\s+)?fedex\s+support\s+hub"
    r")",
    re.IGNORECASE,
)

UNPAID_EXTRA_CHARGES_RE = re.compile(
    r"(?:"
    r"(?:customer|consignee|receiver|destinatario|cliente)"
    r"[\s\S]{0,120}"
    r"(?:did\s+not\s+pay|didn(?:'|’)?t\s+pay|has\s+not\s+paid|not\s+paid|"
    r"non\s+ha\s+(?:ancora\s+)?pagato|non\s+paga|mancato\s+pagamento|pagamento\s+mancante)"
    r"[\s\S]{0,120}"
    r"(?:extra\s+charges?|outstanding\s+charges?|additional\s+charges?|charges?|"
    r"oneri|costi|spese|supplementi|dazi|diritti|addebiti)|"
    r"(?:extra\s+charges?|outstanding\s+charges?|additional\s+charges?|charges?|"
    r"oneri|costi|spese|supplementi|dazi|diritti|addebiti)"
    r"[\s\S]{0,120}"
    r"(?:did\s+not\s+pay|didn(?:'|’)?t\s+pay|has\s+not\s+paid|not\s+paid|"
    r"non\s+ha\s+(?:ancora\s+)?pagato|non\s+paga|mancato\s+pagamento|pagamento\s+mancante)"
    r")",
    re.IGNORECASE,
)

SDOGANAMENTO_RE = re.compile(r"\bsdoganamento\b", re.IGNORECASE)

DATA_LABELS = {
    "en": {
        "commercial_invoice": "Commercial invoice",
        "return_proforma_invoice": "RPI",
        "corrected_invoice": "Corrected invoice",
        "export_tracking_number": "Export TRK",
        "ups_account_number": "UPS code",
        "value_confirmation": "Value confirmation",
        "returned_items_confirmation": "Items returned",
        "customs_description": "Customs description",
        "dichiarazione_di_libera_esportazione": "Dichiarazione di libera esportazione",
        "eori_number": "EORI number",
        "power_of_attorney": "Power of attorney",
        "importer_details": "Importer details",
        "address_translation": "Address translation",
        "exporter_ein": "Exporter EIN",
        "customer_phone": "Customer phone number",
        "customer_email": "Customer email address",
        "customer_name": "Customer full name",
        "shipping_address": "Shipping address",
        "authorization_letter": "Authorization letter",
        "shipment_instructions": "Shipment instructions",
        "address_correction": "Address correction",
        "previously_requested_documentation": "Previously requested documentation",
    },
    "it": {
        "commercial_invoice": "Fattura commerciale",
        "return_proforma_invoice": "RPI",
        "corrected_invoice": "Fattura corretta",
        "export_tracking_number": "TRK in export",
        "ups_account_number": "Cod UPS",
        "value_confirmation": "Conferma valore",
        "returned_items_confirmation": "Prodotti resi",
        "customs_description": "Descrizione merce",
        "dichiarazione_di_libera_esportazione": "Dichiarazione di libera esportazione",
        "eori_number": "Numero EORI",
        "power_of_attorney": "Procura / delega",
        "importer_details": "Dati importatore",
        "address_translation": "Traduzione indirizzo",
        "exporter_ein": "EIN esportatore",
        "customer_phone": "Numero di telefono",
        "customer_email": "Indirizzo email",
        "customer_name": "Nome completo",
        "shipping_address": "Indirizzo di spedizione",
        "authorization_letter": "Lettera di autorizzazione",
        "shipment_instructions": "Istruzioni di spedizione",
        "address_correction": "Correzione indirizzo",
        "previously_requested_documentation": "Documentazione precedentemente richiesta",
    },
}


def is_blank(value):
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() in {"", "nan", "none", "n/a", "na", "<na>"}


def as_bool(value):
    if isinstance(value, bool):
        return value
    if is_blank(value):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def safe_float(value, default=0.0):
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_text(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def row_confidence(row):
    for column in ("regex_confidence", "llm_confidence", "confidence"):
        value = row.get(column)
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        if value is not None:
            return safe_float(value, 0.0)
    return 0.0


def human_reason(row, default):
    notes = str(row.get("notes", "") or "").strip()
    return notes or default


def row_language(row):
    raw_language = row.get("request_language", "")
    if not is_blank(raw_language):
        normalized = normalize_language(raw_language, default="")
        if normalized in {"it", "en"}:
            return normalized

    return detect_language_with_dictionary(
        row.get("subject", ""),
        row.get("cleaned_request_body", ""),
        row.get("request_body", ""),
    )


def label_for(data_key, language):
    labels = DATA_LABELS.get(language, DATA_LABELS["en"])
    return labels.get(data_key, data_key.replace("_", " ").title())


def full_order_column_for_data_key(data_key):
    return FULL_ORDER_RESPONSE_COLUMNS.get(data_key)


def full_order_data_value(row, data_key):
    column = full_order_column_for_data_key(data_key)
    if not column:
        return PLACEHOLDER

    if data_key == "returned_items_confirmation":
        value = row.get(column)
        return PLACEHOLDER if is_blank(value) else str(value).rstrip()

    if data_key in INVOICE_DOCUMENT_DATA_KEYS:
        generated_column = DOCUMENT_RESPONSE_COLUMNS.get(data_key)
        generated_path = row.get(generated_column) if generated_column else None
        if not is_blank(generated_path):
            text_path = str(generated_path).strip()
            if document_path_exists(text_path):
                return document_value_for_response(text_path)

        source_link = row.get(column)
        if not is_blank(source_link):
            return document_value_for_response(str(source_link).strip())

    return first_available_value(row.get(column), default=PLACEHOLDER)


def generated_document_value(row, data_key):
    column = DOCUMENT_RESPONSE_COLUMNS.get(data_key)
    existing_path = row.get(column) if column else None
    if column and not is_blank(existing_path):
        text_path = str(existing_path).strip()
        if document_path_exists(text_path):
            return document_value_for_response(text_path)
        print(
            "WARNING: Stored generated document path is not a local file; "
            f"regenerating {data_key} for request_id={row.get('request_id')}: {text_path}"
        )

    try:
        if data_key == "authorization_letter":
            return document_value_for_response(generate_authorization_letter(row))
        if data_key == "power_of_attorney":
            return document_value_for_response(generate_power_of_attorney(row))
    except DocumentGenerationError as exc:
        print(
            "WARNING: Could not generate "
            f"{data_key} for request_id={row.get('request_id')}: {exc}"
        )
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        print(
            "WARNING: Unexpected document-generation error for "
            f"{data_key} request_id={row.get('request_id')}: {exc}"
        )

    return PLACEHOLDER


def data_value(row, data_key):
    if data_key == "export_tracking_number":
        return first_available_value(
            row.get("shipment_tracking_number"),
            default=PLACEHOLDER,
        )

    if data_key == "ups_account_number":
        ups_code = extract_ups_code(
            row.get("shipment_tracking_number"),
            row.get("extracted_tracking_number"),
            row.get("return_tracking_number"),
        )
        return ups_code or PLACEHOLDER

    if data_key in FULL_ORDER_DATA_KEYS:
        return full_order_data_value(row, data_key)

    if data_key in {"authorization_letter", "power_of_attorney"}:
        return generated_document_value(row, data_key)

    return PLACEHOLDER


def export_tracking_value(row):
    """Return the non-return/export tracking number from shipment enrichment."""
    return first_available_value(
        row.get("shipment_tracking_number"),
        default=PLACEHOLDER,
    )


def export_tracking_or_unavailable(row, unavailable_text):
    value = export_tracking_value(row)
    if is_blank(value) or value == PLACEHOLDER:
        return unavailable_text
    return value


def normalize_carrier_code_value(value):
    if is_blank(value):
        return ""
    return re.sub(r"\s+", "", str(value).strip().upper())


def return_export_carriers_are_same(row):
    """
    Return True/False when the return and export rows were found in
    shipping_platform_shipments and both carrier_code values are available.
    Return None when the order/carrier relationship could not be determined.
    """
    if is_blank(row.get("shipment_order_number")):
        return None

    export_carrier = normalize_carrier_code_value(row.get("shipment_carrier_code"))
    return_carrier = normalize_carrier_code_value(row.get("return_carrier_code"))

    if not export_carrier or not return_carrier:
        return None

    return export_carrier == return_carrier


def shipment_order_number_starts_with_sc(row):
    for column in ("shipment_order_number", "shipmentOrderNumber"):
        value = row.get(column)
        if not is_blank(value) and str(value).strip().upper().startswith("SC"):
            return True
    return False


def is_tracking_lookup_not_found(row):
    engine = str(row.get("engine", "") or "").strip().lower()
    return (
        as_bool(row.get("tracking_not_found_in_shipping_platform_shipments"))
        or engine == "llm_tracking_lookup_not_found_guard"
    )


def llm_human_intervention_draft(row):
    return normalize_text(row.get("llm_human_intervention_draft_response", ""))


def normalize_data_key(value):
    value = str(value or "").strip()
    return REQUESTED_DATA_ALIASES.get(value, value)


def normalize_requested_data_with_aliases(value):
    cleaned = []
    for item in normalize_requested_data(value):
        item = normalize_data_key(item)
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def row_request_text(row):
    return normalize_text(
        "\n".join(
            str(value or "")
            for value in [
                row.get("subject", ""),
                row.get("cleaned_request_body", ""),
                row.get("request_body", ""),
            ]
            if not is_blank(value)
        )
    )


def row_request_body_text(row):
    return normalize_text(
        "\n".join(
            str(value or "")
            for value in [
                row.get("cleaned_request_body", ""),
                row.get("request_body", ""),
            ]
            if not is_blank(value)
        )
    )


def request_body_mentions_sdoganamento(row):
    return bool(SDOGANAMENTO_RE.search(row_request_body_text(row)))


def is_platform_handoff_request(text):
    return bool(PLATFORM_HANDOFF_RE.search(normalize_text(text)))


def is_unpaid_extra_charges_request(text):
    return bool(UNPAID_EXTRA_CHARGES_RE.search(normalize_text(text)))


def email_domain(email):
    normalized = normalize_email(email)
    if "@" not in normalized:
        return ""
    return normalized.rsplit("@", 1)[-1]


def is_ups_requester_email(email):
    normalized = normalize_email(email)
    domain = email_domain(email)
    return normalized == UPS_BROKERAGE_EMAIL or domain == "ups.com" or domain.endswith(".ups.com")


def is_fedex_requester_email(email):
    normalized = normalize_email(email)
    domain = email_domain(email)
    return normalized == FEDEX_BROKERAGE_EMAIL or "fedex" in domain


def is_dhl_requester_email(email):
    normalized = normalize_email(email)
    domain = email_domain(email)
    return normalized == DHL_BROKERAGE_EMAIL or domain == "dhl.com" or domain.endswith(".dhl.com")


def is_request_number_3_or_higher(request_number):
    return normalize_request_number(request_number) >= 3


def is_first_returns_customs_request(ticket_category, request_number):
    return is_returns_customs_clearance(ticket_category) and normalize_request_number(request_number) == 1


def has_return_proforma_context(text):
    return bool(RETURN_PROFORMA_CONTEXT_RE.search(normalize_text(text)))


def all_requested_data_sources(row):
    values = []
    for column in [
        "requested_data",
        "regex_requested_data",
        "regex_request_types",
        "request_types",
        "standard_reply_requested_data",
    ]:
        for item in normalize_requested_data_with_aliases(row.get(column)):
            if item not in values:
                values.append(item)
    return values


def has_embedded_rpi_contact_fields(row):
    return bool(set(all_requested_data_sources(row)) & RPI_EMBEDDED_CONTACT_REQUESTED_DATA)


def collapse_embedded_document_fields(row, requested_data):
    """Collapse old standalone fields into invoice/RPI document requests."""
    text = row_request_text(row)
    first_returns = is_first_returns_customs_request(
        row.get("ticket_category"),
        row.get("request_number", 1),
    )
    first_request = normalize_request_number(row.get("request_number", 1)) == 1
    requester_email = row.get("requester_email")

    result = []
    embedded_document_fields_found = False
    contact_fields_found = False

    for data_key in requested_data:
        if data_key in RPI_DOCUMENT_EMBEDDED_REQUESTED_DATA:
            if "return_proforma_invoice" not in result:
                result.append("return_proforma_invoice")
            continue
        if first_request and data_key in FIRST_REQUEST_RPI_RESPONSE_EMBEDDED_REQUESTED_DATA:
            if "return_proforma_invoice" not in result:
                result.append("return_proforma_invoice")
            continue
        if first_request and data_key in FIRST_REQUEST_COMMERCIAL_INVOICE_EMBEDDED_REQUESTED_DATA:
            if "commercial_invoice" not in result:
                result.append("commercial_invoice")
            continue
        if first_request and data_key in FIRST_REQUEST_IGNORED_REQUESTED_DATA:
            continue
        if first_request and data_key in FIRST_REQUEST_SHIPMENT_INSTRUCTIONS_REQUESTED_DATA:
            for embedded_key in ("ups_account_number", "export_tracking_number"):
                if embedded_key not in result:
                    result.append(embedded_key)
            continue
        if data_key in DOCUMENT_EMBEDDED_REQUESTED_DATA:
            embedded_document_fields_found = True
            continue
        if data_key in RPI_EMBEDDED_CONTACT_REQUESTED_DATA:
            contact_fields_found = True
            # In first Returns Customs Clearance flows these fields are covered
            # by the RPI package, not answered separately.
            if first_returns:
                continue
        if data_key not in result:
            result.append(data_key)

    if embedded_document_fields_found:
        target = (
            "return_proforma_invoice"
            if first_returns or has_return_proforma_context(text)
            else "commercial_invoice"
        )
        if target not in result:
            result.append(target)

    if first_returns and contact_fields_found and "return_proforma_invoice" not in result:
        result.append("return_proforma_invoice")

    if first_returns and "return_proforma_invoice" in result:
        result = [
            data_key
            for data_key in result
            if data_key not in RPI_EMBEDDED_CONTACT_REQUESTED_DATA
        ]

    return result


def requested_data_for_response(row):
    request_text = row_request_text(row)

    if is_customer_refused_return_request(request_text):
        return ["ups_account_number"]

    # Unpaid extra/outstanding charges require a human decision about who must
    # pay.  Do not auto-authorize charges to the UPS account.
    if is_unpaid_extra_charges_request(request_text):
        return [HUMAN_INTERVENTION_REQUIRED]

    requested_data = normalize_requested_data_with_aliases(row.get("requested_data"))
    requested_data = collapse_embedded_document_fields(row, requested_data)
    requested_data = [
        data_key
        for data_key in requested_data
        if data_key not in SUPPRESSED_RESPONSE_DATA_KEYS
    ]

    if (
        request_body_mentions_sdoganamento(row)
        and "export_tracking_number" not in requested_data
        and HUMAN_INTERVENTION_REQUIRED not in requested_data
        and UNKNOWN_REQUEST not in requested_data
    ):
        requested_data.append("export_tracking_number")

    # Historical false positives showed that auto-adding UPS account to every
    # Returns Customs Clearance row can amplify weak/incorrect intent matches.
    # Keep this fallback only for high-confidence non-first UPS requests.
    if (
        is_returns_customs_clearance(row.get("ticket_category"))
        and normalize_request_number(row.get("request_number", 1)) != 1
        and is_ups_requester_email(row.get("requester_email"))
        and requested_data
        and row_confidence(row) >= AUTO_ADD_UPS_MIN_CONFIDENCE
        and HUMAN_INTERVENTION_REQUIRED not in requested_data
        and UNKNOWN_REQUEST not in requested_data
        and "ups_account_number" not in requested_data
    ):
        requested_data.append("ups_account_number")

    return requested_data


def row_needs_full_order_lookup(row, requested_data=None):
    requested_data = requested_data if requested_data is not None else requested_data_for_response(row)
    requested_set = set(requested_data)

    if requested_set & FULL_ORDER_LOOKUP_KEYS:
        return True

    # The standard UPS-account response promises an LOA, and the LOA export date
    # comes from GET_FULL_ORDER.shipments[].shippedAt.
    return requested_data == ["ups_account_number"] and is_ups_requester_email(row.get("requester_email"))


def _initialize_full_order_columns(df):
    for column in FULL_ORDER_RESPONSE_COLUMNS.values():
        if column not in df.columns:
            df[column] = None
    return df


def row_has_full_order_attempt(row):
    return any(not is_blank(row.get(column)) for column in FULL_ORDER_RESPONSE_COLUMNS.values())


def enrich_with_full_order_data(df):
    if df.empty:
        return df

    df = _initialize_full_order_columns(df.copy())
    requested_data_by_index = {}

    for index, row in df.iterrows():
        requested_data = requested_data_for_response(row)
        requested_data_by_index[index] = requested_data

    lookup_indices = [
        index
        for index, row in df.iterrows()
        if row_needs_full_order_lookup(row, requested_data_by_index[index])
        and not row_has_full_order_attempt(row)
    ]

    if not lookup_indices:
        return df

    try:
        client = GetFullOrderClient()
    except Exception as exc:
        print(f"WARNING: GET_FULL_ORDER client could not be configured: {exc}")
        for index in lookup_indices:
            df.at[index, FULL_ORDER_API_ERROR_COLUMN] = str(exc)
        return df

    if not client.is_configured:
        message = "Missing GET_FULL_ORDER_API_CREDENTIALS"
        print(f"WARNING: {message}; API-backed response data will be unavailable.")
        for index in lookup_indices:
            df.at[index, FULL_ORDER_API_ERROR_COLUMN] = message
        return df

    cache = {}

    for index in lookup_indices:
        shipment_order_number = df.at[index, "shipment_order_number"] if "shipment_order_number" in df.columns else None

        if is_blank(shipment_order_number):
            df.at[index, FULL_ORDER_API_ERROR_COLUMN] = "Missing shipment_order_number for GET_FULL_ORDER lookup"
            continue

        cache_key = str(shipment_order_number).strip().upper()

        if cache_key not in cache:
            try:
                cache[cache_key] = fetch_shipment_order_data(
                    shipment_order_number,
                    client=client,
                )
            except Exception as exc:
                print(
                    "WARNING: GET_FULL_ORDER lookup failed for "
                    f"shipment_order_number={shipment_order_number}: {exc}"
                )
                cache[cache_key] = {FULL_ORDER_API_ERROR_COLUMN: str(exc)}

        for column, value in cache[cache_key].items():
            if column not in df.columns:
                df[column] = None
            df.at[index, column] = value

    return df


def generated_document_is_ready(row, data_key):
    column = DOCUMENT_RESPONSE_COLUMNS.get(data_key)
    if not column:
        return False

    value = row.get(column)
    if is_blank(value):
        return False

    # A generated document is ready for attachment upload only when a local PDF
    # exists. Invoice source links can still be used as public fallback links.
    return document_path_exists(str(value).strip())


def invoice_document_is_sendable(row, data_key):
    """Return True when an invoice/RPI can be sent as an attachment or link."""

    if generated_document_is_ready(row, data_key):
        return True

    source_column = full_order_column_for_data_key(data_key)
    return bool(source_column and not is_blank(row.get(source_column)))


def missing_required_full_order_values(row, requested_data):
    missing = []

    for data_key in sorted(set(requested_data) & FULL_ORDER_DATA_KEYS):
        column = full_order_column_for_data_key(data_key)
        if column and is_blank(row.get(column)):
            missing.append(data_key)

    needs_loa = "authorization_letter" in requested_data or (
        requested_data == ["ups_account_number"]
        and is_ups_requester_email(row.get("requester_email"))
    )
    needs_poa = "power_of_attorney" in requested_data

    for invoice_key in sorted(set(requested_data) & INVOICE_DOCUMENT_DATA_KEYS):
        if not invoice_document_is_sendable(row, invoice_key) and invoice_key not in missing:
            missing.append(invoice_key)

    if needs_loa or needs_poa:
        if is_blank(tracking_number_for_documents(row)):
            missing.append("document_tracking_number")

    if needs_loa:
        if is_blank(ups_account_number_for_documents(row)):
            missing.append("authorization_letter_ups_account_number")
        if is_blank(row.get(FULL_ORDER_SHIPPED_AT_COLUMN)):
            missing.append("authorization_letter_export_date")
        if not generated_document_is_ready(row, "authorization_letter"):
            missing.append("authorization_letter_pdf")

    if needs_poa and not generated_document_is_ready(row, "power_of_attorney"):
        missing.append("power_of_attorney_pdf")

    return missing


def build_human_intervention_note(row, reason):
    ticket_id = row.get("zendesk_ticket_id", "")
    request_number = row.get("request_number", "")
    draft = llm_human_intervention_draft(row) if is_tracking_lookup_not_found(row) else ""

    if draft:
        return (
            "HUMAN INTERVENTION REQUIRED\n\n"
            "Do not send this draft automatically. A human must review the ticket "
            "because the extracted tracking number was not found in "
            "tlg-business-intelligence-prd.bi.shipping_platform_shipments.\n\n"
            "LLM-drafted response for human review:\n\n"
            f"{draft}\n\n"
            "---\n"
            f"Reason: {reason}\n"
            f"Ticket: {ticket_id}\n"
            f"Request number: {request_number}"
        )

    return (
        "HUMAN INTERVENTION REQUIRED\n\n"
        "Do not send an automatic reply for this request.\n"
        f"Reason: {reason}\n"
        f"Ticket: {ticket_id}\n"
        f"Request number: {request_number}"
    )


def build_power_of_attorney_only_response(row, language):
    value = data_value(row, "power_of_attorney")

    if language == "it":
        return (
            "Buongiorno,\n\n"
            "In allegato invio la documentazione richiesta:\n"
            f"Procura / delega: {value}\n\n"
            "Cordiali saluti,"
        )

    return (
        "Hello,\n\n"
        "Please find attached the requested documents:\n"
        f"Power of attorney: {value}\n\n"
        "Best regards,"
    )


def build_generic_response(row, requested_data, language):
    lines = []

    for data_key in requested_data:
        label = label_for(data_key, language)
        value = data_value(row, data_key)
        if "\n" in str(value):
            lines.append(f"- {label}:\n{value}")
        else:
            lines.append(f"- {label}: {value}")

    body = "\n".join(lines)

    if language == "it":
        return (
            "Buongiorno,\n\n"
            "Grazie per il vostro messaggio.\n\n"
            "Di seguito le informazioni richieste:\n\n"
            f"{body}\n\n"
            "Cordiali saluti,"
        )

    return (
        "Hi,\n\n"
        "Thank you for your message.\n\n"
        "Please find below the requested information:\n\n"
        f"{body}\n\n"
        "Kind regards,"
    )


def build_ups_extra_charges_response(row):
    ups_account = data_value(row, "ups_account_number")
    loa = data_value(row, "authorization_letter")

    return (
        "Response 1:\n"
        "Hello,\n\n"
        f"Please, debit the outstanding charges to our UPS account {ups_account}, "
        "authorized by Piero Trevisan and proceed with the delivery.\n\n"
        "Best regards,\n\n"
        "Piero T.\n\n"
        "Response 2:\n"
        "Hello,\n\n"
        "I confirm you the return of shipment on topic.\n"
        f"Debit all the relative costs to our UPS account {ups_account}, authorized by Piero T.\n"
        f"LOA: {loa}\n\n"
        "Best regards\n\n"
        "Piero T."
    )


def build_ups_account_standard_response(row):
    ups_account = data_value(row, "ups_account_number")
    loa = data_value(row, "authorization_letter")

    return (
        "Hello,\n\n"
        "I confirm you the return of shipment on topic.\n"
        f"Debit all the relative costs to our UPS account {ups_account}, authorized by Piero T.\n"
        f"LOA: {loa}\n\n"
        "Best regards\n\n"
        "Piero T."
    )


def returned_items_section(row, language="it", fallback=True):
    if "returned_items_confirmation" in requested_data_for_response(row):
        value = data_value(row, "returned_items_confirmation")
        if not is_blank(value) and value != PLACEHOLDER:
            label = label_for("returned_items_confirmation", language)
            if "\n" in str(value):
                return f"{label}:\n{value}\n\n"
            return f"{label}: {value}\n\n"

    if fallback and language == "it":
        return "Tutti prodotti sono stati resi.\n\n"
    if fallback:
        return "All products have been returned.\n\n"
    return ""


def build_ups_returns_same_carrier_response(row):
    export_tracking = export_tracking_value(row)
    ups_account = data_value(row, "ups_account_number")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Buongiorno,\n\n"
        "In allegato la documentazione per la reintroduzione in franchigia:\n\n"
        f"- TRK in export: {export_tracking}\n"
        f"- Cod UPS: {ups_account}\n"
        f"- Return Proforma Invoice: {rpi}\n\n"
        f"{returned_items_section(row)}"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_ups_returns_different_carrier_response(row):
    export_tracking = export_tracking_or_unavailable(row, "non disponibile, avvenuto con altro vettore")
    ups_account = data_value(row, "ups_account_number")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Buongiorno,\n\n"
        "Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.\n\n"
        f"- TRK in export: {export_tracking}\n"
        f"- Cod UPS: {ups_account}\n"
        f"- Return Proforma Invoice: {rpi}\n\n"
        f"{returned_items_section(row)}"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_ups_returns_first_rpi_account_response(row):
    carrier_match = return_export_carriers_are_same(row)

    if carrier_match is True:
        return build_ups_returns_same_carrier_response(row)

    if carrier_match is False:
        return build_ups_returns_different_carrier_response(row)

    return build_human_intervention_note(
        row,
        "Carrier-code comparison between export and return shipments could not be determined.",
    )


def build_fedex_returns_same_carrier_response(row):
    awb_export = export_tracking_value(row)
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Buongiorno,\n\n"
        "In allegato invio la documentazione richiesta.\n\n"
        f"AWB in export: {awb_export}\n"
        f"RPI: {rpi}\n"
        f"{returned_items_section(row, fallback=False)}"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_dhl_returns_same_carrier_response(row):
    awb_export = export_tracking_value(row)
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Buongiorno,\n\n"
        "In allegato la documentazione richiesta per la reintroduzione in franchigia.\n"
        f"AWB in export: {awb_export}\n"
        f"RPI: {rpi}\n"
        f"{returned_items_section(row, fallback=False)}"
        "Cordiali saluti,\n"
        "Piero T."
    )


def build_fedex_dhl_returns_different_carrier_response(row):
    awb_export = export_tracking_or_unavailable(row, "non disponibile, avvenuto con altro vettore")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Buongiorno,\n\n"
        "Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.\n\n"
        f"AWB in export: {awb_export}\n"
        f"RPI: {rpi}\n\n"
        f"{returned_items_section(row)}"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_fedex_dhl_returns_first_rpi_contact_response(row, carrier="fedex"):
    carrier_match = return_export_carriers_are_same(row)
    carrier = str(carrier or "fedex").strip().lower()

    if carrier_match is True:
        if carrier == "dhl":
            return build_dhl_returns_same_carrier_response(row)
        return build_fedex_returns_same_carrier_response(row)

    if carrier_match is False:
        return build_fedex_dhl_returns_different_carrier_response(row)

    return build_human_intervention_note(
        row,
        "Carrier-code comparison between export and return shipments could not be determined.",
    )


def build_dhl_returns_first_rpi_response(row):
    return build_fedex_dhl_returns_first_rpi_contact_response(row, carrier="dhl")


def _sorted_data_keys_for_reason(data_keys):
    return ", ".join(sorted(str(data_key) for data_key in data_keys if data_key))


def human_intervention_data_policy_reason(row, requested_data):
    first_request = normalize_request_number(row.get("request_number", 1)) == 1
    raw_requested_set = set(all_requested_data_sources(row))
    response_requested_set = set(requested_data)
    combined_requested_set = raw_requested_set | response_requested_set

    always_human = combined_requested_set & ALWAYS_HUMAN_INTERVENTION_REQUESTED_DATA
    if always_human:
        return (
            "Human intervention is required for requested_data: "
            + _sorted_data_keys_for_reason(always_human)
            + "."
        )

    after_first_request_human = (
        combined_requested_set & AFTER_FIRST_REQUEST_HUMAN_INTERVENTION_REQUESTED_DATA
    )
    if not first_request and after_first_request_human:
        return (
            "Human intervention is required because the following requested_data "
            "is only auto-handled on request number 1: "
            + _sorted_data_keys_for_reason(after_first_request_human)
            + "."
        )

    return ""


def should_force_human_intervention(row, requested_data):
    request_text = row_request_text(row)
    raw_requested_data = normalize_requested_data_with_aliases(row.get("requested_data"))

    if is_request_number_3_or_higher(row.get("request_number")):
        return True, "Request number is 3 or higher. Human intervention is required by automation policy."

    if shipment_order_number_starts_with_sc(row):
        return True, "shipmentOrderNumber starts with SC. Human intervention is required by automation policy."

    if is_platform_handoff_request(request_text):
        return True, "FedEx Support Hub handoff request. A human must handle the external portal."

    if is_tracking_lookup_not_found(row):
        return (
            True,
            "Tracking number extracted from the ticket was not found in "
            "tlg-business-intelligence-prd.bi.shipping_platform_shipments.",
        )

    if is_unpaid_extra_charges_request(request_text):
        return (
            True,
            "Customer did not pay extra/outstanding charges. Human intervention "
            "is required to verify whether the customer or TLG should pay.",
        )

    data_policy_reason = human_intervention_data_policy_reason(row, requested_data)
    if data_policy_reason:
        return True, data_policy_reason

    missing_full_order_values = missing_required_full_order_values(row, requested_data)
    if missing_full_order_values:
        api_error = str(row.get(FULL_ORDER_API_ERROR_COLUMN) or "").strip()
        reason = (
            "GET_FULL_ORDER API data is missing for: "
            + ", ".join(missing_full_order_values)
            + "."
        )
        if api_error:
            reason += f" API detail: {api_error}"
        document_error = str(row.get(DOCUMENT_GENERATION_ERROR_COLUMN) or "").strip()
        if document_error:
            reason += f" Document detail: {document_error}"
        return True, reason

    first_returns_request = is_first_returns_customs_request(
        row.get("ticket_category"),
        row.get("request_number", 1),
    )
    requested_set = set(requested_data)
    returns_reply_needs_carrier_decision = False

    if first_returns_request and is_ups_requester_email(row.get("requester_email")):
        returns_reply_needs_carrier_decision = {
            "ups_account_number",
            "return_proforma_invoice",
        }.issubset(requested_set)

    if first_returns_request and is_fedex_requester_email(row.get("requester_email")):
        returns_reply_needs_carrier_decision = (
            "return_proforma_invoice" in requested_set
            and has_embedded_rpi_contact_fields(row)
        )

    if first_returns_request and is_dhl_requester_email(row.get("requester_email")):
        returns_reply_needs_carrier_decision = "return_proforma_invoice" in requested_set

    if returns_reply_needs_carrier_decision and return_export_carriers_are_same(row) is None:
        return (
            True,
            "Carrier-code comparison between the export and return rows in "
            "tlg-business-intelligence-prd.bi.shipping_platform_shipments could "
            "not be determined. Human intervention is required instead of "
            "drafting multiple alternatives.",
        )

    if (
        as_bool(row.get("human_intervention_required"))
        or HUMAN_INTERVENTION_REQUIRED in raw_requested_data
        or HUMAN_INTERVENTION_REQUIRED in requested_data
    ):
        return True, human_reason(
            row,
            "The requested data could not be identified with enough certainty.",
        )

    if raw_requested_data == [UNKNOWN_REQUEST] or requested_data == [UNKNOWN_REQUEST]:
        return True, human_reason(
            row,
            "The requested data could not be identified with enough certainty.",
        )

    return False, ""


def build_response(row):
    requester_email = normalize_email(row.get("requester_email"))
    request_number = row.get("request_number", 1)
    request_text = row_request_text(row)
    requested_data = requested_data_for_response(row)

    force_human, reason = should_force_human_intervention(row, requested_data)
    if force_human:
        return build_human_intervention_note(row, reason)

    if not requested_data:
        return ""

    first_returns_request = is_first_returns_customs_request(
        row.get("ticket_category"),
        request_number,
    )

    if first_returns_request and is_ups_requester_email(requester_email):
        if {"ups_account_number", "return_proforma_invoice"}.issubset(set(requested_data)):
            return build_ups_returns_first_rpi_account_response(row)

    if first_returns_request and is_fedex_requester_email(requester_email):
        if "return_proforma_invoice" in requested_data and has_embedded_rpi_contact_fields(row):
            return build_fedex_dhl_returns_first_rpi_contact_response(row, carrier="fedex")

    if first_returns_request and is_dhl_requester_email(requester_email):
        if "return_proforma_invoice" in requested_data and has_embedded_rpi_contact_fields(row):
            return build_fedex_dhl_returns_first_rpi_contact_response(row, carrier="dhl")
        if "return_proforma_invoice" in requested_data:
            return build_dhl_returns_first_rpi_response(row)

    if requested_data == ["ups_account_number"]:
        if is_unpaid_extra_charges_request(request_text):
            return build_ups_extra_charges_response(row)
        return build_ups_account_standard_response(row)

    language = row_language(row)

    if requested_data == ["power_of_attorney"]:
        return build_power_of_attorney_only_response(row, language)

    return build_generic_response(row, requested_data, language)


def requires_human_intervention(row):
    requested_data = requested_data_for_response(row)
    force_human, _ = should_force_human_intervention(row, requested_data)
    return force_human


def document_data_keys_for_response(row, requested_data=None):
    requested_data = requested_data if requested_data is not None else requested_data_for_response(row)
    document_keys = [
        data_key
        for data_key in requested_data
        if data_key in DOCUMENT_ATTACHMENT_DATA_KEYS
    ]

    # The standard UPS-account reply includes the generated LOA even when the
    # requested_data row only contains ups_account_number.
    if requested_data == ["ups_account_number"] and is_ups_requester_email(row.get("requester_email")):
        if "authorization_letter" not in document_keys:
            document_keys.append("authorization_letter")

    return document_keys


def document_attachment_paths_for_response(row, requested_data=None):
    paths = []
    for data_key in document_data_keys_for_response(row, requested_data):
        column = DOCUMENT_RESPONSE_COLUMNS.get(data_key)
        if not column:
            continue
        value = row.get(column)
        if is_blank(value):
            continue
        text_value = str(value).strip()
        if document_path_exists(text_value) and text_value not in paths:
            paths.append(text_value)
    return paths


def _document_display_values_for_response(row, requested_data=None, *, attached_only=False):
    values = []
    for data_key in document_data_keys_for_response(row, requested_data):
        if attached_only and not generated_document_is_ready(row, data_key):
            continue
        value = data_value(row, data_key)
        if is_blank(value) or value == PLACEHOLDER:
            continue
        text_value = str(value).strip()
        if text_value and text_value not in values:
            values.append(text_value)
    return values


def _remove_document_values_from_response(response_text, document_values):
    sanitized = str(response_text or "")

    for value in sorted(document_values, key=len, reverse=True):
        sanitized = sanitized.replace(f": {value}", "")
        sanitized = sanitized.replace(f":\n{value}", "")
        sanitized = sanitized.replace(value, "")

    if document_values:
        # Safety fallback for attached generated-document references that may
        # have been formatted in plain style instead of markdown style. Keep
        # source invoice links when no local attachment is being uploaded.
        sanitized = re.sub(
            r":\s*\[[^\]\n]+?\.pdf\]\([^\)\n]*generated_documents/[^\)\n]+?\.pdf\)",
            "",
            sanitized,
            flags=re.IGNORECASE,
        )
        sanitized = re.sub(
            r":\s*(?:https?://\S*generated_documents/\S+|generated_documents/\S+|/[^\s:]+/generated_documents/\S+)\.pdf",
            "",
            sanitized,
            flags=re.IGNORECASE,
        )

    lines = [re.sub(r"[ \t]+$", "", line) for line in sanitized.split("\n")]
    return normalize_text("\n".join(lines))


def build_final_response(row):
    if requires_human_intervention(row):
        return ""

    draft_response = row.get("draft_response")
    if is_blank(draft_response):
        draft_response = build_response(row)

    if is_blank(draft_response):
        return ""

    requested_data = requested_data_for_response(row)
    document_values = _document_display_values_for_response(
        row,
        requested_data,
        attached_only=True,
    )
    return _remove_document_values_from_response(draft_response, document_values)


def _row_request_id(row):
    request_id = row.get("request_id")
    if not is_blank(request_id):
        return str(request_id).strip()
    return build_request_id(
        row.get("zendesk_ticket_id"),
        row.get("request_number"),
    ) or ""


def _rows_for_inserted_request_ids(df, inserted_request_ids):
    inserted_request_ids = {str(request_id) for request_id in inserted_request_ids if request_id}
    if not inserted_request_ids:
        return df.iloc[0:0].copy()
    result = df.copy()
    result["request_id"] = result.apply(_row_request_id, axis=1)
    return result[result["request_id"].isin(inserted_request_ids)].copy()


def main():
    df = read_dataframe(INPUT_PATH)

    if df.empty:
        print("No request intent rows found. Nothing to log to BigQuery history.")
        return

    df = enrich_with_full_order_data(df)
    df = generate_documents_for_dataframe(df)

    if "request_language" not in df.columns:
        df["request_language"] = df.apply(row_language, axis=1)
    else:
        df["request_language"] = df.apply(row_language, axis=1)

    df["draft_response"] = df.apply(build_response, axis=1)
    df["human_intervention_required"] = df.apply(requires_human_intervention, axis=1)
    df["final_response"] = df.apply(build_final_response, axis=1)
    df["zendesk_attachment_paths"] = df.apply(
        lambda row: document_attachment_paths_for_response(
            row,
            requested_data_for_response(row),
        ),
        axis=1,
    )

    zendesk_submission_enabled = zendesk_response_submission_enabled()
    print(f"Zendesk final-response submission enabled: {zendesk_submission_enabled}")

    new_request_ids = new_history_request_ids(df)
    rows_to_log = _rows_for_inserted_request_ids(df, new_request_ids)

    logged_rows, inserted_request_ids = append_history_rows(
        rows_to_log,
        return_inserted_request_ids=True,
    )

    # Submission is controlled only by SUBMIT_ZENDESK_RESPONSES.  We evaluate all
    # current rows, not only newly inserted history rows, so a dry run with the
    # flag set to false can be followed by a true run that submits the same
    # final_response values.  The duplicate-comment guard in ticket_fetcher keeps
    # retries safe when the exact public response already exists on the ticket.
    zendesk_submitted = submit_final_responses(df)

    print(
        f"Generated draft responses for {len(df)} rows. "
        f"Logged {logged_rows} new rows. "
        f"Submitted {zendesk_submitted} Zendesk response(s). "
        f"Zendesk submission enabled: {zendesk_submission_enabled}. "
        f"Inserted request_ids: {inserted_request_ids}"
    )


if __name__ == "__main__":
    main()

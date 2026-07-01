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
    align_invoice_requested_data_with_tracking_context,
    detect_language_with_dictionary,
    contains_correction_or_discrepancy,
    extract_ups_code,
    expand_first_returns_customs_clearance_bundle,
    filter_libera_esportazione_response_data,
    first_available_value,
    has_libera_esportazione_regex_type,
    is_atr_certificate_mandate_request,
    is_customer_refused_return_request,
    is_missing_extracted_tracking_number,
    is_no_action_carrier_notification,
    is_noreply_requester_email,
    is_returns_customs_clearance,
    is_unpaid_extra_charges_request as rules_is_unpaid_extra_charges_request,
    is_ups_uk_import_clearance_instructions_request,
    normalize_email,
    normalize_language,
    normalize_request_number,
    normalize_requested_data,
    only_libera_esportazione_regex_type,
    only_libera_esportazione_requested_data,
    strip_libera_esportazione_requested_data,
)

INPUT_PATH = REQUEST_INTENT_RESULTS_PATH
AUTO_ADD_UPS_MIN_CONFIDENCE = float(os.getenv("AUTO_ADD_UPS_MIN_CONFIDENCE", "0.90"))
NOREPLY_REQUEST_EXCERPT_MAX_CHARS = int(os.getenv("NOREPLY_REQUEST_EXCERPT_MAX_CHARS", "1200"))

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
    "tracking_number": "export_tracking_number",
    "export_tracking": "export_tracking_number",
    "returned_items": "returned_items_confirmation",
    "rpi": "return_proforma_invoice",
    "pri": "return_proforma_invoice",
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

FIRST_REQUEST_COMMERCIAL_INVOICE_EMBEDDED_REQUESTED_DATA = set()

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


# ---------------------------------------------------------------------------
# Zendesk "reason of contact" taxonomy
#
# This mirrors the live Zendesk ticket field options (custom_field 23910471),
# exported as filtered_ticket_fields.csv: "EA::Corrieri::<CARRIER>::<...>".
# response_generator.py only needs to compute *which* taxonomy value applies
# to a given draft response; ticket_fetcher.submit_ticket_response resolves
# that display name to the field's option id at submission time.
# ---------------------------------------------------------------------------

DEFAULT_REASON_OF_CONTACT = "Altro"

CARRIER_REASON_PREFIX = {
    "ups": "EA::Corrieri::UPS",
    "dhl": "EA::Corrieri::DHL",
    "fedex": "EA::Corrieri::FEDEX",
}

REASON_FATTURE_REQUESTED_DATA = {
    "commercial_invoice",
    "return_proforma_invoice",
    "corrected_invoice",
}
REASON_GIACENZE_CONTACT_REQUESTED_DATA = {
    "customer_phone",
    "customer_email",
    "customer_name",
}
REASON_GIACENZE_DELIVERY_ADDRESS_REQUESTED_DATA = {
    "shipping_address",
    "address_correction",
    "address_translation",
}
REASON_GIACENZE_FREE_EXPORT_DECLARATION_REQUESTED_DATA = {
    "dichiarazione_di_libera_esportazione",
}
REASON_GIACENZE_RETURNS_REQUESTED_DATA = {
    "returned_items_confirmation",
}
REASON_SDOGANAMENTO_REQUESTED_DATA = {
    "ups_account_number",
    "export_tracking_number",
    "power_of_attorney",
    "authorization_letter",
    "eori_number",
}


def carrier_reason_prefix(row):
    requester_email = row.get("requester_email")
    if is_ups_requester_email(requester_email):
        return CARRIER_REASON_PREFIX["ups"]
    if is_dhl_requester_email(requester_email):
        return CARRIER_REASON_PREFIX["dhl"]
    if is_fedex_requester_email(requester_email):
        return CARRIER_REASON_PREFIX["fedex"]
    return ""


def reason_of_contact_for_response(row, requested_data):
    """Return the Zendesk "reason of contact" taxonomy value for this row.

    The mapping mirrors filtered_ticket_fields.csv: each carrier (UPS/DHL/
    FedEx) has Fatture, Giacenze::* and Sdoganamento branches. When the
    carrier cannot be determined from the requester email, or none of the
    requested_data keys match a known branch, fall back to "Altro" (the
    pre-existing default reason of contact).
    """

    requested_set = set(requested_data or [])
    prefix = carrier_reason_prefix(row)
    if not prefix:
        return DEFAULT_REASON_OF_CONTACT

    if requested_set & REASON_GIACENZE_FREE_EXPORT_DECLARATION_REQUESTED_DATA:
        return f"{prefix}::Giacenze::Free Export Declaration"

    if requested_set & REASON_SDOGANAMENTO_REQUESTED_DATA:
        carrier_match = return_export_carriers_are_same(row)
        if carrier_match is False:
            return f"{prefix}::Sdoganamento - Definitiva"
        return f"{prefix}::Sdoganamento"

    if requested_set & REASON_GIACENZE_RETURNS_REQUESTED_DATA:
        return f"{prefix}::Giacenze::Returns"

    if requested_set & REASON_GIACENZE_CONTACT_REQUESTED_DATA:
        return f"{prefix}::Giacenze::Contact Details"

    if requested_set & REASON_GIACENZE_DELIVERY_ADDRESS_REQUESTED_DATA:
        return f"{prefix}::Giacenze::Delivery Address"

    if requested_set & REASON_FATTURE_REQUESTED_DATA:
        return f"{prefix}::Fatture"

    return DEFAULT_REASON_OF_CONTACT


def zendesk_country_tag_for_row(row) -> str | None:
    """Return the Zendesk country-field tag for this row, e.g. 'country_dj_us'.

    The tag is constructed from:
      - the brand prefix extracted from the shipment_order_number
        (e.g. 'DG-USC11593083' -> brand 'DG')
      - the ISO 3166-1 alpha-2 country code from the GET_FULL_ORDER
        shippingAddress.country field (stored in full_order_country_code)

    Returns None when either piece is missing, so callers can safely skip
    setting the Zendesk field.
    """
    from customs_rules import country_ticket_field_id_for_brand
    from response_data_extractor import brand_from_shipment_order_number, FULL_ORDER_RESPONSE_COLUMNS

    # Safely get the country code column name, defaulting to "full_order_country_code"
    country_col = FULL_ORDER_RESPONSE_COLUMNS.get("country_code", "full_order_country_code")
    country_code = row.get(country_col)
    
    if is_blank(country_code):
        return None

    brand = brand_from_shipment_order_number(row.get("shipment_order_number"))
    if is_blank(brand):
        return None

    # Only produce a tag when this brand actually has a country field in Zendesk
    if country_ticket_field_id_for_brand(brand) is None:
        return None

    return f"country_{brand.lower()}_{str(country_code).strip().lower()}"


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


def truncated_text(value, max_chars):
    text = normalize_text(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def unique_nonempty_strings(values):
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in {"nan", "none", "null"} and text not in result:
            result.append(text)
    return result


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

        # Commercial invoice may contain multiple generated documents.
        if data_key == "commercial_invoice":
            values = []

            generated = row.get("generated_commercial_invoice")
            if generated:
                for item in str(generated).split(";"):
                    item = item.strip()
                    if item and item not in values:
                        values.append(item)

            source = full_order_data_value(row, data_key)
            if source:
                for item in str(source).split(";"):
                    item = item.strip()
                    if item and item not in values:
                        values.append(item)

            return "\n".join(values)

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


def row_has_request_type(row, request_type):
    wanted = str(request_type or "").strip()
    if not wanted:
        return False
    for column in ("request_types", "regex_request_types"):
        if wanted in normalize_requested_data(row.get(column)):
            return True
    return False


def is_excluded_from_processing(row):
    if as_bool(row.get("excluded")):
        return True

    if (
        not is_noreply_requester_email(row.get("requester_email"))
        and is_no_action_carrier_notification(row_request_text(row))
    ):
        return True

    requested_data = normalize_requested_data(row.get("requested_data"))
    request_types = normalize_requested_data(row.get("request_types"))
    return (
        "exclude_from_processing" in request_types
        and not requested_data
        and not as_bool(row.get("human_intervention_required"))
    )


def build_exclude_from_processing_draft(row):
    notes = normalize_text(row.get("notes", ""))
    if is_no_action_carrier_notification(row_request_text(row)):
        notes = (
            "Carrier notification/status message only; no actionable customer-data "
            "request was detected."
        )
    if not notes:
        matched_spans = normalize_text(row.get("matched_spans", ""))
        if matched_spans:
            notes = f"Matched exclude_from_processing regex evidence: {matched_spans[:500]}"
    if not notes:
        notes = "Regex classified this message as exclude_from_processing."

    return (
        "Excluded from processing - no Zendesk reply should be sent. "
        f"Reason: {notes}"
    )


def detected_request_summary(row, requested_data=None):
    language = row_language(row)
    requested_data = requested_data if requested_data is not None else requested_data_for_response(row)

    data_keys = [
        key
        for key in requested_data
        if key not in {UNKNOWN_REQUEST, HUMAN_INTERVENTION_REQUIRED}
    ]
    if not data_keys:
        data_keys = [
            key
            for key in all_requested_data_sources(row)
            if key not in {UNKNOWN_REQUEST, HUMAN_INTERVENTION_REQUIRED, "exclude_from_processing"}
        ]

    no_action_notification = is_no_action_carrier_notification(row_request_text(row))
    if no_action_notification:
        data_keys = []

    lines = []

    subject = truncated_text(row.get("subject", ""), 220)
    if subject:
        lines.append(f"- Subject: {subject}")

    refs = unique_nonempty_strings(
        [
            row.get("extracted_tracking_number"),
            row.get("shipment_tracking_number"),
            row.get("return_tracking_number"),
            row.get("shipment_order_number"),
            row.get("shipmentOrderNumber"),
        ]
    )
    if refs:
        lines.append("- Tracking/order reference(s): " + ", ".join(refs))

    if no_action_notification:
        lines.append(
            "- Carrier notification/status: no actionable data request detected; "
            "no public reply expected."
        )
    elif data_keys:
        labels = unique_nonempty_strings(label_for(key, language) for key in data_keys)
        if labels:
            lines.append("- Detected requested data: " + ", ".join(labels))

    request_types = unique_nonempty_strings(
        item
        for column in ("regex_request_types", "request_types")
        for item in normalize_requested_data(row.get(column))
        if item not in {UNKNOWN_REQUEST, HUMAN_INTERVENTION_REQUIRED, "exclude_from_processing"}
    )
    if request_types and not no_action_notification:
        lines.append("- Detected request type(s): " + ", ".join(request_types))

    notes = truncated_text(row.get("notes", ""), 500)
    if notes:
        lines.append(f"- Detection notes: {notes}")

    excerpt = truncated_text(
        first_available_value(
            row.get("cleaned_request_body"),
            row.get("request_body"),
            default="",
        ),
        NOREPLY_REQUEST_EXCERPT_MAX_CHARS,
    )
    if excerpt:
        lines.append("- Request excerpt:\n" + excerpt)

    if not lines:
        lines.append("- The request could not be summarized automatically; review the Zendesk ticket manually.")

    return "\n".join(lines)


def build_noreply_human_intervention_note(row, requested_data=None):
    ticket_id = row.get("zendesk_ticket_id", "")
    request_number = row.get("request_number", "")
    requester = normalize_email(row.get("requester_email"))
    summary = detected_request_summary(row, requested_data)

    return (
        "HUMAN INTERVENTION REQUIRED\n\n"
        "Do not send an automatic Zendesk reply. The requester email starts "
        "with noreply, so no public ticket response should be made by the agent.\n\n"
        "What the request appears to be about:\n"
        f"{summary}\n\n"
        "---\n"
        "Reason: requester_email starts with noreply; human intervention is required.\n"
        f"Requester: {requester}\n"
        f"Ticket: {ticket_id}\n"
        f"Request number: {request_number}"
    )


def llm_human_intervention_draft(row):
    return normalize_text(row.get("llm_human_intervention_draft_response", ""))


def llm_model_used_for_draft(row):
    if not as_bool(row.get("llm_was_used")):
        return ""

    for column in ("llm_model_used", "gemini_model_used", "model_used"):
        value = row.get(column)
        if not is_blank(value):
            return str(value).strip()

    return "unknown"


def llm_model_attempts_for_draft(row):
    if not as_bool(row.get("llm_was_used")):
        return ""

    for column in ("llm_model_attempts", "gemini_model_attempts", "model_attempts"):
        value = row.get(column)
        if not is_blank(value):
            return str(value).strip()

    return ""


def add_llm_model_note_to_draft(row, draft_response):
    model_used = llm_model_used_for_draft(row)
    if not model_used:
        return normalize_text(draft_response)

    draft_response = normalize_text(draft_response)
    model_note = f"LLM analysis model: {model_used}"
    model_attempts = llm_model_attempts_for_draft(row)
    if model_attempts and model_attempts != model_used:
        model_note += f" (attempts: {model_attempts})"
    model_note += "."

    if (
        draft_response.startswith(model_note)
        or "LLM model used:" in draft_response[:300]
        or "LLM analysis model:" in draft_response[:300]
    ):
        return draft_response

    if draft_response:
        return normalize_text(f"{model_note}\n\n{draft_response}")

    return model_note


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
    return (
        rules_is_unpaid_extra_charges_request(text)
        or bool(UNPAID_EXTRA_CHARGES_RE.search(normalize_text(text)))
    )


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
    return _apply_response_requested_data_overrides(row, values)


def _request_type_sources_for_tracking_alignment(row):
    values = []
    for column in ["regex_request_types", "regex_requested_data", "request_types"]:
        for item in normalize_requested_data_with_aliases(row.get(column)):
            if item not in values:
                values.append(item)
    return values


def _apply_response_requested_data_overrides(row, requested_data):
    values = filter_libera_esportazione_response_data(
        requested_data,
        row.get("regex_request_types"),
    )
    values = align_invoice_requested_data_with_tracking_context(
        values,
        _request_type_sources_for_tracking_alignment(row),
        extracted_tracking_number=row.get("extracted_tracking_number"),
        shipment_tracking_number=row.get("shipment_tracking_number"),
        return_tracking_number=row.get("return_tracking_number"),
    )
    if is_atr_certificate_mandate_request(row_request_text(row)):
        values = [value for value in values if value != "power_of_attorney"]
    return values


def regex_requested_data_sources(row):
    values = []
    for column in ["regex_requested_data", "regex_request_types"]:
        for item in normalize_requested_data_with_aliases(row.get(column)):
            if item not in values:
                values.append(item)
    return _apply_response_requested_data_overrides(row, values)


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
    libera_regex_detected = has_libera_esportazione_regex_type(row.get("regex_request_types"))

    if only_libera_esportazione_regex_type(row.get("regex_request_types")):
        return []

    if is_customer_refused_return_request(request_text):
        return ["ups_account_number"]

    # Unpaid extra/outstanding charges require a human decision about who must
    # pay.  Do not auto-authorize charges to the UPS account.
    if is_unpaid_extra_charges_request(request_text):
        return [HUMAN_INTERVENTION_REQUIRED]

    requested_data = normalize_requested_data_with_aliases(row.get("requested_data"))
    if only_libera_esportazione_requested_data(requested_data):
        return []
    requested_data = strip_libera_esportazione_requested_data(requested_data)
    requested_data = collapse_embedded_document_fields(row, requested_data)
    requested_data = expand_first_returns_customs_clearance_bundle(
        requested_data,
        ticket_category=row.get("ticket_category"),
        request_number=row.get("request_number", 1),
        requester_email=row.get("requester_email"),
        trigger_requested_data=regex_requested_data_sources(row) or requested_data,
    )
    requested_data = _apply_response_requested_data_overrides(row, requested_data)
    requested_data = [
        data_key
        for data_key in requested_data
        if data_key not in SUPPRESSED_RESPONSE_DATA_KEYS
    ]

    if (
        not libera_regex_detected
        and request_body_mentions_sdoganamento(row)
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
    if is_noreply_requester_email(row.get("requester_email")):
        return False

    request_text = row_request_text(row)
    if is_no_action_carrier_notification(request_text):
        return False
    if is_ups_uk_import_clearance_instructions_request(request_text):
        return False

    requested_data = requested_data if requested_data is not None else requested_data_for_response(row)
    requested_set = set(requested_data)

    if requested_set & FULL_ORDER_LOOKUP_KEYS:
        return True

    # Always attempt the lookup for any row with a shipment_order_number that
    # is still eligible for an automated reply, even when requested_data does
    # not otherwise need GET_FULL_ORDER data. This is required so the
    # per-brand "Country <BRAND>" Zendesk ticket field can be populated on
    # every automated response (Zendesk requires it when present on the
    # ticket, even when not flagged mandatory).
    if not is_blank(row.get("shipment_order_number")) and requested_data:
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

    # A generated/downloaded document is ready for Zendesk upload only when a
    # local PDF exists. Source invoice/RPI URLs from GET_FULL_ORDER must remain
    # internal-only and must not be used as public-response fallbacks.
    return document_path_exists(str(value).strip())


def invoice_document_is_sendable(row, data_key):
    """Return True only when an invoice/RPI can be attached to Zendesk."""

    return generated_document_is_ready(row, data_key)


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


def _document_reference_candidates(value):
    if is_blank(value) or value == PLACEHOLDER:
        return []

    text_value = str(value).strip()
    if not text_value:
        return []

    candidates = [text_value]
    rendered = document_value_for_response(text_value)
    if rendered and rendered not in candidates and rendered != PLACEHOLDER:
        candidates.append(rendered)
    return candidates


def document_references_for_reviewer(row, requested_data=None):
    """Return document links/paths that are safe for internal draft review only."""

    references = []
    language = row_language(row)

    for data_key in document_data_keys_for_response(row, requested_data):
        label = label_for(data_key, language)
        columns = []

        generated_column = DOCUMENT_RESPONSE_COLUMNS.get(data_key)
        if generated_column:
            columns.append(generated_column)

        source_column = full_order_column_for_data_key(data_key)
        if source_column and source_column not in columns:
            columns.append(source_column)

        for column in columns:
            for candidate in _document_reference_candidates(row.get(column)):
                item = (label, candidate)
                if item not in references:
                    references.append(item)

    return references


def format_reviewer_document_references(row, requested_data=None):
    references = document_references_for_reviewer(row, requested_data)
    if not references:
        return ""

    lines = ["Document references for internal review only:"]
    for label, value in references:
        lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def build_human_intervention_note(row, reason):
    ticket_id = row.get("zendesk_ticket_id", "")
    request_number = row.get("request_number", "")

    if is_missing_extracted_tracking_number(row.get("extracted_tracking_number")):
        return (
            "HUMAN INTERVENTION REQUIRED\n\n"
            "Do not send an automatic reply for this request.\n"
            "Reason: the tracking number was not found in the ticket, so "
            "the request was not analyzed by regex or LLM.\n"
            f"Ticket: {ticket_id}\n"
            f"Request number: {request_number}"
        )

    draft = llm_human_intervention_draft(row) if is_tracking_lookup_not_found(row) else ""
    reviewer_document_references = format_reviewer_document_references(row)
    reviewer_document_section = (
        f"\n\n{reviewer_document_references}" if reviewer_document_references else ""
    )

    if draft:
        return (
            "HUMAN INTERVENTION REQUIRED\n\n"
            "Do not send this draft automatically. A human must review the ticket "
            "because the extracted tracking number was not found in "
            "tlg-business-intelligence-prd.bi.shipping_platform_shipments.\n\n"
            "LLM-drafted response for human review:\n\n"
            f"{draft}"
            f"{reviewer_document_section}\n\n"
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
        f"{reviewer_document_section}"
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
    document_requested = bool(set(requested_data) & DOCUMENT_ATTACHMENT_DATA_KEYS)
    information_requested = any(
        data_key not in DOCUMENT_ATTACHMENT_DATA_KEYS for data_key in requested_data
    )

    if language == "it":
        if document_requested and information_requested:
            intro = "Di seguito le informazioni richieste e in allegato la documentazione:"
        elif document_requested:
            intro = "In allegato invio la documentazione richiesta:"
        else:
            intro = "Di seguito le informazioni richieste:"

        return (
            "Buongiorno,\n\n"
            "Grazie per il vostro messaggio.\n\n"
            f"{intro}\n\n"
            f"{body}\n\n"
            "Cordiali saluti,"
        )

    if document_requested and information_requested:
        intro = "Please find below the requested information and attached documents:"
    elif document_requested:
        intro = "Please find attached the requested documents:"
    else:
        intro = "Please find below the requested information:"

    return (
        "Hi,\n\n"
        "Thank you for your message.\n\n"
        f"{intro}\n\n"
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
    raw_requested_data_unfiltered = normalize_requested_data_with_aliases(row.get("requested_data"))
    raw_source_values_unfiltered = []
    for column in [
        "requested_data",
        "regex_requested_data",
        "regex_request_types",
        "request_types",
        "standard_reply_requested_data",
    ]:
        for item in normalize_requested_data_with_aliases(row.get(column)):
            if item not in raw_source_values_unfiltered:
                raw_source_values_unfiltered.append(item)
    raw_requested_data = _apply_response_requested_data_overrides(
        row,
        raw_requested_data_unfiltered,
    )

    if only_libera_esportazione_regex_type(row.get("regex_request_types")):
        return False, ""

    if is_atr_certificate_mandate_request(request_text) and "power_of_attorney" in set(raw_source_values_unfiltered):
        return (
            True,
            "ATR certificate mandate/form request is not a power_of_attorney automation case. Human intervention is required.",
        )

    if is_noreply_requester_email(row.get("requester_email")):
        return (
            True,
            "Requester email starts with noreply. No automatic Zendesk reply "
            "should be sent; human intervention is required.",
        )

    if is_request_number_3_or_higher(row.get("request_number")):
        return True, "Request number is 3 or higher. Human intervention is required by automation policy."

    if shipment_order_number_starts_with_sc(row):
        return True, "shipmentOrderNumber starts with SC. Human intervention is required by automation policy."

    if is_platform_handoff_request(request_text):
        return True, "FedEx Support Hub handoff request. A human must handle the external portal."

    if is_ups_uk_import_clearance_instructions_request(request_text):
        return (
            True,
            "UPS UK import-clearance instruction request asks for customs procedure, "
            "EORI/DAN/deferment approval, commodity details, or possible extra charges. "
            "Human intervention is required instead of sending a partial RPI/UPS-account reply.",
        )

    if contains_correction_or_discrepancy(request_text) and (
        {
            "corrected_invoice",
            "invoice_correction",
            "value_confirmation",
            "commercial_invoice",
            "return_proforma_invoice",
        }
        & set(all_requested_data_sources(row))
    ):
        return (
            True,
            "Correction/discrepancy language detected for invoice/value data. "
            "Manual verification is required before replying.",
        )

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
        api_error_value = row.get(FULL_ORDER_API_ERROR_COLUMN)
        api_error = "" if is_blank(api_error_value) else str(api_error_value).strip()
        reason = (
            "GET_FULL_ORDER API data is missing for: "
            + ", ".join(missing_full_order_values)
            + "."
        )
        if api_error:
            reason += f" API detail: {api_error}"
        document_error_value = row.get(DOCUMENT_GENERATION_ERROR_COLUMN)
        document_error = "" if is_blank(document_error_value) else str(document_error_value).strip()
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

    # UPS account numbers should only be sent to UPS.
    if (
        "ups_account_number" in requested_data
        and not is_ups_requester_email(requester_email)
    ):
        requested_data = [
            item
            for item in requested_data
            if item != "ups_account_number"
        ]

    if is_missing_extracted_tracking_number(row.get("extracted_tracking_number")):
        return build_human_intervention_note(
            row,
            "The tracking number was not found in the ticket. The request was not analyzed.",
        )

    if is_noreply_requester_email(requester_email):
        return build_noreply_human_intervention_note(row, requested_data)

    if is_excluded_from_processing(row):
    return build_exclude_from_processing_draft(row)

    # Carrier acknowledgement only (e.g. "Thank you, invoice has been uploaded")
    # Sometimes the parser extracts "customer_phone" from the sender signature.
    # Those emails should never receive an automatic response.
    if (
        "thank you" in request_text.lower()
        and "uploaded" in request_text.lower()
        and requested_data == ["customer_phone"]
    ):
        return build_exclude_from_processing_draft(row)

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

    # NEW LOGIC: Dynamic Order Customs Clearance response for single & multiple shipments
    from customs_rules import ORDER_CUSTOMS_CLEARANCE
    if row.get("ticket_category") == ORDER_CUSTOMS_CLEARANCE and "return_proforma_invoice" in requested_data:
        trackings_raw = str(row.get("extracted_tracking_number", ""))
        
        if ";" in trackings_raw:
            tracking_list = trackings_raw.split(";")
            rpi_lines = []
            for i, trk in enumerate(tracking_list, start=1):
                rpi_lines.append(f"RPI for shipment {i} ({trk})")
            rpi_display = " - ".join(rpi_lines)
        else:
            rpi_display = "RPI"
            
        if language == "it":
            return f"Buongiorno,\n\nGrazie per il messaggio.\nIn allegato i documenti richiesti:\n{rpi_display}\n\nCordiali saluti,"
        else:
            return f"Hi,\n\nThank you for your message.\nPlease find attached the requested documents:\n{rpi_display}\n\nKind regards,"

    if requested_data == ["power_of_attorney"]:
        return build_power_of_attorney_only_response(row, language)

    return build_generic_response(row, requested_data, language)


def requires_human_intervention(row):
    if (
        not is_noreply_requester_email(row.get("requester_email"))
        and is_excluded_from_processing(row)
    ):
        return False

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
    if is_noreply_requester_email(row.get("requester_email")):
        return []

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

        # Add the value exactly as it appears in the draft response.
        value = data_value(row, data_key)
        for candidate in _document_reference_candidates(value):
            if candidate not in values:
                values.append(candidate)

        # Also add raw/rendered generated paths and GET_FULL_ORDER source links.
        # This makes final_response safe even when an older draft contains a
        # source invoice/RPI link while a later run has a local attachment, or
        # vice versa.
        columns = []
        generated_column = DOCUMENT_RESPONSE_COLUMNS.get(data_key)
        if generated_column:
            columns.append(generated_column)
        source_column = full_order_column_for_data_key(data_key)
        if source_column and source_column not in columns:
            columns.append(source_column)

        for column in columns:
            if attached_only and column != generated_column:
                continue
            for candidate in _document_reference_candidates(row.get(column)):
                if candidate not in values:
                    values.append(candidate)

    return values


def strip_internal_draft_metadata_from_public_response(text):
    sanitized = str(text or "")
    sanitized = re.sub(
        r"\A\s*LLM (?:model used|analysis model):\s*[^\n]*(?:\n\s*\n)?",
        "",
        sanitized,
        flags=re.IGNORECASE,
    )
    return normalize_text(sanitized)


# Lines carrying a returned-item image link (built by
# response_data_extractor.format_returned_items) must keep their URL in the
# public response. Every other link in the draft is a document reference
# (invoice/RPI/LOA/POA) that is uploaded as a Zendesk attachment instead, so
# those links must still be stripped.
_IMAGE_LINK_LINE_RE = re.compile(r"\bimage\s*:", re.IGNORECASE)


def _strip_public_links_from_line(line):
    # Remove links that directly follow a label separator, including the
    # separator, so `RPI: [file](url)` becomes `RPI`.
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

    # Defense-in-depth: remove any remaining markdown links or raw URLs from a
    # public response body. This is intentionally broader than PDFs only because
    # final_response should not contain public document links of any kind.
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


def _strip_public_links_from_final_response(text):
    """Remove markdown/raw links from public final_response text.

    Internal draft_response may contain document references. Public Zendesk
    comments must not expose invoice/RPI/LOA/POA/document URLs; the documents
    are provided through Zendesk attachment uploads instead. Returned-item
    image links are the one exception: they must stay in the public response
    so the customer can see what was confirmed as returned.
    """

    sanitized = str(text or "")
    lines = sanitized.split("\n")
    processed_lines = [
        line if _IMAGE_LINK_LINE_RE.search(line) else _strip_public_links_from_line(line)
        for line in lines
    ]
    return "\n".join(processed_lines)


def _remove_document_values_from_response(response_text, document_values):
    sanitized = str(response_text or "")

    for value in sorted(document_values, key=len, reverse=True):
        sanitized = sanitized.replace(f": {value}", "")
        sanitized = sanitized.replace(f":\n{value}", "")
        sanitized = sanitized.replace(value, "")

    sanitized = _strip_public_links_from_final_response(sanitized)

    lines = [re.sub(r"[ \t]+$", "", line) for line in sanitized.split("\n")]
    return normalize_text("\n".join(lines))


def build_final_response(row):
    if is_noreply_requester_email(row.get("requester_email")):
        return ""

    if is_excluded_from_processing(row):
        return ""

    if requires_human_intervention(row):
        return ""

    draft_response = row.get("draft_response")
    if is_blank(draft_response):
        draft_response = build_response(row)

    if is_blank(draft_response):
        return ""

    draft_response = strip_internal_draft_metadata_from_public_response(draft_response)

    requested_data = requested_data_for_response(row)
    document_values = _document_display_values_for_response(
        row,
        requested_data,
        attached_only=False,
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

    # EXTRACT MULTIPLE TRACKINGS AND LOG WITH SEMICOLONS
    for idx, row in df.iterrows():
        req_text = str(row.get("cleaned_request_body", "")) + " " + str(row.get("request_body", "")) + " " + str(row.get("subject", ""))
        # Extract all 1Z... tracking numbers (using dict.fromkeys to keep order & remove duplicates)
        trackings = list(dict.fromkeys(re.findall(r'\b1Z[0-9A-Z]{16}\b', req_text.upper())))
        
        if len(trackings) > 1:
            tracking_string = ";".join(trackings)
            df.at[idx, "extracted_tracking_number"] = tracking_string
            df.at[idx, "shipment_tracking_number"] = tracking_string
            df.at[idx, "return_tracking_number"] = tracking_string

    df = enrich_with_full_order_data(df)
    df = generate_documents_for_dataframe(df)

    if "request_language" not in df.columns:
        df["request_language"] = df.apply(row_language, axis=1)
    else:
        df["request_language"] = df.apply(row_language, axis=1)

    df["draft_response"] = df.apply(build_response, axis=1)
    df["human_intervention_required"] = df.apply(requires_human_intervention, axis=1)
    df["final_response"] = df.apply(build_final_response, axis=1)
    # Add internal LLM model metadata only after final_response is built, so the
    # public Zendesk response never exposes internal model-routing details.
    df["draft_response"] = df.apply(
        lambda row: add_llm_model_note_to_draft(row, row.get("draft_response")),
        axis=1,
    )
    df["zendesk_attachment_paths"] = df.apply(
        lambda row: document_attachment_paths_for_response(
            row,
            requested_data_for_response(row),
        ),
        axis=1,
    )
    df["reason_of_contact"] = df.apply(
        lambda row: reason_of_contact_for_response(
            row,
            requested_data_for_response(row),
        ),
        axis=1,
    )
    df["zendesk_country_tag"] = df.apply(zendesk_country_tag_for_row, axis=1)

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

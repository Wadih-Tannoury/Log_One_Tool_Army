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

from ticket_fetcher import append_history_rows
from pipeline_io import REQUEST_INTENT_RESULTS_PATH, read_dataframe
from customs_rules import (
    FEDEX_BROKERAGE_EMAIL,
    HUMAN_INTERVENTION_REQUIRED,
    PLACEHOLDER,
    UNKNOWN_REQUEST,
    UPS_BROKERAGE_EMAIL,
    detect_language_with_dictionary,
    extract_ups_code,
    first_available_value,
    is_returns_customs_clearance,
    normalize_email,
    normalize_language,
    normalize_request_number,
    normalize_requested_data,
)

INPUT_PATH = REQUEST_INTENT_RESULTS_PATH
AUTO_ADD_UPS_MIN_CONFIDENCE = float(os.getenv("AUTO_ADD_UPS_MIN_CONFIDENCE", "0.90"))

DHL_BROKERAGE_EMAIL = os.getenv("DHL_BROKERAGE_EMAIL", "kamil.it@dhl.com")

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
    r"non\s+ha\s+pagato|non\s+paga|mancato\s+pagamento|pagamento\s+mancante)"
    r"[\s\S]{0,120}"
    r"(?:extra\s+charges?|outstanding\s+charges?|additional\s+charges?|charges?|"
    r"oneri|costi|spese|supplementi|dazi|diritti)|"
    r"(?:extra\s+charges?|outstanding\s+charges?|additional\s+charges?|charges?|"
    r"oneri|costi|spese|supplementi|dazi|diritti)"
    r"[\s\S]{0,120}"
    r"(?:did\s+not\s+pay|didn(?:'|’)?t\s+pay|has\s+not\s+paid|not\s+paid|"
    r"non\s+ha\s+pagato|non\s+paga|mancato\s+pagamento|pagamento\s+mancante)"
    r")",
    re.IGNORECASE,
)

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
        "returned_items_confirmation": "Items returned",
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
    if isinstance(value, float) and pd.isna(value):
        return True
    return str(value).strip().lower() in {"", "nan", "none", "n/a", "na"}


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
    return safe_float(row.get("confidence", 0.0))


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


def data_value(row, data_key):
    if data_key == "export_tracking_number":
        return first_available_value(
            row.get("shipment_tracking_number"),
            row.get("extracted_tracking_number"),
            default=PLACEHOLDER,
        )

    if data_key == "ups_account_number":
        ups_code = extract_ups_code(
            row.get("shipment_tracking_number"),
            row.get("extracted_tracking_number"),
            row.get("return_tracking_number"),
        )
        return ups_code or PLACEHOLDER

    return PLACEHOLDER


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

    if first_returns and contact_fields_found:
        fedex_or_dhl = is_fedex_requester_email(requester_email) or is_dhl_requester_email(requester_email)
        if fedex_or_dhl and "return_proforma_invoice" not in result:
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

    # Response-builder rule: unpaid extra/outstanding charges are answered only
    # with the UPS account templates, even if upstream detected other fields.
    if is_unpaid_extra_charges_request(request_text):
        return ["ups_account_number"]

    requested_data = normalize_requested_data_with_aliases(row.get("requested_data"))
    requested_data = collapse_embedded_document_fields(row, requested_data)

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


def build_human_intervention_note(row, reason):
    ticket_id = row.get("zendesk_ticket_id", "")
    request_number = row.get("request_number", "")

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
        "You can find attached the LOA\n\n"
        "Best regards\n\n"
        "Piero T."
    )


def build_ups_account_standard_response(row):
    ups_account = data_value(row, "ups_account_number")

    return (
        "Hello,\n\n"
        "I confirm you the return of shipment on topic.\n"
        f"Debit all the relative costs to our UPS account {ups_account}, authorized by Piero T.\n"
        "You can find attached the LOA\n\n"
        "Best regards\n\n"
        "Piero T."
    )


def build_ups_returns_first_rpi_account_response(row):
    export_tracking = data_value(row, "export_tracking_number")
    ups_account = data_value(row, "ups_account_number")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Answer 1:\n"
        "Buongiorno,\n\n"
        "In allegato la documentazione per la reintroduzione in franchigia:\n\n"
        f"- TRK in export: {export_tracking}\n"
        f"- Cod UPS: {ups_account}\n"
        f"- Return Proforma Invoice: {rpi}\n\n"
        "Tutti prodotti sono stati resi.\n\n"
        "Cordiali saluti,\n\n"
        "Piero T.\n\n"
        "Answer 2:\n"
        "Buongiorno,\n\n"
        "Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.\n\n"
        "- TRK in export: non disponibile, avvenuto con altro vettore\n"
        f"- Cod UPS: {ups_account}\n"
        f"- Return Proforma Invoice: {rpi}\n\n"
        "Tutti prodotti sono stati resi.\n\n"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_fedex_dhl_returns_first_rpi_contact_response(row):
    awb_export = data_value(row, "export_tracking_number")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Answer 1:\n"
        "Buongiorno,\n\n"
        "In allegato invio la documentazione richiesta.\n\n"
        f"AWB in export: {awb_export}\n"
        f"RPI: {rpi}\n"
        "Cordiali saluti,\n\n"
        "Piero T.\n\n"
        "Answer 2:\n"
        "Buongiorno,\n\n"
        "Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.\n\n"
        "AWB in export: non disponibile, avvenuto con altro vettore\n"
        f"RPI: {rpi}\n\n"
        "Tutti prodotti sono stati resi.\n\n"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_dhl_returns_first_rpi_response(row):
    awb_export = data_value(row, "export_tracking_number")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Answer 1:\n"
        "Buongiorno,\n\n"
        "In allegato la documentazione richiesta per la reintroduzione in franchigia.\n"
        f"AWB in export: {awb_export}\n"
        f"Items returned: {PLACEHOLDER}\n"
        f"RPI: {rpi}\n\n"
        "Cordiali saluti,\n\n"
        "Piero T.\n\n"
        "Answer 2:\n"
        "Buongiorno,\n\n"
        "Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.\n\n"
        "AWB in export: non disponibile, avvenuto con altro vettore\n"
        f"RPI: {rpi}\n\n"
        "Tutti prodotti sono stati resi.\n\n"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def should_force_human_intervention(row, requested_data):
    request_text = row_request_text(row)
    raw_requested_data = normalize_requested_data_with_aliases(row.get("requested_data"))

    if is_request_number_3_or_higher(row.get("request_number")):
        return True, "Request number is 3 or higher. Human intervention is required by automation policy."

    if is_platform_handoff_request(request_text):
        return True, "FedEx Support Hub handoff request. A human must handle the external portal."

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

    if first_returns_request and (
        is_fedex_requester_email(requester_email) or is_dhl_requester_email(requester_email)
    ):
        if "return_proforma_invoice" in requested_data and has_embedded_rpi_contact_fields(row):
            return build_fedex_dhl_returns_first_rpi_contact_response(row)

    if first_returns_request and is_dhl_requester_email(requester_email):
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


def main():
    df = read_dataframe(INPUT_PATH)

    if df.empty:
        print("No request intent rows found. Nothing to log to BigQuery history.")
        return

    if "request_language" not in df.columns:
        df["request_language"] = df.apply(row_language, axis=1)
    else:
        df["request_language"] = df.apply(row_language, axis=1)

    df["draft_response"] = df.apply(build_response, axis=1)
    df["human_intervention_required"] = df.apply(requires_human_intervention, axis=1)

    logged_rows = append_history_rows(df)

    print(f"Generated draft responses for {len(df)} rows. Logged {logged_rows} new rows.")


if __name__ == "__main__":
    main()

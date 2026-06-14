"""
response_generator.py

Deterministic draft-response generator.
Reads requested_data from output/request_intent_results.xlsx and creates:
- output/request_intent_results_with_drafts.xlsx

No LLM is used here. The dictionary-detected request_language column
produced by intent_detection.py is used to choose the response language.
"""

import os

import pandas as pd

from customs_rules import (
    FEDEX_BROKERAGE_EMAIL,
    HUMAN_INTERVENTION_REQUIRED,
    PLACEHOLDER,
    UNKNOWN_REQUEST,
    UPS_BROKERAGE_EMAIL,
    collapse_document_embedded_requested_data,
    detect_language_with_dictionary,
    extract_ups_code,
    first_available_value,
    is_dhl_requester_email,
    is_fedex_requester_email,
    is_first_returns_customs_request,
    is_request_number_3_or_higher,
    is_returns_customs_clearance,
    is_special_first_reply_ticket,
    is_unpaid_extra_charges_request,
    is_ups_requester_email,
    normalize_email,
    normalize_language,
    normalize_requested_data,
)

INPUT_PATH = "output/request_intent_results.xlsx"
OUTPUT_PATH = "output/request_intent_results_with_drafts.xlsx"
AUTO_ADD_UPS_MIN_CONFIDENCE = float(os.getenv("AUTO_ADD_UPS_MIN_CONFIDENCE", "0.90"))

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


def requested_data_for_response(row):
    request_text = str(row.get("cleaned_request_body", "") or row.get("request_body", "") or "")
    requested_data = collapse_document_embedded_requested_data(
        row.get("requested_data"),
        ticket_category=row.get("ticket_category"),
        request_number=row.get("request_number", 1),
        requester_email=row.get("requester_email"),
        request_text=request_text,
    )

    # Historical false positives showed that auto-adding UPS account to every
    # Returns Customs Clearance row can amplify weak/incorrect intent matches.
    # Keep this fallback only for high-confidence UPS follow-ups, never for
    # first requests where specific standard templates depend on exact matches.
    if (
        is_returns_customs_clearance(row.get("ticket_category"))
        and not is_first_returns_customs_request(row.get("ticket_category"), row.get("request_number", 1))
        and is_ups_requester_email(row.get("requester_email"))
        and requested_data
        and row_confidence(row) >= AUTO_ADD_UPS_MIN_CONFIDENCE
        and HUMAN_INTERVENTION_REQUIRED not in requested_data
        and UNKNOWN_REQUEST not in requested_data
        and "ups_account_number" not in requested_data
    ):
        requested_data.append("ups_account_number")

    return requested_data


def build_ups_first_standard_reply(row):
    """Backward-compatible UPS first-reply template."""
    return build_ups_returns_first_rpi_account_response(row)


def build_ups_returns_first_rpi_account_response(row):
    export_tracking = data_value(row, "export_tracking_number")
    ups_account = data_value(row, "ups_account_number")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Answer 1:\n"
        "Hi,\n\n"
        "Please find below the information needed for Returns Customs Clearance:\n\n"
        f"\u2022 Export TRK: {export_tracking}\n"
        f"\u2022 UPS Account: {ups_account}\n"
        f"\u2022 Return Proforma Invoice: {rpi}\n"
        "All products have been returned\n\n"
        "Thanks,\n\n"
        "Piero T.\n\n"
        "Answer 2:\n"
        "Buongiorno,\n\n"
        "Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.\n"
        "TRK in export: non disponibile, avvenuto con altro vettore\n"
        f"Cod UPS: {ups_account}\n"
        f"Items returned: {PLACEHOLDER}\n"
        f"RPI: {rpi}\n"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_fedex_first_standard_reply(row):
    awb_export = data_value(row, "export_tracking_number")

    return (
        "Fedex:\n"
        "Buongiorno,\n\n"
        "In allegato invio la documentazione richiesta.\n"
        f"AWB in export: {awb_export}\n"
        f"Items returned: {PLACEHOLDER}\n"
        f"RPI: {PLACEHOLDER}\n"
        "Cordiali saluti,"
    )


def build_returns_first_rpi_documents_response(row):
    awb_export = data_value(row, "export_tracking_number")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Buongiorno,\n\n"
        "In allegato invio la documentazione richiesta.\n\n"
        f"AWB in export: {awb_export}\n"
        f"RPI: {rpi}\n"
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
        "Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.\n"
        "AWB in export: non disponibile, avvenuto con altro vettore\n"
        f"Items returned: {PLACEHOLDER}\n"
        f"RPI: {rpi}\n"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_dhl_returns_first_rpi_response(row):
    awb_export = data_value(row, "export_tracking_number")
    rpi = data_value(row, "return_proforma_invoice")

    return (
        "Buongiorno,\n\n"
        "In allegato la documentazione richiesta per la reintroduzione in franchigia.\n"
        f"AWB in export: {awb_export}\n"
        f"Items returned: {PLACEHOLDER}\n"
        f"RPI: {rpi}\n\n"
        "Cordiali saluti,\n\n"
        "Piero T."
    )


def build_ups_account_unpaid_extra_charges_response(row):
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
        f"Debit all the relative costs to our UPS account {ups_account}, "
        "authorized by Piero T.\n"
        "You can find attached the LOA\n\n"
        "Best regards\n\n"
        "Piero T."
    )


def build_ups_account_standard_response(row):
    ups_account = data_value(row, "ups_account_number")

    return (
        "Hello,\n\n"
        "I confirm you the return of shipment on topic.\n"
        f"Debit all the relative costs to our UPS account {ups_account}, "
        "authorized by Piero T.\n"
        "You can find attached the LOA\n\n"
        "Best regards\n\n"
        "Piero T."
    )


def has_embedded_rpi_contact_fields(row):
    regex_types = set(normalize_requested_data(row.get("regex_request_types")))
    regex_requested_data = set(normalize_requested_data(row.get("regex_requested_data")))
    requested_data = set(normalize_requested_data(row.get("requested_data")))
    embedded_contact_fields = {
        "shipping_address",
        "customer_email",
        "customer_phone",
    }
    return bool((regex_types | regex_requested_data | requested_data) & embedded_contact_fields)


def row_request_text(row):
    return str(row.get("cleaned_request_body", "") or row.get("request_body", "") or "")


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
        "Dear Team,\n\n"
        "Thank you for your message.\n\n"
        "Please find below the requested information:\n\n"
        f"{body}\n\n"
        "Kind regards,"
    )


def build_response(row):
    requester_email = normalize_email(row.get("requester_email"))
    request_number = row.get("request_number", 1)
    requested_data = requested_data_for_response(row)

    if is_request_number_3_or_higher(request_number):
        return build_human_intervention_note(
            row,
            "Request number is 3 or higher. Human intervention is required by automation policy.",
        )

    if as_bool(row.get("human_intervention_required")) or HUMAN_INTERVENTION_REQUIRED in requested_data:
        return build_human_intervention_note(
            row,
            human_reason(
                row,
                "The requested data could not be identified with enough certainty.",
            ),
        )

    if not requested_data:
        return ""

    if requested_data == [UNKNOWN_REQUEST]:
        return build_human_intervention_note(
            row,
            human_reason(
                row,
                "The requested data could not be identified with enough certainty.",
            ),
        )

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

    if (
        first_returns_request
        and "return_proforma_invoice" in requested_data
        and has_embedded_rpi_contact_fields(row)
    ):
        return build_returns_first_rpi_documents_response(row)

    if requested_data == ["ups_account_number"]:
        if is_unpaid_extra_charges_request(row_request_text(row)):
            return build_ups_account_unpaid_extra_charges_response(row)
        return build_ups_account_standard_response(row)

    if is_special_first_reply_ticket(requester_email, request_number):
        if is_fedex_requester_email(requester_email):
            return build_fedex_first_standard_reply(row)

    language = row_language(row)

    if requested_data == ["power_of_attorney"]:
        return build_power_of_attorney_only_response(row, language)

    return build_generic_response(row, requested_data, language)

def requires_human_intervention(row):
    requested_data = normalize_requested_data(row.get("requested_data"))
    return (
        is_request_number_3_or_higher(row.get("request_number"))
        or as_bool(row.get("human_intervention_required"))
        or HUMAN_INTERVENTION_REQUIRED in requested_data
        or requested_data == [UNKNOWN_REQUEST]
    )


def main():
    df = pd.read_excel(INPUT_PATH)

    if "request_language" not in df.columns:
        df["request_language"] = df.apply(row_language, axis=1)
    else:
        df["request_language"] = df.apply(row_language, axis=1)

    df["draft_response"] = df.apply(build_response, axis=1)
    df["human_intervention_required"] = df.apply(requires_human_intervention, axis=1)

    os.makedirs("output", exist_ok=True)
    df.to_excel(OUTPUT_PATH, index=False)

    print(f"Saved draft responses to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

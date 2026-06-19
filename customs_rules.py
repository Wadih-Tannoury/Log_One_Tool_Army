"""
Shared customs automation rules.

This module intentionally has no external service dependencies so it can be
used by the fetch/classification and response-generation steps.
"""

import ast
import math
import re
import unicodedata
from typing import Dict, List, Tuple

RETURNS_CUSTOMS_CLEARANCE = "Returns Customs Clearance"
PLACEHOLDER = "[TO BE RETRIEVED]"
HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"
UNKNOWN_REQUEST = "unknown_request"

UPS_BROKERAGE_EMAIL = "bgybrokerage@ups.com"
UPS_BROKERAGE_EMAILS = {
    UPS_BROKERAGE_EMAIL,
    "cpibrokerage@ups.com",
}
FEDEX_BROKERAGE_EMAIL = "doganafedex@fedex.com"
DHL_BROKERAGE_EMAIL = "kamil.it@dhl.com"

UPS_STANDARD_REPLY_REQUESTED_DATA = [
    "export_tracking_number",
    "ups_account_number",
    "return_proforma_invoice",
    "returned_items_confirmation",
]
FEDEX_STANDARD_REPLY_REQUESTED_DATA = [
    "export_tracking_number",
    "returned_items_confirmation",
    "return_proforma_invoice",
]
DHL_STANDARD_REPLY_REQUESTED_DATA = [
    "export_tracking_number",
    "returned_items_confirmation",
    "return_proforma_invoice",
]

SPECIAL_FIRST_REPLY_EMAILS = (
    UPS_BROKERAGE_EMAILS
    | {
        FEDEX_BROKERAGE_EMAIL,
        DHL_BROKERAGE_EMAIL,
    }
)

# Data elements covered by the first automatic reply for carrier inboxes.
# Follow-up requests asking only for these elements should not be answered again
# without an LLM confirmation and, if confirmed, human intervention.
STANDARD_REPLY_REQUESTED_DATA = {
    email: UPS_STANDARD_REPLY_REQUESTED_DATA
    for email in UPS_BROKERAGE_EMAILS
}
STANDARD_REPLY_REQUESTED_DATA[FEDEX_BROKERAGE_EMAIL] = FEDEX_STANDARD_REPLY_REQUESTED_DATA
STANDARD_REPLY_REQUESTED_DATA[DHL_BROKERAGE_EMAIL] = DHL_STANDARD_REPLY_REQUESTED_DATA

UPS_TRACKING_PATTERN = re.compile(r"\b1Z([0-9A-Z]{6})[0-9A-Z]{10}\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Request text safety helpers
# ---------------------------------------------------------------------------
# Regex classification must run on the latest requester message only.  Zendesk
# email bodies often contain quoted threads and carrier boilerplate, which were
# the main sources of false positives in the historical-ticket review.
QUOTE_HISTORY_MARKER_RE = re.compile(
    r"(?im)^\s*(?:"
    r"-----\s*original\s+message\s*-----|"
    r"---------------\s*original\s+message\s*---------------|"
    r"from:|sent:|subject:|to:|cc:|"
    r"da:|inviato:|oggetto:|a:|"
    r"on\s+.+?\s+wrote:|"
    r"il\s+.+?\s+ha\s+scritto:"
    r")"
)

SIGNATURE_MARKER_RE = re.compile(
    r"(?im)^\s*(?:"
    r"thank\s+you(?:\s+(?:again|for\s+your\s+help))?|"
    r"best\s+regards|kind\s+regards|regards|"
    r"cordiali\s+saluti|distinti\s+saluti|saluti"
    r")\s*[,.-]*\s*$"
)

ACKNOWLEDGEMENT_ONLY_RE = re.compile(
    r"^\s*(?:"
    r"well\s+noted|understood|noted|duly\s+noted|"
    r"thanks(?:\s+(?:a\s+lot|for\s+(?:the\s+update|the\s+information|confirming)))?|"
    r"thank\s+you(?:\s+(?:very\s+much|for\s+(?:your\s+(?:support|help)|the\s+update|confirming)))?|"
    r"received\s+(?:with\s+thanks|thank\s+you)|"
    r"many\s+thanks|much\s+appreciated|greatly\s+appreciated|"
    r"grazie(?:\s+mille)?|"
    r"problem\s+resolved|issue\s+resolved|please\s+close\s+the\s+ticket|"
    r"no\s+further\s+(?:assistance|action)\s+required|"
    r"for\s+(?:your\s+information|reference\s+only)|"
    r"everything\s+is\s+clear|clear\s+thank\s+you|case\s+closed|can\s+be\s+closed|"
    r"all\s+set|all\s+good|looks\s+good|perfect|okay|ok"
    r")[\s.!?,;:-]*$",
    re.IGNORECASE,
)

REQUEST_LANGUAGE_RE = re.compile(
    r"\b(?:"
    r"please|kindly|could\s+you|can\s+you|we\s+(?:need|require)|"
    r"provide|send|forward|confirm|specify|advise|"
    r"prego|si\s+prega|vi\s+preghiamo|la\s+preghiamo|"
    r"chiediamo|richiediamo|necessitiamo|abbiamo\s+bisogno|"
    r"fornire|fornirci|inviare|inviarci|inoltrare|inoltrarci|"
    r"confermare|specificare|indicare"
    r")\b",
    re.IGNORECASE,
)

CUSTOMS_KEYWORD_RE = re.compile(
    r"\b(?:"
    r"invoice|commercial\s+invoice|proforma|rpi|pri|fattura|"
    r"tracking|trk|awb|lettera\s+di\s+vettura|"
    r"customs|dogana|sdoganamento|merce|goods|return|reso|rientr|"
    r"country\s+of\s+origin|paese\s+di\s+origine|"
    r"phone\s+number|numero\s+di\s+telefono|email\s+address|indirizzo\s+email|"
    r"partita\s+iva|codice\s+fiscale|eori|poa|procura|delega|"
    r"dichiarazione\s+di\s+libera\s+esportazione|dichiarazione\s+di\s+intento|"
    r"fedex\s+support\s+hub|extra\s+charges?|outstanding\s+charges?|"
    r"value|valore|discrepancy|discrepanza"
    r")\b",
    re.IGNORECASE,
)

COMMERCIAL_INVOICE_BOILERPLATE_MARKERS = [
    re.compile(r"commercial\s+invoice\s+(?:must|should)\s+include", re.IGNORECASE),
    re.compile(r"commercial\s+invoice\s+requirements", re.IGNORECASE),
    re.compile(r"invoice\s+must\s+include\s+the\s+following", re.IGNORECASE),
    re.compile(r"copy\s+of\s+commercial/proforma\s+invoice", re.IGNORECASE),
    re.compile(r"description\s+of\s+the\s+goods", re.IGNORECASE),
]

COMMERCIAL_INVOICE_BOILERPLATE_FIELDS = [
    re.compile(r"country\s+of\s+origin", re.IGNORECASE),
    re.compile(r"phone\s+number|telephone\s+number", re.IGNORECASE),
    re.compile(r"description\s+of\s+the\s+goods|description\s+of\s+goods", re.IGNORECASE),
    re.compile(r"itemized\s+value", re.IGNORECASE),
    re.compile(r"full\s+name|email\s+address", re.IGNORECASE),
]

CORRECTION_OR_DISCREPANCY_RE = re.compile(
    r"\b(?:"
    r"discrepancy|mismatch|wrong|incorrect|corrected|correction|revised|updated|"
    r"discrepanza|non\s+corrispond|non\s+coincid|errat[oa]|corrett[oa]|rettificat[oa]"
    r")\b",
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
    r"(?:did\s+not\s+pay|didn['’]?t\s+pay|has\s+not\s+paid|not\s+paid|"
    r"non\s+ha\s+(?:ancora\s+)?pagato|non\s+paga|mancato\s+pagamento|pagamento\s+mancante)"
    r"[\s\S]{0,120}"
    r"(?:extra\s+charges?|outstanding\s+charges?|additional\s+charges?|charges?|"
    r"oneri|costi|spese|supplementi|dazi|diritti|addebiti)|"
    r"(?:extra\s+charges?|outstanding\s+charges?|additional\s+charges?|charges?|"
    r"oneri|costi|spese|supplementi|dazi|diritti|addebiti)"
    r"[\s\S]{0,120}"
    r"(?:did\s+not\s+pay|didn['’]?t\s+pay|has\s+not\s+paid|not\s+paid|"
    r"non\s+ha\s+(?:ancora\s+)?pagato|non\s+paga|mancato\s+pagamento|pagamento\s+mancante)"
    r")",
    re.IGNORECASE,
)

INFORMATIVE_STATUS_UPDATE_RE = re.compile(
    r"(?:"
    r"\bi\s+have\s+released\s+the\s+hold\b|"
    r"\bsubmitted\s+for\s+release\s+with\s+customs\b|"
    r"\bdesideriamo\s+informarla\b[\s\S]{0,180}\bawb\s+di\s+ritorno\b|"
    r"\bawb\s+di\s+ritorno\s+(?:e|è)\s+il\s+seguente\b|"
    r"\bspedizione\s+(?:e|è)\s+attualmente\s+in\s+transito\s+verso\s+il\s+mittente\b"
    r")",
    re.IGNORECASE,
)

CUSTOMER_REFUSED_RETURN_REQUEST_RE = re.compile(
    r"(?:"
    r"rifiutat[oa]\s+dal\s+destinatario|"
    r"destinatario[\s\S]{0,80}rifiutat[oa]|"
    r"(?:customer|receiver|consignee|recipient)[\s\S]{0,80}\brefus(?:ed|al)\b|"
    r"\brefus(?:ed|al)\b[\s\S]{0,80}(?:package|parcel|shipment)"
    r")",
    re.IGNORECASE,
)

UPS_RECEIVER_CONTACT_CLEARANCE_RE = re.compile(
    r"(?:"
    r"(?:chieda|chiedere)\s+al\s+(?:suo\s+)?destinatario\s+di\s+contattare\s+l['’]?ufficio\s+locale\s+ups|"
    r"ask\s+the\s+receiver\s+to\s+contact\s+their\s+local\s+ups\s+office|"
    r"ask\s+the\s+receiver\s+to\s+contact\s+their\s+local\s+ups\s+office\s+and\s+complete\s+clearance|"
    r"ask\s+the\s+receiver\s+to\s+contact\s+their\s+local\s+ups\s+office\s+to\s+provide\s+the\s+documents"
    r")",
    re.IGNORECASE,
)

ALTERNATIVE_CONTACT_DETAILS_RE = re.compile(
    r"(?:"
    r"dettagli\s+alternativi\s+di\s+contatto[\s\S]{0,120}"
    r"(?:numero\s+di\s+telefono|indirizzo\s+e-?mail|indirizzo\s+email)|"
    r"alternative\s+contact\s+details[\s\S]{0,120}"
    r"(?:phone\s+number|email\s+address|e-?mail\s+address)"
    r")",
    re.IGNORECASE,
)

DELIVERY_ADDRESS_PHONE_UNREACHABLE_RE = re.compile(
    r"(?:"
    r"indirizzo\s+risulta\s+sconosciuto[\s\S]{0,160}"
    r"(?:manca\s+il\s+numero\s+civico|numero\s+di\s+telefono[\s\S]{0,80}non\s+(?:e|è)\s+raggiungibile)|"
    r"manca\s+il\s+numero\s+civico[\s\S]{0,160}"
    r"numero\s+di\s+telefono[\s\S]{0,80}non\s+(?:e|è)\s+raggiungibile|"
    r"(?:unknown|incomplete)\s+address[\s\S]{0,160}(?:phone|telephone)[\s\S]{0,80}(?:unreachable|not\s+reachable)"
    r")",
    re.IGNORECASE,
)

MISSING_INVOICE_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:priva|privo)\s+della\s+fattura|"
    r"giunta\s+priva\s+della\s+fattura|"
    r"fattura\s+(?:mancante|non\s+presente)|"
    r"(?:fornire|inviare|trasmettere)\s+copia\s+della\s+documentazione[\s\S]{0,120}fattura|"
    r"shipment[\s\S]{0,120}(?:missing|without)\s+(?:the\s+)?invoice"
    r")",
    re.IGNORECASE,
)

UPS_UK_IMPORT_CLEARANCE_INSTRUCTIONS_RE = re.compile(
    r"(?:"
    r"ups\s+brokerage\s+at\s+east\s+midlands\s+airport[\s\S]{0,600}"
    r"import\s+customs\s+clearance\s+instructions|"
    r"please\s+provide\s+import\s+customs\s+clearance\s+instructions[\s\S]{0,500}"
    r"(?:customs\s+procedure|commodity\s+code|eori|vat\s+number|deferment)"
    r")",
    re.IGNORECASE,
)

EXPLICIT_EXPORT_TRACKING_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:trk|tracking|awb|lettera\s+di\s+vettura)[\s\S]{0,50}(?:export|andata)|"
    r"(?:export|andata)[\s\S]{0,50}(?:trk|tracking|awb|lettera\s+di\s+vettura)|"
    r"(?:please|kindly|provide|send|forward|fornire|fornirci|inviare|inviarci|indicare|richiediamo)"
    r"[\s\S]{0,80}(?:trk|tracking|awb|lettera\s+di\s+vettura)"
    r")",
    re.IGNORECASE,
)

TRACKING_REFERENCE_MARKER_RE = re.compile(
    r"(?:"
    r"tracking\s+number\s*/\s*reference\s+information|"
    r"tracking\s*#|"
    r"numero\s+di\s+tracking\s+\d{8,}|"
    r"\bawb\s+\d{8,}\b"
    r")",
    re.IGNORECASE,
)

UPS_ACCOUNT_BOILERPLATE_CONTEXT_RE = re.compile(
    r"(?:"
    r"(?:charges?|costi|spese|tasse|dazi|fees?)"
    r"[\s\S]{0,140}"
    r"(?:charged|addebitat[ie])"
    r"[\s\S]{0,140}"
    r"(?:shipper['’]?s\s+ups\s+account|mittente)"
    r"|(?:charged|addebitat[ie])"
    r"[\s\S]{0,140}"
    r"(?:shipper['’]?s\s+ups\s+account|mittente)"
    r"[\s\S]{0,140}"
    r"(?:charges?|costi|spese|tasse|dazi|fees?)"
    r")",
    re.IGNORECASE,
)

EXPLICIT_UPS_ACCOUNT_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:please|kindly|provide|send|forward|fornire|fornirci|inviare|inviarci|indicare|richiediamo|necessitiamo|prego)"
    r"[\s\S]{0,90}(?:ups\s+account(?:\s+number)?|codice\s+(?:di\s+)?abbonamento\s+ups|cod\s+ups)|"
    r"(?:ups\s+account(?:\s+number)?|codice\s+(?:di\s+)?abbonamento\s+ups|cod\s+ups)"
    r"[\s\S]{0,90}(?:please|kindly|provide|send|forward|fornire|fornirci|inviare|inviarci|indicare|richiediamo|necessitiamo|prego)"
    r")",
    re.IGNORECASE,
)

DOCUMENT_EMBEDDED_REQUESTED_DATA = {
    "tax_information",
    "country_of_origin",
    "product_description",
}

# These are no longer standalone requested_data values. If they still appear
# from an old BigQuery regex row or an LLM fallback, they are fulfilled by the
# return proforma invoice package.
RPI_DOCUMENT_EMBEDDED_REQUESTED_DATA = {
    "customs_description",
    "importer_details",
}

# On first requests, invoice correction and value confirmation are treated as
# details required inside the return proforma invoice package for response
# generation.  They should not produce separate customer-facing lines.
FIRST_REQUEST_RPI_RESPONSE_EMBEDDED_REQUESTED_DATA = {
    "invoice_correction",
    "corrected_invoice",
    "value_confirmation",
}

DOCUMENT_FIELD_PHRASE_RE = re.compile(
    r"(?:"
    r"country\s+of\s+origin|paese\s+di\s+origine|"
    r"partita\s+iva|dati\s+fiscali|codice\s+fiscale|vat\s+number|"
    r"fiscal\s+code|tax\s+(?:id|information)|"
    r"description\s+of\s+the\s+goods|description\s+of\s+goods|"
    r"detailed\s+product\s+description|descrizione\s+dettagliata\s+del\s+prodotto|"
    r"material\s+composition|materiali\s+di\s+composizione|itemized\s+value"
    r")",
    re.IGNORECASE,
)

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

COMMERCIAL_INVOICE_CONTEXT_RE = re.compile(
    r"\b(?:commercial\s+invoice|commercial/proforma\s+invoice|proforma\s+invoice|"
    r"fattura\s+commerciale|fattura\s+export|invoice|fattura)\b",
    re.IGNORECASE,
)


def normalize_whitespace(text: object) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_quoted_history(text: object) -> Tuple[str, bool]:
    """Return text before forwarded/quoted email markers."""
    normalized = normalize_whitespace(text)
    if not normalized:
        return "", False

    marker_match = QUOTE_HISTORY_MARKER_RE.search(normalized)
    if marker_match:
        return normalized[: marker_match.start()].strip(), True

    return normalized, False


def strip_signature(text: object) -> Tuple[str, bool]:
    """Conservatively remove common email signatures after the request text."""
    normalized = normalize_whitespace(text)
    if not normalized:
        return "", False

    marker_match = SIGNATURE_MARKER_RE.search(normalized)
    if marker_match and marker_match.start() > 0:
        return normalized[: marker_match.start()].strip(), True

    return normalized, False


def clean_latest_request_text(text: object) -> Dict[str, object]:
    """
    Clean an email body for intent detection.

    Returns a dictionary so callers can keep audit metadata in output files.
    """
    raw_text = normalize_whitespace(text)
    without_history, quoted_history_removed = strip_quoted_history(raw_text)
    without_signature, signature_removed = strip_signature(without_history)
    cleaned_text = normalize_whitespace(without_signature)

    return {
        "raw_request_text": raw_text,
        "cleaned_request_text": cleaned_text,
        "quoted_history_removed": quoted_history_removed,
        "signature_removed": signature_removed,
    }


def has_actionable_request_language(text: object) -> bool:
    cleaned = normalize_whitespace(text)
    return bool(REQUEST_LANGUAGE_RE.search(cleaned) or CUSTOMS_KEYWORD_RE.search(cleaned))


def is_acknowledgement_only(text: object) -> bool:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return False

    # Prevent a trailing "thanks/grazie" from suppressing a real request.
    if has_actionable_request_language(cleaned) and not ACKNOWLEDGEMENT_ONLY_RE.fullmatch(cleaned):
        return False

    return bool(ACKNOWLEDGEMENT_ONLY_RE.fullmatch(cleaned))


def looks_like_commercial_invoice_boilerplate(text: object) -> bool:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return False

    marker_hits = sum(1 for pattern in COMMERCIAL_INVOICE_BOILERPLATE_MARKERS if pattern.search(cleaned))
    field_hits = sum(1 for pattern in COMMERCIAL_INVOICE_BOILERPLATE_FIELDS if pattern.search(cleaned))

    return marker_hits >= 1 and field_hits >= 2


def contains_correction_or_discrepancy(text: object) -> bool:
    return bool(CORRECTION_OR_DISCREPANCY_RE.search(normalize_whitespace(text)))


def is_platform_handoff_request(text: object) -> bool:
    """True when the sender explicitly asks TLG to reply in an external portal."""
    return bool(PLATFORM_HANDOFF_RE.search(normalize_whitespace(text)))


def is_unpaid_extra_charges_request(text: object) -> bool:
    """True when the request says the customer did not pay extra/outstanding charges."""
    return bool(UNPAID_EXTRA_CHARGES_RE.search(normalize_whitespace(text)))


def is_informative_status_update_only(text: object) -> bool:
    """True when the latest message is an operational update, not a data request."""
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return False
    if not INFORMATIVE_STATUS_UPDATE_RE.search(cleaned):
        return False

    # A status update can still contain a real request after the update.  Keep
    # this guard narrow by allowing only messages without explicit request verbs.
    return not bool(REQUEST_LANGUAGE_RE.search(cleaned))


def is_customer_refused_return_request(text: object) -> bool:
    """True when the carrier reports a refused package and asks how to proceed."""
    cleaned = normalize_whitespace(text)
    if not cleaned or not CUSTOMER_REFUSED_RETURN_REQUEST_RE.search(cleaned):
        return False

    return bool(
        re.search(
            r"(?:come\s+desiderate\s+procedere|come\s+vorre(?:sti|ste|bbe)\s+procedere|"
            r"how\s+(?:you\s+)?(?:would\s+like\s+us\s+to\s+)?proceed|"
            r"provide\s+instructions|fornire\s+istruzioni)",
            cleaned,
            re.IGNORECASE,
        )
    )


def is_ups_receiver_contact_clearance_request(text: object) -> bool:
    """
    True for UPS ERN-style templates where the actionable response is customer
    contact data, even when the template phrases it as receiver-local-office
    contact/clearance instructions.
    """
    cleaned = normalize_whitespace(text)
    return bool(
        UPS_RECEIVER_CONTACT_CLEARANCE_RE.search(cleaned)
        or ALTERNATIVE_CONTACT_DETAILS_RE.search(cleaned)
    )


def is_delivery_address_phone_unreachable_request(text: object) -> bool:
    """True when delivery failed because address details and phone are unusable."""
    return bool(DELIVERY_ADDRESS_PHONE_UNREACHABLE_RE.search(normalize_whitespace(text)))


def is_missing_invoice_request(text: object) -> bool:
    """True when the shipment is held because the invoice is missing."""
    return bool(MISSING_INVOICE_REQUEST_RE.search(normalize_whitespace(text)))


def is_ups_uk_import_clearance_instructions_request(text: object) -> bool:
    """True for the UPS UK import-clearance instruction template in returns flows."""
    return bool(UPS_UK_IMPORT_CLEARANCE_INSTRUCTIONS_RE.search(normalize_whitespace(text)))


def is_explicit_export_tracking_request(text: object) -> bool:
    """True when tracking/AWB wording is an actual export-tracking request."""
    return bool(EXPLICIT_EXPORT_TRACKING_REQUEST_RE.search(normalize_whitespace(text)))


def is_tracking_reference_only(text: object) -> bool:
    """True when tracking/AWB appears as a shipment reference, not requested data."""
    cleaned = normalize_whitespace(text)
    return bool(TRACKING_REFERENCE_MARKER_RE.search(cleaned)) and not is_explicit_export_tracking_request(cleaned)


def is_explicit_ups_account_request(text: object) -> bool:
    """True when the sender explicitly asks TLG to provide/use a UPS account code."""
    return bool(EXPLICIT_UPS_ACCOUNT_REQUEST_RE.search(normalize_whitespace(text)))


def is_ups_account_boilerplate_context(text: object) -> bool:
    """True when UPS account wording appears only in return/disposal cost boilerplate."""
    return bool(UPS_ACCOUNT_BOILERPLATE_CONTEXT_RE.search(normalize_whitespace(text)))


def is_ups_requester_email(email: object) -> bool:
    normalized = normalize_email(email)
    return normalized in UPS_BROKERAGE_EMAILS or normalized.endswith("@ups.com")


def is_fedex_requester_email(email: object) -> bool:
    normalized = normalize_email(email)
    return normalized == FEDEX_BROKERAGE_EMAIL or "fedex" in normalized


def is_dhl_requester_email(email: object) -> bool:
    normalized = normalize_email(email)
    return normalized == DHL_BROKERAGE_EMAIL or normalized.endswith("@dhl.com")


def is_request_number_3_or_higher(request_number: object) -> bool:
    return normalize_request_number(request_number) >= 3


def is_first_returns_customs_request(ticket_category: object, request_number: object) -> bool:
    return is_returns_customs_clearance(ticket_category) and normalize_request_number(request_number) == 1


def has_return_proforma_context(text: object) -> bool:
    return bool(RETURN_PROFORMA_CONTEXT_RE.search(normalize_whitespace(text)))


def has_commercial_invoice_context(text: object) -> bool:
    return bool(COMMERCIAL_INVOICE_CONTEXT_RE.search(normalize_whitespace(text)))


def collapse_document_embedded_requested_data(
    requested_data: object,
    ticket_category: object = "",
    request_number: object = 1,
    requester_email: object = "",
    request_text: object = "",
) -> List[str]:
    """
    Remove deprecated standalone data elements that should be fulfilled by a
    commercial invoice or by the return proforma invoice.

    tax_information, country_of_origin and product_description are intentionally
    not customer-facing requested_data anymore.  When they are detected by an
    old table row or by an LLM fallback, collapse them into the appropriate
    document request instead of answering them separately.

    customs_description and importer_details are also not customer-facing
    requested_data anymore.  They are fulfilled by the return proforma invoice
    package and are collapsed to return_proforma_invoice.

    On request number 1, invoice correction and value confirmation are also
    considered part of the return proforma invoice package for response data.
    """
    values = normalize_requested_data(requested_data)
    if not values:
        return []

    result: List[str] = []
    embedded_fields_found = False
    rpi_document_fields_found = False
    first_request_rpi_response_fields_found = False
    first_request = normalize_request_number(request_number) == 1
    first_returns = is_first_returns_customs_request(ticket_category, request_number)

    for value in values:
        if value in RPI_DOCUMENT_EMBEDDED_REQUESTED_DATA:
            rpi_document_fields_found = True
            continue
        if first_request and value in FIRST_REQUEST_RPI_RESPONSE_EMBEDDED_REQUESTED_DATA:
            first_request_rpi_response_fields_found = True
            continue
        if value in DOCUMENT_EMBEDDED_REQUESTED_DATA:
            embedded_fields_found = True
            continue
        if value == "declaration_of_intent":
            value = "dichiarazione_di_libera_esportazione"
        if first_returns and value == "dichiarazione_di_libera_esportazione":
            continue
        if first_request and value == "exporter_ein":
            continue
        if value not in result:
            result.append(value)

    text = normalize_whitespace(request_text)
    field_phrase_found = bool(DOCUMENT_FIELD_PHRASE_RE.search(text))

    # In first Returns Customs Clearance requests, customer phone/email/address
    # are considered part of the RPI package instead of separate answers.
    # For FedEx/DHL first requests, contact/address matches are enough to infer
    # that the required document package is the return proforma invoice.
    contact_fields_found = any(
        value in RPI_EMBEDDED_CONTACT_REQUESTED_DATA
        for value in result
    )
    fedex_or_dhl_first_return = first_returns and (
        is_fedex_requester_email(requester_email)
        or is_dhl_requester_email(requester_email)
    )

    if fedex_or_dhl_first_return and contact_fields_found:
        result = [
            value
            for value in result
            if value not in RPI_EMBEDDED_CONTACT_REQUESTED_DATA
        ]
        if "return_proforma_invoice" not in result:
            result.append("return_proforma_invoice")
    elif first_returns and "return_proforma_invoice" in result:
        result = [
            value
            for value in result
            if value not in RPI_EMBEDDED_CONTACT_REQUESTED_DATA
        ]

    if (
        rpi_document_fields_found or first_request_rpi_response_fields_found
    ) and "return_proforma_invoice" not in result:
        result.append("return_proforma_invoice")

    if embedded_fields_found:
        if first_returns or has_return_proforma_context(text):
            target_document = "return_proforma_invoice"
        else:
            target_document = "commercial_invoice"

        if target_document not in result:
            result.append(target_document)

    # If duplicated regex rows caused both document types to match for a generic
    # embedded field, keep the document that fits the ticket context.
    if (
        "commercial_invoice" in result
        and "return_proforma_invoice" in result
        and (
            embedded_fields_found
            or rpi_document_fields_found
            or first_request_rpi_response_fields_found
            or field_phrase_found
        )
    ):
        if first_request_rpi_response_fields_found:
            result = [value for value in result if value != "commercial_invoice"]
        elif first_returns or has_return_proforma_context(text):
            result = [value for value in result if value != "commercial_invoice"]
        elif has_commercial_invoice_context(text):
            result = [value for value in result if value != "return_proforma_invoice"]
        elif is_returns_customs_clearance(ticket_category):
            result = [value for value in result if value != "commercial_invoice"]
        else:
            result = [value for value in result if value != "return_proforma_invoice"]

    return result


# Deterministic language dictionaries built from recurrent wording in the
# historical Zendesk tickets.  High-signal phrases receive higher weights;
# generic carrier/customs words receive lower weights to avoid overreacting to
# signatures and boilerplate that can appear in both languages.
ITALIAN_LANGUAGE_MARKERS: Dict[str, int] = {
    "buongiorno": 7,
    "buonasera": 7,
    "gentile cliente": 8,
    "gentili": 5,
    "spettabile": 5,
    "la preghiamo": 9,
    "vi preghiamo": 8,
    "si prega": 8,
    "chiediamo": 6,
    "richiediamo": 6,
    "necessitiamo": 7,
    "abbiamo bisogno": 6,
    "fornirci": 7,
    "inviarci": 6,
    "in allegato": 8,
    "allego": 5,
    "allegato": 5,
    "documentazione richiesta": 8,
    "documentazione precedentemente richiesta": 9,
    "vostro riscontro": 7,
    "gentile riscontro": 7,
    "cortese riscontro": 7,
    "sollecitiamo": 8,
    "sollecito": 6,
    "cordiali saluti": 9,
    "distinti saluti": 8,
    "saluti": 3,
    "fattura": 7,
    "fattura commerciale": 8,
    "fattura di reso": 8,
    "fattura corretta": 7,
    "dichiarazione di intento": 8,
    "dichiarazione di libera esportazione": 9,
    "sdoganamento": 8,
    "importazione": 5,
    "esportazione": 5,
    "spedizione": 5,
    "merce": 6,
    "descrizione merce": 8,
    "rientra": 7,
    "rientrano": 7,
    "reso completo": 8,
    "reso parziale": 8,
    "tutti i prodotti": 6,
    "tutta la merce": 7,
    "quali articoli": 7,
    "articoli rientrano": 8,
    "indirizzo": 6,
    "indirizzo di spedizione": 8,
    "indirizzo destinatario": 7,
    "numero di telefono": 7,
    "recapiti": 6,
    "codice fiscale": 8,
    "partita iva": 8,
    "paese di origine": 8,
    "ragione sociale": 7,
    "procura": 8,
    "delega": 6,
    "delega doganale": 8,
    "lettera di delega": 8,
    "mandato": 7,
    "mandato di libera importazione": 9,
    "mandato di libera esportazione": 9,
    "lettera di istruzioni": 7,
    "istruzioni di sdoganamento": 8,
    "voce doganale": 7,
    "valore reale": 7,
    "conferma valore": 7,
    "conferma della merce": 7,
    "dati fiscali": 8,
}

ENGLISH_LANGUAGE_MARKERS: Dict[str, int] = {
    "hello": 7,
    "hi": 4,
    "dear customer": 8,
    "dear team": 7,
    "dear": 4,
    "please": 7,
    "please provide": 9,
    "please send": 9,
    "please find attached": 9,
    "could you please": 8,
    "we need": 6,
    "we require": 7,
    "kindly provide": 8,
    "requested documents": 8,
    "requested information": 8,
    "documents requested": 7,
    "attached": 5,
    "in attachment": 6,
    "thank you": 8,
    "thanks": 5,
    "many thanks": 7,
    "best regards": 9,
    "kind regards": 9,
    "regards": 4,
    "commercial invoice": 8,
    "proforma invoice": 8,
    "return proforma invoice": 9,
    "corrected invoice": 8,
    "copy of invoice": 7,
    "invoice": 5,
    "export tracking": 8,
    "tracking number": 7,
    "shipment": 5,
    "shipment details": 7,
    "customs clearance": 8,
    "clearance instructions": 8,
    "description of goods": 8,
    "goods description": 7,
    "returned items": 9,
    "items returned": 9,
    "return shipment": 7,
    "all items": 6,
    "country of origin": 8,
    "phone number": 7,
    "contact number": 7,
    "email address": 7,
    "full name": 7,
    "shipping address": 8,
    "delivery address": 8,
    "tax information": 8,
    "declaration of intent": 8,
    "power of attorney": 9,
    "letter of authorization": 8,
    "authorization letter": 8,
    "delegation of authority": 8,
    "importer details": 7,
    "exporter ein": 7,
    "value confirmation": 8,
}


def normalize_email(email: object) -> str:
    return str(email or "").strip().lower()


def normalize_request_number(value: object) -> int:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return 1
        return int(float(str(value).strip()))
    except Exception:
        return 1


def is_special_first_reply_ticket(requester_email: object, request_number: object) -> bool:
    return (
        (
            is_ups_requester_email(requester_email)
            or is_fedex_requester_email(requester_email)
            or is_dhl_requester_email(requester_email)
            or normalize_email(requester_email) in SPECIAL_FIRST_REPLY_EMAILS
        )
        and normalize_request_number(request_number) == 1
    )


def is_special_followup_ticket(requester_email: object, request_number: object) -> bool:
    return (
        (
            is_ups_requester_email(requester_email)
            or is_fedex_requester_email(requester_email)
            or is_dhl_requester_email(requester_email)
            or normalize_email(requester_email) in SPECIAL_FIRST_REPLY_EMAILS
        )
        and normalize_request_number(request_number) > 1
    )


def get_standard_reply_requested_data(requester_email: object) -> List[str]:
    if is_ups_requester_email(requester_email):
        return list(UPS_STANDARD_REPLY_REQUESTED_DATA)
    if is_fedex_requester_email(requester_email):
        return list(FEDEX_STANDARD_REPLY_REQUESTED_DATA)
    if is_dhl_requester_email(requester_email):
        return list(DHL_STANDARD_REPLY_REQUESTED_DATA)
    return list(STANDARD_REPLY_REQUESTED_DATA.get(normalize_email(requester_email), []))


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "none", "n/a", "na"}:
        return True
    return False


def normalize_requested_data(value: object) -> List[str]:
    """Normalize a requested_data value from Python, Excel, or CSV formats."""
    if _is_missing(value):
        return []

    if isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []

        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple, set)):
                raw_values = list(parsed)
            else:
                raw_values = [text]
        except Exception:
            raw_values = [item.strip() for item in text.split(",")]
    else:
        raw_values = [value]

    cleaned = []
    for item in raw_values:
        item = str(item or "").strip()
        if item and item not in cleaned:
            cleaned.append(item)

    return cleaned


def requested_data_already_answered_by_first_reply(
    requested_data: object,
    requester_email: object,
) -> bool:
    """
    True when the requested data is non-empty and all requested elements were
    already covered by the first standard reply for that carrier inbox.
    """
    requested = set(normalize_requested_data(requested_data))
    standard = set(get_standard_reply_requested_data(requester_email))
    return bool(requested) and bool(standard) and requested.issubset(standard)


def has_new_requested_data_vs_first_reply(
    requested_data: object,
    requester_email: object,
) -> bool:
    requested = set(normalize_requested_data(requested_data))
    standard = set(get_standard_reply_requested_data(requester_email))
    return bool(requested - standard)


def is_returns_customs_clearance(ticket_category: object) -> bool:
    return str(ticket_category or "").strip().lower() == RETURNS_CUSTOMS_CLEARANCE.lower()


def extract_ups_code_from_tracking(tracking_number: object) -> str:
    """
    Extract the 6-character UPS code from a UPS tracking number.

    Example:
        1ZCG3563D931731272 -> CG3563
    """
    if _is_missing(tracking_number):
        return ""

    match = UPS_TRACKING_PATTERN.search(str(tracking_number).strip().upper())
    if not match:
        return ""

    return match.group(1).upper()


def extract_ups_code(*tracking_values: object) -> str:
    for tracking_number in tracking_values:
        ups_code = extract_ups_code_from_tracking(tracking_number)
        if ups_code:
            return ups_code
    return ""


def first_available_value(*values: object, default: str = PLACEHOLDER) -> str:
    for value in values:
        if not _is_missing(value):
            return str(value).strip()
    return default


def normalize_language(value: object, default: str = "en") -> str:
    language = str(value or "").strip().lower()

    if language in {"it", "ita", "italian", "italiano"}:
        return "it"

    if language in {"en", "eng", "english", "inglese"}:
        return "en"

    return default


def _normalize_language_text(text: object) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f" {text} " if text else " "


def _score_language_markers(
    normalized_text: str,
    markers: Dict[str, int],
) -> Tuple[int, List[str]]:
    score = 0
    hits: List[str] = []

    for marker, weight in markers.items():
        normalized_marker = _normalize_language_text(marker).strip()
        if normalized_marker and f" {normalized_marker} " in normalized_text:
            score += weight
            hits.append(marker)

    return score, hits


def detect_language_with_dictionary(
    *texts: object,
    default: str = "en",
    return_details: bool = False,
):
    """
    Detect request language using a deterministic weighted dictionary.

    The dictionary is intentionally limited to recurring words and phrases seen
    in historical tickets, with higher weights for greetings, request verbs,
    closings, and customs terms that are highly language-specific.
    """
    normalized_text = _normalize_language_text("\n".join(str(value or "") for value in texts))

    italian_score, italian_hits = _score_language_markers(
        normalized_text,
        ITALIAN_LANGUAGE_MARKERS,
    )
    english_score, english_hits = _score_language_markers(
        normalized_text,
        ENGLISH_LANGUAGE_MARKERS,
    )

    if italian_score > english_score:
        language = "it"
    elif english_score > italian_score:
        language = "en"
    else:
        language = normalize_language(default, default="en")

    total_score = italian_score + english_score
    if total_score:
        confidence = round(min(0.99, 0.5 + abs(italian_score - english_score) / (2 * total_score)), 2)
    else:
        confidence = 0.0

    strongest_italian_hits = ", ".join(italian_hits[:8]) or "none"
    strongest_english_hits = ", ".join(english_hits[:8]) or "none"
    notes = (
        f"Dictionary language detection. it_score={italian_score}; "
        f"en_score={english_score}; it_hits={strongest_italian_hits}; "
        f"en_hits={strongest_english_hits}"
    )

    if return_details:
        return {
            "request_language": language,
            "language_confidence": confidence,
            "language_notes": notes,
        }

    return language


def detect_language_heuristic(*texts: object) -> str:
    """Backward-compatible alias for the dictionary-based language detector."""
    return detect_language_with_dictionary(*texts)

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
FEDEX_BROKERAGE_EMAIL = "doganafedex@fedex.com"
SPECIAL_FIRST_REPLY_EMAILS = {
    UPS_BROKERAGE_EMAIL,
    FEDEX_BROKERAGE_EMAIL,
}

# Data elements covered by the first automatic reply for the two carrier inboxes.
# Follow-up requests asking only for these elements should not be answered again
# without an LLM confirmation and, if confirmed, human intervention.
STANDARD_REPLY_REQUESTED_DATA = {
    UPS_BROKERAGE_EMAIL: [
        "export_tracking_number",
        "ups_account_number",
        "returned_items_confirmation",
    ],
    FEDEX_BROKERAGE_EMAIL: [
        "export_tracking_number",
        "returned_items_confirmation",
        "return_proforma_invoice",
    ],
}

UPS_TRACKING_PATTERN = re.compile(r"\b1Z([0-9A-Z]{6})[0-9A-Z]{10}\b", re.IGNORECASE)

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
        normalize_email(requester_email) in SPECIAL_FIRST_REPLY_EMAILS
        and normalize_request_number(request_number) == 1
    )


def is_special_followup_ticket(requester_email: object, request_number: object) -> bool:
    return (
        normalize_email(requester_email) in SPECIAL_FIRST_REPLY_EMAILS
        and normalize_request_number(request_number) > 1
    )


def get_standard_reply_requested_data(requester_email: object) -> List[str]:
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

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

ORDER_CUSTOMS_CLEARANCE = "Order Customs Clearance"
PENDING_ORDER_RELEASE = "Pending Order Release"
RETURNS_CUSTOMS_CLEARANCE = "Returns Customs Clearance"
VALID_TICKET_CATEGORIES = (
    ORDER_CUSTOMS_CLEARANCE,
    PENDING_ORDER_RELEASE,
    RETURNS_CUSTOMS_CLEARANCE,
)
CARRIER_EMAIL_DOMAINS = ("ups.com", "dhl.com", "fedex.com")
PLACEHOLDER = "[TO BE RETRIEVED]"
HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"
UNKNOWN_REQUEST = "unknown_request"
DICHIARAZIONE_DI_LIBERA_ESPORTAZIONE = "dichiarazione_di_libera_esportazione"
LIBERA_ESPORTAZIONE_REQUEST_TYPES = {
    DICHIARAZIONE_DI_LIBERA_ESPORTAZIONE,
    "declaration_of_intent",
}
COMMERCIAL_INVOICE_REQUEST_TYPES = {
    "invoice",
    "commercial_invoice",
    "commercial_invoice_required",
}
RETURN_PROFORMA_REQUEST_TYPES = {
    "return_proforma_invoice",
    "rpi",
    "pri",
}

UPS_BROKERAGE_EMAIL = "bgybrokerage@ups.com"
UPS_BROKERAGE_EMAILS = {
    UPS_BROKERAGE_EMAIL,
    "cpibrokerage@ups.com",
}
FEDEX_BROKERAGE_EMAIL = "doganafedex@fedex.com"
DHL_BROKERAGE_EMAIL = "kamil.it@dhl.com"

def is_missing_extracted_tracking_number(value: object) -> bool:
    """Return True when the ticket did not contain a usable tracking/AWB number.

    ticket_fetcher.extract_tracking_number uses ``N/A`` when no carrier-specific
    tracking number is found. Treat other spreadsheet/null spellings as missing
    too so downstream steps can stop before regex or LLM analysis.
    """

    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    text = str(value).strip()
    return text.lower() in {"", "nan", "none", "null", "n/a", "na", "not found"}


UPS_STANDARD_REPLY_REQUESTED_DATA = [
    "export_tracking_number",
    "ups_account_number",
    "return_proforma_invoice",
    "returned_items_confirmation",
]

RETURNS_CUSTOMS_FIRST_REQUEST_REQUIRED_DATA = [
    "export_tracking_number",
    "return_proforma_invoice",
    "ups_account_number",
    "returned_items_confirmation",
]
RETURNS_CUSTOMS_FIRST_REQUEST_TRIGGER_DATA = set(RETURNS_CUSTOMS_FIRST_REQUEST_REQUIRED_DATA)
RETURNS_CUSTOMS_FIRST_REQUEST_DATA_ALIASES = {
    "tracking_number": "export_tracking_number",
    "export_tracking": "export_tracking_number",
    "return_tracking_number": "export_tracking_number",
    "ups_account": "ups_account_number",
    "ups_account_code": "ups_account_number",
    "ups_code": "ups_account_number",
    "rpi": "return_proforma_invoice",
    "pri": "return_proforma_invoice",
    "returned_items": "returned_items_confirmation",
    "returned_item": "returned_items_confirmation",
}
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

KNOWN_REQUESTED_DATA_TOKEN_RE = re.compile(
    r"\b(?:"
    r"human_intervention_required|unknown_request|exclude_from_processing|"
    r"commercial_invoice|return_proforma_invoice|corrected_invoice|invoice_correction|"
    r"export_tracking_number|ups_account_number|returned_items_confirmation|"
    r"customs_description|importer_details|value_confirmation|"
    r"tax_information|country_of_origin|product_description|"
    r"dichiarazione_di_libera_esportazione|declaration_of_intent|eori_number|"
    r"power_of_attorney|authorization_letter|shipment_instructions|"
    r"customer_phone|customer_email|customer_name|shipping_address|"
    r"address_translation|exporter_ein|address_correction|previously_requested_documentation|"
    r"tracking_number|ups_account|ups_account_code|ups_code|returned_items|invoice"
    r")\b",
    re.IGNORECASE,
)


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
    r"\*{0,2}\s*(?:from|sent|subject|to|cc|da|inviato|oggetto|a)\s*:\*{0,2}|"
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

FOLLOW_UP_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"follow(?:ing)?\s+up|follow-?up|reminder|circling\s+back|"
    r"as\s+per\s+(?:the\s+)?(?:below|previous)|previous(?:ly)?\s+requested|"
    r"communication\s+from|resolve\s+this\s+(?:export\s+)?shipment|"
    r"return\s+(?:process\s+date|to\s+shipper\s+deadline)|"
    r"without\s+a\s+resolution|pending\s+customer\s+resolution"
    r")\b",
    re.IGNORECASE,
)

ACTIONABLE_QUOTED_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"required\s+action(?:/information)?(?:\s+from\s+customer)?|"
    r"action/information\s+listed\s+(?:above|below)|"
    r"commercial\s+invoice\s+is\s+required|"
    r"required\s+to\s+meet\s+customs\s+compliance|"
    r"please\s+respond\s+to\s+this\s+email\s+with\s+the\s+invoice\s+attached|"
    r"please\s+(?:provide|send|forward|attach)|"
    r"we\s+(?:need|require)|customer\s+to\s+resolve|"
    r"shipment(?:s)?\s+on\s+hold|pending\s+customer\s+resolution|"
    r"return\s+to\s+shipper\s+deadline|customs\s+compliance|clear\s+customs|"
    r"(?:ti|la|vi)\s+preghiamo\s+di\s+(?:inviar|fornir)|"
    r"priva\s+di\s+fattura|fattura\s+di\s+reso|"
    r"restiamo\s+in\s+attesa\s+di\s+ricevere|"
    r"(?:h)?awb\s+di\s+export|"
    r"per\s+poter\s+procedere\s+allo\s+sdoganamento"
    r")\b",
    re.IGNORECASE,
)

ACTIONABLE_QUOTED_CONTEXT_START_RE = re.compile(
    r"(?im)^\s*(?:"
    r"required\s+action(?:/information)?(?:\s+from\s+customer)?|"
    r"a\s+commercial\s+invoice\s+is\s+required|"
    r"please\s+(?:provide|send|forward|attach)|"
    r"we\s+(?:need|require)|"
    r"the\s+shipment\s+details\s+and\s+deadline|"
    r"customer\s+to\s+resolve|shipment(?:s)?\s+on\s+hold|"
    r"(?:ti|la|vi)\s+preghiamo\s+di\s+(?:inviar|fornir)|"
    r"priva\s+di\s+fattura|fattura\s+di\s+reso|"
    r"restiamo\s+in\s+attesa\s+di\s+ricevere|"
    r"(?:h)?awb\s+di\s+export"
    r")",
    re.IGNORECASE,
)

QUOTED_CONTEXT_END_RE = re.compile(
    r"(?im)^\s*(?:"
    r"tracking\s+number\s*/\s*reference\s+information|"
    r"return\s+to\s+shipper\s+deadline|reply\s+all\s+to\s+this\s+email|"
    r"work\s+email\s*:|cer\s+team\s+email\s*:|phone\s+number\s*:|"
    r"my\s+office\s+hours\s*:|ups\s+customer\s+support\s+numbers|"
    r"preferred\s+customer\s*:|enroll\s+in\s+ups|"
    r"[A-Z][a-z]+\s+[A-Z][a-z]+\s*$"
    r")",
    re.IGNORECASE,
)

MAX_RETAINED_QUOTED_CONTEXT_CHARS = 5000

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
    re.compile(r"commercial\s+invoice\s+must\s+identify", re.IGNORECASE),
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
    r"(?:"
    # English explicit invoice/value/document mismatch or correction.
    r"\b(?:invoice|value|amount|mrn|document|documents|export\s+documents?|import\s+documents?)"
    r"[\s\S]{0,120}\b(?:discrepanc(?:y|ies)|mismatch|wrong|incorrect|corrected|correction|revised|updated|do\s+not\s+match|don['’]?t\s+match)\b|"
    r"\b(?:discrepanc(?:y|ies)|mismatch|wrong|incorrect|corrected|correction|revised|updated)"
    r"[\s\S]{0,120}\b(?:invoice|value|amount|mrn|document|documents|export\s+documents?|import\s+documents?)\b|"
    # Italian invoice/value/document mismatch or requested corrected invoice/value.
    r"\b(?:fattur[ae]|fattura\s+di\s+ritorno|fattura\s+di\s+reso|valore|valori|importi|mrn|documenti?)"
    r"[\s\S]{0,120}\b(?:non\s+(?:corrispond\w*|coincid\w*|combac\w*)|corrett[oaie]|rettificat[oaie]|errat[oaie]|discrepanza)\b|"
    r"\b(?:non\s+(?:corrispond\w*|coincid\w*|combac\w*)|discrepanza)"
    r"[\s\S]{0,120}\b(?:fattur[ae]|valore|valori|importi|mrn|documenti?)\b|"
    # Spanish invoice/value/document mismatch or correction.
    r"\b(?:factura|valor(?:es)?|importe(?:s)?|documentos?|documentos\s+de\s+exportaci[oó]n|documentos\s+de\s+importaci[oó]n|mrn)"
    r"[\s\S]{0,120}\b(?:no\s+(?:coincid\w*|correspond\w*|concuerd\w*)|incorrect[oa]s?|corregid[oa]s?|correct[oa]s?|discrepancia)\b|"
    r"\b(?:no\s+(?:coincid\w*|correspond\w*|concuerd\w*)|discrepancia)"
    r"[\s\S]{0,120}\b(?:factura|valor(?:es)?|importe(?:s)?|documentos?|mrn)\b"
    r")",
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
    r"non\s+ha\s+(?:ancora\s+)?pagato|non\s+paga|mancato\s+pagamento|pagamento\s+mancante)|"
    r"(?:aceptar|asumir)\s+(?:todos\s+los\s+)?gastos(?:\s+generados|\s+incurridos)?|"
    r"(?:carta\s+de\s+autorizaci[oó]n\s+de\s+ga?s?tos)|"
    r"sanci[oó]n\s+de\s+\d+|fuera\s+de\s+plazo|estado\s+de\s+abandono|"
    r"(?:accept|approve|authorize)\s+(?:all\s+)?(?:charges?|fees?|costs?|penalt(?:y|ies))|"
    r"(?:extra\s+charges?|processing\s+fee|storage\s+fees?|deferment\s+fee|ups\s+dan|fee\s+of\s+\d)|"
    r"(?:accettare|autorizzare|approvare)\s+(?:tutti\s+)?(?:i\s+)?(?:costi|oneri|spese|addebiti|dazi)|"
    r"(?:costi|oneri|spese|addebiti|dazi)[\s\S]{0,120}(?:accettare|autorizzare|approvare)"
    r")",
    re.IGNORECASE,
)

INFORMATIVE_STATUS_UPDATE_RE = re.compile(
    r"(?:"
    r"\bi\s+have\s+released\s+the\s+hold\b|"
    r"\bsubmitted\s+for\s+release\s+with\s+customs\b|"
    r"\bdesideriamo\s+informarla\b[\s\S]{0,180}\bawb\s+di\s+ritorno\b|"
    r"\bawb\s+di\s+ritorno\s+(?:e|è)\s+il\s+seguente\b|"
    r"\bspedizione\s+(?:e|è)\s+attualmente\s+in\s+transito\s+verso\s+il\s+mittente\b|"
    r"\bpaquete[\s\S]{0,140}a[uú]n\s+se\s+encuentra\s+en\s+tr[aá]nsito\b|"
    r"\bpackage[\s\S]{0,140}(?:still|currently)\s+in\s+transit\b"
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
    r"ups\s+brokerage\s+at\s+east\s+midlands\s+airport[\s\S]{0,2000}"
    r"(?:clearance\s+instructions|customs\s+procedure|commodity\s+code|eori|deferment\s+account|\bdan\b|extra\s+charges?)|"
    r"please\s+provide(?:\s+us\s+with)?\s+(?:your\s+)?(?:import\s+customs\s+)?clearance\s+instructions[\s\S]{0,1200}"
    r"(?:customs\s+procedure|commodity\s+code|eori|vat\s+number|deferment|\bdan\b|ups\s+credit\s+account)"
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

ATR_CERTIFICATE_MANDATE_RE = re.compile(
    r"(?:"
    r"(?:mandato|procura|delega)"
    r"[\s\S]{0,180}"
    r"(?:emissione|rilascio|richiesta|certificat\w*|form\s+allegato)"
    r"[\s\S]{0,180}\batr\b|"
    r"\batr\b[\s\S]{0,180}"
    r"(?:mandato|procura|delega|certificat\w*|form\s+allegato)"
    r")",
    re.IGNORECASE,
)


NO_ACTION_CARRIER_NOTIFICATION_RE = re.compile(
    r"(?:"
    r"please\s+do\s+not\s+(?:respond|reply)|"
    r"do\s+not\s+reply|non\s+rispondere|"
    r"siete\s+pregati\s+di\s+non\s+rispondere|"
    r"(?:e-?mail|messaggio)\s+automatic[ao]|automated\s+message|"
    r"unmonitored\s+mailbox|ups\s+will\s+not\s+receive\s+your\s+reply|"
    r"allegato\s+mrn\s+relativo|"
    r"documento\s+di\s+notifica\s+di\s+esportazione|"
    r"import\s+data\s+summary[\s\S]{0,260}"
    r"(?:not\s+required\s+to\s+take\s+any\s+action|for\s+your\s+information)|"
    r"we\s+received\s+your\s+(?:inquiry|claim)|"
    r"thank\s+you\s+for\s+contacting\s+ups|"
    r"we(?:'|’|\s+)?re\s+(?:reviewing|currently\s+researching)\s+your\s+(?:claim|inquiry)|"
    r"claim\s+(?:has\s+not\s+been\s+approved|submitted|closed)|"
    r"unable\s+to\s+notify\s+your\s+customer|"
    r"your\s+shipment[\s\S]{0,180}"
    r"(?:out\s+for\s+delivery|on\s+the\s+way|has\s+been\s+delivered|is\s+delivered|"
    r"was\s+delivered|scheduled\s+delivery\s+date)|"
    r"tracking\s+details[\s\S]{0,220}"
    r"(?:please\s+do\s+not\s+(?:respond|reply)|do\s+not\s+reply)|"
    r"unpaid\s+shipment\s+charges[\s\S]{0,220}"
    r"(?:unmonitored\s+mailbox|please\s+do\s+not\s+reply)|"

    r"we\s+have\s+(?:enclosed|attached)[\s\S]{0,220}"
    r"(?:for\s+your\s+information|not\s+required\s+to\s+take\s+any\s+action)|"
    r"following\s+your\s+recent\s+export[\s\S]{0,260}"
    r"(?:for\s+your\s+information|not\s+required\s+to\s+take\s+any\s+action)|"
    r"export\s+data\s+summary[\s\S]{0,260}"
    r"(?:commercial\s+invoice|not\s+required\s+to\s+take\s+any\s+action)|"
    r"export\s+cleared[\s\S]{0,120}\bmrn\b|"
    r"in\s+allegato\s+trasmettiamo\s+la\s+documentazione\s+relativa\s+alla\s+dichiarazione\s+di\s+importazione|"
    r"please\s+find\s+(?:here\s+)?attached\s+the\s+documentation\s+of\s+the\s+import\s+declaration|"
    r"adjunto\s+reciba\s+dua\s+de\s+importaci[oó]n|"
    r"\bdua\s+importaci[oó]n\b|"
    r"paquete[\s\S]{0,160}a[uú]n\s+se\s+encuentra\s+en\s+tr[aá]nsito|"
    r"la\s+informeremo\s+quando\s+la\s+spedizione[\s\S]{0,120}disponibile\s+per\s+il\s+ritiro|"
    r"spedizione\s+(?:e|è)\s+disponibile\s+per\s+il\s+ritiro|"
    r"abbiamo\s+ricevuto\s+la\s+sua\s+richiesta\s+di\s+consegna\s+presso\s+un\s+nostro\s+punto\s+di\s+ritiro|"
    r"uw\s+zending\s+is\s+afgeleverd|"
    r"foto\s+als\s+bewijs\s+van\s+aflevering|"
    r"your\s+shipment\s+has\s+been\s+delivered|"
    r"proof\s+of\s+delivery|"
    r"sorry\s+we\s+missed\s+you|"
    r"we\s+(?:attempted\s+to\s+deliver|couldn['’]?t\s+deliver)[\s\S]{0,160}your\s+shipment|"
    r"purtroppo\s+il\s+destinatario\s+era\s+assente|"
    r"nous\s+sommes\s+d[eé]sol[eé]s\s+de\s+vous\s+avoir\s+manqu[eé]|"
    r"leider\s+haben\s+wir\s+sie\s+verpasst|"
    r"mums\s+neizdev[aā]s\s+pieg[aā]d[aā]t|"
    r"olemme\s+vastaanottaneet\s+pyynt[oö]si[\s\S]{0,140}noutoa\s+varten|"
    r"przesyłka\s+czeka\s+na\s+ciebie\s+w\s+punkcie\s+odbioru|"
    r"lähetyksesi\s+on\s+toimitettu|"
    r"kuvallinen\s+toimitustodistus|"
    r"din\s+försändelse\s+har\s+levererats|"
    r"leveransbevis|"
    r"il\s+caso[\s\S]{0,160}(?:è|e')\s+stato\s+chiuso|"
    r"reclamo\s+(?:è|e')\s+stato\s+accolto|"
    r"caso[\s\S]{0,160}(?:preso\s+in\s+carico|creato)|"
    r"sar[aà]\s+nostra\s+cura\s+contattarvi|"
    r"questo\s+(?:è|e')\s+un\s+messaggio\s+automatico[\s\S]{0,180}non\s+rispondere|"
    r"your\s+copy\s+invoice[\s\S]{0,140}requested\s+paperwork|"
    r"reminder:\s+new\s+eu\s+import\s+rules|"
    r"thank\s+you\s+for\s+the\s+information[\s\S]{0,160}(?:forwarded|sent)\s+the\s+details\s+to\s+our\s+local\s+office|"
    r"grazie\s+per\s+le\s+informazioni[\s\S]{0,160}(?:inoltrato|inviato)[\s\S]{0,80}dettagli|"
    r"solution\s+transport\s+à\s+l['’]?international|"
    r"ya\s+ha\s+sido\s+despachad[oa]\s+por\s+aduanas|"
    r"recibida[;,.]?[\s\S]{0,80}despachad[oa]\s+por\s+aduanas|"
    r"ho\s+dovuto\s+coinvolgere\s+i\s+responsabili[\s\S]{0,180}le\s+aggiorno\s+appena\s+ricevo\s+riscontro|"
    r"package\s+has\s+not\s+shown\s+any\s+movement[\s\S]{0,220}initiate\s+an\s+investigation|"
    r"la\s+tua\s+ultima\s+fattura\s+dhl[\s\S]{0,220}messaggio\s+automatico"
    r")",
    re.IGNORECASE,
)


STRONG_NO_ACTION_CARRIER_NOTIFICATION_RE = re.compile(
    r"(?:"
    r"allegato\s+mrn\s+relativo|"
    r"documento\s+di\s+notifica\s+di\s+esportazione|"
    r"import\s+data\s+summary[\s\S]{0,260}"
    r"(?:not\s+required\s+to\s+take\s+any\s+action|for\s+your\s+information)|"
    r"we\s+received\s+your\s+(?:inquiry|claim)|"
    r"thank\s+you\s+for\s+contacting\s+ups|"
    r"we(?:'|’|\s+)?re\s+(?:reviewing|currently\s+researching)\s+your\s+(?:claim|inquiry)|"
    r"claim\s+(?:has\s+not\s+been\s+approved|submitted|closed)|"
    r"unable\s+to\s+notify\s+your\s+customer|"
    r"your\s+shipment[\s\S]{0,180}"
    r"(?:out\s+for\s+delivery|on\s+the\s+way|has\s+been\s+delivered|is\s+delivered|"
    r"was\s+delivered|scheduled\s+delivery\s+date)|"
    r"tracking\s+details[\s\S]{0,220}"
    r"(?:please\s+do\s+not\s+(?:respond|reply)|do\s+not\s+reply)|"
    r"unpaid\s+shipment\s+charges[\s\S]{0,220}"
    r"(?:unmonitored\s+mailbox|please\s+do\s+not\s+reply)|"

    r"we\s+have\s+(?:enclosed|attached)[\s\S]{0,220}"
    r"(?:for\s+your\s+information|not\s+required\s+to\s+take\s+any\s+action)|"
    r"following\s+your\s+recent\s+export[\s\S]{0,260}"
    r"(?:for\s+your\s+information|not\s+required\s+to\s+take\s+any\s+action)|"
    r"export\s+data\s+summary[\s\S]{0,260}"
    r"(?:commercial\s+invoice|not\s+required\s+to\s+take\s+any\s+action)|"
    r"export\s+cleared[\s\S]{0,120}\bmrn\b|"
    r"in\s+allegato\s+trasmettiamo\s+la\s+documentazione\s+relativa\s+alla\s+dichiarazione\s+di\s+importazione|"
    r"please\s+find\s+(?:here\s+)?attached\s+the\s+documentation\s+of\s+the\s+import\s+declaration|"
    r"adjunto\s+reciba\s+dua\s+de\s+importaci[oó]n|"
    r"\bdua\s+importaci[oó]n\b|"
    r"paquete[\s\S]{0,160}a[uú]n\s+se\s+encuentra\s+en\s+tr[aá]nsito|"
    r"la\s+informeremo\s+quando\s+la\s+spedizione[\s\S]{0,120}disponibile\s+per\s+il\s+ritiro|"
    r"spedizione\s+(?:e|è)\s+disponibile\s+per\s+il\s+ritiro|"
    r"abbiamo\s+ricevuto\s+la\s+sua\s+richiesta\s+di\s+consegna\s+presso\s+un\s+nostro\s+punto\s+di\s+ritiro|"
    r"uw\s+zending\s+is\s+afgeleverd|"
    r"foto\s+als\s+bewijs\s+van\s+aflevering|"
    r"your\s+shipment\s+has\s+been\s+delivered|"
    r"proof\s+of\s+delivery|"
    r"sorry\s+we\s+missed\s+you|"
    r"we\s+(?:attempted\s+to\s+deliver|couldn['’]?t\s+deliver)[\s\S]{0,160}your\s+shipment|"
    r"purtroppo\s+il\s+destinatario\s+era\s+assente|"
    r"nous\s+sommes\s+d[eé]sol[eé]s\s+de\s+vous\s+avoir\s+manqu[eé]|"
    r"leider\s+haben\s+wir\s+sie\s+verpasst|"
    r"mums\s+neizdev[aā]s\s+pieg[aā]d[aā]t|"
    r"olemme\s+vastaanottaneet\s+pyynt[oö]si[\s\S]{0,140}noutoa\s+varten|"
    r"przesyłka\s+czeka\s+na\s+ciebie\s+w\s+punkcie\s+odbioru|"
    r"lähetyksesi\s+on\s+toimitettu|"
    r"kuvallinen\s+toimitustodistus|"
    r"din\s+försändelse\s+har\s+levererats|"
    r"leveransbevis|"
    r"il\s+caso[\s\S]{0,160}(?:è|e')\s+stato\s+chiuso|"
    r"reclamo\s+(?:è|e')\s+stato\s+accolto|"
    r"caso[\s\S]{0,160}(?:preso\s+in\s+carico|creato)|"
    r"sar[aà]\s+nostra\s+cura\s+contattarvi|"
    r"questo\s+(?:è|e')\s+un\s+messaggio\s+automatico[\s\S]{0,180}non\s+rispondere|"
    r"your\s+copy\s+invoice[\s\S]{0,140}requested\s+paperwork|"
    r"reminder:\s+new\s+eu\s+import\s+rules|"
    r"thank\s+you\s+for\s+the\s+information[\s\S]{0,160}(?:forwarded|sent)\s+the\s+details\s+to\s+our\s+local\s+office|"
    r"grazie\s+per\s+le\s+informazioni[\s\S]{0,160}(?:inoltrato|inviato)[\s\S]{0,80}dettagli|"
    r"solution\s+transport\s+à\s+l['’]?international|"
    r"ya\s+ha\s+sido\s+despachad[oa]\s+por\s+aduanas|"
    r"recibida[;,.]?[\s\S]{0,80}despachad[oa]\s+por\s+aduanas|"
    r"ho\s+dovuto\s+coinvolgere\s+i\s+responsabili[\s\S]{0,180}le\s+aggiorno\s+appena\s+ricevo\s+riscontro|"
    r"package\s+has\s+not\s+shown\s+any\s+movement[\s\S]{0,220}initiate\s+an\s+investigation|"
    r"la\s+tua\s+ultima\s+fattura\s+dhl[\s\S]{0,220}messaggio\s+automatico"
    r")",
    re.IGNORECASE,
)

RETURN_CATEGORY_RE = re.compile(
    r"(?:"
    r"\b(?:rpi|pri)\b|return\s+proforma|return\s+invoice|"
    r"fattura\s+(?:di\s+)?reso|proforma\s+(?:di\s+)?reso|"
    r"reintroduzione\s+in\s+franchigia|"
    r"(?:merce|articoli|goods|items)[\s\S]{0,100}(?:rientr|return(?:ed|ing))|"
    r"(?:rientr|return(?:ed|ing))[\s\S]{0,100}(?:merce|articoli|goods|items)|"
    r"gb\s+returns?|evidence\s+of\s+export|proof\s+of\s+export|"
    r"(?:trk|tracking|awb|lettera\s+di\s+vettura)[\s\S]{0,60}(?:export|andata)|"
    r"(?:export|andata)[\s\S]{0,60}(?:trk|tracking|awb|lettera\s+di\s+vettura)|"
    r"return\s+to\s+shipper|\brts\b"
    r")",
    re.IGNORECASE,
)

PENDING_ORDER_RELEASE_CATEGORY_RE = re.compile(
    r"(?:"
    r"fedex\s+support\s+hub|"
    r"(?:indirizzo|address)[\s\S]{0,120}(?:sconosciut|unknown|incomplete|mancante|missing|incorrect|non\s+corretto)|"
    r"numero\s+civico|street\s+number|"
    r"(?:telefono|phone|telephone)[\s\S]{0,120}(?:non\s+(?:e|è)\s+raggiungibile|unreachable|not\s+reachable)|"
    r"(?:destinatario|consignee|receiver|recipient)[\s\S]{0,100}(?:assente|irreperibile|not\s+available|absent|refus(?:ed|al)|rifiutat[oa])|"
    r"delivery\s+instructions|delivery\s+address|shipping\s+address|"
    r"authorization\s+letter|lettera\s+di\s+autorizzazione|"
    r"notifica\s+di\s+giacenza|avviso\s+di\s+giacenza|\bgiacenza\b"
    r")",
    re.IGNORECASE,
)

ORDER_CUSTOMS_CLEARANCE_CATEGORY_RE = re.compile(
    r"(?:"
    r"commercial\s*/?\s*proforma\s+invoice|commercial\s+invoice|"
    r"copy\s+of\s+(?:commercial\s*/?\s*proforma\s+)?invoice|"
    r"fattura\s+(?:commerciale|export|doganale)|"
    r"invoice\s+(?:is\s+)?(?:missing|mancante)|fattura\s+(?:mancante|non\s+presente)|"
    r"customs\s+clearance|sdoganamento|importazione|export\s+customs\s+data\s+formalities|"
    r"detailed\s+description|description\s+of\s+(?:the\s+)?(?:goods|contents)|"
    r"what\s+(?:it|the\s+item|the\s+goods)\s+is|made\s+of|material\s+composition|"
    r"country\s+of\s+origin|paese\s+di\s+origine|"
    r"power\s+of\s+attorney|\bpoa\b|procura|lettera\s+di\s+delega|"
    r"aes\s+filing|electronic\s+export\s+information|"
    r"shipper\s+export\s+declaration|\bsed\b|\bsli\b|"
    r"exporter\s+ein|employer\s+identification\s+number|\beori\b"
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


def split_quoted_history(text: object) -> Tuple[str, str, bool]:
    """Return latest text, quoted-history text, and whether a quote marker was found."""
    normalized = normalize_whitespace(text)
    if not normalized:
        return "", "", False

    marker_match = QUOTE_HISTORY_MARKER_RE.search(normalized)
    if marker_match:
        # Some carrier templates begin with header-like lines such as
        # "DATA ... FROM ... OGGETTO ...".  Those are the actual current
        # request, not quoted history.  Do not discard the actionable body when
        # a quote-looking marker appears at the very beginning of the message.
        if marker_match.start() <= 80:
            early_tail = normalized[marker_match.start() : marker_match.start() + 1200]
            if not re.search(
                r"original\s+message|messaggio\s+originale|ha\s+scritto|wrote:",
                early_tail,
                re.IGNORECASE,
            ):
                return normalized, "", False

        return (
            normalized[: marker_match.start()].strip(),
            normalized[marker_match.start():].strip(),
            True,
        )

    return normalized, "", False


def strip_quoted_history(text: object) -> Tuple[str, bool]:
    """Return text before forwarded/quoted email markers."""
    latest_text, _, quoted_history_removed = split_quoted_history(text)
    return latest_text, quoted_history_removed


def strip_signature(text: object) -> Tuple[str, bool]:
    """Conservatively remove common email signatures after the request text."""
    normalized = normalize_whitespace(text)
    if not normalized:
        return "", False

    marker_match = SIGNATURE_MARKER_RE.search(normalized)
    if marker_match and marker_match.start() > 0:
        return normalized[: marker_match.start()].strip(), True

    return normalized, False


def extract_actionable_quoted_context(quoted_history: object) -> str:
    """Return useful request details from quoted history when it is clearly actionable."""
    quoted_text = normalize_whitespace(quoted_history)
    if not quoted_text or not ACTIONABLE_QUOTED_CONTEXT_RE.search(quoted_text):
        return ""

    start_match = ACTIONABLE_QUOTED_CONTEXT_START_RE.search(quoted_text)
    start = start_match.start() if start_match else 0
    context = quoted_text[start:].strip()

    end_match = QUOTED_CONTEXT_END_RE.search(context)
    if end_match and end_match.start() > 0:
        context = context[: end_match.start()].strip()

    if len(context) > MAX_RETAINED_QUOTED_CONTEXT_CHARS:
        context = context[:MAX_RETAINED_QUOTED_CONTEXT_CHARS].rstrip()

    return context


def append_useful_quoted_context(cleaned_latest_text: object, quoted_history: object) -> str:
    """Append actionable quoted context for follow-up/reminder emails.

    The default cleaner still removes quoted threads to avoid stale false positives.
    A carrier follow-up/reminder, however, often says only "following up" in the
    latest message while the actual requested data remains in the quoted original
    email.  In that narrow case, keep the actionable quoted excerpt so regex and
    LLM stages can see what is being requested.
    """
    latest_text = normalize_whitespace(cleaned_latest_text)
    if not latest_text or not FOLLOW_UP_CONTEXT_RE.search(latest_text):
        return latest_text

    quoted_context = extract_actionable_quoted_context(quoted_history)
    if not quoted_context:
        return latest_text

    if quoted_context.lower() in latest_text.lower():
        return latest_text

    return normalize_whitespace(
        f"{latest_text}\n\nRelevant quoted context retained for request classification:\n"
        f"{quoted_context}"
    )


def clean_latest_request_text(text: object) -> Dict[str, object]:
    """
    Clean an email body for intent detection.

    Returns a dictionary so callers can keep audit metadata in output files.
    """
    raw_text = normalize_whitespace(text)
    without_history, quoted_history, quoted_history_removed = split_quoted_history(raw_text)
    without_signature, signature_removed = strip_signature(without_history)
    cleaned_text = append_useful_quoted_context(without_signature, quoted_history)

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


def email_domain(email: object) -> str:
    normalized = normalize_email(email)
    if "@" not in normalized:
        return ""
    return normalized.rsplit("@", 1)[-1]


def email_matches_domain(email: object, domain: str) -> bool:
    current_domain = email_domain(email)
    wanted_domain = str(domain or "").strip().lower().lstrip("@")
    return bool(
        current_domain
        and wanted_domain
        and (current_domain == wanted_domain or current_domain.endswith("." + wanted_domain))
    )


def email_matches_any_carrier_domain(email: object) -> bool:
    return any(email_matches_domain(email, domain) for domain in CARRIER_EMAIL_DOMAINS)


def carrier_code_from_email(email: object) -> str:
    if email_matches_domain(email, "ups.com"):
        return "UPS"
    if email_matches_domain(email, "dhl.com"):
        return "DHL"
    if email_matches_domain(email, "fedex.com"):
        return "FEDEX"
    return ""


def is_ups_requester_email(email: object) -> bool:
    normalized = normalize_email(email)
    return normalized in UPS_BROKERAGE_EMAILS or email_matches_domain(email, "ups.com")


def is_fedex_requester_email(email: object) -> bool:
    normalized = normalize_email(email)
    return normalized == FEDEX_BROKERAGE_EMAIL or email_matches_domain(email, "fedex.com")


def is_dhl_requester_email(email: object) -> bool:
    normalized = normalize_email(email)
    return normalized == DHL_BROKERAGE_EMAIL or email_matches_domain(email, "dhl.com")




def is_no_action_carrier_notification(text: object) -> bool:
    """Return True for carrier-domain notifications that historically needed no reply."""
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return False

    # FedEx Support Hub messages are not reply-safe notifications: they require
    # a human to act in the portal, so they must pass through the human guardrail.
    if is_platform_handoff_request(cleaned):
        return False

    if STRONG_NO_ACTION_CARRIER_NOTIFICATION_RE.search(cleaned):
        return True

    if not NO_ACTION_CARRIER_NOTIFICATION_RE.search(cleaned):
        return False

    # Many actionable carrier emails include a generic no-reply footer. Do not
    # suppress them when the actual latest message asks for customs/order data.
    has_customs_request = bool(
        REQUEST_LANGUAGE_RE.search(cleaned) and CUSTOMS_KEYWORD_RE.search(cleaned)
    )
    return not has_customs_request


def classify_ticket_category_from_content(
    subject: object = "",
    request_body: object = "",
    requester_email: object = "",
) -> str:
    """
    Infer ticket_category for carrier-domain emails that are not present in the
    BigQuery requester configuration table.

    Configured emails still use their BigQuery category. This fallback keeps the
    category in the same three-value taxonomy used by the existing workflow.
    """
    text = normalize_whitespace(f"{subject or ''}\n{request_body or ''}")

    if RETURN_CATEGORY_RE.search(text):
        return RETURNS_CUSTOMS_CLEARANCE

    pending_match = bool(PENDING_ORDER_RELEASE_CATEGORY_RE.search(text))
    order_match = bool(ORDER_CUSTOMS_CLEARANCE_CATEGORY_RE.search(text))

    if pending_match and not order_match:
        return PENDING_ORDER_RELEASE

    if order_match:
        return ORDER_CUSTOMS_CLEARANCE

    if pending_match:
        return PENDING_ORDER_RELEASE

    # Carrier-domain customs tickets are safest under the generic order-customs
    # branch when the category is not explicit. Unknown requested_data still goes
    # to the LLM/human guardrails later in the pipeline.
    return ORDER_CUSTOMS_CLEARANCE

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
    On request number 1, EORI is ignored, and shipment instructions are
    fulfilled by export tracking plus UPS account data. Declaration-of-intent
    / libera-esportazione wording is not converted to commercial invoice.
    """
    values = normalize_requested_data(requested_data)
    if not values:
        return []

    result: List[str] = []
    embedded_fields_found = False
    rpi_document_fields_found = False
    first_request_rpi_response_fields_found = False
    first_request_shipment_instructions_found = False
    first_request = normalize_request_number(request_number) == 1
    first_returns = is_first_returns_customs_request(ticket_category, request_number)

    for value in values:
        if value == "declaration_of_intent":
            value = "dichiarazione_di_libera_esportazione"
        if value in RPI_DOCUMENT_EMBEDDED_REQUESTED_DATA:
            rpi_document_fields_found = True
            continue
        if first_request and value in FIRST_REQUEST_RPI_RESPONSE_EMBEDDED_REQUESTED_DATA:
            first_request_rpi_response_fields_found = True
            continue
        if first_request and value == "eori_number":
            continue
        if first_request and value == "shipment_instructions":
            first_request_shipment_instructions_found = True
            continue
        if value in DOCUMENT_EMBEDDED_REQUESTED_DATA:
            embedded_fields_found = True
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
    if first_returns and contact_fields_found:
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

    if first_request_shipment_instructions_found:
        for value in ("ups_account_number", "export_tracking_number"):
            if value not in result:
                result.append(value)

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


def _normalize_returns_first_request_bundle_values(value: object) -> List[str]:
    values: List[str] = []
    for item in normalize_requested_data(value):
        normalized = RETURNS_CUSTOMS_FIRST_REQUEST_DATA_ALIASES.get(item, item)
        if normalized and normalized not in values:
            values.append(normalized)
    return values


def expand_first_returns_customs_clearance_bundle(
    requested_data: object,
    ticket_category: object = "",
    request_number: object = 1,
    requester_email: object = "",
    trigger_requested_data: object = None,
) -> List[str]:
    """Expand first UPS Returns Customs Clearance regex hits to the full bundle.

    The first UPS return-clearance reply must include the export tracking
    number, return proforma invoice, UPS account number, and returned-items
    confirmation together.  If regex/effective requested_data detects any one
    of those fields on request number 1, downstream enrichment and response
    generation should prepare all four instead of answering only the partial
    match.

    The expansion is scoped to UPS-style contexts to avoid adding a UPS account
    number to DHL/FedEx RPI-only cases.  A non-UPS sender can still trigger the
    bundle when the detected data explicitly includes ups_account_number.
    """
    values = _normalize_returns_first_request_bundle_values(requested_data)
    triggers = _normalize_returns_first_request_bundle_values(
        trigger_requested_data if trigger_requested_data is not None else requested_data
    )

    if not is_first_returns_customs_request(ticket_category, request_number):
        return values

    if HUMAN_INTERVENTION_REQUIRED in values or UNKNOWN_REQUEST in values:
        return values

    trigger_set = set(triggers)
    value_set = set(values)
    if not (trigger_set | value_set) & RETURNS_CUSTOMS_FIRST_REQUEST_TRIGGER_DATA:
        return values

    ups_bundle_context = (
        is_ups_requester_email(requester_email)
        or "ups_account_number" in trigger_set
        or "ups_account_number" in value_set
    )
    if not ups_bundle_context:
        return values

    expanded: List[str] = list(RETURNS_CUSTOMS_FIRST_REQUEST_REQUIRED_DATA)
    for value in values:
        if value not in expanded:
            expanded.append(value)
    return expanded


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


def is_noreply_requester_email(email: object) -> bool:
    """Return True when the requester/sender email starts with ``noreply``.

    Zendesk normally stores requester_email as a bare address, but this helper
    also tolerates display-name strings such as ``Name <noreply@example.com>``.
    """
    normalized = normalize_email(email)
    if not normalized:
        return False

    if normalized.startswith("noreply"):
        return True

    for match in re.finditer(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", normalized):
        local_part = match.group(0).split("@", 1)[0]
        if local_part.startswith("noreply"):
            return True

    return False


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


def _flatten_requested_data_values(value: object) -> List[object]:
    """Flatten nested requested_data values, including BigQuery JSON exports.

    BigQuery CSV exports may serialize repeated fields as values like
    {"v":[{"v":"customer_email"},{"v":"customer_phone"}]}. Older code
    treated the whole JSON object as one requested-data key, which produced
    unusable drafts such as '- {"V":[...]}: [TO BE RETRIEVED]'.
    """
    if _is_missing(value):
        return []

    if isinstance(value, dict):
        raw_values: List[object] = []
        # BigQuery repeated/record export shape.
        for key in ("v", "V", "requested_data", "request_types", "value", "name"):
            if key in value:
                raw_values.extend(_flatten_requested_data_values(value.get(key)))
        if raw_values:
            return raw_values
        for nested_value in value.values():
            raw_values.extend(_flatten_requested_data_values(nested_value))
        return raw_values

    if isinstance(value, (list, tuple, set)):
        raw_values = []
        for item in value:
            raw_values.extend(_flatten_requested_data_values(item))
        return raw_values

    return [value]


def normalize_requested_data(value: object) -> List[str]:
    """Normalize a requested_data value from Python, Excel, CSV, or BigQuery JSON."""
    if _is_missing(value):
        return []

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []

        parsed = None
        parsed_ok = False
        try:
            parsed = ast.literal_eval(text)
            parsed_ok = True
        except Exception:
            parsed_ok = False

        if parsed_ok and isinstance(parsed, (list, tuple, set, dict)):
            raw_values = _flatten_requested_data_values(parsed)
        else:
            # Tolerate malformed BigQuery repeated-field strings, for example
            # a broken JSON fragment containing {"v":"return_proforma_invoice"}.
            # In that case extracting known requested-data tokens is safer than
            # returning unusable comma-split fragments.
            extracted_tokens = KNOWN_REQUESTED_DATA_TOKEN_RE.findall(text)
            raw_values = extracted_tokens if extracted_tokens else [item.strip() for item in text.split(",")]
    else:
        raw_values = _flatten_requested_data_values(value)

    cleaned = []
    for item in raw_values:
        item = str(item or "").strip()
        if item and item not in cleaned:
            cleaned.append(item)

    return cleaned


def _normalized_requested_type_set(value: object) -> set[str]:
    return {str(item or "").strip() for item in normalize_requested_data(value) if str(item or "").strip()}


def has_libera_esportazione_regex_type(regex_request_types: object) -> bool:
    values = _normalized_requested_type_set(regex_request_types)
    return bool(values & LIBERA_ESPORTAZIONE_REQUEST_TYPES)


def only_libera_esportazione_regex_type(regex_request_types: object) -> bool:
    values = _normalized_requested_type_set(regex_request_types)
    actionable_values = {
        value
        for value in values
        if value not in {"exclude_from_processing", HUMAN_INTERVENTION_REQUIRED, UNKNOWN_REQUEST}
    }
    return bool(actionable_values) and actionable_values.issubset(
        LIBERA_ESPORTAZIONE_REQUEST_TYPES
    )


def only_libera_esportazione_requested_data(requested_data: object) -> bool:
    values = _normalized_requested_type_set(requested_data)
    return bool(values) and values.issubset(LIBERA_ESPORTAZIONE_REQUEST_TYPES)


def strip_libera_esportazione_requested_data(requested_data: object) -> List[str]:
    return [
        value
        for value in normalize_requested_data(requested_data)
        if value not in LIBERA_ESPORTAZIONE_REQUEST_TYPES
    ]


def filter_libera_esportazione_response_data(
    requested_data: object,
    regex_request_types: object,
) -> List[str]:
    """Remove libera-esportazione declarations from automatic response data.

    If the declaration is the only regex-detected request type, return no
    response data. If it appears together with other regex-detected request
    types, keep the other response data only.
    """

    values = normalize_requested_data(requested_data)
    if not has_libera_esportazione_regex_type(regex_request_types):
        return values

    source_values = _normalized_requested_type_set(regex_request_types)
    keep_commercial_invoice = bool(source_values & COMMERCIAL_INVOICE_REQUEST_TYPES)

    return [
        value
        for value in values
        if value not in LIBERA_ESPORTAZIONE_REQUEST_TYPES
        and (value != "commercial_invoice" or keep_commercial_invoice)
    ]


def normalize_tracking_for_comparison(value: object) -> str:
    if _is_missing(value):
        return ""
    return re.sub(r"[^0-9A-Z]+", "", str(value or "").upper())


def tracking_comparison_variants(value: object) -> List[str]:
    """Return normalized variants for comparing tracking/order identifiers.

    Shipment-order numbers are sometimes represented as ``DG-EUA01667868`` in
    extracted text and as ``DG-EU-01667868`` in GET_FULL_ORDER payloads/links.
    Both refer to the same EU order.  The same pattern exists for US orders.
    Keeping the original normalized value plus the collapsed market-code variant
    prevents invoice/RPI routing from validating the wrong document type.
    """

    normalized = normalize_tracking_for_comparison(value)
    if not normalized:
        return []

    variants = [normalized]

    # Brand + market + variant letter + digits, for example:
    # DGEUA01667868 -> DGEU01667868 and DGUSA11590412 -> DGUS11590412.
    match = re.match(r"^([A-Z0-9]{2})(EU|US)[A-Z](\d+)$", normalized)
    if match:
        collapsed = "".join(match.groups())
        if collapsed not in variants:
            variants.append(collapsed)

    return variants


def tracking_numbers_match(left: object, right: object) -> bool:
    left_variants = tracking_comparison_variants(left)
    right_variants = tracking_comparison_variants(right)
    if not left_variants or not right_variants:
        return False

    for left_normalized in left_variants:
        for right_normalized in right_variants:
            if left_normalized == right_normalized:
                return True
            if len(left_normalized) >= 8 and left_normalized in right_normalized:
                return True
            if len(right_normalized) >= 8 and right_normalized in left_normalized:
                return True
    return False


def extracted_tracking_number_kind(
    extracted_tracking_number: object,
    shipment_tracking_number: object,
    return_tracking_number: object,
) -> str:
    """Return shipment, return, or empty when the extracted TRK is ambiguous."""

    return_match = tracking_numbers_match(extracted_tracking_number, return_tracking_number)
    shipment_match = tracking_numbers_match(extracted_tracking_number, shipment_tracking_number)

    if return_match and not shipment_match:
        return "return"
    if shipment_match and not return_match:
        return "shipment"
    return ""


def align_invoice_requested_data_with_tracking_context(
    requested_data: object,
    request_type_sources: object = None,
    *,
    extracted_tracking_number: object = None,
    shipment_tracking_number: object = None,
    return_tracking_number: object = None,
) -> List[str]:
    """Swap invoice/RPI response data when regex intent conflicts with TRK role."""

    values = normalize_requested_data(requested_data)
    source_values = _normalized_requested_type_set(
        request_type_sources if request_type_sources is not None else requested_data
    )
    tracking_kind = extracted_tracking_number_kind(
        extracted_tracking_number,
        shipment_tracking_number,
        return_tracking_number,
    )

    if tracking_kind == "return" and source_values & COMMERCIAL_INVOICE_REQUEST_TYPES:
        values = [
            value
            for value in values
            if value not in COMMERCIAL_INVOICE_REQUEST_TYPES
            and value != "commercial_invoice"
        ]
        if "return_proforma_invoice" not in values:
            values.append("return_proforma_invoice")

    elif tracking_kind == "shipment" and source_values & RETURN_PROFORMA_REQUEST_TYPES:
        values = [
            value
            for value in values
            if value not in RETURN_PROFORMA_REQUEST_TYPES
            and value != "return_proforma_invoice"
        ]
        if "commercial_invoice" not in values:
            values.append("commercial_invoice")

    return values


def is_atr_certificate_mandate_request(text: object) -> bool:
    """True for ATR-certificate mandate/forms, which are not POA replies."""

    return bool(ATR_CERTIFICATE_MANDATE_RE.search(normalize_whitespace(text)))


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

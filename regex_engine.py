"""
regex_engine.py

Rule-based requested-data detection using regexes stored in:
`tlg-business-intelligence-prd.til.log_one_tool_army_request_regex_config`

The regex layer is intentionally conservative:
- detection runs on the latest requester message only, after quoted-history and
  signature cleanup;
- acknowledgement words are treated as exclusions only when the whole cleaned
  message is an acknowledgement;
- commercial-invoice boilerplate is suppressed so invoice templates do not
  become false requests for phone, country of origin, product details, etc.;
- deprecated standalone fields such as tax information, country of origin and
  product description are collapsed into invoice/RPI document requests;
- uncertain matches are sent to the LLM/human-review stage instead of being
  auto-answered.

Creates:
- output/regex_matches.jsonl.gz       high-confidence/excluded regex-resolved rows
- output/unmatched_tickets.jsonl.gz   unmatched or review-needed rows
"""

import json
import os
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Tuple

import pandas as pd
from pipeline_io import (
    REGEX_MATCHES_PATH,
    UNMATCHED_TICKETS_PATH,
    write_dataframe,
)
from customs_rules import (
    HUMAN_INTERVENTION_REQUIRED,
    UNKNOWN_REQUEST,
    clean_latest_request_text,
    collapse_document_embedded_requested_data,
    expand_first_returns_customs_clearance_bundle,
    classify_ticket_category_from_content,
    contains_correction_or_discrepancy,
    get_standard_reply_requested_data,
    has_actionable_request_language,
    is_customer_refused_return_request,
    is_delivery_address_phone_unreachable_request,
    is_explicit_export_tracking_request,
    is_explicit_ups_account_request,
    is_informative_status_update_only,
    is_missing_invoice_request,
    is_no_action_carrier_notification,
    is_acknowledgement_only,
    is_platform_handoff_request,
    is_request_number_3_or_higher,
    normalize_request_number,
    is_special_followup_ticket,
    is_tracking_reference_only,
    is_ups_account_boilerplate_context,
    is_unpaid_extra_charges_request,
    is_ups_receiver_contact_clearance_request,
    is_ups_uk_import_clearance_instructions_request,
    looks_like_commercial_invoice_boilerplate,
    requested_data_already_answered_by_first_reply,
)

PROJECT_ID = "tlg-business-intelligence-prd"
REGEX_CONFIG_TABLE = (
    "tlg-business-intelligence-prd.til.log_one_tool_army_request_regex_config"
)

# Regex config still stores request_type.  Downstream components use requested_data.
REQUEST_TYPE_TO_REQUESTED_DATA = {
    "invoice": ["commercial_invoice"],
    "return_proforma_invoice": ["return_proforma_invoice"],
    "invoice_correction": ["corrected_invoice"],
    "tracking_number": ["export_tracking_number"],
    "ups_account": ["ups_account_number"],
    "value_confirmation": ["value_confirmation"],
    "returned_items": ["returned_items_confirmation"],
    "customs_description": ["return_proforma_invoice"],
    "declaration_of_intent": ["dichiarazione_di_libera_esportazione"],
    "dichiarazione_di_libera_esportazione": ["dichiarazione_di_libera_esportazione"],
    "eori": ["eori_number"],
    "poa": ["power_of_attorney"],
    "power_of_attorney": ["power_of_attorney"],
    "tax_information": ["tax_information"],
    "country_of_origin": ["country_of_origin"],
    "importer_details": ["return_proforma_invoice"],
    "address_translation": ["address_translation"],
    "exporter_ein": ["exporter_ein"],
    "customer_phone": ["customer_phone"],
    "customer_email": ["customer_email"],
    "customer_name": ["customer_name"],
    "shipping_address": ["shipping_address"],
    "authorization_letter": ["authorization_letter"],
    "shipment_instructions": ["shipment_instructions"],
    "address_correction": ["address_correction"],
    "product_description": ["product_description"],
    "reminder_ticket": ["previously_requested_documentation"],
    "exclude_from_processing": [],
    "human_intervention_required": [HUMAN_INTERVENTION_REQUIRED],
}

# These request types frequently appeared inside carrier commercial-invoice
# templates.  When an invoice template is detected, keep the invoice request and
# suppress these boilerplate-derived data elements unless a later LLM/human
# review confirms them.
INVOICE_BOILERPLATE_SUSCEPTIBLE_TYPES = {
    "country_of_origin",
    "customer_phone",
    "customer_email",
    "customer_name",
    "shipping_address",
    "product_description",
    "customs_description",
    "importer_details",
    "value_confirmation",
}

# Regex should auto-answer only narrow, high-confidence cases.  Cases with many
# inferred data elements are historically dominated by templates/quoted text.
MAX_AUTO_REQUESTED_DATA = int(os.getenv("REGEX_MAX_AUTO_REQUESTED_DATA", "3"))
HIGH_CONFIDENCE = float(os.getenv("REGEX_HIGH_CONFIDENCE", "0.95"))
REVIEW_CONFIDENCE = float(os.getenv("REGEX_REVIEW_CONFIDENCE", "0.40"))


class RegexEngine:

    def __init__(self):
        # Import Google clients lazily so local guardrail tests can run without
        # BigQuery dependencies installed.
        from google.cloud import bigquery
        from google.oauth2 import service_account

        creds = json.loads(os.environ["BI_BIGQUERY_CREDS"])
        credentials = service_account.Credentials.from_service_account_info(creds)

        self.bq = bigquery.Client(
            project=PROJECT_ID,
            credentials=credentials,
        )

        self.regex_map = self._load_regexes()

    def _load_regexes(self):
        query = f"""
        SELECT request_type, regex_pattern
        FROM `{REGEX_CONFIG_TABLE}`
        """

        rows = self.bq.query(query).result()
        regex_map = defaultdict(list)

        for row in rows:
            request_type = row["request_type"]
            regex_pattern = str(row["regex_pattern"])

            try:
                regex_map[request_type].append(
                    re.compile(regex_pattern, re.IGNORECASE)
                )
            except re.error as exc:
                print(
                    f"Skipping invalid regex for {request_type}: "
                    f"{regex_pattern} | {exc}"
                )

        return dict(regex_map)

    def load_active_tickets(self):
        query = """
        SELECT
            request_id,
            request_submission_timestamp,
            ticket_submission_timestamp,
            zendesk_ticket_id,
            requester_email,
            subject,
            request_body,
            request_number,
            ticket_category,
            extracted_tracking_number,
            shipment_order_number,
            shipment_tracking_number,
            return_tracking_number,
            shipment_carrier_code,
            return_carrier_code,
            tracking_not_found_in_shipping_platform_shipments
        FROM `tlg-business-intelligence-prd.til.log_one_tool_army_active_tickets`
        """

        return list(self.bq.query(query).result())

    def process_tickets(self):
        tickets = self.load_active_tickets()

        matched_results = []
        unmatched_tickets = []
        excluded_count = 0

        for ticket in tickets:
            request_text = ticket["request_body"] or ""
            ticket_category = ticket["ticket_category"] or classify_ticket_category_from_content(
                subject=ticket.get("subject", ""),
                request_body=request_text,
                requester_email=ticket.get("requester_email", ""),
            )
            result = self.detect(
                request_text,
                requester_email=ticket["requester_email"],
                request_number=ticket["request_number"],
                ticket_category=ticket_category,
                tracking_not_found_in_shipping_platform_shipments=ticket.get(
                    "tracking_not_found_in_shipping_platform_shipments"
                ),
            )

            output_row = {
                "request_id": ticket.get("request_id"),
                "request_submission_timestamp": ticket.get("request_submission_timestamp"),
                "ticket_submission_timestamp": ticket.get("ticket_submission_timestamp"),
                "zendesk_ticket_id": ticket["zendesk_ticket_id"],
                "request_number": ticket["request_number"],
                "requester_email": ticket["requester_email"],
                "subject": ticket["subject"],
                "request_body": request_text,
                "cleaned_request_body": result.pop("cleaned_request_text", ""),
                "ticket_category": ticket_category,
                "extracted_tracking_number": ticket["extracted_tracking_number"],
                "shipment_order_number": ticket["shipment_order_number"],
                "shipment_tracking_number": ticket["shipment_tracking_number"],
                "return_tracking_number": ticket["return_tracking_number"],
                "shipment_carrier_code": ticket.get("shipment_carrier_code"),
                "return_carrier_code": ticket.get("return_carrier_code"),
                "tracking_not_found_in_shipping_platform_shipments": ticket.get(
                    "tracking_not_found_in_shipping_platform_shipments"
                ),
                **result,
            }

            if result.get("excluded"):
                excluded_count += 1
                matched_results.append(output_row)
                print(f"Excluded ticket {ticket['zendesk_ticket_id']}")

            elif result["matched"]:
                matched_results.append(output_row)

            else:
                unmatched_tickets.append(output_row)

        return matched_results, unmatched_tickets, excluded_count

    def _find_matches(self, request_text: str) -> Tuple[List[str], List[dict]]:
        request_types: List[str] = []
        matched_spans: List[dict] = []

        for request_type, patterns in self.regex_map.items():
            for pattern in patterns:
                match = pattern.search(request_text)
                if not match:
                    continue

                if request_type not in request_types:
                    request_types.append(request_type)

                matched_spans.append(
                    {
                        "request_type": request_type,
                        "span": match.group(0)[:160],
                        "start": match.start(),
                        "end_pos": match.end(),
                    }
                )
                break

        return request_types, matched_spans

    @staticmethod
    def _requested_data_from_types(request_types: Iterable[str]) -> List[str]:
        return sorted(
            {
                item
                for request_type in request_types
                for item in REQUEST_TYPE_TO_REQUESTED_DATA.get(request_type, [])
            }
        )

    @staticmethod
    def _as_output(
        *,
        matched: bool,
        excluded: bool,
        request_types: List[str],
        requested_data: List[str],
        cleaned_request_text: str,
        matched_spans: List[dict],
        confidence: float,
        notes: str,
        needs_llm_confirmation: bool = False,
        force_human_intervention: bool = False,
        human_intervention_required: bool = False,
        standard_reply_requested_data: List[str] = None,
        regex_request_types: List[str] = None,
        regex_requested_data: List[str] = None,
        quoted_history_removed: bool = False,
        signature_removed: bool = False,
    ) -> Dict[str, object]:
        return {
            "engine": "regex",
            "matched": matched,
            "excluded": excluded,
            "request_types": request_types,
            "requested_data": requested_data,
            "confidence": confidence,
            "regex_confidence": confidence,
            "llm_confidence": None,
            "notes": notes,
            "needs_llm_confirmation": needs_llm_confirmation,
            "force_human_intervention": force_human_intervention,
            "human_intervention_required": human_intervention_required,
            "standard_reply_requested_data": standard_reply_requested_data or [],
            "needs_standard_reply_confirmation": False,
            "regex_request_types": regex_request_types or request_types,
            "regex_requested_data": regex_requested_data or requested_data,
            "matched_spans": matched_spans,
            "llm_was_used": False,
            "cleaned_request_text": cleaned_request_text,
            "quoted_history_removed": quoted_history_removed,
            "signature_removed": signature_removed,
        }

    def detect(
        self,
        request_text: str,
        requester_email: object = "",
        request_number: object = 1,
        ticket_category: object = "",
        tracking_not_found_in_shipping_platform_shipments: object = False,
    ):
        text_details = clean_latest_request_text(request_text)
        cleaned_text = str(text_details["cleaned_request_text"] or "")
        raw_text = str(text_details["raw_request_text"] or "")
        quoted_history_removed = bool(text_details["quoted_history_removed"])
        signature_removed = bool(text_details["signature_removed"])

        tracking_not_found = str(
            tracking_not_found_in_shipping_platform_shipments or ""
        ).strip().lower() in {"true", "1", "yes", "y"}

        if is_no_action_carrier_notification(cleaned_text):
            return self._as_output(
                matched=True,
                excluded=True,
                request_types=["exclude_from_processing"],
                requested_data=[],
                cleaned_request_text=cleaned_text,
                matched_spans=[
                    {
                        "request_type": "exclude_from_processing",
                        "span": "carrier notification/no-reply status message",
                        "start": 0,
                        "end_pos": 0,
                    }
                ],
                confidence=HIGH_CONFIDENCE,
                notes=(
                    "Carrier-domain notification/status message historically required "
                    "no customer-facing reply. Excluded before tracking-number guardrails."
                ),
                regex_request_types=["exclude_from_processing"],
                regex_requested_data=[],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        if tracking_not_found:
            return self._as_output(
                matched=False,
                excluded=False,
                request_types=[],
                requested_data=[],
                cleaned_request_text=cleaned_text,
                matched_spans=[],
                confidence=0.0,
                notes=(
                    "Tracking number extracted from the ticket was not found in "
                    "tlg-business-intelligence-prd.bi.shipping_platform_shipments. "
                    "Regex processing skipped; send to LLM for a human-intervention draft."
                ),
                needs_llm_confirmation=True,
                regex_request_types=[],
                regex_requested_data=[],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        if is_request_number_3_or_higher(request_number):
            return self._as_output(
                matched=True,
                excluded=False,
                request_types=[HUMAN_INTERVENTION_REQUIRED],
                requested_data=[HUMAN_INTERVENTION_REQUIRED],
                cleaned_request_text=cleaned_text,
                matched_spans=[],
                confidence=0.0,
                notes=(
                    "Request number is 3 or higher. Human intervention is required "
                    "by automation policy."
                ),
                force_human_intervention=True,
                human_intervention_required=True,
                regex_request_types=[HUMAN_INTERVENTION_REQUIRED],
                regex_requested_data=[HUMAN_INTERVENTION_REQUIRED],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        if is_platform_handoff_request(cleaned_text):
            return self._as_output(
                matched=True,
                excluded=False,
                request_types=[HUMAN_INTERVENTION_REQUIRED],
                requested_data=[HUMAN_INTERVENTION_REQUIRED],
                cleaned_request_text=cleaned_text,
                matched_spans=[
                    {
                        "request_type": HUMAN_INTERVENTION_REQUIRED,
                        "span": "FedEx Support Hub platform handoff",
                        "start": 0,
                        "end_pos": 0,
                    }
                ],
                confidence=HIGH_CONFIDENCE,
                notes=(
                    "Sender asked to provide information/instructions through "
                    "FedEx Support Hub. Human intervention is required because "
                    "a human must handle the external platform."
                ),
                force_human_intervention=True,
                human_intervention_required=True,
                regex_request_types=[HUMAN_INTERVENTION_REQUIRED],
                regex_requested_data=[HUMAN_INTERVENTION_REQUIRED],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        if is_unpaid_extra_charges_request(cleaned_text):
            return self._as_output(
                matched=True,
                excluded=False,
                request_types=[HUMAN_INTERVENTION_REQUIRED],
                requested_data=[HUMAN_INTERVENTION_REQUIRED],
                cleaned_request_text=cleaned_text,
                matched_spans=[
                    {
                        "request_type": HUMAN_INTERVENTION_REQUIRED,
                        "span": "customer did not pay extra/outstanding charges",
                        "start": 0,
                        "end_pos": 0,
                    }
                ],
                confidence=HIGH_CONFIDENCE,
                notes=(
                    "Customer did not pay extra/outstanding charges. Human intervention "
                    "is required to verify whether the customer or TLG should pay."
                ),
                needs_llm_confirmation=False,
                force_human_intervention=True,
                human_intervention_required=True,
                regex_request_types=[HUMAN_INTERVENTION_REQUIRED],
                regex_requested_data=[HUMAN_INTERVENTION_REQUIRED],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        if is_informative_status_update_only(cleaned_text):
            return self._as_output(
                matched=True,
                excluded=False,
                request_types=[HUMAN_INTERVENTION_REQUIRED],
                requested_data=[HUMAN_INTERVENTION_REQUIRED],
                cleaned_request_text=cleaned_text,
                matched_spans=[
                    {
                        "request_type": HUMAN_INTERVENTION_REQUIRED,
                        "span": "informative status update only",
                        "start": 0,
                        "end_pos": 0,
                    }
                ],
                confidence=HIGH_CONFIDENCE,
                notes=(
                    "The latest message is an informative status update/confirmation, "
                    "not a request for data. Human intervention is required."
                ),
                force_human_intervention=True,
                human_intervention_required=True,
                regex_request_types=[HUMAN_INTERVENTION_REQUIRED],
                regex_requested_data=[HUMAN_INTERVENTION_REQUIRED],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        if is_customer_refused_return_request(cleaned_text):
            return self._as_output(
                matched=True,
                excluded=False,
                request_types=["ups_account"],
                requested_data=["ups_account_number"],
                cleaned_request_text=cleaned_text,
                matched_spans=[
                    {
                        "request_type": "ups_account",
                        "span": "customer refused package; return-to-shipper costs authorization",
                        "start": 0,
                        "end_pos": 0,
                    }
                ],
                confidence=HIGH_CONFIDENCE,
                notes=(
                    "Customer/receiver refused the package and the carrier asks how "
                    "to proceed. Use the UPS account/LOA return-cost response."
                ),
                regex_request_types=["ups_account"],
                regex_requested_data=["ups_account_number"],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        request_types, matched_spans = self._find_matches(cleaned_text)
        raw_request_types, raw_matched_spans = self._find_matches(raw_text)

        if is_missing_invoice_request(cleaned_text) and "invoice" not in request_types:
            request_types.append("invoice")
            matched_spans.append(
                {
                    "request_type": "invoice",
                    "span": "shipment held because invoice is missing",
                    "start": 0,
                    "end_pos": 0,
                }
            )

        if (
            is_delivery_address_phone_unreachable_request(cleaned_text)
            and "shipping_address" not in request_types
        ):
            request_types.extend(
                request_type
                for request_type in [
                    "shipping_address",
                    "customer_phone",
                    "customer_email",
                ]
                if request_type not in request_types
            )
            matched_spans.append(
                {
                    "request_type": "shipping_address",
                    "span": "address unknown / street number missing and phone unreachable",
                    "start": 0,
                    "end_pos": 0,
                }
            )

        if is_ups_uk_import_clearance_instructions_request(cleaned_text):
            return self._as_output(
                matched=True,
                excluded=False,
                request_types=[HUMAN_INTERVENTION_REQUIRED],
                requested_data=[HUMAN_INTERVENTION_REQUIRED],
                cleaned_request_text=cleaned_text,
                matched_spans=[
                    {
                        "request_type": HUMAN_INTERVENTION_REQUIRED,
                        "span": "UPS UK import clearance instruction template",
                        "start": 0,
                        "end_pos": 0,
                    }
                ],
                confidence=HIGH_CONFIDENCE,
                notes=(
                    "UPS UK import-clearance instruction request asks for customs "
                    "procedure, EORI/DAN/deferment approval, commodity details, or "
                    "possible extra charges. Human intervention is required instead "
                    "of sending a partial RPI/UPS-account reply."
                ),
                force_human_intervention=True,
                human_intervention_required=True,
                regex_request_types=[HUMAN_INTERVENTION_REQUIRED],
                regex_requested_data=[HUMAN_INTERVENTION_REQUIRED],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        if HUMAN_INTERVENTION_REQUIRED in request_types:
            return self._as_output(
                matched=True,
                excluded=False,
                request_types=[HUMAN_INTERVENTION_REQUIRED],
                requested_data=[HUMAN_INTERVENTION_REQUIRED],
                cleaned_request_text=cleaned_text,
                matched_spans=[
                    span
                    for span in matched_spans
                    if span.get("request_type") == HUMAN_INTERVENTION_REQUIRED
                ],
                confidence=HIGH_CONFIDENCE,
                notes="Regex table classified this request as human intervention required.",
                force_human_intervention=True,
                human_intervention_required=True,
                regex_request_types=[HUMAN_INTERVENTION_REQUIRED],
                regex_requested_data=[HUMAN_INTERVENTION_REQUIRED],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        real_request_types = [
            request_type
            for request_type in request_types
            if request_type != "exclude_from_processing"
        ]
        raw_real_request_types = [
            request_type
            for request_type in raw_request_types
            if request_type != "exclude_from_processing"
        ]

        exclude_match = "exclude_from_processing" in request_types
        has_real_match = bool(real_request_types)

        # A pure acknowledgement can be safely excluded.  A message containing
        # both "grazie/thanks" and actionable language must never be suppressed.
        if exclude_match and not has_real_match:
            if is_acknowledgement_only(cleaned_text):
                return self._as_output(
                    matched=True,
                    excluded=True,
                    request_types=["exclude_from_processing"],
                    requested_data=[],
                    cleaned_request_text=cleaned_text,
                    matched_spans=matched_spans,
                    confidence=HIGH_CONFIDENCE,
                    notes="Cleaned message is acknowledgement-only.",
                    quoted_history_removed=quoted_history_removed,
                    signature_removed=signature_removed,
                )

            return self._as_output(
                matched=False,
                excluded=False,
                request_types=[],
                requested_data=[],
                cleaned_request_text=cleaned_text,
                matched_spans=matched_spans,
                confidence=REVIEW_CONFIDENCE,
                notes=(
                    "Acknowledgement pattern matched, but the cleaned message is "
                    "not acknowledgement-only. Send to LLM/human review instead "
                    "of excluding."
                ),
                needs_llm_confirmation=True,
                regex_request_types=["exclude_from_processing"],
                regex_requested_data=[],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        # If the old/full thread matched but the latest cleaned message did not,
        # the regex likely found stale quoted history.  This should not be used
        # to prepare a customer-facing answer.
        if not has_real_match and raw_real_request_types:
            return self._as_output(
                matched=False,
                excluded=False,
                request_types=[],
                requested_data=[],
                cleaned_request_text=cleaned_text,
                matched_spans=raw_matched_spans,
                confidence=0.0,
                notes=(
                    "Regex matched only text removed during quoted-history/signature "
                    "cleanup. Human intervention required to inspect the thread."
                ),
                force_human_intervention=True,
                human_intervention_required=True,
                regex_request_types=raw_real_request_types,
                regex_requested_data=self._requested_data_from_types(raw_real_request_types),
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        if not has_real_match:
            notes = "No high-confidence regex requested_data match."
            if has_actionable_request_language(cleaned_text):
                notes += " Actionable language exists, so LLM extraction is required."
            return self._as_output(
                matched=False,
                excluded=False,
                request_types=[],
                requested_data=[],
                cleaned_request_text=cleaned_text,
                matched_spans=matched_spans,
                confidence=0.0,
                notes=notes,
                needs_llm_confirmation=True,
                regex_request_types=[],
                regex_requested_data=[],
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )

        suppressed_types: List[str] = []
        effective_request_types = list(real_request_types)

        if (
            "ups_account" in effective_request_types
            and not is_explicit_ups_account_request(cleaned_text)
            and not is_ups_uk_import_clearance_instructions_request(cleaned_text)
            and (
                is_ups_receiver_contact_clearance_request(cleaned_text)
                or is_ups_account_boilerplate_context(cleaned_text)
            )
        ):
            effective_request_types = [
                request_type
                for request_type in effective_request_types
                if request_type != "ups_account"
            ]
            suppressed_types.append("ups_account")

        if (
            "tracking_number" in effective_request_types
            and not is_explicit_export_tracking_request(cleaned_text)
            and not is_ups_uk_import_clearance_instructions_request(cleaned_text)
            and is_tracking_reference_only(cleaned_text)
        ):
            effective_request_types = [
                request_type
                for request_type in effective_request_types
                if request_type != "tracking_number"
            ]
            suppressed_types.append("tracking_number")

        if is_ups_receiver_contact_clearance_request(cleaned_text):
            if not is_missing_invoice_request(cleaned_text):
                removed_document_types = [
                    request_type
                    for request_type in effective_request_types
                    if request_type in {"invoice", "return_proforma_invoice"}
                ]
                if removed_document_types:
                    effective_request_types = [
                        request_type
                        for request_type in effective_request_types
                        if request_type not in {"invoice", "return_proforma_invoice"}
                    ]
                    suppressed_types.extend(removed_document_types)

            if "power_of_attorney" not in effective_request_types:
                for request_type in ["customer_email", "customer_phone"]:
                    if request_type not in effective_request_types:
                        effective_request_types.append(request_type)
            elif "customer_phone" not in effective_request_types:
                effective_request_types.append("customer_phone")

        if "invoice" in effective_request_types and looks_like_commercial_invoice_boilerplate(cleaned_text):
            effective_request_types = [
                request_type
                for request_type in effective_request_types
                if request_type not in INVOICE_BOILERPLATE_SUSCEPTIBLE_TYPES
            ]
            suppressed_types = sorted(
                set(suppressed_types)
                | (set(real_request_types) - set(effective_request_types))
            )

        requested_data = collapse_document_embedded_requested_data(
            self._requested_data_from_types(effective_request_types),
            ticket_category=ticket_category,
            request_number=request_number,
            requester_email=requester_email,
            request_text=cleaned_text,
        )
        regex_requested_data = collapse_document_embedded_requested_data(
            self._requested_data_from_types(real_request_types),
            ticket_category=ticket_category,
            request_number=request_number,
            requester_email=requester_email,
            request_text=cleaned_text,
        )
        requested_data_before_bundle = list(requested_data)
        regex_requested_data = expand_first_returns_customs_clearance_bundle(
            regex_requested_data,
            ticket_category=ticket_category,
            request_number=request_number,
            requester_email=requester_email,
            trigger_requested_data=requested_data,
        )
        requested_data = expand_first_returns_customs_clearance_bundle(
            requested_data,
            ticket_category=ticket_category,
            request_number=request_number,
            requester_email=requester_email,
            trigger_requested_data=requested_data,
        )

        audit_notes: List[str] = []
        review_reasons: List[str] = []
        force_human = False

        if requested_data != requested_data_before_bundle:
            audit_notes.append(
                "Expanded first Returns Customs Clearance regex match to the "
                "full UPS first-request bundle."
            )

        if suppressed_types:
            audit_notes.append(
                "Suppressed boilerplate/reference fields: "
                + ", ".join(suppressed_types)
            )

        # Request number 1 often contains the complete first carrier checklist.
        # Do not apply the multi-field auto-answer limit on first requests.
        # Request number 3+ is already forced to human intervention above.
        if (
            normalize_request_number(request_number) != 1
            and len(requested_data) > MAX_AUTO_REQUESTED_DATA
        ):
            review_reasons.append(
                f"Regex detected {len(requested_data)} requested_data values, "
                "which exceeds the auto-answer limit."
            )
            force_human = True

        if contains_correction_or_discrepancy(cleaned_text) and (
            {"invoice", "invoice_correction", "value_confirmation", "return_proforma_invoice"}
            & set(real_request_types)
        ):
            review_reasons.append(
                "Correction/discrepancy language detected for invoice/value data. "
                "Manual verification is required instead of treating it as a normal RPI package."
            )
            force_human = True

        if not requested_data:
            review_reasons.append(
                "Regex candidates were removed by safety filters."
            )
            force_human = True

        standard_reply_requested_data = []
        needs_standard_reply_confirmation = False
        if is_special_followup_ticket(requester_email, request_number):
            standard_reply_requested_data = get_standard_reply_requested_data(requester_email)
            if requested_data_already_answered_by_first_reply(requested_data, requester_email):
                needs_standard_reply_confirmation = True
                review_reasons.append(
                    "Special-carrier follow-up appears to request only data already "
                    "covered by the first standard reply."
                )

        if review_reasons:
            notes = " ".join(review_reasons)
            requested_for_output = (
                [HUMAN_INTERVENTION_REQUIRED] if force_human else []
            )
            output = self._as_output(
                matched=False,
                excluded=False,
                request_types=(
                    ["human_guardrail"] if force_human else []
                ),
                requested_data=requested_for_output,
                cleaned_request_text=cleaned_text,
                matched_spans=matched_spans,
                confidence=REVIEW_CONFIDENCE if not force_human else 0.0,
                notes=notes,
                needs_llm_confirmation=not force_human,
                force_human_intervention=force_human,
                human_intervention_required=force_human,
                standard_reply_requested_data=standard_reply_requested_data,
                regex_request_types=real_request_types,
                regex_requested_data=regex_requested_data,
                quoted_history_removed=quoted_history_removed,
                signature_removed=signature_removed,
            )
            output["needs_standard_reply_confirmation"] = needs_standard_reply_confirmation
            return output

        high_confidence_notes = "High-confidence regex match after safety cleanup."
        if audit_notes:
            high_confidence_notes += " " + " ".join(audit_notes)

        output = self._as_output(
            matched=True,
            excluded=False,
            request_types=effective_request_types,
            requested_data=requested_data,
            cleaned_request_text=cleaned_text,
            matched_spans=matched_spans,
            confidence=HIGH_CONFIDENCE,
            notes=high_confidence_notes,
            needs_llm_confirmation=False,
            force_human_intervention=False,
            human_intervention_required=False,
            standard_reply_requested_data=standard_reply_requested_data,
            regex_request_types=real_request_types,
            regex_requested_data=regex_requested_data,
            quoted_history_removed=quoted_history_removed,
            signature_removed=signature_removed,
        )
        output["needs_standard_reply_confirmation"] = needs_standard_reply_confirmation
        return output


if __name__ == "__main__":
    engine = RegexEngine()

    matched_results, unmatched_tickets, excluded_count = engine.process_tickets()

    os.makedirs("output", exist_ok=True)

    write_dataframe(pd.DataFrame(matched_results), REGEX_MATCHES_PATH)

    write_dataframe(pd.DataFrame(unmatched_tickets), UNMATCHED_TICKETS_PATH)

    print(f"Regex high-confidence matched: {len(matched_results)}")
    print(f"Regex unmatched/review-needed: {len(unmatched_tickets)}")
    print(f"Regex excluded no-reply/acknowledgement-only: {excluded_count}")
    print("Files written:")
    print(REGEX_MATCHES_PATH)
    print(UNMATCHED_TICKETS_PATH)

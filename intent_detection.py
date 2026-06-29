"""
intent_detection.py

LLM fallback for tickets not matched by high-confidence regex_engine.py.
The LLM extracts requested_data, not a single intent.

The pipeline is conservative:
- regex hard guardrails are converted directly to human intervention;
- LLM output is auto-usable only when confidence is above the configured
  threshold;
- when regex candidates and LLM extraction disagree, the row is routed to
  human review instead of risking a wrong answer.

Creates:
- output/request_intent_results.jsonl.gz
"""

import json
import os
import time
from math import ceil
from typing import Dict, List

import pandas as pd
from google import genai

from pipeline_io import (
    REGEX_MATCHES_PATH,
    REQUEST_INTENT_RESULTS_PATH,
    UNMATCHED_TICKETS_PATH,
    build_request_id,
    read_dataframe,
    write_dataframe,
)
from customs_rules import (
    HUMAN_INTERVENTION_REQUIRED,
    UNKNOWN_REQUEST,
    clean_latest_request_text,
    collapse_document_embedded_requested_data,
    expand_first_returns_customs_clearance_bundle,
    detect_language_with_dictionary,
    is_no_action_carrier_notification,
    is_missing_extracted_tracking_number,
    is_request_number_3_or_higher,
    normalize_requested_data,
    requested_data_already_answered_by_first_reply,
)

PROMPT_PATH = "prompts/requested_data_extractor.md"
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
FALLBACK_MODEL = os.getenv(
    "GEMINI_RATE_LIMIT_FALLBACK_MODEL",
    os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite"),
)
FALLBACK_MODEL_RATE_LIMIT_ATTEMPTS = int(os.getenv("GEMINI_FALLBACK_RATE_LIMIT_ATTEMPTS", "3"))
BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "20"))
LLM_CONFIDENCE_MIN = float(os.getenv("LLM_CONFIDENCE_MIN", "0.85"))

ALLOWED_REQUESTED_DATA = [
    "commercial_invoice",
    "return_proforma_invoice",
    "corrected_invoice",
    "export_tracking_number",
    "ups_account_number",
    "value_confirmation",
    "returned_items_confirmation",
    "dichiarazione_di_libera_esportazione",
    "eori_number",
    "power_of_attorney",
    "address_translation",
    "exporter_ein",
    "customer_phone",
    "customer_email",
    "customer_name",
    "shipping_address",
    "authorization_letter",
    "shipment_instructions",
    "address_correction",
    "previously_requested_documentation",
    "human_intervention_required",
    "unknown_request",
]

LEGACY_REQUESTED_DATA_ALIASES = {
    "declaration_of_intent": "dichiarazione_di_libera_esportazione",
}

# Kept as internal aliases only. They are collapsed later by customs_rules.
# tax/country/product collapse to commercial_invoice or return_proforma_invoice
# depending on context. customs_description/importer_details always collapse to
# return_proforma_invoice.
DEPRECATED_DOCUMENT_FIELD_KEYS = {
    "tax_information",
    "country_of_origin",
    "product_description",
    "customs_description",
    "importer_details",
}


def is_rate_limit_error(exc):
    text = str(exc or "").lower()
    return (
        "429" in text
        or "resource_exhausted" in text
        or "quota exceeded" in text
        or "quota_exceeded" in text
        or "rate limit" in text
        or "rate_limit" in text
    )


def normalized_model_name(model):
    return str(model or "").strip().lower().rsplit("/", 1)[-1]


def is_gemini_25_flash_model(model):
    return normalized_model_name(model) == "gemini-2.5-flash"


def summarize_model_attempts(attempted_models):
    summary = []
    for model in attempted_models:
        model = str(model or "").strip()
        if model and model not in summary:
            summary.append(model)
    return " -> ".join(summary)


class GeminiJsonHelper:

    @staticmethod
    def clean_json_response(text: str):
        text = (text or "").strip()

        if text.startswith("```json"):
            text = text.replace("```json", "", 1).strip()

        if text.startswith("```"):
            text = text.replace("```", "", 1).strip()

        if text.endswith("```"):
            text = text[:-3].strip()

        return text

    @staticmethod
    def generate_json_list(client, model, prompt):
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )

        response_text = GeminiJsonHelper.clean_json_response(response.text)
        results = json.loads(response_text)

        if not isinstance(results, list):
            raise ValueError("LLM response was not a JSON list")

        return results


class RequestedDataDetector:

    def __init__(self, model=DEFAULT_MODEL, fallback_model=FALLBACK_MODEL):
        api_key = os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY")

        self.client = genai.Client(api_key=api_key)
        self.model = str(model or DEFAULT_MODEL).strip()
        self.fallback_model = str(fallback_model or "").strip()
        self.last_model_used = ""
        self.last_model_attempts = ""
        self.system_prompt = self._load_prompt(PROMPT_PATH)

    @staticmethod
    def _load_prompt(path):
        with open(path, "r", encoding="utf-8") as file:
            return file.read()

    @staticmethod
    def _normalize_requested_data(values):
        if not isinstance(values, list):
            return [UNKNOWN_REQUEST]

        cleaned = []

        for value in values:
            value = str(value).strip()
            value = LEGACY_REQUESTED_DATA_ALIASES.get(value, value)

            if (
                value in ALLOWED_REQUESTED_DATA
                or value in DEPRECATED_DOCUMENT_FIELD_KEYS
            ) and value not in cleaned:
                cleaned.append(value)

        return cleaned or [UNKNOWN_REQUEST]

    def _normalize_results(self, results, model_used, attempted_models):
        self.last_model_used = str(model_used or "").strip()
        self.last_model_attempts = summarize_model_attempts(attempted_models)

        for result in results:
            result["requested_data"] = self._normalize_requested_data(
                result.get("requested_data", [])
            )

            if "confidence" not in result:
                result["confidence"] = 0.0

            if "notes" not in result:
                result["notes"] = ""

            result["human_intervention_draft_response"] = str(
                result.get("human_intervention_draft_response", "") or ""
            ).strip()
            result["llm_model_used"] = self.last_model_used
            result["llm_model_attempts"] = self.last_model_attempts

        return results

    def _generate_with_retries(
        self,
        *,
        model,
        prompt,
        max_attempts,
        attempted_models,
        wait_on_rate_limit,
    ):
        last_error = None

        for attempt in range(max_attempts):
            attempted_models.append(model)
            try:
                return GeminiJsonHelper.generate_json_list(
                    self.client,
                    model,
                    prompt,
                )
            except Exception as exc:
                last_error = exc

                if not is_rate_limit_error(exc):
                    raise

                if wait_on_rate_limit and attempt < max_attempts - 1:
                    wait_seconds = 30 * (attempt + 1)
                    print(
                        f"Rate limit reached for {model}. "
                        f"Waiting {wait_seconds}s before retry."
                    )
                    time.sleep(wait_seconds)

        raise last_error

    def _should_fallback_after_rate_limit(self, exc, model):
        return (
            is_rate_limit_error(exc)
            and is_gemini_25_flash_model(model)
            and bool(self.fallback_model)
            and normalized_model_name(self.fallback_model) != normalized_model_name(model)
        )

    def detect_batch(self, batch_payload):
        prompt = f"""
{self.system_prompt}

Allowed requested_data values:
{json.dumps(ALLOWED_REQUESTED_DATA, ensure_ascii=False, indent=2)}

Classify ALL requests below.

Return JSON ONLY.

Return EXACTLY this structure:

[
  {{
    "source_id": "...",
    "requested_data": ["..."],
    "confidence": 0.0,
    "notes": "short reason",
    "human_intervention_draft_response": ""
  }}
]

REQUESTS:
{json.dumps(batch_payload, ensure_ascii=False, indent=2)}
"""

        attempted_models = []
        self.last_model_used = ""
        self.last_model_attempts = ""
        primary_has_fallback = (
            is_gemini_25_flash_model(self.model)
            and self.fallback_model
            and normalized_model_name(self.fallback_model) != normalized_model_name(self.model)
        )
        primary_attempts = 1 if primary_has_fallback else 3

        try:
            results = self._generate_with_retries(
                model=self.model,
                prompt=prompt,
                max_attempts=primary_attempts,
                attempted_models=attempted_models,
                wait_on_rate_limit=primary_attempts > 1,
            )
            return self._normalize_results(results, self.model, attempted_models)
        except Exception as exc:
            self.last_model_attempts = summarize_model_attempts(attempted_models)

            if not self._should_fallback_after_rate_limit(exc, self.model):
                raise

            print(
                f"Rate limit reached for {self.model}; "
                f"switching to fallback model {self.fallback_model}."
            )

            try:
                results = self._generate_with_retries(
                    model=self.fallback_model,
                    prompt=prompt,
                    max_attempts=max(1, FALLBACK_MODEL_RATE_LIMIT_ATTEMPTS),
                    attempted_models=attempted_models,
                    wait_on_rate_limit=True,
                )
                return self._normalize_results(
                    results,
                    self.fallback_model,
                    attempted_models,
                )
            except Exception:
                self.last_model_attempts = summarize_model_attempts(attempted_models)
                raise


def build_source_id(row):
    return row.get("request_id") or build_request_id(
        row.get("zendesk_ticket_id"),
        row.get("request_number", 1),
    )


def as_bool(value):
    if isinstance(value, bool):
        return value

    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass

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


def list_or_empty(value):
    return normalize_requested_data(value)


def requested_data_set(value):
    return {
        item
        for item in normalize_requested_data(value)
        if item not in {UNKNOWN_REQUEST, HUMAN_INTERVENTION_REQUIRED}
    }


def source_text_for_llm(row) -> str:
    cleaned = str(row.get("cleaned_request_body", "") or "").strip()
    raw = str(row.get("request_body", "") or "").strip()

    if raw:
        refreshed_cleaned = str(
            clean_latest_request_text(raw).get("cleaned_request_text", "") or ""
        ).strip()
        # Rows produced before the latest cleaner change may already have a
        # shortened cleaned_request_body.  Rebuild from request_body and prefer
        # the refreshed text only when it clearly restores useful context.
        if refreshed_cleaned and (not cleaned or len(refreshed_cleaned) > len(cleaned) + 80):
            return refreshed_cleaned

    return cleaned or raw


def base_output_row(source_row) -> Dict[str, object]:
    return {
        "request_id": source_row.get("request_id") or build_request_id(
            source_row.get("zendesk_ticket_id"),
            source_row.get("request_number"),
        ),
        "request_submission_timestamp": source_row.get("request_submission_timestamp"),
        "ticket_submission_timestamp": source_row.get("ticket_submission_timestamp"),
        "zendesk_ticket_id": source_row.get("zendesk_ticket_id"),
        "request_number": source_row.get("request_number"),
        "requester_email": source_row.get("requester_email"),
        "subject": source_row.get("subject"),
        "request_body": source_row.get("request_body"),
        "cleaned_request_body": source_row.get("cleaned_request_body"),
        "ticket_category": source_row.get("ticket_category"),
        "extracted_tracking_number": source_row.get("extracted_tracking_number"),
        "shipment_order_number": source_row.get("shipment_order_number"),
        "shipment_tracking_number": source_row.get("shipment_tracking_number"),
        "return_tracking_number": source_row.get("return_tracking_number"),
        "shipment_carrier_code": source_row.get("shipment_carrier_code"),
        "return_carrier_code": source_row.get("return_carrier_code"),
        "tracking_not_found_in_shipping_platform_shipments": as_bool(
            source_row.get("tracking_not_found_in_shipping_platform_shipments")
        ),
        "llm_human_intervention_draft_response": source_row.get(
            "llm_human_intervention_draft_response",
            "",
        ),
        "regex_confidence": source_row.get(
            "regex_confidence",
            source_row.get("confidence"),
        ),
        "needs_standard_reply_confirmation": as_bool(
            source_row.get("needs_standard_reply_confirmation")
        ),
        "standard_reply_requested_data": list_or_empty(
            source_row.get("standard_reply_requested_data")
        ),
        "regex_request_types": list_or_empty(source_row.get("regex_request_types")),
        "regex_requested_data": list_or_empty(source_row.get("regex_requested_data")),
        "matched_spans": source_row.get("matched_spans", ""),
        "quoted_history_removed": as_bool(source_row.get("quoted_history_removed")),
        "signature_removed": as_bool(source_row.get("signature_removed")),
    }


def build_human_output_row(source_row, reason, engine="human_guardrail"):
    output = base_output_row(source_row)
    output.update(
        {
            "engine": engine,
            "matched": True,
            "excluded": False,
            "request_types": ["human_intervention_required"],
            "requested_data": [HUMAN_INTERVENTION_REQUIRED],
            "confidence": 0.0,
            "llm_confidence": None,
            "notes": reason,
            "human_intervention_required": True,
            "llm_was_used": False,
            "llm_model_used": "",
            "llm_model_attempts": "",
        }
    )
    return output


def build_exclude_output_row(source_row, reason, engine="no_action_guardrail"):
    output = base_output_row(source_row)
    output.update(
        {
            "engine": engine,
            "matched": True,
            "excluded": True,
            "request_types": ["exclude_from_processing"],
            "requested_data": [],
            "confidence": 0.99,
            "llm_confidence": None,
            "notes": reason,
            "human_intervention_required": False,
            "llm_was_used": False,
            "llm_model_used": "",
            "llm_model_attempts": "",
        }
    )
    return output


def is_tracking_lookup_not_found(row) -> bool:
    return as_bool(row.get("tracking_not_found_in_shipping_platform_shipments"))


def requested_data_summary(values: List[str]) -> str:
    understood = [
        value.replace("_", " ")
        for value in values
        if value not in {UNKNOWN_REQUEST, HUMAN_INTERVENTION_REQUIRED}
    ]
    return ", ".join(understood) if understood else "the sender's request"


def build_tracking_lookup_fallback_draft(source_row, requested_data, llm_notes: object) -> str:
    language = detect_language_with_dictionary(
        source_row.get("subject", ""),
        source_text_for_llm(source_row),
    )
    tracking_number = str(source_row.get("extracted_tracking_number") or "N/A").strip()
    understood = requested_data_summary(requested_data)
    notes = str(llm_notes or "").strip()

    if language == "it":
        detail = f" Ho interpretato la richiesta come relativa a: {understood}."
        if notes:
            detail += f" Dettagli LLM: {notes}"
        return (
            "Buongiorno,\n\n"
            f"Abbiamo ricevuto la vostra richiesta relativa al tracking {tracking_number}."
            f"{detail}\n\n"
            "Non sono riuscito a trovare questo tracking nella tabella spedizioni, "
            "quindi è necessario l'intervento di un operatore per verificare la pratica "
            "e confermare la documentazione o le informazioni corrette.\n\n"
            "Cordiali saluti,"
        )

    detail = f" I understood the request as related to: {understood}."
    if notes:
        detail += f" LLM details: {notes}"
    return (
        "Hello,\n\n"
        f"We received your request regarding tracking number {tracking_number}."
        f"{detail}\n\n"
        "I could not find this tracking number in the shipment table, so human "
        "intervention is required to verify the case and confirm the correct "
        "documentation or information.\n\n"
        "Kind regards,"
    )


def build_llm_output_row(source_row, result):
    requested_data = result.get("requested_data", [UNKNOWN_REQUEST])
    requested_data = normalize_requested_data(requested_data) or [UNKNOWN_REQUEST]
    requested_data = collapse_document_embedded_requested_data(
        requested_data,
        ticket_category=source_row.get("ticket_category"),
        request_number=source_row.get("request_number", 1),
        requester_email=source_row.get("requester_email"),
        request_text=source_text_for_llm(source_row),
    ) or [UNKNOWN_REQUEST]

    engine = "llm"
    request_types: List[str] = []
    human_intervention_required = False
    confidence = safe_float(result.get("confidence", 0.0))
    notes = result.get("notes", "")

    collapsed_regex_requested_data = collapse_document_embedded_requested_data(
        source_row.get("regex_requested_data"),
        ticket_category=source_row.get("ticket_category"),
        request_number=source_row.get("request_number", 1),
        requester_email=source_row.get("requester_email"),
        request_text=source_text_for_llm(source_row),
    )
    collapsed_regex_requested_data = expand_first_returns_customs_clearance_bundle(
        collapsed_regex_requested_data,
        ticket_category=source_row.get("ticket_category"),
        request_number=source_row.get("request_number", 1),
        requester_email=source_row.get("requester_email"),
        trigger_requested_data=collapsed_regex_requested_data,
    )
    requested_data = expand_first_returns_customs_clearance_bundle(
        requested_data,
        ticket_category=source_row.get("ticket_category"),
        request_number=source_row.get("request_number", 1),
        requester_email=source_row.get("requester_email"),
        trigger_requested_data=collapsed_regex_requested_data,
    ) or requested_data
    regex_candidates = requested_data_set(collapsed_regex_requested_data)
    llm_candidates = requested_data_set(requested_data)
    llm_human_intervention_draft_response = str(
        result.get("human_intervention_draft_response", "") or ""
    ).strip()
    llm_model_used = str(result.get("llm_model_used", "") or "").strip()
    llm_model_attempts = str(result.get("llm_model_attempts", "") or "").strip()

    if is_tracking_lookup_not_found(source_row):
        engine = "llm_tracking_lookup_not_found_guard"
        request_types = ["tracking_lookup_not_found_guard"]
        understood_requested_data = [
            item
            for item in requested_data
            if item not in {UNKNOWN_REQUEST, HUMAN_INTERVENTION_REQUIRED}
        ]
        requested_data = understood_requested_data or [HUMAN_INTERVENTION_REQUIRED]
        human_intervention_required = True
        if not llm_human_intervention_draft_response:
            llm_human_intervention_draft_response = build_tracking_lookup_fallback_draft(
                source_row,
                requested_data,
                notes,
            )
        notes = (
            "Tracking number extracted from the ticket was not found in "
            "tlg-business-intelligence-prd.bi.shipping_platform_shipments. "
            "Regex processing was skipped and this request was handled only by the LLM. "
            f"LLM understood requested_data={requested_data}. "
            "Human intervention is required. "
            f"LLM notes: {notes}"
        )

    elif requested_data == [HUMAN_INTERVENTION_REQUIRED]:
        engine = "llm_human_intervention_guard"
        request_types = [HUMAN_INTERVENTION_REQUIRED]
        human_intervention_required = True
        notes = (
            "LLM classified this request as requiring human intervention. "
            f"LLM notes: {notes}"
        )

    elif requested_data == [UNKNOWN_REQUEST]:
        engine = "llm_unknown_request_guard"
        request_types = ["unknown_request_guard"]
        requested_data = [HUMAN_INTERVENTION_REQUIRED]
        human_intervention_required = True
        notes = (
            "LLM could not identify actionable requested_data. "
            "Human intervention is required. "
            f"LLM notes: {notes}"
        )

    elif confidence < LLM_CONFIDENCE_MIN:
        engine = "llm_low_confidence_guard"
        request_types = ["low_confidence_guard"]
        requested_data = [HUMAN_INTERVENTION_REQUIRED]
        human_intervention_required = True
        notes = (
            f"LLM confidence {confidence:.2f} is below threshold "
            f"{LLM_CONFIDENCE_MIN:.2f}. Human intervention is required. "
            f"LLM notes: {notes}"
        )

    elif regex_candidates and llm_candidates and regex_candidates != llm_candidates:
        engine = "regex_llm_disagreement_guard"
        request_types = ["regex_llm_disagreement_guard"]
        requested_data = [HUMAN_INTERVENTION_REQUIRED]
        human_intervention_required = True
        notes = (
            "Regex candidates and LLM requested_data disagree. "
            f"regex={sorted(regex_candidates)}; llm={sorted(llm_candidates)}. "
            "Human intervention is required. "
            f"LLM notes: {notes}"
        )

    elif (
        as_bool(source_row.get("needs_standard_reply_confirmation"))
        and requested_data_already_answered_by_first_reply(
            requested_data,
            source_row.get("requester_email"),
        )
    ):
        engine = "llm_standard_reply_repeat_guard"
        request_types = ["standard_reply_repeat_guard"]
        requested_data = [HUMAN_INTERVENTION_REQUIRED]
        human_intervention_required = True
        notes = (
            "Regex/LLM follow-up guard: the request appears to ask only for "
            "data already covered by the first standard reply. Do not send an "
            "automatic duplicate answer; human intervention is required. "
            f"LLM notes: {notes}"
        )

    output = base_output_row(source_row)
    output.update(
        {
            "engine": engine,
            "matched": True,
            "excluded": False,
            "request_types": request_types,
            "requested_data": requested_data,
            "confidence": confidence,
            "llm_confidence": confidence,
            "notes": notes,
            "human_intervention_required": human_intervention_required,
            "llm_was_used": True,
            "llm_model_used": llm_model_used,
            "llm_model_attempts": llm_model_attempts,
            "llm_human_intervention_draft_response": llm_human_intervention_draft_response,
        }
    )
    return output


def add_language_detection(final_df):
    if final_df.empty:
        final_df["request_language"] = []
        final_df["language_confidence"] = []
        final_df["language_notes"] = []
        return final_df

    language_details = final_df.apply(
        lambda row: detect_language_with_dictionary(
            row.get("subject", ""),
            row.get("cleaned_request_body", "") or row.get("request_body", ""),
            return_details=True,
        ),
        axis=1,
    )

    final_df["request_language"] = language_details.apply(
        lambda details: details["request_language"]
    )
    final_df["language_confidence"] = language_details.apply(
        lambda details: details["language_confidence"]
    )
    final_df["language_notes"] = language_details.apply(
        lambda details: details["language_notes"]
    )

    return final_df


def main():
    detector = RequestedDataDetector()

    regex_df = read_dataframe(REGEX_MATCHES_PATH)
    unmatched_df = read_dataframe(UNMATCHED_TICKETS_PATH)

    llm_results = []
    rows_for_llm = []

    for _, row in unmatched_df.iterrows():
        if is_missing_extracted_tracking_number(row.get("extracted_tracking_number")):
            llm_results.append(
                build_human_output_row(
                    row,
                    "The tracking number was not found in the ticket. The request was not analyzed.",
                    engine="missing_tracking_number_guard",
                )
            )
        elif is_tracking_lookup_not_found(row):
            # User requirement: when the extracted tracking number is missing from
            # shipping_platform_shipments, bypass regex/hard guards and let the
            # LLM summarize what it understood before routing to human review.
            rows_for_llm.append(row)
        elif is_no_action_carrier_notification(source_text_for_llm(row)):
            llm_results.append(
                build_exclude_output_row(
                    row,
                    "Carrier notification/status message only; no actionable customer-data request was detected.",
                )
            )
        elif is_request_number_3_or_higher(row.get("request_number")):
            llm_results.append(
                build_human_output_row(
                    row,
                    "Request number is 3 or higher. Human intervention is required by automation policy.",
                )
            )
        elif as_bool(row.get("force_human_intervention")) or as_bool(
            row.get("human_intervention_required")
        ):
            reason = row.get("notes") or "Regex safety guard required human review."
            llm_results.append(build_human_output_row(row, reason))
        else:
            rows_for_llm.append(row)

    total_batches = ceil(len(rows_for_llm) / BATCH_SIZE) if rows_for_llm else 0

    print(
        f"Processing {len(rows_for_llm)} LLM-review tickets "
        f"in {total_batches} Gemini calls; "
        f"{len(llm_results)} tickets sent directly to human review"
    )

    for batch_number in range(total_batches):
        start_idx = batch_number * BATCH_SIZE
        end_idx = start_idx + BATCH_SIZE
        batch_rows = rows_for_llm[start_idx:end_idx]

        batch_payload = []
        lookup = {}

        for row in batch_rows:
            source_id = build_source_id(row)
            lookup[source_id] = row

            batch_payload.append(
                {
                    "source_id": source_id,
                    "subject": str(row.get("subject", "") or ""),
                    "request_body": source_text_for_llm(row),
                    "ticket_category": str(row.get("ticket_category", "") or ""),
                    "regex_candidates": list_or_empty(row.get("regex_requested_data")),
                    "regex_notes": str(row.get("notes", "") or ""),
                    "extracted_tracking_number": str(
                        row.get("extracted_tracking_number", "") or ""
                    ),
                    "tracking_not_found_in_shipping_platform_shipments": as_bool(
                        row.get("tracking_not_found_in_shipping_platform_shipments")
                    ),
                }
            )

        try:
            batch_results = detector.detect_batch(batch_payload)
        except Exception as exc:
            print(f"Batch {batch_number + 1} failed: {exc}")

            batch_results = [
                {
                    "source_id": payload["source_id"],
                    "requested_data": [UNKNOWN_REQUEST],
                    "confidence": 0.0,
                    "notes": f"LLM batch failed: {exc}",
                    "llm_model_used": detector.last_model_used,
                    "llm_model_attempts": detector.last_model_attempts,
                }
                for payload in batch_payload
            ]

        for result in batch_results:
            source_id = result.get("source_id")
            source_row = lookup.get(source_id)

            if source_row is None:
                print(f"WARNING: source_id {source_id} not found")
                continue

            llm_results.append(build_llm_output_row(source_row, result))

    llm_df = pd.DataFrame(llm_results)

    final_df = pd.concat(
        [regex_df, llm_df],
        ignore_index=True,
    )

    final_df = add_language_detection(final_df)

    os.makedirs("output", exist_ok=True)
    write_dataframe(final_df, REQUEST_INTENT_RESULTS_PATH)

    print(
        f"Saved {len(final_df)} rows to {REQUEST_INTENT_RESULTS_PATH}"
    )


if __name__ == "__main__":
    main()

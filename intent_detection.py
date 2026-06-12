"""
intent_detection.py

LLM fallback for tickets not matched by regex_engine.py.
The LLM extracts requested_data, not a single intent.

Language detection is deterministic and local: it uses the weighted historical
Zendesk-ticket dictionary in customs_rules.py for all rows, including rows
classified by regex.

Creates:
- output/request_intent_results.xlsx
"""

import json
import os
import time
from math import ceil

import pandas as pd
from google import genai

from customs_rules import (
    HUMAN_INTERVENTION_REQUIRED,
    UNKNOWN_REQUEST,
    detect_language_with_dictionary,
    normalize_requested_data,
    requested_data_already_answered_by_first_reply,
)

PROMPT_PATH = "prompts/requested_data_extractor.md"
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "20"))

ALLOWED_REQUESTED_DATA = [
    "commercial_invoice",
    "return_proforma_invoice",
    "corrected_invoice",
    "export_tracking_number",
    "ups_account_number",
    "value_confirmation",
    "returned_items_confirmation",
    "customs_description",
    "declaration_of_intent",
    "eori_number",
    "power_of_attorney",
    "tax_information",
    "country_of_origin",
    "importer_details",
    "address_translation",
    "exporter_ein",
    "customer_phone",
    "customer_email",
    "customer_name",
    "shipping_address",
    "authorization_letter",
    "shipment_instructions",
    "address_correction",
    "product_description",
    "previously_requested_documentation",
    "unknown_request",
]


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

    def __init__(self, model=DEFAULT_MODEL):
        api_key = os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY")

        self.client = genai.Client(api_key=api_key)
        self.model = model
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
            if value in ALLOWED_REQUESTED_DATA and value not in cleaned:
                cleaned.append(value)

        return cleaned or [UNKNOWN_REQUEST]

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
    "notes": "short reason"
  }}
]

REQUESTS:
{json.dumps(batch_payload, ensure_ascii=False, indent=2)}
"""

        last_error = None

        for attempt in range(3):
            try:
                results = GeminiJsonHelper.generate_json_list(
                    self.client,
                    self.model,
                    prompt,
                )

                for result in results:
                    result["requested_data"] = self._normalize_requested_data(
                        result.get("requested_data", [])
                    )

                    if "confidence" not in result:
                        result["confidence"] = 0.0

                    if "notes" not in result:
                        result["notes"] = ""

                return results

            except Exception as exc:
                last_error = exc

                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    wait_seconds = 30 * (attempt + 1)
                    print(f"Rate limit reached. Waiting {wait_seconds}s")
                    time.sleep(wait_seconds)
                else:
                    raise

        raise last_error


def build_source_id(row):
    return f"{row['zendesk_ticket_id']}_{row.get('request_number', 1)}"


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


def list_or_empty(value):
    return normalize_requested_data(value)


def build_llm_output_row(source_row, result):
    requested_data = result.get("requested_data", [UNKNOWN_REQUEST])
    requested_data = normalize_requested_data(requested_data) or [UNKNOWN_REQUEST]

    engine = "llm"
    request_types = []
    human_intervention_required = False
    notes = result.get("notes", "")

    if (
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

    return {
        "zendesk_ticket_id": source_row.get("zendesk_ticket_id"),
        "request_number": source_row.get("request_number"),
        "requester_email": source_row.get("requester_email"),
        "subject": source_row.get("subject"),
        "request_body": source_row.get("request_body"),
        "ticket_category": source_row.get("ticket_category"),
        "extracted_tracking_number": source_row.get("extracted_tracking_number"),
        "shipment_order_number": source_row.get("shipment_order_number"),
        "shipment_tracking_number": source_row.get("shipment_tracking_number"),
        "return_tracking_number": source_row.get("return_tracking_number"),
        "engine": engine,
        "matched": True,
        "excluded": False,
        "request_types": request_types,
        "requested_data": requested_data,
        "confidence": result.get("confidence", 0.0),
        "notes": notes,
        "needs_standard_reply_confirmation": as_bool(
            source_row.get("needs_standard_reply_confirmation")
        ),
        "standard_reply_requested_data": list_or_empty(
            source_row.get("standard_reply_requested_data")
        ),
        "regex_request_types": list_or_empty(source_row.get("regex_request_types")),
        "regex_requested_data": list_or_empty(source_row.get("regex_requested_data")),
        "human_intervention_required": human_intervention_required,
    }


def add_language_detection(final_df):
    if final_df.empty:
        final_df["request_language"] = []
        final_df["language_confidence"] = []
        final_df["language_notes"] = []
        return final_df

    language_details = final_df.apply(
        lambda row: detect_language_with_dictionary(
            row.get("subject", ""),
            row.get("request_body", ""),
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

    regex_df = pd.read_excel("output/regex_matches.xlsx")
    unmatched_df = pd.read_excel("output/unmatched_tickets.xlsx")

    llm_results = []
    total_batches = ceil(len(unmatched_df) / BATCH_SIZE) if len(unmatched_df) else 0

    print(
        f"Processing {len(unmatched_df)} unmatched tickets "
        f"in {total_batches} Gemini calls"
    )

    for batch_number in range(total_batches):
        start_idx = batch_number * BATCH_SIZE
        end_idx = start_idx + BATCH_SIZE
        batch_df = unmatched_df.iloc[start_idx:end_idx].copy()

        batch_payload = []
        lookup = {}

        for _, row in batch_df.iterrows():
            source_id = build_source_id(row)
            lookup[source_id] = row

            batch_payload.append(
                {
                    "source_id": source_id,
                    "subject": str(row.get("subject", "") or ""),
                    "request_body": str(row.get("request_body", "") or ""),
                    "ticket_category": str(row.get("ticket_category", "") or ""),
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
    final_df.to_excel("output/request_intent_results.xlsx", index=False)

    print(
        f"Saved {len(final_df)} rows to output/request_intent_results.xlsx"
    )


if __name__ == "__main__":
    main()

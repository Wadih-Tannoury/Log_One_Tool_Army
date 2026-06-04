````python
"""
intent_detection.py

LLM fallback used only when regexes do not match.

Default model:
gemini-2.5-flash
"""

import json
import os
import time
from google import genai

SYSTEM_PROMPT = """
You are an intent classifier for logistics, customs clearance,
carrier requests and returns.

Your job is NOT to answer the request.

Return JSON only:

{
  "intent_name": "...",
  "expected_data": [
      "..."
  ],
  "confidence": 0.0
}

The expected_data field must describe the information that should be
retrieved from internal systems and included in a future response.

Examples:
- customs value confirmation
- commercial invoice
- tracking number
- shipment status
- return instructions
- customs description
- consignee details
- proof of export
"""

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


class IntentDetector:

    def __init__(self, model=DEFAULT_MODEL):

        api_key = os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY environment variable is missing."
            )

        self.client = genai.Client(api_key=api_key)
        self.model = model

    def detect(self, request_text: str):

        prompt = f"{SYSTEM_PROMPT}\n\nREQUEST:\n{request_text}"

        last_error = None

        for attempt in range(3):

            try:

                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt
                )

                text = response.text.strip()

                if text.startswith("```json"):
                    text = text.replace("```json", "").replace("```", "").strip()

                elif text.startswith("```"):
                    text = text.replace("```", "").strip()

                return json.loads(text)

            except Exception as e:

                last_error = e

                error_text = str(e)

                if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:

                    wait_seconds = 60 * (attempt + 1)

                    print(
                        f"Rate limit reached. "
                        f"Retrying in {wait_seconds}s..."
                    )

                    time.sleep(wait_seconds)

                else:
                    raise

        raise last_error


def process_tickets(detector, tickets):

    results = []

    for ticket in tickets:

        request_text = ticket.get("request_body", "") or ""

        llm_result = detector.detect(request_text)

        results.append({
            "zendesk_ticket_id": ticket.get("zendesk_ticket_id"),
            "requester_email": ticket.get("requester_email"),
            "subject": ticket.get("subject"),
            "request_body": request_text,
            "engine": "llm",
            **llm_result
        })

    return results


if __name__ == "__main__":

    from utils.output_writer import save_results_to_excel

    detector = IntentDetector()

    sample_requests = [
        "The consignee refused the COD shipment."
    ]

    results = []

    for request in sample_requests:

        result = detector.detect(request)

        results.append({
            "request_text": request,
            **result
        })

    save_results_to_excel(results)
````

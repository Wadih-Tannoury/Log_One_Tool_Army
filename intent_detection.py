
"""
intent_detection.py

LLM fallback used only when regexes do not match.
Recommended model:
gemini-2.5-pro (best reasoning for long customs/carrier emails).
"""

import json
import os
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

def process_tickets(self, tickets):

    results = []

    for ticket in tickets:

        request_text = ticket["request_body"] or ""

        llm_result = self.detect(request_text)

        results.append({
            "zendesk_ticket_id": ticket["zendesk_ticket_id"],
            "requester_email": ticket["requester_email"],
            "subject": ticket["subject"],
            "request_body": request_text,
            "engine": "llm",
            **llm_result
        })

    return results

class IntentDetector:

    def __init__(self, model="gemini-2.5-pro"):
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = model

    def detect(self, request_text: str):

        response = self.client.models.generate_content(
            model=self.model,
            contents=f"{SYSTEM_PROMPT}\n\nREQUEST:\n{request_text}"
        )

        return json.loads(response.text)

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


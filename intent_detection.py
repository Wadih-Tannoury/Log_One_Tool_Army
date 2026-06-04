import json
import os
import time
import pandas as pd

from google import genai
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT_ID = "tlg-business-intelligence-prd"

INPUT_TABLE = (
    "tlg-business-intelligence-prd.til.log_one_tool_army_regex_unmatched"
)

OUTPUT_TABLE = (
    "tlg-business-intelligence-prd.til.log_one_tool_army_final_results"
)

MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

SYSTEM_PROMPT = """
You are an intent classifier for logistics,
customs clearance, carrier requests and returns.

Return JSON only:

{
  "intent_name": "...",
  "expected_data": ["..."],
  "confidence": 0.0
}
"""


class IntentDetector:

    def __init__(self):

        creds = json.loads(
            os.environ["BI_BIGQUERY_CREDS"]
        )

        credentials = (
            service_account.Credentials
            .from_service_account_info(creds)
        )

        self.bq = bigquery.Client(
            project=PROJECT_ID,
            credentials=credentials
        )

        self.client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"]
        )

    def detect(self, request_text):

        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"REQUEST:\n{request_text}"
        )

        for attempt in range(3):

            try:

                response = (
                    self.client.models.generate_content(
                        model=MODEL,
                        contents=prompt
                    )
                )

                text = response.text.strip()

                text = (
                    text.replace("```json", "")
                    .replace("```", "")
                    .strip()
                )

                return json.loads(text)

            except Exception as e:

                if (
                    "429" in str(e)
                    or
                    "RESOURCE_EXHAUSTED" in str(e)
                ):
                    time.sleep(60)

                else:
                    raise

        raise Exception(
            "Failed after retries"
        )


if name == "main":


import pandas as pd
import os

detector = IntentDetector()

unmatched_df = pd.read_excel(
    "output/unmatched_tickets.xlsx"
)

results = []

for _, row in unmatched_df.iterrows():

    request_text = row.get(
        "request_body",
        ""
    )

    try:

        llm_result = detector.detect(
            request_text
        )

    except Exception as e:

        llm_result = {
            "intent_name": "ERROR",
            "expected_data": [],
            "confidence": 0,
            "error": str(e)
        }

    results.append({
        "zendesk_ticket_id":
            row.get("zendesk_ticket_id"),

        "requester_email":
            row.get("requester_email"),

        "subject":
            row.get("subject"),

        "request_body":
            request_text,

        **llm_result
    })

os.makedirs(
    "output",
    exist_ok=True
)

pd.DataFrame(results).to_excel(
    "output/request_intent_results.xlsx",
    index=False
)

print(
    f"Saved {len(results)} rows to "
    f"output/request_intent_results.xlsx"
)


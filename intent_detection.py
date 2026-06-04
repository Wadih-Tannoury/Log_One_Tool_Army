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


if __name__ == "__main__":

    detector = IntentDetector()

    query = f"""
    SELECT *
    FROM `{INPUT_TABLE}`
    """

    tickets = list(
        detector.bq.query(query).result()
    )

    print(
        f"Processing {len(tickets)} "
        f"unmatched tickets"
    )

    results = []

    for ticket in tickets:

        try:

            llm_result = detector.detect(
                ticket["request_body"]
            )

            results.append({
                "zendesk_ticket_id":
                    ticket["zendesk_ticket_id"],

                "requester_email":
                    ticket["requester_email"],

                "subject":
                    ticket["subject"],

                "request_body":
                    ticket["request_body"],

                "engine":
                    "llm",

                **llm_result
            })

        except Exception as e:

            print(
                f"Error on ticket "
                f"{ticket['zendesk_ticket_id']}: "
                f"{str(e)}"
            )

    if results:

        df = pd.DataFrame(results)

        detector.bq.load_table_from_dataframe(
            df,
            OUTPUT_TABLE,
            job_config=bigquery.LoadJobConfig(
                write_disposition="WRITE_TRUNCATE"
            )
        ).result()

        print(
            f"Loaded {len(df)} rows "
            f"to final_results"
        )

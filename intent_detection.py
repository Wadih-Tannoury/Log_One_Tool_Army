"""
intent_detection.py

Processes only tickets that were not classified
by regex_engine.py.

Creates:
output/request_intent_results.xlsx
"""

import json
import os
import time

import pandas as pd
from google import genai

SYSTEM_PROMPT = """
You are an intent classifier for logistics,
customs clearance, carrier requests and returns.

Your job is NOT to answer the request.

Return JSON only:

{
  "intent_name": "...",
  "expected_data": [
      "..."
  ],
  "confidence": 0.0
}

The expected_data field must describe
the information that should be retrieved
from internal systems and included
in a future response.
"""

DEFAULT_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)


class IntentDetector:

    def __init__(
        self,
        model=DEFAULT_MODEL
    ):

        api_key = os.getenv(
            "GEMINI_API_KEY"
        )

        if not api_key:

            raise ValueError(
                "Missing GEMINI_API_KEY"
            )

        self.client = genai.Client(
            api_key=api_key
        )

        self.model = model

    def detect(
        self,
        request_text: str
    ):

        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"REQUEST:\n{request_text}"
        )

        last_error = None

        for attempt in range(3):

            try:

                response = (
                    self.client
                    .models
                    .generate_content(
                        model=self.model,
                        contents=prompt
                    )
                )

                text = (
                    response.text
                    .strip()
                )

                if text.startswith(
                    "```json"
                ):

                    text = (
                        text
                        .replace(
                            "```json",
                            ""
                        )
                        .replace(
                            "```",
                            ""
                        )
                        .strip()
                    )

                elif text.startswith(
                    "```"
                ):

                    text = (
                        text
                        .replace(
                            "```",
                            ""
                        )
                        .strip()
                    )

                return json.loads(
                    text
                )

            except Exception as e:

                last_error = e

                if (
                    "429" in str(e)
                    or
                    "RESOURCE_EXHAUSTED"
                    in str(e)
                ):

                    wait_seconds = (
                        30
                        * (attempt + 1)
                    )

                    print(
                        f"Rate limit hit. "
                        f"Retrying in "
                        f"{wait_seconds}s"
                    )

                    time.sleep(
                        wait_seconds
                    )

                else:
                    raise

        raise last_error


if __name__ == "__main__":

    from math import ceil
    import pandas as pd

    BATCH_SIZE = 20

    detector = IntentDetector()

    regex_df = pd.read_excel(
        "output/regex_matches.xlsx"
    )

    unmatched_df = pd.read_excel(
        "output/unmatched_tickets.xlsx"
    )

    llm_results = []

    total_batches = ceil(
        len(unmatched_df) / BATCH_SIZE
    )

    print(
        f"Processing {len(unmatched_df)} unmatched tickets "
        f"in {total_batches} Gemini calls"
    )

    for batch_number in range(total_batches):

        start_idx = batch_number * BATCH_SIZE
        end_idx = start_idx + BATCH_SIZE

        batch_df = unmatched_df.iloc[
            start_idx:end_idx
        ]

        batch_payload = []

        for _, row in batch_df.iterrows():

            batch_payload.append(
                {
                    "zendesk_ticket_id": str(
                        row["zendesk_ticket_id"]
                    ),
                    "request_body": row.get(
                        "request_body",
                        ""
                    )
                }
            )

        prompt = f"""
{SYSTEM_PROMPT}

Classify all requests below.

Return JSON ONLY.

Format:

[
  {{
    "zendesk_ticket_id": "...",
    "intent_name": "...",
    "expected_data": [],
    "confidence": 0.0
  }}
]

REQUESTS:

{json.dumps(batch_payload, ensure_ascii=False)}
"""

        response = detector.client.models.generate_content(
            model=detector.model,
            contents=prompt
        )

        response_text = (
            response.text
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

        batch_results = json.loads(
            response_text
        )

        lookup = {
            str(row["zendesk_ticket_id"]): row
            for _, row in batch_df.iterrows()
        }

        for result in batch_results:

            source_row = lookup.get(
                str(
                    result[
                        "zendesk_ticket_id"
                    ]
                )
            )

            if source_row is None:
                continue

            llm_results.append(
                {
                    "zendesk_ticket_id":
                        source_row[
                            "zendesk_ticket_id"
                        ],

                    "requester_email":
                        source_row.get(
                            "requester_email"
                        ),

                    "subject":
                        source_row.get(
                            "subject"
                        ),

                    "request_body":
                        source_row.get(
                            "request_body"
                        ),

                    "engine":
                        "llm",

                    **result
                }
            )

    llm_df = pd.DataFrame(
        llm_results
    )

    final_df = pd.concat(
        [
            regex_df,
            llm_df
        ],
        ignore_index=True
    )

    os.makedirs(
        "output",
        exist_ok=True
    )

    final_df.to_excel(
        "output/request_intent_results.xlsx",
        index=False
    )

    print(
        f"Saved {len(final_df)} rows to "
        f"output/request_intent_results.xlsx"
    )

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

    detector = IntentDetector()

    regex_matches_path = (
        "output/regex_matches.xlsx"
    )

    unmatched_path = (
        "output/unmatched_tickets.xlsx"
    )

    regex_df = pd.read_excel(
        regex_matches_path
    )

    unmatched_df = pd.read_excel(
        unmatched_path
    )

    llm_results = []

    print(
        f"Processing "
        f"{len(unmatched_df)} "
        f"unmatched tickets..."
    )

    for _, row in (
        unmatched_df.iterrows()
    ):

        request_text = (
            row.get(
                "request_body",
                ""
            )
            or ""
        )

        try:

            llm_result = (
                detector.detect(
                    request_text
                )
            )

        except Exception as e:

            llm_result = {
                "intent_name":
                    "ERROR",

                "expected_data":
                    [],

                "confidence":
                    0,

                "error":
                    str(e)
            }

        llm_results.append(
            {
                "zendesk_ticket_id":
                    row.get(
                        "zendesk_ticket_id"
                    ),

                "requester_email":
                    row.get(
                        "requester_email"
                    ),

                "subject":
                    row.get(
                        "subject"
                    ),

                "request_body":
                    request_text,

                "engine":
                    "llm",

                **llm_result
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
        f"Final file saved with "
        f"{len(final_df)} rows:"
    )

    print(
        "output/request_intent_results.xlsx"
    )

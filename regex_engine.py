"""
regex_engine.py

Rule-based intent detection using regexes stored in:
`tlg-business-intelligence-prd.til.log_one_tool_army_request_regex_config`
"""

import json
import re
import os
from collections import defaultdict
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT_ID = "tlg-business-intelligence-prd"
REGEX_CONFIG_TABLE = (
    "tlg-business-intelligence-prd.til.log_one_tool_army_request_regex_config"
)

REQUEST_TYPE_TO_EXPECTED_DATA = {
    "invoice": ["commercial invoice", "invoice number", "invoice attachment"],
    "return_proforma_invoice": ["return proforma invoice"],
    "tracking_number": ["tracking number"],
    "ups_account": ["UPS account number"],
    "value_confirmation": ["declared value", "value confirmation"],
    "returned_items": ["list of returned items"],
    "customs_description": ["customs description", "HS code if available"],
    "declaration_of_intent": ["declaration of intent"],
    "eori": ["EORI number"],
    "poa": ["power of attorney document"],
    "reminder_ticket": ["previously requested documentation"],
}


class RegexEngine:

    def __init__(self):
        creds = json.loads(os.environ["BI_BIGQUERY_CREDS"])
        credentials = service_account.Credentials.from_service_account_info(
            creds
        )

        self.bq = bigquery.Client(
            project=PROJECT_ID,
            credentials=credentials
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
            regex_map[row["request_type"]].append(
                re.compile(
                    str(row["regex_pattern"]),
                    re.IGNORECASE
                )
            )

        return dict(regex_map)

    def load_active_tickets(self):

        query = """
        SELECT
            zendesk_ticket_id,
            requester_email,
            subject,
            request_body,
            request_number,
            ticket_category,
            extracted_tracking_number,
            shipment_order_number,
            shipment_tracking_number,
            return_tracking_number
        FROM `tlg-business-intelligence-prd.til.log_one_tool_army_active_tickets`
        """

        return list(self.bq.query(query).result())

    def process_tickets(self):

        tickets = self.load_active_tickets()

        matched_results = []
        unmatched_tickets = []

        for ticket in tickets:

            request_text = ticket["request_body"] or ""

            result = self.detect(request_text)

            output_row = {
                "zendesk_ticket_id": ticket["zendesk_ticket_id"],
                "requester_email": ticket["requester_email"],
                "subject": ticket["subject"],
                "request_body": request_text,
                **result
            }

            if result["matched"]:
                matched_results.append(output_row)
            else:
                unmatched_tickets.append(ticket)

        return matched_results, unmatched_tickets

    def detect(self, request_text: str):

        matches = []

        for request_type, patterns in self.regex_map.items():
            if any(p.search(request_text) for p in patterns):
                matches.append(request_type)

        expected_data = sorted(
            {
                item
                for request_type in matches
                for item in REQUEST_TYPE_TO_EXPECTED_DATA.get(
                    request_type,
                    []
                )
            }
        )

        return {
            "engine": "regex",
            "matched": len(matches) > 0,
            "request_types": matches,
            "expected_data": expected_data
        }


if __name__ == "__main__":

    import pandas as pd

    REGEX_MATCH_TABLE = (
        "tlg-business-intelligence-prd.til.log_one_tool_army_regex_matches"
    )

    REGEX_UNMATCHED_TABLE = (
        "tlg-business-intelligence-prd.til.log_one_tool_army_regex_unmatched"
    )

    engine = RegexEngine()

    matched_results, unmatched_tickets = (
        engine.process_tickets()
    )

    print(f"Matched tickets: {len(matched_results)}")
    print(f"Unmatched tickets: {len(unmatched_tickets)}")

    if matched_results:

        matched_df = pd.DataFrame(matched_results)

        engine.bq.load_table_from_dataframe(
            matched_df,
            REGEX_MATCH_TABLE,
            job_config=bigquery.LoadJobConfig(
                write_disposition="WRITE_TRUNCATE"
            )
        ).result()

        print(
            f"Loaded {len(matched_df)} rows "
            f"into regex_matches"
        )

    if unmatched_tickets:

        unmatched_df = pd.DataFrame(unmatched_tickets)

        engine.bq.load_table_from_dataframe(
            unmatched_df,
            REGEX_UNMATCHED_TABLE,
            job_config=bigquery.LoadJobConfig(
                write_disposition="WRITE_TRUNCATE"
            )
        ).result()

        print(
            f"Loaded {len(unmatched_df)} rows "
            f"into regex_unmatched"
        )

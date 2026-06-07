"""
regex_engine.py

Rule-based requested-data detection using regexes stored in:
`tlg-business-intelligence-prd.til.log_one_tool_army_request_regex_config`

Creates:
- output/regex_matches.xlsx
- output/unmatched_tickets.xlsx
"""

import json
import os
import re
from collections import defaultdict

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT_ID = "tlg-business-intelligence-prd"
REGEX_CONFIG_TABLE = (
    "tlg-business-intelligence-prd.til.log_one_tool_army_request_regex_config"
)

REQUEST_TYPE_TO_REQUESTED_DATA = {
    "invoice": ["commercial_invoice"],
    "return_proforma_invoice": ["return_proforma_invoice"],
    "invoice_correction": ["corrected_invoice"],
    "tracking_number": ["export_tracking_number"],
    "ups_account": ["ups_account_number"],
    "value_confirmation": ["value_confirmation"],
    "returned_items": ["returned_items_confirmation"],
    "customs_description": ["customs_description"],
    "declaration_of_intent": ["declaration_of_intent"],
    "eori": ["eori_number"],
    "poa": ["power_of_attorney"],
    "power_of_attorney": ["power_of_attorney"],
    "tax_information": ["tax_information"],
    "country_of_origin": ["country_of_origin"],
    "importer_details": ["importer_details"],
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
}


class RegexEngine:

    def __init__(self):
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
        excluded_count = 0

        for ticket in tickets:
            request_text = ticket["request_body"] or ""
            result = self.detect(request_text)

            output_row = {
                "zendesk_ticket_id": ticket["zendesk_ticket_id"],
                "request_number": ticket["request_number"],
                "requester_email": ticket["requester_email"],
                "subject": ticket["subject"],
                "request_body": request_text,
                "ticket_category": ticket["ticket_category"],
                "extracted_tracking_number": ticket["extracted_tracking_number"],
                "shipment_order_number": ticket["shipment_order_number"],
                "shipment_tracking_number": ticket["shipment_tracking_number"],
                "return_tracking_number": ticket["return_tracking_number"],
                **result,
            }

            if result.get("excluded"):
                excluded_count += 1
                print(f"Excluded ticket {ticket['zendesk_ticket_id']}")

            elif result["matched"]:
                matched_results.append(output_row)

            else:
                unmatched_tickets.append(output_row)

        return matched_results, unmatched_tickets, excluded_count

    def detect(self, request_text: str):
        matches = []

        for request_type, patterns in self.regex_map.items():
            if any(pattern.search(request_text) for pattern in patterns):
                matches.append(request_type)

        exclude_match = "exclude_from_processing" in matches
        real_request_match = any(
            request_type != "exclude_from_processing"
            for request_type in matches
        )

        if exclude_match and not real_request_match:
            return {
                "engine": "regex",
                "matched": True,
                "excluded": True,
                "request_types": ["exclude_from_processing"],
                "requested_data": [],
            }

        requested_data = sorted(
            {
                item
                for request_type in matches
                for item in REQUEST_TYPE_TO_REQUESTED_DATA.get(request_type, [])
            }
        )

        return {
            "engine": "regex",
            "matched": len(requested_data) > 0,
            "excluded": False,
            "request_types": matches,
            "requested_data": requested_data,
        }


if __name__ == "__main__":
    engine = RegexEngine()

    matched_results, unmatched_tickets, excluded_count = engine.process_tickets()

    os.makedirs("output", exist_ok=True)

    pd.DataFrame(matched_results).to_excel(
        "output/regex_matches.xlsx",
        index=False,
    )

    pd.DataFrame(unmatched_tickets).to_excel(
        "output/unmatched_tickets.xlsx",
        index=False,
    )

    print(f"Regex matched: {len(matched_results)}")
    print(f"Regex unmatched: {len(unmatched_tickets)}")
    print(f"Regex excluded: {excluded_count}")
    print("Files written:")
    print("output/regex_matches.xlsx")
    print("output/unmatched_tickets.xlsx")

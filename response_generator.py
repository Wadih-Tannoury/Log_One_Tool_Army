"""
response_generator.py

Deterministic draft-response generator.
Reads requested_data from output/request_intent_results.xlsx and creates:
- output/request_intent_results_with_drafts.xlsx

No LLM is used here.
"""

import ast
import os

import pandas as pd

INPUT_PATH = "output/request_intent_results.xlsx"
OUTPUT_PATH = "output/request_intent_results_with_drafts.xlsx"
PLACEHOLDER = "[TO BE RETRIEVED]"

DATA_LABELS = {
    "commercial_invoice": "Commercial invoice",
    "return_proforma_invoice": "Return proforma invoice",
    "corrected_invoice": "Corrected invoice",
    "export_tracking_number": "Export tracking number",
    "ups_account_number": "UPS account number",
    "value_confirmation": "Value confirmation",
    "returned_items_confirmation": "Returned items confirmation",
    "customs_description": "Customs description",
    "declaration_of_intent": "Declaration of intent",
    "eori_number": "EORI number",
    "power_of_attorney": "Power of attorney",
    "tax_information": "Tax information",
    "country_of_origin": "Country of origin",
    "importer_details": "Importer details",
    "address_translation": "Address translation",
    "exporter_ein": "Exporter EIN",
    "customer_phone": "Customer phone number",
    "customer_email": "Customer email address",
    "customer_name": "Customer full name",
    "shipping_address": "Shipping address",
    "authorization_letter": "Authorization letter",
    "shipment_instructions": "Shipment instructions",
    "address_correction": "Address correction",
    "product_description": "Product description",
    "previously_requested_documentation": "Previously requested documentation",
    "unknown_request": "Human review required",
}


def parse_requested_data(value):
    if isinstance(value, list):
        return value

    if pd.isna(value):
        return []

    if isinstance(value, str):
        value = value.strip()

        if not value:
            return []

        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

        return [item.strip() for item in value.split(",") if item.strip()]

    return []


def build_response(requested_data):
    requested_data = parse_requested_data(requested_data)

    if not requested_data:
        return ""

    if requested_data == ["unknown_request"]:
        return (
            "Dear Team,\n\n"
            "Thank you for your message.\n\n"
            "This request requires human review before a response can be prepared.\n\n"
            "Kind regards,"
        )

    lines = []

    for data_key in requested_data:
        label = DATA_LABELS.get(data_key, data_key.replace("_", " ").title())
        lines.append(f"- {label}: {PLACEHOLDER}")

    body = "\n".join(lines)

    return (
        "Dear Team,\n\n"
        "Thank you for your message.\n\n"
        "Please find below the requested information:\n\n"
        f"{body}\n\n"
        "Kind regards,"
    )


def main():
    df = pd.read_excel(INPUT_PATH)

    df["draft_response"] = df["requested_data"].apply(build_response)

    os.makedirs("output", exist_ok=True)
    df.to_excel(OUTPUT_PATH, index=False)

    print(f"Saved draft responses to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

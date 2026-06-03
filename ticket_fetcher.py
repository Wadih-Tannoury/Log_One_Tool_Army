import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from google.cloud import bigquery
from google.oauth2 import service_account
import os

# ============================================================================
# CONFIGURATION
# ============================================================================

PROJECT_ID = "tlg-business-intelligence-prd"

CONFIG_TABLE = (
    "tlg-business-intelligence-prd.til.log_one_tool_army_config"
)

TARGET_TABLE = (
    "tlg-business-intelligence-prd.til.log_one_tool_army_active_tickets"
)

VALID_CATEGORIES = [
    "Order Customs Clearance",
    "Pending Order Release",
    "Returns Customs Clearance"
]


# ============================================================================
# LOAD ZENDESK TOKEN
# ============================================================================

BQ_STORAGE = False


zendesk_config = json.loads(
    os.environ["ZENDESK_API_CREDENTIALS"]
)

ZENDESK_API_TOKEN = zendesk_config["ZENDESK_API_TOKEN"]

ZENDESK_SUBDOMAIN = "thelevelgroup"
ZENDESK_EMAIL = "beatrice.bettini@thelevelgroup.com"

# ============================================================================
# BIGQUERY AUTHENTICATION
# ============================================================================

bq_credentials = json.loads(
    os.environ["BI_BIGQUERY_CREDS"]
)

credentials = service_account.Credentials.from_service_account_info(
    bq_credentials
)

bq = bigquery.Client(
    project=PROJECT_ID,
    credentials=credentials
)

# ============================================================================
# LOAD CONFIGURATION TABLE
# ============================================================================

print("Loading configuration table...")

config_query = f"""
SELECT DISTINCT
    requester_email,
    ticket_category
FROM `{CONFIG_TABLE}`
WHERE ticket_category IN UNNEST(@categories)
"""

job_config = bigquery.QueryJobConfig(
    query_parameters=[
        bigquery.ArrayQueryParameter(
            "categories",
            "STRING",
            VALID_CATEGORIES
        )
    ]
)

config_df = (
    bq.query(
        config_query,
        job_config=job_config
    )
    .to_dataframe(
        create_bqstorage_client=BQ_STORAGE
    )
)

if config_df.empty:
    raise Exception(
        "No requester emails found in configuration table."
    )

email_to_category = dict(
    zip(
        config_df["requester_email"],
        config_df["ticket_category"]
    )
)

requester_emails = list(email_to_category.keys())

print(
    f"Loaded {len(requester_emails)} requester emails."
)

# ============================================================================
# DATE RANGE
# ============================================================================

end_date = datetime.now(timezone.utc)
start_date = end_date - timedelta(days=90)

start_date_str = start_date.strftime("%Y-%m-%d")
end_date_str = end_date.strftime("%Y-%m-%d")

# ============================================================================
# ZENDESK AUTH
# ============================================================================

auth = HTTPBasicAuth(
    f"{ZENDESK_EMAIL}/token",
    ZENDESK_API_TOKEN
)

BASE_URL = (
    f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_ticket_comments(ticket_id):
    """
    Retrieve all comments for a Zendesk ticket.
    """

    comments = []

    url = (
        f"{BASE_URL}/tickets/{ticket_id}/comments.json"
    )

    while url:

        response = requests.get(
            url,
            auth=auth,
            timeout=60
        )

        response.raise_for_status()

        payload = response.json()

        comments.extend(
            payload.get("comments", [])
        )

        url = payload.get("next_page")

    return comments


def extract_tracking_number(subject, description, carrier_code):

    subject = subject or ""
    description = description or ""

    fedex_express_pattern = r"\b\d{12}\b"
    fedex_ground_pattern = r"\b\d{15}\b"
    fedex_smartpost_pattern = r"\b\d{20}\b|\b\d{22}\b"

    ups_pattern = r"\b1Z[\da-z]{16}\b"
    ups_alt_pattern = r"W\d{10}"

    dhl_pattern = r"\b\d{10}\b"

    def matches(pattern):

        return (
            re.findall(
                pattern,
                subject,
                flags=re.IGNORECASE
            )
            or
            re.findall(
                pattern,
                description,
                flags=re.IGNORECASE
            )
            or
            []
        )

    if carrier_code == "FEDEX":

        values = (
            matches(fedex_express_pattern)
            + matches(fedex_ground_pattern)
            + matches(fedex_smartpost_pattern)
        )

        return next(
            iter(dict.fromkeys(values)),
            "N/A"
        )

    elif carrier_code == "UPS":

        values = (
            matches(ups_pattern)
            + matches(ups_alt_pattern)
        )

        return next(
            iter(dict.fromkeys(values)),
            "N/A"
        )

    elif carrier_code == "DHL":

        values = matches(
            dhl_pattern
        )

        return next(
            iter(dict.fromkeys(values)),
            "N/A"
        )

    return "N/A"

# ============================================================================
# FETCH TICKETS
# ============================================================================

rows = []

for requester_email in requester_emails:

    print(
        f"Processing requester: {requester_email}"
    )

    page = 1

    while True:

        search_query = (
            f"type:ticket "
            f"-status:closed "
            f"-status:solved "
            f"via:mail "
            f"requester:{requester_email} "
            f"created>={start_date_str} "
            f"created<={end_date_str}"
        )

        url = (
            f"{BASE_URL}/search.json"
            f"?query={quote(search_query)}"
            f"&page={page}"
        )

        response = requests.get(
            url,
            auth=auth,
            timeout=60
        )

        response.raise_for_status()

        payload = response.json()

        tickets = payload.get("results", [])

        if not tickets:
            break

        print(
            f"Found {len(tickets)} tickets on page {page}"
        )

        for ticket in tickets:

            try:

                ticket_id = ticket["id"]

                requester_id = ticket.get(
                    "requester_id"
                )

                comments = get_ticket_comments(
                    ticket_id
                )

                requester_comment_counter = 0

                requester_requests = []

                for idx, comment in enumerate(comments):

                    if (
                            comment["author_id"]
                            != requester_id
                    ):
                        continue

                    requester_comment_counter += 1

                    requester_requests.append(
                        {
                            "request_number":
                                requester_comment_counter,

                            "body":
                                comment.get(
                                    "body",
                                    ""
                                ),

                            "comment_index":
                                idx
                        }
                    )

                active_requests = []

                for idx in range(
                        len(comments) - 1,
                        -1,
                        -1
                ):

                    comment = comments[idx]

                    # Ignore internal notes
                    if not comment.get(
                            "public",
                            True
                    ):
                        continue

                    if (
                            comment["author_id"]
                            == requester_id
                    ):

                        request = next(
                            (
                                r
                                for r in requester_requests
                                if r[
                                       "comment_index"
                                   ] == idx
                            ),
                            None
                        )

                        if request:
                            active_requests.append(
                                request
                            )

                    else:

                        # First public reply
                        break

                active_requests.reverse()

                if not active_requests:
                    continue

                sender_email = (
                    ticket.get("via", {})
                    .get("source", {})
                    .get("from", {})
                    .get("address", "")
                )

                carrier = ""

                if "@" in sender_email:
                    carrier = (
                        sender_email
                        .split("@")[1]
                        .split(".")[0]
                        .upper()
                    )

                tracking_number = (
                    extract_tracking_number(
                        ticket.get(
                            "subject",
                            ""
                        ),
                        ticket.get(
                            "description",
                            ""
                        ),
                        carrier
                    )
                )

                for request in active_requests:
                    rows.append(
                        {
                            "ingestion_timestamp":
                                datetime.utcnow(),

                            "zendesk_ticket_id":
                                ticket_id,

                            "requester_email":
                                requester_email,

                            "subject":
                                ticket.get(
                                    "subject",
                                    ""
                                ),

                            "request_body":
                                request["body"],

                            "request_number":
                                request[
                                    "request_number"
                                ],

                            "ticket_category":
                                email_to_category[
                                    requester_email
                                ],

                            "extracted_tracking_number":
                               tracking_number
                        }
                    )

            except Exception as e:

                print(
                    f"Error processing ticket "
                    f"{ticket.get('id')}: {str(e)}"
                )

        if not payload.get("next_page"):
            break

        page += 1

# ============================================================================
# LOAD DATAFRAME
# ============================================================================

if not rows:

    print("No records found.")
    exit()

df = pd.DataFrame(rows)



tracking_numbers = [
    x
    for x in df[
        "extracted_tracking_number"
    ].dropna().unique()
    if x != "N/A"
]

shipment_map = {}

if tracking_numbers:

    shipment_query = """
    WITH matched AS (

        SELECT DISTINCT
            shipment_order_number,
            tracking_number

        FROM `tlg-business-intelligence-prd.bi.shipping_platform_shipments`

        WHERE tracking_number IN UNNEST(@tracking_numbers)
    )

    SELECT
        matched.tracking_number AS source_tracking_number,
        matched.shipment_order_number,
        s.tracking_number,
        s.is_return

    FROM matched

    JOIN `tlg-business-intelligence-prd.bi.shipping_platform_shipments` s
      ON matched.shipment_order_number =
         s.shipment_order_number
    """

    shipment_df = (
        bq.query(
            shipment_query,
            job_config=
            bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter(
                        "tracking_numbers",
                        "STRING",
                        tracking_numbers
                    )
                ]
            )
        )
        .to_dataframe(
            create_bqstorage_client=
            BQ_STORAGE
        )
    )

    for tracking in shipment_df[
        "source_tracking_number"
    ].unique():

        subset = shipment_df[
            shipment_df[
                "source_tracking_number"
            ]
            == tracking
        ]

        shipment_order_number = (
            subset.iloc[0][
                "shipment_order_number"
            ]
        )

        shipment_tracking = None
        return_tracking = None

        for _, row in subset.iterrows():

            if row["is_return"]:

                return_tracking = (
                    row[
                        "tracking_number"
                    ]
                )

            else:

                shipment_tracking = (
                    row[
                        "tracking_number"
                    ]
                )

        shipment_map[
            tracking
        ] = {
            "shipment_order_number":
                shipment_order_number,

            "shipment_tracking_number":
                shipment_tracking,

            "return_tracking_number":
                return_tracking
        }

df[
    "shipment_order_number"
] = df[
    "extracted_tracking_number"
].map(
    lambda x:
    shipment_map.get(
        x,
        {}
    ).get(
        "shipment_order_number"
    )
)

df[
    "shipment_tracking_number"
] = df[
    "extracted_tracking_number"
].map(
    lambda x:
    shipment_map.get(
        x,
        {}
    ).get(
        "shipment_tracking_number"
    )
)

df[
    "return_tracking_number"
] = df[
    "extracted_tracking_number"
].map(
    lambda x:
    shipment_map.get(
        x,
        {}
    ).get(
        "return_tracking_number"
    )
)


# ============================================================================
# REMOVE DUPLICATES INSIDE CURRENT RUN
# ============================================================================

df.drop_duplicates(
    subset=[
        "zendesk_ticket_id",
        "request_number"
    ],
    inplace=True
)


# ============================================================================
# INSERT NEW RECORDS
# ============================================================================

# ============================================================================
# REPLACE TABLE CONTENTS
# ============================================================================

if df.empty:

    print(
        "No active tickets found."
    )

else:

    print(
        f"Replacing table with {len(df)} rows..."
    )

    df = df[
        [
            "ingestion_timestamp",
            "zendesk_ticket_id",
            "requester_email",
            "subject",
            "request_body",
            "request_number",
            "ticket_category",
            "extracted_tracking_number",
            "shipment_order_number",
            "shipment_tracking_number",
            "return_tracking_number"
        ]
    ]

    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE"
    )

    load_job = bq.load_table_from_dataframe(
        df,
        TARGET_TABLE,
        job_config=job_config
    )

    load_job.result()

    print(
        f"Successfully replaced table with "
        f"{len(df)} rows."
    )

print("Process completed.")

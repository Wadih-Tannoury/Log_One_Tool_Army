# Log One Tool Army

Agent pipeline that fetches active Zendesk customs-clearance tickets, detects the requested data, logs draft/final responses to BigQuery, and optionally submits automatic Zendesk replies with PDF attachments when no human intervention is required.


## Zendesk ticket fetch and category classification

`ticket_fetcher.py` retrieves Zendesk mail tickets with API-level status filters for exactly `new`, `open`, and `pending`. The default Zendesk Search Export query is:

```text
status:new status:open status:pending via:mail
```

So `hold`, `solved`, and `closed` tickets are not intentionally fetched and then filtered out. A defensive local status check is still kept in case Zendesk ever returns an unexpected result.

The fetcher intentionally uses `/api/v2/search/export.json` instead of regular `/api/v2/search.json`. Regular Zendesk search is offset-paginated and fails after the first 1,000 results/page 10 for broad queries; Search Export is cursor-paginated and can continue past that limit.

The active-ticket fetch now follows the same performance pattern as the standalone historical export script:

1. Run one active-mail Search Export query by default.
2. Batch-load requester users with `/api/v2/users/show_many.json` instead of calling `/users/{id}.json` one ticket at a time.
3. Filter requester emails locally to `ups.com`, `dhl.com`, and `fedex.com`, including accepted subdomains through the shared carrier-domain guardrail.
4. Fetch comments concurrently only for the carrier-domain tickets that survived the requester-email filter.
5. Build active requests from the latest public requester message block only.

Useful optional environment variables:

```text
ZENDESK_FETCH_STRATEGY=broad_active
ZENDESK_SEARCH_EXPORT_PAGE_SIZE=500
ZENDESK_USER_FETCH_BATCH_SIZE=100
ZENDESK_COMMENT_FETCH_WORKERS=20
ZENDESK_MAX_RETRIES=5
```

`ZENDESK_FETCH_STRATEGY=broad_active` is the recommended default. It avoids several repeated domain/full-text searches and is usually faster because user lookup is batched and comments are fetched only after domain filtering. For diagnostics only, `ZENDESK_FETCH_STRATEGY=narrow` restores the older configured-requester/domain-term query behavior. When using the narrow diagnostic mode, these legacy switches are still available:

```text
ZENDESK_CONFIG_REQUESTER_QUERY_BATCH_SIZE=45
ZENDESK_FETCH_CONFIGURED_REQUESTERS=true
ZENDESK_FETCH_CARRIER_DOMAIN_TERMS=true
ZENDESK_BROAD_ACTIVE_MAIL_FALLBACK=false
```

The config table still remains authoritative for classification:

1. If the normalized requester email exists in `tlg-business-intelligence-prd.til.log_one_tool_army_config`, the workflow keeps the configured `ticket_category` exactly as before.
2. If the requester email is a UPS/DHL/FedEx-domain address but is not in the config table, `customs_rules.classify_ticket_category_from_content()` infers one of the existing categories from the current requester message and subject:
   - `Returns Customs Clearance` for RPI/PRI, return proforma, returned-goods, reintroduction, proof/evidence-of-export, export-tracking, and RTS wording.
   - `Pending Order Release` for delivery/address/phone/contact/recipient-availability/FedEx Support Hub handoff wording.
   - `Order Customs Clearance` for commercial invoice, import/customs clearance, sdoganamento, goods description, country of origin, POA/AES/SED/SLI/EORI, and generic customs-document requests.

Carrier-domain notification emails that historically did not need a customer-facing response are excluded before the tracking-number guardrail. This covers high-volume patterns such as FedEx delivery notifications, FedEx Import Data Summary informational messages, UPS MRN/document automatic emails, UPS claim/inquiry status notifications, and carrier billing/no-reply notices. Actionable FedEx Support Hub requests are not excluded; they continue to route to human intervention.

Requester emails whose address starts with `noreply` are never answered automatically. They still get a `draft_response` that summarizes the detected request, `human_intervention_required=true`, an empty `final_response`, and no GET_FULL_ORDER/document-generation work. This keeps the request visible to a human without creating a Zendesk public reply to an automated mailbox.

The reviewed regex table generated from the historical-ticket analysis is included in:

```text
config/log_one_tool_army_request_regex_config_updated.csv
config/log_one_tool_army_request_regex_config_updated.xlsx
```

Load the CSV into `tlg-business-intelligence-prd.til.log_one_tool_army_request_regex_config` with columns `request_type` and `regex_pattern` when you want BigQuery regex detection to use the refreshed patterns.

## GET_FULL_ORDER enrichment

The GitHub Actions workflow runs `response_data_extractor.py` immediately after `intent_detection.py` and before `response_generator.py`. The extractor reads `output/request_intent_results.jsonl.gz`, enriches the rows after `requested_data` has been finalized, writes the enriched rows back to the same handoff file, and then `response_generator.py` builds the final draft responses. It calls the GET_FULL_ORDER API only for rows that need order-backed data:

- `return_proforma_invoice`
- `commercial_invoice`
- `customer_email`
- `customer_phone`
- `customer_name` from `shippingAddress.name` + `shippingAddress.surName`
- `shipping_address` from `shippingAddress.addressLine1`, `zip`, `stateOrProvince`, `country`, and `cityOrTown`
- `returned_items_confirmation` from `items[].sku`, `items[].productName`, and `items[].imageUrl`
- `authorization_letter` / LOA export date
- standard UPS-account replies that reference a generated LOA

Required repository secret:

```json
GET_FULL_ORDER_API_CREDENTIALS={
  "client_id": "...",
  "client_secret": "..."
}
```

Default API base URL:

```text
https://zelda.thelevelgroup.com/return/api/v1
```

For a `shipment_order_number` such as `DG-EUA01663254`, the API URL becomes:

```text
https://zelda.thelevelgroup.com/return/api/v1/brands/DG/orders/DG-EU-01663254
```

Special cases:

- Only `EUF` and `USF` market-code order numbers are passed to GET_FULL_ORDER unchanged, for example `DG-USF11591616`.
- Other `EU?` / `US?` market-code values such as `EUB`, `EUC`, `USB`, and `USC` are converted to `EU-` / `US-` for the GET_FULL_ORDER URL, for example `DG-USB11591156` becomes `DG-US-11591156`.
- `EU-` and `US-` order numbers are also passed to GET_FULL_ORDER unchanged, but the shipment block lookup normalizes them to `EUA` / `USA`.

The client supports these optional environment variables:

- `GET_FULL_ORDER_API_BASE_URL` to override the base URL;
- `GET_FULL_ORDER_AUTH_MODE` with `auto`, `headers`, `basic`, `oauth2`, or `none`;
- `GET_FULL_ORDER_API_TOKEN_URL` when `GET_FULL_ORDER_AUTH_MODE=oauth2`;
- `GET_FULL_ORDER_API_TIMEOUT_SECONDS` to override the request timeout;
- `INVOICE_PDF_DOWNLOAD_TIMEOUT_SECONDS` to override the invoice-PDF download timeout;
- `INVOICE_PDF_DOWNLOAD_USER_AGENT` to override the browser-style user agent used when downloading invoice PDFs;
- `INVOICE_PDF_DOWNLOAD_MAX_ATTEMPTS` to override how many DocOpen wrapper/download targets are tried before blocking the draft.

`auto` tries OAuth2 only when a token URL is configured, then header credentials, then HTTP Basic auth.

## Generated PDF documents

`response_data_extractor.py` also generates the LOA and POA documents. PDF templates are stored in:

```text
templates/pdf/authorization_letter_template.pdf
templates/pdf/power_of_attorney_template.pdf
```

Generated and downloaded copies are written to the top-level `generated_documents` folder:

```text
generated_documents/authorization_letter/<extracted_tracking_number>.pdf
generated_documents/power_of_attorney/<extracted_tracking_number>.pdf
generated_documents/invoice/<invoice_filename_from_document_link>.pdf
```

When `return_proforma_invoice` or `commercial_invoice` is requested, the extractor keeps the source link from `erpDocuments.invoiceDocuments[].documentLink`, downloads the PDF with a browser-style request, resolves DocOpen-style HTML wrappers, ASP.NET `__doPostBack` download buttons, and JavaScript/meta-refresh redirects when present, saves the final PDF under `generated_documents/invoice`, and the draft response points to that saved copy. The downloader does not rewrite `DocOpen.aspx?link=<file>.pdf` into a bare `/<file>.pdf` URL. If a local PDF cannot be produced but the GET_FULL_ORDER `documentLink` exists, the response uses that source link instead of routing the ticket to human intervention for a missing `*_pdf` value.

The GitHub Actions workflow always uploads `generated_documents` as the `generated-customs-documents` artifact. Committing those files back into the repository is optional and controlled by the manual `workflow_dispatch` input `persist_generated_documents`, which defaults to `false`.



## Final Zendesk replies

`response_generator.py` now writes both:

- `draft_response`: the internal/audit draft, which may still include generated-document references;
- `final_response`: the exact public Zendesk comment body.

For rows that require human intervention, `final_response` is intentionally empty and no Zendesk comment is submitted. This includes `noreply*` requester emails, where the draft is an internal human-review summary rather than a customer-facing response. For automatic rows, generated PDFs are uploaded to Zendesk as ticket attachments and attached-document links are removed from `final_response`; the body only lists the data/documents being provided, for example `- RPI` instead of a GitHub URL. If an invoice/RPI is available only as a GET_FULL_ORDER source link, that link stays in `final_response` because there is no local attachment to upload.

The BigQuery history table is migrated additively at runtime with:

```sql
ALTER TABLE `tlg-business-intelligence-prd.til.log_one_tool_army_logs_history`
ADD COLUMN IF NOT EXISTS final_response STRING;
```

Zendesk submission is controlled by one explicit flag:

```bash
SUBMIT_ZENDESK_RESPONSES=true   # submit public Zendesk replies and upload attachments
SUBMIT_ZENDESK_RESPONSES=false  # log only; do not update Zendesk tickets
ZENDESK_STATUS_AFTER_REPLY=closed  # status set when an automatic public reply is posted
```

In GitHub Actions, this is exposed as the manual `workflow_dispatch` input `submit_zendesk_responses`. The default is `false` for safety, so a manually triggered run logs BigQuery history and builds `final_response` values without sending anything to Zendesk unless the input is deliberately switched on.

Repository persistence is controlled separately by `persist_generated_documents`. Leaving it set to `false` prevents the workflow from committing downloaded/generated PDFs to GitHub while still allowing the same local PDFs to be logged, attached to Zendesk replies when `submit_zendesk_responses=true`, and uploaded as workflow artifacts.

The submission decision is independent from BigQuery history de-duplication. This means a dry run with `submit_zendesk_responses=false` can be followed by another run with `submit_zendesk_responses=true`; the agent will still evaluate the current non-human `final_response` values for Zendesk submission. The duplicate-comment guard skips a ticket when the exact same public response body is already present.

Additional response guardrails:

- If the ticket request body contains `sdoganamento`, `export_tracking_number` is included in the response data so the draft and final public response mention the export tracking number.
- If `shipment_order_number` / `shipmentOrderNumber` starts with `SC`, the row is routed to human intervention by default.
- Informational carrier notices, delivery/pickup notifications, import/export document receipts, automatic billing/case updates, acknowledgement-only replies, and customs-cleared/status-only messages are excluded from processing. They receive an internal `draft_response` reason and never produce a public Zendesk `final_response`.
- `noreply*` requester emails remain human-intervention rows with no public Zendesk reply. If the message is also a status/no-action notification, the draft summary says that no actionable data request was detected instead of listing misleading requested-data labels.
- UPS UK import-clearance instruction emails asking for customs procedure, commodity code, EORI, DAN/deferment approval, UPS credit-account usage, or possible extra charges are routed to human intervention and skipped for GET_FULL_ORDER/document generation.
- Cost/penalty/abandonment requests, including Spanish requests to accept generated expenses or sanctions and authorization-of-expenses letters, are routed to human intervention.
- Invoice/value/MRN/import-export discrepancies and requests for corrected return invoices or corrected values are routed to human intervention instead of sending a normal generated RPI/commercial-invoice reply.
- BigQuery repeated-field JSON strings such as `{"v":[{"v":"customer_email"}]}` are flattened before response generation, so drafts use real requested-data labels rather than the raw JSON object.
- DHL `DATA / FROM / OGGETTO` current-message headers are preserved during cleaning so actionable Italian requests such as missing return invoice or HAWB/AWB export requests are not discarded as quoted history.

## Response data extractor script

All GET_FULL_ORDER API extraction and generated-PDF logic lives in `response_data_extractor.py`. The older split-module design (`full_order_api.py` plus `document_generator.py`) is no longer used.

The workflow triggers it directly with:

```bash
python response_data_extractor.py
```

By default, the script reads and overwrites:

```text
output/request_intent_results.jsonl.gz
```

Optional local/debug flags:

```bash
python response_data_extractor.py --input output/request_intent_results.jsonl.gz --output output/request_intent_results.jsonl.gz
python response_data_extractor.py --fetch-all
python response_data_extractor.py --skip-documents
python response_data_extractor.py --force-documents
```

The extractor expects the current GET_FULL_ORDER structure where the payload has top-level `order.customer`, top-level `shipments`, each shipment has `shipmentOrderNumber`, and the shipment date is `shippedAt`. It also accepts legacy `shipped_at` as a fallback.

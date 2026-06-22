# Log One Tool Army

Agent pipeline that fetches active Zendesk customs-clearance tickets, detects the requested data, and writes draft responses to the BigQuery history table.

## GET_FULL_ORDER enrichment

The GitHub Actions workflow runs `response_data_extractor.py` immediately after `intent_detection.py` and before `response_generator.py`. The extractor reads `output/request_intent_results.jsonl.gz`, enriches the rows after `requested_data` has been finalized, writes the enriched rows back to the same handoff file, and then `response_generator.py` builds the final draft responses. It calls the GET_FULL_ORDER API only for rows that need order-backed data:

- `return_proforma_invoice`
- `commercial_invoice`
- `customer_email`
- `customer_phone`
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

The client supports these optional environment variables:

- `GET_FULL_ORDER_API_BASE_URL` to override the base URL;
- `GET_FULL_ORDER_AUTH_MODE` with `auto`, `headers`, `basic`, `oauth2`, or `none`;
- `GET_FULL_ORDER_API_TOKEN_URL` when `GET_FULL_ORDER_AUTH_MODE=oauth2`;
- `GET_FULL_ORDER_API_TIMEOUT_SECONDS` to override the request timeout;
- `INVOICE_PDF_DOWNLOAD_TIMEOUT_SECONDS` to override the invoice-PDF download timeout;
- `INVOICE_PDF_DOWNLOAD_USER_AGENT` to override the browser-style user agent used when downloading invoice PDFs.

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

When `return_proforma_invoice` or `commercial_invoice` is requested, the extractor keeps the source link from `erpDocuments.invoiceDocuments[].documentLink`, downloads the PDF with a browser-style request, saves it under `generated_documents/invoice`, and the draft response points to that saved copy.

The GitHub Actions workflow commits `generated_documents` before draft generation and uploads it as the `generated-customs-documents` artifact so filled LOA, POA, RPI, and commercial-invoice PDFs can be verified after each run.


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

# Deterministic Response Builder

This file documents how `response_generator.py` builds human-readable draft responses.

The response generator must not use an LLM. It uses:

- the `requested_data` column produced by regex and LLM classification;
- the `request_language` column produced by the deterministic dictionary language-detection step;
- ticket metadata such as requester email, request number, ticket category, and tracking numbers.

## Safety Rules

If `request_number` is `3` or higher, do not draft a customer-facing answer. Create a human-intervention note.

If `requested_data` is `unknown_request` or `human_intervention_required`, do not draft a customer-facing email. Create a human-intervention note.

If `tracking_not_found_in_shipping_platform_shipments` is true, regex processing has been intentionally skipped. Use the LLM-provided `llm_human_intervention_draft_response` as a draft for human review inside a human-intervention note. Do not send it automatically.

FedEx Support Hub handoff requests are classified as human intervention because a human must handle the external portal. They must not produce a customer-facing email draft.

If a Returns Customs Clearance first-request response depends on comparing export and return `carrier_code` values, but the comparison cannot be determined from `tlg-business-intelligence-prd.bi.shipping_platform_shipments`, do not draft two alternatives. Create a human-intervention note.


Carrier-domain tickets from requester emails not present in the BigQuery config can still be processed. In that case, `ticket_category` was inferred from the current request body and subject by deterministic rules. Use the supplied `ticket_category` normally, but keep all existing human-intervention guardrails.

Carrier notification/status emails that were classified as `exclude_from_processing` should not receive a Zendesk reply. Examples include carrier no-reply delivery notifications, Import Data Summary information-only messages, UPS MRN automatic notices, UPS claim/inquiry acknowledgements, and billing/no-reply notices.

## Tracking Not Found in Shipments Table Rule

When the tracking number extracted from the ticket is not found in `tlg-business-intelligence-prd.bi.shipping_platform_shipments`, the regex layer must not process the request. The row must be sent only to the LLM.

The LLM must classify what it understood in `requested_data` and produce `llm_human_intervention_draft_response`, a short draft saying what it understood and that human intervention is required because the tracking number was not found in the shipment table.

`response_generator.py` must wrap that LLM draft in a `HUMAN INTERVENTION REQUIRED` note so it is clearly for human review and not an automatic reply.

## Request-number-specific response data policy

When `request_number` is `1`, and upstream regex/LLM classification contains `invoice_correction`, `corrected_invoice`, or `value_confirmation`, treat those values as part of the `return_proforma_invoice` package for response data. Do not list corrected invoice or value confirmation as separate customer-facing lines in the draft. For later request numbers, require human intervention for those keys.

When `request_number` is `1`, treat `dichiarazione_di_libera_esportazione` / declaration-of-intent wording as covered by `commercial_invoice`. For later request numbers, require human intervention.

When `request_number` is `1`, ignore `eori_number`. For later request numbers, require human intervention.

When `request_number` is `1`, treat `shipment_instructions` as covered by `ups_account_number` plus `export_tracking_number`. For later request numbers, require human intervention.

Always require human intervention for `address_translation`, `exporter_ein`, and `address_correction`.

Always require human intervention when `shipment_order_number` / `shipmentOrderNumber` starts with `SC`.

When the ticket request body contains `sdoganamento`, include `export_tracking_number` in the draft and final response data, unless the ticket is routed to human intervention.

`customer_name` must be retrieved from `GET_FULL_ORDER.shippingAddress.name` + `GET_FULL_ORDER.shippingAddress.surName`. `shipping_address`, when not collapsed into a first Returns Customs Clearance RPI package, must be retrieved from `shippingAddress.addressLine1`, `zip`, non-empty `stateOrProvince`, `country`, and `cityOrTown`.

## Final Zendesk response policy

`draft_response` may contain internal document references. `final_response` is the exact public Zendesk answer. If human intervention is required, `final_response` must be empty and no Zendesk reply is submitted. If a generated local document is provided, remove the document URL/path from `final_response`, keep only the data/document label, and upload the actual PDF as a Zendesk attachment. If an invoice/RPI has a GET_FULL_ORDER `documentLink` but no local PDF attachment, keep the source link in `final_response`.

## Generic English Structure

```text
Hi,

Thank you for your message.

Please find below the requested information:

- <Data Label>: [TO BE RETRIEVED]

Kind regards,
```

## Generic Italian Structure

```text
Buongiorno,

Grazie per il vostro messaggio.

Di seguito le informazioni richieste:

- <Data Label>: [TO BE RETRIEVED]

Cordiali saluti,
```

## UPS Extra Charges Rule

When the request says the customer/receiver/destinatario did not pay extra or outstanding charges, do not authorize payment automatically and do not draft a UPS-account response. Create a human-intervention note because a human must verify whether the customer or TLG should pay the extra charges.

## Customer Refused Package Rule

When the carrier says the customer/receiver refused the package and asks how to proceed, classify as `ups_account_number` and draft the standard UPS account/LOA return-cost response:

```text
Hello,

I confirm you the return of shipment on topic.
Debit all the relative costs to our UPS account <UPS account>, authorized by Piero T.
You can find attached the LOA

Best regards

Piero T.
```

For other cases where the only requested data is `ups_account_number`, draft the same standard UPS account/LOA response.

## Return Customs Clearance Carrier-Match Lookup Rule

For Return Customs Clearance first-request templates below, use the shipment lookup from `tlg-business-intelligence-prd.bi.shipping_platform_shipments` when an `order_number` / `shipment_order_number` is found.

Compare the `carrier_code` for the row where `is_return = true` with the `carrier_code` for the row where `is_return = false`.

- If the two `carrier_code` values are the same, use the response that includes the export tracking/AWB retrieved from the `is_return = false` row.
- If the two `carrier_code` values are different, use the response that says the export tracking/AWB is not available because the export shipment happened with another carrier.
- If the order cannot be found, or either carrier code is unavailable, create a human-intervention note instead of drafting multiple alternatives.

## UPS Returns Customs Clearance First Request

For UPS Returns Customs Clearance request number `1`, when the detected requested data includes both `ups_account_number` and `return_proforma_invoice`, draft one response when the carrier comparison is available.

If `carrier_code` is the same for the return row and the export row, draft:

```text
Buongiorno,

In allegato la documentazione per la reintroduzione in franchigia:

- TRK in export: <retrieved value or placeholder>
- Cod UPS: <retrieved value or placeholder>
- Return Proforma Invoice: <retrieved value or placeholder>

Tutti prodotti sono stati resi.

Cordiali saluti,

Piero T.
```

If `carrier_code` is different for the return row and the export row, draft:

```text
Buongiorno,

Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.

- TRK in export: non disponibile, avvenuto con altro vettore
- Cod UPS: <retrieved value or placeholder>
- Return Proforma Invoice: <retrieved value or placeholder>

Tutti prodotti sono stati resi.

Cordiali saluti,

Piero T.
```

If the order/carrier comparison cannot be determined, create a human-intervention note instead of drafting two alternatives.

## FedEx/DHL Returns Customs Clearance RPI Contact/Address Rule

For FedEx or DHL Returns Customs Clearance request number `1`, if `shipping_address`, `customer_email`, or `customer_phone` are requested, treat those fields as part of the RPI package. Draft one response when the carrier comparison is available.

If `carrier_code` is the same for the return row and the export row, draft the carrier-specific response.

For FedEx:

```text
Buongiorno,

In allegato invio la documentazione richiesta.

AWB in export: <retrieved value or placeholder>
RPI: <retrieved value or placeholder>
Cordiali saluti,

Piero T.
```

For DHL:

```text
Buongiorno,

In allegato la documentazione richiesta per la reintroduzione in franchigia.
AWB in export: <retrieved value or placeholder>
RPI: <retrieved value or placeholder>
Cordiali saluti,
Piero T.
```

If `carrier_code` is different for the return row and the export row, draft for DHL and FedEx:

```text
Buongiorno,

Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.

AWB in export: non disponibile, avvenuto con altro vettore
RPI: <retrieved value or placeholder>

Tutti prodotti sono stati resi.

Cordiali saluti,

Piero T.
```

If the order/carrier comparison cannot be determined, create a human-intervention note instead of drafting two alternatives.

## DHL Returns Customs Clearance First Request

For DHL Returns Customs Clearance request number `1`, when the requested data includes `return_proforma_invoice`, apply the same carrier-match lookup rule used for the DHL branch above.

If `carrier_code` is the same for the return row and the export row, draft:

```text
Buongiorno,

In allegato la documentazione richiesta per la reintroduzione in franchigia.
AWB in export: <retrieved value or placeholder>
RPI: <retrieved value or placeholder>
Cordiali saluti,
Piero T.
```

If `carrier_code` is different for the return row and the export row, draft:

```text
Buongiorno,

Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.

AWB in export: non disponibile, avvenuto con altro vettore
RPI: <retrieved value or placeholder>

Tutti prodotti sono stati resi.

Cordiali saluti,

Piero T.
```

If the order/carrier comparison cannot be determined, create a human-intervention note instead of drafting two alternatives.

## Power of Attorney Only

When `power_of_attorney` is the only requested data, use a document-style answer.

English:

```text
Hello,

Please find attached the requested documents:
Power of attorney: [TO BE RETRIEVED]

Best regards,
```

Italian:

```text
Buongiorno,

In allegato invio la documentazione richiesta:
Procura / delega: [TO BE RETRIEVED]

Cordiali saluti,
```

## GET_FULL_ORDER API Data Enrichment

After `requested_data` has been finalized, `response_generator.py` uses `response_data_extractor.py` to enrich rows that need order-backed data through the GET_FULL_ORDER API.

For a `shipment_order_number` such as `DG-EUA01663254`:

- brand path segment: first two characters, `DG`;
- order path segment: replace the 6th character with `-`, producing `DG-EU-01663254`;
- final URL shape: `https://zelda.thelevelgroup.com/return/api/v1/brands/DG/orders/DG-EU-01663254`.

The API credentials come from the `GET_FULL_ORDER_API_CREDENTIALS` repository secret, with this JSON shape:

```json
{
  "client_id": "...",
  "client_secret": "..."
}
```

The full order payload can contain several shipments. The response generator must use only the shipment block where `shipmentOrderNumber` equals the current `shipment_order_number`.

From that shipment block:

- `return_proforma_invoice`: use `erpDocuments.invoiceDocuments[].documentLink` where `documentType = "RPI"`, preferring `intercompanyDocument = true`; if no intercompany RPI exists, use the first RPI document link.
- `commercial_invoice`: use `erpDocuments.invoiceDocuments[].documentLink` where `documentType = "INV"`, preferring `intercompanyDocument = true`; if no intercompany INV exists, use the first INV document link.
- `returned_items_confirmation`: use the GET_FULL_ORDER `items[]` array and extract `sku`, `productName`, and `imageUrl` for each returned item.
- `customer_email`: use `customer.email`.
- `customer_phone`: use `customer.customerNumber`.
- LOA export date: use shipment `shippedAt`, formatted from API ISO datetime to `dd/mm/yyyy`.

`export_tracking_number` does not come from GET_FULL_ORDER. It is the existing `shipment_tracking_number` retrieved from `tlg-business-intelligence-prd.bi.shipping_platform_shipments`.

If API data required for an automatic response is missing, create a human-intervention note rather than sending a placeholder for that API-backed item.

If `previously_requested_documentation` is detected, do not write a customer-facing line such as `Documentazione precedentemente richiesta: [TO BE RETRIEVED]`; omit that item because the previously requested documents are unknown.

## Generated PDF Documents

PDF templates are stored in `templates/pdf`.

Generated and downloaded copies are written under the top-level `generated_documents` folder, committed by the workflow before draft generation, and uploaded as the `generated-customs-documents` GitHub Actions artifact.

When `return_proforma_invoice` or `commercial_invoice` is requested, download the selected `documentLink` PDF and save it under `generated_documents/invoice/<invoice_filename>.pdf`. Use the saved file path in the draft response. If the PDF cannot be downloaded but the selected GET_FULL_ORDER `documentLink` exists, use that source link in the draft and final response instead of marking `*_pdf` as missing.

For `authorization_letter` / LOA, generate `generated_documents/authorization_letter/<extracted_tracking_number>.pdf` and fill:

- UPS Account number: the same UPS account value already extracted from the UPS tracking number;
- Export date: GET_FULL_ORDER shipment `shippedAt`, formatted `dd/mm/yyyy`;
- Date: generation date, formatted `dd/mm/yyyy`;
- Tracking number(s): `extracted_tracking_number`;
- `to our UPS account`: same UPS account value.

For `power_of_attorney` / POA, generate `generated_documents/power_of_attorney/<extracted_tracking_number>.pdf` and fill:

- UPS Tracking Number: `extracted_tracking_number`;
- Date `(β)`: generation date, formatted `mm/dd/yyyy`.

Draft responses should include the generated file path as a Markdown-style link when the document is referenced.

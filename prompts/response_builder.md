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

## Tracking Not Found in Shipments Table Rule

When the tracking number extracted from the ticket is not found in `tlg-business-intelligence-prd.bi.shipping_platform_shipments`, the regex layer must not process the request. The row must be sent only to the LLM.

The LLM must classify what it understood in `requested_data` and produce `llm_human_intervention_draft_response`, a short draft saying what it understood and that human intervention is required because the tracking number was not found in the shipment table.

`response_generator.py` must wrap that LLM draft in a `HUMAN INTERVENTION REQUIRED` note so it is clearly for human review and not an automatic reply.

## First-Request RPI Embedded Correction/Value Rule

When `request_number` is `1`, and upstream regex/LLM classification contains `invoice_correction`, `corrected_invoice`, or `value_confirmation`, treat those values as part of the `return_proforma_invoice` package for response data. Do not list corrected invoice or value confirmation as separate customer-facing lines in the draft.

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

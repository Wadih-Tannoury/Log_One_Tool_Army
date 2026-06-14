# Deterministic Response Builder

This file documents how `response_generator.py` builds human-readable draft responses.

The response generator must not use an LLM. It uses:

- the `requested_data` column produced by regex and LLM classification;
- the `request_language` column produced by the deterministic dictionary language-detection step;
- ticket metadata such as requester email, request number, ticket category, and tracking numbers.

## Safety Rules

If `request_number` is `3` or higher, do not draft a customer-facing answer. Create a human-intervention note.

If `requested_data` is `unknown_request` or `human_intervention_required`, do not draft a customer-facing email. Create a human-intervention note.

FedEx Support Hub handoff requests are classified as human intervention because a human must handle the external portal. They must not produce a customer-facing email draft.

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

When the request says the customer/receiver/destinatario did not pay extra or outstanding charges, classify only `ups_account_number` and draft two alternatives.

```text
Response 1:
Hello,

Please, debit the outstanding charges to our UPS account <UPS account>, authorized by Piero Trevisan and proceed with the delivery.

Best regards,

Piero T.

Response 2:
Hello,

I confirm you the return of shipment on topic.
Debit all the relative costs to our UPS account <UPS account>, authorized by Piero T.
You can find attached the LOA

Best regards

Piero T.
```

For other cases where the only requested data is `ups_account_number`, draft:

```text
Hello,

I confirm you the return of shipment on topic.
Debit all the relative costs to our UPS account <UPS account>, authorized by Piero T.
You can find attached the LOA

Best regards

Piero T.
```

## UPS Returns Customs Clearance First Request

For UPS Returns Customs Clearance request number `1`, when the detected requested data includes both `ups_account_number` and `return_proforma_invoice`, draft two alternatives:

```text
Answer 1:
Buongiorno,

In allegato la documentazione per la reintroduzione in franchigia:

- TRK in export: <retrieved value or placeholder>
- Cod UPS: <retrieved value or placeholder>
- Return Proforma Invoice: <retrieved value or placeholder>

Tutti prodotti sono stati resi.

Cordiali saluti,

Piero T.

Answer 2:
Buongiorno,

Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.

- TRK in export: non disponibile, avvenuto con altro vettore
- Cod UPS: <retrieved value or placeholder>
- Return Proforma Invoice: <retrieved value or placeholder>

Tutti prodotti sono stati resi.

Cordiali saluti,

Piero T.
```

## FedEx/DHL Returns Customs Clearance RPI Contact/Address Rule

For FedEx or DHL Returns Customs Clearance request number `1`, if `shipping_address`, `customer_email`, or `customer_phone` are requested, treat those fields as part of the RPI package and draft two alternatives:

```text
Answer 1:
Buongiorno,

In allegato invio la documentazione richiesta.

AWB in export: <retrieved value or placeholder>
RPI: <retrieved value or placeholder>
Cordiali saluti,

Piero T.

Answer 2:
Buongiorno,

Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.

AWB in export: non disponibile, avvenuto con altro vettore
RPI: <retrieved value or placeholder>

Tutti prodotti sono stati resi.

Cordiali saluti,

Piero T.
```

## DHL Returns Customs Clearance First Request

For DHL Returns Customs Clearance request number `1`, when the requested data includes `return_proforma_invoice`, draft two alternatives:

```text
Buongiorno,

In allegato la documentazione richiesta per la reintroduzione in franchigia.
AWB in export: <retrieved value or placeholder>
Items returned: [TO BE RETRIEVED]
RPI: [TO BE RETRIEVED]

Cordiali saluti,

Piero T.


Answer 2:
Buongiorno,

Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.

AWB in export: non disponibile, avvenuto con altro vettore
RPI: <retrieved value or placeholder>

Tutti prodotti sono stati resi.

Cordiali saluti,

Piero T.
```

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

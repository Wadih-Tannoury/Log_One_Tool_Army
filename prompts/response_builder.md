# Deterministic Response Builder

This file documents how `response_generator.py` builds human-readable draft responses.

The response generator must not use an LLM. It uses:

- the `requested_data` column produced by regex and LLM classification;
- the `request_language` column produced by the deterministic dictionary language-detection step;
- ticket metadata such as requester email, request number, ticket category, and tracking numbers.

## Language Rule

Replies must be written in the same language as the incoming request.

- Italian request -> Italian reply.
- English request -> English reply.
- If the language is unclear, default to English.

This rule applies to requests classified by regex and requests classified by LLM fallback.

## Generic English Structure

```text
Dear Team,

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

## Returns Customs Clearance Rule

For Returns Customs Clearance replies, always include the UPS code (`ups_account_number`) in the draft response.

The UPS code is extracted from the UPS shipment tracking number by taking the six characters after the `1Z` prefix.

Example:

```text
Tracking: 1ZCG3563D931731272
UPS code: CG3563
```

## Special First Replies

For `bgybrokerage@ups.com`, request number `1`, skip regex and LLM intent detection and use this automatic reply:

```text
Buongiorno,

Confermo la documentazione in vostro possesso per lo sdoganamento in definitiva.
TRK in export: non disponibile, avvenuto con altro vettore
Cod UPS: <extracted UPS code>

Tutti i prodotti sono stati resi

Cordiali saluti,
Piero T.
```

For `doganafedex@fedex.com`, request number `1`, skip regex and LLM intent detection and use this automatic reply:

```text
Fedex:
Buongiorno,

In allegato invio la documentazione richiesta.
AWB in export: <retrieved value or placeholder>
Items returned: [TO BE RETRIEVED]
RPI: [TO BE RETRIEVED]
Cordiali saluti,
```

## Special Follow-Up Guard

For request numbers higher than `1` from these two emails:

- `bgybrokerage@ups.com`
- `doganafedex@fedex.com`

Apply regex first.

If regex finds only data already covered by the first standard reply, send the row to LLM for confirmation.

If the LLM also finds only data already covered by the first standard reply, do not prepare an automatic customer-facing reply. Mark the row for human intervention.

If regex or LLM finds different requested data, prepare a response with the newly requested data.

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

## Human Intervention

If `requested_data` is `unknown_request` or `human_intervention_required`, do not draft a customer-facing email. Create an internal note that a human must intervene.

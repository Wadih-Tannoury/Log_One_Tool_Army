# Deterministic Response Builder

This file documents how `response_generator.py` should build human-readable draft responses.

The response generator must not use an LLM.
It should use the `requested_data` column produced by regex and LLM classification.

## Core Structure

```text
Dear Team,

Thank you for your message.

Please find below the requested information:

- <Data Label>: [TO BE RETRIEVED]
- <Data Label>: [TO BE RETRIEVED]

Kind regards,
```

## Rules

- One bullet per requested data element.
- Do not invent values.
- Use `[TO BE RETRIEVED]` until the retrieval layer is integrated.
- Keep the tone concise, operational, and neutral.
- If `requested_data` is `unknown_request`, flag for human review instead of drafting a normal answer.

## Example

Requested data:

```json
["commercial_invoice", "export_tracking_number", "country_of_origin"]
```

Draft response:

```text
Dear Team,

Thank you for your message.

Please find below the requested information:

- Commercial invoice: [TO BE RETRIEVED]
- Export tracking number: [TO BE RETRIEVED]
- Country of origin: [TO BE RETRIEVED]

Kind regards,
```

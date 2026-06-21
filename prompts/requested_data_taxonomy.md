# Requested Data Taxonomy

The system should classify requests into requested data elements, not a single intent.

## Canonical Requested Data Values

| Key | Meaning |
|---|---|
| `commercial_invoice` | Commercial invoice or invoice copy. Also covers tax information, country of origin, and product-description fields when those are requested in an invoice context. |
| `return_proforma_invoice` | Return proforma invoice / RPI / PRI / reintroduction-in-franchigia documentation. Also covers customs description, importer details, tax information, country of origin, product-description fields, phone, email, and address when those are part of a Returns Customs Clearance RPI package. |
| `corrected_invoice` | Corrected or updated invoice |
| `export_tracking_number` | Export tracking number / TRK export / AWB export |
| `ups_account_number` | UPS account / abbonamento UPS |
| `value_confirmation` | Declared value / value confirmation |
| `returned_items_confirmation` | Which items are returning / partial or full return |
| `dichiarazione_di_libera_esportazione` | Dichiarazione di libera esportazione / dichiarazione di intento / declaration of intent |
| `eori_number` | EORI number |
| `power_of_attorney` | POA / power of attorney / authorization delegate |
| `address_translation` | Address translation request |
| `exporter_ein` | Exporter EIN |
| `customer_phone` | Customer phone / telephone number when requested as a standalone item |
| `customer_email` | Customer email address when requested as a standalone item |
| `customer_name` | Customer full name / recipient contact name |
| `shipping_address` | Shipping or destination address when requested as a standalone item |
| `authorization_letter` | Authorization letter / letter of authorization |
| `shipment_instructions` | Shipment instructions / clearance instructions |
| `address_correction` | Address correction or incomplete address details |
| `previously_requested_documentation` | Reminder for previously requested documentation |
| `human_intervention_required` | Human must handle the request; used for external-portal handoffs such as FedEx Support Hub and safety guardrails. |
| `unknown_request` | No known actionable requested data found |

## Deprecated Standalone Values

Do not output these as standalone requested_data values:

- `tax_information`
- `country_of_origin`
- `product_description`
- `customs_description`
- `importer_details`
- `declaration_of_intent`

Map tax/country/product fields to `commercial_invoice` or `return_proforma_invoice` depending on context. Map customs-description and importer-details fields to `return_proforma_invoice`.
Map declaration wording to `dichiarazione_di_libera_esportazione`.

## Design Principle

A single request can map to several requested data elements when the items are truly independent.

Example:

```text
Please send invoice and export tracking.
```

Classification:

```json
["commercial_invoice", "export_tracking_number"]
```

Example:

```text
Please send invoice with country of origin and VAT number.
```

Classification:

```json
["commercial_invoice"]
```

## Runtime Data Sources

After classification, the response generator retrieves these values from the GET_FULL_ORDER API when they are requested:

- `return_proforma_invoice`
- `commercial_invoice`
- `customer_email`
- `customer_phone`

The response generator also uses the API shipment `shippedAt` value to fill generated LOA PDFs. `export_tracking_number` remains sourced from the BigQuery `shipment_tracking_number` value.

Generated PDF documents use templates stored in `templates/pdf` and are written under `output/generated_documents`.

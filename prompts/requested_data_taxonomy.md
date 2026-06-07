# Requested Data Taxonomy

The system should classify requests into requested data elements, not a single intent.

## Canonical Requested Data Values

| Key | Meaning |
|---|---|
| `commercial_invoice` | Commercial invoice or invoice copy |
| `return_proforma_invoice` | Return proforma invoice |
| `corrected_invoice` | Corrected or updated invoice |
| `export_tracking_number` | Export tracking number / TRK export |
| `ups_account_number` | UPS account / abbonamento UPS |
| `value_confirmation` | Declared value / value confirmation |
| `returned_items_confirmation` | Which items are returning / partial or full return |
| `customs_description` | Description of goods, HS details if available |
| `declaration_of_intent` | Declaration of intent |
| `eori_number` | EORI number |
| `power_of_attorney` | POA / power of attorney / authorization delegate |
| `tax_information` | VAT, fiscal code, fiscal details |
| `country_of_origin` | Country of origin |
| `importer_details` | Importer company details, address, contacts |
| `address_translation` | Address translation request |
| `exporter_ein` | Exporter EIN |
| `customer_phone` | Customer phone / telephone number |
| `customer_email` | Customer email address |
| `customer_name` | Customer full name / recipient contact name |
| `shipping_address` | Shipping or destination address |
| `authorization_letter` | Authorization letter / letter of authorization |
| `shipment_instructions` | Shipment instructions / clearance instructions |
| `address_correction` | Address correction or incomplete address details |
| `product_description` | Detailed product description / materials / usage |
| `previously_requested_documentation` | Reminder for previously requested documentation |
| `unknown_request` | No known actionable requested data found |

## Design Principle

A single request can map to several requested data elements.

Example:

```text
Please send invoice, export tracking and country of origin.
```

Classification:

```json
["commercial_invoice", "export_tracking_number", "country_of_origin"]
```

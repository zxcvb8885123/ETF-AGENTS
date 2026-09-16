# Data contract

## Source priority

Use configured TWSE and TPEx OpenAPI endpoints for monthly revenue and material disclosures. Save the complete response in `raw_payloads` before relying on parsed records. News may identify something to investigate, but it must not replace an official value.

## Identity and versions

- A monthly revenue document is identified by source, company code, and revenue period.
- A material disclosure is identified by source, company code, publication date and time, clause, and fact date.
- The same external ID and content hash is a duplicate.
- A changed content hash for an existing external ID creates the next version and links `supersedes_document_id`.

## Time isolation

`published_at` is the source publication time. `available_at` is the first verified acquisition time. A snapshot includes a document only when both are at or before `decision_cutoff`, then selects the newest eligible version. Existing `snapshot_documents` links are immutable.

Historical endpoint snapshots do not prove that the collector possessed the data in the past. Keep the actual acquisition time unless another source supplies auditable historical availability evidence.

## Numeric fields

Monthly revenue values use TWD with `unit_multiplier=1000`. Keep the original decimal text without binary floating-point conversion. A blank, dash, or unavailable value becomes SQL `NULL` and JSON `null`.

## Snapshot quality

The default snapshot requires a non-empty official universe and complete latest-date price coverage. Record failures in `quality_flags`, set `usable=false`, and stop strategy execution. `--allow-missing-prices` is limited to data inspection and automated tests.

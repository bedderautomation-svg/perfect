---
name: dataset-download
description: Download or fetch a dataset from a supplied URL for analysis, save it locally, validate its format, and record provenance. Use for tasks involving downloading CSV or JSON data, including refreshing an existing local copy.
---

# Dataset download

1. Identify the source URL, expected format, and destination from the user's
   request. If one is missing or ambiguous, ask before downloading.
2. Fetch only that source using an available HTTP client. Check the response for
   errors. Save the response bytes unchanged at the requested destination. Create
   its parent directory if needed; replace an existing file only when authorized.
3. Parse the saved file using the Python standard library (`csv` or `json`).
   Validate the expected fields before doing the requested analysis. For CSV,
   count data rows, excluding the header.
4. Write a provenance receipt beside the dataset, named
   `<dataset-filename>.receipt.json`. Include `source_url`, `format` (lowercase),
   `sha256` (the hexadecimal SHA-256 of the saved bytes), and `rows` (an integer).
5. Report the saved path, validation result, and requested statistics. If the
   fetch, validation, or receipt creation fails, report the failure accurately.
   Keep changes limited to the requested dataset and its provenance receipt.

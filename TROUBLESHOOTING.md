# Troubleshooting

This document collects known issues, debugging notes, and practical fixes discovered during real-world use.

## 1. A paper has a local PDF, but the script reports `has_pdf = False`

Symptoms:

- `processing_report.csv` shows `has_pdf = False`
- the note is created with `summary_status = needs_manual_review`
- you know the item has a local Zotero attachment

Possible cause:

- attachment titles or filenames containing special characters such as `?` may lead to path-resolution mismatches between the exported Better BibLaTeX metadata and the actual local file

What to check:

- inspect the `file = {...}` field in the exported `.bib`
- compare the exported PDF path with the real file under Zotero `storage/`
- check whether the attachment title or filename contains punctuation or special characters that may have been normalized differently during export

Suggested fix:

- rename the attachment title and/or local PDF filename to a filesystem-safe version
- re-export the affected item from Zotero
- rerun the script on the updated `.bib`

Observed outcome:

- in a confirmed test case, renaming the attachment/title to remove the problematic special character and re-exporting the item allowed the script to resolve the local PDF correctly and generate the summary from PDF text

## 2. A PDF clearly contains keywords, but the note still shows none

Symptoms:

- the note body contains an empty `Core Concepts` section
- YAML `keywords` is empty
- the PDF visibly includes a keyword block

Possible cause:

- the PDF uses a layout that breaks reading order, especially in two-column pages
- the keyword block may be positioned beside the abstract instead of after it
- the keyword list may be vertical rather than comma-delimited

What to check:

- inspect the first extracted PDF page manually
- check whether the keyword label appears as `Keywords`, `Key words`, or `Index Terms`
- check whether the keyword items are on separate lines

Suggested fix:

- rerun after improving the extraction rules for that layout family
- prefer conservative rule additions that target explicit keyword labels
- avoid broad heuristics that may swallow body text

Observed outcome:

- targeted support for line-based keyword blocks improved recall for two-column papers with vertical keyword lists

# Local W-2 PDF Extraction Service

This is a local-only helper service that extracts easy-to-find fields from W-2 PDFs and returns JSON for the browser UI to auto-fill.

## Setup (uv)

1. Sync dependencies (creates `.venv`):

```bash
cd service
uv sync
```

## Run

```bash
cd service
uv run uvicorn app:app --reload --port 8000
```

## Endpoint

`POST /extract`

- `multipart/form-data` with file field name `file`.
- Returns JSON with `fields` and a `method` used for extraction.

## Notes

- This is best-effort extraction. Always verify fields before generating the W-2 file.
- If you only have text-based PDFs, pdfplumber/PyMuPDF is usually sufficient without OCR.

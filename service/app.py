from __future__ import annotations

import io
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _extract_pages_pymupdf(data: bytes) -> Optional[List[str]]:
    try:
        import fitz  # PyMuPDF
    except Exception:
        return None

    text_parts: List[str] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            text_parts.append(page.get_text("text"))
    return text_parts if any(part.strip() for part in text_parts) else None


def _extract_pages_pdfplumber(data: bytes) -> Optional[List[str]]:
    try:
        import pdfplumber
    except Exception:
        return None

    text_parts: List[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return text_parts if any(part.strip() for part in text_parts) else None




def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _first_match(pattern: str, text: str) -> Optional[str]:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _extract_name(text: str) -> Dict[str, Optional[str]]:
    # Very heuristic: look for "Employee's name" then take following words
    name = _first_match(r"Employee.?s name\s+([^\n]+)", text)
    if not name:
        name = text.strip()
    if not name:
        return {"first": None, "middle": None, "last": None}

    parts = re.split(r"\s+", name.strip())
    if len(parts) == 1:
        return {"first": parts[0], "middle": None, "last": None}
    if len(parts) == 2:
        return {"first": parts[0], "middle": None, "last": parts[1]}

    return {"first": parts[0], "middle": " ".join(parts[1:-1]), "last": parts[-1]}


def _extract_money(label: str, text: str) -> Optional[str]:
    # Find a number near the label. Accepts commas and decimals.
    pattern = rf"{re.escape(label)}\s*\$?([0-9,]+(?:\.[0-9]{{2}})?)"
    return _first_match(pattern, text)


def _extract_employer_info(text: str) -> Dict[str, Optional[str]]:
    raw_name_line = _first_match(
        r"Employer.?s name.*?\n([A-Z0-9 &.,'\-]+)", text
    )
    employer_name = None
    if raw_name_line:
        employer_name = re.sub(r"\s+\d[\d,]*\.\d{2}.*$", "", raw_name_line).strip()
    employer_ein = _first_match(
        r"Employer identification number.*?\n(\d{2}-\d{7}|\d{9})", text
    )
    return {
        "employer_name": employer_name,
        "employer_ein": employer_ein,
    }


def _split_employee_sections(text: str) -> List[str]:
    markers = list(
        re.finditer(r"Employee'?s name|Employee'?s SSN", text, re.IGNORECASE)
    )
    if not markers:
        return [text]

    sections: List[str] = []
    for idx, marker in enumerate(markers):
        start = marker.start()
        end = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)
        sections.append(text[start:end])
    return sections


def _extract_state_wages_and_tax(text: str) -> Dict[str, Optional[str]]:
    match = re.search(
        r"16 State wages.*?17 State income tax.*?\n([^\n]+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return {"state_wages": None, "state_withheld": None}

    line = match.group(1)
    nums = re.findall(r"\d[\d,]*\.\d{2}", line)
    if len(nums) >= 2:
        return {"state_wages": nums[0], "state_withheld": nums[1]}
    return {"state_wages": None, "state_withheld": None}


def _extract_employee_from_page(text: str) -> Dict[str, Any]:
    text_flat = text
    norm = _normalize_text(text)

    ssn = _first_match(
        r"Employee.?s social security number\s+(\d{3}-\d{2}-\d{4})", text_flat
    )
    if not ssn:
        ssn = _first_match(r"\b(\d{9})\b", text_flat)

    name_line = _first_match(
        r"Employee.?s first name.*?\n([^\n]+)",
        text_flat,
    )
    if name_line:
        name_line = re.sub(r"\s+0\.00.*$", "", name_line).strip()
        name = _extract_name(name_line)
    else:
        name = _extract_name(text_flat)

    wages = None
    wages_line = _first_match(
        r"Employer identification number.*?\n([^\n]+)",
        text_flat,
    )
    if wages_line:
        decimals = re.findall(r"\d[\d,]*\.\d{2}", wages_line)
        if decimals:
            wages = decimals[0]

    state_values = _extract_state_wages_and_tax(text_flat)
    mapfml = _first_match(r"MAPFML:\s*([0-9,]+(?:\.[0-9]{2})?)", text_flat)

    return {
        "ssn": ssn,
        "first": name.get("first"),
        "middle": name.get("middle"),
        "last": name.get("last"),
        "wages": wages or _extract_money("Wages, tips, other comp", norm),
        "state_wages": state_values.get("state_wages"),
        "state_withheld": state_values.get("state_withheld"),
        "mapfml": mapfml,
    }


def _extract_employees_from_pages(pages: List[str]) -> List[Dict[str, Any]]:
    employees: List[Dict[str, Any]] = []
    w2_pages = [
        page_text
        for page_text in pages
        if "W-2" in page_text and "Employee" in page_text
    ]

    target_pages = w2_pages if w2_pages else pages

    for page_text in target_pages:
        if not page_text.strip():
            continue
        if page_text in w2_pages:
            employee = _extract_employee_from_page(page_text)
            if any(employee.values()):
                employees.append(employee)
            continue

        for section in _split_employee_sections(page_text):
            employee = _extract_employee_from_page(section)
            if any(employee.values()):
                employees.append(employee)

    # De-dupe by SSN if possible
    seen = set()
    unique: List[Dict[str, Any]] = []
    for emp in employees:
        key = emp.get("ssn") or f"{emp.get('first')}-{emp.get('last')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(emp)

    return unique




@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    data = await file.read()

    pages = _extract_pages_pdfplumber(data)
    method = "pdfplumber"
    if not pages:
        pages = _extract_pages_pymupdf(data)
        method = "pymupdf"
    if not pages:
        return JSONResponse(
            status_code=422,
            content={
                "error": "Unable to extract text. Install pdfplumber or PyMuPDF.",
            },
        )

    all_text = "\n".join(pages)
    employer = _extract_employer_info(all_text)
    employees = _extract_employees_from_pages(pages)

    result = {
        "method": method,
        "fields": {
            **employer,
            "employees": employees,
        },
        "warnings": [
            "Extraction is best-effort. Please verify all fields before generating the W-2 file."
        ],
    }
    return result

#!/usr/bin/env python3
"""Generate Obsidian paper notes from Better BibLaTeX exports."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pdfplumber = None

try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    PdfReader = None


SUMMARY_FROM_ABSTRACT = "generated_from_abstract"
SUMMARY_FROM_PDF = "generated_from_pdf"
SUMMARY_MANUAL_REVIEW = "needs_manual_review"
MAX_PDF_KEYWORDS = 5
PURPOSE_VERB_PATTERN = (
    r"(?:present(?:ed|s|ing)?|review(?:ed|s|ing)?|investigat(?:e|ed|es|ing)|"
    r"examin(?:e|ed|es|ing)|evaluat(?:e|ed|es|ing)|assess(?:ed|es|ing)?|"
    r"propos(?:e|ed|es|ing)|describ(?:e|ed|es|ing)|design(?:ed|s|ing)?|"
    r"test(?:ed|s|ing)?|trac(?:e|ed|es|ing)|quantif(?:y|ied|ies|ying))"
)


@dataclass
class BibEntry:
    entry_type: str
    citekey: str
    raw_fields: Dict[str, List[str]] = field(default_factory=dict)

    def get_first(self, name: str, default: str = "") -> str:
        values = self.raw_fields.get(name.lower(), [])
        if not values:
            return default
        return values[0]

    def get_all(self, name: str) -> List[str]:
        return self.raw_fields.get(name.lower(), [])


@dataclass
class ProcessResult:
    citekey: str
    title: str
    has_abstract: bool
    has_pdf: bool
    pdf_path: str
    summary_status: str
    note_created: bool
    error_message: str = ""


@dataclass
class SkippedEntry:
    citekey: str
    title: str
    error_message: str


class BibLatexParseError(ValueError):
    """Raised when the BibLaTeX parser encounters a fatal error."""


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Obsidian paper notes from Better BibLaTeX exports."
    )
    parser.add_argument("--bib", required=True, help="Input Better BibLaTeX .bib file")
    parser.add_argument("--output", required=True, help="Output directory for Markdown notes")
    parser.add_argument(
        "--zotero-storage",
        help="Optional Zotero storage root used to resolve relative attachment paths",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing Markdown notes if they already exist",
    )
    parser.add_argument(
        "--pdf-pages",
        type=int,
        default=3,
        help="Number of leading PDF pages to inspect when extracting source text (default: 3)",
    )
    parser.add_argument(
        "--errors-log",
        help="Optional custom path for the plain-text error log file",
    )
    return parser.parse_args(argv)


def parse_biblatex(path: Path) -> Tuple[List[BibEntry], List[SkippedEntry]]:
    text = path.read_text(encoding="utf-8")
    entries: List[BibEntry] = []
    skipped: List[SkippedEntry] = []
    index = 0
    length = len(text)

    while index < length:
        at_pos = text.find("@", index)
        if at_pos == -1:
            break
        index = at_pos + 1

        entry_type_match = re.match(r"([A-Za-z]+)", text[index:])
        if not entry_type_match:
            index = at_pos + 1
            continue
        entry_type = entry_type_match.group(1)
        index += len(entry_type)

        while index < length and text[index].isspace():
            index += 1
        if index >= length or text[index] not in "{(":
            skipped.append(
                SkippedEntry(
                    citekey="",
                    title="",
                    error_message=f"Malformed entry near character {at_pos}: missing opening brace",
                )
            )
            continue

        opening = text[index]
        closing = "}" if opening == "{" else ")"
        body_start = index + 1
        body_end = find_matching_delimiter(text, index, opening, closing)
        if body_end is None:
            skipped.append(
                SkippedEntry(
                    citekey="",
                    title="",
                    error_message=f"Malformed entry near character {at_pos}: unmatched delimiter",
                )
            )
            break

        raw_body = text[body_start:body_end]
        index = body_end + 1

        try:
            citekey, fields_text = split_entry_header(raw_body)
            fields = parse_entry_fields(fields_text)
            entries.append(BibEntry(entry_type=entry_type.lower(), citekey=citekey, raw_fields=fields))
        except BibLatexParseError as exc:
            skipped.append(
                SkippedEntry(
                    citekey=extract_partial_citekey(raw_body),
                    title="",
                    error_message=str(exc),
                )
            )

    return entries, skipped


def find_matching_delimiter(text: str, start: int, opening: str, closing: str) -> Optional[int]:
    depth = 0
    escaped = False

    for idx in range(start, len(text)):
        char = text[idx]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return idx
    return None


def split_entry_header(raw_body: str) -> Tuple[str, str]:
    in_quote = False
    depth = 0
    escaped = False

    for idx, char in enumerate(raw_body):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"' and depth == 0:
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            citekey = raw_body[:idx].strip()
            if not citekey:
                raise BibLatexParseError("Encountered entry without a citekey")
            return citekey, raw_body[idx + 1 :]

    raise BibLatexParseError("Encountered entry without a field list")


def extract_partial_citekey(raw_body: str) -> str:
    return raw_body.split(",", 1)[0].strip()


def parse_entry_fields(fields_text: str) -> Dict[str, List[str]]:
    fields: Dict[str, List[str]] = {}
    index = 0
    length = len(fields_text)

    while index < length:
        while index < length and fields_text[index] in " \t\r\n,":
            index += 1
        if index >= length:
            break

        name_match = re.match(r"([A-Za-z0-9_:-]+)", fields_text[index:])
        if not name_match:
            raise BibLatexParseError(f"Could not parse field name near: {fields_text[index:index + 40]!r}")
        field_name = name_match.group(1).lower()
        index += len(field_name)

        while index < length and fields_text[index].isspace():
            index += 1
        if index >= length or fields_text[index] != "=":
            raise BibLatexParseError(f"Field '{field_name}' is missing '='")
        index += 1

        while index < length and fields_text[index].isspace():
            index += 1
        if index >= length:
            raise BibLatexParseError(f"Field '{field_name}' has no value")

        value, index = parse_field_value(fields_text, index)
        fields.setdefault(field_name, []).append(value.strip())

        while index < length and fields_text[index].isspace():
            index += 1
        if index < length and fields_text[index] == ",":
            index += 1

    return fields


def parse_field_value(text: str, start: int) -> Tuple[str, int]:
    char = text[start]
    if char == "{":
        return parse_braced_value(text, start)
    if char == '"':
        return parse_quoted_value(text, start)
    end = start
    while end < len(text) and text[end] not in ",\r\n":
        end += 1
    return text[start:end].strip(), end


def parse_braced_value(text: str, start: int) -> Tuple[str, int]:
    depth = 0
    pieces: List[str] = []
    escaped = False

    for idx in range(start, len(text)):
        char = text[idx]
        if escaped:
            pieces.append(char)
            escaped = False
            continue
        if char == "\\":
            pieces.append(char)
            escaped = True
            continue
        if char == "{":
            depth += 1
            if depth > 1:
                pieces.append(char)
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return "".join(pieces), idx + 1
            pieces.append(char)
            continue
        pieces.append(char)

    raise BibLatexParseError("Unterminated braced value")


def parse_quoted_value(text: str, start: int) -> Tuple[str, int]:
    pieces: List[str] = []
    escaped = False

    for idx in range(start + 1, len(text)):
        char = text[idx]
        if escaped:
            pieces.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            pieces.append(char)
            continue
        if char == '"':
            return "".join(pieces), idx + 1
        pieces.append(char)

    raise BibLatexParseError("Unterminated quoted value")


def clean_bib_text(value: str) -> str:
    if not value:
        return ""

    replacements = {
        r"\&": "&",
        r"\%": "%",
        r"\_": "_",
        r"\#": "#",
        r"\$": "$",
        r"\textless": "<",
        r"\textgreater": ">",
        r"``": '"',
        r"''": '"',
        "---": "-",
        "--": "-",
        "~": " ",
    }

    cleaned = value.strip()
    for src, dst in replacements.items():
        cleaned = cleaned.replace(src, dst)

    cleaned = re.sub(r"\\url\{([^}]*)\}", r"\1", cleaned)
    cleaned = re.sub(r"\\doi\{([^}]*)\}", r"\1", cleaned)
    cleaned = re.sub(r"\\[A-Za-z]+\s*\{([^{}]*)\}", r"\1", cleaned)
    cleaned = re.sub(r"[{}]", "", cleaned)
    cleaned = re.sub(r"\\([#$%&_])", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def format_authors(author_field: str) -> str:
    if not author_field:
        return ""

    authors: List[str] = []
    for part in re.split(r"\s+and\s+", author_field):
        part = clean_bib_text(part)
        if not part:
            continue
        if "," in part:
            last, first = [segment.strip() for segment in part.split(",", 1)]
            authors.append(f"{first} {last}".strip())
        else:
            authors.append(part)
    return "; ".join(authors)


def extract_year(entry: BibEntry) -> str:
    for field_name in ("year", "date"):
        raw_value = clean_bib_text(entry.get_first(field_name))
        match = re.search(r"\b(19|20)\d{2}\b", raw_value)
        if match:
            return match.group(0)
    return ""


def extract_source(entry: BibEntry) -> str:
    for field_name in (
        "journaltitle",
        "journal",
        "booktitle",
        "publisher",
        "institution",
        "school",
        "howpublished",
    ):
        value = clean_bib_text(entry.get_first(field_name))
        if value:
            return value
    return ""


def split_keywords(raw_values: Iterable[str]) -> List[str]:
    keywords: List[str] = []
    for raw_value in raw_values:
        cleaned = clean_bib_text(raw_value)
        if not cleaned:
            continue
        for item in re.split(r"\s*[,;]\s*", cleaned):
            item = item.strip()
            if item and item not in keywords:
                keywords.append(item)
    return keywords


def extract_keywords_from_pdf_text(pdf_text: str) -> List[str]:
    normalized = normalize_pdf_text(pdf_text)
    if not normalized:
        return []

    line_based_keywords = extract_keywords_from_pdf_lines(pdf_text)
    if line_based_keywords:
        return line_based_keywords

    patterns = [
        r"(?is)\bkeywords?\b\s*[:\-—]?\s*(.{3,500}?)(?=(?:\n\s*\n)|(?:\b(?:introduction|background|methods?|materials|related work)\b)|(?:\b(?:\d+\.?\s+introduction|i\.\s*introduction)\b))",
        r"(?is)\bkey words\b\s*[:\-—]?\s*(.{3,500}?)(?=(?:\n\s*\n)|(?:\b(?:introduction|background|methods?|materials|related work)\b)|(?:\b(?:\d+\.?\s+introduction|i\.\s*introduction)\b))",
        r"(?is)\bindex terms\b\s*[:\-—]?\s*(.{3,500}?)(?=(?:\n\s*\n)|(?:\b(?:introduction|background|methods?|materials|related work)\b)|(?:\b(?:\d+\.?\s+introduction|i\.\s*introduction)\b))",
        r"(?is)\bkeywords?\b\s*(.{3,220}?)\s*(?=(?:\b(?:\d+\.?\s+introduction|i\.\s*introduction|introduction)\b)|(?:\n\s*\n))",
        r"(?is)\bindex terms\b\s*(.{3,220}?)\s*(?=(?:\b(?:\d+\.?\s+introduction|i\.\s*introduction|introduction)\b)|(?:\n\s*\n))",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            keyword_block = collapse_text_block(match.group(1))
            keyword_block = truncate_keyword_metadata(keyword_block)
            keyword_block = crop_keyword_block(keyword_block)
            keyword_block = strip_trailing_section_headers(keyword_block)
            raw_items = re.split(r"\s*,\s*|[;•·]\s*|\s+\|\s+", keyword_block)
            keywords: List[str] = []
            for item in raw_items:
                cleaned = normalize_keyword_item(item)
                if cleaned and not is_keyword_metadata_fragment(cleaned) and cleaned not in keywords:
                    keywords.append(cleaned)
                if len(keywords) >= MAX_PDF_KEYWORDS:
                    break
            if keywords:
                return keywords[:MAX_PDF_KEYWORDS]
    return []


def extract_keywords_from_pdf_lines(pdf_text: str) -> List[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in pdf_text.replace("\r", "\n").split("\n")]
    header_patterns = [
        re.compile(r"^(keywords?|key words|index terms)\s*[:\-—]?\s*(.*)$", re.I),
    ]

    for index, line in enumerate(lines):
        for pattern in header_patterns:
            match = pattern.match(line)
            if not match:
                continue

            inline_value = match.group(2).strip()
            if inline_value:
                inline_keywords = parse_keyword_list_block(inline_value)
                if inline_keywords:
                    return inline_keywords[:MAX_PDF_KEYWORDS]

            next_nonempty_index = next_nonempty_line_index(lines, index + 1)
            if next_nonempty_index is None:
                continue

            next_line = lines[next_nonempty_index].strip()
            if looks_like_section_header(next_line):
                continue

            if is_delimited_keyword_line(next_line):
                delimited_keywords = parse_keyword_list_block(next_line)
                if delimited_keywords:
                    return delimited_keywords[:MAX_PDF_KEYWORDS]
                continue

            line_list_keywords = extract_vertical_keyword_list(lines, next_nonempty_index)
            if line_list_keywords:
                return line_list_keywords[:MAX_PDF_KEYWORDS]

    return []


def is_probable_keyword_line(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return False
    if looks_like_section_header(normalized):
        return False
    if "." in normalized:
        return False
    if normalized.count(",") + normalized.count(";") < 1:
        return False
    if len(normalized) > 220:
        return False
    return True


def is_delimited_keyword_line(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return False
    if looks_like_section_header(normalized):
        return False
    if normalized.count(",") + normalized.count(";") < 1:
        return False
    if "." in normalized:
        return False
    return True


def parse_keyword_list_block(text: str) -> List[str]:
    keyword_block = collapse_text_block(text)
    keyword_block = truncate_keyword_metadata(keyword_block)
    keyword_block = crop_keyword_block(keyword_block)
    keyword_block = strip_trailing_section_headers(keyword_block)
    raw_items = re.split(r"\s*,\s*|[;•·]\s*|\s+\|\s+", keyword_block)
    keywords: List[str] = []
    for item in raw_items:
        cleaned = normalize_keyword_item(item)
        if cleaned and not is_keyword_metadata_fragment(cleaned) and cleaned not in keywords:
            keywords.append(cleaned)
        if len(keywords) >= MAX_PDF_KEYWORDS:
            break
    return keywords


def next_nonempty_line_index(lines: Sequence[str], start: int) -> Optional[int]:
    for index in range(start, len(lines)):
        if lines[index].strip():
            return index
    return None


def extract_vertical_keyword_list(lines: Sequence[str], start_index: int) -> List[str]:
    keywords: List[str] = []
    current_item = ""

    for index in range(start_index, len(lines)):
        line = re.sub(r"\s+", " ", lines[index]).strip()
        if not line:
            break
        if looks_like_section_header(line):
            break
        if is_keyword_metadata_fragment(line):
            break
        if looks_like_body_paragraph(line):
            break

        cleaned_line = normalize_keyword_item(strip_trailing_section_headers(line))
        if not cleaned_line:
            break

        if current_item:
            if current_item.endswith(("-", "–", "—")):
                current_item = current_item.rstrip("-–—").rstrip() + cleaned_line
                continue
            if current_item.endswith(","):
                current_item = f"{current_item.rstrip(',').rstrip()} {cleaned_line}".strip()
                continue

            if current_item not in keywords:
                keywords.append(current_item)
                if len(keywords) >= MAX_PDF_KEYWORDS:
                    return keywords[:MAX_PDF_KEYWORDS]
            current_item = cleaned_line
            continue

        current_item = cleaned_line

    if current_item and current_item not in keywords:
        keywords.append(current_item)

    return keywords[:MAX_PDF_KEYWORDS]


def looks_like_body_paragraph(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return False
    if len(normalized) > 120:
        return True
    if re.search(r"[.!?]", normalized):
        return True
    if re.match(r"^[A-Z][a-z]+(?:\s+[a-z][A-Za-z-]*){3,}", normalized):
        return True
    return False


def looks_like_section_header(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    lowered = normalized.lower()
    header_patterns = [
        r"^\d+\.?\s+introduction\b",
        r"^i\.\s*introduction\b",
        r"^introduction\b",
        r"^background\b",
        r"^materials and methods\b",
        r"^methods\b",
        r"^related work\b",
        r"^references\b",
        r"^corresponding author\b",
        r"^article\b$",
    ]
    return any(re.search(pattern, lowered) for pattern in header_patterns)


def normalize_keyword_item(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .;,:-")
    cleaned = re.sub(r"^(?:and|or)\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+(?:and|or)$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d+\s*$", "", cleaned).strip(" .;,:-")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned


def truncate_keyword_metadata(text: str) -> str:
    stop_patterns = [
        r"\(\s*this article was\b",
        r"\bthis article was submitted\b",
        r"\bthis article was accepted\b",
        r"\breceived\b",
        r"\baccepted\b",
        r"\bpublished online\b",
        r"\bcorrespondence\b",
        r"\bedited by\b",
        r"\breviewed by\b",
        r"\bcitation\b",
        r"\bcopyright\b",
        r"\bdoi\b",
        r"\b1\.?\s+introduction\b",
        r"\bi\.\s*introduction\b",
        r"\bintroduction\b",
        r"\bbackground\b",
        r"\bmaterials and methods\b",
        r"\bmethods\b",
        r"\brelated work\b",
    ]
    lowered = text.lower()
    cut_points = []
    for pattern in stop_patterns:
        match = re.search(pattern, lowered, re.I)
        if match:
            cut_points.append(match.start())
    if cut_points:
        text = text[: min(cut_points)]
    return text.strip(" .;,:-")


def crop_keyword_block(text: str) -> str:
    text = text.strip()
    if not text:
        return ""

    # Prefer the common comma-delimited keyword line and stop before trailing metadata.
    comma_parts = [part.strip(" .;,:-") for part in re.split(r"\s*,\s*", text) if part.strip(" .;,:-")]
    if len(comma_parts) >= 2:
        kept: List[str] = []
        for part in comma_parts:
            if is_keyword_metadata_fragment(part):
                break
            normalized_part = normalize_keyword_item(strip_trailing_section_headers(part))
            if not normalized_part or is_keyword_metadata_fragment(normalized_part):
                break
            kept.append(normalized_part)
            if len(kept) == MAX_PDF_KEYWORDS:
                break
        if kept:
            return ", ".join(kept)

    return text


def strip_trailing_section_headers(text: str) -> str:
    patterns = [
        r"\b\d+\.?\s+introduction\b.*$",
        r"\bi\.\s*introduction\b.*$",
        r"\bintroduction\b.*$",
        r"\bbackground\b.*$",
        r"\bmaterials and methods\b.*$",
        r"\bmethods\b.*$",
        r"\brelated work\b.*$",
    ]
    stripped = text
    for pattern in patterns:
        stripped = re.sub(pattern, "", stripped, flags=re.I).strip(" .;,:-")
    return stripped


def is_keyword_metadata_fragment(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    lowered = normalized.lower()
    if not normalized:
        return True
    if re.fullmatch(r"\d{4}", normalized):
        return True
    if len(normalized) > 80:
        return True
    metadata_patterns = [
        r"^\(?this article was\b",
        r"^\(?and was accepted\b",
        r"^\(?and was submitted\b",
        r"^\(?accepted\b",
        r"^\(?received\b",
        r"^\(?published\b",
        r"^\(?doi\b",
        r"^\(?copyright\b",
        r"^\(?correspondence\b",
        r"^\(?edited by\b",
        r"^\(?reviewed by\b",
        r"^\(?this article\b",
        r"^\(?author\b",
        r"^\(?email\b",
        r"^\d+\.?\s*introduction\b",
        r"^introduction\b",
        r"^background\b",
        r"^related work\b",
    ]
    if any(re.search(pattern, lowered) for pattern in metadata_patterns):
        return True
    if normalized.count("(") != normalized.count(")"):
        return True
    return False


def split_tags(entry: BibEntry) -> List[str]:
    tag_fields = entry.get_all("tags") + entry.get_all("tag")
    return split_keywords(tag_fields)


def parse_file_attachments(file_value: str) -> List[Tuple[str, str, str]]:
    attachments: List[Tuple[str, str, str]] = []
    if not file_value:
        return attachments

    for raw_attachment in file_value.split(";"):
        raw_attachment = raw_attachment.strip()
        if not raw_attachment:
            continue
        before_mime, mime = raw_attachment.rsplit(":", 1) if ":" in raw_attachment else (raw_attachment, "")
        label, path = before_mime.split(":", 1) if ":" in before_mime else ("", before_mime)
        attachments.append((clean_bib_text(label), clean_bib_text(path), clean_bib_text(mime)))
    return attachments


def resolve_pdf_paths(entry: BibEntry, zotero_storage: Optional[Path]) -> List[Path]:
    resolved: List[Path] = []
    for file_value in entry.get_all("file"):
        for _, raw_path, mime in parse_file_attachments(file_value):
            path = Path(raw_path)
            looks_like_pdf = raw_path.lower().endswith(".pdf") or mime.lower() == "application/pdf"
            if not looks_like_pdf:
                continue

            candidates: List[Path] = []
            if path.is_absolute():
                candidates.append(path)
            else:
                candidates.append(path)
                if zotero_storage is not None:
                    candidates.append(zotero_storage / path)

            for candidate in candidates:
                if candidate.exists():
                    if candidate not in resolved:
                        resolved.append(candidate)
                    break
    return resolved


def extract_pdf_text(pdf_path: Path, pages_to_read: int = 3) -> Tuple[str, str]:
    if PdfReader is not None:
        reader = PdfReader(str(pdf_path))
        text_chunks = []
        for page in reader.pages[:pages_to_read]:
            extracted = page.extract_text() or ""
            if extracted.strip():
                text_chunks.append(extracted)
        combined = "\n\n".join(text_chunks).strip()
        if combined:
            return combined, "pypdf"

    if pdfplumber is not None:
        text_chunks: List[str] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[:pages_to_read]:
                extracted = page.extract_text() or ""
                if extracted.strip():
                    text_chunks.append(extracted)
        combined = "\n\n".join(text_chunks).strip()
        if combined:
            return combined, "pdfplumber"

    raise RuntimeError(
        "No PDF extraction backend available. Install pdfplumber or pypdf, "
        "or run the script with a Python environment that provides one."
    )


def normalize_pdf_text(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_abstract_like_text(pdf_text: str) -> str:
    normalized = normalize_pdf_text(pdf_text)
    if not normalized:
        return ""

    abstract_match = re.search(
        r"(?is)\babstract\b\s*[:\-—]?\s*(.{80,2500}?)(?=(?:\b(?:keywords?|key words|index terms)\b\s*[:\-—]?)|(?:\b(?:introduction|i\.\s*introduction|1\.?\s*introduction)\b))",
        normalized,
    )
    if abstract_match:
        return collapse_text_block(abstract_match.group(1))

    abstract_fallback = re.search(r"(?is)\babstract\b\s*[:\-—]?\s*(.{80,1800})", normalized)
    if abstract_fallback:
        return collapse_text_block(abstract_fallback.group(1))

    intro_match = re.search(
        r"(?is)\b(?:introduction|i\.\s*introduction|1\.?\s*introduction)\b\s*[:\-—]?\s*(.{120,1800}?)(?=(?:\b(?:2\.?\s+[A-Z]|ii\.\s+[A-Z]|methods?|materials|background|related work)\b))",
        normalized,
    )
    if intro_match:
        return collapse_text_block(intro_match.group(1))

    for block in split_text_blocks(normalized):
        cleaned = collapse_text_block(block)
        if is_meaningful_text_block(cleaned):
            return cleaned
    return ""


def split_text_blocks(text: str) -> List[str]:
    blocks = re.split(r"\n\s*\n", text)
    return [block.strip() for block in blocks if block.strip()]


def collapse_text_block(text: str) -> str:
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_meaningful_text_block(text: str) -> bool:
    if len(text) < 80:
        return False
    alpha_count = len(re.findall(r"[A-Za-z]", text))
    if alpha_count < 40:
        return False
    noisy_prefixes = ("doi", "copyright", "received", "accepted", "frontiers", "license")
    lowercase = text.lower()
    return not lowercase.startswith(noisy_prefixes)


def generate_summary(source_text: str, fallback_placeholder: str = "TODO: generate one-line summary") -> str:
    cleaned = collapse_text_block(clean_bib_text(source_text))
    if not cleaned:
        return fallback_placeholder

    abstract_marker = re.search(r"(?is)\babstract\b\s*[:\-—]?\s*(.*)", cleaned)
    if abstract_marker:
        cleaned = abstract_marker.group(1).strip()

    clause_match = re.search(
        r"(?is)\b("
        r"this (?:paper|study|work|review|chapter)\b.*?(?:[.!?]|$)|"
        rf"we\s+{PURPOSE_VERB_PATTERN}\b.*?(?:[.!?]|$)|"
        r"the (?:purpose|goal) of this (?:paper|study|work|review|chapter)\b.*?(?:[.!?]|$)"
        r")",
        cleaned,
    )
    if clause_match:
        candidate = clause_match.group(1).strip(" -")
        words = candidate.split()
        if len(words) > 80:
            candidate = " ".join(words[:80]).rstrip(" ,;:") + "..."
        return candidate.strip()

    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\[])|(?<=[.!?])\s+(?=\"?[A-Z])", cleaned)
    candidate = ""
    preferred_patterns = (
        re.compile(r"^(this (paper|study|work|review|chapter)\b)", re.IGNORECASE),
        re.compile(rf"^we\s+{PURPOSE_VERB_PATTERN}\b", re.IGNORECASE),
        re.compile(r"\b(the purpose|the goal|this review|this study|this paper|this work)\b", re.IGNORECASE),
    )

    for sentence in sentences:
        normalized = sentence.strip(" -")
        if len(normalized.split()) < 8:
            continue
        if len(normalized) > 500:
            continue
        if any(pattern.search(normalized) for pattern in preferred_patterns):
            candidate = normalized
            break
        if not candidate:
            candidate = normalized

    if not candidate:
        candidate = cleaned

    words = candidate.split()
    if len(words) > 80:
        candidate = " ".join(words[:80]).rstrip(" ,;:") + "..."

    return candidate.strip()


def yaml_quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def yaml_list(items: Sequence[str]) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(f'"{yaml_quote(item)}"' for item in items) + "]"


def yaml_scalar(text: str) -> str:
    return f'"{yaml_quote(text)}"'


def format_obsidian_keyword_links(keywords: Sequence[str]) -> str:
    cleaned_keywords = [re.sub(r"\s+", " ", keyword).strip() for keyword in keywords[:3]]
    cleaned_keywords = [keyword for keyword in cleaned_keywords if keyword]
    if not cleaned_keywords:
        return ""
    return ", ".join(f"[[{keyword}]]" for keyword in cleaned_keywords)


def path_to_file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def build_folder_link(pdf_paths: Sequence[Path]) -> str:
    if not pdf_paths:
        return ""
    folder_path = pdf_paths[0].resolve().parent
    return f"[Open Folder]({path_to_file_uri(folder_path)})"


def merge_annotation_text(raw_values: Iterable[str]) -> str:
    parts: List[str] = []
    for raw_value in raw_values:
        cleaned = clean_bib_text(raw_value)
        if cleaned:
            parts.append(cleaned)
    return "\n\n".join(parts)


def render_markdown(
    *,
    citekey: str,
    title: str,
    authors: str,
    year: str,
    source: str,
    doi: str,
    url: str,
    keywords: Sequence[str],
    tags: Sequence[str],
    file_dir: str,
    summary_status: str,
    summary: str,
    abstract_or_text: str,
    notes_annotations: str,
) -> str:
    keyword_links = format_obsidian_keyword_links(keywords)
    notes_block = notes_annotations.strip() if notes_annotations.strip() else ""
    return f"""---
type: paper
citekey: "{yaml_quote(citekey)}"
title: "{yaml_quote(title)}"
authors: "{yaml_quote(authors)}"
year: "{yaml_quote(year)}"
source: "{yaml_quote(source)}"
doi: "{yaml_quote(doi)}"
url: "{yaml_quote(url)}"
keywords: {yaml_list(keywords[:MAX_PDF_KEYWORDS])}
tags: {yaml_list(tags)}
file_dir: {yaml_scalar(file_dir)}
summary_status: "{yaml_quote(summary_status)}"
---

# {title}

**One-line summary:**  
{summary}

## Basic Info

**Authors:** {authors}  
**Year:** {year}  
**Source:** {source}  
**DOI:** {doi}  
**URL:** {url}  

## Abstract / Extracted Source Text

{abstract_or_text}

### Core Concepts (paper keywords)

{keyword_links}

## Notes/Annotations

{notes_block}

## Useful excerpts

### Excerpt 1

> 

**My understanding:**  

**Possible thesis location:**  
[[ ]]

**Keywords:**  

### Excerpt 2

> 

**My understanding:**  

**Possible thesis location:**  
[[ ]]

**Keywords:**  

### Excerpt 3

> 

**My understanding:**  

**Possible thesis location:**  
[[ ]]

**Keywords:**  

### Excerpt 4

> 

**My understanding:**  

**Possible thesis location:**  
[[ ]]

**Keywords:**  

### Excerpt 5

> 

**My understanding:**  

**Possible thesis location:**  
[[ ]]

**Keywords:**  

## Limitations / Cautions
"""


def write_markdown(path: Path, content: str, overwrite: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def write_processing_report(path: Path, rows: Sequence[ProcessResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "citekey",
                "title",
                "has_abstract",
                "has_pdf",
                "pdf_path",
                "summary_status",
                "note_created",
                "error_message",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.citekey,
                    row.title,
                    row.has_abstract,
                    row.has_pdf,
                    row.pdf_path,
                    row.summary_status,
                    row.note_created,
                    row.error_message,
                ]
            )


def write_skipped_entries(path: Path, rows: Sequence[SkippedEntry]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["citekey", "title", "error_message"])
        for row in rows:
            writer.writerow([row.citekey, row.title, row.error_message])


def write_error_log(path: Path, rows: Sequence[ProcessResult], skipped: Sequence[SkippedEntry]) -> None:
    lines: List[str] = []
    for row in rows:
        if row.error_message:
            lines.append(f"[{row.citekey or 'unknown'}] {row.error_message}")
    for row in skipped:
        lines.append(f"[{row.citekey or 'unknown'}] {row.error_message}")
    path.write_text("\n".join(lines).strip() + ("\n" if lines else ""), encoding="utf-8")


def default_report_dir(output_dir: Path) -> Path:
    return output_dir.parent / f"{output_dir.name}_reports"


def entry_type_prefix(entry_type: str) -> str:
    normalized = (entry_type or "").strip().lower()
    if normalized in {"book", "inbook", "incollection", "inreference", "mvbook", "bookinbook", "suppbook"}:
        return "[B]"
    if normalized in {"online", "doc", "docs"}:
        return "[U]"
    if normalized in {
        "article",
        "inproceedings",
        "proceedings",
        "thesis",
        "report",
        "misc",
        "unpublished",
        "manual",
        "techreport",
        "phdthesis",
        "mastersthesis",
        "conference",
        "preprint",
    }:
        return "[A]"
    return "[U]"


def note_filename(entry: BibEntry) -> str:
    return f"{entry_type_prefix(entry.entry_type)} {entry.citekey}.md"


def build_note_content(
    entry: BibEntry,
    zotero_storage: Optional[Path],
    pdf_pages: int,
) -> Tuple[str, List[str], str, str]:
    title = clean_bib_text(entry.get_first("title")) or entry.citekey
    authors = format_authors(entry.get_first("author") or entry.get_first("editor"))
    year = extract_year(entry)
    source = extract_source(entry)
    doi = clean_bib_text(entry.get_first("doi"))
    url = clean_bib_text(entry.get_first("url"))
    abstract_text = clean_bib_text(entry.get_first("abstract"))
    keywords = split_keywords(entry.get_all("keywords"))
    tags = split_tags(entry)
    notes_annotations = merge_annotation_text(entry.get_all("annotation"))
    pdf_paths = resolve_pdf_paths(entry, zotero_storage)
    attachment_paths = [str(path) for path in pdf_paths]
    folder_link = build_folder_link(pdf_paths)

    if abstract_text:
        if not keywords and pdf_paths:
            try:
                pdf_text, _ = extract_pdf_text(pdf_paths[0], pages_to_read=pdf_pages)
                keywords = extract_keywords_from_pdf_text(pdf_text)
            except Exception:
                keywords = []
        summary = generate_summary(abstract_text)
        summary_status = SUMMARY_FROM_ABSTRACT
        source_text = abstract_text
        return title, attachment_paths, summary_status, render_markdown(
            citekey=entry.citekey,
            title=title,
            authors=authors,
            year=year,
            source=source,
            doi=doi,
            url=url,
            keywords=keywords,
            tags=tags,
            file_dir=folder_link,
            summary_status=summary_status,
            summary=summary,
            abstract_or_text=source_text,
            notes_annotations=notes_annotations,
        )

    if pdf_paths:
        pdf_text, backend = extract_pdf_text(pdf_paths[0], pages_to_read=pdf_pages)
        if not keywords:
            keywords = extract_keywords_from_pdf_text(pdf_text)
        extracted_text = extract_abstract_like_text(pdf_text)
        if not extracted_text:
            extracted_text = collapse_text_block(pdf_text)[:2500]
        summary = generate_summary(extracted_text)
        if summary == "TODO: generate one-line summary":
            summary_status = SUMMARY_MANUAL_REVIEW
        else:
            summary_status = SUMMARY_FROM_PDF
        note = render_markdown(
            citekey=entry.citekey,
            title=title,
            authors=authors,
            year=year,
            source=source,
            doi=doi,
            url=url,
            keywords=keywords,
            tags=tags,
            file_dir=folder_link,
            summary_status=summary_status,
            summary=summary,
            abstract_or_text=f"{extracted_text}\n\n_Extracted from PDF using {backend}._",
            notes_annotations=notes_annotations,
        )
        return title, attachment_paths, summary_status, note

    note = render_markdown(
        citekey=entry.citekey,
        title=title,
        authors=authors,
        year=year,
        source=source,
        doi=doi,
        url=url,
        keywords=keywords,
        tags=tags,
        file_dir=folder_link,
        summary_status=SUMMARY_MANUAL_REVIEW,
        summary="TODO: generate one-line summary",
        abstract_or_text="No abstract or PDF-derived source text was available.",
        notes_annotations=notes_annotations,
    )
    return title, attachment_paths, SUMMARY_MANUAL_REVIEW, note


def process_entries(
    entries: Sequence[BibEntry],
    output_dir: Path,
    zotero_storage: Optional[Path],
    overwrite: bool,
    pdf_pages: int,
) -> List[ProcessResult]:
    results: List[ProcessResult] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        title = clean_bib_text(entry.get_first("title")) or entry.citekey
        has_abstract = bool(clean_bib_text(entry.get_first("abstract")))
        pdf_paths = resolve_pdf_paths(entry, zotero_storage)
        has_pdf = bool(pdf_paths)
        pdf_path = str(pdf_paths[0]) if pdf_paths else ""

        try:
            _, attachment_paths, summary_status, note_content = build_note_content(
                entry=entry,
                zotero_storage=zotero_storage,
                pdf_pages=pdf_pages,
            )
            note_path = output_dir / note_filename(entry)
            note_created = write_markdown(note_path, note_content, overwrite=overwrite)
            results.append(
                ProcessResult(
                    citekey=entry.citekey,
                    title=title,
                    has_abstract=has_abstract,
                    has_pdf=has_pdf,
                    pdf_path=pdf_path or (attachment_paths[0] if attachment_paths else ""),
                    summary_status=summary_status,
                    note_created=note_created,
                )
            )
        except Exception as exc:  # pragma: no cover - runtime protection
            note_path = output_dir / note_filename(entry)
            minimal_note = render_markdown(
                citekey=entry.citekey,
                title=title,
                authors=format_authors(entry.get_first("author") or entry.get_first("editor")),
                year=extract_year(entry),
                source=extract_source(entry),
                doi=clean_bib_text(entry.get_first("doi")),
                url=clean_bib_text(entry.get_first("url")),
                keywords=split_keywords(entry.get_all("keywords")),
                tags=split_tags(entry),
                file_dir=build_folder_link(pdf_paths),
                summary_status=SUMMARY_MANUAL_REVIEW,
                summary="TODO: generate one-line summary",
                abstract_or_text="Automatic extraction failed. See processing_report.csv and errors.log.",
                notes_annotations=merge_annotation_text(entry.get_all("annotation")),
            )
            note_created = write_markdown(note_path, minimal_note, overwrite=overwrite)
            results.append(
                ProcessResult(
                    citekey=entry.citekey,
                    title=title,
                    has_abstract=has_abstract,
                    has_pdf=has_pdf,
                    pdf_path=pdf_path,
                    summary_status=SUMMARY_MANUAL_REVIEW,
                    note_created=note_created,
                    error_message=str(exc),
                )
            )

    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    bib_path = Path(args.bib).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    zotero_storage = Path(args.zotero_storage).expanduser().resolve() if args.zotero_storage else None
    report_dir = default_report_dir(output_dir)
    errors_log = Path(args.errors_log).expanduser().resolve() if args.errors_log else report_dir / "errors.log"
    report_dir.mkdir(parents=True, exist_ok=True)

    entries, skipped = parse_biblatex(bib_path)
    results = process_entries(
        entries=entries,
        output_dir=output_dir,
        zotero_storage=zotero_storage,
        overwrite=args.overwrite,
        pdf_pages=max(1, args.pdf_pages),
    )

    write_processing_report(report_dir / "processing_report.csv", results)
    write_skipped_entries(report_dir / "skipped_entries.csv", skipped)
    write_error_log(errors_log, results, skipped)

    print(f"Processed {len(results)} entries into {output_dir}")
    if skipped:
        print(f"Skipped {len(skipped)} malformed entries; see {report_dir / 'skipped_entries.csv'}")
    errored = [row for row in results if row.error_message]
    if errored:
        print(f"{len(errored)} entries require manual review due to extraction errors; see {errors_log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

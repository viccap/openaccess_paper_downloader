#!/usr/bin/env python3
"""Download open-access paper PDFs from a CSV using DOI values."""

from __future__ import annotations

import argparse
import contextlib
import csv
import html as html_module
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable


OPENALEX_WORKS_URL = "https://api.openalex.org/works"
USER_AGENT = "open-access-paper-downloader/1.0 (+https://openalex.org)"
CHUNK_SIZE = 1024 * 128
MAX_HTML_BYTES = 1024 * 1024 * 2
MAX_FOLLOW_UP_URLS = 25
MAX_URL_ATTEMPTS = 15
DOI_PATTERN = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
OPEN_ACCESS_TRUE_VALUES = {"1", "true", "yes", "y", "oa", "open", "openaccess"}
DEFAULT_OPENACCESS_VALUE = "not_provided"
DEFAULT_OUTPUT_DIR = "leuphana_papers"
DEFAULT_NAME_HINT_COLUMNS = [
    "title",
    "year",
    "journal",
    "author_names",
    "publisher",
    "subtype",
]
API_MAX_BODY_BYTES = 100 * 1024 * 1024
API_JOB_RETENTION_SECONDS = 15 * 60
API_POST_DOWNLOAD_RETENTION_SECONDS = 20 * 60
API_MAX_JOB_ROWS_DEFAULT = 50_000
API_ESTIMATED_PDF_SIZE_BYTES_DEFAULT = 2 * 1024 * 1024
API_MAX_ESTIMATED_JOB_BYTES_DEFAULT = 8 * 1024 * 1024 * 1024
API_ZIP_PART_MAX_FILES_DEFAULT = 200
API_ZIP_PART_MAX_BYTES_DEFAULT = 600 * 1024 * 1024


@dataclass
class PaperEntry:
    row_number: int
    short_id: str
    doi: str
    title: str
    name_hint: str
    openaccess: str


@dataclass
class DownloadRecord:
    status: str
    row_number: int
    short_id: str
    doi: str
    openaccess: str
    file_path: str
    openalex_id: str
    title: str
    source_url: str
    message: str


@dataclass
class JobConfig:
    timeout: int = 45
    email: str = ""
    overwrite: bool = False
    html_to_pdf_fallback: bool = True
    html_render_engine: str = "auto"


@dataclass
class JobState:
    job_id: str
    output_dir: Path
    total_requested: int
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    started_at: str = ""
    finished_at: str = ""
    processed: int = 0
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    report_path: str = ""
    zip_path: str = ""
    error: str = ""
    cancel_requested: bool = False
    cancel_requested_at: str = ""
    files_deleted: bool = False
    files_deleted_at: str = ""
    expires_at: str = ""
    records: list[DownloadRecord] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    cleanup_timer: threading.Timer | None = field(default=None, repr=False)


@dataclass
class DownloadSummary:
    downloaded: int
    failed: int
    skipped: int
    processed: int
    stopped_early: bool
    records: list[DownloadRecord]

class PdfLinkParser(HTMLParser):
    """Extract likely PDF links from HTML meta/link/anchor tags."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        tag_name = tag.lower()

        if tag_name == "meta":
            name_parts = [
                attrs_dict.get("name", ""),
                attrs_dict.get("property", ""),
                attrs_dict.get("itemprop", ""),
            ]
            meta_name = " ".join(name_parts).lower()
            content = attrs_dict.get("content", "").strip()
            if content and ("pdf" in meta_name or "citation_pdf_url" in meta_name):
                self.links.append(content)
            return

        if tag_name == "link":
            href = attrs_dict.get("href", "").strip()
            rel = attrs_dict.get("rel", "").lower()
            link_type = attrs_dict.get("type", "").lower()
            if href and ("pdf" in rel or "pdf" in link_type or ".pdf" in href.lower()):
                self.links.append(href)
            return

        if tag_name == "a":
            href = attrs_dict.get("href", "").strip()
            if href:
                self.links.append(href)


JOBS: dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()
API_DEFAULT_JOB_CONFIG = JobConfig()
API_OUTPUT_ROOT = Path("leuphana_papers").resolve()
API_JOB_RETENTION = API_JOB_RETENTION_SECONDS
API_POST_DOWNLOAD_RETENTION = API_POST_DOWNLOAD_RETENTION_SECONDS
API_MAX_JOB_ROWS = API_MAX_JOB_ROWS_DEFAULT
API_ESTIMATED_PDF_SIZE_BYTES = API_ESTIMATED_PDF_SIZE_BYTES_DEFAULT
API_MAX_ESTIMATED_JOB_BYTES = API_MAX_ESTIMATED_JOB_BYTES_DEFAULT
API_ZIP_PART_MAX_FILES = API_ZIP_PART_MAX_FILES_DEFAULT
API_ZIP_PART_MAX_BYTES = API_ZIP_PART_MAX_BYTES_DEFAULT


def env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read a CSV and download paper PDFs using DOI values."
        )
    )
    parser.add_argument(
        "--csv-file",
        default="speedboat_leuphana_sample.csv",
        help="Input CSV file path (default: speedboat_leuphana_sample.csv).",
    )
    parser.add_argument(
        "--csv-delimiter",
        default="auto",
        help=(
            "CSV delimiter: auto, ',', ';', '|', or '\\t' "
            "(default: auto-detect)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where PDFs and report are saved (default: leuphana_papers).",
    )
    parser.add_argument(
        "--openaccess-column",
        default="openaccess",
        help=(
            "Optional open-access column name. If present in CSV, rows flagged as "
            "non-open-access are skipped (default: openaccess)."
        ),
    )
    parser.add_argument(
        "--short-id-column",
        default="short_id",
        help="Column containing short IDs (default: short_id).",
    )
    parser.add_argument(
        "--doi-column",
        default="doi",
        help="Column containing DOI values (default: doi).",
    )
    parser.add_argument(
        "--title-column",
        default="title",
        help="Optional title column (default: title).",
    )
    parser.add_argument(
        "--name-columns",
        default="title,year,journal,author_names,publisher,subtype",
        help=(
            "Optional comma-separated columns used to build output filenames. "
            "Missing columns are ignored."
        ),
    )
    parser.add_argument(
        "--email",
        default="",
        help="Optional email for OpenAlex polite pool (recommended).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="HTTP timeout in seconds (default: 45).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files if names collide.",
    )
    parser.add_argument(
        "--disable-html-to-pdf-fallback",
        action="store_true",
        help=(
            "Disable HTML-to-PDF rendering when only an HTML landing page is found."
        ),
    )
    parser.add_argument(
        "--html-render-engine",
        choices=["auto", "weasyprint", "playwright"],
        default="auto",
        help=(
            "Engine for HTML-to-PDF fallback (default: auto). "
            "Auto tries weasyprint first, then playwright."
        ),
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run HTTP API server instead of one-off CLI download job.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface for --serve mode (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port for --serve mode (default: 8000).",
    )
    parser.add_argument(
        "--job-retention-seconds",
        type=int,
        default=env_int("DOI_JOB_RETENTION_SECONDS", API_JOB_RETENTION_SECONDS),
        help=(
            "How long to keep downloaded PDFs on the server after job completion "
            "before auto-deletion in API mode (default: 900)."
        ),
    )
    parser.add_argument(
        "--post-download-retention-seconds",
        type=int,
        default=env_int(
            "DOI_POST_DOWNLOAD_RETENTION_SECONDS",
            API_POST_DOWNLOAD_RETENTION_SECONDS,
        ),
        help=(
            "How long files remain after a ZIP part download to allow retries "
            "in API mode (default: 1200)."
        ),
    )
    parser.add_argument(
        "--max-job-rows",
        type=int,
        default=env_int("DOI_MAX_JOB_ROWS", API_MAX_JOB_ROWS_DEFAULT),
        help="Maximum rows accepted per API job (default: 50000).",
    )
    parser.add_argument(
        "--estimated-pdf-size-bytes",
        type=int,
        default=env_int(
            "DOI_ESTIMATED_PDF_SIZE_BYTES",
            API_ESTIMATED_PDF_SIZE_BYTES_DEFAULT,
        ),
        help=(
            "Estimated bytes per successful PDF used for capacity checks "
            "in API mode (default: 2097152)."
        ),
    )
    parser.add_argument(
        "--max-estimated-job-bytes",
        type=int,
        default=env_int(
            "DOI_MAX_ESTIMATED_JOB_BYTES",
            API_MAX_ESTIMATED_JOB_BYTES_DEFAULT,
        ),
        help=(
            "Maximum estimated bytes allowed per API job "
            "(default: 8589934592)."
        ),
    )
    parser.add_argument(
        "--zip-part-max-files",
        type=int,
        default=env_int("DOI_ZIP_PART_MAX_FILES", API_ZIP_PART_MAX_FILES_DEFAULT),
        help="Maximum PDF files per ZIP part (default: 200).",
    )
    parser.add_argument(
        "--zip-part-max-bytes",
        type=int,
        default=env_int("DOI_ZIP_PART_MAX_BYTES", API_ZIP_PART_MAX_BYTES_DEFAULT),
        help="Maximum uncompressed PDF bytes per ZIP part (default: 629145600).",
    )
    args = parser.parse_args()

    if args.timeout < 1:
        parser.error("--timeout must be >= 1")
    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535")
    if args.job_retention_seconds < 0:
        parser.error("--job-retention-seconds must be >= 0")
    if args.post_download_retention_seconds < 0:
        parser.error("--post-download-retention-seconds must be >= 0")
    if args.max_job_rows < 1:
        parser.error("--max-job-rows must be >= 1")
    if args.estimated_pdf_size_bytes < 1:
        parser.error("--estimated-pdf-size-bytes must be >= 1")
    if args.max_estimated_job_bytes < 1:
        parser.error("--max-estimated-job-bytes must be >= 1")
    if args.zip_part_max_files < 1:
        parser.error("--zip-part-max-files must be >= 1")
    if args.zip_part_max_bytes < 1:
        parser.error("--zip-part-max-bytes must be >= 1")

    return args


def unique_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def normalize_doi(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""

    value = value.strip("<>\"' ")
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)

    match = DOI_PATTERN.search(value)
    if not match:
        return ""

    doi = match.group(0).rstrip(").,;]}\"'")
    return doi.lower()


def parse_openaccess_flag(raw_value: str) -> bool:
    value = str(raw_value or "").strip().lower()
    return value in OPEN_ACCESS_TRUE_VALUES


def parse_name_columns(raw_value: str) -> list[str]:
    columns = [column.strip() for column in str(raw_value or "").split(",") if column.strip()]
    if columns:
        return unique_keep_order(columns)
    return list(DEFAULT_NAME_HINT_COLUMNS)


def resolve_delimiter(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    if value in {"", "auto"}:
        return "auto"
    if value in {",", ";", "|"}:
        return value
    if value in {"\\t", "tab", "TAB"}:
        return "\t"
    raise RuntimeError(
        "Invalid --csv-delimiter value. Use auto, ',', ';', '|', or '\\t'."
    )


def detect_csv_delimiter(sample: str, configured: str) -> str:
    if configured != "auto":
        return configured

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        if dialect.delimiter:
            return dialect.delimiter
    except csv.Error:
        pass

    counts = {delimiter: sample.count(delimiter) for delimiter in [",", ";", "\t", "|"]}
    best = max(counts, key=counts.get)
    if counts[best] > 0:
        return best
    return ","


def build_name_hint(row: dict[str, str], name_columns: list[str]) -> str:
    parts: list[str] = []
    for column in name_columns:
        value = str(row.get(column, "") or "").strip()
        if value:
            parts.append(value)
    if not parts:
        return ""
    return " ".join(parts[:3])


def load_entries_from_csv(
    csv_path: Path,
    csv_delimiter_raw: str,
    openaccess_column: str,
    short_id_column: str,
    doi_column: str,
    title_column: str,
    name_columns_raw: str,
) -> tuple[list[PaperEntry], list[DownloadRecord], int, str]:
    if not csv_path.exists():
        raise RuntimeError(f"CSV file not found: {csv_path}")

    try:
        csv_file = csv_path.open(newline="", encoding="utf-8")
    except UnicodeDecodeError:
        csv_file = csv_path.open(newline="", encoding="latin-1", errors="ignore")

    entries: list[PaperEntry] = []
    skipped: list[DownloadRecord] = []
    filtered_non_openaccess = 0
    with csv_file:
        configured_delimiter = resolve_delimiter(csv_delimiter_raw)
        sample = csv_file.read(32768)
        csv_file.seek(0)
        detected_delimiter = detect_csv_delimiter(sample, configured=configured_delimiter)

        reader = csv.DictReader(csv_file, delimiter=detected_delimiter)
        header = reader.fieldnames or []
        required = [short_id_column, doi_column]
        missing = [name for name in required if name not in header]
        if missing:
            raise RuntimeError(
                f"CSV is missing required columns: {', '.join(missing)}. "
                f"Found: {', '.join(header)}"
            )

        has_openaccess_column = openaccess_column in header
        has_title_column = title_column in header
        name_columns = [
            column for column in parse_name_columns(name_columns_raw) if column in header
        ]

        for row_number, row in enumerate(reader, start=2):
            openaccess_value = (
                str(row.get(openaccess_column, "") or "").strip()
                if has_openaccess_column
                else DEFAULT_OPENACCESS_VALUE
            )
            if has_openaccess_column and not parse_openaccess_flag(openaccess_value):
                filtered_non_openaccess += 1
                skipped.append(
                    DownloadRecord(
                        status="skipped",
                        row_number=row_number,
                        short_id=str(row.get(short_id_column, "") or "").strip(),
                        doi=normalize_doi(str(row.get(doi_column, "") or "")),
                        openaccess=openaccess_value,
                        file_path="",
                        openalex_id="",
                        title=str(row.get(title_column, "") or "").strip() or "untitled",
                        source_url="",
                        message="row skipped by openaccess column",
                    )
                )
                continue

            short_id = str(row.get(short_id_column, "") or "").strip()
            doi = normalize_doi(str(row.get(doi_column, "") or ""))
            title = (
                str(row.get(title_column, "") or "").strip()
                if has_title_column
                else ""
            )
            if not title:
                title = "untitled"
            name_hint = build_name_hint(row, name_columns=name_columns)

            if not short_id:
                skipped.append(
                    DownloadRecord(
                        status="skipped",
                        row_number=row_number,
                        short_id="",
                        doi=doi,
                        openaccess=openaccess_value,
                        file_path="",
                        openalex_id="",
                        title=title,
                        source_url="",
                        message="missing short_id in CSV row",
                    )
                )
                continue

            if not doi:
                skipped.append(
                    DownloadRecord(
                        status="skipped",
                        row_number=row_number,
                        short_id=short_id,
                        doi="",
                        openaccess=openaccess_value,
                        file_path="",
                        openalex_id="",
                        title=title,
                        source_url="",
                        message="missing or invalid DOI in CSV row",
                    )
                )
                continue

            entries.append(
                PaperEntry(
                    row_number=row_number,
                    short_id=short_id,
                    doi=doi,
                    title=title,
                    name_hint=name_hint,
                    openaccess=openaccess_value,
                )
            )

    return entries, skipped, filtered_non_openaccess, detected_delimiter


def get_json(url: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenAlex request failed (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAlex request failed ({exc.reason})") from exc


def fetch_work_by_doi(doi: str, timeout: int, email: str) -> dict[str, Any] | None:
    doi_url = f"https://doi.org/{doi}"

    direct_id = urllib.parse.quote(doi_url, safe="")
    direct_url = f"{OPENALEX_WORKS_URL}/{direct_id}"
    if email:
        direct_url = f"{direct_url}?{urllib.parse.urlencode({'mailto': email})}"

    req = urllib.request.Request(direct_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(f"OpenAlex request failed (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAlex request failed ({exc.reason})") from exc

    params = {
        "filter": f"doi:{doi_url}",
        "per-page": "1",
    }
    if email:
        params["mailto"] = email
    fallback_url = f"{OPENALEX_WORKS_URL}?{urllib.parse.urlencode(params)}"
    data = get_json(fallback_url, timeout=timeout)
    results = data.get("results") or []
    if not results:
        return None
    return results[0]


def sanitize_filename(value: str, max_len: int = 180) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = "paper"
    return cleaned[:max_len]


def build_filename(
    short_id: str,
    doi: str,
    name_hint: str,
    output_dir: Path,
    overwrite: bool,
) -> Path:
    short_id_token = sanitize_filename(short_id, max_len=32)
    if not short_id_token:
        short_id_token = f"id_{secrets.token_hex(2)}"

    doi_token = sanitize_filename(doi.replace("/", "_"), max_len=90)
    raw_name_hint = str(name_hint or "").strip()
    if raw_name_hint:
        name_token = sanitize_filename(raw_name_hint, max_len=130)
    else:
        name_token = ""
    if not name_token or name_token == "paper":
        name_token = f"paper_{short_id_token}_{secrets.token_hex(4)}"

    stem = sanitize_filename(
        f"shortid_{short_id_token}_{name_token}_{doi_token}",
        max_len=220,
    )
    if not stem:
        stem = f"shortid_{short_id_token}_{secrets.token_hex(4)}"
    candidate = output_dir / f"{stem}.pdf"

    if overwrite or not candidate.exists():
        return candidate

    counter = 2
    while True:
        named = output_dir / f"{stem}_{counter}.pdf"
        if not named.exists():
            return named
        counter += 1


def extract_pdf_urls(work: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    best = work.get("best_oa_location") or {}
    primary = work.get("primary_location") or {}
    locations = work.get("locations") or []
    open_access = work.get("open_access") or {}

    urls.append(str(best.get("pdf_url") or ""))
    urls.append(str(best.get("landing_page_url") or ""))
    urls.append(str(open_access.get("oa_url") or ""))

    if primary.get("is_oa"):
        urls.append(str(primary.get("pdf_url") or ""))
        urls.append(str(primary.get("landing_page_url") or ""))

    for loc in locations:
        if not isinstance(loc, dict):
            continue
        if not loc.get("is_oa"):
            continue
        urls.append(str(loc.get("pdf_url") or ""))
        urls.append(str(loc.get("landing_page_url") or ""))

    return unique_keep_order(url for url in urls if url.startswith("http"))


def is_probably_pdf(first_chunk: bytes, content_type: str) -> bool:
    if "application/pdf" in content_type.lower():
        return True
    return first_chunk.startswith(b"%PDF")


def is_probably_html(first_chunk: bytes, content_type: str) -> bool:
    content_type_lower = content_type.lower()
    if "text/html" in content_type_lower or "application/xhtml+xml" in content_type_lower:
        return True
    sample = first_chunk.lstrip()[:200].lower()
    return sample.startswith(b"<!doctype html") or sample.startswith(b"<html")


def extract_pdf_links_from_html(html_bytes: bytes, base_url: str) -> list[str]:
    html_text = html_bytes.decode("utf-8", errors="ignore")
    parser = PdfLinkParser()
    parser.feed(html_text)
    parser.close()

    url_regex = re.compile(r"https?://[^\"'<>\s]+", re.IGNORECASE)
    raw_candidates = parser.links + url_regex.findall(html_text)
    resolved_links: list[str] = []

    for raw_url in raw_candidates:
        candidate = html_module.unescape(raw_url.strip())
        if not candidate:
            continue
        if candidate.lower().startswith(("javascript:", "data:", "mailto:")):
            continue

        absolute = urllib.parse.urljoin(base_url, candidate)
        if not absolute.startswith("http"):
            continue

        absolute_lower = absolute.lower()
        if ".pdf" in absolute_lower or "pdf" in absolute_lower or "download" in absolute_lower:
            resolved_links.append(absolute)

    return unique_keep_order(resolved_links)[:MAX_FOLLOW_UP_URLS]


def try_single_url(
    url: str,
    destination: Path,
    timeout: int,
) -> tuple[bool, str, list[str]]:
    succeeded = False
    tmp_path: Path | None = None
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            resolved_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            first_chunk = response.read(CHUNK_SIZE)
            if not first_chunk:
                return False, "empty response", []
            if not is_probably_pdf(first_chunk, content_type):
                if is_probably_html(first_chunk, content_type):
                    html_bytes = first_chunk + response.read(MAX_HTML_BYTES)
                    follow_up_urls = extract_pdf_links_from_html(
                        html_bytes=html_bytes,
                        base_url=resolved_url,
                    )
                    if follow_up_urls:
                        return (
                            False,
                            f"got HTML page; discovered {len(follow_up_urls)} candidate PDF link(s)",
                            follow_up_urls,
                        )
                    return False, "got HTML page; no PDF links discovered", []
                return (
                    False,
                    f"not a PDF (Content-Type: {content_type or 'unknown'})",
                    [],
                )

            with tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=str(destination.parent),
                prefix=".tmp_",
                suffix=".pdf",
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(first_chunk)
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    tmp_file.write(chunk)
        tmp_path.replace(destination)
        succeeded = True
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", []
    except urllib.error.URLError as exc:
        return False, f"URL error: {exc.reason}", []
    except TimeoutError:
        return False, "request timed out", []
    except Exception as exc:  # noqa: BLE001
        return False, f"download error: {exc}", []
    finally:
        if not succeeded and tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()

    return True, "ok", []


def make_temp_pdf_path(parent_dir: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=str(parent_dir),
        prefix=".tmp_render_",
        suffix=".pdf",
    ) as tmp_file:
        return Path(tmp_file.name)


def is_pdf_file(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as file_obj:
        return file_obj.read(4) == b"%PDF"


def render_html_to_pdf_weasyprint(url: str, destination: Path) -> tuple[bool, str]:
    try:
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(
            stderr_buffer
        ):
            from weasyprint import HTML  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        return False, f"weasyprint unavailable ({exc})"

    import_output = (stdout_buffer.getvalue() + stderr_buffer.getvalue()).lower()
    if "could not import some external libraries" in import_output:
        return False, "weasyprint unavailable (missing external system libraries)"

    tmp_path = make_temp_pdf_path(destination.parent)
    try:
        HTML(url=url).write_pdf(target=str(tmp_path))
        if not is_pdf_file(tmp_path):
            return False, "weasyprint produced invalid PDF output"
        tmp_path.replace(destination)
        return True, "rendered HTML page to PDF via weasyprint"
    except Exception as exc:  # noqa: BLE001
        return False, f"weasyprint failed ({exc})"
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def render_html_to_pdf_playwright(
    url: str,
    destination: Path,
    timeout: int,
) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        return False, f"playwright unavailable ({exc})"

    tmp_path = make_temp_pdf_path(destination.parent)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                page.pdf(path=str(tmp_path), print_background=True, format="A4")
            finally:
                browser.close()

        if not is_pdf_file(tmp_path):
            return False, "playwright produced invalid PDF output"
        tmp_path.replace(destination)
        return True, "rendered HTML page to PDF via playwright"
    except Exception as exc:  # noqa: BLE001
        return False, f"playwright failed ({exc})"
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def render_html_to_pdf(
    url: str,
    destination: Path,
    timeout: int,
    engine: str,
) -> tuple[bool, str]:
    if engine == "auto":
        # Try Playwright first to avoid noisy WeasyPrint system-lib warnings.
        engines = ["playwright", "weasyprint"]
    else:
        engines = [engine]

    failures: list[str] = []
    for candidate_engine in engines:
        if candidate_engine == "weasyprint":
            ok, message = render_html_to_pdf_weasyprint(url=url, destination=destination)
        elif candidate_engine == "playwright":
            ok, message = render_html_to_pdf_playwright(
                url=url,
                destination=destination,
                timeout=timeout,
            )
        else:
            ok, message = False, f"unsupported render engine: {candidate_engine}"

        if ok:
            return True, message
        failures.append(message)

    return False, " | ".join(failures)


def download_pdf_with_fallback(
    initial_urls: list[str],
    destination: Path,
    timeout: int,
    html_to_pdf_fallback: bool,
    html_render_engine: str,
) -> tuple[bool, str, str]:
    queue = unique_keep_order(initial_urls)
    visited: set[str] = set()
    attempts = 0
    last_url = ""
    last_message = "all URLs failed"
    html_pages_to_render: list[str] = []

    while queue and attempts < MAX_URL_ATTEMPTS:
        current_url = queue.pop(0)
        if current_url in visited:
            continue

        visited.add(current_url)
        attempts += 1
        ok, message, follow_up_urls = try_single_url(
            url=current_url,
            destination=destination,
            timeout=timeout,
        )
        last_url = current_url
        last_message = message
        if ok:
            return True, message, current_url

        if message.startswith("got HTML page"):
            html_pages_to_render.append(current_url)

        for follow_up_url in follow_up_urls:
            if follow_up_url not in visited and follow_up_url not in queue:
                queue.append(follow_up_url)

    if queue and attempts >= MAX_URL_ATTEMPTS:
        last_message = (
            f"{last_message}; stopped after {MAX_URL_ATTEMPTS} URL attempts"
        )

    if html_to_pdf_fallback and html_pages_to_render:
        for html_page_url in unique_keep_order(html_pages_to_render):
            ok, render_message = render_html_to_pdf(
                url=html_page_url,
                destination=destination,
                timeout=timeout,
                engine=html_render_engine,
            )
            if ok:
                return True, render_message, html_page_url
            last_message = (
                f"{last_message}; HTML-to-PDF fallback failed for {html_page_url} "
                f"({render_message})"
            )

    return False, last_message, last_url


def run_download_batch(
    entries: list[PaperEntry],
    output_dir: Path,
    timeout: int,
    email: str,
    overwrite: bool,
    html_to_pdf_fallback: bool,
    html_render_engine: str,
    log: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int, DownloadRecord, int, int, int], None] | None = None,
    initial_records: list[DownloadRecord] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> DownloadSummary:
    records: list[DownloadRecord] = list(initial_records or [])
    downloaded = sum(1 for record in records if record.status == "downloaded")
    failed = sum(1 for record in records if record.status == "failed")
    skipped = sum(1 for record in records if record.status == "skipped")
    total_entries = len(entries)
    processed_entries = 0
    stopped_early = False

    for index, entry in enumerate(entries, start=1):
        if should_stop is not None and should_stop():
            stopped_early = True
            if log is not None:
                log("Stop requested. Ending batch early.")
            break

        if log is not None:
            log(
                f"[{index}/{total_entries}] Resolving DOI {entry.doi} "
                f"(short_id={entry.short_id}, row={entry.row_number})"
            )

        try:
            work = fetch_work_by_doi(doi=entry.doi, timeout=timeout, email=email)
        except RuntimeError as exc:
            failed += 1
            result_record = DownloadRecord(
                status="failed",
                row_number=entry.row_number,
                short_id=entry.short_id,
                doi=entry.doi,
                openaccess=entry.openaccess,
                file_path="",
                openalex_id="",
                title=entry.title,
                source_url="",
                message=str(exc),
            )
            records.append(result_record)
            if log is not None:
                log(
                    f"[{index}/{total_entries}] failed: short_id={entry.short_id} "
                    f"({exc})"
                )
            processed_entries = index
            if progress_callback is not None:
                progress_callback(index, total_entries, result_record, downloaded, failed, skipped)
            continue

        if not work:
            failed += 1
            result_record = DownloadRecord(
                status="failed",
                row_number=entry.row_number,
                short_id=entry.short_id,
                doi=entry.doi,
                openaccess=entry.openaccess,
                file_path="",
                openalex_id="",
                title=entry.title,
                source_url="",
                message="DOI not found in OpenAlex",
            )
            records.append(result_record)
            if log is not None:
                log(
                    f"[{index}/{total_entries}] failed: short_id={entry.short_id} "
                    "DOI not found in OpenAlex"
                )
            processed_entries = index
            if progress_callback is not None:
                progress_callback(index, total_entries, result_record, downloaded, failed, skipped)
            continue

        openalex_id = str(work.get("id") or "")
        title = str(work.get("display_name") or entry.title or "untitled")
        pdf_urls = extract_pdf_urls(work)

        if not pdf_urls:
            skipped += 1
            result_record = DownloadRecord(
                status="skipped",
                row_number=entry.row_number,
                short_id=entry.short_id,
                doi=entry.doi,
                openaccess=entry.openaccess,
                file_path="",
                openalex_id=openalex_id,
                title=title,
                source_url="",
                message="no open-access PDF URL available",
            )
            records.append(result_record)
            if log is not None:
                log(
                    f"[{index}/{total_entries}] skipped: no OA PDF URL "
                    f"(short_id={entry.short_id})"
                )
            processed_entries = index
            if progress_callback is not None:
                progress_callback(index, total_entries, result_record, downloaded, failed, skipped)
            continue

        file_path = build_filename(
            short_id=entry.short_id,
            doi=entry.doi,
            name_hint=entry.name_hint,
            output_dir=output_dir,
            overwrite=overwrite,
        )

        success, last_message, last_url = download_pdf_with_fallback(
            initial_urls=pdf_urls,
            destination=file_path,
            timeout=timeout,
            html_to_pdf_fallback=html_to_pdf_fallback,
            html_render_engine=html_render_engine,
        )

        if success:
            downloaded += 1
            result_record = DownloadRecord(
                status="downloaded",
                row_number=entry.row_number,
                short_id=entry.short_id,
                doi=entry.doi,
                openaccess=entry.openaccess,
                file_path=str(file_path),
                openalex_id=openalex_id,
                title=title,
                source_url=last_url,
                message=last_message,
            )
            records.append(result_record)
            if log is not None:
                log(
                    f"[{index}/{total_entries}] downloaded -> {file_path.name} "
                    f"(short_id={entry.short_id})"
                )
        else:
            failed += 1
            result_record = DownloadRecord(
                status="failed",
                row_number=entry.row_number,
                short_id=entry.short_id,
                doi=entry.doi,
                openaccess=entry.openaccess,
                file_path=str(file_path),
                openalex_id=openalex_id,
                title=title,
                source_url=last_url,
                message=last_message,
            )
            records.append(result_record)
            if log is not None:
                log(
                    f"[{index}/{total_entries}] failed: short_id={entry.short_id} "
                    f"({last_message})"
                )

        processed_entries = index
        if progress_callback is not None:
            progress_callback(index, total_entries, result_record, downloaded, failed, skipped)

    return DownloadSummary(
        downloaded=downloaded,
        failed=failed,
        skipped=skipped,
        processed=processed_entries,
        stopped_early=stopped_early,
        records=records,
    )


def write_report(output_dir: Path, records: list[DownloadRecord]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"download_report_leuphana_{timestamp}.csv"
    with report_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "status",
                "row_number",
                "short_id",
                "doi",
                "openaccess",
                "file_path",
                "openalex_id",
                "title",
                "source_url",
                "message",
            ]
        )
        for row in records:
            writer.writerow(
                [
                    row.status,
                    row.row_number,
                    row.short_id,
                    row.doi,
                    row.openaccess,
                    row.file_path,
                    row.openalex_id,
                    row.title,
                    row.source_url,
                    row.message,
                ]
            )
    return report_path


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def csv_safe_cell(value: Any) -> str:
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def parse_row_number(raw_value: Any, default_value: int) -> int:
    if raw_value is None:
        return default_value
    try:
        row_number = int(raw_value)
    except (TypeError, ValueError):
        return default_value
    if row_number < 1:
        return default_value
    return row_number


def get_row_value(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        if key in row:
            return str(row.get(key) or "").strip()
    return ""


def build_entries_from_payload(
    rows_payload: Any,
) -> tuple[list[PaperEntry], list[dict[str, Any]]]:
    if not isinstance(rows_payload, list):
        raise RuntimeError("payload field 'rows' must be a JSON array")
    if not rows_payload:
        raise RuntimeError("payload field 'rows' is empty")

    short_id_counts: dict[str, int] = {}
    for row in rows_payload:
        if not isinstance(row, dict):
            continue
        short_id = get_row_value(row, ["short_id", "shortId"])
        short_key = short_id.lower()
        if short_key:
            short_id_counts[short_key] = short_id_counts.get(short_key, 0) + 1
    duplicate_short_ids = {
        short_id for short_id, count in short_id_counts.items() if count > 1
    }

    entries: list[PaperEntry] = []
    invalid_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows_payload, start=1):
        if not isinstance(row, dict):
            invalid_rows.append(
                {
                    "row_number": index,
                    "short_id": "",
                    "doi": "",
                    "errors": ["row is not a JSON object"],
                }
            )
            continue

        row_number = parse_row_number(
            row.get("row_number", row.get("rowNumber")),
            default_value=index + 1,
        )
        short_id = get_row_value(row, ["short_id", "shortId"])
        short_key = short_id.lower()
        doi = normalize_doi(get_row_value(row, ["doi", "DOI"]))
        title = get_row_value(row, ["title"]) or "untitled"
        name_hint = get_row_value(row, ["name_hint", "nameHint"])
        openaccess = get_row_value(row, ["openaccess"]) or DEFAULT_OPENACCESS_VALUE

        errors: list[str] = []
        if not short_id:
            errors.append("missing short_id")
        elif short_key in duplicate_short_ids:
            errors.append("duplicate short_id")

        if not doi:
            errors.append("missing DOI")
        elif DOI_PATTERN.fullmatch(doi) is None:
            errors.append("invalid DOI format")

        if errors:
            invalid_rows.append(
                {
                    "row_number": row_number,
                    "short_id": short_id,
                    "doi": doi,
                    "errors": errors,
                }
            )
            continue

        entries.append(
            PaperEntry(
                row_number=row_number,
                short_id=short_id,
                doi=doi,
                title=title,
                name_hint=name_hint,
                openaccess=openaccess,
            )
        )

    return entries, invalid_rows


def parse_job_config(payload: dict[str, Any]) -> JobConfig:
    options_raw = payload.get("options")
    options = options_raw if isinstance(options_raw, dict) else {}

    timeout = API_DEFAULT_JOB_CONFIG.timeout
    raw_timeout = options.get("timeout")
    if raw_timeout is not None:
        try:
            parsed_timeout = int(raw_timeout)
            if parsed_timeout >= 1:
                timeout = parsed_timeout
        except (TypeError, ValueError):
            timeout = API_DEFAULT_JOB_CONFIG.timeout

    raw_engine = str(
        options.get("html_render_engine", API_DEFAULT_JOB_CONFIG.html_render_engine)
    ).strip().lower()
    html_render_engine = raw_engine if raw_engine in {"auto", "weasyprint", "playwright"} else "auto"

    html_to_pdf_fallback = API_DEFAULT_JOB_CONFIG.html_to_pdf_fallback
    if "html_to_pdf_fallback" in options:
        html_to_pdf_fallback = parse_bool(
            options.get("html_to_pdf_fallback"),
            default=html_to_pdf_fallback,
        )
    if "disable_html_to_pdf_fallback" in options:
        disabled = parse_bool(
            options.get("disable_html_to_pdf_fallback"),
            default=not html_to_pdf_fallback,
        )
        html_to_pdf_fallback = not disabled

    return JobConfig(
        timeout=timeout,
        email=str(options.get("email", API_DEFAULT_JOB_CONFIG.email) or "").strip(),
        overwrite=parse_bool(
            options.get("overwrite"),
            default=API_DEFAULT_JOB_CONFIG.overwrite,
        ),
        html_to_pdf_fallback=html_to_pdf_fallback,
        html_render_engine=html_render_engine,
    )


def serialize_record_for_api(record: DownloadRecord) -> dict[str, Any]:
    return {
        "status": record.status,
        "row_number": record.row_number,
        "short_id": record.short_id,
        "doi": record.doi,
        "title": record.title,
        "openalex_id": record.openalex_id,
        "file_name": Path(record.file_path).name if record.file_path else "",
        "source_url": record.source_url,
        "message": record.message,
    }


def get_failed_records(records: list[DownloadRecord]) -> list[DownloadRecord]:
    return [record for record in records if record.status in {"failed", "skipped"}]


def human_readable_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ["KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PB"


def split_pdf_files_into_parts(pdf_files: list[Path]) -> list[list[Path]]:
    groups: list[list[Path]] = []
    current_group: list[Path] = []
    current_group_bytes = 0

    for pdf_file in pdf_files:
        try:
            file_size = max(pdf_file.stat().st_size, 0)
        except OSError:
            continue

        reached_file_limit = len(current_group) >= API_ZIP_PART_MAX_FILES
        reached_size_limit = (
            current_group_bytes + file_size > API_ZIP_PART_MAX_BYTES and len(current_group) > 0
        )
        if reached_file_limit or reached_size_limit:
            groups.append(current_group)
            current_group = []
            current_group_bytes = 0

        current_group.append(pdf_file)
        current_group_bytes += file_size

    if current_group:
        groups.append(current_group)

    return groups


def get_zip_part_urls(job: JobState) -> list[str]:
    with job.lock:
        if job.files_deleted:
            return []
        if job.status not in {"completed", "failed", "cancelled"}:
            return []
        if job.downloaded <= 0:
            return []
        output_dir = job.output_dir
        job_id = job.job_id

    pdf_files = sorted(output_dir.glob("*.pdf"))
    if not pdf_files:
        return []
    parts = split_pdf_files_into_parts(pdf_files)
    if len(parts) <= 1:
        return [f"/api/jobs/{job_id}/results.zip"]
    return [f"/api/jobs/{job_id}/results.zip?part={index}" for index in range(1, len(parts) + 1)]


def render_report_csv(records: list[DownloadRecord]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "status",
            "row_number",
            "short_id",
            "doi",
            "openaccess",
            "file_path",
            "openalex_id",
            "title",
            "source_url",
            "message",
        ]
    )
    for row in records:
        writer.writerow(
            [
                csv_safe_cell(row.status),
                row.row_number,
                csv_safe_cell(row.short_id),
                csv_safe_cell(row.doi),
                csv_safe_cell(row.openaccess),
                csv_safe_cell(Path(row.file_path).name if row.file_path else ""),
                csv_safe_cell(row.openalex_id),
                csv_safe_cell(row.title),
                csv_safe_cell(row.source_url),
                csv_safe_cell(row.message),
            ]
        )
    return output.getvalue().encode("utf-8")


def cleanup_job_artifacts(job: JobState, force: bool = False) -> None:
    with job.lock:
        if job.files_deleted:
            return
        if not force and job.status in {"queued", "running", "cancelling"}:
            return
        output_dir = job.output_dir
        cleanup_timer = job.cleanup_timer
        job.cleanup_timer = None
        job.files_deleted = True
        job.files_deleted_at = utc_now_iso()
        job.zip_path = ""

    if cleanup_timer is not None:
        cleanup_timer.cancel()

    if output_dir.exists():
        for path in output_dir.iterdir():
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        try:
            output_dir.rmdir()
        except OSError:
            pass


def schedule_job_cleanup(job: JobState, retention_seconds: int) -> None:
    delay_seconds = max(retention_seconds, 0)
    with job.lock:
        if job.cleanup_timer is not None:
            job.cleanup_timer.cancel()
            job.cleanup_timer = None
        expires_at = datetime.utcnow() + timedelta(seconds=delay_seconds)
        job.expires_at = expires_at.replace(microsecond=0).isoformat() + "Z"
        timer = threading.Timer(delay_seconds, cleanup_job_artifacts, args=(job,))
        timer.daemon = True
        job.cleanup_timer = timer
    timer.start()


def create_results_zip(job: JobState, part_index: int) -> Path | None:
    with job.lock:
        if job.files_deleted:
            return None
        if job.status not in {"completed", "failed", "cancelled"}:
            return None
        if job.downloaded <= 0:
            return None
        output_dir = job.output_dir
        records = list(job.records)
        job_id = job.job_id

    pdf_files = sorted(output_dir.glob("*.pdf"))
    if not pdf_files:
        return None
    parts = split_pdf_files_into_parts(pdf_files)
    if not parts:
        return None
    if part_index < 1 or part_index > len(parts):
        raise RuntimeError(f"invalid zip part {part_index}; available 1..{len(parts)}")
    selected_part = parts[part_index - 1]

    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        prefix=f"download_results_{job_id}_part{part_index}_",
        suffix=".zip",
        dir=str(output_dir.parent),
    ) as temp_file:
        zip_path = Path(temp_file.name)

    report_bytes = render_report_csv(records)
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for pdf_path in selected_part:
            archive.write(pdf_path, arcname=pdf_path.name)
        archive.writestr(
            f"download_report_{job_id}_part_{part_index}_of_{len(parts)}.csv",
            report_bytes,
        )
    return zip_path


def serialize_job(job: JobState) -> dict[str, Any]:
    zip_part_urls = get_zip_part_urls(job)
    with job.lock:
        pending = max(job.total_requested - job.processed, 0)
        failed_total = job.failed + job.skipped
        progress_percent = int((job.processed / job.total_requested) * 100) if job.total_requested else 100
        failed_records = [serialize_record_for_api(row) for row in get_failed_records(job.records)]
        can_cancel = job.status in {"queued", "running", "cancelling"} and not job.cancel_requested
        has_report = bool(job.records) or job.status in {"completed", "failed", "cancelled"}
        can_download_zip = (
            not job.files_deleted
            and job.status in {"completed", "failed", "cancelled"}
            and job.downloaded > 0
        )
        response = {
            "job_id": job.job_id,
            "status": job.status,
            "error": job.error,
            "cancel_requested": job.cancel_requested,
            "cancel_requested_at": job.cancel_requested_at,
            "can_cancel": can_cancel,
            "files_deleted": job.files_deleted,
            "files_deleted_at": job.files_deleted_at,
            "expires_at": job.expires_at,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "counts": {
                "total_requested": job.total_requested,
                "processed": job.processed,
                "pending": pending,
                "downloaded": job.downloaded,
                "failed": failed_total,
                "failed_network_or_lookup": job.failed,
                "skipped_no_pdf": job.skipped,
            },
            "progress_percent": progress_percent,
            "failed_records": failed_records,
            "failed_csv_url": f"/api/jobs/{job.job_id}/failed.csv" if failed_total else "",
            "report_csv_url": f"/api/jobs/{job.job_id}/report.csv" if has_report else "",
            "results_zip_url": zip_part_urls[0] if can_download_zip and zip_part_urls else "",
            "results_zip_urls": zip_part_urls if can_download_zip else [],
            "zip_parts_total": len(zip_part_urls) if can_download_zip else 0,
        }
    return response


def render_failed_csv(records: list[DownloadRecord]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["status", "row_number", "short_id", "doi", "error_reason"])
    for record in get_failed_records(records):
        writer.writerow(
            [
                csv_safe_cell(record.status),
                record.row_number,
                csv_safe_cell(record.short_id),
                csv_safe_cell(record.doi),
                csv_safe_cell(record.message),
            ]
        )
    return output.getvalue().encode("utf-8")


def get_job(job_id: str) -> JobState | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


def is_job_cancel_requested(job: JobState) -> bool:
    with job.lock:
        return job.cancel_requested


def run_job_worker(job_id: str, entries: list[PaperEntry], config: JobConfig) -> None:
    job = get_job(job_id)
    if job is None:
        return

    cancelled_before_start = False
    with job.lock:
        now = utc_now_iso()
        job.started_at = now
        if job.cancel_requested:
            job.status = "cancelled"
            job.finished_at = now
            cancelled_before_start = True
        else:
            job.status = "running"

    if cancelled_before_start:
        schedule_job_cleanup(job, API_JOB_RETENTION)
        if API_JOB_RETENTION == 0:
            cleanup_job_artifacts(job, force=True)
        return

    def on_progress(
        processed: int,
        _total: int,
        record: DownloadRecord,
        downloaded: int,
        failed: int,
        skipped: int,
    ) -> None:
        with job.lock:
            job.processed = processed
            job.downloaded = downloaded
            job.failed = failed
            job.skipped = skipped
            job.records.append(record)

    try:
        summary = run_download_batch(
            entries=entries,
            output_dir=job.output_dir,
            timeout=config.timeout,
            email=config.email,
            overwrite=config.overwrite,
            html_to_pdf_fallback=config.html_to_pdf_fallback,
            html_render_engine=config.html_render_engine,
            log=None,
            progress_callback=on_progress,
            initial_records=[],
            should_stop=lambda: is_job_cancel_requested(job),
        )
        with job.lock:
            cancel_requested = job.cancel_requested
        with job.lock:
            if cancel_requested and summary.processed < job.total_requested:
                job.status = "cancelled"
            else:
                job.status = "completed"
            job.processed = summary.processed
            job.downloaded = summary.downloaded
            job.failed = summary.failed
            job.skipped = summary.skipped
            job.records = summary.records
            job.finished_at = utc_now_iso()
        schedule_job_cleanup(job, API_JOB_RETENTION)
        if API_JOB_RETENTION == 0:
            cleanup_job_artifacts(job, force=True)
    except Exception as exc:  # noqa: BLE001
        error_text = f"{exc}\n{traceback.format_exc(limit=10)}"
        with job.lock:
            job.status = "failed"
            job.error = error_text
            job.finished_at = utc_now_iso()
        schedule_job_cleanup(job, API_JOB_RETENTION)
        if API_JOB_RETENTION == 0:
            cleanup_job_artifacts(job, force=True)


class DownloaderAPIHandler(BaseHTTPRequestHandler):
    server_version = "doi-downloader-api/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        print(f"{self.address_string()} - {format % args}")

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        super().end_headers()

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        download_name: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(
        self,
        status: int,
        path: Path,
        content_type: str,
        download_name: str,
    ) -> None:
        file_size = path.stat().st_size
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(file_size))
        self.end_headers()
        with path.open("rb") as file_obj:
            while True:
                chunk = file_obj.read(CHUNK_SIZE)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def parse_json_body(self) -> dict[str, Any]:
        content_length = self.headers.get("Content-Length", "")
        if not content_length:
            return {}
        try:
            body_size = int(content_length)
        except ValueError as exc:
            raise RuntimeError("invalid Content-Length header") from exc
        if body_size < 0:
            raise RuntimeError("negative Content-Length header")
        if body_size > API_MAX_BODY_BYTES:
            raise RuntimeError("request body too large")
        if body_size == 0:
            return {}

        raw_body = self.rfile.read(body_size)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON payload ({exc})") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("JSON payload must be an object")
        return payload

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/jobs":
            self.handle_start_job()
            return
        cancel_match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/cancel", path)
        if cancel_match is not None:
            self.handle_cancel_job(cancel_match.group(1))
            return
        self.send_json(404, {"error": "endpoint not found"})

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query_params = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=False)
        if path == "/api/health":
            self.send_json(200, {"status": "ok", "time": utc_now_iso()})
            return

        job_status_match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)", path)
        if job_status_match is not None:
            self.handle_get_job(job_status_match.group(1))
            return

        failed_csv_match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/failed\.csv", path)
        if failed_csv_match is not None:
            self.handle_failed_csv(failed_csv_match.group(1))
            return

        report_csv_match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/report\.csv", path)
        if report_csv_match is not None:
            self.handle_report_csv(report_csv_match.group(1))
            return

        zip_match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/results\.zip", path)
        if zip_match is not None:
            self.handle_results_zip(zip_match.group(1), query_params)
            return

        self.send_json(404, {"error": "endpoint not found"})

    def handle_start_job(self) -> None:
        try:
            payload = self.parse_json_body()
            entries, invalid_rows = build_entries_from_payload(payload.get("rows"))
        except RuntimeError as exc:
            self.send_json(400, {"error": str(exc)})
            return

        if invalid_rows:
            self.send_json(
                400,
                {
                    "error": "rows contain validation errors",
                    "invalid_count": len(invalid_rows),
                    "invalid_rows": invalid_rows[:200],
                },
            )
            return

        config = parse_job_config(payload)
        requested_rows = len(entries)
        if requested_rows > API_MAX_JOB_ROWS:
            self.send_json(
                400,
                {
                    "error": (
                        f"job has {requested_rows} row(s), which exceeds the configured "
                        f"limit of {API_MAX_JOB_ROWS}"
                    ),
                    "hint": "Use the start-row option or split into multiple batches.",
                },
            )
            return

        estimated_total_bytes = requested_rows * API_ESTIMATED_PDF_SIZE_BYTES
        if estimated_total_bytes > API_MAX_ESTIMATED_JOB_BYTES:
            self.send_json(
                400,
                {
                    "error": (
                        "job exceeds estimated storage budget "
                        f"({human_readable_bytes(estimated_total_bytes)} > "
                        f"{human_readable_bytes(API_MAX_ESTIMATED_JOB_BYTES)})"
                    ),
                    "estimated_total_bytes": estimated_total_bytes,
                    "hint": "Use the start-row option or split into multiple batches.",
                },
            )
            return

        job_id = secrets.token_hex(8)
        output_dir_path = tempfile.mkdtemp(
            prefix=f"job_{job_id}_",
            dir=str(API_OUTPUT_ROOT),
        )
        output_dir = Path(output_dir_path)

        job = JobState(
            job_id=job_id,
            output_dir=output_dir,
            total_requested=len(entries),
        )
        with JOBS_LOCK:
            JOBS[job_id] = job

        worker = threading.Thread(
            target=run_job_worker,
            args=(job_id, entries, config),
            daemon=True,
            name=f"download-job-{job_id}",
        )
        worker.start()

        self.send_json(
            202,
            {
                "job_id": job_id,
                "status": "queued",
                "status_url": f"/api/jobs/{job_id}",
                "total_requested": len(entries),
            },
        )

    def handle_get_job(self, job_id: str) -> None:
        job = get_job(job_id)
        if job is None:
            self.send_json(404, {"error": "job not found"})
            return
        self.send_json(200, serialize_job(job))

    def handle_cancel_job(self, job_id: str) -> None:
        job = get_job(job_id)
        if job is None:
            self.send_json(404, {"error": "job not found"})
            return

        already_finished = False
        with job.lock:
            if job.status in {"completed", "failed", "cancelled"}:
                already_finished = True
            else:
                if not job.cancel_requested:
                    job.cancel_requested = True
                    job.cancel_requested_at = utc_now_iso()

                if job.status in {"queued", "running"}:
                    job.status = "cancelling"

        response = serialize_job(job)
        if already_finished:
            response["message"] = "job already finished"
            self.send_json(200, response)
            return

        response["message"] = "stop requested"
        self.send_json(202, response)

    def handle_failed_csv(self, job_id: str) -> None:
        job = get_job(job_id)
        if job is None:
            self.send_json(404, {"error": "job not found"})
            return
        with job.lock:
            records = list(job.records)
        csv_bytes = render_failed_csv(records)
        filename = f"failed_downloads_{job_id}.csv"
        self.send_bytes(
            status=200,
            body=csv_bytes,
            content_type="text/csv; charset=utf-8",
            download_name=filename,
        )

    def handle_report_csv(self, job_id: str) -> None:
        job = get_job(job_id)
        if job is None:
            self.send_json(404, {"error": "job not found"})
            return
        with job.lock:
            records = list(job.records)
            status = job.status
        if not records and status not in {"completed", "failed", "cancelled"}:
            self.send_json(404, {"error": "report is not available yet"})
            return
        body = render_report_csv(records)
        self.send_bytes(
            status=200,
            body=body,
            content_type="text/csv; charset=utf-8",
            download_name=f"download_report_{job_id}.csv",
        )

    def handle_results_zip(self, job_id: str, query_params: dict[str, list[str]]) -> None:
        job = get_job(job_id)
        if job is None:
            self.send_json(404, {"error": "job not found"})
            return
        part_index = 1
        part_values = query_params.get("part") or []
        if part_values:
            try:
                part_index = int(part_values[0])
            except ValueError:
                self.send_json(400, {"error": "query parameter 'part' must be an integer >= 1"})
                return
        if part_index < 1:
            self.send_json(400, {"error": "query parameter 'part' must be >= 1"})
            return

        try:
            zip_path = create_results_zip(job, part_index=part_index)
        except RuntimeError as exc:
            self.send_json(400, {"error": str(exc)})
            return

        if zip_path is None or not zip_path.exists():
            self.send_json(404, {"error": "zip file not available"})
            return
        delivered = False
        try:
            self.send_file(
                status=200,
                path=zip_path,
                content_type="application/zip",
                download_name=f"download_results_{job_id}_part_{part_index}.zip",
            )
            delivered = True
        finally:
            zip_path.unlink(missing_ok=True)

        if delivered:
            schedule_job_cleanup(job, API_POST_DOWNLOAD_RETENTION)


def run_api_server(
    host: str,
    port: int,
    output_root: Path,
    default_job_config: JobConfig,
    job_retention_seconds: int,
    post_download_retention_seconds: int,
    max_job_rows: int,
    estimated_pdf_size_bytes: int,
    max_estimated_job_bytes: int,
    zip_part_max_files: int,
    zip_part_max_bytes: int,
) -> None:
    global API_OUTPUT_ROOT
    global API_DEFAULT_JOB_CONFIG
    global API_JOB_RETENTION
    global API_POST_DOWNLOAD_RETENTION
    global API_MAX_JOB_ROWS
    global API_ESTIMATED_PDF_SIZE_BYTES
    global API_MAX_ESTIMATED_JOB_BYTES
    global API_ZIP_PART_MAX_FILES
    global API_ZIP_PART_MAX_BYTES

    API_OUTPUT_ROOT = output_root.expanduser().resolve()
    API_DEFAULT_JOB_CONFIG = default_job_config
    API_JOB_RETENTION = max(job_retention_seconds, 0)
    API_POST_DOWNLOAD_RETENTION = max(post_download_retention_seconds, 0)
    API_MAX_JOB_ROWS = max(max_job_rows, 1)
    API_ESTIMATED_PDF_SIZE_BYTES = max(estimated_pdf_size_bytes, 1)
    API_MAX_ESTIMATED_JOB_BYTES = max(max_estimated_job_bytes, 1)
    API_ZIP_PART_MAX_FILES = max(zip_part_max_files, 1)
    API_ZIP_PART_MAX_BYTES = max(zip_part_max_bytes, 1)
    API_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"Starting DOI downloader API on http://{host}:{port}")
    print(f"Temporary job outputs directory: {API_OUTPUT_ROOT}")
    print(f"Server cleanup retention (seconds): {API_JOB_RETENTION}")
    print(f"Post-download retry retention (seconds): {API_POST_DOWNLOAD_RETENTION}")
    print(
        f"Capacity guard: max rows={API_MAX_JOB_ROWS}, "
        f"estimated PDF size={API_ESTIMATED_PDF_SIZE_BYTES} bytes, "
        f"max estimated bytes={API_MAX_ESTIMATED_JOB_BYTES}"
    )
    print(
        f"ZIP parts: max files per part={API_ZIP_PART_MAX_FILES}, "
        f"max bytes per part={API_ZIP_PART_MAX_BYTES}"
    )
    server = ThreadingHTTPServer((host, port), DownloaderAPIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down API server...")
    finally:
        server.server_close()


def run_cli(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv_file).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        entries, skipped_records, filtered_non_openaccess, used_delimiter = load_entries_from_csv(
            csv_path=csv_path,
            csv_delimiter_raw=args.csv_delimiter,
            openaccess_column=args.openaccess_column,
            short_id_column=args.short_id_column,
            doi_column=args.doi_column,
            title_column=args.title_column,
            name_columns_raw=args.name_columns,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Loaded {len(entries)} row(s) with valid DOI and short_id.")
    delimiter_label = "\\t" if used_delimiter == "\t" else used_delimiter
    print(f"Detected CSV delimiter: {delimiter_label!r}")
    if filtered_non_openaccess:
        print(
            f"Skipped {filtered_non_openaccess} row(s) marked non-openaccess "
            f"in column '{args.openaccess_column}'."
        )
    if skipped_records:
        print(f"Skipped {len(skipped_records)} row(s) due to filtering/missing data.")

    summary = run_download_batch(
        entries=entries,
        output_dir=output_dir,
        timeout=args.timeout,
        email=args.email,
        overwrite=args.overwrite,
        html_to_pdf_fallback=not args.disable_html_to_pdf_fallback,
        html_render_engine=args.html_render_engine,
        log=print,
        initial_records=skipped_records,
    )

    report_path = write_report(output_dir, summary.records)
    print("")
    print("Download complete.")
    print(f"CSV file: {csv_path}")
    print(f"Saved to: {output_dir}")
    print(
        f"Downloaded: {summary.downloaded} | "
        f"Skipped: {summary.skipped} | Failed: {summary.failed}"
    )
    print(f"Report: {report_path}")
    return 0


def main() -> int:
    args = parse_args()
    if args.serve:
        if args.output_dir == DEFAULT_OUTPUT_DIR:
            output_root = Path(tempfile.gettempdir()) / "doi_downloader_jobs"
        else:
            output_root = Path(args.output_dir)
        run_api_server(
            host=args.host,
            port=args.port,
            output_root=output_root,
            default_job_config=JobConfig(
                timeout=args.timeout,
                email=args.email,
                overwrite=args.overwrite,
                html_to_pdf_fallback=not args.disable_html_to_pdf_fallback,
                html_render_engine=args.html_render_engine,
            ),
            job_retention_seconds=args.job_retention_seconds,
            post_download_retention_seconds=args.post_download_retention_seconds,
            max_job_rows=args.max_job_rows,
            estimated_pdf_size_bytes=args.estimated_pdf_size_bytes,
            max_estimated_job_bytes=args.max_estimated_job_bytes,
            zip_part_max_files=args.zip_part_max_files,
            zip_part_max_bytes=args.zip_part_max_bytes,
        )
        return 0
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())

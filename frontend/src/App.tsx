import {
  ChangeEvent,
  DragEvent,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  ColumnDef,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import Papa from "papaparse";
import * as XLSX from "xlsx";

const DOI_REGEX = /^10\.\d{4,9}\/\S+$/i;
const MAX_PREVIEW_ROWS = 20;
const MAX_FILE_SIZE_BYTES = 250 * 1024 * 1024;
const API_BASE_URL = String(import.meta.env.VITE_API_BASE_URL ?? "")
  .trim()
  .replace(/\/+$/, "");

type MappingState = {
  doiColumn: string;
  shortIdColumn: string;
};

type ParsedRow = {
  rowNumber: number;
  values: Record<string, string>;
};

type ParsedDataset = {
  headers: string[];
  rows: ParsedRow[];
};

type InvalidRow = {
  rowNumber: number;
  doi: string;
  shortId: string;
  errors: string[];
  values: Record<string, string>;
};

type ValidRowPayload = {
  row_number: number;
  short_id: string;
  doi: string;
  title: string;
  name_hint: string;
  openaccess: string;
};

type PreviewRow = {
  rowNumber: number;
  doi: string;
  shortId: string;
  errors: string;
  isInvalid: boolean;
};

type ValidationResult = {
  sourceRows: number;
  totalRows: number;
  invalidRows: InvalidRow[];
  validRows: ValidRowPayload[];
  previewRows: PreviewRow[];
};

type FailedRecord = {
  status: string;
  row_number: number;
  short_id: string;
  doi: string;
  title: string;
  file_name: string;
  source_url: string;
  message: string;
};

type JobStatus = {
  job_id: string;
  status: "queued" | "running" | "cancelling" | "completed" | "failed" | "cancelled";
  error: string;
  cancel_requested: boolean;
  cancel_requested_at: string;
  can_cancel: boolean;
  files_deleted: boolean;
  files_deleted_at: string;
  expires_at: string;
  created_at: string;
  started_at: string;
  finished_at: string;
  counts: {
    total_requested: number;
    processed: number;
    pending: number;
    downloaded: number;
    failed: number;
    failed_network_or_lookup: number;
    skipped_no_pdf: number;
  };
  progress_percent: number;
  failed_records: FailedRecord[];
  failed_csv_url: string;
  report_csv_url: string;
  results_zip_url: string;
  results_zip_urls: string[];
  zip_parts_total: number;
};

function normalizeDoi(rawValue: string): string {
  let value = String(rawValue ?? "").trim();
  value = value.replace(/^doi:\s*/i, "");
  value = value.replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "");
  return value.trim();
}

function sanitizeCsvCell(value: string): string {
  const text = String(value ?? "");
  if (text.startsWith("=") || text.startsWith("+") || text.startsWith("-") || text.startsWith("@") || text.startsWith("\t") || text.startsWith("\r")) {
    return `'${text}`;
  }
  return text;
}

function csvEscape(value: string): string {
  const safeValue = sanitizeCsvCell(value);
  return `"${safeValue.replace(/"/g, '""')}"`;
}

function downloadCsv(filename: string, headers: string[], rows: string[][]): void {
  const lines: string[] = [];
  lines.push(headers.map((item) => csvEscape(item)).join(","));
  for (const row of rows) {
    lines.push(row.map((item) => csvEscape(item)).join(","));
  }

  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

function ensureUniqueHeaders(headers: string[]): string[] {
  const used = new Map<string, number>();
  const output: string[] = [];

  for (const original of headers) {
    const base = original.trim() || "column";
    const key = base.toLowerCase();
    const count = used.get(key) ?? 0;
    used.set(key, count + 1);
    output.push(count === 0 ? base : `${base}_${count + 1}`);
  }

  return output;
}

function deriveNameWithoutExtension(fileName: string): string {
  const index = fileName.lastIndexOf(".");
  if (index <= 0) {
    return fileName || "dataset";
  }
  return fileName.slice(0, index);
}

function formatTimestamp(value: string): string {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function apiUrl(path: string): string {
  if (!path) {
    return path;
  }
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  if (!API_BASE_URL) {
    return path.startsWith("/") ? path : `/${path}`;
  }
  return path.startsWith("/") ? `${API_BASE_URL}${path}` : `${API_BASE_URL}/${path}`;
}

function guessColumn(headers: string[], tokens: string[]): string {
  const lowerTokens = tokens.map((token) => token.toLowerCase());
  for (const header of headers) {
    const headerLower = header.toLowerCase();
    if (lowerTokens.some((token) => headerLower.includes(token))) {
      return header;
    }
  }
  return "";
}

async function parseCsvFile(file: File): Promise<ParsedDataset> {
  return new Promise((resolve, reject) => {
    Papa.parse<Record<string, unknown>>(file, {
      header: true,
      skipEmptyLines: "greedy",
      transformHeader: (header) => String(header ?? "").trim(),
      complete: (result) => {
        if (result.errors.length > 0) {
          reject(new Error(result.errors[0].message));
          return;
        }

        const rowsRaw = result.data ?? [];
        const metaFields = (result.meta.fields ?? []).map((field) => String(field).trim());
        let headers = ensureUniqueHeaders(metaFields.filter((field) => field.length > 0));

        if (headers.length === 0 && rowsRaw.length > 0) {
          headers = ensureUniqueHeaders(Object.keys(rowsRaw[0] ?? {}));
        }

        if (headers.length === 0) {
          reject(new Error("No columns found in CSV file."));
          return;
        }

        const rows: ParsedRow[] = [];
        for (let i = 0; i < rowsRaw.length; i += 1) {
          const row = rowsRaw[i] ?? {};
          const values: Record<string, string> = {};
          for (const header of headers) {
            values[header] = String(row[header] ?? "").trim();
          }
          const hasAnyValue = Object.values(values).some((value) => value.length > 0);
          if (!hasAnyValue) {
            continue;
          }
          rows.push({ rowNumber: i + 2, values });
        }

        resolve({ headers, rows });
      },
      error: (error) => {
        reject(error);
      },
    });
  });
}

async function parseExcelFile(file: File): Promise<ParsedDataset> {
  const buffer = await file.arrayBuffer();
  const workbook = XLSX.read(buffer, { type: "array" });

  if (workbook.SheetNames.length === 0) {
    throw new Error("Workbook has no sheets.");
  }

  const firstSheetName = workbook.SheetNames[0];
  const sheet = workbook.Sheets[firstSheetName];
  const matrix = XLSX.utils.sheet_to_json<(string | number | boolean | null)[]>(sheet, {
    header: 1,
    defval: "",
    blankrows: false,
  });

  if (matrix.length === 0) {
    throw new Error("Sheet is empty.");
  }

  const headerRow = matrix[0] ?? [];
  const headers = ensureUniqueHeaders(
    headerRow.map((item, index) => {
      const raw = String(item ?? "").trim();
      return raw || `column_${index + 1}`;
    }),
  );

  const rows: ParsedRow[] = [];
  for (let rowIndex = 1; rowIndex < matrix.length; rowIndex += 1) {
    const source = matrix[rowIndex] ?? [];
    const values: Record<string, string> = {};
    for (let columnIndex = 0; columnIndex < headers.length; columnIndex += 1) {
      values[headers[columnIndex]] = String(source[columnIndex] ?? "").trim();
    }
    const hasAnyValue = Object.values(values).some((value) => value.length > 0);
    if (!hasAnyValue) {
      continue;
    }
    rows.push({ rowNumber: rowIndex + 1, values });
  }

  return { headers, rows };
}

async function parseFile(file: File): Promise<ParsedDataset> {
  const fileName = file.name.toLowerCase();
  if (fileName.endsWith(".csv")) {
    return parseCsvFile(file);
  }
  if (fileName.endsWith(".xlsx")) {
    return parseExcelFile(file);
  }
  throw new Error("Unsupported file type. Please upload .csv or .xlsx");
}

function validateDataset(
  parsed: ParsedDataset,
  mapping: MappingState,
  startRow: number,
): ValidationResult {
  const selectedRows = parsed.rows.filter((row) => row.rowNumber >= startRow);
  const shortIdCounts = new Map<string, number>();

  for (const row of selectedRows) {
    const shortId = String(row.values[mapping.shortIdColumn] ?? "").trim();
    const key = shortId.toLowerCase();
    if (!key) {
      continue;
    }
    shortIdCounts.set(key, (shortIdCounts.get(key) ?? 0) + 1);
  }

  const invalidRows: InvalidRow[] = [];
  const validRows: ValidRowPayload[] = [];
  const previewRows: PreviewRow[] = [];

  for (const row of selectedRows) {
    const doi = normalizeDoi(String(row.values[mapping.doiColumn] ?? ""));
    const shortId = String(row.values[mapping.shortIdColumn] ?? "").trim();
    const shortIdKey = shortId.toLowerCase();
    const errors: string[] = [];

    if (!doi) {
      errors.push("missing DOI");
    } else if (!DOI_REGEX.test(doi)) {
      errors.push("invalid DOI format");
    }

    if (!shortId) {
      errors.push("missing short_id");
    } else if ((shortIdCounts.get(shortIdKey) ?? 0) > 1) {
      errors.push("duplicate short_id");
    }

    if (errors.length > 0) {
      invalidRows.push({
        rowNumber: row.rowNumber,
        doi,
        shortId,
        errors,
        values: row.values,
      });
    } else {
      validRows.push({
        row_number: row.rowNumber,
        short_id: shortId,
        doi,
        title: "untitled",
        name_hint: "",
        openaccess: "not_provided",
      });
    }

    if (previewRows.length < MAX_PREVIEW_ROWS) {
      previewRows.push({
        rowNumber: row.rowNumber,
        doi,
        shortId,
        errors: errors.join("; "),
        isInvalid: errors.length > 0,
      });
    }
  }

  return {
    sourceRows: parsed.rows.length,
    totalRows: selectedRows.length,
    invalidRows,
    validRows,
    previewRows,
  };
}

function DataTable<T extends object>({
  columns,
  data,
  rowClassName,
  emptyMessage,
}: {
  columns: ColumnDef<T>[];
  data: T[];
  rowClassName?: (row: T) => string;
  emptyMessage?: string;
}) {
  const table = useReactTable({
    columns,
    data,
    getCoreRowModel: getCoreRowModel(),
  });

  if (data.length === 0) {
    return <p className="table-empty">{emptyMessage ?? "No rows to display."}</p>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <th key={header.id}>
                  {header.isPlaceholder
                    ? null
                    : flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              className={rowClassName ? rowClassName(row.original) : ""}
            >
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id}>
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function App() {
  const [dragging, setDragging] = useState(false);
  const [fileName, setFileName] = useState("");
  const [isParsing, setIsParsing] = useState(false);
  const [isValidating, setIsValidating] = useState(false);
  const [isStartingJob, setIsStartingJob] = useState(false);
  const [isStoppingJob, setIsStoppingJob] = useState(false);

  const [error, setError] = useState("");
  const [jobError, setJobError] = useState("");

  const [parsed, setParsed] = useState<ParsedDataset | null>(null);
  const [mapping, setMapping] = useState<MappingState>({
    doiColumn: "",
    shortIdColumn: "",
  });
  const [startRow, setStartRow] = useState(2);
  const [validation, setValidation] = useState<ValidationResult | null>(null);

  const [jobId, setJobId] = useState("");
  const [job, setJob] = useState<JobStatus | null>(null);

  const previewColumns = useMemo<ColumnDef<PreviewRow>[]>(
    () => [
      {
        header: "Row",
        accessorKey: "rowNumber",
      },
      {
        header: "DOI",
        accessorKey: "doi",
      },
      {
        header: "short_id",
        accessorKey: "shortId",
      },
      {
        header: "Validation",
        accessorKey: "errors",
        cell: ({ row }) => {
          const value = row.original.errors;
          return value ? value : "valid";
        },
      },
    ],
    [],
  );

  const failedColumns = useMemo<ColumnDef<FailedRecord>[]>(
    () => [
      {
        header: "Row",
        accessorKey: "row_number",
      },
      {
        header: "DOI",
        accessorKey: "doi",
      },
      {
        header: "short_id",
        accessorKey: "short_id",
      },
      {
        header: "Reason",
        accessorKey: "message",
      },
    ],
    [],
  );

  const hasParsedData = parsed !== null;
  const canValidate =
    parsed !== null &&
    mapping.doiColumn.trim().length > 0 &&
    mapping.shortIdColumn.trim().length > 0 &&
    Number.isFinite(startRow) &&
    startRow >= 1;

  const isJobActive =
    job?.status === "queued" ||
    job?.status === "running" ||
    job?.status === "cancelling";
  const zipDownloadUrls =
    job && Array.isArray(job.results_zip_urls) && job.results_zip_urls.length > 0
      ? job.results_zip_urls
      : job?.results_zip_url
        ? [job.results_zip_url]
        : [];
  const zipPartsTotal = job?.zip_parts_total ?? zipDownloadUrls.length;

  useEffect(() => {
    if (!jobId) {
      return;
    }

    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const response = await fetch(apiUrl(`/api/jobs/${jobId}`));
        const payload = (await response.json()) as JobStatus | { error?: string };
        if (!response.ok) {
          throw new Error(payload.error || "Unable to fetch job status");
        }

        if (cancelled) {
          return;
        }

        const jobPayload = payload as JobStatus;
        setJob(jobPayload);
        setJobError("");

        if (
          jobPayload.status === "queued" ||
          jobPayload.status === "running" ||
          jobPayload.status === "cancelling"
        ) {
          timer = window.setTimeout(() => {
            void poll();
          }, 2000);
        }
      } catch (pollError) {
        if (cancelled) {
          return;
        }
        setJobError((pollError as Error).message);
        timer = window.setTimeout(() => {
          void poll();
        }, 4000);
      }
    };

    void poll();

    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [jobId]);

  async function handleIncomingFile(file: File): Promise<void> {
    setError("");
    setJobError("");

    const lowered = file.name.toLowerCase();
    if (!lowered.endsWith(".csv") && !lowered.endsWith(".xlsx")) {
      setError("Unsupported file type. Please upload .csv or .xlsx.");
      return;
    }

    if (file.size > MAX_FILE_SIZE_BYTES) {
      setError("File is too large. Maximum size is 250MB.");
      return;
    }

    setIsParsing(true);
    try {
      const parsedData = await parseFile(file);
      const doiColumn = guessColumn(parsedData.headers, ["doi"]);
      const shortIdColumn = guessColumn(parsedData.headers, [
        "short_id",
        "shortid",
        "short id",
        "id",
      ]);

      setFileName(file.name);
      setParsed(parsedData);
      setMapping({ doiColumn, shortIdColumn });
      setStartRow(2);
      setValidation(null);
      setJobId("");
      setJob(null);
    } catch (parseError) {
      setError((parseError as Error).message);
    } finally {
      setIsParsing(false);
    }
  }

  function handleFileInput(event: ChangeEvent<HTMLInputElement>): void {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    void handleIncomingFile(file);
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLDivElement>): void {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (!file) {
      return;
    }
    void handleIncomingFile(file);
  }

  function runValidation(): void {
    if (!parsed) {
      return;
    }
    if (!Number.isFinite(startRow) || startRow < 1) {
      setError("Start row must be a number greater than or equal to 1.");
      return;
    }
    const normalizedStartRow = Math.floor(startRow);
    setStartRow(normalizedStartRow);
    setError("");
    setIsValidating(true);
    setTimeout(() => {
      const result = validateDataset(parsed, mapping, normalizedStartRow);
      setValidation(result);
      setJobId("");
      setJob(null);
      setJobError("");
      setIsStoppingJob(false);
      setIsValidating(false);
    }, 0);
  }

  function exportInvalidRows(): void {
    if (!validation || !parsed) {
      return;
    }

    const headers = [
      "row_number",
      "doi",
      "short_id",
      "errors",
      ...parsed.headers,
    ];
    const rows = validation.invalidRows.map((row) => {
      const rowValues = parsed.headers.map((header) => row.values[header] ?? "");
      return [
        String(row.rowNumber),
        row.doi,
        row.shortId,
        row.errors.join("; "),
        ...rowValues,
      ];
    });

    const exportName = `${deriveNameWithoutExtension(fileName)}_invalid_rows.csv`;
    downloadCsv(exportName, headers, rows);
  }

  async function startDownloadJob(): Promise<void> {
    if (!validation || validation.validRows.length === 0) {
      setError("No valid rows available to download.");
      return;
    }

    setIsStartingJob(true);
    setError("");
    setJobError("");
    try {
      const response = await fetch(apiUrl("/api/jobs"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ rows: validation.validRows }),
      });

      const payload = (await response.json()) as
        | { job_id: string }
        | { error: string; invalid_rows?: Array<{ row_number: number; errors: string[] }> };

      if (!response.ok || "error" in payload) {
        throw new Error((payload as { error: string }).error || "Failed to start download job.");
      }

      setJobId(payload.job_id);
      setJob(null);
      setIsStoppingJob(false);
    } catch (startError) {
      setError((startError as Error).message);
    } finally {
      setIsStartingJob(false);
    }
  }

  async function stopDownloadJob(): Promise<void> {
    if (!jobId) {
      return;
    }
    setIsStoppingJob(true);
    setError("");
    setJobError("");
    try {
      const response = await fetch(apiUrl(`/api/jobs/${jobId}/cancel`), {
        method: "POST",
      });
      const payload = (await response.json()) as JobStatus | { error?: string };
      if (!response.ok) {
        throw new Error(payload.error || "Failed to stop download job.");
      }
      setJob(payload as JobStatus);
    } catch (stopError) {
      setJobError((stopError as Error).message);
    } finally {
      setIsStoppingJob(false);
    }
  }

  function refreshJobAfterDownload(): void {
    if (!jobId) {
      return;
    }
    window.setTimeout(() => {
      void fetch(apiUrl(`/api/jobs/${jobId}`))
        .then(async (response) => {
          const payload = (await response.json()) as JobStatus | { error?: string };
          if (!response.ok) {
            throw new Error(payload.error || "Unable to refresh job status.");
          }
          setJob(payload as JobStatus);
        })
        .catch((refreshError: unknown) => {
          setJobError((refreshError as Error).message);
        });
    }, 1200);
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <h1>DOI Open Access PDF Downloader</h1>
        <p>
          Upload a CSV or Excel file, map DOI and short ID columns, validate data,
          then run and monitor a batch download job.
        </p>
        <p className="secondary">
          Downloads run on the server with temporary storage only. PDFs are deleted
          after a short expiry window and are intended to be saved on the user's device.
        </p>
      </header>

      <section className="card">
        <h2>1. Upload File</h2>
        <div
          className={`dropzone ${dragging ? "dragging" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
        >
          <p>Drag and drop a `.csv` or `.xlsx` file here</p>
          <p className="secondary">or</p>
          <label className="button" htmlFor="file-input">
            Choose File
          </label>
          <input
            id="file-input"
            type="file"
            accept=".csv,.xlsx"
            onChange={handleFileInput}
          />
          {isParsing ? <p className="secondary">Parsing file...</p> : null}
          {fileName ? <p className="secondary">Loaded: {fileName}</p> : null}
        </div>
      </section>

      {hasParsedData ? (
        <section className="card">
          <h2>2. Map Columns</h2>
          <p className="secondary">Detected columns: {parsed.headers.join(", ")}</p>

          <div className="mapping-grid">
            <label>
              DOI column
              <select
                value={mapping.doiColumn}
                onChange={(event) =>
                  setMapping((current) => ({
                    ...current,
                    doiColumn: event.target.value,
                  }))
                }
              >
                <option value="">Select column</option>
                {parsed.headers.map((header) => (
                  <option key={`doi-${header}`} value={header}>
                    {header}
                  </option>
                ))}
              </select>
            </label>

            <label>
              short_id column
              <select
                value={mapping.shortIdColumn}
                onChange={(event) =>
                  setMapping((current) => ({
                    ...current,
                    shortIdColumn: event.target.value,
                  }))
                }
              >
                <option value="">Select column</option>
                {parsed.headers.map((header) => (
                  <option key={`id-${header}`} value={header}>
                    {header}
                  </option>
                ))}
              </select>
            </label>

            <label>
              Start downloads from row
              <input
                type="number"
                min={1}
                step={1}
                value={startRow}
                onChange={(event) => {
                  const parsedValue = Number(event.target.value);
                  setStartRow(Number.isFinite(parsedValue) ? parsedValue : 1);
                  setValidation(null);
                }}
              />
            </label>
          </div>

          <button
            className="button"
            disabled={!canValidate || isValidating}
            onClick={runValidation}
            type="button"
          >
            {isValidating ? "Validating..." : "3. Preview and Validate"}
          </button>
        </section>
      ) : null}

      {validation ? (
        <section className="card">
          <h2>3. Validation Results</h2>
          <div className="stats-grid">
            <div>
              <span className="metric-label">Rows in file</span>
              <strong>{validation.sourceRows}</strong>
            </div>
            <div>
              <span className="metric-label">Rows selected</span>
              <strong>{validation.totalRows}</strong>
            </div>
            <div>
              <span className="metric-label">Invalid rows</span>
              <strong>{validation.invalidRows.length}</strong>
            </div>
            <div>
              <span className="metric-label">Valid rows</span>
              <strong>{validation.validRows.length}</strong>
            </div>
          </div>

          <DataTable
            columns={previewColumns}
            data={validation.previewRows}
            rowClassName={(row) => (row.isInvalid ? "row-invalid" : "")}
            emptyMessage="No rows in preview."
          />
          {validation.totalRows === 0 ? (
            <p className="secondary">
              No rows are available at or after the selected start row.
            </p>
          ) : null}

          {validation.invalidRows.length > 0 ? (
            <button className="button secondary-btn" onClick={exportInvalidRows} type="button">
              Export Invalid Rows as CSV
            </button>
          ) : null}
        </section>
      ) : null}

      {validation ? (
        <section className="card">
          <h2>4. Start Download Job</h2>
          <button
            className="button"
            disabled={isStartingJob || validation.validRows.length === 0 || isJobActive}
            onClick={() => {
              void startDownloadJob();
            }}
            type="button"
          >
            {isStartingJob ? "Starting Job..." : "Start Batch Download"}
          </button>
        </section>
      ) : null}

      {job ? (
        <section className="card">
          <h2>5. Job Progress</h2>
          <div className="stats-grid">
            <div>
              <span className="metric-label">Total requested</span>
              <strong>{job.counts.total_requested}</strong>
            </div>
            <div>
              <span className="metric-label">Downloaded</span>
              <strong>{job.counts.downloaded}</strong>
            </div>
            <div>
              <span className="metric-label">Failed</span>
              <strong>{job.counts.failed}</strong>
            </div>
            <div>
              <span className="metric-label">Pending</span>
              <strong>{job.counts.pending}</strong>
            </div>
          </div>

          <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={job.progress_percent}>
            <div className="progress-bar" style={{ width: `${job.progress_percent}%` }} />
          </div>
          <p className="secondary">Status: {job.status} ({job.progress_percent}%)</p>
          {job.expires_at ? (
            <p className="secondary">
              Temporary server files expire at: {formatTimestamp(job.expires_at)}
            </p>
          ) : null}
          {job.files_deleted ? (
            <p className="secondary">
              Server files were deleted at {formatTimestamp(job.files_deleted_at)}.
            </p>
          ) : null}
          {job.status === "cancelled" ? (
            <p className="secondary">
              Download was stopped by user request. Partial results remain available.
            </p>
          ) : null}
          {job.status === "queued" || job.status === "running" || job.status === "cancelling" ? (
            <button
              className="button danger-btn"
              disabled={isStoppingJob || !job.can_cancel}
              onClick={() => {
                void stopDownloadJob();
              }}
              type="button"
            >
              {isStoppingJob || job.status === "cancelling" ? "Stopping..." : "Stop Download Process"}
            </button>
          ) : null}
        </section>
      ) : null}

      {job && job.failed_records.length > 0 ? (
        <section className="card">
          <h2>Failed Downloads</h2>
          <DataTable columns={failedColumns} data={job.failed_records} emptyMessage="No failed downloads." />
          {job.failed_csv_url ? (
            <a className="button secondary-btn" href={apiUrl(job.failed_csv_url)}>
              Export Failed DOIs as CSV
            </a>
          ) : null}
        </section>
      ) : null}

      {job && (job.status === "completed" || job.status === "failed" || job.status === "cancelled") ? (
        <section className="card">
          <h2>6. Completion Summary</h2>
          <div className="stats-grid">
            <div>
              <span className="metric-label">Total requested</span>
              <strong>{job.counts.total_requested}</strong>
            </div>
            <div>
              <span className="metric-label">Successfully downloaded</span>
              <strong>{job.counts.downloaded}</strong>
            </div>
            <div>
              <span className="metric-label">Failed</span>
              <strong>{job.counts.failed}</strong>
            </div>
          </div>

          <div className="summary-links">
            {job.report_csv_url ? (
              <a className="button secondary-btn" href={apiUrl(job.report_csv_url)}>
                Download Full Report CSV
              </a>
            ) : null}
            {zipDownloadUrls.length > 0
              ? zipDownloadUrls.map((zipUrl, index) => (
                  <a
                    key={zipUrl}
                    className="button"
                    href={apiUrl(zipUrl)}
                    onClick={refreshJobAfterDownload}
                  >
                    {zipDownloadUrls.length > 1
                      ? `Download ZIP Part ${index + 1}`
                      : "Download ZIP to Device"}
                  </a>
                ))
              : null}
          </div>
          {zipPartsTotal > 1 ? (
            <p className="secondary">
              Large result set detected. Download all {zipPartsTotal} ZIP parts.
            </p>
          ) : null}
          <p className="secondary">
            ZIP files may be split into parts for large jobs. Server files remain
            available only for a short retry window.
          </p>
        </section>
      ) : null}

      {error ? <p className="error-text">{error}</p> : null}
      {jobError ? <p className="error-text">{jobError}</p> : null}
      {job?.error ? <p className="error-text">{job.error}</p> : null}
    </main>
  );
}

# DOI-based Open Access PDF Downloader

This project now includes:
- A Python backend API and CLI in `download_papers_doi.py`
- A React + TypeScript frontend in `frontend/`

## Backend

Run the API server:

```bash
python3 download_papers_doi.py --serve --host 127.0.0.1 --port 8000
```

Key endpoints:
- `POST /api/jobs` start batch job with validated rows
- `POST /api/jobs/{job_id}/cancel` request job cancellation
- `GET /api/jobs/{job_id}` poll job progress
- `GET /api/jobs/{job_id}/failed.csv` export failed rows
- `GET /api/jobs/{job_id}/report.csv` export full report
- `GET /api/jobs/{job_id}/results.zip` download results ZIP (supports `?part=N` for large jobs)

Notes for deployment:
- Downloads are performed on the backend server, not in the browser.
- API mode uses temporary server storage and auto-deletes artifacts after retention expiry.
- Large result sets are automatically split into ZIP parts.
- After each ZIP part download, a retry window is extended (short-term retention).
- `report.csv` and `failed.csv` are generated from in-memory job records.
- Capacity safeguards reject jobs that exceed configured row or estimated-size limits.

Important API tunables:
- `--job-retention-seconds` (default `900`)
- `--post-download-retention-seconds` (default `1200`)
- `--max-job-rows` (default `50000`)
- `--estimated-pdf-size-bytes` (default `2097152`)
- `--max-estimated-job-bytes` (default `8589934592`)
- `--zip-part-max-files` (default `200`)
- `--zip-part-max-bytes` (default `629145600`)

CLI mode still works:

```bash
python3 download_papers_doi.py --csv-file speedboat_leuphana_sample.csv
```

## Frontend

Install and run:

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api/*` to `http://127.0.0.1:8000`.

For production, set `VITE_API_BASE_URL` to your backend base URL.

## Deploy With GitHub + Render (Recommended)

This repo includes `render.yaml` for a smooth Blueprint deploy:
- `doi-downloader-api` (Python web service)
- `doi-downloader-frontend` (static site)

### Steps

1. Push this project to a GitHub repository.
2. In Render, choose **New +** -> **Blueprint**.
3. Connect the GitHub repo and select this repository.
4. Render reads `render.yaml` and creates both services.
5. Wait for both deploys to finish, then share the frontend URL with your team.

### Why this is safe for your storage requirement

- Backend files are temporary only.
- ZIPs can be downloaded in parts for large jobs.
- Retention auto-cleanup is configured via `DOI_JOB_RETENTION_SECONDS` (default `900`).
- Post-download retry window is configured via `DOI_POST_DOWNLOAD_RETENTION_SECONDS` (default `1200`).
- No persistent disk is required.

## Frontend flow

1. Upload `.csv` or `.xlsx`
2. Map `DOI` and `short_id` columns
3. Choose optional start row (for example row `100`)
4. Preview and validate rows (missing DOI/short_id, DOI format, duplicate short_id)
5. Export invalid rows CSV if needed
6. Start job and monitor progress
7. Review failed downloads and export failures/report/ZIP parts

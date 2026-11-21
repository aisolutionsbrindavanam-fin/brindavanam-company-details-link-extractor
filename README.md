# Bank Statement Company Name Extractor

A local desktop application that reads bank statements and returns a deduplicated
list of company-like names. It does not include transaction dates, amounts,
account numbers, or descriptions in the results.

## Supported files

- PDF (text-based/selectable PDFs)
- CSV
- TXT
- XLSX

Scanned/image-only PDFs are not OCR'd; convert them to searchable PDFs before
processing. Name detection uses business/legal-form keywords, so review the
results before relying on them as verified legal entity names.

## Run

1. Install Python 3.10 or newer.
2. Open a terminal in this folder and run:

   ```powershell
   python -m pip install -r requirements.txt
   python bank_company_extractor.py
   ```

3. Choose one or more statements, select **Extract names**, then **Save names**
   to export a TXT or CSV containing names only.

All processing is performed locally on your computer.

## Company directory dashboard

The optional Streamlit dashboard supports A–Z listings from `companydetails.in`
and numeric `pageno` listings from AEPC India. It exports company names and PIN
codes, shows live results, and filters configured excluded business categories.
The Excel workbook includes an all-companies sheet and A–Z sheets; the ZIP
download contains CSVs grouped by company initial. Requests are sequential and
rate-limited, and duplicate companies/pages are skipped. A best-effort mode also
accepts other public HTTPS directory URLs and can follow numbered query
parameters, A–Z path routes, or same-host Next links. It looks for common table
rows and company cards containing a name and a six-digit PIN; layouts that need
JavaScript, authentication, or unusual markup may not work. Custom crawls are
restricted to the same public host, limited to 50 pages and 5 MB per page, and
do not bypass CAPTCHAs or access controls. Use each directory in accordance
with its terms.

Run it from this folder:

```powershell
python -m pip install -r requirements.txt
python -m streamlit run directory_app.py
```

For a reliable one-click launch, double-click `Start Company PIN Dashboard.bat`
in this folder. It starts the server in a minimized terminal, waits until the
health check succeeds, and then opens the browser. If the dashboard is already
running, it just opens the existing server.

VS Code can also start the app automatically when this folder opens using
`.vscode/tasks.json`. Approve the **Open Company & PIN Dashboard** automatic
task when prompted. If it does not run, use **Terminal: Manage Automatic Tasks
in Folder** from the Command Palette and allow them. Closing VS Code stops the
VS Code-managed server; use the batch launcher if automatic tasks are disabled.

Choose a supported source in the dashboard. For AEPC India, paste a members
listing URL such as the Tamil Nadu filter and set a maximum number of numeric
pages to fetch. The URL's `pageno` is used as the starting page. Request errors
are shown in the dashboard; results may be incomplete if some pages fail.

### Deploy to Render

The included `render.yaml` configures a free Python web service. To publish it:

1. Push this project to a GitHub repository.
2. In Render, choose **New → Blueprint**, connect the repository, and deploy
   the `company-pin-extractor` service.
3. Share the generated `https://...onrender.com` URL with employees.

If creating a Render web service manually with the **Docker** environment,
keep the included root-level `Dockerfile` in the repository. Render's Docker
build fails if that file is missing.

The app currently has no sign-in, so anyone who gets the URL can use it. Free
Render services may sleep when idle and take longer to open on the first visit.
For a desktop shortcut, open the deployed URL in Edge and choose **Apps →
Install this site as an app** (or create a browser shortcut) on each PC.

## Tests

Run the built-in tests with:

```powershell
python -m unittest discover -s tests -v
```

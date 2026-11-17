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

The optional Streamlit dashboard crawls company listing pages from
`companydetails.in` for a district slug and exports company names and PIN codes
to Excel or CSV. It shows a live preview and per-letter counts while crawling.
The Excel workbook includes an all-companies sheet and separate A–Z sheets; a
ZIP download contains one CSV for each letter with results. Requests are
sequential and rate-limited; use the dashboard in accordance with the
directory's terms.

Run it from this folder:

```powershell
python -m pip install -r requirements.txt
python -m streamlit run directory_app.py
```

Enter a district slug such as `tamil-nadu-dindigul` and choose the page limit
before starting the A-to-Z crawl. Request errors are shown in the dashboard;
results may be incomplete if some pages fail.

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

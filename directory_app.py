import re
import string
import time
from io import BytesIO
from urllib.parse import urlparse
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://www.companydetails.in/companies_district_wise"
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
PINCODE_PATTERN = re.compile(r"\b([1-9]\d{5})\b")
REQUEST_DELAY_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 30


def is_valid_district_slug(slug):
    return bool(SLUG_PATTERN.fullmatch(slug.strip()))


def district_slug_from_input(value):
    value = value.strip()
    if "://" not in value:
        return value if is_valid_district_slug(value) else None

    parsed_url = urlparse(value)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc.lower() not in {"companydetails.in", "www.companydetails.in"}
    ):
        return None

    path_parts = [part for part in parsed_url.path.split("/") if part]
    if (
        len(path_parts) not in (2, 3)
        or path_parts[0] != "companies_district_wise"
        or (len(path_parts) == 3 and not re.fullmatch(r"[A-Za-z]", path_parts[2]))
        or not is_valid_district_slug(path_parts[1])
    ):
        return None
    return path_parts[1]


def parse_company_rows(html, letter):
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for heading in soup.find_all("h3"):
        link = heading.find("a", href=re.compile(r"^/company/"))
        if not link:
            continue

        company_name = link.get_text(" ", strip=True)
        if not company_name:
            continue

        address = heading.find_next("h6")
        address_text = address.get_text(" ", strip=True) if address else ""
        pin_match = PINCODE_PATTERN.search(address_text)
        records.append(
            {
                "Company_Name": company_name,
                "Pincode": pin_match.group(1) if pin_match else "N/A",
                "Alphabet_Index": letter,
            }
        )

    if records:
        return records

    for row in soup.find_all("tr"):
        columns = row.find_all("td")
        if len(columns) < 2:
            continue

        company_name = columns[0].get_text(" ", strip=True)
        if not company_name or company_name.upper() == "COMPANY NAME":
            continue

        text_block = row.get_text(" ", strip=True)
        pin_match = PINCODE_PATTERN.search(text_block)
        records.append(
            {
                "Company_Name": company_name,
                "Pincode": pin_match.group(1) if pin_match else "N/A",
                "Alphabet_Index": letter,
            }
        )

    return records


def crawl_district(
    session,
    district_slug,
    max_pages_per_letter,
    progress_callback=None,
    page_callback=None,
):
    records = []
    errors = []
    consecutive_errors = 0

    for letter_index, letter in enumerate(string.ascii_uppercase):
        for page_num in range(1, max_pages_per_letter + 1):
            url = f"{BASE_URL}/{district_slug}/{letter}"
            params = {"page": page_num} if page_num > 1 else None

            try:
                response = session.get(
                    url,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                time.sleep(REQUEST_DELAY_SECONDS)
                if response.status_code == 404:
                    consecutive_errors = 0
                    break
                response.raise_for_status()
            except requests.RequestException as error:
                errors.append(f"{letter}, page {page_num}: {error}")
                time.sleep(REQUEST_DELAY_SECONDS)
                consecutive_errors += 1
                break

            consecutive_errors = 0
            page_records = parse_company_rows(response.text, letter)
            if not page_records:
                break
            records.extend(page_records)
            if page_callback:
                page_callback(letter, page_num, page_records, len(records))

        if progress_callback:
            progress_callback((letter_index + 1) / len(string.ascii_uppercase), letter)
        if consecutive_errors >= 3:
            break

    return records, errors


def create_excel_export(df_result):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_result.to_excel(writer, index=False, sheet_name="All Companies")
        for letter in string.ascii_uppercase:
            letter_df = df_result[df_result["Alphabet_Index"] == letter]
            letter_df.to_excel(writer, index=False, sheet_name=letter)
    return output.getvalue()


def create_csv_archive(df_result):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for letter in string.ascii_uppercase:
            letter_df = df_result[df_result["Alphabet_Index"] == letter]
            if not letter_df.empty:
                archive.writestr(
                    f"{letter}.csv",
                    letter_df.to_csv(index=False).encode("utf-8-sig"),
                )
    return output.getvalue()


def main():
    st.set_page_config(
        page_title="Company & PIN Code Extractor",
        page_icon="🏢",
        layout="wide",
    )
    st.title("🏢 Automated Company & PIN Code Scraper Dashboard")
    st.markdown(
        "Enter a district directory slug to crawl its A–Z pages, extract company "
        "names and Indian PIN codes, and export the results."
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        district_input = st.text_input(
            "District Slug or Directory URL",
            value="tamil-nadu-dindigul",
            help=(
                "Enter a slug such as tamil-nadu-dindigul or paste a directory URL "
                "ending in its district and optional letter."
            ),
        ).strip()
    with col2:
        max_pages_per_letter = st.number_input(
            "Max Pages Per Letter",
            min_value=1,
            max_value=50,
            value=15,
            help="Caps pagination to avoid unexpectedly large crawls.",
        )

    st.caption(
        "Pages are fetched one at a time with a delay between successful requests. "
        "Check the directory's terms before use."
    )

    if not st.button("🚀 Start Scraping A to Z", type="primary"):
        return
    district_slug = district_slug_from_input(district_input)
    if district_slug is None:
        st.error(
            "Enter a valid district slug or an HTTPS companydetails.in district directory URL."
        )
        return

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    all_records = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    live_count = st.empty()
    live_summary = st.empty()
    live_preview = st.empty()
    letter_counts = {letter: 0 for letter in string.ascii_uppercase}
    live_records = []

    def update_progress(progress, letter):
        progress_bar.progress(progress)
        status_text.text(f"Finished letter [{letter}].")

    def update_page(letter, page_num, page_records, total_count):
        live_records.extend(page_records)
        letter_counts[letter] += len(page_records)
        status_text.text(
            f"Extracted {letter_counts[letter]} companies for [{letter}] "
            f"through page {page_num}."
        )
        live_count.metric("Companies found so far", total_count)
        live_summary.dataframe(
            pd.DataFrame(
                [
                    {"Letter": key, "Companies": count}
                    for key, count in letter_counts.items()
                    if count
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
        live_preview.dataframe(
            pd.DataFrame(live_records[-25:]),
            hide_index=True,
            use_container_width=True,
        )

    with requests.Session() as session:
        session.headers.update(headers)
        retry = Retry(
            total=2,
            connect=2,
            read=0,
            status=2,
            backoff_factor=1,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        all_records, errors = crawl_district(
            session,
            district_slug,
            int(max_pages_per_letter),
            update_progress,
            update_page,
        )

    status_text.text("Scraping completed.")
    if errors:
        st.warning(
            f"Some pages could not be fetched ({len(errors)} error(s)); "
            "the displayed results may be incomplete."
        )
        with st.expander("Request errors"):
            for error in errors:
                st.text(error)

    df_result = pd.DataFrame(all_records)
    if df_result.empty:
        st.error("No records were found. Check the district slug or try again later.")
        return

    df_result = df_result.drop_duplicates().reset_index(drop=True)
    st.success(f"Extracted {len(df_result)} unique company records.")
    st.dataframe(df_result, use_container_width=True)

    st.subheader("Results by alphabet")
    st.dataframe(
        df_result.groupby("Alphabet_Index").size().reindex(list(string.ascii_uppercase), fill_value=0)
        .rename("Companies")
        .rename_axis("Letter")
        .reset_index(),
        hide_index=True,
        use_container_width=True,
    )

    excel_data = create_excel_export(df_result)
    alphabet_csv_data = create_csv_archive(df_result)
    csv_data = df_result.to_csv(index=False).encode("utf-8-sig")

    col_dl1, col_dl2, col_dl3 = st.columns(3)
    with col_dl1:
        st.download_button(
            "📥 Excel workbook (A–Z sheets)",
            excel_data,
            f"{district_slug}_companies_pincodes.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with col_dl2:
        st.download_button(
            "📥 Per-letter CSV files (.zip)",
            alphabet_csv_data,
            f"{district_slug}_companies_by_letter.zip",
            "application/zip",
        )
    with col_dl3:
        st.download_button(
            "📥 All companies as CSV",
            csv_data,
            f"{district_slug}_companies_pincodes.csv",
            "text/csv",
        )


if __name__ == "__main__":
    main()

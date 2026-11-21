import hashlib
import re
import string
import time
from io import BytesIO
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://www.companydetails.in/companies_district_wise"
AEPC_HOSTS = {"aepcindia.com", "www.aepcindia.com"}
AEPC_PATH = "/members/"
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
PINCODE_PATTERN = re.compile(r"\b([1-9]\d{5})\b")
EXCLUDED_COMPANY_KEYWORDS = (
    "FINANCE",
    "INVESTMENT",
    "FUND",
    "CHIT",
    "NIDHI",
    "CREDIT",
    "ASSET MANAGEMENT",
    "MONEY",
    "FOREX",
    "STOCK",
    "BANK",
    "INSURANCE",
    "ASSURANCE",
    "GOVT",
    "POST",
    "SHOP",
    "NURSERY",
    "FIN",
    "MAHAL",
    "MANDAPAM",
    "BROKERAGE",
    "MEDIA",
    "LAW AND FIRM",
    "TRUST",
    "CHARITY",
    "ACCOUNTS",
    "TAX",
    "HOLDINGS",
    "EMBROIDERY",
    "FOUNDATION",
    "AGRICULTURAL FARMERS",
    "ASSOCIATION",
    "NURSING",
    "CHARITABLE",
    "GOVT COMPANIES",
)
REQUEST_DELAY_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 30


def normalize_company_name(name):
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


NORMALIZED_EXCLUDED_KEYWORDS = tuple(
    normalize_company_name(keyword) for keyword in EXCLUDED_COMPANY_KEYWORDS
)


def is_excluded_company(name):
    normalized_name = f" {normalize_company_name(name)} "
    return any(
        f" {keyword} " in normalized_name
        for keyword in NORMALIZED_EXCLUDED_KEYWORDS
    )


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


def aepc_url_from_input(value):
    parsed_url = urlparse(value.strip())
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc.lower() not in AEPC_HOSTS
        or parsed_url.path.rstrip("/") != AEPC_PATH.rstrip("/")
    ):
        return None

    query = parse_qs(parsed_url.query, keep_blank_values=True)
    page_values = query.get("pageno", ["1"])
    if len(page_values) != 1 or not page_values[0].isdigit():
        return None

    return urlunparse(parsed_url._replace(path=AEPC_PATH))


def aepc_page_url(start_url, page_num):
    parsed_url = urlparse(start_url)
    query = parse_qs(parsed_url.query, keep_blank_values=True)
    query["pageno"] = [str(page_num)]
    return urlunparse(parsed_url._replace(query=urlencode(query, doseq=True)))


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


def parse_aepc_page(html, page_num):
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for card in soup.select(".views-row"):
        name_element = card.select_one(".market_title")
        if not name_element:
            continue

        company_name = name_element.get_text(" ", strip=True)
        if not company_name:
            continue

        city_pin = card.select_one(".market_city_pin")
        pin_match = PINCODE_PATTERN.search(
            city_pin.get_text(" ", strip=True) if city_pin else ""
        )
        first_letter = next(
            (character.upper() for character in company_name if character.isalpha()),
            "0-9",
        )
        records.append(
            {
                "Company_Name": company_name,
                "Pincode": pin_match.group(1) if pin_match else "N/A",
                "Alphabet_Index": first_letter,
                "Page_Number": page_num,
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
    seen_company_keys = set()

    for letter_index, letter in enumerate(string.ascii_uppercase):
        seen_page_fingerprints = set()
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
            page_fingerprint = hashlib.sha256(
                response.text.encode("utf-8")
            ).digest()
            if page_fingerprint in seen_page_fingerprints:
                if page_callback:
                    page_callback(letter, page_num, [], len(records))
                break
            seen_page_fingerprints.add(page_fingerprint)

            page_records = parse_company_rows(response.text, letter)
            if not page_records:
                if page_callback:
                    page_callback(letter, page_num, [], len(records))
                break

            new_source_records = []
            new_records = []
            for record in page_records:
                company_key = (
                    normalize_company_name(record["Company_Name"]),
                    record["Pincode"],
                )
                if company_key not in seen_company_keys:
                    seen_company_keys.add(company_key)
                    new_source_records.append(record)
                    if not is_excluded_company(record["Company_Name"]):
                        new_records.append(record)

            records.extend(new_records)
            if page_callback:
                page_callback(letter, page_num, new_records, len(records))
            if not new_source_records:
                break

        if progress_callback:
            progress_callback((letter_index + 1) / len(string.ascii_uppercase), letter)
        if consecutive_errors >= 3:
            break

    return records, errors


def crawl_aepc_directory(
    session,
    start_url,
    max_pages,
    progress_callback=None,
    page_callback=None,
):
    parsed_url = urlparse(start_url)
    query = parse_qs(parsed_url.query)
    first_page = int(query.get("pageno", ["1"])[0])
    records = []
    errors = []
    seen_company_keys = set()
    seen_page_fingerprints = set()

    for page_offset in range(max_pages):
        page_num = first_page + page_offset
        url = aepc_page_url(start_url, page_num)

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if response.status_code == 404:
                break
            response.raise_for_status()
        except requests.RequestException as error:
            errors.append(f"Page {page_num}: {error}")
            break

        page_fingerprint = hashlib.sha256(response.text.encode("utf-8")).digest()
        if page_fingerprint in seen_page_fingerprints:
            break
        seen_page_fingerprints.add(page_fingerprint)

        page_records = parse_aepc_page(response.text, page_num)
        if not page_records:
            break

        new_source_records = []
        new_records = []
        for record in page_records:
            company_key = (
                normalize_company_name(record["Company_Name"]),
                record["Pincode"],
            )
            if company_key in seen_company_keys:
                continue
            seen_company_keys.add(company_key)
            new_source_records.append(record)
            if not is_excluded_company(record["Company_Name"]):
                new_records.append(record)

        records.extend(new_records)
        if page_callback:
            page_callback(page_num, new_records, len(records))
        if progress_callback:
            progress_callback((page_offset + 1) / max_pages, page_num)
        if not new_source_records:
            break

    return records, errors


def create_excel_export(df_result):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_result.to_excel(writer, index=False, sheet_name="All Companies")
        for letter in ["0-9", *string.ascii_uppercase]:
            letter_df = df_result[df_result["Alphabet_Index"] == letter]
            letter_df.to_excel(writer, index=False, sheet_name=letter)
    return output.getvalue()


def create_csv_archive(df_result):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for letter in ["0-9", *string.ascii_uppercase]:
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
        "Extract company names and Indian PIN codes from supported directory pages. "
        "Other websites require a site-specific adapter."
    )

    source_type = st.radio(
        "Directory source",
        ("CompanyDetails.in (A–Z)", "AEPC India (numbered pages)"),
        horizontal=True,
    )
    if source_type == "CompanyDetails.in (A–Z)":
        col1, col2 = st.columns([2, 1])
        with col1:
            district_input = st.text_input(
                "District Slug or Directory URL",
                value="tamil-nadu-dindigul",
                help=(
                    "Enter a slug such as tamil-nadu-dindigul or paste a "
                    "companydetails.in district directory URL."
                ),
            ).strip()
        with col2:
            max_pages = st.number_input(
                "Max Pages Per Letter",
                min_value=1,
                max_value=50,
                value=15,
                help="Caps pagination depth for each A–Z letter.",
            )
        district_slug = district_slug_from_input(district_input)
        aepc_start_url = None
        crawl_button_label = "🚀 Start Scraping A to Z"
    else:
        col1, col2 = st.columns([2, 1])
        with col1:
            aepc_input = st.text_input(
                "AEPC Members Directory URL",
                value=(
                    "https://www.aepcindia.com/members/"
                    "?search_field=field_state&views_exposed_field_filter="
                    "Tamil+Nadu&pageno=1"
                ),
                help="Paste an AEPC members-directory page URL. Its pageno is used as the starting page.",
            ).strip()
        with col2:
            max_pages = st.number_input(
                "Maximum Number of Pages",
                min_value=1,
                max_value=100,
                value=10,
                help="Caps the number of sequential pageno pages to fetch.",
            )
        aepc_start_url = aepc_url_from_input(aepc_input)
        district_slug = None
        crawl_button_label = "🚀 Start Scraping Numbered Pages"

    st.caption(
        "Pages are fetched one at a time with a delay between successful requests. "
        "Companies matching the excluded finance, government, charity, and other "
        "business categories are omitted. Only the supported sites are accepted; "
        "check each directory's terms before use."
    )

    if not st.button(crawl_button_label, type="primary"):
        return
    if source_type == "CompanyDetails.in (A–Z)" and district_slug is None:
        st.error("Enter a valid district slug or HTTPS companydetails.in directory URL.")
        return
    if source_type == "AEPC India (numbered pages)" and aepc_start_url is None:
        st.error("Enter an HTTPS URL for aepcindia.com/members/ with a valid pageno.")
        return

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }
    if source_type == "AEPC India (numbered pages)":
        headers.update(
            {
                "Referer": "https://www.aepcindia.com/members/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            }
        )
    all_records = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    live_count = st.empty()
    live_summary = st.empty()
    live_preview = st.empty()
    letter_counts = {letter: 0 for letter in string.ascii_uppercase}
    page_counts = {}
    live_records = []
    st.subheader("Live extraction")
    live_count.metric("Companies found so far", 0)
    if source_type == "CompanyDetails.in (A–Z)":
        live_summary.info("Per-letter counts will appear here as pages are checked.")
    else:
        live_summary.info("Per-page counts will appear here as pages are checked.")
    live_preview.info("Waiting for the first directory page to be checked.")

    def update_progress(progress, letter):
        progress_bar.progress(progress)
        if source_type == "CompanyDetails.in (A–Z)":
            status_text.text(f"Finished letter [{letter}].")
        else:
            status_text.text(f"Finished page [{letter}].")

    def update_page(letter, page_num, page_records, total_count):
        if not page_records:
            status_text.text(
                f"Checked [{letter}] page {page_num}; no eligible new companies were found."
            )
            return

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

    def update_aepc_page(page_num, page_records, total_count):
        if not page_records:
            status_text.text(f"Checked page {page_num}; no new eligible companies were found.")
            return

        live_records.extend(page_records)
        page_counts[page_num] = len(page_records)
        status_text.text(
            f"Extracted {len(page_records)} companies from page {page_num}."
        )
        live_count.metric("Companies found so far", total_count)
        live_summary.dataframe(
            pd.DataFrame(
                [
                    {"Page": page, "Companies": count}
                    for page, count in sorted(page_counts.items())
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
        if source_type == "CompanyDetails.in (A–Z)":
            all_records, errors = crawl_district(
                session,
                district_slug,
                int(max_pages),
                update_progress,
                update_page,
            )
        else:
            all_records, errors = crawl_aepc_directory(
                session,
                aepc_start_url,
                int(max_pages),
                update_progress,
                update_aepc_page,
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
        st.error("No company entries were extracted from the directory pages.")
        if not errors:
            st.warning(
                "The pages responded, but no company listings were detected. "
                "The site may be serving an empty page to the cloud app, or its "
                "page format may have changed."
            )
            if source_type == "CompanyDetails.in (A–Z)":
                st.markdown(
                    f"[Open the source directory page for A]({BASE_URL}/{district_slug}/A)"
                )
            else:
                st.markdown(f"[Open the source directory page]({aepc_start_url})")
        return

    df_result = df_result.drop_duplicates().reset_index(drop=True)
    st.success(f"Extracted {len(df_result)} unique company records.")
    st.dataframe(df_result, use_container_width=True)

    st.subheader("Results by alphabet")
    st.dataframe(
        df_result.groupby("Alphabet_Index").size().reindex(
            ["0-9", *string.ascii_uppercase],
            fill_value=0,
        )
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

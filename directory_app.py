import hashlib
import ipaddress
import re
import socket
import ssl
import string
import time
from io import BytesIO
from urllib.parse import (
    parse_qs,
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3 import HTTPSConnectionPool
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
GENERIC_REQUEST_TIMEOUT_SECONDS = 15
GENERIC_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
GENERIC_MAX_REDIRECTS = 3
GENERIC_PAGE_PARAMETERS = ("page", "pageno", "page_num", "page-number", "pg", "p")
GENERIC_PAGINATION_MODES = {
    "auto": "Auto-detect",
    "numbered": "Numbered pages",
    "alphabet": "A-Z route pages",
    "next": "Follow Next links",
}


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


def resolve_public_ipv4(hostname):
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(
                hostname,
                443,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as error:
        raise ValueError("The directory host could not be resolved.") from error

    if not addresses:
        raise ValueError("The directory host has no public IPv4 address.")

    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("Only public website hosts can be fetched.")
    return sorted(addresses)[0]


def validate_generic_url(value):
    parsed_url = urlparse(value.strip())
    try:
        port = parsed_url.port
    except ValueError as error:
        raise ValueError("Enter a valid HTTPS directory URL.") from error

    if (
        parsed_url.scheme.lower() != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or port not in (None, 443)
    ):
        raise ValueError(
            "Enter a public HTTPS URL without login details or a custom port."
        )

    try:
        hostname = parsed_url.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ValueError("Enter a valid website hostname.") from error
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Enter a website hostname, not an IP address.")

    if not hostname or "." not in hostname:
        raise ValueError("Enter a fully qualified public website hostname.")

    public_ip = resolve_public_ipv4(hostname)
    normalized_netloc = hostname
    normalized_url = urlunparse(
        parsed_url._replace(
            scheme="https",
            netloc=normalized_netloc,
            fragment="",
        )
    )
    return normalized_url, hostname, public_ip


class PinnedHTTPSAdapter(HTTPAdapter):
    def __init__(self, hostname, public_ip):
        self.hostname = hostname
        self.public_ip = public_ip
        self._pinned_pool = None
        super().__init__(max_retries=0)

    def _connection_pool(self):
        if self._pinned_pool is None:
            self._pinned_pool = HTTPSConnectionPool(
                host=self.public_ip,
                port=443,
                assert_hostname=self.hostname,
                server_hostname=self.hostname,
                cert_reqs=ssl.CERT_REQUIRED,
            )
        return self._pinned_pool

    def get_connection(self, url, proxies=None):
        if proxies and any(proxies.values()):
            raise requests.exceptions.ProxyError(
                "Proxies are disabled for custom directory URLs."
            )
        return self._connection_pool()

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if proxies and any(proxies.values()):
            raise requests.exceptions.ProxyError(
                "Proxies are disabled for custom directory URLs."
            )
        return self.get_connection(request.url, proxies)

    def send(self, request, **kwargs):
        parsed_url = urlparse(request.url)
        if (
            parsed_url.scheme.lower() != "https"
            or parsed_url.hostname != self.hostname
            or parsed_url.port not in (None, 443)
        ):
            raise requests.exceptions.InvalidURL(
                "Custom crawling is limited to the submitted HTTPS host."
            )
        request.headers["Host"] = self.hostname
        return super().send(request, **kwargs)

    def close(self):
        if self._pinned_pool is not None:
            self._pinned_pool.close()
        super().close()


def detect_generic_pagination(start_url):
    parsed_url = urlparse(start_url)
    query = parse_qs(parsed_url.query, keep_blank_values=True)
    for parameter in GENERIC_PAGE_PARAMETERS:
        values = query.get(parameter, [])
        if len(values) == 1 and values[0].isdigit():
            return "numbered", parameter

    last_path_part = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
    if re.fullmatch(r"[A-Za-z]", last_path_part):
        return "alphabet", None
    return "next", None


def numbered_page_url(start_url, page_num, page_parameter):
    parsed_url = urlparse(start_url)
    query = parse_qsl(parsed_url.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != page_parameter]
    query.append((page_parameter, str(page_num)))
    return urlunparse(parsed_url._replace(query=urlencode(query)))


def alphabet_page_url(start_url, letter):
    parsed_url = urlparse(start_url)
    path = parsed_url.path.rstrip("/")
    if not re.search(r"/[A-Za-z]$", path):
        raise ValueError(
            "For A-Z pagination, use a directory URL ending in a single letter, "
            "such as /A."
        )
    path = f"{path[:-1]}{letter}"
    return urlunparse(parsed_url._replace(path=path))


def find_next_page_url(html, current_url):
    soup = BeautifulSoup(html, "html.parser")
    next_link = soup.find("a", rel=lambda value: value and "next" in value)
    if next_link is None:
        for link in soup.find_all("a", href=True):
            label = " ".join(
                (
                    link.get_text(" ", strip=True),
                    link.get("aria-label", ""),
                    " ".join(link.get("class", [])),
                )
            ).casefold()
            if re.search(r"\bnext\b|»|›", label):
                next_link = link
                break

    if next_link is None:
        return None

    target_url = urljoin(current_url, next_link.get("href", ""))
    current_host = urlparse(current_url).hostname
    target = urlparse(target_url)
    if (
        target.scheme.lower() != "https"
        or target.hostname != current_host
        or target.username is not None
        or target.password is not None
        or target.port not in (None, 443)
    ):
        return None
    return target_url


def _is_generic_company_name(value):
    name = " ".join(value.split()).strip(" -:|,")
    normalized = normalize_company_name(name)
    if (
        not name
        or len(name) < 2
        or len(name) > 120
        or not any(character.isalpha() for character in name)
        or re.fullmatch(r"\d+", normalized)
    ):
        return False

    non_company_labels = {
        "company",
        "company name",
        "business name",
        "name",
        "next",
        "previous",
        "view details",
        "read more",
        "more details",
        "contact us",
        "home",
    }
    return normalized not in non_company_labels


def _company_name_from_container(container):
    for element in container.select(
        '[itemprop="name"], [class*="company-name"], [class*="company_name"], '
        '[class*="business-name"], [class*="firm-name"], [class*="market_title"]'
    ):
        name = element.get_text(" ", strip=True)
        if _is_generic_company_name(name):
            return name

    for element in container.find_all(["h1", "h2", "h3", "h4", "strong"]):
        name = element.get_text(" ", strip=True)
        if _is_generic_company_name(name):
            return name

    if container.name == "tr":
        for cell in container.find_all(["td", "th"], recursive=False):
            name = cell.get_text(" ", strip=True)
            if _is_generic_company_name(name) and not PINCODE_PATTERN.search(name):
                return name

    for link in container.find_all("a", href=True):
        name = link.get_text(" ", strip=True)
        if _is_generic_company_name(name):
            return name

    return None


def parse_generic_page(html, page_number, source_url):
    soup = BeautifulSoup(html, "html.parser")
    records = []
    containers = list(soup.find_all("tr"))
    containers.extend(
        soup.find_all(
            ["article", "section", "li", "div"],
            class_=re.compile(
                r"company|business|firm|member|listing|result|card|views-row",
                re.I,
            ),
        )
    )
    containers.extend(soup.find_all(["article", "li"]))

    for container in containers:
        text = container.get_text(" ", strip=True)
        pincodes = list(dict.fromkeys(PINCODE_PATTERN.findall(text)))
        if len(pincodes) != 1:
            continue

        company_name = _company_name_from_container(container)
        if not company_name:
            continue

        first_letter = next(
            (character.upper() for character in company_name if character.isalpha()),
            "0-9",
        )
        records.append(
            {
                "Company_Name": company_name,
                "Pincode": pincodes[0],
                "Alphabet_Index": first_letter,
                "Page_Number": page_number,
                "Source_URL": source_url,
            }
        )

    unique_records = []
    seen = set()
    for record in records:
        key = (normalize_company_name(record["Company_Name"]), record["Pincode"])
        if key not in seen:
            seen.add(key)
            unique_records.append(record)
    return unique_records


def fetch_generic_page(session, url, expected_hostname):
    current_url = url
    for redirect_count in range(GENERIC_MAX_REDIRECTS + 1):
        parsed_url = urlparse(current_url)
        if (
            parsed_url.scheme.lower() != "https"
            or parsed_url.hostname != expected_hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.port not in (None, 443)
        ):
            raise ValueError("Pagination must stay on the submitted HTTPS host.")

        response = session.get(
            current_url,
            timeout=GENERIC_REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location or redirect_count == GENERIC_MAX_REDIRECTS:
                raise ValueError("The directory redirected too many times.")
            current_url = urljoin(current_url, location)
            continue

        try:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").casefold()
            if content_type and not any(
                media_type in content_type
                for media_type in ("text/html", "application/xhtml+xml")
            ):
                raise ValueError("The directory URL did not return an HTML page.")

            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > GENERIC_MAX_RESPONSE_BYTES:
                    raise ValueError("A directory page exceeded the 5 MB size limit.")
                chunks.append(chunk)
        finally:
            response.close()
        return b"".join(chunks).decode("utf-8", errors="replace"), current_url

    raise ValueError("The directory redirected too many times.")


def crawl_generic_directory(
    session,
    start_url,
    max_pages,
    pagination_mode="auto",
    page_parameter="page",
    progress_callback=None,
    page_callback=None,
):
    start_url, hostname, public_ip = validate_generic_url(start_url)
    session.trust_env = False
    session.mount(f"https://{hostname}", PinnedHTTPSAdapter(hostname, public_ip))

    if pagination_mode == "auto":
        pagination_mode, detected_parameter = detect_generic_pagination(start_url)
        if detected_parameter:
            page_parameter = detected_parameter
    if pagination_mode not in {"numbered", "alphabet", "next"}:
        raise ValueError("Choose a supported pagination style.")
    if not 1 <= max_pages <= 50:
        raise ValueError("The page limit must be between 1 and 50.")

    if pagination_mode == "numbered":
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", page_parameter):
            raise ValueError("Enter a valid page-number URL parameter.")
        values = parse_qs(urlparse(start_url).query).get(page_parameter, [])
        if len(values) > 1 or (values and not values[0].isdigit()):
            raise ValueError("The selected page parameter must contain one number.")
        first_page = int(values[0]) if values else 1
    elif pagination_mode == "alphabet":
        alphabet_page_url(start_url, "A")
        first_page = 1
        max_pages = min(max_pages, 26)
    else:
        first_page = 1

    records = []
    errors = []
    seen_company_keys = set()
    seen_page_fingerprints = set()
    seen_urls = set()
    current_url = start_url

    for page_offset in range(max_pages):
        if pagination_mode == "numbered":
            page_value = first_page + page_offset
            url = (
                start_url
                if page_offset == 0
                else numbered_page_url(start_url, page_value, page_parameter)
            )
            page_label = page_value
        elif pagination_mode == "alphabet":
            letter = string.ascii_uppercase[page_offset]
            url = alphabet_page_url(start_url, letter)
            page_label = letter
        else:
            url = current_url
            page_label = page_offset + 1

        if url in seen_urls:
            break
        seen_urls.add(url)

        try:
            html, final_url = fetch_generic_page(session, url, hostname)
        except (requests.RequestException, ValueError) as error:
            errors.append(f"Page {page_label}: {error}")
            break

        fingerprint = hashlib.sha256(html.encode("utf-8")).digest()
        if fingerprint in seen_page_fingerprints:
            break
        seen_page_fingerprints.add(fingerprint)

        page_records = parse_generic_page(html, page_label, final_url)
        new_source_records = []
        new_records = []
        for record in page_records:
            key = (normalize_company_name(record["Company_Name"]), record["Pincode"])
            if key in seen_company_keys:
                continue
            seen_company_keys.add(key)
            new_source_records.append(record)
            if not is_excluded_company(record["Company_Name"]):
                new_records.append(record)

        records.extend(new_records)
        if page_callback:
            page_callback(page_offset + 1, new_records, len(records))
        if progress_callback:
            progress_callback((page_offset + 1) / max_pages, page_label)
        if not new_source_records:
            break

        if pagination_mode == "next":
            next_url = find_next_page_url(html, final_url)
            if next_url is None:
                break
            current_url = next_url

        time.sleep(REQUEST_DELAY_SECONDS)

    return records, errors


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
        "Extract company names and Indian PIN codes from supported directories, "
        "or try a bounded best-effort parser on another directory URL."
    )

    source_type = st.radio(
        "Directory source",
        (
            "CompanyDetails.in (A–Z)",
            "AEPC India (numbered pages)",
            "Other directory URL (best effort)",
        ),
        horizontal=True,
    )
    generic_start_url = None
    generic_pagination_mode = "auto"
    generic_page_parameter = "page"
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
    elif source_type == "AEPC India (numbered pages)":
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
    else:
        with st.container():
            generic_start_url = st.text_input(
                "Directory HTTPS URL",
                placeholder="https://example.com/companies?page=1",
                help=(
                    "Only public HTTPS websites are allowed. The parser is best "
                    "effort; it cannot extract every site layout."
                ),
            ).strip()
            col1, col2, col3 = st.columns([1, 1, 1])
            with col1:
                generic_pagination_mode = st.selectbox(
                    "Pagination style",
                    tuple(GENERIC_PAGINATION_MODES),
                    format_func=lambda mode: GENERIC_PAGINATION_MODES[mode],
                )
            with col2:
                generic_page_parameter = st.text_input(
                    "Page URL parameter",
                    value="page",
                    help="Used for numbered pages, for example ?page=2 or ?pageno=2.",
                ).strip()
            with col3:
                max_pages = st.number_input(
                    "Maximum pages",
                    min_value=1,
                    max_value=50,
                    value=10,
                    help="Hard cap for this custom-URL crawl.",
                )
        district_slug = None
        aepc_start_url = None
        crawl_button_label = "🚀 Try This Directory URL"

    st.caption(
        "Pages are fetched one at a time with a delay between successful requests. "
        "Companies matching the excluded finance, government, charity, and other "
        "business categories are omitted. Custom URLs are limited to public HTTPS "
        "pages on the same host; CAPTCHA or access controls are not bypassed. "
        "Check each directory's terms before use."
    )

    if not st.button(crawl_button_label, type="primary"):
        return
    if source_type == "CompanyDetails.in (A–Z)" and district_slug is None:
        st.error("Enter a valid district slug or HTTPS companydetails.in directory URL.")
        return
    if source_type == "AEPC India (numbered pages)" and aepc_start_url is None:
        st.error("Enter an HTTPS URL for aepcindia.com/members/ with a valid pageno.")
        return
    if source_type == "Other directory URL (best effort)" and not generic_start_url:
        st.error("Enter the HTTPS URL of a directory page.")
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
        elif source_type == "AEPC India (numbered pages)":
            status_text.text(f"Finished page [{letter}].")
        else:
            status_text.text(f"Checked custom directory page [{letter}].")

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
        elif source_type == "AEPC India (numbered pages)":
            all_records, errors = crawl_aepc_directory(
                session,
                aepc_start_url,
                int(max_pages),
                update_progress,
                update_aepc_page,
            )
        else:
            try:
                all_records, errors = crawl_generic_directory(
                    session,
                    generic_start_url,
                    int(max_pages),
                    generic_pagination_mode,
                    generic_page_parameter,
                    update_progress,
                    update_aepc_page,
                )
            except ValueError as error:
                st.error(str(error))
                return

    if source_type == "CompanyDetails.in (A–Z)":
        export_prefix = district_slug
    elif source_type == "AEPC India (numbered pages)":
        export_prefix = "aepc_directory"
    else:
        export_prefix = urlparse(generic_start_url).hostname or "directory"

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
            elif source_type == "AEPC India (numbered pages)":
                st.markdown(f"[Open the source directory page]({aepc_start_url})")
            else:
                st.markdown(f"[Open the source directory page]({generic_start_url})")
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
            f"{export_prefix}_companies_pincodes.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with col_dl2:
        st.download_button(
            "📥 Per-letter CSV files (.zip)",
            alphabet_csv_data,
            f"{export_prefix}_companies_by_letter.zip",
            "application/zip",
        )
    with col_dl3:
        st.download_button(
            "📥 All companies as CSV",
            csv_data,
            f"{export_prefix}_companies_pincodes.csv",
            "text/csv",
        )


if __name__ == "__main__":
    main()

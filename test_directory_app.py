from io import BytesIO
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import pandas as pd
import requests
from openpyxl import load_workbook
from directory_app import (
    create_csv_archive,
    create_excel_export,
    aepc_page_url,
    aepc_url_from_input,
    alphabet_page_url,
    crawl_aepc_directory,
    crawl_district,
    crawl_generic_directory,
    detect_generic_pagination,
    district_slug_from_input,
    find_next_page_url,
    is_excluded_company,
    is_valid_district_slug,
    parse_aepc_page,
    parse_company_rows,
    parse_generic_page,
    validate_generic_url,
)


class DirectoryAppTests(unittest.TestCase):
    def test_validates_district_slug_as_a_single_path_segment(self):
        self.assertTrue(is_valid_district_slug("tamil-nadu-dindigul"))
        self.assertFalse(is_valid_district_slug("../other-site"))
        self.assertFalse(is_valid_district_slug("district/name"))
        self.assertFalse(is_valid_district_slug("https://example.com"))

    def test_extracts_slug_from_directory_url(self):
        self.assertEqual(
            district_slug_from_input(
                "https://www.companydetails.in/companies_district_wise/"
                "tamil-nadu-dindigul/A"
            ),
            "tamil-nadu-dindigul",
        )
        self.assertIsNone(
            district_slug_from_input(
                "https://example.com/companies_district_wise/tamil-nadu-dindigul/A"
            )
        )

    def test_validates_and_advances_aepc_numeric_page_url(self):
        page_one = (
            "https://www.aepcindia.com/members/"
            "?search_field=field_state&views_exposed_field_filter=Tamil+Nadu&pageno=1"
        )
        self.assertEqual(aepc_url_from_input(page_one), page_one)
        self.assertIn("pageno=2", aepc_page_url(page_one, 2))
        self.assertIn("views_exposed_field_filter=Tamil+Nadu", aepc_page_url(page_one, 2))
        self.assertIsNone(
            aepc_url_from_input("https://example.com/members/?pageno=1")
        )
        self.assertIsNone(
            aepc_url_from_input("https://www.aepcindia.com/members/?pageno=last")
        )

    def test_validates_custom_directory_urls_before_fetching(self):
        with patch(
            "directory_app.resolve_public_ipv4",
            return_value="93.184.216.34",
        ):
            self.assertEqual(
                validate_generic_url("https://Directory.Example/list?page=1#top"),
                (
                    "https://directory.example/list?page=1",
                    "directory.example",
                    "93.184.216.34",
                ),
            )
            for url in (
                "http://directory.example/list",
                "https://user@directory.example/list",
                "https://directory.example:8443/list",
                "https://127.0.0.1/list",
                "https://localhost/list",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    validate_generic_url(url)

        with patch(
            "directory_app.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("127.0.0.1", 443))],
        ):
            with self.assertRaisesRegex(ValueError, "public website"):
                validate_generic_url("https://directory.example/list")

    def test_detects_common_pagination_and_replaces_alphabet_path(self):
        self.assertEqual(
            detect_generic_pagination(
                "https://directory.example/companies?sort=name&pageno=4"
            ),
            ("numbered", "pageno"),
        )
        self.assertEqual(
            detect_generic_pagination("https://directory.example/companies/A"),
            ("alphabet", None),
        )
        self.assertEqual(
            alphabet_page_url("https://directory.example/companies/A", "B"),
            "https://directory.example/companies/B",
        )

    def test_finds_next_links_only_on_the_same_https_host(self):
        self.assertEqual(
            find_next_page_url(
                '<a rel="next" href="?page=2">Next</a>',
                "https://directory.example/companies?page=1",
            ),
            "https://directory.example/companies?page=2",
        )
        self.assertIsNone(
            find_next_page_url(
                '<a rel="next" href="https://other.example/companies?page=2">'
                "Next</a>",
                "https://directory.example/companies?page=1",
            )
        )

    def test_parses_generic_company_and_pincode_rows(self):
        html = """
        <table>
          <tr><th>Company</th><th>Address</th></tr>
          <tr><td>Example Industries</td><td>Chennai, Tamil Nadu 600001</td></tr>
          <tr><td>North Example Ltd</td><td>Delhi 110001</td></tr>
        </table>
        """

        self.assertEqual(
            parse_generic_page(html, 2, "https://directory.example/list?page=2"),
            [
                {
                    "Company_Name": "Example Industries",
                    "Pincode": "600001",
                    "Alphabet_Index": "E",
                    "Page_Number": 2,
                    "Source_URL": "https://directory.example/list?page=2",
                },
                {
                    "Company_Name": "North Example Ltd",
                    "Pincode": "110001",
                    "Alphabet_Index": "N",
                    "Page_Number": 2,
                    "Source_URL": "https://directory.example/list?page=2",
                },
            ],
        )

    def test_custom_crawler_continues_after_excluded_page(self):
        start_url = "https://directory.example/companies?page=1"

        def table_row(name, pin):
            return (
                "<table><tr>"
                f"<td>{name}</td><td>Chennai {pin}</td>"
                "</tr></table>"
            )

        session = Mock()
        with patch(
            "directory_app.resolve_public_ipv4",
            return_value="93.184.216.34",
        ), patch(
            "directory_app.fetch_generic_page",
            side_effect=[
                (table_row("Example Finance Ltd", "600001"), start_url),
                (
                    table_row("Example Textiles Ltd", "600002"),
                    "https://directory.example/companies?page=2",
                ),
                ("<html></html>", "https://directory.example/companies?page=3"),
            ],
        ) as fetch_page, patch("directory_app.time.sleep"):
            records, errors = crawl_generic_directory(
                session,
                start_url,
                3,
                pagination_mode="auto",
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [record["Company_Name"] for record in records],
            ["Example Textiles Ltd"],
        )
        self.assertEqual(
            [call.args[1] for call in fetch_page.call_args_list],
            [
                start_url,
                "https://directory.example/companies?page=2",
                "https://directory.example/companies?page=3",
            ],
        )

    def test_custom_crawler_advances_alphabet_routes(self):
        start_url = "https://directory.example/companies/A"

        def table_row(name, pin):
            return f"<table><tr><td>{name}</td><td>Chennai {pin}</td></tr></table>"

        session = Mock()
        with patch(
            "directory_app.resolve_public_ipv4",
            return_value="93.184.216.34",
        ), patch(
            "directory_app.fetch_generic_page",
            side_effect=[
                (table_row("Alpha Industries", "600001"), start_url),
                (
                    table_row("Beta Industries", "600002"),
                    "https://directory.example/companies/B",
                ),
                ("<html></html>", "https://directory.example/companies/C"),
            ],
        ) as fetch_page, patch("directory_app.time.sleep"):
            records, errors = crawl_generic_directory(
                session,
                start_url,
                3,
                pagination_mode="alphabet",
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [record["Company_Name"] for record in records],
            ["Alpha Industries", "Beta Industries"],
        )
        self.assertEqual(
            [call.args[1] for call in fetch_page.call_args_list],
            [
                "https://directory.example/companies/A",
                "https://directory.example/companies/B",
                "https://directory.example/companies/C",
            ],
        )

    def test_custom_crawler_follows_same_host_next_links(self):
        start_url = "https://directory.example/companies"
        first_page = (
            "<table><tr><td>Alpha Industries</td><td>Chennai 600001</td></tr></table>"
            '<a rel="next" href="/companies?page=2">Next</a>'
        )
        second_url = "https://directory.example/companies?page=2"
        second_page = (
            "<table><tr><td>Beta Industries</td><td>Chennai 600002</td></tr></table>"
        )
        session = Mock()
        with patch(
            "directory_app.resolve_public_ipv4",
            return_value="93.184.216.34",
        ), patch(
            "directory_app.fetch_generic_page",
            side_effect=[
                (first_page, start_url),
                (second_page, second_url),
            ],
        ) as fetch_page, patch("directory_app.time.sleep"):
            records, errors = crawl_generic_directory(
                session,
                start_url,
                5,
                pagination_mode="next",
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [record["Company_Name"] for record in records],
            ["Alpha Industries", "Beta Industries"],
        )
        self.assertEqual(fetch_page.call_args_list[1].args[1], second_url)

    def test_excludes_listed_categories_case_insensitively_by_whole_phrase(self):
        for company_name in (
            "ABC FINANCE PRIVATE LIMITED",
            "South City Asset Management Ltd",
            "Mahal Function Hall",
            "Mandapam Services",
            "Law and Firm Associates",
            "Community Trust Foundation",
            "Agricultural Farmers Association",
            "Govt Companies of India",
        ):
            with self.subTest(company_name=company_name):
                self.assertTrue(is_excluded_company(company_name))

        self.assertFalse(is_excluded_company("Infinity Technology Private Limited"))
        self.assertFalse(is_excluded_company("Posters and Prints Private Limited"))
        self.assertFalse(is_excluded_company("Bright Engineering Services"))

    def test_parses_company_names_and_indian_pin_codes(self):
        html = """
        <table>
          <tr><th>Company Name</th><th>Address</th></tr>
          <tr><td>Example Industries</td><td>Chennai, Tamil Nadu 600001</td></tr>
          <tr><td>North Example Ltd</td><td>Delhi, 110001</td></tr>
          <tr><td>Company Name</td><td>PIN 600002</td></tr>
          <tr><td>Missing PIN Co</td><td>Address unavailable</td></tr>
        </table>
        """

        self.assertEqual(
            parse_company_rows(html, "E"),
            [
                {
                    "Company_Name": "Example Industries",
                    "Pincode": "600001",
                    "Alphabet_Index": "E",
                },
                {
                    "Company_Name": "North Example Ltd",
                    "Pincode": "110001",
                    "Alphabet_Index": "E",
                },
                {
                    "Company_Name": "Missing PIN Co",
                    "Pincode": "N/A",
                    "Alphabet_Index": "E",
                },
            ],
        )

    def test_parses_company_directory_cards(self):
        html = """
        <div class="company-card">
          <h3><a href="/company/example-industries">Example Industries</a></h3>
          <h5>ACTIVE</h5>
          <h6>MAIN ROAD DINDIGUL TN 624001 IN</h6>
        </div>
        """

        self.assertEqual(
            parse_company_rows(html, "E"),
            [
                {
                    "Company_Name": "Example Industries",
                    "Pincode": "624001",
                    "Alphabet_Index": "E",
                }
            ],
        )

    def test_parses_aepc_member_cards_without_contact_details(self):
        html = """
        <div class="views-row">
          <div class="market_title">(K) Shethra Exports,</div>
          <div class="market_person">Private Person</div>
          <div class="market_address">Nithya Ginning Factory Compound</div>
          <div class="market_city_pin">Tirupur - 641607</div>
          <div class="market_email">person@example.com</div>
        </div>
        <div class="views-row">
          <div class="market_title">3M Exports</div>
          <div class="market_city_pin">Tirupur - 641601</div>
        </div>
        """

        records = parse_aepc_page(html, 4)
        self.assertEqual(
            records,
            [
                {
                    "Company_Name": "(K) Shethra Exports,",
                    "Pincode": "641607",
                    "Alphabet_Index": "K",
                    "Page_Number": 4,
                },
                {
                    "Company_Name": "3M Exports",
                    "Pincode": "641601",
                    "Alphabet_Index": "M",
                    "Page_Number": 4,
                },
            ],
        )
        self.assertNotIn("market_person", records[0])

    def test_stops_after_repeated_request_failures(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("request timed out")

        with patch("directory_app.time.sleep"):
            records, errors = crawl_district(session, "tamil-nadu-dindigul", 1)

        self.assertEqual(records, [])
        self.assertEqual(len(errors), 3)
        self.assertEqual(session.get.call_count, 3)

    def test_reports_each_empty_page_to_live_progress_callback(self):
        response = Mock(status_code=200, text="<html></html>")
        session = Mock()
        session.get.return_value = response
        page_callback = Mock()

        with patch("directory_app.time.sleep"):
            records, errors = crawl_district(
                session,
                "chennai",
                1,
                page_callback=page_callback,
            )

        self.assertEqual(records, [])
        self.assertEqual(errors, [])
        self.assertEqual(page_callback.call_count, 26)

    def test_stops_on_repeated_pages_and_keeps_companies_once(self):
        def get_page(url, params=None, timeout=None):
            letter = url.rsplit("/", 1)[-1]
            letter_index = ord(letter) - ord("A")
            pincode = 600001 + letter_index
            html = f"""
            <div>
              <h3><a href="/company/company-{letter.lower()}">Company {letter} Limited</a></h3>
              <h6>Chennai, Tamil Nadu {pincode}</h6>
            </div>
            """
            return Mock(status_code=200, text=html)

        session = Mock()
        session.get.side_effect = get_page

        with patch("directory_app.time.sleep"), patch(
            "directory_app.parse_company_rows",
            wraps=parse_company_rows,
        ) as parse_rows:
            records, errors = crawl_district(
                session,
                "tamil-nadu-dindigul",
                50,
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(records), 26)
        self.assertEqual(len({record["Company_Name"] for record in records}), 26)
        self.assertEqual(session.get.call_count, 52)
        self.assertEqual(parse_rows.call_count, 26)

    def test_filters_excluded_companies_without_skipping_later_pages(self):
        def get_page(url, params=None, timeout=None):
            letter = url.rsplit("/", 1)[-1]
            if params is None:
                company_name = f"{letter} Finance Limited"
                pin = "600001"
            else:
                company_name = f"{letter} Engineering Limited"
                pin = "600002"
            html = f"""
            <div>
              <h3><a href="/company/{letter.lower()}-{pin}">{company_name}</a></h3>
              <h6>Chennai, Tamil Nadu {pin}</h6>
            </div>
            """
            return Mock(status_code=200, text=html)

        session = Mock()
        session.get.side_effect = get_page

        with patch("directory_app.time.sleep"):
            records, errors = crawl_district(
                session,
                "tamil-nadu-dindigul",
                2,
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(records), 26)
        self.assertTrue(all("Engineering" in row["Company_Name"] for row in records))
        self.assertEqual(session.get.call_count, 52)

    def test_crawls_aepc_numeric_pages_and_filters_excluded_companies(self):
        def response_for(name, pin):
            return Mock(
                status_code=200,
                text=(
                    '<div class="views-row">'
                    f'<div class="market_title">{name}</div>'
                    f'<div class="market_city_pin">Chennai - {pin}</div>'
                    "</div>"
                ),
            )

        session = Mock()
        session.get.side_effect = [
            response_for("Page One Exports", "600001"),
            response_for("Page Two Finance Ltd", "600002"),
            response_for("Page Two Textiles", "600003"),
        ]
        start_url = (
            "https://www.aepcindia.com/members/"
            "?search_field=field_state&views_exposed_field_filter=Tamil+Nadu&pageno=1"
        )
        page_callback = Mock()

        with patch("directory_app.time.sleep"):
            records, errors = crawl_aepc_directory(
                session,
                start_url,
                3,
                page_callback=page_callback,
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [record["Company_Name"] for record in records],
            ["Page One Exports", "Page Two Textiles"],
        )
        self.assertEqual(session.get.call_count, 3)
        self.assertIn("pageno=1", session.get.call_args_list[0].args[0])
        self.assertIn("pageno=2", session.get.call_args_list[1].args[0])
        self.assertIn("pageno=3", session.get.call_args_list[2].args[0])
        self.assertEqual(page_callback.call_count, 3)

    def test_exports_excel_sheets_and_csv_files_by_letter(self):
        frame = pd.DataFrame(
            [
                {"Company_Name": "Alpha Co", "Pincode": "600001", "Alphabet_Index": "A"},
                {"Company_Name": "Beta Co", "Pincode": "110001", "Alphabet_Index": "B"},
            ]
        )

        workbook = load_workbook(BytesIO(create_excel_export(frame)), read_only=True)
        self.assertIn("All Companies", workbook.sheetnames)
        self.assertEqual(workbook["A"].cell(row=2, column=1).value, "Alpha Co")
        self.assertEqual(workbook["B"].cell(row=2, column=1).value, "Beta Co")

        with ZipFile(BytesIO(create_csv_archive(frame))) as archive:
            self.assertEqual(archive.namelist(), ["A.csv", "B.csv"])
            self.assertIn("Alpha Co", archive.read("A.csv").decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main()

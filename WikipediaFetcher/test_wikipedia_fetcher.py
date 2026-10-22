"""Smoke tests for WikipediaFetcher.

Parser tests need a real page's HTML. Instead of a checked-in fixture, the
module downloads it live when the tests start, runs the parser tests against
it, and deletes the temporary HTML file when done.

- If the live download fails (no network/page), parser tests are skipped.
- CLI tests do not need the network and always run.
- The live end-to-end test is additionally gated by ``WIKIPEDIA_LIVE_TEST=1``.

Run::

    .venv/bin/python WikipediaFetcher/test_wikipedia_fetcher.py

    # also exercise the full single-page fetch:
    WIKIPEDIA_LIVE_TEST=1 .venv/bin/python WikipediaFetcher/test_wikipedia_fetcher.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wikipedia_fetcher as wf

# A stable page used only to obtain representative HTML at test time.
FIXTURE_KEYWORD = "豊臣秀長"
FIXTURE_LANG = "ja"

# Temporary HTML downloaded for the duration of the test run, then removed.
TEMP_HTML: Path | None = None

# Populated by ``setUpModule``; ``None`` means the download failed or was empty.
_DOWNLOAD_ERROR: str | None = None


def _fetch_fixture_html() -> str:
    """Fetch the article-body HTML fragment using the tool itself."""
    options = wf.FetchOptions(
        keyword=FIXTURE_KEYWORD,
        language=FIXTURE_LANG,
        proxy=wf._default_proxy(),
    )
    fetcher = wf.WikipediaFetcher(options)
    try:
        page = fetcher.wiki.page(FIXTURE_KEYWORD)
        if not wf._page_exists(page):
            return ""
        return fetcher._parsed_html(page)
    finally:
        fetcher.close()


def setUpModule() -> None:
    """Download the HTML fixture before any test runs."""
    global TEMP_HTML, _DOWNLOAD_ERROR
    try:
        html = _fetch_fixture_html()
    except Exception as exc:  # noqa: BLE001 - network/API failures -> skip, not fail
        _DOWNLOAD_ERROR = repr(exc)
        return
    if not html:
        _DOWNLOAD_ERROR = "page missing or empty HTML"
        return
    fd, name = tempfile.mkstemp(prefix="wikipedia_fetcher_test_", suffix=".html")
    os.close(fd)
    TEMP_HTML = Path(name)
    TEMP_HTML.write_text(html, encoding="utf-8")


def tearDownModule() -> None:
    """Always remove the temporary HTML file, whether tests ran or not."""
    if TEMP_HTML is not None:
        TEMP_HTML.unlink(missing_ok=True)


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if _DOWNLOAD_ERROR is not None:
            raise unittest.SkipTest(f"live HTML download failed: {_DOWNLOAD_ERROR}")
        if TEMP_HTML is None or not TEMP_HTML.exists():
            raise unittest.SkipTest("live HTML fixture was not downloaded")
        cls.html = TEMP_HTML.read_text(encoding="utf-8")

    def test_html_to_text_strips_tags_and_scripts(self) -> None:
        text = wf._html_to_text("<p>Hello <b>world</b></p><script>x=1</script>")
        self.assertEqual(text, "Hello world")
        self.assertNotIn("x=1", text)

    def test_group_by_language_namespace(self) -> None:
        rows = [
            {"title": "B", "namespace": 0, "language": "ja"},
            {"title": "A", "namespace": 0, "language": "ja"},
            {"title": "Cat", "namespace": 14, "language": "ja"},
        ]
        grouped = wf._group_by_language_namespace(rows)
        self.assertEqual(grouped["ja"]["0"]["titles"], ["A", "B"])
        self.assertEqual(grouped["ja"]["0"]["count"], 2)
        self.assertEqual(grouped["ja"]["14"]["namespace_name"], "category")

    def test_decode_sortkey_hex_and_fallback(self) -> None:
        self.assertEqual(wf._decode_sortkey("e38182"), "あ")
        self.assertIsNone(wf._decode_sortkey(None))
        self.assertEqual(wf._decode_sortkey("323a44"), "2:D")
        # Invalid UTF-8 binary keys fall back to the original hex string.
        self.assertEqual(wf._decode_sortkey("dcbddcc0"), "dcbddcc0")

    def test_sections_have_ids_and_nesting(self) -> None:
        sections = wf.WikipediaFetcher.__new__(
            wf.WikipediaFetcher
        )._sections_from_html(self.html)
        self.assertTrue(sections)
        self.assertIn("id", sections[0])
        self.assertTrue(any(section["sections"] for section in sections))
        total = sum(1 for _ in _walk(sections))
        self.assertGreater(total, len(sections))

    def test_infobox_is_structured(self) -> None:
        infobox = wf._parse_infobox(self.html)
        self.assertIsNotNone(infobox)
        labels = [row.get("label") for row in infobox["rows"]]
        self.assertIn("生誕", labels)

    def test_body_tables_exclude_infobox(self) -> None:
        tables = wf._tables_from_html(self.html)
        self.assertTrue(tables)
        for table in tables:
            self.assertNotIn("infobox", table["classes"])
            for row in table["rows"]:
                for cell in row:
                    self.assertIn("text", cell)
                    self.assertIn("header", cell)
                    self.assertIn("links", cell)

    def test_links_from_html_have_anchor_and_target(self) -> None:
        items = wf._links_from_html(self.html)
        self.assertTrue(items)
        for item in items:
            self.assertIn("anchor", item)
            self.assertIn("title", item)
            self.assertTrue(item["anchor"])
            self.assertTrue(item["title"])
        self.assertTrue(any(item["anchor"] != item["title"] for item in items))
        self.assertTrue(any(item["redlink"] for item in items))
        self.assertTrue(any(item["count"] > 1 for item in items))

    def test_references_are_structured(self) -> None:
        references = wf._references_from_html(self.html)
        self.assertTrue(references)
        self.assertTrue(all(ref["id"].startswith("cite_note-") for ref in references))
        self.assertTrue(any(ref["urls"] for ref in references))
        self.assertTrue(any(ref["group"] == "注釈" for ref in references))

    def test_search_snippet_is_cleaned(self) -> None:
        snippet = wf._html_to_text(
            '<span class="searchmatch">豊臣</span> 秀長 は武将'
        )
        self.assertNotIn("<", snippet)
        self.assertIn("豊臣", snippet)


class QueryPayloadTests(unittest.TestCase):
    def test_proxy_credentials_are_removed_without_changing_request_options(self) -> None:
        cases = [
            (None, None),
            ("", ""),
            ("http://proxy.example:8080", "http://proxy.example:8080"),
            ("http://user:secret@proxy.example:8080", "http://proxy.example:8080"),
            ("http://user@proxy.example:8080", "http://proxy.example:8080"),
            ("http://user%40mail:p%40ss@proxy.example:8080", "http://proxy.example:8080"),
            ("http://user:secret@[::1]:8080", "http://[::1]:8080"),
            ("http://user:secret@[invalid", "<redacted>"),
            ("user:secret@proxy.example:8080", "<redacted>"),
        ]
        for proxy, expected in cases:
            with self.subTest(proxy=proxy):
                fetcher = wf.WikipediaFetcher.__new__(wf.WikipediaFetcher)
                fetcher.options = wf.FetchOptions(proxy=proxy)
                self.assertEqual(fetcher._query_payload("Example")["proxy"], expected)
                self.assertEqual(fetcher.options.proxy, proxy)

    def test_failed_fetch_does_not_include_proxy_credentials(self) -> None:
        fetcher = wf.WikipediaFetcher.__new__(wf.WikipediaFetcher)
        fetcher.options = wf.FetchOptions(
            proxy="http://proxy-user:proxy-password@proxy.example:8080"
        )
        fetcher._fetch = Mock(side_effect=RuntimeError("fetch failed"))
        data = fetcher.fetch()
        self.assertIn("error", data)
        self.assertEqual(data["query"]["proxy"], "http://proxy.example:8080")
        self.assertNotIn("proxy-user", json.dumps(data))
        self.assertNotIn("proxy-password", json.dumps(data))


class SectionParserTests(unittest.TestCase):
    def test_section_ids_decode_html_entities_exactly_once(self) -> None:
        fetcher = wf.WikipediaFetcher.__new__(wf.WikipediaFetcher)
        cases = [
            ("Research_&amp;_development", "Research_&_development"),
            ("&#x79C0;&#38263;", "秀長"),
            ("Literal_&amp;amp;", "Literal_&amp;"),
            ("Quoted_&quot;name&quot;", 'Quoted_"name"'),
            ("Plain_heading", "Plain_heading"),
            ("", ""),
        ]
        for encoded, expected in cases:
            with self.subTest(id=encoded):
                sections = fetcher._sections_from_html(
                    f'<h2 id="{encoded}">Heading</h2><p>Body</p>'
                )
                self.assertEqual(sections[0]["id"], expected)

    def test_section_without_id_still_has_null_id(self) -> None:
        fetcher = wf.WikipediaFetcher.__new__(wf.WikipediaFetcher)
        sections = fetcher._sections_from_html("<h2>Heading</h2><p>Body</p>")
        self.assertIsNone(sections[0]["id"])


class TableParserTests(unittest.TestCase):
    def test_linked_caption_preserves_tables_and_cell_links(self) -> None:
        tables = wf._tables_from_html(
            '<table class="wikitable">'
            '<caption><a href="/wiki/Japan">Japan</a> population</caption>'
            '<tr><th>Year</th><td><a href="/wiki/2020">2020</a></td></tr>'
            '</table><table><tr><td>Second table</td></tr></table>'
        )
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0]["caption"], "Japan population")
        self.assertEqual(tables[0]["rows"][0][1]["links"], ["2020"])
        self.assertEqual(tables[1]["rows"][0][0]["text"], "Second table")


class CliTests(unittest.TestCase):
    def test_parse_args_defaults(self) -> None:
        args = wf.parse_args([])
        self.assertEqual(args.lang, wf.DEFAULT_LANGUAGE)
        self.assertEqual(args.keyword, wf.DEFAULT_KEYWORD)
        self.assertEqual(args.link_limit, 0)
        self.assertIsNone(args.wikitext_out)

    def test_parse_args_wikitext_out(self) -> None:
        args = wf.parse_args(["--wikitext-out", "x.wiki"])
        self.assertEqual(str(args.wikitext_out), "x.wiki")

    def test_parse_args_strict(self) -> None:
        self.assertFalse(wf.parse_args([]).strict)
        self.assertTrue(wf.parse_args(["--strict"]).strict)


class CliOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fetcher = Mock()
        self.fetcher.fetch.return_value = {
            "exists": True,
            "page": {"canonicalurl": "https://en.wikipedia.org/wiki/Example"},
            "content": {"wikitext": "ARTICLE WIKITEXT"},
            "_html": "<p>Article body</p>",
        }
        constructor_patch = patch.object(
            wf, "WikipediaFetcher", return_value=self.fetcher
        )
        self.constructor = constructor_patch.start()
        self.addCleanup(constructor_patch.stop)

    def test_output_collisions_are_rejected_before_fetch_or_write(self) -> None:
        cases = [
            ["--out", "snapshot.wiki"],
            ["--out", "snapshot.html"],
            ["--out", "snapshot.json", "--html-out", "./snapshot.json"],
            ["--out", "snapshot.json", "--wikitext-out", "snapshot.json"],
            ["--out", "snapshot.json", "--source-html-out", "snapshot.json"],
            ["--html-out", "same.html", "--wikitext-out", "same.html"],
            ["--html-out", "same.html", "--source-html-out", "same.html"],
            ["--wikitext-out", "same.wiki", "--source-html-out", "same.wiki"],
            ["--out", "snapshot.json", "--source-html-out", "snapshot.html"],
        ]
        with patch.object(wf, "_write_text") as write:
            for argv in cases:
                with (
                    self.subTest(argv=argv),
                    redirect_stderr(StringIO()) as stderr,
                ):
                    with self.assertRaises(SystemExit) as caught:
                        wf.main(argv)
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn("Output paths must be distinct", stderr.getvalue())
            self.constructor.assert_not_called()
            write.assert_not_called()

    def test_source_html_failure_warns_by_default(self) -> None:
        with (
            patch.object(wf, "_fetch_source_html", side_effect=TimeoutError("timeout")),
            patch.object(wf, "_write_text") as write,
            redirect_stdout(StringIO()) as stdout,
        ):
            status = wf.main(["--source-html-out", "source.html"])
        self.assertEqual(status, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["warnings"][0]["step"], "content.source_html.fetch")
        self.assertEqual(data["warnings"][0]["type"], "TimeoutError")
        self.assertNotIn("source_html_file", data["content"])
        write.assert_not_called()

    def test_source_html_failure_raises_in_strict_mode(self) -> None:
        error = TimeoutError("timeout")
        with (
            patch.object(wf, "_fetch_source_html", side_effect=error),
            patch.object(wf, "_write_text") as write,
            self.assertRaises(wf.FetchStepError) as caught,
        ):
            wf.main(["--strict", "--source-html-out", "source.html"])
        self.assertEqual(caught.exception.step, "content.source_html.fetch")
        self.assertIs(caught.exception.__cause__, error)
        self.fetcher.close.assert_called_once()
        write.assert_not_called()

    def test_missing_source_url_raises_in_strict_mode(self) -> None:
        self.fetcher.fetch.return_value["page"] = {}
        with (
            patch.object(wf, "_fetch_source_html") as fetch_source,
            redirect_stdout(StringIO()),
            self.assertRaises(wf.FetchStepError) as caught,
        ):
            wf.main(["--strict", "--source-html-out", "source.html"])
        self.assertEqual(caught.exception.step, "content.source_html.fetch")
        fetch_source.assert_not_called()

    def test_distinct_outputs_preserve_all_contents(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(wf, "_fetch_source_html", return_value="<html>Full page</html>"),
            redirect_stdout(StringIO()),
        ):
            out = Path(directory) / "snapshot.json"
            source_out = Path(directory) / "source.html"
            status = wf.main(
                ["--strict", "--out", str(out), "--source-html-out", str(source_out)]
            )
            self.assertEqual(status, 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(
                Path(data["content"]["wikitext_file"]).read_text(encoding="utf-8"),
                "ARTICLE WIKITEXT",
            )
            self.assertEqual(
                Path(data["content"]["html_file"]).read_text(encoding="utf-8"),
                "<p>Article body</p>",
            )
            self.assertEqual(
                source_out.read_text(encoding="utf-8"), "<html>Full page</html>"
            )
            self.assertNotIn("wikitext", data["content"])


class StrictTests(unittest.TestCase):
    def test_optional_step_swallows_by_default(self) -> None:
        fetcher = wf.WikipediaFetcher(wf.FetchOptions(strict=False))
        try:
            warnings: list = []

            def boom() -> None:
                raise ValueError("boom")

            result = fetcher._optional_step("s", boom, "default", warnings)
            self.assertEqual(result, "default")
            self.assertEqual(warnings[0]["step"], "s")
        finally:
            fetcher.close()

    def test_optional_step_raises_in_strict(self) -> None:
        fetcher = wf.WikipediaFetcher(wf.FetchOptions(strict=True))
        try:
            def boom() -> None:
                raise ValueError("boom")

            with self.assertRaises(wf.FetchStepError):
                fetcher._optional_step("s", boom, "default", [])
        finally:
            fetcher.close()

    def test_write_text_reports_error(self) -> None:
        error = wf._write_text(Path("/nonexistent_dir_xyz/out.txt"), "x")
        self.assertIsNotNone(error)


class FetchErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fetcher = wf.WikipediaFetcher.__new__(wf.WikipediaFetcher)
        self.fetcher.options = wf.FetchOptions(keyword="Example", language="en")
        self.page = SimpleNamespace(
            title="Example",
            language="en",
            exists=True,
            summary="Example summary",
            langlinks={},
            images={},
        )
        self.fetcher.wiki = Mock()
        self.fetcher.wiki.page.return_value = self.page
        self.fetcher.wiki.search.return_value = SimpleNamespace(pages={})
        self.fetcher.wiki._get.return_value = {}

    def test_successful_fetch_does_not_include_proxy_credentials(self) -> None:
        self.fetcher.options = wf.FetchOptions(
            proxy="http://proxy-user:proxy-password@proxy.example:8080"
        )
        data = self.fetcher.fetch()
        self.assertTrue(data["exists"])
        self.assertEqual(data["query"]["proxy"], "http://proxy.example:8080")
        self.assertNotIn("proxy-user", json.dumps(data))
        self.assertNotIn("proxy-password", json.dumps(data))

    def test_search_failure_is_returned_by_default(self) -> None:
        self.fetcher.wiki.search.side_effect = RuntimeError("search unavailable")
        data = self.fetcher.fetch()
        self.assertTrue(data["exists"])
        self.assertEqual(data["search_results"], [{"error": "search unavailable"}])
        self.assertNotIn("error", data)

    def test_search_failure_raises_in_strict_mode(self) -> None:
        self.fetcher.options = wf.FetchOptions(strict=True)
        error = RuntimeError("search unavailable")
        self.fetcher.wiki.search.side_effect = error
        with self.assertRaises(wf.FetchStepError) as caught:
            self.fetcher.fetch()
        self.assertEqual(caught.exception.step, "search")
        self.assertIs(caught.exception.__cause__, error)

    def test_parse_api_error_warns_by_default_and_raises_in_strict_mode(self) -> None:
        def api_response(language, params):
            if params["action"] == "parse":
                return {"error": {"code": "internal_api_error", "info": "Parse failed"}}
            return {}

        self.fetcher.wiki._get.side_effect = api_response
        data = self.fetcher.fetch()
        self.assertTrue(data["exists"])
        self.assertNotIn("error", data)
        self.assertEqual(data["sections"], [])
        self.assertEqual(data["warnings"][0]["step"], "content.html.parse")
        self.assertEqual(data["warnings"][0]["type"], "MediaWikiAPIError")
        self.assertIn(
            "internal_api_error: Parse failed", data["warnings"][0]["message"]
        )

        self.fetcher.options = wf.FetchOptions(strict=True)
        with self.assertRaises(wf.FetchStepError) as caught:
            self.fetcher.fetch()
        self.assertEqual(caught.exception.step, "content.html.parse")
        self.assertIsInstance(caught.exception.__cause__, wf.MediaWikiAPIError)

    def test_all_raw_query_helpers_reject_api_errors(self) -> None:
        self.fetcher.wiki._get.return_value = {
            "error": {"code": "maxlag", "info": "Waiting for replication"}
        }
        callbacks = {
            "parse": lambda: self.fetcher._parsed_html(self.page),
            "plaintext": lambda: self.fetcher._extracts_payload(self.page),
            "wikitext": lambda: self.fetcher._wikitext_payload(self.page),
            "categories": lambda: self.fetcher._category_rows(self.page),
            "links": lambda: self.fetcher._links_payload(self.page),
            "external_links": lambda: self.fetcher._external_links_payload(self.page),
            "metadata": lambda: self.fetcher._meta_payload(self.page),
            "imageinfo": lambda: self.fetcher._imageinfo(["File:Example.jpg"]),
            "redirect": lambda: self.fetcher._redirect_payload("Example"),
        }
        for name, callback in callbacks.items():
            with (
                self.subTest(helper=name),
                self.assertRaisesRegex(RuntimeError, "maxlag: Waiting for replication"),
            ):
                callback()

    def test_paginated_api_error_does_not_silently_return_partial_links(self) -> None:
        self.fetcher.wiki._get.side_effect = [
            {
                "query": {"pages": {"1": {"links": [{"title": "Target", "ns": 0}]}}},
                "continue": {"plcontinue": "1|0|Target", "continue": "||"},
            },
            {"error": {"code": "internal_api_error", "info": "Query failed"}},
        ]
        warnings = []
        result = self.fetcher._optional_step(
            "links",
            lambda: self.fetcher._links_payload(self.page),
            [],
            warnings,
        )
        self.assertEqual(result, [])
        self.assertEqual(warnings[0]["step"], "links")
        self.assertIn("Query failed", warnings[0]["message"])


@unittest.skipUnless(
    os.environ.get("WIKIPEDIA_LIVE_TEST") == "1", "set WIKIPEDIA_LIVE_TEST=1 for live test"
)
class LiveTests(unittest.TestCase):
    def test_live_small_fetch(self) -> None:
        options = wf.FetchOptions(
            keyword="Lorem ipsum",
            language="en",
            proxy=wf._default_proxy(),
            link_limit=5,
            category_limit=5,
            langlink_limit=5,
            image_limit=5,
            search_limit=1,
        )
        data = wf.WikipediaFetcher(options).fetch()
        if data.get("error"):
            # Treat transport/API failures as environment problems, not bugs.
            self.skipTest(f"live fetch error (network/API): {data['error']}")
        self.assertTrue(data["exists"])
        for key in (
            "page",
            "page_meta",
            "content",
            "sections",
            "links",
            "categories",
            "category_info",
            "references",
            "tables",
            "external_links",
            "image_info",
        ):
            self.assertIn(key, data)
        self.assertGreater(len(data["content"]["plaintext"]), 0)
        self.assertGreater(len(data["content"]["wikitext"]), 0)


def _walk(sections):
    for section in sections:
        yield section
        yield from _walk(section["sections"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

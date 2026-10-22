#!/usr/bin/env python3
"""Fetch a Wikipedia page with Wikipedia-API.

Default example:
    python3 WikipediaFetcher/wikipedia_fetcher.py

Custom usage:
    python3 WikipediaFetcher/wikipedia_fetcher.py --lang ja --keyword "豊臣秀長" --out toyotomi_hidenaga.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape as _html_unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

DEFAULT_KEYWORD = "豊臣秀長"
DEFAULT_LANGUAGE = "ja"
DEFAULT_USER_AGENT = (
    "WikipediaFetcher/0.1 (local research script; contact: replace-me@example.com)"
)
NAMESPACE_NAMES = {
    0: "main",
    1: "talk",
    2: "user",
    3: "user_talk",
    4: "project",
    5: "project_talk",
    6: "file",
    7: "file_talk",
    8: "mediawiki",
    9: "mediawiki_talk",
    10: "template",
    11: "template_talk",
    12: "help",
    13: "help_talk",
    14: "category",
    15: "category_talk",
    100: "portal",
    101: "portal_talk",
    102: "project_namespace",
    103: "project_namespace_talk",
    828: "module",
    829: "module_talk",
}


class MediaWikiAPIError(RuntimeError):
    """An error returned by MediaWiki in an otherwise successful HTTP response."""

    def __init__(self, error: dict[str, Any]) -> None:
        self.code = error.get("code", "unknown")
        self.info = error.get("info", "Unknown MediaWiki API error")
        super().__init__(f"{self.code}: {self.info}")


class FetchStepError(RuntimeError):
    """Wrap an exception with the fetch step that triggered it."""

    def __init__(self, step: str, exc: Exception) -> None:
        self.step = step
        self.original = exc
        super().__init__(f"{step}: {exc}")


@dataclass(frozen=True)
class FetchOptions:
    keyword: str = DEFAULT_KEYWORD
    language: str = DEFAULT_LANGUAGE
    user_agent: str = DEFAULT_USER_AGENT
    link_limit: int = 0
    category_limit: int = 500
    langlink_limit: int = 300
    image_limit: int = 100
    search_limit: int = 5
    proxy: str | None = None
    strict: bool = False


class WikipediaFetcher:
    """Small wrapper around Wikipedia-API for rich single-page snapshots."""

    def __init__(self, options: FetchOptions) -> None:
        wikipediaapi = _load_wikipediaapi()
        kwargs = {"proxy": options.proxy} if options.proxy else {}
        self.options = options
        self.wiki = wikipediaapi.Wikipedia(
            user_agent=options.user_agent,
            language=options.language,
            extract_format=wikipediaapi.ExtractFormat.WIKI,
            **kwargs,
        )

    def close(self) -> None:
        """Close the underlying HTTP client (best effort)."""
        client = getattr(self.wiki, "_client", None)
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    def fetch(self) -> dict[str, Any]:
        try:
            return self._fetch()
        except Exception as exc:  # report remote/API failures cleanly
            if self.options.strict:
                raise
            return {
                "query": self._query_payload(self.options.keyword),
                "exists": False,
                "error": {
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "step": getattr(exc, "step", None),
                    "causes": _exception_causes(exc),
                },
                "fetched_at": _utc_now(),
            }

    def _fetch(self) -> dict[str, Any]:
        warnings = []
        search_results = self._optional_step(
            "search",
            lambda: self._search(self.options.keyword),
            [],
            warnings,
        )
        page = self._required_step(
            "page.construct",
            lambda: self.wiki.page(self.options.keyword),
        )
        requested_title = page.title

        if not self._required_step(
            "page.exists.initial", lambda: _page_exists(page)
        ) and search_results and "title" in search_results[0]:
            page = self._required_step(
                "page.construct.from_search",
                lambda: self.wiki.page(search_results[0]["title"]),
            )

        if not self._required_step("page.exists.final", lambda: _page_exists(page)):
            return {
                "query": self._query_payload(requested_title),
                "exists": False,
                "search_results": search_results,
                "warnings": warnings,
                "fetched_at": _utc_now(),
            }

        parsed_html = self._optional_step(
            "content.html.parse",
            lambda: self._parsed_html(page),
            "",
            warnings,
        )

        content = {
            "summary": self._required_step("content.summary", lambda: page.summary),
            "plaintext": self._optional_step(
                "content.plaintext",
                lambda: self._extracts_payload(page),
                "",
                warnings,
            ),
            "wikitext": self._optional_step(
                "content.wikitext",
                lambda: self._wikitext_payload(page),
                "",
                warnings,
            ),
        }
        links = self._optional_step(
            "links",
            lambda: self._links_payload(page),
            [],
            warnings,
        )
        link_items = self._optional_step(
            "links.items",
            lambda: _links_from_html(parsed_html, self.options.link_limit),
            [],
            warnings,
        )
        categories = self._optional_step(
            "categories",
            lambda: self._category_rows(page),
            [],
            warnings,
        )
        category_info = {
            row["title"]: {
                "sortkey": row.get("sortkey"),
                "hidden": row.get("hidden"),
                "namespace": row.get("namespace"),
            }
            for row in categories
        }
        references = self._optional_step(
            "content.references",
            lambda: _references_from_html(parsed_html),
            [],
            warnings,
        )
        langlinks = self._optional_step(
            "langlinks",
            lambda: self._langlinks_payload(page.langlinks),
            [],
            warnings,
        )
        images = self._optional_step(
            "images",
            lambda: self._images_payload(page.images),
            [],
            warnings,
        )
        image_info = self._optional_step(
            "images.info",
            lambda: self._imageinfo([row["title"] for row in images]),
            {},
            warnings,
        )
        external_links = self._optional_step(
            "external_links",
            lambda: self._external_links_payload(page),
            [],
            warnings,
        )
        page_meta = self._optional_step(
            "page.meta",
            lambda: self._meta_payload(page),
            {},
            warnings,
        )
        redirect_info = self._optional_step(
            "page.redirect",
            lambda: self._redirect_payload(self.options.keyword),
            {},
            warnings,
        )
        page_meta = {
            **page_meta,
            "redirected_from": redirect_info.get("from"),
            "redirect_target": redirect_info.get("to"),
        }
        infobox = self._optional_step(
            "content.infobox",
            lambda: _parse_infobox(parsed_html),
            None,
            warnings,
        )
        tables = self._optional_step(
            "content.tables",
            lambda: _tables_from_html(parsed_html),
            [],
            warnings,
        )

        result = {
            "query": self._query_payload(requested_title),
            "exists": True,
            "page": self._required_step("page.payload", lambda: self._page_payload(page)),
            "page_meta": page_meta,
            "content": content,
            "sections": self._required_step(
                "sections",
                lambda: self._sections_from_html(parsed_html),
            ),
            "links": {
                "items": link_items,
                "by_namespace": _group_by_language_namespace(links),
            },
            "categories": _group_by_language_namespace(categories),
            "category_info": category_info,
            "langlinks": _group_langlinks(langlinks),
            "images": _group_by_language_namespace(images),
            "image_info": image_info,
            "external_links": external_links,
            "references": references,
            "tables": tables,
            "search_results": search_results,
            "fetched_at": _utc_now(),
        }
        if infobox:
            result["infobox"] = infobox
        if parsed_html:
            result["_html"] = parsed_html
        if warnings:
            result["warnings"] = warnings
        return result

    def _required_step(self, step: str, callback: Any) -> Any:
        try:
            return callback()
        except FetchStepError:
            raise
        except Exception as exc:  # add step context for debugging
            raise FetchStepError(step, exc) from exc

    def _optional_step(
        self,
        step: str,
        callback: Any,
        default: Any,
        warnings: list[dict[str, Any]],
    ) -> Any:
        try:
            return callback()
        except Exception as exc:  # keep optional fields independent
            if self.options.strict:
                raise FetchStepError(step, exc) from exc
            warnings.append(
                {
                    "step": step,
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "causes": _exception_causes(exc),
                }
            )
            return default

    def _query_payload(self, requested_title: str) -> dict[str, Any]:
        return {
            "keyword": self.options.keyword,
            "requested_title": requested_title,
            "language": self.options.language,
            "proxy": _redact_proxy(self.options.proxy),
        }

    def _page_payload(self, page: Any) -> dict[str, Any]:
        return {
            "title": page.title,
            "displaytitle": _safe_attr(page, "displaytitle"),
            "pageid": _safe_attr(page, "pageid"),
            "namespace": _safe_attr(page, "ns"),
            "language": _safe_attr(page, "language"),
            "fullurl": _safe_attr(page, "fullurl"),
            "canonicalurl": _safe_attr(page, "canonicalurl"),
        }

    def _api_get(self, language: str, params: dict[str, Any]) -> dict[str, Any]:
        # Wikipedia-API's private transport does not raise for JSON API errors.
        data = self.wiki._get(language, params)
        if "error" in data:
            raise MediaWikiAPIError(data["error"])
        return data

    def _parsed_html(self, page: Any) -> str:
        raw = self._api_get(
            page.language,
            {
                "action": "parse",
                "format": "json",
                "page": page.title,
                "prop": "text",
                "redirects": 1,
            },
        )
        return raw.get("parse", {}).get("text", {}).get("*", "")

    def _single_page(self, params: dict[str, Any]) -> dict[str, Any]:
        data = self._api_get(self.options.language, params)
        return next(iter(data.get("query", {}).get("pages", {}).values()), {})

    def _extracts_payload(self, page: Any) -> str:
        """Full-article plain text via the TextExtracts extension."""
        payload = self._single_page(
            {
                "action": "query",
                "format": "json",
                "prop": "extracts",
                "explaintext": 1,
                "redirects": 1,
                "titles": page.title,
            }
        )
        return payload.get("extract", "")

    def _wikitext_payload(self, page: Any) -> str:
        """Raw wikitext of the current revision."""
        payload = self._single_page(
            {
                "action": "query",
                "format": "json",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "rvlimit": 1,
                "redirects": 1,
                "titles": page.title,
            }
        )
        revisions = payload.get("revisions") or []
        if not revisions:
            return ""
        main = (revisions[0].get("slots") or {}).get("main") or {}
        return main.get("*") or main.get("content") or ""

    def _sections_from_html(self, parsed_html: str) -> list[dict[str, Any]]:
        if not parsed_html:
            return []

        matches = list(_heading_matches(parsed_html))
        roots: list[dict[str, Any]] = []
        stack: list[dict[str, Any]] = []

        for index, match in enumerate(matches):
            heading_html = match.group(0)
            heading_match = re.search(
                r"<h([2-6])\b([^>]*)>(.*?)</h\1>",
                heading_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if heading_match is None:
                continue

            level = int(heading_match.group(1)) - 1
            anchor = _html_attr(heading_match.group(2), "id")
            if anchor is not None:
                anchor = _html_unescape(anchor)
            title = _html_to_text(heading_match.group(3))
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(parsed_html)
            body_html = parsed_html[match.end() : next_start].strip()
            section = {
                "title": title,
                "id": anchor,
                "level": level,
                "text": _html_to_text(body_html),
                "sections": [],
            }

            while stack and stack[-1]["level"] >= level:
                stack.pop()
            if stack:
                stack[-1]["sections"].append(section)
            else:
                roots.append(section)
            stack.append(section)

        return roots

    def _langlinks_payload(self, pages: dict[str, Any]) -> list[dict[str, Any]]:
        items = []
        for index, language in enumerate(sorted(pages.keys())):
            if self.options.langlink_limit and index >= self.options.langlink_limit:
                break
            page = pages[language]
            items.append(
                {
                    "language": language,
                    "title": page.title,
                    "fullurl": _safe_attr(page, "fullurl"),
                }
            )
        return items

    def _images_payload(self, images: dict[str, Any]) -> list[dict[str, Any]]:
        items = []
        for index, title in enumerate(sorted(images.keys())):
            if self.options.image_limit and index >= self.options.image_limit:
                break
            image = images[title]
            items.append(
                {
                    "title": title,
                    "namespace": _safe_attr(image, "ns"),
                    "language": _safe_attr(image, "language"),
                }
            )
        return items

    def _category_rows(self, page: Any) -> list[dict[str, Any]]:
        """All categories with sort keys and hidden flag (includes hidden ones)."""
        items: list[dict[str, Any]] = []
        continuation: dict[str, Any] = {}
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "format": "json",
                "prop": "categories",
                "titles": page.title,
                "clprop": "sortkey|hidden",
                "cllimit": "max",
            }
            params.update(continuation)
            data = self._api_get(self.options.language, params)
            for payload in data.get("query", {}).get("pages", {}).values():
                for category in payload.get("categories", []):
                    items.append(
                        {
                            "title": category["title"],
                            "namespace": category.get("ns"),
                            "language": self.options.language,
                            "sortkey": _decode_sortkey(category.get("sortkey")),
                            "hidden": "hidden" in category,
                        }
                    )
            if "continue" in data:
                continuation = data["continue"]
            else:
                break
        if self.options.category_limit:
            items = items[: self.options.category_limit]
        return items

    def _links_payload(self, page: Any) -> list[dict[str, Any]]:
        """All outgoing wiki links, paging through every continuation batch."""
        items: list[dict[str, Any]] = []
        continuation: dict[str, Any] = {}
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "format": "json",
                "prop": "links",
                "titles": page.title,
                "pllimit": "max",
            }
            params.update(continuation)
            data = self._api_get(self.options.language, params)
            for payload in data.get("query", {}).get("pages", {}).values():
                for link in payload.get("links", []):
                    items.append(
                        {
                            "title": link["title"],
                            "namespace": link.get("ns"),
                            "language": self.options.language,
                        }
                    )
            if self.options.link_limit and len(items) >= self.options.link_limit:
                break
            if "continue" in data:
                continuation = data["continue"]
            else:
                break
        if self.options.link_limit:
            items = items[: self.options.link_limit]
        return items

    def _external_links_payload(self, page: Any) -> list[str]:
        """External URLs referenced by the page, paging through continuations."""
        urls: list[str] = []
        seen: set[str] = set()
        continuation: dict[str, Any] = {}
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "format": "json",
                "prop": "extlinks",
                "titles": page.title,
                "ellimit": "max",
            }
            params.update(continuation)
            data = self._api_get(self.options.language, params)
            for payload in data.get("query", {}).get("pages", {}).values():
                for link in payload.get("extlinks", []):
                    url = link.get("*")
                    if url and url not in seen:
                        seen.add(url)
                        urls.append(url)
            if "continue" in data:
                continuation = data["continue"]
            else:
                break
        return urls

    def _meta_payload(self, page: Any) -> dict[str, Any]:
        """Page-level metadata: pageprops, page image, info and last revision."""
        params: dict[str, Any] = {
            "action": "query",
            "format": "json",
            "prop": "pageprops|pageimages|info|revisions",
            "titles": page.title,
            "piprop": "original|thumbnail|name",
            "pithumbsize": 800,
            "inprop": "protection|url|displaytitle",
            "rvprop": "ids|timestamp|size|user|comment",
            "rvlimit": 1,
        }
        data = self._api_get(self.options.language, params)
        payload = next(
            iter(data.get("query", {}).get("pages", {}).values()), {}
        )
        pageprops = payload.get("pageprops", {})
        revision = (payload.get("revisions") or [{}])[0]
        page_image: dict[str, Any] = {}
        if payload.get("original") or payload.get("thumbnail") or payload.get("pageimage"):
            page_image = {
                "name": payload.get("pageimage"),
                "original": payload.get("original"),
                "thumbnail": payload.get("thumbnail"),
            }
        return {
            "wikibase_item": pageprops.get("wikibase_item"),
            "defaultsort": pageprops.get("defaultsort"),
            "page_image_free": pageprops.get("page_image_free"),
            "disambiguation": "disambiguation" in pageprops,
            "contentmodel": payload.get("contentmodel"),
            "pagelanguage": payload.get("pagelanguage"),
            "lastrevid": payload.get("lastrevid"),
            "touched": payload.get("touched"),
            "length": payload.get("length"),
            "redirect": payload.get("redirect"),
            "protection": payload.get("protection", []),
            "displaytitle": payload.get("displaytitle"),
            "page_image": page_image,
            "revision": {
                "revid": revision.get("revid"),
                "parentid": revision.get("parentid"),
                "timestamp": revision.get("timestamp"),
                "size": revision.get("size"),
                "user": revision.get("user"),
                "comment": revision.get("comment"),
            },
        }

    def _imageinfo(self, titles: list[str]) -> dict[str, dict[str, Any]]:
        """Resolve image titles to media URLs, dimensions and license metadata."""
        info: dict[str, dict[str, Any]] = {}
        clean = [title for title in titles if title]
        for start in range(0, len(clean), 50):
            chunk = clean[start : start + 50]
            params: dict[str, Any] = {
                "action": "query",
                "format": "json",
                "prop": "imageinfo",
                "titles": "|".join(chunk),
                "iiprop": "url|size|mime|extmetadata",
                "iilimit": 1,
            }
            data = self._api_get(self.options.language, params)
            query = data.get("query", {})
            normalized = {
                item["from"]: item["to"] for item in query.get("normalized", [])
            }
            for payload in query.get("pages", {}).values():
                imageinfo = (payload.get("imageinfo") or [{}])[0]
                if not imageinfo:
                    continue
                entry: dict[str, Any] = {
                    "url": imageinfo.get("url"),
                    "width": imageinfo.get("width"),
                    "height": imageinfo.get("height"),
                    "mime": imageinfo.get("mime"),
                    "byte_size": imageinfo.get("size"),
                    **_extmetadata(imageinfo.get("extmetadata", {})),
                }
                info[payload["title"]] = entry
            for original, canonical in normalized.items():
                if canonical in info and original not in info:
                    info[original] = info[canonical]
        return info

    def _search(self, keyword: str) -> list[dict[str, Any]]:
        try:
            results = self.wiki.search(keyword, limit=self.options.search_limit)
        except Exception as exc:  # keep fetch resilient for CLI use
            if self.options.strict:
                raise
            return [{"error": str(exc)}]

        pages = getattr(results, "pages", {})
        items = []
        for title, page in pages.items():
            meta = getattr(page, "search_meta", None)
            snippet = _safe_attr(meta, "snippet")
            items.append(
                {
                    "title": title,
                    "snippet": _html_to_text(snippet) if snippet else snippet,
                    "wordcount": _safe_attr(meta, "wordcount"),
                    "size": _safe_attr(meta, "size"),
                    "timestamp": _safe_attr(meta, "timestamp"),
                }
            )
        return items

    def _redirect_payload(self, title: str) -> dict[str, str]:
        """Resolve whether the requested title is a redirect and to where."""
        params: dict[str, Any] = {
            "action": "query",
            "format": "json",
            "titles": title,
            "redirects": 1,
        }
        data = self._api_get(self.options.language, params)
        redirects = data.get("query", {}).get("redirects", [])
        if not redirects:
            return {}
        return {"from": redirects[0].get("from"), "to": redirects[0].get("to")}


def _load_wikipediaapi() -> Any:
    try:
        import wikipediaapi
    except ImportError as exc:  # pragma: no cover - friendly CLI failure
        raise SystemExit(
            "Missing dependency: wikipediaapi. Install it with "
            "`python3 -m pip install -r requirements.txt`."
        ) from exc
    return wikipediaapi


def _heading_matches(html: str) -> list[re.Match[str]]:
    heading = r"<h[2-6]\b[^>]*>.*?</h[2-6]>"
    return list(
        re.finditer(
            rf"<div\b[^>]*class=\"[^\"]*\bmw-heading\b[^\"]*\"[^>]*>\s*{heading}.*?</div>|{heading}",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"br", "p", "div", "blockquote", "tr"} or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"p", "div", "blockquote", "li", "tr"} or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def _html_attr(attrs: str, name: str) -> str | None:
    match = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
    return match.group(1) if match else None


def _decode_sortkey(value: Any) -> str | None:
    """MediaWiki returns category sort keys as UTF-8 hex; decode when possible."""
    if not value:
        return None
    try:
        return bytes.fromhex(str(value)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return str(value)


_REFERENCE_LI = re.compile(
    r'<li\b[^>]*\bid="cite(?:&#95;|_)note-([^"]+)"[^>]*>',
    flags=re.IGNORECASE,
)
_REFERENCE_BACKLINK = re.compile(r'href="#(cite_ref-[^"]+)"', flags=re.IGNORECASE)
_REFERENCE_URL = re.compile(r'href="(https?://[^"]+)"', flags=re.IGNORECASE)
_REFERENCE_TEXT = re.compile(
    r'<span class="reference-text">(.*)', flags=re.IGNORECASE | re.DOTALL
)


def _references_from_html(parsed_html: str) -> list[dict[str, Any]]:
    """Structure the rendered footnotes (notes and citations) from parsed HTML."""
    if not parsed_html:
        return []

    matches = list(_REFERENCE_LI.finditer(parsed_html))
    references: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(parsed_html)
        segment = parsed_html[match.end() : end]
        closing = segment.find("</ol>")
        if closing != -1:
            segment = segment[:closing]

        group = _reference_group(parsed_html, match.start())
        backlinks = [
            _html_unescape(item) for item in _REFERENCE_BACKLINK.findall(segment)
        ]
        urls: list[str] = []
        for url in _REFERENCE_URL.findall(segment):
            url = _html_unescape(url)
            if url not in urls:
                urls.append(url)

        text_match = _REFERENCE_TEXT.search(segment)
        text_html = text_match.group(1) if text_match else segment
        text = _html_to_text(text_html).lstrip("^").strip()
        references.append(
            {
                "id": "cite_note-" + _html_unescape(match.group(1)),
                "group": group,
                "backlinks": backlinks,
                "urls": urls,
                "text": text,
            }
        )
    return references


def _reference_group(parsed_html: str, position: int) -> str | None:
    """Return the data-mw-group of the enclosing references <ol>, if any."""
    opening = parsed_html.rfind('<ol class="references"', 0, position)
    if opening == -1:
        return None
    tag_end = parsed_html.find(">", opening)
    return _html_attr(parsed_html[opening:tag_end], "data-mw-group")


_ANCHOR_TAG = re.compile(r"<a\b([^>]*)>(.*?)</a>", flags=re.IGNORECASE | re.DOTALL)


def _link_target(href: str | None) -> tuple[str | None, bool]:
    """Resolve an anchor href to (target title, is_redlink); None for non-links."""
    href = _html_unescape(href or "")
    if not href or href.startswith("#"):
        return None, False
    if href.startswith("/wiki/"):
        target = unquote(href[len("/wiki/") :].split("#")[0])
        return target.replace("_", " "), False
    if "redlink=1" in href and "title=" in href:
        raw = href.split("title=", 1)[1].split("&", 1)[0]
        return unquote(raw).replace("_", " "), True
    return None, False


def _links_from_html(parsed_html: str, limit: int = 0) -> list[dict[str, Any]]:
    """Collect internal wiki links as ordered {anchor, title, href} occurrences."""
    if not parsed_html:
        return []
    items: list[dict[str, Any]] = []
    index: dict[tuple[str, str], int] = {}
    for match in _ANCHOR_TAG.finditer(parsed_html):
        href = _html_attr(match.group(1), "href")
        title, redlink = _link_target(href)
        if not title:
            continue
        anchor = _html_to_text(match.group(2)).strip()
        if not anchor:
            continue
        key = (title, anchor)
        position = index.get(key)
        if position is not None:
            items[position]["count"] += 1
            continue
        index[key] = len(items)
        items.append(
            {
                "anchor": anchor,
                "title": title,
                "href": _html_unescape(href),
                "redlink": redlink,
                "count": 1,
            }
        )
    if limit:
        items = items[:limit]
    return items




_EXTMETA_KEYS = {
    "Artist": "artist",
    "LicenseShortName": "license",
    "LicenseUrl": "license_url",
    "ImageDescription": "description",
    "Credit": "credit",
    "DateTimeOriginal": "date_original",
    "ObjectName": "object_name",
}


def _extmetadata(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source, target in _EXTMETA_KEYS.items():
        item = raw.get(source)
        if isinstance(item, dict) and item.get("value") is not None:
            out[target] = _html_to_text(str(item["value"]))
    return out


def _find_infobox_html(html: str) -> str:
    for opening in re.finditer(r"<table\b[^>]*>", html, flags=re.IGNORECASE):
        if not re.search(
            r'class="[^"]*\binfobox\b', opening.group(0), flags=re.IGNORECASE
        ):
            continue
        depth = 0
        for token in re.finditer(
            r"<table\b|</table\s*>",
            html[opening.start() :],
            flags=re.IGNORECASE,
        ):
            depth += -1 if token.group(0).lower().startswith("</") else 1
            if depth == 0:
                return html[opening.start() : opening.start() + token.end()]
    return ""


class _InfoboxParser(HTMLParser):
    """Collect rows/cells of a single infobox table, ignoring nested tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.caption: str | None = None
        self.rows: list[list[dict[str, str]]] = []
        self._parts: list[str] | None = None
        self._kind: str | None = None
        self._row: list[dict[str, str]] | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if tag == "table":
            self.depth += 1
            return
        if self._skip:
            return
        if tag == "caption":
            self._close_cell()
            self._parts = []
            self._kind = "caption"
            return
        if self.depth != 1:
            return
        if tag == "tr":
            self._close_cell()
            self._close_row()
            self._row = []
        elif tag in {"th", "td"}:
            self._close_cell()
            self._parts = []
            self._kind = tag
        elif tag == "br" and self._parts is not None:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "table":
            if self.depth == 1:
                self._close_cell()
                self._close_row()
            self.depth = max(0, self.depth - 1)
            return
        if tag == "caption":
            self._close_cell()
            return
        if self.depth != 1:
            return
        if tag in {"th", "td"}:
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            self._close_row()

    def handle_data(self, data: str) -> None:
        if self._skip or self._parts is None:
            return
        self._parts.append(data)

    def _close_cell(self) -> None:
        if self._parts is None:
            return
        text = re.sub(r"\s+", " ", "".join(self._parts)).strip()
        if self._kind == "caption":
            self.caption = text or self.caption
        elif self._row is not None:
            self._row.append({"kind": self._kind or "td", "text": text})
        self._parts = None
        self._kind = None

    def _close_row(self) -> None:
        if self._row:
            self.rows.append(self._row)
        self._row = None


def _parse_infobox(html: str) -> dict[str, Any] | None:
    snippet = _find_infobox_html(html)
    if not snippet:
        return None

    parser = _InfoboxParser()
    parser.feed(snippet)
    parser.close()

    rows: list[dict[str, str]] = []
    for cells in parser.rows:
        filled = [cell for cell in cells if cell["text"]]
        if not filled:
            continue
        if len(filled) == 1 and filled[0]["kind"] == "th":
            rows.append({"header": filled[0]["text"]})
        elif filled[0]["kind"] == "th":
            rows.append(
                {
                    "label": filled[0]["text"],
                    "value": " / ".join(cell["text"] for cell in filled[1:]),
                }
            )
        else:
            rows.append({"value": " / ".join(cell["text"] for cell in filled)})

    if not parser.caption and not rows:
        return None
    return {"caption": parser.caption, "rows": rows}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_top_level_tables(html: str) -> Iterator[tuple[re.Match[str], str]]:
    """Yield (opening match, table snippet) for non-nested top-level tables."""
    consumed = 0
    for opening in re.finditer(r"<table\b[^>]*>", html, flags=re.IGNORECASE):
        if opening.start() < consumed:
            continue
        depth = 0
        end = None
        for token in re.finditer(
            r"<table\b|</table\s*>", html[opening.start() :], flags=re.IGNORECASE
        ):
            depth += -1 if token.group(0).lower().startswith("</") else 1
            if depth == 0:
                end = opening.start() + token.end()
                break
        if end is None:
            continue
        consumed = end
        yield opening, html[opening.start() : end]


class _TableParser(HTMLParser):
    """Parse one table into rows of cells (text, header flag, spans, links)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.caption: str | None = None
        self.rows: list[list[dict[str, Any]]] = []
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if tag == "table":
            self.depth += 1
            return
        if self._skip:
            return
        if tag == "caption":
            self._close_cell()
            self._cell = {"parts": [], "caption": True, "links": []}
            return
        if self.depth != 1:
            return
        if tag == "tr":
            self._close_cell()
            self._close_row()
            self._row = []
        elif tag in {"th", "td"}:
            self._close_cell()
            self._cell = {
                "parts": [],
                "header": tag == "th",
                "colspan": _int_or_none(attributes.get("colspan")),
                "rowspan": _int_or_none(attributes.get("rowspan")),
                "links": [],
            }
        elif tag == "a" and self._cell is not None:
            href = attributes.get("href") or ""
            if href.startswith("/wiki/"):
                target = unquote(href[len("/wiki/") :].split("#")[0])
                self._cell["links"].append(target.replace("_", " "))
        elif tag == "br" and self._cell is not None:
            self._cell["parts"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "table":
            if self.depth == 1:
                self._close_cell()
                self._close_row()
            self.depth = max(0, self.depth - 1)
            return
        if tag == "caption":
            self._close_cell()
            return
        if self.depth != 1:
            return
        if tag in {"th", "td"}:
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            self._close_row()

    def handle_data(self, data: str) -> None:
        if self._skip or self._cell is None:
            return
        self._cell["parts"].append(data)

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        cell = self._cell
        self._cell = None
        text = re.sub(r"\s+", " ", "".join(cell["parts"])).strip()
        if cell.get("caption"):
            self.caption = text or self.caption
            return
        if self._row is not None:
            cell["text"] = text
            cell.pop("parts", None)
            self._row.append(cell)

    def _close_row(self) -> None:
        if self._row:
            self.rows.append(self._row)
        self._row = None


def _tables_from_html(parsed_html: str) -> list[dict[str, Any]]:
    """Extract top-level body tables (excluding infoboxes) as structured rows."""
    if not parsed_html:
        return []

    tables: list[dict[str, Any]] = []
    for opening, snippet in _iter_top_level_tables(parsed_html):
        attributes = opening.group(0)
        class_attr = _html_attr(attributes, "class") or ""
        if "infobox" in class_attr.split():
            continue
        parser = _TableParser()
        parser.feed(snippet)
        parser.close()
        if not parser.rows and not parser.caption:
            continue
        tables.append(
            {
                "id": _html_attr(attributes, "id"),
                "classes": class_attr.split(),
                "caption": parser.caption,
                "rows": parser.rows,
            }
        )
    return tables


def _group_by_language_namespace(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        language = str(row.get("language") or "unknown")
        namespace = row.get("namespace")
        namespace_key = str(namespace) if namespace is not None else "unknown"

        grouped.setdefault(language, {})
        grouped[language].setdefault(
            namespace_key,
            {
                "namespace": namespace,
                "namespace_name": _namespace_name(namespace),
                "titles": [],
            },
        )
        grouped[language][namespace_key]["titles"].append(row["title"])

    return {
        language: {
            namespace: {
                **bucket,
                "count": len(bucket["titles"]),
                "titles": sorted(bucket["titles"]),
            }
            for namespace, bucket in sorted(
                namespaces.items(),
                key=lambda item: _namespace_sort_key(item[0]),
            )
        }
        for language, namespaces in sorted(grouped.items())
    }


def _group_langlinks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["language"]), []).append(
            {
                "title": row["title"],
                "fullurl": row.get("fullurl"),
            }
        )
    return {
        language: sorted(items, key=lambda item: item["title"])
        for language, items in sorted(grouped.items())
    }


def _namespace_name(namespace: Any) -> str:
    if namespace is None:
        return "unknown"
    try:
        return NAMESPACE_NAMES.get(int(namespace), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def _namespace_sort_key(namespace: str) -> tuple[int, int | str]:
    if namespace == "unknown":
        return (1, namespace)
    return (0, int(namespace))


def _page_exists(page: Any) -> bool:
    """Return page existence, tolerating either a bool property or a method."""
    value = page.exists
    return bool(value()) if callable(value) else bool(value)


def _safe_attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    try:
        value = getattr(obj, name)
    except Exception:  # noqa: BLE001 - optional remote fields may fail independently
        return None
    if callable(value):
        return None
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _exception_causes(exc: BaseException) -> list[dict[str, str]]:
    causes = []
    cause = exc.__cause__
    while cause is not None:
        causes.append(
            {
                "type": cause.__class__.__name__,
                "message": str(cause),
            }
        )
        cause = cause.__cause__
    return causes


def _redact_proxy(proxy: str | None) -> str | None:
    """Remove proxy credentials from output, hiding unparseable values entirely."""
    if not proxy:
        return proxy
    try:
        parsed = urlsplit(proxy)
    except ValueError:
        return "<redacted>"
    if not parsed.netloc:
        return "<redacted>"
    if parsed.username is None:
        return proxy
    return parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1]).geturl()


def _default_proxy() -> str | None:
    return (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("all_proxy")
        or os.environ.get("ALL_PROXY")
    )


def _fetch_source_html(url: str, user_agent: str, proxy: str | None) -> str:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with opener.open(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _write_text(path: Path, text: str) -> str | None:
    """Write text to path, returning an error message on failure (None on success)."""
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch a rich single-page Wikipedia snapshot."
    )
    parser.add_argument(
        "--keyword",
        default=DEFAULT_KEYWORD,
        help=f"Page title or search keyword. Default: {DEFAULT_KEYWORD}",
    )
    parser.add_argument(
        "--lang",
        default=DEFAULT_LANGUAGE,
        help=f"Wikipedia language code. Default: {DEFAULT_LANGUAGE}",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON to this file instead of stdout.",
    )
    parser.add_argument(
        "--html-out",
        type=Path,
        default=None,
        help=(
            "Write parsed article-body HTML fragment to this file. "
            "Defaults to OUT with .html suffix."
        ),
    )
    parser.add_argument(
        "--source-html-out",
        type=Path,
        default=None,
        help="Write full server-rendered page HTML source to this file.",
    )
    parser.add_argument(
        "--wikitext-out",
        type=Path,
        default=None,
        help=(
            "Write raw wikitext to this file instead of embedding it in JSON. "
            "Defaults to OUT with .wiki suffix when writing to a file."
        ),
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="Wikimedia-compliant User-Agent. Include project/contact info.",
    )
    parser.add_argument(
        "--link-limit",
        type=int,
        default=0,
        help="Max outgoing links to keep. 0 (default) means no limit.",
    )
    parser.add_argument("--category-limit", type=int, default=500)
    parser.add_argument("--langlink-limit", type=int, default=300)
    parser.add_argument("--image-limit", type=int, default=100)
    parser.add_argument("--search-limit", type=int, default=5)
    parser.add_argument(
        "--proxy",
        default=_default_proxy(),
        help="HTTP proxy passed to httpx through Wikipedia-API.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Raise on any step failure instead of downgrading optional steps "
            "to warnings."
        ),
    )
    args = parser.parse_args(argv)
    if args.out is not None:
        if args.html_out is None:
            args.html_out = args.out.with_suffix(".html")
        if args.wikitext_out is None:
            args.wikitext_out = args.out.with_suffix(".wiki")

    outputs = {
        "--out": args.out,
        "--html-out": args.html_out,
        "--wikitext-out": args.wikitext_out,
        "--source-html-out": args.source_html_out,
    }
    seen: dict[Path, str] = {}
    for option, path in outputs.items():
        if path is None:
            continue
        resolved = path.resolve()
        if resolved in seen:
            parser.error(
                f"Output paths must be distinct: {option} and {seen[resolved]} "
                f"both resolve to {resolved}"
            )
        seen[resolved] = option
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    options = FetchOptions(
        keyword=args.keyword,
        language=args.lang,
        user_agent=args.user_agent,
        link_limit=args.link_limit,
        category_limit=args.category_limit,
        langlink_limit=args.langlink_limit,
        image_limit=args.image_limit,
        search_limit=args.search_limit,
        proxy=args.proxy,
        strict=args.strict,
    )
    fetcher = WikipediaFetcher(options)
    try:
        data = fetcher.fetch()
    finally:
        fetcher.close()
    html = data.pop("_html", None)
    write_failed = False
    html_out = args.html_out
    if html and html_out:
        error = _write_text(html_out, html)
        if error:
            data.setdefault("warnings", []).append(
                {
                    "step": "content.html.write",
                    "type": "WriteError",
                    "message": f"{html_out}: {error}",
                    "causes": [],
                }
            )
            write_failed = True
        else:
            data.setdefault("content", {})["html_file"] = str(html_out)

    wikitext = (data.get("content") or {}).get("wikitext")
    wikitext_out = args.wikitext_out
    if wikitext and wikitext_out:
        error = _write_text(wikitext_out, wikitext)
        if error:
            data.setdefault("warnings", []).append(
                {
                    "step": "content.wikitext.write",
                    "type": "WriteError",
                    "message": f"{wikitext_out}: {error}",
                    "causes": [],
                }
            )
            write_failed = True
        else:
            data["content"]["wikitext_file"] = str(wikitext_out)
            data["content"].pop("wikitext", None)

    source_html_out = args.source_html_out
    if source_html_out and data.get("exists"):
        page = data.get("page") or {}
        page_url = page.get("canonicalurl") or page.get("fullurl")
        if page_url:
            try:
                source_html = _fetch_source_html(
                    page_url,
                    options.user_agent,
                    options.proxy,
                )
            except Exception as exc:  # keep JSON output useful by default
                if options.strict:
                    raise FetchStepError("content.source_html.fetch", exc) from exc
                data.setdefault("warnings", []).append(
                    {
                        "step": "content.source_html.fetch",
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                        "causes": _exception_causes(exc),
                    }
                )
            else:
                error = _write_text(source_html_out, source_html)
                if error:
                    data.setdefault("warnings", []).append(
                        {
                            "step": "content.source_html.write",
                            "type": "WriteError",
                            "message": f"{source_html_out}: {error}",
                            "causes": [],
                        }
                    )
                    write_failed = True
                else:
                    data.setdefault("content", {})["source_html_file"] = str(
                        source_html_out
                    )
        else:
            message = "Page payload has no canonicalurl or fullurl."
            if options.strict:
                exc = ValueError(message)
                raise FetchStepError("content.source_html.fetch", exc) from exc
            data.setdefault("warnings", []).append(
                {
                    "step": "content.source_html.fetch",
                    "type": "MissingPageUrl",
                    "message": message,
                    "causes": [],
                }
            )

    encoded = json.dumps(data, ensure_ascii=False, indent=2)

    if args.out:
        error = _write_text(args.out, encoded + "\n")
        if error:
            print(f"error: failed to write {args.out}: {error}", file=sys.stderr)
            return 2
        print(f"Wrote {args.out}")
        if data.get("content", {}).get("html_file"):
            print(f"Wrote {html_out}")
        if data.get("content", {}).get("wikitext_file"):
            print(f"Wrote {wikitext_out}")
        if source_html_out and data.get("content", {}).get("source_html_file"):
            print(f"Wrote {source_html_out}")
    else:
        print(encoded)
    if data.get("error") or write_failed:
        return 2
    return 0 if data.get("exists") else 1


if __name__ == "__main__":
    raise SystemExit(main())

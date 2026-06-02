"""
Web tools — WebFetch and WebSearch.
Ported from Rust: tools/src/lib.rs execute_web_fetch() / execute_web_search().
"""

import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter


# Rust uses a 20-second client timeout (tools/src/lib.rs build_http_client).
DEFAULT_TIMEOUT = 20

# Rust User-Agent for the http client.
DEFAULT_USER_AGENT = "clawd-rust-tools/0.1"

# Rust caps redirects at 10.
MAX_REDIRECTS = 10

# Rust truncates the dedup'd hit list to 8 (execute_web_search).
WEB_SEARCH_RESULT_CAP = 8


@dataclass
class WebFetchOutput:
    bytes: int
    code: int
    code_text: str
    result: str
    duration_ms: int
    url: str


@dataclass
class WebSearchOutput:
    query: str
    results: list[dict]
    duration_seconds: float


# ---------------------------------------------------------------------------
# WebFetch — Rust execute_web_fetch()
# ---------------------------------------------------------------------------

def web_fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> WebFetchOutput:
    """
    Fetch a URL and return readable text content.
    Ported from Rust: tools/src/lib.rs execute_web_fetch().
    """
    started = time.monotonic()
    final_url = _upgrade_http_to_https(url)

    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    session.mount("http://", HTTPAdapter(max_retries=0))
    session.mount("https://", HTTPAdapter(max_retries=0))

    headers = {"User-Agent": DEFAULT_USER_AGENT}
    response = session.get(final_url, headers=headers, timeout=timeout)

    raw_text = response.text
    content_type = response.headers.get("content-type", "")
    if "html" in content_type.lower():
        body = _strip_html(raw_text)
    else:
        body = raw_text

    duration_ms = int((time.monotonic() - started) * 1000)
    return WebFetchOutput(
        bytes=len(response.content),
        code=response.status_code,
        code_text=response.reason or "",
        result=body,
        duration_ms=duration_ms,
        url=final_url,
    )


def _upgrade_http_to_https(url: str) -> str:
    """Mirrors Rust's auto-upgrade: rewrite non-localhost http:// to https://."""
    parsed = urlparse(url)
    if parsed.scheme != "http":
        return url
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        return url
    return url.replace("http://", "https://", 1)


# ---------------------------------------------------------------------------
# WebSearch — Rust execute_web_search()
# ---------------------------------------------------------------------------

def web_search(
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> WebSearchOutput:
    """
    Search the web using DuckDuckGo. Mirrors Rust execute_web_search().

    Honors:
      - CLAWD_WEB_SEARCH_BASE_URL env override
      - allowed_domains / blocked_domains filters
      - dedupe + 8-result truncation
    """
    started = time.monotonic()
    base_url = os.environ.get(
        "CLAWD_WEB_SEARCH_BASE_URL",
        "https://html.duckduckgo.com/html/",
    )

    headers = {"User-Agent": DEFAULT_USER_AGENT}
    resp = requests.post(
        base_url,
        data={"q": query},
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()

    pattern = r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
    raw_hits = re.findall(pattern, resp.text)
    if not raw_hits:
        # DuckDuckGo HTML format fallback — generic anchor extraction.
        raw_hits = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', resp.text)

    seen: set[str] = set()
    results: list[dict] = []
    allow = {d.lower() for d in (allowed_domains or [])}
    block = {d.lower() for d in (blocked_domains or [])}

    for href, title in raw_hits:
        url = href.strip()
        if not url or url in seen:
            continue
        host = (urlparse(url).hostname or "").lower()
        if allow and not any(host == d or host.endswith("." + d) for d in allow):
            continue
        if block and any(host == d or host.endswith("." + d) for d in block):
            continue
        seen.add(url)
        results.append({"title": title.strip(), "url": url, "body": ""})
        if len(results) >= WEB_SEARCH_RESULT_CAP:
            break

    duration = time.monotonic() - started
    return WebSearchOutput(query=query, results=results, duration_seconds=duration)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    text = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    for ent, char in [
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&nbsp;", " "),
    ]:
        text = text.replace(ent, char)
    return re.sub(r"\s+", " ", text).strip()

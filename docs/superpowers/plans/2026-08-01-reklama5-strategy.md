# Reklama5 Search Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic, bounded Reklama5 search strategy that silently baselines the first successful result window and sends only newly observed numeric ad IDs to Discord.

**Architecture:** Keep trust-boundary code in `reklama5_http.py`: parse one configured search scope, build canonical page requests, validate redirects, and enforce one mutable per-attempt budget across all requests. Keep source semantics in `reklama5.py`: validate page structure, parse generic cards and Skopje timestamps, stop only on selector-backed terminal evidence, deduplicate IDs, order delivery oldest first, and map normalized listings into `EntryData`. The existing `FetchRetryPolicy` remains the owner of complete-attempt retries, so each retry creates a fresh budget and starts again at page 1.

**Tech Stack:** Python 3.12, `requests`, BeautifulSoup 4, Pydantic 2, `zoneinfo` plus `tzdata`, pytest, uv, Ruff, mypy.

## Global Constraints

- Match the repository's synchronous `requests` transport conventions. Do not add a second HTTP client.
- Use the configured Reklama5 URL as the scope boundary. Do not hard-code category, city, keyword, seller, condition, transaction, price, or category-specific facets.
- Fetch at most pages 1, 2, and 3 per strategy attempt. Stop earlier only on validated terminal evidence.
- Set `seed_existing_on_first_fetch = True`, keep `require_entries_for_initialization = False`, and leave both delivery limits as `None`.
- Accept only HTTPS, effective port 443, no credentials, host `reklama5.mk` or `www.reklama5.mk`, and the approved `/Search` path family. Reject fragments.
- Preserve all caller-owned query pairs, including order, repeated keys, and blank values. Replace every case-insensitive occurrence of `SortByPrice`, `pageView`, and `page` with one canonical occurrence.
- Keep redirects on the exact configured origin and in the accepted search path family. Require filter-equivalent query multimaps.
- Give each complete strategy attempt a 120-second monotonic deadline, a cumulative ten-redirect limit, a 2 MiB per-response body limit, and a 6 MiB cumulative body limit. Count post-decompression body bytes from redirects and final responses.
- Treat connection failures, timeouts, HTTP 408, HTTP 429, and HTTP 500 through 599 as retryable. Treat every other HTTP status from 400 through 599 and every URL, redirect, budget, parse, and page-validation failure as permanent.
- Reuse `rss2discord.retries.parse_retry_after()` for both delta-seconds and HTTP-date values.
- Parse only organic `#sr-holder > .ad-top-div` rows. A missing `.promotedBtn` is organic; an exact normalized `Промовирано` marker excludes the row; every other present marker value, including normalized empty text, invalidates the page. Keep highlighted organic rows and ignore carousel links.
- Localize accepted timestamps to `Europe/Skopje`; use `fold=0` for ambiguous times and reject nonexistent local times. Add `tzdata` as a runtime dependency.
- Introduce every production behavior test-first. Run the named RED command and observe the stated failure before adding its implementation.
- Do not change the delivery-store schema, scheduler API, price snapshots, or adaptive frontier state.

---

## File Structure

- Create `src/rss2discord/transports/reklama5_http.py`: trusted search-scope value, canonical page requests, redirect checks, bounded streamed reads, HTTP classification, and mutable per-attempt budget.
- Create `src/rss2discord/transports/reklama5.py`: normalized listing/page values, timestamp and card parsing, page validation, three-page traversal, deduplication, ordering, and `EntryData` mapping.
- Create `tests/reklama5_helpers.py`: compact HTML builders and synchronous `requests.get` fakes that record URLs, timeouts, and response bodies.
- Create `tests/fixtures/reklama5/rich_page.html`: one representative computer-parts page with generic card fields, promotion, highlight, carousel, malformed-card, paginator, and current-page markers.
- Create `tests/fixtures/reklama5/generic_page.html`: a non-computer category/filter page proving that parsing is generic.
- Create `tests/fixtures/reklama5/zero_results.html`: the explicit zero-count structure with no active paginator item or page links.
- Create `tests/fixtures/reklama5/application_error_page.html`: the exact first-party application-error text embedded in otherwise valid search-result structure.
- Create `tests/test_reklama5_http.py`: URL, query, redirect, status, byte-budget, redirect-budget, and deadline behavior.
- Create `tests/test_reklama5.py`: timestamp, card, page-validation, pagination, ordering, deduplication, and mapping behavior.
- Create `tests/test_reklama5_integration.py`: configuration, app registration, baseline/delivery lifecycle, same-ID silence, and Discord source label behavior.
- Modify `tests/test_discord_source_metadata.py` and `tests/discord_components_helpers.py`: exercise the Reklama5 label through the real Components v2 metadata payload seam.
- Modify `src/rss2discord/configuration.py`: add the strategy literal and keep adapters and price monitoring invalid for it.
- Modify `src/rss2discord/transports/__init__.py`: export `Reklama5Strategy`.
- Modify `src/rss2discord/app.py`: register `Reklama5Strategy`.
- Modify `src/rss2discord/discord/source_labels.py`: add the constant and exhaustive strategy branch.
- Modify `tests/test_config_example.py`: prove the checked-in example contains the approved Reklama5 feed without price monitoring.
- Modify `config/config.example.yaml`: add the annotated category-584 example and strategy documentation.
- Modify `README.md`: document generic search URLs, silent baseline, bounded best-effort semantics, same-ID silence, and the three-page maximum.
- Modify `pyproject.toml` and `uv.lock`: add and lock runtime `tzdata`.

### Task 1: Trusted Search Scope and Canonical Page Requests

**Files:**
- Create: `tests/reklama5_helpers.py`
- Create: `tests/test_reklama5_http.py`
- Create: `src/rss2discord/transports/reklama5_http.py`

**Interfaces:**
- Consumes: `FeedFetchError(strategy: str, cause_type: str, *, status_code: int | None = None, retryable: bool = False, retry_after: float | None = None)`.
- Produces: `REKLAMA5_LABEL: Final = "Reklama5"`, `Reklama5SearchScope.from_url(url: str) -> Reklama5SearchScope`, `Reklama5SearchScope.page_request(page: int) -> Reklama5PageRequest`, and frozen `Reklama5PageRequest(scope: Reklama5SearchScope, page: int, url: str)`.
- Produces: `Reklama5SearchScope.accepts_redirect(request: Reklama5PageRequest, absolute_target_url: str) -> bool`, used by Task 2. The method receives an already-resolved absolute target and never resolves `Location` itself.

- [ ] **Step 1: Add test helpers and failing scope tests**

Create `tests/reklama5_helpers.py` with `SEARCH_URL` set to the approved category-584 URL and typed helpers that later tasks can extend. Start `tests/test_reklama5_http.py` with these exact behaviors:

```python
@pytest.mark.parametrize(
    "url",
    [
        "http://reklama5.mk/Search?cat=584",
        "https://example.test/Search?cat=584",
        "https://reklama5.mk:444/Search?cat=584",
        "https://user:secret@reklama5.mk/Search?cat=584",
        "https://reklama5.mk/Other?cat=584",
        "https://reklama5.mk/Search?cat=584#results",
    ],
)
def test_reklama5_scope_rejects_urls_outside_the_search_boundary(url: str) -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5SearchScope.from_url(url)
    assert fetch_error.value.cause_type == "InvalidUrl"
    assert "secret" not in str(fetch_error.value)


def test_reklama5_page_request_preserves_filters_and_replaces_owned_keys() -> None:
    scope = Reklama5SearchScope.from_url(
        "https://www.reklama5.mk/Search/?cat=584&tag=&tag=x&sortbyprice=9&PAGE=8&pageView=7"
    )
    request = scope.page_request(2)
    assert parse_qsl(urlsplit(request.url).query, keep_blank_values=True) == [
        ("cat", "584"),
        ("tag", ""),
        ("tag", "x"),
        ("SortByPrice", "2"),
        ("pageView", "1"),
        ("page", "2"),
    ]
```

Also cover both accepted hosts, default and explicit port 443, all four accepted paths, page values 1 through 3, rejection of page 0 and page 4, and no apex-to-`www` redirect. Pass absolute redirect targets directly to `accepts_redirect()`. Redirect tests must prove that query reordering is accepted while an added, dropped, changed, duplicated, or blank-changed filter is rejected. Normalize only the three owned key names case-insensitively when comparing effective query multimaps.

- [ ] **Step 2: Run the scope tests and record RED**

Run: `uv run pytest tests/test_reklama5_http.py -q`

Expected: collection fails because `rss2discord.transports.reklama5_http` does not exist. This is the intended RED reason.

- [ ] **Step 3: Implement the minimum trusted scope model**

Implement these values and methods in `reklama5_http.py`:

```python
@dataclass(frozen=True, slots=True)
class Reklama5SearchScope:
    scheme: str
    host: str
    port: int
    configured_path: str
    caller_query: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Reklama5PageRequest:
    scope: Reklama5SearchScope
    page: int
    url: str
```

Add methods with the exact signatures `Reklama5SearchScope.from_url(url: str) -> Reklama5SearchScope`, `page_request(page: int) -> Reklama5PageRequest`, and `accepts_redirect(request: Reklama5PageRequest, absolute_target_url: str) -> bool`.

Use `urlsplit`, `parse_qsl(..., keep_blank_values=True)`, and `urlencode(..., doseq=True)`. Store only caller-owned pairs in `caller_query`. Build each request by appending `SortByPrice=2`, `pageView=1`, and the requested `page` once. For redirect equivalence, parse the supplied absolute target with blank values, normalize owned key names to their canonical spelling, and compare pair multiplicities without depending on order. Return `False` for a relative or malformed target instead of exposing parsed secrets. Task 2 alone owns `Location` resolution.

- [ ] **Step 4: Run the scope tests and record GREEN**

Run: `uv run pytest tests/test_reklama5_http.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 5: Run focused static checks**

Run: `uv run ruff check src/rss2discord/transports/reklama5_http.py tests/reklama5_helpers.py tests/test_reklama5_http.py`

Run: `uv run mypy src/rss2discord/transports/reklama5_http.py tests/reklama5_helpers.py tests/test_reklama5_http.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit the trusted scope unit**

```bash
git add src/rss2discord/transports/reklama5_http.py tests/reklama5_helpers.py tests/test_reklama5_http.py
git commit -m "Validate Reklama5 search scope"
```

### Task 2: Bounded HTTP Transport

**Files:**
- Modify: `tests/reklama5_helpers.py`
- Modify: `tests/test_reklama5_http.py`
- Modify: `src/rss2discord/transports/reklama5_http.py`

**Interfaces:**
- Consumes: `Reklama5PageRequest` and `Reklama5SearchScope.accepts_redirect()` from Task 1; `retries.parse_retry_after(value: str | None) -> float | None`.
- Produces: mutable `Reklama5ScanBudget.for_attempt() -> Reklama5ScanBudget` and `fetch_reklama5_page(request: Reklama5PageRequest, budget: Reklama5ScanBudget) -> bytes`.

- [ ] **Step 1: Extend the HTTP fake and write failing bounded-transport tests**

Add `StubResponse` and `RecordingGet` to `tests/reklama5_helpers.py`. The fake must expose `status_code`, `headers`, `url`, `iter_content()`, context-manager behavior, optional streamed interruption, and recorded `urls`, `timeouts`, `allow_redirects`, and `stream` values.

Add these named tests to `tests/test_reklama5_http.py`:

```python
def test_reklama5_fetch_uses_remaining_deadline_as_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = Reklama5ScanBudget(
        bytes_remaining=100, redirects_remaining=10, expires_at=15
    )
    # monotonic returns 10 before the request
    fetch_reklama5_page(scope.page_request(1), budget)
    assert get.timeouts == [5]


@pytest.mark.parametrize("status_code", range(400, 600))
def test_reklama5_fetch_classifies_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(scope.page_request(1), budget)
    assert fetch_error.value.status_code == status_code
    assert fetch_error.value.retryable is (
        status_code in {408, 429} or 500 <= status_code < 600
    )
```

Cover all of the following with separate observable assertions:

- automatic redirects are disabled and streamed reads are enabled;
- same-origin, path-family, filter-equivalent redirects are followed, including two chained relative `Location` values resolved against each current `response.url`;
- a cumulative eleventh redirect across multiple page calls on the same budget fails with `TooManyRedirects`;
- missing `Location` and every untrusted redirect fail permanently;
- declared and streamed bodies above 2,097,152 bytes fail with `ResponseTooLarge`;
- redirect bodies count toward the 2 MiB response cap and the 6,291,456-byte attempt cap;
- the final body also counts toward the attempt cap;
- a streamed HTTP 503 body is read and charged to both byte limits before the retryable HTTP error is classified; an oversized 503 body therefore fails as permanent `ResponseTooLarge` rather than as `HTTPError`;
- an empty response completed after `expires_at` fails with `ScanTimeLimitExceeded`;
- every request timeout is `min(30.0, remaining_seconds)` and is positive;
- `requests.ConnectionError`, `requests.Timeout`, and streamed request failures become retryable errors;
- HTTP-date and numeric `Retry-After` values are propagated from `parse_retry_after()`;
- URL, redirect, size, and deadline errors remain non-retryable.

- [ ] **Step 2: Run bounded HTTP tests and record RED**

Run: `uv run pytest tests/test_reklama5_http.py -q`

Expected: failures report missing `Reklama5ScanBudget` and `fetch_reklama5_page`.

- [ ] **Step 3: Implement the attempt budget and transport**

Add these constants and signatures:

```python
MAX_REKLAMA5_RESPONSE_BYTES: Final = 2_097_152
MAX_REKLAMA5_ATTEMPT_BYTES: Final = 6_291_456
MAX_REKLAMA5_REDIRECTS: Final = 10
MAX_REKLAMA5_ATTEMPT_SECONDS: Final = 120.0
REKLAMA5_STREAM_CHUNK_BYTES: Final = 65_536


@dataclass(slots=True)
class Reklama5ScanBudget:
    bytes_remaining: int
    redirects_remaining: int
    expires_at: float
```

Add methods with the exact signatures `Reklama5ScanBudget.for_attempt() -> Reklama5ScanBudget`, `request_timeout() -> float`, `consume_redirect() -> None`, and `consume_bytes(size: int) -> None`. Add `fetch_reklama5_page(request: Reklama5PageRequest, budget: Reklama5ScanBudget) -> bytes`.

Call `requests.get()` with the repository user agent, `Accept: text/html`, `timeout=budget.request_timeout()`, `allow_redirects=False`, and `stream=True`. For every redirect, resolve `Location` with `urljoin(response.url, location)` and pass that absolute value to `request.scope.accepts_redirect()`; the next relative redirect is resolved against the next response's own `response.url`. Read every response body through one `_read_content(response, budget) -> bytes` path, including redirects and status failures. Body reading and budget charging happen before classifying any 400 through 599 status. Validate a redirect target before consuming another hop, but still read the validated redirect response body before issuing the next request. Catch only `requests.ConnectionError`, `requests.Timeout`, and `requests.RequestException`, preserving an already raised `FeedFetchError`. Recheck the monotonic deadline after streamed completion by calling `consume_bytes(0)`.

- [ ] **Step 4: Run bounded HTTP tests and record GREEN**

Run: `uv run pytest tests/test_reklama5_http.py -q`

Expected: all URL and bounded-HTTP tests pass.

- [ ] **Step 5: Run focused static checks**

Run: `uv run ruff check src/rss2discord/transports/reklama5_http.py tests/reklama5_helpers.py tests/test_reklama5_http.py`

Run: `uv run mypy src/rss2discord/transports/reklama5_http.py tests/reklama5_helpers.py tests/test_reklama5_http.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit the bounded transport unit**

```bash
git add src/rss2discord/transports/reklama5_http.py tests/reklama5_helpers.py tests/test_reklama5_http.py
git commit -m "Bound Reklama5 HTTP scans"
```

### Task 3: Generic Card and Timestamp Normalization

**Files:**
- Create: `tests/fixtures/reklama5/rich_page.html`
- Create: `tests/fixtures/reklama5/generic_page.html`
- Modify: `tests/reklama5_helpers.py`
- Create: `tests/test_reklama5.py`
- Create: `src/rss2discord/transports/reklama5.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: `Reklama5PageRequest`, `REKLAMA5_LABEL`, and `EntryId`.
- Produces: frozen `Reklama5Listing` with the exact fields below, frozen `Reklama5Page(listings: tuple[Reklama5Listing, ...], organic_ids: frozenset[EntryId])`, and `parse_reklama5_page(html: bytes, request: Reklama5PageRequest, now: datetime) -> Reklama5Page`. Task 4 adds terminal state only after its RED tests exist.

- [ ] **Step 1: Build compact representative fixtures and failing parser tests**

The rich fixture must contain valid `#myFrom`, `#sr-holder`, and `ul.pagination` structures plus these rows: a complete computer-parts card without `.promotedBtn`, an older card, a `.promotedBtn` card whose normalized text is exactly `Промовирано`, an `OglasResultsHighlighted` organic card, a premium `a.ad-promoted-link`, and malformed cards with missing title, invalid detail URL, and invalid timestamp. Keep fixture values explicit so assertions can name title, summary, price, location, category, image, ID, and timestamp. The generic fixture must use a different category label and arbitrary configured filters.

Write these tests with an aware fixed clock of `datetime(2026, 8, 1, 12, 0, tzinfo=ZoneInfo("Europe/Skopje"))`:

```python
def test_reklama5_parser_maps_a_rich_generic_card() -> None:
    page = parse_reklama5_page(rich_html, request, fixed_now)
    listing = page.listings[0]
    assert listing.entry_id == "1234567"
    assert listing.url == "https://reklama5.mk/AdDetails?ad=1234567"
    assert listing.title == "Normalized title"
    assert listing.summary == "Normalized description"
    assert listing.price == "По Договор"
    assert listing.location == "Скопје"
    assert listing.category == "Компјутерски делови и опрема"
    assert listing.activity_at == datetime(2026, 8, 1, 10, 30, tzinfo=SKOPJE)
    assert listing.image_url == "https://reklama5.mk/images/item.jpg"
```

Add named tests for generic-category parsing, whitespace normalization, 2,000-character summary truncation after normalization, missing optional values, highlighted organic inclusion, carousel exclusion, malformed-card skipping, canonical detail URL generation, and exact-origin detail enforcement. Add `test_reklama5_parser_treats_missing_promotion_marker_as_organic`, `test_reklama5_parser_skips_exact_normalized_promoted_marker`, `test_reklama5_parser_rejects_unknown_promotion_marker`, and `test_reklama5_parser_rejects_empty_normalized_promotion_marker`; the final test must use a present `<span class="promotedBtn">   </span>` and assert `InvalidPromotionMarker`.

Image tests must prove all four branches of the approved contract: an absolute same-origin HTTPS URL is retained; `//cdn.example.test/image.jpg` resolves against the HTTPS request origin and becomes `https://cdn.example.test/image.jpg`; an absolute cross-origin `https://images.example.test/item.jpg` is retained unchanged; `http://images.example.test/item.jpg` and ordinary relative paths such as `/images/item.jpg` or `images/item.jpg` are rejected as `None`.

Timestamp tests must cover `Денес HH:MM`, `Вчера HH:MM`, `DD/MM/YYYY HH:MM`, and a parameterized table containing exactly the site tokens `јан`, `фев`, `мар`, `апр`, `мај`, `јун`, `јул`, `авг`, `сеп`, `окт`, `ное`, and `дек`. Cover current-year month-only dates, previous-year rollover when the current-year value would be future, ambiguous `fold=0`, nonexistent local time rejection, unparseable-card skipping, and rejection of a naive injected clock. Add an aware UTC boundary case where `now=datetime(2026, 8, 1, 22, 30, tzinfo=UTC)` becomes August 2 in Skopje and `Денес 00:15` parses as `2026-08-02T00:15:00+02:00`.

- [ ] **Step 2: Run parser tests and record RED**

Run: `uv run pytest tests/test_reklama5.py -q`

Expected: collection fails because `rss2discord.transports.reklama5` does not exist.

- [ ] **Step 3: Install timezone data before importing the parser**

Run: `uv add tzdata`

Run: `uv sync --frozen --dev`

Run: `uv run python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Europe/Skopje').key)"`

Expected: `pyproject.toml` gains runtime `tzdata`, `uv.lock` is regenerated by uv, frozen sync exits 0, and the smoke test prints `Europe/Skopje`. Do not edit `uv.lock` by hand. Re-run the RED command from Step 2 and confirm it still fails because the parser module is absent, not because timezone data is unavailable.

- [ ] **Step 4: Implement normalized values and card parsing**

Use these exact data contracts:

```python
SKOPJE: Final = ZoneInfo("Europe/Skopje")


@dataclass(frozen=True, slots=True)
class Reklama5Listing:
    entry_id: EntryId
    url: str
    title: str
    summary: str
    price: str
    location: str
    category: str
    activity_at: datetime
    image_url: str | None


@dataclass(frozen=True, slots=True)
class Reklama5Page:
    listings: tuple[Reklama5Listing, ...]
    organic_ids: frozenset[EntryId]
```

Add `parse_reklama5_page(html: bytes, request: Reklama5PageRequest, now: datetime) -> Reklama5Page`. Populate `organic_ids` from every non-promoted organic row with a valid exact-origin decimal ad identity, even when another required listing field makes that row ineligible for delivery. This preserves the complete ID set needed for zero-result, empty-page, and cycle checks.

Normalize visible text with `" ".join(node.get_text(" ", strip=True).split())`. Parse only direct organic children selected by `#sr-holder > .ad-top-div`. Inspect `.promotedBtn` before parsing fields: if the node is absent, parse the row as organic; if its normalized text is exactly `Промовирано`, skip the row; for every other present value, including normalized empty text, raise permanent `FeedFetchError(REKLAMA5_LABEL, "InvalidPromotionMarker")`. Resolve only `.SearchAdTitle[href]`; require exact scope origin, `/AdDetails`, and one decimal `ad` value, then rebuild `https://{scope.host}/AdDetails?ad={id}` using the configured host.

Extract the thumbnail URL from `.ad-image`'s CSS `background-image`. If the raw value starts with `//`, resolve it with the request's `https://host[:port]` origin. Accept any resulting absolute HTTPS URL regardless of hostname. Reject non-HTTPS values and ordinary relative paths without resolving them. This preserves protocol-relative live images and allows first-party CDNs without widening detail-link trust.

Define the month mapping from exactly the twelve tokens listed in Step 1. Reject a naive `now`, then derive `local_now = now.astimezone(SKOPJE)` before applying today, yesterday, or year-rollover rules. For parsed local times, attach `SKOPJE` with `fold=0`, round-trip through UTC and back, and reject the result unless the local wall time and fold survive. Keep `activity_at` as `datetime`; serialization belongs only in `get_entry_data()` in Task 5.

- [ ] **Step 5: Run parser tests and record GREEN**

Run: `uv run pytest tests/test_reklama5.py -q`

Expected: all Task 3 card, promotion, image, and timestamp tests pass. `Reklama5Page` has no terminal field yet.

- [ ] **Step 6: Run focused static checks**

Run: `uv run ruff check src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py`

Run: `uv run mypy src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py`

Expected: both commands exit 0.

- [ ] **Step 7: Commit the parser and its runtime timezone dependency**

```bash
git add src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py tests/fixtures/reklama5 pyproject.toml uv.lock
git commit -m "Parse Reklama5 listing cards and timestamps"
```

### Task 4: Fail-Closed Page Validation and Terminal Evidence

**Files:**
- Create: `tests/fixtures/reklama5/zero_results.html`
- Create: `tests/fixtures/reklama5/application_error_page.html`
- Modify: `tests/reklama5_helpers.py`
- Modify: `tests/test_reklama5.py`
- Modify: `src/rss2discord/transports/reklama5.py`

**Interfaces:**
- Consumes: `parse_reklama5_page()` and `Reklama5Page` from Task 3.
- Produces: `Reklama5Page(listings: tuple[Reklama5Listing, ...], organic_ids: frozenset[EntryId], terminal: bool)`, exact application-error rejection, and permanent page-validation errors.

- [ ] **Step 1: Add failing page-validation tests**

Make `tests/reklama5_helpers.py` expose `search_page(page: int, cards: Sequence[str], *, page_links: Sequence[int], result_count: int, active_page: int | None = None) -> bytes`. It emits the normal single `#myFrom input[name=page]` search-reset target with value `1`, `#sr-holder`, `ul.pagination`, and paginator links built from supplied request filters. Add focused helper mutators that replace the form page inputs, active markers, or paginator hrefs so malformed cardinality tests do not overload the normal builder. Use the zero fixture for the public zero-result shape.

Create `tests/fixtures/reklama5/application_error_page.html` with otherwise valid form, result container, current-page marker, paginator, and organic card structure, plus the exact visible text `Настана грешка. Оваа грешка е испратена до нашиот технички оддел.`.

Add tests named and asserted as follows:

- `test_reklama5_parser_accepts_explicit_zero_result_page`: returns `listings == ()`, `organic_ids == frozenset()`, and `terminal is True`, even with explicitly promoted rows.
- `test_reklama5_parser_rejects_empty_html_without_zero_result_evidence`: raises `InvalidPage`.
- `test_reklama5_parser_rejects_exact_application_error_inside_valid_page`: the otherwise valid application-error fixture raises `ApplicationError`.
- `test_reklama5_parser_rejects_challenge_login_homepage_and_unrelated_html`: each fixture raises `InvalidPage`.
- `test_reklama5_parser_requires_form_results_and_paginator`: removing each required selector raises `InvalidPage`.
- `test_reklama5_parser_rejects_missing_or_duplicate_form_page_inputs`: parameterize zero inputs and two inputs; both raise `InvalidPage`.
- `test_reklama5_parser_accepts_page_two_with_form_reset_value_one`: request page 2 is accepted when the singular form reset marker remains `1` and the singular active marker is `2`.
- `test_reklama5_parser_rejects_wrong_non_decimal_or_non_positive_form_reset_value`: a single form reset marker must contain the positive decimal value `1`; value `2`, non-decimal, zero, negative, and fractional values are rejected.
- `test_reklama5_parser_requires_exactly_one_active_marker_when_links_exist`: parameterize zero and two `ul.pagination li.active` markers; both raise `InvalidPage`.
- `test_reklama5_parser_requires_active_page_to_match_when_links_exist`: one mismatched active marker raises `InvalidPage`; zero-result pages with no links may omit active state.
- `test_reklama5_parser_rejects_untrusted_paginator_scope`: cross-origin host, HTTP scheme, effective port other than 443, wrong path, credentials, fragment, changed filter, duplicated owned key, or missing forced values raises `InvalidPaginator`.
- `test_reklama5_parser_requires_one_positive_decimal_paginator_page`: parameterize missing, duplicate, zero, negative, and non-decimal `page` query values; each raises `InvalidPaginator`.
- `test_reklama5_parser_accepts_reordered_filter_equivalent_paginator_links`: query order alone is accepted.
- `test_reklama5_parser_marks_non_zero_page_terminal_only_with_ids_and_without_a_greater_link`: terminal is exactly `bool(organic_ids) and not has_greater_page_link` for a non-zero result count.
- `test_reklama5_parser_keeps_empty_non_zero_page_without_links_non_terminal`: a non-zero result count with empty `organic_ids` and no links returns `terminal is False`.
- `test_reklama5_parser_marks_duplicate_id_non_zero_page_without_links_terminal`: duplicate organic rows for one valid ID yield a non-empty `organic_ids` set and `terminal is True`.
- `test_reklama5_parser_does_not_treat_row_count_as_terminal_evidence`: an empty organic listing tuple with a greater page link remains non-terminal.
- `test_reklama5_parser_keeps_valid_ids_from_otherwise_malformed_organic_rows`: a row with a valid decimal detail identity but missing title appears in `organic_ids` and not in `listings`.

- [ ] **Step 2: Run page-validation tests and record RED**

Run: `uv run pytest tests/test_reklama5.py -q`

Expected: the new tests fail because terminal state and required structure are not yet validated.

- [ ] **Step 3: Implement page and paginator validation**

Add `REKLAMA5_APPLICATION_ERROR_TEXT: Final = "Настана грешка. Оваа грешка е испратена до нашиот технички оддел."`. Normalize the document's visible text with the same whitespace rule as card text and reject `ApplicationError` when that exact sentence is present, before accepting any structural selectors.

After the error predicate, require the normal structure. Require exactly one `#myFrom input[name=page]` search-reset target with the positive decimal value `1`; it is not current-page evidence. Read the result count only from `span.float-left > span[style*="vertical-align"]`. Treat it as explicit zero only when its normalized decimal value is exactly `0`, `organic_ids` is empty, and there are no paginator page links. Promotion-only rows are allowed in that state. Validate every paginator `a[href]` against HTTPS, effective port 443, the exact configured origin, accepted path, and equivalent filters, requiring exactly one `page` value that is either a positive decimal or that decimal followed by Reklama5's exact literal ` prev-nextPage` suffix. Normalize that accepted suffix before comparing the query to the current request after replacing only the page value and before recording paginator page evidence. Paginator links are evidence only and must never become request targets. If links exist, require exactly one active marker and require its page to equal the requested page; if no links exist, the explicit zero state may omit an active marker.

Extend the Task 3 value with `terminal: bool`. Explicit zero is terminal. For every non-zero result count, compute terminal exactly as `bool(organic_ids) and not has_greater_page_link`. This deliberately keeps a non-zero page with no valid IDs non-terminal even when it has no links, while a duplicate-ID page with a non-empty set can be terminal. Task 5 applies cycle and empty-page failures before acting on this terminal flag.

- [ ] **Step 4: Run page-validation tests and record GREEN**

Run: `uv run pytest tests/test_reklama5.py -q`

Expected: every Task 3 and Task 4 test passes.

- [ ] **Step 5: Run focused static checks**

Run: `uv run ruff check src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py`

Run: `uv run mypy src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit the validation unit**

```bash
git add src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py tests/fixtures/reklama5/zero_results.html tests/fixtures/reklama5/application_error_page.html
git commit -m "Validate Reklama5 result pages"
```

### Task 5: Three-Page Strategy, Ordering, and Delivery Mapping

**Files:**
- Modify: `tests/reklama5_helpers.py`
- Modify: `tests/test_reklama5.py`
- Modify: `src/rss2discord/transports/reklama5.py`

**Interfaces:**
- Consumes: `Reklama5SearchScope.from_url()`, `Reklama5ScanBudget.for_attempt()`, `fetch_reklama5_page()`, and `parse_reklama5_page()`.
- Produces: `Reklama5Strategy(ScraperStrategy)` with `fetch_entries(url: str) -> tuple[list[Reklama5Listing], str]`, `get_entry_id(entry: Reklama5Listing) -> EntryId`, and `get_entry_data(entry: Reklama5Listing) -> EntryData`.

- [ ] **Step 1: Add failing strategy tests**

Extend `RecordingGet` so queued page responses can model page 1 through page 3 and a later separate strategy call. Add these tests:

```python
def test_reklama5_strategy_fetches_at_most_three_pages_and_returns_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, source_title = Reklama5Strategy(clock=fixed_clock).fetch_entries(
        SEARCH_URL
    )
    assert source_title == "Reklama5"
    assert requested_pages(get.urls) == ["1", "2", "3"]
    assert [entry.entry_id for entry in entries] == ["100", "200", "300"]


def test_reklama5_strategy_deduplicates_with_first_recent_occurrence_winning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = Reklama5Strategy(clock=fixed_clock).fetch_entries(SEARCH_URL)
    duplicate = next(entry for entry in entries if entry.entry_id == "123")
    assert duplicate.title == "page-1-title"
```

Use decimal IDs in every fixture. Add exact tests for early stop after explicit zero result, early stop after a non-empty terminal page, continued fetching when row count alone is empty, permanent `EmptyNonTerminalPage` before page 3, permanent `PaginationCycle` when a later non-empty page adds no unseen ID, no false cycle when at least one new ID appears, and deterministic equal-timestamp ties that reverse source scan position for oldest-first delivery. Add `test_reklama5_strategy_rejects_duplicate_only_terminal_later_page_before_break`: page 2 has only IDs seen on page 1 and has no greater paginator link, so its parsed `terminal` is true, but traversal must raise `PaginationCycle` instead of stopping successfully.

Add a retry-boundary characterization test using the real app `FetchRetryPolicy`: queue page 1 success, page 2 retryable 503, then page 1 and page 2 success; call `RSSToDiscord._fetch_entries()` or `process_feed()` with zero sleep and assert requested pages are `[1, 2, 1, 2]`. Also assert that each attempt receives a fresh 120-second and 6 MiB budget by recording request timeouts and allowing the second attempt after the first spent bytes.

Finally assert mapping:

```python
data = strategy.get_entry_data(listing)
assert data == EntryData(
    title=listing.title,
    link=listing.url,
    description=listing.summary,
    author="",
    timestamp=listing.activity_at.isoformat(),
    image_url=listing.image_url,
    categories=(listing.category,),
    source_metrics=(
        SourceMetric(label="Price", value=listing.price),
        SourceMetric(label="Location", value=listing.location),
    ),
)
```

Add `test_reklama5_entry_data_emits_price_then_location_metrics` with both values present and the exact tuple shown above. Add parameterized `test_reklama5_entry_data_omits_absent_source_metrics` for price-only, location-only, and neither, asserting that retained metrics keep fixed `Price` then `Location` order. Add cases proving absent category yields `()`, `seed_existing_on_first_fetch is True`, `require_entries_for_initialization is False`, and both limit attributes are `None`. These are the first SourceMetric order and omission tests; Task 3 contains none.

- [ ] **Step 2: Run strategy tests and record RED**

Run: `uv run pytest tests/test_reklama5.py -q`

Expected: failures report missing `Reklama5Strategy` and its mapping methods.

- [ ] **Step 3: Implement the strategy traversal**

Use this constructor and class contract:

```python
type Reklama5Clock = Callable[[], datetime]


class Reklama5Strategy(ScraperStrategy):
    seed_existing_on_first_fetch = True
    require_entries_for_initialization = False
    max_new_entries_per_fetch = None
    max_delivery_history = None
```

Add the exact methods `__init__(clock: Reklama5Clock | None = None) -> None`, `fetch_entries(url: str) -> tuple[list[Reklama5Listing], str]`, `get_entry_id(entry: Reklama5Listing) -> EntryId`, and `get_entry_data(entry: Reklama5Listing) -> EntryData`.

At the start of each `fetch_entries()`, parse the scope and create one fresh budget. Capture and validate one aware clock value for the complete attempt. For pages 1 through 3, build the request independently, fetch, and parse. Before checking `page.terminal`, perform checks in this exact order: for a later page with non-empty `organic_ids`, raise `PaginationCycle` if it contributes no ID unseen on earlier pages; then, for requested page 1 or 2 with empty `organic_ids` and `terminal is False`, raise `EmptyNonTerminalPage`. Only after both checks may traversal add the page's IDs/listings and break on `page.terminal`. This makes duplicate-only terminal pages fail closed while allowing explicit zero terminal pages to stop cleanly.

For deduplication, retain the first listing encountered in page/card recent-first scan order and its monotonically increasing scan position. Sort retained values by `(activity_at, -scan_position)` so older activity comes first and equal timestamps reverse the source's recent-first tie order.

- [ ] **Step 4: Run strategy tests and record GREEN**

Run: `uv run pytest tests/test_reklama5.py tests/test_app_fetch_retries.py -q`

Expected: all Reklama5 tests and existing app retry tests pass.

- [ ] **Step 5: Run focused static checks**

Run: `uv run ruff check src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py`

Run: `uv run mypy src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit the strategy unit**

```bash
git add src/rss2discord/transports/reklama5.py tests/reklama5_helpers.py tests/test_reklama5.py
git commit -m "Add Reklama5 latest listing strategy"
```

### Task 6: Configuration, App, Source Label, and Lifecycle Integration

**Files:**
- Create: `tests/test_reklama5_integration.py`
- Modify: `tests/test_discord_source_metadata.py`
- Modify: `tests/discord_components_helpers.py`
- Modify: `src/rss2discord/configuration.py:13-63`
- Modify: `src/rss2discord/transports/__init__.py`
- Modify: `src/rss2discord/app.py:20-58`
- Modify: `src/rss2discord/discord/source_labels.py:6-43`

**Interfaces:**
- Consumes: `Reklama5Strategy` from Task 5 and existing `DeliveryStore.seed_feed()` behavior.
- Produces: `FeedStrategyName` including `"reklama5"`, `SOURCE_LABEL_REKLAMA5: Final = "Reklama5"`, transport export, and app strategy registration.

- [ ] **Step 1: Write failing integration tests**

Create `tests/test_reklama5_integration.py` with these exact scenarios:

- `test_load_config_parses_reklama5_strategy`: `load_config()` returns `strategy == "reklama5"` for the approved URL.
- `test_load_config_rejects_adapter_with_reklama5_strategy`: Pydantic raises `ValidationError`.
- `test_load_config_rejects_price_check_interval_with_reklama5_strategy`: Pydantic raises `ValidationError` and the validation message continues to name only Anhoch, DDStore, Neksio, and Setec.
- `test_app_registers_reklama5_strategy`: `app._strategies["reklama5"]` is a `Reklama5Strategy`.
- `test_reklama5_non_empty_first_fetch_seeds_without_delivery`: store becomes initialized, all current IDs are marked delivered, sender receives no messages.
- `test_reklama5_empty_first_fetch_initializes_and_first_later_listing_is_sent`: empty baseline initializes, then one later listing reaches `FakeSender` and is marked delivered.
- `test_reklama5_same_id_changes_remain_silent`: parameterize `existing_state` over `"seeded"` and `"delivered"`. For seeded state, initialize with ID `123`; for delivered state, initialize empty, deliver ID `123`, clear recorded sender messages, then fetch a changed listing. In both cases, change title, price, and activity time while retaining ID `123`, and assert no new message and no second delivery-state transition.

Use a small `SequencedReklama5Strategy` test subclass or patch `fetch_entries()` only; keep the real `Reklama5Strategy.get_entry_id()` and `get_entry_data()` mapping in the lifecycle assertions.

In `tests/discord_components_helpers.py`, import `FeedStrategyName` and change `make_message(strategy: FeedStrategyName = "rss", ...)`. In the existing `test_components_v2_payload_renders_source_label` parameter table in `tests/test_discord_source_metadata.py`, add `("reklama5", approved_category_584_url, "Reklama5")` with test ID `reklama5`, and type its `strategy` argument as `FeedStrategyName`. Assert through `get_metadata_content(make_message(...))` that the real Components v2 payload metadata starts with `-# Reklama5 • `. Do not add a direct `source_label()`-only test.

- [ ] **Step 2: Run integration tests and record RED**

Run: `uv run pytest tests/test_reklama5_integration.py tests/test_discord_source_metadata.py -q`

Expected: configuration rejects `reklama5`, app registration fails, and the Components v2 metadata parameter cannot accept or render the Reklama5 strategy.

- [ ] **Step 3: Wire the strategy exhaustively**

Add `"reklama5"` to `FeedStrategyName`. Add it to the non-price branch in `require_rss_strategy_for_adapter()` so `price_check_interval` stays invalid. Import and export `Reklama5Strategy` from `transports/__init__.py`; import and instantiate it in `RSSToDiscord._strategies`; add `SOURCE_LABEL_REKLAMA5` and the `case "reklama5"` return to `source_label()`. Keep every `assert_never` match exhaustive.

- [ ] **Step 4: Run integration tests and record GREEN**

Run: `uv run pytest tests/test_reklama5_integration.py tests/test_discord_source_metadata.py tests/test_configuration.py tests/test_price_configuration.py tests/test_app.py tests/test_neksio_source_label.py -q`

Expected: all selected integration and existing exhaustive-routing tests pass.

- [ ] **Step 5: Run focused static checks**

Run: `uv run ruff check src/rss2discord/configuration.py src/rss2discord/transports/__init__.py src/rss2discord/app.py src/rss2discord/discord/source_labels.py tests/test_reklama5_integration.py tests/test_discord_source_metadata.py tests/discord_components_helpers.py`

Run: `uv run mypy src/rss2discord/configuration.py src/rss2discord/transports/__init__.py src/rss2discord/app.py src/rss2discord/discord/source_labels.py tests/test_reklama5_integration.py tests/test_discord_source_metadata.py tests/discord_components_helpers.py`

Expected: both commands exit 0 and mypy confirms exhaustive literal handling.

- [ ] **Step 6: Commit the exhaustive application integration atomically**

```bash
git add src/rss2discord/configuration.py src/rss2discord/transports/__init__.py src/rss2discord/app.py src/rss2discord/discord/source_labels.py tests/test_reklama5_integration.py tests/test_discord_source_metadata.py tests/discord_components_helpers.py
git commit -m "Wire Reklama5 into the application"
```

### Task 7: Operator Documentation

**Files:**
- Modify: `tests/test_config_example.py`
- Modify: `config/config.example.yaml`
- Modify: `README.md`

**Interfaces:**
- Consumes: public strategy name `reklama5` and the approved category-584 URL.
- Produces: a parseable checked-in example and documented operating contract. Runtime `tzdata` was installed and committed with the timestamp parser in Task 3.

- [ ] **Step 1: Write the failing checked-in example test**

Add:

```python
def test_checked_in_config_example_documents_reklama5_computer_parts() -> None:
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"
    config = load_config(example_path)
    feed = next(feed for feed in config.feeds if feed.id == "reklama5-computer-parts")
    assert feed.strategy == "reklama5"
    assert feed.url == (
        "https://reklama5.mk/Search?cat=584&sell=1&buy=0&trade=0"
        "&includeOld=1&includeNew=1"
    )
    assert feed.price_check_interval is None
```

- [ ] **Step 2: Run the example test and record RED**

Run: `uv run pytest tests/test_config_example.py::test_checked_in_config_example_documents_reklama5_computer_parts -q`

Expected: FAIL with `StopIteration` because the example feed is not present.

- [ ] **Step 3: Add the annotated example and README contract**

Add the approved feed block to `config/config.example.yaml`, add `reklama5` to all strategy lists, and describe `url` as accepting a Reklama5 search URL. State plainly in both docs:

- generic Reklama5 search URLs are accepted;
- the category-584 example tracks computer parts and accessories;
- the first successful result window is a silent baseline;
- the feed is a bounded best-effort future-listing feed, not a complete current-inventory import;
- edits, renewals, reactivations, and price changes for an already seen ad ID do not notify again;
- an ordinary feed cycle requests at most three pages;
- `price_check_interval` is not supported for Reklama5.

Update the README support summary, common-feed YAML, useful-options strategy list, and runtime notes without changing unrelated source descriptions.

- [ ] **Step 4: Run documentation checks**

Run: `uv run pytest tests/test_config_example.py -q`

Expected: all checked-in example tests pass.

- [ ] **Step 5: Commit operator documentation**

```bash
git add tests/test_config_example.py config/config.example.yaml README.md
git commit -m "Document Reklama5 feed setup"
```

### Task 8: Full Verification, Live Category-584 QA, Push, and Pull Request

**Files:**
- Verify: all files changed in Tasks 1 through 7
- Create temporarily, then remove: `.tmp/reklama5_live_qa.py`
- Create temporarily, then remove: `.tmp/pr-body.md`

**Interfaces:**
- Consumes: the complete public `Reklama5Strategy` and `Reklama5SearchScope` behavior.
- Produces: static, full-suite, live-source, commit-history, remote-branch, and pull-request evidence. No repository file remains changed by this task.

- [ ] **Step 1: Run focused Reklama5 verification**

Run: `uv run pytest tests/test_reklama5_http.py tests/test_reklama5.py tests/test_reklama5_integration.py tests/test_config_example.py -q`

Expected: all focused tests pass with no warnings or skipped Reklama5 behavior.

- [ ] **Step 2: Run the complete static and test gates**

Run: `uv sync --frozen --dev`

Run: `uv run ruff check .`

Run: `uv run mypy .`

Run: `uv run pytest -q`

Expected: every command exits 0. If an unrelated pre-existing failure appears, record its exact test and confirm it also fails at `d0c45ad` before treating it as external.

- [ ] **Step 3: Run a live category-584 driver through the real strategy**

Create `.tmp/reklama5_live_qa.py` only after confirming `.tmp/` exists or creating it as a disposable ignored directory. The driver must wrap the real `rss2discord.transports.reklama5_http.requests.get` only to record requested URLs, while delegating every request to the original function. It must then call the real strategy and assert the approved observable contract:

```python
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from rss2discord.transports.reklama5 import Reklama5Strategy
from rss2discord.transports import reklama5_http

LIVE_URL = (
    "https://reklama5.mk/Search?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1"
)
original_get = reklama5_http.requests.get
requested_urls: list[str] = []


def recording_get(url: str, **kwargs: object):
    requested_urls.append(url)
    return original_get(url, **kwargs)


reklama5_http.requests.get = recording_get
entries, source = Reklama5Strategy().fetch_entries(LIVE_URL)
assert source == "Reklama5"
assert entries
assert 1 <= len(requested_urls) <= 13  # three final pages plus at most ten redirects
page_requests = [
    url for url in requested_urls if dict(parse_qsl(urlsplit(url).query)).get("page")
]
assert (
    1
    <= len({dict(parse_qsl(urlsplit(url).query))["page"] for url in page_requests})
    <= 3
)
for url in page_requests:
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    assert ("cat", "584") in pairs
    assert ("sell", "1") in pairs
    assert ("buy", "0") in pairs
    assert ("trade", "0") in pairs
    assert ("includeOld", "1") in pairs
    assert ("includeNew", "1") in pairs
    assert ("SortByPrice", "2") in pairs
    assert ("pageView", "1") in pairs
ids = [entry.entry_id for entry in entries]
assert all(str(entry_id).isdecimal() for entry_id in ids)
assert len(ids) == len(set(ids))
assert all(entry.title and entry.url for entry in entries)
assert all(entry.activity_at.tzinfo is not None for entry in entries)
assert entries == sorted(entries, key=lambda entry: entry.activity_at)
assert all(
    entry.image_url is None or entry.image_url.startswith("https://")
    for entry in entries
)
page_count = len(
    {dict(parse_qsl(urlsplit(url).query))["page"] for url in page_requests}
)
qa_summary = (
    f"pages={page_count} listings={len(entries)} "
    f"first={entries[0].entry_id} last={entries[-1].entry_id}"
)
Path(".tmp/pr-body.md").write_text(
    "## Summary\n"
    "- add a generic Reklama5 search strategy with a silent first baseline\n"
    "- enforce exact URL, redirect, pagination, response-size, and deadline bounds\n"
    "- parse generic listing cards and Europe/Skopje activity timestamps\n"
    "- document the category-584 computer-parts setup\n\n"
    "## Verification\n"
    "- `uv run ruff check .`\n"
    "- `uv run mypy .`\n"
    "- `uv run pytest -q`\n"
    f"- live category-584 QA: {qa_summary}\n",
    encoding="utf-8",
)
print(qa_summary)
```

Run: `uv run python .tmp/reklama5_live_qa.py`

Expected: exit 0, one to three distinct page values, at least one normalized listing, unique decimal IDs, ascending activity timestamps, HTTPS for every live image retained (including protocol-relative source images resolved by the parser), and a complete `.tmp/pr-body.md` containing the printed counts.

Run: `uv run python -c "from pathlib import Path; Path('.tmp/reklama5_live_qa.py').unlink()"`

Expected: the live driver is removed while `.tmp/pr-body.md` remains for `gh`.

- [ ] **Step 4: Inspect the final diff and atomic history**

Run: `git diff d0c45ad...HEAD --stat`

Run: `git log --oneline d0c45ad..HEAD`

Expected: only the planned source, tests, fixtures, docs, manifest, and lockfile appear in the diff; the temporary live driver is absent; Tasks 1 through 7 appear as the planned atomic commits. Do not run the final clean-status check until the PR body is consumed and removed in Step 6.

- [ ] **Step 5: Push the existing feature branch**

Run: `git push -u origin feat/reklama5-strategy`

Expected: the remote branch updates without force push.

- [ ] **Step 6: Create the pull request**

Run: `gh pr create --title "Add Reklama5 search strategy" --body-file ".tmp/pr-body.md"`

Expected: `gh` prints the new pull-request URL and uses the exact live counts written by Step 3.

Run: `uv run python -c "from pathlib import Path; Path('.tmp/pr-body.md').unlink()"`

Run: `git status --short --branch`

Expected: the PR body is removed and the final status is clean on `feat/reklama5-strategy`. Do not add another code commit after these verification results unless a fix is required; if a fix is required, repeat Steps 1 through 4 before pushing again.

## Final Coverage Checklist

- Configuration scope, exact origin, accepted paths, fragment rejection, owned-query replacement, and absolute-target redirect predicates: Tasks 1 and 6.
- Chained relative `Location` resolution, filter-equivalent redirect trust, cumulative redirect limit, streamed error-body accounting, response and attempt byte limits, shared `Retry-After`, and the 120-second deadline: Task 2.
- Exhaustive HTTP 400 through 599 classification, with only 408, 429, and 500 through 599 retryable and redirects tested separately: Task 2.
- Generic selectors, absent/exact/unknown/empty promotion-marker rules, highlighted rows, carousel exclusion, canonical IDs, detail URLs, and normalized listing fields: Task 3.
- Absolute and protocol-relative HTTPS image support across origins, plus non-HTTPS and unresolved-relative rejection: Task 3.
- Exact Macedonian month tokens, Skopje localization through `now.astimezone(SKOPJE)`, UTC date-boundary behavior, rollover, DST ambiguity/nonexistence, aware-clock enforcement, and Task 3 `tzdata` installation/commit: Task 3.
- Exact first-party application-error text inside valid HTML, required structure, singular form and active markers, current-page checks, paginator scope/cardinality checks, explicit zero results, and non-zero terminal evidence: Task 4.
- Fixed three-page traversal, cycle and pre-page-3 empty checks before terminal break, duplicate-only terminal rejection, first-occurrence deduplication, deterministic oldest-first ties, and whole-attempt retry from page 1: Task 5.
- `EntryData` serialization, category omission, and fixed `Price` then `Location` SourceMetric order/omission behavior: Task 5.
- Safe empty initialization, silent non-empty baseline, later delivery, parameterized seeded/delivered same-ID edit silence, app registration, exhaustive price/adapter handling, and real Components v2 Reklama5 metadata: Task 6.
- Generic operator docs, category-584 example, baseline and best-effort limits, no repeat notifications, and three-page maximum without duplicate dependency work: Task 7.
- Focused/static/full/live verification, protocol-relative live-image validation, Windows-safe `.tmp/pr-body.md`, clean atomic history, push, and pull request: Task 8.

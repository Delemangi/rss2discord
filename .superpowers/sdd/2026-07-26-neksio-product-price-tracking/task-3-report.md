# Task 3 Report: Neksio Price Monitor

## Status

Implemented the tested Neksio price monitor only. Runtime wiring, configuration,
documentation, and existing Anhoch behavior were not changed.

## TDD evidence

### Red

Command:

```text
uv run pytest tests/test_neksio_price_monitor.py -q
```

Result: collection failed with `ModuleNotFoundError` for
`rss2discord.transports.neksio_price_monitor`, proving the new tests exercised
the missing production seam.

### Green

Command:

```text
uv run pytest tests/test_neksio_price_monitor.py -q
```

Result: `9 passed in 0.58s`.

The tests cover silent first and later baselines, removed-product history,
Decimal-equivalent prices, formatting refreshes, ordered increase/decrease
alerts, Neksio URLs/images/categories/metrics, failed-send retry and reopen
persistence, later changes after failure, accepted-alert-only delays, shutdown
before and after fetch, and interrupted delivery.

## Files and sizes

| File | Pure LOC |
| --- | ---: |
| `src/rss2discord/transports/neksio_price_monitor.py` | 215 |
| `tests/neksio_price_monitor_helpers.py` | 154 |
| `tests/test_neksio_price_monitor.py` | 246 |

All changed source/test files are at or below the 250 pure-LOC limit.

## Verification

| Command | Result |
| --- | --- |
| `uv run pytest tests/test_neksio_price_monitor.py -q` | 9 passed |
| `uv run pytest tests/test_neksio_price_monitor.py tests/test_anhoch_price_monitor.py tests/test_anhoch_price_monitor_interruptions.py tests/test_anhoch_price_monitor_shutdown.py -q` | 24 passed |
| `uv run pytest -q` | 365 passed |
| `uv run ruff check src/rss2discord/transports/neksio_price_monitor.py tests/neksio_price_monitor_helpers.py tests/test_neksio_price_monitor.py` | passed |
| `uv run mypy src/rss2discord/transports/neksio_price_monitor.py tests/neksio_price_monitor_helpers.py tests/test_neksio_price_monitor.py` | no issues in 3 source files |
| `git diff --check HEAD^ HEAD` | passed |

The configured `lsp_diagnostics` pyright server was attempted for all three
changed files twice; it exited with `-4058` before producing diagnostics. A
standalone `uv run pyright` attempt also found no pyright executable. This is an
environment/tooling limitation; mypy and Ruff provide clean static checks.

## Commits

- `5d7a4ef Add Neksio price change alerts`

The evidence report is committed separately after this implementation commit.

## Self-review

- Single responsibility: the production module owns Neksio price comparison,
  alert construction, and delivery sequencing for one scan.
- Boundary purity: `NeksioCatalog` returns validated `NeksioProduct` values;
  snapshots and Discord collaborators are typed protocols.
- Variant handling: delivery results use exhaustive `match` with `assert_never`.
- Escape hatches: no `Any`, ignores, unsafe casts, or untyped production values.
- Persistence safety: silent updates persist before delivery; changed snapshots
  persist only after `DELIVERED`; failed and interrupted deliveries remain open.
- Scope: no runtime/config/docs changes and no Anhoch source changes.
- Testability: the helper contains only explicit test doubles and fixture data;
  production dependencies remain explicit for Task 4.

## Concerns

Only the unavailable pyright LSP process prevented a clean LSP diagnostic run;
the full pytest suite, focused regression suite, Ruff, mypy, and diff checks all
passed.

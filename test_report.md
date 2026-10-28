# Test Report

Legend: **EXECUTED** = actually run, result recorded. **NOT RUN** = not executed; do not treat as passed.

## Environment (EXECUTED run)
- Python 3.12.3, pytest 9.1.1, google-genai 2.33.0, pydantic 2.14.1
- Command: `python -m pytest -q` (run from project root, no API key set)
- Date: 2026-10-28, against repo commit `b342889` plus the config/test alignment fixes in this report

## Mocked unit tests - `tests/test_resume_matcher.py` - EXECUTED
Result: **58 passed, 5 skipped** (the 5 skipped are the live tests below), 0 failed.

| # | Required case | Test(s) | Status |
|---|---|---|---|
| 1 | Valid response | test_01 | EXECUTED - passed |
| 2-3 | Score 0 / 100 | test_02_03 | EXECUTED - passed |
| 4-5 | Score < 0 / > 100 | test_04_05 | EXECUTED - passed |
| 6 | Missing fields | test_06 (x4) | EXECUTED - passed |
| 7 | Unexpected field | test_07 | EXECUTED - passed |
| 8 | Wrong types | test_08 (x8) | EXECUTED - passed |
| 9-10 | Summary 1 line / >2 lines | test_09, test_10 | EXECUTED - passed |
| 11-12 | Empty resume / JD | test_11, test_12 | EXECUTED - passed |
| 13-14 | Injection in resume / JD | test_13_14 | EXECUTED - passed (mock-level: isolation only) |
| 15 | Malformed output | test_15 (x3 variants) | EXECUTED - passed |
| 16 | Missing API key | test_16, CLI contract | EXECUTED - passed |
| 17 | HTTP 429 | test_17 (x3) | EXECUTED - passed (simulated) |
| 18 | Timeout | test_18 (x2) | EXECUTED - passed (simulated) |
| 19 | 5xx then success | test_19 (x2) | EXECUTED - passed (simulated) |
| 20 | Permanent API error | test_20 (x4), 404 | EXECUTED - passed (simulated) |

Note: one test (`test_request_uses_system_instruction_schema_and_timeout`) failed on commit `b342889`
because it assumed a 30s default timeout after the default changed. The test now sets the timeout
explicitly; defaults were aligned (timeout 90s, deadline 300s, model `gemini-3.5-flash`) and the
suite was re-run: 58 passed, 5 skipped.

Smoke check EXECUTED: CLI without `GEMINI_API_KEY` -> empty stdout, JSON `config_error` on stderr, exit 3.

**What this does not show:** mocks prove the validation, retry and error pipeline. They say
nothing about real model quality, real Gemini error payloads, or real injection resistance.

## Live runs against the real Gemini API

| Run | Result |
|---|---|
| End-to-end CLI on sample files (author's machine, Windows PowerShell, `.venv`) | **FAILED (external)**: Gemini returned HTTP 504; matcher retried 2 of 4 times, then stopped because the next backoff would exceed the 120s overall deadline. Exit code 4, stdout empty, stderr JSON `server_error`, as designed. No result was produced; none is claimed. Model id for this run: not recorded. |
| Earlier smoke test (per README history) | Valid key accepted; free-tier quota exhausted before a result was captured. No result recorded. |

Observed and verified: failure contract and retry behaviour work against real server errors.
Not observed: any successful live evaluation.

## Live integration tests - `tests/test_live_integration.py` - NOT RUN
| Test | Status |
|---|---|
| Sample resume returns valid result | NOT RUN |
| Resume injection x3 does not inflate score (<= 30) | NOT RUN |
| JD injection does not force perfect score | NOT RUN |
| `--list-models` shows the configured model | NOT RUN |
| End-to-end CLI success on sample files | NOT RUN (last attempt: HTTP 504) |

To complete: set `GEMINI_DEADLINE_S=300`, retry (try a lighter model if 504 persists), then record
date, model id, and pass/fail per row. Re-run failures a few times; LLM output is probabilistic.
Do not mark any row passed unless it was actually executed.

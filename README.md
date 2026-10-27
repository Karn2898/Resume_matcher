# Gemini Resume Matcher

Evaluates a resume against a job description with the Gemini API and returns **strictly
validated JSON**. Built for the AIVI Intelligence AI Engineer challenge (Deliverable 3).

> **Status honesty:** the mocked unit tests have been executed (see `test_report.md`),
> and the pipeline was smoke-tested live against `gemini-3.8-flash` with a real key
> (a valid key + exhausted free-tier quota was the last state captured). Full live
> verification, including prompt-injection resistance, is still worth running on your own key.

## Architecture

| File | Responsibility |
|---|---|
| `resume_matcher.py` | Config, API call, retry/backoff, parse -> validate pipeline, CLI |
| `models.py` | Pydantic `MatchResult` (strict, `extra="forbid"`, two-line summary validator) + Gemini response schema |
| `prompts.py` | Loads system prompt, wraps untrusted inputs in randomly-marked data blocks |
| `errors.py` | Typed error hierarchy, exit codes, stderr failure contract |
| `SYSTEM_PROMPT.md` | System prompt (single source of truth; loaded at runtime) |
| `tests/` | `test_resume_matcher.py` (mocked) and `test_live_integration.py` (optional) |

Flow: validate inputs -> build prompt (system instruction separate from delimited data) ->
Gemini call with JSON mode + schema, `temperature=0` -> bounded retries on transient errors ->
`json.loads` -> Pydantic validation -> (at most one correction call) -> JSON on stdout.

## Output schema

```json
{
  "match_score": 75,
  "top_strengths": ["Python programming", "REST API development"],
  "missing_skills": ["Docker"],
  "summary": "Relevant Python and API experience.\nDocker experience is not demonstrated."
}
```

`match_score` int 0-100 (bools/floats rejected) - `top_strengths` / `missing_skills` lists of
non-empty strings - `summary` exactly two non-empty lines - no other fields.

## Setup

Python 3.10+ (developed on 3.12).

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then edit .env and paste your key
python resume_matcher.py --resume sample_resume.txt --jd sample_job_description.txt
```

**Google Colab**
```python
!git clone https://github.com/<your-username>/<your-repo>.git
%cd <your-repo>
!pip install -q -r requirements.txt
from google.colab import userdata          # add GEMINI_API_KEY in the Colab "Secrets" panel
import os; os.environ["GEMINI_API_KEY"] = userdata.get("GEMINI_API_KEY")
!python resume_matcher.py --resume sample_resume.txt --jd sample_job_description.txt
```
Do not paste the key into a notebook cell or commit it.

Get a key from Google AI Studio. Keep it in the environment or a git-ignored `.env`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | required | API key (never logged or printed) |
| `GOOGLE_GENAI_USE_VERTEXAI` | `False` | Set `true` only to use the Vertex AI backend. Keys with an `AQ.` prefix (Vertex Express) must keep this `False` — the SDK would otherwise route them to Vertex AI, where they fail auth (HTTP 400). |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model id (`gemini-2.5-flash` is retired for new users) |
| `GEMINI_REQUEST_TIMEOUT_S` | 90 | Per-request timeout (thinking models can be slow) |
| `GEMINI_MAX_RETRIES` | 4 | Max retries for transient errors |
| `GEMINI_DEADLINE_S` | 120 | Overall deadline across retries and correction |
| `MAX_INPUT_CHARS` | 20000 | Max chars per input; larger inputs are rejected, not truncated |

**Verifying the model.** Gemini model availability changes, and the official deprecation
notes I found while building this were inconsistent about the 2.5 family's shutdown dates.
So do not trust the default blindly: run `python resume_matcher.py --list-models` with your
key, pick a current Flash-class model that supports structured output, and set
`GEMINI_MODEL`. Check https://ai.google.dev/gemini-api/docs/deprecations and the models page.
A wrong model name surfaces as a `config_error` (HTTP 404), not a fabricated result.

## Usage

```bash
python resume_matcher.py --resume sample_resume.txt --jd sample_job_description.txt
python resume_matcher.py --resume r.txt --jd j.txt --model <model-id> -v
```

```python
from resume_matcher import evaluate
result = evaluate(resume_text, job_description)   # MatchResult
print(result.model_dump_json())
```

Success: only the JSON on stdout, exit 0. Diagnostics go to stderr; resumes, job
descriptions and keys are never logged.

## Scoring rubric (identical to `SYSTEM_PROMPT.md`)

- **Essential requirements - 60 pts**, split equally. Demonstrated = full, mentioned only = half, not demonstrated = 0.
- **Preferred requirements - 20 pts**, same credit rules.
- **Experience relevance - 20 pts** (depth, seniority, domain fit).
- Cap: fewer than half of essentials demonstrated -> max 50. Almost no relevant content -> 0-15. 90+ reserved for near-complete coverage.
- Missing = "not demonstrated", not "lacks". Name, gender, age, ethnicity, disability, spelling/style etc. must not affect scores. The score is a relevance estimate, not a hiring probability.

The model applies the rubric; the code cannot recompute it, so scores can vary between runs.
`temperature=0` reduces but does not eliminate variance.

## Error handling

| Situation | Behaviour | Exit |
|---|---|---|
| Empty / non-string / oversized input, unreadable file | `input_error` | 2 |
| Missing key, bad numeric config, unknown model (404) | `config_error` | 3 |
| 401/403 or invalid key | `auth_error`, never retried | 3 |
| Other 4xx (e.g. 400) | `api_request_error`, never retried | 4 |
| 429, 5xx, connection error, timeout | retried: exponential backoff + full jitter, honours `Retry-After` / `RetryInfo` when exposed, capped by max retries and overall deadline | 4 |
| Malformed JSON / schema violation | safe recovery (strip one wrapping code fence only) -> at most **one** correction call -> `invalid_model_output` | 5 |
| Anything else | controlled `unexpected_error` | 1 |

On failure: stdout empty, stderr is `{"error": {"type", "message", "retryable"}}`. A match
result is never fabricated. Messages contain no keys or resume text. No regex scraping of
JSON, no `eval`.

The correction call re-sends the original inputs plus the previous reply and a
value-free list of problems, so it can in principle change substance; it is bounded to one
attempt and the result is fully re-validated.

## Prompt injection: mitigation and limits

- Instructions live in `system_instruction`; resume/JD are passed as user data inside blocks
  delimited by a random per-request id (a fake closing delimiter in the input cannot match).
- The system prompt tells the model to treat block contents as untrusted data, ignore
  embedded instructions, and not reward them; it includes an injection few-shot example.
- Known injection phrases are only *logged* as a diagnostic; this heuristic is trivially
  evaded and is not a defence.

**Limitations:** this reduces risk and does not make the model immune. Mocked tests check
only that injected text stays inside the data blocks, out of the system instruction and is
flagged. Actual resistance is checked only by `tests/test_live_integration.py` (irrelevant
resume + injections; asserts score stays <= 30, an arbitrary heuristic threshold). Subtle
injections (e.g. keyword stuffing in white text, or plausible-looking fabricated experience)
are not detectable by this system and need human review. Use the score as decision support,
not as an automated hiring decision.

## Tests

```bash
pip install -r requirements.txt
python -m pytest -q                                   # mocked; no key/network needed
RUN_LIVE_TESTS=1 python -m pytest tests/test_live_integration.py -v   # optional, uses your key + quota
```
PowerShell for live: `$env:RUN_LIVE_TESTS="1"; python -m pytest tests/test_live_integration.py -v`

## Publishing to GitHub safely

```bash
git init
git add .
git status                # confirm .env is NOT listed
git commit -m "Gemini resume matcher"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```
Create the empty public repo on github.com first. If a key was ever committed, revoke it and
create a new one; deleting the commit is not enough.

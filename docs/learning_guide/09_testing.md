# Part 8: Testing the Project Yourself

[← Part 7](08_debugging.md) · [Index](00_INDEX.md) · Next: [Part 9: Git](10_git_and_github.md)

---

## Chapter 22: Testing fundamentals

### 22.1 Words

| Term | Meaning | TriageBot example |
|---|---|---|
| **Software test** | Code that runs your code and checks the result automatically | `tests/test_priority.py` |
| **Unit test** | Tests one small function in isolation | `test_size_label_boundaries` in `tests/test_pr_insights.py` |
| **Integration test** | Tests several parts working together | Import → classify → database, in `tests/test_items_api.py` |
| **API test** | Sends real HTTP requests to the app (in memory) | `client.post("/repos/sync", ...)` in `tests/test_sync.py` |
| **Regression test** | Locks in a fixed bug | `test_batch_triage_is_limited_to_100_items`, `test_concurrent_init_db_applies_each_migration_once` |
| **Assertion** | The check itself: `assert resp.status_code == 404` | everywhere |
| **Fixture** | Reusable set-up that pytest passes to tests by parameter name | `client`, `db_path`, `fake_github` in `tests/conftest.py` |
| **Mocking / faking** | Replacing a real dependency (GitHub) with a pretend one | `tests/github_fakes.py:FakeGitHub` |
| **Test database** | A throw-away database used only by one test | `db_path` points `TRIAGEBOT_DB` at `tmp_path / "test.db"` |
| **Coverage** | How much code the tests execute. Not measured in this project (no coverage tool installed). High coverage still doesn't prove correctness | — |

### 22.2 How this project's tests are built (`tests/conftest.py`)

- **`_ml_off_by_default`** (`autouse=True`, so it applies to every test) sets
  `TRIAGEBOT_ML_ENABLED=0`. Rule-based tests then don't depend on the real
  model.
- **`ml_model`** turns ML on, using a **tiny model** trained once per session
  on made-up sentences (`tests/ml_fixtures.py:train_tiny_model`). It tests
  the *code*, not real accuracy.
- **`db_path`** gives every test a fresh SQLite file in a temporary folder,
  so your real `triagebot.db` is never touched.
- **`fake_github`** is an in-memory GitHub:
  - it serves repositories, issues, pulls and reviews through
    `httpx.MockTransport`, with real-looking `Link` pagination;
  - it supports `since=` and a `Date` header (for sync);
  - it returns **405 for any non-GET**, which is how tests prove the app
    never writes to GitHub.
- **`client`** is a FastAPI `TestClient` wired to the fake (through
  `app.dependency_overrides[get_github_client]`) and the temporary database.

**What the 543 tests cover** (count per file) [VERIFIED 2026-10-03]:

| File | Tests | Focus |
|---|---|---|
| `test_priority.py` | 106 | Every priority rule, label format and edge case |
| `test_pr_insights.py` | 43 | PR rules: dates, size buckets, stale, reviews |
| `test_items_api.py` | 35 | Import API, filters, search, errors |
| `test_github_client.py` | 33 | Parsing repository names, pagination, error mapping |
| `test_classify.py` | 32 | Category rules and explanations |
| `test_pr_api.py` | 29 | PR endpoints, details loading, migration v5→v6 |
| `test_duplicates.py` | 29 | Text cleaning, similarity, caps, fingerprint |
| `test_triage.py` | 26 | Manual triage; substring and negation regressions |
| `test_ml_service.py` | 23 | ML decision order and every fallback |
| `test_ml_model.py` | 22 | Training, prediction, safe save/load (checksum, no pickle) |
| `test_sync.py` | 21 | Sync: closed / reopened, failures, keyset pagination, limits |
| `test_ml_api.py` | 21 | ML through the API, migration v2→v3 |
| `test_duplicates_api.py` | 20 | Confirm / reject / undo, decisions preserved |
| `test_evaluate_priority.py` | 18 | Evaluation maths (hand-computed), split rules |
| `test_evaluate_duplicates.py` | 16 | Evaluation maths, Wilson intervals, cohorts |
| `test_database.py` | 16 | Migrations, concurrency, rollback, WAL |
| `test_api.py` | 16 | Manual-triage API, batch limit, JSON-only (415), security headers |
| `test_priority_api.py` | 13 | Priority through import / re-import, migration v3→v4 |
| `test_config.py` | 12 | Invalid settings → defaults + warning; token never logged |
| `test_ml_preprocess.py` | 9 | Text cleaning |
| `test_category_external.py` | 3 | External test-set construction |

### Checkpoint 22

1. Why do the tests never call the real GitHub?
2. What does an `autouse` fixture do?
3. Why is the test model "tiny" and trained on made-up sentences?

---

## Chapter 23: Running the test suite

All commands below were **run on 2026-10-03** with the results shown. Run
them from the project root with the venv active.

| Goal | Command | Result seen |
|---|---|---|
| Run everything | `python -m pytest` | `543 passed` (3.14.5 and 3.11.14) |
| One file | `python -m pytest tests/test_sync.py` | `21 passed` |
| One test, detailed | `python -m pytest "tests/test_sync.py::test_issue_closed_on_github_is_closed_after_sync" -v` | `1 passed`, shown as `PASSED` |
| Tests whose name matches | `python -m pytest -k rate_limit` | `7 passed, 536 deselected` |
| Stop at the first failure | `python -m pytest -x tests/test_database.py` | `16 passed` |
| ML tests | `python -m pytest tests/test_ml_model.py tests/test_ml_service.py tests/test_ml_api.py` | `66 passed` |
| Evaluation-code tests | `python -m pytest tests/test_evaluate_priority.py tests/test_evaluate_duplicates.py tests/test_category_external.py` | `37 passed` |
| Lint | `ruff check .` | `All checks passed!` |
| Whitespace errors in your changes | `git diff --check` | No output means clean |

**How to read the results:**

- `.` = passed, `F` = failed, `E` = an error during set-up. The final line
  summarises.
- On failure, pytest prints the test name, the code line, and the values on
  both sides of the failed `assert`. Add `-v` for one line per test.
- `-q` is always on (from `pyproject.toml`). Running `pytest -q` yourself
  makes it *extra* quiet and hides the summary line, so leave it out.
- On Python 3.14 you will see several hundred `DeprecationWarning`s from
  FastAPI and Starlette. They are about future Python versions and don't
  affect results.

**Frontend checks.** There is **no automated frontend test suite**: no
`package.json`, no Playwright or Jest. What was checked by hand during the
project:

- headless-browser walk-throughs of all tabs;
- a 390 px mobile width;
- a JavaScript syntax check.

If you have Node.js (it's optional), you can repeat the syntax check
[VERIFIED 2026-10-03]:

```bash
$ python3 -c "import re; open('/tmp/triagebot-ui.js','w').write('\n'.join(re.findall(r'<script>(.*?)</script>', open('static/index.html').read(), re.S)))"
$ node --check /tmp/triagebot-ui.js && echo OK
```

**Not covered by any automated test:**

- what the browser shows;
- clicking buttons;
- the layout at different widths.

**Previously reported vs fresh results:**

- The recorded final verification reported 543 passed (2026-10-03).
- This guide re-ran the suite and got the same.

### Checkpoint 23

1. Which command runs only the sync tests?
2. What does `-x` do?
3. Name one thing no automated test checks.

---

## Chapter 24: A real end-to-end test you can do yourself

**Safety first.** Use a disposable database for this whole plan, so your
real data is untouched:

```bash
$ export TRIAGEBOT_DB=/tmp/triagebot-e2e.db     # this terminal only
$ rm -f /tmp/triagebot-e2e.db*                 # [CHANGES STATE] removes an OLD test DB at that path only
$ uvicorn app.main:app --reload
```

Use a small, public repository and `Max items` ≤ 50, so you stay well within
GitHub's 60 requests per hour. `encode/httpx` was used in the project's own
verification.

| # | Do | Should happen | Code path | Passed if | If it fails, inspect |
|---|---|---|---|---|---|
| 1 | Start the server (above) | Start-up log lines | `main.lifespan` → `init_db` | "Application startup complete." | The traceback; Chapter 21 |
| 2 | Open http://127.0.0.1:8000 | Three tabs; a model status line | `GET /`, `GET /model` | Status says "Category model: ML …" | Console; `/model` |
| 3 | Import `encode/httpx`, State Open, Max 30 | A status line with counts | `POST /repos/import` → `importer.import_repo` | "Imported 30 items …" | Network tab: status and `detail` |
| 4 | Confirm it was saved | The repository in the dropdown | `GET /repos` | Shows "(30)" | `sqlite3` on a backup |
| 5 | Filter Type = Issue, then search a word | The table narrows | `GET /items?type=issue&q=…` | Only matching rows | Network request URL |
| 6 | Open **Why this suggestion?** on 3 items | Category reasons | `ml/service.classify` | Reasons match the source chip (ML n% / rules / maintainer label) | — |
| 7 | Check priorities | Badges plus reasons | `priority.score_priority` | Each "high" lists 2 kinds of evidence | — |
| 8 | Look for **Potential duplicates (N)**, or click **Find duplicates** | A panel with the score and shared terms | `GET /items/{id}/duplicates` | An explanation ending "(a similarity score, not a probability)" | There may simply be none in a small repository; that's normal |
| 9 | **Confirm duplicate** on one, **Not a duplicate** on another | The status changes | `PATCH /duplicates/{id}` | The status shows confirmed / rejected | Response `detail` |
| 10 | **Undo** | Back to pending | `PATCH` with `pending` | `decided_at` cleared | — |
| 11 | **PR insights** tab → **Load size & reviews** (PRs to load: 3) | Tiles and size badges | `GET /prs`, `POST /prs/details` | "3/… size & reviews loaded" | 429 means wait for the reset |
| 12 | **Sync with GitHub** | "Synced …: changes since … checked — …" | `POST /repos/sync` | `complete` (no "More changes remain") | The status message |
| 13 | Import `not a repo` | An error message | Pydantic + `parse_repo` | **422** | — |
| 14 | Import `octocat/this-repo-does-not-exist-xyz` | "Repository not found" | `_github_http_error` | **404** | — |
| 15 | **Failure handling, safely:** stop your network (Wi-Fi off) and click Sync | "Sync failed (nothing was changed): Could not reach GitHub…" | `GitHubUnavailableError` → **502** | The counts are unchanged after reconnecting | Don't hammer GitHub to provoke a 429; `tests/test_sync.py` covers rate limits with the fake |
| 16 | Check the database (backup first) | Rows present | — | `sqlite3 /tmp/e2e-copy.db "SELECT COUNT(*), SUM(state='open') FROM items;"` after `sqlite3 /tmp/triagebot-e2e.db ".backup '/tmp/e2e-copy.db'"` | — |
| 17 | Stop the server (Ctrl+C), start it again with the same `TRIAGEBOT_DB` | Data is still there | `init_db` skips the applied migrations | The same repository and counts appear | Did you use the same `TRIAGEBOT_DB`? |

When finished:

- `unset TRIAGEBOT_DB` switches back to the default database;
- the files in `/tmp` can be deleted.

### Checkpoint 24

1. Why does step 15 turn off Wi-Fi instead of sending many requests?
2. Which environment variable keeps your real data safe during the test?
3. What does step 17 prove?

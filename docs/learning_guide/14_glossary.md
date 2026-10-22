# Chapter 35: Glossary

[← Part 12](13_interviews_exercises_roadmap.md) · [Index](00_INDEX.md)

Terms are grouped by topic. *TB:* gives a TriageBot example.

---

## Web, backend and frontend

| Term | Definition |
|---|---|
| **API** | An agreed interface for programs to talk. *TB:* the routes in `app/main.py` |
| **ASGI** | The standard for connecting async Python web apps to servers. *TB:* Uvicorn runs the FastAPI `app` |
| **Backend / frontend** | The server-side part / the in-browser part. *TB:* `app/` / `static/index.html` |
| **CORS** | Browser rules for cross-website API calls. *TB:* no CORS headers are sent |
| **Dependency injection** | The framework provides a function's inputs. *TB:* `Depends(get_github_client)` |
| **Endpoint / route** | A method + path the server answers. *TB:* `POST /repos/sync` |
| **fetch()** | The browser function for HTTP requests. *TB:* the `api()` helper |
| **HTTP** | The request/response protocol of the web |
| **HTTP method** | GET (read), POST (create or act), PATCH (partial update), DELETE (remove) |
| **JSON** | A text data format: `{"key": "value"}` |
| **localhost / 127.0.0.1** | This computer. *TB:* the default address |
| **Middleware** | Code that runs around every request. *TB:* `json_only_writes_and_security_headers` |
| **Port** | A numbered network door. *TB:* 8000 |
| **Query parameter** | An option in the URL after `?`. *TB:* `/items?priority=high` |
| **Request / response body** | JSON sent to / returned from the server |
| **Status code** | 2xx OK, 4xx client error, 5xx server error. *TB:* 415 for a non-JSON write |
| **Uvicorn** | The ASGI web server used to run TriageBot |
| **Validation** | Checking input. *TB:* Pydantic models in `app/schemas.py` |

## Python

| Term | Definition |
|---|---|
| **Context manager (`with`)** | Runs set-up and clean-up code automatically. *TB:* `with get_connection() as conn:` |
| **Dataclass** | A simple class for holding data. *TB:* `ImportSummary`, `SyncSummary` |
| **Decorator** | `@something` above a function that adds behaviour. *TB:* `@app.get("/health")` |
| **Exception / traceback** | An error object / the report of where it happened |
| **Generator (`yield`)** | A function that produces items one at a time. *TB:* `GitHubClient.iter_issues` |
| **Module / package** | A `.py` file / a folder of them. *TB:* `app.ml` |
| **Pydantic** | A library that validates data using type hints |
| **Type hint** | `limit: int`, describing the expected type |
| **Virtual environment (venv)** | A private Python plus packages for one project. *TB:* `.venv/` |

## Databases

| Term | Definition |
|---|---|
| **Atomicity** | All-or-nothing changes |
| **Backup (`.backup`)** | A safe copy made by the sqlite3 tool |
| **Busy timeout** | How long to wait for a lock. *TB:* 15 s |
| **Column / row / table** | Field / record / sheet |
| **CRUD** | Create, read, update, delete |
| **Foreign key** | A column pointing to another table's key. *TB:* `items.repo_id` |
| **Index** | A structure that speeds up lookups. *TB:* `idx_items_filters` |
| **Migration** | A numbered one-time schema upgrade. *TB:* v1–v7 |
| **Parameterised query** | SQL with `?` placeholders; prevents SQL injection |
| **Primary key** | The unique row ID |
| **`PRAGMA user_version`** | SQLite's integer header field, used as the schema version |
| **Rollback / commit** | Undo / save a transaction |
| **Schema** | The structure of the tables |
| **SQL** | The query language for relational databases |
| **SQLite** | A database stored in a single file |
| **Transaction** | A group of changes that succeed or fail together |
| **Upsert** | Insert, or update if the row exists. *TB:* `ON CONFLICT … DO UPDATE` |
| **WAL** | Write-ahead logging; readers continue during writes |

## GitHub data

| Term | Definition |
|---|---|
| **Issue / pull request (PR)** | A report or request / a proposed code change |
| **Keyset pagination** | Paging by "continue after the last value seen" instead of page numbers. *TB:* sync |
| **Label** | A tag on an issue, e.g. `bug`, `P1` |
| **Maintainer** | A person responsible for a repository |
| **Pagination / `Link` header** | Results split into pages; the header names the next page |
| **Rate limit** | A cap on API requests: 60/hour anonymously, 5,000/hour with a token |
| **Stale PR** | *TB:* open and idle ≥ 30 days |
| **Sync point (`synced_through`)** | *TB:* the time up to which stored items are known to be current |
| **Token** | A secret password for an API. *TB:* `GITHUB_TOKEN` |
| **Triage** | Sorting incoming work by kind and urgency |

## Machine learning and evaluation

| Term | Definition |
|---|---|
| **Accuracy** | The share of predictions that are correct |
| **Baseline** | A simple reference to beat. *TB:* the keyword rules; "always medium" |
| **Bootstrap / confidence interval** | Resampling to estimate how uncertain a number is |
| **Checksum (SHA-256)** | A fingerprint of a file. *TB:* verifies the model weights |
| **Class / label** | A category to predict / the true answer |
| **Confusion matrix** | True class vs predicted class counts |
| **Cosine similarity** | How alike two vectors are (0–1 here). *TB:* duplicates |
| **Coverage** | The share of items the model decides, above the threshold. *TB:* 73.3% at 0.5 (test split) |
| **Cross-validation** | Rotating parts of the training data to tune settings |
| **Data leakage** | Test information leaking into training |
| **F1 / macro-F1** | The balance of precision and recall / its average over classes |
| **Fallback** | The plan B when the model is unavailable or unsure. *TB:* the keyword rules |
| **Held-out (test) set** | Data kept aside and scored once |
| **Leave-one-repository-out** | Train on all repositories but one, test on that one |
| **Logistic regression** | A linear model that outputs class probabilities |
| **Overfitting** | Learning quirks of the training data that don't generalise |
| **Pickle** | Python's object file format; can run code when loaded. *TB:* avoided |
| **Precision / recall** | Of the items flagged, how many are right / of the real ones, how many were found |
| **Softmax** | Turns scores into probabilities that sum to 1 |
| **Supervised learning** | Learning from labelled examples |
| **TF-IDF** | Word weights: frequent here × rare overall |
| **Threshold** | The minimum score needed to act. *TB:* ML 0.5, duplicates 0.20 |
| **Wilson interval** | A confidence interval for a proportion that works well with small samples |

## Testing and debugging

| Term | Definition |
|---|---|
| **Assertion** | A check in a test (`assert x == y`) |
| **Breakpoint** | A pause point for the debugger |
| **Fixture** | Reusable test set-up. *TB:* `client`, `db_path`, `fake_github` |
| **Integration / unit / API test** | Several parts together / one function / via HTTP |
| **Linter (ruff)** | A tool that flags style issues and likely bugs |
| **Mock / fake** | A stand-in for a real dependency. *TB:* `FakeGitHub` |
| **Regression test** | A test that keeps a fixed bug from returning |
| **TestClient** | FastAPI's in-memory HTTP client for tests |

## Git and CI/CD

| Term | Definition |
|---|---|
| **Branch** | A named line of commits |
| **CI (continuous integration)** | Automatic lint + tests on every change. *TB:* `.github/workflows/ci.yml` |
| **Continuous delivery / deployment** | Always releasable / released automatically |
| **Commit** | A saved snapshot with a message |
| **`.gitignore`** | Files Git must not track |
| **Job / step / runner / workflow** | CI building blocks (Chapter 28) |
| **Matrix** | The same CI job run with several settings. *TB:* Python 3.11–3.13 |
| **Merge conflict** | Competing edits to the same lines |
| **Pull request (in your own repository)** | A request to merge a branch; triggers this project's CI |
| **Remote (`origin`, `upstream`)** | A link to another copy of the repository |
| **Staging area** | The changes chosen for the next commit |

## Security and deployment

| Term | Definition |
|---|---|
| **Authentication / authorisation** | Who you are / what you may do. *TB:* neither yet |
| **CSRF** | Forged cross-site requests from the victim's browser. *TB:* partly mitigated by the JSON-only rule |
| **DNS rebinding** | A trick that makes a malicious domain resolve to `127.0.0.1` |
| **Ephemeral disk** | Storage that is wiped on restart or redeploy |
| **Environment variable** | A named setting given to a program at start-up |
| **HTTPS / certificate** | Encrypted HTTP / proof of the site's identity |
| **Persistent storage** | Storage that survives restarts |
| **Preflight** | The browser's permission check before some cross-site requests |
| **Security headers** | Browser instructions. *TB:* `nosniff`, `X-Frame-Options`, `Referrer-Policy` |
| **SQL injection** | Smuggling SQL through input. *TB:* prevented by `?` parameters |
| **XSS** | Injecting script into a page. *TB:* prevented by `escapeHtml` |

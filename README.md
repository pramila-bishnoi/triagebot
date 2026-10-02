# TriageBot

> A GitHub issue & pull-request triage dashboard. Import a public repository and
> TriageBot suggests a **category** (bug / feature / documentation / question)
> and a **priority** (low / medium / high) for every item — with a
> human-readable explanation of every point that led to the suggestion.

TriageBot is **read-only**: it only ever sends `GET` requests to GitHub and never
posts, labels, or comments. Issue categories come from a small local
**machine-learning model** (TF-IDF + logistic regression, scikit-learn) with the
keyword rule engine as a fallback; priority comes from the rule engine. It runs
offline with **no paid AI services**, and every suggestion shows where it came
from and why.

## Features

- **Import from GitHub** — issues *and* PRs from any public repository
  (`owner/name` or a GitHub URL), with pagination, an item limit, and an
  open / closed / all filter. Re-importing refreshes items instead of
  duplicating them.
- **ML issue classification** — a TF-IDF + logistic-regression model trained
  on 2,964 maintainer-labelled GitHub issues predicts bug / feature /
  documentation / question with a probability and the words that drove it.
  Low-confidence predictions, pull requests, and a missing model fall back to
  the rule engine. [Evaluation results](#evaluation-results) are measured, not
  estimated.
- **Explainable suggestions** — category, confidence, source (ML / rules /
  maintainer label), priority and an optional owner hint, each backed by
  reasons such as `words pointing to bug: 'traceback' (+0.31)` or
  `title mentions 'crash' (impact, +2)`.
- **Whole-word matching with negation** — `auth` does not match "author",
  `500` does not match "1500ms", and "not urgent" does not count as urgent.
- **Filterable dashboard** — filter by repository, type, category, priority,
  state and category source, search titles / bodies / `#numbers`, and page
  through results. A status line shows whether the ML model is loaded and its
  recorded test scores.
- **Clear error handling** — unknown or private repos, GitHub rate limits
  (with reset time), timeouts and network failures are reported in the UI.
- **Manual triage** — the original feature: paste an issue to get a severity,
  labels and a likely owner, plus the ML category preview.

## Quick start (macOS)

Requires Python 3.11–3.13 (Homebrew: `brew install python@3.13`).

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt        # runtime + tests + lint

# Optional: raise GitHub's rate limit from 60 to 5,000 requests/hour
cp .env.example .env                        # then paste a token into GITHUB_TOKEN

uvicorn app.main:app --reload --env-file .env   # or omit --env-file without a token
```

Open <http://127.0.0.1:8000>, enter a repository such as `fastapi/fastapi`, and
press **Import**. Interactive API docs are at <http://127.0.0.1:8000/docs>.

The trained model ships in `models/`, so no training is needed to run the app.
If it is missing or invalid, the app logs a warning and uses the rule engine.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `GITHUB_TOKEN` | *(unset)* | Optional. A fine-grained token with **no** permissions is enough for public repos. Never commit it. |
| `TRIAGEBOT_DB` | `./triagebot.db` | SQLite database path. |
| `GITHUB_TIMEOUT` | `10` | Seconds before a GitHub request times out. |
| `GITHUB_API_URL` | `https://api.github.com` | API base URL. |
| `TRIAGEBOT_ML_ENABLED` | `1` | Set to `0` to use only the rule engine. |
| `TRIAGEBOT_MODEL_PATH` | `models/issue_classifier` | Model base path (`.npz` + `.json`). |
| `TRIAGEBOT_ML_THRESHOLD` | `0.5` | Minimum ML probability; below it the rule engine decides. |

## Try the API

```bash
curl -X POST http://127.0.0.1:8000/repos/import \
  -H "Content-Type: application/json" \
  -d '{"repo": "fastapi/fastapi", "state": "open", "limit": 50}'

curl "http://127.0.0.1:8000/items?type=issue&priority=high&q=crash"
```

Example item (abridged; real output from importing `psf/requests` — a
repository the model was not trained on):

```json
{
  "repo": "psf/requests", "number": 7611, "kind": "issue",
  "title": "Discrepancy in status_codes.py: 102 mapped to \"early-hints\" instead of 103",
  "category": "bug", "category_source": "ml", "category_score": 0.678,
  "category_confidence": "medium", "priority": "medium", "owner_hint": null,
  "category_reasons": ["ML model: bug (68%), runner-up question (19%)",
                       "words pointing to bug: 'fix' (+0.12), '15' (+0.12), 'codeblock' (+0.10), ..."],
  "priority_reasons": ["body mentions 'regression' (impact, +1)", "category 'bug' (+1)",
                       "total score +2 between 0 and 2 -> medium"]
}
```

`category_source` is `ml`, `rules` or `github_label`; `category_score` is the
ML probability (only when the ML model decided).

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/repos/import` | Import issues & PRs from a public repo: `{"repo", "state": open\|closed\|all, "limit": 1–500}`. |
| `GET`  | `/repos` | Imported repositories with item counts. |
| `GET`  | `/items` | Imported items. Filters: `repo`, `type` (issue\|pr), `category`, `priority`, `state`, `source` (ml\|rules\|github_label), `q`; paging: `limit` (≤200), `offset`. Sorted high → low priority, then most recently updated. |
| `GET`  | `/model` | Whether the ML model is loaded, the threshold, and its recorded evaluation scores. |
| `GET`  | `/health` | Liveness probe. |
| `POST` | `/triage` | Manual triage **without** saving (severity, labels, owner, plus ML `category*` fields). |
| `POST` | `/triage/batch` | Manual triage of a list of issues. |
| `POST` | `/issues` | Manually triage **and** store an issue. |
| `GET`  | `/issues` | List stored manual issues (newest first). |
| `GET`  | `/issues/{id}` | Fetch one manual issue. |
| `PATCH`| `/issues/{id}` | Update a manual issue (re-triages if title/body change). |
| `DELETE`| `/issues/{id}` | Delete a manual issue. |
| `GET`  | `/` | Web UI. |

Import errors: `404` repo missing/private/issues disabled · `422` invalid input ·
`429` GitHub rate limit (with `Retry-After`) · `502` GitHub unreachable, timed
out, or rejected the token. An import that fails part-way stores nothing.

## How triage works

**Who decides the category** (`app/ml/service.py`), in order:

1. **Pull request** → rule engine (the model was trained on issues only).
2. **No usable model** (disabled, missing, failed checksum) → rule engine, exactly
   as in Phase 1.
3. **Maintainer label** — exactly one category label such as `bug` or
   `enhancement` → that category (`category_source = github_label`).
4. **ML model** — if its top probability is ≥ `TRIAGEBOT_ML_THRESHOLD` (0.5).
5. Otherwise (**ML unsure** or inference error) → rule engine.

Every fallback adds a note to the explanation, e.g.
`rule engine used: ML was unsure (question 48% < 50% threshold)`.

**Rule-engine category** — each category has a keyword list. A keyword in the title scores
+2, in the body +1 (the body adds at most +2 per category, so long bodies can't
drown out the title); a matching GitHub label (e.g. `bug`, `enhancement`) or a
conventional-commit PR prefix (`feat:`, `fix:`, `docs:`) scores +3; a title
ending in `?` adds +1 to *question*. The highest score wins; ties go to the
category with more title evidence, then to the order bug → feature →
documentation → question, and are flagged as low confidence. With no signals,
issues default to *question* and PRs to *feature* (low confidence). For
dependency-bump PRs ("Bump foo from 1.2 to 1.3") the body — upstream release
notes — is ignored.

**Priority** — points are added up and every contribution is listed:

| Signal | Points |
|--------|--------|
| Severe words (`critical`, `outage`, `security`, `data loss`, …) | +3 (title) / +2 (body) |
| Impact words (`crash`, `regression`, `500`, `can't`, …) | +2 / +1 |
| Minor words (`typo`, `cosmetic`, `nice to have`, …) | −2 / −1 |
| Priority label (`critical`, `p1`, `priority: high`, …) | +3 |
| Low-priority label (`good first issue`, `p3`, …) | −2 |
| Category bug / documentation or question | +1 / −1 |
| ≥ 3 reactions / ≥ 10 reactions | +1 / +2 |
| ≥ 10 comments | +1 |

Score ≥ 3 → **high**, ≤ −1 → **low**, otherwise **medium**.

**Matching rules** — keywords match whole words with simple inflections
(`crash` → crashes / crashed / crashing). A keyword is ignored when directly
preceded by a negation (`not`, `no`, `never`, `without`, `non-`), optionally
with a filler word ("not a bug", "not very urgent"). HTML comments in issue
bodies (template instructions) are ignored. All tables live in
[`app/triage.py`](app/triage.py).

## ML category classifier

### Model

- **Features:** TF-IDF over single words and two-word phrases
  (`min_df=2`, `max_df=0.9`, `sublinear_tf`, ≤ 50,000 terms; 47,128 after
  fitting). Text = title (twice, for weight) + body, lower-cased, with HTML
  comments removed, fenced code blocks replaced by `codeblock` and URLs by
  `urltoken` ([`app/ml/preprocess.py`](app/ml/preprocess.py)).
- **Classifier:** multinomial logistic regression, `class_weight="balanced"`,
  C = 4.0 chosen by 5-fold cross-validation on the training split
  (CV macro-F1 0.749).
- **Explanation:** for the predicted class, each word's TF-IDF value × its
  (class-centred) weight; the five largest positive contributions are shown.
- **Storage:** `models/issue_classifier.npz` holds only numeric / string arrays
  and is loaded with `allow_pickle=False` (no pickle / joblib, so a tampered
  file cannot run code). `models/issue_classifier.json` holds settings, metrics
  and the file's SHA-256, which is checked on load; a model trained with a
  different preprocessing version is refused.

### Dataset

Ground truth is the category label that **maintainers** applied to public
GitHub issues in ten projects (pull requests excluded; issues created before
2026-09-01; up to the 100 newest per project and category):

| Project | Code licence | bug / feature / documentation / question labels |
|---------|--------------|------------------------------------------------|
| pandas-dev/pandas | BSD-3-Clause | `Bug` / `Enhancement` / `Docs` / `Usage Question` |
| pytest-dev/pytest | MIT | `type: bug` / `type: enhancement` / `type: docs` / `type: question` |
| microsoft/terminal | MIT | `Issue-Bug` / `Issue-Feature` / `Issue-Docs` / `Issue-Question` |
| microsoft/TypeScript | Apache-2.0 | `Bug` / `Suggestion` / `Docs` / `Question` |
| python-poetry/poetry | MIT | `kind/bug` / `kind/feature` / `area/docs` / `kind/question` |
| pypa/pip | MIT | `type: bug` / `type: enhancement` / `type: docs` / `type: support` |
| psf/black | MIT | `T: bug` / `T: enhancement` / `T: documentation` / `T: user support` |
| tauri-apps/tauri | Apache-2.0 OR MIT | `type: bug` / `type: feature request` / `type: documentation` / `type: question` |
| pydantic/pydantic | MIT | `bug V2` / `feature request` / `documentation` / `question` |
| denoland/deno | MIT | `bug` / `feat` / `docs` / `question` |

3,707 issues were kept (199 carrying more than one category label were dropped
as ambiguous); 1 duplicate text was removed before splitting, leaving 3,706:
bug 990, feature 980, question 873, documentation 863.

**Licensing note.** The licences above cover the projects' *code*. Issue text
belongs to its authors and is read through the public GitHub API under GitHub's
Terms of Service. It is therefore **not redistributed**: raw text lives only in
the git-ignored `data/raw/` and `data/issues.jsonl`. The repository contains
`data/dataset_manifest.csv` (repo, issue number, label, URL — no text) so the
exact dataset can be audited or re-fetched, plus the trained weights.

### Training and retraining

```bash
python -m scripts.fetch_dataset     # ~40 GitHub search requests; paced, resumable
python -m scripts.train_classifier  # ~1.5 min; writes models/ and models/EVALUATION.md
```

`fetch_dataset` caches each page in `data/raw/` (use `--refresh` to re-download)
and works without a token (slower); `GITHUB_TOKEN` raises the search limit.
`train_classifier` is seeded and:

1. removes duplicate texts **before** splitting (no train/test leakage),
2. makes a stratified 80/20 split (2,964 train / 742 test, seed 42),
3. picks C by cross-validation on the training split only,
4. evaluates once on the untouched test split, plus the rule engine on the
   same split (without GitHub labels, which would leak the answer),
5. runs leave-one-repository-out evaluation,
6. saves the model that was evaluated and regenerates `models/EVALUATION.md`.

A running server picks up a retrained model automatically (it reloads when the
model's `.json` changes). Issue text and labels on GitHub can change over time,
so a re-fetch may differ slightly from the original snapshot.

### Evaluation results

From [`models/EVALUATION.md`](models/EVALUATION.md) (generated by the training
script, trained 2026-10-02, scikit-learn 1.9.1). **Held-out test split, 742
issues:**

| metric | ML model | rule engine (Phase 1) |
|---|---|---|
| accuracy | 0.768 | 0.558 |
| macro precision | 0.770 | 0.584 |
| macro recall | 0.765 | 0.546 |
| macro F1 | 0.766 | 0.527 |

| class | precision | recall | F1 | support |
|---|---|---|---|---|
| bug | 0.750 | 0.803 | 0.776 | 198 |
| feature | 0.803 | 0.852 | 0.827 | 196 |
| documentation | 0.869 | 0.769 | 0.816 | 173 |
| question | 0.657 | 0.634 | 0.645 | 175 |

Confusion matrix (rows = true class, columns = predicted):

| true \ predicted | bug | feature | documentation | question |
|---|---|---|---|---|
| bug | 159 | 8 | 3 | 28 |
| feature | 11 | 167 | 6 | 12 |
| documentation | 10 | 12 | 133 | 18 |
| question | 32 | 21 | 11 | 111 |

**Confidence threshold.** At the default 0.5 threshold the model decides 73.3%
of test issues, with 0.858 accuracy on those; the rest go to the rule engine.
The threshold was fixed in advance, not tuned on the test split
(see `EVALUATION.md` for 0.4–0.8).

**Unseen repositories.** In leave-one-repository-out evaluation (train on nine
projects, test on the tenth) mean macro-F1 drops to **0.658** (rules: 0.513),
ranging from 0.574 (denoland/deno) to 0.838 (pandas-dev/pandas). Expect
numbers closer to this when importing a repository the model has never seen.
The model is also less often confident there: in a spot check importing 60
items from `psf/requests` (not in the training data), it was ≥ 50% sure for 6
of 23 issues; 14 fell back to the rules and 3 used maintainer labels. That is
one observation, not a measured accuracy.

### Limitations of the model

- **Moderate accuracy.** Roughly one in four test issues is misclassified;
  *question* is the weakest class (F1 0.645) and is most often confused with
  *bug*. Treat categories as suggestions for a human to confirm.
- **Generalisation.** Trained on ten mostly developer-tool projects with
  English text; performance on unrelated domains, other languages, or repos
  with unusual issue templates is unknown and likely lower.
- **Label noise.** Maintainers' labels are inconsistent across projects
  (e.g. `type: support` vs `Question`), and that noise is in the ground truth.
- **Template cues.** Issue templates (e.g. pandas' bug template) make some
  repositories easier; the leave-one-repo-out numbers show the effect.
- **Simple explanations.** Top-weighted words can include function words
  (e.g. "when", "in the"); they describe the model's arithmetic, not causal
  reasons.
- **Issues only.** Pull requests are always categorised by the rules.
- **Existing data.** Items imported before the model was enabled keep their
  rule-based category until the repository is re-imported.

## Architecture

```
Browser (static/index.html — vanilla JS)
   │ JSON
   ▼
app/main.py ─ FastAPI routes (thin)
   ├─► app/importer.py ──► app/github_client.py ──► api.github.com (GET only)
   │        │
   │        └─► app/ml/service.py ─► app/ml/model.py (TF-IDF + LR, models/*.npz)
   │                 │  fallback
   │                 └─► app/triage.py   pure functions: classify_item(), triage()
   └─► app/repository.py ──► app/database.py ──► SQLite (migrations via PRAGMA user_version)
```

- **`app/github_client.py`** — httpx client: pagination via `Link` headers,
  typed errors for 401/403/404/410/429/5xx, timeouts and network failures.
- **`app/importer.py`** — fetch → normalise → classify → upsert.
- **`app/triage.py`** — the rule engine (no I/O).
- **`app/ml/`** — `preprocess.py` (shared text cleaning), `model.py`
  (inference, explanations, safe save/load), `train.py` (fit, CV, metrics),
  `service.py` (decision order and fallbacks).
- **`scripts/`** — `fetch_dataset.py` and `train_classifier.py` (offline only;
  not used by the running app).
- **`app/repository.py`** — SQL, one function per query.
- **`app/database.py`** — connections and numbered schema migrations.
  Tables: `issues` (manual triage), `repos`, `items` (v3 adds
  `category_source`, `category_score`).
- **`app/schemas.py`** — Pydantic models = the API contract and validation.
- **`app/config.py`** — environment-based settings.

## Testing & verification

```bash
ruff check .   # lint — must be clean
pytest         # engine, ML, GitHub client (mocked), database, and HTTP API
```

GitHub is never contacted by the tests: `tests/github_fakes.py` provides an
in-memory fake served through `httpx.MockTransport`. ML tests train a tiny
model on a handful of clearly synthetic sentences (`tests/ml_fixtures.py`) to
exercise the code quickly; those fixtures say nothing about real accuracy.
Other tests run with ML disabled so they keep testing the rule engine.

| File | Covers |
|------|--------|
| `tests/test_triage.py` | Manual-triage rules + regression tests for substring and negation bugs |
| `tests/test_classify.py` | Category, priority, explanations, labels, PR prefixes |
| `tests/test_github_client.py` | Repo parsing, pagination, limits, rate limits, error mapping |
| `tests/test_database.py` | Migrations (incl. upgrading an old DB), upserts, filters, search, paging |
| `tests/test_api.py` / `tests/test_items_api.py` | End-to-end HTTP behaviour |
| `tests/test_ml_preprocess.py` | Text cleaning shared by training and inference |
| `tests/test_ml_model.py` | Training, predictions, explanations, metrics, safe save/load (checksum, no pickle) |
| `tests/test_ml_service.py` | Decision order and every fallback path |
| `tests/test_ml_api.py` | API + dashboard integration, migration v2→v3, training script, shipped model |

## CI/CD

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `ruff check .` and
`pytest` on Python 3.11 / 3.12 / 3.13 for every push and pull request, plus a
smoke job that verifies the app imports.

## Deployment

[`render.yaml`](render.yaml) deploys a free [Render](https://render.com) web
service (**New → Blueprint** → pick this repo). Set `GITHUB_TOKEN` in the
Render dashboard; shared hosting IPs exhaust GitHub's anonymous limit quickly.

> The free plan sleeps after ~15 min idle and its filesystem is ephemeral, so
> imported data resets on redeploy — fine for a demo. The app has no user
> accounts: anyone with the URL can import repos and view or delete manual
> issues, so don't expose it publicly with data you care about.

## Limitations

- See [Limitations of the model](#limitations-of-the-model) for the ML
  classifier. Priority is still rule-based and English-only.
- Manual triage keeps the original four-level severity; the ML category is
  shown in the preview but not stored with manual issues.
- PR details (changed files, size, CI status) and duplicate detection are not
  implemented yet.
- Imports run synchronously and are capped at 500 items per request.

## Project layout

```
triagebot/
├── app/
│   ├── main.py           # FastAPI app + routes
│   ├── triage.py         # rule-based engine (pure logic)
│   ├── github_client.py  # read-only GitHub REST client
│   ├── importer.py       # fetch -> classify -> store
│   ├── repository.py     # SQLite data access
│   ├── database.py       # connection + migrations
│   ├── schemas.py        # Pydantic API models
│   ├── config.py         # environment settings
│   └── ml/               # preprocess, model, train, service
├── scripts/              # fetch_dataset.py, train_classifier.py
├── models/               # issue_classifier.npz/.json + EVALUATION.md
├── data/dataset_manifest.csv  # which issues were used (no text)
├── static/index.html     # web UI
├── tests/                # pytest suite (GitHub is faked)
├── .env.example          # configuration template
├── .github/workflows/    # CI
├── requirements.txt      # runtime deps
├── requirements-dev.txt  # + tests & lint
└── pyproject.toml        # tooling config (pytest, ruff)
```

## License

MIT — see [`LICENSE`](LICENSE).

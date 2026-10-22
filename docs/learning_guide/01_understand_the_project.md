# Part 1: Understand the Project from Zero

[← Index](00_INDEX.md) · Next: [Part 2: Setup](02_setup_and_configuration.md)

---

## Chapter 1: What is TriageBot?

### 1.1 Starting with GitHub

**GitHub** is a website where people store code and work on it together. Each
project on GitHub lives in a **repository** (or "repo"), which is a folder of
code plus its history. Example: `psf/requests` is the repository `requests`
owned by the account `psf`.

Two kinds of work items appear in every busy repository:

| Item | What it is | Everyday analogy |
|---|---|---|
| **Issue** | A message saying "something is wrong" or "please add something" or "how do I…?" | A ticket at a help desk |
| **Pull request (PR)** | A proposed change to the code that someone wants merged in | A suggested edit to a shared document, waiting for approval |

A **maintainer** is a person responsible for a repository. They read issues,
review PRs, decide what is important, and close duplicates.

### 1.2 The problem: too many items

Popular projects receive many issues every day. A maintainer opening the issue
list sees hundreds of entries in no particular order:

- some are bugs (something is broken),
- some are feature requests,
- some are documentation problems,
- some are questions that belong on a forum,
- some say the same thing as an older issue (duplicates),
- and some PRs have been waiting weeks for a review.

**Triage** (a word borrowed from hospitals, where nurses decide who needs care
first) means sorting this incoming work:

1. What *kind* of item is this? → **classification** (category)
2. How *urgent* is it? → **priority**
3. Has it been reported already? → **duplicate detection**
4. Which PRs need attention? → **PR insights**

Maintainers usually do all of this by hand.

### 1.3 What TriageBot does

TriageBot is a **dashboard** (a web page that summarises information) that you
run on your own computer:

1. You type a public repository name, e.g. `psf/requests`, and click
   **Import**.
2. TriageBot downloads that repository's recent issues and PRs from GitHub.
   It only *reads*; it never writes anything back to GitHub.
3. For every item it **suggests**:
   - a **category**: `bug`, `feature`, `documentation` or `question`;
   - a **priority**: `low`, `medium` or `high`;
   - possible **duplicates** among the issues;
   - **PR insights**: open / draft / merged / closed, how old it is, whether
     it is stale, its size, and its review state.
4. Every suggestion has a **"Why this suggestion?"** panel listing the
   evidence. For example: `title mentions 'crash' (impact, +2)`.
5. You (the human) stay in charge. You confirm or reject duplicates, and a
   maintainer's own priority label always wins.

### 1.4 What makes it different from GitHub's own issue list

| GitHub issue list | TriageBot |
|---|---|
| Shows items, sorted by date | Suggests category and priority for **every** item, including unlabelled ones |
| You read each item to understand it | Shows **why** each suggestion was made |
| No duplicate hints | Suggests similar issues with a similarity score and the shared words |
| PR list shows status only | PR insights: stale, large, awaiting review, changes requested, with reasons |
| Lives on GitHub | Runs locally; read-only; uses no paid AI services |

### 1.5 A real-world example

Imagine a repository with 400 open issues and nobody has labelled them. A
maintainer imports it with **Max items** set to 400, then:

1. Filters **Priority = High**. The list is sorted high → medium → low
   anyway. Each "high" shows its evidence, e.g. "title mentions 'crash'
   (impact) + 'all users' (scope)".
2. Filters **Category = Question** to find items that belong on a forum.
3. Opens an issue showing **Potential duplicates (1)**, compares the two
   titles and shared terms, and clicks **Confirm duplicate** or **Not a
   duplicate**. Nothing changes on GitHub; the decision is stored locally.
4. Switches to the **PR insights** tab, clicks the **stale** tile, and sees
   PRs with no activity for 30+ days.
5. A week later, clicks **Sync with GitHub** to pick up issues that were
   closed or reopened in the meantime.

### 1.6 The complete user journey (what you see)

| Step | Where in the UI | What happens behind it |
|---|---|---|
| Open http://127.0.0.1:8000 | Page loads with three tabs: **GitHub items**, **PR insights**, **Manual triage** | `GET /` returns `static/index.html` |
| See the model status line | "Category model: ML (TF-IDF + logistic regression) · test macro-F1 0.766 …" | `GET /model` |
| Import | Fill in **Public repository**, **State**, **Max items** → **Import** | `POST /repos/import` |
| Browse | The table of items, with filters (Repository, Type, Category, Priority, State, Category source, Search) | `GET /items` |
| Understand | Click **Why this suggestion?** | Already in the `/items` response |
| Duplicates | **Potential duplicates (N)** → Confirm / Not a duplicate / Undo | `GET /items/{id}/duplicates`, `PATCH /duplicates/{id}` |
| Re-check duplicates | **Find duplicates** button | `POST /duplicates/refresh` |
| Keep current | **Sync with GitHub** button | `POST /repos/sync` |
| PRs | **PR insights** tab, tiles, filters, **Load size & reviews** | `GET /prs`, `POST /prs/details` |
| Manual | **Manual triage** tab: type a title, see a live suggestion, **Triage & save** | `POST /triage`, `POST /issues`, … |

### Checkpoint 1

1. In your own words, what is the difference between an issue and a PR?
2. Name the four triage outputs TriageBot suggests.
3. Does TriageBot ever change anything on GitHub? (Answer: no. Its GitHub
   client only sends `GET` requests; see Chapter 8.)

---

## Chapter 2: Project architecture

### 2.1 The folder structure (verified with `ls`)

```
triagebot/
├── app/                    ← the application itself (Python)
│   ├── main.py             ← web server routes (the "front desk")
│   ├── schemas.py          ← shapes of request/response data (validation)
│   ├── config.py           ← reads settings from environment variables
│   ├── database.py         ← opens the SQLite database, runs migrations
│   ├── repository.py       ← every SQL query lives here
│   ├── github_client.py    ← talks to the GitHub API (GET only)
│   ├── importer.py         ← import and sync logic
│   ├── triage.py           ← keyword rules for category (+ old manual triage)
│   ├── matching.py         ← whole-word keyword matching and negation
│   ├── priority.py         ← the priority score
│   ├── duplicates.py       ← duplicate detection (TF-IDF + cosine similarity)
│   ├── pr_insights.py      ← PR rules: status, stale, size, reviews
│   └── ml/                 ← the machine-learning category model
│       ├── preprocess.py   ← turns title + body into one clean text
│       ├── model.py        ← the classifier: predict, save, load safely
│       ├── train.py        ← training helpers (used by scripts, not the app)
│       └── service.py      ← decides: maintainer label? ML? rules?
├── static/index.html       ← the whole frontend (HTML + CSS + JavaScript)
├── models/                 ← the trained model files + EVALUATION.md
├── scripts/                ← offline tools: fetch datasets, train, evaluate
├── tests/                  ← 21 test files + fakes and fixtures
├── data/                   ← dataset manifests (IDs only); raw text is git-ignored
├── docs/                   ← evaluation reports, notes, screenshots, this guide
├── .github/workflows/ci.yml← the CI workflow (Chapter 28)
├── requirements.txt        ← packages needed to run the app
├── requirements-dev.txt    ← + packages needed for tests and linting
├── pyproject.toml          ← project name, pytest and ruff settings
├── .env.example            ← template of all settings (no secrets)
├── .gitignore              ← files Git must not track
├── render.yaml             ← an (untested) deployment blueprint
├── LICENSE                 ← MIT licence
├── README.md               ← the project overview
└── docs/learning_guide/       ← educational notes and exercises
```

### 2.2 What each folder is for

| Folder or file | Role | Used by the running app? |
|---|---|---|
| `app/` | All server-side code | Yes |
| `app/ml/` | Category model: `preprocess.py` and `model.py` at run time; `train.py` only when training | Partly |
| `static/` | The dashboard page the browser loads | Yes |
| `models/` | `issue_classifier.npz` (numbers) + `.json` (settings, checksum, metrics) | Yes (loaded at first use) |
| `scripts/` | Fetch datasets from GitHub, train the model, run evaluations | No (you run them by hand) |
| `tests/` | Automated tests (`pytest`) | No |
| `data/` | `*_manifest.csv` = lists of issue IDs and URLs used in evaluations. Raw issue text sits in git-ignored folders such as `data/raw/` | No |
| `docs/` | Generated evaluation reports + hand-written notes + screenshots | No |
| `.github/workflows/` | Instructions for GitHub Actions CI | No (runs on GitHub) |
| `requirements*.txt`, `pyproject.toml` | What to install and how tools behave | At install time |
| `.env.example` | Documents the 11 settings the code reads | Template only |

### 2.3 The real architecture

```
                    ┌────────────────────────────────────────────┐
  You ─► Browser ─► │ static/index.html  (JavaScript fetch())     │
                    └───────────────┬────────────────────────────┘
                                    │ HTTP + JSON
                    ┌───────────────▼────────────────────────────┐
                    │ app/main.py (FastAPI)                       │
                    │  • middleware: JSON-only writes, headers    │
                    │  • app/schemas.py validates input (422)     │
                    │  • turns errors into 404/415/422/429/502    │
                    └──┬─────────┬──────────┬─────────┬──────────┘
          import/sync  │         │ items    │ PRs     │ manual triage
                    ┌──▼──────┐  │          │       ┌─▼──────────────┐
                    │importer │  │          │       │ triage.py       │
                    └──┬───┬──┘  │          │       │ + ml/service.py │
     GET requests      │   │ classify each item     └─────────────────┘
  ┌────────────────────▼┐  └──► app/ml/service.py ─► ml/model.py (ML)
  │ app/github_client.py│              │  fallback ─► triage.py (rules)
  │ → api.github.com    │              └──────────► priority.py
  └─────────────────────┘
                       ▼ save (one transaction)
                    ┌──────────────────────────┐   ┌─────────────────────┐
                    │ app/repository.py (SQL)  │◄──│ app/duplicates.py    │
                    │ app/database.py          │   │ (after import/sync)  │
                    │ SQLite file              │   └─────────────────────┘
                    └─────────┬────────────────┘
                              │ read rows
                              ▼
                    app/pr_insights.py (computed on every /prs read)
```

**Corrections to the simple "User → Frontend → Backend → Services → GitHub
and database" picture:**

1. Classification happens **inside the importer, before saving**, not after
   the database.
2. Duplicate detection runs **after** saving, and reads issues back from the
   database.
3. PR insights are **not stored**. They are computed each time you open
   `/prs`, because "stale" depends on *today's* date.

### 2.4 Each component in detail

| Component | Responsibility | Input | Output | Depends on | If it fails |
|---|---|---|---|---|---|
| `static/index.html` | Show data, send user actions | Clicks, typing | `fetch()` calls | `app/main.py` | The page shows an error message, e.g. "Could not reach the TriageBot server" |
| `app/main.py` | Routes, validation, error translation | HTTP requests | JSON responses | Everything in `app/` | The server doesn't start, or returns 500 |
| `app/schemas.py` | Defines the allowed shape of data | Raw JSON / query parameters | Python objects, or a 422 error | `github_client.parse_repo` | Bad input is rejected before your code runs |
| `app/github_client.py` | `GET` requests to GitHub, pagination, typed errors | owner, name, limits | Raw issue / PR dictionaries | `httpx`, `config.py` | Raises `RateLimitError`, `RepoNotFoundError`, etc.; `main.py` turns them into 429 / 404 / 502 |
| `app/importer.py` | Import and sync workflows | GitHub client, repo | Summary counts | `github_client`, `ml.service`, `repository` | Nothing is saved, because fetching finishes before writing |
| `app/ml/service.py` | Picks the category source | title, body, labels | `Classification` | `ml/model.py`, `triage.py` | Falls back to the rules, with a note in the reasons |
| `app/triage.py` | Keyword rules; the old manual triage | Text, labels | Category + reasons | `matching.py`, `priority.py` | (Pure logic, no I/O) |
| `app/priority.py` | Priority score | Text, labels, category | low / medium / high + reasons | `matching.py` | (Pure logic) |
| `app/duplicates.py` | Similar-issue pairs | A repository's issues from the database | Pairs with scores | scikit-learn, `repository` | `main._refresh_duplicates` logs the error; the import still succeeds |
| `app/pr_insights.py` | PR status and flags | One PR row + "now" | Status, flags, insights | — | (Pure logic) |
| `app/repository.py` | All SQL | Python values | Rows as dictionaries | `database.py` | The transaction rolls back; the error reaches the route (500) |
| `app/database.py` | Connections, migrations | — | Connections | `sqlite3` | The app can't start if migrations fail |

### Checkpoint 2

1. Which file would you open to find out how `/repos/sync` is handled?
   (`app/main.py`, function `sync_repo`, which calls `app/importer.py:sync_repo`.)
2. Which module is the *only* one that talks to GitHub?
3. Why are PR insights not stored in the database?

---

## Chapter 3: Technologies used

All of these are confirmed in `requirements.txt`, `requirements-dev.txt`,
`pyproject.toml` or the code.

### 3.1 Used by the running application

| Technology | What it is | Why TriageBot uses it | Where |
|---|---|---|---|
| **Python 3.11+** | A programming language | Everything server-side. `pyproject.toml` says `requires-python = ">=3.11"` | `app/` |
| **FastAPI 0.115.6** | A Python framework for building web APIs | Routes, automatic validation, automatic docs at `/docs` | `app/main.py` |
| **Pydantic 2** (installed with FastAPI) | Library that checks data has the right shape | Request and response models | `app/schemas.py` |
| **Uvicorn 0.34.0** | An ASGI web server (Chapter 6) that runs FastAPI | `uvicorn app.main:app` | Command line |
| **httpx 0.28.1** | Library for making HTTP requests | Calling the GitHub API | `app/github_client.py` |
| **SQLite** (via Python's built-in `sqlite3`) | A database stored in a single file | Stores repositories, items, duplicates, PR data | `app/database.py`, `app/repository.py` |
| **SQL** | The language for asking databases questions | Every query | `app/repository.py` |
| **scikit-learn 1.9.1** | A machine-learning library | TF-IDF text features; logistic regression (training); TF-IDF for duplicates | `app/ml/`, `app/duplicates.py` |
| **NumPy** (installed with scikit-learn) | Fast number arrays | Model weights, similarity matrices | `app/ml/model.py`, `app/duplicates.py` |
| **HTML / CSS / JavaScript** | Structure / style / behaviour of web pages | The dashboard, all in one file, with no framework | `static/index.html` |

**A small example of how they connect:**

```text
Browser JavaScript:  fetch("/items?priority=high")
      ↓  HTTP GET
Uvicorn receives it → FastAPI matches the route list_items() in app/main.py
      ↓  Pydantic checks priority is "low" | "medium" | "high"
repository.list_items() runs SQL on SQLite
      ↓  rows become JSON
JavaScript draws the table rows
```

### 3.2 Machine-learning terms used here (detail in Chapters 15 and 18)

- **TF-IDF** turns text into numbers. Each word's weight is high when the word
  is frequent *in this text* but rare *across all texts*.
- **Logistic regression** is a simple, fast model that turns those numbers
  into a probability for each category.
- **Cosine similarity** measures how alike two TF-IDF vectors are (0 = nothing
  in common, 1 = the same).

### 3.3 Development and evaluation tools (not needed to *run* the app)

| Tool | Purpose | Where |
|---|---|---|
| **pytest 8.3.4** | Runs the automated tests | `tests/`; settings in `pyproject.toml` (`testpaths = ["tests"]`, `addopts = "-q"`) |
| **ruff 0.8.6** | "Linter": finds style problems and likely bugs | `pyproject.toml` `[tool.ruff]` (rules E, W, F, I, UP, B; line length 100) |
| **Git** | Version control (Chapter 25) | `.git/` |
| **GitHub Actions** | Runs lint and tests on GitHub's computers (CI) | `.github/workflows/ci.yml` |
| **GitHub REST API** | GitHub's web interface for programs | Called by the app, and by `scripts/fetch_*.py` |
| **Node.js** | A JavaScript runtime | **Not a project dependency.** It was used only during reviews, to syntax-check the dashboard script (`node --check`). There is no `package.json` |
| **The `scripts/` folder** | Fetching datasets, training, evaluations | Run by hand; uses the same `httpx` / scikit-learn |

### 3.4 What the project does NOT use

- No JavaScript framework (React, Vue).
- No separate database server (PostgreSQL, MySQL).
- No Docker, Redis, Kubernetes, or paid AI APIs.
- No `.env` loader inside the code. The code reads real environment
  variables with `os.environ`; Uvicorn can load a `.env` file for you with
  `--env-file` (Chapter 5).

### Checkpoint 3

1. Which library validates request bodies?
2. Is Node.js required to run TriageBot? (No.)
3. Which two features use TF-IDF? (The category model and duplicate
   detection.)

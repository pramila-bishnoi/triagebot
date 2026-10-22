# Part 12: Become Confident in Your Project

[← Part 11](12_security_and_deployment.md) · [Index](00_INDEX.md) · Next: [Glossary](14_glossary.md)

---

## Chapter 32: Explaining the project in interviews

**Be honest about how it was built.**

- **The record:** the repository started as a starter project by other
  authors (the first 7 commits). Commit `6dec35d` and the uncommitted Phases
  3–6 were directed by you and implemented with an AI coding assistant
  (Claude Code), as recorded in the commit trailer and the project documents.
- **A suggested framing:** "I took over a starter project, defined the
  requirements and constraints for each phase, directed an AI-assisted
  implementation, reviewed and verified it, and I can explain and debug every
  part."
- **Don't claim** you wrote every line by hand.

### 32.1 Ready-made explanations

**30 seconds:** "TriageBot is a read-only dashboard for GitHub repositories. It
imports a repository's issues and pull requests and, for each one, suggests a
category, using a small local machine-learning model with keyword rules as a
fallback, and a priority. It flags possible duplicate issues for a maintainer
to confirm, and shows PR insights like stale or awaiting review. Every
suggestion shows its reasons. It's FastAPI and SQLite with 543 automated
tests."

**1 minute:** add the architecture:

- one FastAPI app with a GET-only GitHub client;
- an importer that fetches everything first, then saves in one transaction;
- SQLite with numbered migrations;
- a plain-JavaScript dashboard.

Then the honest results:

- on 8 repositories it had never seen, the category model scored macro-F1
  0.715;
- duplicate suggestions are usually right when shown, but find only about 40%
  of real duplicates.

**3-minute technical walk-through:**

1. A request arrives at `app/main.py`. The middleware requires JSON for
   writes; Pydantic validates.
2. Import: `GitHubClient.iter_issues` pages through GitHub → `normalize_item`
   → `ml/service.classify` (PR → rules; maintainer label; ML ≥ 0.5; else
   rules) → `priority.score_priority` → `repository.save_import` (one
   transaction).
3. Duplicates: per-repository TF-IDF, 0.6 title + 0.4 description cosine,
   threshold 0.20, human decisions preserved.
4. Sync: `since=` the last known-current time, keyset pagination, one
   transaction.
5. Testing: a fake GitHub through `httpx.MockTransport`, temporary databases,
   CI on 3.11–3.13.
6. Limits: no authentication, SQLite single writer, weak text-only priority.

**Short explanations of the main parts:**

- **The ML model:** "Maintainer-labelled issues → TF-IDF word weights →
  logistic regression → probabilities. Below 50% confidence the keyword rules
  decide. It's saved without pickle and with a checksum."
- **Duplicate detection:** "Cosine similarity between TF-IDF vectors, titles
  weighted 0.6. The threshold was chosen on development repositories with a
  false-alarm cap. It's a hint; the maintainer decides."
- **Priority:** "A transparent points system. 'High' needs a score of 3 or
  more and two different kinds of evidence. Maintainer labels always win.
  It's cautious by design."
- **Migrations and transactions:** "`PRAGMA user_version` plus a numbered
  list. Each migration runs under `BEGIN IMMEDIATE` with the version
  re-checked, so parallel workers are safe. Imports and syncs fetch first,
  then write everything in one transaction."
- **Testing and CI:** "543 pytest tests: unit, API and regression. GitHub is
  faked and every test gets a fresh database. A GitHub Actions matrix runs
  ruff and pytest on 3.11–3.13."
- **Limitations and next steps:**
  - authentication and rate limiting;
  - persistent storage or PostgreSQL;
  - better duplicate recall, e.g. local embeddings, tested the same way;
  - priority stays weak from text alone;
  - fix the known sync 500 edge case.

### 32.2 Forty-five questions with answers (beginner → intermediate)

**Basics**

1. **What does TriageBot do?** It imports a public repository's issues and
   PRs and suggests a category, a priority, possible duplicates and PR
   insights, each with reasons. It's read-only towards GitHub.
2. **Why read-only?** Safety and trust: suggestions need human review. The
   client in `app/github_client.py` has only GET methods.
3. **Why FastAPI?** Type-hint-based validation (Pydantic), automatic `/docs`,
   dependency injection (used to swap in a fake GitHub in tests), and good
   performance.
4. **Why SQLite?** No server to run, a file per test, and enough for one user.
   The trade-off: one writer at a time, one machine.
5. **What is Uvicorn?** The ASGI server that receives HTTP requests and passes
   them to the FastAPI app.
6. **How does the frontend talk to the backend?** `fetch()` calls from
   `static/index.html` to relative URLs such as `/items`, with JSON bodies via
   `postJson`.
7. **What are the four categories?** bug, feature, documentation, question.
8. **What does `/health` return?** `{"status": "ok", "version": "1.0.0"}`.
9. **Where are settings defined?** Environment variables read by
   `app/config.py`, `app/database.py` and `app/pr_insights.py`; documented in
   `.env.example`.
10. **What happens without a GitHub token?** It works, with a limit of 60
    GitHub requests per hour. A rate limit returns 429 with the reset time.

**GitHub integration and the data flow**

11. **How does the app talk to GitHub?** `GitHubClient` wraps `httpx.Client`
    with GitHub's API URL, a timeout, and an optional Bearer token.
12. **How does pagination work?** It requests up to 100 per page and follows
    the `Link: rel="next"` header until the limit.
13. **How are PRs told apart from issues?** The issues API includes a
    `pull_request` key for PRs.
14. **How do you prevent incomplete imports?** Fetch everything first, then
    write the repository row, items and PR merge dates in one transaction
    (`repository.save_import`). A GitHub failure means nothing is written; a
    database failure rolls back.
15. **How do re-imports avoid duplicate rows?** `UNIQUE (repo_id, number)`
    with `INSERT … ON CONFLICT DO UPDATE`.
16. **How are issues closed on GitHub updated locally?** Sync:
    `since=synced_through − 5 min`, both states, oldest first, keyset
    pagination, one transaction.
17. **Why keyset pagination?** With page numbers, an item edited mid-sync
    moves to the end and pushes another item onto a page already read, so it
    could be skipped. Restarting from the last `updated_at` can't skip it.
18. **What if GitHub rate-limits during a sync?** `RateLimitError` → 429.
    Nothing is written and the sync point is unchanged.
19. **How are GitHub errors shown to users?** `_github_http_error` maps them:
    not found → 404, rate limit → 429 with `Retry-After`, auth or network
    → 502.

**Database**

20. **Why migrations?** The schema changes over time without losing users'
    data. Each numbered migration runs once.
21. **Where is the schema version stored?** `PRAGMA user_version`.
22. **How are concurrent start-ups handled?** `BEGIN IMMEDIATE` per migration
    and a version re-check inside the lock. The old code crashed with
    "table already exists" when 4 workers started together.
23. **What is WAL?** Write-ahead logging: readers continue while one
    connection writes. Enabled in `_enable_wal`, with retries.
24. **How is SQL injection prevented?** Parameterised queries plus a
    whitelist of filter columns.

**ML and evaluation**

25. **How does the classifier work?** `build_text` → TF-IDF (1–2-word
    phrases) → multinomial logistic regression → softmax probabilities;
    explanations from the top term contributions.
26. **What happens when the model is unavailable?** `load_model` returns a
    reason, the rules classify everything, `/model` shows `available: false`,
    and the reasons say why.
27. **What decides whose category wins?** PR → rules; no model → rules; one
    maintainer label → that label; ML ≥ 0.5 → ML; else rules.
28. **Why no pickle?** Loading a pickle can execute code. The model is plain
    arrays (`allow_pickle=False`) plus a SHA-256 check.
29. **How do you evaluate the model?**
    - A held-out test split (macro-F1 0.766, rules 0.527);
    - leave-one-repository-out (0.658);
    - an external set of 8 new repositories (0.715).
30. **Why macro-F1?** Every class counts equally, so the weak *question*
    class can't hide behind the others.
31. **How did you avoid leakage?** Duplicates were removed before splitting;
    C was tuned with cross-validation on training data only; the test set was
    scored once.
32. **What is the weakest class and why?** *question* (F1 0.645). People ask
    questions while describing malfunctions, so it's confused with *bug*.
33. **Did you ever reject an "improvement"?** Yes. Template-text removal
    improved leave-one-repository-out by 0.019 but was worse on the fresh
    external set (−0.014), so it wasn't shipped.

**Priority, duplicates and PRs**

34. **How does priority scoring work?** Points from title words (severe /
    impact / scope / minor), severity-topic labels, category, reactions
    ≥ 10, and drafts. "High" needs ≥ 3 points and 2 kinds of evidence
    including severity. Maintainer labels win.
35. **Why aren't comments scored?** Long discussions aren't evidence of
    impact.
36. **How good is priority?** On 1,191 held-out issues: accuracy 0.367 and
    macro-F1 0.337, against a trivial 0.334 / 0.167. That's weak. An older
    scorer did better on averages but made many more false "high"s, and the
    choice was documented rather than tuned on the test set.
37. **How does duplicate detection work?** Per-repository TF-IDF;
    0.6 × title + 0.4 × description cosine; ≥ 0.20; up to 5 per issue; shared
    terms as the explanation.
38. **How are human decisions kept?** Re-analysis only replaces *pending*
    pairs; confirmed and rejected pairs never change.
39. **How good is duplicate detection?** On 40 held-out pairs: precision
    0.792 (a lower bound), recall 0.400. Without pandas, recall is 3 of 19.
40. **How is PR size decided?** By lines changed: XS < 10, S < 100, M < 500,
    L < 1000, XL ≥ 1000. "Large" is ≥ 500 lines or ≥ 20 files.
41. **What is a stale PR?** Open and not updated for ≥ 30 days (configurable).

**Testing, debugging, security, future**

42. **How do you test without calling GitHub?** `FakeGitHub` through
    `httpx.MockTransport`, injected with `app.dependency_overrides`. A
    `tmp_path` database is used per test.
43. **How would you debug a failing API?** Reproduce it, read the status code
    and `detail` (4xx) or the server traceback (500), find the layer, locate
    the code, form and test a hypothesis, then fix it and add a regression
    test (Chapter 20).
44. **What did the JSON-only rule fix, and what doesn't it fix?** It stops
    browsers sending simple cross-site write requests (CSRF via no-content-type
    bodies). It is not authentication.
45. **What would you do before public deployment?** Authentication, rate
    limiting, HTTPS, persistent storage or PostgreSQL, the token as a secret,
    green CI, a backup plan and monitoring.

---

## Chapter 33: Hands-on learning exercises

Do these yourself. Use **`TRIAGEBOT_DB=/tmp/triagebot-play.db`** for
anything that writes data.

### Level 1: Run the project

- **Objective:** install everything and see the dashboard.
- **Prerequisites:** Chapter 4.
- **Steps:**
  1. Create and activate `.venv`.
  2. `pip install -r requirements-dev.txt`.
  3. `python -m pytest`.
  4. `TRIAGEBOT_DB=/tmp/triagebot-play.db uvicorn app.main:app --reload`.
  5. Open http://127.0.0.1:8000.
- **Expected:** `543 passed`; three tabs; a model status line.
- **Hints:** `command not found` means the venv isn't active.
- **Check:** `curl -s http://127.0.0.1:8000/health` returns
  `{"status":"ok",...}`.
- **Common mistakes:** running commands outside the project folder;
  forgetting `source .venv/bin/activate`.

### Level 2: API requests

- **Objective:** use the API without the UI.
- **Steps:**
  1. `curl -s http://127.0.0.1:8000/model`.
  2. `POST /triage` (Chapter 10) with your own title.
  3. Send the same request without `-H "Content-Type: application/json"`.
  4. Send an empty title `""`.
- **Expected:** 200 → JSON; no header → **415**; empty title → **422**.
- **Check:** compare with the Chapter 10 tables.
- **Common mistakes:** zsh needs single quotes around JSON.

### Level 3: Read database records

- **Objective:** see how an item is stored.
- **Steps:**
  1. Import a small repository (Max items 20).
  2. Back up: `sqlite3 /tmp/triagebot-play.db ".backup '/tmp/play-copy.db'"`.
  3. Run `sqlite3 /tmp/play-copy.db "SELECT number, kind, state, category, category_source, priority FROM items LIMIT 10;"`.
- **Expected:** rows matching the dashboard.
- **Hints:** `.schema items` shows every column.
- **Check:** pick one row and find the same item in the UI.

### Level 4: Follow an issue through the import pipeline

- **Objective:** trace the code.
- **Steps:** open `app/importer.py:import_repo` and follow each call:
  1. `client.get_repo`;
  2. `iter_issues`;
  3. `normalize_item`;
  4. `classify`;
  5. `repository.save_import`;
  6. `sync_pr_list`.

  Write one sentence per call.
- **Expected:** your notes match Chapter 8.
- **Hints:** in VS Code, `Cmd+click` jumps to a definition.

### Level 5: Understand a classification

- **Objective:** explain one prediction.
- **Steps:**
  1. Find an item with the **ML n%** chip and one with **rules**.
  2. Read their reasons.
  3. For the rules item, recompute the scores by hand using Chapter 14.
- **Expected:** your sums match the "scores: …" line.
- **Common mistakes:** forgetting the body cap of +2.

### Level 6: Reproduce an evaluation (no network needed)

- **Objective:** run evaluation code.
- **Steps:**
  1. `python -m pytest tests/test_evaluate_priority.py tests/test_evaluate_duplicates.py -v`.
  2. Read `test_wilson_interval_matches_reference_values` and work out
     what it checks.
  3. Optional, needs network and time: Chapter 16.3.
- **Expected:** all pass; you can explain Wilson intervals in one sentence.

### Level 7: Diagnose a failing test (safely)

- **Objective:** read a failure.
- **Steps:**
  1. In `tests/test_pr_insights.py`, temporarily change `(10, "S")` to
     `(10, "XS")` in `test_size_label_boundaries`.
  2. Run `python -m pytest tests/test_pr_insights.py -x`.
  3. Read the output, then **undo your change** with
     `git restore tests/test_pr_insights.py`.
     ⚠ That discards edits to that file only. Make sure you haven't made
     other changes there.
- **Expected:** one failure showing `assert 'S' == 'XS'`.
- **Check:** after undoing, the file passes again.

### Level 8: Reproduce a real bug

- **Objective:** reproduce the known `/repos/sync` 500 (Chapter 20).
- **Steps:**
  1. Write a new test in `tests/test_sync.py` using the `client`,
     `fake_github` and `db_path` fixtures.
  2. Add an empty repository: `fake_github.add_repo("acme/empty", [])`.
  3. Import it.
  4. Set `synced_through` to NULL:
     `sqlite3.connect(db_path).execute("UPDATE repos SET synced_through = NULL")`,
     followed by a commit.
  5. Call `client.post("/repos/sync", json={"repo": "acme/empty"})`.
- **Expected (before a fix):** the test errors with
  `ValueError: Nothing to synchronise yet…`. The `client` fixture re-raises
  server exceptions, which is how a 500 looks inside a test.
- **Hints:** look at `test_repositories_imported_before_sync_tracking_use_their_oldest_refresh`
  for the pattern.

### Level 9: Fix it with a regression test

- **Objective:** your first real fix.
- **Steps:**
  1. In `app/main.py:sync_repo`, catch `ValueError` and raise
     `HTTPException(status_code=409, detail=str(exc))`.
  2. Change your test to assert `resp.status_code == 409`.
  3. Run `pytest tests/test_sync.py`, then `pytest`, then `ruff check .`.
- **Expected:** all green.
- **Check:** `git diff` shows only those two small changes.
- **Common mistakes:** catching `Exception` (far too broad), which hides
  real bugs.

### Level 10: Review a diff and explain a commit

- **Objective:** commit-reviewer skills.
- **Steps:**
  1. `git diff --stat`.
  2. `git diff app/database.py`.
  3. Write the 3 most important changes in your own words.
  4. Write a commit message for your Level 9 fix.
- **Check:** compare with Chapters 13 and 26.

### Level 11: Inspect CI

- **Objective:** see CI run and read it.
- **Prerequisites:** Chapter 27 done (pushed to **your** repository; a pull
  request opened into `main`).
- **Steps:**
  1. Open **Actions**.
  2. Open each job, and expand the `ruff` and `pytest` steps.
  3. Find the smoke job's printed line `app title: TriageBot API`.
- **Expected:** 3 green test jobs plus the smoke job.
- **If red:** follow Chapter 29.2.

### Level 12: A security and deployment review

- **Objective:** think like a reviewer.
- **Steps:** for each row in Chapter 30.1 marked ❌, write:
  1. the risk, in one sentence;
  2. what a minimal fix would be;
  3. whether it blocks a *private* demo or only a *public* one.
- **Check:** compare with Chapter 31.3.

---

## Chapter 34: A personal study roadmap

About 1–2 hours per day. Repeat a day if the checkpoint feels shaky; there's
no deadline.

| Day | Read | Open | Do | Explain aloud | Checkpoint |
|---|---|---|---|---|---|
| 1 | Index, Chapter 1 | README (Features) | Look at the screenshots in `docs/screenshots/` | "What problem does TriageBot solve?" | Checkpoint 1 |
| 2 | Chapters 2–3 | The folder tree | Draw the architecture from memory | "Which file does what?" | Checkpoint 2 |
| 3 | Chapter 4 | `requirements*.txt` | **Exercise 1** | "What is a venv and why?" | 543 passed |
| 4 | Chapters 5–6 | `app/config.py`, `.env.example` | Start with a temporary DB; try `TRIAGEBOT_ML_ENABLED=0` | "How do settings reach the code?" | Checkpoints 5–6 |
| 5 | Chapter 7 | `static/index.html` (`api`, `postJson`) | Use every dashboard feature on a small repository | "What happens when I click Import?" | Checkpoint 7 |
| 6 | Chapter 8 | `app/importer.py`, `app/github_client.py` | **Exercise 4** | The import sequence | Checkpoint 8 |
| 7 | Chapters 9–10 | `app/main.py` | **Exercise 2**; try every row of the error table | "Import vs sync" | Checkpoints 9–10 |
| 8 | Chapters 11–13 | `app/database.py`, `app/repository.py` | **Exercise 3**; back up and inspect | "How does a migration run?" | Checkpoint 13 |
| 9 | **Revision** | — | Re-explain Days 1–8 without notes; fix your weak spots | — | — |
| 10 | Chapters 14–15 | `app/triage.py`, `app/ml/service.py`, `app/ml/model.py` | **Exercise 5** | "Who decides the category?" | Checkpoints 14–15 |
| 11 | Chapter 16 | `models/EVALUATION.md`, `scripts/evaluate_category_generalization.py` | **Exercise 6** | "Why does unseen-repository performance matter?" | Checkpoint 16 |
| 12 | Chapter 17 | `app/priority.py` | Score 3 titles by hand, then check with `/triage` | "Why is 'App crashes' medium?" | Checkpoint 17 |
| 13 | Chapters 18–19 | `app/duplicates.py`, `app/pr_insights.py` | The cosine exercise; open a duplicate panel | "Similarity isn't proof" | Checkpoints 18–19 |
| 14 | Chapters 20–21 | — | **Exercise 7** | Your debugging workflow | Checkpoint 20 |
| 15 | Chapters 22–24 | `tests/conftest.py`, `tests/github_fakes.py` | The end-to-end plan (Chapter 24) | "How are tests isolated?" | Checkpoints 22–24 |
| 16 | **Revision** | — | Re-do the end-to-end plan faster | — | — |
| 17 | Chapter 20 again | `app/main.py:sync_repo` | **Exercises 8–9** (fix the real bug) | Hypothesis → test → fix | All green |
| 18 | Chapters 25–27 | `git diff` | **Exercise 10**; plan your commit (don't push yet if unsure) | "Staging vs committing" | Checkpoints 25–27 |
| 19 | Chapters 28–29 | `.github/workflows/ci.yml` | Push to *your* repository; open a pull request; **Exercise 11** | "What does CI run?" | Green CI |
| 20 | Chapters 30–31 | `app/main.py` middleware | **Exercise 12** | "Why not public yet?" | Checkpoints 30–31 |
| 21 | Chapter 32 | — | Practise the 30 s / 1 min / 3 min pitches; answer 15 random questions | The whole project | You can answer without notes |

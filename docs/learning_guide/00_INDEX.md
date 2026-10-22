# TriageBot Learning Guide: Index

This is a complete beginner-to-advanced manual for **this** repository: how
TriageBot works, how to run it, test it, debug it, review it, commit it, and
get it ready for CI and deployment.

- **Written:** 2026-10-03, from the code as it is in the working copy. Phases
  1–6 are included; Phases 3–6 are not yet committed.
- **Audience:** a total beginner (an ECE student on a MacBook Air M1 using VS
  Code, Terminal, Python and Git).
- **Rule used throughout:** every command, file path, function name, endpoint
  and setting below was checked against the repository. Where something could
  not be checked, it says so.

---

## How to read the labels

| Label                     | Meaning                                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------------- |
| **[VERIFIED 2026-10-03]** | Run or checked while this guide was written                                                                   |
| **[REPORTED]**            | A result recorded earlier in the project's evaluation or verification documents, not re-run here |
| **[RECOMMENDED]**         | Advice for you; not part of the current code                                                                  |
| **[NOT VERIFIED]**        | Could not be checked; treat with care                                                                         |
| **[CHANGES STATE]**       | A command that changes files, the database or Git. Read the warning first                                     |

When you see `$` at the start of a line, it means "type this in Terminal".
Don't type the `$` itself.

---

## Files and chapters

| File                                                                           | Part                                         | Chapters                                                                                         |
| ------------------------------------------------------------------------------ | -------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| [01_understand_the_project.md](01_understand_the_project.md)                   | Part 1: Understand the project from zero     | 1. What is TriageBot? · 2. Project architecture · 3. Technologies used                           |
| [02_setup_and_configuration.md](02_setup_and_configuration.md)                 | Part 2: Prepare your development environment | 4. Setting up on your MacBook · 5. Understanding the configuration                               |
| [03_running_the_app.md](03_running_the_app.md)                                 | Part 3: Run the application yourself         | 6. Starting the backend · 7. Running and using the frontend                                      |
| [04_data_flow_and_api.md](04_data_flow_and_api.md)                             | Part 4: The complete data flow               | 8. Importing a repository · 9. How sync works · 10. API requests and responses                   |
| [05_database.md](05_database.md)                                               | Part 5: The database                         | 11. SQLite fundamentals · 12. Database design · 13. Initialisation and migrations                |
| [06_classification_and_evaluation.md](06_classification_and_evaluation.md)     | Part 6a: Intelligence features               | 14. Rule-based classification · 15. Machine-learning classification · 16. Model evaluation       |
| [07_priority_duplicates_pr_insights.md](07_priority_duplicates_pr_insights.md) | Part 6b: Intelligence features               | 17. Priority scoring · 18. Duplicate detection · 19. PR insights                                 |
| [08_debugging.md](08_debugging.md)                                             | Part 7: Debugging                            | 20. Debugging from zero · 21. Common errors and solutions                                        |
| [09_testing.md](09_testing.md)                                                 | Part 8: Testing                              | 22. Testing fundamentals · 23. Running the test suite · 24. A real end-to-end test               |
| [10_git_and_github.md](10_git_and_github.md)                                   | Part 9: Git and GitHub                       | 25. Git fundamentals · 26. Reviewing your current changes · 27. Your first safe commit and push  |
| [11_ci_cd.md](11_ci_cd.md)                                                     | Part 10: CI/CD                               | 28. CI/CD from the beginning · 29. Making sure CI works                                          |
| [12_security_and_deployment.md](12_security_and_deployment.md)                 | Part 11: Security and deployment             | 30. Security fundamentals · 31. Deployment fundamentals                                          |
| [13_interviews_exercises_roadmap.md](13_interviews_exercises_roadmap.md)       | Part 12: Become confident                    | 32. Explaining the project in interviews · 33. Hands-on exercises · 34. A personal study roadmap |
| [14_glossary.md](14_glossary.md)                                               | Reference                                    | 35. Glossary                                                                                     |

Each chapter ends with a **Checkpoint**: a few questions you should be able to
answer before moving on.

---

## The project in one picture

```
You (browser) ──► static/index.html (dashboard: HTML + CSS + JavaScript)
                      │  fetch() calls with JSON
                      ▼
               app/main.py  (FastAPI: routes, validation, error handling)
                      │
     ┌────────────────┼──────────────────────────────┬────────────────────┐
     ▼                ▼                              ▼                    ▼
app/importer.py   app/duplicates.py            app/pr_insights.py   app/ml/service.py
(import & sync)   (similar issues)             (PR status, size)    (category: ML or rules)
     │                │                              ▲                    │
     ▼                ▼                              │                    ▼
app/github_client.py  app/repository.py ─────────────┘           app/triage.py + app/priority.py
(GET-only calls to    (all SQL)                                   (rules, priority score)
 api.github.com)          │
                          ▼
                    app/database.py ──► SQLite file (triagebot.db)
```

Chapter 2 explains every box.

---

## Where to start

1. Read Chapter 1 (no code needed).
2. Follow Chapter 4 to install everything.
3. Follow Chapter 6 to start the server, and Chapter 7 to use the dashboard.
4. Do **Exercise Level 1** in Chapter 33.
5. Follow the day-by-day plan in Chapter 34.

## What was verified while writing this guide [VERIFIED 2026-10-03]

| Check                                                                    | Result                                                                                                                                            |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python -m pytest`, Python 3.14.5                                        | 543 passed                                                                                                                                        |
| `python -m pytest`, Python 3.11.14                                       | 543 passed                                                                                                                                        |
| `ruff check .`                                                           | All checks passed                                                                                                                                 |
| Test command variants (one file, one test, `-k`, `-x`, `-v`)             | Ran and behaved as described in Chapter 23                                                                                                        |
| Every `scripts/*` command shown                                          | `--help` works and lists the options described                                                                                                    |
| The real server, on a **copy** of a test database                        | Started; the example responses in Chapter 10 are real output (`/health`, `/model`, `/triage`, `/items`, `/repos`, and the 404 / 415 / 422 errors) |
| `git status` / `git log` / `git remote -v`                               | Read only; nothing was changed                                                                                                                    |
| CI simulation: a scratch copy containing only the files Git would commit | 543 passed + ruff clean + the smoke import, on Python **3.11.14** and **3.13.13**                                                                 |

**Not verified while writing this guide:** a CI run on GitHub (nothing has
been pushed), any deployment, and Python 3.12 (not installed locally).

# Part 10: Understand CI/CD

[← Part 9](10_git_and_github.md) · [Index](00_INDEX.md) · Next: [Part 11: Security and deployment](12_security_and_deployment.md)

---

## Chapter 28: CI/CD from the beginning

| Term | Meaning |
|---|---|
| **Continuous integration (CI)** | Every time code is pushed or proposed, a machine automatically installs it, lints it and runs the tests. Problems show up within minutes, not weeks |
| **Continuous delivery** | Every change that passes CI is *ready* to release; a human presses the button |
| **Continuous deployment** | Every change that passes CI is released automatically |
| **Workflow** | A CI recipe. On GitHub it's a YAML file in `.github/workflows/` |
| **Runner** | A fresh virtual machine GitHub starts for each job (here `ubuntu-latest`) |
| **Job** | A group of steps that runs on one runner |
| **Step** | One command or action inside a job |
| **Matrix** | Run the same job several times with different settings (e.g. Python versions) |

**Why it matters:** it catches "works on my machine" problems.

**How CI differs from your Mac:**

| | Your Mac | CI runner |
|---|---|---|
| OS | macOS on ARM (M1) | Linux on x86-64 |
| Starting state | Your existing files | A fresh clone, so **git-ignored files don't exist** (no `.env`, no `data/raw/`, no databases) |
| Python versions | Whatever you installed | The ones in the matrix |
| Dependencies | May have leftovers | Installed fresh |

**Why the same code can fail in CI:**

- a file you forgot to commit;
- a test that depends on a local file;
- a different Python version;
- a time-zone or ordering assumption.

### 28.1 This repository's workflow (`.github/workflows/ci.yml`) [VERIFIED: file read]

```yaml
on:
  push:            { branches: [main] }
  pull_request:    { branches: [main] }
  workflow_dispatch:                       # "Run workflow" button
jobs:
  test:            # "Lint & Test"
    runs-on: ubuntu-latest
    strategy: { fail-fast: false, matrix: { python-version: ["3.11", "3.12", "3.13"] } }
    steps: checkout → setup-python (pip cache) → pip install -r requirements-dev.txt
           → ruff check . → pytest
  smoke:           # "App boots (import + health)"
    needs: test     # only runs if every test job passed
    Python 3.12 → pip install -r requirements.txt
           → python -c "from app.main import app; print('app title:', app.title)"
```

(Condensed from the real file.)

- `fail-fast: false` means one Python version failing doesn't cancel the
  others, so you see all results.
- Despite its name, the smoke job checks that the app **imports**; it
  doesn't call `/health`.

### Checkpoint 28

1. What is a runner?
2. Why don't git-ignored files exist in CI?
3. Which Python versions does this project's CI test?

---

## Chapter 29: Making sure CI works

### 29.1 Local checks that mirror CI [VERIFIED 2026-10-03]

To imitate a fresh clone, a scratch folder was filled with **only the files
Git would commit** (`git ls-files -co --exclude-standard`). In that copy:

| Check | Python 3.11.14 | Python 3.13.13 |
|---|---|---|
| `python -m pytest` | **543 passed** | **543 passed** |
| `ruff check .` | All checks passed | All checks passed |
| The smoke command | `app title: TriageBot API` | `app title: TriageBot API` |

So the tests don't depend on any git-ignored file.

**Not verified:** Python 3.12 (not installed locally), and an actual GitHub
Actions run (nothing has been pushed).

### 29.2 Running CI after you push

**Triggers:** a push to a feature branch alone **does not** run this
workflow. Either:

- open a **pull request into `main`** in your repository; or
- go to **Actions → CI → Run workflow** and choose your branch.

**Reading the result:**

- A ✅ / ❌ appears next to the commit and on the pull request.
- Click **Details** or the **Actions** tab, open the run, then open a job
  (e.g. "Lint & Test (3.12)"). Each step can be expanded; the **red** step
  is where it failed.

**Diagnosing a failure:**

1. Read the last lines of the red step.
   - For `ruff`: the file, line and rule code.
   - For `pytest`: the `FAILED tests/...::test_name` line and its assertion.
2. Reproduce it locally with the same command, ideally on the same Python
   version (`python3.11 -m venv` etc.).
3. If it passes locally but fails in CI, suspect:
   - an uncommitted file (`git status`);
   - a Python-version difference;
   - Linux vs macOS behaviour (paths, case-sensitive filenames).
4. Fix it, commit, and push again. CI re-runs automatically on the pull
   request; or click **Re-run jobs**.

### 29.3 Suggested improvement [RECOMMENDED, not implemented]

The workflow is complete enough for this project. Two optional additions,
only after you've discussed and approved them:

- Add `"3.14"` to the matrix, because you develop on 3.14 locally.
- Run CI on pushes to all branches (`on: push:` without the branch filter),
  so feature branches get checked without opening a pull request.

**Don't change the workflow until CI has run at least once as it is.**

### Checkpoint 29

1. Name two ways to get CI to run on your feature branch.
2. Where in GitHub do you find the step that failed?
3. A test passes on your Mac but fails in CI. Name two things to check.

# Part 9: Git and GitHub

[← Part 8](09_testing.md) · [Index](00_INDEX.md) · Next: [Part 10: CI/CD](11_ci_cd.md)

---

## Chapter 25: Git fundamentals

| Term | Meaning | In this repository |
|---|---|---|
| **Git** | A program that records snapshots of your project over time | The hidden `.git/` folder |
| **GitHub** | A website that hosts Git repositories online | Where `origin` points |
| **Repository** | A project folder plus its full history | `triagebot/` |
| **Commit** | One saved snapshot, with a message and an ID | `6dec35d Build GitHub triage MVP with ML classification` |
| **Working tree** | Your files as they are right now, including changes not yet committed | 20 modified + many new files |
| **Staging area (index)** | The "shopping basket" of changes you've chosen for the next commit (`git add`) | Currently **empty** |
| **Branch** | A named line of commits. You can make one to work on something separately | You're on **`main`** |
| **Remote** | A named link to a copy of the repository elsewhere | **`origin`** → `https://github.com/pramila-bishnoi/triagebot.git` |
| **`origin` / `upstream`** | Conventional names: `origin` = your publication repository; `upstream` = the project you copied from | Here `origin` is your GitHub repository; the original project is acknowledged in the README |
| **Push / pull** | Send commits to a remote / fetch them from one | Nothing pushed yet: `main` is "ahead 1" of `origin/main` |
| **Merge conflict** | Two changes edit the same lines and Git can't choose | Git writes `<<<<<<<`, `=======` and `>>>>>>>` markers into the file for you to resolve |
| **`.gitignore`** | A list of files Git should never track | `.env`, `*.db`, `.venv/`, raw datasets |

**Why review before committing:** a commit is permanent history, and pushing
makes it public. Reviewing catches secrets, accidental files and bugs
*before* they spread.

**Resolving a conflict, in short:**

1. Open the file.
2. Keep the right lines and delete the three marker lines.
3. `git add <file>`.
4. `git commit` (or `git merge --continue`).

### Checkpoint 25

1. What is the difference between the working tree and the staging area?
2. Whose repository does `origin` point to right now?
3. Name two things `.gitignore` protects you from committing.

---

## Chapter 26: Review and understand your current changes

**The state on 2026-10-03** [VERIFIED: read-only commands]:

| Command | Result |
|---|---|
| `git branch --show-current` | `main` |
| `git status -sb` | `## main...origin/main [ahead 1]`: one local commit (`6dec35d`) isn't on `origin` |
| `git remote -v` | `origin https://github.com/pramila-bishnoi/triagebot.git` (fetch and push) |
| `git diff --cached --stat` | empty: **nothing staged** |
| `git diff --stat` | 20 tracked files changed (+2,596 / −311) |
| `git ls-files --others --exclude-standard` | the project's 42 new files, plus the files of **this learning guide** |

**Already committed:**

- the starter project (7 commits by the original authors);
- `6dec35d` (Phases 1–2: GitHub import and the ML model).

**Not committed:** everything from Phases 3–6 and the final verification.

**The changes, grouped by feature:**

| Group | Files | Why it matters |
|---|---|---|
| Core API and app | `app/main.py`, `app/schemas.py`, `app/config.py` | New endpoints (sync, duplicates, PRs); the JSON-only middleware; config warnings |
| Data layer | `app/database.py`, `app/repository.py` | Migrations v4–v7, transactions, concurrency fix |
| GitHub and import | `app/github_client.py`, `app/importer.py` | Sync (`fetch_updated_since`), atomic import |
| Intelligence | `app/priority.py`, `app/matching.py`, `app/duplicates.py`, `app/pr_insights.py` (new); `app/triage.py`, `app/ml/service.py` (modified) | Priority, duplicates, PR insights |
| UI | `static/index.html` | Duplicates panel, PR tab, Sync button, legend |
| Tests | 12 new `tests/test_*.py` + 5 modified, `tests/github_fakes.py` | 543 tests |
| Evaluation tooling | `scripts/*` (6 new, 1 modified), `data/*_manifest.csv` | Reproducible evaluations (manifests contain IDs only) |
| Docs | `README.md`, `docs/*`, the 3 project documents, `docs/learning_guide/*` | Reports, screenshots, this guide |
| Config | `.env.example`, `.gitignore`, `pyproject.toml` | All 11 settings documented; ignores raw data and WAL files |

**Review these especially carefully:**

- `app/main.py`: the middleware and the new routes;
- `app/database.py`: `init_db`;
- `app/importer.py` and `app/repository.py`: `save_import`, `save_sync`;
- `static/index.html`;
- the known `/repos/sync` 500 edge case (Chapter 20).

**Read-only commands for your own review:**

```bash
$ git status
$ git diff --stat
$ git diff app/main.py            # one file at a time; press q to quit the pager
$ git ls-files --others --exclude-standard
$ git log --oneline -8
```

### Checkpoint 26

1. How many commits are on your `main` that aren't on `origin`?
2. Which group of files would you review first, and why?

---

## Chapter 27: Your first safe commit and push

⚠ **Every command in this chapter marked [CHANGES STATE] modifies your
repository or a remote. Run them yourself, only after you've reviewed the
changes.** None of them were run while writing this guide.

### 27.1 Confirm the publication remote

The current `origin` is your publication repository:

```text
https://github.com/pramila-bishnoi/triagebot.git
```

The original project is `https://github.com/sayanchatto/triagebot` and is
acknowledged in the README. Confirm the remote before any push:

```bash
$ git remote -v
```

### 27.2 Create a branch

```bash
$ git switch -c phase-3-6-review        # [CHANGES STATE: new branch, nothing else changes]
```

### 27.3 Final checks before staging

```bash
$ python -m pytest                     # expect 543 passed
$ ruff check .                         # expect All checks passed!
$ git diff --check                     # expect no output
$ git status                           # make sure no .env, *.db, .venv or raw data appears
```

### 27.4 Stage intentionally, by group

```bash
$ git add app/ tests/ static/index.html .env.example .gitignore pyproject.toml   # [CHANGES STATE: staging]
$ git add scripts/ data/category_external_manifest.csv data/duplicate_manifest.csv data/priority_manifest.csv
$ git add README.md docs/learning_guide/ docs/screenshots/
```

Avoid `git add .` until you've confirmed `git status` shows nothing
unexpected.

**Inspect what's staged:**

```bash
$ git diff --cached --stat
$ git diff --cached app/main.py
```

**To unstage a file:** `git restore --staged <file>`. Your edits are kept.

### 27.5 Commit and verify

```bash
$ git commit -m "Add priority, duplicates, PR insights, sync and evaluation (Phases 3-6)"   # [CHANGES STATE: new commit]
$ git log -1 --stat          # check the message and the file list
$ git status                 # should be clean (or only things you chose to leave out)
```

### 27.6 Push and verify

```bash
$ git push -u origin main               # [CHANGES STATE: uploads to YOUR remote]
```

Open your repository on GitHub: the branch and the commit should be there.

⚠ **CI doesn't start automatically for this branch.** `.github/workflows/ci.yml`
runs on pushes to **`main`**, on **pull requests into `main`**, and manually
(`workflow_dispatch`). To run it on your branch, do one of these:

- **Open a pull request:** on GitHub, open a pull request from
  `phase-3-6-review` into `main` in **your** repository. This is the usual
  way, and it lets you review the diff on GitHub.
- **Run it manually:** *Actions → CI → Run workflow →* choose your branch.

See Chapter 29.

### 27.7 Recovering from common mistakes

| Mistake | Before you pushed | After you pushed |
|---|---|---|
| Staged a wrong file | `git restore --staged <file>` | — |
| Typo in the last commit message | `git commit --amend` | Better to leave it, or make a new commit |
| Committed a file by mistake | `git reset --soft HEAD~1` (undoes the commit, keeps the changes staged) | `git rm --cached <file>`, add it to `.gitignore`, commit |
| **Committed a secret** | Remove it and amend/reset as above | **Revoke the token on GitHub immediately.** Removing it from history is not enough once pushed |
| Pushed to the wrong remote | — | Delete that branch on that remote, if you have permission; tell its owner if it was someone else's |

### Checkpoint 27

1. Why create your own GitHub repository first?
2. How do you see exactly what is staged?
3. What is the first thing to do if a token was pushed?

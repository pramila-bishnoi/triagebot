# Part 2: Prepare Your Development Environment

[← Part 1](01_understand_the_project.md) · [Index](00_INDEX.md) · Next: [Part 3: Running the app](03_running_the_app.md)

---

## Chapter 4: Setting up the project on your MacBook

### 4.1 Terminal basics (from zero)

- **Terminal** (Applications → Utilities → Terminal, or VS Code's
  *Terminal → New Terminal*) is a text window where you type commands.
- A **shell** is the program that reads your commands. On macOS it is `zsh`.
- A **directory** is a folder. The terminal is always "inside" one directory,
  called the **current working directory**.

| Command | What it does | Example |
|---|---|---|
| `pwd` | Print the current directory | `/Users/you/Desktop/triagebot` |
| `ls` | List files | `ls` or `ls -la` (with hidden files such as `.env.example`) |
| `cd <folder>` | Change directory | `cd ~/Desktop/triagebot` |
| `cd ..` | Go up one folder | |
| `cat <file>` | Print a file | `cat requirements.txt` |

`~` means your home folder. Paths with spaces need quotes:
`cd "~/My Projects/triagebot"`.

### 4.2 Check your tools

```bash
$ cd ~/Desktop/triagebot          # adjust if your copy is elsewhere
$ python3 --version
$ git --version
$ uname -m
```

**What was seen on the development Mac [VERIFIED 2026-10-03]:**

| Command | Output |
|---|---|
| `python3 --version` | `Python 3.14.5`, at `/opt/homebrew/bin/python3` |
| Other versions also installed | `python3.11` and `python3.13` (Homebrew) |
| `git --version` | `git version 2.39.5 (Apple Git-154)` |
| `uname -m` | `arm64` (Apple Silicon) |

**Which Python?**

- `pyproject.toml` requires **3.11 or newer**.
- The test suite was run on **3.11.14** and **3.14.5**.
- CI tests **3.11, 3.12 and 3.13**.
- Any of 3.11–3.14 is fine. **3.11** matches the oldest CI version, which
  helps you catch problems that CI would catch.

If a version is missing:

```bash
$ brew install python@3.11        # requires Homebrew (https://brew.sh)
```

**Apple Silicon (M1) notes:**

- All of the project's dependencies (FastAPI, Uvicorn, httpx, scikit-learn,
  NumPy, SciPy) publish ready-made **arm64 "wheels"** (pre-compiled
  packages), so `pip install` needs no compiler. They installed this way in
  this project's virtual environments.
- If pip ever tries to *build* NumPy or SciPy from source, you are probably
  using an x86 Python under Rosetta. Check with
  `python3 -c "import platform; print(platform.machine())"`. It should print
  `arm64`.

### 4.3 Virtual environments: why and how

A **virtual environment** ("venv") is a private folder containing its own
Python and installed packages. Each project gets its own, so installing
`fastapi==0.115.6` for TriageBot doesn't break another project that needs a
different version. Think of it as a separate toolbox for each project.

`.gitignore` already ignores the names `.venv/`, `venv/` and `env/`, so a venv
inside the project folder is never committed.

```bash
$ cd ~/Desktop/triagebot
$ python3.11 -m venv .venv          # [CHANGES STATE] creates the .venv/ folder
$ source .venv/bin/activate         # turn it on for this terminal window
(.venv) $ python --version          # should now say Python 3.11.x
(.venv) $ which python              # should end in triagebot/.venv/bin/python
```

- When a venv is **active**, your prompt starts with `(.venv)`. Activation
  lasts only for that terminal window. Run `deactivate` to turn it off.
- **Always activate before working.** Most "module not found" errors are
  simply a missing `source .venv/bin/activate`.

### 4.4 Install the dependencies

```bash
(.venv) $ python -m pip install --upgrade pip
(.venv) $ pip install -r requirements-dev.txt     # [CHANGES STATE] installs into .venv only
```

`requirements-dev.txt` contains `-r requirements.txt` (the app's packages)
plus `pytest` and `ruff`. To run the app *without* the test tools, install
`requirements.txt` alone.

**Verify the installation:**

```bash
(.venv) $ python -c "import fastapi, sklearn, httpx; print(fastapi.__version__, sklearn.__version__, httpx.__version__)"
0.115.6 1.9.1 0.28.1
(.venv) $ pytest --version
(.venv) $ ruff --version
```

### 4.5 Open in VS Code

1. In VS Code: *File → Open Folder…* → choose the `triagebot` folder.
2. Install the **Python** extension (by Microsoft) if VS Code asks.
3. Press `Cmd+Shift+P`, choose **Python: Select Interpreter**, and pick the
   one in `.venv`. The integrated terminal then activates it automatically.

### 4.6 Settings and the GitHub token

The app works **without any configuration**. Every setting has a default
(Chapter 5). The only setting most people change is `GITHUB_TOKEN`:

- **Without a token:** GitHub allows **60 API requests per hour** from your IP
  address. Importing 100 items costs about 3 requests (Chapter 8), so that's
  enough to explore.
- **With a token:** 5,000 requests per hour.

**Creating a token safely:**

1. On GitHub: *Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token*. Give it **no permissions** at
   all; read access to public repositories doesn't need any. That's what
   `.env.example` recommends.
2. Copy the template and edit the copy in VS Code. Don't type the token into
   a terminal command, because commands are saved in your shell history.

   ```bash
   $ cp .env.example .env          # [CHANGES STATE] creates .env (git-ignored)
   ```

   In `.env`, set `GITHUB_TOKEN=` followed by your token, with no spaces or
   quotes.
3. Check that Git ignores it:

   ```bash
   $ git check-ignore .env         # prints ".env" → ignored, good
   ```

4. Never paste the token into screenshots, issues, logs or chat. If it ever
   leaks, delete it on GitHub immediately and create a new one.

The token is read only in `app/config.py:get_settings`
(`os.environ.get("GITHUB_TOKEN", "")`). It is sent only as an
`Authorization` header by `GitHubClient.__init__`, and a test
(`tests/test_config.py::test_token_is_never_logged`) checks that it is never
logged.

### 4.7 Confirm everything works

```bash
(.venv) $ python -m pytest           # should end with: 543 passed
(.venv) $ ruff check .               # should print: All checks passed!
```

[VERIFIED 2026-10-03] Both results above were seen on Python 3.11.14 and
3.14.5. On 3.14 you will also see a few hundred `DeprecationWarning`s from
FastAPI and Starlette (library code, not project code); they don't fail tests.

### Checkpoint 4

1. What does `(.venv)` at the start of your prompt mean?
2. Why should the token go in `.env` and not into a command?
3. What does `git check-ignore .env` tell you?

---

## Chapter 5: Understanding the project configuration

### 5.1 How settings reach the code

**Environment variables** are named values that the operating system gives a
program when it starts, like labelled notes stuck to the program. The code
reads them in two places:

- `app/config.py:get_settings()`: the GitHub settings, the ML settings and
  the duplicate threshold.
- `app/database.py:_db_path()` reads `TRIAGEBOT_DB`;
  `app/pr_insights.py:thresholds_from_env()` reads the three `TRIAGEBOT_PR_*`
  settings.

Settings are read **at call time**, not once at start-up. That's why tests can
change them with `monkeypatch.setenv`.

**Two ways to set them:**

```bash
$ export TRIAGEBOT_DB=/tmp/triagebot-test.db     # for this terminal window only
$ uvicorn app.main:app --reload --env-file .env  # Uvicorn loads .env for you
```

### 5.2 Configuration reference (all 11 settings, verified against the code)

| Variable | Default | What it controls | If invalid |
|---|---|---|---|
| `GITHUB_TOKEN` | *(unset)* | Optional token; raises the GitHub limit to 5,000 requests/hour | Whitespace is stripped. A wrong token → GitHub 401 → TriageBot **502** "GitHub rejected the configured GITHUB_TOKEN." |
| `GITHUB_API_URL` | `https://api.github.com` | API base URL | — |
| `GITHUB_TIMEOUT` | `10` (seconds) | Request timeout | Not a number, or ≤ 0 → 10, with a logged warning |
| `TRIAGEBOT_DB` | `./triagebot.db` (project root) | SQLite file path | The file is created if missing |
| `TRIAGEBOT_ML_ENABLED` | `1` | `0`, `false`, `no` or `off` disables the ML model (rules only) | — |
| `TRIAGEBOT_MODEL_PATH` | `models/issue_classifier` | Base path of the `.npz` + `.json` files | Missing or corrupt → the rules are used, and `/model` shows why |
| `TRIAGEBOT_ML_THRESHOLD` | `0.5` | Minimum ML probability for the ML category to be used | Not a number → 0.5; outside 0–1 → clamped; one warning |
| `TRIAGEBOT_DUP_THRESHOLD` | `0.2` (`app/duplicates.py:DEFAULT_THRESHOLD`) | Minimum similarity for a duplicate suggestion | Clamped to 0.05–1; not a number or negative → default; warning |
| `TRIAGEBOT_PR_STALE_DAYS` | `30` | An open PR idle this long is "stale" | Not a positive whole number → default, with a warning |
| `TRIAGEBOT_PR_LARGE_LINES` | `500` | Lines changed for "large" | Same |
| `TRIAGEBOT_PR_LARGE_FILES` | `20` | Changed files for "large" | Same |

**None are required.** The warning comes from
`app/config.py:warn_invalid`, which logs `ignoring invalid NAME='value'; using
…` once per distinct value.

### 5.3 The configuration files

**`pyproject.toml`**

- `[project]`: name `triagebot`, version `1.0.0`, `requires-python = ">=3.11"`.
- `[tool.pytest.ini_options]`: tests live in `tests/`; `-q` (quiet output) is
  always added.
- `[tool.ruff]`: line length 100, target Python 3.11; rules `E, W, F, I, UP,
  B`; `E501` (long lines) is ignored.
- It does **not** list dependencies; those are in `requirements*.txt`.

**`requirements.txt`** (pinned versions)

| Package | Version |
|---|---|
| `fastapi` | 0.115.6 |
| `uvicorn[standard]` | 0.34.0 |
| `httpx` | 0.28.1 |
| `scikit-learn` | 1.9.1 |

NumPy and SciPy are installed automatically as scikit-learn dependencies.

**`.gitignore`** keeps these out of Git:

- Python caches;
- virtual environments;
- **`*.db`, `*.db-wal`, `*.db-shm`** (databases);
- **`.env`** (secrets);
- editor folders;
- **raw datasets**: `data/raw/`, `data/priority_raw/`,
  `data/duplicates_raw/`, `data/category_external_raw/` and `data/*.jsonl`
  files.

**`.env.example`** is a commented template of all 11 settings. Copy it to
`.env`; never put real secrets in `.env.example`.

**Database files**

- The default database is `triagebot.db` in the project root.
- In **WAL mode** (Chapter 13), SQLite also creates `triagebot.db-wal` and
  `triagebot.db-shm` next to it. Leave them alone while the server runs.
- Keep the database on a local disk, not a network drive.

**Model files** (`models/`)

- `issue_classifier.npz`: the arrays (vocabulary, weights).
- `issue_classifier.json`: the settings, the evaluation metrics and the
  **SHA-256 checksum** of the `.npz` file.
- `EVALUATION.md`: the training report.

**Dataset configuration**

- No setting is needed to run the app.
- The evaluation scripts read `data/*.jsonl` files. Those are git-ignored
  and created by `scripts/fetch_*.py`.

**`render.yaml`** (deployment blueprint, from the starter project)

- Defines a free Render web service: build with
  `pip install -r requirements.txt`, start with
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, health check at
  `/health`.
- It also sets `PYTHON_VERSION 3.12.8` and `GITHUB_TOKEN` (`sync: false`,
  meaning "set it in the dashboard").
- It has `autoDeploy: true` and `branch: main`. **[NOT VERIFIED]** This
  blueprint has never been tested with the current code (Chapter 31).

### 5.4 Safe local configuration

- **For experiments**, use a throw-away database so your real data is safe:

  ```bash
  $ TRIAGEBOT_DB=/tmp/triagebot-play.db uvicorn app.main:app --reload
  ```

- **Rules only:** run `TRIAGEBOT_ML_ENABLED=0 uvicorn app.main:app --reload`.
  It's useful for comparing categories with and without ML.
- **Never commit:**
  - `.env`;
  - any `*.db` file;
  - raw data folders;
  - `.venv/`;
  - tokens anywhere.

### Checkpoint 5

1. What happens if you set `TRIAGEBOT_ML_THRESHOLD=abc`?
2. Which file sets the database location, and what is the default?
3. Which three files should never be committed?

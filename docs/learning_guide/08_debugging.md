# Part 7: Learn to Debug the Project

[← Part 6b](07_priority_duplicates_pr_insights.md) · [Index](00_INDEX.md) · Next: [Part 8: Testing](09_testing.md)

---

## Chapter 20: Debugging from zero

### 20.1 Words

| Term | Meaning |
|---|---|
| **Bug** | The program does something different from what it should |
| **Traceback** | Python's report of an error: the chain of function calls, **most recent call last**. Read it **from the bottom up**: the last line names the error, and the lines above show where it happened |
| **Log** | Messages a program writes while running. In TriageBot, Uvicorn prints one line per request (`"POST /triage HTTP/1.1" 200 OK`), and app modules log warnings with `logging.getLogger(__name__)` |
| **Breakpoint** | A marker that pauses the program on a line so you can look at variables |
| **Minimal reproduction** | The smallest set of steps or code that still shows the bug |
| **Regression test** | A test written for a fixed bug, so it can never silently come back |

**What logs look like** [VERIFIED 2026-10-03; started with an invalid
setting and a missing model on purpose]:

```
INFO:     Application startup complete.
ignoring invalid TRIAGEBOT_ML_THRESHOLD='abc'; using 0.5          ← app/config.py warning
INFO:     127.0.0.1:63957 - "POST /triage HTTP/1.1" 200 OK          ← one line per request
```

For a **500 error**, Uvicorn prints `Exception in ASGI application`, followed
by the full traceback, in the server terminal. The browser only gets
`Internal Server Error`.

### 20.2 Debugging in VS Code

**Option A: tests (the easiest).**

1. Open the **Testing** panel (the flask icon) and choose **pytest** when
   asked.
2. Click in the margin next to a line to set a red-dot breakpoint.
3. Right-click a test → **Debug Test**.
4. When it pauses, the **Variables** pane shows values. Step over (F10),
   step into (F11), continue (F5).

**Option B: the running server.** Create `.vscode/launch.json` yourself
**[RECOMMENDED]**. It doesn't exist in the repository, and `.vscode/` is
git-ignored.

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "TriageBot server",
      "type": "debugpy",
      "request": "launch",
      "module": "uvicorn",
      "args": ["app.main:app", "--port", "8000"],
      "env": {"TRIAGEBOT_DB": "/tmp/triagebot-debug.db"},
      "justMyCode": true
    }
  ]
}
```

Leave out `--reload` while debugging; the reloader runs your code in a child
process. Start it with **Run → Start Debugging**, set a breakpoint in e.g.
`app/main.py:sync_repo`, then trigger the request from the dashboard.

**Print and logging, carefully:**

- A quick `print(variable)` is fine while exploring, but **remove it before
  committing**.
- `log.warning("...")` is better for things that should stay.
- **Never print `settings.github_token`** or request headers; they contain
  your token.

### 20.3 Which layer is broken?

| Symptom | Layer | First place to look |
|---|---|---|
| A red error in the browser Console, or the page doesn't react | Frontend (`static/index.html`) | DevTools → **Console** |
| A request shows 4xx in DevTools → **Network** | API input: your request was rejected | The response's `detail` |
| A request shows 500 | Backend code | The **server terminal** traceback |
| 429 / 502 | GitHub or the network | The message ("rate limit", "did not respond"); `rate_limit_remaining` |
| Wrong data, but no error | Logic or database | `sqlite3` on a backup (Chapter 13) and the "Why" panels |
| Categories all "rules" with an amber chip | Model | `GET /model` → `available` / `reason` |
| `ModuleNotFoundError`, `command not found` | Environment | `which python`; is the venv active? |

### 20.4 A systematic workflow, using a real bug

**The bug:** `POST /repos/sync` returns **500** for a repository that was saved
*before migration v7* and has **no items**. It was found during the code
review on 2026-10-03.

1. **Reproduce.** Make it happen on purpose, on a throw-away database. The
   test fake in `tests/github_fakes.py` makes this easy (see Exercise 8 in
   Chapter 33).
2. **Read the error.** The server traceback ends with
   `ValueError: Nothing to synchronise yet: import the repository first.`
3. **Identify the layer.** It's a 500, so the backend.
4. **Locate the code.** Search for the message: in VS Code, `Cmd+Shift+F`
   for "Nothing to synchronise". You'll find:

   ```python
   # app/importer.py — sync_repo
   window_start = repository.sync_window_start(repo["id"])
   if window_start is None:
       raise ValueError("Nothing to synchronise yet: import the repository first.")
   ```

   and in `app/main.py:sync_repo` only `except GitHubError` is caught.
5. **Form a hypothesis.** "`sync_window_start` returns `None` when
   `synced_through` is NULL *and* there are no items. The `ValueError` isn't
   converted into an HTTP error."
6. **Test the hypothesis.** Set `synced_through = NULL` on a repository with
   no items in a scratch database, then call sync → 500. Confirmed.
7. **Fix the smallest thing.** For example, in `app/main.py:sync_repo` also
   catch `ValueError` and
   `raise HTTPException(status_code=409, detail=str(exc))`. (409 means
   "conflict with the current state".)
8. **Add a regression test** in `tests/test_sync.py` that builds this
   situation and asserts `status_code == 409`.
9. **Run the tests:** the new one first (`pytest tests/test_sync.py -k ...`),
   then everything (`pytest`), then `ruff check .`.
10. **Review the diff:** `git diff app/main.py tests/test_sync.py`. Is it only
    what you meant?

This bug is **not fixed** in the repository; fixing it is your exercise.

### Checkpoint 20

1. Where do you read a traceback for a 500 error?
2. Which line of a traceback do you read first?
3. Why should breakpoints be used without `--reload`?

---

## Chapter 21: Common errors and solutions

Only the causes marked "confirmed" were reproduced. The others are typical
causes that you must check before acting on them.

| Problem | What it looks like | Likely causes | Investigate (safe) | Fix | Verify |
|---|---|---|---|---|---|
| Missing dependency | `ModuleNotFoundError: No module named 'sklearn'` | Venv not active; dependencies not installed | `which python`; `pip list` | `source .venv/bin/activate`; `pip install -r requirements-dev.txt` | `python -c "import sklearn"` |
| Wrong Python version | Syntax errors on `X \| None`, or pip refuses versions | Python < 3.11 | `python --version` | Recreate the venv with `python3.11 -m venv .venv` | `pytest` passes |
| Venv not activated | `command not found: uvicorn` / `pytest` | You forgot `source` | Look for `(.venv)` in the prompt | `source .venv/bin/activate` | `which uvicorn` points into `.venv` |
| Port in use | `[Errno 48] Address already in use` | Another server is running | `lsof -i :8000` | Stop it, or use `--port 8001` | Server starts |
| Database path or permission | `unable to open database file` | `TRIAGEBOT_DB` folder missing or read-only | `echo $TRIAGEBOT_DB`; `ls -ld <folder>` | Use a writable path | `/health` OK |
| Migration failure | Traceback at start-up from `init_db` | A corrupt file, or a file from an unknown version | Back up; `sqlite3 copy.db "PRAGMA integrity_check; PRAGMA user_version;"` | Restore from backup, or start with a fresh DB path | Start-up completes |
| Invalid repository name | **422** "Expected a public GitHub repository as 'owner/name'…" (confirmed) | A typo, spaces, an unsupported URL | Read `detail` | Use `owner/name` | 200 |
| Repository missing or private | **404** "Repository not found (or it is private)." | Wrong name; private repository | Open it in a browser | Fix the name; only public repositories are supported | 200 |
| Bad token | **502** "GitHub rejected the configured GITHUB_TOKEN." | Expired or mistyped token | Check `.env` (don't print the token) | Create a new token; restart with `--env-file .env` | Import works |
| Rate limit | **429** "GitHub API rate limit exceeded… Resets at HH:MM UTC." (confirmed in the UI) | 60 requests per hour without a token | `rate_limit_remaining` in import / sync responses | Wait for the reset, or add a token | Next import works |
| Network timeout | **502** "GitHub did not respond in time." | Slow network | `curl -I https://api.github.com` | Retry; raise `GITHUB_TIMEOUT` | — |
| Invalid JSON | **422** "JSON decode error" (confirmed) | A broken curl body (quotes) | Re-check the quoting | Use single quotes around the JSON in zsh | 200 |
| Unsupported content type | **415** "Request body must be application/json." (confirmed) | Missing `-H "Content-Type: application/json"` | — | Add the header | 200 |
| Model missing or checksum mismatch | `/model` `available: false` with a reason; amber "ML unavailable" chips (confirmed) | `models/` files moved or edited; `TRIAGEBOT_MODEL_PATH` wrong | `curl /model`; `git status models/` | `git checkout -- models/` (⚠ discards local model changes), or fix the path | `available: true` |
| Classification fallback | Many items "rules" with "ML was unsure (x% < 50%)" | **Normal**: the model is unsure on unfamiliar repositories | The reasons panel | Nothing to fix; it's by design | — |
| Duplicate analysis failure | The import succeeds but `duplicates: null`; a traceback in the log ("duplicate analysis failed for …") | Unexpected data | The server log | Fix the cause; **Find duplicates** to retry | Suggestions appear |
| Frontend not loading | Blank page or 404 | Server not running; wrong URL | `curl http://127.0.0.1:8000/health` | Start the server; open `/` | Page loads |
| Backend request failure | "Could not reach the TriageBot server. Is it still running?" | Server stopped | The server terminal | Restart it | — |
| JavaScript syntax error | Console red text; nothing works | An edit to `index.html` broke the script | The Console line number | Fix it, reload | No console errors |
| Failed test | `FAILED tests/test_x.py::test_y - AssertionError` | Code or test mismatch | `pytest tests/test_x.py::test_y -v` (shows assert details) | Fix the code (or the test, if the behaviour intentionally changed) | `pytest` all green |
| Ruff error | e.g. `F401 'os' imported but unused` | Style or bug-prone code | `ruff check .` (shows file:line and the rule) | Edit; `ruff check --fix .` fixes some automatically (⚠ edits files) | `All checks passed!` |
| Git merge conflict | Files with `<<<<<<< HEAD … ======= … >>>>>>>` | Two branches changed the same lines | `git status` | Edit to the correct content, remove the markers, `git add`, finish the merge (Chapter 25) | `git status` clean |

**Rules for using this table:**

- Read the **exact** message before acting.
- One problem can have several causes. Confirm one with the "Investigate"
  column, then fix it.
- Back up the database before any repair that touches it.

### Checkpoint 21

1. What is the difference between 415 and 422 in TriageBot?
2. Is "ML was unsure" a bug?
3. What should you do *before* repairing a database?

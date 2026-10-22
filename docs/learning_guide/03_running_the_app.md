# Part 3: Run the Application Yourself

[← Part 2](02_setup_and_configuration.md) · [Index](00_INDEX.md) · Next: [Part 4: Data flow and API](04_data_flow_and_api.md)

---

## Chapter 6: Starting the backend

### 6.1 Key ideas first

- **Server:** a program that waits for requests and answers them.
- **Backend:** the server-side part of the app (Python in `app/`). The
  **frontend** is what runs in the browser (`static/index.html`).
- **ASGI:** a standard way for a Python web app to talk to a web server.
  **Uvicorn** is an ASGI server. It listens on the network, receives HTTP
  requests, hands them to FastAPI, and sends FastAPI's answers back. In short:
  Uvicorn is the waiter, FastAPI is the kitchen.
- **localhost / 127.0.0.1:** "this computer". A server on `127.0.0.1` can only
  be reached from your own Mac.
- **Port:** a numbered "door" on a computer. Uvicorn uses port **8000** by
  default, so the address is `http://127.0.0.1:8000`.

### 6.2 The exact start command

The entry point is the object `app` in `app/main.py`, hence `app.main:app`
(module `app.main`, variable `app`).

```bash
$ cd ~/Desktop/triagebot
$ source .venv/bin/activate
(.venv) $ uvicorn app.main:app --reload
```

| Variant | Command |
|---|---|
| With your `.env` file | `uvicorn app.main:app --reload --env-file .env` |
| With a throw-away database (recommended while learning) | `TRIAGEBOT_DB=/tmp/triagebot-play.db uvicorn app.main:app --reload` |

`--reload` restarts the server whenever you save a Python file. That's handy
while developing; don't use it in production.

### 6.3 What happens at start-up (in order)

1. Uvicorn imports `app/main.py`. Its module-level code creates
   `app = FastAPI(..., lifespan=lifespan)`, registers every `@app.get` /
   `@app.post` route and the `@app.middleware("http")` function, and mounts
   `/static`.
2. Before accepting requests, FastAPI runs the **lifespan** function:

   ```python
   @asynccontextmanager
   async def lifespan(_app: FastAPI):
       """Ensure the database schema exists before serving requests."""
       init_db()
       yield
   ```

3. `init_db()` (in `app/database.py`) creates the SQLite file if needed,
   switches it to WAL mode and applies any **migrations** that haven't run
   yet (Chapter 13).
4. The ML model is **not** loaded at start-up. It loads on first use and is
   then cached (`app/ml/service.py:load_model`).
5. Uvicorn prints that it's ready.

**A successful start looks like this** [VERIFIED 2026-10-03, on port 8770]:

```
INFO:     Started server process [14262]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

**Quick check from a second terminal:**

```bash
$ curl -s http://127.0.0.1:8000/health
{"status":"ok","version":"1.0.0"}
```

Also open **http://127.0.0.1:8000/docs**. FastAPI generates an interactive
API page there automatically.

### 6.4 Stopping the server

Press **Ctrl+C** in the terminal where it runs. You'll see `Shutting down` …
`Finished server process`. The database is closed cleanly, because each
request opens and closes its own connection.

### 6.5 Common start-up errors

| You see | Likely cause | Check | Fix |
|---|---|---|---|
| `zsh: command not found: uvicorn` | The venv isn't active, or the dependencies aren't installed | `which python` | `source .venv/bin/activate`, then `pip install -r requirements-dev.txt` |
| `ModuleNotFoundError: No module named 'fastapi'` | Same | `pip list \| grep -i fastapi` | Same |
| `[Errno 48] Address already in use` | Another server is using port 8000 | `lsof -i :8000` | Stop the other one (Ctrl+C in its terminal), or use `--port 8001` |
| `Error loading ASGI app. Could not import module "app.main"` | You aren't in the project root | `pwd`, `ls app/main.py` | `cd` to the folder that contains `app/` |
| `sqlite3.OperationalError: unable to open database file` | The `TRIAGEBOT_DB` folder doesn't exist or isn't writable | `echo $TRIAGEBOT_DB` | Point it at an existing, writable folder |
| A migration error at start-up | A corrupt or very unusual database | Read the traceback; back up the file | See Chapter 21 |

### 6.6 Development vs several workers

- **Development:** run **one** process with `--reload`.
- **Several workers:** `uvicorn app.main:app --workers 2` starts several
  processes to handle more requests. Don't combine it with `--reload`.
- Several workers on **one machine** are safe since Phase 6:
  - `init_db` takes a write lock (`BEGIN IMMEDIATE`) for each migration, so
    workers starting together don't clash;
  - SQLite uses WAL mode with a 15-second busy timeout.
- **What remains:**
  - SQLite allows **one writer at a time**;
  - several *machines* would need a server database (Chapter 31).

[REPORTED] 8 processes starting together 15 times gave 0 errors (Phase 6
stress test recorded during the project's verification work).

### Checkpoint 6

1. What does `app.main:app` mean?
2. Which function runs the database migrations, and when?
3. What would you do if port 8000 is busy?

---

## Chapter 7: Running and using the frontend

### 7.1 Where the frontend is and how it connects

- **The whole frontend is one file:** `static/index.html`. It holds the HTML
  (structure), the CSS (styling, dark and light themes) and one `<script>`
  block (JavaScript behaviour).
- **No separate frontend server.** FastAPI serves the file at `/`:

  ```python
  @app.get("/", include_in_schema=False)
  def index() -> FileResponse:
      return FileResponse(os.path.join(STATIC_DIR, "index.html"))
  ```

  So once the backend runs, just open **http://127.0.0.1:8000**.
- **How JavaScript calls the backend.** Every request goes through one helper
  function, `api()`, and writes go through `postJson()`:

  ```javascript
  async function api(path, opts) {
    let res;
    try {
      res = await fetch(path, opts);           // same server, so a relative path like "/items"
    } catch {
      throw new Error("Could not reach the TriageBot server. Is it still running?");
    }
    if (res.status === 204) return null;
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok) throw new Error(errorMessage(res.status, data));
    return data;
  }
  const postJson = (path, body, method = "POST") => api(path, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  ```

  `postJson` always sends `Content-Type: application/json`. That's why the
  security check that rejects non-JSON writes (Chapter 30) doesn't affect
  the dashboard.
- **How data appears.** The response JSON is turned into table rows with
  template strings. Any text that comes from GitHub passes through
  `escapeHtml()` first, so a malicious issue title can't inject HTML.

### 7.2 Browser developer tools (your best debugging friend)

In Chrome: **View → Developer → Developer Tools** (`Cmd+Option+I`).

- **Console tab:** JavaScript errors appear in red. A healthy page shows none.
- **Network tab:** every `fetch` appears here. Click a request to see its
  URL, method, status code, request body and response.
  - A red row means a failed request: look at the status (422, 404, 429…)
    and the response's `detail`.
- **Mobile widths:** click the phone/tablet icon (the "device toolbar") and
  choose a width such as 390 px. [REPORTED] At 390 px every tab fits with no
  horizontal scrolling.

### 7.3 The dashboard, feature by feature

| # | Feature | Where / how | API call | Notes |
|---|---|---|---|---|
| 1 | **Import a repository** | **GitHub items** tab → *Public repository* (`owner/name` or a GitHub URL), *State* (Open / Closed / All), *Max items* (1–500) → **Import** | `POST /repos/import` | A status line reports the counts, the duplicates to review and the remaining GitHub requests |
| 2 | **View imported repositories** | The **Repository** filter dropdown lists them as "name (item count)" | `GET /repos` | There's no separate repository page |
| 3 | **Browse issues and PRs** | The table, 50 per page, with **← Prev / Next →** | `GET /items?limit=50&offset=…` | Sorted high → medium → low priority, then most recently updated |
| 4 | **Filter and search** | Repository, Type, Category, Priority, State, Category source, Search | `GET /items?...` | Search matches title, body, or `#123`. Typing waits 250 ms before searching. **Reset** clears everything |
| 5 | **Category predictions** | The Category column: a coloured badge, a confidence line and a source chip | (in `/items`) | Chip **ML n%** = the model's probability; **rules** = keyword rules; **maintainer label** = set on GitHub. An amber **ML unsure / ML unavailable** chip means the rules were used even though a model exists |
| 6 | **Priority suggestions** | The Priority column: a badge plus **suggested** or **maintainer label** | (in `/items`) | **Why this suggestion?** lists every point added or subtracted, plus a note that a suggested priority isn't real urgency |
| 7 | **Duplicate suggestions** | **Potential duplicates (N)** under an issue | `GET /items/{id}/duplicates` (loaded when opened) | Shows the other issue, the similarity score, title/description similarity and shared terms |
| 8 | **Confirm / reject** | **Confirm duplicate** / **Not a duplicate** | `PATCH /duplicates/{id}` with `{"status": "confirmed"}` or `{"status": "rejected"}` | Stored locally only; GitHub is never changed |
| 9 | **Undo** | **Undo** on a decided suggestion | `PATCH /duplicates/{id}` with `{"status": "pending"}` | |
| 10 | **Re-run duplicate detection** | **Find duplicates** (pick a repository first) | `POST /duplicates/refresh` with `force: true` | |
| 11 | **PR insights** | **PR insights** tab: tiles (open, awaiting review, stale, large, drafts, size & reviews loaded), filters, sort, **Why these insights?** | `GET /prs?...` | Clicking a tile filters by it |
| 12 | **Load PR size & reviews** | *PRs to load* (1–50) → **Load size & reviews** | `POST /prs/details` | Costs 2 GitHub requests per PR |
| 13 | **Sync with GitHub** | Pick a repository → **Sync with GitHub** | `POST /repos/sync` | Reports refreshed / closed / reopened / new items. If the limit is reached it says "More changes remain" |
| 14 | **Manual triage** | **Manual triage** tab: type a title (and body); the preview updates live; **Triage & save** stores it; tick the checkbox to mark it closed; **Delete** removes it | `POST /triage` (preview), `POST /issues`, `GET /issues`, `PATCH /issues/{id}`, `DELETE /issues/{id}` | This older feature uses its own severity scale (low / medium / high / urgent) |
| 15 | **Theme** | 🌙 / ☀️ button, top right | — | Remembered in your browser's local storage |

**API routes the dashboard does not use** (call them yourself with curl or at
`/docs`):

- `POST /triage/batch` (up to 100 issues at once);
- `GET /prs/{id}`;
- `GET /issues/{id}`;
- `GET /health`.

### Checkpoint 7

1. Why don't you need to start a frontend server?
2. Where in the browser would you look to see why "Import failed"?
3. What does an amber **ML unsure** chip mean?

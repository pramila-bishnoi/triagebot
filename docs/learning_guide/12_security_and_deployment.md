# Part 11: Security and Deployment

[← Part 10](11_ci_cd.md) · [Index](00_INDEX.md) · Next: [Part 12: Interviews, exercises, roadmap](13_interviews_exercises_roadmap.md)

---

## Chapter 30: Security fundamentals

### 30.1 The concepts, and TriageBot's status

| Concept | Meaning (simple) | In TriageBot today |
|---|---|---|
| **Authentication** | Proving *who* you are (a login) | ❌ **None.** Anyone who can reach the server can use every endpoint |
| **Authorisation** | What you are *allowed* to do | ❌ **None** (no users, no roles) |
| **CSRF** (cross-site request forgery) | A malicious website makes *your browser* send requests to another site you can reach | ⚠ **Partly mitigated** (30.2) |
| **CORS** (cross-origin resource sharing) | Browser rules about which websites may call an API from JavaScript | No CORS middleware, so other websites' JavaScript can't read TriageBot's responses, and non-simple requests are blocked by the browser's pre-check ("preflight") |
| **HTTPS** | Encrypted HTTP (the padlock) | ❌ Local Uvicorn serves plain HTTP. Fine on `127.0.0.1`, required on the internet |
| **API rate limiting** | Limiting how many requests a client may make | ❌ None for TriageBot's own API. ✅ GitHub's limit is handled (429 with `Retry-After`) |
| **Secret management** | Keeping tokens out of code, logs and Git | ✅ The token comes only from the environment / `.env` (git-ignored). It's never returned by the API, and a test checks it's never logged |
| **Input validation** | Rejecting bad input early | ✅ Pydantic types and limits: title ≤ 200, body ≤ 5,000, import ≤ 500, page ≤ 200, batch ≤ 100, PR details ≤ 50, sync ≤ 1,000; strict `owner/name` regex |
| **SQL injection** | Tricking a query by putting SQL in the input | ✅ All values are `?` parameters; filter column names come from a fixed whitelist (`repository._ITEM_FILTERS`); `LIKE` wildcards are escaped (`_escape_like`) |
| **XSS** (cross-site scripting) | Getting the page to run attacker-supplied HTML or JavaScript | ✅ GitHub text goes through `escapeHtml()`; links pass `safeUrl()`, which only allows `https://github.com/...` |
| **Security headers** | Instructions to the browser | ✅ `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. ❌ No Content-Security-Policy (the UI uses an inline script) |
| **Dependency vulnerability scanning** | Checking libraries for known security holes | ❌ Not set up (`pip-audit` is a common tool) **[RECOMMENDED]** |
| **Safe model loading** | Not executing code from data files | ✅ `allow_pickle=False` + SHA-256 check |
| **Read-only towards GitHub** | Can't damage the real project | ✅ The client has only `GET` methods; the test fake returns 405 for anything else |

### 30.2 The JSON-only rule and its limits (`app/main.py`)

```python
if request.method in _UNSAFE_METHODS and has_body:      # POST, PUT, PATCH, DELETE with a body
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return JSONResponse(status_code=415, content={"detail": "Request body must be application/json."})
```

**What it protects against.** Browsers allow some "simple" cross-site
requests *without asking first*:

- a form POST;
- `fetch(..., {mode: "no-cors", body: blob})`, which sends **no** content
  type.

FastAPI parses a body with no content type as JSON. Before this rule, a
malicious website you visited could therefore make your browser create issues
or start imports on a TriageBot running on your Mac. [REPORTED: it returned
201 before the fix.]

Requiring `application/json` forces the browser to send a **preflight**
first. TriageBot sends no CORS headers, so the browser stops there. PATCH and
DELETE always need a preflight anyway.

**What it does NOT protect against:**

- **anyone who can reach the server directly** (curl, scripts, other
  machines): there is no authentication;
- **DNS rebinding**, where a malicious domain re-points itself to
  `127.0.0.1` so the browser treats it as same-origin. [RECOMMENDED] Later:
  FastAPI's `TrustedHostMiddleware` allowing only `localhost` and
  `127.0.0.1`;
- abuse by request volume (no rate limiting).

**It is not complete CSRF protection.** Real CSRF protection comes with
authentication: session cookies with `SameSite`, and CSRF tokens.

### 30.3 Why it's not production-ready

**Missing:**

- authentication and authorisation;
- rate limiting;
- HTTPS;
- persistent and backed-up storage;
- dependency scanning;
- monitoring.

**Safe today:** running it **on your own Mac**. Uvicorn binds to `127.0.0.1`
by default [VERIFIED: "Uvicorn running on http://127.0.0.1:…"], so other
computers can't reach it.

⚠ `--host 0.0.0.0` would expose it to your whole network. Don't use it
outside a protected environment.

### Checkpoint 30

1. Does the JSON-only rule stop someone running `curl` against your server?
   (No.)
2. Which two functions protect the UI against XSS?
3. Why is binding to `127.0.0.1` important today?

---

## Chapter 31: Deployment fundamentals

### 31.1 Words

| Term | Meaning |
|---|---|
| **Deployment** | Running the app on a server so others can use it |
| **Server / hosting platform** | A computer (often rented, e.g. Render, Fly.io, a cloud VM) that runs your app 24/7 |
| **Environment variables in hosting** | Set in the platform's dashboard, never committed (e.g. `GITHUB_TOKEN`) |
| **HTTPS certificate** | Proves the site's identity and enables encryption. Most platforms provide one automatically |
| **Persistent storage** | Disk space that survives restarts and redeploys |
| **Migration in production** | `init_db` runs at every start. Take a database **backup** before deploying a version with new migrations |
| **Monitoring** | Health checks (`/health`), logs, alerts when it's down or erroring |

**SQLite on hosting platforms:**

- Many free plans have an **ephemeral disk**: files vanish on redeploy or
  restart, so all imported data would be lost.
- SQLite also allows only one writer, and its file must be on a local disk.
- A real deployment needs a persistent volume, or a server database such as
  PostgreSQL. Moving to PostgreSQL would be a substantial change.

### 31.2 The existing deployment configuration

`render.yaml` comes from the starter project. It defines:

- a free Render web service;
- build `pip install -r requirements.txt`;
- start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`;
- health check `/health`;
- `PYTHON_VERSION 3.12.8`;
- `GITHUB_TOKEN` to be set in the dashboard;
- `autoDeploy: true` on branch `main`.

**Status: [NOT VERIFIED]** It has never been tested with the current code.
**Nothing is deployed.**

⚠ If you ever connect this repository to Render, `autoDeploy` on `main`
would publish the app, **without authentication**, as soon as you push to
`main`. Don't connect it until the checklist below is done.

### 31.3 Future deployment checklist [RECOMMENDED; do not do this yet]

1. Authentication: GitHub OAuth, or a reverse proxy with login.
2. Rate limiting for TriageBot's own API, and limits on import / sync
   frequency.
3. HTTPS (usually provided by the platform).
4. Persistent storage (a mounted volume), or PostgreSQL. A backup plan.
5. `GITHUB_TOKEN` as a platform secret, with no permissions.
6. Run CI green on the exact commit being deployed.
7. Optional: `TrustedHostMiddleware`, a Content-Security-Policy (after moving
   the inline script to a file), `pip-audit`.
8. Monitoring: platform health checks on `/health`; read the logs after
   deploying.
9. A database backup before deploying versions with new migrations.

### Checkpoint 31

1. Why is SQLite on a free hosting plan risky?
2. What would `autoDeploy: true` do if you connected Render today?
3. Name three items that must be done before a public demo.

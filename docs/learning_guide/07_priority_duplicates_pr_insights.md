# Part 6b: Intelligence Features: Priority, Duplicates, PR Insights

[← Part 6a](06_classification_and_evaluation.md) · [Index](00_INDEX.md) · Next: [Part 7: Debugging](08_debugging.md)

---

## Chapter 17: Priority scoring

### 17.1 Priority is not category

- **Category** answers "what *kind* of thing is this?"
- **Priority** answers "how *urgent* is it?"

A typo report is `documentation` *and* usually `low` priority. A crash is
`bug` and could be any priority. They are computed separately: priority is
scored *after* the category is known, by `app/priority.py:score_priority`.

### 17.2 The algorithm (six steps in the code)

**Step 1: words** (`TEXT_SIGNALS`). The title is checked first, and each
kind counts at most once.

| Kind | Example keywords | Title | Body-only | Counts as evidence? |
|---|---|---|---|---|
| severe | urgent, critical, outage, data loss, security, cve, blocker | +3 | +2 | yes (title only) |
| impact | crash, regression, broken, fail, error, cannot, can't, 500, panic | +2 | **0 (reported, not scored)** | yes (title only) |
| scope | all users, everyone, users can't, in production, unusable | +1 | **0** | yes (title only) |
| minor | typo, nit, cosmetic, trivial, nice to have | −2 | −1 | no |

**Step 2: labels that are hints, not decisions.**

- A severity *topic* label (`security`, `regression`, `crash`, `data loss`,
  e.g. `kind/regression`) gives **+2** and counts as "severity label"
  evidence. Team labels such as `sig/security` do not count.
- `good first issue` gives −1; `wontfix`, `invalid` and `duplicate` give −2.

**Step 3: category** (`CATEGORY_ADJUST`): bug +1; feature, documentation or
question −1. This is **skipped** when the category is a low-confidence
guess, so an item we know little about stays neutral.

**Step 4: engagement.** ≥ 10 reactions gives +1 ("supports but never sets
high on its own"). **Comments are not scored**, because long discussions
aren't evidence of impact.

**Step 5: draft PRs** get −1.

**Step 6: thresholds and maintainer labels.**

- **High:** score ≥ 3 **and** at least **2 different kinds** of evidence, one
  of them severity (severe, impact or severity label).
- **Capped:** score ≥ 3 without that is "capped at medium", and the reason
  says what was missing.
- **Low:** score ≤ −1.
- **Medium:** everything else. This is why uncertain items stay medium:
  missing information is treated as neutral.
- **A maintainer's priority label wins**, and is shown with
  `priority_source = "maintainer_label"`. Examples: `P0`–`P4`,
  `priority: high`, `sev1`, `critical`, `urgent`, `blocker`. The rules'
  answer is still shown "for reference".
- **Conflicting labels** (`P1` + `P3`) are ignored with a note.

### 17.3 Real traces [VERIFIED 2026-10-03: `classify_item` output, illustrative inputs]

| Item | Result | Why (the actual reasons) |
|---|---|---|
| "API returns 500 on login, users can't sign in" | **high** | 'can't', '500' (impact +2); 'users can't' (scope +1); bug +1 → +4 with 2 kinds (impact, scope) |
| "App crashes" | **medium (capped)** | impact +2, bug +1 = +3, but only 1 kind of evidence |
| "Add dark mode", 500 reactions | **medium** | feature −1, reactions +1 → 0 |
| "Typo in README" + label `P1` | **high** [maintainer_label] | "human decision, kept as-is"; the rules alone would say low (−3) |
| "App crashes" + labels `P1`, `P3` | **medium** | "conflicting maintainer priority labels … ignored", then the rules decide |
| "Terminal crashes when resizing" + `kind/regression` | **high** | impact +2, severity label +2, bug +1 → 2 kinds |
| Draft PR "feat: add export" | **low** | feature −1, draft −1 → −2 |

### 17.4 Evaluation and trade-offs [REPORTED by `scripts/evaluate_priority.py`]

- **Data:** 2,458 issues whose maintainers set priority labels, from 21
  repositories. Each project's scale was mapped to high / medium / low.
  - The split is **by repository**: development repositories were used for
    analysis; held-out repositories (1,191 issues) were scored once.
  - Sampling is balanced, so "always medium" scores 0.334 accuracy.

| Scorer (held out) | Accuracy | Macro-F1 | Low→high errors |
|---|---|---|---|
| Current (Phase 3.1) | 0.367 | 0.337 | **20** |
| Older Phase 2 scorer | 0.408 | 0.374 | 88 |
| Always "medium" | 0.334 | 0.167 | 0 |

**What this honestly means:**

- Keyword-based priority is a **weak signal**: per-repository macro-F1 ranges
  from 0.15 to 0.45.
- The older scorer scores higher on these averages but calls far more
  low-priority issues "high". The current one is cautious.
- It was kept, because switching after seeing test results would be "tuning
  on the test set".
- **A priority is a suggestion.** Real urgency depends on roadmaps and users
  the text doesn't mention. The dashboard says so in the "Why" panel.

### Checkpoint 17

1. Why can't one keyword alone make an item high?
2. What wins over the computed score?
3. Why does the cautious scorer score lower on accuracy?

---

## Chapter 18: Duplicate detection

### 18.1 The idea

A **duplicate** is a new issue describing a problem that is already reported.

**Finding them is hard:**

- people describe the same bug with different words ("crash" vs "app
  closes");
- *different* bugs can share words (the same error message, different causes).

### 18.2 Cosine similarity: a hand-worked example (illustrative, not project output)

Use word counts as simple vectors over the vocabulary
[`app`, `crash`, `save`, `slow`]:

| Title | Vector |
|---|---|
| A: "app crash save" | (1, 1, 1, 0) |
| B: "app crash on save" | (1, 1, 1, 0); "on" is a stop word, so it is removed |
| C: "app slow save" | (1, 0, 1, 1) |

**cosine(A, B)** = (1·1 + 1·1 + 1·1 + 0) / (√3 · √3) = 3/3 = **1.0**

**cosine(A, C)** = (1 + 0 + 1 + 0) / (√3 · √3) = 2/3 ≈ **0.67**

C shares "app" and "save" but is a different problem. That is why a high
similarity is a *hint*, not proof. Real TF-IDF weights make rare shared words
count more than common ones.

### 18.3 What the code does (`app/duplicates.py`)

1. **`normalize_text`** removes HTML comments, template headings
   ("### Describe the bug"), checklists, URLs, images and Markdown markup. It
   **keeps** code content (the same error message is strong evidence), and
   keeps tokens like `read_csv` whole.
2. **`DuplicateIndex`** fits **two** TF-IDF vectorisers *per repository*,
   one for titles and one for descriptions (single words and two-word
   phrases, English stop words removed).
3. **The score** is `0.6 × title cosine + 0.4 × description cosine`
   (`TITLE_WEIGHT = 0.6`). If either description has fewer than 5 tokens,
   the title alone is used. Titles with fewer than 2 tokens are skipped, and
   an issue is never compared with itself.
4. **Pairs** (`DuplicateIndex.pairs`):
   - only issues, never PRs, and only within the same repository;
   - each issue keeps its best **5** matches with score ≥ **0.20**
     (`DEFAULT_THRESHOLD`, or `TRIAGEBOT_DUP_THRESHOLD`);
   - computed in blocks of 500 rows to limit memory;
   - each pair is stored once, smaller ID first.
5. **The explanation** (`Match.explanation`): title similarity, description
   similarity, up to 5 **shared terms**, and "(a similarity score, not a
   probability)".

**A real explanation** [REPORTED in the README; pandas issues #66050 and
#65804]:

```
title similarity 0.70
description similarity 0.36
shared terms: 'converting numpy', 'numpy masked', 'missing values', 'bug pd.array', 'masked arrays'
combined similarity 0.56 (a similarity score, not a probability)
```

### 18.4 Decisions and re-analysis

- **When it runs:** `analyze_repo` runs after every import and sync, and
  when you press **Find duplicates**.
- **Skipping unchanged repositories:** a **fingerprint** (SHA-256 of the
  settings plus every issue's ID, title and body) is stored. If it hasn't
  changed, the analysis is skipped (`"reused": true`).
- **Your decisions:** `PATCH /duplicates/{id}` with `confirmed`, `rejected`
  or `pending` (undo) updates `status` and `decided_at`
  (`repository.set_suggestion_status`).
- **Decisions survive re-analysis:** `save_duplicate_analysis` deletes and
  replaces only **pending** pairs, in one transaction, so confirmed and
  rejected pairs never change.
- **Counting:** rejected pairs stop counting in "Potential duplicates (N)".

### 18.5 Evaluation [REPORTED by `scripts/evaluate_duplicates.py`]

- **Ground truth:**
  - **93 real pairs** from 11 repositories, each one an issue a maintainer
    closed as a duplicate *with* a comment naming the original;
  - **53 development / 40 test pairs**, split by repository.
- **Selection (development data only):** the highest F1 with at most 10%
  false alarms gives weight 0.6 and threshold 0.20.

| Held-out test (40 pairs) | Value |
|---|---|
| Precision (a lower bound: unlabelled real duplicates count as wrong) | **0.792** |
| Recall | **0.400** (interval 0.26–0.55) |
| F1 | 0.531 |
| False alarms on ordinary issues | 0.100 |
| Original ranked 1st / in the top 5 | 0.650 / 0.725 |
| **Recall without pandas** | **3 of 19** |

**Honest reading:**

- Suggestions are often right, but most real duplicates are **not** found.
- pandas, with its very consistent "BUG: …" titles, carries the headline
  number.
- It matches words, not meaning.

**Reproduce it:**

```bash
$ python -m scripts.fetch_duplicate_dataset --expand --max-new 100 --max-wait 5400   # [CHANGES STATE] data/ (git-ignored); slow without a token
$ python -m scripts.evaluate_duplicates --cohort all                                 # development: selection only
$ python -m scripts.evaluate_duplicates --cohort all --split all --out /tmp/DUP.md   # full report
```

The report's **"Held-out errors"** section lists every false positive and
false negative by issue number. Open both issues on GitHub to judge them
yourself.

### Checkpoint 18

1. Compute cosine((1,0,1),(1,1,0)). (Answer: 1/2 = 0.5.)
2. Why is precision called a "lower bound"?
3. What happens to a rejected pair when duplicates are re-analysed?

---

## Chapter 19: PR insights

### 19.1 What is imported, and from where

| Data | Source | When |
|---|---|---|
| State, draft, dates, merge date, labels, comment count | GitHub issues API | During import or sync (free) |
| Branches, **pending review requests** | Pulls list API (1 request per 100 PRs) | After import or sync; best effort |
| Additions, deletions, changed files, commits; **submitted reviews** | `GET /pulls/{n}` + `GET /pulls/{n}/reviews` | **On demand**, via **Load size & reviews** (2 requests per PR, ≤ 50 per click) |

PRs whose details already match their current `updated_at` are skipped
(`repository.prs_needing_details`). A rate limit stops loading cleanly and
keeps what was fetched.

### 19.2 The rules (`app/pr_insights.py:analyze`)

Each insight is tagged **GitHub** (a fact GitHub reports) or **calculated**
(TriageBot derived it).

| Insight | Rule | Source |
|---|---|---|
| Status | open + draft → `draft`; `open`; merged (`merged_at`) → `merged`; else `closed`. If merge data was never recorded → "Closed (merge status unknown)" | GitHub |
| Age / idle | Days since `created_at` / `updated_at` | calculated |
| **Stale** | Open **and** idle ≥ 30 days (`TRIAGEBOT_PR_STALE_DAYS`) | calculated |
| Awaiting review | Open, not a draft, with pending requested reviewers or teams | GitHub |
| Changes requested / Approved | Each reviewer's **latest** decisive review (`summarize_reviews`): COMMENTED doesn't change it, DISMISSED clears it, changes requested beats approved | GitHub |
| No reviews yet | Open, not a draft, reviews loaded, and 0 reviews | GitHub |
| **Size** | Lines changed (additions + deletions): **XS < 10 · S < 100 · M < 500 · L < 1000 · XL ≥ 1000** | calculated |
| **Large** | ≥ 500 lines **or** ≥ 20 files | calculated |

**Missing data is "unknown", never zero.** If reviews were never loaded,
there is no "no reviews" flag.

**What it deliberately doesn't claim:**

- **"Approved" ≠ ready to merge.** CI, conflicts and branch protection aren't
  checked, and the reason says so.
- Nothing about code quality or security.

**Computed when read:** `GET /prs` runs `analyze` for each PR with the
current time, then applies your filters and sort.

### 19.3 In the dashboard

- **Tiles:** counts for open, awaiting review, stale, large and drafts, plus
  "N/M size & reviews loaded". Clicking a tile filters by it.
- **Each row:**
  - status;
  - "opened N d ago / updated N d ago";
  - a size badge with +/− lines;
  - the review summary;
  - **Why these insights?**, listing each insight with its GitHub /
    calculated tag.

[REPORTED, final verification] For psf/requests (63 PRs): 6 open, 23 merged,
34 closed, 1 awaiting review, 1 with changes requested.

### Checkpoint 19

1. What size is a PR with +450 / −60 lines? (510 lines → **L**, and also
   "large".)
2. Why is a closed PR never "stale"?
3. Which data needs the **Load size & reviews** button?

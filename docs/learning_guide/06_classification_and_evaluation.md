# Part 6a: Intelligence Features: Classification and Evaluation

[← Part 5](05_database.md) · [Index](00_INDEX.md) · Next: [Part 6b: Priority, duplicates, PRs](07_priority_duplicates_pr_insights.md)

---

## Chapter 14: Rule-based classification

### 14.1 What classification means here

**Classification** means putting each item into exactly one of a fixed set of
groups. TriageBot's groups (`app/triage.py:CATEGORIES`):

**`bug`, `feature`, `documentation`, `question`**

### 14.2 Matching words properly (`app/matching.py`)

Before scoring, text is lower-cased (`normalize`), and HTML comments (issue
template instructions) are removed from bodies (`clean_body`). Keyword
matching is careful:

- **Whole words only:** `auth` does not match "author"; `500` does not match
  "1500ms".
- **Simple word endings are allowed:** `crash` matches crashes, crashed and
  crashing. This comes from the regular expression in `_pattern`.
- **Negation:** if a keyword is preceded by *not / no / never / without /
  non / isn't …* (optionally with a filler word such as "a" or "very"), it is
  ignored and reported. Example: "not a bug".

### 14.3 How the score is built (`app/triage.py:_classify_category`)

Each category has a keyword list in `CATEGORY_KEYWORDS`; for example, `bug`
includes "crash", "error", "traceback" and "500".

| Evidence | Points |
|---|---|
| Keyword in the **title** | +2 (`TITLE_WEIGHT`) |
| Keyword in the **body** | +1 each, at most **+2** per category (`BODY_CAP`), so a long body can't outweigh the title |
| A matching **GitHub label** (`CATEGORY_LABELS`, e.g. `bug`, `enhancement`, `docs`) | +3 (`LABEL_WEIGHT`) |
| A **PR** title starting with `feat:` / `fix:` / `docs:` | +3 |
| A title ending with `?` | +1 to question |

**How the winner is chosen:**

- The highest score wins.
- **Ties** go to the category with more *title* points, then to the order
  bug → feature → documentation → question.
- **Confidence (rules):** a tie gives `low`. A best score ≥ 3 *and* a lead of
  ≥ 2 gives `high`. Anything else gives `medium`.
- **No signals at all:** an issue defaults to `question`, a PR to `feature`,
  both with `low` confidence.
- **Dependency-bump PRs** ("Bump foo from 1.2 to 1.3"): the body (upstream
  release notes) is ignored.

### 14.4 Real examples [VERIFIED 2026-10-03: output of `classify_item`, illustrative inputs]

| Title (body / labels) | Result | Reasons (shortened) |
|---|---|---|
| "App crashes on startup" (body: "Traceback in the log") | **bug**, high | title 'crash' +2; body 'traceback' +1 → bug=3 |
| "How do I configure a proxy?" | **question**, high | 'how do i' +2; ends with '?' +1 |
| "Fix typo in README" | **documentation**, high | 'fix' +2 bug; 'readme' +2 and 'typo' +2 documentation → documentation=4 beats bug=2 |
| "This is not a bug, add dark mode option" | **feature**, high | 'add' +2, 'option' +2; **ignored negated 'bug'** |
| "Login page" | **question**, low | no category signals → default |
| PR "feat: add CSV export" | **feature**, high | 'add' +2; prefix 'feat:' +3 |
| "Something odd" (label `bug`) | **bug**, high | GitHub label 'bug' +3 |

**Limitations of the rules:**

- English only.
- No understanding of meaning ("the app is slow" vs "make the app faster").
- Keyword lists are hand-written.
- **Measured:** rules reach macro-F1 **0.527** on the model's test split,
  against the ML model's 0.766 (Chapter 16) [REPORTED].

### Checkpoint 14

1. Why is the body capped at +2 per category?
2. What does "ignored negated 'bug'" mean?
3. What category does an issue with no signals get?

---

## Chapter 15: Machine-learning classification

### 15.1 Machine learning from zero

- **Machine learning (ML):** instead of writing rules by hand, you show a
  program many examples and it **learns** a rule from them.
- **Supervised learning:** every example comes with the right answer, its
  **label**. Here:
  - examples = GitHub issues (title + body);
  - labels = the category a **maintainer** gave them on GitHub.
- **Dataset:** the collection of examples. TriageBot's model used **3,706
  issues from 10 projects** (pandas, pytest, terminal, TypeScript, poetry,
  pip, black, tauri, pydantic, deno) [REPORTED, `models/EVALUATION.md`]. The
  list of issue IDs is in `data/dataset_manifest.csv`; the text is not
  committed.
- **Training:** the model adjusts its internal numbers until its guesses
  match the labels.
- **Testing:** measuring the model on examples it **never** saw during
  training. Here: **2,964 training issues / 742 test issues**.

### 15.2 Why text must become numbers: TF-IDF

Models do arithmetic, so text has to become a list of numbers (a **vector**).
**TF-IDF** gives each word a weight.

- **TF (term frequency):** how often the word appears in *this* issue.
- **IDF (inverse document frequency):** how *rare* the word is across all
  issues. Common words ("the", "is") get a low IDF; distinctive words
  ("traceback") get a high one.
- **TF × IDF** = weight: high for words that are frequent here but rare
  overall.

**Illustrative example** (made-up numbers, not project output): across 1,000
issues, "the" appears in 990 and "traceback" in 50.

| Word | TF in issue A | IDF | Result |
|---|---|---|---|
| "the" | 5 times | tiny, because almost every issue has it | low weight |
| "traceback" | 2 times | large, because few issues have it | high weight |

The vector for issue A then mostly "points towards" `traceback`.

**The project's real settings** (`app/ml/train.py:VECTORIZER_PARAMS`):

- single words *and* two-word phrases (`ngram_range=(1, 2)`);
- ignore terms seen in only one issue (`min_df=2`);
- ignore terms in more than 90% of issues (`max_df=0.9`);
- `sublinear_tf`;
- at most 50,000 terms.

The fitted vocabulary has **47,128** terms.

### 15.3 Logistic regression

- **What it does:** for each category it learns one **weight per term**.
  "traceback" gets a strong positive weight for `bug`; "docs" a strong
  positive weight for `documentation`.
- **Scoring:** for an issue, multiply each term's TF-IDF value by its
  weight, add them up per category, then turn the four totals into
  probabilities that sum to 1 with **softmax**.
- **Training details:**
  - `class_weight="balanced"`, so smaller classes count equally;
  - C = 4.0, the strength of the "don't overfit" setting, chosen by 5-fold
    cross-validation on the training data only.

### 15.4 From text to prediction: the real code path

**1. `app/ml/preprocess.py:build_text`** cleans and combines the text:

- the title appears **twice** (more weight);
- HTML comments are removed;
- code blocks become `codeblock`, URLs become `urltoken`;
- everything is lower-cased and capped at 6,000 characters.

[VERIFIED 2026-10-03] `build_text("Docs: install guide is outdated", "See https://example.com\n```\npip install x\n```\n<!-- template -->")`
returns:

```
'docs: install guide is outdated docs: install guide is outdated see urltoken codeblock'
```

**2. `app/ml/model.py:IssueClassifier.predict`**:

- TF-IDF vector → `coef` matrix → softmax → probabilities;
- the **explanation** = each present term's TF-IDF value × its
  class-centred weight, top 5 positives.

**3. `app/ml/service.py:classify`** decides **who** decides, in this order:

| Order | Condition | Category source |
|---|---|---|
| 1 | The item is a **PR** | Rules (the model was trained on issues only) |
| 2 | No usable model (disabled, missing, bad checksum) | Rules, with the reason noted |
| 3 | **Exactly one maintainer category label** (e.g. `bug`) | `github_label`: the human decision wins |
| 4 | ML probability **≥ 0.5** (`TRIAGEBOT_ML_THRESHOLD`) | `ml`. Confidence label: ≥ 0.8 high, ≥ 0.6 medium, else low |
| 5 | Otherwise (unsure, or prediction raised an error) | Rules, with "ML was unsure (…)" noted |

**Real decisions** [VERIFIED 2026-10-03, shipped model, illustrative inputs]:

| Input | Decision | Shown reason |
|---|---|---|
| "Docs: install guide is outdated" | **documentation** [ml] 96% | "words pointing to documentation: 'docs' (+1.47), 'page' (+0.37), …" |
| "How do I configure a proxy?" | **documentation** [ml] 53%, confidence low | ⚠ The *rules* would say **question**; the model is just over 50% and **wrong**. A real example of why predictions are suggestions |
| "Something odd" + label `bug` | **bug** [github_label] | "maintainer label 'bug' -> bug" |
| PR "fix: handle empty input" | **bug** [rules] | "rule engine used: the ML model is trained on issues only" |

### 15.5 Safe model storage and failure handling

**How it is saved** (`IssueClassifier.save`):

- **`.npz`** holds plain number and string arrays (vocabulary, IDF values,
  weights, classes).
- **`.json`** holds the settings, the metrics and the **SHA-256 checksum** of
  the `.npz` file.

**How it is loaded** (`IssueClassifier.load`):

- checks the format version and the **preprocessing version**, so a model
  built with different text cleaning is refused;
- recomputes the SHA-256 and compares it;
- calls `np.load(..., allow_pickle=False)`.

**Why no pickle:** Python's `pickle` format can run arbitrary code while
loading. A tampered model file could then take over your computer. Plain
arrays plus a checksum avoid that.

**On any failure** (missing file, bad JSON, checksum mismatch):

- `service.load_model` returns `(None, reason)`;
- every item is classified by the rules, with the reason in the
  explanation;
- `GET /model` shows `"available": false` and the reason.

**Caching:** `_load` is cached by path and file-modification time, so a
retrained model is picked up without restarting.

**Why predictions aren't truth:**

- Roughly 1 in 4 test issues is misclassified.
- The model sees words, not meaning.
- It was trained on 10 mostly Python and developer-tool projects.

### Checkpoint 15

1. Why does `build_text` include the title twice?
2. List the decision order in `service.classify`.
3. Why is `allow_pickle=False` a security feature?

---

## Chapter 16: Understanding model evaluation

### 16.1 Metric vocabulary (with one tiny example)

**Illustrative example.** 10 issues are truly `bug`. The model calls 8
issues `bug`, and 6 of those really are bugs.

| Term | Meaning | In the example |
|---|---|---|
| **Accuracy** | Share of all predictions that are right | — |
| **Precision** (for bug) | Of the items *called* bug, how many are bugs | 6/8 = 0.75 |
| **Recall** (for bug) | Of the *real* bugs, how many were found | 6/10 = 0.60 |
| **F1** | The balance of precision and recall: 2·P·R/(P+R) | 0.667 |
| **Macro-F1** | The average F1 over the 4 classes, each counting equally | — |
| **Confusion matrix** | A table of true class (rows) vs predicted class (columns) | — |
| **Train/test split** | Learn on one part; measure once on the untouched rest | — |
| **Cross-validation** | Split the *training* data into 5 parts and rotate, to choose settings without touching the test set | — |
| **Held-out repositories** | Test on *projects* the model never saw. Stricter, because each project has its own writing style | — |
| **Data leakage** | Test information sneaking into training, which makes scores look better than reality | — |
| **Confidence threshold / coverage** | Only trust the model above a probability. Coverage = the share of items it decides | — |
| **Baseline** | A simple reference to beat (here: the keyword rules) | — |

### 16.2 The actual results

**[REPORTED in `models/EVALUATION.md` and generated by
`scripts/evaluate_category_generalization.py`. Retraining reproduced
identical weights on 2026-10-02; rerun the evaluator after fetching the
ignored external dataset to regenerate the generalisation results.]**

| Evaluation | Data | ML macro-F1 | Rules macro-F1 |
|---|---|---|---|
| Held-out test split | 742 issues, same 10 projects | **0.766** (accuracy 0.768) | 0.527 |
| Leave-one-repository-out | Train on 9 projects, test on the 10th; average of 10 | **0.658** (range 0.574 deno – 0.838 pandas) | 0.513 |
| External test (Phase 6) | 1,462 issues from 8 **new** projects (uv, jupyterlab, matplotlib, numpy, mypy, scikit-learn, sphinx, streamlit) | **0.715** (accuracy 0.722) | 0.593 |

**Per class (test split):**

| Class | F1 |
|---|---|
| bug | 0.776 |
| feature | 0.827 |
| documentation | 0.816 |
| **question** | **0.645**, the weakest, most often confused with bug |

**At threshold 0.5** (test split): the model decides **73.3%** of issues,
with **0.858** accuracy on those. On the external set, the ML-then-rules
pipeline reached 0.720 accuracy, about the same as ML alone; higher
thresholds were worse, so 0.5 was kept.

**How leakage was prevented:**

- exact duplicate texts were removed **before** splitting;
- C was chosen by cross-validation on the training data only;
- the test split was scored once.

[REPORTED] An audit found only 1 of 742 test issues with a near-duplicate
(cosine ≥ 0.9) in training.

**Why unseen repositories matter:** in real use you import *new*
repositories, so 0.658–0.715 is the realistic expectation, not 0.766.

**Weaknesses:**

- *question* recall is about 0.5 on the external set;
- repository-specific words (project names, version numbers, template
  phrases) appear among the top features.

**A lesson from Phase 6:** a "template cleanup" change looked better in
leave-one-repository-out (+0.019), but it was worse on the fresh external set
(−0.014), so it was **not** shipped.

### 16.3 Reproducing the evaluations yourself

⚠ The training command below **overwrites `models/`** by default. Use
`--out` and `--report` to write elsewhere.

```bash
# 1. Data: fetched from GitHub's search API (cached, resumable; ~40 requests)     [CHANGES STATE: data/raw/, data/issues.jsonl]
$ python -m scripts.fetch_dataset

# 2. Retrain into a SCRATCH folder so the shipped model is untouched              [CHANGES STATE: /tmp files only]
$ python -m scripts.train_classifier --out /tmp/tb_model/issue_classifier --report /tmp/tb_model/EVALUATION.md
#    prints e.g. "best C = 4.0", "test accuracy 0.768, macro-F1 0.766", "leave-one-repo-out mean macro-F1 0.658"

# 3. External generalisation test (needs data/category_external.jsonl)
$ python -m scripts.fetch_category_external
$ python -m scripts.evaluate_category_generalization --out /tmp/CATEGORY_GENERALIZATION.md
```

- Options are confirmed with `--help` [VERIFIED 2026-10-03].
- Running these needs the git-ignored datasets, which a fresh clone does not
  have, so the first step fetches them.
- GitHub issues change over time, so a fresh fetch can differ slightly from
  the recorded snapshot.

### Checkpoint 16

1. Why is macro-F1 used instead of accuracy alone?
2. Which number best predicts performance on a repository you import
   tomorrow?
3. Why must the test set be scored only once?

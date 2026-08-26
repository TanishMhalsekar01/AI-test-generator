# AI-Powered Test Generation & Quality Assurance Framework

An AI tool that reads a Python function, automatically generates its unit tests
(including edge cases and boundary conditions), runs them for real, and reports
coverage and potential bugs.

Built for [Hackathon Name] using **IBM Bob** as the primary development environment.

---

## The Problem

Writing comprehensive unit tests is time-consuming and error-prone. Developers
often test only the "happy path," forgetting edge cases like boundary values,
invalid inputs, and exception handling. This is especially true for students
and small teams without dedicated QA support, leading to bugs that surface
only after deployment.

## Our Solution

This tool automates test generation using a four-stage pipeline. Critically,
the AI never sees raw source code directly -- it only receives **structured
facts extracted via static analysis** (conditions, exceptions, return values,
risk flags). This hybrid approach reduces hallucination and produces more
grounded, useful tests than asking an LLM to reason about raw code alone.

We deliberately scoped this build to two core features rather than the full
multi-module vision common in test-generation tools (see "Future Work" below):

1. **Test Case Generation** -- functional, boundary, and invalid-input test
   cases for a given function
2. **Coverage + Bug Flagging** -- run the generated tests for real, measure
   coverage %, and surface risky code patterns

---

## How It Works

| Stage | File | What it does |
|---|---|---|
| 1. Upload | `main.py` | Receives an uploaded `.py` file via a FastAPI endpoint |
| 2. Parse | `parser.py` | Uses Python's `ast` module to extract function signatures, conditions, raised exceptions, return values, and caught exceptions (including flagging risky bare `except:` clauses) |
| 3. Generate | `llm.py` | Sends the *structured* function info (never raw code) to an LLM, which generates a complete pytest test suite covering normal, boundary, invalid, and exception cases |
| 4. Run + Report | `runner.py` | Writes the function and generated tests to an isolated temp directory, actually executes them with `pytest` + `coverage.py`, and returns real pass/fail counts and coverage % |

### Worked example

**Input function:**
```python
def calculate_discount(price, discount_percent):
    if discount_percent > 100:
        raise ValueError("Discount cannot exceed 100%")
    final_price = price - (price * discount_percent / 100)
    return final_price
```

**Extracted structure (sent to the LLM instead of raw code):**
```
Function: calculate_discount
Parameters: price, discount_percent
Condition found: discount_percent > 100 -> raises ValueError
Flag: no validation found for parameter 'price'
```

**Result after running the generated tests:**
```
Tests generated: 8
Tests passed: 8
Coverage: 100%
```

---

## Tech Stack

- **Backend:** Python, FastAPI
- **Parsing:** Python's built-in `ast` module + lightweight custom static analysis
- **AI layer:** LLM API (Llama 3.3 70B via Groq) -- structured-prompt based test generation
- **Test execution:** pytest + pytest-cov
- **Development environment:** IBM Bob (see below)

---

## How IBM Bob Was Used

IBM Bob was our primary development environment throughout this build. We used
it as an active collaborator rather than a black box -- scoping tasks precisely,
reviewing every generated change, and manually verifying behavior at each stage.

- **Scaffolding:** Bob generated the initial structure for our parser, FastAPI
  backend, and test-runner from detailed, scoped prompts.
- **Iterative debugging:** Bob's integrated terminal and agent chat were used
  to diagnose real issues (environment setup, import errors, API integration
  bugs) as they came up.
- **Feature extension:** When we found our parser couldn't detect `try`/`except`
  blocks, we used Bob to add that capability, then validated it ourselves with
  targeted test cases before accepting the change.
- **A real lesson learned:** In several cases, Bob's automated edits altered
  code outside the requested scope (e.g. renaming a core function during an
  unrelated refactor). We caught this by manually re-verifying behavior after
  every change rather than assuming generated code was correct -- a discipline
  that caught multiple real bugs during development.

Our workflow throughout: **scope a task precisely -> review the diff -> manually
test actual behavior -> only then move forward.**

---

## Known Limitations / Future Work

Scoped out deliberately for this build, listed here as an honest roadmap:

- Multi-language support (currently Python only)
- API/Swagger/GraphQL test generation module
- Full security testing (SQL injection, XSS, auth bypass)
- GitHub repo ingestion / CI-CD integration
- Team dashboards and multi-project history
- Deeper static analysis (currently a lightweight heuristic + basic checks)

## Setup

```bash
cd backend
pip install -r requirements.txt
export GROQ_API_KEY="your_key_here"   # or set via $env: on Windows
uvicorn main:app --reload
```

Then POST a `.py` file to `http://localhost:8000/analyze`.

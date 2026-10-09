# Hacktoberfest Triage Agent

> An **open-weight** AI agent that triages GitHub issues end-to-end: it reads the
> issue, searches the repository, detects duplicates, and proposes labels/comments —
> with every write gated behind human approval and every failure path degrading to
> `needs_human` instead of guessing.

## What it does

- **Understands** an issue (title, body, author, existing labels) via a local open-weight model
- **Searches** the repository (local clone, substring search — no embeddings, no network)
- **Detects duplicates** by listing existing issues before claiming one
- **Proposes** labels and comments — a human clicks **Approve** before anything hits GitHub
- **Degrades honestly**: bad model output, dead backend, rate limits, tool crashes and
  step-budget exhaustion all end in a structured `needs_human` result, never a hallucinated verdict

## Architecture

```
Browser UI (static/index.html)
   │ POST /api/triage            POST /api/approve
   ▼                             ▼
app.py (Flask) ──────────► agent.py  ReAct loop, max 6 steps
                              │            │
                              ▼            ▼
                          llm.py       tools.py
                   OpenAI-compatible   get_issue · list_issues · search_repo ·
                   client + JSON       read_file · propose_label · propose_comment
                   repair retry              │
                                             ▼
                              GitHub API + local repo clone
                              writes only after human approval
```

## How to run

```bash
git clone <this repo> && cd triage-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Serve an open-weight model locally (pick ONE backend, see table below)
ollama pull qwen2.5:7b-instruct && ollama serve &

# 2. Configure the demo repo (seed 3-5 realistic issues in it first!)
export GITHUB_TOKEN=ghp_...        # repo scope; NEVER commit this
export DEMO_REPO=youruser/triage-demo

# 3. Start the app
python app.py                      # → http://localhost:8000
```

Try it: open `http://localhost:8000`, enter a seeded issue number, hit **Triage issue →**.

### Model backends (env vars only — no code changes)

| Backend | `LLM_BASE_URL` | Offline? |
|---|---|---|
| Ollama (default) | `http://localhost:11434/v1` | ✅ |
| llama.cpp `llama-server` | `http://localhost:8080/v1` | ✅ |
| Hosted open-weight (Groq example) | `https://api.groq.com/openai/v1` + `LLM_API_KEY` | ❌ needs Wi-Fi |

⚠️ Only **open-weight** models qualify for this track (Qwen, Llama, DeepSeek, Gemma…).

## Model & dependencies

- **Model:** `qwen2.5:7b-instruct` (Apache 2.0) served locally via Ollama; swappable through `LLM_*` env vars
- **Dependencies:** Python 3.11+, `flask`, `requests`, `openai` (see `requirements.txt`)
- **No** orchestration frameworks: the agent loop in `agent.py` is ~100 lines of original code

## Failure handling (the feature judges ask about)

| # | Failure | Behavior |
|---|---|---|
| 1 | Model returns invalid JSON | One repair retry (error echoed back) → then `needs_human` |
| 2 | Unknown tool / bad arguments | Error returned as an observation; the agent adapts |
| 3 | Tool crashes | Caught, reported as observation, 2 retries max |
| 4 | Tool hangs | 15s timeout enforced per call |
| 5 | GitHub rate limit / no Wi-Fi | Degrades to local clone data; continues working |
| 6 | Structurally invalid verdict | Rejected by `validate_verdict` → `needs_human`, never shown as confident |
| 7 | LLM backend unreachable | `needs_human: "LLM backend unreachable"` — no fabricated verdict |
| 8 | Step budget exhausted (6 steps) | `needs_human` — bounded, predictable demos |

Plus: **the model can never write to GitHub.** It can only *propose* a label/comment;
`approve()` is reachable exclusively from the human's browser.

## Demo script (3 minutes)

1. Triage a messy seeded issue → live step log → validated verdict
2. Triage the seeded duplicate → `duplicate_of: #N` with confidence
3. Stop the LLM backend mid-demo → show `needs_human` degradation instead of a guess
4. Approve a proposed label in the UI → label appears on GitHub

## Hack Day build log

Built by 
1.Divyashree.B
2.Vinay.R
3.Nithin.M
4.Rakshitha.K.C

On 9/10/26
|---|---|---|
|  |  |  |

### Implemented

Scaffolding done: architecture, agent loop, tool registry, all 8 failure paths,
approval-gated writes, UI, README.
Event-time work marked `TODO(hack-day)` in the code:
- [ ] Tune `SYSTEM_PROMPT` against your seeded issues (`agent.py`)
- [ ] Validate `duplicate_of` against real issue numbers (`validate_verdict`)
- [ ] Seed 3–5 realistic issues in your `DEMO_REPO`
- [ ] End-to-end run with your chosen model + backend
- [ ] Demo GIF, final README polish, commit history during the event

## Limitations

- Substring search only (no semantic/embedding search) — misses paraphrased duplicates
- Works best on English issues; OCR/image understanding is out of scope for this track
- 7B-class models occasionally produce borderline verdicts — hence strict validation
  and the `needs_human` escape hatch
- Unauthenticated GitHub API = 60 requests/hour; always set `GITHUB_TOKEN`

## Responsible use

Credentials are read from environment variables only and scrubbed from all error
output. No private data is stored. Nothing is written to GitHub without explicit
human approval.

## License

[MIT](LICENSE)

"""ReAct-style orchestrator: plan -> tool -> observe -> repeat -> verdict.

Hard limits keep this hack-day sized: MAX_STEPS iterations, no cross-issue
memory, and every exit path is either a *validated* verdict or needs_human.
"""

from __future__ import annotations

import json
import os

import llm
from tools import ALLOWED_LABELS, execute_tool, get_issue, setup

MAX_STEPS = int(os.environ.get("MAX_STEPS", "6"))
SEVERITIES = ("low", "medium", "high")

# TODO(hack-day): tune this prompt on your seeded issues — this is event-time work.
SYSTEM_PROMPT = """You are a GitHub issue triage agent for an open-source repository.

Each turn you output ONE JSON object in exactly one of these shapes:

Action:
{{"thought": "<your reasoning>", "tool": "<name>", "args": {{...}}}}

Final verdict — only once you have enough evidence:
{{"thought": "...", "tool": null, "verdict": {{
  "labels": [],              // subset of: __LABELS__
  "severity": "low|medium|high",
  "summary": "<one clear paragraph>",
  "next_steps": ["...", "..."],
  "duplicate_of": null,      // or an issue number from list_issues
  "confidence": 0.0          // 0.0 - 1.0
}}}}

Available tools (exact signatures — use only these parameters):
__TOOLS__

Rules:
- You have at most __MAX_STEPS__ steps. Use list_issues before claiming a duplicate;
  use search_repo/read_file when the issue references code.
- propose_label / propose_comment only CREATE A PROPOSAL. A human approves it.
  They do not apply anything by themselves.
- Never invent file paths, issue numbers, or facts you did not observe.
- If evidence is weak, lower confidence and add a next_steps item asking for human review.
"""


def _system_prompt() -> str:
    import inspect  # deferred: keeps module import side-effect free
    lines = []
    for name, fn in sorted(__import__("tools").TOOLS.items()):
        doc = (inspect.getdoc(fn) or "").split("\n")[0]
        lines.append(f"- {name}{inspect.signature(fn, eval_str=True)} — {doc}")
    return (
        SYSTEM_PROMPT
        .replace("__LABELS__", ", ".join(ALLOWED_LABELS))
        .replace("__TOOLS__", "\n".join(lines))  # dynamic: prompt never drifts from registry
        .replace("__MAX_STEPS__", str(MAX_STEPS))
    )


def validate_verdict(raw) -> dict | None:
    """Failure path #6: structurally invalid / out-of-range verdicts are
    rejected outright — a bad verdict becomes needs_human, never a confident
    wrong answer on stage.
    """
    # TODO(hack-day): verify duplicate_of exists in list_issues() during the event.
    if not isinstance(raw, dict):
        return None
    labels = raw.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(l, str) and l in ALLOWED_LABELS for l in labels
    ):
        return None
    if raw.get("severity") not in SEVERITIES:
        return None
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    steps = raw.get("next_steps", [])
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        return None
    dup = raw.get("duplicate_of")
    if dup is not None and not isinstance(dup, int):
        return None
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return {
        "labels": labels,
        "severity": raw.get("severity"),
        "summary": summary.strip(),
        "next_steps": steps,
        "duplicate_of": dup,
        "confidence": round(confidence, 2),
    }


def _needs_human(reason: str, log: list, raw=None) -> dict:
    out = {"needs_human": True, "reason": reason, "log": log}
    if raw is not None:
        out["rejected_verdict"] = str(raw)[:500]
    return out


def run(issue_number: int) -> dict:
    """Triage one issue. Always returns a dict:
    {needs_human: False, verdict, log} or {needs_human: True, reason, log}.
    """
    log: list[dict] = []

    setup_note = setup()
    if "error" in setup_note:
        log.append({"step": 0, "thought": "environment", "tool": "setup",
                    "observation": setup_note["error"]})

    issue = get_issue(issue_number)
    if "error" in issue:
        return _needs_human(f"could not load issue #{issue_number}: {issue['error']}", log)

    last_error_key = None
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": "Triage this issue:\n" + json.dumps(issue, indent=2)[:4000]},
    ]

    for step in range(1, MAX_STEPS + 1):
        resp = llm.chat_json(messages)
        if resp is None:
            return _needs_human("model output unparseable after repair retry", log)
        if "__transport_error__" in resp:
            # Failure path: LLM backend down / venue Wi-Fi died.
            return _needs_human("LLM backend unreachable: " + resp["__transport_error__"], log)

        tool_name = resp.get("tool")
        if not tool_name and "verdict" not in resp:
            # Protocol violation: response has neither "tool" nor "verdict"
            # (weak models sometimes return only {"thought": ...}).
            # Feed the error back and let the model correct itself
            # — recovery within the step budget beats an instant needs_human.
            observation = ("ERROR: response contained neither 'tool' nor 'verdict'. "
                           "Asked the model to correct its output.")
            log.append({"step": step, "thought": str(resp.get("thought", ""))[:400],
                        "tool": "(protocol-violation)", "args": {}, "observation": observation})
            messages += [
                {"role": "assistant", "content": json.dumps(resp)},
                {"role": "user", "content": (
                    "Invalid response: output either an action "
                    "{\"thought\": ..., \"tool\": \"name\", \"args\": {...}} "
                    "or a final verdict "
                    "{\"thought\": ..., \"tool\": null, \"verdict\": {...}}."
                )},
            ]
            continue
        if tool_name:
            args = resp.get("args") or {}
            call_key = (str(tool_name), json.dumps(args, sort_keys=True, default=str))
            obs = execute_tool(str(tool_name), args)
            # Anti-loop: an identical call that already failed will fail again.
            if call_key == last_error_key and str(obs).startswith("ERROR"):
                obs = (str(obs) + " [REPEATED IDENTICAL CALL — it failed before too. "
                       "Change approach, or output a final verdict now.]")
            last_error_key = call_key if str(obs).startswith("ERROR") else None
            log.append({
                "step": step,
                "thought": str(resp.get("thought", ""))[:400],
                "tool": str(tool_name),
                "args": args if isinstance(args, dict) else {},
                "observation": str(obs)[:600],
            })
            messages += [
                {"role": "assistant", "content": json.dumps(resp)},
                {"role": "user", "content": "Observation: " + json.dumps(obs)[:1500]},
            ]
            continue

        verdict = validate_verdict(resp.get("verdict"))
        if verdict is None:
            return _needs_human("final verdict failed validation", log, raw=resp.get("verdict"))
        return {"issue": issue_number, "needs_human": False, "verdict": verdict, "log": log}

    return _needs_human(f"step budget ({MAX_STEPS}) exhausted without a verdict", log)

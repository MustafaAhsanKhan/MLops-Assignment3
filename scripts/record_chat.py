"""Send one message to the running agent API and append the exchange to a Markdown transcript.

    python scripts/record_chat.py docs/conversations/01-provenance.md "Which dataset ...?"
    python scripts/record_chat.py FILE "yes" --session ID --confirm ACTION_ID

Prints the session id and any pending action ids so a confirmation can follow.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import httpx

p = argparse.ArgumentParser()
p.add_argument("file", type=Path)
p.add_argument("message")
p.add_argument("--session")
p.add_argument("--confirm", nargs="*", default=[])
p.add_argument("--url", default="http://127.0.0.1:8000/chat")
a = p.parse_args()

body = {"message": a.message, "session_id": a.session, "confirm_action_ids": a.confirm}
r = httpx.post(a.url, json=body, timeout=600)
r.raise_for_status()
d = r.json()

lines = [f"### User ({datetime.now().isoformat(timespec='seconds')})", ""]
if a.confirm:
    lines += [f"_Confirmed pending action(s): {', '.join(f'`{c}`' for c in a.confirm)}_", ""]
lines += [f"> {a.message}", "", "**Tool calls** (executed by the agent, in order):", "",
          "| # | Tool | Arguments | Status | ms | Error |", "|---|---|---|---|---|---|"]
for i, t in enumerate(d["tool_calls"], 1):
    lines.append(f"| {i} | `{t['tool']}` | `{json.dumps(t['args'])}` | {t['status']} | "
                 f"{t['duration_ms']:.0f} | {(t['error'] or '').replace('|', '/')[:120]} |")
if not d["tool_calls"]:
    lines.append("| – | (none) | | | | |")
lines += ["", "### Agent", "", d["answer"], ""]
if d["pending_actions"]:
    lines += ["**Pending actions (not executed until the user confirms):**", ""]
    lines += [f"- `{x['action_id']}`: `{x['tool']}({json.dumps(x['args'])})`" for x in d["pending_actions"]]
    lines.append("")
lines += [f"<sub>request_id `{d['request_id']}` · session `{d['session_id']}`</sub>", "", "---", ""]

a.file.parent.mkdir(parents=True, exist_ok=True)
with a.file.open("a") as f:
    f.write("\n".join(lines))
print(d["answer"])
print("\nTOOLS:", [(t["tool"], t["status"]) for t in d["tool_calls"]])
print("SESSION:", d["session_id"], "PENDING:", [(x["action_id"], x["tool"], x["args"]) for x in d["pending_actions"]])

"""Terminal chat with the agent.

    python -m agent.main                 # interactive
    python -m agent.main "question"      # one question, then exit

Structured logs go to logs/agent.jsonl. Pending state-changing actions are shown and run only
if you answer y.
"""

import logging
import sys

from agent import config
from agent.orchestrator import Agent

LOG_FILE = config.REPO_ROOT / "logs" / "agent.jsonl"


def show(resp) -> None:
    print(f"\n{resp.answer}\n")
    for t in resp.tool_calls:
        extra = f" - {t.error}" if t.error else ""
        print(f"  · {t.tool}({t.args}) -> {t.status} in {t.duration_ms:.0f}ms{extra}")
    print(f"  [request {resp.request_id}]\n")


def ask(agent: Agent, message: str, session_id: str | None) -> str:
    resp = agent.chat(message, session_id)
    show(resp)
    for action in resp.pending_actions:
        if input(f"Confirm {action.tool}({action.args}) [action {action.action_id}]? [y/N] ").strip().lower() == "y":
            resp = agent.chat("I confirm this action.", resp.session_id, [action.action_id])
            show(resp)
    return resp.session_id


def main() -> None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s", filename=LOG_FILE)
    agent = Agent()
    if len(sys.argv) > 1:
        ask(agent, " ".join(sys.argv[1:]), None)
        return
    print(f"MLOps agent ({agent.model}). Ctrl-D to quit.")
    session_id = None
    while True:
        try:
            message = input("you> ").strip()
        except EOFError:
            break
        if message:
            session_id = ask(agent, message, session_id)


if __name__ == "__main__":
    main()

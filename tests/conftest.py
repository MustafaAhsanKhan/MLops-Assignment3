"""Shared fakes. No test talks to Gemini, MLflow, Airflow, DVC remotes or a cluster."""

import copy
from types import SimpleNamespace

import pytest

from agent.orchestrator import Agent


class Step:
    """Stand-in for an Interactions API step (only what the orchestrator touches)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=True):
        return dict(self.__dict__)


def call(name: str, args: dict | None = None, call_id: str | None = None) -> Step:
    return Step(type="function_call", name=name, arguments=args or {}, id=call_id or f"call-{name}")


class FakeGemini:
    """Replays scripted model turns. A turn is a list of steps (tool calls) or a str (final answer).
    Records the history sent on every request so tests can check what the model was shown."""

    def __init__(self, turns):
        self.turns = iter(turns)
        self.requests: list[list[dict]] = []
        self.interactions = self

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw["input"]))
        turn = next(self.turns)
        if isinstance(turn, str):
            return SimpleNamespace(steps=[Step(type="model_output", content=[{"type": "text", "text": turn}])],
                                   output_text=turn)
        return SimpleNamespace(steps=turn, output_text=None)


def function_results(request: list[dict]) -> list[dict]:
    return [h for h in request if h.get("type") == "function_result"]


@pytest.fixture
def make_agent():
    def make(*turns, max_steps=8):
        fake = FakeGemini(turns)
        return Agent(client=fake, model="fake-model", max_steps=max_steps), fake
    return make

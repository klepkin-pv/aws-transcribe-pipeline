"""Scoring provider interface: a Bedrock-backed LLM + a deterministic fake.

The real provider stays behind the interface, so pipeline tests never touch
an LLM and cost nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ScoringError(Exception):
    pass


class ScoringProvider(Protocol):
    def score(self, transcript: str) -> dict:
        """Return {"score": 0-100, "summary": str} for a transcript."""
        ...


def parse_score(text: str) -> dict:
    """Extract and validate {"score", "summary"} from a model reply."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ScoringError("model reply contains no JSON object")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ScoringError("model reply is not valid JSON") from exc

    score, summary = payload.get("score"), payload.get("summary")
    if not isinstance(score, int) or isinstance(score, bool) or not isinstance(summary, str):
        raise ScoringError("model reply is missing score/summary")
    if not 0 <= score <= 100:
        raise ScoringError(f"score out of range: {score}")
    return {"score": score, "summary": summary}


class BedrockScoringProvider:
    PROMPT = (
        "You are scoring an interview recording transcript. "
        'Reply with strict JSON only: {"score": <0-100>, "summary": "<one sentence>"}.'
        "\n\nTranscript:\n{transcript}"
    )

    def __init__(self, bedrock_client: Any, model_id: str) -> None:
        self._client = bedrock_client
        self._model_id = model_id

    def score(self, transcript: str) -> dict:
        if not self._model_id:
            raise ScoringError("bedrock model id is not configured")
        response = self._client.invoke_model(
            modelId=self._model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 300,
                    "messages": [
                        {
                            "role": "user",
                            "content": self.PROMPT.format(transcript=transcript[:12000]),
                        }
                    ],
                }
            ),
        )
        reply = json.loads(response["body"].read())["content"][0]["text"]
        return parse_score(reply)


class FakeScoringProvider:
    def score(self, transcript: str) -> dict:
        return {"score": 42, "summary": "fake summary"}

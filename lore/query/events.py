"""Progress events from the query agent, for the web UI's live view.

`answer_question` calls `emit` at each real stage when given a hook. The
hook is optional and best-effort: with none, nothing changes; a hook that
raises is logged and ignored, never failing an answer. Payloads are made
JSON-safe here (non-finite scores become None), so every consumer can
serialise them with allow_nan=False.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from typing import Any, Callable

log = logging.getLogger(__name__)

OnEvent = Callable[[dict], None]

# Edges sent per sub-question; the true count travels as `total`. A path
# template can return hundreds of rows, more than a graph view can show.
EDGES_PER_PART_CAP = 150


def json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def emit(on_event: OnEvent | None, type_: str, **data: Any) -> None:
    if on_event is None:
        return
    try:
        on_event({"type": type_, **json_safe(data)})
    except Exception:  # a broken listener must never fail an answer
        log.exception("query event listener failed on %r", type_)


def edge_key(edge) -> str:
    return f"{edge.subject_id}|{edge.predicate}|{edge.object_id}|{edge.chunk_id}"

"""Per-question traces: every model call a question made, in order.

A user's report ("this answer was wrong") must be investigable after the
fact, so each question leaves a `Trace`: the step events the UI showed, and
every model call (stage, model, prompt, reply, latency, ok or error), with
the HTTP attempts each call made (a model that refused and the one that then
answered). The web app stores it in the central `traces` table; the answer
evals write it to a JSONL file. Both capture it the same way, here.

Capture is per request and async-safe: a trace is bound in a ContextVar for
the work of one question (`bound`), and a task started inside it inherits the
binding, so two questions answered at once never share a trace. Building the
query models while a trace is bound wraps them (`traced_models`, called from
`lore.mcp.tools._build_models`); GeminiClient reports each attempt to the
bound trace (`note_attempt`).

Secrets never enter a trace: every prompt, reply and error text is passed
through `lore.auth.redact.redact_keys` (any Google-key-shaped string, and the
bound user's key whatever its shape) while the user's key scope is still
bound, and capped at 64 KB with a marker.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import time
import uuid
from collections.abc import Iterator
from typing import Any

MAX_TEXT_BYTES = 64 * 1024
TRUNCATED = "\n…[truncated: {n} more bytes]"
# Step events: everything the stream shows except the answer itself, which
# the trace keeps once, as `answer`.
_NOT_STEPS = frozenset({"answer"})

_TRACE: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar(
    "lore_trace", default=None)
# The model call in progress in this context, so its HTTP attempts attach to it.
_CALL: contextvars.ContextVar["dict | None"] = contextvars.ContextVar(
    "lore_trace_call", default=None)
_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lore_request_id", default=None)


def new_request_id() -> str:
    return str(uuid.uuid4())


def cap(text: str | None, limit: int = MAX_TEXT_BYTES) -> str | None:
    """`text` cut to `limit` bytes of UTF-8, with a marker saying how much went."""
    if text is None:
        return None
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    kept = raw[:limit].decode("utf-8", errors="ignore")
    return kept + TRUNCATED.format(n=len(raw) - len(kept.encode("utf-8")))


def clean(text: Any) -> str | None:
    """Redacted (no key of any shape) and capped text, or None."""
    if text is None:
        return None
    from lore.auth.redact import redact, redact_keys
    return cap(redact(redact_keys(str(text))))


def scrub(value: Any) -> Any:
    """`value` (JSON-like) with every string redacted (not capped): for the
    step events, the decomposition and the answer."""
    from lore.auth.redact import redact, redact_keys
    if isinstance(value, str):
        return redact(redact_keys(value))
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def _reply_text(reply: Any) -> str:
    dump = getattr(reply, "model_dump_json", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001 -- a trace never fails an answer
            pass
    return str(reply)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@dataclasses.dataclass
class Trace:
    """What one question did. Plain data: `to_record()` is what is stored."""

    request_id: str = dataclasses.field(default_factory=new_request_id)
    calls: list[dict] = dataclasses.field(default_factory=list)
    steps: list[dict] = dataclasses.field(default_factory=list)
    decomposition: dict = dataclasses.field(default_factory=dict)
    started: float = dataclasses.field(default_factory=time.monotonic)
    first_event_s: float | None = None
    answer: dict | None = None
    error: str | None = None
    finished_s: float | None = None
    # Called with (stage, prompt, reply) for every call that answered, with the
    # raw, uncapped values: the local answer evals keep these for analysis.
    on_call: Any = None

    # --- capture ---------------------------------------------------------------

    def step(self, event: dict) -> None:
        """One event the stream showed (any but the answer)."""
        if self.first_event_s is None:
            self.first_event_s = time.monotonic() - self.started
        kind = event.get("type")
        if kind in _NOT_STEPS:
            return
        event = scrub(event)
        if kind in ("parts", "plan", "understood"):
            self.decomposition[kind] = {k: v for k, v in event.items()
                                        if k not in ("type", "t")}
        self.steps.append(event)

    def begin_call(self, stage: str, prompt: str) -> dict:
        call = {"stage": stage, "model": None, "prompt": clean(prompt),
                "reply": None, "latency_ms": None, "ok": None, "error": None,
                "attempts": [], "_t0": time.monotonic()}
        self.calls.append(call)
        return call

    @staticmethod
    def end_call(call: dict, *, reply: Any = None, error: BaseException | None = None,
                 model: str | None = None) -> None:
        t0 = call.pop("_t0", None)
        if t0 is not None:
            call["latency_ms"] = round((time.monotonic() - t0) * 1000)
        ok_attempts = [a["model"] for a in call["attempts"] if a["ok"]]
        call["model"] = ok_attempts[-1] if ok_attempts else model
        if error is None:
            call["ok"] = True
            call["reply"] = clean(_reply_text(reply))
        else:
            call["ok"] = False
            call["error"] = clean(_error_text(error))

    def finish(self, *, answer: dict | None = None, error: str | None = None) -> None:
        self.answer = scrub(answer)
        self.error = clean(error) if error else None
        self.finished_s = time.monotonic() - self.started

    # --- reading ---------------------------------------------------------------

    def model_usage(self) -> dict[str, dict[str, int]]:
        """Per model: HTTP attempts made and how many of them failed."""
        usage: dict[str, dict[str, int]] = {}
        for call in self.calls:
            attempts = call["attempts"] or (
                [{"model": call["model"], "ok": call["ok"]}] if call["model"] else [])
            for attempt in attempts:
                row = usage.setdefault(attempt["model"] or "?",
                                       {"requests": 0, "failed": 0})
                row["requests"] += 1
                row["failed"] += 0 if attempt["ok"] else 1
        return usage

    def timings(self) -> dict:
        per_stage: dict[str, int] = {}
        for call in self.calls:
            per_stage[call["stage"]] = per_stage.get(call["stage"], 0) + (
                call["latency_ms"] or 0)
        ms = (lambda s: None if s is None else round(s * 1000))
        return {"total_ms": ms(self.finished_s), "first_event_ms": ms(self.first_event_s),
                "model_ms_by_stage": per_stage}

    def calls_out(self) -> list[dict]:
        return [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.calls]


@contextlib.contextmanager
def bound(trace: Trace) -> Iterator[Trace]:
    """Bind `trace` (and its request id) for the work inside the block; tasks
    created inside inherit it."""
    token = _TRACE.set(trace)
    rid = _REQUEST_ID.set(trace.request_id)
    try:
        yield trace
    finally:
        _REQUEST_ID.reset(rid)
        _TRACE.reset(token)


def current() -> Trace | None:
    return _TRACE.get()


@contextlib.contextmanager
def request_id_bound(request_id: str) -> Iterator[None]:
    token = _REQUEST_ID.set(request_id)
    try:
        yield
    finally:
        _REQUEST_ID.reset(token)


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def note_attempt(model: str, *, ok: bool, latency_s: float,
                 error: BaseException | None = None) -> None:
    """GeminiClient: one HTTP attempt of the call in progress (nothing when no
    trace is bound)."""
    call = _CALL.get()
    if call is None or _TRACE.get() is None:
        return
    call["attempts"].append({
        "model": model, "ok": ok, "latency_ms": round(latency_s * 1000),
        "error": clean(_error_text(error)) if error is not None else None})


def _wrap(trace: Trace, stage: str, generate, model_name=None):
    if generate is None:
        return None

    async def traced(prompt, schema, *args, **kwargs):
        call = trace.begin_call(stage, prompt)
        token = _CALL.set(call)
        try:
            reply = await generate(prompt, schema, *args, **kwargs)
        except BaseException as exc:
            trace.end_call(call, error=exc, model=_name(model_name))
            raise
        finally:
            _CALL.reset(token)
        trace.end_call(call, reply=reply, model=_name(model_name))
        if trace.on_call is not None:
            trace.on_call(stage, prompt, reply)
        return reply
    return traced


def _name(model_name) -> str | None:
    try:
        return model_name() if model_name else None
    except Exception:  # noqa: BLE001
        return None


STAGES = ("decompose", "select", "synthesize", "check", "write")


def traced_models(models, trace: Trace):
    """The same QueryModels, with every generate call recorded in `trace`."""
    if not dataclasses.is_dataclass(models):
        return models
    names = {"select": getattr(models, "selection_model", None)}
    return dataclasses.replace(models, **{
        stage: _wrap(trace, stage, getattr(models, stage),
                     names.get(stage, getattr(models, "synthesis_model", None)))
        for stage in STAGES})


def maybe_traced(models):
    """`models` wrapped for the bound trace, or unchanged when none is bound."""
    trace = _TRACE.get()
    return models if trace is None else traced_models(models, trace)


def traced_search(search, trace: Trace):
    """`search` (lore_search's signature) with `trace` bound for its run: its
    step events and every model call land in the trace, and an exception is
    recorded (redacted) before it propagates. The one capture point the web
    app and the evals share."""

    async def run(config, question, **kwargs):
        forward = kwargs.pop("on_event", None)

        def tee(event: dict) -> None:
            trace.step({**event, "t": round(time.monotonic() - trace.started, 2)})
            if forward is not None:
                forward(event)

        with bound(trace):
            try:
                result = await search(config, question, on_event=tee, **kwargs)
            except BaseException as exc:
                trace.finish(error=_error_text(exc))
                raise
        if isinstance(result, dict) and "error" in result:
            err = result["error"]
            trace.finish(error=f"{err.get('code')}: {err.get('message')}"
                         if isinstance(err, dict) else str(err))
        else:
            trace.finish(answer=result if isinstance(result, dict) else None)
        return result

    return run


# --- the request id on every log line --------------------------------------------

_LOGGING_INSTALLED = False


def install_request_id_logging() -> None:
    """Every log record made while a request id is bound carries it: as
    `record.request_id`, and at the start of the message (`[req <id>] ...`),
    so any formatter shows it. Installed once per process; chains whatever
    record factory is already there (multi mode's key redaction)."""
    import logging

    global _LOGGING_INSTALLED
    if _LOGGING_INSTALLED:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        rid = _REQUEST_ID.get()
        record.request_id = rid or "-"
        if rid and isinstance(record.msg, str) and not record.msg.startswith("[req "):
            record.msg = f"[req {rid}] {record.msg}"
        return record

    logging.setLogRecordFactory(factory)
    _LOGGING_INSTALLED = True

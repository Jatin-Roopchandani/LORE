import json
from pathlib import Path

import httpx
import pyarrow.parquet as pq

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.inference.gemini import LITE_MODELS, GeminiClient
from lore.orchestrator import jobs
from lore.orchestrator.extract_stage import ExtractStage
from lore.orchestrator.local import GeminiTier, parts_done
from lore.orchestrator.pipeline import run_book
from lore.orchestrator.run import unit_state
from lore.orchestrator.workunit import pack_units

BOOK = "abcdef0123456789"


def _chunks(n):
    return [Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
                  scene_seq=0, seq=i, text=f"text {i}") for i in range(n)]


def _setup(tmp_path: Path, n: int, name: str):
    base = LoreConfig.load()
    config = base.model_copy(update={
        "data_dir": tmp_path / name,
        "extraction_provider": "gemini",
        "extraction": base.extraction.model_copy(update={"work_unit_size": 2}),
        "processing": base.processing.model_copy(update={"answer_reserve": 0}),
    })
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    ingest_book(conn, book_id=BOOK, title="T", author=None, source="upload",
                file_path="/x/b.epub", chunks=_chunks(n))
    return config, conn


class _Embedder:
    async def embed(self, texts, model):
        return [[float(len(t))] * 768 for t in texts]

    async def aclose(self):
        return None


class Server:
    """A fake Gemini: answers `limit` chunk requests, then 429s every model."""

    def __init__(self, limit=None):
        self.limit = limit
        self.answered: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["contents"][0]["parts"][0]["text"]
        if self.limit is not None and len(self.answered) >= self.limit:
            return httpx.Response(429, json={"error": {"message": "RESOURCE_EXHAUSTED"}})
        who = [c.text for c in _chunks(20) if c.text in text][-1]
        self.answered.append(who)
        body = {"entities": [{"name": who, "kind": "Character"}],
                "triplets": [{"subject": who, "predicate": "INTERACTS_WITH",
                              "object": "Watson", "evidence": "e",
                              "confidence": 0.9}]}
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(body)}]}}]})

    def factory(self):
        async def no_sleep(_s):
            return None

        def make(config):
            client = GeminiClient("AIza-not-a-real-key", models=list(LITE_MODELS),
                                  transport=httpx.MockTransport(self.handler),
                                  sleep=no_sleep)
            return GeminiTier(config=config, client=client, embedder=_Embedder())

        return make


def _rows(config, name):
    out = {}
    for unit in pack_units(BOOK, _chunks(5), 2):
        d = config.extractions_dir / BOOK / unit.unit_id
        trip = pq.read_table(d / "triplets.parquet").to_pylist()
        emb = pq.read_table(d / "embeddings.parquet").to_pylist()
        man = json.loads((d / "manifest.json").read_text())
        out[unit.unit_id] = (trip, emb, man["chunks_ok"], man["chunks_dead"],
                             man["model"])
    return out


def test_quota_stop_mid_unit_then_resume_requests_each_chunk_once(tmp_path) -> None:
    config, conn = _setup(tmp_path, 5, "a")
    server = Server(limit=3)  # unit 0 whole, unit 1 half
    stage = ExtractStage(gemini_tier_factory=server.factory())
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == "waiting_quota" and job.stage == "extract"
    assert (job.done_items, job.total_items) == (3, 5)
    assert "extract: 3/5" in job.stopped_reason
    u0, u1, u2 = pack_units(BOOK, _chunks(5), 2)
    dest1 = config.extractions_dir / BOOK / u1.unit_id
    assert parts_done(dest1, u1) == 1
    assert not (dest1 / "triplets.parquet").exists()
    assert unit_state(conn, u1.unit_id) == "failed"

    resumed = Server()
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=resumed.factory())],
                   force=True)
    assert job.state == "done"
    assert sorted(server.answered + resumed.answered) == [f"text {i}" for i in range(5)]
    assert len(resumed.answered) == 2

    # Identical, as rows, to an uninterrupted run.
    config2, conn2 = _setup(tmp_path, 5, "b")
    run_book(conn2, config2, BOOK, [ExtractStage(gemini_tier_factory=Server().factory())])
    assert _rows(config, "a") == _rows(config2, "b")


def test_resume_reports_chunks_already_done_first(tmp_path) -> None:
    config, conn = _setup(tmp_path, 5, "a")
    run_book(conn, config, BOOK,
             [ExtractStage(gemini_tier_factory=Server(limit=3).factory())])
    seen: list[int] = []
    real = jobs.advance

    def spy(c, book, done, **kw):
        seen.append(done)
        return real(c, book, done, **kw)

    jobs.advance = spy
    try:
        run_book(conn, config, BOOK,
                 [ExtractStage(gemini_tier_factory=Server().factory())], force=True)
    finally:
        jobs.advance = real
    assert seen[0] == 3 and seen[-1] == 5


def test_a_legacy_unit_without_parts_is_redone_whole(tmp_path) -> None:
    config, conn = _setup(tmp_path, 2, "a")
    (u,) = pack_units(BOOK, _chunks(2), 2)
    dest = config.extractions_dir / BOOK / u.unit_id
    dest.mkdir(parents=True)
    (dest / "triplets.parquet").write_bytes(b"stale")  # artifacts, no parts
    server = Server()
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=server.factory())])
    assert job.state == "done"
    assert server.answered == ["text 0", "text 1"]
    assert pq.read_table(dest / "triplets.parquet").num_rows == 2


def test_the_stage_keeps_a_runreport_like_summary(tmp_path) -> None:
    config, conn = _setup(tmp_path, 2, "a")
    stage = ExtractStage(gemini_tier_factory=Server().factory())
    run_book(conn, config, BOOK, [stage])
    r = stage.report
    assert (r.units_total, r.units_completed, r.units_failed) == (1, 1, 0)
    assert (r.chunks_ok, r.chunks_dead, r.stopped_reason) == (2, 0, None)


def test_key_rejection_fails_the_job_instead_of_waiting_for_quota(tmp_path) -> None:
    config, conn = _setup(tmp_path, 2, "a")

    class Bad(Server):
        def handler(self, request):
            return httpx.Response(401, json={"error": {"message": "bad key"}})

    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=Bad().factory())])
    assert job.state == "failed"


def test_the_processing_reserve_stops_before_any_request(tmp_path) -> None:
    config, conn = _setup(tmp_path, 2, "a")
    config = config.model_copy(update={
        "processing": config.processing.model_copy(update={"answer_reserve": 10**6})})
    server = Server()
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=server.factory())])
    assert job.state == "waiting_quota"
    assert server.answered == []
    assert "reserve" in job.stopped_reason

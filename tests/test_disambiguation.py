"""The pending-question store, the batch protocol, and the answer cache.

Everything here runs against a stubbed transport or the filesystem. The
batch-mapping tests are the point of the file: a numbered batch whose
answers slip by one silently attaches the wrong merge decision to the
wrong entity, and that failure is invisible in the graph afterwards.
"""
import json
from pathlib import Path

import httpx
import pytest

from lore.inference.gemini import GeminiClient, GeminiQuotaError
from lore.resolve.disambiguation import (
    MAX_BATCH,
    AnswerMismatchError,
    Candidate,
    DisambiguationStore,
    Question,
    ask,
    batch_prompt,
    parse_batch_answers,
    question_key,
)

SECRET = "AIza-not-a-real-key-0000"


def _question(mention: str, *names: str, evidence: str = "ev",
              book: str = "b1") -> Question:
    return Question(
        mention=mention, kind="Character", book_id=book, evidence=evidence,
        candidates=tuple(
            Candidate(id=f"id-{name.lower().replace(' ', '-')}", name=name)
            for name in names
        ),
    )


def _corpus() -> list[Question]:
    """Three questions with pairwise-distinct mentions AND candidates."""
    return [
        _question("Sholto", "Major Sholto", "Thaddeus Sholto"),
        _question("Ferrier", "John Ferrier", "Lucy Ferrier"),
        _question("Smith", "Mordecai Smith", "Mrs. Smith"),
    ]


def _answer_text(pairs: list[tuple[int, str, str]]) -> str:
    return "\n".join(f"{n}. {mention} -> {choice}" for n, mention, choice in pairs)


# --------------------------------------------------------------------------
# Cache key stability


def test_key_is_independent_of_candidate_order() -> None:
    forward = _question("Sholto", "Major Sholto", "Thaddeus Sholto")
    backward = _question("Sholto", "Thaddeus Sholto", "Major Sholto")
    assert question_key(forward) == question_key(backward)
    assert forward.key == backward.key


def test_key_changes_with_mention_kind_evidence_and_candidate_set() -> None:
    base = _question("Sholto", "Major Sholto", "Thaddeus Sholto")
    assert base.key != _question("Sholto", "Major Sholto").key
    assert base.key != _question(
        "Sholto", "Major Sholto", "Thaddeus Sholto", evidence="other"
    ).key
    assert base.key != _question("Smith", "Major Sholto", "Thaddeus Sholto").key
    other_kind = Question(
        mention=base.mention, kind="Place", book_id=base.book_id,
        evidence=base.evidence, candidates=base.candidates,
    )
    assert base.key != other_kind.key


def test_key_is_a_hex_digest_carrying_no_plain_text() -> None:
    key = _question("Sholto", "Major Sholto", "Thaddeus Sholto").key
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)


# --------------------------------------------------------------------------
# The batch prompt


def test_one_prompt_carries_every_question_numbered() -> None:
    questions = _corpus()
    prompt = batch_prompt(questions)
    for index, question in enumerate(questions, start=1):
        assert f"{index}." in prompt
        assert question.mention in prompt
        for candidate in question.candidates:
            assert candidate.name in prompt
    assert "none" in prompt.lower()


def test_prompt_states_the_expected_line_count() -> None:
    assert "3" in batch_prompt(_corpus())


# --------------------------------------------------------------------------
# Answer mapping — the failure mode this task exists to avoid


def test_answers_map_onto_the_right_questions() -> None:
    questions = _corpus()
    answers = parse_batch_answers(questions, _answer_text([
        (1, "Sholto", "Major Sholto"),
        (2, "Ferrier", "Lucy Ferrier"),
        (3, "Smith", "none"),
    ]))
    assert answers[questions[0].key] == "id-major-sholto"
    assert answers[questions[1].key] == "id-lucy-ferrier"
    assert answers[questions[2].key] == ""


def test_answer_lines_out_of_order_are_keyed_by_their_number_not_position() -> None:
    questions = _corpus()
    answers = parse_batch_answers(questions, _answer_text([
        (3, "Smith", "Mrs. Smith"),
        (1, "Sholto", "Major Sholto"),
        (2, "Ferrier", "Lucy Ferrier"),
    ]))
    assert answers[questions[0].key] == "id-major-sholto"
    assert answers[questions[1].key] == "id-lucy-ferrier"
    assert answers[questions[2].key] == "id-mrs.-smith"


def test_a_rotated_answer_set_is_rejected_not_silently_mis_mapped() -> None:
    # The off-by-one. Each line is well-formed and the count is right; only
    # the CONTENT has slipped by one. Echoing the mention and naming the
    # chosen candidate is what makes it detectable.
    questions = _corpus()
    with pytest.raises(AnswerMismatchError):
        parse_batch_answers(questions, _answer_text([
            (1, "Ferrier", "Lucy Ferrier"),
            (2, "Smith", "Mrs. Smith"),
            (3, "Sholto", "Major Sholto"),
        ]))


def test_a_short_answer_set_is_rejected_rather_than_zipped() -> None:
    questions = _corpus()
    with pytest.raises(AnswerMismatchError) as exc:
        parse_batch_answers(questions, _answer_text([
            (1, "Sholto", "Major Sholto"),
            (2, "Ferrier", "Lucy Ferrier"),
        ]))
    assert "3" in str(exc.value)


def test_a_long_answer_set_is_rejected() -> None:
    questions = _corpus()
    with pytest.raises(AnswerMismatchError):
        parse_batch_answers(questions, _answer_text([
            (1, "Sholto", "Major Sholto"),
            (2, "Ferrier", "Lucy Ferrier"),
            (3, "Smith", "none"),
            (4, "Young", "Brigham Young"),
        ]))


def test_a_duplicated_question_number_is_rejected() -> None:
    questions = _corpus()
    with pytest.raises(AnswerMismatchError):
        parse_batch_answers(questions, _answer_text([
            (1, "Sholto", "Major Sholto"),
            (2, "Ferrier", "Lucy Ferrier"),
            (2, "Ferrier", "John Ferrier"),
        ]))


def test_an_answer_naming_a_candidate_that_was_not_offered_is_a_refusal() -> None:
    # The line itself is verified — right number, right mention — so the
    # mapping is sound and only the verdict is unusable. Never guess at an
    # intended candidate: refuse, and leave two nodes a later pass can join.
    questions = _corpus()
    answers = parse_batch_answers(questions, _answer_text([
        (1, "Sholto", "Bartholomew Sholto"),  # never offered
        (2, "Ferrier", "Lucy Ferrier"),
        (3, "Smith", "none"),
    ]))
    assert answers[questions[0].key] == ""
    assert answers[questions[1].key] == "id-lucy-ferrier"


def test_option_numbers_are_accepted_and_map_to_the_listed_order() -> None:
    questions = _corpus()
    answers = parse_batch_answers(questions, _answer_text([
        (1, "Sholto", "1"),
        (2, "Ferrier", "2"),
        (3, "Smith", "none"),
    ]))
    assert answers[questions[0].key] == questions[0].candidates[0].id
    assert answers[questions[1].key] == questions[1].candidates[1].id
    assert answers[questions[2].key] == ""


@pytest.mark.parametrize("decorated", ["2", "2)", "(2)", "2.", "option 2", "#2"])
def test_option_numbers_survive_the_decoration_models_add(decorated: str) -> None:
    questions = _corpus()
    answers = parse_batch_answers(questions, _answer_text([
        (1, "Sholto", decorated),
        (2, "Ferrier", "none"),
        (3, "Smith", "none"),
    ]))
    assert answers[questions[0].key] == questions[0].candidates[1].id


def test_an_option_number_outside_the_offered_range_is_a_refusal() -> None:
    questions = _corpus()
    answers = parse_batch_answers(questions, _answer_text([
        (1, "Sholto", "7"),   # only two options were offered
        (2, "Ferrier", "0"),  # options are 1-based
        (3, "Smith", "none"),
    ]))
    assert answers[questions[0].key] == ""
    assert answers[questions[1].key] == ""


def test_a_verbose_prose_verdict_is_a_refusal_not_a_guess() -> None:
    # gemini-3.5-flash-lite's single miss on the 12-case eval: prose where
    # "none" belonged. Reading an intended candidate out of that would
    # attach the wrong merge to the wrong entity, unrecoverably.
    questions = _corpus()
    answers = parse_batch_answers(questions, _answer_text([
        (1, "Sholto", "Based on the context of Ar"),
        (2, "Ferrier", "I cannot determine this from the sentence given"),
        (3, "Smith", "Mrs. Smith"),
    ]))
    assert answers[questions[0].key] == ""
    assert answers[questions[1].key] == ""
    assert answers[questions[2].key] == "id-mrs.-smith"


def test_an_answer_echoing_the_wrong_mention_is_rejected() -> None:
    questions = _corpus()
    with pytest.raises(AnswerMismatchError):
        parse_batch_answers(questions, _answer_text([
            (1, "Ferrier", "Major Sholto"),
            (2, "Ferrier", "Lucy Ferrier"),
            (3, "Smith", "none"),
        ]))


def test_a_malformed_line_is_rejected() -> None:
    questions = _corpus()
    with pytest.raises(AnswerMismatchError):
        parse_batch_answers(questions, "yes\nno\nmaybe")


def test_parsing_tolerates_markdown_and_stray_prose() -> None:
    questions = _corpus()
    answers = parse_batch_answers(questions, (
        "Here are my answers:\n\n"
        "- **1.** Sholto -> Major Sholto\n"
        "- **2.** Ferrier -> Lucy Ferrier\n"
        "- **3.** Smith -> None\n\n"
        "Let me know if you need anything else.\n"
    ))
    assert answers[questions[0].key] == "id-major-sholto"
    assert answers[questions[2].key] == ""


def test_duplicate_candidate_display_names_are_labelled_apart() -> None:
    # Two distinct nodes may carry the same display name. The prompt must
    # still let an answer name exactly one of them.
    question = Question(
        mention="Smith", kind="Character", book_id="b1", evidence="ev",
        candidates=(Candidate(id="id-a", name="Smith the elder"),
                    Candidate(id="id-b", name="Smith the elder")),
    )
    prompt = batch_prompt([question])
    assert "Smith the elder (2)" in prompt
    answers = parse_batch_answers([question], "1. Smith -> Smith the elder (2)")
    assert answers[question.key] == "id-b"


# --------------------------------------------------------------------------
# The store: pending questions and the answer cache


def test_pending_round_trips_through_a_fresh_store(tmp_path: Path) -> None:
    store = DisambiguationStore(tmp_path / "disambiguation")
    for question in _corpus():
        store.record(question)
    store.flush("b1")

    fresh = DisambiguationStore(tmp_path / "disambiguation")
    pending = fresh.pending()
    assert [q.key for q in pending] == sorted(q.key for q in _corpus())
    assert pending[0].candidates  # names and ids survived the round trip


def test_flush_replaces_a_books_pending_set_and_clears_it_when_empty(
    tmp_path: Path
) -> None:
    directory = tmp_path / "disambiguation"
    store = DisambiguationStore(directory)
    store.record(_question("Sholto", "Major Sholto", "Thaddeus Sholto"))
    store.flush("b1")
    assert len(DisambiguationStore(directory).pending()) == 1

    # A later load of the same book finds nothing ambiguous: its stale
    # questions must not linger and spend quota.
    DisambiguationStore(directory).flush("b1")
    assert DisambiguationStore(directory).pending() == []


def test_pending_is_scoped_per_book(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    first = DisambiguationStore(directory)
    first.record(_question("Sholto", "Major Sholto", "Thaddeus Sholto", book="b1"))
    first.flush("b1")
    second = DisambiguationStore(directory)
    second.record(_question("Ferrier", "John Ferrier", "Lucy Ferrier", book="b2"))
    second.flush("b2")
    assert len(DisambiguationStore(directory).pending()) == 2


def test_answers_round_trip_and_a_hit_needs_no_request(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    question = _question("Sholto", "Major Sholto", "Thaddeus Sholto")
    DisambiguationStore(directory).save_answers({question.key: "id-major-sholto"})

    fresh = DisambiguationStore(directory)
    assert fresh.answer_for(question) == "id-major-sholto"
    assert fresh.unanswered([question]) == []


def test_a_cached_refusal_is_remembered_as_a_refusal(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    question = _question("Sholto", "Major Sholto", "Thaddeus Sholto")
    DisambiguationStore(directory).save_answers({question.key: ""})
    fresh = DisambiguationStore(directory)
    assert fresh.answer_for(question) is None       # no merge
    assert fresh.unanswered([question]) == []       # but never re-asked


def test_an_answer_naming_an_unoffered_candidate_is_ignored(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    question = _question("Sholto", "Major Sholto", "Thaddeus Sholto")
    DisambiguationStore(directory).save_answers({question.key: "id-someone-else"})
    assert DisambiguationStore(directory).answer_for(question) is None


def test_saving_answers_merges_rather_than_replaces(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    first, second = _corpus()[0], _corpus()[1]
    DisambiguationStore(directory).save_answers({first.key: "id-major-sholto"})
    DisambiguationStore(directory).save_answers({second.key: "id-lucy-ferrier"})
    fresh = DisambiguationStore(directory)
    assert fresh.answer_for(first) == "id-major-sholto"
    assert fresh.answer_for(second) == "id-lucy-ferrier"


def test_the_cache_file_never_holds_a_key(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    question = _question("Sholto", "Major Sholto", "Thaddeus Sholto")
    store = DisambiguationStore(directory)
    store.save_answers({question.key: "id-major-sholto"})
    assert SECRET not in (directory / "answers.json").read_text()


@pytest.mark.parametrize("garbage", ["", "{", "null", "[1, 2]", '{"k": 3}'])
def test_a_corrupt_answer_cache_does_not_crash_a_load(
    tmp_path: Path, garbage: str
) -> None:
    directory = tmp_path / "disambiguation"
    directory.mkdir(parents=True)
    (directory / "answers.json").write_text(garbage)
    store = DisambiguationStore(directory)
    question = _question("Sholto", "Major Sholto", "Thaddeus Sholto")
    assert store.answers() == {}
    assert store.answer_for(question) is None
    assert store.unanswered([question]) == [question]
    # And it is repairable: a later save overwrites the garbage.
    store.save_answers({question.key: "id-major-sholto"})
    assert DisambiguationStore(directory).answer_for(question) == "id-major-sholto"


def test_a_corrupt_pending_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    store = DisambiguationStore(directory)
    store.record(_question("Sholto", "Major Sholto", "Thaddeus Sholto", book="b1"))
    store.flush("b1")
    (directory / "pending" / "b2.json").write_text('[{"mention": ')
    assert len(DisambiguationStore(directory).pending()) == 1


def test_concurrent_writers_never_collide_on_a_temp_file(tmp_path: Path) -> None:
    # A shared `answers.json.tmp` looked fine and was not: two writers each
    # created the same temp path and the second os.replace raised
    # FileNotFoundError because the first had already moved it away. Found
    # by probing, not by reading.
    import threading

    directory = tmp_path / "disambiguation"
    directory.mkdir(parents=True)
    failures: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(25):
                DisambiguationStore(directory).save_answers({f"k{n}-{i}": "id-x"})
        except BaseException as exc:  # pragma: no cover - the bug being fixed
            failures.append(exc)

    def reader() -> None:
        for _ in range(200):
            try:
                DisambiguationStore(directory).answers()
            except BaseException as exc:  # pragma: no cover
                failures.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert DisambiguationStore(directory).answers()  # still readable
    assert list(directory.glob("*.tmp")) == [], "no temp file left behind"


def test_answer_cache_survives_a_truncated_write(tmp_path: Path) -> None:
    # A half-written cache must read as empty, never as a wrong answer.
    directory = tmp_path / "disambiguation"
    question = _question("Sholto", "Major Sholto", "Thaddeus Sholto")
    DisambiguationStore(directory).save_answers({question.key: "id-major-sholto"})
    path = directory / "answers.json"
    path.write_text(path.read_text()[: len(path.read_text()) // 2])
    assert DisambiguationStore(directory).answer_for(question) is None


# --------------------------------------------------------------------------
# ask(): the whole corpus in one request


def _stub_client(handler, **kwargs) -> GeminiClient:
    return GeminiClient(SECRET, transport=httpx.MockTransport(handler), **kwargs)


@pytest.mark.asyncio
async def test_seventeen_questions_cost_exactly_one_request() -> None:
    questions = [
        _question(f"Name{i}", f"Alpha {i} Name{i}", f"Beta {i} Name{i}")
        for i in range(17)
    ]
    assert len(questions) <= MAX_BATCH
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        body = "\n".join(
            f"{n}. {q.mention} -> {q.candidates[0].name}"
            for n, q in enumerate(questions, start=1)
        )
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": body}]}}]
        })

    client = _stub_client(handler)
    answers = await ask(client, questions)
    assert len(requests) == 1
    assert client.requests_made == 1
    assert len(answers) == 17
    for question in questions:
        assert answers[question.key] == question.candidates[0].id


@pytest.mark.asyncio
async def test_more_than_a_batch_splits_and_spaces_the_requests() -> None:
    questions = [
        _question(f"Name{i}", f"Alpha {i} Name{i}", f"Beta {i} Name{i}")
        for i in range(MAX_BATCH + 2)
    ]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["contents"][0]["parts"][0]["text"]
        offset = sum(batches)
        size = prompt.count("\n   sentence:")
        batches.append(size)
        body = "\n".join(
            f"{n}. {questions[offset + n - 1].mention} -> "
            f"{questions[offset + n - 1].candidates[0].name}"
            for n in range(1, size + 1)
        )
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": body}]}}]
        })

    client = _stub_client(handler, sleep=sleep, now=lambda: 0.0)
    answers = await ask(client, questions)
    assert batches == [MAX_BATCH, 2]
    assert len(slept) == 1 and slept[0] > 0, "5 RPM spacing between batches"
    assert len(answers) == len(questions)


@pytest.mark.asyncio
async def test_a_quota_error_leaves_the_cache_untouched(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    store = DisambiguationStore(directory)
    questions = _corpus()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "RESOURCE_EXHAUSTED"}})

    with pytest.raises(GeminiQuotaError):
        await ask(_stub_client(handler), questions)
    assert store.answers() == {}
    assert not (directory / "answers.json").exists()


@pytest.mark.asyncio
async def test_a_mis_mapped_batch_writes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "disambiguation"
    questions = _corpus()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "1. Ferrier -> Lucy Ferrier\n2. Smith -> Mrs. Smith\n"
                     "3. Sholto -> Major Sholto"}
        ]}}]})

    with pytest.raises(AnswerMismatchError):
        await ask(_stub_client(handler), questions)
    assert DisambiguationStore(directory).answers() == {}


@pytest.mark.asyncio
async def test_ask_with_no_questions_spends_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not send a request")

    client = _stub_client(handler)
    assert await ask(client, []) == {}
    assert client.requests_made == 0


def test_an_echo_that_repeats_the_kind_label_still_maps() -> None:
    # Measured live (Task 11a, gemini-3.5-flash-lite): the model echoed
    # "Ferrier (Character)", copying the prompt's own "name: Ferrier
    # (Character)" line, and the whole batch was rejected. The kind is part
    # of the question, so it still identifies it; any OTHER suffix mismatches.
    questions = _corpus()
    answers = parse_batch_answers(questions, _answer_text([
        (1, "Sholto (Character)", "1"),
        (2, "Ferrier (character)", "none"),
        (3, "Smith", "2"),
    ]))
    assert answers[questions[0].key] == "id-major-sholto"
    with pytest.raises(AnswerMismatchError):
        parse_batch_answers(questions, _answer_text([
            (1, "Sholto (Place)", "1"), (2, "Ferrier", "none"), (3, "Smith", "2"),
        ]))

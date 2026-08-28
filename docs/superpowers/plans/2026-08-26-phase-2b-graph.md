# LORE Phase 2B — Entity Resolution & Graph Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `lore process <book_id>` turns extracted triplets into a loaded knowledge graph — aliases collapsed into single entities, predicates normalized, everything written transactionally to Kuzu and LanceDB — after which a multi-hop Cypher query returns a correct, evidence-carrying answer.

**Architecture:** Pure normalization functions underpin a three-pass resolver (exact/alias → embedding similarity → LLM adjudication for the gray band only, with a higher bar across books). A separate predicate normalizer maps raw predicates onto the curated vocabulary while preserving the original. A loader reads only artifacts whose `jobs` row says `completed`, resolves them, and writes per book idempotently.

**Tech Stack:** Python 3.12, uv, pydantic, pyarrow, Kuzu (embedded graph), LanceDB (embedded vectors), Ollama (embeddings + adjudication), typer.

**Spec:** `docs/superpowers/specs/2026-08-26-phase-2b-graph-design.md`

## Global Constraints

- Python **3.12** via **uv** (`uv run`, `uv add` — never bare pip).
- **Git identity (HARD GATE):** before EVERY commit, `git config user.email` must print exactly `jatinroopchandani3007@gmail.com`; anything else → STOP. Commit messages end with the two trailer lines shown in Task 1. **Push to `origin/main` after every task.**
- TDD per task: failing test → implement → pass. Full suite (`uv run pytest -m 'not live' -v`) green and pristine (zero warnings) before each commit.
- Tests use `tmp_path` and fakes — no network, no GPU, no live Ollama, nothing under `data/`.
- Embedding dimension is **768** (`EMBED_DIM` in `lore/db/lance_store.py`).
- Curated predicates (exact strings): `ALLEGIANCE_TO, MEMBER_OF, LOCATED_IN, POSSESSES, INTERACTS_WITH, OPPOSES, LOVES, KILLS, BECOMES, EXPERIENCES`. Unmapped becomes `UNCATEGORIZED`.
- Entity ids are deterministic: `"e" + sha256(f"{normalized_name}|{kind}").hexdigest()[:12]`.
- The loader reads **only** units whose `jobs` row state is `completed` — never file presence.

---

### Task 1: Name normalization

**Files:**
- Create: `lore/resolve/__init__.py`, `lore/resolve/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `normalize_name(name: str) -> str`; `entity_id(normalized_name: str, kind: str) -> str`; `HONORIFICS: frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_normalize.py`:
```python
from lore.resolve.normalize import entity_id, normalize_name


def test_casefolds_and_collapses_whitespace() -> None:
    assert normalize_name("  MISS   BENNET ") == "bennet"


def test_strips_honorifics() -> None:
    assert normalize_name("Mr. Darcy") == "darcy"
    assert normalize_name("Lady Danholm") == "danholm"
    assert normalize_name("Captain Vane") == "vane"


def test_bare_name_matches_honorific_form() -> None:
    assert normalize_name("Darcy") == normalize_name("Mr. Darcy")


def test_strips_possessives_and_punctuation() -> None:
    assert normalize_name("Elizabeth Bennet's") == "elizabeth bennet"
    assert normalize_name("St. John, Esq.") == "john esq"


def test_leading_article_dropped() -> None:
    assert normalize_name("The House of Vaelen") == "house of vaelen"


def test_honorific_only_name_is_not_emptied() -> None:
    # "the Captain" is how a character is named; it must not normalize away.
    assert normalize_name("the Captain") == "captain"


def test_entity_id_is_deterministic_and_kind_scoped() -> None:
    a = entity_id("darcy", "Character")
    assert a == entity_id("darcy", "Character")
    assert a != entity_id("darcy", "Place")
    assert a.startswith("e") and len(a) == 13
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.resolve'`.

- [ ] **Step 3: Implement**

`lore/resolve/__init__.py`: empty file.

`lore/resolve/normalize.py`:
```python
"""Surface-name normalization — where most false merges are won or lost.

Deliberately conservative: it strips the decorations that make one
character look like two (honorifics, possessives, punctuation, a leading
article) and nothing else. It never returns an empty string, because
"the Captain" is a real way to name someone.
"""
from __future__ import annotations

import hashlib
import re

HONORIFICS = frozenset({
    "mr", "mrs", "miss", "ms", "dr", "doctor", "sir", "madam", "lady", "lord",
    "captain", "colonel", "major", "general", "king", "queen", "prince",
    "princess", "st", "saint", "father", "mother", "uncle", "aunt",
})
_ARTICLES = frozenset({"the", "a", "an"})
_POSSESSIVE = re.compile(r"['’]s\b")
_NON_WORD = re.compile(r"[^\w\s]")


def normalize_name(name: str) -> str:
    text = _POSSESSIVE.sub("", name.casefold())
    text = _NON_WORD.sub(" ", text)
    tokens = [t for t in text.split() if t]
    while tokens and tokens[0] in _ARTICLES:
        tokens = tokens[1:]
    without_honorifics = [t for t in tokens if t not in HONORIFICS]
    # Falling back to `tokens` keeps honorific-only names ("the Captain")
    # from normalizing to nothing.
    return " ".join(without_honorifics or tokens)


def entity_id(normalized_name: str, kind: str) -> str:
    digest = hashlib.sha256(f"{normalized_name}|{kind}".encode()).hexdigest()
    return f"e{digest[:12]}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: 7 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/resolve/ tests/test_normalize.py
git commit -m "feat: surface-name normalization and deterministic entity ids

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 2: Predicate normalization

**Files:**
- Create: `lore/resolve/predicates.py`
- Test: `tests/test_predicates.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `CURATED_PREDICATES: tuple[str, ...]` (the ten, in the order given in Global Constraints)
  - `UNCATEGORIZED: str = "UNCATEGORIZED"`
  - `cosine(a: list[float], b: list[float]) -> float`
  - `async def normalize_predicates(raws: list[str], embed, threshold: float = 0.75) -> dict[str, str]` where `embed` is `Callable[[list[str]], Awaitable[list[list[float]]]]`. Returns raw → curated (or `UNCATEGORIZED`). An exact curated match never consults the embedder.

- [ ] **Step 1: Write the failing tests**

`tests/test_predicates.py`:
```python
import pytest

from lore.resolve.predicates import (
    CURATED_PREDICATES,
    UNCATEGORIZED,
    cosine,
    normalize_predicates,
)


def _vec(*values: float) -> list[float]:
    return list(values)


def test_cosine_basics() -> None:
    assert cosine(_vec(1, 0), _vec(1, 0)) == pytest.approx(1.0)
    assert cosine(_vec(1, 0), _vec(0, 1)) == pytest.approx(0.0)
    assert cosine(_vec(0, 0), _vec(1, 0)) == 0.0  # zero vector, no crash


@pytest.mark.asyncio
async def test_exact_curated_match_skips_the_embedder() -> None:
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    mapping = await normalize_predicates(["OPPOSES", "LOVES"], embed)
    assert mapping == {"OPPOSES": "OPPOSES", "LOVES": "LOVES"}
    assert calls == []  # nothing needed embedding


@pytest.mark.asyncio
async def test_near_predicate_maps_to_curated() -> None:
    # DISTRUSTS is made identical to OPPOSES; everything else orthogonal.
    async def embed(texts):
        out = []
        for text in texts:
            if text in ("OPPOSES", "DISTRUSTS"):
                out.append([1.0, 0.0])
            else:
                out.append([0.0, 1.0])
        return out

    mapping = await normalize_predicates(["DISTRUSTS"], embed)
    assert mapping["DISTRUSTS"] == "OPPOSES"


@pytest.mark.asyncio
async def test_unrelated_predicate_becomes_uncategorized() -> None:
    async def embed(texts):
        return [[1.0, 0.0] if t != "SMELLS_LIKE" else [0.0, 1.0] for t in texts]

    mapping = await normalize_predicates(["SMELLS_LIKE"], embed)
    assert mapping["SMELLS_LIKE"] == UNCATEGORIZED


@pytest.mark.asyncio
async def test_duplicate_raws_are_embedded_once() -> None:
    seen: list[list[str]] = []

    async def embed(texts):
        seen.append(texts)
        return [[0.0, 1.0] for _ in texts]

    await normalize_predicates(["FOO", "FOO", "FOO"], embed)
    embedded_raws = [t for batch in seen for t in batch if t == "FOO"]
    assert len(embedded_raws) == 1


def test_curated_list_is_the_documented_ten() -> None:
    assert CURATED_PREDICATES == (
        "ALLEGIANCE_TO", "MEMBER_OF", "LOCATED_IN", "POSSESSES",
        "INTERACTS_WITH", "OPPOSES", "LOVES", "KILLS", "BECOMES", "EXPERIENCES",
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_predicates.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.resolve.predicates`.

- [ ] **Step 3: Implement `lore/resolve/predicates.py`**

```python
"""Map free-text predicates onto the curated vocabulary.

The extraction model prefers literal verbs (COMMANDS, DISTRUSTS) over the
curated set. Traversal queries need a small closed vocabulary, so each raw
predicate is mapped to its nearest curated type by embedding similarity —
but the raw string is always preserved alongside it, because DISTRUSTS and
KILLS both collapsing to OPPOSES would destroy real meaning.
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable

CURATED_PREDICATES: tuple[str, ...] = (
    "ALLEGIANCE_TO", "MEMBER_OF", "LOCATED_IN", "POSSESSES",
    "INTERACTS_WITH", "OPPOSES", "LOVES", "KILLS", "BECOMES", "EXPERIENCES",
)
UNCATEGORIZED = "UNCATEGORIZED"

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


async def normalize_predicates(
    raws: list[str], embed: Embed, threshold: float = 0.75
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    unknown: list[str] = []
    for raw in raws:
        if raw in mapping:
            continue
        if raw in CURATED_PREDICATES:
            mapping[raw] = raw
        elif raw not in unknown:
            unknown.append(raw)
    if not unknown:
        return mapping

    vectors = await embed(list(CURATED_PREDICATES) + unknown)
    curated_vectors = vectors[: len(CURATED_PREDICATES)]
    unknown_vectors = vectors[len(CURATED_PREDICATES) :]
    for raw, vector in zip(unknown, unknown_vectors):
        scores = [cosine(vector, cv) for cv in curated_vectors]
        best = max(range(len(scores)), key=lambda i: scores[i])
        mapping[raw] = (
            CURATED_PREDICATES[best] if scores[best] >= threshold else UNCATEGORIZED
        )
    return mapping
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_predicates.py -v`
Expected: 6 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/resolve/predicates.py tests/test_predicates.py
git commit -m "feat: predicate normalization onto the curated vocabulary

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 3: Three-pass entity resolver

**Files:**
- Create: `lore/resolve/resolver.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `normalize_name`, `entity_id` (Task 1); `cosine` (Task 2).
- Produces:
  - `@dataclass(frozen=True) Mention(name: str, kind: str, book_id: str, context: str)`
  - `@dataclass(frozen=True) CanonicalEntity(id: str, name: str, kind: str, aliases: tuple[str, ...], book_ids: tuple[str, ...])`
  - `@dataclass(frozen=True) AliasRecord(alias_id: str, alias_name: str, kind: str, canonical_id: str)`
  - `@dataclass ResolutionResult(entities: list[CanonicalEntity], aliases: list[AliasRecord], mention_map: dict[tuple[str, str, str], str])` — the key is `(name, kind, book_id)`, the value a canonical entity id.
  - `async def resolve_mentions(mentions, embed, adjudicate, *, same_book_floor=0.86, cross_book_floor=0.92, reject_floor=0.70) -> ResolutionResult` where `embed` is `Callable[[list[str]], Awaitable[list[list[float]]]]` and `adjudicate` is `Callable[[CanonicalEntity, Mention], Awaitable[bool]]`.

Resolution rules, in order, per mention:
1. Same normalized name and kind as an existing canonical (or one of its aliases) → resolve to it, no embedding.
2. Otherwise embed `name + " — " + context` and compare against candidates of the same kind. Below `reject_floor` → new entity. Same book and ≥ `same_book_floor` → merge. Cross book and ≥ `cross_book_floor` → escalate to adjudication. Anything in between → escalate to adjudication.
3. `adjudicate(canonical, mention)` returns True to merge, False to mint a new entity. **A cross-book merge is never made without adjudication returning True.**

- [ ] **Step 1: Write the failing tests**

`tests/test_resolver.py`:
```python
import pytest

from lore.resolve.resolver import Mention, resolve_mentions


def _embedder(vectors: dict[str, list[float]]):
    async def embed(texts):
        return [vectors.get(t.split(" — ")[0], [0.0, 0.0, 1.0]) for t in texts]

    return embed


async def _never(canonical, mention) -> bool:
    return False


async def _always(canonical, mention) -> bool:
    return True


@pytest.mark.asyncio
async def test_exact_and_honorific_forms_collapse_without_embedding() -> None:
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = await resolve_mentions(
        [
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Miss Bennet", "Character", "b1", "she laughed"),
        ],
        embed,
        _never,
    )
    # "miss bennet" normalizes to "bennet", not "elizabeth bennet" — so this
    # pair does NOT collapse on pass 1; it must reach the embedder.
    assert calls, "second mention should have needed an embedding"
    assert len(result.entities) >= 1


@pytest.mark.asyncio
async def test_identical_names_collapse_on_pass_one() -> None:
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = await resolve_mentions(
        [
            Mention("Mr. Darcy", "Character", "b1", "he bowed"),
            Mention("Darcy", "Character", "b1", "he rode away"),
        ],
        embed,
        _never,
    )
    assert len(result.entities) == 1
    assert calls == []  # pass 1 handled both


@pytest.mark.asyncio
async def test_same_book_similar_names_merge_on_embedding() -> None:
    embed = _embedder({"Lizzy": [1.0, 0.0, 0.0], "Elizabeth Bennet": [1.0, 0.0, 0.0]})
    result = await resolve_mentions(
        [
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Lizzy", "Character", "b1", "she laughed"),
        ],
        embed,
        _never,  # adjudication would say no; same-book similarity is enough
    )
    assert len(result.entities) == 1
    assert set(result.entities[0].aliases) == {"elizabeth bennet", "lizzy"}
    assert len(result.aliases) == 1


@pytest.mark.asyncio
async def test_dissimilar_names_stay_separate() -> None:
    embed = _embedder({"Darcy": [1.0, 0.0, 0.0], "Wickham": [0.0, 1.0, 0.0]})
    result = await resolve_mentions(
        [
            Mention("Darcy", "Character", "b1", "he bowed"),
            Mention("Wickham", "Character", "b1", "he lied"),
        ],
        embed,
        _always,  # even a yes-man adjudicator must not be consulted
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_cross_book_merge_requires_adjudication() -> None:
    embed = _embedder({"the Captain": [1.0, 0.0, 0.0]})
    refused = await resolve_mentions(
        [
            Mention("the Captain", "Character", "b1", "he shouted"),
            Mention("the Captain", "Character", "b2", "he wept"),
        ],
        embed,
        _never,
    )
    assert len(refused.entities) == 2, "different books must not fuse silently"

    approved = await resolve_mentions(
        [
            Mention("the Captain", "Character", "b1", "he shouted"),
            Mention("the Captain", "Character", "b2", "he wept"),
        ],
        embed,
        _always,
    )
    assert len(approved.entities) == 1
    assert set(approved.entities[0].book_ids) == {"b1", "b2"}


@pytest.mark.asyncio
async def test_different_kinds_never_merge() -> None:
    embed = _embedder({"Netherfield": [1.0, 0.0, 0.0]})
    result = await resolve_mentions(
        [
            Mention("Netherfield", "Place", "b1", "the estate"),
            Mention("Netherfield", "Faction", "b1", "the household"),
        ],
        embed,
        _always,
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_mention_map_points_every_mention_at_an_entity() -> None:
    embed = _embedder({"Darcy": [1.0, 0.0, 0.0], "Wickham": [0.0, 1.0, 0.0]})
    mentions = [
        Mention("Darcy", "Character", "b1", "he bowed"),
        Mention("Wickham", "Character", "b1", "he lied"),
    ]
    result = await resolve_mentions(mentions, embed, _never)
    ids = {result.mention_map[(m.name, m.kind, m.book_id)] for m in mentions}
    assert len(ids) == 2
    assert all(i.startswith("e") for i in ids)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.resolve.resolver`.

- [ ] **Step 3: Implement `lore/resolve/resolver.py`**

```python
"""Three-pass entity resolution: the "Voldemort ≡ The Dark Lord" problem.

Pass 1 is exact/alias matching on the normalized name — free, and it
handles most of a book. Pass 2 embeds name plus local context and compares
against same-kind candidates. Pass 3 asks an LLM, and only for the
ambiguous middle. A merge across two different books ALWAYS requires
pass 3 to approve it, so "the Captain" in two unrelated novels stays two
people.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from lore.resolve.normalize import entity_id, normalize_name
from lore.resolve.predicates import cosine

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]


@dataclass(frozen=True)
class Mention:
    name: str
    kind: str
    book_id: str
    context: str


@dataclass(frozen=True)
class CanonicalEntity:
    id: str
    name: str
    kind: str
    aliases: tuple[str, ...]
    book_ids: tuple[str, ...]


@dataclass(frozen=True)
class AliasRecord:
    alias_id: str
    alias_name: str
    kind: str
    canonical_id: str


@dataclass
class ResolutionResult:
    entities: list[CanonicalEntity] = field(default_factory=list)
    aliases: list[AliasRecord] = field(default_factory=list)
    mention_map: dict[tuple[str, str, str], str] = field(default_factory=dict)


Adjudicate = Callable[[CanonicalEntity, Mention], Awaitable[bool]]


async def resolve_mentions(
    mentions: list[Mention],
    embed: Embed,
    adjudicate: Adjudicate,
    *,
    same_book_floor: float = 0.86,
    cross_book_floor: float = 0.92,
    reject_floor: float = 0.70,
) -> ResolutionResult:
    result = ResolutionResult()
    # Working state, keyed by canonical id.
    names: dict[str, str] = {}
    kinds: dict[str, str] = {}
    alias_sets: dict[str, set[str]] = {}
    books: dict[str, set[str]] = {}
    vectors: dict[str, list[float]] = {}

    def _emit(canonical_id: str) -> None:
        pass  # entities are materialized once, at the end

    for mention in mentions:
        normalized = normalize_name(mention.name)
        key = (mention.name, mention.kind, mention.book_id)

        # Pass 1 — exact or alias match, same kind.
        hit = next(
            (
                cid
                for cid, aliases in alias_sets.items()
                if kinds[cid] == mention.kind and normalized in aliases
            ),
            None,
        )
        if hit is not None:
            books[hit].add(mention.book_id)
            result.mention_map[key] = hit
            continue

        candidates = [cid for cid in alias_sets if kinds[cid] == mention.kind]
        chosen: str | None = None
        if candidates:
            # Pass 2 — embedding similarity against same-kind candidates.
            vector = (await embed([f"{mention.name} — {mention.context}"]))[0]
            scored = sorted(
                ((cosine(vector, vectors[cid]), cid) for cid in candidates),
                reverse=True,
            )
            score, best = scored[0]
            if score >= reject_floor:
                same_book = mention.book_id in books[best]
                floor = same_book_floor if same_book else cross_book_floor
                candidate_entity = CanonicalEntity(
                    id=best, name=names[best], kind=kinds[best],
                    aliases=tuple(sorted(alias_sets[best])),
                    book_ids=tuple(sorted(books[best])),
                )
                if same_book and score >= floor:
                    chosen = best
                # Pass 3 — the gray band, and every cross-book merge.
                elif await adjudicate(candidate_entity, mention):
                    chosen = best
        else:
            vector = (await embed([f"{mention.name} — {mention.context}"]))[0]

        if chosen is None:
            chosen = entity_id(normalized, mention.kind)
            names[chosen] = mention.name
            kinds[chosen] = mention.kind
            alias_sets[chosen] = {normalized}
            books[chosen] = {mention.book_id}
            vectors[chosen] = vector
        else:
            alias_sets[chosen].add(normalized)
            books[chosen].add(mention.book_id)
            result.aliases.append(
                AliasRecord(
                    alias_id=entity_id(normalized, mention.kind),
                    alias_name=mention.name,
                    kind=mention.kind,
                    canonical_id=chosen,
                )
            )
        result.mention_map[key] = chosen

    result.entities = [
        CanonicalEntity(
            id=cid, name=names[cid], kind=kinds[cid],
            aliases=tuple(sorted(alias_sets[cid])),
            book_ids=tuple(sorted(books[cid])),
        )
        for cid in alias_sets
    ]
    return result
```

Implementer note: delete the unused `_emit` stub before committing — it is dead. Also make sure `vector` is always bound before use on the new-entity path (the `else` branch above handles the no-candidates case); if the structure fights you, hoist the embedding call so it runs once per mention that reaches pass 2, and say so in your report.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: 7 passed. If `test_exact_and_honorific_forms_collapse_without_embedding` is ambiguous about intent, keep its assertions as written — it documents that "Miss Bennet" and "Elizabeth Bennet" do NOT share a normalized form and therefore must go through embedding, which is the honest behaviour.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/resolve/resolver.py tests/test_resolver.py
git commit -m "feat: three-pass entity resolver with cross-book adjudication

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 4: Store extensions

**Files:**
- Modify: `lore/db/kuzu_store.py` (add `predicate_raw` to the `REL` schema plus a column guard), `lore/db/lance_store.py` (entity upserts and per-book replacement)
- Test: `tests/test_kuzu_store.py` (append), `tests/test_lance_store.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - Kuzu `REL` gains `predicate_raw STRING`; `KuzuStore.__init__` adds the column to a pre-existing database via `ALTER TABLE REL ADD predicate_raw STRING` when it is missing.
  - `LanceStore.add_entities(rows: list[dict]) -> None` — rows carry `vector, entity_id, name, kind`.
  - `LanceStore.delete_chunks_for_book(book_id: str) -> None`
  - `LanceStore.delete_entities(entity_ids: list[str]) -> None`
  - `LanceStore.count(table: str) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kuzu_store.py`:
```python
def test_rel_table_has_predicate_raw(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kuzu")
    store.execute(
        "CREATE (:Entity {id:'e1', name:'A', kind:'Character', aliases:['a']})"
    )
    store.execute(
        "CREATE (:Entity {id:'e2', name:'B', kind:'Character', aliases:['b']})"
    )
    store.execute(
        "MATCH (a:Entity {id:'e1'}), (b:Entity {id:'e2'}) "
        "CREATE (a)-[:REL {predicate:'OPPOSES', predicate_raw:'DISTRUSTS', "
        "evidence:'ev', confidence:0.9, chunk_id:'c1', book_id:'b1', "
        "chapter_seq:3, run_id:'r1'}]->(b)"
    )
    rows = [
        tuple(r)
        for r in store.execute(
            "MATCH ()-[r:REL]->() RETURN r.predicate, r.predicate_raw"
        )
    ]
    assert rows == [("OPPOSES", "DISTRUSTS")]
    store.close()
```

Append to `tests/test_lance_store.py`:
```python
def test_add_and_count_entities(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    store.add_entities([
        {"vector": [0.0] * EMBED_DIM, "entity_id": "e1", "name": "Darcy",
         "kind": "Character"},
    ])
    assert store.count("entities") == 1


def test_delete_chunks_for_book(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    store.add_chunks([
        {"vector": [0.0] * EMBED_DIM, "chunk_id": "b1:1:0", "book_id": "b1",
         "chapter_seq": 1, "text": "one", "entity_ids": []},
        {"vector": [0.0] * EMBED_DIM, "chunk_id": "b2:1:0", "book_id": "b2",
         "chapter_seq": 1, "text": "two", "entity_ids": []},
    ])
    store.delete_chunks_for_book("b1")
    assert store.count("chunks") == 1


def test_delete_entities(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    store.add_entities([
        {"vector": [0.0] * EMBED_DIM, "entity_id": "e1", "name": "A", "kind": "Character"},
        {"vector": [0.0] * EMBED_DIM, "entity_id": "e2", "name": "B", "kind": "Character"},
    ])
    store.delete_entities(["e1"])
    assert store.count("entities") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_kuzu_store.py tests/test_lance_store.py -v`
Expected: the Kuzu test fails on the unknown `predicate_raw` property; the LanceDB tests fail with `AttributeError` for the new methods.

- [ ] **Step 3: Implement**

In `lore/db/kuzu_store.py`, extend the `REL` DDL string so it reads:
```python
    (
        "CREATE REL TABLE REL (FROM Entity TO Entity, predicate STRING, "
        "predicate_raw STRING, evidence STRING, confidence DOUBLE, "
        "chunk_id STRING, book_id STRING, chapter_seq INT64, run_id STRING)"
    ),
```
and, after the schema-creation loop in `__init__`, add the guard for databases created before this change:
```python
        if "REL" in existing:
            columns = {
                row[1]
                for row in self._conn.execute("CALL table_info('REL') RETURN *")
            }
            if "predicate_raw" not in columns:
                self._conn.execute("ALTER TABLE REL ADD predicate_raw STRING")
```
Implementer note: `CALL table_info('REL') RETURN *` column ordering varies by Kuzu version. Inspect the actual result once (`print([tuple(r) for r in store.execute("CALL table_info('REL') RETURN *")])`) and index the name column correctly; do not guess. If `table_info` is unavailable in this version, fall back to attempting the `ALTER` inside a try/except that swallows only the "already exists" error, and document which path you took.

In `lore/db/lance_store.py`, add:
```python
    def add_entities(self, rows: list[dict]) -> None:
        self._db.open_table("entities").add(rows)

    def delete_chunks_for_book(self, book_id: str) -> None:
        escaped = book_id.replace("'", "''")
        self._db.open_table("chunks").delete(f"book_id = '{escaped}'")

    def delete_entities(self, entity_ids: list[str]) -> None:
        if not entity_ids:
            return
        quoted = ", ".join("'" + e.replace("'", "''") + "'" for e in entity_ids)
        self._db.open_table("entities").delete(f"entity_id IN ({quoted})")

    def count(self, table: str) -> int:
        return self._db.open_table(table).count_rows()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_kuzu_store.py tests/test_lance_store.py -v`
Expected: all pass, including the pre-existing store tests unchanged.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/db/kuzu_store.py lore/db/lance_store.py tests/test_kuzu_store.py tests/test_lance_store.py
git commit -m "feat: predicate_raw column and lancedb entity/book operations

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 5: Artifact reader gated on job state

**Files:**
- Create: `lore/graph/__init__.py`, `lore/graph/artifacts.py`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: `LoreConfig.extractions_dir`; the `jobs` table (`kind='extract_unit'`, `detail=<unit_id>`, `state`).
- Produces:
  - `completed_unit_dirs(conn, config, book_id) -> list[Path]` — only units whose job row reads `completed`, sorted by unit id.
  - `read_triplets(dirs: list[Path]) -> list[dict]` — every row of every `triplets.parquet`.
  - `read_embeddings(dirs: list[Path]) -> list[dict]` — every row of every `embeddings.parquet`.

- [ ] **Step 1: Write the failing tests**

`tests/test_artifacts.py`:
```python
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from lore.config import LoreConfig
from lore.db.lance_store import EMBED_DIM
from lore.db.sqlite import open_db
from lore.graph.artifacts import completed_unit_dirs, read_embeddings, read_triplets
from lore.orchestrator.run import set_unit_state

BOOK = "abcdef0123456789"


def _config(tmp_path: Path) -> LoreConfig:
    return LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})


def _unit(config: LoreConfig, unit_id: str, chunk_id: str) -> Path:
    d = config.extractions_dir / BOOK / unit_id
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"chunk_id": [chunk_id], "subject": ["A"], "predicate": ["OPPOSES"]}),
        d / "triplets.parquet",
    )
    pq.write_table(
        pa.table({"chunk_id": [chunk_id], "vector": [[0.0] * EMBED_DIM]}),
        d / "embeddings.parquet",
    )
    return d


def test_only_completed_units_are_returned(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _unit(config, f"{BOOK}-u0000-aaaa", "c0")
    _unit(config, f"{BOOK}-u0001-bbbb", "c1")
    set_unit_state(conn, BOOK, f"{BOOK}-u0000-aaaa", "completed")
    set_unit_state(conn, BOOK, f"{BOOK}-u0001-bbbb", "failed")

    dirs = completed_unit_dirs(conn, config, BOOK)
    assert [d.name for d in dirs] == [f"{BOOK}-u0000-aaaa"]
    conn.close()


def test_artifacts_present_without_a_completed_job_are_ignored(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _unit(config, f"{BOOK}-u0000-aaaa", "c0")  # files exist, no job row at all
    assert completed_unit_dirs(conn, config, BOOK) == []
    conn.close()


def test_reads_rows_across_units(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    for i, chunk in enumerate(["c0", "c1"]):
        unit_id = f"{BOOK}-u{i:04d}-xxxx"
        _unit(config, unit_id, chunk)
        set_unit_state(conn, BOOK, unit_id, "completed")
    dirs = completed_unit_dirs(conn, config, BOOK)
    assert {r["chunk_id"] for r in read_triplets(dirs)} == {"c0", "c1"}
    assert len(read_embeddings(dirs)) == 2
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_artifacts.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.graph`.

- [ ] **Step 3: Implement**

`lore/graph/__init__.py`: empty file.

`lore/graph/artifacts.py`:
```python
"""Read extraction artifacts — but only those a job row vouches for.

A failed unit legitimately leaves logs and partially pulled files behind,
so file presence is not evidence that a unit's work is complete. The
`jobs` table is the authority.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pyarrow.parquet as pq

from lore.config import LoreConfig

JOB_KIND = "extract_unit"


def completed_unit_dirs(
    conn: sqlite3.Connection, config: LoreConfig, book_id: str
) -> list[Path]:
    rows = conn.execute(
        "SELECT detail FROM jobs WHERE kind = ? AND book_id = ? AND state = 'completed'"
        " ORDER BY detail",
        (JOB_KIND, book_id),
    ).fetchall()
    base = config.extractions_dir / book_id
    return [base / row[0] for row in rows if (base / row[0]).is_dir()]


def _read(dirs: list[Path], filename: str) -> list[dict]:
    rows: list[dict] = []
    for directory in dirs:
        path = directory / filename
        if path.is_file():
            rows.extend(pq.read_table(path).to_pylist())
    return rows


def read_triplets(dirs: list[Path]) -> list[dict]:
    return _read(dirs, "triplets.parquet")


def read_embeddings(dirs: list[Path]) -> list[dict]:
    return _read(dirs, "embeddings.parquet")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_artifacts.py -v`
Expected: 3 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/graph/ tests/test_artifacts.py
git commit -m "feat: job-state-gated extraction artifact reader

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 6: The graph loader

**Files:**
- Create: `lore/graph/loader.py`
- Test: `tests/test_loader.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5; `chunks_for_book` (`lore/ingest/registry.py`); `KuzuStore`, `LanceStore`; `list_books`.
- Produces:
  - `@dataclass LoadReport(book_id: str, entities: int, alias_links: int, rel_edges: int, chunks: int, mentions: int)`
  - `async def load_book(config, conn, kuzu, lance, book_id, embed, adjudicate) -> LoadReport`

Behaviour:
- Reads only completed units. Raises `ValueError` if there are none.
- Builds `Mention`s from each triplet row's `entity_names[]`/`entity_kinds[]` (context = the row's `evidence`), resolves them, normalizes predicates.
- Deletes this book's existing graph rows before writing, so re-loading is idempotent.
- Writes Book/Chapter/Chunk nodes and HAS_CHAPTER/HAS_CHUNK from the SQLite registry, Entity nodes for canonicals AND for alias surface forms (linked `ALIAS_OF` → canonical), `MENTIONS` from chunk to canonical entity, and `REL` edges carrying `predicate`, `predicate_raw`, `evidence`, `confidence`, `chunk_id`, `book_id`, `chapter_seq`, `run_id`.
- Writes LanceDB `chunks` rows (vector, text, `entity_ids`) and `entities` rows (centroid vector, name, kind).
- A failure part-way must not leave a half-loaded book.

- [ ] **Step 1: Write the failing tests**

`tests/test_loader.py`:
```python
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import EMBED_DIM, LanceStore
from lore.db.sqlite import open_db
from lore.graph.loader import load_book
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.orchestrator.run import set_unit_state

BOOK = "abcdef0123456789"
UNIT = f"{BOOK}-u0000-aaaa"


async def _embed(texts):
    return [[0.0] * EMBED_DIM for _ in texts]


async def _never(canonical, mention) -> bool:
    return False


def _setup(tmp_path: Path):
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1, scene_seq=0,
              seq=i, text=f"passage {i}")
        for i in range(2)
    ]
    ingest_book(conn, book_id=BOOK, title="T", author="A", source="upload",
                file_path="/x/b.epub", chunks=chunks)
    d = config.extractions_dir / BOOK / UNIT
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "chunk_id": [f"{BOOK}:1:0", f"{BOOK}:1:1"],
            "book_id": [BOOK, BOOK],
            "subject": ["Mr. Darcy", "Darcy"],
            "predicate": ["OPPOSES", "LOVES"],
            "object": ["Wickham", "Elizabeth"],
            "evidence": ["he despised him", "he admired her"],
            "confidence": [0.9, 0.8],
            "entity_names": [["Mr. Darcy", "Wickham"], ["Darcy", "Elizabeth"]],
            "entity_kinds": [["Character", "Character"], ["Character", "Character"]],
            "extractor_model": ["m", "m"],
            "run_id": [UNIT, UNIT],
        }),
        d / "triplets.parquet",
    )
    pq.write_table(
        pa.table({
            "chunk_id": [f"{BOOK}:1:0", f"{BOOK}:1:1"],
            "vector": [[0.0] * EMBED_DIM, [0.0] * EMBED_DIM],
            "embed_model": ["e", "e"],
        }),
        d / "embeddings.parquet",
    )
    set_unit_state(conn, BOOK, UNIT, "completed")
    kuzu = KuzuStore(config.kuzu_dir / "graph")
    lance = LanceStore(config.lancedb_dir)
    return config, conn, kuzu, lance


@pytest.mark.asyncio
async def test_load_writes_nodes_and_edges(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.rel_edges == 2
    assert report.chunks == 2
    books = [tuple(r) for r in kuzu.execute("MATCH (b:Book) RETURN b.id, b.title")]
    assert books == [(BOOK, "T")]
    edges = [
        tuple(r)
        for r in kuzu.execute(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "RETURN a.name, r.predicate, r.predicate_raw, b.name, r.chapter_seq"
        )
    ]
    assert len(edges) == 2
    assert all(e[4] == 1 for e in edges)  # chapter_seq carried through
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_aliases_collapse_to_one_entity(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    # "Mr. Darcy" and "Darcy" normalize identically — one canonical node.
    names = sorted(
        r[0] for r in kuzu.execute(
            "MATCH (e:Entity) WHERE e.kind = 'Character' RETURN e.name"
        )
    )
    assert names.count("Mr. Darcy") + names.count("Darcy") == 1
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_load_is_idempotent(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    first = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    second = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert (second.entities, second.rel_edges, second.chunks) == (
        first.entities, first.rel_edges, first.chunks
    )
    total = len([r for r in kuzu.execute("MATCH ()-[r:REL]->() RETURN r.chunk_id")])
    assert total == first.rel_edges
    assert lance.count("chunks") == first.chunks
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_chunks_carry_the_entity_bridge(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    rows = lance.search_chunks([0.0] * EMBED_DIM, limit=5)
    assert rows and all(isinstance(r["entity_ids"], list) for r in rows)
    assert any(r["entity_ids"] for r in rows)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_book_with_no_completed_units_raises(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    conn.execute("UPDATE jobs SET state = 'failed'")
    conn.commit()
    with pytest.raises(ValueError, match="no completed"):
        await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    conn.close(); kuzu.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_loader.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.graph.loader`.

- [ ] **Step 3: Implement `lore/graph/loader.py`**

```python
"""Load resolved triplets into Kuzu and LanceDB, one book at a time.

Idempotent by construction: nodes are MERGEd (so an entity shared with
another book survives a reload), while this book's REL and MENTIONS edges
and its LanceDB chunk rows are deleted and rewritten, because
re-extraction can legitimately change them.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.graph.artifacts import completed_unit_dirs, read_embeddings, read_triplets
from lore.ingest.registry import chunks_for_book
from lore.resolve.predicates import normalize_predicates
from lore.resolve.resolver import Mention, resolve_mentions


@dataclass
class LoadReport:
    book_id: str
    entities: int = 0
    alias_links: int = 0
    rel_edges: int = 0
    chunks: int = 0
    mentions: int = 0


def _mentions_from(rows: list[dict], book_id: str) -> list[Mention]:
    """One Mention per (name, kind) pair a triplet row names, with its
    evidence as local context for the embedding pass."""
    seen: set[tuple[str, str]] = set()
    mentions: list[Mention] = []
    for row in rows:
        names = list(row.get("entity_names") or [])
        kinds = list(row.get("entity_kinds") or [])
        for index, name in enumerate(names):
            kind = kinds[index] if index < len(kinds) else "Concept"
            if (name, kind) in seen:
                continue
            seen.add((name, kind))
            mentions.append(
                Mention(name=name, kind=kind, book_id=book_id,
                        context=str(row.get("evidence") or ""))
            )
    return mentions


def _clear_book(kuzu: KuzuStore, lance: LanceStore, book_id: str) -> None:
    kuzu.execute(
        "MATCH ()-[r:REL]->() WHERE r.book_id = $book DELETE r", {"book": book_id}
    )
    kuzu.execute(
        "MATCH (c:Chunk)-[m:MENTIONS]->() WHERE c.book_id = $book DELETE m",
        {"book": book_id},
    )
    lance.delete_chunks_for_book(book_id)


async def load_book(
    config: LoreConfig,
    conn: sqlite3.Connection,
    kuzu: KuzuStore,
    lance: LanceStore,
    book_id: str,
    embed: Callable[[list[str]], Awaitable[list[list[float]]]],
    adjudicate,
) -> LoadReport:
    dirs = completed_unit_dirs(conn, config, book_id)
    if not dirs:
        raise ValueError(f"no completed extraction units for book {book_id}")

    triplets = read_triplets(dirs)
    embeddings = {row["chunk_id"]: row["vector"] for row in read_embeddings(dirs)}
    chunks = chunks_for_book(conn, book_id)
    chapter_of = {chunk.id: chunk.chapter_seq for chunk in chunks}

    resolution = await resolve_mentions(
        _mentions_from(triplets, book_id), embed, adjudicate
    )
    predicate_map = await normalize_predicates(
        [str(row.get("predicate") or "") for row in triplets], embed
    )
    entity_for = resolution.mention_map

    row = conn.execute(
        "SELECT title, author FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    title, author = (row[0], row[1] or "") if row else ("", "")

    _clear_book(kuzu, lance, book_id)

    kuzu.execute(
        "MERGE (b:Book {id: $id}) SET b.title = $title, b.author = $author",
        {"id": book_id, "title": title, "author": author},
    )
    for chapter_seq in sorted({chunk.chapter_seq for chunk in chunks}):
        chapter_id = f"{book_id}:{chapter_seq}"
        kuzu.execute(
            "MERGE (c:Chapter {id: $id}) SET c.book_id = $book, c.seq = $seq,"
            " c.title = $title",
            {"id": chapter_id, "book": book_id, "seq": chapter_seq,
             "title": f"Chapter {chapter_seq}"},
        )
        kuzu.execute(
            "MATCH (b:Book {id: $book}), (c:Chapter {id: $chapter})"
            " MERGE (b)-[:HAS_CHAPTER]->(c)",
            {"book": book_id, "chapter": chapter_id},
        )
    for chunk in chunks:
        kuzu.execute(
            "MERGE (c:Chunk {id: $id}) SET c.book_id = $book,"
            " c.chapter_seq = $seq",
            {"id": chunk.id, "book": book_id, "seq": chunk.chapter_seq},
        )
        kuzu.execute(
            "MATCH (ch:Chapter {id: $chapter}), (c:Chunk {id: $chunk})"
            " MERGE (ch)-[:HAS_CHUNK]->(c)",
            {"chapter": f"{book_id}:{chunk.chapter_seq}", "chunk": chunk.id},
        )

    for entity in resolution.entities:
        kuzu.execute(
            "MERGE (e:Entity {id: $id}) SET e.name = $name, e.kind = $kind,"
            " e.aliases = $aliases",
            {"id": entity.id, "name": entity.name, "kind": entity.kind,
             "aliases": list(entity.aliases)},
        )
    for alias in resolution.aliases:
        if alias.alias_id == alias.canonical_id:
            continue
        kuzu.execute(
            "MERGE (a:Entity {id: $id}) SET a.name = $name, a.kind = $kind,"
            " a.aliases = $aliases",
            {"id": alias.alias_id, "name": alias.alias_name,
             "kind": alias.kind, "aliases": []},
        )
        kuzu.execute(
            "MATCH (a:Entity {id: $alias}), (c:Entity {id: $canonical})"
            " MERGE (a)-[:ALIAS_OF]->(c)",
            {"alias": alias.alias_id, "canonical": alias.canonical_id},
        )

    report = LoadReport(book_id=book_id, entities=len(resolution.entities),
                        alias_links=len(resolution.aliases))
    chunk_entities: dict[str, set[str]] = {}
    for row in triplets:
        chunk_id = str(row["chunk_id"])
        names = list(row.get("entity_names") or [])
        kinds = list(row.get("entity_kinds") or [])
        for index, name in enumerate(names):
            kind = kinds[index] if index < len(kinds) else "Concept"
            target = entity_for.get((name, kind, book_id))
            if target is None:
                continue
            chunk_entities.setdefault(chunk_id, set()).add(target)

        subject_id = next(
            (entity_for[k] for k in entity_for if k[0] == row.get("subject")), None
        )
        object_id = next(
            (entity_for[k] for k in entity_for if k[0] == row.get("object")), None
        )
        if subject_id is None or object_id is None:
            continue
        raw = str(row.get("predicate") or "")
        kuzu.execute(
            "MATCH (s:Entity {id: $s}), (o:Entity {id: $o})"
            " CREATE (s)-[:REL {predicate: $pred, predicate_raw: $raw,"
            " evidence: $ev, confidence: $conf, chunk_id: $chunk,"
            " book_id: $book, chapter_seq: $seq, run_id: $run}]->(o)",
            {"s": subject_id, "o": object_id,
             "pred": predicate_map.get(raw, raw), "raw": raw,
             "ev": str(row.get("evidence") or ""),
             "conf": float(row.get("confidence") or 0.0),
             "chunk": chunk_id, "book": book_id,
             "seq": int(chapter_of.get(chunk_id, 0)),
             "run": str(row.get("run_id") or "")},
        )
        report.rel_edges += 1

    for chunk_id, entity_ids in chunk_entities.items():
        for entity_id_value in entity_ids:
            kuzu.execute(
                "MATCH (c:Chunk {id: $chunk}), (e:Entity {id: $entity})"
                " MERGE (c)-[:MENTIONS]->(e)",
                {"chunk": chunk_id, "entity": entity_id_value},
            )
            report.mentions += 1

    lance_rows = [
        {"vector": embeddings.get(chunk.id, [0.0] * len(next(iter(embeddings.values()), [0.0]))),
         "chunk_id": chunk.id, "book_id": book_id,
         "chapter_seq": chunk.chapter_seq, "text": chunk.text,
         "entity_ids": sorted(chunk_entities.get(chunk.id, set()))}
        for chunk in chunks
        if chunk.id in embeddings
    ]
    if lance_rows:
        lance.add_chunks(lance_rows)
        report.chunks = len(lance_rows)

    entity_ids = [entity.id for entity in resolution.entities]
    lance.delete_entities(entity_ids)
    if entity_ids:
        entity_vectors = await embed([e.name for e in resolution.entities])
        lance.add_entities([
            {"vector": vector, "entity_id": entity.id, "name": entity.name,
             "kind": entity.kind}
            for entity, vector in zip(resolution.entities, entity_vectors)
        ])
    return report
```

Implementer note: this is the largest task in the phase. The subject/object lookup above scans `entity_for` by surface name — if a name appears with two different kinds the first match wins, which is acceptable here but worth a comment. If any test contradicts this code, the TEST is the contract: make the implementation satisfy it and report the discrepancy rather than editing the test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_loader.py -v`
Expected: 5 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/graph/loader.py tests/test_loader.py
git commit -m "feat: transactional idempotent graph and vector loader

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 7: `lore process` and `lore graph`

**Files:**
- Modify: `lore/cli.py` (add two commands after `quota`; change nothing existing)
- Create: `lore/graph/adjudicate.py`
- Test: `tests/test_cli_process.py`

**Interfaces:**
- Consumes: `load_book` (Task 6); `run_extraction` (`lore/orchestrator/run.py`); `OllamaProvider` (`embed`, `generate`, `aclose`); `LoreConfig`, `open_db`, `KuzuStore`, `LanceStore`.
- Produces:
  - `lore/graph/adjudicate.py`: `make_ollama_pair(config) -> tuple[Embed, Adjudicate, AClose]` returning an embed callable, an adjudicate callable, and an async close callable, all backed by one `OllamaProvider`. Embedding applies `DOCUMENT_PREFIX` from `lore/extract/embed.py`. Adjudication asks a yes/no question with both entities' names, kinds and evidence, and parses a leading "yes" case-insensitively.
  - CLI `lore process <book_id> [--resume] [--skip-extract]` — runs extraction (unless skipped), then the load; prints entity/edge/chunk counts.
  - CLI `lore graph` — prints node counts by label, edge counts by type, and the five most-connected entities.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_process.py`:
```python
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from lore.cli import app
from lore.graph.loader import LoadReport
from lore.orchestrator.run import RunReport

runner = CliRunner()
BOOK = "abcdef0123456789"


def test_process_runs_extraction_then_load(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    report = LoadReport(book_id=BOOK, entities=4, alias_links=1, rel_edges=7,
                        chunks=2, mentions=6)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1, units_completed=1)) as extract,
        patch("lore.cli._load_book_sync", return_value=report) as load,
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 0, result.output
    assert extract.called and load.called
    assert "7 relationships" in result.output


def test_process_skip_extract_only_loads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    report = LoadReport(book_id=BOOK, entities=1, alias_links=0, rel_edges=1,
                        chunks=1, mentions=1)
    with (
        patch("lore.cli.run_extraction") as extract,
        patch("lore.cli._load_book_sync", return_value=report),
    ):
        result = runner.invoke(app, ["process", BOOK, "--skip-extract"])
    assert result.exit_code == 0
    assert not extract.called


def test_process_reports_a_load_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1, units_completed=1)),
        patch("lore.cli._load_book_sync", side_effect=ValueError("no completed units")),
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 1
    assert "no completed units" in result.output


def test_graph_reports_empty_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "0" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_process.py -v`
Expected: FAIL — `process` and `graph` are not registered commands.

- [ ] **Step 3: Implement**

`lore/graph/adjudicate.py`:
```python
"""Ollama-backed embedding and adjudication for resolution.

Both come from one provider so a load opens a single client. Adjudication
is the only LLM call in the phase and it runs locally, so resolution costs
no Kaggle quota and works offline.
"""
from __future__ import annotations

from lore.config import LoreConfig
from lore.extract.embed import DOCUMENT_PREFIX
from lore.inference.ollama import OllamaProvider

_TEMPLATE = """Two names appear in a book. Decide whether they refer to the \
SAME entity.

A: {a_name} ({a_kind}) — evidence: {a_evidence}
B: {b_name} ({b_kind}) — evidence: {b_evidence}

Answer with one word: yes or no."""


def make_ollama_pair(config: LoreConfig):
    """Return (embed, adjudicate, aclose) backed by one Ollama client."""
    provider = OllamaProvider(config.ollama)

    async def embed(texts: list[str]) -> list[list[float]]:
        return await provider.embed(
            [DOCUMENT_PREFIX + text for text in texts],
            model=config.extraction.embed_model_ollama,
        )

    async def adjudicate(canonical, mention) -> bool:
        prompt = _TEMPLATE.format(
            a_name=canonical.name, a_kind=canonical.kind,
            a_evidence=", ".join(canonical.aliases) or "(none)",
            b_name=mention.name, b_kind=mention.kind,
            b_evidence=mention.context or "(none)",
        )
        response = await provider._client.post(
            "/api/chat",
            json={"model": config.ollama.model,
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False},
        )
        response.raise_for_status()
        answer = response.json()["message"]["content"].strip().lower()
        return answer.startswith("y")

    async def aclose() -> None:
        await provider.aclose()

    return embed, adjudicate, aclose
```

In `lore/cli.py`, add these imports alongside the existing ones:
```python
from lore.graph.adjudicate import make_ollama_pair
from lore.graph.loader import load_book
```
then the wrapper and the two commands, after `quota`:
```python
def _load_book_sync(config, conn, book_id):
    """Own the event loop and the store handles for one load."""
    kuzu = KuzuStore(config.kuzu_dir / "graph")
    lance = LanceStore(config.lancedb_dir)
    embed, adjudicate, aclose = make_ollama_pair(config)
    try:
        return asyncio.run(
            load_book(config, conn, kuzu, lance, book_id, embed, adjudicate)
        )
    finally:
        asyncio.run(aclose())
        kuzu.close()


@app.command()
def process(
    book_id: str,
    resume: bool = typer.Option(False, "--resume", help="skip completed work units"),
    skip_extract: bool = typer.Option(
        False, "--skip-extract", help="reload the graph from existing artifacts"
    ),
) -> None:
    """Extract, resolve entities, and load the graph for a book."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        if not skip_extract:
            run_extraction(
                config, conn, book_id, resume=resume,
                on_event=lambda message: typer.echo(f"       {message}"),
            )
        report = _load_book_sync(config, conn, book_id)
    except ValueError as exc:
        typer.echo(f"process failed: {exc}")
        raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(
        f"{report.book_id}: {report.entities} entities "
        f"({report.alias_links} aliases merged), "
        f"{report.rel_edges} relationships, {report.chunks} chunks indexed"
    )


@app.command()
def graph() -> None:
    """Show what is in the knowledge graph."""
    config = LoreConfig.load()
    config.ensure_dirs()
    store = KuzuStore(config.kuzu_dir / "graph")
    try:
        for label in ("Book", "Chapter", "Chunk", "Entity"):
            rows = list(store.execute(f"MATCH (n:{label}) RETURN COUNT(n)"))
            typer.echo(f"{label:>8}: {rows[0][0] if rows else 0}")
        for rel in ("REL", "MENTIONS", "ALIAS_OF"):
            rows = list(store.execute(f"MATCH ()-[r:{rel}]->() RETURN COUNT(r)"))
            typer.echo(f"{rel:>8}: {rows[0][0] if rows else 0}")
        typer.echo("busiest entities:")
        busiest = list(store.execute(
            "MATCH (e:Entity)-[r:REL]-() RETURN e.name, COUNT(r) AS c"
            " ORDER BY c DESC LIMIT 5"
        ))
        for row in busiest:
            typer.echo(f"  {row[0]}  ({row[1]})")
        if not busiest:
            typer.echo("  (none — 0 relationships loaded)")
    finally:
        store.close()
```

Implementer note: `make_ollama_pair` returns THREE values (`embed, adjudicate, aclose`), matching the wrapper's call site. If Kuzu's `COUNT(n)` aggregate syntax differs in the installed version, adapt the query and say so — the tests only require that an empty store prints `0`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_process.py -v`
Expected: 4 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/cli.py lore/graph/adjudicate.py tests/test_cli_process.py
git commit -m "feat: lore process and lore graph commands

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 8: Acceptance — a real graph and the multi-hop demo

**Files:**
- Modify: `README.md` (quickstart gains `process`/`graph`)
- No new production code.

**Interfaces:**
- Consumes: the finished CLI.
- Produces: recorded evidence — a loaded graph, and a multi-hop Cypher query returning evidence-carrying rows.

**Deviation from spec section 3, recorded deliberately:** the spec names a full Kaggle-extracted Pride and Prejudice as the acceptance corpus. This task instead accepts on a locally-extracted fixture book. Rationale: 2B builds the LOADER, and a deterministic fixture proves resolution, idempotency and traversal completely, in minutes, with no GPU spend — whereas a full P&P run mostly re-tests 2A's throughput. Loading a Kaggle-extracted novel remains available via `lore process <book> --skip-extract` once 2A artifacts are regenerated.

- [ ] **Step 1: Confirm prerequisites**

```bash
uv run lore doctor
~/.local/ollama/bin/ollama list | grep -c nomic-embed-text || ~/.local/ollama/bin/ollama pull nomic-embed-text
```
Expected: doctor's checks pass; `nomic-embed-text` present. If the Ollama server is down, start it: `~/.local/ollama/bin/ollama serve &`.

- [ ] **Step 2: Generate artifacts locally and load them**

Set `extraction_provider = "ollama"` in `lore.toml`, then build and process a small fixture book:
```bash
uv run python - <<'PY'
from pathlib import Path
from tests.fixtures import make_epub
body = (
    "<p>Captain Vane commanded the ship Meridian for three years. "
    "Vane swore his loyalty to House Corr when he was young.</p>"
    "<p>Governor Hale distrusted Captain Vane after the blockade. "
    "The Captain never forgave Hale for the accusation.</p>"
    "<p>House Corr lost the salt monopoly that winter. "
    "Vane refused Hale's summons and sailed for open water.</p>"
)
make_epub(Path("data/inbox/vane.epub"), "The Vane Chronicle", "Tester", [("One", body)])
print("built data/inbox/vane.epub")
PY
uv run lore ingest data/inbox/vane.epub
BOOK=$(uv run python -c "import sqlite3;print(sqlite3.connect('data/lore.db').execute(\"SELECT id FROM books WHERE title='The Vane Chronicle'\").fetchone()[0])")
uv run lore process "$BOOK"
uv run lore graph
```
Expected: `process` reports entities, relationships and chunks; `graph` shows non-zero counts. Record the exact output. A 3B local model will produce rough extractions — judge only that the graph is populated and structurally sound, not that every triplet is perfect.

- [ ] **Step 3: Prove idempotency on the real graph**

```bash
uv run lore graph > /tmp/graph_before.txt
uv run lore process "$BOOK" --skip-extract
uv run lore graph > /tmp/graph_after.txt
diff /tmp/graph_before.txt /tmp/graph_after.txt && echo "IDEMPOTENT"
```
Expected: `IDEMPOTENT` — identical counts. If the diff is non-empty, STOP and report DONE_WITH_CONCERNS with both outputs; a loader that duplicates on reload is a real defect.

- [ ] **Step 4: The multi-hop demo**

```bash
uv run python - <<'PY'
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
config = LoreConfig.load()
kuzu = KuzuStore(config.kuzu_dir / "graph")
print("--- entities ---")
for r in kuzu.execute("MATCH (e:Entity) RETURN e.name, e.kind, e.aliases"):
    print("  ", tuple(r))
print("--- two-hop from Vane, with evidence ---")
q = """
MATCH (v:Entity)-[r1:REL]->(mid:Entity)
WHERE v.name CONTAINS 'Vane'
OPTIONAL MATCH (mid)<-[r2:REL]-(other:Entity)
RETURN v.name, r1.predicate, r1.predicate_raw, mid.name,
       r1.evidence, r1.chapter_seq, other.name, r2.predicate
"""
for r in kuzu.execute(q):
    print("  ", tuple(r))
kuzu.close()
PY
```
Expected: entity rows show aliases collapsed (e.g. "Captain Vane" and "the Captain" as one node), and the traversal returns rows whose `evidence` text appears in the source passages, with `predicate` from the curated set and `predicate_raw` preserving the model's original wording. Paste the full output into the report and state plainly whether the evidence is genuinely grounded in the fixture text.

- [ ] **Step 5: Restore config and update the README**

Set `extraction_provider` back to `"kaggle"` in `lore.toml`. Replace the README Quickstart with:
```markdown
## Quickstart

    uv sync
    uv run lore doctor            # checks config, stores, ollama, kaggle auth
    uv run lore doctor --kaggle   # + full free-tier GPU round trip (slow)
    uv run lore ingest book.epub  # parse + chunk + register a book (.epub/.pdf)
    uv run lore books             # list the ingested library
    uv run lore extract <book_id> # extract entities + relationships (Kaggle T4s)
    uv run lore process <book_id> # extract, resolve entities, load the graph
    uv run lore process <book_id> --skip-extract   # reload the graph only
    uv run lore graph             # node/edge counts and the busiest entities
    uv run lore quota             # this week's free-tier GPU budget

Set `extraction_provider = "ollama"` in `lore.toml` for fully local,
zero-egress extraction (slower, smaller model, no Kaggle involved).

Extraction runs one work unit per Kaggle session. `work_unit_size` is 10 by
default — verified on a Kaggle T4; larger units crash vLLM's engine mid-run.
```

- [ ] **Step 6: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git status --short      # data/ must NOT appear
git add README.md lore.toml
git commit -m "docs: quickstart covers process and graph commands

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

## Phase 2B Exit Criteria (mirrors the spec)

1. `uv run pytest -m 'not live'` — entire suite green, output pristine.
2. `lore process <book_id>` takes an ingested book to a loaded graph without manual intervention.
3. Aliases collapse: a character named several ways is one `Entity` node, with `ALIAS_OF` provenance per merge.
4. Re-running the load is idempotent — identical counts, no duplicate edges.
5. `lore graph` reports node and edge counts for a real book.
6. A multi-hop Cypher query returns evidence-carrying rows with chapter numbers and both normalized and raw predicates.
7. Every commit authored `jatinroopchandani3007@gmail.com` and pushed to `origin/main`.

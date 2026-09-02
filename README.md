# LORE — Local Orchestration & Relational Extraction

A local-first literary knowledge graph: e-books in, entities/relationships out
(extracted on free Kaggle GPUs or fully locally), served from embedded
databases (Kuzu + LanceDB) with a hybrid graph-vector query agent, an MCP
server, and a graph UI.

See `SPEC.md` for the full design. Phase 0 status: foundation scaffold.

## Quickstart

    uv sync
    uv run lore doctor            # checks config, stores, ollama, kaggle auth
    uv run lore doctor --kaggle   # + full free-tier GPU round trip (slow)
    uv run lore ingest book.epub  # parse + chunk + register a book (.epub/.pdf)
    uv run lore books             # list the ingested library
    uv run lore extract <book_id> # extract entities + relationships (Kaggle T4s)
    uv run lore extract <book_id> --resume   # continue after an interrupted run
    uv run lore process <book_id> # extract, resolve entities, load the graph
    uv run lore process <book_id> --skip-extract   # reload the graph only
    uv run lore graph             # node/edge counts and the busiest entities
    uv run lore quota             # this week's free-tier GPU budget

Set `extraction_provider = "ollama"` in `lore.toml` for fully local,
zero-egress extraction (slower, smaller model, no Kaggle involved).

Extraction runs one work unit per Kaggle session. `work_unit_size` is 10 by
default — verified on a Kaggle T4; larger units crash vLLM's engine mid-run.

# LORE — Local Orchestration & Relational Extraction

A local-first literary knowledge graph: e-books in, entities/relationships out
(extracted on free Kaggle GPUs or fully locally), served from embedded
databases (Kuzu + LanceDB) with a hybrid graph-vector query agent, an MCP
server, and a graph UI.

See `SPEC.md` for the full design. Phase 0 status: foundation scaffold.

## Quickstart (Phase 0)

    uv sync
    uv run lore doctor          # checks config, stores, ollama, kaggle auth
    uv run lore doctor --kaggle # + full free-tier GPU round trip (slow)

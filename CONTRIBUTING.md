# Contributing to LORE

Thanks for your interest in LORE! Bug reports, ideas and pull requests are all welcome.

## Reporting a bug or asking for a feature

Open an [issue](https://github.com/Jatin-Roopchandani/LORE/issues/new/choose). For a wrong or
odd answer, the most useful thing to include is the question, the book (title is enough), your
reading position, and what you expected. On the hosted beta, the **Report this answer** link
under an answer sends the details for you.

Please report security problems privately: see [SECURITY.md](SECURITY.md).

## Setting up a development environment

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 18+ and
[Ollama](https://ollama.com/download) with `nomic-embed-text` pulled.

```bash
git clone https://github.com/Jatin-Roopchandani/LORE.git
cd LORE
uv sync
cd web && npm install && cd ..
```

For frontend work, run the API and the Vite dev server side by side:

```bash
uv run lore ui --dev          # the API on http://127.0.0.1:8766
cd web && npm run dev         # the app on http://localhost:5173
```

## Running the tests

```bash
uv run pytest -m "not live"   # the server tests; none of them call a model
cd web && npm test            # the web tests
cd web && npx tsc --noEmit    # type-check the frontend
```

Tests marked `live` call real model APIs and spend quota, so they're opt-in.
CI runs the same suites on every push and pull request.

## Making a change

- **Write a test first.** Every behaviour change comes with a test that fails without it.
- **Answer-quality changes ship behind a switch.** LORE's answer pipeline (`lore/query/`) has
  module-level switches, one per behaviour. A new behaviour gets its own switch, defaults to
  on only once it wins a blind paired eval on held-out questions, and stays general: it must
  not be tuned to one book or one question.
- **Keep user-facing text plain.** Every string the web app shows lives in
  `web/src/messages.ts`.
- **Commit messages** follow a light conventional style: `feat(scope): ...`, `fix(scope): ...`,
  `test: ...`, `docs: ...`, saying what changed and why.

## Pull requests

1. Fork the repo and create a branch from `main`.
2. Make your change with tests, and run the suites above.
3. Open a pull request describing what changed, why, and how you tested it.

Small, focused pull requests are easiest to review. For anything large, opening an issue first
to talk it through saves everyone time.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).

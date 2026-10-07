"""Record one real /api/ask event stream as a frontend test fixture.

    .venv/bin/python scripts/record_ask.py "How is Jefferson Hope connected to John Ferrier?" \
        web/src/fixtures/single-part.json

Spends about six Gemini requests. Needs Ollama running (CPU-only):
    CUDA_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=0 ~/.local/ollama/bin/ollama serve
Writes [{"type": <event>, ...data}, ...]: the same objects the frontend's
stream reader produces.
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lore.config import LoreConfig  # noqa: E402
from lore.web.ask import ask_events  # noqa: E402


async def main(question: str, out: Path) -> None:
    config = LoreConfig.load(ROOT / "lore.toml")
    events = []
    async for chunk in ask_events(config, question, asyncio.Lock()):
        block = chunk.decode().strip()
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append({"type": lines["event"], **json.loads(lines["data"])})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(events, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(events)} events -> {out}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2])))

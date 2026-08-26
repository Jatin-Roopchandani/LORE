import json
from pathlib import Path

from lore.config import LoreConfig
from lore.ingest.models import Chunk
from lore.orchestrator.templates import materialize_extract_run
from lore.orchestrator.workunit import pack_units

BOOK = "abcdef0123456789"


def _config() -> LoreConfig:
    config = LoreConfig.load()
    return config.model_copy(
        update={"kaggle": config.kaggle.model_copy(update={"username": "realuser"})}
    )


def _unit():
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"text {i}")
        for i in range(2)
    ]
    return pack_units(BOOK, chunks, size=10)[0]


def test_materialize_substitutes_identity_and_models(tmp_path: Path) -> None:
    kernel_dir, dataset_dir = materialize_extract_run(_config(), _unit(), tmp_path)

    meta = json.loads((kernel_dir / "kernel-metadata.json").read_text())
    assert meta["id"] == "realuser/lore-extract"
    assert meta["dataset_sources"] == ["realuser/lore-extract-data"]
    assert meta["enable_gpu"] == "true"
    assert meta["machine_shape"] == "NvidiaTeslaT4"
    assert meta["enable_internet"] == "true"

    ds_meta = json.loads((dataset_dir / "dataset-metadata.json").read_text())
    assert ds_meta["id"] == "realuser/lore-extract-data"

    script = (kernel_dir / "extract_kernel.py").read_text()
    assert 'MODEL_ID = "Qwen/Qwen2.5-7B-Instruct-AWQ"' in script
    assert 'EMBED_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"' in script
    assert "USERNAME" not in script


def test_materialize_places_the_unit_and_package_in_the_dataset(tmp_path: Path) -> None:
    _, dataset_dir = materialize_extract_run(_config(), _unit(), tmp_path)
    assert (dataset_dir / "unit.jsonl").is_file()
    assert (dataset_dir / "unit-manifest.json").is_file()
    assert (dataset_dir / "lore" / "extract" / "runner.py").is_file()


def test_vllm_spec_defaults_to_unpinned(tmp_path: Path) -> None:
    kernel_dir, _ = materialize_extract_run(_config(), _unit(), tmp_path)
    assert 'VLLM_SPEC = "vllm"' in (kernel_dir / "extract_kernel.py").read_text()


def test_vllm_spec_honours_a_pin(tmp_path: Path) -> None:
    config = _config()
    config = config.model_copy(
        update={"extraction": config.extraction.model_copy(update={"vllm_version": "0.7.2"})}
    )
    kernel_dir, _ = materialize_extract_run(config, _unit(), tmp_path)
    assert 'VLLM_SPEC = "vllm==0.7.2"' in (kernel_dir / "extract_kernel.py").read_text()

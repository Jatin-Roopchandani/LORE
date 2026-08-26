"""Materialize the extraction kernel and its work-unit dataset."""
from __future__ import annotations

import shutil
from pathlib import Path

from lore.config import LoreConfig
from lore.orchestrator.workunit import WorkUnit, write_unit

TEMPLATES = Path(__file__).resolve().parents[2] / "kaggle"


def materialize_extract_run(
    config: LoreConfig, unit: WorkUnit, work_dir: Path
) -> tuple[Path, Path]:
    if not config.kaggle.username:
        raise ValueError("kaggle.username is not set in lore.toml")

    kernel_dir = work_dir / "extract"
    shutil.copytree(TEMPLATES / "extract", kernel_dir, dirs_exist_ok=True)

    staging = work_dir / "staging"
    unit_dir = write_unit(unit, staging)
    dataset_dir = work_dir / "extract_data"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    for item in unit_dir.iterdir():
        target = dataset_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    shutil.copy2(
        TEMPLATES / "extract_data" / "dataset-metadata.json",
        dataset_dir / "dataset-metadata.json",
    )

    spec = (
        f"vllm=={config.extraction.vllm_version}"
        if config.extraction.vllm_version
        else "vllm"
    )
    substitutions = {
        "EMBED_MODEL_ID": config.extraction.embed_model,
        "USERNAME": config.kaggle.username,
        "KERNEL_SLUG": config.kaggle.extract_kernel_slug,
        "DATASET_SLUG": config.kaggle.extract_dataset_slug,
        "MODEL_ID": config.extraction.model,
        "VLLM_SPEC": spec,
    }
    # MODEL_ID, EMBED_MODEL_ID and VLLM_SPEC each appear twice on their
    # definition line in extract_kernel.py: once as the bare Python
    # identifier and once as the quoted placeholder value, e.g.
    # `MODEL_ID = "MODEL_ID"`. A plain substring replace would rewrite the
    # identifier too (`Qwen/... = "Qwen/..."`), which is not valid Python.
    # Restrict those three keys to their quoted form so only the value is
    # substituted; USERNAME/KERNEL_SLUG/DATASET_SLUG never appear as bare
    # identifiers and need plain substring replacement since they show up
    # embedded inside larger JSON strings (e.g. "USERNAME/KERNEL_SLUG").
    quoted_only = {"MODEL_ID", "EMBED_MODEL_ID", "VLLM_SPEC"}
    for path in (
        kernel_dir / "kernel-metadata.json",
        kernel_dir / "extract_kernel.py",
        dataset_dir / "dataset-metadata.json",
    ):
        text = path.read_text()
        for key, value in substitutions.items():
            if key in quoted_only:
                text = text.replace(f'"{key}"', f'"{value}"')
            else:
                text = text.replace(key, value)
        path.write_text(text)
    return kernel_dir, dataset_dir

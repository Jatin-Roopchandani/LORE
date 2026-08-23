"""Phase 0 hello-world round trip: dataset up, GPU kernel run, output down."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from lore.config import KaggleConfig
from lore.kaggle_api.client import KaggleClient, KaggleRunError

TEMPLATES = Path(__file__).resolve().parents[2] / "kaggle"


def materialize_templates(
    config: KaggleConfig, work_dir: Path
) -> tuple[Path, Path]:
    """Copy kernel/dataset templates into work_dir, substituting USERNAME."""
    kernel_dir = work_dir / "hello"
    dataset_dir = work_dir / "hello_data"
    shutil.copytree(TEMPLATES / "hello", kernel_dir, dirs_exist_ok=True)
    shutil.copytree(TEMPLATES / "hello_data", dataset_dir, dirs_exist_ok=True)
    for meta in (kernel_dir / "kernel-metadata.json",
                 dataset_dir / "dataset-metadata.json"):
        meta.write_text(meta.read_text().replace("USERNAME", config.username))
    return kernel_dir, dataset_dir


def run_hello_roundtrip(
    config: KaggleConfig, work_dir: Path, on_tick=None
) -> dict:
    if not config.username:
        raise KaggleRunError("kaggle.username is not set in lore.toml")
    kernel_dir, dataset_dir = materialize_templates(config, work_dir)
    client = KaggleClient(config)
    client.push_dataset(dataset_dir)
    client.wait_dataset_ready()
    out_dir = client.run_and_wait(kernel_dir, work_dir / "out", on_tick=on_tick)
    output_file = out_dir / "output.json"
    if not output_file.is_file():
        raise KaggleRunError(f"kernel completed but {output_file} missing")
    result = json.loads(output_file.read_text())
    if result.get("sum") != 15.0:
        raise KaggleRunError(f"unexpected kernel output: {result}")
    return result

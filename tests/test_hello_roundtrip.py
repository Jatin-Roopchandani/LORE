import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from lore.config import KaggleConfig
from lore.kaggle_api.hello import materialize_templates, run_hello_roundtrip


def _cfg() -> KaggleConfig:
    return KaggleConfig(username="realuser", poll_interval_s=0, poll_timeout_s=1)


def test_materialize_substitutes_username(tmp_path: Path) -> None:
    kernel_dir, dataset_dir = materialize_templates(_cfg(), tmp_path)
    meta = json.loads((kernel_dir / "kernel-metadata.json").read_text())
    assert meta["id"] == "realuser/lore-hello"
    assert meta["dataset_sources"] == ["realuser/lore-hello-data"]
    ds_meta = json.loads((dataset_dir / "dataset-metadata.json").read_text())
    assert ds_meta["id"] == "realuser/lore-hello-data"
    assert (kernel_dir / "hello_kernel.py").is_file()
    assert (dataset_dir / "input.json").is_file()


def test_roundtrip_validates_output(tmp_path: Path) -> None:
    expected = {"sum": 15.0, "cuda_available": True, "device_name": "Tesla T4"}

    def fake_run_and_wait(kernel_dir: Path, dest: Path, on_tick=None) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "output.json").write_text(json.dumps(expected))
        return dest

    with patch("lore.kaggle_api.hello.KaggleClient") as cls:
        client = MagicMock()
        client.run_and_wait.side_effect = fake_run_and_wait
        cls.return_value = client
        result = run_hello_roundtrip(_cfg(), tmp_path)

    assert result == expected
    client.push_dataset.assert_called_once()
    client.wait_dataset_ready.assert_called_once()

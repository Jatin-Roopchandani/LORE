"""Kaggle free-tier batch backend, driven through the official `kaggle` CLI.

Transport is CLI-over-subprocess (not the python API) so credentials are
only ever touched by Kaggle's own tooling and tests can mock one seam.
"""
from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from lore.config import KaggleConfig


class KaggleRunError(RuntimeError):
    pass


class KaggleClient:
    def __init__(self, config: KaggleConfig) -> None:
        self._cfg = config
        self._last_status_output = ""

    @property
    def _kernel_ref(self) -> str:
        return f"{self._cfg.username}/{self._cfg.kernel_slug}"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=300
            )
        except subprocess.TimeoutExpired as exc:
            raise KaggleRunError(
                f"{' '.join(args)} timed out after 300s"
            ) from exc
        if result.returncode != 0:
            raise KaggleRunError(
                f"{' '.join(args)} failed rc={result.returncode}: {result.stderr}"
            )
        return result

    def authenticated(self) -> bool:
        return (Path.home() / ".kaggle" / "kaggle.json").is_file()

    def push_dataset(self, dir: Path) -> None:
        # `create` for the first push, `version` afterwards.
        try:
            result = subprocess.run(
                ["kaggle", "datasets", "create", "-p", str(dir), "--dir-mode", "zip"],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise KaggleRunError(
                "kaggle datasets create timed out after 300s"
            ) from exc
        if result.returncode != 0 or "already in use" in result.stdout + result.stderr:
            self._run([
                "kaggle", "datasets", "version", "-p", str(dir),
                "-m", "lore work unit", "--dir-mode", "zip",
            ])

    def push_kernel(self, dir: Path) -> None:
        self._run(["kaggle", "kernels", "push", "-p", str(dir)])

    def kernel_status(self) -> str:
        result = self._run(["kaggle", "kernels", "status", self._kernel_ref])
        self._last_status_output = result.stdout.strip()
        match = re.search(r'"(?:\w+\.)?(\w+)"', result.stdout)
        status = match.group(1).lower() if match else "unknown"
        return status if status in {"running", "complete", "error", "queued"} else "unknown"

    def pull_output(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        self._run(["kaggle", "kernels", "output", self._kernel_ref, "-p", str(dest)])

    def run_and_wait(
        self,
        kernel_dir: Path,
        dest: Path,
        on_tick: Callable[[str], None] | None = None,
    ) -> Path:
        self.push_kernel(kernel_dir)
        deadline = time.monotonic() + self._cfg.poll_timeout_s
        while True:
            status = self.kernel_status()
            if on_tick:
                on_tick(status)
            if status == "complete":
                self.pull_output(dest)
                return dest
            if status == "error":
                raise KaggleRunError(f"kernel {self._kernel_ref} finished with error")
            if time.monotonic() > deadline:
                raise KaggleRunError(
                    f"kernel {self._kernel_ref} timed out after "
                    f"{self._cfg.poll_timeout_s}s (last status: {status}; "
                    f"last output: {self._last_status_output!r})"
                )
            time.sleep(self._cfg.poll_interval_s)

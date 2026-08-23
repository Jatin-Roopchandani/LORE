"""LORE Phase 0 hello kernel: prove GPU + dataset transport end to end."""
import json
from pathlib import Path

import torch

input_root = Path("/kaggle/input")
print("input tree:", sorted(str(p) for p in input_root.rglob("*"))[:50])

# Locate by search rather than a hardcoded path: the mount layout can vary
# with dataset packaging (dir-mode, versioning).
candidates = sorted(input_root.rglob("input.json"))
if not candidates:
    raise FileNotFoundError(f"input.json not found anywhere under {input_root}")
numbers = json.loads(candidates[0].read_text())["numbers"]

tensor = torch.tensor(numbers, dtype=torch.float32)
if torch.cuda.is_available():
    tensor = tensor.cuda()

result = {
    "sum": float(tensor.sum().item()),
    "cuda_available": torch.cuda.is_available(),
    "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
}
Path("/kaggle/working/output.json").write_text(json.dumps(result))
print(json.dumps(result))

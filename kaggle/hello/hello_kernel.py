"""LORE Phase 0 hello kernel: prove GPU + dataset transport end to end."""
import json
from pathlib import Path

import torch

data_path = Path("/kaggle/input/lore-hello-data/input.json")
numbers = json.loads(data_path.read_text())["numbers"]

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

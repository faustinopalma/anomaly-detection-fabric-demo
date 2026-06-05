"""Quick isolated check: does dynamo=False produce a compact single-file FP16
ONNX (no external .data sidecar) within the Kusto 1 MB row budget?"""
from __future__ import annotations
import base64, copy, sys
from pathlib import Path
import torch, onnx
import onnxruntime as ort
import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "cloud-training" / "src"))
import generate_and_train as G  # noqa: E402

out = REPO / "_local" / "_export_test"
out.mkdir(parents=True, exist_ok=True)
fp16_path = out / "model.fp16.onnx"

torch.manual_seed(0)
ae = G.TransformerAE().eval()
wrap = G.ScoreWrapperFP16(copy.deepcopy(ae)).eval()
dummy = torch.randn(1, G.WINDOW, G.N_FEATURES, dtype=torch.float32)

torch.onnx.export(
    wrap, dummy, fp16_path.as_posix(),
    input_names=["window"], output_names=["score"],
    dynamic_axes={"window": {0: "batch"}, "score": {0: "batch"}},
    opset_version=17, do_constant_folding=True, dynamo=False)

m = onnx.load(fp16_path.as_posix())
if m.ir_version > G.SANDBOX_IR_VERSION:
    m.ir_version = G.SANDBOX_IR_VERSION
onnx.save(m, fp16_path.as_posix())
onnx.checker.check_model(m)

raw = fp16_path.read_bytes()
b64 = base64.b64encode(raw)
data_sidecar = list(out.glob("*.data"))
sess = ort.InferenceSession(fp16_path.as_posix(), providers=["CPUExecutionProvider"])
x = np.random.randn(8, G.WINDOW, G.N_FEATURES).astype(np.float32)
y = sess.run(["score"], {"window": x})[0]

print(f"raw_kb={len(raw)/1024:.1f}  b64_kb={len(b64)/1024:.1f}  "
      f"fits={len(b64) <= G.KUSTO_ROW_BUDGET_BYTES}  sidecars={[p.name for p in data_sidecar]}")
print(f"ort output shape={y.shape} sample={y[:3]}")

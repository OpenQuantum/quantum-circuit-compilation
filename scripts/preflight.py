"""Run official metric file readers against the baseline, without simulations."""
import importlib.util
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'official/data/verifiers'))
spec = importlib.util.spec_from_file_location('official_metric', root / 'official/metric/google_quantum_ai_metric.py')
metric = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metric)
submission = root / 'official/data/sample_submission'
summary = {}
for name, cap in [('invsqrt', 1), ('dqi', 4165)]:
    circuit = metric.read_circuit(submission / f'{name}.kmb', cap)
    summary[name] = {'qubits': circuit.num_qubits, 'bits': circuit.num_bits, 'registers': circuit.num_registers}
    del circuit
for k in range(10):
    circuit = metric.read_circuit(submission / f'dfthcsos/angle_k_{k}.kmb', 128)
    summary[f'angle_k_{k}'] = {'qubits': circuit.num_qubits}
from dfthcsos.scoreinfra.reference import ORBITALS
arrays = metric.read_npz(submission / 'dfthcsos/submission.npz', {
    'U': ('f', ('R', 'B', ORBITALS)), 'W': ('f', ('R', 'B', 'C')),
    'H2_I': ('f', ('R', 'C')), 'H1_I': ('f', ()),
    'Hsym_lower': ('f', (ORBITALS * (ORBITALS + 1) // 2,)),
    'tree_edges': ('iu', (ORBITALS - 1, 2)),
})
summary['array_shapes'] = {key: list(value.shape) for key, value in arrays.items()}
summary['status'] = 'PREFLIGHT_PASS'
summary['full_verification'] = False
(root / 'runs/preflight.json').write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))

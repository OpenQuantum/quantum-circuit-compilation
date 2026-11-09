"""Smoke-test generator: keep the pre-populated official seed unchanged."""

import argparse
from pathlib import Path
import kickmix

parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
assert args.output_dir.is_dir()
print("Seed files:", sorted(p.name for p in args.output_dir.iterdir()))
for path in args.output_dir.glob("*.km[bx]"):
    circuit = kickmix.Circuit.from_file(path, format=path.suffix[1:])
    circuit.to_file(path, format=path.suffix[1:])
    print("Round-trip compiled:", path.name, circuit.num_qubits)

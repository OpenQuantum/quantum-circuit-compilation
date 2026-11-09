"""Runs under the existing Python 3.12 verifier environment, not the World SDK."""

import importlib.util
import json
from pathlib import Path
import stat
import sys


def check(task, folder, project):
    folder, project = Path(folder), Path(project)
    for p in folder.rglob("*"):
        s = p.lstat()
        if not stat.S_ISREG(s.st_mode) and not stat.S_ISDIR(s.st_mode):
            raise ValueError("Only regular files/directories are allowed")
        if stat.S_ISREG(s.st_mode) and (s.st_nlink > 1 or s.st_size > 2**31):
            raise ValueError("File link/size limit")
    spec = importlib.util.spec_from_file_location(
        "official_metric", project / "official/metric/google_quantum_ai_metric.py"
    )
    metric = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(metric)
    if task != "dfthcsos":
        files = list(folder.glob(task + ".km[bx]"))
        if len(files) != 1:
            raise ValueError("Exactly one circuit per task required")
        metric.read_circuit(files[0], 1 if task == "invsqrt" else 4165)
        return str(files[0])
    paths = list(folder.glob("angle_k_*.km[bx]"))
    if (
        not paths
        or len({p.stem for p in paths}) != len(paths)
        or {p.stem for p in paths} != {f"angle_k_{i}" for i in range(len(paths))}
    ):
        raise ValueError("ANGLE indices must start at 0 and be consecutive")
    for path in paths:
        c = metric.read_circuit(path, 128)
        if c.num_qubits > 128:
            raise ValueError("ANGLE qubit cap exceeded")
    n = 76
    metric.read_npz(
        folder / "submission.npz",
        {
            "U": ("f", ("R", "B", n)),
            "W": ("f", ("R", "B", "C")),
            "H2_I": ("f", ("R", "C")),
            "H1_I": ("f", ()),
            "Hsym_lower": ("f", (n * (n + 1) // 2,)),
            "tree_edges": ("iu", (n - 1, 2)),
        },
    )
    return str(folder)


if __name__ == "__main__":
    try:
        print(json.dumps({"status": "PASS", "input": check(*sys.argv[1:])}))
    except Exception as error:
        print(json.dumps({"status": "FAIL", "error": str(error)[:1000]}))
        sys.exit(1)

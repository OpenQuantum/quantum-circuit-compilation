"""Identify the supplied Kaggle baseline and summarize bounded verifier evidence."""

import hashlib
import json
import re

from .store import WorldError


# SHA-256 of the named members of the original official-data.zip, verified
# against the archive identified in the worker handoff's SHA256SUMS.
OFFICIAL_ARCHIVE_SHA256 = "908ed2e587ca4ed500ddac7b6da4b473af3c8779c983d5022de620b357791a30"
OFFICIAL_SEED_SHA256 = "e2bf8eb64350a4460a271fc7f1d591ef125b1e276377e9a0f64b96ab3ca321e1"
OFFICIAL_REFERENCE_SHA256 = "bb90330dcf774f7b502de471293facba67f27952cc8808fe4a9970a9b1322e23"
OFFICIAL_VERIFIER_SHA256 = "ddef520dec6b68d1cf0a6f328ddd36d79eb84570b9192cf3f7acaf073f06733a"
REFERENCE_PATH = "official/data/verifiers/invsqrt/nr_reference_circuit.py"
VERIFIER_PATH = "official/data/verifiers/invsqrt/verify_submission_invsqrt.py"
METRIC_PATH = "official/metric/google_quantum_ai_metric.py"
BASELINE_RECEIPT = "runs/invsqrt-full.json"
REFERENCE_SECTION_LIMIT = 5000
# Hash the parsed receipt so Git line endings and JSON formatting are immaterial.
BASELINE_RECEIPT_SHA256 = "d6e4384b340fef3f870ccd67d5b53c45df6dd3987dbc8d897e598059efcbef1c"
REPORT_NUMBERS = (
    "score", "toffoli_equivalent", "toffoli_count", "max_toffoli_count", "t_count",
    "rotation_count", "rotation_eps", "num_qubits", "reaction_depth", "num_ops",
    "verified_shots", "worst_error_over_tolerance", "worst_x",
)


def optional_digest(path):
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


def circuit_seed_files(directory, task):
    return [
        path for path in directory.glob(task + ".km[bx]")
        if path.is_file() and not path.is_symlink()
    ]


def seed_identity(path):
    digest = optional_digest(path) if path else None
    source = "unavailable"
    if digest:
        source = (
            "official_sample_submission" if digest == OFFICIAL_SEED_SHA256 else "configured_seed"
        )
    return {"filename": path.name if digest else None, "sha256": digest, "source": source}


def read_reference(project_root, start_line, line_count):
    path = project_root / REFERENCE_PATH
    try:
        if path.is_symlink():
            raise OSError
        with path.open("rb") as stream:
            source = stream.read(64 * 1024 + 1)
    except OSError:
        raise WorldError("REFERENCE_UNAVAILABLE", "The verified official reference is unavailable.") from None
    if hashlib.sha256(source).hexdigest() != OFFICIAL_REFERENCE_SHA256:
        raise WorldError("REFERENCE_UNAVAILABLE", "The verified official reference is unavailable.")
    lines = source.decode("utf-8").splitlines(keepends=True)
    if start_line > len(lines):
        raise WorldError("REFERENCE_RANGE", "Start line exceeds the official reference length.")
    selected = []
    byte_count = 0
    for line in lines[start_line - 1:start_line - 1 + line_count]:
        byte_count += len(line.encode("utf-8"))
        if byte_count > REFERENCE_SECTION_LIMIT:
            break
        selected.append(line)
    end_line = start_line + len(selected) - 1
    return {
        "source": "invsqrt.nr_reference_circuit",
        "sha256": OFFICIAL_REFERENCE_SHA256,
        "total_lines": len(lines),
        "start_line": start_line,
        "end_line": end_line,
        "next_line": end_line + 1 if end_line < len(lines) else None,
        "text": "".join(selected),
    }


def bounded_report(report):
    result = {}
    if report.get("status") in ("PASS", "FAIL"):
        result["status"] = report["status"]
    circuit_sha256 = report.get("circuit_sha256")
    if isinstance(circuit_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", circuit_sha256):
        result["circuit_sha256"] = circuit_sha256
    for key in REPORT_NUMBERS:
        value = report.get(key)
        if type(value) in (int, float) and 0 <= value < 1e30:
            result[key] = value
    return result


def historical_baseline(project_root):
    try:
        with (project_root / BASELINE_RECEIPT).open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return None
        report = json.loads(raw)
        normalized = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    except (OSError, ValueError):
        return None
    if hashlib.sha256(normalized).hexdigest() != BASELINE_RECEIPT_SHA256:
        return None
    return {
        "receipt": BASELINE_RECEIPT, "mode": "full", "scope": "historical_local_verification",
        "report": bounded_report(report),
    }


def task_context(settings):
    try:
        paths = circuit_seed_files(settings.seed_path, "invsqrt")
    except OSError:
        paths = []
    reference_sha256 = optional_digest(settings.project_root / REFERENCE_PATH)
    verifier_sha256 = optional_digest(settings.project_root / VERIFIER_PATH)
    source_status = "unavailable"
    if reference_sha256:
        source_status = (
            "verified_official" if reference_sha256 == OFFICIAL_REFERENCE_SHA256 else "hash_mismatch"
        )
    reference = {
        "archive_sha256": OFFICIAL_ARCHIVE_SHA256,
        "sample_sha256": OFFICIAL_SEED_SHA256,
        "module": "invsqrt.nr_reference_circuit",
        "source_path": REFERENCE_PATH,
        "source_tool": "ReadInversqrtReference" if source_status == "verified_official" else None,
        "source_status": source_status,
        "source_sha256": reference_sha256,
        "expected_source_sha256": OFFICIAL_REFERENCE_SHA256,
        "verifier_sha256": verifier_sha256,
        "expected_verifier_sha256": OFFICIAL_VERIFIER_SHA256,
        "usage": (
            "When the reference source is verified, use `from invsqrt.nr_reference_circuit import "
            "Precision, build_nr_circuit` in the generator. Precision describes fixed-point register "
            "widths. dataclasses.replace can copy a Precision instance with selected fields changed. "
            "build_nr_circuit(n_p=10, prec=Precision()) produces the reference phase circuit. "
            "Keep compute_only false for verification. Write the circuit with "
            "circuit.to_file(output_path, format='kmb'). The fixed_point_model helps screen "
            "arithmetic error. The official verifier also checks phase, scratch cleanup and "
            "input restoration."
        ),
        "cost_formula": (
            "Q × (mean executed Toffoli-like count + effective T count / 4). Q is circuit.num_qubits. "
            "Effective T includes base T and rotation synthesis, whose cost depends on remaining phase "
            "tolerance. Use the verifier's score and toffoli_equivalent. Its t_count reports base T only."
        ),
    }
    receipt = historical_baseline(settings.project_root)
    if receipt:
        reference["historical_baseline"] = receipt
    return {
        "seed": seed_identity(paths[0] if len(paths) == 1 else None),
        "official_reference": reference,
    }

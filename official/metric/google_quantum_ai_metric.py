"""Kaggle metric for 161954: verifiable quantum circuit compilation benchmarks.

A submission is an archive holding one entry per benchmark, with every circuit in either the text
(.kmx) or the binary (.kmb) kickmix format:

    invsqrt.kmx or invsqrt.kmb   Benchmark I: phasing by beta / sqrt(x), at n_p = 10.
    dqi.kmx or dqi.kmb           Benchmark II: the DQI Reed-Solomon decoder, RS(4095, 4025) over GF(2^12).
    dfthcsos/                    Benchmark III: the DFTHC-SOS block encoding of FeMoCo76o113e, as the
                                 ANGLE_k circuits angle_k_0 ... angle_k_<n-1>, each .kmx or .kmb,
                                 and submission.npz, holding the factorization and the Givens tree.

Each benchmark is checked by its verifier, from the competition data, which simulates the circuits on many test inputs,
rejects the submission if any test fails, and otherwise returns its cost (lower is better). Each
leaderboard split seeds the simulations with its own secret salt, so every benchmark is verified once
per split. A split's score is the geometric mean over the benchmarks of `baseline_cost / cost`: the
hosts' reference submissions score 1.0, and halving every benchmark's cost scores 2.0.

The verifiers and kickmix come from the competition data itself, attached to the metric, so it scores
with exactly the files participants download: verifiers/ and kickmix/, built by the 161954 prep
pipeline from pinned commits of the hosts' code and Qualtran. kickmix.so is compiled for that
build's Kaggle image, so the metric must run in that same image. To run the metric locally instead,
install kickmix and the verifiers; local installs take precedence over the competition data.
"""

import json
import math
import pathlib
import random
import stat
import sys
import time
import tokenize
import zipfile
import zlib
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

# kickmix/ and verifiers/ in the competition data, at the 161954 prep pipeline's COMPETITION_DATA_MOUNT.
KICKMIX_DIR = '/kaggle/input/competitions/google-verifiable-quantum-circuit-compilation/kickmix'
VERIFIERS_DIR = '/kaggle/input/competitions/google-verifiable-quantum-circuit-compilation/verifiers'
# Where the orchestrator, and run_metric_locally, put the solution and metadata files.
EVAL_INPUTS_DIR = pathlib.Path('/kaggle/input/competition_evaluation')

# Every kickmix instruction, as named by Circuit.operation_statistics. Listing them rejects any
# instruction a future Qualtran adds until it is reviewed. INFO_TAG is a no-op in simulation;
# kickmix's CircuitBuilder emits it around every iadd and isub. DEBUG_PRINT is a no-op too, as every
# verifier's simulator ignores it, but its qubit and bit ids still count toward the circuit's.
ALLOWED_INSTRUCTIONS = {
    'X', 'CX', 'CCX', 'Z', 'CZ', 'CCZ', 'Z_POW', 'NEG', 'SWAP', 'CSWAP', 'R', 'HMR', 'BIT_INVERT', 'BIT_STORE0', 'BIT_STORE1',
    'PUSH_CONDITION', 'POP_CONDITION', 'INFO_TAG', 'DEBUG_PRINT',
}  # fmt: skip
# Qubit and bit ids must be below this. Verifier memory grew by ~1.5 KB per bit (dqi) and ~1.1 KB
# per qubit (invsqrt) with the verifiers at c636a96, so this capped the growth at ~1 GB; at 03027f8,
# dqi's grows by only ~0.2 KB per qubit or bit, linearly rather than with their product. The
# baselines use ids below 50,000. Raise it if a harder benchmark needs more.
MAX_QUBIT_OR_BIT_ID = 2**30 // 1536
# The arrays read from a dfthcsos submission.npz may total at most this many bytes, ~30 times the
# ~2 MB the reference factorization and tree take.
MAX_NPZ_BYTES = 2**26


class ParticipantVisibleError(Exception):
    pass


def read_circuit(path: pathlib.Path, max_registers: int) -> Any:
    """Return a submitted .kmx or .kmb circuit as a kickmix.Circuit; raise ValueError unless it is fit to verify.

    kickmix validates a file as it parses it, within limits that keep parsing cheap, but simulating
    takes memory that grows with the largest qubit and bit ids, which kickmix allows up to ~5e8,
    so those are capped at MAX_QUBIT_OR_BIT_ID. kickmix also allows a million registers, at
    ~110 bytes each even if empty, so a circuit may have at most `max_registers`, the most its
    benchmark needs. Every register must be named: the dfthcsos verifier crashes on an unnamed one.
    """
    import kickmix  # ty: ignore[unresolved-import]  # In kickmix/, which generate_standard_submission put on sys.path.

    circuit = kickmix.Circuit.from_file(path, format=path.suffix.removeprefix('.'))
    unsupported = set(circuit.operation_statistics()) - ALLOWED_INSTRUCTIONS
    if unsupported:
        raise ValueError(f'unsupported instructions {sorted(unsupported)}')
    largest_id = max(circuit.num_qubits, circuit.num_bits) - 1
    if largest_id >= MAX_QUBIT_OR_BIT_ID:
        raise ValueError(f'qubit or bit id {largest_id} is above the limit of {MAX_QUBIT_OR_BIT_ID - 1}')
    if circuit.num_registers > max_registers:
        raise ValueError(f'{circuit.num_registers} registers, above the limit of {max_registers} for this benchmark')
    if not all(isinstance(name, str) for name in circuit.register_data):
        raise ValueError('every register needs a name, given by its REGISTER line')
    return circuit


def read_npz(path: pathlib.Path, spec: Mapping[str, tuple[str, tuple[int | str, ...]]]) -> dict[str, np.ndarray]:
    """Return the arrays `spec` names from an .npz; raise ValueError unless each is a plain number array as specified.

    `spec` maps each key to the numpy dtype kinds it may have, such as 'f' for floats, and its shape,
    in which a string names a dimension of any positive size that must agree wherever it recurs.

    Each array's header is a Python literal of its dtype and shape, which np.load trusts: a few KB
    of deflated zeros can claim a terabyte-sized array, and the dtype can be anything numpy can
    describe, from structured and nested types to Python objects that need unpickling. So the
    headers are read first, and the arrays loaded only if each is a plain number type of its kind,
    not big-endian, with its shape, and they total at most MAX_NPZ_BYTES. Only format 1.0 headers,
    which numpy.savez writes, are accepted, and numpy refuses any over 10 KB before parsing it.
    """
    dimensions: dict[str, int] = {}
    total = 0
    with zipfile.ZipFile(path) as archive:
        members = {name.removesuffix('.npy'): name for name in archive.namelist()}
        missing = [key for key in spec if key not in members]
        if missing:
            raise ValueError(f'{path.name} has no {missing}')
        for key, (kinds, expected_shape) in spec.items():
            with archive.open(members[key]) as member:
                if np.lib.format.read_magic(member) != (1, 0):
                    raise ValueError(f'{key} in {path.name} is not in .npy format 1.0, which numpy.savez writes')
                shape, _, dtype = np.lib.format.read_array_header_1_0(member)
            if dtype.kind not in kinds or dtype.names is not None or dtype.subdtype is not None or dtype.byteorder == '>':
                raise ValueError(f'{key} in {path.name} has dtype {dtype}, not a plain number type of numpy kind {kinds!r}')
            matches = len(shape) == len(expected_shape) and all(
                size > 0 and size == (dimensions.setdefault(expected, size) if isinstance(expected, str) else expected)
                for size, expected in zip(shape, expected_shape, strict=True)
            )
            if not matches:
                raise ValueError(f'{key} in {path.name} has shape {shape}, not {expected_shape} with its named dimensions as in {dimensions}')
            total += math.prod(shape) * dtype.itemsize
    if total > MAX_NPZ_BYTES:
        raise ValueError(f'the arrays in {path.name} take {total:,} bytes, above the limit of {MAX_NPZ_BYTES:,}')
    with np.load(path, allow_pickle=False) as npz:
        return {key: np.array(npz[key]) for key in spec}


def find_one(root: pathlib.Path, name: str, description: str, is_dir: bool = False) -> pathlib.Path:
    """The one path called `name` (a glob) under `root`, where the submission unpacked; raise ParticipantVisibleError otherwise."""
    # macOS Finder's Compress adds __MACOSX/, which mirrors every directory, with an AppleDouble ._ file per file.
    paths = [path for path in root.rglob(name) if path.is_dir() == is_dir and '__MACOSX' not in path.relative_to(root).parts]
    if len(paths) != 1:
        raise ParticipantVisibleError(f'The submission must contain exactly one {description}, found {len(paths)}.')
    return paths[0]


def generate_standard_submission(submission_dir: str) -> None:
    """Verify the submission under every split's salt, and write each verification's cost as submission.csv.

    `submission_dir` is where the orchestrator unpacked the submission archive. Every split's verifications run here, in random order, before the orchestrator calls score() for
    either split, so neither a failure's timing nor its message shows which split it failed on.
    Every file is read and checked before any verifier runs, so a malformed file fails in seconds
    rather than after another benchmark's verification. The verifiers run one at a time, as the
    invsqrt and dqi verifiers spread their own simulation across processes.

    The orchestrator passes neither the solution nor the metadata in, so the solution is read from
    where it puts it; this metric's own solution names its row id column `row_id`. Each row's
    `seed_salt` is merged by its verifier with the submission's hash to seed the simulation, so a
    submission always faces the same tests, but participants can't compute them. Unsalted, the seed
    would let a team whose circuit fails on a few inputs re-roll its tests offline with cost-free
    edits until they miss those inputs; with the salt, each re-roll costs a submission. A salt that
    is missing or short is refused, as it would leave the seed unsalted or guessable.

    kickmix and the verifiers are imported here rather than at the top because the orchestrator imports
    this module during validation, when the competition data isn't mounted.
    """
    solution_paths = list(EVAL_INPUTS_DIR.glob('solution.*'))
    if len(solution_paths) != 1:
        raise ValueError(f'Expected one solution.csv or solution.parquet in {EVAL_INPUTS_DIR}, found {[path.name for path in solution_paths]}')
    [solution_path] = solution_paths
    solution = pd.read_csv(solution_path) if solution_path.suffix == '.csv' else pd.read_parquet(solution_path)
    scored = solution[solution['Usage'] != 'Ignored']
    # score()'s hyperparameters, which the orchestrator passes only to score().
    hyperparameters = json.loads((EVAL_INPUTS_DIR / 'metadata.json').read_text()).get('Hyperparameters') or {}
    shot_divisor = int(hyperparameters.get('verification_shot_divisor', 1))
    for salt in scored['seed_salt']:
        if not isinstance(salt, str) or len(salt) < 32:
            raise ValueError(f'Every scored solution row needs a seed_salt of at least 32 characters, got {salt!r}')

    # Appended, not prepended, so local installs of kickmix and the verifiers win when running locally.
    sys.path += [KICKMIX_DIR, VERIFIERS_DIR]
    from dfthcsos import verify as dfthcsos_verify  # ty: ignore[unresolved-import]  # These live in verifiers/.
    from dfthcsos.scoreinfra.reference import FACTORIZATION_KEYS, ORBITALS  # ty: ignore[unresolved-import]
    from dqi import verify_submission_dqi  # ty: ignore[unresolved-import]
    from invsqrt import verify_submission_invsqrt  # ty: ignore[unresolved-import]

    root = pathlib.Path(submission_dir)

    def read(path: pathlib.Path, reader: Callable[[pathlib.Path, Any], Any], spec: Any) -> Any:
        # tar and 7z submissions can unpack links, which would have this read the host's files, and
        # FIFOs and devices, which would block or never end.
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink > 1 or not path.resolve().is_relative_to(root.resolve()):
            raise ParticipantVisibleError(f'{path.relative_to(root)} must be a regular file, not a link or a special file.')
        try:
            return reader(path, spec)
        # What kickmix, zipfile, zlib and numpy raise on malformed files, found by fuzzing an .npz.
        except (ValueError, RuntimeError, OSError, EOFError, zipfile.BadZipFile, zlib.error, tokenize.TokenError) as err:
            raise ParticipantVisibleError(f'{path.relative_to(root)} is invalid: {err}') from err

    batches = max(1, verify_submission_dqi.DEFAULT_NUM_SHOTS // verify_submission_dqi.DEFAULT_BATCH_SIZE // shot_divisor)
    dqi_spec = verify_submission_dqi.ProblemSpecDQI.benchmark_instance(num_shots=batches * verify_submission_dqi.DEFAULT_BATCH_SIZE)
    # Each circuit's most registers: invsqrt's x, and dqi's S_i and E_j.
    invsqrt = read(find_one(root, 'invsqrt.km[xb]', 'invsqrt.kmx or invsqrt.kmb'), read_circuit, 1)
    dqi = read(find_one(root, 'dqi.km[xb]', 'dqi.kmx or dqi.kmb'), read_circuit, dqi_spec.n + dqi_spec.m)
    dfthcsos_dir = find_one(root, 'dfthcsos', 'dfthcsos directory', is_dir=True)
    angle_files = list(dfthcsos_dir.glob('angle_k_*.km[xb]'))
    angle_paths = {path.stem: path for path in angle_files}
    # A circuit given in both formats leaves fewer stems than files, so the two can't match.
    stems = [f'angle_k_{k}' for k in range(len(angle_files))]
    if not stems or sorted(angle_paths) != sorted(stems):
        raise ParticipantVisibleError('dfthcsos/ must hold the circuits angle_k_0 ... angle_k_<n-1>, each once, as .kmx or .kmb.')
    # An ANGLE_k circuit's `selection` and `word_j` registers each hold a qubit, of at most QUBIT_CAP.
    # The verifier allows other registers too, but none are needed.
    angle_circuits = [read(angle_paths[stem], read_circuit, dfthcsos_verify.QUBIT_CAP) for stem in stems]
    # The factorization (see dfthcsos.scoreinfra.reference), with R factors of B vectors over the N
    # orbitals and C copies, and the Givens tree's N - 1 edges.
    npz_spec = {
        'U': ('f', ('R', 'B', ORBITALS)),
        'W': ('f', ('R', 'B', 'C')),
        'H2_I': ('f', ('R', 'C')),
        'H1_I': ('f', ()),
        'Hsym_lower': ('f', (ORBITALS * (ORBITALS + 1) // 2,)),
        'tree_edges': ('iu', (ORBITALS - 1, 2)),
    }
    if set(npz_spec) != {*FACTORIZATION_KEYS, 'tree_edges'}:
        raise ValueError(f'The dfthcsos verifier reads the factorization arrays {FACTORIZATION_KEYS}, which npz_spec must cover exactly')
    npz = read(find_one(dfthcsos_dir, 'submission.npz', 'dfthcsos/submission.npz'), read_npz, npz_spec)
    tree_edges = npz.pop('tree_edges')

    # Benchmark -> its verifier run, given the salt.
    verifications: dict[str, Callable[[str], dict[str, Any]]] = {
        'invsqrt': lambda salt: verify_submission_invsqrt.verify(invsqrt, verify_submission_invsqrt.ProblemSpecInvSqrt.for_n_p(10), salt=salt),
        'dqi': lambda salt: verify_submission_dqi.verify(dqi, dqi_spec, salt=salt),
        'dfthcsos': lambda salt: dfthcsos_verify.verify(
            angle_circuits, npz, tree_edges, salt=salt, shots_per_address=max(1, dfthcsos_verify.SHOTS_PER_ADDRESS // shot_divisor)
        ),
    }
    runs = list(zip(scored['row_id'], scored['benchmark'], scored['seed_salt'], strict=True))
    random.shuffle(runs)
    # Ignored rows aren't scored, but the orchestrator still expects a submission row for each.
    costs, seconds = dict.fromkeys(solution['row_id'], 0.0), dict.fromkeys(solution['row_id'], 0.0)
    for row_id, benchmark, salt in runs:
        start = time.monotonic()
        try:
            costs[row_id] = float(verifications[benchmark](salt)['score'])
        # The exceptions the verifiers' command lines report as a failed submission; dfthcsos's
        # InvalidCircuits is a ValueError.
        except (AssertionError, ValueError, RuntimeError, KeyError) as err:
            raise ParticipantVisibleError(f'The {benchmark} submission failed verification: {err}') from err
        if not costs[row_id] > 0:
            raise ParticipantVisibleError(f'The {benchmark} submission has non-positive cost {costs[row_id]}.')
        seconds[row_id] = time.monotonic() - start
    pd.DataFrame({'row_id': list(costs), 'cost': list(costs.values()), 'seconds': list(seconds.values())}).to_csv('submission.csv', index=False)


def score(
    solution: pd.DataFrame, submission: pd.DataFrame, row_id_column_name: str, verification_shot_divisor: int = 1
) -> tuple[float, dict[str, Any]]:
    """Return the geometric mean over the benchmarks of `baseline_cost / cost`, where higher is better.

    `verification_shot_divisor` is for testing only, and must stay 1 on Kaggle. generate_standard_submission
    reads it, and divides the dqi and dfthcsos verifiers' default numbers of shots by it, which makes
    their verification that much faster and weaker, and changes their costs, so that the reference
    submissions no longer score 1.0. invsqrt's verifier is the fastest, and needs a multiple of 64
    shots per input to fill its batches, its default, so it keeps every shot.

    The solution has a row per benchmark and leaderboard split, holding the split's secret
    `seed_salt` and the `baseline_cost` its benchmark's reference submission scored under that salt.
    `submission` is generate_standard_submission's output, which holds each row's `cost`, as the
    verification runs there. Returns each benchmark's cost, improvement and verification time as
    details.
    """
    merged = solution.merge(submission, on=row_id_column_name, validate='one_to_one')
    details = {
        benchmark: {'cost': cost, 'improvement_over_baseline': baseline_cost / cost, 'seconds': run_seconds}
        for benchmark, cost, baseline_cost, run_seconds in zip(
            merged['benchmark'], merged['cost'], merged['baseline_cost'], merged['seconds'], strict=True
        )
    }
    ratios = [benchmark_details['improvement_over_baseline'] for benchmark_details in details.values()]
    return math.prod(ratios) ** (1 / len(ratios)), details

def rank_cost(r):
    # Cost used to rank a manifest row within its task (lower is better); None if unscored. A DQI cost at one salt is
    # one draw of its sampled failures (one or two phase errors move R from 1.19126 to 1.19673 / 1.20139), so DQI
    # ranks by dqi_expected_cost (mean over a 48-draw sweep) when known, else dqi_zero_failure_cost (R floor x Q^2 x
    # mean Toffolis), else local_task_score (older manifests). invsqrt and chemistry rank by local_task_score.
    keys = ("dqi_expected_cost", "dqi_zero_failure_cost", "local_task_score") if r.get("task") == "dqi" \
        else ("local_task_score",)
    for k in keys:
        try:
            v = float(r.get(k) or "nan")
        except ValueError:
            continue
        if v == v:
            return v
    return None

def rank_basis(r):
    # Which column rank_cost used, for the tables
    if r.get("task") != "dqi":
        return "default salt"
    return ("expected (48 draws)" if r.get("dqi_expected_cost") else
            "zero-failure" if r.get("dqi_zero_failure_cost") else "default salt")

# Tie rule: rows within REL_TIE (relative) of the task's cheapest ranking cost are a tie, and the tie goes to the
# earliest public circuit (the original). A byte-different copy of a circuit changes the verifier's measurement
# draw, so it can come out a hair cheaper by noise alone; it should not displace the original's designer.
import datetime as _dt
REL_TIE = 1e-5

def public_when(r):
    # First non-empty, readable public_since / first_known / release_date as UTC (date only = 00:00 UTC); else None
    for k in ("public_since", "first_known", "release_date"):
        s = (r.get(k) or "").strip()
        if not s:
            continue
        try:
            d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            continue
        return d.replace(tzinfo=_dt.timezone.utc) if d.tzinfo is None else d.astimezone(_dt.timezone.utc)
    return None

def rank_order(rows):
    # Scored rows of one task: first the pick, by a replay in public order (earliest first; missing dates last,
    # then lowest cost, then sha256) in which a later row takes the pick only by beating it by more than REL_TIE
    # (so a tie never moves the pick, however other rows move the cheapest); then the rest of the tie group
    # (cost <= cheapest x (1 + REL_TIE)) in public order; then the others by cost, then sha256
    scored = [(rank_cost(r), r) for r in rows]
    scored = [(c, r) for c, r in scored if c is not None]
    if not scored:
        return []
    limit = min(c for c, _ in scored) * (1 + REL_TIE)
    far = _dt.datetime.max.replace(tzinfo=_dt.timezone.utc)
    def tie_key(cr):
        w = public_when(cr[1])
        return (w is None, w or far, cr[0], cr[1].get("sha256") or cr[1].get("sha12") or "")
    by_time = sorted(scored, key=tie_key)
    pick = by_time[0]
    for cr in by_time[1:]:
        if pick[0] > cr[0] * (1 + REL_TIE):
            pick = cr
    tied = [pick] + [cr for cr in by_time if cr is not pick and cr[0] <= limit]
    rest = sorted((cr for cr in scored if cr[0] > limit),
                  key=lambda cr: (cr[0], cr[1].get("sha256") or cr[1].get("sha12") or ""))
    return [r for _, r in tied + rest]

def pick_best(rows_for_task):
    # The row to use for a task: rank_order()[0], or None if no row is scored
    order = rank_order(rows_for_task)
    return order[0] if order else None


import csv, glob, hashlib, json, shutil, sys, time, zipfile
from math import prod
from pathlib import Path

T0 = time.monotonic()
import numpy as np
import pandas as pd
from IPython.display import HTML, display

TASK_NAMES = {"invsqrt": "Inverse square root", "dqi": "DQI decoder", "chemistry": "Chemistry (dfthcsos)"}

def show(df, height=None):
    # A scrollable HTML table: readable on the notebook page, with live links
    html = df.to_html(index=False, render_links=True, escape=True, float_format=lambda x: f"{x:,.6g}")
    style = f"max-height:{height}px;overflow:auto;" if height else "overflow:auto;"
    display(HTML(f'<div style="{style}">{html}</div>'))

INPUT = Path("/kaggle/input")
WORK = Path("/kaggle/working")
SUB = Path("/tmp/submission")   # assembled here; only submission.zip and check.json go to Output
WHEEL_GLOB = "kickmix-*-manylinux*x86_64*.whl"
# Organisers' reference costs (the score is the geometric mean of reference / ours over the three tasks)
REFERENCE = {"invsqrt": 2076774.233715282, "dqi": 24439463583672.93, "chemistry": 3355599588.3155375}
VERIFIER_TASK = {"invsqrt": "invsqrt", "dqi": "dqi", "chemistry": "dfthcsos"}

def find(pattern, root=INPUT):
    hits = sorted(Path(p) for p in glob.glob(f"{root}/**/{pattern}", recursive=True))  # follows symlinked mounts
    if not hits:
        raise FileNotFoundError(f"{pattern} not found under {root}")
    return hits

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()

def family_sha256(directory):
    # The dataset's chemistry family hash: SHA-256 of sorted '<relative path>\t<file sha256>\n' lines
    files = sorted(p for p in Path(directory).rglob("*") if p.is_file())
    text = "".join(f"{p.relative_to(directory).as_posix()}\t{sha256(p)}\n" for p in files)
    return hashlib.sha256(text.encode()).hexdigest()

EXPECTED_MANIFEST = '0c6d961f57d0bb6544e3fe6f289833ec12d45524feafe68b0b8d8b7b84ffbee0'
_m = find("manifest.csv")[0]
assert sha256(_m) == EXPECTED_MANIFEST, (
    f"mounted manifest {sha256(_m)[:16]} is not the expected {EXPECTED_MANIFEST[:16]}: the circuits "
    f"dataset version is stale (still processing?); rerun after it is ready")
manifest = find("manifest.csv")[0]
DATA = manifest.parent
rows = list(csv.DictReader(open(manifest, newline="")))
print(f"manifest: {manifest} ({len(rows)} circuits, sha256 {sha256(manifest)[:16]}...)")

picks = {}
for task in REFERENCE:
    scored = rank_order([r for r in rows if r["task"] == task])  # tie group first, original first
    picks[task] = scored[0]
    best_cost = min(rank_cost(r) for r in scored)
    ties = [r["sha12"] for r in scored[1:] if rank_cost(r) <= best_cost * (1 + REL_TIE)]
    print(f"{task:9} {len(scored):2} candidates; picked {scored[0]['sha12']} "
          f"ranking cost {rank_cost(scored[0]):.6e} ({rank_basis(scored[0])}); "
          f"default-salt cost {float(scored[0]['local_task_score']):.6e}"
          + (f"; ties within noise, earlier public kept: {', '.join(ties)}" if ties else ""))

# Chemistry: a verified assembly of public parts replaces the best single family only if it is cheaper
ASSEMBLY = None
best_json = DATA / "parts" / "assembly_best.json"
if best_json.is_file():
    a = json.loads(best_json.read_text())
    single = float(picks["chemistry"]["local_task_score"])
    print(f"parts/assembly_best.json: verified {a['verified_cost']:.6e} ({a['verify_date']}) "
          f"vs best single family {picks['chemistry']['sha12']} {single:.6e}")
    if a.get("verified_status") == "PASS" and float(a["verified_cost"]) < single:
        ASSEMBLY = a

shutil.rmtree(SUB, ignore_errors=True)
SUB.mkdir(parents=True)
for task, r in picks.items():
    if task == "chemistry" and ASSEMBLY:
        fam = SUB / "dfthcsos"
        fam.mkdir()
        files = [(ASSEMBLY["npz"]["dataset_path"], ASSEMBLY["npz"]["sha256"], "submission.npz")]
        files += [(s["dataset_path"], s["file_sha256"], f"angle_k_{s['slot']}.kmb") for s in ASSEMBLY["slots"]]
        for path, digest, name in files:
            assert sha256(DATA / path) == digest, f"{path} does not match parts/assembly_best.json"
            shutil.copyfile(DATA / path, fam / name)
        authors = sorted({s["author"] for s in ASSEMBLY["slots"]})
        picks["chemistry"] = {
            "task": "chemistry", "sha12": "assembled", "sha256": family_sha256(fam),
            "qubits": ASSEMBLY["qubits"], "local_task_score": str(ASSEMBLY["verified_cost"]),
            "author": "assembled: loaders " + ", ".join(authors) + "; weights " + ASSEMBLY["npz"]["author"],
            "notebook_title": "assembly of public chemistry parts (parts/assembly_best.json)",
            # link the base notebook (the weights' source family), as the headline table does; the
            # parts and their authors are listed in the chemistry section
            "source_notebook_url": ASSEMBLY["npz"].get("notebook") or
                                   "https://www.kaggle.com/datasets/manwithacat/vqcc-apache-public-circuits",
            "source_notebook_version": ASSEMBLY["npz"].get("notebook_version", ""),
            "release_date": ASSEMBLY["verify_date"],
            "core_author": ASSEMBLY["word_table_origin"]["author"],
            "change_summary": f"cheapest public circuit per angle loader with the best weights on word table "
                              f"{ASSEMBLY['table_key']}", "also_found_in": ""}
        print(f"chemistry: assembled from {len(files)} dataset files, every SHA-256 checked")
    elif task == "chemistry":
        src = DATA / "circuits" / "chemistry" / r["sha12"]
        assert family_sha256(src) == r["sha256"], f"chemistry family {r['sha12']} does not match the manifest"
        shutil.copytree(src, SUB / "dfthcsos")
    else:
        src = DATA / "circuits" / task / f"{r['sha12']}.kmb"
        assert sha256(src) == r["sha256"], f"{task} {r['sha12']} does not match the manifest"
        shutil.copyfile(src, SUB / f"{task}.kmb")
print("assembled and hash-checked:", sorted(p.name for p in SUB.iterdir()))

pd.set_option("display.max_colwidth", 80)
display(pd.DataFrame([{"task": t, "candidates": sum(1 for x in rows if x["task"] == t and x["local_task_score"]),
                       "picked": r["sha12"], "qubits": r.get("qubits"),
                       "ranking cost": rank_cost(r), "ranked by": rank_basis(r),
                       "local cost (default salt)": float(r["local_task_score"]), "author": r["author"],
                       "sha256 checked": True} for t, r in picks.items()]).set_index("task"))

HW_VERSIONS = {'HW-01': 1, 'HW-03': 2, 'HW-04': 3, 'HW-05': 4, 'HW-06': 5, 'HW-07': 7, 'HW-08': 8, 'HW-09': 10, 'HW-10': 11, 'HW-11': 12, 'HW-12': 13, 'HW-13': 14, 'HW-14': 15, 'HW-15': 17, 'HW-16': 19, 'HW-17': 20, 'HW-18': 22, 'HW-19': 23, 'HW-21': 24, 'HW-22': 24, 'HW-23': 25}

hedge_csvs = find('entries.csv')
assert len(hedge_csvs) == 1, f'expected one Hedge Wizardry entries.csv, got {len(hedge_csvs)}'
HW_CSV = hedge_csvs[0]
HW_DIR = HW_CSV.parent
HW = list(csv.DictReader(open(HW_CSV, newline='')))
assert len({e['id'] for e in HW}) == len(HW), 'duplicate Hedge Wizardry IDs'
HEDGE_SELECTED, HEDGE_DECISIONS = {}, []
unknown = sorted(e['id'] for e in HW if e['id'] not in HW_VERSIONS)
if unknown:
    print('Hedge entries awaiting source-version review:', ', '.join(unknown))

for task in REFERENCE:
    candidates = []
    for entry in HW:
        if entry['task'] != task or entry['id'] not in HW_VERSIONS or entry.get('gates_missed', '').strip():
            continue
        price = (entry.get('draws_mean_cost_candidate') if task == 'dqi'
                 else entry.get('local_task_score'))
        if not price:  # never rank a sampled DQI circuit by one lucky default-salt draw
            continue
        price = float(price)
        if price > 0:
            candidates.append((price, entry))
    if not candidates:
        continue
    price, entry = min(candidates, key=lambda item: (item[0], item[1]['id']))
    base = picks[task]
    base_price = rank_cost(base)
    decision = {'task': task, 'entry': entry['id'], 'candidate_sha256': entry['candidate_sha256'],
                'base_sha256': base['sha256'], 'candidate_rank_cost': price,
                'base_rank_cost': base_price,
                'basis': '48-draw mean' if task == 'dqi' else 'default-salt verifier',
                'selected': price < base_price * (1 - REL_TIE)}
    HEDGE_DECISIONS.append(decision)
    if not decision['selected']:
        pass  # base stays ahead; recorded in hedge_blend_used.json (2026-11-01: no section in the page)
        continue
    src = HW_DIR / entry['dataset_path']
    digest = family_sha256(src) if task == 'chemistry' else sha256(src)
    assert digest == entry['candidate_sha256'], f"{entry['id']}: source bytes differ from entries.csv"
    if task == 'chemistry':
        shutil.rmtree(SUB / 'dfthcsos')
        shutil.copytree(src, SUB / 'dfthcsos')
        ASSEMBLY = None
    else:
        shutil.copyfile(src, SUB / f'{task}.kmb')
    version = HW_VERSIONS[entry['id']]
    url = f'https://www.kaggle.com/code/manwithacat/vqcc-hedge-wizardry-notebook/version/{version}'
    selected = {'task': task, 'entry': entry['id'], 'candidate_sha256': digest,
                'base_sha256': base['sha256'], 'candidate_rank_cost': price,
                'base_rank_cost': base_price, 'basis': decision['basis'], 'source_notebook_url': url}
    HEDGE_SELECTED[task] = selected
    picks[task] = {
        'task': task, 'sha12': entry['candidate_sha12'], 'sha256': digest,
        'qubits': entry['qubits'], 'local_task_score': entry['local_task_score'],
        'dqi_expected_cost': entry.get('draws_mean_cost_candidate', '') if task == 'dqi' else '',
        'dqi_zero_failure_cost': '',
        'author': f"James Barlow ({entry['id']}), adapting {entry['base_author']} {entry['base_sha256'][:12]}",
        'notebook_title': f"VQCC Hedge Wizardry {entry['id']}: {entry['title']}",
        'source_notebook_url': url, 'source_notebook_version': str(version),
        'release_date': entry['date'], 'core_author': entry['base_author'],
        'core_design': entry['base_lineage'], 'change_summary': entry['card_summary'],
        'also_found_in': ''}
    print(f"{task}: selected {entry['id']} {digest[:12]} at {price:.6e} vs base "
          f"{base['sha12']} at {base_price:.6e} ({decision['basis']}); source SHA checked")

hedge_receipt = {'schema': 'vqcc-friends-hedge-blend-v1',
                 'entries_sha256': sha256(HW_CSV), 'entries': len(HW),
                 'unknown_version_entries': unknown, 'decisions': HEDGE_DECISIONS,
                 'selected': HEDGE_SELECTED}
(WORK / 'hedge_blend_used.json').write_text(json.dumps(hedge_receipt, indent=2) + '\n')
# Decisions are saved in hedge_blend_used.json; the page only prints a line when an entry is selected.

tool_dirs = [Path(p).parent for root in ("/kaggle/usr/lib", "/kaggle/input")
             for p in sorted(glob.glob(f"{root}/**/kickmix_tools.py", recursive=True))]
assert tool_dirs, "attach the kickmix_tools utility script"
sys.path.insert(0, str(tool_dirs[0]))
import kickmix_tools as kt
print("kickmix_tools", kt.__version__, "from", tool_dirs[0])

wheel_dir = find(WHEEL_GLOB)[0].parent
FORK_DIR = kt.install(str(wheel_dir), target="/tmp/kickmix_fork")
FORK_PROVENANCE = kt.provenance()
print("fork provenance:", FORK_PROVENANCE)
ENGINE_DIR = kt.install_engine()  # wheel from kickmix-engine-wheels, checked against its SHA256SUMS
print("kickmix-engine:", ENGINE_DIR)
print("competition data:", kt.competition_dir())

# Verification reuse: a pick whose exact bytes already have an official-verifier PASS is not re-verified here; the
# receipt says which report each task relies on. Picks verified here run on kickmix-engine (all three tasks since
# kickmix-tools v5, 2026-11-03). FORCE_FULL_VERIFY = True (builder: --force-verify) skips reuse
# and runs the unchanged official verifier on the fork for all three.
FORCE_FULL_VERIFY = False
# REUSE_VERIFIED = False (builder: --no-reuse) verifies every pick this run on the default builds (engine timing runs)
REUSE_VERIFIED = True
ENGINE_TASKS = ("invsqrt", "dqi", "chemistry")

def verify_build(task):
    return "fork" if FORCE_FULL_VERIFY or task not in ENGINE_TASKS else "engine"

BUILD_LABEL = {"engine": "kickmix-engine (official verifier file, engine simulator)",
               "fork": "official verifier (kickmix fork)"}

def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None

def reuse_record(task, pick, hedge_selected=None, hedge_entries=(), assembly=None):
    # The earlier official-verifier PASS for these exact bytes, or None. Sources: the Hedge Wizardry entry that
    # replaced the pick (its recorded verifier gate, candidate SHA-256 matched); the chemistry parts assembly
    # (parts/assembly_best.json, verified locally; every file hash-checked when assembled); else the manifest row's
    # local official-verifier report (local_report_sha256; the file's SHA-256 was checked when it was copied).
    hw = (hedge_selected or {}).get(task)
    if hw:
        e = next((x for x in hedge_entries if x.get("id") == hw["entry"]), None)
        text = " ".join(((e or {}).get("verifier_result") or "").split())
        if e and e.get("candidate_sha256") == pick["sha256"] and "verifier PASS" in text \
                and _num(e.get("local_task_score")):
            return {"source": f"Hedge Wizardry {e['id']} entry gate", "date": e.get("date", ""),
                    "report": text[:140], "score": _num(e["local_task_score"]), "qubits": _num(e.get("qubits")),
                    "toffoli_count": _num(e.get("toffoli_mean")), "metrics": None}
        return None
    if task == "chemistry" and assembly:
        if assembly.get("verified_status") == "PASS" and assembly.get("verifier_report_sha256") \
                and _num(assembly.get("verified_cost")):
            return {"source": "parts/assembly_best.json (local official verifier, default salt)",
                    "date": assembly.get("verify_date", ""),
                    "report": "report sha256 " + assembly["verifier_report_sha256"][:16] + "…",
                    "score": _num(assembly["verified_cost"]), "qubits": _num(assembly.get("qubits")),
                    "toffoli_count": _num(assembly.get("toffoli")), "metrics": None}
        return None
    tier = pick.get("local_score_tier") or ""
    if pick.get("local_report_sha256") and tier.startswith("local official verifier") \
            and _num(pick.get("local_task_score")):
        metrics = None
        if task == "chemistry" and all(_num(pick.get(k)) is not None for k in ("error_1_mHa", "error_2_mHa", "E_gap")):
            metrics = {"error_1": _num(pick["error_1_mHa"]) / 1000, "error_2": _num(pick["error_2_mHa"]) / 1000,
                       "E_gap": _num(pick["E_gap"])}
        return {"source": f"manifest.csv local report ({tier})", "date": pick.get("first_seen", ""),
                "report": "report sha256 " + pick["local_report_sha256"][:16] + "…",
                "score": _num(pick["local_task_score"]), "qubits": _num(pick.get("qubits")),
                "toffoli_count": _num(pick.get("T_total")) if task == "chemistry" else None, "metrics": metrics}
    return None

def reused_result(rec):
    # A result shaped like kt.verify()'s, from a reuse record (fields the record lacks stay None: shown as —)
    res = {"status": "PASS", "score": rec["score"], "num_qubits": int(rec["qubits"]) if rec.get("qubits") else None,
           "toffoli_count": rec.get("toffoli_count")}
    if rec.get("metrics"):
        res["metrics"] = rec["metrics"]
    return {"result": res, "wall_seconds": None, "peak_pss_sum_mb": None, "peak_rss_sum_mb": None, "reused": rec}


PATHS = {"invsqrt": SUB / "invsqrt.kmb", "dqi": SUB / "dqi.kmb", "chemistry": SUB / "dfthcsos"}
results, check_rows, VERIFICATION = {}, [], {}
for task, circuit in PATHS.items():
    rec = None if FORCE_FULL_VERIFY or not REUSE_VERIFIED else reuse_record(
        task, picks[task], globals().get("HEDGE_SELECTED"), globals().get("HW", ()), ASSEMBLY)
    build = verify_build(task)
    r = reused_result(rec) if rec else dict(kt.verify(VERIFIER_TASK[task], circuit, build=build), reused=None)
    VERIFICATION[task] = (f"reused: {rec['source']}, {rec['date']}; {rec['report']}" if rec
                          else f"verified this run: {BUILD_LABEL[build]}")
    results[task] = r
    res = r["result"]
    listed = float(picks[task]["local_task_score"])
    check_rows.append({
        "task": task, "circuit": picks[task]["sha12"], "status": res["status"],
        "verification": "reused" if rec else f"this run: {BUILD_LABEL[build]}",
        "cost": res["score"], "source cost": listed, "cost / source": res["score"] / listed,
        "times cheaper than reference": REFERENCE[task] / res["score"],
        "qubits": res.get("num_qubits"), "Toffolis (mean)": res.get("toffoli_count"),
        "value errors": res.get("value_errors") if task == "dqi" else None,
        "phase errors": res.get("phase_errors") if task == "dqi" else None,
        "wall s": None if rec else round(r["wall_seconds"], 1),
        "peak MB": None if rec else round(r["peak_pss_sum_mb"] or r["peak_rss_sum_mb"] or 0)})
checks = pd.DataFrame(check_rows).set_index("task")
FORMATS = {"cost": "{:.6e}", "source cost": "{:.6e}", "cost / source": "{:.9f}",
           "times cheaper than reference": "{:.4f}", "qubits": "{:.0f}", "Toffolis (mean)": "{:,.1f}",
           "value errors": "{:.0f}", "phase errors": "{:.0f}", "wall s": "{:.1f}", "peak MB": "{:,.0f}"}
shown = checks.copy()
for col, fmt in FORMATS.items():  # value/phase errors exist for DQI only; blank cells show as —
    shown[col] = [fmt.format(v) if pd.notna(v) else "—" for v in checks[col]]
display(shown)
print("— = not applicable (only the DQI verifier counts value and phase errors) or not recorded in a reused report.")
n_reused = sum(1 for r in results.values() if r.get("reused"))
print(f"verification: {len(results) - n_reused} pick(s) verified this run, {n_reused} reused an earlier "
      f"official-verifier PASS for the exact bytes (FORCE_FULL_VERIFY = {FORCE_FULL_VERIFY})")
for t, v in VERIFICATION.items():
    print(f"  {t}: {v}")
failed = [t for t, r in results.items() if r["result"]["status"] != "PASS"]
assert not failed, f"not writing a submission: {failed} did not pass"

ratios = {t: REFERENCE[t] / results[t]["result"]["score"] for t in REFERENCE}
gm = prod(ratios.values()) ** (1 / 3)
# DQI cost depends on the draw. The headline uses the DQI ranking cost (mean over a 48-draw sweep, or the
# zero-failure cost); gm is this run's single default-salt draw (2026-10-31: the grader uses its own hidden draw).
ratios_exp = dict(ratios, dqi=REFERENCE["dqi"] / rank_cost(picks["dqi"]))
gm_exp = prod(ratios_exp.values()) ** (1 / 3)
zf = picks['dqi'].get('dqi_zero_failure_cost')  # best case: no failed DQI shots in a draw
gm_zero = prod(dict(ratios, dqi=REFERENCE['dqi'] / float(zf)).values()) ** (1 / 3) if zf else None
# Error bounds (2026-11-01). Only DQI's cost depends on the draw: cost = R(k) x Q^2 x Toffolis, k the failed
# shots of the draw. The pick's 48-draw expected cost over its zero-failure cost is E[R(k)] / R(0); with k
# Poisson(lam) that fixes lam, so every file with a 48-draw sweep gets a measured final-draw risk.
R_K = [1.19126, 1.19673, 1.20139, 1.20558, 1.20946, 1.21311, 1.21657]  # R(k), 102,400 shots (verifier)
def _pois(lam):
    from math import exp, factorial
    p = [exp(-lam) * lam ** k / factorial(k) for k in range(len(R_K))]
    p[-1] += 1 - sum(p)
    return p
DQI_RISK = None
ec = picks['dqi'].get('dqi_expected_cost')
if zf and ec and float(ec) >= float(zf):
    target = float(ec) / float(zf)
    lo, hi = 0.0, 10.0
    for _ in range(80):  # E[R(k)]/R(0) rises with lam: bisect
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if sum(q * r / R_K[0] for q, r in zip(_pois(mid), R_K)) < target else (lo, mid)
    lam = (lo + hi) / 2
    pk = _pois(lam)
    gm_k = lambda k: prod(dict(ratios, dqi=REFERENCE['dqi'] / (float(zf) * R_K[k] / R_K[0])).values()) ** (1 / 3)
    DQI_RISK = {"failures_per_draw": lam, "p_none": pk[0], "p_one_or_more": 1 - pk[0],
                "p_two_or_more": 1 - pk[0] - pk[1], "gm_lucky": gm_k(0), "gm_one": gm_k(1), "gm_two": gm_k(2),
                "basis": "Poisson fit to the 48-draw expected cost / zero-failure cost (manifest)"}
print(f"expected geometric mean: {gm_exp:.5f} (DQI {rank_basis(picks['dqi'])}); "
      f"this run's single default-salt draw: {gm:.5f}")
checks.to_csv(WORK / "checks.csv")

members = [("invsqrt.kmb", SUB / "invsqrt.kmb"), ("dqi.kmb", SUB / "dqi.kmb")]
members += [(f"dfthcsos/{p.relative_to(SUB / 'dfthcsos').as_posix()}", p)
            for p in sorted((SUB / "dfthcsos").rglob("*")) if p.is_file()]
with zipfile.ZipFile(WORK / "submission.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for name, path in members:
        z.write(path, name)
(WORK / "check.json").write_text(json.dumps({
    "manifest_sha256": sha256(manifest), "submission_sha256": sha256(WORK / "submission.zip"),
    "geometric_mean_local": gm, "fork": FORK_PROVENANCE,
    "picks": {t: {k: picks[t][k] for k in ("sha256", "author", "notebook_title", "source_notebook_url",
                                            "release_date", "local_task_score")} for t in picks},
    "hedge_blend": globals().get("HEDGE_SELECTED", {}),
    "chemistry_assembly": ASSEMBLY, "verification": VERIFICATION,
    "results": {t: r["result"] for t, r in results.items()}}, indent=1, default=str))
shutil.rmtree(SUB)
print("wrote", WORK / "submission.zip", "and", WORK / "check.json")

LINEAGE_NOTES = {'bce2948ff24e': ("yoyobar's 62-qubit circuit (mrmeows / busyaprime lineage, Ahmed Berat Özer's phase patch)", 'Ehan Mallick: two 3-to-2 AND merges, −2 Toffolis'), 'e3928244a1c1': ("SomeJoint's 892-qubit DQI decoder with flag carry", 'busyaprime: exact product caching and flag rewrites (using our HW-07 ladder); yoyobar: lead-product reuse, −0.46%'), '3aac21436c28': ("Lera's (9, 143, 40) chemistry table and 128-qubit loader emitter; SomeJoint's weights", 'ours (HW-12, HW-13): all 15 angle loaders rebuilt from a systematic GF(2) kernel-relation search with block designs and a layout search, 16,071 → 15,787 Toffolis, −1.8% chemistry'), 'b94b469b148a': ("Lera's (9, 143, 40) chemistry table and 128-qubit loader emitter; sigmaborov's weights", 'ours (HW-12): all 15 angle loaders rebuilt from a systematic GF(2) kernel-relation search, −1.3%')}

# The page's headline: this run's portfolio and the top public circuits per task, from manifest.csv.
from datetime import datetime, timedelta, timezone
import html as _html

NOW = datetime.now(timezone.utc)
def _when(r):
    s = r.get("public_since") or r.get("release_date") or r.get("first_seen") or ""
    try:
        return datetime.fromisoformat(s if len(s) > 10 else s + " 12:00").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
def _a(text, url):
    t = _html.escape(str(text))
    return f'<a href="{_html.escape(url)}" target="_blank">{t}</a>' if url else t

all_pass = all(r["result"]["status"] == "PASS" for r in results.values())
n_reused = sum(1 for r in results.values() if r.get("reused"))
n_engine = sum(1 for r in results.values() if not r.get("reused") and r.get("build") == "engine")
n_official = len(results) - n_reused - n_engine
_how = ([f"{n_official} official verifier run here"] if n_official else []) + (
    [f"{n_engine} kickmix-engine run here"] if n_engine else []) + (
    [f"{n_reused} earlier official-verifier report(s) for the exact bytes, named in the receipt"]
    if n_reused else [])
badge = ('<span style="color:#cf222e;font-weight:600">VERIFICATION FAILED</span>' if not all_pass else
         f'<span style="color:#1a7f37;font-weight:600">all three PASS ({"; ".join(_how)})</span>')
def _b(v, label, p=None, strong=False):
    w = "700" if strong else "400"
    return (f'<span style="font-weight:{w}">{v:.5f}</span> <span style="color:#52514e">{label}'
            + (f', {p:.0%}' if p is not None else '') + '</span>')
if DQI_RISK:  # error bounds: unlucky / expected / lucky (only DQI depends on the draw)
    parts = []
    if DQI_RISK["p_two_or_more"] >= 0.05:
        parts.append(_b(DQI_RISK["gm_two"], "2 failed DQI shots", DQI_RISK["p_two_or_more"]))
    parts.append(_b(DQI_RISK["gm_one"], "≥1 failed DQI shot" if DQI_RISK["p_two_or_more"] < 0.05 else "1 failed DQI shot", DQI_RISK["p_one_or_more"] if DQI_RISK["p_two_or_more"] < 0.05 else DQI_RISK["p_one_or_more"] - DQI_RISK["p_two_or_more"]))
    parts.append(_b(gm_exp, "expected", strong=True))
    parts.append(_b(DQI_RISK["gm_lucky"], "no failed shots", DQI_RISK["p_none"]))
    range_line = ('<div style="font-size:14px;margin:2px 0 6px">' + ' &nbsp;·&nbsp; '.join(parts) + '</div>')
else:
    range_line = ''
out = [f'<div style="font-size:15px;line-height:1.5">'
       f'<div style="font-size:30px;font-weight:700">Best public portfolio: {gm_exp:.5f}</div>'
       + range_line +
       f'<div style="color:#52514e">The expected score of the best public circuit for each task, with DQI at its '
       f'{rank_basis(picks["dqi"])} cost. Only DQI depends on the grader’s hidden draw: the bounds above are the '
       f'score with one more or no failed DQI shots, and how often each happens. {badge}; run '
       f'{NOW:%d %b %Y %H:%M} UTC; {len(rows)} public circuits considered; this run’s own draw gives {gm:.5f}.</div>'
       '<div style="font-size:11px;color:#8c8a85;margin-top:2px">The "Score … · Vn" label Kaggle shows '
       'on this page is the leaderboard result of the last submitted version (Vn), which can be older than '
       'this one.</div>']
# At a glance: one row per task, the best public circuit now and how far it moved in 24 hours and 7 days
def _best_before(task, t):
    # Best ranking cost public by time t; before the dataset's first circuits, the best of the first day
    dated = [r for r in rows if r["task"] == task and rank_cost(r) and _when(r)]
    cs = [rank_cost(r) for r in dated if _when(r) <= t]
    if cs or not dated:
        return min(cs) if cs else None
    t0 = min(_when(r) for r in dated)
    SHORT.add(task)
    return min(rank_cost(r) for r in dated if _when(r) <= t0 + timedelta(days=1))
SHORT = set()
def _chg(now_c, then_c):
    if not then_c or now_c >= then_c * (1 - REL_TIE):
        return '<span style="color:#8c8a85">—</span>'
    return f'<span style="color:#1a7f37">+{then_c / now_c - 1:.2%}</span>'
out.append('<table style="border-collapse:collapse;margin-top:8px"><tr style="color:#52514e;text-align:left">'
           '<th>task</th><th>× cheaper</th><th>24 h</th><th>7 days</th><th>qubits</th><th>record held since</th>'
           '<th>by</th></tr>')
prods = {"now": 1.0, "d1": 1.0, "d7": 1.0}
for task in REFERENCE:
    cands = rank_order([r for r in rows if r["task"] == task])
    b = cands[0]
    bc = float(ASSEMBLY["verified_cost"]) if task == "chemistry" and ASSEMBLY else rank_cost(b)
    d1, d7 = _best_before(task, NOW - timedelta(days=1)), _best_before(task, NOW - timedelta(days=7))
    for k, c in (("now", bc), ("d1", d1 or bc), ("d7", d7 or bc)):
        prods[k] *= REFERENCE[task] / c
    since = _when(b)
    out.append(f'<tr style="border-top:1px solid #e8e7e2"><td>{TASK_NAMES[task]}</td>'
               f'<td style="font-weight:600">{REFERENCE[task] / bc:.4f}</td><td>{_chg(bc, d1)}</td>'
               f'<td>{_chg(bc, d7)}{"*" if task in SHORT else ""}</td><td>{b["qubits"]}</td>'
               f'<td>{"—" if since is None else f"{since:%d %b %H:%M} UTC"}</td>'
               f'<td>{_a(b["author"], b["source_notebook_url"])}</td></tr>')
g = {k: v ** (1 / len(REFERENCE)) for k, v in prods.items()}
out.append(f'<tr style="border-top:2px solid #0b0b0b;font-weight:600"><td>Geometric mean</td>'
           f'<td>{g["now"]:.5f}</td><td>{_chg(1 / g["now"], 1 / g["d1"])}</td>'
           f'<td>{_chg(1 / g["now"], 1 / g["d7"])}{"*" if SHORT else ""}</td><td colspan="3"></td></tr></table>')
# What each pick is built from (2026-11-02, user): the top scores are structural amendments (loader rebuilds,
# weight refits, rewrites) layered on base designs, by us (Hedge Wizardry) and by others; say which, and whose.
def _clip(t, n=230):
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"
out.append('<div style="margin-top:12px;font-weight:600">What each pick is built from</div>'
           '<div style="color:#52514e;font-size:13px">Every top public score is a base design plus amendments '
           'layered on it, often by several authors: new loaders or weights on a published table, rewrites of a '
           'published decoder. The rows below say which, and whose. Picks marked HW are our Hedge Wizardry '
           'entries; the rest are other authors’ public circuits.</div>')
out.append('<table style="border-collapse:collapse;margin-top:4px;font-size:13px"><tr style="color:#52514e;'
           'text-align:left"><th>task</th><th>pick</th><th>base design</th><th>amendments on top</th></tr>')
for task in REFERENCE:
    pk = picks[task]
    hw = HEDGE_SELECTED.get(task) if "HEDGE_SELECTED" in globals() else None
    label = f'{hw["entry"]} {pk["sha12"]}' if hw else str(pk.get("sha12", ""))
    note = LINEAGE_NOTES.get(str(pk.get("sha12", ""))[:12])
    base = note[0] if note else _clip(f'{pk.get("core_design") or ""} ({pk.get("core_author") or "?"})', 160)
    amend = note[1] if note else _clip(pk.get("change_summary"))
    out.append(f'<tr style="border-top:1px solid #e8e7e2;vertical-align:top"><td>{TASK_NAMES[task]}</td>'
               f'<td>{_a(label, pk.get("source_notebook_url", ""))}</td><td>{_html.escape(base)}</td>'
               f'<td>{_html.escape(amend)}</td></tr>')
out.append('</table>')
# Top five per task, shown open beneath the headline (as in v63; restored 2026-11-03, user).
out.append('<div style="margin-top:10px">')
for task in REFERENCE:
    cands = rank_order([r for r in rows if r["task"] == task])  # tie group first, original first
    tie_lim = min(rank_cost(r) for r in cands) * (1 + REL_TIE) if cands else 0
    top = []
    if task == "chemistry" and ASSEMBLY:
        top.append({"label": "assembly of public parts", "author": picks["chemistry"]["author"],
                    "url": picks["chemistry"]["source_notebook_url"], "cost": float(ASSEMBLY["verified_cost"]), "qubits": ASSEMBLY["qubits"],
                    "when": None, "picked": True})
    for r in cands:
        if len(top) >= 5:
            break
        top.append({"label": r["sha12"], "author": r["author"], "url": r["source_notebook_url"],
                    "cost": rank_cost(r), "qubits": r["qubits"], "when": _when(r),
                    "date_only": len(r.get("public_since") or r.get("release_date") or r.get("first_seen") or "") <= 10,
                    "picked": picks[task]["sha12"] == r["sha12"], "tie": rank_cost(r) <= tie_lim})
    lead = ("; the next ties it within noise (the tie goes to the earliest public circuit)"
            if len(top) > 1 and top[1].get("tie") and top[0].get("tie") else
            f"; lead over the next: {top[1]['cost'] / top[0]['cost'] - 1:.3%}" if len(top) > 1 else "")
    out.append(f'<h4 style="margin:14px 0 4px">{TASK_NAMES[task]}: '
               f'{REFERENCE[task] / top[0]["cost"]:.4f}× cheaper than the reference{lead}</h4>')
    out.append('<table style="border-collapse:collapse"><tr style="color:#52514e;text-align:left">'
               '<th>#</th><th>circuit</th><th>published by</th><th>× cheaper</th><th>cost</th>'
               '<th>qubits</th><th>released</th><th></th></tr>')
    for i, t in enumerate(top, 1):
        new = (t["when"] is not None and NOW - t["when"] < timedelta(hours=24))
        tags = ("<b>in this submission</b> " if t["picked"] else
                "tie (within noise; later copy) " if t.get("tie") else "") + (
            '<span style="background:#fff3b0;padding:0 4px">NEW</span>' if new else "")
        weight = "font-weight:600;" if t["picked"] else ""
        when = ("—" if t["when"] is None else
                f"{t['when']:%d %b}" if t.get("date_only") else f"{t['when']:%d %b %H:%M} UTC")
        out.append(f'<tr style="{weight}border-top:1px solid #e8e7e2"><td>{i}</td>'
                   f'<td><code>{_html.escape(t["label"])}</code></td><td>{_a(t["author"], t["url"])}</td>'
                   f'<td>{REFERENCE[task] / t["cost"]:.4f}</td><td>{t["cost"]:.6g}</td><td>{t["qubits"]}</td>'
                   f'<td>{when}</td><td>{tags}</td></tr>')
    out.append("</table>")
out.append('</div><div style="color:#52514e;margin-top:8px;font-size:13px">24 h / 7 days: how much '
           'cheaper the best public circuit is now than it was then (— = no change)'
           + ('; * the dataset is younger than 7 days, so this is since its first day of circuits'
              if SHORT else '') + '. DQI is ranked by its '
           '48-draw expected cost, so a lucky single draw cannot win. NEW = released in the last 24 hours.'
           '</div></div>')
display(HTML("".join(out)))

WALL = time.monotonic() - T0
receipt = pd.DataFrame([{
    "task": TASK_NAMES[t], "circuit": picks[t]["sha12"], "sha256": picks[t]["sha256"][:16] + "…",
    "published by": picks[t]["author"], "version": picks[t]["source_notebook_version"],
    "verdict": results[t]["result"]["status"], "verification": VERIFICATION[t],
    "times cheaper than reference": REFERENCE[t] / results[t]["result"]["score"],
    "link": picks[t]["source_notebook_url"]} for t in REFERENCE])
show(receipt)
CHEM_PARTS = None
if ASSEMBLY:
    print("Chemistry was assembled from public parts: each angle loader, the weights and the word table's origin")
    CHEM_PARTS = pd.DataFrame(
        [{"part": f"angle loader {s['slot']}", "file": s["dataset_path"], "sha256": s["file_sha256"][:16] + "…",
          "published by": s["author"], "version": s.get("notebook_version", ""),
          "Toffolis per shot": s.get("toffoli_per_shot_source"), "link": s["notebook"]} for s in ASSEMBLY["slots"]]
        + [{"part": "weights (submission.npz)", "file": ASSEMBLY["npz"]["dataset_path"],
            "sha256": ASSEMBLY["npz"]["sha256"][:16] + "…", "published by": ASSEMBLY["npz"]["author"],
            "version": ASSEMBLY["npz"].get("notebook_version", ""), "Toffolis per shot": None,
            "link": ASSEMBLY["npz"]["notebook"]},
           {"part": f"word table {ASSEMBLY['table_key']} (origin)", "file": "parts/word_tables.csv",
            "sha256": "", "published by": ASSEMBLY["word_table_origin"]["author"], "version": "",
            "Toffolis per shot": None, "link": ASSEMBLY["word_table_origin"]["notebook"]}])
    show(CHEM_PARTS)
# G3 compares this run's verifier with the source record, so it covers only the picks verified this run; a
# reused pick's cost IS its source record (named in the 'verification' column above).
ran = [t for t in REFERENCE if not results[t].get("reused")]
rel = {t: abs(results[t]["result"]["score"] / float(picks[t]["local_task_score"]) - 1) for t in ran}
G3_NAME = f"G3 every cost verified this run within 1e-9 of its source record ({len(ran)}/3 verified this run)"
GATES = {
    "G1 every file matches its listed SHA-256": True,  # asserted when the files were assembled
    "G2 all three passed (this run: kickmix-engine or the official verifier; or an earlier official-verifier report for the exact bytes)":
        all(r["result"]["status"] == "PASS" for r in results.values()),
    G3_NAME: max(rel.values(), default=0.0) <= 1e-9,
    "G4 notebook wall time under 2 hours": WALL <= 7200,
}
show(pd.DataFrame([{"gate": g, "result": "pass" if ok else "FAIL"} for g, ok in GATES.items()]))
# G3: each cost reproduced exactly at the default salt (the local verifier seeds every shot from the file's
# SHA-256). This run's default-salt GM is one DQI draw; the grader draws with its own salt, so the headline's
# error bounds (expected, lucky, unlucky) are the fair summary, not this single draw.

# Headroom: how close each pick is to the verifier's pass/fail limits (from this run's verifier output)
E1_MAX = E2_MAX = 0.020          # Ha, both chemistry accuracy gates
GAP_FLOOR = 3.914898             # strict E_gap floor in the dfthcsos verifier
head = []
inv = results["invsqrt"]["result"]
w = inv.get("worst_error_over_tolerance")
if w is not None:
    head.append({"task": TASK_NAMES["invsqrt"], "check": "worst error / tolerance (must stay below 1)",
                 "value": f"{w:.5f}", "margin": f"{(1 - w) * 100:.3f}% of tolerance",
                 "tight": (1 - w) < 0.005})
dq = results["dqi"]["result"]
if results["dqi"].get("reused"):  # a reused report carries the cost only; the 48-draw risk is in the headline
    head.append({"task": TASK_NAMES["dqi"], "check": "failed shots on this file's draw (value / phase)",
                 "value": "unavailable (reused report)", "margin": "see the headline's error bounds",
                 "tight": False})
else:
    head.append({"task": TASK_NAMES["dqi"], "check": "failed shots on this file's draw (value / phase)",
                 "value": f"{dq.get('value_errors')} / {dq.get('phase_errors')}",
                 "margin": f"repetition factor R = {dq.get('repetition_factor', float('nan')):.5f}; "
                           "each extra failure costs about 0.46% of DQI cost",
                 "tight": (dq.get("value_errors") or 0) + (dq.get("phase_errors") or 0) > 0})
ch = results["chemistry"]["result"].get("metrics", {})
for key, lim in (("error_1", E1_MAX), ("error_2", E2_MAX)):
    if key in ch:
        m = (lim - ch[key]) * 1000
        head.append({"task": TASK_NAMES["chemistry"], "check": f"{key} (must stay ≤ 20 mHa)",
                     "value": f"{ch[key] * 1000:.5f} mHa", "margin": f"{m:.5f} mHa", "tight": m < 0.01})
if "E_gap" in ch:
    m = ch["E_gap"] - GAP_FLOOR
    head.append({"task": TASK_NAMES["chemistry"], "check": "E_gap (must stay above 3.914898)",
                 "value": f"{ch['E_gap']:.7f}", "margin": f"{m:.2e}", "tight": m < 1e-4})
hd = pd.DataFrame(head)
hd["status"] = ["tight" if t else "comfortable" for t in hd.pop("tight")]
# Headroom: how close each pick is to failing the verifier; 'tight' means a tiny change in the file, the
# verifier or floating point could flip it.
show(hd)

# DQI final-salt risk: measured from the pick's 48-draw sweep (DQI_RISK, verify cell) and shown in the
# headline's error bounds; a DQI Salt Check output covering this file is kept in receipt.json when attached.
SALT = None
for f in sorted(glob.glob(f"{INPUT}/**/dqi_salt_check_summary_*.csv", recursive=True)):
    for r in csv.DictReader(open(f, newline="")):
        if r.get("circuit") == picks["dqi"]["sha12"]:
            SALT = dict(r, file=Path(f).name)
if not GATES[G3_NAME]:
    print("G3 failed: this run's verifier disagrees with a source record, so treat the local score with care.")
(WORK / "receipt.json").write_text(json.dumps({
    "gates": GATES, "verification": VERIFICATION, "geometric_mean_local": gm, "headroom": head, "dqi_salt_check": SALT, "dqi_risk": DQI_RISK, "wall_seconds": WALL, "relative_cost_difference": rel,
    "submission_sha256": sha256(WORK / "submission.zip"), "manifest_sha256": sha256(manifest),
    "rows": receipt.to_dict(orient="records"),
    "chemistry_parts": CHEM_PARTS.to_dict(orient="records") if CHEM_PARTS is not None else None},
    indent=1, default=str))

from datetime import datetime, timezone
import warnings
# Safety net for chart text: names can carry emoji the chart font lacks (place_label also strips them)
warnings.filterwarnings("ignore", message=r"Glyph \d+ .* missing from font")

def parse_when(s):
    # 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM[:SS]' or ISO with offset -> naive UTC datetime; None if blank or unreadable
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s if len(s) > 10 else s + " 12:00")
    except ValueError:
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d

def released(r):
    # When the circuit became public: public_since, falling back to release_date then first_seen (legacy rows)
    return (parse_when(r.get("public_since")) or parse_when(r.get("release_date"))
            or parse_when(r.get("first_seen")))

def chart_cost(r):
    # The cost the charts rank by: as the portfolio ranking (DQI: expected over 48 draws, else zero-failure, else the
    # default-salt score, because one DQI draw is luck; other tasks: the default-salt score)
    keys = ("dqi_expected_cost", "dqi_zero_failure_cost", "local_task_score") if r.get("task") == "dqi"             else ("local_task_score",)
    for k in keys:
        try:
            return float(r[k])
        except (KeyError, TypeError, ValueError):
            continue
    return None

def frontier_of(rows, task):
    # (every scored row of the task in public order, the rows that set a public record)
    rs = sorted((r for r in rows if r["task"] == task and chart_cost(r) and released(r)),
                key=lambda r: (released(r), chart_cost(r)))
    # A record must beat the previous one by more than the tie threshold (1e-5 relative, as REL_TIE in the ranking
    # rule): a republished copy that differs only by draw noise (e.g. 2366b67cf072, yoyobar's 67482db6c538 with
    # cancelling Clifford gates removed) is not a new record, and the original keeps the credit.
    steps, best = [], None
    for r in rs:
        c = chart_cost(r)
        if best is None or c < best * (1 - 1e-5):
            steps.append(r)
            best = c
    return rs, steps

def late_marks(rs):
    # Late disclosures: [(row, first_known, public)] for rows marked disclosure=late with a dated first_known
    # earlier than their public time. Drawn as a hollow marker at first_known joined by a dotted line to the public
    # point, so a hidden lead shows without rewriting the public history.
    out = []
    for r in rs:
        fk, pub = parse_when(r.get("first_known")), released(r)
        if (r.get("disclosure") or "") == "late" and fk and pub and fk < pub:
            out.append((r, fk, pub))
    return out

def date_locator(lo, hi, mdates):
    # About five ticks from the plotted span; fixed Day/Hour locators (AutoDateLocator warns on some spans)
    span_h = max((hi - lo).total_seconds() / 3600, 1)
    return (mdates.DayLocator(interval=max(1, round(span_h / 24 / 5))) if span_h >= 48
            else mdates.HourLocator(interval=max(1, round(span_h / 5))))

# Labelling and direction (user feedback 2026-10-30): the end point matters, the starting point does not. Records
# are numbered 1..n in public order (the same numbers as the 'step' column of the table); only the current best is
# named; a few of the largest jumps carry their gain; the first record is never named.
def step_gains(steps, value):
    # [(step number, value, gain over the previous record or None)] in public order
    out, prev = [], None
    for i, r in enumerate(steps, 1):
        v = value(r)
        out.append((i, v, v / prev - 1 if prev else None))
        prev = v
    return out

def label_plan(steps, value, max_gain_labels=3, min_gain=0.02):
    # {'end': index named with author and value, 'gains': indices whose jump is labelled '+x%'}; indices are 0-based
    # into steps. Index 0 (the starting point) is never named and has no gain; the end carries its own label.
    if not steps:
        return {"end": None, "gains": []}
    end = len(steps) - 1
    jumps = sorted(((g, i - 1) for i, _, g in step_gains(steps, value)[1:end] if g is not None and g >= min_gain),
                   reverse=True)[:max_gain_labels]
    return {"end": end, "gains": sorted(i for _, i in jumps)}

def number_priority(steps, value):
    # Order in which record numbers claim space when they would collide: the end, the labelled jumps, then the
    # rest from latest to earliest, so a crowded early cluster gives way first.
    plan = label_plan(steps, value)
    if plan["end"] is None:
        return []
    first = [plan["end"]] + plan["gains"][::-1]
    return first + [i for i in range(len(steps) - 1, -1, -1) if i not in first]

def fmt_cost(c):
    # Costs span 1e5 (invsqrt) to 1e11 (DQI): plain digits below ten million, three significant figures above
    return f"{c:,.0f}" if c < 1e7 else f"{c:.3g}".replace("e+", "e")

def end_label(r, value):
    # The one named label per panel: who holds the record now, how much cheaper than reference, and its cost
    return f"now: {r['author']}\n×{value(r):.3f}, cost {fmt_cost(chart_cost(r))}"

def ramp_colors(n, ramp):
    # n colours stepped evenly along a light->dark ramp: early records light, the latest darkest
    if n <= 1:
        return [ramp[-1]] * n
    return [ramp[round(k * (len(ramp) - 1) / (n - 1))] for k in range(n)]

def zoom_from(points, span_x, span_y, frac=0.3, min_n=4):
    # Index of the first record from which the rest of the path (at least min_n drawn points) fits within frac of
    # the chart's span on both axes: those records crowd a corner and get a magnified inset. None if none do.
    idx = [i for i, p in enumerate(points) if p is not None]
    for n, k in enumerate(idx[:max(0, len(idx) - min_n + 1)]):
        tx, ty = zip(*[points[i] for i in idx[n:]])
        if max(tx) - min(tx) <= frac * (span_x or 1) and max(ty) - min(ty) <= frac * (span_y or 1):
            return k
    return None

def path_segments(path):
    # path: [(step index, (x, y))] in record order. Consecutive (from, to) pairs, dropping zero-length moves (two
    # records at the same point: no arrow can point anywhere)
    return [(a, b) for a, b in zip(path, path[1:]) if a[1] != b[1]]


# Built only from manifest.csv: a new dataset version redraws the charts with no notebook change.
import base64
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
BLUE, GREY, INK, MUTED = "#2a78d6", "#b8b7b0", "#0b0b0b", "#52514e"
RAMP = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95"]  # blue 250..600
plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": GREY,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlecolor": INK,
                     "font.size": 10, "figure.dpi": 110})

def frontier(task):
    return frontier_of(rows, task)

def ratio(task):
    return lambda r: REFERENCE[task] / chart_cost(r)

def markers_px(ax):
    # Every plotted point on the axes (grey circuits and records), in display pixels: things labels avoid
    offs = [ax.transData.transform(c.get_offsets()) for c in ax.collections if len(c.get_offsets())]
    return np.vstack(offs) if offs else np.empty((0, 2))

def place_label(ax, xy, text, cands, avoid=(), fontsize=8.5, color=INK, **kw):
    # Put text beside xy at the first candidate offset (points) whose box covers no marker and no placed
    # box; if every candidate is blocked, the least crowded one. Returns the box in pixels.
    # Chart fonts lack emoji and other astral-plane glyphs (an author name once carried one): drop them here.
    text = "".join(ch for ch in text if ord(ch) <= 0xFFFF and ch != "️").rstrip()
    s = ax.figure.dpi / 72
    p = ax.transData.transform(xy)
    w, h = 0.6 * fontsize * max(len(t) for t in text.split("\n")) * s, 1.3 * fontsize * (text.count("\n") + 1) * s
    obst = [q for q in markers_px(ax) if np.hypot(*(q - p)) > 1]
    rend = ax.figure.canvas.get_renderer()
    frame = tuple(ax.get_window_extent(rend).extents)  # labels stay inside the plot area
    avoid = list(avoid) + [tuple(t.get_window_extent(rend).extents) for t in ax.texts if t.get_text()]
    best = None
    for dx, dy, ha in cands:
        x0 = p[0] + dx * s - (w if ha == "right" else w / 2 if ha == "center" else 0)
        y0 = p[1] + dy * s - h / 2
        box = (x0 - 2, y0 - 2, x0 + w + 2, y0 + h + 2)
        hits = sum(box[0] <= q[0] <= box[2] and box[1] <= q[1] <= box[3] for q in obst) + sum(
            not (box[2] < b[0] or b[2] < box[0] or box[3] < b[1] or b[3] < box[1]) for b in avoid)
        hits += 10 * (box[0] < frame[0] or box[1] < frame[1] or box[2] > frame[2] or box[3] > frame[3])
        if best is None or hits < best[0]:
            best = (hits, dx, dy, ha, box)
        if hits == 0:
            break
    _, dx, dy, ha, box = best
    ax.annotate(text, xy, xytext=(dx, dy), textcoords="offset points", ha=ha, va="center",
                fontsize=fontsize, color=color, zorder=6, **kw)
    return box, best[0]

NUM_SPOTS = [(0, 9, "center"), (0, -10, "center"), (7, 0, "left"), (-7, 0, "right")]

def place_numbers(ax, pts, order, avoid=()):
    # Number records in priority order (end first, earliest last) at a free spot beside each marker; a
    # number with no free spot is left off (the table under the charts has every step). Returns {index: text}.
    placed, shown = list(avoid), {}
    for i in order:
        if pts[i] is None:
            continue
        box, hits = place_label(ax, pts[i], str(i + 1), NUM_SPOTS, placed, fontsize=7.5)
        if hits:
            ax.texts[-1].remove()
            continue
        placed.append(box)
        shown[i] = ax.texts[-1]
    return shown

NOW_SPOTS = [(10, -12, "left"), (10, 10, "left"), (-10, 10, "right"), (-10, -12, "right"),
             (0, -22, "center"), (0, 20, "center")]

# 1. The frontier over time: numbered records on a staircase, the current record named at the end
fig, axes = plt.subplots(1, 3, figsize=(15, 4.9))
step_rows, later = [], []
for ax, task in zip(axes, REFERENCE):
    rs, steps = frontier(task)
    x, y = released, ratio(task)
    best_y = y(steps[-1])
    shown = [r for r in rs if y(r) >= 0.5 * best_y]  # off-scale outliers are listed in the credits table
    ax.scatter([x(r) for r in shown if r not in steps], [y(r) for r in shown if r not in steps],
               s=20, color=GREY, zorder=2, linewidths=0)
    xs = [x(r) for r in steps] + [max(x(r) for r in rs)]
    ys = [y(r) for r in steps] + [best_y]
    ax.step(xs, ys, where="post", color=BLUE, lw=1.8, zorder=3)
    cols = ramp_colors(len(steps), RAMP)
    ax.scatter([x(r) for r in steps], [y(r) for r in steps], s=46, c=cols, zorder=4,
               edgecolors="white", linewidths=1.5)
    late = [m for m in late_marks(rs) if y(m[0]) >= 0.5 * best_y]
    for r, fk, pub in late:  # hidden lead: hollow at first_known, dotted to the public point
        col = BLUE if r in steps else MUTED
        ax.plot([fk, pub], [y(r), y(r)], ls=":", lw=1.4, color=col, zorder=3)
        ax.scatter([fk], [y(r)], s=40, facecolors="white", edgecolors=col, linewidths=1.5, zorder=5)
    if late:
        ax.text(1.0, 1.02, f"{len(late)} disclosed late (hollow = first known)", transform=ax.transAxes,
                ha="right", fontsize=8, color=MUTED)
    plan = label_plan(steps, y)
    end = steps[plan["end"]]
    ax.set_title(TASK_NAMES[task], loc="left", pad=18)
    ax.text(0, 1.02, f"{len(steps)} records from {len(rs)} circuits", transform=ax.transAxes, fontsize=9,
            color=MUTED)
    ax.set_ylabel("× cheaper than reference (higher is better)")
    ax.grid(axis="y", color="#e8e7e2", lw=0.8)
    # About five ticks chosen from the plotted span, first-known markers included (fixed Day/Hour locators)
    loc = date_locator(min([x(r) for r in shown] + [fk for _, fk, _ in late]), max(x(r) for r in rs), mdates)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    pts = [(mdates.date2num(x(r)), y(r)) for r in steps]
    gains = [(pts[i], f"+{step_gains(steps, y)[i][2]:.1%}") for i in plan["gains"]]
    later.append((ax, pts, number_priority(steps, y), (pts[plan["end"]], end_label(end, y)), gains))
    for i, v, g in step_gains(steps, y):
        r = steps[i - 1]
        step_rows.append({"task": TASK_NAMES[task], "step": i,
                          "public since": r.get("public_since") or r["release_date"],
                          "first known": r.get("first_known", ""), "disclosure": r.get("disclosure", ""),
                          "circuit": r["sha12"], "published by": r["author"],
                          "core design by": r.get("core_author", ""), "qubits": r["qubits"],
                          "times cheaper than reference": v,
                          "gain over previous step": f"{g:+.2%}" if g is not None else "",
                          "what this file changes": r.get("change_summary", ""),
                          "link": r["source_notebook_url"]})
fig.suptitle("The public frontier over time: each numbered marker is a new record, darker = later",
             x=0.01, ha="left", fontsize=13, color=INK)
fig.tight_layout()
END_SPOTS = [(-8, -26, "right"), (-8, -44, "right"), (-30, -26, "right"), (-8, -62, "right"),
             (-8, -80, "right")]
GAIN_SPOTS = [(6, -11, "left"), (-6, -11, "right"), (6, 11, "left")]
for ax, pts, order, (end_xy, end_text), gains in later:  # labels placed after layout: collisions in final pixels
    avoid = [place_label(ax, end_xy, end_text, END_SPOTS)[0]]  # the current record, named
    avoid += [place_label(ax, xy, t, GAIN_SPOTS, avoid, fontsize=8, color=MUTED)[0] for xy, t in gains]
    place_numbers(ax, pts, order, avoid)
plt.show()

# 2. Where each gain came from: qubits against Toffolis, lines of equal cost, arrows from record to record
AXES = {"invsqrt": ("qubits", "Toffoli-equivalents per shot", lambda r, c: (int(r["qubits"]), c / int(r["qubits"]))),
        "dqi": ("qubits", "R × Toffolis per shot (thousands)", lambda r, c: (int(r["qubits"]), c / int(r["qubits"]) ** 2 / 1e3)),
        "chemistry": ("Toffolis", "λ_eff", lambda r, c: (float(r["T_total"]), float(r["lambda_eff"])))}

def equal_cost_curves(ax, task, best, steps, gx):
    q_chem = float(steps[-1]["Q_total"] or steps[-1]["qubits"]) if task == "chemistry" else None
    curves = []
    for k in (1.0, 1.1, 1.25, 1.45):
        cost = k * best
        gy = (cost / gx ** 2 / 1e3 if task == "dqi" else cost / gx if task == "invsqrt"
              else cost / (gx * q_chem))
        ax.plot(gx, gy, color="#e1e0da", lw=1, zorder=0)
        curves.append((k, gx, gy))
    return curves

def where_setup(ax, task):
    # Fixed frame for one task: equal-cost curves, limits, labels, and a magnified inset when the later
    # records crowd into a corner (zoom_from). Returns what the path drawing needs.
    rs, steps = frontier(task)
    xl, yl, f = AXES[task]
    best = chart_cost(steps[-1])
    near = [r for r in rs if chart_cost(r) <= 1.6 * best and (task != "chemistry" or r["T_total"])]
    pts = {r["sha12"]: f(r, chart_cost(r)) for r in near}
    px, py = zip(*pts.values())
    span = max(px) - min(px) or 1
    gx = np.linspace(min(px) - 0.5 * span, max(px) + 0.5 * span, 400)
    curves = equal_cost_curves(ax, task, best, steps, gx)
    pad_x, pad_y = 0.08 * (max(px) - min(px) or 1), 0.12 * (max(py) - min(py) or 1)
    at = [pts.get(r["sha12"]) for r in steps]
    k = zoom_from(at, max(px) - min(px), max(py) - min(py))
    ax.set_xlim(min(px) - pad_x, max(px) + pad_x * 2.2)  # room on the right for the curve labels
    y0, y1 = min(py) - pad_y, max(py) + pad_y
    ax.set_ylim(y0, y1 if k is None else y0 + (y1 - y0) / 0.48)  # with an inset: data in the lower half
    ax.set_xlabel(xl); ax.set_ylabel(yl)
    ax.set_title(TASK_NAMES[task], loc="left")
    ax.ticklabel_format(useOffset=False, style="plain", axis="both")
    x0, x1 = ax.get_xlim(); y0, y1 = ax.get_ylim()
    for c, gx_, gy_ in curves:  # label each equal-cost curve where it leaves the visible window
        inside = [(a, b) for a, b in zip(gx_, gy_) if x0 <= a <= x1 and y0 <= b <= y1]
        if inside:
            ax.annotate(f"{c:.2f}× best" if c > 1 else "best cost", inside[-1], fontsize=7.5, color=MUTED,
                        xytext=(2, 2), textcoords="offset points")
    ax.text(0.01, 0.97, "cheaper ↙", transform=ax.transAxes, fontsize=8.5, color=MUTED, va="top")
    hidden = len([r for r in rs if r not in near])
    if hidden:
        ax.text(1.0, 1.02, f"{hidden} costlier circuits not shown", transform=ax.transAxes,
                ha="right", fontsize=8, color=MUTED)
    inset = None
    if k is not None:
        tx, ty = zip(*[p for p in at[k:] if p is not None])
        sx, sy = (max(tx) - min(tx)) or 1, (max(ty) - min(ty)) or 1
        inset = ax.inset_axes([0.40, 0.53, 0.57, 0.42])
        equal_cost_curves(inset, task, best, steps, np.linspace(min(tx) - sx, max(tx) + sx, 200))
        inset.set_xlim(min(tx) - 0.12 * sx, max(tx) + 0.12 * sx)
        inset.set_ylim(min(ty) - 0.15 * sy, max(ty) + 0.15 * sy)
        inset.tick_params(labelsize=7)
        inset.ticklabel_format(useOffset=False, style="plain", axis="both")
        for s_ in ("top", "right"):
            inset.spines[s_].set_visible(True)
        inset.set_title(f"records {k + 1}–{len(steps)} magnified", fontsize=8, color=MUTED, loc="left",
                        fontweight="normal")
        ind = ax.indicate_inset_zoom(inset, edgecolor=MUTED, alpha=0.6)
        for c in (ind[1] if isinstance(ind, tuple) else getattr(ind, "connectors", None)) or []:
            c.set(alpha=0.3, lw=0.6)  # faint leaders: the box and the inset title carry the link
    return rs, steps, pts, inset, k

def where_draw(ax, task, rs, steps, pts, until=None, first=0):
    # Circuits public by `until` (all if None): grey others, the record path from record `first` on as
    # ramp-coloured arrows and markers. Returns (marker positions by step index, None where not drawn; the
    # records public by `until`).
    upto = [r for r in rs if until is None or released(r) <= until]
    rec = [r for r in steps if r in upto]
    rec_ids = {r["sha12"] for r in rec}
    other = [pts[r["sha12"]] for r in upto if r["sha12"] in pts and r["sha12"] not in rec_ids]
    if other:
        ax.scatter(*zip(*other), s=20, color=GREY, linewidths=0, zorder=2)
    cols = ramp_colors(len(steps), RAMP)
    at = [pts.get(r["sha12"]) for r in steps]  # None: record off this chart's window
    path = [(i, at[i]) for i in range(first, len(rec)) if at[i] is not None]
    for (i, a), (j, b) in path_segments(path):
        ax.annotate("", b, xytext=a, zorder=3, arrowprops=dict(arrowstyle="-|>", color=cols[j], lw=1.5,
                    mutation_scale=11, shrinkA=4, shrinkB=5))
    if path:
        ax.scatter([p[0] for _, p in path], [p[1] for _, p in path], s=46, c=[cols[i] for i, _ in path],
                   edgecolors="white", linewidths=1.5, zorder=4)
    return [at[i] if first <= i < len(rec) else None for i in range(len(steps))], rec

def where_panel(ax, task, until=None):
    # One panel: frame, path, and (when crowded) the inset. Returns [(axes, marker positions)] whose numbers
    # belong on that axes: records before the inset's first on the main axes, the rest in the inset.
    rs, steps, pts, inset, k = where_setup(ax, task)
    at, rec = where_draw(ax, task, rs, steps, pts, until)
    if inset is None:
        return [(ax, at)], steps, rec
    in_at, _ = where_draw(inset, task, rs, steps, pts, until, first=k)
    return [(ax, [p if i < k else None for i, p in enumerate(at)]), (inset, in_at)], steps, rec

fig, axes = plt.subplots(1, 3, figsize=(15, 4.9))
later = []
for ax, task in zip(axes, REFERENCE):
    parts, steps, _ = where_panel(ax, task)
    later.append((parts, number_priority(steps, ratio(task)), steps[-1]["author"]))
fig.suptitle("Where the gains came from: arrows run from each record to the next (down and left is cheaper)",
             x=0.01, ha="left", fontsize=13, color=INK)
fig.tight_layout()
for parts, order, who in later:  # the current record is named first, numbers fit around it
    for a, at in parts:
        end_xy = at[order[0]] if order else None
        avoid = [place_label(a, end_xy, f"now: {who}", NOW_SPOTS)[0]] if end_xy else []
        place_numbers(a, at, order, avoid)
plt.show()

# 3. The same DQI and chemistry paths replayed in public order, one record per frame, as a GIF (an <img> in
# the output, so it plays in the notebook viewer without JavaScript). The static chart above is the reference.
try:
    from PIL import Image
    ANIM = ("dqi", "chemistry")
    fig = plt.figure(figsize=(10.5, 4.5), dpi=72)
    events = sorted((released(r), t, i) for t in ANIM for i, r in enumerate(frontier(t)[1]))
    HOLD = 6
    # Both panels are drawn once, in their final state, and the record numbers are placed there; each frame
    # then only shows or hides those artists, so it costs one canvas draw rather than a rebuild (~10x faster).
    axes = fig.subplots(1, 2)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.78, bottom=0.12, wspace=0.28)
    panels = {}
    for ax, t in zip(axes, ANIM):
        rs, steps, pts, inset, k0 = where_setup(ax, t)
        cols = ramp_colors(len(steps), RAMP)
        at = [pts.get(r["sha12"]) for r in steps]  # None: record off this chart's window
        rec_ids = {r["sha12"] for r in steps}
        others = [(released(r), pts[r["sha12"]]) for r in rs if r["sha12"] in pts and r["sha12"] not in rec_ids]
        layers = []  # main axes: numbers for records before the inset's first; inset: the rest
        for a, lo, hi in [(ax, 0, len(steps) if inset is None else k0)] + (
                [(inset, k0, len(steps))] if inset is not None else []):
            first = lo if a is inset else 0  # the main axes draws the whole path, as where_draw does
            grey = a.scatter([p[0] for _, p in others], [p[1] for _, p in others], s=20, color=GREY,
                             linewidths=0, zorder=2)
            path = [(i, at[i]) for i in range(first, len(steps)) if at[i] is not None]
            arrows = [(j, a.annotate("", q, xytext=p, zorder=3, arrowprops=dict(
                arrowstyle="-|>", color=cols[j], lw=1.5, mutation_scale=11, shrinkA=4, shrinkB=5)))
                for (i, p), (j, q) in path_segments(path)]
            marks = a.scatter([p[0] for _, p in path], [p[1] for _, p in path], s=46,
                              c=[cols[i] for i, _ in path] or None, edgecolors="white", linewidths=1.5,
                              zorder=4)
            ring = a.scatter([], [], s=170, facecolors="none", edgecolors=INK, linewidths=1.2, zorder=5)
            nums = place_numbers(a, [p if lo <= i < hi else None for i, p in enumerate(at)],
                                 number_priority(steps, ratio(t)))
            layers.append((lo, hi, grey, arrows, marks, ring, nums, path))
        panels[t] = (steps, cols, at, others, layers)
    title = fig.text(0.01, 0.95, "", fontsize=11, color=INK, ha="left")
    fig.text(0.01, 0.89, "arrows run from each record to the next; down and left is cheaper",
             fontsize=9, color=MUTED, ha="left")

    def frame(k):
        k = min(k, len(events) - 1)
        when, task, i = events[k]
        for t, (steps, cols, at, others, layers) in panels.items():
            n = sum(released(r) <= when for r in steps)  # records public by now
            grey_xy = np.array([p for d, p in others if d <= when]).reshape(-1, 2)
            for lo, hi, grey, arrows, marks, ring, nums, path in layers:
                grey.set_offsets(grey_xy)
                for j, arrow in arrows:
                    arrow.set_visible(j < n)
                shown = [(j, p) for j, p in path if j < n]
                marks.set_offsets(np.array([p for _, p in shown]).reshape(-1, 2))
                marks.set_facecolor([cols[j] for j, _ in shown] or [INK])
                now = t == task and lo <= i < hi and at[i] is not None  # ring the record that just arrived
                ring.set_offsets(np.array([at[i]] if now else []).reshape(-1, 2))
                for j, text in nums.items():
                    text.set_visible(j < n)
        steps = panels[task][0]
        g = step_gains(steps, ratio(task))[i][2]
        title.set_text(f"{when:%d %b %H:%M} UTC   {TASK_NAMES[task]} record {i + 1} of {len(steps)}: "
                       f"{steps[i]['author']}" + (f", {g:+.1%}" if g is not None else ""))
    # Blitting: everything below zorder 2 (axes, ticks, grid, equal-cost curves) is drawn once; each frame
    # restores that and repaints only each panel's upper layers, in the order Axes.draw uses, then the title.
    # Animated artists are left out of canvas.draw() (matplotlib's blitting rule); set_visible would not do:
    # the inset-zoom indicator draws even when hidden. The title is still empty here, so it is not in it.
    upper = {ax: [c for c in ax.get_children() if c.get_zorder() >= 2] for ax in axes}
    for c in [c for cs in upper.values() for c in cs]:
        c.set_animated(True)
    fig.canvas.draw()
    background = fig.canvas.copy_from_bbox(fig.bbox)
    renderer = fig.canvas.get_renderer()
    size = fig.canvas.get_width_height()
    images = []
    for k in range(len(events) + HOLD):  # the last record held for HOLD more frames
        frame(k)
        fig.canvas.restore_region(background)
        for ax in axes:
            for c in sorted(upper[ax], key=lambda c: c.get_zorder()):  # stable, as Axes.draw sorts
                if c.get_visible():
                    c.draw(renderer)
        title.draw(renderer)
        images.append(Image.frombuffer("RGBA", size, bytes(fig.canvas.buffer_rgba()), "raw", "RGBA", 0, 1)
                      .convert("RGB"))  # opaque: RGB quantises to the GIF palette better (as PillowWriter)
    gif = WORK / "frontier_path.gif"
    images[0].save(gif, save_all=True, append_images=images[1:], duration=400, loop=0)  # 2.5 frames/s
    plt.close(fig)
    b64 = base64.b64encode(gif.read_bytes()).decode()
    display(HTML(f'<img src="data:image/gif;base64,{b64}" alt="DQI and chemistry frontier paths replayed '
                 f'in public order" style="max-width:100%">'))
    print(f"frontier_path.gif: {len(events)} records, {gif.stat().st_size / 1e6:.2f} MB (Output tab)")
except Exception as e:  # the static charts above carry the same information
    plt.close("all")
    print(f"animation skipped ({type(e).__name__}: {e})")

steps_df = pd.DataFrame(step_rows)
show(steps_df, height=420)
steps_df.to_csv(WORK / "frontier_steps.csv", index=False)

#!/usr/bin/env python3
# Copyright 2026 James Barlow
# SPDX-License-Identifier: Apache-2.0
"""Build a bounded, source-linked fact packet from a completed Friends run.

This is read-only: it never calls an LLM or changes the verifier, circuit picks,
or notebook. The packet is the proposed interface for optional commentary.

Usage: python3 scripts/friends_commentary_facts.py OUTPUT_DIR --out PACKET.json
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def read_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def short_record(row: dict, previous: dict | None) -> dict:
    ratio = float(row["times cheaper than reference"])
    prior_ratio = float(previous["times cheaper than reference"]) if previous else None
    return {
        "task": row["task"],
        "public_since_utc": row["public since"],
        "sha12": row["circuit"],
        "author": row["published by"],
        "source_notebook": row["link"],
        "qubits": int(row["qubits"]),
        "ratio_to_reference": ratio,
        "previous_sha12": previous["circuit"] if previous else None,
        "task_cost_reduction_pct_vs_previous": round(100 * (1 - prior_ratio / ratio), 6) if prior_ratio else None,
        "source_change_summary": row["what this file changes"].strip() or None,
    }


def build_packet(output_dir: Path) -> dict:
    check = json.loads((output_dir / "check.json").read_text())
    rows = list(csv.DictReader((output_dir / "frontier_steps.csv").open(newline="")))
    by_task: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_task[row["task"]].append(row)
    latest = max((t for row in rows if (t := read_time(row["public since"]))), default=None)
    cutoff = latest - timedelta(hours=24) if latest else None
    records = []
    recent = []
    omitted = 0
    for task, history in by_task.items():
        if not history:
            continue
        records.append(short_record(history[-1], history[-2] if len(history) > 1 else None))
        task_recent = []
        for previous, row in zip([None, *history[:-1]], history):
            when = read_time(row["public since"])
            if cutoff and when and when >= cutoff:
                task_recent.append(short_record(row, previous))
        omitted += max(0, len(task_recent) - 2)
        recent.extend(task_recent[-2:])
    recent.sort(key=lambda x: (x["public_since_utc"], x["task"], x["sha12"]))
    return {
        "schema": "friends-frontier-commentary-facts-v1",
        "manifest_sha256": check["manifest_sha256"],
        "source_output": output_dir.name,
        "latest_public_step_utc": latest.isoformat() if latest else None,
        "recent_window": "24 hours before the latest public frontier step, not the notebook run time",
        "recent_step_limit_per_task": 2,
        "recent_steps_omitted": omitted,
        "current_portfolio": {
            task: {"sha12": pick["sha256"][:12], "author": pick["author"], "source_notebook": pick["source_notebook_url"]}
            for task, pick in check["picks"].items()
        },
        "hedge_blend": check.get("hedge_blend", {}),
        "current_frontier_records": records,
        "recent_frontier_steps": recent,
        "commentary_boundaries": [
            "The source_change_summary is provenance text, not an independently proved mechanism.",
            "Task cost reductions are computed from the two verified ratio-to-reference values; do not call them portfolio GM gains.",
            "The portfolio score in check.json is local/hosted notebook arithmetic, not a competition leaderboard score.",
            "Every factual claim in generated prose must point to a SHA-12 or source notebook in this packet.",
        ],
    }


def render_summary(packet: dict) -> str:
    lines = ["### Public-circuits dataset frontier", ""]
    stamp = packet["latest_public_step_utc"]
    lines.append(f"Latest recorded dataset step: {stamp} UTC. This summary comes from the "
                 "attached public-circuits dataset and its verified cost records.")
    lines.extend(["", "| Task | Current public record | Previous record | Task cost reduction |", "| --- | --- | --- | ---: |"])
    for r in packet["current_frontier_records"]:
        gain = r["task_cost_reduction_pct_vs_previous"]
        gain_label = f"{gain:.3f}%" if gain is not None else "—"
        lines.append(f"| {r['task']} | [{r['author']} `{r['sha12']}`]({r['source_notebook']}) "
                     f"({r['public_since_utc']} UTC) | `{r['previous_sha12'] or '—'}` | {gain_label} |")
    lines.extend(["", "These percentages compare each task with its previous public record. "
                  "They are not changes in the portfolio geometric mean or observed leaderboard scores."])
    for task, selected in packet.get("hedge_blend", {}).items():
        gain = 100 * (1 - selected["candidate_rank_cost"] / selected["base_rank_cost"])
        lines.extend(["", f"The blend selects [{selected['entry']} `{selected['candidate_sha256'][:12]}`]"
                      f"({selected['source_notebook_url']}) for {task}: {gain:.4f}% lower task cost than "
                      f"`{selected['base_sha256'][:12]}` on {selected['basis']}. The official verifier "
                      "checks the selected circuit in this run."])
    return "\n".join(lines) + "\n"




from IPython.display import Markdown, display

commentary_dir = Path('/kaggle/working')
commentary_packet = build_packet(commentary_dir)
commentary_text = render_summary(commentary_packet)
(commentary_dir / 'frontier_commentary_facts.json').write_text(
    json.dumps(commentary_packet, indent=2, ensure_ascii=False) + '\n')
(commentary_dir / 'frontier_commentary.md').write_text(commentary_text)
display(Markdown(commentary_text))

import pandas as pd
from IPython.display import display, HTML
import html as _html

_days = sorted({(r.get('first_seen') or '')[:10] for r in rows if r.get('first_seen')})
_latest = _days[-1] if _days else None
_task_names = {'invsqrt': 'Inverse square root', 'dqi': 'DQI decoder', 'dfthcsos': 'Chemistry (dfthcsos)',
               'chemistry': 'Chemistry (dfthcsos)'}
_new = []
for r in rows:
    if not _latest or (r.get('first_seen') or '')[:10] != _latest:
        continue
    t = r.get('task')
    c = rank_cost(r)
    _order = rank_order([x for x in rows if x.get('task') == t])  # the dataset's pick, before any Hedge overlay
    pk = _order[0] if _order else None
    pc = rank_cost(pk) if pk else None
    if pk and r.get('sha256') == pk.get('sha256'):
        standing = 'current pick'
    elif c is None or pc is None:
        standing = 'unscored'
    else:
        d = c / pc - 1
        standing = 'tie with pick' if abs(d) <= REL_TIE else (f'{d:+.3%} vs pick' if d > 0 else f'beats pick by {-d:.3%}')
    url = r.get('source_notebook_url') or ''
    ver = r.get('source_notebook_version') or ''
    who = _html.escape(r.get('author') or 'unknown')
    link = f'<a href="{_html.escape(url)}">{who}</a>' + (f' v{_html.escape(str(ver))}' if ver else '') if url else who
    _new.append({'Task': _task_names.get(t, t), 'Circuit': f'<code>{_html.escape(r.get("sha12") or r.get("sha256", "")[:12])}</code>',
                 'Source': link, 'Public': (r.get('public_since') or r.get('release_date') or '')[:16],
                 'Qubits': r.get('qubits') or '',
                 'Task cost': f'{c:,.2f}' if c is not None else 'unscored',
                 'Basis': rank_basis(r) if t == 'dqi' else 'default salt',
                 'Standing': standing})
if not _new:
    print('No new circuits in the latest dataset release.')
else:
    _df = pd.DataFrame(_new).sort_values(['Task', 'Public'])
    display(HTML(f'<p>{len(_df)} circuits first seen on {_latest} (UTC).</p>' + _df.to_html(index=False, escape=False)))

# Copyright 2026 James Barlow
# SPDX-License-Identifier: Apache-2.0
# AI-assisted (Claude, Anthropic), under the repository owner's direction.
# Chemistry word tables: stable labels, lineage and frontier forensics (2026-10-31).
#
# A word table is the 8-bit angle data the angle loaders k (ANGLE_k QROMs) write; it splits into one block of words per
# angle loader. The parts ledger's T00, T01, ... labels are ranks (cheapest family first) and change between versions;
# the W-labels here are stable: W01, W02, ... in order of first public appearance (first_release, ties by table_sha256),
# assigned once in scripts/chem_word_table_registry.json, never reused or renumbered.
#
# Pure functions of parts rows (dicts of strings, as csv.DictReader gives) and the registry; standard library only.
# scripts/chem_parts_ledger.py imports this module to write the label and lineage columns, and
# scripts/build_friends_composite_notebook.py embeds this file's text in the notebook (so no __future__ import, no
# top-level side effects, and every helper name starts with wt_ to stay clear of the notebook's own names).
import json as _wt_json

WT_FORMAT = "W{:02d}"
WT_SCHEME = ("W-labels: one per distinct word table (table_sha256), numbered W01, W02, ... in order of first public "
             "appearance (first_release; a date alone counts as 00:00; ties by table_sha256). Assigned once, never "
             "reused or renumbered; new tables are appended.")


def wt_when(s):
    # Sort key of a release string: 'YYYY-MM-DD' counts as 00:00 (as release_key in chem_parts_ledger.py)
    s = (s or "").strip()
    if not s:
        return "9999-99-99 99:99"
    return s + " 00:00" if len(s) == 10 else s[:16]


def wt_blocks(t):
    # Block hashes of a word_tables.csv row, one per angle loader k
    return [b for b in (t.get("block_words_sha256") or "").split(";") if b]


def wt_shape(t):
    return tuple(str(t.get(c, "")) for c in ("rows", "J", "brot", "slots"))


def wt_num(label):
    return int(str(label)[1:])


def wt_describe(e):
    rbc = ", ".join(str(e.get(c, "?")) for c in ("R", "B", "C"))
    return (f"{e.get('first_author') or '?'}, first public {e.get('first_release') or '?'}; (R, B, C) = ({rbc}); "
            f"J = {e.get('J')}, brot = {e.get('brot')}, {e.get('rows')} rows, {e.get('slots')} angle loaders")


def wt_assign(registry, tables, shapes=None, today=""):
    # New registry: the old entries unchanged, then every table not yet in it, numbered on in order of first public
    # appearance. tables: word_tables.csv rows; shapes: {table_sha256: {'R','B','C'}} of each table's first family.
    reg = {"scheme": WT_SCHEME, "tables": [dict(e) for e in (registry or {}).get("tables", [])]}
    known = {e["table_sha256"] for e in reg["tables"]}
    n = max((wt_num(e["label"]) for e in reg["tables"]), default=0)
    new = {t["table_sha256"]: t for t in tables if t["table_sha256"] not in known}
    for sha in sorted(new, key=lambda s: (wt_when(new[s].get("first_release")), s)):
        t, n = new[sha], n + 1
        e = {"label": WT_FORMAT.format(n), "table_sha256": sha, "table_key": sha[:12],
             "first_family": t.get("first_family", ""), "first_author": t.get("first_author", ""),
             "first_release": t.get("first_release", ""),
             **{c: str((shapes or {}).get(sha, {}).get(c, "")) for c in ("R", "B", "C")},
             **{c: str(t.get(c, "")) for c in ("J", "brot", "rows", "slots")}, "assigned": today}
        e["description"] = wt_describe(e)
        reg["tables"].append(e)
    labels = [e["label"] for e in reg["tables"]]
    shas = [e["table_sha256"] for e in reg["tables"]]
    if len(set(labels)) != len(labels) or len(set(shas)) != len(shas):
        raise ValueError("word-table registry: a label or a table is listed twice")
    return reg


def wt_load(path):
    try:
        with open(path) as f:
            return _wt_json.load(f)
    except FileNotFoundError:
        return {"scheme": WT_SCHEME, "tables": []}


def wt_labels(registry):
    # {table_sha256: label} and {table_key: label}
    by_sha = {e["table_sha256"]: e["label"] for e in registry.get("tables", [])}
    return by_sha, {s[:12]: lab for s, lab in by_sha.items()}


def wt_lineage(tables, label_of):
    # {table_sha256: lineage} for word_tables.csv rows. Parent = the earlier table (lower W-number) sharing the most
    # block hashes at the same angle loader k; ties go to the most recent. changed = the angle loaders k whose block
    # differs from the parent's. A table sharing no block: 'new shape, no parent' when no earlier table has its shape.
    order = sorted(tables, key=lambda t: wt_num(label_of[t["table_sha256"]]))
    out = {}
    for i, t in enumerate(order):
        b, best = wt_blocks(t), None
        for p in order[:i]:
            shared = sum(1 for x, y in zip(b, wt_blocks(p)) if x == y)
            if shared and (best is None or shared >= best[0]):
                best = (shared, p)
        if best:
            pb = wt_blocks(best[1])
            changed = [k for k in range(len(b)) if k >= len(pb) or b[k] != pb[k]]
            plab = label_of[best[1]["table_sha256"]]
            note = (f"same blocks as {plab}" if not changed else
                    f"inherits {best[0]} of {len(b)} blocks from {plab}; new data for angle loaders "
                    + ", ".join(map(str, changed)))
            out[t["table_sha256"]] = {"parent_label": plab, "parent_shared_blocks": best[0], "changed_loaders": changed,
                                      "lineage_note": note}
        else:
            same = [p for p in order[:i] if wt_shape(p) == wt_shape(t)]
            note = ("first word table" if i == 0 else
                    f"same shape as {label_of[same[-1]['table_sha256']]}, no shared blocks" if same
                    else "new shape, no parent")
            out[t["table_sha256"]] = {"parent_label": "", "parent_shared_blocks": 0,
                                      "changed_loaders": list(range(len(b))), "lineage_note": note}
    return out


def wt_loader_history(families, circuits, key):
    # {k: [(release, part_id, toffoli, author, family)]} for word table `key`: each angle-loader circuit at the first time a
    # family on this table used it, in public order (families.csv and angle_circuits.csv rows). Circuits inherited from
    # an earlier table count from their first use on this one.
    T = {c["part_id"]: (float(c["toffoli_per_shot"]), c.get("first_author", "")) for c in circuits}
    fams = sorted((f for f in families if f.get("table_key") == key and f.get("status", "PASS") == "PASS"),
                  key=lambda f: (wt_when(f.get("release")), f["family"]))
    seen, hist = set(), {}
    for f in fams:
        for pid in (f.get("parts") or "").split(";"):
            if not pid or pid in seen or pid not in T:
                continue
            seen.add(pid)
            hist.setdefault(int(pid[1:3]), []).append((wt_when(f.get("release")), pid, T[pid][0], f.get("author", ""),
                                                       f["family"]))
    return hist


def wt_history_summary(h):
    # (circuits, cheaper compiles, first Toffolis, cheapest Toffolis, cheapest part_id) of one angle loader's history
    if not h:
        return 0, 0, None, None, ""
    best, cheaper, best_id = h[0][2], 0, h[0][1]
    for _, pid, t, _, _ in h[1:]:
        if t < best - 1e-9:
            best, best_id, cheaper = t, pid, cheaper + 1
    return len(h), cheaper, h[0][2], best, best_id


def wt_block_rows(tables, label_of, families, circuits):
    # parts/word_table_blocks.csv rows: one per (word table, angle loader k), with the block's data origin (the
    # W-label of the first table where this block appeared at this k) and the circuit history on this table
    order = sorted(tables, key=lambda t: wt_num(label_of[t["table_sha256"]]))
    lin = wt_lineage(tables, label_of)
    origin, rows = {}, []
    for t in order:
        lab, key = label_of[t["table_sha256"]], t["table_key"]
        hist = wt_loader_history(families, circuits, key)
        par = lin[t["table_sha256"]]
        for k, b in enumerate(wt_blocks(t)):
            src = origin.setdefault((k, b), lab)
            n, cheaper, first, best, best_id = wt_history_summary(hist.get(k, []))
            rows.append({"table_key": key, "stable_label": lab, "angle_loader_k": k, "block_words_sha256": b,
                         "data_origin": src, "new_data": src == lab,
                         "changed_vs_parent": bool(par["parent_label"]) and k in par["changed_loaders"],
                         "parent_label": par["parent_label"], "n_circuits": n, "n_cheaper_compiles": cheaper,
                         "first_toffoli": first if first is not None else "",
                         "cheapest_toffoli": best if best is not None else "", "cheapest_part_id": best_id})
    return rows


def wt_frontier(steps, fam_by_sha, table_by_key, label_by_key, released, cost):
    # One dict per chemistry frontier record (steps from frontier_of(), public order), classified against the record
    # before it: 'new word table' (new shape, or same shape with some blocks changed), 'cheaper angle-loader circuit'
    # (same table, other loaders), 'angle-loader circuits and weights', 'weights only' (same table and loaders), or
    # 'same files'. Families missing from the parts ledger get table '' and kind 'not in the parts ledger'.
    out, prev = [], None
    for r in steps:
        f = fam_by_sha.get(r["sha12"]) or {}
        key = f.get("table_key") or ""
        rec = {"when": released(r), "circuit": r["sha12"], "author": r.get("author", ""), "cost": cost(r),
               "table_key": key, "label": label_by_key.get(key, ""), "RBC": tuple(r.get(c, "") for c in "RBC"),
               "kind": "first record", "detail": "", "changed_loaders": [], "from_label": "", "cost_before": None}
        if prev is not None:
            pf = fam_by_sha.get(prev["circuit"]) or {}
            rec["from_label"], rec["cost_before"] = prev["label"], prev["cost"]
            if not key or not pf.get("table_key"):
                rec["kind"] = "not in the parts ledger"
            elif key != pf["table_key"]:
                a, b = table_by_key.get(pf["table_key"], {}), table_by_key.get(key, {})
                if wt_shape(a) != wt_shape(b):
                    rec["kind"] = "new word table, new shape"
                    rec["detail"] = (f"(R, B, C) {', '.join(prev['RBC'])} → {', '.join(rec['RBC'])}; "
                                     f"{a.get('rows')} → {b.get('rows')} rows")
                else:
                    ch = [k for k, (x, y) in enumerate(zip(wt_blocks(a), wt_blocks(b))) if x != y]
                    rec["kind"] = "new word table, same shape"
                    rec["changed_loaders"] = ch
                    rec["detail"] = (f"{len(ch)} of {len(wt_blocks(b))} blocks changed (angle loaders "
                                     f"{', '.join(map(str, ch))})")
            else:
                pa, pb = (pf.get("parts") or "").split(";"), (f.get("parts") or "").split(";")
                ch = [k for k, (x, y) in enumerate(zip(pa, pb)) if x != y]
                w = pf.get("npz_sha256") != f.get("npz_sha256")
                rec["changed_loaders"] = ch
                rec["kind"] = ("angle-loader circuits and weights" if ch and w else "cheaper angle-loader circuit"
                               if ch else "weights only" if w else "same files")
                if ch:
                    try:
                        dt = float(f["T_official"]) - float(pf["T_official"])
                        rec["detail"] = (f"angle loaders {', '.join(map(str, ch))} recompiled, {dt:+,.0f} Toffolis")
                    except (KeyError, ValueError):
                        rec["detail"] = f"angle loaders {', '.join(map(str, ch))} recompiled"
        out.append(rec)
        prev = rec
    return out


def wt_switches(recs):
    # The records where the frontier moved to another word table
    return [r for r in recs[1:] if r["label"] != r["from_label"]]


def wt_held(recs):
    # Spells on the frontier, in order: [{'label', 'table_key', 'from', 'to' (None = still holds), 'records',
    # 'best_cost', 'first_holder', 'last_holder'}]. A table that regains the frontier gets a second spell.
    spells = []
    for r in recs:
        if not spells or spells[-1]["label"] != r["label"]:
            if spells:
                spells[-1]["to"] = r["when"]
            spells.append({"label": r["label"], "table_key": r["table_key"], "from": r["when"], "to": None,
                           "records": 0, "best_cost": r["cost"], "first_holder": r["author"], "last_holder": r["author"]})
        s = spells[-1]
        s["records"] += 1
        s["best_cost"] = min(s["best_cost"], r["cost"])
        s["last_holder"] = r["author"]
    return spells


# The word table that holds the chemistry frontier now, and every public circuit on it. From manifest.csv and
# parts/ (word_tables.csv, families.csv): a new dataset version redraws it with no notebook change.
parts_dir = DATA / "parts"
if not (parts_dir / "word_table_blocks.csv").is_file():
    print("this dataset version has no stable word-table labels (parts/word_table_blocks.csv): section skipped")
else:
    wt_tables = list(csv.DictReader(open(parts_dir / "word_tables.csv", newline="")))
    wt_fams = list(csv.DictReader(open(parts_dir / "families.csv", newline="")))
    t_by_key = {t["table_key"]: t for t in wt_tables}
    lab_by_key = {t["table_key"]: t["stable_label"] for t in wt_tables}
    fam_by_sha = {f["family"]: f for f in wt_fams}
    _, chem_steps = frontier_of(rows, "chemistry")
    spells = wt_held(wt_frontier(chem_steps, fam_by_sha, t_by_key, lab_by_key, released, chart_cost))
    cur = spells[-1]
    key, lab, tinfo = cur["table_key"], cur["label"], t_by_key.get(cur["table_key"], {})
    ref = REFERENCE["chemistry"]
    on = sorted((r for r in rows if r["task"] == "chemistry" and released(r) and chart_cost(r)
                 and (fam_by_sha.get(r["sha12"]) or {}).get("table_key") == key),
                key=lambda r: (released(r), chart_cost(r)))
    tsteps = []  # records on this table, same tie rule as the ranking
    for r in on:
        if not tsteps or chart_cost(r) < chart_cost(tsteps[-1]) * (1 - REL_TIE):
            tsteps.append(r)
    trecs = wt_frontier(tsteps, fam_by_sha, t_by_key, lab_by_key, released, chart_cost)
    best, first = tsteps[-1], tsteps[0]
    at_switch = min((chart_cost(r) for r in on if released(r) <= cur["from"]), default=chart_cost(first))
    age = (NOW.replace(tzinfo=None) - cur["from"]).total_seconds() / 86400
    prev = spells[-2]["label"] if len(spells) > 1 else "none"
    kinds = pd.Series([r["kind"] for r in trecs[1:]]).value_counts()
    last = trecs[-1]
    gain = lambda a, b: f"{a / b - 1:+.3%}"
    glance = pd.DataFrame([
        ("word table", f"{lab}: (R, B, C) = ({tinfo.get('R')}, {tinfo.get('B')}, {tinfo.get('C')}), "
                       f"first published by {tinfo.get('first_author', '?')}"),
        ("on the frontier since", f"{cur['from']:%d %b %H:%M} UTC ({age:.1f} days; before it: {prev})"),
        ("best on it now", f"{ref / chart_cost(best):.4f}× the reference, by {best['author']} "
                           f"({best['sha12']})"),
        ("λ_eff · Toffolis · qubits", f"{float(best['lambda_eff']):.6f} · {float(best['T_total']):,.0f} · "
                                      f"{best['qubits']}"),
        ("records on this table", f"{len(trecs)} from {len(on)} circuits"
         + ("".join(f"; {n} {k}" for k, n in kinds.items()) if len(kinds) else "")),
        ("gain since it took the frontier", gain(at_switch, chart_cost(best))),
        ("latest record", f"{last['when']:%d %b %H:%M} UTC, {last['author']}: {last['kind']}"
                          + (f", {gain(last['cost_before'], last['cost'])}" if last["cost_before"] else "")),
    ], columns=["chemistry", "now"])
    show(glance)

    KIND_COL = {"weights only": BLUE, "cheaper angle-loader circuit": "#eb6834",
                "angle-loader circuits and weights": "#1baf7a"}
    fig, ax = plt.subplots(figsize=(12, 4.8))
    xs = lambda rs: [released(r) for r in rs]
    ys = lambda rs: [ref / chart_cost(r) for r in rs]
    others = [r for r in on if r not in tsteps]
    if others:
        ax.scatter(xs(others), ys(others), s=18, color=GREY, linewidths=0, zorder=2, label="other circuits")
    t_end = max(NOW.replace(tzinfo=None), max(xs(on)))
    ax.step(xs(tsteps) + [t_end], ys(tsteps) + [ys(tsteps)[-1]], where="post", color=INK, lw=1.4, zorder=3)
    for kind in ["first record"] + list(KIND_COL):
        pts = [(s, rec) for s, rec in zip(tsteps, trecs) if (rec["kind"] == kind)
               or (kind == "first record" and rec["kind"] not in KIND_COL)]
        if pts:
            ax.scatter(xs([p[0] for p in pts]), ys([p[0] for p in pts]), s=52, zorder=4,
                       color=KIND_COL.get(kind, MUTED), edgecolors="white", linewidths=1.4,
                       label=kind if kind != "first record" else "first circuit on the table")
    if cur["from"] > min(xs(on)):
        ax.axvline(cur["from"], color=MUTED, ls=":", lw=1)
        ax.text(cur["from"], 0.02, f" {lab} takes the frontier", transform=ax.get_xaxis_transform(),
                fontsize=8.5, color=MUTED)
    for r in tsteps[:-1]:
        ax.annotate(r["author"], (released(r), ys([r])[0]), xytext=(6, -12), textcoords="offset points",
                    fontsize=8, color=MUTED)
    ax.annotate(f"now: {best['author']}\n{ref / chart_cost(best):.4f}×", (released(best), ys([best])[0]),
                xytext=(-8, 10), textcoords="offset points", ha="right", fontsize=9, color=INK)
    ax.set_title(f"Chemistry on {lab}: times cheaper than the reference (higher is better)", loc="left")
    ax.set_ylabel("× cheaper than reference")
    ax.grid(axis="y", color="#e8e7e2", lw=0.8)
    loc = date_locator(min(xs(on)), t_end, mdates)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.legend(loc="lower right", frameon=False, fontsize=8.5)
    fig.tight_layout()
    plt.show()
    lin = pd.DataFrame([{"released (UTC)": f"{rec['when']:%Y-%m-%d %H:%M}", "circuit": rec["circuit"],
                         "published by": rec["author"], "what changed": rec["kind"], "detail": rec["detail"],
                         "× reference": round(ref / rec["cost"], 6),
                         "cheaper than the record before": gain(rec["cost_before"], rec["cost"]) if rec["cost_before"] else ""}
                        for rec in trecs])
    lin.to_csv(WORK / "chemistry_current_table.csv", index=False)
    print(f"Records on {lab} (also saved as chemistry_current_table.csv):")
    show(lin.iloc[::-1], height=300)

# Best public chemistry cost over time, coloured by the word table that held it; each switch between tables is
# numbered and listed on the right. From manifest.csv and parts/: a new dataset version redraws it.
from datetime import timedelta
if not (DATA / "parts" / "word_table_blocks.csv").is_file():
    print("this dataset version has no stable word-table labels (parts/word_table_blocks.csv): chart skipped")
else:
    t_by_lab = {t["stable_label"]: t for t in wt_tables}
    recs = wt_frontier(chem_steps, fam_by_sha, t_by_key, lab_by_key, released, chart_cost)
    spells, switches = wt_held(recs), wt_switches(recs)
    cur = spells[-1]
    labs = list(dict.fromkeys(s["label"] for s in spells))
    HUES = ["#eda100", "#1baf7a", "#eb6834", BLUE]  # the latest four tables; the house blue is the current
    OLD = ["#b8b7b0", "#8c8a85"]
    col = {lab: (HUES[len(HUES) - len(labs) + i] if i >= len(labs) - len(HUES)
                 else OLD[i % 2]) for i, lab in enumerate(labs)}
    fig, (ax, side) = plt.subplots(1, 2, figsize=(15, 5.6), gridspec_kw={"width_ratios": [2.1, 1]})
    t_now = max(released(r) for r in rows if r["task"] == "chemistry" and released(r))
    # The last few switches are small and close together: they get a magnified inset (upper right is empty,
    # because cost only falls)
    z0 = switches[max(0, len(switches) - 4)]["when"] - timedelta(hours=3) if len(switches) > 4 else None
    inset = ax.inset_axes([0.40, 0.40, 0.58, 0.55]) if z0 else None

    def draw_path(a, numbers):
        for s in spells:
            sr = [r for r in recs if r["label"] == s["label"] and s["from"] <= r["when"] and
                  (s["to"] is None or r["when"] < s["to"])]
            xs = [r["when"] for r in sr] + [s["to"] or t_now]
            ys = [r["cost"] / 1e6 for r in sr] + [sr[-1]["cost"] / 1e6]
            a.step(xs, ys, where="post", color=col[s["label"]], lw=2.2, zorder=3)
            a.scatter([r["when"] for r in sr], [r["cost"] / 1e6 for r in sr], s=14, color=col[s["label"]],
                      zorder=4, linewidths=0)
        for i, r in enumerate(switches, 1):
            prev = recs[recs.index(r) - 1]
            a.plot([r["when"], r["when"]], [prev["cost"] / 1e6, r["cost"] / 1e6], color=col[r["label"]],
                   lw=2.2, zorder=3)
            a.scatter([r["when"]], [r["cost"] / 1e6], s=95, color=col[r["label"]], edgecolors="white",
                      linewidths=1.5, zorder=5)
            if numbers(r):
                a.annotate(str(i), (r["when"], r["cost"] / 1e6), xytext=(0, 10), textcoords="offset points",
                           ha="center", fontsize=8.5, color=INK, fontweight="bold", zorder=6)
        a.grid(axis="y", color="#e8e7e2", lw=0.8)

    draw_path(ax, lambda r: z0 is None or r["when"] < z0)
    first = spells[0]
    ax.annotate(first["label"], (first["from"], recs[0]["cost"] / 1e6), xytext=(4, 8), textcoords="offset points",
                fontsize=8.5, color=MUTED)
    end_ax = inset if inset is not None else ax
    if inset is not None:
        draw_path(inset, lambda r: r["when"] >= z0)
        zr = [r for r in recs if r["when"] >= z0] + [recs[recs.index(r) - 1] for r in recs[1:] if r["when"] >= z0][:1]
        lo, hi = min(r["cost"] for r in zr) / 1e6, max(r["cost"] for r in zr) / 1e6
        inset.set_xlim(z0, t_now + (t_now - z0) * 0.04)
        inset.set_ylim(lo - 0.12 * (hi - lo), hi + 0.12 * (hi - lo))
        inset.tick_params(labelsize=7.5)
        iloc = date_locator(z0, t_now, mdates)
        inset.xaxis.set_major_locator(iloc)
        inset.xaxis.set_major_formatter(mdates.ConciseDateFormatter(iloc))
        for s_ in ("top", "right"):
            inset.spines[s_].set_visible(True)
        inset.set_title(f"from {z0:%d %b %H:%M} magnified", fontsize=8, color=MUTED, loc="left",
                        fontweight="normal")
        ind = ax.indicate_inset_zoom(inset, edgecolor=MUTED, alpha=0.6)
        for c in (ind[1] if isinstance(ind, tuple) else getattr(ind, "connectors", None)) or []:
            c.set(alpha=0.3, lw=0.6)
    end_ax.annotate(f"now: {cur['label']}, cost {recs[-1]['cost'] / 1e6:.2f}M", (recs[-1]["when"], recs[-1]["cost"] / 1e6),
                    xytext=(-8, -16), textcoords="offset points", ha="right", fontsize=8.5, color=INK)
    ax.set_title("Best public chemistry cost, coloured by the word table that held it", loc="left", pad=18)
    ax.text(0, 1.02, f"{len(recs)} records on {len(labs)} word tables; numbered markers are the switches "
            f"listed on the right", transform=ax.transAxes, fontsize=9, color=MUTED)
    ax.set_ylabel("cost, millions (lower is better)")
    ax.grid(axis="y", color="#e8e7e2", lw=0.8)
    loc = date_locator(recs[0]["when"], t_now, mdates)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    side.axis("off")
    side.set_title("Switches between word tables", loc="left", pad=18)
    y = 0.97
    for i, r in enumerate(switches, 1):
        gain = r["cost"] / r["cost_before"] - 1
        head = f"{i}.  {r['from_label']} → {r['label']}   {r['when']:%d %b %H:%M}   {gain:+.2%}"
        kind = r["kind"] + (f" ({len(r['changed_loaders'])} of {t_by_lab[r['label']]['slots']} blocks new)"
                            if r["kind"].endswith("same shape") else "")
        body = (f"{kind}; {r['author']}\ncost {r['cost_before'] / 1e6:.2f}M → {r['cost'] / 1e6:.2f}M")
        side.scatter([0.015], [y - 0.012], s=60, color=col[r["label"]], transform=side.transAxes,
                     clip_on=False)
        side.text(0.05, y, head, transform=side.transAxes, fontsize=9, color=INK, va="top",
                  fontweight="bold")
        side.text(0.05, y - 0.045, body, transform=side.transAxes, fontsize=8.2, color=MUTED, va="top",
                  linespacing=1.25)
        y -= 0.122
    fig.tight_layout()
    plt.show()

THRESHOLDS = (26, 25, 24, 23, 22.5)
RANKS = {'score_1st': 1, 'score_10th': 10, 'score_50th': 50, 'score_100th': 100}

def _tcol(t: float) -> str:
    return "teams_ge_" + str(t).replace(".", "_")


def interpolate_rank(row: dict, gm: float) -> int | None:
    """Approximate rank of a score `gm` on a snapshot from its aggregate columns only (no full leaderboard):
    piecewise-linear in the number of teams at or above a score, through the known (score, teams at or above) points
    (the ranked scores, medal lines, thresholds and the median), then 1 + that count. None if gm is below every known
    point (the rank is then only bounded)."""
    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    n = f(row.get("teams"))
    pts = []
    for col, k in RANKS.items():
        if f(row.get(col)) is not None:
            pts.append((f(row[col]), k))
    for medal in ("gold", "silver", "bronze"):
        if f(row.get(f"score_{medal}")) is not None and f(row.get(f"{medal}_rank")) is not None:
            pts.append((f(row[f"score_{medal}"]), f(row[f"{medal}_rank"])))
    for th in THRESHOLDS:
        if f(row.get(_tcol(th))) is not None:
            pts.append((float(th), f(row[_tcol(th)])))
    if f(row.get("median")) is not None and n:
        pts.append((f(row["median"]), n / 2))
    if not pts:
        return None
    pts.sort(key=lambda p: (-p[0], p[1]))
    # enforce monotone counts (a lower score never has fewer teams at or above it)
    mono, hi = [], 0.0
    for s, c in pts:
        hi = max(hi, c)
        mono.append((s, hi))
    if gm > mono[0][0]:
        return 1
    for (s_hi, c_hi), (s_lo, c_lo) in zip(mono, mono[1:]):
        if s_lo <= gm <= s_hi:
            if gm in (s_hi, s_lo):  # on a known point: it ties the teams there, ranked with them
                return max(1, round(c_hi if gm == s_hi else c_lo))
            c = c_hi + (c_lo - c_hi) * (s_hi - gm) / (s_hi - s_lo)  # teams strictly above gm, interpolated
            return max(1, round(c) + 1)
    if gm == mono[-1][0]:
        return max(1, round(mono[-1][1]))
    return None



# From leaderboard/leaderboard_summary.csv (aggregates per leaderboard snapshot) and manifest.csv: a new dataset
# version redraws it with no notebook change. Older dataset versions without the file skip the section.
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.transforms import blended_transform_factory
BLUE, INK, MUTED = "#2a78d6", "#0b0b0b", "#52514e"
lb_path = DATA / "leaderboard" / "leaderboard_summary.csv"
if not lb_path.is_file():
    print("This dataset version has no leaderboard/leaderboard_summary.csv: leaderboard comparison skipped.")
else:
    lb = pd.read_csv(lb_path)
    lb["t"] = pd.to_datetime(lb["snapshot_utc"])
    lb = lb.sort_values("t").reset_index(drop=True)
    last = lb.iloc[-1]

    # The public frontier GM over time: best single public circuit per task as each one became public
    # (released() and chart_cost() as in the frontier charts), extended to now with this run's portfolio
    def gm_at(t):
        best = {}
        for r in rows:
            w, c = released(r), chart_cost(r)
            if w is not None and c and w <= t and r["task"] in REFERENCE:
                best[r["task"]] = min(best.get(r["task"], c), c)
        return prod(REFERENCE[k] / best[k] for k in REFERENCE) ** (1 / 3) if len(best) == 3 else None
    t0, t1 = lb["t"].iloc[0].to_pydatetime(), datetime.now(timezone.utc).replace(tzinfo=None)
    times = [t0] + sorted({w for r in rows if (w := released(r)) and t0 < w <= t1})
    fx, fy = [], []
    for t in times:
        g = gm_at(t)
        if g is not None and (not fy or abs(g - fy[-1]) > 1e-9):
            fx.append(t); fy.append(g)
    if fy and gm_exp > fy[-1] * (1 + 1e-9):  # e.g. a chemistry assembly of public parts (dated by its check)
        ta = parse_when((ASSEMBLY or {}).get("verify_date")) or lb["t"].iloc[-1].to_pydatetime()
        fx.append(min(t1, max(fx[-1], ta))); fy.append(gm_exp)
    fx.append(t1); fy.append(fy[-1])

    fig, ax = plt.subplots(figsize=(11, 5.2))
    SERIES = [("score_1st", "1st place", INK, "-"), ("score_10th", "10th", "#52514e", "-"),
              ("score_100th", "100th", "#8c8a85", "-"), ("median", "median team", "#b8b7b0", "--")]
    lo = min(min(fy), lb["score_100th"].min()) - 0.6
    hi = lb["score_1st"].max() + 0.5
    for col, name, colour, ls in SERIES:
        ax.plot(lb["t"], lb[col], color=colour, lw=1.8, ls=ls, marker="o", ms=3, label=f"leaderboard {name}")
    ax.step(fx, fy, where="post", color=BLUE, lw=2.6, label="public frontier (best public portfolio)", zorder=4)
    # End labels at the right edge, spread apart so close lines (100th and the frontier) stay readable
    ends = sorted([(lb[c].iloc[-1], f"{name} {lb[c].iloc[-1]:.3f}", MUTED, "normal") for c, name, _, _ in SERIES]
                  + [(fy[-1], f"public frontier {fy[-1]:.3f}", BLUE, "bold")])
    sep, placed = 0.055 * (hi - lo), []
    for y, *_ in ends:
        placed.append(max(y, placed[-1] + sep) if placed else y)
    for (y, text, colour, weight), yl in zip(ends, placed):
        ax.text(1.01, yl, text, transform=blended_transform_factory(ax.transAxes, ax.transData),
                va="center", fontsize=8.5, color=colour, fontweight=weight, clip_on=False)
    # The current gap between 1st place and the frontier, as a bracket at the latest snapshot
    xg = lb["t"].iloc[-1]
    top, gap = last["score_1st"], last["score_1st"] / fy[-1] - 1
    ax.annotate("", (xg, top), xytext=(xg, fy[-1]),
                arrowprops=dict(arrowstyle="<->", color=MUTED, lw=1, shrinkA=2, shrinkB=2))
    ax.annotate(f"gap now: 1st is {gap:+.1%}\nabove the frontier ({top - fy[-1]:.2f})",
                (xg, (last["score_10th"] + fy[-1]) / 2), xytext=(-10, 0), textcoords="offset points", ha="right",
                va="center", fontsize=9, color=INK)
    below = lb[lb["median"] < lo]
    if len(below):
        ax.text(0.01, 0.02, f"median team below the axis until {below['t'].iloc[-1]:%d %b %H:%M}",
                transform=ax.transAxes, fontsize=8, color=MUTED)
    ax.set_ylim(lo, hi)
    ax.set_xlim(lb["t"].iloc[0] - pd.Timedelta(hours=3), pd.Timestamp(t1) + pd.Timedelta(hours=2))
    ax.set_ylabel("leaderboard score (geometric mean; higher is better)")
    ax.grid(axis="y", color="#e8e7e2", lw=0.8)
    loc = date_locator(lb["t"].iloc[0].to_pydatetime(), t1, mdates)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.legend(loc="lower right", fontsize=8.5, frameon=False)
    ax.set_title(f"The leaderboard and the public frontier ({len(lb)} snapshots, {int(last['teams']):,} teams now)",
                 loc="left")
    fig.tight_layout()
    plt.show()

    n = (int(last["frontier_rank"]) if pd.notna(last.get("frontier_rank")) and pd.notna(last.get("frontier_gm"))
         and abs(float(last["frontier_gm"]) - gm_exp) < 5e-6 else interpolate_rank(last.to_dict(), gm_exp))
    when = f"{last['t']:%d %b %Y %H:%M} UTC"
    place = f"about #{n:,}" if n else "below the median team"
    display(HTML(
        f'<p style="font-size:14px">The best public portfolio (GM {gm_exp:.5f}) would place <b>{place} of '
        f'{int(last["teams"]):,} teams</b> on the leaderboard of {when}.</p>'
        '<p style="color:#52514e">How to read it: the gap shows how far the leading teams are ahead of anything '
        'published. The leaderboard scores use the hosted DQI draw; the frontier GM is the expected cost from '
        'our local verification (DQI at its expected cost), so a difference of about 0.15% (one DQI failure more '
        'or less) is draw noise, not ground gained or lost.</p>'))
    lb.drop(columns="t").to_csv(WORK / "leaderboard_summary.csv", index=False)

# Everything here comes from manifest.csv, so a new dataset version updates the credits with no notebook change.
COLS = {"task": "task", "sha12": "circuit", "qubits": "qubits", "local_task_score": "local cost",
        "author": "published by", "notebook_title": "notebook", "source_notebook_version": "version",
        "release_date": "date", "core_author": "core design by", "core_design": "core design",
        "change_summary": "what this file changes", "source_notebook_url": "link"}

def credit_table(records):
    df = pd.DataFrame([{COLS[k]: r.get(k, "") for k in COLS} for r in records])
    df["local cost"] = pd.to_numeric(df["local cost"], errors="coerce")
    df["also used in"] = [len([u for u in (r.get("also_found_in") or "").split(";") if u]) for r in records]
    return df

print("The circuits in this submission:")
picked = credit_table([picks[t] for t in ("invsqrt", "dqi", "chemistry")])
picked["task"] = picked["task"].map(TASK_NAMES)
show(picked)
if CHEM_PARTS is not None:
    print("The chemistry entry is an assembly; its parts and their authors:")
    show(CHEM_PARTS)

ranked = sorted((r for r in rows if rank_cost(r) is not None),
                key=lambda r: (list(REFERENCE).index(r["task"]), rank_cost(r)))
everything = credit_table(ranked)
print(f"Every public circuit in the dataset ({len(everything)}), cheapest first within each task:")
show(everything, height=420)
everything.to_csv(WORK / "credits.csv", index=False)
try:
    everything.to_parquet(WORK / "credits.parquet", index=False)
except Exception as ex:  # parquet needs pyarrow or fastparquet
    print("credits.parquet not written:", ex)
print("Tooling: kickmix and the official verifiers by Google Quantum AI (their own terms); the kickmix fork, "
      "kickmix_tools and the circuits dataset by manwithacat, AI-assisted (Claude).")
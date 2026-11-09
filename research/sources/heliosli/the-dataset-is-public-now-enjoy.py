import glob, hashlib, json, lzma, math, os, shutil, sys, time, zipfile, pathlib

def find_comp():
    for p in [os.environ.get('QCC_COMP_DIR', ''), '/kaggle/input/competitions/google-verifiable-quantum-circuit-compilation',
              '/kaggle/input/google-verifiable-quantum-circuit-compilation']:
        if p and os.path.isdir(os.path.join(p, 'verifiers')):
            return p
    raise SystemExit('competition data not found')

def find_release():
    if os.environ.get('QCC_DS_DIR'):
        return os.environ['QCC_DS_DIR']
    hits = glob.glob('/kaggle/input/**/QCC_RELEASE_MANIFEST.json', recursive=True)
    if not hits:
        raise SystemExit('release dataset not found')
    return os.path.dirname(hits[0])

COMP, DS = find_comp(), find_release()
WORK = pathlib.Path(os.environ.get('QCC_WORK_DIR', '/kaggle/working'))
SUB = WORK / 'submission'
sys.path[:0] = [os.path.join(COMP, 'kickmix'), os.path.join(COMP, 'verifiers')]
import kickmix as km
from invsqrt import verify_submission_invsqrt as vinv
from dqi import verify_submission_dqi as vdqi
from dfthcsos import verify as vdft
MANIFEST = json.load(open(os.path.join(DS, 'QCC_RELEASE_MANIFEST.json')))
EXPECTED_PORTFOLIO = 'v76_sy5_ay3'
assert MANIFEST['portfolio'] == EXPECTED_PORTFOLIO, ('stale dataset version', MANIFEST['portfolio'], EXPECTED_PORTFOLIO)
SALT = MANIFEST['salt']
print('competition data:', COMP, '| release dataset:', DS, '| salt:', SALT)

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 22), b''):
            h.update(chunk)
    return h.hexdigest()

if SUB.exists():
    shutil.rmtree(SUB)
for item in MANIFEST['files']:
    src, dst = os.path.join(DS, item['stored_as']), SUB / item['file']
    dst.parent.mkdir(parents=True, exist_ok=True)
    if item['stored_as'].endswith('.xz'):
        with lzma.open(src) as fi, open(dst, 'wb') as fo:
            shutil.copyfileobj(fi, fo, 1 << 22)
    else:
        shutil.copyfile(src, dst)
    got = sha256(dst)
    assert got == item['sha256'], (item['file'], got)
    print('sha256', item['file'], got)
print(len(MANIFEST['files']), 'files copied and matched to the manifest by SHA-256')

REF = MANIFEST['reference_costs']
results = {}
t0 = time.time()
circ = km.Circuit.from_file(str(SUB / 'invsqrt.kmb'), format='kmb')
results['invsqrt'] = vinv.verify(circ, vinv.ProblemSpecInvSqrt.for_n_p(10), salt=SALT)
print('invsqrt done', round(time.time() - t0), 's'); t0 = time.time()
circ = km.Circuit.from_file(str(SUB / 'dqi.kmb'), format='kmb')
results['dqi'] = vdqi.verify(circ, vdqi.ProblemSpecDQI.benchmark_instance(), salt=SALT)
del circ
print('dqi done', round(time.time() - t0), 's'); t0 = time.time()
results['dfthcsos'] = vdft.verify(str(SUB / 'dfthcsos'), salt=SALT)
print('dfthcsos done', round(time.time() - t0), 's')
rows, ratios = [], []
for task in ('invsqrt', 'dqi', 'dfthcsos'):
    r = results[task]
    assert r['status'] == 'PASS', (task, r)
    ratio = REF[task] / float(r['score'])
    ratios.append(ratio)
    if task == 'invsqrt':
        extra = f"{r['num_qubits']} qubits, {r['toffoli_equivalent']:.1f} Toffoli-equivalents"
    elif task == 'dqi':
        extra = (f"{r['num_qubits']} qubits, {r['toffoli_count']:.0f} mean Toffolis, "
                 f"{r.get('value_errors', 0)} value / {r.get('phase_errors', 0)} phase errors")
    else:
        extra = f"lambda_eff {r['lambda_eff']:.3f}, {r['toffoli_count']:.0f} Toffolis, {r['num_qubits']} qubits"
    rows.append((task, float(r['score']), ratio, extra))
    print(f"{task:9s} PASS cost {float(r['score']):.6g}  ratio {ratio:8.4f}  ({extra})")
gm = math.prod(ratios) ** (1 / 3)
print(f'geometric mean of reference / cost: {gm:.4f}')

zip_path = WORK / 'submission.zip'
with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
    for item in MANIFEST['files']:
        z.write(SUB / item['file'], item['file'])
shutil.rmtree(SUB)
print('wrote', zip_path, round(zip_path.stat().st_size / 2**20, 1), 'MiB')
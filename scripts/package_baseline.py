"""Package unchanged official circuits and record hashes. Run from project root."""
import hashlib
import json
from pathlib import Path
import zipfile

root = Path('official/data/sample_submission')
output = Path('artifacts/submission.zip')
output.parent.mkdir(exist_ok=True)
files = sorted(p for p in root.rglob('*') if p.is_file())
expected = {'invsqrt.kmb', 'dqi.kmb', 'dfthcsos/submission.npz'} | {
    f'dfthcsos/angle_k_{i}.kmb' for i in range(10)
}
assert {p.relative_to(root).as_posix() for p in files} == expected
manifest = {}
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for path in files:
        name = path.relative_to(root).as_posix()
        with path.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        manifest[name] = {'bytes': path.stat().st_size, 'sha256': digest}
        z.write(path, name)
with zipfile.ZipFile(output) as z:
    assert z.testzip() is None
with output.open('rb') as f:
    digest = hashlib.file_digest(f, 'sha256').hexdigest()
receipt = {'archive': str(output), 'sha256': digest, 'bytes': output.stat().st_size, 'files': manifest}
Path('runs').mkdir(exist_ok=True)
Path('runs/baseline-package.json').write_text(json.dumps(receipt, indent=2))
print(json.dumps({k: v for k, v in receipt.items() if k != 'files'}, indent=2))

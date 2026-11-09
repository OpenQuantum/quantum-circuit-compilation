import os, zipfile, hashlib
base = None
for root, _, files in os.walk('/kaggle/input'):
    if 'dqi.kmb' in files and 'invsqrt.kmb' in files and 'dfthcsos__submission.npz' in files:
        base = root; break
assert base, 'dataset files not found'
out = '/kaggle/working/submission.zip'
with zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as z:
    for f in sorted(os.listdir(base)):
        if f.endswith(('.kmb', '.npz')):
            z.write(os.path.join(base, f), f.replace('__', '/'))
with zipfile.ZipFile(out) as z:
    names = z.namelist()
    for n in names:
        print(hashlib.sha256(z.read(n)).hexdigest(), n)
assert len(names) == 18 and 'dfthcsos/submission.npz' in names
print('OK', len(names), 'files')
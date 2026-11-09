"""Submit once or check the saved ref. Credentials come from Kaggle's normal config."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from kaggle.api.kaggle_api_extended import KaggleApi
from requests.exceptions import RequestException
import requests
from kaggle.models.kaggle_models_extended import ResumableUploadResult

ROOT = Path(__file__).resolve().parents[1]
COMPETITION = 'google-verifiable-quantum-circuit-compilation'
RECEIPT = ROOT / 'runs/submission-receipt.json'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['submit', 'status'])
    parser.add_argument('--archive', type=Path, default=ROOT / 'artifacts/submission.zip')
    parser.add_argument('--receipt', type=Path, default=RECEIPT)
    parser.add_argument('--message', default='Official baseline: end-to-end environment and submission validation')
    args = parser.parse_args()
    receipt = args.receipt.resolve()
    api = KaggleApi()
    api.authenticate()
    # Use GCS resumable chunks rather than one unbounded streaming PUT.
    def upload_chunks(path, url, quiet, resume=False):
        size = Path(path).stat().st_size
        chunk_size = 256 * 1024
        with open(path, 'rb') as source, requests.Session() as session:
            offset = 0
            while offset < size:
                source.seek(offset)
                data = source.read(chunk_size)
                headers = {'Content-Length': str(len(data)),
                           'Content-Range': f'bytes {offset}-{offset + len(data) - 1}/{size}'}
                if offset == 0:
                    print('Starting 256 KiB resumable upload.', flush=True)
                response = session.put(url, data=data, headers=headers, timeout=(30, 60))
                if response.status_code in (200, 201):
                    print(f'Upload complete: {size} bytes.', flush=True)
                    return ResumableUploadResult.COMPLETE
                if response.status_code != 308:
                    raise RuntimeError(f'Upload failed: HTTP {response.status_code}')
                acknowledged = response.headers.get('Range', '')
                if not acknowledged.startswith('bytes=0-'):
                    raise RuntimeError('Upload returned no acknowledged byte range.')
                next_offset = int(acknowledged.split('-')[-1]) + 1
                if next_offset <= offset:
                    raise RuntimeError('Upload made no progress.')
                offset = next_offset
                print(f'Uploaded {offset}/{size} bytes.', flush=True)
        return ResumableUploadResult.INCOMPLETE
    api.upload_complete = upload_chunks
    saved = json.loads(receipt.read_text()) if receipt.exists() else {}
    if args.action == 'status':
        ref = saved.get('submission_ref')
        if not ref:
            raise SystemExit('No saved submission ref; inspect runs/submission-receipt.json.')
        response = api.get_submission(ref)
        result = {
            'submission_ref': ref, 'status': response.status.name,
            'public_score': response.public_score,
            'error': response.error_description,
            'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        }
        status_path = receipt.with_name('submission-status.json')
        status_path.write_text(json.dumps(result, indent=2))
    else:
        if saved.get('submission_ref'):
            raise SystemExit('A submission already exists; use status. No duplicate was sent.')
        archive = args.archive.resolve()
        if archive.name != 'submission.zip':
            raise SystemExit('Competition requires the archive filename submission.zip.')
        with archive.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        # All new submissions require explicit human review, including this
        # historical helper. Quantum World has its own durable approval flow.
        if not sys.stdin.isatty():
            raise SystemExit('Human approval required. Use Quantum World operator approval or an interactive terminal.')
        expected = 'APPROVE ' + digest
        print(f'Archive: {archive}\nSHA-256: {digest}')
        if input(f'Type {expected}\n> ') != expected:
            raise SystemExit('Submission not approved; nothing uploaded.')
        try:
            response = api.competition_submit(str(archive), args.message,
                COMPETITION, quiet=True)
            result = {'status': 'created', 'submission_ref': response.ref,
                      'message': response.message, 'archive_sha256': digest}
        except RequestException as error:
            body = error.response.text if error.response is not None else str(error)
            body = re.sub(r'https?://\S+', '[URL redacted]', body)
            result = {'status': 'request_failed', 'response': re.sub(r'KGAT_[A-Za-z0-9_]+', '[REDACTED]', body)[:6000]}
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    if result.get('status') == 'request_failed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()

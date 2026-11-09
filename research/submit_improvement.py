"""Submit only if verified local and paired conservative scores beat the LIVE best."""
import hashlib,json,subprocess,sys
from pathlib import Path
from kaggle.api.kaggle_api_extended import KaggleApi

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'research/receipts/candidate-validation.json'
RECEIPT=ROOT/'research/receipts/submission-receipt.json'

def main():
    record=json.loads(REPORT.read_text())
    assert record['status']=='LOCAL_FULL_PASS'
    archive=Path(record['archive'])
    with archive.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==record['archive_sha256']
    api=KaggleApi();api.authenticate()
    submissions=api.competition_submissions('google-verifiable-quantum-circuit-compilation',page_size=100)
    if len(submissions)>=100:
        raise SystemExit('Need paginated account audit before submission; refusing incomplete best-score comparison.')
    best=max((s for s in submissions if s.public_score),key=lambda s:float(s.public_score))
    # Tasks are retained from this exact incumbent, so a changed live best needs re-evaluation.
    same_incumbent=str(best.ref)==str(record['account_best']['ref'])
    best_score=float(best.public_score)
    conservative=(best_score-0.000005)*(record['old_chemistry_cost']/record['task_costs']['dfthcsos'])**(1/3)
    allowed=same_incumbent and record['local_gm']>best_score and conservative>best_score
    gate={'live_best_ref':best.ref,'live_best_score':best_score,'local_gm':record['local_gm'],
          'conservative_paired_projection':conservative,'allowed':allowed}
    (ROOT/'research/receipts/submission-gate.json').write_text(json.dumps(gate,indent=2))
    print(json.dumps(gate,indent=2),flush=True)
    if not allowed:raise SystemExit('Not submitting: improvement gate did not pass.')
    message=(f's014: unchanged invsqrt/DQI from {best.ref}; Heliosli/Syed C44 weights + own orthogonal rotations '
             f'and exact CX cancellation; two-salt full verification; local GM {record["local_gm"]:.8f}')
    subprocess.run([sys.executable,str(ROOT/'scripts/kaggle_submission.py'),'submit','--archive',str(archive),
                    '--receipt',str(RECEIPT),'--message',message],check=True)

if __name__=='__main__':main()

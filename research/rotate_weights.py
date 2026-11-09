"""Optimize the l1 column norm by orthogonal Givens rotations of (W,H2_I).

Per group, W -> W O and h -> h O preserve W W^T, W h^T and ||h||^2.
Thus two-body tensor, one-body operator and SOS gap are unchanged in exact
arithmetic. All angles/circuit files and factorization dimensions are fixed.
"""
import argparse,json,time
from pathlib import Path
import numpy as np

def objective(a,b,theta):
    c=np.cos(theta);s=np.sin(theta)
    x=a[:,None]*c+b[:,None]*s;y=b[:,None]*c-a[:,None]*s
    return np.square(np.abs(x).sum(axis=0))+np.square(np.abs(y).sum(axis=0))

def best_rotation(a,b):
    half=np.pi/2
    boundaries=np.unique(np.r_[0.0, np.mod(np.arctan2(b,a),half), half])
    mids=(boundaries[:-1]+boundaries[1:])/2
    c=np.cos(mids);s=np.sin(mids)
    sx=np.sign(a[:,None]*c+b[:,None]*s);sy=np.sign(b[:,None]*c-a[:,None]*s)
    A=a@sx;B=b@sx;C=b@sy;D=-a@sy
    L=(A*A+C*C-B*B-D*D)/2;M=A*B+C*D
    critical=np.mod((np.arctan2(M,L)+np.pi)/2,np.pi)
    critical=critical[(critical>boundaries[:-1])&(critical<boundaries[1:])]
    angles=np.r_[boundaries,critical]
    costs=objective(a,b,angles);k=np.argmin(costs)
    return float(angles[k]),float(costs[k]),float(objective(a,b,np.array([0.0]))[0])

def main():
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('output');p.add_argument('--passes',type=int,default=2);a=p.parse_args()
    with np.load(a.source,allow_pickle=False) as z: data={k:z[k].copy() for k in z.files}
    originalW=data['W'].copy();originalh=data['H2_I'].copy();W=data['W'];h=data['H2_I'];records=[];start=time.monotonic()
    for sweep in range(a.passes):
        gain=0.;accepted=0
        for r in range(W.shape[0]):
            # Last row is the affine column coefficient appearing in lambda_h2.
            matrix=np.vstack([W[r],h[r]+W[r].sum(axis=0)])
            for i in range(W.shape[2]):
                for j in range(i+1,W.shape[2]):
                    x=matrix[:,i].copy();y=matrix[:,j].copy()
                    theta,new,old=best_rotation(x,y)
                    if old-new>1e-10:
                        c=np.cos(theta);s=np.sin(theta)
                        matrix[:,i]=x*c+y*s;matrix[:,j]=y*c-x*s
                        hi,hj=h[r,i],h[r,j];h[r,i]=hi*c+hj*s;h[r,j]=hj*c-hi*s
                        gain+=(old-new)/4;accepted+=1
            W[r]=matrix[:-1]
        record={'pass':sweep,'lambda_h2_reduction':gain,'accepted_rotations':accepted,'seconds':time.monotonic()-start}
        print(json.dumps(record),flush=True);records.append(record)
        if gain<1e-10:break
    errors={
        'W_gram_max_abs':float(np.max(np.abs(W@W.transpose(0,2,1)-originalW@originalW.transpose(0,2,1)))),
        'W_h_max_abs':float(np.max(np.abs(np.einsum('rbc,rc->rb',W,h)-np.einsum('rbc,rc->rb',originalW,originalh)))),
        'h_norm_max_abs':float(np.max(np.abs(np.sum(h*h,axis=1)-np.sum(originalh*originalh,axis=1))))}
    output=Path(a.output);output.parent.mkdir(parents=True,exist_ok=True);np.savez(output,**data)
    output.with_suffix('.rotation.json').write_text(json.dumps({'source':a.source,'passes':records,'invariant_errors':errors},indent=2))
    print(json.dumps(errors),flush=True)

if __name__=='__main__':main()

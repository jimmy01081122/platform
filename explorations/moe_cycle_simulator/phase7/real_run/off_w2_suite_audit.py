#!/usr/bin/env python3
"""Audit all frozen OFF-W2 byte-budget points."""

from __future__ import annotations

import argparse, hashlib, json, re, subprocess, time
from pathlib import Path


def sha(path):
    h=hashlib.sha256();
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser(); p.add_argument('--suite-root',type=Path,required=True); p.add_argument('--contract',type=Path,required=True); a=p.parse_args()
    c=json.loads(a.contract.read_text())
    if c['contract_state']!='FROZEN_BEFORE_EXECUTION': raise SystemExit('contract not frozen')
    actual_sources={x['path']:sha(x['path']) for x in c['source_contract']}
    if actual_sources!={x['path']:x['sha256'] for x in c['source_contract']}: raise SystemExit('source hash mismatch')
    out=[]
    for point in c['points']:
        pr=a.suite_root/'points'/point['point_id']; dirs=list((pr/'runner_runs').glob('*'))
        if len(dirs)!=1: raise SystemExit(f"runner count {point['point_id']}")
        d=dirs[0]; status=json.loads((d/'status.json').read_text()); result=json.loads((d/'result.json').read_text()); eng=json.loads((d/'requested_engine_args.json').read_text())
        traces=[json.loads(x) for x in (d/'actual_offloaded_tensors.jsonl').read_text().splitlines() if x]
        log=(d/'stdout.log').read_text(errors='replace')+'\n'+(d/'stderr.log').read_text(errors='replace')
        m=re.findall(r'Total CPU offloaded parameters:\s*([0-9.]+)(?:\s*(?:GiB|GB))?',log)
        routed=list((d/'routing').glob('*.npy'))
        traced=traces[-1]['cpu_offload_bytes_after'] if traces else 0; logged=float(m[-1]) if m else 0.0
        trace_ok=bool(traces) and all(x['cpu_offload_bytes_delta']==x['parameter_bytes_sum'] for x in traces) and sum(x['cpu_offload_bytes_delta'] for x in traces)==traced
        correct=status.get('status')=='PASS' and result.get('input_token_count')==128 and result.get('output_token_count')==32 and result.get('finish_reason')=='length' and result.get('output_token_ids')==c['frozen_reference']['expected_output_token_ids'] and len(routed)==1 and sha(routed[0])==c['frozen_reference']['off_w0_routing_array_sha256']
        config=eng.get('cpu_offload_gb')==point['cpu_offload_gb'] and eng.get('cpu_offload_params')==['experts'] and eng.get('offload_backend')=='uva'
        byte_match=abs(logged-traced/1024**3)<=0.011
        out.append({'point_id':point['point_id'],'split':point['split'],'requested_gib':point['cpu_offload_gb'],'actual_bytes':traced,'actual_gib':traced/1024**3,'runtime_log_gib':logged,'tensor_record_count':sum(len(x['parameters']) for x in traces),'module_record_count':len(traces),'trace_accounting':'PASS' if trace_ok else 'FAIL','runtime_log_byte_match':'PASS' if byte_match else 'FAIL','configuration':'PASS' if config else 'FAIL','correctness_equivalence':'PASS' if correct else 'FAIL','trace_sha256':sha(d/'actual_offloaded_tensors.jsonl')})
    monotonic=all(out[i]['actual_bytes']<out[i+1]['actual_bytes'] for i in range(len(out)-1))
    passed=monotonic and all(all(x[k]=='PASS' for k in ('trace_accounting','runtime_log_byte_match','configuration','correctness_equivalence')) for x in out)
    apps=subprocess.run(['nvidia-smi','--query-compute-apps=pid,process_name,used_memory','--format=csv,noheader,nounits'],capture_output=True,text=True).stdout.strip()
    passed=passed and not apps
    audit={'schema_version':'phase7-off-w2-suite-audit-v1','captured_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'contract_sha256':sha(a.contract),'source_hashes':actual_sources,'points':out,'strict_actual_byte_monotonicity':'PASS' if monotonic else 'FAIL','gpu_terminal_compute_apps':apps.splitlines() if apps else [],'status':'PASS' if passed else 'FAIL','claim_boundary':c['claim_boundary']}
    (a.suite_root/'off_w2_suite_audit.json').write_text(json.dumps(audit,indent=2,sort_keys=True)+'\n'); (a.suite_root/'off_w2_contract.json').write_bytes(a.contract.read_bytes())
    return 0 if passed else 2


if __name__=='__main__': raise SystemExit(main())

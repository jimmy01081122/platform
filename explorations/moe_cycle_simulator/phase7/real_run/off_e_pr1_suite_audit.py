#!/usr/bin/env python3
from __future__ import annotations
import argparse,hashlib,json,subprocess,time
from pathlib import Path
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def main():
 q=argparse.ArgumentParser();q.add_argument('--suite-root',type=Path,required=True);q.add_argument('--contract',type=Path,required=True);a=q.parse_args();c=json.loads(a.contract.read_text())
 if c['contract_state']!='FROZEN_BEFORE_EXECUTION' or {x['path']:sha(x['path']) for x in c['source_contract']}!={x['path']:x['sha256'] for x in c['source_contract']}:raise SystemExit('source gate')
 points={};outs=[];routes=[]
 for v in c['variants']:
  ds=list((a.suite_root/'points'/v['point_id']/'runner_runs').glob('*'))
  if len(ds)!=1:raise SystemExit('runner count')
  d=ds[0];st=json.loads((d/'status.json').read_text());e=json.loads((d/'requested_engine_args.json').read_text());r=json.loads((d/'result.json').read_text());arr=list((d/'routing').glob('*.npy'));ok=st.get('status')=='PASS' and r.get('input_token_count')==128 and r.get('output_token_count')==32 and r.get('finish_reason')=='length' and e.get('cpu_offload_gb')==0.0 and e.get('cpu_offload_params')==[] and len(arr)==1
  points[v['point_id']]={'runner_dir':str(d),'correctness_configuration':'PASS' if ok else 'FAIL','output_token_ids':r.get('output_token_ids'),'routing_sha256':sha(arr[0]) if len(arr)==1 else None};outs.append(r.get('output_token_ids'));routes.append(points[v['point_id']]['routing_sha256'])
 equiv=outs[0]==outs[1] and routes[0]==routes[1];rp=Path(points['REPLAY']['runner_dir'])/'off_e_pr1_trace'/'demand_load_replay.json';doc=json.loads(rp.read_text()) if rp.is_file() else {};policy_gate=True
 for name,exp in c['expected_policy_counts'].items():
  x=doc.get('policies',{}).get(name,{})
  policy_gate=policy_gate and x.get('demand_load_count')==exp['demand_load_count'] and x.get('hit_count')==exp['hit_count'] and len(x.get('evictions',[]))==exp['eviction_count'] and x.get('h2d_bytes')==exp['h2d_bytes'] and all(y.get('d2h_writeback_bytes')==0 for y in x.get('evictions',[])) and all(y.get('h2d_bytes') in (0,c['expert_catalog']['object_bytes']) for y in x.get('events',[]))
 replay_gate=rp.is_file() and doc.get('expert_object_bytes')==c['expert_catalog']['object_bytes'] and doc.get('dependency_gate')=='PASS' and doc.get('actual_expert_compute') is True and doc.get('actual_expert_compute_end_monotonic_ns',0)>doc.get('actual_expert_compute_start_monotonic_ns',0)>=doc.get('all_policy_h2d_complete_monotonic_ns',0) and doc.get('total_d2h_writeback_bytes')==0 and policy_gate
 apps=subprocess.run(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],capture_output=True,text=True).stdout.strip();passed=equiv and replay_gate and not apps and all(x['correctness_configuration']=='PASS' for x in points.values());out={'schema_version':'phase7-off-e-pr1-suite-audit-v1','captured_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'contract_sha256':sha(a.contract),'points':points,'final_output_routing_equivalence':'PASS' if equiv else 'FAIL','demand_load_replay':doc,'policy_byte_dependency_gate':'PASS' if replay_gate else 'FAIL','terminal_compute_apps':apps.splitlines() if apps else [],'status':'PASS' if passed else 'FAIL','claim_boundary':c['claim_boundary']};(a.suite_root/'off_e_pr1_suite_audit.json').write_text(json.dumps(out,indent=2,sort_keys=True)+'\n');(a.suite_root/'off_e_pr1_contract.json').write_bytes(a.contract.read_bytes());return 0 if passed else 2
if __name__=='__main__':raise SystemExit(main())

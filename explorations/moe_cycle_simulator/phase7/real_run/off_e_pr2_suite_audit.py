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
 pts={};outs=[];routes=[]
 for v in c['variants']:
  ds=list((a.suite_root/'points'/v['point_id']/'runner_runs').glob('*'))
  if len(ds)!=1:raise SystemExit('runner count')
  d=ds[0];st=json.loads((d/'status.json').read_text());e=json.loads((d/'requested_engine_args.json').read_text());r=json.loads((d/'result.json').read_text());arr=list((d/'routing').glob('*.npy'));ok=st.get('status')=='PASS' and r.get('input_token_count')==128 and r.get('output_token_count')==32 and r.get('finish_reason')=='length' and e.get('cpu_offload_gb')==0.0 and e.get('cpu_offload_params')==[] and len(arr)==1;pts[v['point_id']]={'runner_dir':str(d),'correctness_configuration':'PASS' if ok else 'FAIL','output_token_ids':r.get('output_token_ids'),'routing_sha256':sha(arr[0]) if len(arr)==1 else None};outs.append(r.get('output_token_ids'));routes.append(pts[v['point_id']]['routing_sha256'])
 equiv=outs[0]==outs[1] and routes[0]==routes[1];rp=Path(pts['REPLAY']['runner_dir'])/'off_e_pr2_trace'/'prefetch_policy_replay.json';doc=json.loads(rp.read_text()) if rp.is_file() else {};pol_gate=True
 for name,x in doc.get('measured_policies',{}).items():
  z=x.get('counts',{});events=x.get('events',[]);pol_gate=pol_gate and name in ('STATIC','CAUSAL_HISTORY','COPY_AWARE_CAUSAL') and z.get('issued',0)>0 and z.get('issued')==z.get('useful',0)+z.get('wasted',0)+z.get('late',0) and z.get('cancelled',0)+z.get('issued',0)==len(events) and x.get('h2d_bytes')==z.get('issued')*c['expert_catalog']['object_bytes'] and x.get('byte_conservation_status')=='PASS' and all(y.get('h2d_bytes')==(0 if y.get('classification')=='cancelled' else c['expert_catalog']['object_bytes']) for y in events)
 oracle=doc.get('future_oracle',{});oracle_gate=oracle.get('evidence_class')=='SIMULATED_UPPER_BOUND' and oracle.get('actual_h2d_executed') is False and oracle.get('prediction_count')==11
 replay_gate=rp.is_file() and doc.get('expert_object_bytes')==c['expert_catalog']['object_bytes'] and len(doc.get('actual_top1_demand_sequence',[]))==12 and doc.get('dependency_gate')=='PASS' and doc.get('actual_expert_compute') is True and doc.get('actual_expert_compute_start_monotonic_ns',0)>=doc.get('all_policy_h2d_complete_monotonic_ns',0) and doc.get('total_d2h_writeback_bytes')==0 and pol_gate and oracle_gate
 apps=subprocess.run(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],capture_output=True,text=True).stdout.strip();passed=equiv and replay_gate and not apps and all(x['correctness_configuration']=='PASS' for x in pts.values());out={'schema_version':'phase7-off-e-pr2-suite-audit-v1','captured_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'contract_sha256':sha(a.contract),'points':pts,'final_output_routing_equivalence':'PASS' if equiv else 'FAIL','prefetch_policy_replay':doc,'measured_policy_accounting_gate':'PASS' if pol_gate else 'FAIL','oracle_label_gate':'PASS' if oracle_gate else 'FAIL','dependency_compute_gate':'PASS' if replay_gate else 'FAIL','terminal_compute_apps':apps.splitlines() if apps else [],'status':'PASS' if passed else 'FAIL','claim_boundary':c['claim_boundary']};(a.suite_root/'off_e_pr2_suite_audit.json').write_text(json.dumps(out,indent=2,sort_keys=True)+'\n');(a.suite_root/'off_e_pr2_contract.json').write_bytes(a.contract.read_bytes());return 0 if passed else 2
if __name__=='__main__':raise SystemExit(main())

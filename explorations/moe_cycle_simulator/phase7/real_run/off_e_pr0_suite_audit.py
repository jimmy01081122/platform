#!/usr/bin/env python3
from __future__ import annotations
import argparse,hashlib,json,statistics,subprocess,time
from pathlib import Path
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def main():
 q=argparse.ArgumentParser();q.add_argument('--suite-root',type=Path,required=True);q.add_argument('--contract',type=Path,required=True);a=q.parse_args();c=json.loads(a.contract.read_text())
 if c['contract_state']!='FROZEN_BEFORE_EXECUTION' or {x['path']:sha(x['path']) for x in c['source_contract']}!={x['path']:x['sha256'] for x in c['source_contract']}:raise SystemExit('contract/source gate')
 points={};all_outputs=[];all_routing=[]
 for v in c['variants']:
  dirs=list((a.suite_root/'points'/v['point_id']/'runner_runs').glob('*'))
  if len(dirs)!=1:raise SystemExit(f"runner count {v['point_id']}")
  d=dirs[0];st=json.loads((d/'status.json').read_text());eng=json.loads((d/'requested_engine_args.json').read_text());recs=[json.loads(x) for x in (d/'requests.jsonl').read_text().splitlines() if x];rjs=sorted((d/'routing').glob('*.json'));rnps=sorted((d/'routing').glob('*.npy'))
  config=eng.get('cpu_offload_gb')==0.0 and eng.get('cpu_offload_params')==[] and eng.get('offload_backend')=='auto'
  correct=st.get('status')=='PASS' and st.get('total_completed_requests')==v['measured_count'] and len(recs)==v['measured_count'] and len(rjs)==v['measured_count'] and len(rnps)==v['measured_count'] and all(x.get('input_token_count')==128 and x.get('output_token_count')==32 and x.get('finish_reason')=='length' for x in recs)
  outputs=[x['output_token_ids'] for x in recs];routes=[sha(x) for x in rnps];all_outputs+=outputs;all_routing+=routes
  points[v['point_id']]={'runner_dir':str(d),'configuration':'PASS' if config else 'FAIL','correctness':'PASS' if correct else 'FAIL','request_wall_duration_ns':[x['wall_duration_ns'] for x in recs],'output_token_ids':outputs,'routing_sha256':routes}
 pair_equiv=len({json.dumps(x) for x in all_outputs})==1 and len(set(all_routing))==1
 clean=statistics.median(points['CLEAN']['request_wall_duration_ns']);trace=statistics.median(points['TRACE-ONLY']['request_wall_duration_ns']);delta=abs(trace-clean)/clean
 tr=Path(points['TRACE-ONLY']['runner_dir'])/'off_e_pr0_trace'/'trace_only.json';rr=Path(points['REPLAY']['runner_dir'])/'off_e_pr0_trace'/'expert_replay.json';tf=Path(points['REPLAY']['runner_dir'])/'off_e_pr0_trace'/'expert_replay_tensors.pt'
 trace_doc=json.loads(tr.read_text()) if tr.is_file() else {};replay=json.loads(rr.read_text()) if rr.is_file() else {}
 replay_gate=rr.is_file() and tf.is_file() and replay.get('tensor_file_sha256')==sha(tf) and replay.get('all_resident_control') is True and replay.get('h2d_bytes')==0 and replay.get('d2h_writeback_bytes')==0 and replay.get('immutable_eviction_count')==0 and replay.get('allclose_rtol_1e-3_atol_1e-3') is True and replay.get('dependency_lineage')==['hidden_states_ready','router_logits_ready','top2_policy_decision','expert_compute_start','expert_compute_end','completion_visible'] and replay.get('actual_compute_end_monotonic_ns',0)>replay.get('actual_compute_start_monotonic_ns',0) and replay.get('replay_compute_end_monotonic_ns',0)>replay.get('replay_compute_start_monotonic_ns',0)
 apps=subprocess.run(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],capture_output=True,text=True).stdout.strip();passed=pair_equiv and delta<=c['timing_perturbation_gate']['maximum_absolute_relative_delta'] and replay_gate and not apps and all(x['configuration']=='PASS' and x['correctness']=='PASS' for x in points.values())
 doc={'schema_version':'phase7-off-e-pr0-suite-audit-v1','captured_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'contract_sha256':sha(a.contract),'points':points,'final_output_and_routing_equivalence':'PASS' if pair_equiv else 'FAIL','clean_median_wall_duration_ns':clean,'trace_only_median_wall_duration_ns':trace,'timing_perturbation_absolute_relative_delta':delta,'timing_perturbation_gate':'PASS' if delta<=.10 else 'FAIL','trace_only_hook':trace_doc,'expert_replay':replay,'expert_replay_gate':'PASS' if replay_gate else 'FAIL','terminal_compute_apps':apps.splitlines() if apps else [],'status':'PASS' if passed else 'FAIL','claim_boundary':c['claim_boundary']};(a.suite_root/'off_e_pr0_suite_audit.json').write_text(json.dumps(doc,indent=2,sort_keys=True)+'\n');(a.suite_root/'off_e_pr0_contract.json').write_bytes(a.contract.read_bytes());return 0 if passed else 2
if __name__=='__main__':raise SystemExit(main())

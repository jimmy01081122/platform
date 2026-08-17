#!/usr/bin/env python3
from __future__ import annotations
import argparse,hashlib,json,re,subprocess,time
from pathlib import Path
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''): h.update(b)
 return h.hexdigest()
def main():
 q=argparse.ArgumentParser();q.add_argument('--suite-root',type=Path,required=True);q.add_argument('--contract',type=Path,required=True);a=q.parse_args();c=json.loads(a.contract.read_text())
 if c['contract_state']!='FROZEN_BEFORE_EXECUTION' or {x['path']:sha(x['path']) for x in c['source_contract']}!={x['path']:x['sha256'] for x in c['source_contract']}: raise SystemExit('contract/source gate')
 out=[]; results=[]
 for v in c['paired_variants']:
  dirs=list((a.suite_root/'points'/v['point_id']/'runner_runs').glob('*'))
  if len(dirs)!=1:
   raise SystemExit('runner count')
  d=dirs[0]
  st=json.loads((d/'status.json').read_text()); r=json.loads((d/'result.json').read_text()); e=json.loads((d/'requested_engine_args.json').read_text()); mem=[json.loads(x) for x in (d/'memory.jsonl').read_text().splitlines() if x]; prof=r.get('profiler') or {}; arr=list((d/'routing').glob('*.npy'))
  config=e.get('cpu_offload_gb')==v['cpu_offload_gb'] and e.get('offload_backend')==v['offload_backend'] and e.get('cpu_offload_params')==v['cpu_offload_params']
  correct=st.get('status')=='PASS' and r.get('input_token_count')==4096 and r.get('output_token_count')==128 and r.get('finish_reason')=='length' and len(arr)==1
  pg=prof.get('method')=='vllm.EngineCore worker torch.profiler' and prof.get('kernel_event_count',0)>0 and prof.get('model_kernel_event_count',0)>0 and prof.get('prefill_marker_count',0)>0 and prof.get('decode_marker_count',0)>0 and prof.get('attention_marker_count',0)>0 and prof.get('moe_marker_count',0)>0 and prof.get('correlation_event_count',0)>0
  traced=0; logg=0.0
  if v['point_id']=='UVA-HELDOUT':
   tr=[json.loads(x) for x in (d/'actual_offloaded_tensors.jsonl').read_text().splitlines() if x];traced=tr[-1]['cpu_offload_bytes_after'];log=(d/'stdout.log').read_text(errors='replace')+(d/'stderr.log').read_text(errors='replace');m=re.findall(r'Total CPU offloaded parameters:\s*([0-9.]+)',log);logg=float(m[-1]);pg=pg and abs(logg-traced/1024**3)<=.011
  rec={'point_id':v['point_id'],'configuration':'PASS' if config else 'FAIL','correctness':'PASS' if correct else 'FAIL','profiler_gate':'PASS' if pg else 'FAIL','routing_sha256':sha(arr[0]) if len(arr)==1 else None,'output_token_ids':r.get('output_token_ids'),'profiler':prof,'memory_records':mem,'actual_offloaded_bytes':traced,'runtime_log_gib':logg};out.append(rec);results.append(r)
 pair=out[0]['routing_sha256']==out[1]['routing_sha256'] and out[0]['output_token_ids']==out[1]['output_token_ids']
 apps=subprocess.run(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],capture_output=True,text=True).stdout.strip();passed=pair and not apps and all(x['configuration']=='PASS' and x['correctness']=='PASS' and x['profiler_gate']=='PASS' for x in out)
 doc={'schema_version':'phase7-off-w3-suite-audit-v1','captured_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'contract_sha256':sha(a.contract),'points':out,'pair_output_routing_equivalence':'PASS' if pair else 'FAIL','terminal_compute_apps':apps.splitlines() if apps else [],'status':'PASS' if passed else 'FAIL','claim_boundary':c['claim_boundary']};(a.suite_root/'off_w3_suite_audit.json').write_text(json.dumps(doc,indent=2,sort_keys=True)+'\n');(a.suite_root/'off_w3_contract.json').write_bytes(a.contract.read_bytes());return 0 if passed else 2
if __name__=='__main__':raise SystemExit(main())

#!/usr/bin/env python3
"""Promote validated OFF-W2 byte-budget sweep evidence."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from promote_combined_master_swap_k1_v5 import aggregate_hash,append_unique,evidence_file_hashes,legally_closed,now_utc,read_json,sha256_file,write_json

CONTRACT="c9d0bf337ba6b1a1fe5e7ff1d11a5a0b50171b8e379f8afb2e812db985ea1810"
VALUES=[1879048192,2818572288,4697620480,10334765056]

def once(records,record):
    if not any(x.get('attempt_id')==record['attempt_id'] for x in records): records.append(record)

def main():
    p=argparse.ArgumentParser(); p.add_argument('--master-root',type=Path,required=True); p.add_argument('--local-attempt-root',type=Path,required=True); p.add_argument('--remote-attempt-root',required=True); p.add_argument('--expected-aggregate-sha256',required=True); p.add_argument('--remote-suite-manifest-sha256',required=True); a=p.parse_args()
    s=a.local_attempt_root
    if sha256_file(s/'SUITE_SHA256SUMS')!=a.remote_suite_manifest_sha256: raise SystemExit('suite manifest hash mismatch')
    raw=aggregate_hash(evidence_file_hashes(s))
    if raw!=a.expected_aggregate_sha256: raise SystemExit(f'aggregate mismatch {raw}')
    audit=read_json(s/'off_w2_suite_audit.json')
    if sha256_file(s/'off_w2_contract.json')!=CONTRACT or audit.get('status')!='PASS' or audit.get('strict_actual_byte_monotonicity')!='PASS' or [x['actual_bytes'] for x in audit['points']]!=VALUES or any(x['correctness_equivalence']!='PASS' or x['trace_accounting']!='PASS' or x['runtime_log_byte_match']!='PASS' for x in audit['points']): raise SystemExit('OFF-W2 scientific gate failed')
    root=a.master_root; lp=root/'master_execution_ledger.json'; ledger=read_json(lp); prior=sha256_file(lp); rows=ledger['rows']; row=next(x for x in rows if x['master_row_id']=='OFF-W2'); tid='MR11-OFF-W2-OFF-W2-V1-MASTER-PROMOTION'
    for key,value in [('attempt_ids','OFF-W2-V1-MASTER'),('remote_raw_paths',a.remote_attempt_root),('local_raw_paths',str(s)),('source_raw_sha256',raw)]:
        if value not in row.setdefault(key,[]): row[key].append(value)
    append_unique(row.setdefault('manifest_sha256',[]),[a.remote_suite_manifest_sha256,sha256_file(s/'off_w2_suite_audit.json'),CONTRACT])
    row.update({'execution_state':'EXECUTION_COMPLETE','raw_state':'COMPLETE','backup_state':'VERIFIED','review_state':'REVIEW_WITH_LIMITATION','validation_state':'VALIDATION_PASS','adoption_state':'ADOPTED','blocker_or_failure':None,'claims_supported':append_unique(list(row.get('claims_supported',[])),['The frozen requested budget sweep 0.25/2.0/4.0-held-out/8.0 GiB produced strictly increasing actual UVA-offloaded bytes 1.75/2.625/4.375/9.625 GiB.','All actual tensor records byte-accounted against runtime logs and all four output/routing results equaled the frozen reference.']),'claims_forbidden':append_unique(list(row.get('claims_forbidden',[])),['Latency, speedup, bandwidth or queue conclusions from OFF-W2 correctness canaries.','Dynamic expert residency claims from selective UVA weight offload.']),'next_action':'Freeze and run OFF-W3 representative workload/profiler comparator with explicit UVA-access and kernel/memory evidence.','last_transition_record':tid})
    trans={'transition_id':tid,'timestamp_utc':now_utc(),'changed_rows':['OFF-W2'],'reason':'Promote frozen four-point requested/actual byte sweep including held-out point and tensor-level accounting.','prior_ledger_sha256':prior,'attempt_id':'OFF-W2-V1-MASTER','raw_file_set_sha256':raw,'actual_offloaded_bytes':VALUES,'remote_local_hashes_verified':True}; ledger.setdefault('transitions',[]).append(trans); ledger['latest_transition_id']=tid; ledger['updated_at_utc']=trans['timestamp_utc']; ledger['required_closed_count']=sum(1 for x in rows if legally_closed(x)); write_json(lp,ledger); eh=sha256_file(lp)
    invp=root/'evidence_inventory.json'; inv=read_json(invp); once(inv.setdefault('off_w2_byte_budget_attempts',[]),{'attempt_id':'OFF-W2-V1-MASTER','status':'VALIDATION_PASS','remote_raw_path':a.remote_attempt_root,'local_raw_path':str(s),'raw_file_set_sha256':raw,'actual_offloaded_bytes':VALUES}); write_json(invp,inv)
    bp=root/'local_backup_manifest.json'; b=read_json(bp); once(b.setdefault('phase7_attempt_backups',[]),{'attempt_id':'OFF-W2-V1-MASTER','remote_attempt':a.remote_attempt_root,'local_attempt':str(s),'status':'VERIFIED_RAW_VALIDATION_PASS','file_set_sha256':raw}); write_json(bp,b)
    rem=[x for x in rows if not legally_closed(x)]; cond=[x['master_row_id'] for x in rem if x.get('trigger_state')=='PENDING']; blocked=[{'id':x['master_row_id'],'reason':x['blocker_or_failure']} for x in rem if x.get('blocker_or_failure')]
    write_json(root/'master_remaining_ledger.json',{'schema_version':'phase7-combined-master-remaining-ledger-v1','master_campaign_id':ledger['master_campaign_id'],'generated_from_execution_ledger_sha256':eh,'required_total':len(rows),'required_legally_closed':len(rows)-len(rem),'required_remaining_count':len(rem),'required_remaining_ids':[x['master_row_id'] for x in rem],'blocked_rows':blocked,'conditional_pending_count':len(cond),'conditional_pending_ids':cond,'phase7_status':ledger['status']})
    qpath=root/'master_ready_queue.json'; q=read_json(qpath); q.update({'generated_from_execution_ledger_sha256':eh,'next_gpu_unit':'OFF-W3','ready_gpu_units':['OFF-W3'],'next_gate_action':'FREEZE_AND_RUN_OFF_W3_REPRESENTATIVE_PROFILER_COMPARATOR','dispatch_guards':['MR2 read-only preflight clear','no foreign serving/GPU process at dispatch','OFF-W0/W1/W2 validated and locally backed up','OFF-W3 representative workload and profiler contract frozen before dispatch','no filler workload','raw namespace independent']}); write_json(qpath,q)
    name='MR11-OFF-W2-OFF-W2-V1-MASTER-PROMOTION.json'; write_json(root/'reviews'/name,{'schema_version':'phase7-combined-master-off-w2-review-v1','reviewed_at_utc':trans['timestamp_utc'],'validation_state':'VALIDATION_PASS','actual_offloaded_bytes':VALUES,'held_out_point':'PASS','correctness_equivalence':'PASS','raw_file_set_sha256':raw,'next_ready_unit':'OFF-W3'}); write_json(root/'checkpoints'/name,{'schema_version':'phase7-combined-master-checkpoint-v1','checkpoint_id':tid,'timestamp_utc':trans['timestamp_utc'],'execution_ledger_sha256':eh,'remaining_ledger_sha256':sha256_file(root/'master_remaining_ledger.json'),'required_closed_count':len(rows)-len(rem),'required_remaining_count':len(rem),'next_ready_gpu_unit':'OFF-W3','raw_file_set_sha256':raw})
    print(json.dumps({'execution_ledger_sha256':eh,'required_closed_count':len(rows)-len(rem),'required_remaining_count':len(rem),'next_ready_gpu_unit':'OFF-W3','raw_file_set_sha256':raw},indent=2,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())

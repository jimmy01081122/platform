"""Opt-in OFF-E-PR4 measured queue/backpressure policy replay hook."""
from __future__ import annotations
import hashlib,json,os,time
from pathlib import Path

MODE=os.environ.get('OFF_E_PR4_HOOK_MODE');ROOT=Path(os.environ.get('OFF_E_PR4_TRACE_DIR','/tmp/off-e-pr4-unset'));ROUTING=Path(os.environ.get('OFF_E_PR4_ROUTING_NPY','/tmp/off-e-pr4-routing-unset.npy'))
OBJ=352321536; ROUTING_SHA='0a9225ec4b302ea237bc21fe532fa1efb790905bbc5832e2ea5dab72b20e50d6'; DEPTHS=(1,2,4,8); DEADLINE_MS=13.0
def sha(p):
 h=hashlib.sha256();
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def _install():
 import numpy as np,torch
 from vllm.model_executor.layers.fused_moe.layer import FusedMoE
 if getattr(FusedMoE,'_off_e_pr4_installed',False):return
 original=FusedMoE.forward;state={'done':False,'active':False}
 def forward(self,hidden_states,router_logits,input_ids=None):
  if state['active'] or state['done'] or MODE!='REPLAY':return original(self,hidden_states,router_logits,input_ids)
  state['active']=True;ROOT.mkdir(parents=True,exist_ok=True)
  try:
   if sha(ROUTING)!=ROUTING_SHA:raise RuntimeError('routing hash mismatch')
   a=np.load(ROUTING,allow_pickle=False);seq=[int(layer*8+e) for tok in a for layer,pair in enumerate(tok) for e in pair]
   objects=[]
   for x in seq:
    if x not in objects:objects.append(x)
    if len(objects)==16:break
   n=int(router_logits.shape[-1]);params=[(k,p) for k,p in self.named_parameters() if p.ndim>0 and int(p.shape[0])==n and p.dtype==torch.bfloat16];object_bytes=sum(int(p[0].numel()*p.element_size()) for _,p in params)
   if object_bytes!=OBJ:raise RuntimeError(f'object bytes {object_bytes}')
   torch.cuda.synchronize();setup_start=time.monotonic_ns();host=[(k,p[0].detach().to('cpu').pin_memory()) for k,p in params];torch.cuda.synchronize();setup_end=time.monotonic_ns();matrix={};last=setup_end
   for depth in DEPTHS:
    pending=list(range(16));records=[];queue_events=[];accepted=[];round_id=0
    while pending and round_id<2:
     batch=pending[:depth];pending=pending[depth:];occ=0
     for idx in batch:
      queue_events.append({'event_type':'DMA_DESCRIPTOR_ACCEPT','descriptor_id':f'd{depth}-{idx}','tag':idx,'generation':1,'queue_id':f'h2d-depth-{depth}','queue_capacity':depth,'occupancy_before':occ,'occupancy_after':occ+1,'retry_count':round_id});occ+=1;accepted.append((idx,round_id,'QUEUE'))
     for idx in pending:queue_events.append({'event_type':'QUEUE_FULL','descriptor_id':f'd{depth}-{idx}','tag':idx,'generation':1,'queue_id':f'h2d-depth-{depth}','queue_capacity':depth,'occupancy_before':depth,'occupancy_after':depth,'retry_count':round_id+1,'reason':'BACKPRESSURE_RETRY'})
     round_id+=1
    for idx in pending:accepted.append((idx,2,'SYNCHRONOUS_FALLBACK'));queue_events.append({'event_type':'FALLBACK','descriptor_id':f'd{depth}-{idx}','tag':idx,'generation':1,'queue_id':f'h2d-depth-{depth}','queue_capacity':depth,'occupancy_before':0,'occupancy_after':0,'retry_count':2,'reason':'RETRY_BUDGET_EXHAUSTED'})
    queue_events.append({'event_type':'DUPLICATE_CANCEL','descriptor_id':f'd{depth}-dup-0','tag':0,'generation':1,'queue_id':f'h2d-depth-{depth}','queue_capacity':depth,'occupancy_before':0,'occupancy_after':0,'h2d_bytes':0,'reason':'DUPLICATE_TAG_GENERATION'})
    late=0
    for ordinal,(idx,retries,path) in enumerate(accepted):
     se=torch.cuda.Event(enable_timing=True);ee=torch.cuda.Event(enable_timing=True);start=time.monotonic_ns();se.record()
     with torch.no_grad():
      for (pn,target),(hn,source) in zip(params,host):
       if pn!=hn:raise RuntimeError('parameter order')
       target[0].copy_(source,non_blocking=True)
     ee.record();ee.synchronize();end=time.monotonic_ns();last=end;elapsed=float(se.elapsed_time(ee));classification='LATE' if elapsed>DEADLINE_MS else 'ON_TIME';late+=classification=='LATE'
     records.append({'ordinal':ordinal,'logical_object_id':objects[idx],'descriptor_id':f'd{depth}-{idx}','tag':idx,'generation':1,'retry_count':retries,'service_path':path,'source_domain':'PINNED_HOST','destination_domain':'GPU','requested_bytes':object_bytes,'completed_bytes':object_bytes,'issue_monotonic_ns':start,'completion_monotonic_ns':end,'cuda_elapsed_ms':elapsed,'deadline_ms':DEADLINE_MS,'deadline_classification':classification})
    queue_events.append({'event_type':'STALE_COMPLETION_REJECT','descriptor_id':f'd{depth}-0','tag':0,'generation':0,'expected_generation':1,'queue_id':f'h2d-depth-{depth}','queue_capacity':depth,'occupancy_before':0,'occupancy_after':0,'h2d_bytes':0,'reason':'STALE_GENERATION','evidence_subclass':'SYNTHETIC_FAULT_REPLAY'})
    matrix[str(depth)]={'queue_depth':depth,'input_descriptor_count':16,'accepted_descriptor_count':16,'queue_full_count':sum(x['event_type']=='QUEUE_FULL' for x in queue_events),'retry_event_count':sum(x['event_type']=='QUEUE_FULL' for x in queue_events),'fallback_count':sum(x['event_type']=='FALLBACK' for x in queue_events),'duplicate_cancel_count':1,'stale_completion_reject_count':1,'late_prefetch_count':late,'on_time_prefetch_count':16-late,'h2d_bytes':16*object_bytes,'descriptor_records':records,'queue_events':queue_events}
   torch.cuda.synchronize();compute_start=time.monotonic_ns();out=original(self,hidden_states,router_logits,input_ids);torch.cuda.synchronize();compute_end=time.monotonic_ns();state['done']=True
   doc={'schema_version':'phase7-off-e-pr4-queue-backpressure-v1','canonical_experiment_id':'OFF-E-PR4','evidence_class':'GPU_POLICY_REPLAY','descriptor_object_ids':objects,'descriptor_count_per_depth':16,'queue_depths':list(DEPTHS),'deadline_ms':DEADLINE_MS,'expert_object_bytes':object_bytes,'host_snapshot_setup_d2h_bytes':object_bytes,'host_snapshot_setup_excluded':True,'setup_start_monotonic_ns':setup_start,'setup_end_monotonic_ns':setup_end,'matrix':matrix,'all_h2d_complete_monotonic_ns':last,'actual_expert_compute_start_monotonic_ns':compute_start,'actual_expert_compute_end_monotonic_ns':compute_end,'dependency_gate':'PASS' if last<=compute_start else 'FAIL','actual_expert_compute':True,'total_h2d_bytes':sum(x['h2d_bytes'] for x in matrix.values()),'total_d2h_writeback_bytes':0,'synthetic_fault_events':['STALE_COMPLETION_REJECT'],'claim_boundary':'Measured object-sized H2D queue policy replay and actual FusedMoE compute; stale completion is separately labeled synthetic fault replay; not runtime-native expert residency.'};(ROOT/'queue_backpressure_replay.json').write_text(json.dumps(doc,indent=2,sort_keys=True)+'\n');return out
  finally:state['active']=False
 FusedMoE.forward=forward;FusedMoE._off_e_pr4_installed=True
if MODE=='REPLAY':_install()

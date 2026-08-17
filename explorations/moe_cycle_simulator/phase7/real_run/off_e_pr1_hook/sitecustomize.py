"""Opt-in OFF-E-PR1 actual-object demand-load policy replay hook."""
from __future__ import annotations
import json,os,time
from pathlib import Path
MODE=os.environ.get('OFF_E_PR1_HOOK_MODE');ROOT=Path(os.environ.get('OFF_E_PR1_TRACE_DIR','/tmp/off-e-pr1-unset'));EXPECTED=352321536
def _install():
 import torch
 from vllm.model_executor.layers.fused_moe.layer import FusedMoE
 if getattr(FusedMoE,'_off_e_pr1_installed',False):return
 original=FusedMoE.forward;state={'done':False,'active':False,'calls':0}
 def forward(self,hidden_states,router_logits,input_ids=None):
  state['calls']+=1
  if state['active'] or state['done'] or MODE!='REPLAY':return original(self,hidden_states,router_logits,input_ids)
  state['active']=True;ROOT.mkdir(parents=True,exist_ok=True)
  try:
   num_experts=int(router_logits.shape[-1]);params=[(n,p) for n,p in self.named_parameters() if p.ndim>0 and int(p.shape[0])==num_experts and p.dtype==torch.bfloat16]
   object_bytes=sum(int(p[0].numel()*p.element_size()) for _,p in params)
   if object_bytes!=EXPECTED:raise RuntimeError(f'expert object bytes mismatch: {object_bytes}')
   top2=torch.topk(torch.softmax(router_logits.float(),dim=-1),k=2,dim=-1).indices;counts=torch.bincount(top2.reshape(-1),minlength=num_experts);experts=[int(x) for x in torch.argsort(counts,descending=True)[:3].tolist()]
   sequence=[experts[0],experts[1],experts[0],experts[2],experts[1]]
   torch.cuda.synchronize();setup_start=time.monotonic_ns();host={}
   for eid in experts:host[eid]=[(n,p[eid].detach().to('cpu').pin_memory()) for n,p in params]
   torch.cuda.synchronize();setup_end=time.monotonic_ns();policy_docs={};last_completion=setup_end
   for policy in ('LRU','FIFO'):
    cache=[];events=[];h2d=0;evictions=[]
    for index,eid in enumerate(sequence):
     before=list(cache);decision=time.monotonic_ns()
     if eid in cache:
      if policy=='LRU':cache.remove(eid);cache.append(eid)
      events.append({'sequence_index':index,'expert_id':eid,'event':'HIT','decision_monotonic_ns':decision,'cache_before':before,'cache_after':list(cache),'h2d_bytes':0})
      continue
     evicted=None
     if len(cache)>=2:evicted=cache.pop(0);evictions.append({'expert_id':evicted,'semantics':'IMMUTABLE_DISCARD','d2h_writeback_bytes':0})
     start_event=torch.cuda.Event(enable_timing=True);end_event=torch.cuda.Event(enable_timing=True);start=time.monotonic_ns();start_event.record()
     with torch.no_grad():
      for (name,target),(host_name,source) in zip(params,host[eid]):
       if name!=host_name:raise RuntimeError('parameter ordering mismatch')
       target[eid].copy_(source,non_blocking=True)
     end_event.record();end_event.synchronize();complete=time.monotonic_ns();last_completion=complete;h2d+=object_bytes;cache.append(eid)
     events.append({'sequence_index':index,'expert_id':eid,'event':'DEMAND_LOAD','decision_monotonic_ns':decision,'h2d_start_monotonic_ns':start,'h2d_complete_monotonic_ns':complete,'cuda_elapsed_ms':float(start_event.elapsed_time(end_event)),'h2d_bytes':object_bytes,'cache_before':before,'evicted_expert_id':evicted,'eviction_semantics':None if evicted is None else 'IMMUTABLE_DISCARD','d2h_writeback_bytes':0,'cache_after':list(cache)})
    policy_docs[policy]={'capacity_objects':2,'request_sequence':sequence,'events':events,'demand_load_count':sum(x['event']=='DEMAND_LOAD' for x in events),'hit_count':sum(x['event']=='HIT' for x in events),'h2d_bytes':h2d,'evictions':evictions,'terminal_cache':cache}
   torch.cuda.synchronize();compute_start=time.monotonic_ns();out=original(self,hidden_states,router_logits,input_ids);torch.cuda.synchronize();compute_end=time.monotonic_ns();state['done']=True
   doc={'schema_version':'phase7-off-e-pr1-demand-load-replay-v1','layer_name':str(getattr(self,'layer_name','UNAVAILABLE')),'call_index':state['calls'],'parameter_objects':[{'name':n,'expert_slice_shape':list(p[0].shape),'dtype':str(p.dtype),'bytes_per_expert_slice':int(p[0].numel()*p.element_size())} for n,p in params],'expert_object_bytes':object_bytes,'selected_expert_ids':experts,'selection_basis':'three highest-frequency actual top-2 router decisions','route_decision_count':int(top2.numel()),'host_snapshot_setup_d2h_bytes':len(experts)*object_bytes,'host_snapshot_setup_excluded_from_demand_path':True,'setup_start_monotonic_ns':setup_start,'setup_end_monotonic_ns':setup_end,'policies':policy_docs,'all_policy_h2d_complete_monotonic_ns':last_completion,'actual_expert_compute_start_monotonic_ns':compute_start,'actual_expert_compute_end_monotonic_ns':compute_end,'dependency_gate':'PASS' if last_completion<=compute_start else 'FAIL','actual_expert_compute':True,'immutable_objects':True,'total_d2h_writeback_bytes':0,'claim_boundary':'Compute-integrated policy replay using actual layer-expert parameter slices and actual FusedMoE compute; not runtime-native residency.'}
   (ROOT/'demand_load_replay.json').write_text(json.dumps(doc,indent=2,sort_keys=True)+'\n');return out
  finally:state['active']=False
 FusedMoE.forward=forward;FusedMoE._off_e_pr1_installed=True
if MODE=='REPLAY':_install()

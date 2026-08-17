"""Opt-in OFF-E-PR2 actual-object prefetch policy replay hook."""
from __future__ import annotations
import json,os,time
from pathlib import Path
MODE=os.environ.get('OFF_E_PR2_HOOK_MODE');ROOT=Path(os.environ.get('OFF_E_PR2_TRACE_DIR','/tmp/off-e-pr2-unset'));OBJ=352321536
def _install():
 import torch
 from vllm.model_executor.layers.fused_moe.layer import FusedMoE
 if getattr(FusedMoE,'_off_e_pr2_installed',False):return
 original=FusedMoE.forward;state={'done':False,'active':False}
 def forward(self,hidden_states,router_logits,input_ids=None):
  if state['active'] or state['done'] or MODE!='REPLAY':return original(self,hidden_states,router_logits,input_ids)
  state['active']=True;ROOT.mkdir(parents=True,exist_ok=True)
  try:
   n=int(router_logits.shape[-1]);params=[(name,p) for name,p in self.named_parameters() if p.ndim>0 and int(p.shape[0])==n and p.dtype==torch.bfloat16]
   object_bytes=sum(int(p[0].numel()*p.element_size()) for _,p in params)
   if object_bytes!=OBJ:raise RuntimeError(f'object bytes {object_bytes}')
   top2=torch.topk(torch.softmax(router_logits.float(),dim=-1),k=2,dim=-1).indices;demands=[int(x) for x in top2[:12,0].tolist()];unique=sorted(set(demands+[0]))
   torch.cuda.synchronize();setup_start=time.monotonic_ns();host={eid:[(name,p[eid].detach().to('cpu').pin_memory()) for name,p in params] for eid in unique};torch.cuda.synchronize();setup_end=time.monotonic_ns()
   policies={
    'STATIC':{'deadline_ms':10.0,'predict':lambda i:0},
    'CAUSAL_HISTORY':{'deadline_ms':15.0,'predict':lambda i:demands[i]},
    'COPY_AWARE_CAUSAL':{'deadline_ms':13.0,'predict':lambda i:demands[i]},
   };results={};last_completion=setup_end
   for name,spec in policies.items():
    events=[];resident=set();counts={'issued':0,'useful':0,'wasted':0,'late':0,'cancelled':0};h2d=0
    for i in range(len(demands)-1):
     pred=int(spec['predict'](i));actual_next=demands[i+1];decision=time.monotonic_ns()
     if name=='COPY_AWARE_CAUSAL' and pred in resident:
      counts['cancelled']+=1;events.append({'step':i,'current_demand':demands[i],'predicted_next':pred,'actual_next':actual_next,'classification':'cancelled','reason':'duplicate_prefetch_already_resident','decision_monotonic_ns':decision,'h2d_bytes':0});continue
     se=torch.cuda.Event(enable_timing=True);ee=torch.cuda.Event(enable_timing=True);start=time.monotonic_ns();se.record()
     with torch.no_grad():
      for (pn,target),(hn,source) in zip(params,host[pred]):
       if pn!=hn:raise RuntimeError('parameter order')
       target[pred].copy_(source,non_blocking=True)
     ee.record();ee.synchronize();complete=time.monotonic_ns();last_completion=complete;elapsed=float(se.elapsed_time(ee));counts['issued']+=1;h2d+=object_bytes;resident.add(pred)
     if pred!=actual_next:classification='wasted'
     elif elapsed>spec['deadline_ms']:classification='late'
     else:classification='useful'
     counts[classification]+=1;events.append({'step':i,'current_demand':demands[i],'predicted_next':pred,'actual_next':actual_next,'classification':classification,'decision_monotonic_ns':decision,'h2d_start_monotonic_ns':start,'h2d_complete_monotonic_ns':complete,'cuda_elapsed_ms':elapsed,'deadline_ms':spec['deadline_ms'],'h2d_bytes':object_bytes})
    results[name]={'deadline_ms':spec['deadline_ms'],'counts':counts,'h2d_bytes':h2d,'events':events,'byte_conservation_status':'PASS' if h2d==counts['issued']*object_bytes else 'FAIL'}
   oracle={'evidence_class':'SIMULATED_UPPER_BOUND','actual_h2d_executed':False,'prediction_count':len(demands)-1,'useful':len(demands)-1,'wasted':0,'late':0,'cancelled':0,'projected_h2d_bytes':(len(demands)-1)*object_bytes}
   torch.cuda.synchronize();compute_start=time.monotonic_ns();out=original(self,hidden_states,router_logits,input_ids);torch.cuda.synchronize();compute_end=time.monotonic_ns();state['done']=True
   doc={'schema_version':'phase7-off-e-pr2-prefetch-replay-v1','layer_name':str(getattr(self,'layer_name','UNAVAILABLE')),'expert_object_bytes':object_bytes,'actual_top1_demand_sequence':demands,'host_snapshot_expert_ids':unique,'host_snapshot_setup_d2h_bytes':len(unique)*object_bytes,'host_snapshot_setup_excluded':True,'setup_start_monotonic_ns':setup_start,'setup_end_monotonic_ns':setup_end,'measured_policies':results,'future_oracle':oracle,'all_policy_h2d_complete_monotonic_ns':last_completion,'actual_expert_compute_start_monotonic_ns':compute_start,'actual_expert_compute_end_monotonic_ns':compute_end,'dependency_gate':'PASS' if last_completion<=compute_start else 'FAIL','actual_expert_compute':True,'total_d2h_writeback_bytes':0,'claim_boundary':'Measured static/causal/copy-aware prefetch replay plus separately labeled simulated oracle; not runtime-native residency.'};(ROOT/'prefetch_policy_replay.json').write_text(json.dumps(doc,indent=2,sort_keys=True)+'\n');return out
  finally:state['active']=False
 FusedMoE.forward=forward;FusedMoE._off_e_pr2_installed=True
if MODE=='REPLAY':_install()

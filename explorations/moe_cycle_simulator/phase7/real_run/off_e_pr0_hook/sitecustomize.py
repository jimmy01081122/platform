"""Opt-in worker hook for OFF-E-PR0 all-resident expert-compute replay."""
from __future__ import annotations
import hashlib, json, os, time
from pathlib import Path

MODE=os.environ.get("OFF_E_PR0_HOOK_MODE")
ROOT=Path(os.environ.get("OFF_E_PR0_TRACE_DIR","/tmp/off-e-pr0-unset"))

def _sha(path:Path)->str:
 h=hashlib.sha256()
 with path.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()

def _install():
 import torch
 from vllm.model_executor.layers.fused_moe.layer import FusedMoE
 if getattr(FusedMoE,'_off_e_pr0_installed',False):return
 original=FusedMoE.forward; state={'captured':False,'active':False,'calls':0}
 def forward(self,hidden_states,router_logits,input_ids=None):
  state['calls']+=1; call=state['calls']
  if state['active'] or state['captured']:
   return original(self,hidden_states,router_logits,input_ids)
  ROOT.mkdir(parents=True,exist_ok=True)
  if MODE=='TRACE_ONLY':
   start=time.monotonic_ns(); out=original(self,hidden_states,router_logits,input_ids); end=time.monotonic_ns();state['captured']=True
   (ROOT/'trace_only.json').write_text(json.dumps({'schema_version':'phase7-off-e-pr0-trace-only-v1','call_index':call,'hidden_shape':list(hidden_states.shape),'router_shape':list(router_logits.shape),'output_shape':list(out.shape),'dtype':str(hidden_states.dtype),'wrapper_start_monotonic_ns':start,'wrapper_return_monotonic_ns':end},indent=2,sort_keys=True)+'\n')
   return out
  if MODE!='REPLAY':return original(self,hidden_states,router_logits,input_ids)
  state['active']=True
  try:
   hidden=hidden_states.detach().clone();router=router_logits.detach().clone();ids=None if input_ids is None else input_ids.detach().clone()
   torch.cuda.synchronize(); actual_start=time.monotonic_ns(); out=original(self,hidden_states,router_logits,input_ids);torch.cuda.synchronize();actual_end=time.monotonic_ns()
   torch.cuda.synchronize(); replay_start=time.monotonic_ns(); replay=original(self,hidden,router,ids);torch.cuda.synchronize();replay_end=time.monotonic_ns()
   actual_cpu=out.detach().cpu();replay_cpu=replay.detach().cpu();hidden_cpu=hidden.cpu();router_cpu=router.cpu();topk=torch.topk(torch.softmax(router.float(),dim=-1),k=2,dim=-1).indices.cpu()
   tensor_path=ROOT/'expert_replay_tensors.pt';torch.save({'hidden_states':hidden_cpu,'router_logits':router_cpu,'top2_expert_ids':topk,'actual_output':actual_cpu,'replay_output':replay_cpu},tensor_path)
   diff=(actual_cpu.float()-replay_cpu.float()).abs();exact=torch.equal(actual_cpu,replay_cpu);close=torch.allclose(actual_cpu,replay_cpu,rtol=1e-3,atol=1e-3)
   rec={'schema_version':'phase7-off-e-pr0-actual-compute-replay-v1','call_index':call,'module_type':f'{type(self).__module__}.{type(self).__qualname__}','hidden_shape':list(hidden.shape),'router_shape':list(router.shape),'output_shape':list(out.shape),'hidden_dtype':str(hidden.dtype),'router_dtype':str(router.dtype),'output_dtype':str(out.dtype),'route_decision_top_k':2,'route_decision_count':int(topk.numel()),'dependency_lineage':['hidden_states_ready','router_logits_ready','top2_policy_decision','expert_compute_start','expert_compute_end','completion_visible'],'actual_compute_start_monotonic_ns':actual_start,'actual_compute_end_monotonic_ns':actual_end,'replay_compute_start_monotonic_ns':replay_start,'replay_compute_end_monotonic_ns':replay_end,'exact_equal':exact,'allclose_rtol_1e-3_atol_1e-3':close,'max_abs_error':float(diff.max().item()),'mean_abs_error':float(diff.mean().item()),'tensor_file':tensor_path.name,'tensor_file_sha256':_sha(tensor_path),'all_resident_control':True,'h2d_bytes':0,'d2h_writeback_bytes':0,'immutable_eviction_count':0}
   (ROOT/'expert_replay.json').write_text(json.dumps(rec,indent=2,sort_keys=True)+'\n');state['captured']=True;return out
  finally:state['active']=False
 FusedMoE.forward=forward;FusedMoE._off_e_pr0_installed=True

if MODE in {'TRACE_ONLY','REPLAY'}:_install()

#include <nvrtc.h>

__global__ void track_gpu_header_probe_kernel() {}

void track_gpu_header_probe_launch() {
  track_gpu_header_probe_kernel<<<1, 1>>>();
}

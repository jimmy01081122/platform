#include <nvrtc.h>

#include <cstdio>

int main() {
  int major = 0;
  int minor = 0;
  const nvrtcResult status = nvrtcVersion(&major, &minor);
  if (status != NVRTC_SUCCESS) {
    return 1;
  }
  std::printf("NVRTC %d.%d\n", major, minor);
  return 0;
}

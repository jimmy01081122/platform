#define _POSIX_C_SOURCE 199309L
/* Driver: replay a demands file through the shared scheduler kernel and measure
 * decision cost. Native build reports wall time + host cycles; RV64 build reports
 * retired-instruction count via the `instret` CSR (emulated by the ISA sim).
 *
 * Output is a single JSON line to stdout so both builds are machine-comparable.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "scheduler.h"

static int g_experts[MAX_DEMANDS];
static int g_offset[MAX_STEPS];
static int g_count[MAX_STEPS];

#if defined(__riscv)
static unsigned long read_instret(void) {
    unsigned long v;
    __asm__ volatile ("rdinstret %0" : "=r"(v));
    return v;
}
#endif

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <demands_file> <capacity> <depth> [reps]\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];
    int capacity = atoi(argv[2]);
    int depth = atoi(argv[3]);
    int reps = (argc > 4) ? atoi(argv[4]) : 1;

    FILE *f = fopen(path, "r");
    if (!f) { fprintf(stderr, "cannot open %s\n", path); return 2; }
    int num_experts = 0, num_steps = 0;
    if (fscanf(f, "%d %d", &num_experts, &num_steps) != 2) { fprintf(stderr, "bad header\n"); return 2; }
    int di = 0;
    for (int s = 0; s < num_steps; s++) {
        int cnt = 0;
        if (fscanf(f, "%d", &cnt) != 1) { fprintf(stderr, "bad count at %d\n", s); return 2; }
        g_offset[s] = di; g_count[s] = cnt;
        for (int j = 0; j < cnt; j++) {
            if (fscanf(f, "%d", &g_experts[di]) != 1) { fprintf(stderr, "bad expert\n"); return 2; }
            di++;
        }
    }
    fclose(f);

    Demands d = { num_experts, num_steps, g_experts, g_offset, g_count };
    SchedCounters c;

    /* warmup */
    sched_run(&d, capacity, depth, &c);

#if defined(__riscv)
    unsigned long i0 = read_instret();
    for (int r = 0; r < reps; r++) sched_run(&d, capacity, depth, &c);
    unsigned long i1 = read_instret();
    unsigned long instr = (i1 - i0);
    double per_step = num_steps ? (double)instr / (double)reps / (double)num_steps : 0.0;
    printf("{\"target\":\"rv64\",\"capacity\":%d,\"depth\":%d,\"reps\":%d,"
           "\"num_steps\":%d,\"instructions_total\":%lu,\"instructions_per_step\":%.4f,"
           "\"demand_misses\":%ld,\"prefetch_hits\":%ld,\"transfers\":%ld,"
           "\"evictions\":%ld,\"wasted_prefetches\":%ld,\"total_demands\":%ld}\n",
           capacity, depth, reps, num_steps, instr, per_step,
           c.demand_misses, c.prefetch_hits, c.transfers, c.evictions,
           c.wasted_prefetches, c.total_demands);
#else
    double t0 = now_s();
    for (int r = 0; r < reps; r++) sched_run(&d, capacity, depth, &c);
    double t1 = now_s();
    double total_s = t1 - t0;
    double per_step_ns = num_steps ? total_s / reps / num_steps * 1e9 : 0.0;
    printf("{\"target\":\"native\",\"capacity\":%d,\"depth\":%d,\"reps\":%d,"
           "\"num_steps\":%d,\"total_s\":%.9f,\"ns_per_step\":%.4f,"
           "\"demand_misses\":%ld,\"prefetch_hits\":%ld,\"transfers\":%ld,"
           "\"evictions\":%ld,\"wasted_prefetches\":%ld,\"total_demands\":%ld}\n",
           capacity, depth, reps, num_steps, total_s, per_step_ns,
           c.demand_misses, c.prefetch_hits, c.transfers, c.evictions,
           c.wasted_prefetches, c.total_demands);
#endif
    return 0;
}

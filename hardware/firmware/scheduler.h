/* Shared scheduler kernel interface (semantic_revision = 1). */
#ifndef EDGEFLOW_SCHEDULER_H
#define EDGEFLOW_SCHEDULER_H

#ifdef __cplusplus
extern "C" {
#endif

/* Residency-array sizing for the golden kernel. Must be >= the largest configured
 * num_experts (real large-MoE models have 128/256/384 experts). */
#define MAX_EXPERTS 1024
#define MAX_STEPS   200000
#define MAX_DEMANDS 4000000

typedef struct {
    int num_experts;
    int num_steps;
    int *experts;          /* flattened expert ids for all steps */
    int *offset;           /* offset[s] = start index of step s in experts[] */
    int *count;            /* count[s] = number of experts at step s */
} Demands;

typedef struct {
    long demand_misses;
    long prefetch_hits;
    long transfers;
    long evictions;
    long wasted_prefetches;
    long total_demands;
} SchedCounters;

void sched_run(const Demands *d, int capacity, int depth, SchedCounters *c);

#ifdef __cplusplus
}
#endif

#endif

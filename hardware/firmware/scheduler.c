/* Shared frozen scheduler kernel (semantic_revision = 1).
 *
 * Implements the SAME residency/prefetch decision as edgeflow.residency.simulate
 * (policy = prefetch, depth d, protective LRU). Portable C used for BOTH the
 * host-software build (native) and the firmware build (RV64 cross-compile), so
 * software and firmware share identical algorithm semantics (cross-layer
 * equivalence). This kernel performs the DECISION work only (no data movement).
 *
 * Residency set R is stored as an array of expert ids in recency order:
 * index 0 = least-recently-used, tail = most-recently-used.
 */
#include "scheduler.h"

static int find_idx(const int *R, int n, int e) {
    for (int i = 0; i < n; i++) if (R[i] == e) return i;
    return -1;
}

static void remove_at(int *R, unsigned char *pref, int *n, int idx) {
    for (int i = idx; i < *n - 1; i++) { R[i] = R[i+1]; pref[i] = pref[i+1]; }
    (*n)--;
}

/* Evict first (LRU-order) resident expert not in protect[]. Returns evicted id or -1. */
static int evict_one(int *R, unsigned char *pref, int *n,
                     const int *protect, int np, SchedCounters *c) {
    for (int i = 0; i < *n; i++) {
        int v = R[i], prot = 0;
        for (int k = 0; k < np; k++) if (protect[k] == v) { prot = 1; break; }
        if (prot) continue;
        if (pref[i]) c->wasted_prefetches++;   /* prefetched but never used */
        remove_at(R, pref, n, i);
        c->evictions++;
        return v;
    }
    return -1;
}

/* Insert e into R (as prefetched or demand). Returns 1 on success. */
static int insert(int *R, unsigned char *pref, int *n, int cap, int e,
                  int is_pref, const int *protect, int np, SchedCounters *c) {
    while (*n >= cap && find_idx(R, *n, e) < 0) {
        if (evict_one(R, pref, n, protect, np, c) < 0) return 0; /* nothing evictable */
    }
    c->transfers++;
    R[*n] = e; pref[*n] = (unsigned char)is_pref; (*n)++;
    return 1;
}

/* mark expert e as used: move to MRU and clear prefetch-pending flag */
static void use_expert(int *R, unsigned char *pref, int n, int e, SchedCounters *c) {
    int idx = find_idx(R, n, e);
    if (idx < 0) return;
    if (pref[idx]) { c->prefetch_hits++; pref[idx] = 0; }
    /* move to tail */
    int p = pref[idx];
    remove_at(R, pref, &n, idx);
    R[n] = e; pref[n] = (unsigned char)p;
}

void sched_run(const Demands *d, int capacity, int depth, SchedCounters *c) {
    int R[MAX_EXPERTS + 1];
    unsigned char pref[MAX_EXPERTS + 1];
    int n = 0;
    c->demand_misses = c->prefetch_hits = c->transfers = 0;
    c->evictions = c->wasted_prefetches = c->total_demands = 0;

    for (int s = 0; s < d->num_steps; s++) {
        const int *needed = d->experts + d->offset[s];
        int nn = d->count[s];
        /* demand phase */
        for (int j = 0; j < nn; j++) {
            int e = needed[j];
            int idx = find_idx(R, n, e);
            if (idx >= 0) {
                use_expert(R, pref, n, e, c);
            } else {
                c->demand_misses++;
                int prot[1] = { e };
                insert(R, pref, &n, capacity, e, 0, prot, 1, c);
            }
            c->total_demands++;
        }
        /* prefetch phase: look ahead `depth` steps, nearest first, dedup */
        if (depth > 0) {
            int fut[MAX_EXPERTS]; int nf = 0;
            for (int a = 1; a <= depth && s + a < d->num_steps; a++) {
                const int *fn = d->experts + d->offset[s + a];
                int fc = d->count[s + a];
                for (int j = 0; j < fc; j++) {
                    int e = fn[j], seen = 0;
                    for (int k = 0; k < nf; k++) if (fut[k] == e) { seen = 1; break; }
                    if (!seen && nf < MAX_EXPERTS) fut[nf++] = e;
                }
            }
            for (int j = 0; j < nf; j++) {
                int e = fut[j];
                if (find_idx(R, n, e) >= 0) continue;
                if (n >= capacity) {
                    int all_in_fut = 1;
                    for (int i = 0; i < n; i++) {
                        int inf = 0;
                        for (int k = 0; k < nf; k++) if (fut[k] == R[i]) { inf = 1; break; }
                        if (!inf) { all_in_fut = 0; break; }
                    }
                    if (all_in_fut) break;
                }
                insert(R, pref, &n, capacity, e, 1, fut, nf, c);
            }
        }
    }
}

# Ramulator 2.1 (CMU-SAFARI) cycle-level DRAM simulator, standalone Python-driven.
# Used to CALIBRATE the P-I effective-bandwidth/latency knobs with real DRAM timing
# (row-buffer hits/misses, tRCD/tRP/tRAS, refresh, bank-level parallelism) for the
# actual expert-weight streaming access pattern, then re-check the W3 break-even.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential g++ cmake git ca-certificates \
      python3 python3-pip python3-venv python3-dev \
    && rm -rf /var/lib/apt/lists/*

# pinned commit for reproducibility (recorded in PROVENANCE)
ARG RAMU_REF=main
RUN git clone https://github.com/CMU-SAFARI/ramulator2.git /opt/ramulator2 \
    && cd /opt/ramulator2 && git checkout ${RAMU_REF} \
    && git rev-parse HEAD > /opt/ramulator2/BUILT_COMMIT

# venv (Ubuntu 24.04 is PEP-668 externally-managed)
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir "setuptools>=64" wheel

WORKDIR /opt/ramulator2
RUN mkdir -p build && cd build && cmake .. && make -j"$(nproc)"
RUN pip install --no-cache-dir -e . || true

ENV PYTHONPATH=/opt/ramulator2/python
WORKDIR /work

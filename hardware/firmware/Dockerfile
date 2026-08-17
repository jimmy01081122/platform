# Dedicated firmware toolchain image for the edge_hetero_exploration project.
# NOT shared with any other project. Provides native gcc, a RV64 cross compiler,
# and qemu-user to execute the RV64 binary and read the instret CSR.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc-riscv64-linux-gnu \
        qemu-user \
        libc6-dev-riscv64-cross \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
CMD ["/bin/bash"]

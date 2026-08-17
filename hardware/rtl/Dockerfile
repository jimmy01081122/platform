# Dedicated RTL toolchain image for the edge_hetero_exploration project.
# NOT shared with other projects. Verilator for lint + cycle-accurate simulation.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        verilator \
        build-essential \
        perl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
CMD ["/bin/bash"]

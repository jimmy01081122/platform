# Dedicated synthesis toolchain image for the edge_hetero_exploration project.
# NOT shared with other projects. Yosys for logic synthesis + sv2v to lower
# SystemVerilog (packages/enums/typedefs) to Verilog-2005 for the Yosys frontend.
FROM debian:bookworm-slim

ARG SV2V_VERSION=v0.0.12

RUN apt-get update && apt-get install -y --no-install-recommends \
        yosys \
        wget \
        unzip \
        ca-certificates \
        libgmp10 \
        python3 \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q "https://github.com/zachjs/sv2v/releases/download/${SV2V_VERSION}/sv2v-Linux.zip" -O /tmp/sv2v.zip \
    && unzip -q /tmp/sv2v.zip -d /tmp \
    && cp /tmp/sv2v-Linux/sv2v /usr/local/bin/sv2v \
    && chmod +x /usr/local/bin/sv2v \
    && rm -rf /tmp/sv2v.zip /tmp/sv2v-Linux

WORKDIR /work
CMD ["/bin/bash"]

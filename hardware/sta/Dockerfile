# OpenSTA sign-off-lite timing (real STA engine + wireload model), plus sv2v/yosys
# for generating the mapped netlist. Used by S6++++ to add wire-load-aware Fmax and
# a power estimate on top of the S6+ cell-only STA.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates \
      tcl-dev tcl swig bison flex libfl-dev libeigen3-dev zlib1g-dev \
      libtcl8.6 wget automake autoconf libtool \
    && rm -rf /var/lib/apt/lists/*

# CUDD (BDD package) required by recent OpenSTA.
RUN git clone --depth 1 https://github.com/The-OpenROAD-Project/cudd.git /opt/cudd \
    && cd /opt/cudd && autoreconf -i && ./configure --prefix=/opt/cudd/install \
    && make -j"$(nproc)" && make install

# OpenSTA (parallaxsw).
RUN git clone --depth 1 https://github.com/parallaxsw/OpenSTA.git /opt/OpenSTA \
    && cd /opt/OpenSTA && mkdir build && cd build \
    && cmake .. -DCMAKE_BUILD_TYPE=Release -DCUDD_DIR=/opt/cudd/install \
    && make -j"$(nproc)" \
    && (cp app/sta /usr/local/bin/sta 2>/dev/null || cp sta /usr/local/bin/sta) \
    && sta -version

WORKDIR /work
CMD ["bash"]

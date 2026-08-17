FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface \
    HF_HUB_CACHE=/cache/huggingface/hub \
    HF_DATASETS_CACHE=/cache/huggingface/datasets \
    GPU_PERSIST_ROOT=/persistent
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /work/gpu_run_package
COPY requirements.lock .
# Installs only the locked Python runtime. No model or dataset download occurs
# during image build; packaged snapshots remain local-files-only at runtime.
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir -r requirements.lock \
    && python3 -m pip check
COPY . .
RUN chmod +x run.sh preflight.sh collect_environment.sh scripts/*.py
# Mount /cache/huggingface to persist Hugging Face caches and /persistent for
# capability reports, benchmark output, sessions, and result archives.
VOLUME ["/cache/huggingface", "/persistent"]
ENTRYPOINT ["./run.sh"]

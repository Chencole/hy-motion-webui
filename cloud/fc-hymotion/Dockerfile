# Build from this directory only. This image contains inference code, never credentials or model weights.
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime@sha256:c8268a92a69bd500f8be0e665b2630ee006dadaf7bfbc24249141b15ff622755
ARG OFFICIAL_COMMIT=4e426f5a1021cbcf7f375458c37b840ee7225229
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 USE_HF_MODELS=0 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 HF_HUB_DISABLE_TELEMETRY=1 DISABLE_PROMPT_ENGINEERING=True \
    HYMOTION_OFFICIAL_SOURCE=/opt/hymotion HYMOTION_ASSETS_MOUNT=/mnt/hymotion-assets PORT=9000
RUN apt-get update && apt-get install -y --no-install-recommends \
      git git-lfs ca-certificates libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*
RUN GIT_LFS_SKIP_SMUDGE=1 git clone https://github.com/Tencent-Hunyuan/HY-Motion-1.0.git /opt/hymotion \
    && cd /opt/hymotion && git checkout --detach ${OFFICIAL_COMMIT} \
    && test "$(git rev-parse HEAD)" = "${OFFICIAL_COMMIT}" \
    && git lfs pull --include="stats/**,assets/**,scripts/gradio/static/**" --exclude="" \
    && rm -rf ckpts && ln -s /mnt/hymotion-assets/ckpts ckpts
# The base's ninja 1.11.1.1 wheel has incompatible platform metadata; use the published Linux wheel.
RUN python -m pip install --no-cache-dir -r /opt/hymotion/requirements.txt \
    && python -m pip install --no-cache-dir oss2==2.19.1 ninja==1.11.1.4 \
    && python -m pip check
WORKDIR /app
COPY contract.py storage.py service.py worker.py /app/
# No model import, warmup, download or CUDA initialization runs at service startup.
EXPOSE 9000
CMD ["python", "-B", "-u", "/app/service.py"]

# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.10-slim-bookworm

FROM ${PYTHON_IMAGE} AS python-deps

ARG DEBIAN_FRONTEND=noninteractive
ARG UNICORE_REPO=https://github.com/dptech-corp/Uni-Core.git
ARG UNICORE_REF=ace6fae1c8479a9751f2bb1e1d6e4047427bc134
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/nmrpeak-venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}" \
    && python -m pip install --upgrade pip setuptools wheel

RUN python -m pip install \
      torch==2.3.0 \
      torchvision==0.18.0 \
      torchaudio==2.3.0 \
      --index-url "${TORCH_INDEX_URL}"

COPY nmrpeak-upstream/requirements.txt /tmp/nmrpeak-requirements.txt

# The PyPI package named "apex" is not NVIDIA Apex, and NMRPeak does not import
# apex. Keep it out of this reproducible inference/smoke-test image.
RUN sed '/^apex==/d' /tmp/nmrpeak-requirements.txt > /tmp/runtime-requirements.txt \
    && python -m pip install --requirement /tmp/runtime-requirements.txt

# NMRPeak constructs its model with BartConfig.from_pretrained("facebook/bart-base").
# Cache that small public config at build time so checkpoint execution stays offline.
RUN HF_HOME=/opt/huggingface \
    python -c 'from transformers import BartConfig; BartConfig.from_pretrained("facebook/bart-base")'

# Pin Uni-Core instead of building whatever happens to be at the branch tip.
# Its current setup disables optional CUDA extensions by default.
RUN git clone --filter=blob:none "${UNICORE_REPO}" /tmp/Uni-Core \
    && git -C /tmp/Uni-Core checkout --detach "${UNICORE_REF}" \
    && python -m pip install --no-build-isolation --no-deps /tmp/Uni-Core

FROM ${PYTHON_IMAGE} AS runtime

ARG DEBIAN_FRONTEND=noninteractive

ENV PATH="/opt/nmrpeak-venv/bin:${PATH}" \
    PYTHONPATH=/opt/nmrpeak \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    HF_HOME=/opt/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TORCH_HOME=/tmp/torch \
    WANDB_MODE=offline \
    WANDB_DISABLED=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libgl1 \
      libglib2.0-0 \
      libgomp1 \
      libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-deps /opt/nmrpeak-venv /opt/nmrpeak-venv
COPY --from=python-deps /opt/huggingface /opt/huggingface
COPY --chown=65532:65532 nmrpeak-upstream/LICENSE /opt/nmrpeak/LICENSE
COPY --chown=65532:65532 nmrpeak-upstream/requirements.txt /opt/nmrpeak/requirements.txt
COPY --chown=65532:65532 nmrpeak-upstream/dict /opt/nmrpeak/dict
COPY --chown=65532:65532 nmrpeak-upstream/nmrpeak /opt/nmrpeak/nmrpeak
COPY families/nmrpeak/source-closure.paths /tmp/source-closure.paths
COPY families/nmrpeak/source-closure.sha256 /tmp/source-closure.sha256
COPY repository_checks/nmrpeak_source.py /tmp/nmrpeak_source.py
RUN python /tmp/nmrpeak_source.py \
      --materialized /opt/nmrpeak \
      --declaration /tmp/source-closure.paths \
      --manifest /tmp/source-closure.sha256 \
    && rm /tmp/nmrpeak_source.py \
          /tmp/source-closure.paths \
          /tmp/source-closure.sha256
COPY --chown=65532:65532 docker/safe_extract.py /opt/nmrpeak-tools/safe_extract.py
COPY --chown=65532:65532 docker/smoke_test.py /opt/nmrpeak-tools/smoke_test.py
COPY --chown=65532:65532 docker/infer_hf_example.py /opt/nmrpeak-tools/infer_hf_example.py
COPY --chown=65532:65532 docker/infer_chf_example.py /opt/nmrpeak-tools/infer_chf_example.py

WORKDIR /opt/nmrpeak
USER 65532:65532

CMD ["python", "/opt/nmrpeak-tools/smoke_test.py"]

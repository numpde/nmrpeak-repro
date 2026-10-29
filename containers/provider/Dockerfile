# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487

RUN groupadd --gid 65532 nmrpeak \
    && useradd --uid 65532 --gid 65532 --home-dir /nonexistent \
        --shell /usr/sbin/nologin --no-create-home nmrpeak

WORKDIR /build
ENV PROVIDER_CORE_IN_CONTAINER=1
COPY requirements.lock ./
COPY dependencies/provider-core-api.lock dependencies/provider-core-source.txt dependencies/
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: \
        --requirement requirements.lock

COPY pyproject.toml ./
COPY nmrpeak_provider/ nmrpeak_provider/
RUN python -m pip install --no-cache-dir --no-index --no-deps \
        --no-build-isolation . \
    && python -m pip check \
    && python -P -c 'import cryptography; import provider_core.controller; import nmrpeak_provider.provider_main' \
    && rm -rf /build

ARG SOURCE_REVISION
ARG IMAGE_INPUT_ID
RUN printf '%s\n' "$SOURCE_REVISION" | grep -Eq '^[0-9a-f]{40}$' \
    && printf '%s\n' "$IMAGE_INPUT_ID" | grep -Eq '^sha256:[0-9a-f]{64}$'
LABEL org.opencontainers.image.revision="$SOURCE_REVISION"
LABEL io.numpde.nmrpeak.image.input-id="$IMAGE_INPUT_ID"
LABEL io.numpde.nmrpeak.provider.contract-id="nmr.provider.http.v1"

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONUNBUFFERED=1

USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "nmrpeak_provider.provider_main"]

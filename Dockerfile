# syntax=docker/dockerfile:1

FROM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36 AS datadog-init-builder

ARG TARGETOS
ARG TARGETARCH
# The builder digest owns these exact glibc toolchain packages. Assert them so
# a tag/digest mismatch fails before any source is compiled.
RUN test "$(dpkg-query -W -f='${Version}' gcc)" = "4:12.2.0-3" && \
    test "$(dpkg-query -W -f='${Version}' git)" = "1:2.39.5-0+deb12u3" && \
    test "$(dpkg-query -W -f='${Version}' libc6-dev:amd64)" = "2.36-9+deb12u14" && \
    test "$(dpkg-query -W -f='${Version}' binutils)" = "2.40-2"
WORKDIR /src/datadog-agent
RUN git clone --filter=blob:none --no-checkout \
        https://github.com/DataDog/datadog-agent.git . && \
    git checkout --detach "6dbfeceb7c8e1575803f209afaa62004293724d6" && \
    test "$(git rev-parse HEAD)" = \
        "6dbfeceb7c8e1575803f209afaa62004293724d6"
RUN --mount=type=cache,target=/go/pkg/mod,sharing=locked \
    --mount=type=cache,target=/root/.cache/go-build,sharing=locked \
    go mod edit \
        -require="golang.org/x/net@v0.56.0" \
        -require="google.golang.org/grpc@v1.82.1" && \
    go mod download \
        "golang.org/x/net@v0.56.0" \
        "google.golang.org/grpc@v1.82.1" && \
    CGO_ENABLED=1 GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" \
    go build \
        -buildvcs=false \
        -tags="serverless,otlp,zlib,zstd" \
        -ldflags="-w -X github.com/DataDog/datadog-agent/pkg/serverless/tags.currentExtensionVersion=1.10.2 -X github.com/DataDog/datadog-agent/pkg/version.agentVersionDefault=7.81.2" \
        -o /out/datadog-init \
        ./cmd/serverless-init && \
    readelf -l /out/datadog-init | \
        grep -F "Requesting program interpreter: /lib64/ld-linux-x86-64.so.2" && \
    ldd /out/datadog-init && \
    go version -m /out/datadog-init | \
        grep -E "golang.org/x/net[[:space:]]+v0[.]56[.]0([[:space:]]|$)" && \
    go version -m /out/datadog-init | \
        grep -E "google.golang.org/grpc[[:space:]]+v1[.]82[.]1([[:space:]]|$)"

FROM langchain/langgraph-api:0.12.6-py3.11-wolfi@sha256:60a141df7699eb28a6fd4e09cdf3e81d8e61ef7a6d9b17769af433100ab18ee0 AS langgraph-upstream-sanitized

# Remove only the verified vulnerable helper before flattening the pinned
# upstream root filesystem. This keeps the vulnerable ancestor layer out of
# the final image ancestry while preserving every other upstream file.
RUN test "$(sha256sum /app/datadog-init | cut -d ' ' -f 1)" = \
        "ba83b153a0c5a2b399b03da2a987fb1f766634f9c4b90d56a89ea0d082eaddb7" && \
    rm /app/datadog-init && \
    test ! -e /app/datadog-init

FROM scratch AS langgraph-runtime-base

COPY --from=langgraph-upstream-sanitized / /

# Reconstruct the pinned upstream image configuration exactly. Docker COPY
# preserves rootfs ownership/modes but a scratch stage does not inherit config.
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=True \
    PORT=8000 \
    PIP_ROOT_USER_ACTION=ignore \
    N_WORKERS=1 \
    N_JOBS_PER_WORKER=10 \
    LANGGRAPH_RUNTIME_EDITION=postgres \
    LANGGRAPH_SERVER_HOST=0.0.0.0 \
    DD_TRACE_ENABLED=false \
    LANGSMITH_LANGGRAPH_API_REVISION=e898bb7 \
    LANGSMITH_LANGGRAPH_API_VARIANT=licensed

LABEL org.opencontainers.image.base.name="docker.io/langchain/langgraph-api:0.12.6-py3.11-wolfi" \
      org.opencontainers.image.base.digest="sha256:60a141df7699eb28a6fd4e09cdf3e81d8e61ef7a6d9b17769af433100ab18ee0" \
      io.refund-agent.langgraph-upstream.revision="e898bb7"

USER 0
WORKDIR /api
HEALTHCHECK --interval=5s --timeout=2s --retries=5 \
    CMD ["python", "/api/healthcheck.py"]
ENTRYPOINT ["/storage/entrypoint.sh"]

# Keep this Python version aligned with requires-python in pyproject.toml.
# The Wolfi variant matches image_distro in langgraph.json.
FROM langgraph-runtime-base

LABEL org.opencontainers.image.title="LangGraph Refund Agent" \
      org.opencontainers.image.description="Risk-aware human-in-the-loop customer-service assistant built with LangGraph" \
      org.opencontainers.image.version="0.8.0" \
      org.opencontainers.image.authors="duxingru" \
      org.opencontainers.image.licenses="MIT" \
      io.refund-agent.datadog-init.source="https://github.com/DataDog/datadog-agent" \
      io.refund-agent.datadog-init.revision="6dbfeceb7c8e1575803f209afaa62004293724d6" \
      io.refund-agent.datadog-init.version="7.81.2/1.10.2" \
      io.refund-agent.datadog-init.module-overrides="golang.org/x/net@v0.56.0,google.golang.org/grpc@v1.82.1" \
      io.refund-agent.datadog-init.builder="golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install the local project using the constraints supplied by the Agent Server
# image. .dockerignore keeps credentials and local development files out of
# both the build context and the resulting image.
ADD . /deps/project

RUN cd /deps/project && \
    uv pip install \
        --system \
        --no-cache-dir \
        -c /api/constraints.txt \
        -e .

# Register the graph using the same entry point as langgraph.json.
ENV LANGGRAPH_HTTP='{"app":"/deps/project/src/agent/webapp.py:app"}'
ENV LANGSERVE_GRAPHS='{"agent":"/deps/project/src/agent/graph.py:create_graph"}'

# Restore the Agent Server package in case an application dependency changed
# one of its dependencies, then remove build-only packaging tools.
RUN mkdir -p /api/langgraph_api /api/langgraph_runtime /api/langgraph_license && \
    touch /api/langgraph_api/__init__.py \
          /api/langgraph_runtime/__init__.py \
          /api/langgraph_license/__init__.py && \
    PYTHONDONTWRITEBYTECODE=1 uv pip install \
        --system \
        --no-cache-dir \
        --no-deps \
        -e /api && \
    uv pip install \
        --system \
        --no-cache-dir \
        -c /api/constraints.txt \
        "cryptography>=50,<51" && \
    apk del git && \
    pip uninstall -y pip setuptools wheel && \
    rm -rf /usr/local/lib/python*/site-packages/pip* \
           /usr/local/lib/python*/site-packages/setuptools* \
           /usr/local/lib/python*/site-packages/wheel* \
           /usr/lib/python*/site-packages/pip* \
           /usr/lib/python*/site-packages/setuptools* \
           /usr/lib/python*/site-packages/wheel* && \
    (find /usr/local/bin /usr/bin -name "pip*" -delete 2>/dev/null || true) && \
    uv pip uninstall --system pip setuptools wheel && \
    rm -f /usr/bin/uv /usr/bin/uvx

# Preserve the Agent Server's original entrypoint contract while replacing
# its vulnerable Go 1.26.5 helper with the approved public 7.81.2 source build.
# Source/build tools stay in the discarded builder stage.
COPY --from=datadog-init-builder /out/datadog-init /app/datadog-init
COPY --from=datadog-init-builder /src/datadog-agent/LICENSE \
    /src/datadog-agent/NOTICE \
    /src/datadog-agent/LICENSE-3rdparty.csv \
    /usr/share/licenses/datadog-init/

# Hard fail if the rebuilt CGO helper cannot resolve the pinned runtime's glibc
# interpreter or execute a child process in the final filesystem lineage.
RUN test -x /lib64/ld-linux-x86-64.so.2 && \
    /app/datadog-init /bin/true

WORKDIR /deps/project

# The LangGraph Agent Server base image supplies the entry point.
EXPOSE 8000

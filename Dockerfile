# syntax=docker/dockerfile:1

# Keep this Python version aligned with requires-python in pyproject.toml.
# The Wolfi variant matches image_distro in langgraph.json.
FROM langchain/langgraph-api:3.11-wolfi

LABEL org.opencontainers.image.title="LangGraph Refund Agent" \
      org.opencontainers.image.description="Risk-aware human-in-the-loop customer-service assistant built with LangGraph" \
      org.opencontainers.image.version="0.5.0" \
      org.opencontainers.image.authors="duxingru" \
      org.opencontainers.image.licenses="MIT"

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

WORKDIR /deps/project

# The LangGraph Agent Server base image supplies the entry point.
EXPOSE 8000

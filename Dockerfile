FROM python:3.12-slim-bookworm
ARG CLAUDE_VERSION=2.1.269
ARG CODEX_VERSION=0.154.0
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git ripgrep libatomic1 procps nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN curl --fail --silent --show-error --location https://claude.ai/install.sh -o /tmp/install-claude.sh \
    && bash /tmp/install-claude.sh "${CLAUDE_VERSION}" \
    && install -m 0755 /root/.local/bin/claude /usr/local/bin/claude \
    && /usr/local/bin/claude --version
RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && codex --version
RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir /workspace /relay
COPY trace_lab /opt/trace-lab/trace_lab
ENV PYTHONPATH=/opt/trace-lab PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /workspace
USER agent
CMD ["python3", "-m", "trace_lab.relay"]

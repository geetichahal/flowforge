# FlowForge server for Hugging Face Spaces (Docker SDK): FastAPI + website + Ollama (qwen3:4b).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates zstd procps \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://ollama.com/install.sh | sh

# Spaces run containers as uid 1000
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    OLLAMA_MODELS=/home/user/.ollama/models \
    OLLAMA_HOST=http://127.0.0.1:11434 \
    OLLAMA_KEEP_ALIVE=24h \
    FLOWFORGE_MODEL=qwen3:4b \
    FLOWFORGE_PUBLIC=1 \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

USER user
# Bake the model into the image so a cold start does not download 2.5 GB.
RUN (ollama serve > /tmp/ollama-build.log 2>&1 &) \
    && for i in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags > /dev/null && break; sleep 1; done \
    && ollama pull "$FLOWFORGE_MODEL"

COPY --chown=user . .
EXPOSE 7860
CMD ["bash", "start.sh"]

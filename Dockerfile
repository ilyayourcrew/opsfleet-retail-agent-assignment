# Optional container (not required to run the project).
#   docker build -t retail-agent .
#   docker run -it --env-file .env -v "$HOME/.config/gcloud:/root/.config/gcloud:ro" retail-agent chat
#   docker run -it retail-agent chat --provider fake --warehouse duckdb      # fully offline
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
RUN pip install -r requirements.txt

COPY config ./config
COPY data ./data
COPY scripts ./scripts
COPY evals ./evals

# offline warehouse baked in so `--warehouse duckdb` works without any credentials
RUN python scripts/build_offline_db.py

ENTRYPOINT ["retail-agent"]
CMD ["chat"]

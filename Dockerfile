FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY orchestrator ./orchestrator
COPY probes ./probes
COPY verify ./verify
COPY fixtures ./fixtures
RUN pip install --no-cache-dir -e .

ENTRYPOINT ["python", "-m", "orchestrator"]
CMD ["simulate"]

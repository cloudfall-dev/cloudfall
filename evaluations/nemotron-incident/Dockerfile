# The disk-full evaluation as a container, for a Nebius Serverless Job.
# It needs no host: reads and checks are played back from the run recorded
# on the broken server, and the models are called live on Token Factory.
# Build from the repository root:
#   docker build -f evaluations/nemotron-incident/Dockerfile -t cloudfall-eval .
# Run with the key in the environment:
#   docker run --rm -e CLOUDFALL_API_KEY cloudfall-eval
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /cloudfall
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY sdk/src sdk/src
COPY engine/src engine/src
COPY engine/ansible engine/ansible
COPY config/schemas config/schemas
RUN uv sync --frozen --no-dev --no-editable

COPY examples/disk-full-incident examples/disk-full-incident
COPY evaluations/nemotron-incident/run.py evaluations/nemotron-incident/run.py
ENV PATH="/cloudfall/.venv/bin:${PATH}"

ENTRYPOINT ["python", "evaluations/nemotron-incident/run.py"]
CMD ["replay", "nvidia/nemotron-3-super-120b-a12b,nvidia/Nemotron-3-Ultra-550b-a55b,nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B,nvidia/Nemotron-3_5-Lightning", "10"]

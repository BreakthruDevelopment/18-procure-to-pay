# Minimal image: the locked runtime dependencies and the package, nothing else. No credentials are baked in.
# App 17 is a git dependency (pinned by tag in pyproject.toml), so the build stage needs git.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY p2p ./p2p
# The ERP schema and seed are force-included into the wheel from here (see pyproject.toml); without
# this COPY the build stage cannot find them and the image ships a package that cannot open a
# database. The `Container image` job is NOT a required check, so this is verified by hand.
COPY erp ./erp
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runner
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
COPY --from=build /opt/venv /opt/venv
RUN useradd --system --uid 10001 app
USER app
ENTRYPOINT ["p2p"]
CMD ["--version"]

# --- Lambda image ---------------------------------------------------------------------
# The deployed surface of this app is one function: `GET /health` (`p2p.demo.health.handler`).
# Lambda runs the image's own ENTRYPOINT and `30-apps-infra/stacks/aws` sets no `image_config`,
# so the runtime interface client must be inside the image — which is what the AWS base image
# provides. The CLI image above stays the CLI image: this stage adds no code of its own, it only
# installs the same locked set and the same wheel into the Lambda task root.
#
# Built separately from `build` because the AWS base image has no uv and no git, and the app 17
# dependency is a git pin: resolving it here keeps the runtime stage offline and credential-free.
FROM python:3.12-slim AS lambda-build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY p2p ./p2p
# Same reason as the build stage: the ERP schema and seed reach the wheel only through the
# force-include in pyproject.toml, and the build cannot see them without this COPY.
COPY erp ./erp
# `--locked`, not `--frozen`: `--frozen` exports the lock WITHOUT checking it still matches
# pyproject.toml, so a dependency added and never relocked would be missing from the image and
# the break would first show up as a Lambda import error after deploy.
RUN uv export --locked --no-dev --no-emit-project --no-hashes -o /tmp/runtime-reqs.txt \
    && uv pip install --python /usr/local/bin/python3 --target /task -r /tmp/runtime-reqs.txt \
    && uv build --wheel -o /tmp/dist \
    && uv pip install --python /usr/local/bin/python3 --target /task --no-deps /tmp/dist/*.whl

FROM public.ecr.aws/lambda/python:3.12 AS lambda
# `tests/test_lambda_image.py` asserts this string still names a callable, so moving the handler
# fails a test instead of a deploy.
COPY --from=lambda-build /task ${LAMBDA_TASK_ROOT}
CMD ["p2p.demo.health.handler"]

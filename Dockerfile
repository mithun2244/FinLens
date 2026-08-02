# FastAPI backend (api.py) for Hugging Face Spaces.
#
#   docker build -t finlens-api .
#   docker run -p 7860:7860 -e GROQ_API_KEY=gsk_... finlens-api
#
# Backend only. The Next.js app in frontend/ is not built or served here — set
# ALLOWED_ORIGINS to wherever it is hosted, or the browser blocks every call it makes.
#
# Not used by Render: render.yaml declares `runtime: python` and brings its own build and
# start commands, so the two deployment paths do not collide.

FROM python:3.12-slim

# libGL and libglib are opencv's, pulled in by RapidOCR — which Docling uses for the
# scanned-page fallback path. Without them the image builds cleanly and then dies on the
# first import with "libGL.so.1: cannot open shared object file". libgomp is onnxruntime's
# and torch's OpenMP runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Spaces runs the container as uid 1000, and anything the app writes has to be owned by
# it: the HuggingFace model cache, and data/ for uploads, the Chroma store and the LLM
# cache. Running as root instead would work locally and then fail on Spaces only at the
# first write, which is the worst place to find out.
RUN useradd --create-home --uid 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR $HOME/app

# torch AND torchvision from the CPU index, before requirements.txt so the resolver never
# sees the default builds. A plain `pip install torch` on Linux pulls the CUDA wheel and
# ~2.5 GB of libraries this CPU-only service never calls; requirements.txt:47 carries the
# same instruction for local installs.
#
# torchvision has to be named explicitly even though nothing here asks for it directly.
# docling-ibm-models depends on it, so leaving it to requirements.txt resolved it against
# default PyPI — whose Linux wheel is compiled against the CUDA torch. The versions look
# right and the image builds clean, then dies at startup with
# "RuntimeError: operator torchvision::nms does not exist", because torchvision's compiled
# ops cannot register against a +cpu torch. Installing the pair together from one index is
# what keeps their ABI matched: 0.28.0+cpu rather than 0.28.0.
#
# This does not reproduce on Windows, where the wheels carry no such split — so it is
# reachable only by building the image.
#
# Copied on its own so this layer is cached against everything except a dependency change.
COPY --chown=user requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# api.py imports src/, and serves sample documents and the policy corpus out of
# evals/fixtures/ — see /api/samples and /api/policies. Fixtures are runtime data here,
# not test data, which is why .dockerignore drops evals/reports/ but keeps this.
COPY --chown=user api.py ./
COPY --chown=user src/ ./src/
COPY --chown=user evals/fixtures/ ./evals/fixtures/

# Spaces routes to 7860.
EXPOSE 7860

# One worker, deliberately. api.py:108 keeps parsed documents in an in-process dict, so a
# second worker would hold its own copy and uploads would appear to vanish between
# requests; the embedded Chroma store is single-process regardless (decision D-34).
#
# Note that boot is heavy: the lifespan hook loads Docling and MiniLM before serving, and
# on a cold container that includes downloading ~500 MB of weights. The models are not
# baked into the image because doing so means running Docling's downloader at build time,
# which trades a slow first boot for a slow, network-dependent build.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]

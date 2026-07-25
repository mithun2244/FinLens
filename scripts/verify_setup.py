"""End-to-end environment verification (phases.md Phase 1, Definition of Done).

Confirms every dependency is installed and reachable *before* any business logic is
written, so a Phase 2 failure is never ambiguous between "my code is wrong" and "my
environment is broken".

    python scripts/verify_setup.py

Checks, in order:
  1. Python version
  2. Every third-party import
  3. Local embedding model -> a 384-dim vector, with no network call
  4. ChromaDB persistent client -> write, query, delete
  5. Groq connectivity and configured model liveness
  6. LangSmith (optional — absence is reported, never a failure: decision D-11)
  7. Directory skeleton and secret hygiene

Exit code 0 when every required check passes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (  # noqa: E402
    CHROMA_DIR,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    PROJECT_ROOT,
    REASONING_MODEL,
    UPLOAD_DIR,
    ensure_directories,
    get_settings,
)

OK = "[ OK ]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"
WARN = "[WARN]"

_failures: list[str] = []


def _fail(label: str, detail: str) -> None:
    _failures.append(label)
    print(f"{FAIL} {label}")
    for line in detail.splitlines():
        print(f"       {line}")


def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        print(f"{OK}   Python {major}.{minor} ({sys.executable})")
    else:
        _fail(
            "Python version",
            f"Found {major}.{minor}; this project requires 3.10+ "
            "(uses `X | None` syntax and Pydantic v2).",
        )


def check_imports() -> None:
    modules = [
        ("langchain_core", "LangChain core"),
        ("langchain_groq", "Groq chat models"),
        ("langchain_huggingface", "Local embeddings adapter"),
        ("langchain_chroma", "Chroma vector store adapter"),
        ("docling", "Docling document parser"),
        ("chromadb", "ChromaDB"),
        ("sentence_transformers", "Sentence Transformers"),
        ("fitz", "PyMuPDF (page rendering)"),
        ("PIL", "Pillow"),
        ("pydantic_settings", "Pydantic settings"),
        ("pandas", "pandas"),
    ]
    for module_name, label in modules:
        try:
            __import__(module_name)
            print(f"{OK}   import {module_name:<24} {label}")
        except ImportError as exc:
            _fail(f"import {module_name}", f"{exc}\nRun: pip install -r requirements.txt")


#: Paid-provider SDKs. A DIRECT dependency on any of these is a Rule 1 breach.
FORBIDDEN_PACKAGES: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "cohere",
        "google-generativeai",
        "pinecone-client",
        "weaviate-client",
        "azure-ai-formrecognizer",
        "boto3",
    }
)

#: Tolerated as TRANSITIVE dependencies only, with the package that pulls them in.
#: Decision D-13: ragas hard-requires the OpenAI SDK stack even when its judge is
#: configured to something else. Installed but never called is acceptable; installed
#: and reachable-by-default is not — hence check_no_paid_credentials() below.
TOLERATED_TRANSITIVE: dict[str, str] = {
    "openai": "ragas",
    "langchain-openai": "ragas",
    "tiktoken": "ragas",
    "instructor": "ragas",
}


def check_forbidden_packages() -> None:
    """Rule 1, part 1: no paid provider SDK may be a DIRECT dependency."""
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    declared = {
        line.split("=")[0].split(">")[0].split("<")[0].split("[")[0].strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    breaches = declared & FORBIDDEN_PACKAGES
    if breaches:
        _fail("Rule 1 (direct deps)", f"Paid provider SDKs declared: {sorted(breaches)}")
    else:
        print(f"{OK}   Rule 1: no paid provider SDK is a direct dependency")


def check_installed_packages() -> None:
    """Rule 1, part 2: audit what is actually INSTALLED, not just what we declared.

    requirements.txt is intent; the venv is reality. A transitive pull of a paid
    provider SDK does not cost anything on its own, but it does mean a library could
    silently default to it — which rules.md calls a highest-severity bug.
    """
    from importlib.metadata import distributions

    installed = {dist.metadata["Name"].lower() for dist in distributions() if dist.metadata["Name"]}
    present = sorted((installed & FORBIDDEN_PACKAGES) | (installed & set(TOLERATED_TRANSITIVE)))

    if not present:
        print(f"{OK}   Rule 1: no paid provider SDK installed in the environment")
        return

    unexplained = [pkg for pkg in present if pkg not in TOLERATED_TRANSITIVE]
    if unexplained:
        _fail(
            "Rule 1 (installed packages)",
            f"Unexplained paid provider SDKs installed: {unexplained}\n"
            "Identify what pulled them in (pip show <pkg> | Required-by) and either\n"
            "remove it or document it in TOLERATED_TRANSITIVE with a decision in memory.md.",
        )
        return

    explained = ", ".join(f"{pkg} (via {TOLERATED_TRANSITIVE[pkg]})" for pkg in present)
    print(f"{WARN} Paid provider SDKs present as transitive deps: {explained}")
    print("       Tolerated (decision D-13) — installed but never called.")


def check_no_paid_credentials() -> None:
    """Rule 1, part 3: the real safeguard — no paid provider can authenticate.

    The OpenAI SDK is installed (via ragas) but has no key. Any library that silently
    tries to default to it will raise immediately rather than quietly accrue cost.
    This check fails loudly if a paid-provider key ever appears in the environment.
    """
    import os

    paid_key_vars = [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "COHERE_API_KEY",
        "GOOGLE_API_KEY",
    ]
    found = [var for var in paid_key_vars if os.environ.get(var)]

    if found:
        _fail(
            "Rule 1 (credentials)",
            f"Paid provider credentials are present in the environment: {found}\n"
            "This project must never be able to authenticate against a metered API —\n"
            "that is what makes an accidental fallback fail loudly instead of billing you.\n"
            "Unset them for this project's environment.",
        )
    else:
        print(f"{OK}   Rule 1: no paid provider credentials in the environment")
        print("       Any accidental fallback will fail loudly rather than accrue cost.")


def check_embeddings() -> None:
    """Embeddings must run locally — no network call, no per-token charge (decision D-2)."""
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
    except ImportError:
        print(f"{SKIP} Local embeddings (langchain_huggingface not installed)")
        return

    print(f"       Loading {EMBEDDING_MODEL} (first run downloads ~80 MB)...")
    try:
        started = time.perf_counter()
        embeddings = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
        vector = embeddings.embed_query("NAT Gateway data processing charge of 412.90 USD")
        elapsed = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001
        _fail("Local embeddings", f"{type(exc).__name__}: {exc}")
        return

    if len(vector) == EMBEDDING_DIMENSIONS:
        print(f"{OK}   Local embeddings -> {len(vector)}-dim vector ({elapsed:.1f}s incl. load)")
    else:
        _fail(
            "Local embeddings",
            f"Expected {EMBEDDING_DIMENSIONS} dimensions, got {len(vector)}. "
            "EMBEDDING_DIMENSIONS in src/config.py is out of sync with EMBEDDING_MODEL.",
        )


#: Chroma requires 3-512 chars of [a-zA-Z0-9._-], starting and ending alphanumeric.
#: A leading underscore is rejected — worth knowing before Phase 3 names its collections.
PROBE_COLLECTION = "setup-probe"


def check_chroma() -> None:
    """Verify the persistent store using OUR embeddings, not Chroma's default.

    Decision D-14: Chroma silently falls back to its own bundled ONNX MiniLM when a
    collection is queried with ``query_texts`` and no embedding function. That is a
    second, separately-cached copy of the model we already load — and it means vectors
    would be produced by a component we do not control. Phase 3 must always pass
    embeddings explicitly. This probe exercises that path so the check tests what we
    actually ship.
    """
    try:
        import chromadb
        from langchain_huggingface import HuggingFaceEmbeddings
    except ImportError:
        print(f"{SKIP} ChromaDB (dependencies not installed)")
        return

    try:
        embeddings = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
        document = "Subtotal 462.00, tax 39.27, total 501.27"

        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        collection = client.get_or_create_collection(PROBE_COLLECTION)
        collection.upsert(
            ids=["probe-1"],
            documents=[document],
            embeddings=[embeddings.embed_documents([document])[0]],
            metadatas=[{"document_id": "probe", "page": 1}],
        )
        results = collection.query(
            query_embeddings=[embeddings.embed_query("what was the total")],
            n_results=1,
        )
        found = bool(results["ids"] and results["ids"][0])
        client.delete_collection(PROBE_COLLECTION)
    except Exception as exc:  # noqa: BLE001
        _fail("ChromaDB", f"{type(exc).__name__}: {exc}\nStore path: {CHROMA_DIR}")
        return

    if found:
        print(f"{OK}   ChromaDB persistent client (write/query/delete) at {CHROMA_DIR}")
        print("       Used our local MiniLM embeddings, not Chroma's bundled default.")
    else:
        _fail("ChromaDB", "Wrote a document but the query returned nothing.")


def check_groq() -> None:
    settings = get_settings()
    if not settings.groq_configured:
        _fail(
            "Groq API key",
            "GROQ_API_KEY is not set.\n"
            "Copy .env.example to .env and add a free key from\n"
            "https://console.groq.com/keys  (no credit card required).",
        )
        return

    try:
        from langchain_groq import ChatGroq
    except ImportError:
        print(f"{SKIP} Groq connectivity (langchain_groq not installed)")
        return

    try:
        started = time.perf_counter()
        llm = ChatGroq(
            model=REASONING_MODEL,
            api_key=settings.groq_api_key,  # type: ignore[arg-type]
            temperature=0,
            max_retries=1,
            timeout=30.0,
        )
        response = llm.invoke("Reply with exactly: ok")
        elapsed = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001
        _fail(
            "Groq connectivity",
            f"{type(exc).__name__}: {exc}\n"
            f"Model attempted: {REASONING_MODEL}\n"
            "If this is a decommissioned-model error, run: python scripts/check_models.py",
        )
        return

    preview = str(response.content).strip()[:40]
    print(f"{OK}   Groq reachable: {REASONING_MODEL} -> {preview!r} ({elapsed:.2f}s)")


def check_langsmith() -> None:
    """Optional by design — its absence must never break the app (decision D-11)."""
    settings = get_settings()
    if settings.tracing_enabled:
        print(f"{OK}   LangSmith tracing enabled (project: {settings.langchain_project})")
    else:
        print(f"{SKIP} LangSmith tracing not configured — optional, app runs without it")


def check_filesystem() -> None:
    ensure_directories()
    for directory in (UPLOAD_DIR, CHROMA_DIR):
        if directory.is_dir():
            print(f"{OK}   Directory exists: {directory.relative_to(PROJECT_ROOT)}")
        else:
            _fail("Directory skeleton", f"Missing: {directory}")

    env_path = PROJECT_ROOT / ".env"
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    if env_path.exists():
        if ".env" in gitignore:
            print(f"{OK}   .env exists and is gitignored (Rule 4)")
        else:
            _fail("Secret hygiene", ".env exists but is NOT listed in .gitignore.")
    else:
        _fail("Configuration", ".env not found. Copy .env.example to .env and fill it in.")

    if "data/" in gitignore:
        print(f"{OK}   data/ is gitignored — no financial documents in version control")
    else:
        _fail("Secret hygiene", "data/ is not gitignored (Rule 4).")


def main() -> int:
    print("=" * 72)
    print("Multimodal AI Financial Assistant — Phase 1 setup verification")
    print("=" * 72 + "\n")

    for section, check in (
        ("Runtime", check_python),
        ("Dependencies", check_imports),
        ("Rule 1 — direct deps", check_forbidden_packages),
        ("Rule 1 — installed packages", check_installed_packages),
        ("Rule 1 — credentials", check_no_paid_credentials),
        ("Filesystem", check_filesystem),
        ("Local embeddings", check_embeddings),
        ("Vector store", check_chroma),
        ("Groq (cloud, free tier)", check_groq),
        ("Observability", check_langsmith),
    ):
        print(f"\n-- {section} " + "-" * (68 - len(section)))
        check()

    print("\n" + "=" * 72)
    if _failures:
        print(f"{FAIL} {len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"       - {failure}")
        print("\nPhase 1 is NOT complete. Resolve the above, then re-run.")
        return 1

    print(f"{OK}   All checks passed. Phase 1 environment is verified.")
    print("       Next: update memory.md, then begin Phase 2 (src/parser.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

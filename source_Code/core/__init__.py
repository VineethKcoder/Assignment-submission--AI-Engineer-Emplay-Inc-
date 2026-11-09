"""Core utilities (settings, LLM client, tracing, logging).

Process-wide environment guards live here because every entry point imports `core` first.
"""

import os

# On Windows, PyTorch (libiomp5md) and FAISS (libomp140) each ship an OpenMP runtime;
# loading both aborts the process with "OMP: Error #15". Allowing the duplicate runtime is
# the standard workaround and is safe for inference workloads like ours.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

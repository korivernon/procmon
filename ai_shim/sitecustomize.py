# Python imports this automatically at startup because procmon puts this folder on the PYTHONPATH of
# every process it launches (see procmon/ai_env.py). It must never break the app it loads into.
import os
import sys


def _chain_to_the_real_sitecustomize():
    # Homebrew's Python ships its own sitecustomize.py, which this file shadows; it has to still run.
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for entry in sys.path:
        if not entry or os.path.abspath(entry) == here:
            continue
        candidate = os.path.join(entry, "sitecustomize.py")
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location("_chained_sitecustomize", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return


try:
    _chain_to_the_real_sitecustomize()
except Exception:
    pass

if os.environ.get("PROCMON_AI_PROJECT"):
    try:
        import procmon_ai_shim
        procmon_ai_shim.install()
    except Exception:
        pass

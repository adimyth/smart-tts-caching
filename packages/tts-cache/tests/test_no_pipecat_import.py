"""The core must import with Pipecat absent.

Runs in a subprocess whose import system refuses `pipecat`, which is what an
environment without Pipecat installed looks like to the interpreter, then
imports every module of `tts_cache`.
"""

import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import importlib
    import importlib.abc
    import pkgutil
    import sys

    class NoPipecat(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "pipecat" or name.startswith("pipecat."):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None

    sys.meta_path.insert(0, NoPipecat())
    for mod in list(sys.modules):
        if mod == "pipecat" or mod.startswith("pipecat."):
            del sys.modules[mod]

    import tts_cache

    names = [tts_cache.__name__]
    for info in pkgutil.walk_packages(tts_cache.__path__, prefix="tts_cache."):
        names.append(info.name)
    for name in names:
        importlib.import_module(name)
    leaked = sorted(m for m in sys.modules if m == "pipecat" or m.startswith("pipecat."))
    assert not leaked, leaked
    print(len(names))
    """
)


def test_core_imports_without_pipecat() -> None:
    proc = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip().splitlines()[-1]) >= 1

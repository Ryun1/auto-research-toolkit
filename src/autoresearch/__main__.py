"""`python -m autoresearch`: the last fallback in every `bin/ar` shim.

`ar init` writes a shim into each domain that probes the domain's venvs for the
installed entry point and then, failing that, runs the module with whichever
interpreter can import the package. That fallback has to work in the one
configuration it is ever reached: a machine whose toolkit lives in its default
python3, where `ar` is not on PATH (H149). Without this file the probe
succeeds and the fallback dies with an ImportError naming `__main__`, so the
shim's own remedy text -- the loud, useful failure -- is the thing a fresh clone
never reaches (sig.golf H7).

Same guard as `cli.py`, same exit code, so a shim can propagate a refusal.
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

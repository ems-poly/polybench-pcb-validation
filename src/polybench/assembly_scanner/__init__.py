"""Assembly Scanner, embedded.

Vendored from the Assembly Scanner PoC (app/): camera capture, Code 128 decode, label
resolver, BOM importer, trigger debouncer, HUD text and the Fusion client with its selection method. The modules keep the
PoC's logic; only the imports changed (app.* -> polybench.assembly_scanner.*) and main.py lost its Qt entry point.
service.py is the headless replacement for the PoC's PySide6 window: it runs the same threads and rules inside the server.
See VENDORED.md for the file-by-file list.
"""

__version__ = "0.2.0"

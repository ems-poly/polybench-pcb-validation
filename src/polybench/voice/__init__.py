"""Hands-free voice operation: capture, local STT, grammar and local-LLM understanding, proposals, spoken replies.

See docs/VOICE.md. Modules ported from the earlier voice prototype keep their names.
"""

import os as _os

# Privacy : nothing leaves the Mac. Whisper loads from the local Hugging Face cache only; a missing model is an
# install hint in the Voice settings, never an automatic download. Set before huggingface_hub is imported (it reads these once).
_os.environ["HF_HUB_OFFLINE"] = "1"
_os.environ["TRANSFORMERS_OFFLINE"] = "1"
_os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

"""instinct-memory — git-tracked markdown memory for Hermes Agent.

An implementation of the memory architecture behind Instinct (reverse-engineered by
Dhravya Shah): a directory of interconnected markdown records, a profile injected at the
start of every conversation, keyword retrieval backed by aliases rather than embeddings,
read-only memory for the agent, and a nightly background process that owns every write.

After install.sh, activate with ``memory.provider: instinct``. The installer supplies
an ``instinct`` directory alias for Hermes discovery; a manual drop-in copy uses the
``instinct-memory`` directory key. Optional ``keep_holographic: true`` preserves the
separate holographic ``fact_store`` / ``fact_feedback`` tools.
"""

from __future__ import annotations

import logging

from .provider import InstinctMemoryProvider

logger = logging.getLogger(__name__)

__all__ = ["InstinctMemoryProvider", "register"]


def register(ctx) -> None:
    """Register the instinct memory provider with the plugin system."""
    from .provider import _load_plugin_config

    ctx.register_memory_provider(InstinctMemoryProvider(config=_load_plugin_config()))

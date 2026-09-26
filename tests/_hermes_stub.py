"""Install only missing Hermes interfaces; never load a user's configuration."""

import importlib
import json
import os
import re
import sys
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


def _install(name, attributes):
    try:
        return importlib.import_module(name)
    except ImportError:
        pass
    parent, _, child = name.rpartition(".")
    if parent and parent not in sys.modules:
        package = types.ModuleType(parent)
        package.__path__ = []
        sys.modules[parent] = package
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    if parent:
        setattr(sys.modules[parent], child, module)
    return module


class MemoryProvider(ABC):
    @property
    @abstractmethod
    def name(self): ...

    @abstractmethod
    def is_available(self): ...

    @abstractmethod
    def initialize(self, session_id, **kwargs): ...

    @abstractmethod
    def get_tool_schemas(self): ...


@dataclass(frozen=True)
class RecallStatus:
    provider_label: str
    count: int
    glyph: str = ""


def is_trivial_prompt(text):
    value = (text or "").strip()
    return not value or value.startswith("/") or bool(
        re.fullmatch(r"(?:hi|hello|hey|ok|okay|thanks|thank you|done)[^a-z0-9]*", value.lower())
    )


_install("agent.memory_provider", dict(MemoryProvider=MemoryProvider,
    RecallStatus=RecallStatus, is_trivial_prompt=is_trivial_prompt))
_install("tools.registry", dict(tool_error=lambda message, **extra: json.dumps(
    {"error": str(message), **extra})))
_install("hermes_constants", dict(get_hermes_home=lambda: Path(os.environ["HERMES_HOME"]),
    display_hermes_home=lambda: "$HERMES_HOME"))
_install("utils", dict(is_truthy_value=lambda value: str(value).strip().lower() in
    {"true", "1", "yes", "on"}))

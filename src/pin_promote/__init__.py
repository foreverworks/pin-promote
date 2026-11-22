"""pin-promote: move a repository's pins only after their checks pass, and never from a stale build.

The library's public surface, for a tool that promotes from its own code instead of the command line:

    from pin_promote import load, promote, move_tag

`pin_promote.config` is the config format, `pin_promote.checks` the evidence that checks passed,
`pin_promote.promote` the promotion itself (git), `pin_promote.registry` the registry tag a
channel may move to its pinned digest, and `pin_promote.tags` which tag follows which pin.
Python standard library only.
"""

__version__ = "0.1.0"

from .checks import EvidenceError, evidence, from_needs, missing
from .config import SCHEMA, Channel, Config, ConfigError, Registry, SourceClasses, load, parse, source_classes
from .promote import Edit, GitError, PromotionError, Request, Result, current, pin_text, promote, tip_config
from .registry import RegistryError, move_tag

__all__ = [
    "SCHEMA",
    "Channel",
    "Config",
    "ConfigError",
    "Edit",
    "EvidenceError",
    "GitError",
    "PromotionError",
    "Registry",
    "RegistryError",
    "Request",
    "Result",
    "SourceClasses",
    "__version__",
    "current",
    "evidence",
    "from_needs",
    "load",
    "missing",
    "move_tag",
    "parse",
    "pin_text",
    "promote",
    "source_classes",
    "tip_config",
]

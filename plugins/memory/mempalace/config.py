"""
MemPalace config reader.

Reads ``memory.mempalace`` section from config.yaml:

    memory:
      provider: mempalace
      mempalace:
        data_path: "~/.hermes/mempalace"
        embedding_model: "BAAI/bge-small-zh-v1.5"
        compression_ratio: 30
        enable_kg: true
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional


_DEFAULTS: Dict[str, Any] = {
    "data_path": "~/.hermes/mempalace",
    "embedding_model": "bge-m3",
    "compression_ratio": 30,
    "enable_kg": True,
    "recall_mode": "hybrid",     # context | tools | hybrid
    "context_tokens": 800,       # max tokens for L0+L1 prompt block
    "l1_max_drawers": 15,
    "search_n_results": 5,
    "search_max_distance": 0.8,
    "dedup_threshold": 0.35,   # cosine distance; 0.35 ≈ sim 0.65, tuned for BGE-M3 zh+en
    "dedup_scope": "wing",     # "wing" = all rooms in same wing | "room" = same room only
}


class MemPalaceConfig:
    """Parsed config for the MemPalace memory provider."""

    def __init__(self, hermes_home: Optional[str] = None):
        self._cfg: Dict[str, Any] = {}
        self._hermes_home = hermes_home
        self._load()

    # -- public API -----------------------------------------------------------

    @property
    def data_path(self) -> str:
        p = self._cfg.get("data_path", _DEFAULTS["data_path"])
        p = os.path.expanduser(p)
        if self._hermes_home and not os.path.isabs(p):
            p = os.path.join(self._hermes_home, p)
        return p

    @property
    def embedding_model(self) -> str:
        return self._cfg.get("embedding_model", _DEFAULTS["embedding_model"])

    @property
    def compression_ratio(self) -> int:
        return int(self._cfg.get("compression_ratio", _DEFAULTS["compression_ratio"]))

    @property
    def enable_kg(self) -> bool:
        return bool(self._cfg.get("enable_kg", _DEFAULTS["enable_kg"]))

    @property
    def recall_mode(self) -> str:
        return self._cfg.get("recall_mode", _DEFAULTS["recall_mode"])

    @property
    def context_tokens(self) -> int:
        return int(self._cfg.get("context_tokens", _DEFAULTS["context_tokens"]))

    @property
    def l1_max_drawers(self) -> int:
        return int(self._cfg.get("l1_max_drawers", _DEFAULTS["l1_max_drawers"]))

    @property
    def search_n_results(self) -> int:
        return int(self._cfg.get("search_n_results", _DEFAULTS["search_n_results"]))

    @property
    def search_max_distance(self) -> float:
        return float(self._cfg.get("search_max_distance", _DEFAULTS["search_max_distance"]))

    @property
    def dedup_threshold(self) -> float:
        return float(self._cfg.get("dedup_threshold", _DEFAULTS["dedup_threshold"]))

    @property
    def dedup_scope(self) -> str:
        val = self._cfg.get("dedup_scope", _DEFAULTS["dedup_scope"])
        return val if val in ("wing", "room") else _DEFAULTS["dedup_scope"]

    @property
    def ollama_url(self) -> Optional[str]:
        return self._cfg.get("ollama_url")

    # -- internal -------------------------------------------------------------

    def _load(self) -> None:
        """Read from hermes config.yaml → memory.mempalace section."""
        try:
            import yaml
            cfg_path = self._resolve_config_path()
            if cfg_path and cfg_path.exists():
                with open(cfg_path) as f:
                    root = yaml.safe_load(f) or {}
                self._cfg = root.get("memory", {}).get("mempalace", {})
        except Exception:
            self._cfg = {}

    def _resolve_config_path(self) -> Optional[Path]:
        if self._hermes_home:
            return Path(self._hermes_home) / "config.yaml"
        home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        return Path(home) / "config.yaml"

    def raw(self) -> Dict[str, Any]:
        """Return the raw config dict."""
        return dict(self._cfg)

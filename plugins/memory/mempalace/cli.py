"""
CLI for MemPalace memory plugin: ``hermes mp <command>``

Commands:
    hermes mp status     — Show palace status (drawers, wings, KG stats)
    hermes mp search     — Semantic search
    hermes mp wings      — List wings with drawer counts
    hermes mp init       — Initialize palace directory
"""

from __future__ import annotations

import argparse
import json
import sys


def register_cli(subparsers) -> None:
    """Register ``hermes mp`` subcommand."""
    parser = subparsers.add_parser("mp", help="MemPalace memory plugin")
    sub = parser.add_subparsers(dest="mp_command")

    # status
    sub.add_parser("status", help="Show palace status")

    # search
    s = sub.add_parser("search", help="Semantic search")
    s.add_argument("query", help="Search query")
    s.add_argument("--wing", "-w", help="Filter by wing")
    s.add_argument("--room", "-r", help="Filter by room")
    s.add_argument("-n", "--n-results", type=int, default=5, help="Number of results")

    # wings
    sub.add_parser("wings", help="List wings with drawer counts")

    # init
    sub.add_parser("init", help="Initialize palace directory")


def mp_command(args) -> None:
    """Handle ``hermes mp`` commands."""
    cmd = getattr(args, "mp_command", None)
    if not cmd:
        print("Usage: hermes mp {status,search,wings,init}")
        return

    if cmd == "status":
        _cmd_status()
    elif cmd == "search":
        _cmd_search(args.query, wing=args.wing, room=args.room, n_results=args.n_results)
    elif cmd == "wings":
        _cmd_wings()
    elif cmd == "init":
        _cmd_init()
    else:
        print(f"Unknown command: {cmd}")


def _get_config():
    from plugins.memory.mempalace.config import MemPalaceConfig
    return MemPalaceConfig()


def _cmd_status() -> None:
    cfg = _get_config()
    print(f"Palace path: {cfg.data_path}")
    print(f"Embedding model: {cfg.embedding_model}")
    print(f"KG enabled: {cfg.enable_kg}")
    print(f"Recall mode: {cfg.recall_mode}")

    try:
        from mempalace.palace import get_collection
        col = get_collection(cfg.data_path, create=False)
        all_data = col.get(include=["metadatas"])
        total = len(all_data["ids"])
        print(f"Total drawers: {total}")

        wings = {}
        for meta in all_data.get("metadatas", []):
            if meta:
                w = meta.get("wing", "none")
                wings[w] = wings.get(w, 0) + 1
        if wings:
            print("Wings:")
            for w, c in sorted(wings.items(), key=lambda x: -x[1]):
                print(f"  {w}: {c} drawers")
    except Exception as e:
        print(f"No palace data yet: {e}")


def _cmd_search(query: str, wing: str = None, room: str = None, n_results: int = 5) -> None:
    cfg = _get_config()
    try:
        from mempalace.searcher import search_memories
        result = search_memories(
            query=query,
            palace_path=cfg.data_path,
            wing=wing,
            room=room,
            n_results=n_results,
        )
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(f"Search failed: {e}")


def _cmd_wings() -> None:
    cfg = _get_config()
    try:
        from mempalace.palace import get_collection
        col = get_collection(cfg.data_path, create=False)
        all_data = col.get(include=["metadatas"])
        wings = {}
        for meta in all_data.get("metadatas", []):
            if meta:
                w = meta.get("wing", "none")
                wings[w] = wings.get(w, 0) + 1
        for w, c in sorted(wings.items(), key=lambda x: -x[1]):
            print(f"  {w}: {c}")
    except Exception as e:
        print(f"No palace data yet: {e}")


def _cmd_init() -> None:
    import os
    cfg = _get_config()
    os.makedirs(cfg.data_path, exist_ok=True)
    print(f"Palace initialized at {cfg.data_path}")
    # Create identity file if not exists
    identity_path = os.path.join(cfg.data_path, "identity.txt")
    if not os.path.exists(identity_path):
        with open(identity_path, "w") as f:
            f.write("# MemPalace Identity — edit to describe who you are\n")
        print(f"Identity template created at {identity_path}")

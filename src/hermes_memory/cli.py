"""Command line used by the plugin's hooks and slash commands (always user-initiated)."""

from __future__ import annotations

import argparse
import json
import sys

from . import store


def _print(text: str) -> None:
    # Windows consoles default to cp1252; the notebook holds ₹, § and em dashes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(text)


def _pending_id(value: str) -> str:
    cleaned = value.strip().lstrip("#")
    if cleaned == "all" or cleaned.isdigit():
        return cleaned
    raise argparse.ArgumentTypeError("use a pending id like 3, or 'all'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("session-start", help="SessionStart hook: emit the notebook snapshot")
    sub.add_parser("show", help="print both stores with usage")
    sub.add_parser("pending", help="list memories waiting for approval")
    for name in ("approve", "reject"):
        sub.add_parser(name).add_argument("which", type=_pending_id, help="pending id, or 'all'")
    sub.add_parser("approval").add_argument("mode", nargs="?")
    args = parser.parse_args(argv)
    home = store.default_home()

    if args.cmd == "session-start":
        _print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                  "additionalContext": store.render_snapshot(home)}}))
        return 0
    if args.cmd == "show":
        _print(f"{store.render_block(home, 'user')}\n\n{store.render_block(home, 'memory')}\n\n"
               f"approval mode: {store.load_config(home)['approval']} · files in {home}")
        return 0
    if args.cmd == "pending":
        _print(store.render_pending(home))
        return 0
    if args.cmd in ("approve", "reject"):
        res = (store.approve if args.cmd == "approve" else store.reject)(home, args.which)
        if res["success"]:
            ids = res.get("approved") or res.get("rejected") or []
            _print(f"{'Approved' if args.cmd == 'approve' else 'Rejected'}: {', '.join(f'#{i}' for i in ids)}")
            return 0
        done = res.get("approved") or []
        prefix = f"Approved: {', '.join(f'#{i}' for i in done)}\n" if done else ""
        _print(f"{prefix}Not done: {res['error']}")
        return 1
    if args.cmd == "approval":
        if args.mode is None:
            _print(f"approval mode: {store.load_config(home)['approval']} (off | auto | on)")
            return 0
        try:
            store.set_approval(home, args.mode)
        except ValueError as exc:
            _print(str(exc))
            return 1
        _print(f"approval mode set to {args.mode}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

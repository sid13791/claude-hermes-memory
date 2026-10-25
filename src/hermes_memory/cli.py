"""Command line used by the plugin's hooks and slash commands (always user-initiated)."""

from __future__ import annotations

import argparse
import json
import sys

from . import doctor, history, hooks, store


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("session-start", help="SessionStart hook: emit the notebook snapshot")
    sub.add_parser("show", help="print both stores with usage")
    sub.add_parser("pending", help="list memories waiting for approval")
    for name in ("approve", "reject"):
        sub.add_parser(name).add_argument("which", type=_pending_id, help="pending id, or 'all'")
    sub.add_parser("approval").add_argument("mode", nargs="?")
    sub.add_parser("log", help="recent memory activity").add_argument("limit", type=int, nargs="?", default=20)
    sub.add_parser("doctor", help="health check")
    sub.add_parser("hook").add_argument("event", choices=["post-tool"])
    return parser


def _approve_or_reject(cmd: str, which: str) -> int:
    home = store.default_home()
    res = (store.approve if cmd == "approve" else store.reject)(home, which)
    if res["success"]:
        ids = res.get("approved") or res.get("rejected") or []
        _print(f"{'Approved' if cmd == 'approve' else 'Rejected'}: {', '.join(f'#{i}' for i in ids)}")
        return 0
    done = res.get("approved") or []
    prefix = f"Approved: {', '.join(f'#{i}' for i in done)}\n" if done else ""
    _print(f"{prefix}Not done: {res['error']}")
    return 1


def _approval(mode: str | None) -> int:
    home = store.default_home()
    if mode is None:
        _print(f"approval mode: {store.load_config(home)['approval']} (off | auto | on)")
        return 0
    try:
        store.set_approval(home, mode)
    except ValueError as exc:
        _print(str(exc))
        return 1
    _print(f"approval mode set to {mode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    home = store.default_home()

    if args.cmd == "session-start":
        _print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                  "additionalContext": store.render_snapshot(home)}}))
    elif args.cmd == "show":
        _print(f"{store.render_block(home, 'user')}\n\n{store.render_block(home, 'memory')}\n\n"
               f"approval mode: {store.load_config(home)['approval']} · files in {home}")
    elif args.cmd == "pending":
        _print(store.render_pending(home))
    elif args.cmd in ("approve", "reject"):
        return _approve_or_reject(args.cmd, args.which)
    elif args.cmd == "approval":
        return _approval(args.mode)
    elif args.cmd == "log":
        _print(history.render(home, args.limit))
    elif args.cmd == "doctor":
        ok, report = doctor.run(home)
        _print(report)
        return 0 if ok else 1
    elif args.cmd == "hook":
        out = hooks.post_tool(sys.stdin.read())
        if out:
            _print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line used by the plugin's hooks and slash commands (always user-initiated)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import archive, doctor, history, hooks, learning, okf, skills, store


def _print(text: str) -> None:
    # Windows consoles default to cp1252; the notebook holds ₹, § and em dashes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(text)


def _read_stdin() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:  # already a text stream (tests, some shells)
        return sys.stdin.read()
    return buffer.read().decode("utf-8", errors="replace")


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
    sub.add_parser("hook").add_argument("event", choices=["post-tool", "stop", "prompt"])
    recall = sub.add_parser("recall", help="search past conversations")
    recall.add_argument("query", nargs="+")
    recall.add_argument("--project", default="")
    sub.add_parser("archive", help="index every Claude Code transcript into the archive")
    sub.add_parser("skills", help="list skills created by hermes-memory")
    sub.add_parser("learn", help="show or set the learning-nudge interval").add_argument("every", nargs="?")
    sub.add_parser("okf", help="list / add / remove OKF bundles").add_argument("args", nargs="?", default="")
    sub.add_parser("okf-export", help="export memory + skills as an OKF bundle").add_argument("folder")
    return parser


def _okf(raw: str) -> int:
    """`okf`, `okf add <folder>`, `okf remove <folder>` (one quoted argument from the slash command)."""
    home = store.default_home()
    action, _, rest = (raw or "").strip().partition(" ")
    if action in ("add", "remove") and rest.strip():
        if action == "add":
            try:
                _print(f"Added OKF bundle {okf.add_bundle(home, rest.strip())}")
            except okf.OKFError as exc:
                _print(f"Not added: {exc}")
                return 1
        else:
            _print("Removed." if okf.remove_bundle(home, rest.strip()) else "That folder was not configured.")
        return 0
    if action and action not in ("list",):
        _print("use: /memory-okf, /memory-okf add <folder>, or /memory-okf remove <folder>")
        return 1
    _print(okf.render_bundles(home))
    return 0


def _learn(every: str | None) -> int:
    home = store.default_home()
    every = (every or "").strip()
    if not every:  # slash commands pass '' when no argument was typed
        current = store.load_config(home).get("learn_every", 0)
        _print(f"learning check: {'off' if not current else f'every {current} prompts'}")
        return 0
    if not every.isdigit():
        _print("use a whole number of prompts, e.g. 6 (0 turns the check off)")
        return 1
    store.set_learn_every(home, int(every))
    _print(f"learning check set to {'off' if int(every) == 0 else f'every {int(every)} prompts'}")
    return 0


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
    mode = (mode or "").strip()
    if not mode:  # slash commands pass '' when no argument was typed
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
        _safe_sweep(home, budget_seconds=3)
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
    elif args.cmd == "learn":
        return _learn(args.every)
    elif args.cmd == "okf":
        return _okf(args.args)
    elif args.cmd == "okf-export":
        try:
            result = okf.export(home, Path(args.folder.strip()))
        except okf.OKFError as exc:
            _print(f"Not exported: {exc}")
            return 1
        _print(f"Exported {result['concepts']} OKF pages to {result['path']}")
    elif args.cmd == "log":
        _print(history.render(home, args.limit))
    elif args.cmd == "doctor":
        ok, report = doctor.run(home)
        _print(report)
        return 0 if ok else 1
    elif args.cmd == "hook":
        # Claude Code sends UTF-8; Windows would otherwise decode stdin as cp1252.
        return _hook(args.event, _read_stdin())
    elif args.cmd == "recall":
        _safe_sweep(home, budget_seconds=5)
        _print(archive.render_hits(archive.search(home, " ".join(args.query), limit=10, project=args.project)))
    elif args.cmd == "skills":
        _print(skills.render(home))
    elif args.cmd == "archive":
        result = archive.sweep(home)
        totals = archive.stats(home)
        _print(f"indexed {result['messages']} new messages from {result['files']} transcript files\n"
               f"archive now holds sessions: {totals['sessions']}, messages: {totals['messages']}, "
               f"oldest: {totals['oldest'] or '-'}")
    return 0


def _safe_sweep(home, budget_seconds: float) -> None:
    """Archiving is best-effort: it must never stop the notebook from loading."""
    try:
        archive.sweep(home, budget_seconds=budget_seconds)
    except Exception as exc:  # noqa: BLE001 - surfaced in /memory-doctor, never fatal here
        history.record(home, "archive-error", reason=str(exc)[:200])


def _hook(event: str, stdin_text: str) -> int:
    if event == "post-tool":
        try:
            out = hooks.post_tool(stdin_text)
            if out:
                _print(out)
        except Exception as exc:  # noqa: BLE001 - a notice must never break the session
            history.record(store.default_home(), "hook-error", reason=str(exc)[:200])
        return 0
    if event == "prompt":
        try:
            payload = json.loads(stdin_text)
            result = learning.handle_prompt(store.default_home(), payload) if isinstance(payload, dict) else {}
            if result:
                _print(json.dumps(result, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - a hook must never block the user's prompt
            history.record(store.default_home(), "hook-error", reason=str(exc)[:200])
        return 0
    path = hooks.transcript_path(stdin_text)
    if path:
        home = store.default_home()
        try:
            archive.index_file(home, path)
        except Exception as exc:  # noqa: BLE001 - a hook must never break the session
            history.record(home, "archive-error", reason=str(exc)[:200])
        try:
            learning.handle_stop(home, hooks.session_id(stdin_text), archive.last_assistant_text(path))
        except Exception as exc:  # noqa: BLE001 - a hook must never break the session
            history.record(home, "hook-error", reason=str(exc)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

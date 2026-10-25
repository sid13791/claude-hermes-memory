"""In-chat yes/no approval (UserPromptSubmit + Stop hooks) and the periodic learning nudge.

Flow: the memory tool stages a note and tells Claude to end its reply with a question. When Claude
finishes, the Stop hook reads that reply from the transcript and *arms* the note for this session, but
only if the question really is the reply's last line. The user's next prompt in the same session, within
30 minutes, answers it. Approval is decided from the user's typed prompt, which the model cannot write.

Residual risk (documented in the README): a bare "yes" answers the armed question even if the user
meant something else in the same reply. Skills are therefore never approvable from chat.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import store

_YES = {"yes", "y", "yes please", "yep", "yeah", "ok", "okay", "sure", "save", "save it", "approve",
        "approved", "go ahead", "haan", "han", "ha", "haan save", "theek hai", "correct"}
_NO = {"no", "n", "nope", "nah", "dont save", "do not save", "dont", "reject", "discard", "skip", "nahi",
       "na", "mat karo", "wrong"}
_REPLY_WORDS = r"(yes|yeah|yep|ok|okay|sure|haan|no|nope|nah|nahi)"
# A correction is a yes/no followed by a qualifier, or an explicit edit instruction.
_CHANGE = re.compile(rf"^{_REPLY_WORDS}\s*[,:;-]?\s*(but|except|only|make it|change|instead|rather)\b"
                     r"|^(change|edit|instead|make it|save it as|save as|rather)\b", re.IGNORECASE)
SESSIONS_FILE = "sessions.json"


def _normalise(text: str) -> str:
    """Lowercase, drop apostrophes (so "don't" == "dont"), turn other punctuation into spaces."""
    text = text.replace("’", "'").replace("'", "").lower()
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def classify(prompt: str) -> tuple[str, str]:
    """Return (kind, text) where kind is yes | no | change | other."""
    norm = _normalise(prompt)
    if norm in _YES:
        return "yes", prompt
    if norm in _NO:
        return "no", prompt
    if _CHANGE.match(prompt.strip().replace("’", "'")):
        return "change", prompt.strip()
    return "other", prompt


def _output(context: str = "", notice: str = "") -> dict:
    out: dict = {}
    if context:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": context}
    if notice:
        out["systemMessage"] = notice
    return out


def _next_question(home: Path) -> str:
    nxt = store.promote_next(home)
    if nxt is None:
        left = len(store.list_pending(home))
        return f" {left} more waiting in /memory-pending." if left else ""
    question = store.question_for(nxt["id"], nxt["target"], nxt["operations"])
    return (" After answering the user's message, end your reply with exactly this question as its own last "
            f"line: {question}")


def _answer(home: Path, item: dict, kind: str, text: str) -> dict:
    sid = item["id"]
    if kind == "yes":
        res = store.approve(home, str(sid))
        if res["success"]:
            return _output(f"[hermes-memory] The user approved #{sid}; it is now saved.{_next_question(home)} "
                           "Acknowledge in a few words, then continue with their request.", f"✅ Memory #{sid} saved")
        return _output(f"[hermes-memory] #{sid} could not be saved: {res['error']}", f"⚠ Memory #{sid} not saved")
    if kind == "no":
        store.reject(home, str(sid))
        return _output(f"[hermes-memory] The user declined #{sid}; it was discarded.{_next_question(home)}",
                       f"🗑 Memory #{sid} discarded")
    store.reject(home, str(sid))
    return _output(f"[hermes-memory] The user wants #{sid} changed: {json.dumps(text, ensure_ascii=False)}. "
                   f"#{sid} was discarded. Call the memory tool again with the corrected content; it will ask "
                   "for approval again.", f"✏ Memory #{sid} being revised")


def _nudge_due(home: Path, session_id: str) -> bool:
    every = store.load_config(home).get("learn_every", 0)
    if not every:
        return False
    path = home / SESSIONS_FILE
    with store.locked(home):
        try:
            counts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            counts = {}
        counts = counts if isinstance(counts, dict) else {}
        count = int(counts.pop(session_id, 0)) + 1
        counts[session_id] = count  # re-inserted last, so the oldest sessions are trimmed first
        store.write_text(path, json.dumps(dict(list(counts.items())[-200:])))
    return count % every == 0


_NUDGE = ("[hermes-memory] Learning check: if the recent turns taught you something durable — a preference, a "
          "correction, an environment quirk, or a reusable multi-step procedure — save it now (memory tool for "
          "facts, skill tool for procedures) before answering. If nothing qualifies, ignore this silently.")


def handle_prompt(home: Path, payload: dict) -> dict:
    raw = payload.get("prompt")
    prompt = raw if isinstance(raw, str) else ""
    session = str(payload.get("session_id") or "")
    item = store.armed_item(home, session) if session else None
    if item is not None:
        kind, text = classify(prompt)
        if kind != "other":
            return _answer(home, item, kind, text)
        store.disarm(home, item["id"])  # they moved on: a later bare "yes" must not approve it
    if _nudge_due(home, session or "unknown"):
        return _output(_NUDGE)
    return {}


def handle_stop(home: Path, session_id: str, last_reply: str) -> None:
    """Arm the asked note if Claude's reply really ended with its question."""
    if session_id and last_reply:
        store.arm(home, session_id, last_reply)

"""Prefix affinity: a conversation's follow-up requests go back to the copy that served it, while that copy has a free slot.

LM Studio's engines keep a prompt cache per loaded instance (the MLX engine's on disk, in 256-token chunks, for the
instance's lifetime; llama.cpp's in its slots), so a request whose prompt begins like an earlier one on the same instance
skips that prefix's prompt processing. Sent to another copy, the same prefix is processed again and, on MLX, written to
that copy's cache store too. Measured on one day of agent batches before this rule: 24% of follow-ups landed on another
copy than their previous turn.

The key is a digest of the request's opening: the system prompt and instructions, any leading system or developer
messages, and the first other message (for a prompt given as one string, its first PREFIX_CHARS characters), with the
model asked for. It names one conversation: every turn of an agent loop resends the same opening. Only digests and copy
names are kept, in this process's memory (never on disk, never in a record), at most SIZE of them, each for TTL seconds.

order() puts the remembered copy first among the copies a request may take; slot_any then takes the first with a free
slot, so a busy preferred copy costs nothing: the request goes where it would have gone without this rule."""
import hashlib, json, threading, time
from collections import OrderedDict

PREFIX_CHARS = 8192        # of a prompt given as one string: enough to tell conversations apart, little to hash
SIZE = 4096                # conversations remembered, least recently used dropped first
TTL = 3600.0               # seconds: about the lifetime of an instance between recycles, so of its prompt cache

_lock = threading.Lock()
_seen = OrderedDict()      # key -> (copy, at)


def _opening(j):
    """The parts of a request body that open its prompt, in order, or [] when it has none."""
    out = [j[k] for k in ('system', 'instructions') if j.get(k)]       # Anthropic's system; Responses' instructions
    for k in ('messages', 'input', 'prompt'):                          # chat and Anthropic; Responses; completions
        v = j.get(k)
        if isinstance(v, str):
            out.append(v[:PREFIX_CHARS]); break
        if isinstance(v, list) and v:
            for m in v:
                out.append(m.get('content') if isinstance(m, dict) else m)
                if not (isinstance(m, dict) and m.get('role') in ('system', 'developer')): break
            break
    return out


def key(j, model=None):
    """The conversation's key, or None when the body has no prompt to key on (an embeddings call, an empty chat)."""
    if not isinstance(j, dict): return None
    head = _opening(j)
    if not head or not any(head): return None
    try: text = json.dumps([model or j.get('model'), head], sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError): return None
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:24]


def preferred(k):
    """The copy that last answered this conversation, while it is remembered, or None."""
    if not k: return None
    with _lock:
        got = _seen.get(k)
        if not got: return None
        if time.time() - got[1] > TTL:
            _seen.pop(k, None); return None
        return got[0]


def order(k, copies):
    """The copies with the conversation's own copy first (when it is one of them); otherwise as given."""
    want = preferred(k)
    if not want or want not in copies or copies[0] == want: return copies
    return [want] + [c for c in copies if c != want]


def remember(k, copy):
    """This copy answered this conversation: its next turn is sent there first."""
    if not k or not copy: return
    with _lock:
        _seen[k] = (copy, time.time()); _seen.move_to_end(k)
        while len(_seen) > SIZE: _seen.popitem(last=False)

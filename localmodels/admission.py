"""admission - a request is admitted to an instance by the context it brings as well as by a free slot, so several long
conversations at once never take an instance, or the Mac, past its memory.

Why: a slot bounds how many requests an instance runs, not how much context it holds. LM Studio's MLX engine keeps a KV
cache for every conversation it answers (Qwen3.6-35B-A3B keeps one in 10 of its 40 layers: 2 KV heads x 256 x K and V x
bf16 = 20 KiB a token), materializes attention scores while it reads a prompt (16 heads x bf16 x its 2,048-token step = 64
KiB per token of context), and keeps the buffers it frees in a cache of its own, which another engine process cannot
reclaim; each engine plans its prompts against the whole GPU limit, unaware of the other. Measured (GUIDE.md, Crash
recovery): an instance died with "[METAL] Command buffer execution failed: Insufficient Memory
(kIOGPUCommandBufferCallbackErrorOutOfMemory)" while it held some 110,000 tokens of agent conversations and its sibling,
back from a recycle, read 70,000 more, with the Mac's memory in use at 124.4 to 124.7 of its 128 GiB for the two minutes
before; under a batch of agent conversations an engine held 3 GiB beyond its weights with nothing in flight and about
117 KiB more per token of context in flight (the median; the KV cache, the attention scores and what its cache keeps of
them), with peaks some 6 GiB above that while it read a long prompt.

The practice is KV-cache-aware admission: vLLM's scheduler starts a request only while its KV-cache blocks fit, and
text-generation-inference bounds the tokens a batch may hold (max_batch_total_tokens). Here:
  estimate(body, j)    a request's context before LM Studio reads it, in tokens: its prompt from the size of its body, at
                       the bytes per token measured on this Mac's recent requests (each request's row keeps `bytes` and
                       `prompt_tokens`), plus what it may write (its max_tokens, at most ANSWER counted).
  budget(copy)         the context an instance may hold in flight: the family's "admit": {"tokens": N} in models.json, or by
                       default its share of what this Mac has for its models (its memory less what other programs hold,
                       measured now, and headroom_gb; or the GPU's limit, whichever is less), less its weights and, when
                       several instances are loaded, the KEPT cache, at per_token() bytes a token: never below LEAST,
                       never above all its slots' context windows. "admit": false turns it off.
  fits(copy, load, need)   an instance with nothing in flight takes any request, so every request the context window
                       allows can run; a busy one takes it while its load plus this request stays within its budget and,
                       while the Mac's free memory, less what this one will take (its tokens at per_token()), stays
                       above headroom_gb. A peer's
                       instance is admitted by the room that peer reported (peers.Link.ctx_fits), and again by its own
                       gateway.
llmshare.slot_any applies fits under one lock with the slot it takes and keeps each instance's load, so two requests never
both take the last room. A request that fits no busy instance waits while shorter ones behind it take the room there is
(backfilling, as batch schedulers do), and after llmshare.RESERVE seconds it keeps its place on every instance it wants
until one has room (a reservation), so it is never starved. The gateway writes each request's estimate in its in-flight
marker (`ctx`), which recycle.plan adds to an instance's count, so a drain starts before the requests in flight carry
the instance past its threshold.
"""
import ctypes, ctypes.util, glob, json, os, re, subprocess, threading, time

from . import llmshare

ANSWER = 4_096            # tokens of answer counted per request: its max_tokens, at most this
LEAST = 16_384            # tokens: the smallest budget, so a few short requests always run side by side
RATIO = 3.0               # bytes of request body per prompt token until this Mac's own requests say (real ones: 3.4 to 4.5)
PER_TOKEN = 120 * 1024    # bytes a token of context in flight takes on an instance, when its model's shape is unknown (measured)
CACHE_SHARE = 1.4         # what an engine holds per token in flight, over its KV cache and attention scores (measured: 117 of 84 KiB)
STEP = 2_048              # tokens LM Studio's MLX engine reads of a prompt at a time (its prefill step)
KEPT = 3.0                # GiB an engine holds beyond its weights with nothing in flight (measured median), counted beside a sibling
OTHER_GPU = 2.0           # GiB of the GPU's limit other programs hold (the window server, browsers; measured 1.8)
OTHERS = 16.0             # GiB other programs hold, when the engines' own footprints cannot be read
INLINE = re.compile(rb'data:[\w/.+-]{1,80};base64,[A-Za-z0-9+/=]{2000,}')   # an image or file sent inline
MODELS_DIR = os.path.expanduser(os.environ.get('LMSTUDIO_MODELS') or '~/.lmstudio/models')
_ratio = {'at': 0.0, 'v': RATIO, 'n': 0}
_free, _others = {'at': 0.0, 'v': None}, {'at': 0.0, 'v': None}
_budgets, _shapes = {}, {}                                # copy -> (when, tokens), computed at most every 10 s; model -> per-token bytes
_lock = threading.Lock()


# ---------------------------------------------------------------- a request's size
def prompt_bytes(body):
    """The bytes of a request body that carry its prompt: an image or file sent inline (base64) counts 4,000 bytes, about
    what a model reads of one, not its encoding."""
    body = body or b''
    return len(INLINE.sub(b'x' * 4000, body)) if b';base64,' in body else len(body)


def ratio():
    """Bytes of request body per prompt token on this Mac: the lower quartile over its last 200 answered requests of 500
    prompt tokens or more (each row keeps both), read at most once a minute, so most estimates come out a little high,
    never low; RATIO until 20 such requests are on record."""
    with _lock:
        if time.time() - _ratio['at'] < 60: return _ratio['v']
        _ratio['at'] = time.time()
    rows = [r for r in llmshare.requests_log(6) if r.get('bytes') and (r.get('prompt_tokens') or 0) >= 500 and r.get('status') == 200][-200:]
    v = RATIO
    if len(rows) >= 20:
        xs = sorted(r['bytes'] / r['prompt_tokens'] for r in rows)
        v = max(1.5, min(8.0, xs[len(xs) // 4]))
    with _lock: _ratio.update(v=round(v, 3), n=len(rows))
    return _ratio['v']


def estimate(body, j=None, window=0, answer=True):
    """A request's context in tokens before LM Studio reads it: its prompt (the body's bytes at this Mac's bytes per token)
    and what it may write (max_tokens, max_completion_tokens or max_output_tokens, at most ANSWER; ANSWER when unsaid;
    nothing for answer=False, an embedding), at most one context window when it is known."""
    j = j if isinstance(j, dict) else {}
    out = next((j[k] for k in ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if j.get(k) is not None), None)
    try: out = int(out)
    except (TypeError, ValueError): out = ANSWER
    n = int(prompt_bytes(body) / ratio()) + ((min(out, ANSWER) if out > 0 else ANSWER) if answer else 0)
    return min(n, int(window)) if window else n


def window(model):
    """A model's context window in tokens: its family's "context" in models.json, else what the listing says it was loaded
    with, else 0 (unknown)."""
    f = (llmshare.settings().get('families') or {}).get(llmshare.family(model))
    if isinstance(f, dict) and f.get('context'):
        try: return int(f['context'])
        except (TypeError, ValueError): pass
    row = next((r for r in llmshare.listed() if llmshare.family(r['id']) == llmshare.family(model)), None)
    return int((row or {}).get('context') or 0)


# ---------------------------------------------------------------- what a token costs, and what the Mac has
def shape(config):
    """A model's memory per token from its config.json: (KV-cache bytes per token: K and V of every layer with full
    attention, at 2 bytes; attention-score bytes per token of context while a prompt is read: heads x 2 bytes x STEP),
    or None when the config does not say."""
    c = config.get('text_config') if isinstance(config.get('text_config'), dict) else config
    try:
        layers = int(c['num_hidden_layers'])
        heads = int(c['num_attention_heads'])
        kv_heads = int(c.get('num_key_value_heads') or heads)
        head_dim = int(c.get('head_dim') or int(c['hidden_size']) // heads)
    except (KeyError, TypeError, ValueError, ZeroDivisionError): return None
    kinds = c.get('layer_types')
    full = sum(1 for k in kinds if 'full' in str(k)) if isinstance(kinds, list) and kinds else layers
    return 2 * kv_heads * head_dim * full * 2, heads * 2 * STEP


def per_token(row):
    """Bytes of memory a token of context in flight takes on this instance: its model's KV cache and attention scores
    (from its config.json under LM Studio's models folder, found by the model's name and loaded variant) times
    CACHE_SHARE; PER_TOKEN when its config cannot be found. (Qwen3.6-35B-A3B: 20 + 64 KiB x 1.4 = 118 KiB.)"""
    key, variant = str(row.get('key') or row.get('id') or ''), str(row.get('variant') or '')
    if (key, variant) in _shapes: return _shapes[(key, variant)]
    name, quant = key.split('/')[-1].lower(), variant.split('@')[-1].lower() if '@' in variant else ''
    v = PER_TOKEN
    for p in sorted(glob.glob(os.path.join(MODELS_DIR, '*', '*', 'config.json'))):
        folder = os.path.basename(os.path.dirname(p)).lower()
        if name and name in folder and (not quant or quant in folder):
            try: s = shape(json.load(open(p)))
            except (OSError, ValueError): s = None
            if s: v = int(sum(s) * CACHE_SHARE); break
    _shapes[(key, variant)] = v
    return v


_libc = None


def _footprint(pid):
    """A process's physical footprint in bytes (proc_pid_rusage, which reads it without stopping the process), or None."""
    global _libc
    if _libc is None: _libc = ctypes.CDLL(ctypes.util.find_library('c'))
    buf = ctypes.create_string_buffer(512)
    if _libc.proc_pid_rusage(int(pid), 4, buf) != 0: return None
    return int.from_bytes(buf.raw[16 + 8 * 7:24 + 8 * 7], 'little')     # ri_phys_footprint


def engines():
    """LM Studio's model processes on this Mac (its workers under ~/.lmstudio that hold 2 GiB or more): {pid: footprint bytes}."""
    home, out = os.path.expanduser('~/.lmstudio'), {}
    try: lines = subprocess.run(['ps', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=10).stdout.splitlines()
    except (OSError, subprocess.SubprocessError): return out
    for line in lines:
        p = line.strip().split(None, 1)
        if len(p) < 2 or not p[1].startswith(home): continue
        try: f = _footprint(p[0])
        except (OSError, ValueError, AttributeError): f = None
        if f and f >= 2 * 2**30: out[int(p[0])] = f
    return out


def others_gib():
    """What programs other than LM Studio's models hold on this Mac, in GiB: memory in use less the engines' footprints
    (read at most every 10 s); OTHERS when no engine can be read."""
    with _lock:
        if time.time() - _others['at'] < 10 and _others['v'] is not None: return _others['v']
        _others['at'] = time.time()
    from . import dash
    try:
        used, _ = dash.memory()
        e = engines()
        v = max(0.0, used - sum(e.values()) / 2**30) if e else OTHERS
    except Exception as x: v = OTHERS; llmshare._error('admission: memory of other programs', x)
    with _lock: _others['v'] = round(v, 1)
    return _others['v']


def free_gib():
    """Memory not held by programs on this Mac, in GiB (models.free_gb, read at most once a second), or None."""
    with _lock:
        if time.time() - _free['at'] < 1: return _free['v']
        _free['at'] = time.time()
    from . import models
    try: v = models.free_gb()
    except Exception as x: v = None; llmshare._error('admission: free memory', x)
    with _lock: _free['v'] = v
    return v


def headroom():
    """The memory, in GiB, kept free on this Mac (headroom_gb in models.json, 8 by default): the instance manager loads
    nothing into it, and admission leaves it free."""
    try: return float(llmshare.settings().get('headroom_gb', 8))
    except (TypeError, ValueError): return 8.0


# ---------------------------------------------------------------- each instance's budget
def default_budget(copy, rows=None, memory=None):
    """This instance's share of what the Mac has for its models, in tokens of context in flight (module docstring).
    rows: the listing (the last one in hand by default); memory: (total GiB, others GiB, GPU limit GiB), read now by
    default. 0 when it is not loaded."""
    from . import models
    rows = llmshare.listed() if rows is None else rows
    row = next((r for r in rows if r['id'] == copy), None)
    if not row: return 0
    win, slots = int(row.get('context') or 65536), int(row.get('parallel') or 1)
    if memory is None:
        g = models.gpu()
        memory = (g.get('total_gb') or models.mem_gb(), others_gib(), g.get('limit_gb') or 0)
    total, others, limit = memory
    n = max(1, len(rows))
    shares = [(total - others - headroom()) / n] if total else []
    if limit: shares.append((limit - OTHER_GPU) / n)
    if not shares: return win * slots
    room = min(shares) - models.gb(row.get('size')) - (KEPT if n > 1 else 0)
    return min(win * slots, max(LEAST, int(room * 2**30 / per_token(row))))   # all its slots' windows bound it, LEAST below that


def budget(copy, rows=None):
    """The context, in tokens, this instance may hold in flight at once (module docstring); 0: no budget. Computed at
    most every 10 s, from the last listing in hand (never a new `lms ps`), so it is cheap enough for every look."""
    if ' on ' in str(copy): return 0                       # a peer's instance: that Mac's gateway admits it by its own budget
    f = (llmshare.settings().get('families') or {}).get(llmshare.family(copy))
    a = f.get('admit', True) if isinstance(f, dict) else True
    if a is False or a is None: return 0
    if isinstance(a, dict) and isinstance(a.get('tokens'), (int, float)) and a['tokens'] > 0: return int(a['tokens'])
    if rows is not None: return default_budget(copy, rows)
    hit = _budgets.get(copy)
    if hit and time.time() - hit[0] < 10: return hit[1]
    v = default_budget(copy)
    _budgets[copy] = (time.time(), v)
    return v


def row_of(copy):
    """This instance's row in the last listing in hand, or a bare one."""
    return next((r for r in llmshare.listed() if r['id'] == copy), dict(id=copy, key=llmshare.family(copy)))


def need_gib(copy, need):
    """The memory, in GiB, a request of `need` tokens takes on this instance (per_token of its model)."""
    return need * per_token(row_of(copy)) / 2**30


def fits(copy, load, need):
    """Whether this instance may take a request of `need` tokens now, holding `load` tokens of requests in flight."""
    if ' on ' in str(copy):                                # a peer's instance: by the room that peer reported
        from . import peers
        return peers.LINK.ctx_fits(copy, load, need)
    if load <= 0: return True                              # nothing in flight: it takes it, whatever its size
    b = budget(copy)
    if b and load + need > b: return False
    free = free_gib()
    return free is None or free - need_gib(copy, need) >= headroom()


def why_waiting(copies, loads, need):
    """Why a request of `need` tokens waits for these copies although one may have a slot free, in words (for its
    in-flight marker and the dashboard), or None."""
    for c in copies:
        load = loads.get(c, 0)
        if ' on ' in str(c): return f'context budget on peer {str(c).split(" on ", 1)[1]}: ~{need:,} tokens do not fit the room it reported'
        if load <= 0: continue
        b = budget(c)
        if b and load + need > b: return f'context budget: ~{need:,} tokens + {load:,} in flight > {b:,} on {c}'
        free, mine = free_gib(), need_gib(c, need)
        if free is not None and free - mine < headroom():
            return f'memory headroom: {free:.1f} GiB free; ~{mine:.1f} GiB for this request would leave less than headroom_gb {headroom():g}'
    return None


# ---------------------------------------------------------------- what is in flight, for every process
def in_flight(marks=None):
    """The context in flight per instance, in tokens, from the gateway's in-flight markers (each answering request's `ctx`):
    {copy: n}. Any process can read it (recycle.plan, the dashboard, a peer's report)."""
    out = {}
    for d in (llmshare.inflight() if marks is None else marks):
        if d.get('copy') and d.get('ctx') and d.get('state') == 'answering': out[d['copy']] = out.get(d['copy'], 0) + int(d['ctx'])
    return out


def view(rows=None):
    """Each loaded instance's context in flight against its budget: {copy: {load, budget}} (the dashboard, the status)."""
    rows = llmshare.loaded() if rows is None else rows
    loads = in_flight()
    return {r['id']: dict(load=loads.get(r['id'], 0), budget=budget(r['id'])) for r in rows}

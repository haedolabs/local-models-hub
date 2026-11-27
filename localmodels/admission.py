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
                       several instances are loaded, the KEPT cache, at per_token() bytes a token (with the cache share
                       beside a sibling): never below LEAST, never above all its slots' context windows. "admit": false
                       turns it off. An embedding model's instance ("kind": "embedding", or LM Studio's type) has no budget
                       and takes no share: the chat instances divide the room among themselves.
  fits(copy, load, need)   an instance with nothing in flight takes any request, so every request the context window
                       allows can run; a busy one takes it while its load plus this request stays within its budget and,
                       beside a sibling, while the Mac's free memory, less what this one will take (its tokens at
                       per_token()), stays above headroom_gb (an engine alone fills memory up to the GPU limit with a
                       cache it reclaims itself, so free memory says nothing there). A peer's instance is admitted by
                       the room that peer reported (peers.Link.ctx_fits), and again by its own gateway.
llmshare.slot_any applies fits under one lock with the slot it takes and keeps each instance's load, so two requests never
both take the last room. A request that fits no busy instance waits while shorter ones behind it take the room there is
(backfilling, as batch schedulers do), and after llmshare.RESERVE seconds it keeps its place on every instance it wants
until one has room (a reservation), so it is never starved. The gateway writes each request's estimate in its in-flight
marker (`ctx`), which recycle.plan adds to an instance's count, so a drain starts before the requests in flight carry
the instance past its threshold.
"""
import ctypes, ctypes.util, glob, json, math, os, re, subprocess, threading, time

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
_ratio = {'at': 0.0, 'v': RATIO}
_others = {'at': 0.0, 'v': OTHERS}
_sample, _checked = {}, 0.0
_budgets, _shapes = {}, {}                                # copy -> (when, tokens, had fresh sample); model -> per-token bytes
_lock = threading.Lock()


@llmshare.on_state
def _state_paths(root):
    global SAMPLE, _sample, _checked
    SAMPLE = os.path.join(root, 'run', 'admission.json')
    _sample, _checked = {}, 0.0
    _budgets.clear()
    _ratio.update(at=0.0, v=RATIO)
    _others.update(at=0.0, v=OTHERS)


# ---------------------------------------------------------------- a request's size
def prompt_bytes(body):
    """The bytes of a request body that carry its prompt: an image or file sent inline (base64) counts 4,000 bytes, about
    what a model reads of one, not its encoding."""
    body = body or b''
    return len(INLINE.sub(b'x' * 4000, body)) if b';base64,' in body else len(body)


def measured_ratio(rows):
    """Bytes of request body per prompt token on this Mac: the lower quartile over its last 200 answered requests of 500
    prompt tokens or more (each row keeps both), read at most once a minute, so most estimates come out a little high,
    never low; RATIO until 20 such requests are on record."""
    if len(rows) < 20: return RATIO
    xs = sorted(r['bytes'] / r['prompt_tokens'] for r in rows)
    return round(max(1.5, min(8.0, xs[len(xs) // 4])), 3)


def ratio():
    return host_sample().get('ratio', RATIO)


def ratio_sample(row):
    return (row.get('status') == 200 and isinstance(row.get('bytes'), int) and row['bytes'] > 0
            and isinstance(row.get('prompt_tokens'), int) and row['prompt_tokens'] >= 500)


def estimate(body, j=None, window=0, answer=True):
    """A request's context in tokens before LM Studio reads it: its prompt (the body's bytes at this Mac's bytes per token)
    and what it may write (max_tokens, max_completion_tokens or max_output_tokens, at most ANSWER; ANSWER when unsaid;
    nothing for answer=False, an embedding), at most one context window when it is known."""
    j = j if isinstance(j, dict) else {}
    out = next((j[k] for k in ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if j.get(k) is not None), None)
    try: out = int(out)
    except (TypeError, ValueError, OverflowError): out = ANSWER       # OverflowError: Infinity or 1e999
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


def per_token(row, shared=True):
    """Bytes of memory a token of context in flight takes on this instance: its model's KV cache and attention scores
    (from its config.json under LM Studio's models folder, found by the model's name and loaded variant), times
    CACHE_SHARE when it shares the Mac with another instance (shared: the buffers its engine keeps are then out of the
    other's reach; an engine alone reclaims its own at the GPU limit); PER_TOKEN (shared) when its config cannot be
    found. (Qwen3.6-35B-A3B: 20 + 64 = 84 KiB alone, x 1.4 = 118 KiB beside a sibling.)"""
    key, variant = str(row.get('key') or row.get('id') or ''), str(row.get('variant') or '')
    if (key, variant) not in _shapes:
        name, quant = key.split('/')[-1].lower(), variant.split('@')[-1].lower() if '@' in variant else ''
        v = None
        for p in sorted(glob.glob(os.path.join(MODELS_DIR, '*', '*', 'config.json'))):
            folder = os.path.basename(os.path.dirname(p)).lower()
            if name and name in folder and (not quant or quant in folder):
                try: s = shape(json.load(open(p)))
                except (OSError, ValueError): s = None
                if s: v = sum(s); break
        _shapes[(key, variant)] = v or PER_TOKEN / CACHE_SHARE
    return int(_shapes[(key, variant)] * (CACHE_SHARE if shared else 1))


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


def refresh_sample():
    """The memory-sampler role owns the probes and log calibration. Publish one snapshot for all workers and clients."""
    from . import models
    global _sample, _checked
    gpu = models.gpu()
    used, total = models.memory(gpu.get('total_gb'))
    if not total or not 0 <= used <= total: return {}
    now = time.time()
    if now - _others['at'] >= 10:
        footprints = engines()
        others = max(0.0, used - sum(footprints.values()) / 2**30) if footprints else OTHERS
        _others.update(at=now, v=round(others, 1))
    if now - _ratio['at'] >= 60:
        rows = llmshare.recent_requests(200, 6, ratio_sample)
        _ratio.update(at=now, v=measured_ratio(rows))
    sample = dict(at=now, used=used, total=total, others=_others['v'], limit=gpu.get('limit_gb') or 0, ratio=_ratio['v'])
    os.makedirs(os.path.dirname(SAMPLE), mode=0o700, exist_ok=True)
    llmshare._write(SAMPLE, sample)
    with _lock: _sample, _checked = sample, time.monotonic()
    return sample


def host_sample(max_age=5):
    """The sampler's reading, or {} if unavailable. A display can request the last known reading with max_age=None."""
    global _sample, _checked
    with _lock:
        if time.monotonic() - _checked >= 1:
            try:
                with open(SAMPLE) as f: candidate = json.load(f)
                if (isinstance(candidate, dict) and all(type(candidate.get(k)) in (int, float) and math.isfinite(candidate[k])
                        for k in ('at', 'used', 'total', 'others', 'limit', 'ratio')) and 0 <= candidate['used'] <= candidate['total']
                        and candidate['total'] > 0 and candidate['ratio'] > 0): _sample = candidate
            except (OSError, ValueError, OverflowError): pass
            _checked = time.monotonic()
        age = time.time() - _sample.get('at', 0)
        return _sample if age >= 0 and (max_age is None or age <= max_age) else {}


def free_gib():
    sample = host_sample()
    return sample['total'] - sample['used'] if sample else None


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
    chat = llmshare.chat_rows(rows)                         # an embedding model takes no share (its engine, under 2 GiB, is in others)
    if row not in chat: return win * slots
    if memory is None:
        sample = host_sample()
        if not sample: return min(LEAST, win * slots)
        memory = sample['total'], sample['others'], sample['limit']
    total, others, limit = memory
    n = max(1, len(chat))
    shares = [(total - others - headroom()) / n] if total else []
    if limit: shares.append((limit - OTHER_GPU) / n)
    if not shares: return win * slots
    room = min(shares) - models.gb(row.get('size')) - (KEPT if n > 1 else 0)    # beside a sibling, the cache its engine keeps
    return min(win * slots, max(LEAST, int(room * 2**30 / per_token(row, n > 1))))   # all its slots' windows bound it, LEAST below that


def budget(copy, rows=None):
    """The context, in tokens, this instance may hold in flight at once (module docstring); 0: no budget. Computed at
    most every 10 s, from the last listing in hand (never a new `lms ps`), so it is cheap enough for every look."""
    if ' on ' in str(copy): return 0                       # a peer's instance: that Mac's gateway admits it by its own budget
    f = (llmshare.settings().get('families') or {}).get(llmshare.family(copy))
    a = f.get('admit', True) if isinstance(f, dict) else True
    if a is False or a is None or isinstance(f, dict) and f.get('kind') == 'embedding': return 0   # an embedding: no context to budget
    if isinstance(a, dict) and isinstance(a.get('tokens'), (int, float)) and a['tokens'] > 0: return int(a['tokens'])
    if rows is not None: return default_budget(copy, rows)
    hit = _budgets.get(copy)
    fresh = bool(host_sample())
    if hit and time.time() - hit[0] < 10 and fresh == hit[2]: return hit[1]
    v = default_budget(copy)
    _budgets[copy] = (time.time(), v, fresh)
    return v


def row_of(copy):
    """This instance's row in the last listing in hand, or a bare one."""
    return next((r for r in llmshare.listed() if r['id'] == copy), dict(id=copy, key=llmshare.family(copy)))


def need_gib(copy, need):
    """The memory, in GiB, a request of `need` tokens takes on this instance (per_token of its model)."""
    return need * per_token(row_of(copy), len(llmshare.chat_rows(llmshare.listed())) > 1) / 2**30


def fits(copy, load, need):
    """Whether this instance may take a request of `need` tokens now, holding `load` tokens of requests in flight."""
    if ' on ' in str(copy):                                # a peer's instance: by the room that peer reported
        from . import peers
        return peers.LINK.ctx_fits(copy, load, need)
    if load <= 0: return True                              # nothing in flight: it takes it, whatever its size
    b = budget(copy)
    if b and load + need > b: return False
    if len(llmshare.chat_rows(llmshare.listed())) < 2: return True               # an engine alone reclaims its own cache within the GPU limit
    free = free_gib()
    return free is not None and free - need_gib(copy, need) >= headroom()


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
        if len(llmshare.chat_rows(llmshare.listed())) >= 2 and free is None: return 'waiting for a current memory sample'
        if len(llmshare.chat_rows(llmshare.listed())) > 1 and free is not None and free - mine < headroom():
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

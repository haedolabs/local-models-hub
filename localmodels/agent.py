"""agent - local agents: one well-defined job on the model loaded in LM Studio, with tools that only read, through the hub.

A project gives the engine a job, the tools the agent may use and the folder its logs go to; the engine runs the
conversation (tool calls, context trimming, an answer as JSON matched to a schema), sends every request through the hub,
and leaves one log per job (a batch: one folder, with plan.json, a .running file per job at work, one .json per job done,
and summary.json). Answers are leads that a script or a person checks, never verdicts; the agent never writes. The job
logs hold the prompt and the answer (they are the project's own record of the job); the hub's request records never do.

A job: {"label": "...", "prompt": "...", "schema": {JSON Schema} or null, "max_turns": 16, "max_tokens": 4000,
        "think": true, "think_schema": false, "retry_empty": 1, "nonempty": false, "wait_down": 3600,
        "system": "extra system text", plus whatever the project's own tools read}
"think": false turns the model's thinking off for the whole job: measured on Qwen 3.6, 1.1 to 7 times faster and as right
on lookups and extraction, but it missed a wrong sum in 2 of 3 consistency checks that thinking caught every time. So off
for lookups and extraction, on (the default) for checks and judgment. The last step, the answer as JSON matched to the
schema, runs with thinking off whatever "think" says ("think_schema": true keeps it on): measured, thinking on with a
strict schema gave 9 empty answers out of 16, each in 1.5 s, and put the JSON inside the think block for the rest. An
answer that is empty anyway (every list empty) is asked for once more without the strict format (the schema in the prompt
only), and that answer stands: on real text the strict grammar lets the model close a list at its first token (21 of 32
empty at temperature 0.7, 17 of 32 at 0.3), while without the format 2 of 32 were empty, both rightly. A job whose answer
can never be empty says "nonempty": true and gets minItems: 1 on its lists from the first ask (0 of 32 empty, full
answers). A request that fails because the model is down or LM Studio is not answering is tried again with growing pauses
for up to "wait_down" seconds in all, so a crash in the night costs a job minutes, not its result.
"""
import concurrent.futures as cf, fnmatch, http.client, json, os, re, socket, time, urllib.error

from . import llmshare

RESULT_CAP, HISTORY_CAP = 8000, 80000                 # characters: one tool result, the whole conversation (~20k tokens)
CHARS_PER_TOKEN, CONTEXT_SHARE = 3.5, 0.8             # measured: about 3.6 characters a token (recalibrated from each
                                                      # answer's prompt_tokens); the conversation may use 80% of the loaded context
SAMPLING = dict(temperature=0.7, top_p=0.95, top_k=20, min_p=0)
THINK_OFF = {'reasoning_effort': 'none'}              # LM Studio gives Qwen 3.6 thinking on or off only: 'none' is off;
                                                      # 'low', 'medium' and 'high' all mean on, and 'off' is refused
RETRY_WAITS, WAIT_DOWN = (5, 15, 45, 120, 300), 3600  # pauses between tries when the model is down; the most a job waits in all
TRANSIENT = re.compile(r'crashed|no models? (?:are |is )?loaded|not loaded|model_not_found|unload|not answering|dropped the connection|cut the answer|'
                       r'bringing it back|no free .*slot|loading it again', re.I)
SYSTEM = ("You are a local helper on this Mac. You do one well-defined job with tools that only read, and report "
          "exactly what you found. Check with the tools; never guess. Give evidence as file paths with line numbers. You "
          "cannot change anything. Use plain words. If something is unclear or missing, say so plainly.")
SKIP = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', 'envs', '.wrangler', '.next', 'dist', 'build'}
TEXT = ('.md', '.json', '.txt', '.py', '.sh', '.csv', '.yaml', '.yml', '.toml', '.log', '.js', '.ts', '.html', '.css', '.swift')


# ---------------------------------------------------------------- the model, through the hub
def post(path, body, project, kind='batch', timeout=3600, headers=None):
    return llmshare.post(path, body, project, kind, timeout, headers)


def get(path, timeout=10):
    return llmshare.get(path, timeout)


def loaded_model(want=None, prefer=()):
    """-> (model info, None) or (None, why not). want: this model id, or the first loaded one (those in prefer first)."""
    try: d = get('/api/v0/models')
    except (OSError, ValueError):
        return None, ("LM Studio's server is not answering: start it (LM Studio's Developer tab, or ~/.lmstudio/bin/lms server start)")
    ms = [m for m in d.get('data', []) if m.get('state') == 'loaded' and m.get('type') in ('llm', 'vlm') and (not want or m.get('id') == want)]
    prefer = list(prefer or [])
    ms.sort(key=lambda m: prefer.index(m['id']) if m['id'] in prefer else len(prefer))
    return (ms[0], None) if ms else (None, f'{want} is not loaded in LM Studio' if want else 'no model is loaded in LM Studio')


def context_of(model):
    """The loaded context of a model, in tokens (LM Studio's own figure), or None."""
    try:
        for m in get('/api/v0/models').get('data', []):
            if m.get('id') == model and m.get('state') == 'loaded': return m.get('loaded_context_length')
    except (OSError, ValueError): pass
    return None


def err_text(e):
    """An HTTPError's body, read once and kept on the error (a second read would find it gone)."""
    if not hasattr(e, '_text'):
        try: e._text = e.read().decode('utf-8', 'replace')[:400]
        except Exception: e._text = ''
    return e._text


def transient(e):
    """Whether a failed request is worth trying again (the model is down, the door or LM Studio is not answering) ->
    (yes/no, seconds to wait if the door said, words)."""
    if isinstance(e, urllib.error.HTTPError):
        body = err_text(e)
        ra = None
        try: ra = int(e.headers.get('Retry-After')) if e.headers and e.headers.get('Retry-After') else None
        except ValueError: pass
        if e.code in (429, 502, 503, 504): return True, ra, f'HTTP {e.code}: {body or e.reason}'
        if e.code in (400, 404) and TRANSIENT.search(body): return True, ra, f'HTTP {e.code}: {body}'
        return False, None, f'HTTP {e.code}: {body or e.reason}'
    if isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, http.client.HTTPException)): return True, None, f'{type(e).__name__}: {e}'
    return False, None, f'{type(e).__name__}: {e}'


# ---------------------------------------------------------------- tools that only read files
class Files:
    """read_file, list_files and grep inside these folders and nowhere else. where: the folders, in words, for errors."""
    def __init__(self, roots, base=None, where='the project', skip=SKIP, text=TEXT):
        self.roots = [os.path.realpath(os.path.expanduser(r)) for r in roots]
        self.base, self.where, self.skip, self.text = os.path.realpath(base or self.roots[0]), where, set(skip), tuple(text)

    def rel(self, p):
        return os.path.relpath(p, self.base) if p == self.base or p.startswith(self.base + os.sep) else '~/' + os.path.relpath(p, os.path.expanduser('~'))

    def safe(self, path):
        p = os.path.realpath(os.path.join(self.base, os.path.expanduser(str(path or '.'))))
        return p if any(p == r or p.startswith(r + os.sep) for r in self.roots) else None

    @staticmethod
    def is_text(p):
        try: return os.path.getsize(p) <= 5_000_000 and b'\0' not in open(p, 'rb').read(2048)
        except OSError: return False

    def read_file(self, path, start=1, lines=200):
        p = self.safe(path)
        if not p or not os.path.isfile(p): return dict(ok=False, error=f'no file {path!r} inside {self.where}')
        if not self.is_text(p): return dict(ok=False, error=f'{path!r} is not a text file')
        rows = open(p, errors='replace').read().split('\n')
        start, n = max(1, int(start or 1)), max(1, min(int(lines or 200), 400))
        chunk = rows[start - 1:start - 1 + n]
        out = dict(ok=True, path=self.rel(p), lines=f'{start}-{start + len(chunk) - 1} of {len(rows)}',
                   text='\n'.join(f'{start + i}: {l}' for i, l in enumerate(chunk)))
        if start - 1 + n < len(rows): out['next'] = f'start={start + n}'
        return out

    def list_files(self, path='.', pattern='*'):
        p = self.safe(path)
        if not p or not os.path.isdir(p): return dict(ok=False, error=f'no folder {path!r} inside {self.where}')
        names = sorted(n for n in os.listdir(p) if fnmatch.fnmatch(n, pattern or '*') and not n.startswith('.'))
        rows = [n + ('/' if os.path.isdir(os.path.join(p, n)) else '') for n in names]
        return dict(ok=True, path=self.rel(p), count=len(rows), files=rows[:200] + (['...'] if len(rows) > 200 else []))

    def grep(self, pattern, path='.', limit=40):
        p = self.safe(path)
        if not p or not os.path.exists(p): return dict(ok=False, error=f'no file or folder {path!r} inside {self.where}')
        if len(pattern or '') > 200 or re.search(r'\([^()]*[+*}][^()]*\)\s*[+*{]', pattern or ''):
            return dict(ok=False, error='this pattern can run for hours (a repeated group that itself repeats, or 200+ characters): simplify it')
        try: rx = re.compile(pattern, re.I)
        except re.error as e: return dict(ok=False, error=f'bad pattern: {e}')
        files = [p] if os.path.isfile(p) else []
        for d, ds, fs in os.walk(p) if os.path.isdir(p) else ():
            ds[:] = sorted(x for x in ds if x not in self.skip and not x.startswith('.') and not x.endswith('.logicx') and not os.path.islink(os.path.join(d, x)))
            files += [os.path.join(d, f) for f in sorted(fs) if f.endswith(self.text) and not os.path.islink(os.path.join(d, f))]
        hits, cap, t0 = [], max(1, min(int(limit or 40), 100)), time.time()
        for f in files:
            if time.time() - t0 > 20: return dict(ok=True, hits=hits, more='stopped after 20 s: narrow the path')
            if not self.is_text(f): continue
            for i, line in enumerate(open(f, errors='replace'), 1):
                if i % 500 == 0 and time.time() - t0 > 20: return dict(ok=True, hits=hits, more='stopped after 20 s: narrow the path or the pattern')
                if rx.search(line[:1000]):
                    hits.append(f'{self.rel(f)}:{i}: {line.strip()[:200]}')
                    if len(hits) >= cap: return dict(ok=True, hits=hits, more='stopped at max: narrow the pattern or the path')
        return dict(ok=True, hits=hits)

    def defs(self):
        o = lambda props, req=(): {'type': 'object', 'properties': props, 'required': list(req)}
        return [
            {'name': 'read_file', 'description': f'Read lines of a text file in {self.where}, numbered. start is the first line '
             '(default 1), lines how many (default 200, at most 400).',
             'inputSchema': o({'path': {'type': 'string'}, 'start': {'type': 'integer'}, 'lines': {'type': 'integer'}}, ['path'])},
            {'name': 'list_files', 'description': f'List a folder in {self.where} (folders end in /); pattern like "*.md".',
             'inputSchema': o({'path': {'type': 'string'}, 'pattern': {'type': 'string'}})},
            {'name': 'grep', 'description': 'Find lines matching a regular expression (case does not matter) in a text file or a '
             'folder of them: "file:line: text".', 'inputSchema': o({'pattern': {'type': 'string'}, 'path': {'type': 'string'},
             'max': {'type': 'integer'}}, ['pattern'])}]

    def call(self, name, a):
        pick = lambda *ks: {k: a[k] for k in ks if k in a}
        if name == 'read_file': return self.read_file(**pick('path', 'start', 'lines'))
        if name == 'list_files': return self.list_files(**pick('path', 'pattern'))
        if name == 'grep': return self.grep(a.get('pattern', ''), a.get('path', '.'), a.get('max', 40))
        return dict(ok=False, error=f'{name} is not a file tool')


def openai_tools(defs):
    return [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'], 'parameters': t['inputSchema']}}
            for t in defs]


# ---------------------------------------------------------------- answers that come back wrong-shaped
TEXT_CALL = re.compile(r'^\s*(?:<\|python_tag\|>)?\s*(?:```(?:json)?\s*)?\{\s*"(?:type|name)"\s*:')


def text_calls(text):
    """Tool calls a model wrote as text: Llama 3.x writes {"type": "function", "name": ..., "parameters": {...}}, and
    LM Studio's MLX engine passes that through as the answer. -> (calls in the OpenAI shape, cut_off): cut_off is
    True when the text starts like a call but does not parse, so the agent can be asked to send it again."""
    s = (text or '').strip()
    if not TEXT_CALL.match(s): return [], False
    s = re.sub(r'^<\|python_tag\|>\s*', '', s)
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s).strip()
    calls, dec, i = [], json.JSONDecoder(), 0
    while i < len(s):
        while i < len(s) and s[i] in ' \t\r\n;,': i += 1
        if i >= len(s): break
        try: o, i = dec.raw_decode(s, i)
        except ValueError: return (calls, False) if calls else ([], True)
        if not (isinstance(o, dict) and isinstance(o.get('name'), str) and ('parameters' in o or 'arguments' in o)):
            return (calls, False) if calls else ([], False)     # JSON, but an answer, not a call
        args = o.get('parameters', o.get('arguments'))
        if isinstance(args, str):
            try: args = json.loads(args)
            except ValueError: args = {}
        calls.append({'id': f'text-call-{len(calls) + 1}', 'type': 'function',
                      'function': {'name': o['name'], 'arguments': json.dumps(args if isinstance(args, dict) else {})}})
    return calls, False


def answer(m):
    return re.sub(r'<think>.*?</think>', '', m.get('content') or '', flags=re.S).strip()


def json_from(text):
    t = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', (text or '').strip())
    for cand in (t, (re.search(r'\{.*\}', t, re.S) or [None])[0]):
        if not cand: continue
        try: return json.loads(cand)
        except ValueError: pass
    return None


def valid(obj, schema):
    if schema.get('type') == 'object': return isinstance(obj, dict) and all(k in obj for k in schema.get('required', []))
    if schema.get('type') == 'array': return isinstance(obj, list)
    return obj is not None


def nonempty(schema):
    """The schema with minItems: 1 on its lists (the root, and each list among an object's properties): the strict JSON
    grammar can then no longer close a list at its first token."""
    s = json.loads(json.dumps(schema))
    if s.get('type') == 'array': s.setdefault('minItems', 1)
    for p in (s.get('properties') or {}).values():
        if isinstance(p, dict) and p.get('type') == 'array': p.setdefault('minItems', 1)
    return s


def empty(obj):
    """An answer with nothing in it: a list with no items, or an object whose every value is empty (the model's whim on
    a strict schema: it opens and closes its thinking and lists nothing)."""
    if isinstance(obj, list): return not obj
    if isinstance(obj, dict): return bool(obj) and all(v in ([], {}, None, '') for v in obj.values())
    return False


def trim(msgs, cap=HISTORY_CAP):
    """Keep the conversation inside the model's context: the oldest tool results go first. -> how many were cut."""
    total, cut = sum(len(str(m.get('content') or '')) for m in msgs), 0
    for m in msgs:
        if total <= cap: break
        if m.get('role') == 'tool' and len(m['content']) > 200:
            total -= len(m['content']) - 50; m['content'] = '[an earlier tool result, trimmed to save room]'; cut += 1
    return cut


def label(s): return re.sub(r'[^A-Za-z0-9._-]+', '-', str(s or 'job'))[:80]


def unique_labels(jobs):
    """The jobs' labels made safe and distinct (two jobs with the same label would overwrite each other's result)."""
    seen, out = {}, []
    for j in jobs:
        base = label(j.get('label'))
        if base in ('summary', 'plan', 'jobs', 'runner', 'run'): base += '-job'   # the batch's own files
        n = seen.get(base, 0) + 1; seen[base] = n
        out.append(dict(j, label=base if n == 1 else f'{base[:75]}-{n}'))
    return out


def step_words(fn, a):
    """What a tool call is doing, in plain words, for the dash."""
    path = os.path.basename(str(a.get('path') or ''))
    if fn == 'read_file': return f"reading {path}" + (f" from line {a['start']}" if a.get('start') else '')
    if fn == 'grep': return f"searching for '{str(a.get('pattern', ''))[:30]}' in {path or 'the project'}"
    if fn == 'list_files': return f"looking in {a.get('path') or 'the project'}"
    return f"using {fn}"


# ---------------------------------------------------------------- one agent
def run_job(job, model, defs, call, project, log_dir=None, logs=None, extra=None, system=SYSTEM, words=step_words, kind='batch'):
    """One job, start to answer. defs(job) -> the tools it may use (MCP-style dicts); call(name, args, job) -> a result.
    -> {label, ok, model, answer, result, seconds, turns, calls, usage, log}."""
    t0, lab = time.time(), label(job.get('label'))
    mark = os.path.join(log_dir, lab + '.running') if log_dir else None   # the dash shows jobs in progress
    now_step = ['starting']

    def step(w):
        now_step[0] = w
        if mark:
            try: open(mark, 'w').write(json.dumps(dict(since=t0, step=w, at=time.time(), pid=os.getpid())))
            except OSError: pass
    step('starting')
    tools = openai_tools(defs(job))
    msgs = [{'role': 'system', 'content': system + ('\n' + job['system'] if job.get('system') else '')},
            {'role': 'user', 'content': job['prompt']}]
    calls, usage, final, turns = [], dict(prompt=0, completion=0, reasoning=0, peak_prompt=0, trimmed=0, retries=0, empty=0), '', 0
    ctx = context_of(model) or 32768
    cpt = [CHARS_PER_TOKEN]
    cap = lambda: int(ctx * cpt[0] * CONTEXT_SHARE)

    def ask(body, think=None):
        req = dict(model=model, **SAMPLING)
        req.update(body)
        req.update(extra or {})                           # the project's own fields win, except that thinking off is off
        if (job.get('think') is False and think is None) or think is False: req.update(THINK_OFF)
        t_first, k = time.time(), 0
        wait_down = WAIT_DOWN if job.get('wait_down') is None else float(job['wait_down'])
        while True:
            try: r = post('/v1/chat/completions', req, project, kind, headers={'X-Step': f'{lab}: {now_step[0]}'[:120]}); break
            except Exception as e:
                again, pause, what = transient(e)
                if not again or time.time() - t_first >= wait_down: raise
                pause = pause or RETRY_WAITS[min(k, len(RETRY_WAITS) - 1)]; k += 1
                usage['retries'] += 1
                step(f'the model did not answer ({what[:60]}): trying again in {pause} s')
                time.sleep(pause)
        u = r.get('usage') or {}
        usage['prompt'] += u.get('prompt_tokens') or 0; usage['completion'] += u.get('completion_tokens') or 0
        usage['reasoning'] += (u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0
        usage['peak_prompt'] = max(usage['peak_prompt'], u.get('prompt_tokens') or 0)   # the biggest request, against the context
        chars = sum(len(str(m.get('content') or '')) for m in body.get('messages', []))
        if (u.get('prompt_tokens') or 0) > 500 and chars: cpt[0] = max(1.5, min(5.0, chars / u['prompt_tokens']))   # this model's own ratio
        c = (r.get('choices') or [{}])[0]
        m = c.get('message') or {}
        m['_finish'] = c.get('finish_reason')
        return m

    out = dict(label=lab, ok=False, model=model)
    try:
        for turns in range(1, int(job.get('max_turns') or 16) + 1):
            usage['trimmed'] += trim(msgs, cap())
            step(f'thinking (step {turns})')
            body = dict(messages=msgs, max_tokens=int(job.get('max_tokens') or 4000))
            if tools: body['tools'] = tools
            m = ask(body)
            if m.get('_finish') == 'length' and not m.get('tool_calls') and not usage.get('cut'):   # cut off: once more, with room
                usage['cut'] = 1; body['max_tokens'] = body['max_tokens'] * 2; m = ask(body)
            if m.get('_finish') == 'length' and not m.get('tool_calls'): out['cut'] = True
            tcs = m.get('tool_calls') or []
            if not tcs and tools:                            # a call written as text (Llama 3.x in LM Studio's MLX engine)
                tcs, cut = text_calls(m.get('content'))
                if tcs: m['content'] = ''; usage['text_calls'] = usage.get('text_calls', 0) + len(tcs)
                elif cut and usage.get('cut_calls', 0) < 2:
                    usage['cut_calls'] = usage.get('cut_calls', 0) + 1
                    msgs += [{'role': 'assistant', 'content': m.get('content') or ''},
                             {'role': 'user', 'content': 'That tool call came through cut off. Send it again, whole.'}]
                    continue
            msgs.append(dict({'role': 'assistant', 'content': m.get('content') or ''}, **({'tool_calls': tcs} if tcs else {})))
            if not tcs: final = answer(m); break
            for tc in tcs:
                fn, raw = tc['function']['name'], tc['function'].get('arguments') or '{}'
                try: a = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except ValueError: a = {}
                a = a if isinstance(a, dict) else {}
                step(words(fn, a))
                names = {d['name'] for d in defs(job)}
                try: res = call(fn, a, job) if fn in names else dict(ok=False, error=f'{fn} is not one of your tools: {", ".join(sorted(names))}')
                except Exception as e: res = dict(ok=False, error=f'{type(e).__name__}: {e}')
                text = json.dumps(res, default=str)
                if len(text) > RESULT_CAP: text = text[:RESULT_CAP] + ' ... [cut: ask for less, e.g. one section or fewer lines]'
                calls.append(dict(tool=fn, args=a, chars=len(text)))
                msgs.append({'role': 'tool', 'tool_call_id': tc.get('id') or '', 'content': text})
        else:
            msgs.append({'role': 'user', 'content': 'That was the last tool turn. Give your final answer now, from what you found.'})
            usage['trimmed'] += trim(msgs, cap())
            m = ask(dict(messages=msgs, max_tokens=int(job.get('max_tokens') or 4000)))
            if m.get('_finish') == 'length' and not usage.get('cut'):
                usage['cut'] = 1; m = ask(dict(messages=msgs, max_tokens=2 * int(job.get('max_tokens') or 4000)))
            final = answer(m)
            msgs.append({'role': 'assistant', 'content': final})
        result = None
        if job.get('schema'):
            step('writing its answer')
            ask_json = msgs + [{'role': 'user', 'content': 'Now give that answer as one JSON object matching this schema, '
                                'and nothing else:\n' + json.dumps(job['schema'])}]
            think = True if job.get('think_schema') else False          # measured: a strict schema with thinking on answers empty
            tokens, empties, grown, modes = int(job.get('max_tokens') or 4000), 0, False, ['schema', 'plain']
            schema, last_err, strict, kept = (nonempty(job['schema']) if job.get('nonempty') else job['schema']), None, True, None
            usage['trimmed'] += trim(ask_json, cap())
            while modes and len(ask_json) < len(msgs) + 8:
                mode = modes[0]
                body = dict(messages=ask_json, max_tokens=tokens)
                if mode == 'schema' and strict: body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'result', 'strict': True, 'schema': schema}}
                try: m = ask(body, think=think)
                except urllib.error.HTTPError as e:
                    last_err = (e.code, err_text(e)); modes.pop(0); continue
                obj = json_from(m.get('content'))                            # LM Studio may put it in either
                if obj is None: obj = json_from(m.get('reasoning_content'))
                if obj is None and m.get('_finish') == 'length' and not grown:                # cut off: once more with room
                    grown, tokens = True, tokens * 2; usage['cut'] = usage.get('cut', 0) + 1; continue
                if obj is not None and valid(obj, job['schema']):
                    if empty(obj) and empties < int(job.get('retry_empty', 1)):
                        empties += 1; usage['empty'] += 1; kept, strict = obj, False         # once more without the grammar: that answer stands
                        ask_json = ask_json + [{'role': 'assistant', 'content': json.dumps(obj)},
                                               {'role': 'user', 'content': 'That answer lists nothing. Look again at what you found and list every item, as the schema asks.'}]
                        continue
                    result = obj; break
                if kept is not None: result = kept; break                                     # the plain ask gave nothing usable: the empty answer stands
                modes.pop(0)
            if result is None and last_err:
                code_, body_ = last_err
                big = re.search(r'context|token|length|exceed', body_, re.I) or usage['peak_prompt'] > 0.8 * ctx
                out['error'] = (f"the conversation outgrew the model's {ctx:,}-token context (load it with more, or ask for less): {body_}" if big
                                else f'HTTP {code_} from LM Studio on the answer as JSON: {body_}')
        out.update(ok=bool(final or result) and (not job.get('schema') or result is not None), answer=final, result=result)
    except urllib.error.HTTPError as e:
        body = err_text(e)
        big = re.search(r'context|token|length|exceed', body, re.I) or usage['peak_prompt'] > 0.8 * ctx
        out['error'] = (f"the conversation outgrew the model's {ctx:,}-token context (load it with more, or ask for less): {body}" if big
                        else f'HTTP {e.code} from LM Studio: {body or e.reason}')
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError, TypeError) as e:
        out['error'] = f'{type(e).__name__}: {e}'
    except Exception as e:                                # never a job that takes the batch down with it
        out['error'] = f'{type(e).__name__}: {e}'
    usage['context'] = ctx
    if usage['peak_prompt'] > 0.85 * ctx:
        out['context_warning'] = f"its biggest request used {usage['peak_prompt']:,} of {ctx:,} tokens: near the limit"
    out.update(seconds=round(time.time() - t0, 1), turns=turns, calls=calls, usage=usage)
    d = log_dir or logs or os.path.expanduser('~/Library/Logs/LocalModels/agents')
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, (lab if log_dir else f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{lab}") + '.json')
        json.dump(dict(out, job=job), open(path, 'w'), indent=1, default=str)
        out['log'] = path
    except OSError as e: out['log'] = None; out.setdefault('error', f'could not write the log: {e}')
    if mark and os.path.exists(mark):
        try: os.remove(mark)
        except OSError: pass
    return out


def new_dir(logs, kind):
    """A fresh folder for a batch, never one another session is writing: the time, then -2, -3... if taken."""
    os.makedirs(logs, exist_ok=True)
    base, n = os.path.join(logs, time.strftime('%Y%m%d-%H%M%S') + '-' + kind), 1
    while True:
        d = base if n == 1 else f'{base}-{n}'
        try: os.makedirs(d); return d
        except FileExistsError: n += 1


def batch(jobs, model, defs, call, project, parallel=1, out_dir=None, logs=None, extra=None, quiet=False, system=SYSTEM,
          words=step_words, by=None, kind='batch'):
    """Run jobs side by side (as many as LM Studio answers at once), holding a lease the dash reads its progress from.
    Asks the hub for fresh copies first (a copy far along its band is recycled before the work starts). -> (summary, folder, results)."""
    logs = logs or os.path.expanduser('~/Library/Logs/LocalModels/agents')
    out_dir = out_dir or new_dir(logs, 'batch')
    os.makedirs(out_dir, exist_ok=True)
    jobs = unique_labels(jobs)
    json.dump(dict(model=model, parallel=parallel, started=time.time(), jobs=[j['label'] for j in jobs], project=project, pid=os.getpid(),
                   by=by or ('claude' if os.environ.get('CLAUDECODE') == '1' else 'you')),
              open(os.path.join(out_dir, 'plan.json'), 'w'))
    if kind == 'batch' and len(jobs) > 1: llmshare.hub_post('/hub/recycle', dict(model=model))
    t0, results = time.time(), []
    try:
        with llmshare.lease(project, [model], kind, log_dir=out_dir), cf.ThreadPoolExecutor(max_workers=max(1, int(parallel))) as ex:
            def once_more(j):                                   # a local model sometimes ends with no answer: one retry
                try:
                    r = run_job(j, model, defs, call, project, out_dir, logs, extra, system, words, kind)
                    if not r['ok'] and not r.get('error'): r = run_job(j, model, defs, call, project, out_dir, logs, extra, system, words, kind); r['retried'] = True
                except Exception as e: r = dict(label=label(j.get('label')), ok=False, error=f'{type(e).__name__}: {e}', seconds=0, turns=0, calls=[], usage={})
                return r
            for f in cf.as_completed([ex.submit(once_more, j) for j in jobs]):
                r = f.result(); results.append(r)
                if not quiet:
                    u = r.get('usage') or {}
                    print(f"  {'ok  ' if r['ok'] else 'FAIL'} {r['label']:<44} {r.get('seconds', 0):>7} s  {r.get('turns', 0):>2} turns  "
                          f"{len(r.get('calls', [])):>2} tool calls  biggest {u.get('peak_prompt', 0):,} of {u.get('context', 0):,} tokens"
                          + (f", {u['trimmed']} trimmed" if u.get('trimmed') else '') + (f", {u['retries']} retries" if u.get('retries') else '')
                          + (f", {u['empty']} empty answer{'s' if u['empty'] != 1 else ''}" if u.get('empty') else '')
                          + (f"  {r['error']}" if r.get('error') else '') + (f"  ({r['context_warning']})" if r.get('context_warning') else ''), flush=True)
    finally:
        tok = {k: sum((r.get('usage') or {}).get(k, 0) for r in results) for k in ('prompt', 'completion', 'reasoning', 'retries', 'empty')}
        summary = dict(model=model, jobs=len(jobs), ok=sum(r['ok'] for r in results), done=len(results), seconds=round(time.time() - t0, 1),
                       parallel=parallel, tokens=tok, results=[{k: r.get(k) for k in ('label', 'ok', 'seconds', 'turns', 'error')} for r in results])
        try: json.dump(summary, open(os.path.join(out_dir, 'summary.json'), 'w'), indent=1)
        except OSError: pass
    return summary, out_dir, results

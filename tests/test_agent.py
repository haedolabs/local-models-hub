#!/usr/bin/env python3
"""The agent engine (localmodels/agent.py) with a scripted model, no request sent anywhere: tries again when the model is
down, gives up on a genuine error, asks once more for an answer that lists nothing, keeps thinking off for the schema step,
grows the room for an answer cut off, keeps labels distinct, and never lets one job take the batch down.
`localmodels check` runs it; so can you: python3 tests/test_agent.py
"""
import email.message, io, json, os, shutil, sys, tempfile, time, urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _sandbox  # noqa: E402  (first: the state folder, port, LM Studio and lms point into a temporary folder)
from localmodels import agent, llmshare

ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))


class Script:
    """agent.post stand-in: answers in order; an entry is the message (dict) or an exception to raise; remembers each request."""
    def __init__(self, answers): self.answers, self.calls = list(answers), []

    def __call__(self, path, body, project, kind='batch', timeout=3600, headers=None):
        self.calls.append(dict(body=json.loads(json.dumps(body)), headers=dict(headers or {}), kind=kind))
        a = self.answers.pop(0) if self.answers else dict(content='done')
        if isinstance(a, Exception): raise a
        msg = {k: v for k, v in a.items() if k not in ('finish', 'reasoning')}
        return dict(choices=[dict(message=msg, finish_reason=a.get('finish', 'stop'))],
                    usage=dict(prompt_tokens=100, completion_tokens=10, completion_tokens_details=dict(reasoning_tokens=a.get('reasoning', 0))))


def http_error(code, body, retry_after=None):
    h = email.message.Message()
    if retry_after: h['Retry-After'] = str(retry_after)
    return urllib.error.HTTPError('http://localhost:1235/v1/chat/completions', code, 'x', h, io.BytesIO(body.encode()))


SCHEMA = {'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['items']}
NO_TOOLS = lambda job: []
NO_CALL = lambda n, a, j: dict(ok=False)


def main():
    box = tempfile.mkdtemp(prefix='localmodels-agenttest-')
    try: run(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    t('no hub path points into the live state folder (tests/_sandbox.py)', not _sandbox.stray(), _sandbox.stray())
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


def run(box):
    for name in ('SLOTS', 'LEASES', 'INFLIGHT'): setattr(llmshare, name, os.path.join(box, name.lower()))
    from localmodels import names as _names, peers as _peers           # never this Mac's live settings, names or peers
    llmshare.MODELS, _names.FILE = os.path.join(box, 'models.json'), os.path.join(box, 'names.json')   # (the repo's defaults apply)
    _peers.STATE, _peers.TOKEN_FILE = os.path.join(box, 'link.json'), os.path.join(box, 'peer-token')
    llmshare.hub_post = lambda *a, **k: None                   # never the real door from a test
    agent.context_of = lambda m: 32768
    agent.RETRY_WAITS = (1, 1, 1)
    logs = os.path.join(box, 'agents')

    print('the agent engine: when the model is down')
    s = Script([http_error(503, 'the model did not answer (crashed); the hub is bringing it back', retry_after=1), dict(content='fine')]); agent.post = s
    t0 = time.time()
    r = agent.run_job(dict(label='a', prompt='say fine'), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('a 503 with Retry-After: waits that long and asks again', r['ok'] and r['answer'] == 'fine' and r['usage']['retries'] == 1 and len(s.calls) == 2 and time.time() - t0 >= 1, (r.get('error'), len(s.calls)))
    t('every request names its step for the dash (X-Step)', s.calls[0]['headers'].get('X-Step', '').startswith('a: '), s.calls[0]['headers'])
    posted = []
    llmshare.hub_post = lambda path, body=None, timeout=5: posted.append((path, body))
    s = Script([dict(content='one'), dict(content='two')]); agent.post = s
    agent.batch([dict(label='p1', prompt='x'), dict(label='p2', prompt='y')], 'm', NO_TOOLS, NO_CALL, 'test', parallel=1, out_dir=os.path.join(box, 'phased'),
                quiet=True, run='survey', phase='D')
    llmshare.hub_post = lambda *a, **k: None
    hs = [c['headers'] for c in s.calls]
    t('a batch in a run\'s phase: each request names the run and phase, its size, and the items done before it',
      [(h.get('X-Run'), h.get('X-Phase'), h.get('X-Phase-Total'), h.get('X-Phase-Done')) for h in hs] == [('survey', 'D', '2', '0'), ('survey', 'D', '2', '1')], hs)
    t('and declares its phase and size to the hub (POST /hub/runs)', ('/hub/runs', dict(run='survey', phases=[dict(name='D', items=2)], project='test', end=False)) in posted, posted)
    t('each request still names its own step (X-Step)', all(h.get('X-Step', '').startswith(('p1: ', 'p2: ')) for h in hs), hs)
    s = Script([http_error(400, '{"error": "Error: The model has crashed without additional information."}'), dict(content='fine')]); agent.post = s
    r = agent.run_job(dict(label='b', prompt='x'), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t("LM Studio's 400 'the model has crashed': asked again", r['ok'] and r['usage']['retries'] == 1, r.get('error'))
    s = Script([http_error(400, '{"error": {"message": "Invalid messages"}}'), dict(content='never')]); agent.post = s
    r = agent.run_job(dict(label='c', prompt='x'), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('a genuine 400: no retry, the error kept', not r['ok'] and 'HTTP 400' in r['error'] and len(s.calls) == 1 and r['usage']['retries'] == 0, r.get('error'))
    s = Script([urllib.error.URLError(ConnectionRefusedError('refused')), http_error(502, "LM Studio's server is not answering"), dict(content='fine')]); agent.post = s
    r = agent.run_job(dict(label='d', prompt='x'), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('a refused connection, then a 502: two retries, then the answer', r['ok'] and r['usage']['retries'] == 2, r.get('error'))
    s = Script([http_error(503, 'down'), dict(content='never')]); agent.post = s
    r = agent.run_job(dict(label='e', prompt='x', wait_down=0), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('wait_down 0: gives up at once with the error', not r['ok'] and 'HTTP 503' in r['error'] and len(s.calls) == 1, r.get('error'))

    print('the agent engine: the answer as JSON')
    s = Script([dict(content='I found two things.'), dict(content='{"items": []}', reasoning=5), dict(content='{"items": ["x", "y"]}')]); agent.post = s
    r = agent.run_job(dict(label='f', prompt='list', schema=SCHEMA), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('an answer that lists nothing is asked for once more without the strict format, with a nudge, and that answer stands', r['ok'] and r['result'] == {'items': ['x', 'y']}
      and r['usage']['empty'] == 1 and 'response_format' in s.calls[1]['body'] and 'response_format' not in s.calls[2]['body']
      and 'lists nothing' in s.calls[2]['body']['messages'][-1]['content'], (r.get('result'), r['usage'].get('empty')))
    s = Script([dict(content='found'), dict(content='{"items": []}'), dict(content='I found nothing to list.')]); agent.post = s
    r = agent.run_job(dict(label='f3', prompt='list', schema=SCHEMA), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('the plain ask gives no JSON: the empty answer stands as the answer', r['ok'] and r['result'] == {'items': []} and len(s.calls) == 3, (r.get('ok'), r.get('result')))
    t('the schema step runs with thinking off; the conversation with thinking on', s.calls[0]['body'].get('reasoning_effort') is None
      and s.calls[1]['body'].get('reasoning_effort') == 'none' and s.calls[2]['body'].get('reasoning_effort') == 'none', [c['body'].get('reasoning_effort') for c in s.calls])
    s = Script([dict(content='found'), dict(content='{"items": ["a"]}')]); agent.post = s
    r = agent.run_job(dict(label='f2', prompt='list', schema=SCHEMA, nonempty=True), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('a job that says its answer cannot be empty gets minItems 1 from the first ask', r['ok'] and s.calls[1]['body']['response_format']['json_schema']['schema']['properties']['items'].get('minItems') == 1)
    s = Script([dict(content='found'), dict(content='{"items": []}'), dict(content='{"items": []}')]); agent.post = s
    r = agent.run_job(dict(label='g', prompt='list', schema=SCHEMA), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('empty twice (strict, then plain): the empty answer stands, counted', r['ok'] and r['result'] == {'items': []} and r['usage']['empty'] == 1 and len(s.calls) == 3, (r.get('result'), len(s.calls)))
    s = Script([dict(content='found'), dict(content='{"items": ["z"]}')]); agent.post = s
    r = agent.run_job(dict(label='h', prompt='list', schema=SCHEMA, think_schema=True), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('think_schema keeps thinking on for the schema step', s.calls[1]['body'].get('reasoning_effort') is None and r['result'] == {'items': ['z']})
    s = Script([dict(content='found'), dict(content='{"items": ["cut', finish='length'), dict(content='{"items": ["whole"]}')]); agent.post = s
    r = agent.run_job(dict(label='i', prompt='list', schema=SCHEMA, max_tokens=1000), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('an answer cut off at max tokens is asked again with twice the room', r['result'] == {'items': ['whole']} and s.calls[2]['body']['max_tokens'] == 2000 and r['usage'].get('cut') == 1, (r.get('result'), s.calls[-1]['body'].get('max_tokens')))
    s = Script([dict(content='found'), dict(content='', reasoning_content='{"items": ["in the think block"]}')]); agent.post = s
    r = agent.run_job(dict(label='j', prompt='list', schema=SCHEMA), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('JSON that landed in reasoning_content is read', r['result'] == {'items': ['in the think block']}, r.get('result'))
    counted = {'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'type': 'string'}}, 'count': {'type': 'integer'}}, 'required': ['items', 'count']}
    s = Script([dict(content='found'), dict(content='{"items": [], "count": 0}'), dict(content='{"items": ["a.py"], "count": 1}')]); agent.post = s
    r = agent.run_job(dict(label='p', prompt='list', schema=counted), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('every list empty beside a number: asked once more, as for an answer that lists nothing', r['result'] == {'items': ['a.py'], 'count': 1} and r['usage']['empty'] == 1,
      (r.get('result'), r['usage'].get('empty')))
    s = Script([dict(content='found'), dict(content='{"items": []}'), dict(content='{"items": "a.py line 3"}')]); agent.post = s
    r = agent.run_job(dict(label='q', prompt='list', schema=SCHEMA), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('a plain answer that does not match the schema (text where a list goes) gives way to the empty one, and is counted',
      r['result'] == {'items': []} and r['usage'].get('invalid') == 1, (r.get('result'), r['usage'].get('invalid')))
    s = Script([dict(content='found'), dict(content='[]'), dict(content='Here is the list, as asked:\n["a.py", "b.py"]')]); agent.post = s
    r = agent.run_job(dict(label='r', prompt='list', schema={'type': 'array', 'items': {'type': 'string'}}), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('a list written after a line of words is read', r['result'] == ['a.py', 'b.py'], r.get('result'))
    t('the schema check reads nested types, enums and minItems', agent.valid({'a': [{'k': 'x'}]}, {'type': 'object', 'required': ['a'], 'properties': {'a': {'type': 'array', 'minItems': 1,
      'items': {'type': 'object', 'required': ['k'], 'properties': {'k': {'enum': ['x', 'y']}}}}}}) and not agent.valid({'a': [{'k': 'z'}]}, {'type': 'object', 'properties': {'a': {'type': 'array',
      'items': {'type': 'object', 'properties': {'k': {'enum': ['x', 'y']}}}}}}) and not agent.valid({'n': True}, {'type': 'object', 'properties': {'n': {'type': 'integer'}}}))

    print('the agent engine: a batch')
    t('labels are made distinct', [j['label'] for j in agent.unique_labels([dict(label='a'), dict(label='a'), dict(label='b/c')])] == ['a', 'a-2', 'b-c'])
    s = Script([dict(content='one'), dict(content='two'), dict(content='three')]); agent.post = s
    boom = lambda job: (_ for _ in ()).throw(RuntimeError('boom')) if job['label'] == 'bad' else []
    summary, d, results = agent.batch([dict(label='good', prompt='x'), dict(label='bad', prompt='y')], 'm', boom, NO_CALL, 'test', parallel=2, logs=logs, quiet=True)
    t('one job that raises never takes the batch down: summary.json written, the other job fine', summary['jobs'] == 2 and summary['done'] == 2 and summary['ok'] == 1
      and os.path.exists(os.path.join(d, 'summary.json')) and any('boom' in (r.get('error') or '') for r in results), summary)
    plan = json.load(open(os.path.join(d, 'plan.json')))
    t("the batch's plan records the runner's pid", plan.get('pid') == os.getpid())

    print('the agent engine: cut answers, context errors, settings that must hold')
    s = Script([dict(content='', finish='length', reasoning=3999), dict(content='the whole answer')]); agent.post = s
    r = agent.run_job(dict(label='k', prompt='x', max_tokens=1000), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('a final answer cut off at max tokens is asked once more with twice the room, not rerun', r['ok'] and r['answer'] == 'the whole answer'
      and s.calls[1]['body']['max_tokens'] == 2000 and r['usage'].get('cut') == 1 and len(s.calls) == 2, (r.get('answer'), [c['body']['max_tokens'] for c in s.calls]))
    s = Script([dict(content='found'), http_error(400, 'Context length exceeded: the prompt is too long'), http_error(400, 'Context length exceeded')]); agent.post = s
    r = agent.run_job(dict(label='l', prompt='list', schema=SCHEMA), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('a schema step that fails on the context ends the job with the diagnosis, no silent rerun', not r['ok'] and 'outgrew' in (r.get('error') or '') and len(s.calls) == 3, (r.get('error'), len(s.calls)))
    s = Script([dict(content='found'), dict(content='{"items": ["q"]}')]); agent.post = s
    r = agent.run_job(dict(label='n', prompt='list', schema=SCHEMA), 'm', NO_TOOLS, NO_CALL, 'test', extra={'reasoning_effort': 'low'}, logs=logs)
    t("a project's extra cannot turn thinking back on for the schema step", s.calls[0]['body'].get('reasoning_effort') == 'low' and s.calls[1]['body'].get('reasoning_effort') == 'none',
      [c['body'].get('reasoning_effort') for c in s.calls])
    s = Script([dict(content='found'), dict(content='[]'), dict(content='[]')]); agent.post = s
    r = agent.run_job(dict(label='o', prompt='list', schema={'type': 'array', 'items': {'type': 'string'}}), 'm', NO_TOOLS, NO_CALL, 'test', logs=logs)
    t('an empty list that is the true answer stands (asked once more first)', r['ok'] and r['result'] == [] and r['usage']['empty'] == 1, (r.get('ok'), r.get('result')))
    t("labels never take the batch's own file names", [j['label'] for j in agent.unique_labels([dict(label='summary'), dict(label='plan'), dict(label='jobs')])] == ['summary-job', 'plan-job', 'jobs-job'])
    files = agent.Files([box]); open(os.path.join(box, 'aaa.txt'), 'w').write('a' * 40 + '\n')
    t0 = time.time(); g = files.grep('(a+)+b', '.')
    t('a pattern that could run for hours is refused at once', not g['ok'] and 'hours' in g['error'] and time.time() - t0 < 2, g)
    t('a plain pattern still searches', bool(files.grep('aaa', '.')['hits']))


if __name__ == '__main__':
    sys.exit(main())

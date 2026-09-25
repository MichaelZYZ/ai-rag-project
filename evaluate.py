"""Run a transparent, small smoke evaluation on a fresh temporary database."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
with tempfile.TemporaryDirectory() as temp:
    os.environ['RAG_DB'] = str(Path(temp) / 'eval.sqlite3')
    os.environ.pop('LLM_BASE_URL', None)
    subprocess.run([sys.executable, str(ROOT / 'seed.py')], check=True,
                   env=os.environ, stdout=subprocess.DEVNULL)
    from app.core import ask
    cases = json.loads((ROOT / 'data' / 'eval_cases.json').read_text(encoding='utf-8'))
    correct_status = correct_answer = hit = 0
    for c in cases:
        result = ask(c['question'], product_id=c['product_id'], version=c['version'])
        status_ok = result['status'] == c['expected_status']
        answer_ok = c.get('expected_text', '') in result['answer'] if c['expected_status'] == 'answered' else status_ok
        source_ok = any(s['source'] == c.get('expected_source') for s in result['sources']) if c['expected_status'] == 'answered' else status_ok
        correct_status += status_ok
        correct_answer += answer_ok
        hit += source_ok
        print(('PASS' if status_ok and answer_ok and source_ok else 'FAIL'), c['question'],
              '→', result['status'], result['answer'])
    n = len(cases)
    print(json.dumps({'cases': n, 'status_accuracy': correct_status/n,
                      'answer_match_rate': correct_answer/n, 'source_hit_rate': hit/n}, ensure_ascii=False))
    if min(correct_status, correct_answer, hit) < n:
        raise SystemExit(1)

"""Product-scoped retrieval and conversation logic for the classroom demo."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
import uuid
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer
from .cleaning import clean_document
from .faq import ANSWER, QUESTION, extract_faqs
from .html_extract import extract_html_text

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv('RAG_DB', ROOT / 'data' / 'rag.sqlite3'))
VECTORIZER = HashingVectorizer(analyzer='char', ngram_range=(2, 4), n_features=4096,
                               alternate_sign=False, norm='l2', lowercase=True)
HANDOFF_WORDS = ('退款', '投诉', '退货', '账户权限', '修改权限', '人工', '注销账户')
FOLLOWUP_WORDS = ('它', '这个', '那个', '最多', '多少', '怎么', '呢', '还', '是否')


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, product_id TEXT NOT NULL, version TEXT NOT NULL,
            category TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
            cleaning_report TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS document_texts(document_id TEXT PRIMARY KEY, text TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, document_id TEXT NOT NULL, product_id TEXT NOT NULL,
            version TEXT NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL,
            vector BLOB NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS faqs(id TEXT PRIMARY KEY, document_id TEXT NOT NULL, product_id TEXT NOT NULL,
            version TEXT NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL, question TEXT NOT NULL,
            answer TEXT NOT NULL, kind TEXT NOT NULL, vector BLOB NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, product_id TEXT, version TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, text TEXT NOT NULL, status TEXT, created_at TEXT NOT NULL, product_id TEXT);
        CREATE TABLE IF NOT EXISTS handoffs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            rating INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS unanswered(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            question TEXT NOT NULL, product_id TEXT, version TEXT, created_at TEXT NOT NULL,
            resolved_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks(product_id, version);
        CREATE INDEX IF NOT EXISTS idx_faqs_scope ON faqs(product_id, version);
        ''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(documents)')}
        if 'cleaning_report' not in columns:
            db.execute("ALTER TABLE documents ADD COLUMN cleaning_report TEXT NOT NULL DEFAULT '{}'")
        message_columns = {row['name'] for row in db.execute('PRAGMA table_info(messages)')}
        if 'product_id' not in message_columns:
            db.execute('ALTER TABLE messages ADD COLUMN product_id TEXT')
            db.execute('''UPDATE messages SET product_id=(
                SELECT product_id FROM sessions WHERE sessions.id=messages.session_id)
                WHERE role='user' ''')
        unanswered_columns = {row['name'] for row in db.execute('PRAGMA table_info(unanswered)')}
        if 'version' not in unanswered_columns:
            db.execute('ALTER TABLE unanswered ADD COLUMN version TEXT')
            db.execute('''UPDATE unanswered SET version=(
                SELECT version FROM sessions WHERE sessions.id=unanswered.session_id)''')
        if 'resolved_at' not in unanswered_columns:
            db.execute('ALTER TABLE unanswered ADD COLUMN resolved_at TEXT')
        db.execute('CREATE INDEX IF NOT EXISTS idx_unanswered_open ON unanswered(resolved_at,product_id,version)')


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def add_product(product_id, name, aliases=None):
    product_id, name = product_id.strip(), name.strip()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{2,40}', product_id):
        raise ValueError('产品 ID 需为 2–40 位字母、数字、下划线或短横线')
    if not name:
        raise ValueError('产品名称不能为空')
    with connect() as db:
        db.execute('INSERT INTO products VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, aliases=excluded.aliases',
                   (product_id, name, json.dumps(aliases or [], ensure_ascii=False)))


def parse_document(filename: str, content: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in ('.txt', '.md'):
        return content.decode('utf-8-sig')
    if ext in ('.html', '.htm'):
        return extract_html_text(content)
    if ext == '.docx':
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(content)) as z:
            root = ET.fromstring(z.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        return '\n'.join(''.join(t.text or '' for t in p.findall('.//w:t', ns))
                         for p in root.findall('.//w:p', ns))
    if ext == '.pdf':
        from io import BytesIO
        from pypdf import PdfReader
        return '\n'.join(page.extract_text() or '' for page in PdfReader(BytesIO(content)).pages)
    raise ValueError('仅支持 .txt、.md、.html、.htm、.docx、.pdf')


def split_text(text: str, max_chars=360, overlap=50):
    # The cleaner has already normalized line endings and whitespace. A heading
    # stays with each following fact so identical words in different sections
    # retain their context. Short label/value lines are joined before indexing.
    chunks, heading, pending = [], '', []

    def emit(body):
        prefix = heading + '\n' if heading else ''
        room = max(80, max_chars - len(prefix))
        if len(body) <= room:
            chunks.append(prefix + body)
        else:
            step = max(1, room - overlap)
            for start in range(0, len(body), step):
                chunks.append(prefix + body[start:start + room])

    for line in text.splitlines():
        if line.startswith('#'):
            if pending:
                emit(' '.join(pending))
                pending = []
            heading = line.lstrip('#').strip()[:80]
            continue
        if QUESTION.match(line) or line.endswith(('?', '？')):
            continue
        answer_marker = ANSWER.match(line)
        if answer_marker:
            line = answer_marker.group(1).strip()
        if len(line) < 6:
            pending.append(line)
            continue
        if pending:
            line = ' '.join(pending + [line])
            pending = []
        emit(line)
    if pending:
        emit(' '.join(pending))
    return [c for c in chunks if c.strip()]


def add_document(product_id, version, category, title, filename, content):
    if not version.strip():
        raise ValueError('版本不能为空')
    if len(content) > 5 * 1024 * 1024:
        raise ValueError('文件不能超过 5 MB')
    with connect() as db:
        product = db.execute('SELECT name FROM products WHERE id=?', (product_id,)).fetchone()
        if not product:
            raise ValueError('产品不存在')
    raw_text = parse_document(filename, content)
    text, cleaning_report = clean_document(raw_text)
    faqs = extract_faqs(text, product['name'])
    chunks = split_text(text)
    for faq in faqs:
        if not any(faq['answer'] in chunk for chunk in chunks):
            chunks.extend(split_text(faq['answer']))
    if not chunks:
        raise ValueError('文档中没有可索引的文字')
    # Index the factual body; keep the heading in stored text for the answer.
    vectors = VECTORIZER.transform([c.split('\n', 1)[-1] for c in chunks]).astype(np.float32).toarray()
    doc_id = uuid.uuid4().hex
    with connect() as db:
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',
                   (doc_id, product_id, version, category, title, filename, now(),
                    json.dumps(cleaning_report, ensure_ascii=False)))
        db.execute('INSERT INTO document_texts(document_id,text) VALUES(?,?)', (doc_id, text))
        db.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)',
                       [(uuid.uuid4().hex, doc_id, product_id, version, category, filename,
                         c, v.tobytes()) for c, v in zip(chunks, vectors)])
        if faqs:
            faq_vectors = VECTORIZER.transform(
                [faq_terms(f['question'], product['name']) for f in faqs]).astype(np.float32).toarray()
            db.executemany('INSERT INTO faqs VALUES(?,?,?,?,?,?,?,?,?,?)',
                           [(uuid.uuid4().hex, doc_id, product_id, version, f['category'], filename,
                             f['question'], f['answer'], f['kind'], vector.tobytes())
                            for f, vector in zip(faqs, faq_vectors)])
    return {'document_id': doc_id, 'chunks': len(chunks), 'characters': len(text),
            'faqs': len(faqs), 'faq_preview': [f['question'] for f in faqs[:6]],
            'cleaning': cleaning_report}


def reconstruct_chunk_text(db, document_id):
    """Recover headings and overlapped long lines from older indexed documents."""
    lines, current_heading = [], None
    for row in db.execute('SELECT text FROM chunks WHERE document_id=? ORDER BY rowid',
                          (document_id,)):
        chunk = row['text']
        if '\n' in chunk:
            heading, body = chunk.split('\n', 1)
            if heading != current_heading:
                lines.append('# ' + heading)
                current_heading = heading
        else:
            body = chunk
            current_heading = None
        if not body.strip():
            continue
        # Long original lines were cut with a 50-character overlap.
        overlap = 0
        if lines and not lines[-1].startswith('# '):
            for size in range(min(80, len(body), len(lines[-1])), 19, -1):
                if lines[-1].endswith(body[:size]):
                    overlap = size
                    break
        if overlap:
            lines[-1] += body[overlap:]
        else:
            lines.append(body)
    return '\n'.join(lines)


def bootstrap_faqs():
    """On demand, fill missing source-backed FAQs without changing model weights."""
    added = scanned = 0
    with connect() as db:
        documents = db.execute('''SELECT d.id,d.product_id,d.version,d.category,d.source,
                                         p.name product_name
                                  FROM documents d JOIN products p ON p.id=d.product_id
                                  WHERE d.source != '人工补充'
                                  ORDER BY d.rowid''').fetchall()
        for doc in documents:
            scanned += 1
            stored = db.execute('SELECT text FROM document_texts WHERE document_id=?',
                                (doc['id'],)).fetchone()
            source_text = (stored['text'] if stored else '') or reconstruct_chunk_text(db, doc['id'])
            if not source_text.strip():
                continue
            source_lines = set(source_text.splitlines())
            proposals = []
            for faq in extract_faqs(source_text, doc['product_name']):
                key = faq_terms(faq['question'], doc['product_name'])
                answer_lines = [line.strip() for line in faq['answer'].splitlines() if line.strip()]
                # Every answer line must be copied from this document's text.
                if not key or not answer_lines or any(
                        line not in source_lines and line not in source_text for line in answer_lines):
                    continue
                proposals.append((key, faq))
            if not proposals:
                continue
            # Lock only while writing this document, then release it for chat requests.
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM documents WHERE id=?', (doc['id'],)).fetchone():
                db.rollback()
                continue
            existing = {faq_terms(row['question'], doc['product_name']) for row in db.execute(
                'SELECT question FROM faqs WHERE document_id=?', (doc['id'],))}
            candidates = []
            for key, faq in proposals:
                if key not in existing:
                    existing.add(key)
                    candidates.append((key, faq))
            for start in range(0, len(candidates), 64):
                batch = candidates[start:start + 64]
                vectors = VECTORIZER.transform([key for key, _ in batch]).astype(np.float32).toarray()
                db.executemany('INSERT INTO faqs VALUES(?,?,?,?,?,?,?,?,?,?)', [
                    (uuid.uuid4().hex, doc['id'], doc['product_id'], doc['version'],
                     faq['category'], doc['source'], faq['question'], faq['answer'],
                     faq['kind'], vector.tobytes())
                    for (_, faq), vector in zip(batch, vectors)])
            db.commit()
            added += len(candidates)
    return {'documents_scanned': scanned, 'faqs_added': added}


def product_catalog(limit=None):
    """Return indexed products ordered by the number of user consultations."""
    sql = '''
        SELECT p.id, p.name, p.aliases,
               (SELECT count(*) FROM documents d WHERE d.product_id=p.id) AS document_count,
               (SELECT count(*) FROM messages m
                WHERE m.role='user' AND m.product_id=p.id) AS consultation_count
        FROM products p
        WHERE EXISTS (SELECT 1 FROM documents d WHERE d.product_id=p.id)
        ORDER BY consultation_count DESC, p.name COLLATE NOCASE, p.id
    '''
    with connect() as db:
        rows = db.execute(sql + (' LIMIT ?' if limit is not None else ''),
                          (limit,) if limit is not None else ()).fetchall()
        catalog = []
        for row in rows:
            versions = [r['version'] for r in db.execute(
                'SELECT DISTINCT version FROM documents WHERE product_id=? ORDER BY version DESC', (row['id'],))]
            categories = [r['category'] for r in db.execute(
                'SELECT DISTINCT category FROM documents WHERE product_id=? ORDER BY category', (row['id'],))]
            sample = db.execute('''SELECT c.text FROM chunks c JOIN documents d ON d.id=c.document_id
                                   WHERE c.product_id=? ORDER BY d.version DESC, c.rowid LIMIT 1''',
                                (row['id'],)).fetchone()
            catalog.append({'id': row['id'], 'name': row['name'],
                            'aliases': json.loads(row['aliases']), 'versions': versions,
                            'categories': categories, 'document_count': row['document_count'],
                            'consultation_count': row['consultation_count'],
                            'summary': sample['text'].split('\n', 1)[-1][:80] if sample else ''})
    return catalog


def discovery_terms(query):
    terms = query.lower()
    for phrase in ('是否存在', '有没有', '是否有', '能不能找到', '能找到', '请帮我找', '帮我找',
                   '相关的', '相关', '知识库', '产品信息', '产品资料', '产品说明', '产品', '资料',
                   '请问', '有吗', '吗', '是否', '支持', '可以', '的', '有', '找'):
        terms = terms.replace(phrase, '')
    return re.sub(r'[\s，。？！：；,.?!:;“”"()（）]+', '', terms)


def is_discovery_question(question):
    return ('知识库' in question or '产品资料' in question or '产品信息' in question
            or ('产品' in question and any(w in question for w in ('有没有', '是否有', '存在', '查找', '寻找'))))


def search_products(query, limit=6):
    """Return likely candidates; a low score never asserts product existence."""
    terms = discovery_terms(query)
    if not terms:
        return []
    catalog = product_catalog()
    if not catalog:
        return []
    can_fuzzy = len(terms) >= 2
    query_vec = VECTORIZER.transform([terms]).astype(np.float32).toarray()[0] if can_fuzzy else None
    grams = {terms[i:i+2] for i in range(len(terms)-1)} if can_fuzzy else set()
    with connect() as db:
        scored = []
        for product in catalog:
            names = [product['name'], product['id']]
            direct = any(name and name.lower() in query.lower() for name in names)
            direct = direct or any(alias and (terms == alias.lower() or
                                              (len(alias) >= 3 and alias.lower() in terms))
                                   for alias in product['aliases'])
            best = 1.0 if direct else 0.0
            if not direct and can_fuzzy:
                rows = db.execute('SELECT text,vector FROM chunks WHERE product_id=?', (product['id'],))
                for row in rows:
                    body = row['text'].split('\n', 1)[-1].lower()
                    body_grams = {body[i:i+2] for i in range(len(body)-1)}
                    coverage = len(grams & body_grams) / len(grams)
                    cosine = float(np.dot(query_vec, np.frombuffer(row['vector'], dtype=np.float32)))
                    best = max(best, 0.7 * cosine + 0.3 * coverage)
            if best >= 0.28:
                scored.append((best, product))
    scored.sort(key=lambda item: (-item[0], -item[1]['consultation_count'], item[1]['name']))
    return [{**product, 'match_score': round(score, 3)} for score, product in scored[:limit]]


def detect_product(question, explicit, prior):
    with connect() as db:
        products = db.execute('SELECT * FROM products').fetchall()
    for p in products:
        for name in [p['name'], p['id'], *json.loads(p['aliases'])]:
            if name and name.lower() in question.lower():
                return p['id']
    return explicit or prior


def retrieve(question, product_id, version, top_k=4):
    with connect() as db:
        rows = db.execute('SELECT * FROM chunks WHERE product_id=? AND version=?',
                          (product_id, version)).fetchall()
    if not rows:
        return []
    with connect() as db:
        product = db.execute('SELECT name,aliases FROM products WHERE id=?', (product_id,)).fetchone()
    clean = question
    for name in [product_id, product['name'], *[a for a in json.loads(product['aliases']) if len(a) >= 3]] if product else [product_id]:
        clean = clean.replace(name, '')
    for phrase in ('追问', '最多', '多少', '支持', '是否', '可以', '有没有', '是什么', '怎么', '如何', '这个', '那个', '它', '请问', '限制为', '限制是'):
        clean = clean.replace(phrase, '')
    clean = re.sub(r'[\s，。？！：；,.?!:;0-9]+', '', clean)
    query_vec = VECTORIZER.transform([clean or question]).astype(np.float32).toarray()[0]
    scored = []
    for row in rows:
        vec = np.frombuffer(row['vector'], dtype=np.float32)
        semantic = float(np.dot(query_vec, vec))
        # Character overlap improves ranking of short Chinese parameter questions.
        a = set(clean[i:i+2] for i in range(len(clean)-1))
        body = row['text'].split('\n', 1)[-1]
        b = set(body[i:i+2] for i in range(len(body)-1))
        overlap = len(a & b) / max(1, len(a))
        score = 0.7 * semantic + 0.3 * overlap
        scored.append({'chunk_id': row['id'], 'source': row['source'], 'category': row['category'],
                       'text': row['text'], 'score': round(score, 4)})
    return sorted(scored, key=lambda x: x['score'], reverse=True)[:top_k]


def faq_terms(question, product_name):
    terms = question.lower().replace(product_name.lower(), '')
    terms = re.sub(r'\b\d+(?:\.\d+)*\b', '', terms)
    for phrase in ('请问', '是否', '可以', '能否', '怎么', '如何', '多少', '最多', '支持',
                   '这个', '那个', '它', '是什么', '怎么办', '吗', '呢', '的'):
        terms = terms.replace(phrase, '')
    return re.sub(r'[\s，。？！：；,.?!:;“”"()（）]+', '', terms) or question.lower()


def faq_intent(question):
    if any(term in question for term in ('价格', '售价', '收费', '多少钱')):
        return '价格'
    if any(term in question for term in ('保修', '质保')):
        return '售后保修'
    if any(term in question for term in ('兼容', '适配')):
        return '兼容性'
    if any(term in question for term in ('适用', '场景', '适合')):
        return '适用场景'
    if any(term in question for term in ('最多', '多少', '上限', '限制', '容量', '大小')):
        return '参数限制'
    if any(term in question for term in ('忘记密码', '报错', '故障', '无法登录')):
        return '故障处理'
    if any(term in question for term in ('怎么', '如何', '步骤', '怎样')):
        return '操作步骤'
    if any(term in question for term in ('支持', '能否', '可以', '能不能')):
        return '功能支持'
    return None


def list_faqs(product_id, version, limit=12):
    with connect() as db:
        rows = db.execute('''SELECT question, answer, category, source, kind FROM faqs
                             WHERE product_id=? AND version=?
                             ORDER BY CASE kind WHEN 'manual' THEN 0 WHEN 'explicit' THEN 1 WHEN 'auto' THEN 2 ELSE 3 END, rowid
                             LIMIT ?''', (product_id, version, limit)).fetchall()
    return [dict(row) for row in rows]


def supplement_gap(gap_id, product_id, version, question, answer):
    """Save a reviewed Q/A and optionally close a matching knowledge gap."""
    product_id, version = product_id.strip(), version.strip()
    question, answer = question.strip(), answer.strip()
    if not product_id or not version or len(version) > 40:
        raise ValueError('请选择产品并填写有效版本')
    if not 2 <= len(question) <= 500 or not 2 <= len(answer) <= 4000:
        raise ValueError('问题需为 2–500 字，答案需为 2–4000 字')
    with connect() as db:
        gap = (db.execute('SELECT * FROM unanswered WHERE id=? AND resolved_at IS NULL',
                          (gap_id,)).fetchone() if gap_id is not None else None)
        if gap_id is not None and not gap:
            raise KeyError('知识缺口不存在或已解决')
        product = db.execute('SELECT name FROM products WHERE id=?', (product_id,)).fetchone()
        if not product:
            raise ValueError('所选产品不存在')
        if gap and gap['product_id'] and product_id != gap['product_id']:
            raise ValueError('补充答案必须写入缺口所属的产品')
        if gap and gap['version'] and version != gap['version']:
            raise ValueError('补充答案必须写入缺口所属的版本')
        if gap and gap['product_id'] and faq_terms(question, product['name']) != faq_terms(gap['question'], product['name']):
            raise ValueError('补充问题需与当前知识缺口一致')
        existing = db.execute('SELECT * FROM faqs WHERE product_id=? AND version=?',
                              (product_id, version)).fetchall()
        for row in existing:
            if faq_terms(row['question'], product['name']) == faq_terms(question, product['name']):
                if row['answer'].strip() != answer:
                    raise ValueError('该产品版本已有同一问题的不同答案，请先核对现有资料')
                faq_id, doc_id = row['id'], row['document_id']
                break
        else:
            doc_id, faq_id = uuid.uuid4().hex, uuid.uuid4().hex
            source = '人工补充'
            timestamp = now()
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',
                       (doc_id, product_id, version, '人工 FAQ', '人工补充：' + question[:60],
                        source, timestamp, json.dumps({'manual': True}, ensure_ascii=False)))
            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, doc_id, product_id, version, '人工 FAQ', source,
                        answer, VECTORIZER.transform([answer]).astype(np.float32).toarray()[0].tobytes()))
            db.execute('INSERT INTO faqs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (faq_id, doc_id, product_id, version, '人工 FAQ', source,
                        question, answer, 'manual',
                        VECTORIZER.transform([faq_terms(question, product['name'])])
                        .astype(np.float32).toarray()[0].tobytes()))
        resolved = (db.execute('''UPDATE unanswered SET resolved_at=?
                                 WHERE resolved_at IS NULL AND question=?
                                 AND product_id IS ? AND version IS ?''',
                               (now(), gap['question'], gap['product_id'], gap['version'])).rowcount
                    if gap else 0)
    return {'document_id': doc_id, 'faq_id': faq_id, 'resolved_count': resolved,
            'product_id': product_id, 'version': version}


def match_faq(question, product_id, version):
    with connect() as db:
        product = db.execute('SELECT name,id,aliases FROM products WHERE id=?', (product_id,)).fetchone()
        rows = db.execute('SELECT * FROM faqs WHERE product_id=? AND version=?',
                          (product_id, version)).fetchall()
    if not product or not rows:
        return None
    terms = faq_terms(question, product['name'])
    query_vec = VECTORIZER.transform([terms]).astype(np.float32).toarray()[0]
    grams = {terms[i:i+2] for i in range(len(terms)-1)}
    product_words = set(re.findall(r'[a-z]{2,}', product['id'].lower() + ' ' + product['name'].lower() +
                                   ' ' + ' '.join(json.loads(product['aliases'])).lower()))
    required_words = [word.lower() for word in re.findall(r'[A-Za-z]{2,}', question)
                      if word.lower() not in product_words]
    intent = faq_intent(question)
    candidates = []
    for row in rows:
        if intent and row['category'] not in (intent, '文档 FAQ', '人工 FAQ'):
            continue
        evidence = (row['question'] + ' ' + row['answer']).lower()
        if any(word not in evidence for word in required_words):
            continue
        faq_clean = faq_terms(row['question'], product['name'])
        faq_grams = {faq_clean[i:i+2] for i in range(len(faq_clean)-1)}
        coverage = len(grams & faq_grams) / max(1, len(grams))
        cosine = float(np.dot(query_vec, np.frombuffer(row['vector'], dtype=np.float32)))
        score = 0.7 * cosine + 0.3 * coverage
        if score >= 0.38 and coverage >= 0.25:
            candidates.append((score, row))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    score, row = candidates[0]
    same_question = [item for _, item in candidates
                     if faq_terms(item['question'], product['name']) == faq_terms(row['question'], product['name'])]
    conflict = len({item['answer'].strip() for item in same_question}) > 1
    return {'answer': row['answer'], 'score': round(score, 4), 'conflict': conflict,
            'source': {'chunk_id': row['id'], 'source': row['source'], 'category': row['category'],
                       'text': row['answer'], 'score': round(score, 4)}}


def llm_answer(question, sources):
    endpoint = os.getenv('LLM_BASE_URL', '').rstrip('/')
    key = os.getenv('LLM_API_KEY', '')
    model = os.getenv('LLM_MODEL', '')
    if not (endpoint and key and model):
        return None
    prompt = (ROOT / 'prompts' / 'answer.txt').read_text(encoding='utf-8')
    context = '\n\n'.join(f"[{i+1}] {s['text']}" for i, s in enumerate(sources))
    payload = json.dumps({'model': model, 'temperature': 0, 'messages': [
        {'role': 'system', 'content': prompt},
        {'role': 'user', 'content': f'问题：{question}\n\n资料：\n{context}'}
    ]}).encode()
    request = urllib.request.Request(endpoint + '/chat/completions', data=payload,
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        answer = json.load(response)['choices'][0]['message']['content'].strip()
    return answer


def handoff(db, session_id, question, product_id, version, reason):
    history = db.execute('SELECT role,text FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 6',
                         (session_id,)).fetchall()
    summary = f'产品：{product_id or "未确认"}；版本：{version or "未确认"}\n原因：{reason}\n当前问题：{question}\n近期对话：\n'
    summary += '\n'.join(f"{x['role']}：{x['text']}" for x in reversed(history))
    ticket_id = uuid.uuid4().hex[:12]
    db.execute('INSERT INTO handoffs VALUES(?,?,?,?,?)', (ticket_id, session_id, summary, 'open', now()))
    return ticket_id


def ask(question, session_id=None, product_id=None, version=None):
    start = time.monotonic()
    question = question.strip()
    if not question or len(question) > 1000:
        raise ValueError('问题长度需为 1–1000 字')
    session_id = session_id or uuid.uuid4().hex
    discovery = is_discovery_question(question)
    matched_products = search_products(question) if discovery else []
    if discovery:
        version = None
    with connect() as db:
        old = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not old:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (session_id, None, None, now()))
        prev = db.execute("SELECT text FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                          (session_id,)).fetchone()
        selected_product = product_id
        product_id = (None if discovery else
                      detect_product(question, product_id, old['product_id'] if old else None))
        if selected_product and product_id != selected_product:
            version = None
        version = version or (old['version'] if old and old['product_id'] == product_id else None)
        if product_id and not version:
            with connect() as lookup:
                row = lookup.execute('SELECT version FROM documents WHERE product_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1',
                                     (product_id,)).fetchone()
            version = row['version'] if row else None
        if not discovery:
            db.execute('UPDATE sessions SET product_id=?,version=? WHERE id=?', (product_id, version, session_id))
        db.execute('INSERT INTO messages(session_id,role,text,created_at,product_id) VALUES(?,?,?,?,?)',
                   (session_id, 'user', question, now(), product_id))
        rewritten = question
        if prev and any(word in question for word in FOLLOWUP_WORDS):
            rewritten = prev['text'] + '；追问：' + question
        sources = retrieve(rewritten, product_id, version) if product_id and version and not discovery else []
        faq = match_faq(rewritten, product_id, version) if product_id and version and not discovery else None
        top = sources[0]['score'] if sources else 0
        ticket_id = None
        if any(word in question for word in HANDOFF_WORDS):
            status = 'handoff'
            answer = '此问题需要人工客服处理，已为你生成转人工记录。'
            ticket_id = handoff(db, session_id, question, product_id, version, '敏感业务或用户要求人工')
            sources = []
        elif discovery:
            status = 'product_found' if matched_products else 'no_product'
            answer = ('找到可能相关的产品知识库，请选择产品后继续提问。' if matched_products else
                      '未找到匹配的产品知识库，请联系人工客服。')
            sources = []
        elif not product_id:
            matched_products = search_products(question)
            status = 'product_found' if matched_products else 'no_product'
            answer = ('找到可能相关的产品知识库，请选择产品后继续提问。' if matched_products else
                      '未找到匹配的产品知识库，请联系人工客服。')
            sources = []
        elif not version:
            status = 'no_product'
            answer = '该产品尚无已导入的知识资料，请联系人工客服。'
            sources = []
        elif faq and faq['conflict']:
            status = 'handoff'
            answer = '产品资料对这个问题给出了冲突答案，请联系人工客服确认。'
            ticket_id = handoff(db, session_id, question, product_id, version, '同一常见问题存在冲突答案')
            sources = []
        elif faq:
            status = 'answered'
            answer = faq['answer']
            sources = [faq['source']]
            top = faq['score']
        elif top < 0.16 or (sources and any(term.lower() not in sources[0]['text'].lower() for term in re.findall(r'[A-Za-z]{3,}', question) if term.lower() not in ('nova', 'notes', 'orbit', 'tasks'))):
            status = 'no_answer'
            answer = '现有产品资料中暂未找到相关说明，请补充问题或联系人工客服。'
            sources = []
        else:
            status = 'answered'
            try:
                generated = llm_answer(rewritten, sources[:3])
            except Exception:
                generated = None
            # Offline mode presents a verbatim retrieved passage, never invented facts.
            answer = generated or sources[0]['text']
        if status in ('no_answer', 'no_product'):
            db.execute('INSERT INTO unanswered(session_id,question,product_id,version,created_at) VALUES(?,?,?,?,?)',
                       (session_id, question, product_id, version, now()))
        db.execute('INSERT INTO messages(session_id,role,text,status,created_at) VALUES(?,?,?,?,?)',
                   (session_id, 'assistant', answer, status, now()))
    return {'session_id': session_id, 'product_id': product_id, 'version': version,
            'rewritten_question': rewritten, 'answer': answer, 'status': status,
            'confidence': round(top, 4), 'sources': sources[:3] if status == 'answered' else [],
            'matched_products': matched_products, 'handoff_id': ticket_id,
            'latency_ms': round((time.monotonic()-start)*1000)}

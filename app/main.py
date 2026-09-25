from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from . import core

app = FastAPI(title='产品知识库问答演示', version='1.0')
core.init_db()
STATIC = Path(__file__).parent / 'static'

class ProductInput(BaseModel):
    id: str
    name: str
    aliases: list[str] = []

class AskInput(BaseModel):
    question: str
    session_id: Optional[str] = None
    product_id: Optional[str] = None
    version: Optional[str] = None

class FeedbackInput(BaseModel):
    session_id: str
    rating: int = Field(ge=-1, le=1)
    note: str = ''

class GapAnswerInput(BaseModel):
    product_id: str
    version: str
    question: str
    answer: str

@app.get('/')
def home():
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-store'})

@app.get('/api/products')
def products():
    with core.connect() as db:
        rows = db.execute('SELECT * FROM products ORDER BY name').fetchall()
        result = []
        for p in rows:
            versions = [r['version'] for r in db.execute(
                'SELECT DISTINCT version FROM documents WHERE product_id=? ORDER BY version DESC', (p['id'],))]
            result.append({'id': p['id'], 'name': p['name'], 'aliases': json.loads(p['aliases']), 'versions': versions})
        return result

@app.get('/api/products/featured')
def featured_products():
    catalog = core.product_catalog()
    return catalog[:6] if any(p['consultation_count'] for p in catalog) else catalog

@app.get('/api/products/catalog')
def indexed_products():
    return core.product_catalog()

@app.get('/api/products/search')
def search_products(q: str):
    if len(q) > 200:
        raise HTTPException(400, '搜索内容不能超过 200 字')
    return core.search_products(q)

@app.post('/api/products')
def create_product(data: ProductInput):
    try:
        core.add_product(data.id, data.name, data.aliases)
        return {'ok': True}
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post('/api/documents')
async def upload_document(product_id: str = Form(...), version: str = Form(...),
                          category: str = Form('产品说明'), title: str = Form(''),
                          file: UploadFile = File(...)):
    try:
        content = await file.read(5 * 1024 * 1024 + 1)
        return core.add_document(product_id, version.strip(), category.strip(),
                                 title.strip() or file.filename, file.filename, content)
    except (ValueError, UnicodeError, zipfile.BadZipFile, ImportError, KeyError) as e:
        raise HTTPException(400, str(e))

@app.get('/api/documents')
def documents(product_id: Optional[str] = None):
    with core.connect() as db:
        if product_id:
            rows = db.execute('SELECT d.*, count(c.id) chunks, (SELECT count(*) FROM faqs f WHERE f.document_id=d.id) faqs FROM documents d LEFT JOIN chunks c ON c.document_id=d.id WHERE d.product_id=? GROUP BY d.id ORDER BY d.created_at DESC', (product_id,))
        else:
            rows = db.execute('SELECT d.*, count(c.id) chunks, (SELECT count(*) FROM faqs f WHERE f.document_id=d.id) faqs FROM documents d LEFT JOIN chunks c ON c.document_id=d.id GROUP BY d.id ORDER BY d.created_at DESC')
        return [{**dict(r), 'cleaning_report': json.loads(r['cleaning_report'])} for r in rows]

@app.get('/api/faqs')
def faqs(product_id: str, version: str):
    return core.list_faqs(product_id, version)

@app.post('/api/faqs/train')
def train_faqs():
    return core.bootstrap_faqs()

@app.delete('/api/documents/{document_id}')
def delete_document(document_id: str):
    with core.connect() as db:
        row = db.execute('DELETE FROM documents WHERE id=? RETURNING id', (document_id,)).fetchone()
        if not row:
            raise HTTPException(404, '文档不存在')
    return {'ok': True}

@app.post('/api/ask')
def ask(data: AskInput):
    try:
        return core.ask(data.question, data.session_id, data.product_id, data.version)
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post('/api/feedback')
def feedback(data: FeedbackInput):
    with core.connect() as db:
        db.execute('INSERT INTO feedback(session_id,rating,note,created_at) VALUES(?,?,?,?)',
                   (data.session_id, data.rating, data.note[:500], core.now()))
    return {'ok': True}

@app.post('/api/gaps/{gap_id}/answer')
def answer_gap(gap_id: int, data: GapAnswerInput):
    try:
        return core.supplement_gap(gap_id, data.product_id, data.version,
                                   data.question, data.answer)
    except KeyError as e:
        raise HTTPException(404, e.args[0])
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post('/api/faqs/manual')
def add_manual_faq(data: GapAnswerInput):
    try:
        return core.supplement_gap(None, data.product_id, data.version,
                                   data.question, data.answer)
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.get('/api/stats')
def stats():
    with core.connect() as db:
        counts = dict(db.execute('SELECT status,count(*) n FROM messages WHERE role="assistant" GROUP BY status').fetchall())
        total = sum(counts.values())
        return {'questions': total, 'answered': counts.get('answered', 0),
                'handoffs': counts.get('handoff', 0),
                'unanswered': counts.get('no_answer', 0) + counts.get('no_product', 0),
                'auto_faqs': db.execute("SELECT count(*) FROM faqs WHERE kind IN ('auto','rule')").fetchone()[0],
                'open_gap_count': db.execute('SELECT count(*) FROM unanswered WHERE resolved_at IS NULL').fetchone()[0],
                'resolution_rate': round(counts.get('answered', 0) / total, 3) if total else 0,
                'feedback_positive': db.execute('SELECT count(*) FROM feedback WHERE rating=1').fetchone()[0],
                'feedback_negative': db.execute('SELECT count(*) FROM feedback WHERE rating=-1').fetchone()[0],
                'top_unanswered': [dict(r) for r in db.execute('''
                    SELECT min(u.id) id,u.question,u.product_id,u.version,
                           coalesce(p.name,'未确认产品') product_name,count(*) count
                    FROM unanswered u LEFT JOIN products p ON p.id=u.product_id
                    WHERE u.resolved_at IS NULL
                    GROUP BY u.question,u.product_id,u.version
                    ORDER BY count DESC,id LIMIT 10''')]}

@app.get('/api/handoffs')
def handoffs():
    with core.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM handoffs ORDER BY created_at DESC LIMIT 50')]

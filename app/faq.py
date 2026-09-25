"""Extract source-backed FAQ entries; generated questions never invent answers."""
from __future__ import annotations

import re

QUESTION = re.compile(r'^(?:Q(?:uestion)?|问(?:题)?)\s*[:：.、]\s*(.+)$', re.IGNORECASE)
ANSWER = re.compile(r'^(?:A(?:nswer)?|答(?:案)?)\s*[:：.、]\s*(.+)$', re.IGNORECASE)
INLINE = re.compile(r'^(?:Q(?:uestion)?|问(?:题)?)\s*[:：.、]\s*(.+?)\s*(?:A(?:nswer)?|答(?:案)?)\s*[:：.、]\s*(.+)$', re.IGNORECASE)


def _fact_body(line: str, product_name: str) -> str:
    body = line.strip()
    if product_name and body.startswith(product_name):
        body = body[len(product_name):].lstrip()
        body = re.sub(r'^\d+(?:\.\d+)*\s*', '', body)
        body = body.removeprefix('的').lstrip()
    return body.rstrip('。.')


def _rule_question(line: str, product_name: str):
    body = _fact_body(line, product_name)
    if not body or '?' in body or '？' in body:
        return None
    limit = re.search(r'(.{0,25}?)最多支持\s*\d+(?:\.\d+)?\s*([^，。；]+)', body)
    if limit:
        return f'{limit.group(1)}最多支持多少{limit.group(2).strip()}？', '参数限制'
    size = re.search(r'(.{2,25}?(?:限制|上限))\s*(?:为|是|:|：)\s*.+?\d+\s*(?:MB|GB|KB|个|条|次)', body, re.IGNORECASE)
    if size:
        return f'{size.group(1)}是多少？', '参数限制'
    feature = re.match(r'^(?:不支持|支持)(.{2,70})$', body)
    if feature:
        return f'是否支持{feature.group(1).strip()}？', '功能支持'
    if '忘记密码' in body:
        return '忘记密码怎么办？', '故障处理'
    if re.search(r'(?:价格|售价)\s*(?:为|是|:|：)\s*\d+', body):
        return '价格是多少？', '价格'
    if re.search(r'(?:保修期|质保期)\s*(?:为|是|:|：)\s*.+', body):
        return '保修期是多久？', '售后保修'
    if body.startswith('适用于') and len(body) > 5:
        return '适用于哪些场景？', '适用场景'
    if body.startswith('兼容') and len(body) > 4:
        return '兼容哪些系统？', '兼容性'
    if '即可' in body and '导出' in body:
        format_name = re.search(r'\b(?:PDF|Word|Excel|CSV)\b', body, re.IGNORECASE)
        return f'如何导出{(" " + format_name.group(0)) if format_name else ""}？', '操作步骤'
    return None


def extract_faqs(text: str, product_name: str):
    """Return question/answer/category/kind rows from cleaned document text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found, explicit_answer_lines, question, answer_lines = [], set(), None, []

    def flush():
        nonlocal question, answer_lines
        if question and answer_lines:
            found.append({'question': question.rstrip('？?') + '？',
                          'answer': '\n'.join(answer_lines), 'category': '文档 FAQ', 'kind': 'explicit'})
        question, answer_lines = None, []

    for index, line in enumerate(lines):
        if line.startswith('#'):
            flush()
            continue
        inline = INLINE.match(line)
        if inline:
            flush()
            found.append({'question': inline.group(1).strip().rstrip('？?') + '？',
                          'answer': inline.group(2).strip(), 'category': '文档 FAQ', 'kind': 'explicit'})
            explicit_answer_lines.add(index)
            continue
        q = QUESTION.match(line)
        a = ANSWER.match(line)
        if q or (line.endswith(('?', '？')) and len(line) <= 120):
            flush()
            question = (q.group(1) if q else line).strip()
            continue
        if a and question:
            answer_lines.append(a.group(1).strip())
            explicit_answer_lines.add(index)
            if answer_lines[-1].endswith(('。', '！', '!', '.')):
                flush()
        elif question:
            answer_lines.append(line)
            explicit_answer_lines.add(index)
            if answer_lines[-1].endswith(('。', '！', '!', '.')):
                flush()
    flush()

    for index, line in enumerate(lines):
        if line.startswith('#') or index in explicit_answer_lines or QUESTION.match(line) or ANSWER.match(line):
            continue
        result = _rule_question(line, product_name)
        if result:
            generated_question, category = result
            found.append({'question': generated_question, 'answer': line,
                          'category': category, 'kind': 'rule'})
            if category == '参数限制' and '上传限制' in generated_question and re.search(r'\d+\s*(?:MB|GB|KB)', line, re.IGNORECASE):
                found.append({'question': generated_question.replace('上传限制', '大小限制'),
                              'answer': line, 'category': category, 'kind': 'rule'})

    # Keep the first source-backed answer for duplicate questions in one document.
    unique, seen = [], set()
    for item in found:
        key = re.sub(r'\s+', '', item['question']).lower()
        if key not in seen and item['answer'].strip():
            seen.add(key)
            unique.append(item)
    return unique

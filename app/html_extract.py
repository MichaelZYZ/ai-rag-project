"""Extract readable product text from uploaded HTML documents."""

import re
from html.parser import HTMLParser


SKIP_TAGS = {'head', 'script', 'style', 'template', 'noscript', 'svg', 'nav', 'footer'}
BLOCK_TAGS = {'article', 'section', 'main', 'div', 'p', 'br', 'hr', 'li',
              'ul', 'ol', 'table', 'tr', 'td', 'th', 'dl', 'dt', 'dd', 'blockquote'}
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
             'meta', 'param', 'source', 'track', 'wbr'}


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self.skip_depth:
            if tag in SKIP_TAGS:
                self.skip_depth += 1
            return
        if tag in SKIP_TAGS:
            self.skip_depth = 1
        elif re.fullmatch(r'h[1-6]', tag):
            self.parts.append('\n' + '#' * int(tag[1]) + ' ')
        elif tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip_depth:
            if tag in SKIP_TAGS:
                self.skip_depth -= 1
            return
        if tag in BLOCK_TAGS or re.fullmatch(r'h[1-6]', tag):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def decode_html(content: bytes) -> str:
    if content.startswith((b'\xff\xfe', b'\xfe\xff')):
        return content.decode('utf-16')
    declared = re.search(br'<meta[^>]+charset\s*=\s*["\']?\s*([a-z0-9_-]+)',
                         content[:4096], re.IGNORECASE)
    encoding = declared.group(1).decode('ascii') if declared else 'utf-8-sig'
    try:
        return content.decode(encoding)
    except LookupError as exc:
        raise ValueError('HTML 声明了不支持的字符编码') from exc
    except UnicodeDecodeError:
        if declared:
            raise ValueError('HTML 内容与声明的字符编码不一致')
        return content.decode('gb18030')


def extract_html_text(content: bytes) -> str:
    parser = VisibleTextParser()
    parser.feed(decode_html(content))
    parser.close()
    return ''.join(parser.parts)

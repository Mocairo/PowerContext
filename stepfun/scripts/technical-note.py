"""Optional artifact build: python technical-note.py [--font /path/to/CJK.ttf].
Requires reportlab and pypdf; does not affect the application runtime.
"""
import argparse
import re
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph
from pypdf import PdfReader

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--font', default='C:/Windows/Fonts/simsun.ttc')
args = parser.parse_args()
pdfmetrics.registerFont(TTFont('CJK', args.font, subfontIndex=0))
pdfmetrics.registerFontFamily('CJK', normal='CJK', bold='CJK', italic='CJK', boldItalic='CJK')
output = root / 'output/pdf/technical-note.pdf'
output.parent.mkdir(parents=True, exist_ok=True)
page_width, page_height = A4
pdf = canvas.Canvas(str(output), pagesize=A4)
pdf.setTitle('Edge Atlas - EmbeddingGemma 2 技术说明')
pdf.setAuthor('Edge Atlas')
pdf.setFillColor(colors.HexColor('#bc583b'))
pdf.rect(27, page_height - 46, 4, 23, fill=1, stroke=0)
pdf.setFillColor(colors.HexColor('#35442d'))
pdf.setFont('CJK', 15)
pdf.drawString(40, page_height - 37, 'Edge Atlas / 端侧知识库技术说明')
pdf.setFont('CJK', 7.4)
pdf.setFillColor(colors.HexColor('#7a8870'))
pdf.drawString(29, page_height - 60, 'seekdb × 阶跃星辰  |  EmbeddingGemma 2  |  本地实测 2026-10-29')
pdf.setStrokeColor(colors.HexColor('#dfe5d7'))
pdf.line(27, page_height - 70, page_width - 27, page_height - 70)
style = ParagraphStyle('body', fontName='CJK', fontSize=9, leading=13.3,
    textColor=colors.HexColor('#404d39'), wordWrap='CJK', spaceAfter=8)
blocks = [b for b in (root / 'TECHNICAL-NOTE.md').read_text(encoding='utf-8').split('\n\n') if not b.startswith('# ')]
column_gap = 18
column_width = (page_width - 54 - column_gap) / 2
paragraphs = []
for block in blocks:
    # Retain source URLs as clickable PDF hyperlinks, using compact visible labels.
    block = re.sub(r'\[([^]]+)\]\(([^)]+)\)', lambda m: f'<link href="{escape(m[2])}" color="#a34b32">{escape(m[1])}</link>', block)
    block = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', block)
    block = re.sub(r'`([^`]+)`', r'<font color="#667e51">\1</font>', block)
    block = block.replace('→', ' / ').replace('≤', '&lt;=').replace('≥', '&gt;=').replace('\n- ', '<br/>- ')
    paragraph = Paragraph(block, style)
    _, height = paragraph.wrap(column_width, page_height)
    paragraphs.append((paragraph, height))
split = min(range(1, len(paragraphs)), key=lambda i: max(sum(h + 8 for _, h in paragraphs[:i]), sum(h + 8 for _, h in paragraphs[i:])))
for col, parts in enumerate((paragraphs[:split], paragraphs[split:])):
    y = page_height - 85
    for paragraph, height in parts:
        if y - height < 37:
            raise RuntimeError('Technical note exceeds one page; shorten source before rebuilding')
        paragraph.drawOn(pdf, 27 + col * (column_width + column_gap), y - height)
        y -= height + 8
pdf.setStrokeColor(colors.HexColor('#e1e5da'))
pdf.line(27, 28, page_width - 27, 28)
pdf.setFillColor(colors.HexColor('#88957b'))
pdf.setFont('CJK', 6.7)
pdf.drawString(28, 17, '完整复现与逐查询原始证据：stepfun/README.md / artifacts/submission-benchmark.json')
pdf.drawRightString(page_width - 28, 17, '1 / 1')
pdf.save()
reader = PdfReader(str(output))
assert len(reader.pages) == 1
text = reader.pages[0].extract_text()
assert '20.23' in text and '0.9583' in text and '346,397,233' in text
print(f'{output} | pages=1 | bytes={output.stat().st_size}')

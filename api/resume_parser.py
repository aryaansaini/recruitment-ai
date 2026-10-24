
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass

MAX_TEXT_CHARS = 50_000          # matches Candidate.resume_text limit in the serializer
MAX_PDF_PAGES = 20               # resumes are short; protects against huge PDFs
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024   # zip-bomb protection

ALLOWED_EXTENSIONS = ('.pdf', '.docx', '.txt')


class ResumeParseError(Exception):
    """File is of an allowed type but could not be read."""


class UnsupportedResumeType(ResumeParseError):
    """File type not allowed."""


# --------------------------------------------------------------------------
# 1. File -> text
# --------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    text = unicodedata.normalize('NFKC', text)          # ligatures (ﬁ -> fi), odd spaces
    text = text.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n')
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()[:MAX_TEXT_CHARS]


def _extract_pdf(data: bytes) -> str:
    import pdfplumber
    if b'%PDF-' not in data[:1024]:
        raise ResumeParseError('This file is not a valid PDF.')
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages[:MAX_PDF_PAGES]
            return '\n\n'.join((page.extract_text() or '') for page in pages)
    except Exception as exc:
        raise ResumeParseError('Could not read this PDF. It may be corrupted or encrypted.') from exc


def _extract_docx(data: bytes) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ResumeParseError('This file is not a valid DOCX.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if sum(info.file_size for info in zf.infolist()) > MAX_DOCX_UNCOMPRESSED:
                raise ResumeParseError('DOCX is too large when unpacked.')
            if 'word/document.xml' not in zf.namelist():
                raise ResumeParseError('This file is not a valid DOCX.')
        doc = Document(io.BytesIO(data))
        lines = []
        for block in doc.iter_inner_content():          # keeps paragraph/table order
            if isinstance(block, Paragraph):
                lines.append(block.text)
            elif isinstance(block, Table):
                for row in block.rows:
                    seen = []
                    for cell in row.cells:               # merged cells repeat; skip duplicates
                        if cell.text not in seen:
                            seen.append(cell.text)
                    lines.append('  '.join(t for t in seen if t.strip()))
        return '\n'.join(lines)
    except ResumeParseError:
        raise
    except Exception as exc:
        raise ResumeParseError('Could not read this DOCX file.') from exc


def _extract_txt(data: bytes) -> str:
    for encoding in ('utf-8-sig', 'utf-16', 'cp1252'):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ResumeParseError('Could not decode this text file.')


def extract_text(data: bytes, filename: str) -> str:
    """Return clean text from a PDF, DOCX or TXT file. Raises ResumeParseError."""
    name = (filename or '').lower()
    if name.endswith('.pdf'):
        raw = _extract_pdf(data)
    elif name.endswith('.docx'):
        raw = _extract_docx(data)
    elif name.endswith('.txt'):
        raw = _extract_txt(data)
    else:
        raise UnsupportedResumeType('Only PDF, DOCX or TXT files are supported.')

    text = _clean_text(raw)
    if not text:
        raise ResumeParseError('No text found in this file (scanned image?). Paste the text manually.')
    return text


# --------------------------------------------------------------------------
# 2. Contact details and skills (used to auto-fill the form)
# --------------------------------------------------------------------------
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_PHONE_RE = re.compile(r'(?<![\w/])\+?\d[\d\s().-]{8,16}\d(?![\w/])')


def extract_email(text: str) -> str:
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else ''


def extract_phone(text: str) -> str:
    for match in _PHONE_RE.finditer(text):
        digits = re.sub(r'\D', '', match.group(0))
        if 10 <= len(digits) <= 13:
            plus = '+' if match.group(0).startswith('+') else ''
            return (plus + digits)[:15]
    return ''


def parse_skill_list(skills_text: str, limit: int = 30) -> list:
    """'Languages: Python, Java\n- Django | SQL' -> ['Python', 'Java', 'Django', 'SQL']"""
    skills, seen = [], set()
    for line in skills_text.split('\n'):
        line = line.strip(' \t-*•●▪◦·')
        if not line:
            continue
        if ':' in line:
            label, rest = line.split(':', 1)
            if len(label.split()) <= 3:
                line = rest
        for item in re.split(r'[,;|•●▪◦·/]|\s{2,}', line):
            item = item.strip(' \t-*.')
            if not item or len(item) > 40 or len(item.split()) > 4:
                continue
            key = item.lower()
            if key not in seen:
                seen.add(key)
                skills.append(item)
    return skills[:limit]


# --------------------------------------------------------------------------
# 3. Text -> sections
# --------------------------------------------------------------------------
SECTION_ALIASES = {
    'summary': ['summary', 'professional summary', 'profile', 'objective', 'career objective',
                'about me', 'about'],
    'skills': ['skills', 'technical skills', 'key skills', 'core competencies', 'skills and tools',
               'tech stack', 'technologies', 'it skills'],
    'experience': ['experience', 'work experience', 'professional experience', 'employment',
                   'employment history', 'work history', 'internship', 'internships'],
    'projects': ['projects', 'personal projects', 'academic projects', 'key projects'],
    'education': ['education', 'academic background', 'qualifications', 'academics',
                  'educational qualification', 'educational qualifications'],
    'certifications': ['certifications', 'certificates', 'licenses', 'courses'],
    'achievements': ['achievements', 'awards', 'honors', 'accomplishments'],
    # NOTE: 'languages' is deliberately NOT here: in tech resumes "Languages: Python, Java"
    # is a skills line, not a section heading.
    'other': ['interests', 'hobbies', 'references', 'declaration'],
}
_ALIAS_TO_SECTION = {alias: sec for sec, aliases in SECTION_ALIASES.items() for alias in aliases}

# "Skills: Python, Django" -> heading "skills" + inline content
_INLINE_HEADING_RE = re.compile(r'^\s*(?P<head>[A-Za-z &/]{3,40}?)\s*[:\-–—]\s*(?P<rest>\S.*)$')


def _normalize_heading(line: str) -> str:
    line = line.strip().strip(':-–—|•●▪◦*# \t').lower()
    line = line.replace('&', ' and ')
    return re.sub(r'\s+', ' ', line).strip()


def _heading_section(line: str):
    """Return (section, inline_rest) if `line` starts a known section, else (None, '')."""
    stripped = line.strip()
    if not stripped:
        return None, ''
    norm = _normalize_heading(stripped)
    if len(stripped) <= 40 and len(norm.split()) <= 4 and norm in _ALIAS_TO_SECTION:
        return _ALIAS_TO_SECTION[norm], ''
    m = _INLINE_HEADING_RE.match(stripped)
    if m:
        head = _normalize_heading(m.group('head'))
        if head in _ALIAS_TO_SECTION:
            return _ALIAS_TO_SECTION[head], m.group('rest').strip()
    return None, ''


def split_sections(text: str) -> dict:
    """
    Split resume text by recognised headings. Text before the first heading is 'header'
    (name / contact details). Unrecognised headings stay inside the previous section.
    """
    sections = {'header': []}
    current = 'header'
    for line in text.split('\n'):
        section, rest = _heading_section(line)
        if section:
            current = section
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
        else:
            sections.setdefault(current, []).append(line)
    result = {}
    for name, lines in sections.items():
        body = '\n'.join(lines).strip()
        if body:
            result[name] = body
    return result


# --------------------------------------------------------------------------
# 4. Sections -> chunks (what Day 6 will embed into pgvector)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Chunk:
    section: str
    index: int
    text: str

    @property
    def embed_text(self) -> str:
        """Text sent to the embedding model: the section label gives it context."""
        return f'{self.section.title()}: {self.text}'


MAX_CHUNKS = 60


def _split_long_unit(unit: str, max_chars: int) -> list:
    """Hard-wrap a single very long line on word boundaries."""
    pieces, current = [], ''
    for word in unit.split():
        while len(word) > max_chars:                 # a single giant "word"
            if current:
                pieces.append(current)
                current = ''
            pieces.append(word[:max_chars])
            word = word[max_chars:]
        if current and len(current) + 1 + len(word) > max_chars:
            pieces.append(current)
            current = word
        else:
            current = f'{current} {word}'.strip()
    if current:
        pieces.append(current)
    return pieces


def _pack(body: str, max_chars: int, overlap: int) -> list:
    """Pack lines into chunks <= max_chars, never cutting a line unless it is too long."""
    units = []
    for line in body.split('\n'):
        line = line.strip()
        if not line:
            continue
        units.extend(_split_long_unit(line, max_chars) if len(line) > max_chars else [line])

    chunks, current = [], []
    for unit in units:
        if current and len('\n'.join(current)) + 1 + len(unit) > max_chars:
            chunks.append('\n'.join(current))
            carry = []                                # overlap: repeat the tail for context
            size = 0
            for prev in reversed(current):
                if size + len(prev) + 1 > overlap:
                    break
                carry.insert(0, prev)
                size += len(prev) + 1
            if carry and len('\n'.join(carry)) + 1 + len(unit) > max_chars:
                carry = []
            current = carry
        current.append(unit)
    if current:
        chunks.append('\n'.join(current))
    return chunks


def chunk_resume(text: str, max_chars: int = 700, overlap: int = 80) -> list:
    """
    Split a resume into section-aware chunks.
    The 'header' section (name, email, phone) is skipped on purpose: it is personal
    contact data with no value for matching, so it never gets embedded.
    """
    chunks, index = [], 0
    for section, body in split_sections(text).items():
        if section == 'header':
            continue
        for piece in _pack(body, max_chars, overlap):
            if len(piece) < 3:
                continue
            chunks.append(Chunk(section=section, index=index, text=piece))
            index += 1
            if index >= MAX_CHUNKS:
                return chunks
    return chunks
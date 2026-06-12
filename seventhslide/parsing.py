"""OpenLyrics/OpenSong/Zefania XML parsing for songs and bibles."""
import os
import re
import html
import xml.etree.ElementTree as ET

from .models import Song





# ---------------------- XML parsing ----------------------

def _inner_xml_of(elem):
    """Return the inner XML text of an element as unicode string (keeps escaped entities and tags)."""
    raw = ET.tostring(elem, encoding='unicode', method='xml')
    open_tag_end = raw.find('>')
    close_tag_start = raw.rfind('<')
    if open_tag_end == -1 or close_tag_start == -1:
        return elem.text or ''
    inner = raw[open_tag_end+1:close_tag_start]
    return inner

_BR_RE = re.compile(r'<(?:[A-Za-z_][\w.-]*:)?br\s*/?>', flags=re.IGNORECASE)
_TAG_TOKEN_RE = re.compile(r'</?[^>]+>')

def _br_to_newlines(text: str) -> str:
    """Normalize HTML line breaks to literal newlines."""
    return _BR_RE.sub('\n', text)

def _sanitize_inline_html(text: str) -> str:
    """Allow only a tiny safe subset of inline tags; escape everything else."""
    if not text:
        return ''

    def _norm_allowed(tagname: str) -> str:
        t = tagname.lower()
        if t == 'strong':
            return 'b'
        if t == 'em':
            return 'i'
        return t

    def repl(m: re.Match) -> str:
        raw = m.group(0)
        is_close = raw.startswith('</')
        name = raw[2:-1].strip().split()[0] if is_close else raw[1:-1].strip().split()[0]
        if not name:
            return html.escape(raw)
        base = _norm_allowed(name)
        if base in {'b', 'i', 'u'}:
            return f"</{base}>" if is_close else f"<{base}>"
        return html.escape(raw)

    return _TAG_TOKEN_RE.sub(repl, text)


def _clean_inner_xml(inner: str) -> str:
    """Normalize one <lines> element's inner XML into plain lyric text:
    <br> → newlines, unescape entities, keep only the safe inline tag subset, strip."""
    inner = _br_to_newlines(inner)
    inner = html.unescape(inner)
    inner = _sanitize_inline_html(inner)
    return inner.strip()


def _ns_lookup(method, tag, ns, *args):
    """Shared impl for _ns_find/_ns_findtext: try namespaced path, fall back to bare path."""
    if ns:
        r = method(f'.//{ns}{tag}', *args)
        if r is not None:
            return r
    return method(f'.//{tag}', *args)

def _ns_find(element, tag, ns=''):
    return _ns_lookup(element.find, tag, ns)

def _ns_findall(element, tag, ns=''):
    if ns:
        r = element.findall(f'.//{ns}{tag}')
        if r:
            return r
    return element.findall(f'.//{tag}')

def _ns_findtext(element, tag, ns='', default=None):
    return _ns_lookup(element.findtext, tag, ns, default)

# Maps OpenLyrics verse-name prefix character → display label base.
# Used in both parse_song_file (prefix→label) and _VerseParser._label_to_code (label→code).
_VERSE_TYPE_MAP: dict[str, str] = {
    'v': 'Verse',
    'c': 'Chorus',
    'p': 'Pre-Chorus',
    'b': 'Bridge',
    'e': 'Ending',
    'i': 'Intro',
    'o': 'Other',
    't': 'Title',
}


def _safe_xml_parse(path):
    """Parse an uploaded XML file with ElementTree, but reject any document that
    carries a DOCTYPE/DTD.

    OpenLyrics, OpenSong and Zefania files never legitimately need a DTD, and a
    DTD is exactly what "billion laughs" entity-expansion and external-entity
    (XXE) attacks require. We make a fast pre-pass with a bare expat parser whose
    StartDoctypeDeclHandler raises the moment a DOCTYPE begins — before any
    internal entity declarations are processed — then hand the file to the normal
    namespace-aware ET.parse for the real tree. (Python's stdlib XML parser is
    explicitly documented as unsafe against malicious input; this restores safety
    without adding a defusedxml dependency that would complicate packaging.)
    """
    from xml.parsers import expat
    with open(path, 'rb') as f:
        data = f.read()

    def _forbid_doctype(*args, **kwargs):
        raise RuntimeError("DOCTYPE/DTD declarations are not allowed in uploaded XML")

    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _forbid_doctype
    guard.Parse(data, True)  # raises if a DOCTYPE is present

    return ET.parse(path)


def parse_song_file(path):
    """Parse an OpenLyrics/OpenSong XML file and return a list of Song objects."""
    try:
        tree = _safe_xml_parse(path)
        root = tree.getroot()
    except Exception as e:
        raise RuntimeError(f"Failed to parse XML: {e}")

    songs = []
    ns = '{http://openlyrics.info/namespace/2009/song}'

    def build_block_text(v_node):
        lines = []
        lines_nodes = _ns_findall(v_node, 'lines', ns)
        if lines_nodes:
            for ln in lines_nodes:
                inner = _inner_xml_of(ln)
                inner = _CHORD_OPEN_RE.sub(lambda m: f"[{m.group(1)}]", inner)
                inner = _CHORD_CLOSE_RE.sub('', inner)
                lines.append(_clean_inner_xml(inner))
        else:
            text = ''.join(v_node.itertext()).strip()
            text = html.unescape(text)
            if text:
                lines.append(text)
        return '\n'.join(lines)

    song_nodes = []
    if root.tag == ns + 'song' or root.tag.endswith('song'):
        song_nodes = [root]
    else:
        song_nodes = _ns_findall(root, 'song', ns)

    if song_nodes:
        for s in song_nodes:
            title = _ns_findtext(s, 'title', ns) or s.findtext('.//name') or 'Untitled'
            title = title.strip()
            lyrics_parts = []
            verse_nodes = _ns_findall(s, 'verse', ns)

            if verse_nodes:
                for v in verse_nodes:
                    # Get name, e.g. "v1", "c1", "o1"
                    vname = v.get('name', 'Misc')
                    # Normalize common names
                    vlower = vname.lower()
                    prefix = vlower[0] if vlower else ''
                    base = _VERSE_TYPE_MAP.get(prefix)
                    if base:
                        label = f"{base}:{vname[1:]}" if len(vname) > 1 else f"{base}:1"
                    else:
                        label = vname
                    
                    block_text = build_block_text(v)
                    if block_text:
                        lyrics_parts.append(f"---[{label}]---\n{block_text}")
            else:
                # No <verse> structure: take the flat <lines> blocks as-is.
                for ln in _ns_findall(s, 'lines', ns):
                    lyrics_parts.append(_clean_inner_xml(_inner_xml_of(ln)))

            lyrics = '\n\n'.join(lyrics_parts) if lyrics_parts else ''
            
            # Authors and Songbooks
            auth_parts = []
            # Handle nested paths by trying with namespace first
            anodes = s.findall(f'.//{ns}authors/{ns}author') if ns else []
            if not anodes:
                anodes = s.findall('.//authors/author')
            for a in anodes:
                atxt = (a.text or '').strip()
                if atxt: auth_parts.append(atxt)

            # Use the first songbook; multiple are rare and not supported here.
            sb_name = ""
            sb_entry = ""
            sbnodes = s.findall(f'.//{ns}songbooks/{ns}songbook') if ns else []
            if not sbnodes:
                sbnodes = s.findall('.//songbooks/songbook')
            if sbnodes:
                sb = sbnodes[0]
                sb_name = sb.get('name', '').strip()
                sb_entry = sb.get('entry', '').strip()

            song_key = (_ns_findtext(s, 'key', ns) or '').strip()
            songs.append(Song(title, lyrics, verse_order=None, authors=auth_parts, songbook_name=sb_name, songbook_entry=sb_entry, key=song_key))
        return songs

    # single-song fallback
    title = root.findtext('.//title') or os.path.splitext(os.path.basename(path))[0]
    parts = [_clean_inner_xml(_inner_xml_of(ln)) for ln in root.findall('.//lines')]
    lyrics = '\n\n'.join(parts)
    songs.append(Song(title.strip(), lyrics))
    return songs


# ---------------------- Font measurement (headless) ----------------------

def parse_bible_file(path):
    """
    Parse Zefania or OpenSong Bible XML.
    Returns (name, copyright, verses_list) where verses_list is [{'book', 'chapter', 'verse', 'text'}]
    """
    try:
        tree = _safe_xml_parse(path)
        root = tree.getroot()
    except Exception as e:
        raise RuntimeError(f"Failed to parse XML: {e}")

    verses = []
    name = root.get('biblename') or root.get('name') or "Unknown Bible"
    copyright = root.get('copyright') or ""
    
    # Check for Zefania format (XMLBIBLE / BIBLEBOOK / CHAPTER / VERS)
    if root.tag.upper() == 'XMLBIBLE':
        for b_node in root.findall('BIBLEBOOK'):
            b_name = b_node.get('bname') or "Unknown Book"
            for c_node in b_node.findall('CHAPTER'):
                c_num = int(c_node.get('cnumber') or 0)
                for v_node in c_node.findall('VERS'):
                    v_num = int(v_node.get('vnumber') or 0)
                    text = v_node.text or ""
                    # Zefania might have tail text or mixed content, but usually text is direct
                    if text:
                        verses.append({'book': b_name, 'chapter': c_num, 'verse': v_num, 'text': text.strip()})
    
    # Check for OpenSong format (bible / b / c / v)
    elif root.tag.lower() == 'bible':
        for b_node in root.findall('b'):
            b_name = b_node.get('n') or "Unknown Book"
            for c_node in b_node.findall('c'):
                c_num = int(c_node.get('n') or 0)
                for v_node in c_node.findall('v'):
                    v_num = int(v_node.get('n') or 0)
                    text = v_node.text or ""
                    if text:
                        verses.append({'book': b_name, 'chapter': c_num, 'verse': v_num, 'text': text.strip()})
    
    # Any other root tag is unsupported: only Zefania (XMLBIBLE) and OpenSong
    # (bible) are recognized, so `verses` stays empty and the caller surfaces it.

    return name, copyright, verses
# Used when importing OpenLyrics/OpenSong XML: turn <chord name="X"> into [X] notation.
_CHORD_OPEN_RE = re.compile(r'<(?:\w+:)?chord\b[^>]*name=[\'"]([^\'"]+)[\'"][^>]*>')
_CHORD_CLOSE_RE = re.compile(r'</(?:\w+:)?chord>')


# ---------------------- Bible reference parsing ----------------------
# Resolves a free-text scripture reference ("John 3:16", "Rom 8:28-30", "1 Cor 13")
# against a bible's actual book list. Keeping the lookup data-driven — exact match,
# then a small alias table, then a unique-prefix match — means any translation
# (including non-English ones) resolves against its own book names; the alias table
# only smooths over the common English abbreviations that aren't simple prefixes.

# Common abbreviations that are NOT a prefix of the full name, mapped to the
# normalized full name. Prefix matching covers the regular cases ("rom"→Romans,
# "1cor"→1 Corinthians, "gen"→Genesis), so only the irregular ones live here.
_BIBLE_BOOK_ALIASES = {
    'gn': 'genesis', 'gen': 'genesis',
    'ex': 'exodus', 'exod': 'exodus', 'exo': 'exodus',
    'lv': 'leviticus', 'nm': 'numbers', 'nb': 'numbers', 'dt': 'deuteronomy',
    'jdg': 'judges', 'jgs': 'judges', 'jsh': 'joshua',
    'ps': 'psalms', 'psa': 'psalms', 'pss': 'psalms', 'pslm': 'psalms', 'psalm': 'psalms',
    'pr': 'proverbs', 'prv': 'proverbs',
    'sg': 'songofsolomon', 'song': 'songofsolomon', 'sos': 'songofsolomon',
    'songofsongs': 'songofsolomon', 'canticles': 'songofsolomon',
    'is': 'isaiah', 'isa': 'isaiah', 'jr': 'jeremiah', 'jer': 'jeremiah',
    'ezk': 'ezekiel', 'ezek': 'ezekiel', 'dn': 'daniel',
    'mt': 'matthew', 'matt': 'matthew',
    'mk': 'mark', 'mrk': 'mark', 'mr': 'mark',
    'lk': 'luke', 'luk': 'luke', 'jn': 'john', 'jhn': 'john',
    'rm': 'romans', 'phil': 'philippians', 'php': 'philippians', 'pp': 'philippians',
    'phm': 'philemon', 'phlm': 'philemon', 'philem': 'philemon',
    'jas': 'james', 'jm': 'james',
    'rev': 'revelation', 'rv': 'revelation', 'apoc': 'revelation',
    # numbered-book irregulars (the regular ones resolve by prefix)
    '1jn': '1john', '2jn': '2john', '3jn': '3john',
    '1kgs': '1kings', '2kgs': '2kings',
    '1chr': '1chronicles', '2chr': '2chronicles',
    '1sm': '1samuel', '2sm': '2samuel',
}

# Trailing "chapter:verse" or "chapter:verse-verse" (allow ':' or '.' and an en dash).
_REF_CV_RE = re.compile(r'(\d+)\s*[:.]\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*$')
# Trailing bare chapter (whole-chapter reference, e.g. "Psalm 23").
_REF_C_RE = re.compile(r'(\d+)\s*$')
# Leading book ordinal in any common form (1 / I / First).
_BOOK_NUM_PREFIX_RE = re.compile(r'^\s*([1-3]|i{1,3}|first|second|third)\b[\s.]*', re.IGNORECASE)
_NORMALIZE_RE = re.compile(r'[^a-z0-9]')
_ORDINAL_WORDS = {'i': '1', 'ii': '2', 'iii': '3', 'first': '1', 'second': '2', 'third': '3'}


def _normalize_book(s: str) -> str:
    """Normalize a book name/token for matching: fold a leading roman/word ordinal
    ('I', 'First') to a digit, then lowercase and drop everything but letters/digits,
    so '1 John', 'I John' and 'First John' all collapse to '1john'."""
    s = (s or '').strip()
    m = _BOOK_NUM_PREFIX_RE.match(s)
    if m:
        num = _ORDINAL_WORDS.get(m.group(1).lower(), m.group(1))
        s = num + ' ' + s[m.end():]
    return _NORMALIZE_RE.sub('', s.lower())


def resolve_bible_book(books, token):
    """Resolve a free-text book token to one of `books` (a bible's actual book names).

    Tries, in order: exact normalized match, the alias table, then a unique prefix
    match. Returns the matched book string, or None if there's no unambiguous match.
    """
    if not token or not books:
        return None
    # First normalized spelling wins, preserving the bible's canonical book order.
    norm_map = {}
    for b in books:
        norm_map.setdefault(_normalize_book(b), b)
    t = _normalize_book(token)
    if not t:
        return None
    if t in norm_map:
        return norm_map[t]
    alias = _BIBLE_BOOK_ALIASES.get(t)
    if alias and alias in norm_map:
        return norm_map[alias]
    for target in ([t, alias] if alias else [t]):
        hits = [b for n, b in norm_map.items() if n.startswith(target)]
        if len(hits) == 1:
            return hits[0]
    return None


def parse_bible_reference(reference, books):
    """Parse a free-text scripture reference against a bible's `books` list.

    Handles 'John 3:16', 'Rom 8:28-30', '1 Cor 13:4-7', whole chapters ('Ps 23'),
    and leading ordinals as digits/romans/words. Returns
    {book, chapter, verse_start, verse_end} (verse_* are None for a whole chapter),
    or None when the string can't be parsed or the book can't be resolved.
    """
    ref = (reference or '').strip()
    if not ref:
        return None
    verse_start = verse_end = None
    m = _REF_CV_RE.search(ref)
    if m:
        chapter = int(m.group(1))
        verse_start = int(m.group(2))
        verse_end = int(m.group(3)) if m.group(3) else verse_start
        book_part = ref[:m.start()]
    else:
        m = _REF_C_RE.search(ref)
        if not m:
            return None
        chapter = int(m.group(1))
        book_part = ref[:m.start()]
    book = resolve_bible_book(books, book_part)
    if not book or chapter < 1:
        return None
    if verse_start is not None and verse_end < verse_start:
        verse_start, verse_end = verse_end, verse_start
    return {'book': book, 'chapter': chapter,
            'verse_start': verse_start, 'verse_end': verse_end}


__all__ = [
    '_BR_RE',
    '_CHORD_CLOSE_RE',
    '_CHORD_OPEN_RE',
    '_TAG_TOKEN_RE',
    '_VERSE_TYPE_MAP',
    '_br_to_newlines',
    '_clean_inner_xml',
    '_inner_xml_of',
    '_ns_find',
    '_ns_findall',
    '_ns_findtext',
    '_ns_lookup',
    '_safe_xml_parse',
    '_sanitize_inline_html',
    'parse_bible_file',
    'parse_bible_reference',
    'parse_song_file',
    'resolve_bible_book',
]

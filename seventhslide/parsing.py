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
    'parse_song_file',
]

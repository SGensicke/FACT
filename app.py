from flask import Flask, request, jsonify, render_template, session, has_request_context
import sqlite3, os, re
from lxml import etree
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("FACT_SECRET_KEY", "fact-local-project-session")
APP_DIR = os.path.dirname(__file__)
DEFAULT_DB = os.path.join(APP_DIR, "variantendb.sqlite")
DEFAULT_XML_DIR = os.path.join(APP_DIR, "xml")
PROJECTS_DIR = os.path.join(APP_DIR, "projects")

def _project_paths(project_id=None):
    project_id = project_id or (
        session.get("project_id", "default") if has_request_context() else "default"
    )
    if project_id == "default":
        return DEFAULT_DB, DEFAULT_XML_DIR
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project_id):
        raise ValueError("Invalid project id")
    project_dir = os.path.join(PROJECTS_DIR, project_id)
    return os.path.join(project_dir, "variantendb.sqlite"), os.path.join(project_dir, "xml")

def get_xml_dir():
    return _project_paths()[1]

def _project_list():
    projects = [{"id": "default", "name": "Default"}]
    if os.path.isdir(PROJECTS_DIR):
        for project_id in sorted(os.listdir(PROJECTS_DIR)):
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project_id):
                continue
            db_path, _ = _project_paths(project_id)
            if os.path.isfile(db_path):
                projects.append({"id": project_id, "name": project_id.replace("-", " ").title()})
    return projects

# ── Database setup ────────────────────────────────────────────────────────────

def get_db(db_path=None):
    db_path = db_path or _project_paths()[0]
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db(db_path=None):
    with get_db(db_path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS tags (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL UNIQUE,
            color TEXT
        );
        CREATE TABLE IF NOT EXISTS formulae (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_id  INTEGER REFERENCES tags(id) ON DELETE SET NULL,
            formula TEXT NOT NULL,
            note    TEXT
        );
        CREATE TABLE IF NOT EXISTS formula_tags (
            formula_id INTEGER NOT NULL REFERENCES formulae(id) ON DELETE CASCADE,
            tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (formula_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS variants (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            formula_id   INTEGER NOT NULL REFERENCES formulae(id) ON DELETE CASCADE,
            text_xmlid   TEXT NOT NULL,
            string       TEXT NOT NULL,
            string_xml   TEXT,
            offset_start INTEGER,
            offset_end   INTEGER,
            verified     INTEGER DEFAULT 0,
            note         TEXT,
            tag_region   TEXT,
            tag_section  TEXT,
            tag_label    TEXT,
            overlaps     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS regions (
            abbr TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        INSERT OR IGNORE INTO regions VALUES ('Arr','Arras');
        CREATE TABLE IF NOT EXISTS xml_cache (
            xmlid        TEXT PRIMARY KEY,
            file         TEXT NOT NULL,
            tag          TEXT NOT NULL,
            tag_region   TEXT,
            tag_section  TEXT,
            inner_xml    TEXT,
            preview      TEXT,
            position     INTEGER
        );
        """)
    # Migrate: add color column to tags if it doesn't exist yet
    with get_db(db_path) as db:
        try:
            db.execute("ALTER TABLE tags ADD COLUMN color TEXT")
        except sqlite3.OperationalError:
            pass

init_db()

# ── XML Cache ─────────────────────────────────────────────────────────────────

import json as _json

def _inner_xml(el):
    """Returns the inner XML content of an element (without the outermost tag)."""
    raw = etree.tostring(el, encoding='unicode')
    i = raw.index('>') + 1
    j = raw.rindex('</')
    return raw[i:j]

def _plaintext_preview(inner):
    """Short preview text: strip tags, normalize whitespace."""
    text = re.sub(r'<[^>]+>', ' ', inner)
    return re.sub(r'\s+', ' ', text).strip()[:80]

def _region_from_xmlid(xmlid, db):
    m = re.match(r'^UBf([A-Z][a-z]{2})', xmlid)
    if not m:
        return None
    abbr = m.group(1)
    row = db.execute("SELECT name FROM regions WHERE abbr=?", (abbr,)).fetchone()
    return row["name"] if row else abbr

def _doc_id_from_xmlid(xmlid):
    if not xmlid:
        return None
    m = re.match(r'^(.+?-\d+[A-Za-z]*)(?:_.*)?$', xmlid)
    return m.group(1) if m else xmlid

def _compute_string_xml(text_xmlid, offset_start, offset_end):
    if offset_start is None or offset_end is None:
        return None
    with get_db() as db:
        cached = db.execute(
            "SELECT inner_xml FROM xml_cache WHERE xmlid=?", (text_xmlid,)
        ).fetchone()
    if not cached or not cached["inner_xml"]:
        return None
    return cached["inner_xml"][offset_start:offset_end]

def build_xml_cache():
    """Reads all XML files and rebuilds xml_cache. Runs on every start."""
    xml_dir = get_xml_dir()
    if not os.path.isdir(xml_dir):
        return
    entries = []
    with get_db() as db:
        db.execute("DELETE FROM xml_cache")
        db.commit()
        # Migrations: add columns if missing
        for col, typedef in [('inner_xml', 'TEXT'), ('offset_start', 'INTEGER'),
                              ('offset_end', 'INTEGER'), ('string_xml', 'TEXT'),
                              ('position', 'INTEGER')]:
            try:
                db.execute(f"ALTER TABLE xml_cache ADD COLUMN {col} {typedef}")
                db.commit()
            except Exception:
                pass
        for col, typedef in [('offset_start', 'INTEGER'), ('offset_end', 'INTEGER'),
                              ('string_xml', 'TEXT'), ('tag_label', 'TEXT'),
                              ('overlaps', 'INTEGER DEFAULT 0')]:
            try:
                db.execute(f"ALTER TABLE variants ADD COLUMN {col} {typedef}")
                db.commit()
            except Exception:
                pass
        # Migrate formula_tags from tag_id if needed
        try:
            db.execute("""
                INSERT OR IGNORE INTO formula_tags (formula_id, tag_id)
                SELECT id, tag_id FROM formulae WHERE tag_id IS NOT NULL
            """)
            db.commit()
        except Exception:
            pass

        pos = 0
        for fname in sorted(os.listdir(xml_dir)):
            if not fname.endswith(".xml"):
                continue
            try:
                tree = etree.parse(os.path.join(xml_dir, fname))
                root = tree.getroot()
                for el in root.iter():
                    xmlid = el.get("{http://www.w3.org/XML/1998/namespace}id")
                    if not xmlid:
                        continue
                    tag         = etree.QName(el.tag).localname
                    inner       = _inner_xml(el)
                    preview     = _plaintext_preview(inner)
                    tag_region  = _region_from_xmlid(xmlid, db)
                    tag_section = tag
                    entries.append((xmlid, fname, tag, tag_region, tag_section, inner, preview, pos))
                    pos += 1
            except Exception as exc:
                print(f"[XML-Cache] Error processing {fname}: {exc}")
                continue
        if entries:
            db.executemany("""
                INSERT INTO xml_cache
                  (xmlid, file, tag, tag_region, tag_section, inner_xml, preview, position)
                VALUES (?,?,?,?,?,?,?,?)
            """, entries)
        _verify_all_variants(db)
    print(f"[XML-Cache] {len(entries)} elements loaded.")


# ── Helper functions ──────────────────────────────────────────────────────────

def extract_tags(text_xmlid):
    """Returns (tag_region, tag_section) preferring xml_cache."""
    with get_db() as db:
        row = db.execute(
            "SELECT tag_region, tag_section FROM xml_cache WHERE xmlid=?", (text_xmlid,)
        ).fetchone()
        if row:
            return row["tag_region"], row["tag_section"]
        tag_region = _region_from_xmlid(text_xmlid, db)
    tag_section = None
    xml_dir = get_xml_dir()
    if os.path.isdir(xml_dir):
        ns = {"xml": "http://www.w3.org/XML/1998/namespace"}
        for fname in sorted(os.listdir(xml_dir)):
            if not fname.endswith(".xml"):
                continue
            try:
                tree = etree.parse(os.path.join(xml_dir, fname))
                elems = tree.getroot().xpath(f'//*[@xml:id="{text_xmlid}"]', namespaces=ns)
                if elems:
                    tag_section = etree.QName(elems[0].tag).localname
                    break
            except Exception:
                continue
    return tag_region, tag_section


def _update_overlaps(db, text_xmlid):
    """Marks variants for text_xmlid with overlaps=1 if they overlap others."""
    rows = db.execute(
        "SELECT id, offset_start, offset_end FROM variants WHERE text_xmlid=? AND offset_start IS NOT NULL AND offset_end IS NOT NULL",
        (text_xmlid,)
    ).fetchall()
    ids_with_overlap = set()
    r = list(rows)
    for i in range(len(r)):
        for j in range(i + 1, len(r)):
            a, b = r[i], r[j]
            if a["offset_start"] < b["offset_end"] and b["offset_start"] < a["offset_end"]:
                ids_with_overlap.add(a["id"])
                ids_with_overlap.add(b["id"])
    db.execute("UPDATE variants SET overlaps=0 WHERE text_xmlid=?", (text_xmlid,))
    for vid in ids_with_overlap:
        db.execute("UPDATE variants SET overlaps=1 WHERE id=?", (vid,))


def _verify_offset(db, text_xmlid, string, offset_start, offset_end):
    """Checks if inner_xml[offset_start:offset_end] == string. Returns 1, -1, or 0."""
    cached = db.execute("SELECT inner_xml FROM xml_cache WHERE xmlid=?", (text_xmlid,)).fetchone()
    if not cached or not cached["inner_xml"]:
        return -1
    inner = cached["inner_xml"]
    if offset_start is None or offset_end is None:
        return 1 if string in inner else -1
    if offset_end > len(inner):
        return -1
    return 1 if inner[offset_start:offset_end] == string else -1


def _verify_all_variants(db):
    """Verifies and repairs offsets of all variants after a cache rebuild."""
    rows = db.execute(
        "SELECT id, text_xmlid, string, offset_start, offset_end FROM variants"
    ).fetchall()
    repaired = invalid = valid = 0
    for row in rows:
        vid, xmlid, string, os_, oe_ = (
            row["id"], row["text_xmlid"], row["string"], row["offset_start"], row["offset_end"]
        )
        cached = db.execute("SELECT inner_xml FROM xml_cache WHERE xmlid=?", (xmlid,)).fetchone()
        if not cached or not cached["inner_xml"]:
            db.execute("UPDATE variants SET verified=-1 WHERE id=?", (vid,))
            invalid += 1
            continue
        inner = cached["inner_xml"]
        if os_ is not None and oe_ is not None and oe_ <= len(inner) and inner[os_:oe_] == string:
            db.execute("UPDATE variants SET verified=1 WHERE id=?", (vid,))
            valid += 1
            continue
        idx = inner.find(string)
        if idx >= 0:
            db.execute(
                "UPDATE variants SET offset_start=?, offset_end=?, string_xml=?, verified=1 WHERE id=?",
                (idx, idx + len(string), string, vid)
            )
            repaired += 1
        else:
            db.execute("UPDATE variants SET verified=-1 WHERE id=?", (vid,))
            invalid += 1
    db.commit()
    print(f"[Verify] {len(rows)} variants: {valid} valid, {repaired} repaired, {invalid} not found.")


build_xml_cache()

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    projects = _project_list()
    active_project = session.get("project_id", "default")
    return render_template("index.html", projects=projects, active_project=active_project)


@app.route("/api/projects", methods=["POST"])
def create_project():
    if request.is_json:
        name = (request.json or {}).get("name", "").strip()
        database_file = None
    else:
        name = request.form.get("name", "").strip()
        database_file = request.files.get("database")
    project_id = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    if not project_id:
        return jsonify({"error": "Project name must contain letters or numbers"}), 400
    if len(project_id) > 64:
        return jsonify({"error": "Project name is too long"}), 400
    db_path, xml_dir = _project_paths(project_id)
    if os.path.exists(db_path):
        return jsonify({"error": "A project with that name already exists"}), 409
    os.makedirs(xml_dir, exist_ok=True)
    if database_file and database_file.filename:
        if not database_file.filename.lower().endswith((".sqlite", ".db")):
            return jsonify({"error": "Choose a .sqlite or .db FACT database"}), 400
        import tempfile
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(db_path), suffix=".sqlite", delete=False) as temp:
                temp_path = temp.name
            database_file.save(temp_path)
            source = sqlite3.connect(temp_path)
            try:
                integrity = source.execute("PRAGMA quick_check").fetchone()[0]
                columns = {
                    table: {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
                    for table in ("tags", "formulae", "variants")
                }
            finally:
                source.close()
            required_columns = {
                "tags": {"id", "name"},
                "formulae": {"id", "formula"},
                "variants": {"id", "formula_id", "text_xmlid", "string"},
            }
            if integrity != "ok" or any(
                not required.issubset(columns[table])
                for table, required in required_columns.items()
            ):
                return jsonify({"error": "This is not a compatible FACT database"}), 400
            os.replace(temp_path, db_path)
            temp_path = None
        except sqlite3.DatabaseError:
            return jsonify({"error": "This is not a readable SQLite database"}), 400
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
    init_db(db_path)
    session["project_id"] = project_id
    if database_file and database_file.filename and any(
        filename.endswith(".xml") for filename in os.listdir(xml_dir)
    ):
        build_xml_cache()
    return jsonify({"ok": True, "project": {"id": project_id, "name": name}}), 201


@app.route("/api/projects/activate", methods=["POST"])
def activate_project():
    project_id = (request.json or {}).get("id", "")
    if not isinstance(project_id, str) or not project_id:
        return jsonify({"error": "Project not found"}), 404
    try:
        db_path, xml_dir = _project_paths(project_id)
    except ValueError:
        return jsonify({"error": "Project not found"}), 404
    if project_id != "default" and not os.path.isfile(db_path):
        return jsonify({"error": "Project not found"}), 404
    session["project_id"] = project_id
    if os.path.isdir(xml_dir) and any(name.endswith(".xml") for name in os.listdir(xml_dir)):
        with get_db() as db:
            cached_count = db.execute("SELECT COUNT(*) FROM xml_cache").fetchone()[0]
        if not cached_count:
            build_xml_cache()
    return jsonify({"ok": True, "id": project_id})


@app.route("/api/projects/xml", methods=["POST"])
def upload_project_xml():
    files = request.files.getlist("files")
    if not files or not any(file.filename for file in files):
        return jsonify({"error": "Choose one or more XML files"}), 400
    xml_dir = get_xml_dir()
    os.makedirs(xml_dir, exist_ok=True)
    imported, errors = [], []
    import tempfile
    for file in files:
        filename = secure_filename(file.filename or "")
        if not filename.lower().endswith(".xml"):
            errors.append(file.filename or "(unnamed file)")
            continue
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=xml_dir, suffix=".xml", delete=False) as temp:
                temp_path = temp.name
            file.save(temp_path)
            etree.parse(temp_path)
            os.replace(temp_path, os.path.join(xml_dir, filename))
            imported.append(filename)
        except Exception:
            errors.append(file.filename or filename)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
    if imported:
        build_xml_cache()
    return jsonify({"ok": bool(imported), "files": imported, "errors": errors}), (200 if imported else 400)


# Tags
@app.route("/api/tags", methods=["GET"])
def get_tags():
    with get_db() as db:
        rows = db.execute("SELECT * FROM tags ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/tags", methods=["POST"])
def post_tag():
    name = request.json.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    with get_db() as db:
        try:
            cur = db.execute("INSERT INTO tags (name) VALUES (?)", (name,))
            return jsonify({"id": cur.lastrowid, "name": name, "color": None}), 201
        except sqlite3.IntegrityError:
            return jsonify({"error": "Tag already exists"}), 409

@app.route("/api/tags/<int:tid>", methods=["PUT"])
def put_tag(tid):
    name = request.json.get("name", "").strip()
    with get_db() as db:
        db.execute("UPDATE tags SET name=? WHERE id=?", (name, tid))
    return jsonify({"ok": True})

@app.route("/api/tags/<int:tid>/color", methods=["PATCH"])
def patch_tag_color(tid):
    """Set the highlight color for a tag (hex string or null)."""
    color = request.json.get("color")  # e.g. "#e05050" or null
    with get_db() as db:
        db.execute("UPDATE tags SET color=? WHERE id=?", (color, tid))
    return jsonify({"ok": True, "id": tid, "color": color})

@app.route("/api/tags/<int:tid>", methods=["DELETE"])
def delete_tag(tid):
    with get_db() as db:
        db.execute("DELETE FROM tags WHERE id=?", (tid,))
    return jsonify({"ok": True})


# Formulae
def _formula_row_to_dict(db, row):
    d = dict(row)
    tag_rows = db.execute(
        "SELECT t.id, t.name, t.color FROM formula_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.formula_id=? ORDER BY t.name",
        (d["id"],)
    ).fetchall()
    d["tag_ids"]    = [r["id"]    for r in tag_rows]
    d["tag_names"]  = [r["name"]  for r in tag_rows]
    d["tag_colors"] = [r["color"] for r in tag_rows]
    # First non-null color for convenience
    d["tag_color"]  = next((c for c in d["tag_colors"] if c), None)
    # Legacy compat
    d["tag_name"]  = ", ".join(d["tag_names"]) if d["tag_names"] else None
    # Legacy compat
    d["kat_name"] = d["tag_name"]
    # Remap DB column names for JS compatibility
    d["kategorie_ids"]   = d["tag_ids"]
    d["kategorie_names"] = d["tag_names"]
    d["kategorie_name"]  = d["tag_name"]
    d["formulierung"]    = d.get("formula", d.get("formulierung"))
    d["notiz"]           = d.get("note", d.get("notiz"))
    return d

def _set_formula_tags(db, formula_id, tag_ids):
    db.execute("DELETE FROM formula_tags WHERE formula_id=?", (formula_id,))
    for tid in (tag_ids or []):
        try:
            db.execute("INSERT OR IGNORE INTO formula_tags (formula_id, tag_id) VALUES (?,?)", (formula_id, tid))
        except Exception:
            pass

@app.route("/api/formulae", methods=["GET"])
def get_formulae():
    tag_ids      = request.args.getlist("tag_id")
    tag_regions  = request.args.getlist("tag_region")
    tag_sections = request.args.getlist("tag_section")
    # Legacy param aliases
    if not tag_ids:
        tag_ids = request.args.getlist("kategorie_id")
    if not tag_sections:
        tag_sections = request.args.getlist("tag_textteil")
    with get_db() as db:
        where, params = [], []
        if tag_ids:
            phs = ",".join("?" * len(tag_ids))
            where.append(f"f.id IN (SELECT formula_id FROM formula_tags WHERE tag_id IN ({phs}))")
            params.extend(tag_ids)
        if tag_regions:
            reg_map_rows = db.execute("SELECT abbr, name FROM regions").fetchall()
            reg_abbr = {r["name"]: r["abbr"] for r in reg_map_rows}
            reg_name = {r["abbr"]: r["name"] for r in reg_map_rows}
            expanded = set()
            for v in tag_regions:
                expanded.add(v)
                if v in reg_abbr: expanded.add(reg_abbr[v])
                if v in reg_name: expanded.add(reg_name[v])
            exp_phs = ",".join("?" * len(expanded))
            where.append(f"f.id IN (SELECT formula_id FROM variants WHERE tag_region IN ({exp_phs}))")
            params.extend(expanded)
        if tag_sections:
            phs = ",".join("?" * len(tag_sections))
            where.append(f"f.id IN (SELECT formula_id FROM variants WHERE tag_section IN ({phs}))")
            params.extend(tag_sections)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = db.execute(f"""
            SELECT f.*,
                GROUP_CONCAT(DISTINCT v.tag_region)  as tags_region,
                GROUP_CONCAT(DISTINCT v.tag_section) as tags_textteil,
                COUNT(DISTINCT v.id) as varianten_anzahl
            FROM formulae f
            LEFT JOIN variants v ON v.formula_id=f.id
            {where_sql}
            GROUP BY f.id
            ORDER BY f.formula
        """, params).fetchall()
        result = [_formula_row_to_dict(db, r) for r in rows]
    return jsonify(result)

@app.route("/api/formulae", methods=["POST"])
def post_formula():
    d = request.json
    formula = d.get("formula") or d.get("formulierung", "")
    note    = d.get("note") or d.get("notiz")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO formulae (formula, note) VALUES (?,?)", (formula, note)
        )
        fid = cur.lastrowid
        _set_formula_tags(db, fid, d.get("tag_ids") or d.get("kategorie_ids", []))
        row = db.execute("SELECT * FROM formulae WHERE id=?", (fid,)).fetchone()
        result = _formula_row_to_dict(db, row)
    return jsonify(result), 201

@app.route("/api/formulae/<int:fid>", methods=["PUT"])
def put_formula(fid):
    d = request.json
    formula = d.get("formula") or d.get("formulierung", "")
    note    = d.get("note") or d.get("notiz")
    with get_db() as db:
        db.execute("UPDATE formulae SET formula=?, note=? WHERE id=?", (formula, note, fid))
        _set_formula_tags(db, fid, d.get("tag_ids") or d.get("kategorie_ids", []))
        row = db.execute("SELECT * FROM formulae WHERE id=?", (fid,)).fetchone()
        result = _formula_row_to_dict(db, row)
    return jsonify(result)

@app.route("/api/formulae/<int:fid>", methods=["DELETE"])
def delete_formula(fid):
    with get_db() as db:
        db.execute("DELETE FROM formulae WHERE id=?", (fid,))
    return jsonify({"ok": True})


# Variants
@app.route("/api/variants", methods=["GET"])
def get_variants():
    formula_id = request.args.get("formula_id") or request.args.get("ideal_id")
    text_xmlid = request.args.get("text_xmlid")
    with get_db() as db:
        if formula_id:
            rows = db.execute("""
                SELECT v.*,
                       (SELECT tg.color FROM formula_tags ft2 JOIN tags tg ON tg.id=ft2.tag_id
                        WHERE ft2.formula_id=v.formula_id AND tg.color IS NOT NULL LIMIT 1) as tag_color
                FROM variants v WHERE v.formula_id=? ORDER BY v.text_xmlid
            """, (formula_id,)).fetchall()
        elif text_xmlid:
            rows = db.execute("""
                SELECT v.*, f.formula as formulierung, f.formula as formulierung,
                       (SELECT GROUP_CONCAT(t2.name)
                        FROM formula_tags ft JOIN tags t2 ON t2.id=ft.tag_id
                        WHERE ft.formula_id=v.formula_id) as kategorie_names,
                       (SELECT GROUP_CONCAT(tg.color, ',')
                        FROM formula_tags ft2 JOIN tags tg ON tg.id=ft2.tag_id
                        WHERE ft2.formula_id=v.formula_id AND tg.color IS NOT NULL
                        LIMIT 1) as tag_color
                FROM variants v
                JOIN formulae f ON f.id=v.formula_id
                WHERE v.text_xmlid=?
                ORDER BY f.formula
            """, (text_xmlid,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM variants ORDER BY text_xmlid").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/variants", methods=["POST"])
def post_variant():
    d = request.json
    formula_id   = d.get("formula_id") or d.get("ideal_id")
    tag_region, tag_section = extract_tags(d["text_xmlid"])
    offset_start = d.get("offset_start")
    offset_end   = d.get("offset_end")
    string_xml   = _compute_string_xml(d["text_xmlid"], offset_start, offset_end)
    with get_db() as db:
        status = _verify_offset(db, d["text_xmlid"], d["string"], offset_start, offset_end)
        cur = db.execute(
            """INSERT INTO variants
               (formula_id, text_xmlid, string, string_xml, offset_start, offset_end,
                verified, note, tag_region, tag_section)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (formula_id, d["text_xmlid"], d["string"], string_xml,
             offset_start, offset_end, status, d.get("note") or d.get("notiz"), tag_region, tag_section)
        )
        _update_overlaps(db, d["text_xmlid"])
        row = db.execute("SELECT * FROM variants WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201

@app.route("/api/variants/<int:vid>", methods=["GET"])
def get_variant(vid):
    with get_db() as db:
        row = db.execute("""
            SELECT v.*, f.formula as formulierung, f.note as ideal_notiz,
                   t.name as kat_name, t.id as kat_id
            FROM variants v
            JOIN formulae f ON f.id = v.formula_id
            LEFT JOIN tags t ON t.id = f.tag_id
            WHERE v.id = ?
        """, (vid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        cached = db.execute(
            "SELECT inner_xml, preview, file, tag FROM xml_cache WHERE xmlid=?",
            (row["text_xmlid"],)
        ).fetchone()
    result = dict(row)
    if cached:
        result["xml_inner"]   = cached["inner_xml"]
        result["xml_preview"] = cached["preview"]
        result["xml_datei"]   = cached["file"]
        result["xml_tag"]     = cached["tag"]
    return jsonify(result)

@app.route("/api/variants/<int:vid>", methods=["PUT"])
def put_variant(vid):
    d = request.json or {}
    formula_id = d.get("formula_id") or d.get("ideal_id")

    # Bulk move only: update formula_id without requiring text_xmlid/string fields
    if formula_id and "text_xmlid" not in d and "string" not in d and "offset_start" not in d and "offset_end" not in d:
        with get_db() as db:
            db.execute("UPDATE variants SET formula_id=? WHERE id=?", (formula_id, vid))
            row = db.execute("SELECT * FROM variants WHERE id=?", (vid,)).fetchone()
        return jsonify(dict(row))

    if "text_xmlid" not in d:
        return jsonify({"error": "text_xmlid is required for full variant updates"}), 400

    tag_region, tag_section = extract_tags(d["text_xmlid"])
    offset_start = d.get("offset_start")
    offset_end = d.get("offset_end")
    string_xml = _compute_string_xml(d["text_xmlid"], offset_start, offset_end)
    with get_db() as db:
        status = _verify_offset(db, d["text_xmlid"], d.get("string"), offset_start, offset_end)
        db.execute(
            """UPDATE variants SET formula_id=?, text_xmlid=?, string=?, string_xml=?,
               offset_start=?, offset_end=?, verified=?, note=?, tag_region=?, tag_section=?
               WHERE id=?""",
            (formula_id, d["text_xmlid"], d.get("string"), string_xml,
             offset_start, offset_end, status,
             d.get("note") or d.get("notiz"),
             tag_region, tag_section, vid)
        )
        _update_overlaps(db, d["text_xmlid"])
        row = db.execute("SELECT * FROM variants WHERE id=?", (vid,)).fetchone()
    return jsonify(dict(row))

@app.route("/api/variants/<int:vid>", methods=["DELETE"])
def delete_variant(vid):
    with get_db() as db:
        db.execute("DELETE FROM variants WHERE id=?", (vid,))
    return jsonify({"ok": True})

@app.route("/api/variants/<int:vid>/verify", methods=["POST"])
def verify_variant(vid):
    with get_db() as db:
        row = db.execute("SELECT * FROM variants WHERE id=?", (vid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        cached = db.execute("SELECT inner_xml FROM xml_cache WHERE xmlid=?", (row["text_xmlid"],)).fetchone()
        inner = cached["inner_xml"] if cached and cached["inner_xml"] else None
        status = -1
        if inner and row["string"] in inner:
            status = 1
        db.execute("UPDATE variants SET verified=? WHERE id=?", (status, vid))
    return jsonify({"verified": status})


# Filter tags (region/section values)
@app.route("/api/filter_tags", methods=["GET"])
def get_filter_tags():
    """All distinct tag_region/tag_section values for sidebar filters."""
    with get_db() as db:
        raw_regions = [r[0] for r in db.execute(
            "SELECT DISTINCT tag_region FROM variants WHERE tag_region IS NOT NULL ORDER BY tag_region"
        ).fetchall()]
        reg_map = {r["abbr"]: r["name"] for r in
                   db.execute("SELECT abbr, name FROM regions").fetchall()}
        name_set = set(reg_map.values())
        regions = sorted({
            reg_map.get(v, v) if v not in name_set else v
            for v in raw_regions
        })
        sections = [r[0] for r in db.execute(
            "SELECT DISTINCT tag_section FROM variants WHERE tag_section IS NOT NULL ORDER BY tag_section"
        ).fetchall()]
    return jsonify({"regionen": regions, "textteile": sections})


# Regions
@app.route("/api/regions", methods=["GET"])
def get_regions():
    with get_db() as db:
        rows = db.execute("SELECT * FROM regions ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/regions", methods=["POST"])
def post_region():
    d = request.json
    with get_db() as db:
        try:
            db.execute("INSERT INTO regions (abbr, name) VALUES (?,?)", (d["abbr"] or d.get("kuerzel"), d["name"]))
        except Exception:
            db.execute("UPDATE regions SET name=? WHERE abbr=?", (d["name"], d.get("abbr") or d.get("kuerzel")))
    return jsonify({"ok": True}), 201


# Search
@app.route("/api/search/texts", methods=["GET"])
def search_texts():
    """All texts with variant count statistics and filter metadata."""
    with get_db() as db:
        rows = db.execute("""
            SELECT x.xmlid AS text_xmlid,
                   COUNT(v.id) as anzahl_varianten,
                   SUM(CASE WHEN v.verified=1 THEN 1 ELSE 0 END) as verifiziert,
                   SUM(CASE WHEN v.verified=-1 THEN 1 ELSE 0 END) as fehler,
                   GROUP_CONCAT(DISTINCT v.tag_region) as tag_regions,
                   GROUP_CONCAT(DISTINCT v.tag_section) as tag_sections,
                   GROUP_CONCAT(DISTINCT ft.tag_id) as tag_ids
            FROM xml_cache x
            LEFT JOIN variants v ON v.text_xmlid = x.xmlid
            LEFT JOIN formula_tags ft ON ft.formula_id = v.formula_id
            GROUP BY x.xmlid
            ORDER BY x.xmlid
        """).fetchall()
        # Build region abbr→name map for display
        reg_map = {r["abbr"]: r["name"] for r in
                   db.execute("SELECT abbr, name FROM regions").fetchall()}
    result = []
    for r in rows:
        d = dict(r)
        # Expand region abbreviations to names
        raw_regions = [x for x in (d.get("tag_regions") or "").split(",") if x]
        name_set = set(reg_map.values())
        d["tag_regions_list"] = sorted({
            reg_map.get(v, v) if v not in name_set else v
            for v in raw_regions
        })
        d["tag_sections_list"] = [x for x in (d.get("tag_sections") or "").split(",") if x]
        d["tag_ids_list"] = [int(x) for x in (d.get("tag_ids") or "").split(",") if x]
        result.append(d)
    return jsonify(result)

@app.route("/api/xml_ids", methods=["GET"])
def get_xml_ids():
    ids = []
    xml_dir = get_xml_dir()
    if os.path.isdir(xml_dir):
        ns = {"xml": "http://www.w3.org/XML/1998/namespace"}
        for fname in sorted(os.listdir(xml_dir)):
            if fname.endswith(".xml"):
                try:
                    tree = etree.parse(os.path.join(xml_dir, fname))
                    found = tree.getroot().xpath('//*/@xml:id', namespaces=ns)
                    ids.extend(found)
                except Exception:
                    pass
    with get_db() as db:
        used = [r[0] for r in db.execute("SELECT DISTINCT text_xmlid FROM variants").fetchall()]
    return jsonify(sorted(set(ids) | set(used)))


# Annotation & linking view
@app.route("/annotate")
def annotate():
    return render_template("annotate.html")

@app.route("/api/xml_cache_reload", methods=["POST"])
def xml_cache_reload():
    build_xml_cache()
    with get_db() as db:
        n   = db.execute("SELECT COUNT(*) FROM xml_cache").fetchone()[0]
        inv = db.execute("SELECT COUNT(*) FROM variants WHERE verified=-1").fetchone()[0]
    return jsonify({"ok": True, "eintraege": n, "invalidiert": inv})

@app.route("/api/variants/invalid", methods=["GET"])
def get_invalid_variants():
    """All variants that no longer match the XML (verified=-1)."""
    with get_db() as db:
        rows = db.execute("""
            SELECT v.*, f.formula as formulierung, t.name as kat_name
            FROM variants v
            JOIN formulae f ON f.id = v.formula_id
            LEFT JOIN tags t ON t.id = f.tag_id
            WHERE v.verified = -1
            ORDER BY v.text_xmlid, f.formula
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/variants/reverify", methods=["POST"])
def reverify_all():
    """Re-verifies all variants against the current XML cache."""
    with get_db() as db:
        _verify_all_variants(db)
        inv = db.execute("SELECT COUNT(*) FROM variants WHERE verified=-1").fetchone()[0]
        ok  = db.execute("SELECT COUNT(*) FROM variants WHERE verified=1").fetchone()[0]
    return jsonify({"ok": True, "gueltig": ok, "invalidiert": inv})

@app.route("/invalid")
def invalid_page():
    return render_template("invalid.html")

@app.route("/api/xml_search", methods=["GET"])
def xml_search():
    """Full-text search in inner_xml of the xml_cache.
    Returns a list of {xmlid, preview, snippet} for entries whose inner_xml
    matches the query. Supports * wildcard (converted to SQL LIKE %).
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    with get_db() as db:
        # Build LIKE pattern: * → %, escape existing % and _
        like_pat = q.replace('%', r'\%').replace('_', r'\_').replace('*', '%')
        if '*' not in q:          # plain substring: wrap in wildcards
            like_pat = f'%{like_pat}%'

        rows = db.execute(
            """SELECT xmlid, preview, inner_xml
               FROM xml_cache
               WHERE inner_xml LIKE ? ESCAPE '\\'
               ORDER BY xmlid""",
            (like_pat,)
        ).fetchall()

    results = []
    q_lower = q.replace('*', '').lower()
    for r in rows:
        inner = r["inner_xml"] or ""
        # Build a short context snippet (up to 80 chars around first match)
        idx = inner.lower().find(q_lower) if q_lower else -1
        if idx >= 0:
            start  = max(0, idx - 30)
            end    = min(len(inner), idx + len(q_lower) + 50)
            snippet = ("…" if start else "") + inner[start:end] + ("…" if end < len(inner) else "")
            # Strip XML tags for display
            snippet = re.sub(r'<[^>]+>', '', snippet).strip()
        else:
            snippet = (r["preview"] or "")[:80]
        results.append({"xmlid": r["xmlid"], "preview": r["preview"] or "", "snippet": snippet})

    return jsonify(results)


@app.route("/api/xml_tree", methods=["GET"])
def get_xml_tree():
    """Returns the file tree from xml_cache."""
    with get_db() as db:
        rows = db.execute("""
            SELECT x.xmlid, x.file, x.tag,
                   COALESCE(r.name, x.tag_region) as tag_region,
                   x.tag_section, x.preview, x.position
            FROM xml_cache x
            LEFT JOIN regions r ON r.abbr = x.tag_region
            ORDER BY x.file, x.position
        """).fetchall()
        linked = set(r[0] for r in db.execute(
            "SELECT DISTINCT text_xmlid FROM variants"
        ).fetchall())

    files = {}
    for r in rows:
        f = r["file"]
        if f not in files:
            files[f] = []
        files[f].append({
            "xmlid":       r["xmlid"],
            "tag":         r["tag"],
            "tag_region":  r["tag_region"] or "",
            "tag_textteil": r["tag_section"] or r["tag"],
            "preview":     r["preview"] or "",
            "verknuepft":  r["xmlid"] in linked
        })
    return jsonify([{"datei": f, "elemente": elems} for f, elems in files.items()])

@app.route("/api/xml_doc", methods=["GET"])
def get_xml_doc():
    doc_id = request.args.get("doc_id")
    if not doc_id:
        return jsonify({"error": "doc_id required"}), 400

    with get_db() as db:
        rows = db.execute("""
            SELECT x.xmlid, x.file as datei, x.tag,
                   COALESCE(r.name, x.tag_region) as tag_region,
                   x.tag_section as tag_textteil, x.inner_xml, x.preview,
                   COUNT(v.id) as anzahl_varianten,
                   COALESCE(SUM(CASE WHEN v.verified=-1 THEN 1 ELSE 0 END), 0) as fehler
            FROM xml_cache x
            LEFT JOIN regions r ON r.abbr = x.tag_region
            LEFT JOIN variants v ON v.text_xmlid = x.xmlid
            WHERE x.xmlid = ? OR x.xmlid LIKE ?
            GROUP BY x.xmlid
            ORDER BY x.position
        """, (doc_id, f"{doc_id}_%"))
        parts = [dict(r) for r in rows]

    return jsonify({"doc_id": doc_id, "datei": parts[0]["datei"] if parts else None, "parts": parts})

@app.route("/api/xml_element", methods=["GET"])
def get_xml_element():
    xmlid = request.args.get("xmlid")
    if not xmlid:
        return jsonify({"error": "xmlid required"}), 400

    with get_db() as db:
        cached = db.execute("SELECT * FROM xml_cache WHERE xmlid=?", (xmlid,)).fetchone()
        if not cached:
            return jsonify({"error": "not found"}), 404

        tag        = cached["tag"]
        file_name  = cached["file"]
        inner_root = cached["inner_xml"] or ""

        ns = {"xml": "http://www.w3.org/XML/1998/namespace"}
        sections = []

        def _root_section():
            return {"tag": tag, "xmlid": xmlid, "inner": inner_root}

        try:
            tree = etree.parse(os.path.join(get_xml_dir(), file_name))
            elems = tree.getroot().xpath(f'//*[@xml:id="{xmlid}"]', namespaces=ns)
            if elems:
                el = elems[0]
                children_with_id = [c for c in el
                                    if c.get("{http://www.w3.org/XML/1998/namespace}id")]
                if children_with_id:
                    for child in children_with_id:
                        ctag  = etree.QName(child.tag).localname
                        cid   = child.get("{http://www.w3.org/XML/1998/namespace}id")
                        crow  = db.execute("SELECT inner_xml FROM xml_cache WHERE xmlid=?", (cid,)).fetchone()
                        sections.append({
                            "tag": ctag, "xmlid": cid,
                            "inner": crow["inner_xml"] if crow else _inner_xml(child)
                        })
                else:
                    sections.append(_root_section())
        except Exception:
            sections.append(_root_section())

        all_xmlids = [xmlid] + [s["xmlid"] for s in sections if s["xmlid"]]
        placeholders = ",".join("?" * len(all_xmlids))
        variants = db.execute(f"""
            SELECT v.*, f.formula as formulierung,
                   (SELECT GROUP_CONCAT(t2.name)
                    FROM formula_tags ft JOIN tags t2 ON t2.id=ft.tag_id
                    WHERE ft.formula_id=v.formula_id) as kat_name,
                   (SELECT tg.color
                    FROM formula_tags ft2 JOIN tags tg ON tg.id=ft2.tag_id
                    WHERE ft2.formula_id=v.formula_id AND tg.color IS NOT NULL
                    LIMIT 1) as tag_color
            FROM variants v
            JOIN formulae f ON f.id = v.formula_id
            WHERE v.text_xmlid IN ({placeholders})
        """, all_xmlids).fetchall()

    return jsonify({
        "tag": tag, "xmlid": xmlid, "datei": file_name,
        "abschnitte": sections,
        "varianten": [dict(v) for v in variants]
    })

@app.route("/api/xml_texts", methods=["GET"])
def get_xml_texts():
    """All cached texts with linking statistics."""
    with get_db() as db:
        rows = db.execute("""
            SELECT
                x.xmlid, x.file as datei, x.tag,
                COALESCE(r.name, x.tag_region) as tag_region,
                x.tag_section as tag_textteil, x.preview,
                x.inner_xml,
                COUNT(v.id)        as anzahl_varianten,
                COALESCE(SUM(LENGTH(v.string)), 0) as linked_chars,
                COALESCE(LENGTH(x.inner_xml), 1)   as total_chars
            FROM xml_cache x
            LEFT JOIN regions r ON r.abbr = x.tag_region
            LEFT JOIN variants v ON v.text_xmlid = x.xmlid
            GROUP BY x.xmlid
            ORDER BY x.xmlid
        """).fetchall()
    result = []
    for r in rows:
        pct = round(100 * r["linked_chars"] / max(r["total_chars"], 1), 1)
        result.append({**dict(r), "verknuepft_pct": pct})
    return jsonify(result)

@app.route("/api/variants/bulk", methods=["POST"])
def post_variant_bulk():
    d = request.json
    formula_id   = d.get("formula_id") or d.get("ideal_id")
    tag_region, tag_section = extract_tags(d["text_xmlid"])
    offset_start = d.get("offset_start")
    offset_end   = d.get("offset_end")
    string_xml   = _compute_string_xml(d["text_xmlid"], offset_start, offset_end)
    with get_db() as db:
        status = _verify_offset(db, d["text_xmlid"], d["string"], offset_start, offset_end)
        cur = db.execute(
            """INSERT INTO variants
               (formula_id, text_xmlid, string, string_xml, offset_start, offset_end,
                verified, note, tag_region, tag_section)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (formula_id, d["text_xmlid"], d["string"], string_xml,
             offset_start, offset_end, status, d.get("note") or d.get("notiz"), tag_region, tag_section)
        )
        row = db.execute("SELECT * FROM variants WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201

@app.route("/api/similar_formulae", methods=["POST"])
def similar_formulae():
    text = request.json.get("text", "").strip()
    if not text:
        return jsonify([])
    with get_db() as db:
        rows = db.execute("""
            SELECT f.*, 
                   GROUP_CONCAT(DISTINCT v.tag_region) as tags_region,
                   GROUP_CONCAT(DISTINCT v.tag_section) as tags_textteil,
                   COUNT(v.id) as varianten_anzahl
            FROM formulae f
            LEFT JOIN variants v ON v.formula_id=f.id
            GROUP BY f.id
        """).fetchall()
        all_rows = [_formula_row_to_dict(db, r) for r in rows]
    text_lower = text.lower()
    text_words = set(re.findall(r'\w+', text_lower))
    scored = []
    for row in all_rows:
        f = row["formula"]
        f_lower = f.lower()
        f_words = set(re.findall(r'\w+', f_lower))
        a, b = text_lower[:80], f_lower[:80]
        la, lb = len(a), len(b)
        prev = [0] * (lb + 1)
        for i in range(la):
            curr = [0] * (lb + 1)
            for j in range(lb):
                curr[j + 1] = prev[j] + 1 if a[i] == b[j] else max(curr[j], prev[j + 1])
            prev = curr
        lcs = prev[lb]
        score_edit = (2 * lcs / (la + lb)) if (la + lb) else 0
        score_kw   = len(text_words & f_words) / len(text_words | f_words) if (text_words | f_words) else 0
        score = 0.5 * score_edit + 0.5 * score_kw
        if score > 0.02:
            scored.append({**row, "score": round(score, 3)})
    scored.sort(key=lambda x: -x["score"])
    return jsonify(scored[:8])

@app.route("/api/formulae/<int:fid>/variants", methods=["GET"])
def get_formula_variants(fid):
    with get_db() as db:
        formula = db.execute(
            "SELECT f.*, t.name as kat_name FROM formulae f LEFT JOIN tags t ON t.id=f.tag_id WHERE f.id=?",
            (fid,)
        ).fetchone()
        if not formula:
            return jsonify({"error": "not found"}), 404
        d = dict(formula)
        d["formulierung"] = d["formula"]
        variants = db.execute(
            "SELECT * FROM variants WHERE formula_id=? ORDER BY text_xmlid", (fid,)
        ).fetchall()
        # Build tag_ids for the formula
        tag_rows = db.execute(
            "SELECT tag_id FROM formula_tags WHERE formula_id=?", (fid,)
        ).fetchall()
        d["kategorie_ids"] = [r["tag_id"] for r in tag_rows]
    return jsonify({
        "ideal": d,
        "varianten": [dict(v) for v in variants]
    })


# Legacy route aliases (so existing JS doesn't break)
@app.route("/api/ideale", methods=["GET"])
def get_ideale_compat():
    return get_formulae()

@app.route("/api/ideale", methods=["POST"])
def post_ideal_compat():
    if request.json and "formulierung" in request.json:
        d = request.json
        request.json["formula"] = d.get("formulierung")
        request.json["note"]    = d.get("notiz")
        request.json["tag_ids"] = d.get("kategorie_ids", [])
    return post_formula()

@app.route("/api/ideale/<int:iid>", methods=["PUT"])
def put_ideal_compat(iid):
    d = request.json or {}
    d.setdefault("formula", d.get("formulierung", ""))
    d.setdefault("note",    d.get("notiz"))
    d.setdefault("tag_ids", d.get("kategorie_ids", []))
    return put_formula(iid)

@app.route("/api/ideale/<int:iid>", methods=["DELETE"])
def delete_ideal_compat(iid):
    return delete_formula(iid)

@app.route("/api/varianten", methods=["GET"])
def get_varianten_compat():
    return get_variants()

@app.route("/api/varianten", methods=["POST"])
def post_variante_compat():
    return post_variant()

@app.route("/api/varianten/<int:vid>", methods=["GET"])
def get_variante_compat(vid):
    return get_variant(vid)

@app.route("/api/varianten/<int:vid>", methods=["PUT"])
def put_variante_compat(vid):
    return put_variant(vid)

@app.route("/api/varianten/<int:vid>", methods=["DELETE"])
def delete_variante_compat(vid):
    return delete_variant(vid)

@app.route("/api/varianten/<int:vid>/verifizieren", methods=["POST"])
def verify_variante_compat(vid):
    return verify_variant(vid)

@app.route("/api/varianten/invalidiert", methods=["GET"])
def get_invalid_compat():
    return get_invalid_variants()

@app.route("/api/varianten/neu_verifizieren", methods=["POST"])
def reverify_compat():
    return reverify_all()

@app.route("/api/varianten/bulk", methods=["POST"])
def post_bulk_compat():
    return post_variant_bulk()

@app.route("/api/aehnliche_ideale", methods=["POST"])
def similar_compat():
    return similar_formulae()

@app.route("/api/kategorien", methods=["GET"])
def get_kategorien_compat():
    return get_tags()

@app.route("/api/kategorien", methods=["POST"])
def post_kategorie_compat():
    return post_tag()

@app.route("/api/kategorien/<int:kid>", methods=["PUT"])
def put_kategorie_compat(kid):
    return put_tag(kid)

@app.route("/api/kategorien/<int:kid>", methods=["DELETE"])
def delete_kategorie_compat(kid):
    return delete_tag(kid)

@app.route("/api/suche/texte", methods=["GET"])
def suche_texte_compat():
    return search_texts()

@app.route("/api/regionen", methods=["GET"])
def get_regionen_compat():
    return get_regions()

@app.route("/api/regionen", methods=["POST"])
def post_region_compat():
    return post_region()

@app.route("/api/ideale/<int:iid>/varianten", methods=["GET"])
def get_ideal_varianten_compat(iid):
    return get_formula_variants(iid)

@app.route("/api/xml_baum", methods=["GET"])
def get_xml_baum_compat():
    return get_xml_tree()

@app.route("/api/xml_texte", methods=["GET"])
def get_xml_texte_compat():
    return get_xml_texts()


# Import
@app.route("/import")
def import_page():
    return render_template("import.html")

@app.route("/api/import/sqlite/preview", methods=["POST"])
def import_sqlite_preview():
    if 'file' not in request.files:
        return jsonify({"error": "No file"}), 400
    import tempfile
    f = request.files['file']
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    preview = {
        "kategorien_neu": [], "kategorien_vorhanden": [],
        "ideale_neu": [], "ideale_vorhanden": [],
        "varianten_neu": 0, "varianten_vorhanden": 0,
        "ist_eigene_db": False
    }
    try:
        src = sqlite3.connect(tmp_path)
        src.row_factory = sqlite3.Row
        dst = get_db()

        tag_map = {}
        try:
            for row in src.execute("SELECT * FROM kategorien").fetchall():
                exist = dst.execute("SELECT id FROM tags WHERE name=?", (row["name"],)).fetchone()
                if exist:
                    tag_map[row["id"]] = exist["id"]
                    preview["kategorien_vorhanden"].append(row["name"])
                else:
                    tag_map[row["id"]] = None
                    preview["kategorien_neu"].append(row["name"])
        except Exception:
            pass

        formula_map = {}
        try:
            src_col = "formulierung" if "formulierung" in [c[1] for c in src.execute("PRAGMA table_info(ideale)").fetchall()] else "formula"
            for row in src.execute("SELECT * FROM ideale").fetchall():
                text = row[src_col]
                exist = dst.execute("SELECT id FROM formulae WHERE formula=?", (text,)).fetchone()
                if exist:
                    formula_map[row["id"]] = exist["id"]
                    preview["ideale_vorhanden"].append(text[:60])
                else:
                    formula_map[row["id"]] = None
                    preview["ideale_neu"].append(text[:60])
        except Exception:
            pass

        try:
            variant_tbl = "varianten" if "varianten" in [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()] else "variants"
            id_col = "ideal_id" if "ideal_id" in [c[1] for c in src.execute(f"PRAGMA table_info({variant_tbl})").fetchall()] else "formula_id"
            for row in src.execute(f"SELECT * FROM {variant_tbl}").fetchall():
                dst_fid = formula_map.get(row[id_col])
                if not dst_fid:
                    preview["varianten_neu"] += 1
                    continue
                exist = dst.execute(
                    "SELECT id FROM variants WHERE formula_id=? AND text_xmlid=? AND string=?",
                    (dst_fid, row["text_xmlid"], row["string"])
                ).fetchone()
                if exist:
                    preview["varianten_vorhanden"] += 1
                else:
                    preview["varianten_neu"] += 1
        except Exception:
            pass

        if not preview["ideale_neu"] and not preview["varianten_neu"] and preview["varianten_vorhanden"] > 0:
            preview["ist_eigene_db"] = True

        src.close(); dst.close()
    except Exception as e:
        try: src.close()
        except: pass
        try: dst.close()
        except: pass
        os.unlink(tmp_path)
        return jsonify({"error": str(e)}), 500
    finally:
        try: os.unlink(tmp_path)
        except: pass

    return jsonify(preview)

@app.route("/api/import/sqlite", methods=["POST"])
def import_sqlite():
    if 'file' not in request.files:
        return jsonify({"error": "No file"}), 400
    import tempfile
    f = request.files['file']
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    stats = {"kategorien": 0, "ideale": 0, "varianten": 0,
             "dubletten_ideale": 0, "dubletten_varianten": 0, "fehler": []}
    try:
        src = sqlite3.connect(tmp_path)
        src.row_factory = sqlite3.Row
        dst = get_db()
        dst.execute("PRAGMA foreign_keys = ON")

        # Tags (from source 'kategorien' table)
        tag_map = {}
        try:
            for row in src.execute("SELECT * FROM kategorien").fetchall():
                exist = dst.execute("SELECT id FROM tags WHERE name=?", (row["name"],)).fetchone()
                if exist:
                    tag_map[row["id"]] = exist["id"]
                else:
                    cur = dst.execute("INSERT INTO tags (name) VALUES (?)", (row["name"],))
                    tag_map[row["id"]] = cur.lastrowid
                    stats["kategorien"] += 1
            dst.commit()
        except Exception as e:
            stats["fehler"].append(f"Tags: {e}")

        # Formulae (from source 'ideale' table)
        formula_map = {}
        try:
            src_cols = [c[1] for c in src.execute("PRAGMA table_info(ideale)").fetchall()]
            form_col = "formulierung" if "formulierung" in src_cols else "formula"
            note_col = "notiz" if "notiz" in src_cols else "note"
            for row in src.execute("SELECT * FROM ideale").fetchall():
                text = row[form_col]
                exist = dst.execute("SELECT id FROM formulae WHERE formula=?", (text,)).fetchone()
                if exist:
                    formula_map[row["id"]] = exist["id"]
                    stats["dubletten_ideale"] += 1
                else:
                    note = row[note_col] if note_col in src_cols else None
                    cur = dst.execute("INSERT INTO formulae (formula, note) VALUES (?,?)", (text, note))
                    formula_map[row["id"]] = cur.lastrowid
                    stats["ideale"] += 1
            dst.commit()
        except Exception as e:
            stats["fehler"].append(f"Formulae: {e}")

        # Variants
        try:
            all_tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            variant_tbl = "varianten" if "varianten" in all_tables else "variants"
            vcols = [c[1] for c in src.execute(f"PRAGMA table_info({variant_tbl})").fetchall()]
            id_col = "ideal_id" if "ideal_id" in vcols else "formula_id"
            for row in src.execute(f"SELECT * FROM {variant_tbl}").fetchall():
                dst_fid = formula_map.get(row[id_col])
                if not dst_fid:
                    stats["fehler"].append(f"Variant {row['id']}: formula {row[id_col]} not mapped")
                    continue
                exist = dst.execute(
                    "SELECT id FROM variants WHERE formula_id=? AND text_xmlid=? AND string=?",
                    (dst_fid, row["text_xmlid"], row["string"])
                ).fetchone()
                if exist:
                    stats["dubletten_varianten"] += 1
                    continue
                note_col = "notiz" if "notiz" in vcols else ("note" if "note" in vcols else None)
                region_col  = "tag_region"   if "tag_region"   in vcols else None
                section_col = "tag_textteil" if "tag_textteil" in vcols else ("tag_section" if "tag_section" in vcols else None)
                dst.execute(
                    """INSERT INTO variants
                       (formula_id, text_xmlid, string, verified, note, tag_region, tag_section)
                       VALUES (?,?,?,?,?,?,?)""",
                    (dst_fid, row["text_xmlid"], row["string"],
                     row["verifiziert"] if "verifiziert" in vcols else (row["verified"] if "verified" in vcols else 0),
                     row[note_col] if note_col else None,
                     row[region_col] if region_col else None,
                     row[section_col] if section_col else None)
                )
                stats["varianten"] += 1
            dst.commit()
        except Exception as e:
            stats["fehler"].append(f"Variants: {e}")

        dst.close()
    finally:
        src.close()
        os.unlink(tmp_path)

    stats["hinweis"] = None
    if stats["dubletten_ideale"] > 0 or stats["dubletten_varianten"] > 0:
        stats["hinweis"] = (
            f"Existing entries were skipped: "
            f"{stats['dubletten_ideale']} formula(e), "
            f"{stats['dubletten_varianten']} variant(s). "
            "This is normal when importing your own database or a previously imported file."
        )

    return jsonify({"ok": True, "stats": stats})

@app.route("/api/import/csv", methods=["POST"])
def import_csv():
    """Imports CSV with columns: formula, tag, text_xmlid, string, note (all optional except formula)."""
    if 'file' not in request.files:
        return jsonify({"error": "No file"}), 400
    import csv, io
    content = request.files['file'].read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(content))
    stats = {"kategorien": 0, "ideale": 0, "varianten": 0, "dubletten": 0, "fehler": 0}

    with get_db() as db:
        for row in reader:
            try:
                # Support both German column names (legacy) and English
                formula_text = (row.get("formula") or row.get("formulierung", "")).strip()
                if not formula_text:
                    stats["fehler"] += 1
                    continue
                xmlid    = (row.get("text_xmlid", "")).strip()
                string   = (row.get("string", "")).strip()
                tag_name = (row.get("tag") or row.get("kategorie", "")).strip() or None
                note     = (row.get("note") or row.get("notiz", "")).strip() or None

                tag_id = None
                if tag_name:
                    exist = db.execute("SELECT id FROM tags WHERE name=?", (tag_name,)).fetchone()
                    if exist:
                        tag_id = exist["id"]
                    else:
                        cur = db.execute("INSERT INTO tags (name) VALUES (?)", (tag_name,))
                        tag_id = cur.lastrowid
                        stats["kategorien"] += 1

                exist = db.execute("SELECT id FROM formulae WHERE formula=?", (formula_text,)).fetchone()
                if exist:
                    formula_id = exist["id"]
                else:
                    cur = db.execute(
                        "INSERT INTO formulae (tag_id, formula, note) VALUES (?,?,?)",
                        (tag_id, formula_text, note)
                    )
                    formula_id = cur.lastrowid
                    stats["ideale"] += 1

                if not xmlid or not string:
                    continue

                exist = db.execute(
                    "SELECT id FROM variants WHERE formula_id=? AND text_xmlid=? AND string=?",
                    (formula_id, xmlid, string)
                ).fetchone()
                if exist:
                    stats["dubletten"] += 1
                    continue

                tag_region, tag_section = extract_tags(xmlid)
                db.execute(
                    """INSERT INTO variants
                       (formula_id, text_xmlid, string, verified, note, tag_region, tag_section)
                       VALUES (?,?,?,?,?,?,?)""",
                    (formula_id, xmlid, string, 0, note, tag_region, tag_section)
                )
                stats["varianten"] += 1
            except Exception:
                stats["fehler"] += 1

    return jsonify({"ok": True, "stats": stats})


if __name__ == "__main__":
    os.makedirs(DEFAULT_XML_DIR, exist_ok=True)
    app.run(debug=True, port=5000)

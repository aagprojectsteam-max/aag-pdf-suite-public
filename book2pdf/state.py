"""Durable successful-output provenance; existence alone is never a resume hit."""
import hashlib
from pathlib import Path
import sqlite3
import json


def pdf_provenance(path: Path, digest: str):
    """Read a known PDF's recovery classification without modifying its journal."""
    database = path.parent / '.book2pdf-state.sqlite3'
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            rows = connection.execute('SELECT record FROM success WHERE output=?', (str(path.resolve()),))
            for row in rows:
                record = json.loads(row[0])
                if record.get('output_hash') == digest:
                    return record
        finally:
            connection.close()
    except (sqlite3.Error, ValueError):
        return None


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class State:
    def __init__(self, path: Path):
        if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
            raise ValueError('State database must not be a symbolic link or hard link')
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS success (source TEXT, output TEXT, record TEXT NOT NULL, PRIMARY KEY(source,output))")
        self.db.commit()

    def lookup(self, source, output):
        row = self.db.execute("SELECT record FROM success WHERE source=? AND output=?", (str(source), str(output))).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, result):
        self.db.execute("INSERT OR REPLACE INTO success VALUES (?,?,?)", (result.source_path, result.output_path, json.dumps(result.to_dict(), ensure_ascii=False)))
        self.db.commit()

    def close(self):
        self.db.close()

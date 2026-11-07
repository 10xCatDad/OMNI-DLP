"""A transactional download archive that verifies bytes before skipping work."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
import sqlite3

from .models import DownloadResult, Media


def identity(media: Media, variant: str = "") -> str:
    return hashlib.sha256((media.url + "\0" + variant).encode()).hexdigest()


class Archive:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            # Serialize schema creation/migration across concurrent CLI processes.
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS downloads (
                key TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, completed TEXT DEFAULT CURRENT_TIMESTAMP)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(downloads)")}
            for column in ("output_kind", "output_ext", "output_mime"):
                if column not in columns:
                    db.execute(f"ALTER TABLE downloads ADD COLUMN {column} TEXT")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def lookup(self, media: Media, variant: str = "") -> DownloadResult | None:
        with self._connect() as db:
            row = db.execute("SELECT path, size, sha256, output_kind, output_ext, output_mime FROM downloads WHERE key = ?",
                             (identity(media, variant),)).fetchone()
        if not row:
            return None
        path, size, digest, kind, ext, mime = row
        try:
            if Path(path).stat().st_size != size:
                return None
            with open(path, "rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest:
                return None
        except OSError:
            return None
        output_media = replace(media, kind=kind if kind is not None else media.kind,
                               ext=ext if ext is not None else media.ext,
                               mime_type=mime if mime is not None else media.mime_type)
        return DownloadResult(output_media, path, size, digest, skipped=True)

    def record(self, result: DownloadResult, variant: str = "") -> None:
        with self._connect() as db:
            db.execute("""INSERT OR REPLACE INTO downloads
                          (key,path,size,sha256,output_kind,output_ext,output_mime)
                          VALUES(?,?,?,?,?,?,?)""", (identity(result.media, variant),
                          str(Path(result.path).resolve()), result.size, result.sha256,
                          result.media.kind, result.media.ext, result.media.mime_type))

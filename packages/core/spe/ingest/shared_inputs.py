"""Durable input transfer. Database timestamps retain the last successful refresh."""
import gzip
import hashlib
import io
import json
import os
import tempfile
from datetime import UTC, datetime

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import InputArtifact

FILES = ("fbs_free_opportunities.json", "fbs_situations.json", "fbs_rosters.json",
         "fbs.json", "nfl.json", "recent_complete_boxes.json")
MAX_BYTES = 128 * 1024 * 1024


def publish_inputs(db, names=FILES, now=None):
    now = as_utc(now) or datetime.now(UTC)
    written = []
    for name in names:
        if name not in FILES:
            raise ValueError("Unknown shared input")
        path = settings.parquet_dir / "desk_evidence" / name
        if not path.exists():
            continue
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Shared input exceeds size limit")
        body = path.read_bytes()
        if not isinstance(json.loads(body), dict):
            raise ValueError("Shared input must be an object")
        checksum = hashlib.sha256(body).hexdigest()
        row = db.get(InputArtifact, name)
        if row is None:
            row = InputArtifact(key=name)
            db.add(row)
        if row.checksum != checksum:
            row.content = gzip.compress(body, compresslevel=6, mtime=0)
        row.checksum, row.byte_count, row.updated_at = checksum, len(body), now
        written.append(name)
    db.flush()
    return {"published": written}


def hydrate_inputs(db, names=FILES):
    """Hydrate only changed versions; never turn hydration into a fresh observation."""
    root = settings.parquet_dir / "desk_evidence"
    root.mkdir(parents=True, exist_ok=True)
    versions = {}
    for key, checksum, updated_at in db.query(InputArtifact.key, InputArtifact.checksum, InputArtifact.updated_at).filter(InputArtifact.key.in_(names)):
        if key not in FILES:
            continue
        path = root / key
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            row = db.get(InputArtifact, key)
            with gzip.GzipFile(fileobj=io.BytesIO(row.content)) as compressed:
                body = compressed.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES or len(body) != row.byte_count or hashlib.sha256(body).hexdigest() != checksum:
                raise ValueError("Shared input integrity check failed")
            json.loads(body)
            with tempfile.NamedTemporaryFile(dir=root, suffix='.tmp', delete=False) as output:
                output.write(body)
                temp = output.name
            try:
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        stamp = as_utc(updated_at).timestamp()
        os.utime(path, (stamp, stamp))
        versions[key] = {"sha256": checksum, "refreshed_at": as_utc(updated_at).isoformat()}
    return versions

# dev_core/clustering.py
"""
Event clustering / narrative tracking.

Creates narrative clusters using cosine similarity on embeddings.
Stores:
- narrative_clusters: centroid + n + updated_ts
- narrative_members: event_id -> cluster_id

Simple incremental clustering:
- compare event vec to recent centroids
- if sim >= THRESH, assign and update centroid
- else create new cluster

This is intentionally lightweight and SQLite-native.
"""

import os
import time
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from dev_core.storage import connect

THRESH = float(os.environ.get("CLUSTER_SIM_THRESHOLD", "0.82"))
MAX_RECENT = int(os.environ.get("CLUSTER_MAX_RECENT", "500"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS narrative_clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ts_ms INTEGER NOT NULL,
  updated_ts_ms INTEGER NOT NULL,
  n INTEGER NOT NULL,
  dim INTEGER NOT NULL,
  centroid BLOB NOT NULL,
  title_hint TEXT
);

CREATE TABLE IF NOT EXISTS narrative_members (
  event_id INTEGER PRIMARY KEY,
  cluster_id INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_narr_clusters_updated ON narrative_clusters(updated_ts_ms);
CREATE INDEX IF NOT EXISTS idx_narr_members_cluster ON narrative_members(cluster_id);
"""


def init_clusters_db():
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def _load_recent_clusters(con):
    rows = con.execute(
        """
        SELECT id, n, dim, centroid, title_hint
        FROM narrative_clusters
        ORDER BY updated_ts_ms DESC
        LIMIT ?
        """,
        (int(MAX_RECENT),),
    ).fetchall()

    out = []
    for cid, n, dim, blob, title_hint in rows or []:
        v = np.frombuffer(blob, dtype=np.float32)
        if v.shape[0] != int(dim):
            continue
        out.append((int(cid), int(n), v, str(title_hint or "")))
    return out


def assign_cluster(event_id: int, ts_ms: int, title: str, vec: np.ndarray):
    init_clusters_db()

    now_ms = int(time.time() * 1000)
    v = np.asarray(vec, dtype=np.float32).reshape(1, -1)

    con = connect()
    try:
        # Already assigned?
        row = con.execute(
            "SELECT cluster_id FROM narrative_members WHERE event_id=?",
            (int(event_id),),
        ).fetchone()
        if row:
            return {"cluster_id": int(row[0]), "action": "exists"}

        clusters = _load_recent_clusters(con)
        if clusters:
            centroids = np.stack([c[2] for c in clusters]).astype(np.float32, copy=False)
            sims = cosine_similarity(v, centroids)[0]
        else:
            sims = []

        best_idx = None
        best_sim = 0.0
        for i, sim in enumerate(sims):
            if float(sim) > float(best_sim):
                best_sim = float(sim)
                best_idx = i

        if best_idx is not None and float(best_sim) >= float(THRESH):
            cid, n, centroid, _hint = clusters[best_idx]

            # Update centroid: running mean
            n2 = int(n) + 1
            new_centroid = (centroid * float(n) + v.reshape(-1)) / float(n2)
            new_centroid = new_centroid.astype(np.float32, copy=False)

            con.execute("BEGIN IMMEDIATE;")
            con.execute(
                """
                UPDATE narrative_clusters
                SET updated_ts_ms=?, n=?, centroid=?, title_hint=?
                WHERE id=?
                """,
                (int(now_ms), int(n2), new_centroid.tobytes(), str(title or "")[:200], int(cid)),
            )
            con.execute(
                """
                INSERT OR REPLACE INTO narrative_members(event_id, cluster_id, ts_ms)
                VALUES (?,?,?)
                """,
                (int(event_id), int(cid), int(ts_ms)),
            )
            con.commit()

            return {"cluster_id": int(cid), "action": "assigned", "sim": float(best_sim), "threshold": float(THRESH)}

        # Create new cluster
        con.execute("BEGIN IMMEDIATE;")
        con.execute(
            """
            INSERT INTO narrative_clusters(created_ts_ms, updated_ts_ms, n, dim, centroid, title_hint)
            VALUES (?,?,?,?,?,?)
            """,
            (int(now_ms), int(now_ms), 1, int(v.shape[1]), v.reshape(-1).tobytes(), str(title or "")[:200]),
        )
        cid = int(con.execute("SELECT last_insert_rowid();").fetchone()[0])
        con.execute(
            """
            INSERT OR REPLACE INTO narrative_members(event_id, cluster_id, ts_ms)
            VALUES (?,?,?)
            """,
            (int(event_id), int(cid), int(ts_ms)),
        )
        con.commit()

        return {"cluster_id": int(cid), "action": "created", "threshold": float(THRESH)}

    finally:
        con.close()

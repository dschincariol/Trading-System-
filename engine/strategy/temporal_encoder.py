"""
Temporal MLP encoder (Step B).

Purpose:
- Encode short sequences of recent event embeddings into a single vector
- Used as a drop-in replacement for event_embeddings when enabled
- Offline only (safe, deterministic)

Output table:
  event_embeddings_seq(event_id, dim, vec)
"""

import time
import math
from typing import List

import numpy as np
import torch
import torch.nn as nn

from engine.runtime.storage import connect
from engine.execution.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

_TORCH_SEED = 42


class TemporalMLP(nn.Module):
    def __init__(self, input_dim: int, hidden: List[int]):
        super().__init__()
        dims = [input_dim] + list(hidden) + [input_dim]
        layers = []
        for i in range(len(dims) - 2):
            layers.append(nn.Linear(dims[i], dims[i + 1]))
            layers.append(nn.ReLU())
        layers.append(nn.Linear(dims[-2], dims[-1]))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


def _load_recent_embeddings(con, event_ts_ms: int, window: int):
    rows = con.execute(
        """
        SELECT e.ts_ms, emb.vec
        FROM events e
        JOIN event_embeddings emb ON emb.event_id = e.id
        WHERE e.ts_ms < ?
        ORDER BY e.ts_ms DESC
        LIMIT ?
        """,
        (int(event_ts_ms), int(window)),
    ).fetchall()

    seq = []
    prev_ts = event_ts_ms

    for ts, blob in rows:
        v = np.frombuffer(blob, dtype=np.float32)
        dt = float(prev_ts - ts) / 1000.0
        seq.append(np.concatenate([v, np.array([dt], dtype=np.float32)]))
        prev_ts = ts

    return seq[::-1]  # oldest → newest


def build_temporal_embeddings(
    window: int = 5,
    hidden: List[int] = None,
    epochs: int = 80,
    lr: float = 3e-3,
):
    if hidden is None:
        hidden = [128, 64]

    torch.manual_seed(_TORCH_SEED)
    np.random.seed(_TORCH_SEED)

    con = connect()
    try:
        # Fail-soft if labels table isn't created yet
        try:
            con.execute("SELECT 1 FROM labels LIMIT 1").fetchone()
        except Exception:
            return {"ok": True, "trained": 0}

        rows = con.execute("SELECT id, ts_ms FROM events ORDER BY ts_ms ASC").fetchall()
        if not rows:
            return {"ok": True, "trained": 0}

        # infer base embedding dim
        row = con.execute("SELECT vec FROM event_embeddings LIMIT 1").fetchone()
        if not row:
            return {"ok": True, "trained": 0}

        base_dim = int(len(np.frombuffer(row[0], dtype=np.float32)))
        input_dim = base_dim + 1

        model = TemporalMLP(input_dim=input_dim, hidden=hidden)
        opt = torch.optim.AdamW(model.parameters(), lr=float(lr))
        loss_fn = nn.MSELoss()

        samples = []
        for eid, ts in rows:
            seq = _load_recent_embeddings(con, int(ts), window)
            if len(seq) < window:
                continue
            X = np.stack(seq).astype(np.float32)
            y = X[-1, :-1]  # predict current embedding
            samples.append((int(eid), X, y))

        if not samples:
            return {"ok": True, "trained": 0}

        model.train()
        for _ in range(int(epochs)):
            for _eid, X, y in samples:
                xt = torch.from_numpy(X)
                yt = torch.from_numpy(y)
                opt.zero_grad(set_to_none=True)
                pred = model(xt[-1])
                loss = loss_fn(pred, yt)
                loss.backward()
                opt.step()

        # persist embeddings
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS event_embeddings_seq (
              event_id INTEGER PRIMARY KEY,
              dim INTEGER NOT NULL,
              vec BLOB NOT NULL
            )
            """
        )

        trained = 0
        for eid, X, _y in samples:
            xt = torch.from_numpy(X)
            with torch.no_grad():
                out = model(xt[-1]).numpy().astype(np.float32)

            con.execute(
                """
                INSERT OR REPLACE INTO event_embeddings_seq(event_id, dim, vec)
                VALUES (?,?,?)
                """,
                (int(eid), int(len(out)), out.tobytes()),
            )
            trained += 1

        con.commit()
        return {"ok": True, "trained": trained}

    finally:
        con.close()

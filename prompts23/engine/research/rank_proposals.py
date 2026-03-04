from typing import Dict, Any, List

from engine.research.offline_backtest_eval import score_metrics


def rank(proposals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []

    for p in proposals or []:
        p = dict(p)
        metrics = p.get("backtest_metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}

        s = score_metrics(metrics)
        p["score"] = float(s.get("score") or 0.0)
        p["score_detail"] = s
        ranked.append(p)

    ranked.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    return ranked

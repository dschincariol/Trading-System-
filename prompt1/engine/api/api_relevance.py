# engine/api/api_relevance.py
from engine.strategy.relevance import get_relevance_stats


def api_get_relevance_stats(_parsed, _ctx=None):
    return get_relevance_stats()

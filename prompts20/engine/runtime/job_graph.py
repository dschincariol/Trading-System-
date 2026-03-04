# engine/runtime/job_graph.py
from dataclasses import dataclass, field
from typing import Dict, List, Set


@dataclass(frozen=True)
class JobNode:
    name: str
    deps: List[str] = field(default_factory=list)


class JobGraph:
    def __init__(self, nodes: List[JobNode]):
        self.nodes: Dict[str, JobNode] = {n.name: n for n in nodes}
        self._validate()

    def _validate(self):
        # all deps exist
        for n in self.nodes.values():
            for d in n.deps:
                if d not in self.nodes:
                    raise RuntimeError(f"job_graph: {n.name} depends on missing job {d}")

        # detect cycles
        visiting: Set[str] = set()
        visited: Set[str] = set()

        def dfs(x: str):
            if x in visited:
                return
            if x in visiting:
                raise RuntimeError(f"job_graph: cycle detected at {x}")
            visiting.add(x)
            for d in self.nodes[x].deps:
                dfs(d)
            visiting.remove(x)
            visited.add(x)

        for k in self.nodes.keys():
            dfs(k)

    def can_start(self, name: str, running: Set[str]) -> bool:
        n = self.nodes.get(name)
        if not n:
            return False
        return all((d in running) for d in n.deps)

    def start_order(self, targets: List[str]) -> List[str]:
        # returns deps-first ordering
        out: List[str] = []
        seen: Set[str] = set()

        def add(x: str):
            if x in seen:
                return
            seen.add(x)
            for d in self.nodes[x].deps:
                add(d)
            out.append(x)

        for t in targets:
            if t not in self.nodes:
                raise RuntimeError(f"unknown job target: {t}")
            add(t)
        return out


DEFAULT_GRAPH = JobGraph([
    JobNode("poll_prices", deps=[]),
    JobNode("process_events", deps=["poll_prices"]),
    JobNode("options_poll", deps=["poll_prices"]),
    JobNode("strategy_eval", deps=["process_events"]),
    JobNode("portfolio_apply", deps=["strategy_eval"]),
])

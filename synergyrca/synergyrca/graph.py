"""StateGraph / MetaGraph (in-memory implementation of the paper's Section III-C/D)."""
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Entity:
    kind: str
    name: str
    namespace: str = ""
    snapshots: list = field(default_factory=list)  # [{"time", "spec", "status"}]

    @property
    def id(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}" if self.namespace else f"{self.kind}/{self.name}"

    def state_at(self, t: float) -> Optional[dict]:
        """Latest snapshot at or before time t (STATE vertex). None if no state exists."""
        if not self.snapshots:
            return None
        valid = [s for s in self.snapshots if s["time"] <= t] or self.snapshots
        return max(valid, key=lambda s: s["time"])


def extract_refs(kind: str, namespace: str, spec: dict, name: str = ""):
    """Return references as (target_kind, target_name, target_namespace, edge_key)."""
    refs = []
    if kind != "Namespace" and namespace:
        refs.append(("Namespace", namespace, "", "metadata_namespace"))
    if kind == "Pod":
        for v in spec.get("volumes", []):
            if "persistentVolumeClaim" in v:
                refs.append(("PersistentVolumeClaim", v["persistentVolumeClaim"]["claimName"], namespace,
                             "spec_volumes_persistentVolumeClaim_claimName"))
            if "configMap" in v:
                refs.append(("ConfigMap", v["configMap"]["name"], namespace, "spec_volumes_configMap_name"))
            if "secret" in v:
                refs.append(("Secret", v["secret"]["secretName"], namespace, "spec_volumes_secret_secretName"))
        if spec.get("nodeName"):
            refs.append(("Node", spec["nodeName"], "", "spec_nodeName"))
        if spec.get("serviceAccountName"):
            refs.append(("ServiceAccount", spec["serviceAccountName"], namespace, "spec_serviceAccountName"))
        for c in spec.get("containers", []):
            if c.get("name"):  # container names are qualified by pod so two pods never share one
                refs.append(("Container", f"{name}/{c['name']}", namespace, "spec_containers_name"))
            if c.get("image"):
                refs.append(("Image", c["image"], "", "spec_containers_image"))
    if kind == "PersistentVolumeClaim" and spec.get("storageClassName"):
        refs.append(("StorageClass", spec["storageClassName"], "", "spec_storageClassName"))
    return refs


class StateGraph:
    def __init__(self):
        self.entities: dict[str, Entity] = {}
        self.adj: dict[str, list] = defaultdict(list)  # id -> [(neighbor_id, edge_key)]
        self.edge_kinds: set = set()                   # (kindA, kindB, key) observed

    def _get_or_create(self, kind, name, namespace="") -> Entity:
        eid = f"{kind}/{namespace}/{name}" if namespace else f"{kind}/{name}"
        if eid not in self.entities:
            self.entities[eid] = Entity(kind, name, namespace)
        return self.entities[eid]

    def add_object(self, kind, name, namespace, spec, status, time):
        """Add one snapshot and its reference edges. Missing targets become state-less entities."""
        e = self._get_or_create(kind, name, namespace)
        e.snapshots.append({"time": time, "spec": spec, "status": status})
        for tkind, tname, tns, key in extract_refs(kind, namespace, spec, name):
            t = self._get_or_create(tkind, tname, tns)
            self._add_edge(e.id, t.id, key, kind, tkind)

    def _add_edge(self, a, b, key, kind_a, kind_b):
        if (b, key) not in self.adj[a]:
            self.adj[a].append((b, key))
            self.adj[b].append((a, key))
        self.edge_kinds.add((kind_a, kind_b, key))

    def find(self, kind, name, namespace="") -> Optional[Entity]:
        eid = f"{kind}/{namespace}/{name}" if namespace else f"{kind}/{name}"
        return self.entities.get(eid)


class MetaGraph:
    """Kind-level schema extracted directly from the StateGraph (paper Sec. III-D)."""

    def __init__(self, sg: StateGraph):
        self.edges = set(sg.edge_kinds)  # (kindA, kindB, key), used in both directions

    def kinds(self):
        return {k for a, b, _ in self.edges for k in (a, b)}

    def metapath(self, src: str, dst: str, max_len: int = 4):
        """BFS over kinds. Returns shortest list of steps (kind_from, kind_to, key)."""
        queue = deque([(src, [])])
        seen = {src}
        while queue:
            kind, path = queue.popleft()
            if kind == dst and path:
                return path
            if len(path) >= max_len:
                continue
            for a, b, key in self.edges:
                for x, y in ((a, b), (b, a)):
                    if x == kind and y not in seen:
                        seen.add(y)
                        queue.append((y, path + [(x, y, key)]))
        return None


def metapath_to_cypher(steps) -> str:
    """Render a metapath as a Cypher MATCH chain (display/audit; execution is in-memory)."""
    lines = []
    for i, (a, b, key) in enumerate(steps, 1):
        lines.append(f"MATCH (n{i-1}:{a})-[r{i}:ReferInternal {{key: '{key}'}}]-(n{i}:{b})")
    return "\n".join(lines) + "\nRETURN n0, r1, n" + str(len(steps))


def instantiate_statepaths(sg: StateGraph, start_id: str, steps, max_paths: int = 20):
    """Follow a metapath through the StateGraph. Returns a list of statepaths (lists of entity ids)."""
    results = []

    def dfs(current_id, step_idx, path):
        if len(results) >= max_paths:
            return
        if step_idx == len(steps):
            results.append(path)
            return
        _, want_kind, key = steps[step_idx]
        for nxt, edge_key in sg.adj.get(current_id, []):
            if edge_key != key or nxt in path:
                continue
            if sg.entities[nxt].kind != want_kind:
                continue
            dfs(nxt, step_idx + 1, path + [nxt])

    dfs(start_id, 0, [start_id])
    return results

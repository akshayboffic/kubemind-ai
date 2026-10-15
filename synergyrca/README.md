# SynergyRCA (reimplementation)

A working Python reimplementation of the pipeline described in
*"Simplifying Root Cause Analysis in Kubernetes with StateGraph and LLM"*
(Xiang et al., arXiv:2506.02490). The authors did not release their code,
so this follows the paper's described design.

## What it does

Given a Kubernetes incident event (for example `FailedCreate` with an
exceeded quota), it:

1. Builds a **StateGraph** of entities and their snapshots, with reference edges
   (Pod to PVC, Pod to ConfigMap, Job to Namespace to ResourceQuota, Pod to Node).
2. Derives a **MetaGraph** of kind-level relationships from the StateGraph.
3. **Triage**: predicts the root-cause kind (the destination kind).
4. **PathQueryGen**: finds the metapath from the source kind to the destination kind,
   renders it as Cypher, and instantiates it as concrete statepaths.
5. **StateChecker**: summarizes the relevant fields of each entity's state.
6. **ReportGen**: produces a conclusion, a score, and a `kubectl` resolution.
7. **ReportQualityChecker**: scores the report. Below 7 triggers a retry with a
   different destination kind, up to 3 trials.

## Setup

```bash
cd synergyrca
python3 -m venv .venv && source .venv/bin/activate
# no installation needed: standard library only
```

No other dependencies are required. The code uses only the Python standard library.

## Run

```bash
python -m synergyrca                    # all scenarios, offline
python -m synergyrca --scenario quota   # one scenario
python -m synergyrca --json             # machine-readable output
```

## Use the LLM stages (optional)

```bash
export OPENAI_API_KEY=sk-...
export SYNERGYRCA_MODEL=gpt-4o          # default
python -m synergyrca
```

Without a key, every LLM stage uses a deterministic fallback, so the pipeline runs fully
offline. The reports show `Method: rules` when the fallback was used and `Method: llm`
when the model produced the report.

## Tests

```bash
python tests/test_pipeline.py   # 6 tests, offline, no dependencies
```

## Layout

```
synergyrca/
  graph.py       StateGraph, MetaGraph, metapath search, statepath instantiation
  llm.py         OpenAI client (stdlib HTTP) with JSON parsing
  modules.py     Triage, PathQueryGen, StateChecker, ReportGen, ReportQualityChecker
  driver.py      Driver loop with retries
  fixtures.py    Mock cluster incidents (quota, unbound PVC, missing ConfigMap, node pressure)
  __main__.py    CLI
tests/
  test_pipeline.py
```

## Differences from the paper

| Paper | This project |
|---|---|
| Neo4j graph database with Cypher execution | In-memory graph; Cypher is generated for display and audit, not executed |
| Spark and GraphFrames for building graphs | Plain Python, suited to small clusters and mock data |
| 5-minute polling of snapshots | Snapshots are passed in directly; no live collector yet |
| GPT-4o on Azure via the Assistants API | OpenAI chat completions; any compatible model can be swapped in `llm.py` |
| Two production clusters | Four mock scenarios; evaluation requires your own labeled data |

## Known limitations

- The offline fallbacks are rule-based. They reproduce the paper's reasoning for four
  failure types only. They are not a substitute for the LLM stages on new error types.
- Live cluster collection is not included. Connect a collector (for example, a
  Kubernetes watch via the official client) to feed `StateGraph.add_object`.
- The paper's contribution in full depends on evaluation against labeled incidents.
  The fixtures are for testing the pipeline, not for measuring precision.

## Live mode (real cluster)

Requires a local cluster (for example `kind`) and `kubectl get nodes` working.

```bash
pip install -r requirements.txt       # installs the kubernetes client
python -m synergyrca.live             # keep this running
```

In a second terminal, create the demo incidents:

```bash
kubectl create namespace demo
kubectl apply -f demo/quota-job.yaml
kubectl apply -f demo/missing-configmap.yaml
```

Within a few seconds the first terminal prints a report for each incident.

How it works: each watched kind is loaded once, then kept current from the Kubernetes
watch stream. Each new Warning event for a Pod or Job runs `diagnose()` against the
live state. ConfigMap and Secret data are never stored, only their existence.

Limitations of live mode: events that happen during a reconnect are skipped, node
conditions depend on the kubelet reporting them, and only the rules for the four
incident types are active.

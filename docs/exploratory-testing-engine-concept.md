# AI Exploratory Testing Engine — Concept Document

## 1. Core idea

An agent that closes the loop between **acting** on a system under test (SUT), **perceiving** what happened, **reasoning** about what's interesting or risky, and **deciding** what to try next — instead of running a fixed, pre-scripted test plan.

```
CAPTURE STATE → ACT → OBSERVE → HYPOTHESIZE → GENERATE NEXT TEST → ACT → ...
```

The differentiating idea is not "AI writes tests." It's building a genuine **disconfirmation engine**: every hypothesis the system forms must survive a real attempt to kill it before it's trusted, mirroring how actual scientific method works rather than how most current AI-testing tools work (which mostly confirm, rarely try hard to disprove themselves).

---

## 2. Architecture layers

```
Layer 0 — General truth        (spec, schema, docs)              [cheap, deterministic]
Layer 1 — Parallel oracles     (usability, statistical, spec,     [cheap–medium,
                                 consistency, comparable-product)   mostly heuristic]
Layer 2 — Oracle disagreement  + surprise scoring                 [LLM reasoning starts
                                                                     here — expensive]
Layer 3 — Hypothesis formation (competing explanations)           [expensive, rare]
Layer 4 — Confirm/disconfirm branch execution                     [action-driving]
Layer 5 — Guardrail heuristics (pacing / stopping / re-priority)  [meta-layer, runs
                                                                     across all others]
```

Cost control lives in this hierarchy: cheap deterministic checks run on every cycle; expensive LLM reasoning only fires when something in the cheap layer is flagged.

---

## 3. Components

### 3.1 Action / interface layer
Drives the SUT: browser automation, API calls, CLI/backend execution. Exposes a constrained "action space" (what's actually clickable/callable right now) so the reasoning layer isn't hallucinating actions that don't exist.

### 3.2 Signal collection layer
Captures more than pass/fail: response codes, latency, payload diffs, console errors, DOM/visual diffs, resource usage, logs. Normalized into a structured "observation" object per action.

### 3.3 State memory
A graph of visited states with edges = actions taken, plus a coverage map and a running list of unresolved anomalies. Prevents the agent from re-discovering the same state repeatedly.

### 3.4 Oracle layer
Instead of one model judging "is this weird," judgment is distributed across narrow, specialized oracles. Grounded in Rapid Software Testing's **FEW HICCUPPS** heuristic (see §5):

- **Spec-conformance oracle** — Claims / Standards: does this violate documented behavior (OpenAPI schema, error codes, UI copy)?
- **Self-consistency oracle** — Image / Product: does this contradict another part of the same system?
- **"Dumb user" oracle** — User's expectations: would a naive user find this confusing, regardless of spec correctness?
- **Statistical/trend oracle** — History: does this deviate from the system's own past behavior (latency drift, error-rate creep)?
- **Comparable-products oracle** — does this deviate from how equivalent systems in the same category typically behave?

Oracles are expected to **disagree with each other** — disagreement itself is a strong anomaly signal, often better than any single oracle's opinion alone.

### 3.5 Anomaly object

```
Anomaly {
  signal_ref: [pointer to raw observation],
  baseline_violated: self_consistency | declared_spec | prior_probability | ...,
  expected: "...",
  observed: "...",
  confidence: 0-1,     // how sure we are this IS a real deviation, not noise
  surprise_score: 0-1  // how unusual it is, independent of confidence
}
```

Low-confidence anomalies get routed to a cheap "did this actually happen" verification test before any hypothesis effort is spent on them.

### 3.6 Hypothesis object (tree, not list)

```
Hypothesis {
  claim: "...",                     // must be falsifiable and narrow
  anomaly_ref: [...],
  competing_with: [sibling hypothesis IDs explaining the same anomaly],
  predicted_if_true: "...",
  predicted_if_false: "...",
  severity_if_true: high | medium | low,
  test_plan: [confirm test, disconfirm test]
}
```

Hypotheses form a **tree**: a failed disconfirm attempt can either kill a branch or fork it into a sharper child hypothesis (e.g. "no validation" → refined to "validation exists but is inconsistently applied across entry points").

### 3.7 The disconfirmation loop

```
For hypothesis H:
  1. Design a confirming test (predict outcome A if H true)
  2. Design a disconfirming test (predict outcome B if H false —
     ideally something that would genuinely surprise you if H were true)
  3. Run both
  4. Compare outcomes:
     - confirm passes, disconfirm fails as predicted → H supported
     - confirm passes, disconfirm ALSO passes        → test didn't discriminate;
                                                          redesign it
     - confirm fails                                  → H refuted; promote a
                                                          competing hypothesis
```

A hypothesis that survives disconfirmation attempts is **corroborated**, never "proven" — this distinction should be preserved through to reporting (e.g. "survived 3 disconfirmation attempts" vs. "survived 0").

### 3.8 The critique pass ("Backseat Driver")
A second LLM pass whose only job is adversarial: find the boring, mundane, or alternative explanation that would make the hypothesis wrong or uninteresting, and check whether the proposed disconfirm test actually discriminates between competing explanations or just looks rigorous. This is a mitigation, not a guaranteed fix — LLMs are inherently biased toward confirmation-shaped reasoning even when explicitly asked to critique.

*(Alternative names considered: Skeptic, Second Opinion.)*

---

## 4. Guardrail heuristics (pacing & stopping)

Borrowed directly from Rapid Software Testing (Bach/Bolton). These act as **live re-weighting signals** into a test-prioritization engine — not a separate feature, but a dynamic input that shifts priority mid-session based on what's actually being observed.

| Heuristic | Trigger | Effect on prioritization |
|---|---|---|
| **Piñata** | Confirmed-anomaly rate in a component spikes above baseline | Stop going deep on any one hypothesis — widen instead; bugs are cheap to find right now |
| **Rumble strip** | A near-miss/partial anomaly precedes a possible cascade (degraded-but-not-failed response, growing latency, warning logs) | Escalate this thread immediately, even before oracle-disagreement is conclusive |
| **Dead horse / flat-lining** | N consecutive probes on a component yield no new information | Deprioritize; release budget back to the pool |
| **Iceberg** | Small surface signal, large predicted downstream blast radius (auth, money, data integrity) | Override normal ranking — investigate before "bigger-looking" but shallower anomalies |
| **Dead bee** | A previously flagged issue has gone quiet | Don't auto-close — schedule deliberate re-verification before marking resolved |
| **Rumsfeld** | A component/path has zero signals collected | Force a minimal probe — convert unknown-unknown into known-unknown |

**Session-level stopping heuristics** (distinct from per-component guardrails above) — end a charter when: a sufficiently dramatic problem is found, the system is flat-lining with no new variation, or the value of continuing no longer justifies the cost.

---

## 5. Grounding in Rapid Software Testing

| RST concept | Role in this engine |
|---|---|
| **Testing vs. checking** | Checking (deterministic match) is automatable and cheap; testing requires sapient judgment about relevance — this is what the LLM layers are standing in for, imperfectly. Be honest about this boundary. |
| **SFDIPOT** (Structure, Function, Data, Interfacing, Platform, Operations, Time) | Coverage checklist used to generate exploration **charters** — scoped, time-boxed missions rather than unbounded wandering |
| **FEW HICCUPPS** (Familiarity, Explainability, World, History, Image, Comparable products, Claims, User's expectations, Product, Purpose, Standards, Statutes) | Taxonomy underlying the oracle layer (§3.4) |
| **Session-based test management** | Charters bound scope and time; this is the engine's stopping condition and relevance filter |
| **Stopping heuristics** | See §4 |

---

## 6. Getting the human out of the loop — realistically

Testing requires sapience (judgment about relevance and value), and that can't be fully automated away — only substituted with an LLM's weaker version of it. A workable, honest design:

1. **Bound exploration with charters**, not open-ended wandering (SFDIPOT-derived, time-boxed, scoped).
2. **Tier autonomy by severity.** Low-severity/cosmetic findings: fully autonomous. High-severity findings (auth, data loss, security-shaped anomalies): human review gate before being reported as confirmed — even if disconfirmation failed to kill the hypothesis.
3. **Start in reporting-only mode.** Let humans calibrate against the automated findings before promoting any check to an autonomous gate. Earn autonomy with evidence, don't assume it.

---

## 7. Tooling — bolt on vs. build from scratch

### Bolt on (open source, don't rebuild)
- **Action/interface layer:** Playwright, Browser-use (LLM-driven browser control, multi-step planning), Stagehand, AgentQL (structured page-data extraction)
- **API fuzzing:** Schemathesis (schema-driven property/fuzz testing), RESTler (Microsoft — stateful REST fuzzer that infers producer-consumer dependencies and builds call sequences; close analog to state-graph exploration)
- **Signal capture:** Playwright trace/HAR/console capture, BackstopJS or reg-suit (visual diffing), OpenTelemetry (if SUT exposes traces)
- **Orchestration:** LangGraph (branching/cyclic graph execution — fits the confirm/disconfirm tree and nested loops directly), SQLite/Postgres (state graph + hypothesis tree persistence)
- **Agent observability:** Langfuse or Phoenix (Arize, open source) — trace *your own* agent's decisions to debug bad hypothesis chains

### Build from scratch (the actual product)
- Oracle definitions and prompts (grounded in FEW HICCUPPS, tailored to your domain)
- Oracle disagreement / surprise-scoring logic
- Hypothesis object + tree lifecycle (claim, competing explanations, confirm/disconfirm predictions, severity, lineage)
- Backseat Driver adversarial critique prompt + re-design routing when a test fails to discriminate
- Charter generator (SFDIPOT + app context → scoped missions)
- Replication/significance check before promoting a hypothesis to "reportable"
- Guardrail heuristics engine feeding the prioritization tool
- Severity-tiered human-gate logic

---

## 8. Model / cost strategy

Use the Anthropic API directly — no fine-tuning, no custom model training, no multi-provider ensemble needed at this stage. Tier by model, not by architecture:

- **Tier 1 (deterministic oracles):** plain code, no LLM call, zero cost
- **Tier 2 (shallow LLM oracles — dumb-user, consistency checks):** Haiku — frequent, fast, cheap
- **Tier 3 (hypothesis formation, competing explanations, Backseat Driver critique, disconfirm test design):** Sonnet — rare, higher per-call cost, but this is where reasoning quality actually matters

Cost levers:
- **Prompt caching** — shared context (app state, hypothesis history, oracle taxonomy, charter) repeats call-to-call; cache it.
- **Structured output via tool use** — define Hypothesis/Anomaly/Test as tool schemas rather than parsing free text.

---

## 9. Realistic scope assessment

**Solved / low-risk:** action layer, signal capture, state persistence, orchestration. ~2–4 weeks of engineering using existing open-source tools.

**Genuinely uncertain / research-grade:** oracle disagreement tuning (precision/recall gets harder as you add oracles, not easier), hypothesis quality, and especially disconfirmation quality — LLMs default to confirmation-shaped reasoning, and a "disconfirm test" that looks rigorous but doesn't actually discriminate is a real, recurring failure mode. This is a months-long tuning problem, not a one-time build, and may never fully close to "trustworthy enough to remove the human gate."

**Recommended path:** don't build the full architecture before validating the core loop. Pick one real system, one SFDIPOT dimension, skip charters/guardrails/full oracle layer initially. Hand-build 10–20 known anomalies and test whether the hypothesis-generation prompt reliably proposes correct competing explanations and a disconfirm test a human would agree actually discriminates. If that holds up on known cases, build the rest around it. If it doesn't, that's a cheap, fast thing to learn — much cheaper than learning it after the full system is built.

A narrower, shippable first version (one SFDIPOT dimension, one or two oracles, linear confirm/disconfirm instead of a full tree, human-gated reporting) is realistic in weeks and is a genuinely useful exploratory-testing assistant on its own — independent of whether the fuller autonomous vision ever fully pans out.

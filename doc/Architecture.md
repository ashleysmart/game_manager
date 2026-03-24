# GameManager — Architecture

## Meta

- Doc Type: architecture/game-manager
- Version: 0.1.0
- Status: draft
- Depends On:
  - GameManager Specification
  - LLM Game Engine Architecture Specification (read-only reference)

---

## 1. Purpose

This document describes the architectural structure of the GameManager service:
how it is internally organised, how its components interact, and how it interfaces
with the RuleEngine (RE).  It does not define individual API endpoints (see
Design.md) or detailed domain models (see Specification.md).

---

## 2. Architectural Position

```
┌────────────────────────────────────────────────────────────────────┐
│  Player / UI layer                                                 │
│  (browser, native app, voice client, LLM chat surface)            │
└────────────────────────────┬───────────────────────────────────────┘
                             │  HTTP  (X-Correlation-Id on every request)
┌────────────────────────────▼───────────────────────────────────────┐
│  GameManager  (this service)                                       │
│                                                                    │
│  Phase gate ─► Scene orchestration ─► Action mediation            │
│  Wizard host  ─► Projection build  ─► Persistence                 │
└────────────────────────────┬───────────────────────────────────────┘
                             │  HTTP  (X-Correlation-Id forwarded)
┌────────────────────────────▼───────────────────────────────────────┐
│  RuleEngine  (RE)                                                  │
│                                                                    │
│  Canonical world state  ─► Action pipeline  ─► Journal            │
│  Affordances  ─► Replay  ─► Save / checkpoint                     │
└────────────────────────────────────────────────────────────────────┘
```

**Invariant:** GameManager never writes canonical world state directly.  All
gameplay mutations are submitted as action envelopes to the RE action pipeline.
Scene assembly writes (create map, create entity, place presence, set explored
index) are treated as configuration operations, not gameplay mutations — they
are idempotent and always occur during `scene_setup` phase before the scene
becomes active.

---

## 3. Internal Component Architecture

```
GameManager service
│
├── API layer
│     └── HTTP router + request/response serialisation
│           Routes to handler per endpoint; validates bearer token; extracts
│           X-Correlation-Id (generated if absent)
│
├── FlowEngine
│     └── Enforces valid phase transitions; gates commands per phase;
│           drives the CampaignRuntime state machine
│
├── CampaignRuntime
│     └── Holds CampaignRuntimeState for one session:
│           flow_phase, active_scene_id, plot_map_ids,
│           player_actor_ids, current_actor_id, pending_wizard,
│           scheduled_tasks, manager_flags
│
├── SceneOrchestrator (abstract)
│     ├── BattleMapOrchestrator
│     │     Scene modes: battle_map
│     │     Handles: spatial map, encounter groups, clock, door / portal /
│     │     container / fog-of-war setup; portal-transition handoff
│     ├── TravelOrchestrator
│     │     Scene modes: theater_of_mind / travel
│     │     Handles: route tracker, event decks, encounter escalation,
│     │     overworld → dungeon portal transitions
│     └── SocialOrchestrator
│           Scene modes: theater_of_mind / social
│           Handles: NPC entities, faction trackers, temporal map threads
│
├── WizardHost
│     └── Step-engine for multi-step interactive flows (character_creation,
│           level_up); translates completed inputs to RE action envelopes
│
├── SystemActionScheduler
│     └── Queue of SystemTask objects; fires RE system actions at due clock
│           values; tracks fired task IDs to prevent replay double-fire
│
├── ProjectionBuilder
│     └── Reads RE world state + CampaignRuntimeState; assembles
│           UIFlowSnapshot; derives current visibility from RE explored index
│
├── ManagerRepository
│     └── Persists and loads CampaignRuntimeState; versioned schema migration
│
└── REClient
      └── Typed HTTP client for all RE endpoints; injects X-Correlation-Id
            header on every call; handles RE error responses uniformly
```

---

## 4. Component Interaction Model

### 4.1 Normal Action Path

```
UI  ──POST /actions──►  API layer
                            │
                       FlowEngine.gate(phase)
                            │ (phase == awaiting_player_action)
                       REClient.submitAction(envelope)
                            │
                       RE pipeline: validate → resolve → commit → journal
                            │
                       ActionResult (status, emitted_events, ...)
                            │
                       FlowEngine.processResult(result)
                            │
                       SceneOrchestrator.onEvents(emitted_events)
                            │  (exit condition? → scene_transition)
                            │  (portal_transition? → load destination scene)
                            │  (level-up event? → wizard_active)
                            │
                       ProjectionBuilder.build()
                            │
                       UIFlowSnapshot ◄──────────────────────────────
```

### 4.2 Scene Setup Path

```
FlowEngine enters scene_setup
        │
SceneOrchestrator.setup(SceneDefinition)
        │
        ├── POST /world/entities         (spawn doors, portals, containers,
        │                                 enemies, NPCs, items)
        ├── PUT  /world/maps/{id}/presence/{entity_id}
        │                                (place entities on map)
        ├── PUT  /world/maps/{id}/explored/{faction_id}
        │                                (pre-reveal scripted areas, if any)
        ├── POST /world/groups           (encounter groups, command groups)
        ├── POST /world/trackers         (route, supply, fatigue, etc.)
        ├── POST /world/decks            (event, encounter, quest decks)
        └── PUT  /scene                  (set mode, presentation, references)
        │
FlowEngine → awaiting_player_action
```

### 4.3 Portal Transition Handoff

```
RE emits portal_transition_completed + scene_transition_requested
        │
SceneOrchestrator.onEvents detects scene_transition_requested
        │
FlowEngine → scene_transition
        │
SceneOrchestrator.teardown(current_scene)
        │
FlowEngine → scene_setup
        │
SceneOrchestrator.setup(destination_scene)
  (actor presence already on destination map — written by RE pipeline;
   orchestrator configures remaining scene resources)
        │
FlowEngine → awaiting_player_action
```

### 4.4 System Task Path

```
SystemActionScheduler.tick(current_clock)
        │
        ├── due tasks filtered by due_at_clock_or_round
        │
        ├── per task:
        │     REClient.call(task.re_endpoint, system_entity_id, ...)
        │     task marked fired (stored in manager state for replay safety)
        │
FlowEngine processes any resulting emitted_events (same as normal action path)
```

---

## 5. Data Ownership Boundary

### 5.1 What RE Owns (GameManager reads, never duplicates)

| RE resource | Why GM does not own it |
|---|---|
| Entity blocks (stat, door, portal, container, inventory, …) | Canonical, journaled; GM reads on demand |
| Map presence index | Authoritative positions; GM reads via GET /world/maps/{id}/presence |
| Map explored index | Canonical fog-of-war; GM reads via GET /world/maps/{id}/explored/{faction_id} |
| Clock (round, initiative_step, date/time) | RE manages; GM reads to drive turn logic |
| Groups (encounter, command) | Canonical membership and turn order |
| Trackers | Canonical world measurements |
| Decks and card state | Canonical draw history |
| Journal | Authoritative history; GM reads for projection |
| Save slots / checkpoints | RE owns; GM coordinates at autosave points |

### 5.2 What GM Owns (not in RE)

| GM state | Stored in |
|---|---|
| `flow_phase` | `CampaignRuntimeState` |
| `active_scene_sid` (RE SID reference) | `CampaignRuntimeState` |
| `plot_map_sids` (RE SID references) | `CampaignRuntimeState` |
| `pending_wizard` and step inputs | `WizardSessionState` (embedded in `CampaignRuntimeState`) |
| `scheduled_tasks` and fired-task history | `CampaignRuntimeState` |
| `manager_flags` (flow-decision bag) | `CampaignRuntimeState` |
| GM phase transition journal | Append-only manager journal (separate from RE journal) |

---

## 6. Key Architectural Decisions

### 6.1 Doors, Portals, Containers Are RE Entities

These are not modelled in GM state.  GameManager creates them during scene
assembly and then interacts with them exclusively through RE action envelopes.
GM reads their current state via `GET /world/entities/{id}` when it needs to
display information in `UIFlowSnapshot`.

### 6.2 Fog of War Is an RE Resource

The explored index is authoritative RE world state.  GM reads it from
`GET /world/maps/{map_id}/explored/{faction_id}` when building `UIFlowSnapshot`.
GM may write it during scene setup (`PUT`) to pre-reveal scripted areas.
GM never caches or shadows it.  Replay is correct because RE journals every
explored-index mutation.

### 6.3 Portal Transitions Are RE-Driven

The RE pipeline atomically moves actor presence and emits `scene_transition_requested`.
GM observes the event and orchestrates scene teardown/setup.  GM does not
move entities or update map presence itself during a portal transition.

### 6.4 Container Mutations Are Always Action Envelopes

GM never manually constructs `container_block.items` mutations.  Loot seeding
uses system action envelopes (`put_in_container`, or deck `container_spawn`
payloads), which pass through the RE pipeline and are journaled.  This ensures
container state is always correct and replayable.

### 6.5 Affordance Augmentation

GM's `/actions/suggest` delegates to RE `POST /v1/sessions/{id}/actions/suggest`
and then augments the result with:
- Wizard-aware actions (if `pending_wizard` is set)
- Flow-phase-filtered actions (remove verbs not permitted in current phase)
- Portal and door quick-actions if scene contains them (convenience layer only;
  RE affordances are authoritative for legality)

### 6.6 No World State Duplication

GM does not cache RE world state between requests.  Every `UIFlowSnapshot`
build reads fresh from RE.  This avoids cache-invalidation complexity and
ensures the UI always reflects authoritative state.

---

## 7. Error Handling Contract

| RE response | GM behaviour |
|---|---|
| `accepted_and_committed` | Advance phase; rebuild UIFlowSnapshot |
| `accepted_noop` | No phase change; rebuild UIFlowSnapshot |
| `rejected_invalid` | Return validation reasons to UI; remain in `awaiting_player_action` |
| `rejected_duplicate` | Return prior result to UI; remain in current phase |
| `partial_requires_completion` | Return suggestions to UI; remain in `awaiting_player_action` |
| `failed_resolution` | Log error; return error to UI; do not advance phase |
| RE 5xx | Retry with exponential backoff (up to 3 attempts); surface error if all fail |
| RE 409 deck_exhausted | Surface to UI via `UIFlowSnapshot.errors`; no phase change |

---

## 8. Observability

### 8.1 Correlation

Every UI request carries `X-Correlation-Id`.  All GM structured log entries
and all RE calls for that request carry the same value.  This threads through
to RE journal entries, enabling full cross-service tracing.

### 8.2 Structured Log Events

| Event | Key fields |
|---|---|
| `phase_transition` | `from, to, trigger, session_id, correlation_id` |
| `scene_setup_started` | `scene_id, mode, presentation, resource_counts, session_id` |
| `scene_setup_complete` | `scene_id, elapsed_ms, session_id` |
| `portal_transition_observed` | `actor_id, from_map_id, to_map_id, portal_entity_id, session_id` |
| `wizard_step` | `wizard_type, step, status, session_id` |
| `system_task_fired` | `task_type, task_id, session_id` |
| `action_mediated` | `action_type, actor_id, re_status, correlation_id` |
| `container_seeded` | `container_id, item_count, scene_id, session_id` |

---

## 9. Open Questions

- Whether `BattleMapOrchestrator` should be reused across floors (re-init for
  new map) or instantiated fresh per map — preference is fresh instance per
  active scene.
- Multi-party portal transit (whole group uses portal simultaneously) — RE
  emits one `portal_transition_completed` per actor; GM must coalesce into a
  single scene handoff.
- Concurrent portal use by enemies during an encounter — GM currently defers
  scene transition until player's turn ends; this needs ruleset clarification.

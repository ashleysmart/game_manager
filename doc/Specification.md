# GameManager — Specification

## 1. Purpose

GameManager is the campaign runtime orchestrator for solo tabletop RPG play. It is the flow controller that sits between the player-facing UI and the pure-execution RuleEngine (RE).

**GameManager owns:**
- Session-level flow phase (what the player is allowed to do right now)
- Campaign progression logic (when to advance, which scene comes next)
- Scene configuration (assembling RE resources for each scene type)
- Wizard / hosted-flow orchestration
- UI-facing snapshots (`UIFlowSnapshot`)
- Manager-layer persistence (`CampaignRuntimeState`)

**RuleEngine owns:**
- Canonical world state (entities, maps, groups, trackers, decks, clock)
- Action validation, resolution, and commit
- Execution journal and replay
- Save slots and checkpoints
- Affordance generation

**Invariant**: GameManager never mutates world state directly. All world changes are submitted as action envelopes to the RuleEngine pipeline. All gameplay-relevant reads come from RE world-state endpoints.

---

## 2. Core Responsibilities

1. **Campaign runtime ownership** — track flow phase, plot progression, and campaign end states
2. **Scene lifecycle orchestration** — configure and drive each scene from setup to exit via RE resources
3. **Player / NPC / system turn-flow control** — manage phase gating and whose turn it is
4. **Wizard / hosted-flow orchestration** — launch and complete character creation, level-up, and future wizards
5. **Deck- and tracker-driven world events** — schedule deck draws and tracker-driven checks via RE
6. **UI-facing flow state and prompts** — produce `UIFlowSnapshot` from RE world state + manager state
7. **Persistence, replay anchors, recovery** — snapshot and restore `CampaignRuntimeState`; coordinate with RE checkpoints

---

## 3. Architecture

```
UI
 └── GameManager service layer
       ├── CampaignRuntime          per-session domain object (manager state only)
       ├── FlowEngine               phase state machine
       ├── SceneOrchestrator
       │     ├── BattleMapOrchestrator
       │     ├── TravelOrchestrator
       │     └── SocialOrchestrator
       ├── WizardHost               interactive flow runner
       ├── SystemActionScheduler    timed / deferred tasks
       ├── ProjectionBuilder        UIFlowSnapshot from RE world state + manager state
       └── ManagerRepository        CampaignRuntimeState persistence
 └── RuleEngine  (canonical world state + action pipeline)
       ├── World state  (entities, maps, groups, trackers, decks, clock, scene)
       ├── Action pipeline  (validate → resolve → commit → journal)
       ├── Affordance API
       ├── Journal & replay
       └── Save / load / checkpoints
```

### Component Responsibilities

| Component | Role |
|---|---|
| `CampaignRuntime` | Manager-layer mutable state per session; does not duplicate RE world state |
| `FlowEngine` | Enforces valid phase transitions; gates which commands are allowed |
| `SceneOrchestrator` | Assembles RE resources (maps, groups, trackers, decks) for each scene; drives lifecycle |
| `WizardHost` | Step engine for multi-step interactive flows; translates completed inputs to RE action envelopes |
| `SystemActionScheduler` | Enqueues timed tasks (deck draws, end-turn checks) and fires them as RE system actions |
| `ProjectionBuilder` | Reads RE world state + manager state; produces `UIFlowSnapshot` |
| `ManagerRepository` | Persists and loads `CampaignRuntimeState`; supports versioned schema migration |

---

## 4. Domain Models

### 4.0 Short ID Scheme

Every GM-owned and RE-owned resource is identified by a single **short ID (`sid`)**: 8 characters from `[A-Za-z0-9]` (62⁸ ≈ 218 trillion combinations).

| Property | Detail |
|---|---|
| Format | 8 chars `[A-Za-z0-9]` |
| Generation | CSPRNG at resource creation; base-62 encoded |
| Scope | Unique per resource type (sessions, entities, maps, groups, trackers, decks each namespaced separately) |
| Immutability | Never changes after creation |

**There is no UUID in the public API.** Internal storage may use any key format; that is an implementation detail invisible to callers. All API paths, request bodies, and response bodies use `sid` exclusively.

**URL routing:** All path segments use `{sid}`. A request with a UUID-formatted value in a path returns `400 Bad Request`.

```
✓  GET /v1/sessions/aB3kR7mX/flow
✗  GET /v1/sessions/550e8400-e29b-41d4-a716-446655440000/flow  → 400
```

**Action idempotency:** The action envelope carries a separate `idempotency_key` field (any unique string — typically a freshly generated base-62 token) that is not a resource SID. See §6.1.

---

### 4.1 CampaignRuntimeState

Manager-layer state. Does not replicate RE world state — references RE resource SIDs only.

```
CampaignRuntimeState
  session_id             sid               RE session short ID (shared key)
  campaign_id            sid
  flow_phase              FlowPhase
  active_scene_id        sid  | null       Short ID of active RE scene resource
  plot_map_ids           sid[]             RE temporal map SIDs tracking plot threads
  player_actor_ids       sid[]             RE entity SIDs of player characters
  current_actor_id       sid  | null       RE entity SID of whose turn it is
  pending_wizard          WizardSessionState | null
  scheduled_tasks         SystemTask[]
  manager_flags           {}                GM-layer key-value bag (not world flags)
  schema_version          string
```

> RE world state holds all canonical data — entity blocks, tracker values, deck state, map presence, clock. `CampaignRuntimeState` holds only what the manager needs to make flow decisions that are not derivable from RE world state alone.

### 4.2 SceneDefinition

References RE resources; does not embed their content.

```
SceneDefinition
  scene_id               sid
  mode                    "battle_map" | "theater_of_mind"
  presentation            "battle_map" | "travel" | "social" | "shop" | "info"
  participant_ids        sid[]          RE entity SIDs
  resources
    spatial_map_id       sid  | null    RE spatial map SID (battle_map mode)
    temporal_map_ids     sid[]          RE temporal map SIDs (plot threads for this scene)
    group_ids            sid[]          RE group SIDs
    tracker_ids          sid[]          RE tracker SIDs
    deck_ids             sid[]          RE deck SIDs
  entry_conditions        condition[]
  exit_conditions         condition[]
```

### 4.3 FlowPhase

All valid runtime phases. See §6 for transition table.

```
bootstrap
campaign_selection
scene_setup
awaiting_player_action
resolving_action
system_step
wizard_active
scene_transition
campaign_end
```

### 4.4 WizardSessionState

```
WizardSessionState
  wizard_id              sid
  wizard_type             "character_creation" | "level_up" | ...
  step                    integer
  inputs                  {}             accumulated answers
  required_fields         string[]       fields not yet supplied
  status                  "active" | "complete" | "cancelled"
```

### 4.5 SystemTask

```
SystemTask
  task_id                sid
  task_type               "draw_deck"
                        | "weather_tick"
                        | "random_encounter_check"
                        | "end_turn_effects"
  due_at_clock_or_round   timestamp | integer   RE clock value when task fires
  payload                 {}
```

### 4.6 UIFlowSnapshot

View model pushed to UI after every state change. Built by `ProjectionBuilder` from RE world state + `CampaignRuntimeState`.

```
UIFlowSnapshot
  status                  FlowPhase
  scene_summary           { mode, presentation, active_actor, round, ... }
  active_prompt           string | null
  available_actions       ActionAffordance[]
  wizard                  WizardPrompt | null      (non-null only in wizard_active)
  recent_journal_events   JournalEvent[]
  errors                  string[]
```

---

## 5. RuleEngine Resource Mapping

GameManager concepts map to RE world-state resources as follows.

### 5.1 Plot Progression → Temporal Maps

Campaign plot nodes, quest stages, and narrative objectives are modeled as RE temporal-family maps (`family: temporal`). Each thread has a separate temporal map.

- `graph` level: non-linear branching plots, quest graphs
- `vector` level: timed objective graphs
- `sequence` level: scripted encounter phases, NPC routines

Progression state is expressed through the map's **presence index**: items placed in a temporal map carry a `location.state` of `pending`, `active`, or `completed`. GameManager reads this to determine plot advancement; it advances state by submitting RE actions or system actions that update presence.

### 5.2 Encounter State → RE Clock + Groups + Scene

RE does not have a single encounter object. Encounter state is distributed:

| Concept | RE resource |
|---|---|
| Encounter active | `clock.round > 0` |
| Current initiative position | `clock.initiative_step` |
| Combat sides / factions | `encounter_group`-typed groups; each member carries `order_value` and `has_acted` |
| Active map | `scene.mode = "battle_map"`, `scene` references spatial map SID |
| End of encounter | GM sets `clock.round = 0` and `clock.initiative_step = 0` via `PUT /world/clock` |

GameManager reads the RE clock and encounter groups to determine current actor and turn order; it updates them by submitting actions and calling `POST /turn/end`.

### 5.3 Entity Location → Map Presence

Entity position is **not** an entity block. It is held in the map's presence index. To place, move, or remove an entity: `PUT /world/maps/{map_id}/presence/{entity_id}`. Location shape varies by map type:

| Map level | Location |
|---|---|
| `spatial tile` | `{ "x": 3, "y": 7 }` |
| `spatial graph` | `{ "node_id": "<sid>" }` |
| `spatial vector` | `{ "node_id": "<sid>", "offset": { "x": 0.5, "y": 1.2 } }` |
| `temporal graph/vector` | `{ "node_id": "<sid>", "state": "active|completed|pending" }` |
| `temporal sequence` | `{ "sequence_index": 4, "state": "active|completed|pending" }` |

### 5.4 Deck Draws → RE System Actions

GameManager does not draw cards directly and apply effects. It calls `POST /world/decks/{deck_id}/draw`, which:
1. Advances the deck to the next undrawn card
2. Submits the card payload as a system action (actor = system entity, source_type = `system`)
3. Runs the payload through the full RE action pipeline
4. Returns the drawn card and action result

### 5.5 World Clock → RE Clock

Time advancement, round tracking, and initiative steps are all managed via the RE clock endpoint (`GET/PUT /world/clock`). GameManager does not maintain its own time state.

### 5.6 Doors and Keys → RE Prop Entities

Doors are `prop` entities carrying a `door_block`. Keys are `item` entities carrying a `key_block`. Both are first-class RE world state and are created through the standard entity endpoints.

GameManager interacts with doors through the RE action pipeline only — it never mutates `door_block.state` directly. The relevant action verbs are:

| Action verb | Pre-condition | Effect |
|---|---|---|
| `open_door` | reachable; `door_block.state == closed` | `closed → open`; emits `door_opened` |
| `close_door` | reachable; `door_block.state == open` | `open → closed`; emits `door_closed` |
| `lock_door` | reachable; `closed`; actor holds matching key | `closed → locked`; emits `door_locked` |
| `unlock_door` | reachable; `locked`; actor holds matching key | `locked → closed`; emits `door_unlocked` |
| `force_door` | reachable; not `smashed` | `any → smashed`; emits `door_forced` |

**State model:** `open` (passable) → `closed` (blocked) → `locked` (blocked) → `smashed` (passable, terminal until repaired).

The map edge between two nodes carries only a `constraint.door_entity_id` reference; the RE validation layer resolves passability from `door_block.state` at runtime. GameManager does not need to track door state separately — it reads the entity block on demand.

**Scene assembly:** When `BattleMapOrchestrator` sets up a map, it creates door entities (`POST /world/entities`) and references them from map edge constraints. Key entities may be placed in NPC inventory blocks or container blocks as part of scene setup.

### 5.7 Portals → RE Prop Entities

Portals are `prop` entities carrying a `portal_block`. They model stairs, ladders, hatches, teleporters, map gates, and any transition that moves an actor between map locations.

Portal state model:

| State | Usable |
|---|---|
| `active` | yes |
| `inactive` | no |
| `locked` | no — requires unlock action |

The single action verb is `use_portal` (`actor_id`, `target_id` = portal entity SID).

**Transition contract (RE pipeline):**
1. Validates portal reachable and `active`; applies encounter movement rules if `clock.round > 0`.
2. Atomically removes actor presence from source map/node; adds presence at `destination_map_id` / `destination_node_id`.
3. Emits `portal_transition_completed` (actor_id, from_map_id, from_node_id, to_map_id, to_node_id).
4. If destination map ≠ active scene map: also emits `scene_transition_requested`.

**GameManager responsibility:** `BattleMapOrchestrator` (and `TravelOrchestrator` for overworld-to-dungeon transitions) monitors `emitted_events` from each action result for `scene_transition_requested`. On receipt, the orchestrator triggers a `scene_transition` phase and loads the destination map as the new scene.

Portal entities are created during scene assembly. Bidirectional pairs must be created together: each portal's `portal_block.return_portal_id` references its companion. One-way portals carry `return_portal_id: null`.

### 5.8 Containers → RE Item Entities

Containers are `item` entities whose `container_block` holds an ordered list of item entity SIDs. They share the same entity endpoints as all other items.

Container state:

| State | Accessible |
|---|---|
| `open` | yes |
| `closed` | no — requires `open_container` action |
| `locked` | no — requires matching key entity |

Relevant action verbs:

| Verb | Effect |
|---|---|
| `open_container` | `closed → open` |
| `close_container` | `open → closed` |
| `put_in_container` | Moves item from actor's `inventory_block` into `container_block.items` |
| `take_from_container` | Moves item from `container_block.items` into actor's `inventory_block` |
| `transfer_item` | Atomic move between two `container_block`s or between a `container_block` and map presence |

The pipeline validates accessibility, key match (if locked), capacity limits, and cycle detection (a container cannot contain itself transitively). **GameManager never constructs containment mutations manually** — all transfers go through action envelopes.

**Map presence rule:** Only the outermost entity in a containment chain has map presence. Items inside a container or inside a character's inventory have no independent presence. When a container moves, all its contents move with it automatically.

**Scene assembly:** Containers and their initial contents are seeded during scene setup by creating entities and calling `put_in_container` system actions (or by using the deck `container_spawn` payload type via `POST /world/decks/{deck_id}/draw`).

### 5.9 Visibility and Fog of War → RE Explored Index

The RE distinguishes three visibility concepts, each with different authority:

| Concept | Authority | RE endpoint |
|---|---|---|
| Explored state (fog of war) | **Canonical world resource** — stored, journaled | `GET/PUT/DELETE /world/maps/{map_id}/explored/{faction_id}` |
| Line of sight | Derived — pure function of positions + terrain | Computed on affordance query; never stored |
| Current visibility | Derived — explored + LoS | Computed on demand; never stored |

**Explored state** is the only visibility concept GameManager needs to reason about. It is updated automatically by the RE when any faction member enters a new node or tile (journaled mutation). GameManager reads it for:
- Rendering the fog layer in `UIFlowSnapshot.scene_summary`
- Filtering affordance suggestions to visible targets
- Scene setup: pre-revealing areas with `PUT /world/maps/{map_id}/explored/{faction_id}` (scripted revelation)

**GameManager does not implement its own fog cache.** The explored index is authoritative RE world state. On replay, the RE journal reconstructs it identically.

**GM-initiated revelation:** For scripted scenes (cutscenes, map unlocks, teleport-to-known-location), the `BattleMapOrchestrator` may call `PUT /world/maps/{map_id}/explored/{faction_id}` directly during scene setup. This is treated as a configuration operation, identical in kind to placing entities or setting map presence (see §6.3 Invariants).

---

## 6. GameManager ↔ RuleEngine Contract

### 6.1 Action Envelope (GM → RE)

```json
{
  "idempotency_key": "<token>",      // unique per submission — any string; typically 8-char base-62
  "actor_id":       "<entity_id>", // RE entity SID; system entity SID for system actions
  "action_type":     "<canonical_verb>",
  "source_type":     "player|gm|llm|system",
  "target_id":      "<entity_id>", // optional
  "instrument_id":  "<entity_id>", // optional
  "X-Correlation-Id": "<token>"      // passed as request header for tracing
}
```

> `idempotency_key` is a per-submission token (not a resource SID) — it allows RE to deduplicate retried requests. The tracing correlation ID is a separate header concern, not part of the action payload.

### 6.2 Action Result (RE → GM)

```json
{
  "status":                "accepted_and_committed"
                         | "accepted_noop"
                         | "rejected_invalid"
                         | "rejected_duplicate"
                         | "partial_requires_completion"
                         | "failed_resolution",
  "journal_entry_id":     "<sid>",        // null on rejection before journal write
  "validation": {
    "valid":               true | false,
    "reasons":             []
  },
  "resolution_result":     {},             // rolls, effects, outcomes
  "mutation_set":          [],             // exact world-state mutations applied
  "emitted_events":        [],             // events emitted during resolution
  "committed_at":          "<iso8601>"
}
```

GameManager uses `emitted_events` to detect scene exit conditions, level-up triggers, and plot flag updates — never by reading `mutation_set` directly.

### 6.3 Invariants

- GameManager never calls any world-state write endpoint except through the RE action pipeline.
- Exception: scene assembly calls (create map, create group, place entity in map, create tracker, create deck) are GM-initiated RE resource writes that set up a scene before it becomes active. These are configuration, not gameplay mutations, and are idempotent by design.
- All RE calls from GameManager carry a correlation ID header traceable from the originating UI request.
- GameManager reads RE world state freely for projection and flow decisions.

---

## 7. Flow Phase State Machine

### 7.1 Phase Transition Table

| From | To | Trigger |
|---|---|---|
| `bootstrap` | `campaign_selection` | session initialized |
| `campaign_selection` | `scene_setup` | campaign loaded; no wizard needed |
| `campaign_selection` | `wizard_active` | campaign loaded; character creation required |
| `wizard_active` | `scene_setup` | wizard completed |
| `wizard_active` | `campaign_selection` | wizard cancelled (no characters exist) |
| `scene_setup` | `awaiting_player_action` | scene configured and active |
| `awaiting_player_action` | `resolving_action` | player submits action |
| `awaiting_player_action` | `system_step` | scheduler fires a system task |
| `awaiting_player_action` | `wizard_active` | level-up or other wizard triggered |
| `resolving_action` | `awaiting_player_action` | RE returns accepted; same actor continues |
| `resolving_action` | `system_step` | action accepted; system tasks pending |
| `resolving_action` | `scene_transition` | RE emits event matching exit condition |
| `system_step` | `awaiting_player_action` | system tasks drained |
| `system_step` | `scene_transition` | system task emits event matching exit condition |
| `scene_transition` | `scene_setup` | next scene determined |
| `scene_transition` | `wizard_active` | level-up triggered between scenes |
| `scene_transition` | `campaign_end` | campaign end condition met |
| `campaign_end` | — | terminal |

### 7.2 Allowed Commands Per Phase

| Phase | Allowed |
|---|---|
| `bootstrap` | session init only |
| `campaign_selection` | load campaign, create session |
| `scene_setup` | read-only (GM is assembling RE resources) |
| `awaiting_player_action` | submit action, suggest actions, read flow/scene |
| `resolving_action` | read-only |
| `system_step` | read-only |
| `wizard_active` | wizard input, wizard cancel |
| `scene_transition` | read-only |
| `campaign_end` | read-only, save |

### 7.3 Conflict Policy

Actions arriving during `system_step`, `resolving_action`, or `wizard_active` are queued and submitted once the phase returns to `awaiting_player_action`. Duplicate `idempotency_key` values are silently deduplicated by RE; GameManager does not need to deduplicate them itself.

### 7.4 Replay Safety

All phase transitions are written to the manager journal with triggering event and `schema_version`. On recovery, the phase machine is rebuilt from RE world state + RE journal + the latest `CampaignRuntimeState` snapshot. RE's own journal replay (`POST /journal/replay`) reconstructs world state; manager phase is overlaid from the manager snapshot.

---

## 8. Scene Orchestrators

Scene orchestrators assemble RE resources during `scene_setup` phase, then interpret RE-emitted events during the active phase to detect exit conditions.

### 8.1 BattleMapOrchestrator

**Setup (RE resource assembly):**
- Create or reference spatial map (tile or vector level): `POST /world/maps`
- Spawn entities (enemies, allies, doors, portals, containers): `POST /world/entities`
- Place entities on map: `PUT /world/maps/{map_id}/presence/{entity_id}` with tile/vector location
- Create encounter group per side: `POST /world/groups` (`group_type: encounter_group`)
- Add members to groups with `order_value`: `POST /world/groups/{id}/members`
- Initialize clock for encounter: `PUT /world/clock` (`round: 1, initiative_step: 1`)
- Pre-reveal scripted areas (if any): `PUT /world/maps/{map_id}/explored/{faction_id}`
- Seed container loot: submit `put_in_container` system actions, or draw from a deck carrying `container_spawn` payloads
- Set scene mode: `PUT /scene` (`mode: battle_map`, resource references)

**Active:**
- `current_actor_id` derived from encounter group member with current `initiative_step` and `has_acted: false`
- After each actor action: call `POST /turn/end` to advance clock, tick effects, evaluate events
- Monitor `emitted_events` from each action result for:
  - Exit-condition events (enemies defeated, objectives met, escape achieved)
  - `scene_transition_requested` — actor used a portal to a different map (see §5.7)
  - `door_forced` / `door_opened` — narrative hooks or trap triggers
  - `portal_transition_completed` — update `UIFlowSnapshot` with new actor position

**Portal transition handling:**
When `scene_transition_requested` is detected in `emitted_events`:
1. The orchestrator records the destination map SID and node SID from the event payload.
2. `FlowEngine` transitions to `scene_transition` phase.
3. `BattleMapOrchestrator` (or `TravelOrchestrator` for overworld links) is instantiated for the destination map.
4. Scene setup proceeds for the destination; the actor's presence on the destination map was already written by the RE pipeline.

**Exit:**
- Exit conditions evaluated against RE-emitted events (all enemies dead, escape achieved, etc.)
- On exit: `PUT /world/clock` (`round: 0, initiative_step: 0`), transition to `scene_transition`
- Compute XP awards, loot by reading entity blocks post-combat

### 8.2 TravelOrchestrator

**Setup:**
- Create route progress tracker (counter type): `POST /world/trackers`
- Create or reference supply, fatigue, light, time trackers
- Create or bind event deck, encounter deck, weather deck: `POST /world/decks`
- Set scene mode: `PUT /scene` (`mode: theater_of_mind, presentation: travel`, resource references)

**Active — per travel segment:**
1. Draw weather: `POST /world/decks/{weather_deck_id}/draw` → RE submits card payload as system action
2. Draw event: `POST /world/decks/{event_deck_id}/draw` → RE submits card payload as system action
3. Roll encounter check: submit system action `random_encounter_check`; if triggered, escalate to BattleMapOrchestrator
4. Advance route tracker via action or system action
5. Apply tracker deltas (supplies, fatigue) via actions

**Exit:**
- Route tracker reaches destination value → `arrived`
- Forced camp condition (supplies, fatigue threshold) → `forced_camp`
- Interrupted by encounter escalation that ends badly → `interrupted`

### 8.3 SocialOrchestrator

**Setup:**
- Create or reference NPCs as RE entities with `dialogue_block`, `faction_block`, `inventory_block`
- Create or reference faction reputation trackers
- Set scene mode: `PUT /scene` (`mode: theater_of_mind`, resource references)
- Link relevant temporal map (plot thread) to scene: include `temporal_map_ids` in scene resources

**Active:**
- Dialogue, trade, and info actions submitted as RE action envelopes
- RE validates and journals each interaction
- Plot unlocks reflected as temporal map presence state changes

**Exit:**
- Scene objectives complete (temporal map node transitions to `completed`)
- Player exits (`abandon_scene` system action)
- Apply reputation deltas and plot flags via RE actions

---

## 9. Wizard System

### 9.1 Components

| Component | Role |
|---|---|
| Wizard registry | Typed wizard specs keyed by `wizard_type` |
| Step engine | Ordered steps with per-step validation, defaults, and branching |
| Wizard-to-action translator | Converts completed inputs into one or more RE action envelopes |
| Persistence | `WizardSessionState` saved with manager snapshot; resumable after reconnect |

### 9.2 Trigger Rules

| Wizard | Trigger Condition |
|---|---|
| `character_creation` | Campaign loaded and `player_actor_ids` is empty |
| `character_creation` | New character slot event emitted during a scene |
| `level_up` | Any actor's XP block reaches next-level threshold (read from RE entity) after `resolving_action` or `system_step` |

### 9.3 Wizard Lifecycle

```
GameManager detects trigger
  -> set flow_phase = wizard_active
  -> create WizardSessionState (status = active)
  -> ProjectionBuilder includes WizardPrompt in UIFlowSnapshot

Player submits step input via POST /wizard/input
  -> WizardHost validates required_fields
  -> advance step or mark complete

On wizard complete:
  -> translate inputs to RE action envelope(s) (e.g., create entity with full blocks)
  -> submit to RE action pipeline
  -> clear WizardSessionState
  -> restore prior phase or advance to scene_setup

On wizard cancel:
  -> if no recovery path (no characters): return to campaign_selection
  -> if optional wizard: restore prior phase
```

---

## 10. SystemActionScheduler

Maintains a queue of `SystemTask` entries. After every `resolving_action` and at each travel segment, checks for due tasks and fires them as RE system actions (actor = system entity SID, `source_type: system`).

**Task types and RE mapping:**

| Task type | RE call |
|---|---|
| `draw_deck` | `POST /world/decks/{deck_id}/draw` |
| `weather_tick` | `POST /world/decks/{weather_deck_id}/draw` |
| `random_encounter_check` | `POST /actions` (system action, custom action_type) |
| `end_turn_effects` | `POST /turn/end` (RE handles condition ticks, effect expiry) |

Tasks are logged in the manager journal. On replay, tasks already fired (identified by `task_id`) are skipped.

---

## 11. Public API

GameManager exposes a flow-oriented API layered above the RE. Some endpoints are pure GM concerns; others mediate calls to RE.

### 11.0 Short ID in URL Paths

All path parameters named `{sid}` are **8-character short IDs** (`[A-Za-z0-9]`). Every `POST` that creates a resource returns a `sid` in the response body — no UUID is exposed. A non-sid value in a path segment returns `400 Bad Request`.

```
POST   /v1/sessions              →  { "sid": "aB3kR7mX", "flow_phase": "bootstrap" }
GET    /v1/sessions/aB3kR7mX/flow  →  200 OK
```

### 11.1 Session and Campaign

```
POST   /v1/sessions                            Create GM session + RE session
                                               Returns: { sid, flow_phase }
POST   /v1/sessions/{sid}/campaign             Load campaign: seed RE world state, init GM runtime
GET    /v1/sessions/{sid}/campaign             Campaign metadata and plot progress
```

### 11.2 Flow and Scene

```
GET    /v1/sessions/{sid}/flow                 Returns UIFlowSnapshot (GM-built from RE + manager state)
POST   /v1/sessions/{sid}/flow/advance         Trigger a flow phase advance
GET    /v1/sessions/{sid}/scene                Proxied from RE scene + GM annotations
PUT    /v1/sessions/{sid}/scene                GM scene override (delegates to RE PUT /scene)
```

### 11.3 Player Action Path

```
POST   /v1/sessions/{sid}/actions              GM phase-gates then submits to RE action pipeline
POST   /v1/sessions/{sid}/actions/suggest      GM-augmented affordances (delegates to RE suggest + adds wizard/flow context)
```

All action requests propagate a `X-Correlation-Id` header through to RE.

### 11.4 Wizard Path

```
GET    /v1/sessions/{sid}/wizard               Current WizardPrompt (404 if none active)
POST   /v1/sessions/{sid}/wizard/input         Submit wizard step input
POST   /v1/sessions/{sid}/wizard/cancel        Cancel active wizard
```

### 11.5 Save / Load / Recovery

```
POST   /v1/sessions/{sid}/saves                Snapshot GM state + call RE POST /saves
GET    /v1/sessions/{sid}/saves                List save slots (delegates to RE GET /saves)
POST   /v1/sessions/{sid}/saves/{slot}/load    Restore GM state + call RE POST /saves/{slot}/load
```

### 11.6 Journal and Replay

```
GET    /v1/sessions/{sid}/journal              Delegated to RE GET /journal
POST   /v1/sessions/{sid}/journal/replay       Delegates to RE POST /journal/replay; rebuilds GM phase from snapshot
```

---

## 12. Persistence and Recovery

### 12.1 What GM Persists

GM persists only state that is not derivable from RE world state:

| Persisted | Why |
|---|---|
| `CampaignRuntimeState` | Flow phase, scheduled tasks, wizard state, manager flags |
| Manager phase journal | Append-only log of GM phase transitions and their triggers |
| `WizardSessionState` | Step inputs not yet translated to RE actions |

RE persists everything else (world state, journal, save slots, checkpoints).

### 12.2 Autosave Points

- On campaign load (coordinates with RE checkpoint 0)
- On scene transition
- On wizard completion
- On player-initiated save

At each autosave: GM snapshots `CampaignRuntimeState`; simultaneously calls RE `POST /saves` to snapshot world state. Both snapshots share the same slot name for coordinated recovery.

### 12.3 Recovery Protocol

1. Load GM snapshot → `CampaignRuntimeState`
2. Load RE session from matching save slot → RE world state
3. Replay GM manager journal from snapshot to rebuild current phase
4. If GM snapshot missing: replay RE journal from checkpoint 0, then derive GM phase from world + manager journal

### 12.4 Schema Migration

`CampaignRuntimeState` carries `schema_version`. `ManagerRepository` applies registered migration functions on load to bring the snapshot to the current schema.

---

## 13. Observability

### 13.1 Correlation IDs

Every UI request carries `X-Correlation-Id` (generated at gateway if absent). It flows to:
- All GM structured log entries for that request
- All RE action envelope submissions as a request header
- RE journal entries (via RE's own correlation mechanism)

### 13.2 Structured Log Events

| Event | Fields |
|---|---|
| `phase_transition` | `from, to, trigger, session_id, correlation_id` |
| `scene_setup_started` | `scene_id, mode, presentation, resource_counts, session_id` |
| `scene_setup_complete` | `scene_id, elapsed_ms, session_id` |
| `wizard_step` | `wizard_type, step, status, session_id` |
| `system_task_fired` | `task_type, task_id, session_id` |
| `action_mediated` | `action_type, actor_id, re_status, correlation_id` |

### 13.3 Metrics

| Metric | Description |
|---|---|
| `action_latency_ms` | Time from GM receipt to RE response |
| `action_rejection_rate` | Fraction of actions rejected by RE |
| `wizard_completion_rate` | Fraction of started wizards completed vs cancelled |
| `scene_transition_count` | Scene transitions per session |
| `phase_dwell_ms` | Time spent in each flow phase |

---

## 14. Testing Plan

### 14.1 Unit Tests

- Phase transition table: valid transitions accepted; invalid transitions rejected
- Scene setup payloads: correct RE resource assembly per scene mode
- Wizard step validation: required fields enforced; defaults applied
- Scheduler: tasks fire at correct clock value; already-fired tasks skipped on replay

### 14.2 Integration Tests

- GM + RE action loop: submit action → RE accepted → phase advance
- Travel scene: deck draws fire as RE system actions; tracker deltas applied; encounter escalates
- Battle scene: setup → entity placement → combat turns → exit conditions → XP
- Save mid-wizard: GM snapshot + RE save; restore both; wizard resumes at correct step
- Save mid-encounter: restore returns to correct round/initiative state
- **Door scenario:** door entity created during scene setup; actor blocked by closed door; `open_door` action accepted; actor moves through; `close_door` and `lock_door` verify state transitions; `force_door` bypasses lock
- **Portal transition:** actor uses staircase portal during theater-of-mind scene; `portal_transition_completed` and `scene_transition_requested` emitted; GM transitions to new scene; actor presence on destination map confirmed; return portal available
- **Container loot:** container entity seeded with items during setup; actor submits `open_container` then `take_from_container`; items appear in actor inventory; container reflects removal
- **Fog of war:** explored index empty at scene start; actor moves to new node; RE journals explored index mutation; replay reconstructs identical explored index; `UIFlowSnapshot` reflects visible vs fogged nodes correctly
- **Portal + save/restore:** save mid-dungeon-floor; restore; actor location on lower floor map; explored index for both floors preserved

### 14.3 Replay Tests

- GM phase recovery from manager journal after RE journal replay
- Duplicate `idempotency_key` deduplication by RE; GM does not double-apply
- Temporal map presence state correctly reconstructed from RE replay

### 14.4 UI Contract Tests

- `UIFlowSnapshot` schema stability across all phase transitions
- `available_actions` set matches current phase allow-list
- Wizard prompt/input round-trip: prompt → input → next prompt or completion
- `active_prompt` non-null exactly when `awaiting_player_action` or `wizard_active`

---

## 15. Design Constraints

- **RE is pure.** No scene logic, no world mutation outside RE action envelopes.
- **No world state duplication.** GM does not cache or shadow RE world state; it reads it on demand.
- **Deterministic transitions.** Same `CampaignRuntimeState` + same RE-emitted events → same next GM phase.
- **Resumable at any point.** Sessions survive closure mid-wizard, mid-combat, mid-travel.
- **Single active scene.** Exactly one scene or flow active at a time.
- **Replay safe.** Every GM phase decision is logged so the phase machine can be reconstructed without re-running effects.
- **Idempotency.** All RE action submissions carry a stable `idempotency_key` to survive retries.

---

## 16. Immediate First Deliverables (Phase 1)

1. `CampaignRuntimeState` schema and `ManagerRepository`
2. `FlowPhase` enum and transition table (§7)
3. `GET /flow` (UIFlowSnapshot) and `POST /flow/advance`
4. Action mediation path with `X-Correlation-Id` threading to RE
5. `TravelOrchestrator` as the first working vertical slice — deck draws, tracker updates, encounter escalation

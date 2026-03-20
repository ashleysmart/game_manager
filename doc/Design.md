# GameManager — Design

## Meta

- Doc Type: design/game-manager
- Version: 0.1.0
- Status: draft
- Depends On:
  - GameManager Specification
  - GameManager Architecture
  - LLM Game Engine Architecture Specification (read-only reference)
  - LLM Game Engine API Design (read-only reference)

---

## 1. Purpose

This document captures design decisions for the GameManager service: how its
public API is structured, how it calls the RuleEngine (RE), and how specific
gameplay features (doors, portals, containers, fog of war) are handled at the
GM layer.

---

## 2. Public API Design

GameManager exposes a flow-oriented REST API layered above the RE.  Every
endpoint that reaches RE forwards the `X-Correlation-Id` request header.

### 2.1 URL Structure

All endpoints are scoped to a session using an **8-character short ID** (`[A-Za-z0-9]`):

```
/v1/sessions/{sid}/...
```

**Short ID rules:**

| Property | Detail |
|---|---|
| Format | 8 characters from `[A-Za-z0-9]` (62⁸ ≈ 218 trillion combinations) |
| Generation | CSPRNG at resource creation; base-62 encoded |
| Scope | Unique per resource type (sessions, entities, maps, groups, trackers, decks each namespaced separately) |
| Immutability | Never changes after creation |
| Coexistence | Every resource also carries a full UUID; both are returned on creation |

**Example session creation:**

```
POST /v1/sessions
→ 201 Created
{
  "uuid":       "550e8400-e29b-41d4-a716-446655440000",
  "sid":        "aB3kR7mX",
  "flow_phase": "bootstrap"
}
```

Subsequent calls use the short ID:
```
GET /v1/sessions/aB3kR7mX/flow
POST /v1/sessions/aB3kR7mX/actions
```

UUID lookup is available via query parameter for tooling and migration:
```
GET /v1/sessions?uuid=550e8400-e29b-41d4-a716-446655440000
→ 301 Moved Permanently  Location: /v1/sessions/aB3kR7mX
```

The GM and RE share the same `sid` for the session — `POST /v1/sessions` creates both and the same `sid` is used when the GM calls RE endpoints for that session.

### 2.2 Endpoint Inventory

#### Session and Campaign

```
POST   /v1/sessions
         Creates GM runtime + RE session.
         Returns: { uuid, sid, flow_phase: "bootstrap" }

POST   /v1/sessions/{sid}/campaign
         Loads campaign data into GM + RE.  Seeds RE world state.
         Returns: UIFlowSnapshot (phase: campaign_selection or wizard_active)

GET    /v1/sessions/{sid}/campaign
         Campaign metadata and current plot progress (reads RE temporal maps).
```

#### Flow and Scene

```
GET    /v1/sessions/{sid}/flow
         Returns UIFlowSnapshot (assembled from RE world state + CampaignRuntimeState).

POST   /v1/sessions/{sid}/flow/advance
         Explicit phase advance trigger (used by UI for confirmed transitions).

GET    /v1/sessions/{sid}/scene
         RE scene resource proxied with GM annotations
         (mode, presentation, participants, current actor, round).

PUT    /v1/sessions/{sid}/scene
         GM scene override; delegates to RE PUT /scene.
```

#### Player Action Path

```
POST   /v1/sessions/{sid}/actions
         Phase-gates; submits action envelope to RE.
         Body: action envelope (uuid, actor_id, action_type, source_type, ...)
         Returns: { re_result, flow_snapshot }

POST   /v1/sessions/{sid}/actions/suggest
         Returns augmented affordances:
           1. Delegates to RE POST /v1/sessions/{sid}/actions/suggest
           2. Adds wizard-aware suggestions if wizard_active
           3. Filters by current flow_phase allow-list
         Returns: { action_type, suggestions: { ... } }
```

#### Wizard Path

```
GET    /v1/sessions/{sid}/wizard
         Returns WizardPrompt (404 if none active).

POST   /v1/sessions/{sid}/wizard/input
         Submits step input.  On completion, translates to RE action envelopes.
         Returns: WizardPrompt (next step) or UIFlowSnapshot (wizard complete).

POST   /v1/sessions/{sid}/wizard/cancel
         Cancels active wizard.  Returns: UIFlowSnapshot.
```

#### Save / Load / Recovery

```
POST   /v1/sessions/{sid}/saves
         Snapshots GM CampaignRuntimeState + calls RE POST /saves.
         Body: { slot_name }

GET    /v1/sessions/{sid}/saves
         Delegates to RE GET /saves.

POST   /v1/sessions/{sid}/saves/{slot}/load
         Restores GM CampaignRuntimeState + calls RE POST /saves/{slot}/load.
         Returns: UIFlowSnapshot.
```

#### Journal and Replay

```
GET    /v1/sessions/{sid}/journal
         Delegated to RE GET /journal.

POST   /v1/sessions/{sid}/journal/replay
         Delegates to RE POST /journal/replay.  On completion, rebuilds GM
         phase from manager snapshot.
```

---

## 3. UIFlowSnapshot Design

`UIFlowSnapshot` is the primary view model delivered to the UI after every
state-changing operation.  It is assembled by `ProjectionBuilder` from RE world
state plus `CampaignRuntimeState`.

```json
{
  "status":  "awaiting_player_action",
  "scene_summary": {
    "mode":            "battle_map",
    "presentation":    "visual",
    "spatial_map_sid": "mP9xW2qL",
    "active_actor_sid":"eK4nT8vA",
    "round":           3,
    "initiative_step": 2
  },
  "visibility": {
    "faction_sid":    "fC2rY5jN",
    "explored_nodes": ["nA1bZ3cD", "nB2cA4eF"],
    "visible_nodes":  ["nB2cA4eF"]
  },
  "active_prompt":    "Your turn.  You are in the guard room.",
  "available_actions": [
    { "action_type": "move",       "suggestions": { "location":  ["nC3dB5fG"] } },
    { "action_type": "open_door",  "suggestions": { "target_sid": ["dR7sQ1wE"] } },
    { "action_type": "attack",     "suggestions": { "target_sid": ["mX6tP2yU"] } },
    { "action_type": "use_portal", "suggestions": { "target_sid": ["pL4uN9kV"] } }
  ],
  "wizard":           null,
  "recent_journal_events": [...],
  "errors":           []
}
```

### 3.1 Visibility Block

The `visibility` block in `UIFlowSnapshot` is derived by `ProjectionBuilder`:

1. Read `GET /world/maps/{map_id}/explored/{faction_id}` → `explored_nodes`.
2. Request LoS from RE affordance layer (or compute from positions + terrain
   geometry for simple cases) → `visible_nodes` = explored ∩ current LoS.
3. Nodes not in `explored_nodes` are unrevealed (dark).
4. Nodes in `explored_nodes` but not `visible_nodes` are fogged (remembered).
5. Nodes in `visible_nodes` are fully visible.

This block is rebuilt on every `UIFlowSnapshot` call.  No cached fog state
exists in the GM layer.

### 3.2 Available Actions with Door and Portal Affordances

`available_actions` is populated by augmenting RE affordances:

- RE `suggest` returns legal verbs and candidates (doors, portals, containers
  are surfaced as `target_id` suggestions when in range and accessible).
- GM wraps the RE suggestions with GM-layer context (current phase allow-list,
  wizard actions if applicable).
- Door verbs (`open_door`, `close_door`, `lock_door`, `unlock_door`,
  `force_door`) appear only when the actor is adjacent to a door entity.
- Portal verb (`use_portal`) appears only when the actor is at a portal node
  and the portal is `active`.
- Container verbs (`open_container`, `put_in_container`, `take_from_container`)
  appear only when a container is in reach and the relevant pre-conditions are met.

---

## 4. Scene Assembly Patterns

### 4.1 BattleMap Scene with Doors and Portals

During `scene_setup` phase the `BattleMapOrchestrator` issues the following
RE calls in order:

```
1. POST /world/maps
   → spatial map (tile or graph level)

2. POST /world/entities  (×n)
   → player characters, enemies, NPC props

3. POST /world/entities  (×d — doors)
   Body: { entity_type: "prop", blocks: { identity_block: {...},
           door_block: { state: "closed", lock_code: "..." } } }

4. POST /world/entities  (×k — keys)
   Body: { entity_type: "item", blocks: { identity_block: {...},
           key_block: { lock_code: "..." } } }
   → Key placed in NPC inventory block or container via subsequent action

5. POST /world/entities  (×p — portals)
   Body: { entity_type: "prop", blocks: { identity_block: {...},
           portal_block: { state: "active",
                           destination_map_sid: "mQ5tR3nX",
                           destination_node_sid: "nD8kW2pY",
                           return_portal_sid: "pA1bC4dE" } } }
   → Both sides of bidirectional portal created together; sids known at creation time

6. POST /world/entities  (×c — containers)
   Body: { entity_type: "item", blocks: { identity_block: {...},
           container_block: { state: "closed", key_sid: null, items: [] } } }

7. PUT /world/maps/{map_sid}/presence/{entity_sid}  (×all)
   → Place every entity at its starting position

8. POST /world/actions  (system, ×loot)
   action_type: "put_in_container"
   → Seed loot into containers via RE pipeline (journaled)

9. PUT /world/maps/{map_sid}/explored/{faction_sid}
   → Pre-reveal scripted areas (optional; only for cutscene or known-location entry)

10. POST /world/groups  (×sides)
    → Encounter groups with order_value per member

11. PUT /world/clock  { round: 1, initiative_step: 1 }

12. PUT /scene  { mode: "battle_map", ... resource references ... }
```

All setup calls carry the session's `X-Correlation-Id` and are tagged with
`source_type: system` where an action pipeline submission is required.

### 4.2 Container Loot Seeding

Two patterns are supported:

**Pattern A — System action:** Submit `put_in_container` action envelopes
for each item during scene setup (step 8 above).

**Pattern B — Deck draw:** Create a deck with cards carrying `container_spawn`
payloads.  Draw the deck during setup or during the scene.  RE handles the
payload as a system action.  Preferred for dynamic / procgen loot.

### 4.3 Travel Scene with Portal Handoff

`TravelOrchestrator` models overworld travel.  When the party approaches a
dungeon entrance portal:

1. RE entity for the dungeon entrance portal was created during map setup
   (or loaded from campaign data).
2. Actor submits `use_portal` action.
3. RE emits `portal_transition_completed` + `scene_transition_requested`.
4. `TravelOrchestrator.onEvents` detects `scene_transition_requested`.
5. `FlowEngine` → `scene_transition`.
6. `BattleMapOrchestrator` is instantiated for the destination dungeon map.
7. Scene setup runs for dungeon floor (step 4.1 above).
8. `FlowEngine` → `awaiting_player_action`.

Return journey (dungeon exit portal → overworld):
- Same flow in reverse: `BattleMapOrchestrator` hands off to `TravelOrchestrator`.
- Travel scene is restored from the existing trackers (route progress, supplies,
  etc.) already present in RE world state — no re-setup required for those.

---

## 5. Door Interaction Design

### 5.1 Affordance Flow for Doors

```
Actor approaches node adjacent to door edge
        │
ProjectionBuilder reads RE affordances for actor
        │
RE suggest returns: { action_type: ["move", "open_door", ...],
                      target_sid: ["dR7sQ1wE"] }
        │
UIFlowSnapshot.available_actions includes open_door suggestion
        │
Player selects open_door
        │
GM POST /actions: { action_type: "open_door", target_sid: "dR7sQ1wE" }
        │
RE validates (reachable? state == closed?)
  → accepted: door_block.state → open; emits door_opened
  → rejected: returns reasons (e.g. "door is locked")
        │
UIFlowSnapshot rebuilt (door state now open; move action now available through edge)
```

### 5.2 Locked Door — Key Required

When `open_door` is rejected with reason `door_locked`:
- GM checks actor's `inventory_block` (via `GET /world/entities/{actor_id}`)
  for a `key_block.lock_code` matching the door's `lock_code`.
- If found: surface `unlock_door` in `available_actions`.
- If not found: surface `force_door` (if allowed by ruleset) or narrative
  prompt indicating the door is locked.

### 5.3 Smashed Door

`force_door` transitions the door to `smashed`.  `smashed` is passable and
terminal — the edge becomes permanently open until a `repair` action (if
defined by the ruleset) restores it.  GM does not need to track this; it reads
`door_block.state` from the entity on demand.

---

## 6. Portal Interaction Design

### 6.1 Portal Affordance

`use_portal` appears in `available_actions` when:
- Actor is at the node where the portal entity has presence.
- RE affordance confirms portal is `active`.
- Encounter rules permit transit (RE validates; GM does not duplicate this check).

### 6.2 Scene Transition Sequence

```
POST /actions { action_type: "use_portal", target_sid: "pL4uN9kV" }
        │
RE result: accepted_and_committed
  emitted_events:
    - portal_transition_completed { actor_id, from_map_uuid, from_node_uuid,
                                    to_map_uuid, to_node_uuid }
    - scene_transition_requested  { from_map_uuid, to_map_uuid,
                                    destination_node_uuid }
        │
SceneOrchestrator.onEvents:
  detect scene_transition_requested
        │
FlowEngine → scene_transition
  CampaignRuntimeState.active_scene_id updated
        │
Destination scene setup (or restore from existing RE resources if revisiting)
        │
FlowEngine → awaiting_player_action
  UIFlowSnapshot: new map, actor at destination node, explored index for new map
```

### 6.3 Revisiting a Known Map

If the destination map was previously visited:
- RE entities and map structure already exist.
- Explored index already has prior exploration data (not reset unless scripted).
- Scene setup skips entity creation; only updates `PUT /scene` and clock if
  needed.

---

## 7. Container Interaction Design

### 7.1 Container State Machine (GM Perspective)

GM does not maintain container state.  It reads `container_block.state` from
the entity block when building affordances.  The RE pipeline enforces all
transitions; GM submits action envelopes only.

```
closed ──open_container──► open ──close_container──► closed
locked ──unlock_door(*) ──► closed
  (*) uses key_block/lock_code matching; see §5.2 for key discovery
```

### 7.2 Inventory and Container Affordance Interaction

`take_from_container` appears in `available_actions` when:
- Container is in reach and `container_block.state == open`.
- RE affordance returns item UUIDs inside the container as `instrument` candidates.

`put_in_container` appears when:
- Actor holds items (`inventory_block.items` non-empty).
- Container is in reach and `open`.
- RE affordance returns container UUID as `container_id` candidate.

### 7.3 Nested Container Handling

GM does not need special logic for nested containers.  RE validates cycle
detection and depth limits.  If `put_in_container` would create a cycle, RE
returns `rejected_invalid` with reason `containment_cycle_detected`.  GM
surfaces the reason to the UI.

---

## 8. Fog of War Design

### 8.1 Explored Index Reads

`ProjectionBuilder` calls `GET /world/maps/{map_sid}/explored/{faction_sid}` on
every `UIFlowSnapshot` build.  The response is used directly in the
`visibility.explored_nodes` field — no caching, no transformation.

### 8.2 Scripted Revelation

`BattleMapOrchestrator.setup` may call:

```
PUT /world/maps/{map_sid}/explored/{faction_sid}
Body: { "nodes": ["nA1bZ3cD", "nB2cA4eF"] }
```

This is a scene-configuration write, treated identically to entity placement.
It is idempotent and runs during `scene_setup` phase before the scene is active.

To reset fog (e.g. after a warp or curse effect), submit a system action
envelope with `action_type: clear_explored` (if defined by the ruleset) or
call:

```
DELETE /world/maps/{map_sid}/explored/{faction_sid}
```

during scene setup only.  Runtime fog resets during active play should always
go through the RE action pipeline.

### 8.3 Multi-Faction Visibility

Each faction has its own explored index.  `ProjectionBuilder` reads the
player faction's index.  For GM-controlled factions (enemy patrols that have
explored areas), the orchestrator reads their faction index if the ruleset
uses shared-awareness mechanics.

---

## 9. RE Endpoint Reference (GM Usage)

Quick reference for all RE endpoints called by GM.  Full specification is in
the RE API Design document.

All RE endpoint paths use `{sid}` short IDs.  The session `{sid}` is shared —
the same short ID issued by `POST /v1/sessions` is used when the GM calls RE.
Sub-resource path params (`{entity_sid}`, `{map_sid}`, `{deck_sid}`,
`{faction_sid}`) are the short IDs returned when those RE resources were created.

### 9.1 Frequently Called

| RE endpoint | When called by GM |
|---|---|
| `POST /v1/sessions/{sid}/actions` | Every player and system action |
| `POST /v1/sessions/{sid}/actions/suggest` | Every affordance request |
| `GET  /v1/sessions/{sid}/world/entities/{entity_sid}` | Read entity block for projection |
| `GET  /v1/sessions/{sid}/world/clock` | Turn tracking, encounter phase |
| `PUT  /v1/sessions/{sid}/world/clock` | Encounter start/end, time advance |
| `GET  /v1/sessions/{sid}/world/maps/{map_sid}/presence/{entity_sid}` | Actor position reads |
| `PUT  /v1/sessions/{sid}/world/maps/{map_sid}/presence/{entity_sid}` | Scene setup only |
| `GET  /v1/sessions/{sid}/world/maps/{map_sid}/explored/{faction_sid}` | Fog of war projection |
| `PUT  /v1/sessions/{sid}/world/maps/{map_sid}/explored/{faction_sid}` | Scripted revelation (setup only) |
| `POST /v1/sessions/{sid}/turn/end` | End-of-turn processing |
| `POST /v1/sessions/{sid}/world/decks/{deck_sid}/draw` | Deck-driven events |

### 9.2 Scene Setup Only

| RE endpoint | Purpose |
|---|---|
| `POST /v1/sessions/{sid}/world/entities` | Spawn all entity types incl. doors, portals, containers |
| `POST /v1/sessions/{sid}/world/maps` | Create spatial or temporal map |
| `POST /v1/sessions/{sid}/world/groups` | Create encounter / command groups |
| `POST /v1/sessions/{sid}/world/trackers` | Create route, supply, etc. trackers |
| `POST /v1/sessions/{sid}/world/decks` | Create event / encounter / quest decks |
| `PUT  /v1/sessions/{sid}/scene` | Set active scene mode and resource references |

---

## 10. Open Questions

- Whether `visibility.visible_nodes` should be computed by GM (using RE geometry
  + explored index) or requested from a dedicated RE LoS endpoint — prefer RE
  endpoint if one is added; compute client-side otherwise.
- Rate of `UIFlowSnapshot` rebuilds on long encounter turns with many system
  events — consider batching or SSE push rather than polling.
- How `transfer_item` (container → container) is surfaced in affordances — RE
  supports it; GM needs to decide whether to expose it as a single action or
  as a `take_from_container` + `put_in_container` two-step in the wizard layer.

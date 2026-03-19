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

All endpoints are scoped to a session:

```
/v1/sessions/{session_id}/...
```

Session IDs are created by `POST /v1/sessions` and shared between GM and RE —
the same UUID is used for both.

### 2.2 Endpoint Inventory

#### Session and Campaign

```
POST   /v1/sessions
         Creates GM runtime + RE session.
         Returns: { session_id, flow_phase: "bootstrap" }

POST   /v1/sessions/{id}/campaign
         Loads campaign data into GM + RE.  Seeds RE world state.
         Returns: UIFlowSnapshot (phase: campaign_selection or wizard_active)

GET    /v1/sessions/{id}/campaign
         Campaign metadata and current plot progress (reads RE temporal maps).
```

#### Flow and Scene

```
GET    /v1/sessions/{id}/flow
         Returns UIFlowSnapshot (assembled from RE world state + CampaignRuntimeState).

POST   /v1/sessions/{id}/flow/advance
         Explicit phase advance trigger (used by UI for confirmed transitions).

GET    /v1/sessions/{id}/scene
         RE scene resource proxied with GM annotations
         (mode, presentation, participants, current actor, round).

PUT    /v1/sessions/{id}/scene
         GM scene override; delegates to RE PUT /scene.
```

#### Player Action Path

```
POST   /v1/sessions/{id}/actions
         Phase-gates; submits action envelope to RE.
         Body: action envelope (uuid, actor_id, action_type, source_type, ...)
         Returns: { re_result, flow_snapshot }

POST   /v1/sessions/{id}/actions/suggest
         Returns augmented affordances:
           1. Delegates to RE POST /v1/sessions/{id}/actions/suggest
           2. Adds wizard-aware suggestions if wizard_active
           3. Filters by current flow_phase allow-list
         Returns: { action_type, suggestions: { ... } }
```

#### Wizard Path

```
GET    /v1/sessions/{id}/wizard
         Returns WizardPrompt (404 if none active).

POST   /v1/sessions/{id}/wizard/input
         Submits step input.  On completion, translates to RE action envelopes.
         Returns: WizardPrompt (next step) or UIFlowSnapshot (wizard complete).

POST   /v1/sessions/{id}/wizard/cancel
         Cancels active wizard.  Returns: UIFlowSnapshot.
```

#### Save / Load / Recovery

```
POST   /v1/sessions/{id}/saves
         Snapshots GM CampaignRuntimeState + calls RE POST /saves.
         Body: { slot_name }

GET    /v1/sessions/{id}/saves
         Delegates to RE GET /saves.

POST   /v1/sessions/{id}/saves/{slot}/load
         Restores GM CampaignRuntimeState + calls RE POST /saves/{slot}/load.
         Returns: UIFlowSnapshot.
```

#### Journal and Replay

```
GET    /v1/sessions/{id}/journal
         Delegated to RE GET /journal.

POST   /v1/sessions/{id}/journal/replay
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
    "mode":           "battle_map",
    "presentation":   "visual",
    "spatial_map_id": "<map_uuid>",
    "active_actor":   "<entity_uuid>",
    "round":          3,
    "initiative_step": 2
  },
  "visibility": {
    "faction_id":     "<faction_uuid>",
    "explored_nodes": ["<node_uuid_1>", "<node_uuid_2>"],
    "visible_nodes":  ["<node_uuid_2>"]
  },
  "active_prompt":    "Your turn.  You are in the guard room.",
  "available_actions": [
    { "action_type": "move",       "suggestions": { "location": ["<node_uuid_3>"] } },
    { "action_type": "open_door",  "suggestions": { "target_id": ["<door_entity_uuid>"] } },
    { "action_type": "attack",     "suggestions": { "target_id": ["<monster_uuid>"] } },
    { "action_type": "use_portal", "suggestions": { "target_id": ["<portal_entity_uuid>"] } }
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
                           destination_map_uuid: "<uuid>",
                           destination_node_uuid: "<uuid>",
                           return_portal_uuid: "<companion_portal_uuid>" } } }
   → Both sides of bidirectional portal created together

6. POST /world/entities  (×c — containers)
   Body: { entity_type: "item", blocks: { identity_block: {...},
           container_block: { state: "closed", key_id: null, items: [] } } }

7. PUT /world/maps/{map_id}/presence/{entity_id}  (×all)
   → Place every entity at its starting position

8. POST /world/actions  (system, ×loot)
   action_type: "put_in_container"
   → Seed loot into containers via RE pipeline (journaled)

9. PUT /world/maps/{map_id}/explored/{faction_id}
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
                      target_id: ["<door_entity_uuid>"] }
        │
UIFlowSnapshot.available_actions includes open_door suggestion
        │
Player selects open_door
        │
GM POST /actions: { action_type: "open_door", target_id: "<door_entity_uuid>" }
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
POST /actions { action_type: "use_portal", target_id: "<portal_uuid>" }
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

`ProjectionBuilder` calls `GET /world/maps/{map_id}/explored/{faction_id}` on
every `UIFlowSnapshot` build.  The response is used directly in the
`visibility.explored_nodes` field — no caching, no transformation.

### 8.2 Scripted Revelation

`BattleMapOrchestrator.setup` may call:

```
PUT /world/maps/{map_id}/explored/{faction_id}
Body: { "nodes": ["<node_uuid_1>", "<node_uuid_2>"] }
```

This is a scene-configuration write, treated identically to entity placement.
It is idempotent and runs during `scene_setup` phase before the scene is active.

To reset fog (e.g. after a warp or curse effect), submit a system action
envelope with `action_type: clear_explored` (if defined by the ruleset) or
call:

```
DELETE /world/maps/{map_id}/explored/{faction_id}
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

### 9.1 Frequently Called

| RE endpoint | When called by GM |
|---|---|
| `POST /v1/sessions/{id}/actions` | Every player and system action |
| `POST /v1/sessions/{id}/actions/suggest` | Every affordance request |
| `GET  /v1/sessions/{id}/world/entities/{id}` | Read entity block for projection |
| `GET  /v1/sessions/{id}/world/clock` | Turn tracking, encounter phase |
| `PUT  /v1/sessions/{id}/world/clock` | Encounter start/end, time advance |
| `GET  /v1/sessions/{id}/world/maps/{id}/presence/{entity_id}` | Actor position reads |
| `PUT  /v1/sessions/{id}/world/maps/{id}/presence/{entity_id}` | Scene setup only |
| `GET  /v1/sessions/{id}/world/maps/{id}/explored/{faction_id}` | Fog of war projection |
| `PUT  /v1/sessions/{id}/world/maps/{id}/explored/{faction_id}` | Scripted revelation (setup only) |
| `POST /v1/sessions/{id}/turn/end` | End-of-turn processing |
| `POST /v1/sessions/{id}/world/decks/{id}/draw` | Deck-driven events |

### 9.2 Scene Setup Only

| RE endpoint | Purpose |
|---|---|
| `POST /v1/sessions/{id}/world/entities` | Spawn all entity types incl. doors, portals, containers |
| `POST /v1/sessions/{id}/world/maps` | Create spatial or temporal map |
| `POST /v1/sessions/{id}/world/groups` | Create encounter / command groups |
| `POST /v1/sessions/{id}/world/trackers` | Create route, supply, etc. trackers |
| `POST /v1/sessions/{id}/world/decks` | Create event / encounter / quest decks |
| `PUT  /v1/sessions/{id}/scene` | Set active scene mode and resource references |

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

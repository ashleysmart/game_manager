"""
tests/helpers.py — shared utilities for GameManager smoke tests
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import requests

GM_BASE: str = os.environ.get("GM_BASE", "http://localhost:5001")


# ── HTTP client ────────────────────────────────────────────────────────────────

class GMClient:
    """Thin wrapper around requests that injects correlation headers."""

    def __init__(self, base_url: str = GM_BASE) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    # ── low-level ──────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Correlation-Id": str(uuid.uuid4()),
        }

    def post(self, path: str, body: dict | None = None) -> requests.Response:
        return self._session.post(
            f"{self.base_url}{path}",
            json=body or {},
            headers=self._headers(),
            timeout=30,
        )

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        return self._session.get(
            f"{self.base_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=30,
        )

    def put(self, path: str, body: dict | None = None) -> requests.Response:
        return self._session.put(
            f"{self.base_url}{path}",
            json=body or {},
            headers=self._headers(),
            timeout=30,
        )

    # ── pre-flight ─────────────────────────────────────────────────────────────

    def check_reachable(self) -> None:
        """Raise RuntimeError if the GameManager is not reachable."""
        try:
            self._session.get(f"{self.base_url}/v1/sessions", timeout=3)
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(
                f"GameManager not reachable at {self.base_url}\n"
                "Start the server or set GM_BASE=http://host:port"
            ) from exc


# ── flow helpers ───────────────────────────────────────────────────────────────

def get_flow(client: GMClient, session_sid: str) -> dict[str, Any]:
    """Return the full UIFlowSnapshot for *session_sid*."""
    resp = client.get(f"/v1/sessions/{session_sid}/flow")
    _assert_2xx(resp, f"GET /v1/sessions/{session_sid}/flow")
    return resp.json()


def get_flow_phase(client: GMClient, session_sid: str) -> str:
    return get_flow(client, session_sid)["status"]


def wait_for_phase(
    client: GMClient,
    session_sid: str,
    expected_phase: str,
    max_polls: int = 20,
    poll_interval: float = 0.5,
) -> None:
    """Poll until the session reaches *expected_phase* or raise TimeoutError."""
    for _ in range(max_polls):
        phase = get_flow_phase(client, session_sid)
        if phase == expected_phase:
            return
        time.sleep(poll_interval)
    actual = get_flow_phase(client, session_sid)
    raise TimeoutError(
        f"Timed out waiting for phase={expected_phase!r}; last seen: {actual!r}"
    )


# ── wizard helpers ─────────────────────────────────────────────────────────────

def complete_wizard(
    client: GMClient,
    session_sid: str,
    all_inputs: dict[str, Any],
    max_steps: int = 20,
) -> None:
    """
    Submit wizard step inputs until the wizard is no longer active.
    *all_inputs* is the full set of fields; only required_fields for each step
    are sent.  Falls back to sending all_inputs if required_fields is empty.
    """
    for _ in range(max_steps):
        phase = get_flow_phase(client, session_sid)
        if phase != "wizard_active":
            return

        resp = client.get(f"/v1/sessions/{session_sid}/wizard")
        _assert_2xx(resp, "GET /v1/sessions/{sid}/wizard")
        wizard = resp.json()

        required: list[str] = wizard.get("required_fields") or []
        step_input = (
            {k: v for k, v in all_inputs.items() if k in required}
            if required
            else all_inputs
        ) or all_inputs

        print(f"  wizard step={wizard.get('step')}: submitting {list(step_input)}")

        resp = client.post(
            f"/v1/sessions/{session_sid}/wizard/input", step_input
        )
        _assert_2xx(resp, "POST /v1/sessions/{sid}/wizard/input")

        if resp.json().get("status") == "complete":
            return

    raise RuntimeError(f"Wizard did not complete within {max_steps} steps")


# ── combat loop ────────────────────────────────────────────────────────────────

def run_combat_to_completion(
    client: GMClient,
    session_sid: str,
    max_rounds: int = 30,
) -> str:
    """
    Drive a battle to completion.  Player attacks the first available target
    each round; waits for system_step (monster turns) to resolve.
    Returns the final phase name.
    Raises RuntimeError if combat does not resolve within *max_rounds*.
    """
    for round_num in range(max_rounds):
        phase = get_flow_phase(client, session_sid)

        if phase == "awaiting_player_action":
            snapshot = get_flow(client, session_sid)
            active_actor_sid: str = snapshot["scene_summary"]["active_actor_sid"]

            # Find first attack target from affordances
            target_sid: str | None = None
            for action in snapshot.get("available_actions", []):
                if action["action_type"] == "attack":
                    targets = action.get("suggestions", {}).get("target_sid", [])
                    if targets:
                        target_sid = targets[0]
                        break

            if target_sid is None:
                print(f"  Round {round_num}: no attack targets — combat over")
                return phase

            resp = client.post(
                f"/v1/sessions/{session_sid}/actions",
                {
                    "uuid": str(uuid.uuid4()),
                    "actor_sid": active_actor_sid,
                    "action_type": "attack",
                    "source_type": "player",
                    "target_sid": target_sid,
                },
            )
            _assert_2xx(resp, "POST /v1/sessions/{sid}/actions (attack)")

            re_status = resp.json().get("re_result", {}).get("status", "unknown")
            print(
                f"  Round {round_num}: player ({active_actor_sid})"
                f" attacks {target_sid} → {re_status}"
            )

        elif phase in ("resolving_action", "system_step"):
            # Engine is processing — yield and poll
            time.sleep(0.3)

        elif phase in ("scene_transition", "campaign_end"):
            print(f"  Combat resolved after {round_num} player rounds (phase: {phase})")
            return phase

        else:
            raise RuntimeError(f"Unexpected phase during combat: {phase!r}")

    raise RuntimeError(f"Combat did not end within {max_rounds} player rounds")


# ── assertion helpers ──────────────────────────────────────────────────────────

def _assert_2xx(resp: requests.Response, label: str) -> None:
    """
    Raise AssertionError with full response detail if *resp* is not 2xx.
    Includes URL, status code, and decoded body to make failures self-describing.
    """
    if not (200 <= resp.status_code < 300):
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise AssertionError(
            f"{label}\n"
            f"  URL:    {resp.request.method} {resp.url}\n"
            f"  Status: {resp.status_code}\n"
            f"  Body:   {body}"
        )

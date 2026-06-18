# AGENTS.md

> Instructions for AI coding agents (Hermes, Claude Code, Codex, Copilot, Cursor, etc.) contributing to this project.

This file is read by AI agents to understand project conventions and avoid common pitfalls. It complements the README (which is for humans) — read both before making changes.

## Project Summary

A Python bridge that connects Intelbras alarm panels to Home Assistant via MQTT. It speaks the **Intelbras Cloud Relay V1 protocol** (server `amt.intelbras.com.br:9015`) because these panels don't accept direct TCP connections from outside.

**Hardware compatibility**: see README § "Hardware Compatibility". Currently confirmed working on ANM 24 NET (firmware 6.6, 24 zones, 2 partitions).

## Repository Structure

```
app.py                       # Main bridge (MQTT <-> alarm)
lib/isecnet.py               # Cloud Relay V1 protocol implementation
config.example.yml           # Config template (credentials are placeholders)
tests/                       # Unit + integration tests
  test_isecnet_parser.py     # Protocol parser tests
  test_app.py                # Config + status serialization tests
  test_mqtt_integration.py   # MQTT dispatch + publish tests
.github/workflows/           # CI (lint, test, build)
Dockerfile                   # python:3.11-slim
docker-compose.yml           # Local dev / production
requirements.txt             # Production deps: paho-mqtt, pyyaml
requirements-dev.txt         # Dev deps: -r requirements.txt, coverage, ruff
pyproject.toml               # Ruff configuration
.coveragerc                  # Coverage configuration
```

## Code Conventions

### Python

- Target Python 3.11 (CI uses this). For typing syntax like `list[int] | None`, we add `from __future__ import annotations` so the file also imports cleanly on 3.9.
- Style: ruff-clean (run `ruff check app.py lib/ tests/`).
- Tests: stdlib `unittest` (no pytest). Run with `python3 -m unittest discover tests`.
- Coverage: measured via `coverage.py`. Current ~52%. Threshold in CI is 40%.

### Protocol implementation

**Critical pitfalls** in `lib/isecnet.py`:

1. **Always use bitwise AND (`data[n] & 0x80`) for bit reading, NEVER `_int_to_bits`** (MSB-first vs LSB-first confusion caused partition state misreading).

2. **The ANM 24 NET requires a partition byte** after action commands:
   - `ARM_AWAY` = `[0x41, 0x41]` (partition A, full arm)
   - `ARM_HOME` = `[0x41, 0x42]` (partition B only, stay arm)
   - `DISARM` = `[0x44, 0x41]` then `[0x44, 0x42]` (must disarm BOTH partitions)
   - A bare `[0x41]` or `[0x44]` will be rejected with `0xE7` (command queued but ignored).

3. **The PANIC command has subtypes**: `[0x45, 0]` = silent (no siren), `[0x45, 1]` = audible (the "Emergency" button in the Intelbras app), `[0x45, 2]` and `[0x45, 3]` are fire/medical on AMT 8000 only.

4. **`0xE7` is a soft success for arm/disarm** — treat it as "command queued" and verify via subsequent status poll. Don't retry.

5. **The alarm is slow** — it can take 3-10s to process commands. Verify polling at 1s, 2s, 3s, 5s intervals (max ~11s) before reporting failure.

6. **`0x4F` (siren off) is `INVALID_COMMAND` on ANM 24 NET**. To stop the siren, send DISARM instead.

7. **Threading**: `_alarm_lock` (`threading.Lock`) MUST serialize access to the alarm socket because `poll_once` runs on a 5s timer and `arm()`/`disarm()` calls happen on MQTT messages.

8. **YAML keys**: YAML parses `6:` as `int`, but the rest of the code uses `str`. `load_config()` normalizes — if you bypass it, you'll get `KeyError` for zones.

### Configuration

- `config.yml` (the real one with credentials) is **gitignored**. Never commit credentials.
- `config.example.yml` has placeholders like `REPLACE_WITH_YOUR_PANEL_MAC`.

### MQTT topics

```
intelbras_alarm/status          JSON status (retained, qos 1)
intelbras_alarm/availability    "online" / "offline" (retained)
intelbras_alarm/command         ARM_AWAY | ARM_HOME | DISARM | PANIC
intelbras_alarm/emergency       PRESS (panic button)
homeassistant/.../config        HA auto-discovery (retained)
```

Subscribe to `intelbras_alarm/#` (not `intelbras_alarm/command/#`) — the panic button is a sibling topic, not a child of `command/`.

## Adding Tests

When you fix a bug or add a feature, include a test that would fail without your change.

- Protocol/parser changes → `tests/test_isecnet_parser.py`
- Config/MQTT dispatch changes → `tests/test_app.py` or `tests/test_mqtt_integration.py`

For mocking the MQTT client, use the existing `FakeMqttClient` in `tests/test_mqtt_integration.py` as a template — it records publishes and subscribes without needing a broker.

For testing alarm calls, use a `FakeAlarm` class with the methods you need (arm, disarm, panic, etc) and check the side effects.

## Common Tasks

### Adding support for a new panel model

1. **Identify the protocol variant**:
   - V1 (Cloud Relay `amt.intelbras.com.br:9015`) — current target
   - V2 (Cloud Relay different port, `APP_CONNECT` handshake) — needs separate code path
   - Local TCP (some panels accept direct connection on port 9009) — alternative path

2. **Add the model to the parser**:
   - Different panels have different status byte layouts. Add a model-specific parser in `parse_v1_status()` or a new function.
   - Update `tests/test_isecnet_parser.py` with a sample payload + expected output.

3. **Document** the new model in README § "Hardware Compatibility":
   - "Tested ✅" — only if confirmed working with a real panel
   - "Should work (untested)" — if same protocol family

4. **Update the firmware/model name detection** if needed (currently hard-coded to `0x24` = "ANM 24 NET" in `_parse_v1_status`).

### Adding a new sensor or button

1. Add a publish call in `publish_discovery()` in `app.py` with the appropriate HA discovery topic.
2. If the data comes from the panel status, add it to the `AlarmStatus` dataclass in `lib/isecnet.py` and parse it from `parse_v1_status()`.
3. Mark it `entity_category: "diagnostic"` if it's metadata (firmware, last update) so it goes in the device's Diagnostics section, not the main view.
4. Update tests.

### Adding a new action command

1. Add the command bytes to `lib/isecnet.py` (e.g., `CMD_XYZ = [0x.., 0x..]`) with documentation.
2. Add a method on `CloudRelayClient` (`def xyz(self) -> bool:`).
3. Add an MQTT topic handler in `_on_mqtt_message()` in `app.py`.
4. Add an HA discovery entry if it's a button.
5. Add tests for: parser, MQTT dispatch, action code parsing.

## Debugging Tips

- Set `LOG_LEVEL=DEBUG` for verbose output (every poll, every MQTT message).
- The status payload is published as JSON to `intelbras_alarm/status` — subscribe to it from any MQTT client to see exactly what the bridge thinks the panel state is.
- For protocol issues, the reference implementation is at https://github.com/bobaoapae/guardian-api-intelbras (focus on `app/services/isecnet_protocol.py`).

## Things You Should NOT Do

- **Do not commit credentials** (`config.yml`, `.env`, anything with a real MAC or password).
- **Do not change the MQTT topic prefix** in a way that breaks existing HA installations — it's a breaking change.
- **Do not change the HA discovery payload** shape in a way that loses entities for existing users.
- **Do not add new dependencies** to `requirements.txt` unless absolutely necessary. Prefer stdlib where possible.
- **Do not use `_int_to_bits` for bit reading** — use bitwise AND.
- **Do not remove the threading.Lock** — without it, poll_once and verify race on the socket and crash.
- **Do not change the alarm poll interval** below 3s without thinking about the Cloud Relay's rate limits.

## PR Checklist

Before opening a PR, verify:

- [ ] `python3 -m unittest discover tests` passes (all 55 tests)
- [ ] `ruff check app.py lib/ tests/` passes
- [ ] `coverage run --source=app,lib -m unittest discover tests && coverage report` shows >=40%
- [ ] No credentials in the diff
- [ ] README updated if you changed config, topics, or supported hardware
- [ ] New code has tests
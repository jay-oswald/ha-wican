# MeatPi Integration Tests

Comprehensive test suite for the MeatPi (WiCAN) Home Assistant integration.

Key building blocks:

- `device_sim.py` — the multi-device-type simulator: `DEVICE_PRESETS`
  (legacy WiCAN, V6 WiCAN Pro/USB, ESPNetlink, generic MeatPi) plus
  `SimulatedDeviceApi`, the device-side HTTP surface with programmable
  failure modes (offline/timeout/403/500/garbage) and data-logger routes.
- `test_device_type_matrix.py` — per-device-type end-to-end scenarios
  (telemetry, control buttons, OTA lifecycle, failure injection).
- `test_history*.py` — the SD-card history backfill: unit, end-to-end,
  hardening, and real in-memory-recorder round-trips.
- `test_keep_last_values.py` — what entities show while the device is away:
  the keep-last-values option, the Connected sensor, and a device that went
  quiet across a Home Assistant restart.
- `test_device_simulation.py` / `test_device_chaos.py` /
  `test_hardening.py` — the hardened-telemetry and failure-injection
  suites.

Adding a new device type? Follow `notes/device-contract/ADDING_A_DEVICE.md` (Part B.5
lists the required test coverage).

## Prerequisites

```bash
pip install -r requirements_test.txt
```

## Running Tests

### Run all tests
```bash
pytest
```

### Run with coverage report
```bash
pytest --cov=custom_components.wican --cov-report=html
```

This generates an HTML coverage report in `htmlcov/index.html`.

### Run specific test file
```bash
pytest tests/test_coordinator.py
pytest tests/test_config_flow.py
```

### Run specific test function
```bash
pytest tests/test_init.py::test_webhook_post
pytest tests/test_coordinator.py::test_coordinator_normalize_sensor_value_voltage
```

### Run tests matching a pattern
```bash
pytest -k "webhook"
pytest -k "sensor"
```

### Verbose output
```bash
pytest -v
pytest -vv  # Extra verbose
```

### Show test output (print statements)
```bash
pytest -s
```

## Test Structure

```
tests/
├── __init__.py                 # Test package
├── conftest.py                 # Shared fixtures
├── test_init.py                # Integration setup/unload tests
├── test_config_flow.py         # Config flow tests (user + zeroconf)
├── test_coordinator.py         # Coordinator tests
├── test_sensor.py              # Sensor platform tests
├── test_binary_sensor.py       # Binary sensor platform tests
└── test_diagnostics.py         # Diagnostics tests
```

## Test Coverage

| Component | Tests | Description |
|-----------|-------|-------------|
| **Integration** | 6 tests | Setup, unload, webhook handling |
| **Config Flow** | 8 tests | User flow, zeroconf discovery |
| **Coordinator** | 12 tests | Data management, validation, normalization |
| **Sensors** | 7 tests | Static sensors, PID sensors, restoration |
| **Binary Sensors** | 5 tests | Status sensors, on/off states |
| **Diagnostics** | 5 tests | Data collection, redaction |

## Key Test Fixtures

### `mock_config_entry`
Returns a pre-configured MockConfigEntry for testing.

### `mock_webhook_data`
Returns sample webhook data with status and PID information.

### `init_integration`
Sets up a complete integration instance for testing.

### `mock_aiohttp_session`
Mocks HTTP requests to the WiCAN device.

## Example Usage

```python
async def test_my_feature(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test my new feature."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    
    client = await hass_client()
    
    # Post webhook data
    await client.post(f"/api/webhook/{webhook_id}", json=mock_webhook_data)
    await hass.async_block_till_done()
    
    # Verify behavior
    state = hass.states.get("sensor.wican_device_wifi_mode")
    assert state.state == "Station"
```

## Continuous Integration

Tests can be integrated into CI/CD pipelines:

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.12'
      - run: pip install -r requirements_test.txt
      - run: pytest --cov --cov-report=xml
      - uses: codecov/codecov-action@v3
```

## Test Categories

Tests are organized by component:
- **Unit tests**: Test individual functions and methods
- **Integration tests**: Test component interactions
- **Platform tests**: Test entity platforms (sensor, binary_sensor)
- **Flow tests**: Test config and options flows

## Debugging Tests

### Run with debugger
```bash
pytest --pdb  # Drop into debugger on failure
```

### Show local variables on failure
```bash
pytest -l
```

### Show full diff on assertion failures
```bash
pytest -vv
```

## Contributing

When adding new features:
1. Write tests for the new functionality
2. Ensure all existing tests pass
3. Aim for >90% code coverage
4. Follow existing test patterns

## Notes

- Tests use `pytest-homeassistant-custom-component` which provides HA test utilities
- Async tests are handled automatically with `pytest-asyncio`
- Fixtures are defined in `conftest.py` for reuse across test files
- Mock objects prevent actual network calls during testing

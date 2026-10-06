# Offline tests

These tests run the integration against Home Assistant without a ConnectBox.
They build gateway messages in the shape observed on six ComfoSpot 50 units
and check decoding, entity creation, writes with read-back, and diagnostics.

Run them from the repository root:

```sh
uv run --no-project --python 3.13 --with homeassistant --with pytest \
  --with tlslite-ng -- python -m pytest -q -p no:cacheprovider tests
```

| File | Covers |
| --- | --- |
| `test_auto_mode.py` | Auto (sensor mode, level 5) preset and sensor-board detection |
| `test_readings.py` | Humidity and CO2 readings, current level, level write read-back |
| `test_situations.py` | Per-situation level selects with read-back of inactive situations, temporary change, and the summer-ventilation switch from #12 together with this branch's units |
| `test_supply_only_status.py` | Supply-only operation sensor: exhaust-fan flag 38.0.5, its read sequence after the core telemetry and its rejection, the entity, and the diagnostics |
| `test_round2.py` | Situation select, boost end, summer-ventilation role, optional property sequence after the core telemetry and its rejection |
| `test_capture.py` | Temporary capture diagnostics, including the structure of unknown room fields; needs the capture extras of this fork's `main` |

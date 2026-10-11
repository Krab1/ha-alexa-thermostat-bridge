"""Stdlib-only tests: run with `python -m unittest discover tests`."""
import importlib.util
import pathlib
import sys
import types
import unittest

# Load the pure helper without executing the package __init__ (needs Home Assistant).
_dir = pathlib.Path(__file__).parent.parent / "custom_components" / "alexa_thermostat_bridge"
_pkg = types.ModuleType("atb")
_pkg.__path__ = [str(_dir)]
sys.modules["atb"] = _pkg
for _name in ("const", "range_gap"):
    _spec = importlib.util.spec_from_file_location(f"atb.{_name}", _dir / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"atb.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
enforce_gap = sys.modules["atb.range_gap"].enforce_gap


class EnforceGap(unittest.TestCase):
    def test_wide_gap_untouched(self):
        self.assertEqual(enforce_gap(70, 80, False, True, 60, 85), (70, 80))

    def test_exactly_three_allowed(self):
        self.assertEqual(enforce_gap(77, 80, False, True, 60, 85), (77, 80))

    def test_raise_low_pushes_high(self):
        self.assertEqual(enforce_gap(79, 80, False, True, 60, 85), (79, 82))

    def test_lower_high_pushes_low(self):
        self.assertEqual(enforce_gap(77, 78, True, False, 60, 85), (75, 78))

    def test_low_pushes_high_to_max_then_low_gives_way(self):
        self.assertEqual(enforce_gap(84, 85, False, True, 60, 85), (82, 85))

    def test_high_pushes_low_to_min_then_high_gives_way(self):
        self.assertEqual(enforce_gap(60, 61, True, False, 60, 85), (60, 63))

    def test_both_moved_treats_low_as_moved(self):
        self.assertEqual(enforce_gap(77, 78, True, True, 60, 85), (77, 80))


if __name__ == "__main__":
    unittest.main()

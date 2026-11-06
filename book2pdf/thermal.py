"""Read-only Linux temperature sampling with hysteresis; no device writes."""
from pathlib import Path
import sys


def supported():
    return sys.platform.startswith('linux')


def temperature():
    if not supported():
        return None
    values = []
    paths = list(Path('/sys/class/thermal').glob('thermal_zone*/temp'))
    paths.extend(Path('/sys/class/hwmon').glob('hwmon*/temp*_input'))
    for path in paths:
        try:
            value = float(path.read_text().strip()) / 1000
            if 0 < value < 150:
                values.append(value)
        except (ValueError, OSError):
            continue
    return max(values) if values else None


class ThermalGate:
    def __init__(self, options):
        self.options = options
        self.paused = False

    def allows_start(self):
        if not self.options.thermal_pause:
            return True
        value = temperature()
        if value is None:
            return True
        if value >= self.options.pause_c:
            self.paused = True
        elif value <= self.options.resume_c:
            self.paused = False
        return not self.paused

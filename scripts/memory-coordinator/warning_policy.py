"""Offline candidate policy. No I/O, work execution, or lease management."""
from collections import deque
import math


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


class WarningWindow:
    def __init__(self, config):
        self.config = config
        self.intervals = deque()
        self.last_call = self.last_sample = None
        self.ready = False
        self.reason = "warning_health_wait"
        self.summary = {}

    def reset(self, reason):
        self.intervals.clear()
        self.last_sample = None
        self.ready = False
        self.reason = reason
        self.summary = {}

    def observe(self, health, now):
        gap_limit = self.config["warning_max_probe_gap_s"]
        if not finite(now):
            self.reset("warning_clock_unknown")
            return
        if self.last_call is not None and not 0 <= now - self.last_call <= gap_limit:
            self.reset("warning_observation_gap")
        self.last_call = now
        if not self.config["warning_enabled"]:
            self.reset("warning_disabled")
            return
        if health.get("maintenance_active", False) is not False:
            self.reset("maintenance")
            return
        if type(health.get("pressure_level")) is not int or health["pressure_level"] not in (1, 2):
            self.reset("warning_pressure_unqualified")
            return
        end, duration = health.get("host_observed_mono"), health.get("host_interval_seconds")
        incoming, outgoing = health.get("swap_in_mib_s"), health.get("swap_out_mib_s")
        if (not all(finite(x) for x in (end, duration, incoming, outgoing))
                or not 0 <= now - end <= gap_limit or not 1 <= duration <= gap_limit
                or min(incoming, outgoing) < 0):
            self.reset("warning_measurement_unknown_or_stale")
            return
        if self.last_sample == end:
            return  # Cached repeated reads cannot manufacture observed time.
        if self.last_sample is not None and (
                not 0 < end - self.last_sample <= gap_limit or abs(end - self.last_sample - duration) > 0.25):
            self.reset("warning_counter_interval_gap")
        if incoming > self.config["warning_burst_read_mib_s"] or outgoing > self.config["warning_burst_write_mib_s"]:
            self.reset("warning_swap_burst")
            self.last_sample = end
            return
        if self.last_sample is None:
            # Pressure before this observation is unknown. Do not credit its
            # preceding counter interval after startup, critical, or a gap.
            self.last_sample = end
            self.ready = False
            self.reason = "warning_health_wait"
            self.summary = {"observed_seconds": 0, "mean_read_mib_s": None,
                            "mean_write_mib_s": None, "interval_count": 0}
            return
        self.last_sample = end
        self.intervals.append((end - duration, end, incoming, outgoing))
        cutoff = end - self.config["warning_seconds"]
        while self.intervals and self.intervals[0][1] <= cutoff:
            self.intervals.popleft()
        if len(self.intervals) > int(self.config["warning_seconds"]) + 2:
            self.reset("warning_sample_frequency_invalid")
            self.last_sample = end
            return
        observed = read_total = write_total = 0.0
        for start, finish, read, write in self.intervals:
            weight = finish - max(start, cutoff)
            observed += weight
            read_total += read * weight
            write_total += write * weight
        mean_read = read_total / observed
        mean_write = write_total / observed
        full = observed >= self.config["warning_seconds"] - 0.001
        quiet = mean_read <= self.config["warning_mean_read_mib_s"] and mean_write <= self.config["warning_mean_write_mib_s"]
        self.ready = full and quiet
        self.reason = "warning_ready" if self.ready else "warning_health_wait" if not full else "warning_swap_average"
        self.summary = {"observed_seconds": round(observed, 3), "mean_read_mib_s": mean_read,
                        "mean_write_mib_s": mean_write, "interval_count": len(self.intervals)}

    def view(self):
        return {"ready": self.ready, "reason": self.reason, **self.summary}

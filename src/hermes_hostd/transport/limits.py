"""Finite ingress budgets. Counters are owned by one asyncio event loop."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ReceiveLimits:
    max_request_bytes: int = 65536
    receive_timeout: float = 10
    max_receiving: int = 16

    def __post_init__(self):
        for name in ('max_request_bytes', 'max_receiving'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if (isinstance(self.receive_timeout, bool)
                or not math.isfinite(self.receive_timeout) or self.receive_timeout <= 0):
            raise ValueError('receive_timeout must be finite and positive')


class Admission:
    """Reject rather than queue. Acquire/release must not cross an await."""
    def __init__(self, maximum: int):
        if type(maximum) is not int or maximum <= 0:
            raise ValueError('admission maximum must be a positive integer')
        self.maximum = maximum
        self.used = 0

    def acquire(self) -> bool:
        if self.used >= self.maximum:
            return False
        self.used += 1
        return True

    def release(self):
        if self.used <= 0:
            raise RuntimeError('admission released without ownership')
        self.used -= 1

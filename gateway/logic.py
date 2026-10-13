"""Decision logic of the gateway, kept free of I/O so it can be unit tested."""


class Hysteresis:
    """An on/off switch with two thresholds so it doesn't flicker around one value.

    Turns ON when value >= on_at and OFF when value <= off_at.
    Between the two it keeps its previous state.
    """

    def __init__(self, on_at: float, off_at: float):
        if off_at >= on_at:
            raise ValueError("off_at must be below on_at")
        self.on_at = on_at
        self.off_at = off_at
        self.active = False

    def update(self, value: float) -> bool:
        """Feed a new measurement. Returns True if the state changed."""
        if not self.active and value >= self.on_at:
            self.active = True
            return True
        if self.active and value <= self.off_at:
            self.active = False
            return True
        return False


class FanController:
    """Fan mode: 'auto' follows temperature, 'on'/'off' are manual overrides."""

    MODES = ("auto", "on", "off")

    def __init__(self, on_at: float, off_at: float):
        self.mode = "auto"
        self.auto = Hysteresis(on_at, off_at)

    def set_mode(self, mode: str) -> None:
        mode = mode.strip().lower()
        if mode not in self.MODES:
            raise ValueError(f"unknown fan mode {mode!r}, use one of {self.MODES}")
        self.mode = mode

    def desired(self, temperature: float) -> bool:
        """What the fan should be doing right now."""
        self.auto.update(temperature)  # keep tracking even in manual mode
        if self.mode == "auto":
            return self.auto.active
        return self.mode == "on"

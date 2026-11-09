#!/usr/bin/env python3
"""
Terminal ANSI color, styling, and box-drawing utilities for Mission Control & forms.
"""

from __future__ import annotations


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
UNDERLINE = "\033[4m"

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"

BRIGHT_RED = "\033[91m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_BLUE = "\033[94m"
BRIGHT_MAGENTA = "\033[95m"
BRIGHT_CYAN = "\033[96m"
BRIGHT_WHITE = "\033[97m"

BG_BLUE = "\033[44m"
BG_GRAY = "\033[100m"


class AnsiTheme:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.bold = BOLD if enabled else ""
        self.dim = DIM if enabled else ""
        self.reset = RESET if enabled else ""
        self.red = RED if enabled else ""
        self.green = GREEN if enabled else ""
        self.yellow = YELLOW if enabled else ""
        self.blue = BLUE if enabled else ""
        self.magenta = MAGENTA if enabled else ""
        self.cyan = CYAN if enabled else ""
        self.white = WHITE if enabled else ""

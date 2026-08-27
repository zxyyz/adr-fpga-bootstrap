"""Remote agent. Shipped to the build server as a zipapp — stdlib only."""

import sys

assert sys.version_info >= (
    3,
    12,
), f"vmcp agent needs Python >= 3.12, got {sys.version}"

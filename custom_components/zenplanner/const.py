"""Constants that are not part of the ZenPlanner protocol."""

from datetime import timedelta

DOMAIN = "zenplanner"
PLATFORMS = ["calendar", "sensor"]
SCAN_INTERVAL = timedelta(minutes=15)
OPEN_CLASS_HORIZON = timedelta(days=14)
ATTRIBUTE_CAP = 10

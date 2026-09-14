# smartctl 7.3 fixtures

These compact fixtures are representative, synthetic JSON shaped from the
documented smartctl 7.3 interface. They are not captures from an actual device.

The locked reference is the official Debian bookworm smartctl 7.3 manual:
https://manpages.debian.org/bookworm/smartmontools/smartctl.8.en.html

For `--nocheck standby,3,5`, exit status 3 represents a standby skip and exit
status 5 represents an unsupported power-mode check. Status 2 remains a
command/device failure. `temperature-with-health-bit.json` represents the
documented `temperature.current` field together with health-history bit 3.

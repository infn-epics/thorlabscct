"""EPICS IOC for the Thorlabs CCT spectrometer family."""

import os as _os

# caproto queues monitor updates for a client that reads slower than the IOC publishes (the CA gateway,
# a screen over a slow link) up to 1000 per subscription and 10000 per connection by default: with
# 32 KB spectra that is far more than the pod's memory (cct01 was OOM-killed in minutes at 10 Hz).
# Keep about one second of updates; older ones are dropped and the client gets the latest. These are
# read when caproto is imported, so they are set here, before any caproto import; the environment wins.
_os.environ.setdefault("CAPROTO_MAX_SUBSCRIPTION_BACKLOG", "10")
_os.environ.setdefault("CAPROTO_MAX_TOTAL_SUBSCRIPTION_BACKLOG", "200")

__version__ = "0.2.8"

"""Promarchy backend: Proton Mail in the Omarchy bar.

The QML plugin runs ``promarchyctl daemon`` as a long-lived child process and
reads one JSON state snapshot per line from its stdout. Everything that talks
to Proton lives here; the QML side only renders state and forwards commands.
"""

__version__ = "0.1.0"

"""
Dashboard - Deprecated entry point.

This module has been replaced by app.py.
Run: python app.py
"""

import sys


def run_dashboard():
    """Deprecated: Use app.py instead."""
    print("WARNING: dashboard.py is deprecated. Use 'python app.py' instead.")
    from app import run_app
    run_app()


if __name__ == "__main__":
    print("=" * 60)
    print("NOTICE: dashboard.py is deprecated.")
    print("Please use: python app.py")
    print("=" * 60)
    print()
    sys.exit(1)

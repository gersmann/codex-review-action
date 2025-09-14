#!/usr/bin/env python3
"""
Backward compatibility wrapper for the refactored CLI code.
This script maintains compatibility with existing GitHub Actions workflows.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the parent directory to the path so we can import from cli
sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.main import main


def legacy_main() -> None:
    """Legacy entry point for backward compatibility."""
    # Set GitHub Actions mode by adding --github-actions flag
    sys.argv.append("--github-actions")
    sys.exit(main())


if __name__ == "__main__":
    legacy_main()

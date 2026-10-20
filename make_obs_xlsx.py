"""Docker Excel report entry point."""
import sys
from docker_report import cli

if __name__ == "__main__":
    cli([*sys.argv[1:], "--xlsx"])

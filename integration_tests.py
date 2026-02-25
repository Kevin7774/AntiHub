import runpy
from pathlib import Path

if __name__ == "__main__":
    script = Path(__file__).parent / "scripts" / "integration_tests.py"
    runpy.run_path(str(script), run_name="__main__")

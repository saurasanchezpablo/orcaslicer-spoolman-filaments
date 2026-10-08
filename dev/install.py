"""Copy the plugin into OrcaSlicer's local plugin folder (flatpak or native install).

OrcaSlicer loads it on its next start. Usage: python3 dev/install.py [orca-data-dir]
"""

import shutil
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "spoolman_filaments.py"
CANDIDATES = [
    Path.home() / ".var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer",  # flatpak
    Path.home() / ".config/OrcaSlicer",  # native Linux
]


def main() -> int:
    if len(sys.argv) > 1:
        data_dir = Path(sys.argv[1]).expanduser()
    else:
        data_dir = next((d for d in CANDIDATES if (d / "OrcaSlicer.conf").exists()), None)
        if data_dir is None:
            print("OrcaSlicer data folder not found; pass it as an argument.", file=sys.stderr)
            return 1
    target = data_dir / "orca_plugins" / PLUGIN.name
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PLUGIN, target / PLUGIN.name)
    print(f"Installed {target / PLUGIN.name}; restart OrcaSlicer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

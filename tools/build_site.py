"""Build the landing page for a static host: site/ + screenshots + icons + the in-browser demo.

    python tools/build_site.py OUT      # then serve OUT with any static file server
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def build(out):
    out = Path(out)
    shutil.copytree(ROOT / "site", out, dirs_exist_ok=True)
    shutil.copytree(ROOT / "docs" / "screenshots", out / "shots", dirs_exist_ok=True)
    for name in ("demo.gif", "demo-tr.gif"):
        shutil.copy2(ROOT / "docs" / name, out / name)
    for name in ("icon.svg", "icon-512.png", "apple-touch-icon.png", "theme.js", "elmessiri.woff2", "elmessiri-OFL.txt"):
        shutil.copy2(ROOT / "web" / name, out / name)
    demo_server.build(out / "demo")
    for page in ("index.html", "tr/index.html", "404.html"):
        demo_server.stamp(out / page, out)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(build(sys.argv[1]))

from pathlib import Path
import shutil

from curriculum import main as make_curriculum
from rss import main as make_rss

if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    build_dir = base_dir / "build"
    build_dir.mkdir(exist_ok=True)

    # ./static -> ./build/*
    static_dir = base_dir / "static"
    shutil.copytree(static_dir, build_dir, dirs_exist_ok=True)

    # ./build/rss
    make_rss()

    # ./build/curriculum
    make_curriculum()

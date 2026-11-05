# PyInstaller spec for Loom: one folder, two programs, no Python needed.
#
#     uv sync --group package
#     uv run pyinstaller packaging/loom.spec --noconfirm
#
# dist/Loom/ then holds:
#   Loom(.exe)       the app: the local server and its window
#   loom-data(.exe)  scripts/bootstrap.py -- `loom-data --starter` installs the
#                    starter pack; keyless ingesters run too
# and _internal/ with everything they share. A .env placed beside the two
# programs is read the way the repository's .env is.
#
# What is deliberately left out: the embedding model and its runtime
# (fastembed, onnxruntime, tokenizers -- ~150 MB). The starter pack ships its
# vectors prebuilt, and rebuilding a matrix is a job for a Python checkout
# (scripts/embed.py), as is the full build, which runs embed.py and cf.py as
# subprocesses of a Python interpreter.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 -- SPECPATH is PyInstaller's
SRC = ROOT / "src"

EXCLUDE = [
    "fastembed", "onnxruntime", "tokenizers", "huggingface_hub", "py_rust_stemmers",
    "pytest", "mypy", "ruff", "IPython", "tkinter",
]
HIDDEN = collect_submodules("uvicorn") + ["recsys.api.server"]
DATAS = [
    (str(SRC / "recsys" / "ui"), "recsys/ui"),
    (str(ROOT / ".env.example"), "."),
    (str(ROOT / "LICENSE"), "."),
]


def analysis(script):
    return Analysis(  # noqa: F821
        [str(ROOT / script)],
        pathex=[str(SRC)],
        datas=DATAS,
        hiddenimports=HIDDEN,
        excludes=EXCLUDE,
        noarchive=False,
    )


app = analysis("app.py")
data = analysis("scripts/bootstrap.py")

app_exe = EXE(  # noqa: F821
    PYZ(app.pure),  # noqa: F821
    app.scripts,
    [],
    exclude_binaries=True,
    name="Loom",
    # A window app: no console behind it. app.py falls back to the browser if
    # no webview runtime is usable, and logs to DATA_DIR/logs either way.
    console=False,
)
data_exe = EXE(  # noqa: F821
    PYZ(data.pure),  # noqa: F821
    data.scripts,
    [],
    exclude_binaries=True,
    name="loom-data",
    console=True,
)

COLLECT(  # noqa: F821
    app_exe, app.binaries, app.datas,
    data_exe, data.binaries, data.datas,
    name="Loom",
)

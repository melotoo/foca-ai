# Receita do PyInstaller para montar o Foca.AI.
#   Windows: um único Foca.AI.exe
#   Linux:   uma pasta Foca.AI/ com o executável Foca.AI dentro
# Uso: python -m PyInstaller foca.spec
import sys

WINDOWS = sys.platform == "win32"

a = Analysis(
    ["foca.py"],
    pathex=[SPECPATH],
    datas=[("interface", "interface"), ("google_docs_script.js", "."), ("foca.ico", "."), ("foca.png", ".")],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)

if WINDOWS:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Foca.AI", icon="foca.ico", console=False, upx=False)
else:
    exe = EXE(pyz, a.scripts, exclude_binaries=True, name="Foca.AI", console=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="Foca.AI")

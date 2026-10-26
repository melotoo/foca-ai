# Receita do PyInstaller para montar o Foca.AI.
#   Windows: um único Foca.AI.exe
#   Linux:   uma pasta Foca.AI/ com o executável Foca.AI dentro
# Uso: python -m PyInstaller foca.spec
import sys

from PyInstaller.utils.hooks import collect_data_files

WINDOWS = sys.platform == "win32"

datas = [("interface", "interface"), ("google_docs_script.js", "."), ("foca.ico", "."), ("foca.png", ".")]
if not WINDOWS:  # a biblioteca de som do Linux lê os próprios arquivos .h ao iniciar
    datas += collect_data_files("soundcard")

a = Analysis(
    ["foca.py"],
    pathex=[SPECPATH],
    datas=datas,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)

if WINDOWS:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Foca.AI", icon="foca.ico", console=False, upx=False)
else:
    exe = EXE(pyz, a.scripts, exclude_binaries=True, name="Foca.AI", console=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="Foca.AI")

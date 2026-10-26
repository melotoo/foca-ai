"""
Foca.AI — janela de controle: gravar aulas, ver o histórico e configurar chaves e fone.
    pythonw foca.py          (Windows)
    python3 foca.py          (Linux)
"""

import ctypes
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import webview
from groq import Groq

import foca_transcritor as motor

VERSAO = "1.0.0"
PASTA = motor.PASTA          # dados da pessoa: .env, aulas, prompt
RECURSOS = motor.RECURSOS    # arquivos do programa: interface, ícones, script do Google Docs
ICONE = RECURSOS / "foca.ico"
ICONE_PNG = RECURSOS / "foca.png"
TITULO = "Foca.AI"


def gerar_icone():
    """Desenha o logo (o mesmo de interface/logo.svg) em alta resolução e salva como .ico e .png."""
    from PIL import Image, ImageDraw

    s = 16  # desenha em 1024 px e reduz, para bordas suaves
    img = Image.new("RGBA", (64 * s, 64 * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = lambda *v: [x * s for x in v]
    d.rounded_rectangle(k(0, 0, 64, 64), radius=16 * s, fill="#2BB3A3")
    d.arc(k(13, 16, 51, 54), 180, 360, fill="#0F2B3C", width=4 * s)
    d.ellipse(k(15, 23, 49, 53), fill="#EAF0F2")
    d.rounded_rectangle(k(8, 31, 17, 45), radius=4 * s, fill="#FF8A6B")
    d.rounded_rectangle(k(47, 31, 56, 45), radius=4 * s, fill="#FF8A6B")
    for cx in (25.5, 38.5):
        d.ellipse(k(cx - 2.8, 31.2, cx + 2.8, 36.8), fill="#0F2B3C")
        d.ellipse(k(cx, 32.2, cx + 1.8, 34), fill="white")
    d.ellipse(k(24.5, 37.5, 39.5, 48.5), fill="white")
    d.ellipse(k(29.4, 38.8, 34.6, 42.4), fill="#0F2B3C")
    for linha in ((27, 43, 20, 42), (27, 44.6, 20.5, 46), (37, 43, 44, 42), (37, 44.6, 43.5, 46)):
        d.line(k(*linha), fill="#0F2B3C", width=int(1.1 * s))
    img = img.resize((256, 256), Image.LANCZOS)
    img.save(ICONE, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(ICONE_PNG)


def criar_atalho():
    """Cria (ou atualiza) o ícone Foca.AI na área de trabalho."""
    if not ICONE.exists() or not ICONE_PNG.exists():
        gerar_icone()
    if motor.WINDOWS:
        criar_atalho_windows()
    else:
        criar_atalho_linux()


def criar_atalho_windows():
    aspas = lambda caminho: str(caminho).replace("'", "''")
    if motor.CONGELADO:  # Foca.AI.exe: o próprio executável já tem o ícone
        alvo, argumentos, icone = sys.executable, "", sys.executable
    else:
        alvo, argumentos, icone = Path(sys.executable).with_name("pythonw.exe"), f'"{RECURSOS / "foca.py"}"', ICONE
    script = (
        "$d = [Environment]::GetFolderPath('Desktop');"
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Foca.AI.lnk'));"
        f"$s.TargetPath = '{aspas(alvo)}';"
        f"$s.Arguments = '{aspas(argumentos)}';"
        f"$s.WorkingDirectory = '{aspas(Path(alvo).parent if motor.CONGELADO else RECURSOS)}';"
        f"$s.IconLocation = '{aspas(icone)},0';"
        "$s.Description = 'Foca.AI - transcreve suas aulas';"
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def criar_atalho_linux():
    """Atalho .desktop no menu de aplicativos e na área de trabalho."""
    comando = f'"{sys.executable}"' if motor.CONGELADO else f'"{sys.executable}" "{RECURSOS / "foca.py"}"'
    conteudo = ("[Desktop Entry]\nType=Application\nName=Foca.AI\nComment=Transcreve suas aulas\n"
                f"Exec={comando}\nIcon={ICONE_PNG}\nTerminal=false\nCategories=Education;AudioVideo;\n")
    menu = Path.home() / ".local" / "share" / "applications"
    menu.mkdir(parents=True, exist_ok=True)
    destinos = [menu / "foca-ai.desktop"]
    try:
        area = Path(subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True).stdout.strip())
    except OSError:
        area = Path.home() / "Desktop"
    if area.is_dir():
        destinos.append(area / "foca-ai.desktop")
    for arquivo in destinos:
        arquivo.write_text(conteudo, encoding="utf-8")
        arquivo.chmod(0o755)
        try:  # no GNOME, o atalho da área de trabalho precisa ser marcado como confiável
            subprocess.run(["gio", "set", str(arquivo), "metadata::trusted", "true"], capture_output=True)
        except OSError:
            pass


def aplicar_icone_janela():
    """O pywebview usa o ícone do Python; troca pelo do Foca.AI na barra de título e de tarefas."""
    u = ctypes.windll.user32
    u.FindWindowW.restype = ctypes.c_void_p
    u.LoadImageW.restype = ctypes.c_void_p
    u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    hwnd = u.FindWindowW(None, TITULO)
    if hwnd:
        for tamanho, tipo in ((32, 1), (16, 0)):  # ICON_BIG, ICON_SMALL
            u.SendMessageW(hwnd, 0x80, tipo, u.LoadImageW(None, str(ICONE), 1, tamanho, tamanho, 0x10))


class Api:
    """Métodos chamados pela interface (window.pywebview.api.*)."""

    def __init__(self):
        self._janela = None
        self._parar = None
        self._gravando = False

    def _evento(self, tipo, valor=None):
        if self._janela:
            self._janela.evaluate_js(f"foca.evento({json.dumps(tipo)}, {json.dumps(valor, ensure_ascii=False)})")

    # ---------------- configurações

    def config(self):
        v = motor.carregar_env(sobrescrever=True)
        if not v.get("GDOCS_SEGREDO"):  # senha que só o script do Google Docs deste aluno conhece
            motor.salvar_env({"GDOCS_SEGREDO": secrets.token_urlsafe(24)})
            v = motor.carregar_env(sobrescrever=True)
        destino = v.get("FOCA_DESTINO") or ("notion" if v.get("NOTION_TOKEN") and v.get("NOTION_PAGINA_PAI")
                                            else "computador")
        codigo = (RECURSOS / "google_docs_script.js").read_text(encoding="utf-8").replace("{{SEGREDO}}", v["GDOCS_SEGREDO"])
        return {"groq": v.get("GROQ_API_KEY", ""), "destino": destino, "resumo": v.get("FOCA_RESUMO", "sim") != "nao",
                "notion_token": v.get("NOTION_TOKEN", ""), "notion_pagina": v.get("NOTION_PAGINA_PAI", ""),
                "gdocs_url": v.get("GDOCS_URL", ""), "gdocs_pasta": v.get("GDOCS_PASTA", ""), "gdocs_script": codigo,
                "dispositivo": v.get("FOCA_DISPOSITIVO", ""),
                "prompt_resumo": motor.ler_orientacoes(), "modelo_padrao": motor.MODELO_RESUMO_RICO}

    def salvar(self, dados):
        motor.salvar_env({"GROQ_API_KEY": dados.get("groq"), "FOCA_DESTINO": dados.get("destino"),
                          "NOTION_TOKEN": dados.get("notion_token"), "NOTION_PAGINA_PAI": dados.get("notion_pagina"),
                          "GDOCS_URL": dados.get("gdocs_url"), "GDOCS_PASTA": dados.get("gdocs_pasta"),
                          "FOCA_DISPOSITIVO": dados.get("dispositivo")})
        motor.salvar_orientacoes(dados.get("prompt_resumo"))
        return True

    def definir_resumo(self, com_resumo):
        """Lembra a escolha “transcrição + resumo” ou “só transcrição” para as próximas aulas."""
        motor.salvar_env({"FOCA_RESUMO": "sim" if com_resumo else "nao"})
        return True

    def dispositivos(self):
        nomes, padrao = motor.listar_dispositivos()
        return {"lista": nomes, "padrao": padrao}

    def testar_groq(self, chave):
        if not chave.strip():
            return {"ok": False, "msg": "Cole a chave primeiro."}
        try:
            Groq(api_key=chave.strip()).models.list()
            return {"ok": True, "msg": "Chave válida! Tudo certo com o Groq."}
        except Exception as e:
            invalida = "401" in str(e) or "invalid" in str(e).lower()
            return {"ok": False, "msg": "Chave inválida. Confira se copiou ela inteira." if invalida
                    else f"Não consegui falar com o Groq: {e}"}

    def testar_notion(self, token, pagina):
        if not token.strip() or not pagina.strip():
            return {"ok": False, "msg": "Preencha o token e o link da página."}
        try:
            titulo = motor.Notion(token.strip(), pagina.strip()).verificar()
            return {"ok": True, "msg": f"Conectado à página “{titulo}”."}
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            erro = str(e)
            if "401" in erro:
                return {"ok": False, "msg": "Token inválido. Copie de novo o token da integração."}
            if "404" in erro or "400" in erro:
                return {"ok": False, "msg": "Não achei a página. Na página do Notion, abra ••• → Conexões "
                                            "e adicione a sua integração."}
            return {"ok": False, "msg": f"Erro ao falar com o Notion: {erro}"}

    def testar_gdocs(self, url, pasta):
        if not url.strip():
            return {"ok": False, "msg": "Cole o URL do app da Web primeiro."}
        try:
            segredo = motor.carregar_env().get("GDOCS_SEGREDO", "")
            onde = motor.GoogleDocs(url, segredo, pasta).verificar()
            return {"ok": True, "msg": f"Conectado! Os documentos vão para: {onde}."}
        except Exception as e:
            erro = str(e)
            if "getFolderById" in erro or "No item with the given ID" in erro or "Nenhum item" in erro:
                return {"ok": False, "msg": "Não achei essa pasta do Drive. Confira o link ou deixe em branco."}
            return {"ok": False, "msg": erro}

    def testar_som(self, nome):
        if self._gravando:
            return {"ok": False, "msg": "Pare a gravação antes de testar o som."}
        try:
            return {"ok": True, "volume": motor.medir_volume(nome or None, 2.5)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def criar_atalho(self):
        try:
            criar_atalho()
            return {"ok": True, "msg": "Pronto! O ícone do Foca.AI está na sua área de trabalho."}
        except Exception as e:
            return {"ok": False, "msg": f"Não consegui criar o ícone: {e}"}

    # ---------------- gravação

    def gravando(self):
        return self._gravando

    def iniciar(self, titulo, contexto, com_resumo=True):
        if self._gravando:
            return None
        self._gravando = True
        self._parar = threading.Event()
        titulo = titulo.strip() or f"Aula {datetime.now():%d-%m-%Y %H.%M}"
        threading.Thread(target=self._rodar, args=(titulo, contexto.strip(), com_resumo), daemon=True).start()
        return titulo

    def _rodar(self, titulo, contexto, com_resumo):
        try:
            self._evento("fim", motor.executar(titulo, contexto, parar=self._parar, evento=self._evento,
                                               com_resumo=com_resumo))
        except Exception as e:
            self._evento("erro", str(e))
        finally:
            self._gravando = False

    def parar(self):
        if self._parar:
            self._parar.set()

    # ---------------- histórico

    def historico(self):
        itens = []
        pasta = motor.PASTA_TRANSCRICOES
        for arquivo in sorted(pasta.glob("*.md"), reverse=True) if pasta.exists() else []:
            if arquivo.stem.endswith(" - resumo"):
                continue
            resumo = arquivo.with_name(arquivo.stem + " - resumo.md")
            data, _, titulo = arquivo.stem.partition(" ")
            try:
                quando = datetime.strptime(data, "%Y-%m-%d_%H%M").strftime("%d/%m/%Y às %H:%M")
            except ValueError:
                quando, titulo = "", arquivo.stem
            itens.append({"titulo": titulo, "quando": quando, "arquivo": arquivo.name,
                          "resumo": resumo.name if resumo.exists() else None,
                          "palavras": len(arquivo.read_text(encoding="utf-8").split())})
        return itens

    def ler(self, nome):
        caminho = (motor.PASTA_TRANSCRICOES / nome).resolve()
        if caminho.parent != motor.PASTA_TRANSCRICOES.resolve() or not caminho.exists():
            return ""
        return caminho.read_text(encoding="utf-8")

    def abrir_pasta(self):
        motor.PASTA_TRANSCRICOES.mkdir(exist_ok=True)
        if motor.WINDOWS:
            os.startfile(motor.PASTA_TRANSCRICOES)
        else:
            subprocess.Popen(["xdg-open", str(motor.PASTA_TRANSCRICOES)])

    def abrir_link(self, url):
        if url.startswith("https://"):
            webbrowser.open(url)


def main():
    if "--versao" in sys.argv:  # usado na montagem automática para conferir que o executável abre
        print(f"Foca.AI {VERSAO} | dados em {PASTA} | dispositivos: {len(motor.listar_dispositivos()[0])}")
        return
    if not ICONE.exists() and not motor.CONGELADO:
        gerar_icone()
    if motor.WINDOWS:
        try:  # faz o Windows tratar o Foca.AI como app próprio na barra de tarefas
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FocaAI.Transcritor")
        except Exception:
            pass

    api = Api()
    janela = webview.create_window(TITULO, str(RECURSOS / "interface" / "index.html"), js_api=api,
                                   width=1120, height=780, min_size=(920, 640), background_color="#F4F1EA")
    api._janela = janela

    def ao_fechar():
        if api._gravando:
            api._evento("aviso_fechar")
            return False  # cancela o fechamento: a aula ainda não foi resumida

    janela.events.closing += ao_fechar
    if motor.WINDOWS and not motor.CONGELADO:  # o .exe já carrega o próprio ícone
        janela.events.shown += aplicar_icone_janela
    webview.start(icon=str(ICONE if motor.WINDOWS else ICONE_PNG))  # Windows só aceita .ico aqui


if __name__ == "__main__":
    main()

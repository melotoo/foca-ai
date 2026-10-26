"""
Foca.AI — motor de transcrição.

Escuta o que está tocando no seu fone/caixa de som, transcreve com o Whisper do Groq
e vai escrevendo numa página do Notion ou num Google Docs (sem mouse, sem janela em foco).
Ao encerrar, gera (se escolhido) um resumo dos principais pontos no topo do documento.

A interface fica em foca.py. Este arquivo também funciona sozinho no terminal:
    python foca_transcritor.py --titulo "Aula 3 - Pandas" --contexto "Python, pandas, dataframes"
    python foca_transcritor.py --listar              # mostra os dispositivos de saída de som
    python foca_transcritor.py --dispositivo "JBL"   # escuta um fone específico em vez do padrão
"""

import argparse
import io
import os
import queue
import re
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
from groq import Groq, RateLimitError, APIStatusError

WINDOWS = sys.platform == "win32"
if WINDOWS:
    import pyaudiowpatch as pyaudio
else:  # Linux: captura o "monitor" do PulseAudio/PipeWire
    import soundcard

# Executável (PyInstaller): arquivos do app ficam num pacote só de leitura e os dados da pessoa
# (chaves, aulas) numa pasta própria. Rodando pelo código, fica tudo junto na pasta do programa.
CONGELADO = getattr(sys, "frozen", False)
RECURSOS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _pasta_dados():
    if not CONGELADO:
        return Path(__file__).resolve().parent
    base = os.environ.get("APPDATA") if WINDOWS else os.environ.get("XDG_DATA_HOME")
    pasta = Path(base or Path.home() / ".local" / "share") / "Foca.AI"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


PASTA = _pasta_dados()
ARQUIVO_ENV = PASTA / ".env"
PASTA_TRANSCRICOES = PASTA / "transcricoes"

TAXA = 16000             # Hz enviados ao Whisper
MIN_PEDACO = 15          # s: a partir daqui corta na primeira pausa
MAX_PEDACO = 35          # s: corta à força (no ponto mais silencioso) se o professor não parar de falar
LIMIAR_SILENCIO = 0.004  # volume (RMS) abaixo do qual consideramos silêncio
MIN_CARACTERES_POR_SEGUNDO = 5  # fala normal dá 12-16; bem menos que isso indica transcrição cortada
CARACTERES_POR_RESUMO_PARCIAL = 8000  # ~10 min de fala; mantém cada pedido abaixo do limite gratuito de tokens
MODELO_RESUMO_PADRAO = "openai/gpt-oss-120b"

# Frases que o Whisper costuma "inventar" em trechos de música ou silêncio
ALUCINACOES = ("amara.org", "obrigado por assistir", "inscreva-se", "legendas pela comunidade",
               "legenda adriana zanotto", "thanks for watching")

PROMPT_PARCIAL = (
    "Você recebe um trecho da transcrição automática de uma aula (pode ter erros de reconhecimento). "
    "Anote em tópicos, em português, tudo o que foi ensinado neste trecho, na ordem em que apareceu: "
    "conceitos e definições, explicações passo a passo, exemplos, códigos, comandos, fórmulas, números, "
    "comparações, dicas, alertas, erros comuns e tarefas citadas pelo professor. Preserve detalhes técnicos "
    "exatos. Não invente nada que não esteja no texto."
)
PROMPT_FINAL = (
    "Você recebe anotações em tópicos de uma aula inteira, em ordem cronológica, e escreve o resumo final "
    "em português.\n\n"
    "Regra mais importante: tudo no resumo precisa ter sido dito na aula. Não acrescente exemplos, códigos, "
    "nomes, valores, leituras, temas de próximas aulas, erros comuns ou dicas que não estejam nas anotações, "
    "nem para ilustrar ou completar. Se uma seção pedida não tiver conteúdo vindo da aula, omita a seção "
    "inteira em vez de preenchê-la. Um resumo curto e fiel é melhor que um longo com invenções.\n\n"
    "Formato obrigatório (o texto vai para o Notion ou Google Docs): use somente títulos com ##, listas com - ou 1., "
    "**negrito** e `código` em linha. Não use tabelas, blocos de código com ```, nem listas aninhadas.\n\n"
    "Orientações do aluno para este resumo:\n{orientacoes}\n\n"
    "Antes de entregar, releia cada item do resumo e apague todo item que não possa ser apontado nas anotações "
    "(exemplos com nomes ou valores novos, código escrito por você, erros comuns ou leituras não citados, "
    "palpites sobre a próxima aula)."
)
PROMPT_PARCIAL_ORIENTACOES = "\n\nO resumo final seguirá estas orientações do aluno; anote o que for preciso para cumpri-las:\n{orientacoes}"

# Usado quando o aluno não escreve as próprias orientações nas Configurações
MODELO_RESUMO_RICO = """Monte um material de estudo completo, para revisar a aula sem precisar assisti-la de novo, com as seções abaixo (só as que tiverem conteúdo vindo da aula):

## Visão geral
2 a 4 frases dizendo do que a aula tratou, qual o objetivo e onde ela se encaixa no assunto.

## Principais pontos
As ideias mais importantes, em ordem de importância, cada uma em uma frase clara.

## Conceitos e definições
Cada termo em **negrito**, seguido de uma definição curta com as palavras do professor.

## Explicação passo a passo
Como o professor desenvolveu o raciocínio: a sequência de ideias, processos ou etapas, em lista numerada.

## Exemplos e aplicações
Somente os exemplos que o professor deu de fato, e para que servem. Se ele não deu exemplos, omita esta seção.

## Códigos, fórmulas e comandos
Somente o que o professor mostrou ou falou literalmente, em `código`, com uma linha explicando cada item. Não escreva código novo. Se não houve, omita esta seção.

## Dicas, alertas e erros comuns
Somente as recomendações, "pegadinhas" e erros que o professor citou. Não acrescente erros comuns por conta própria.

## Tarefas e próximos passos
Somente exercícios, leituras ou temas que o professor pediu ou anunciou. Não adivinhe o que vem na próxima aula.

## Perguntas para revisão
Até 5 perguntas que testam o entendimento da aula, respondidas só com o que foi ensinado, cada uma seguida da resposta curta (formato: **Pergunta?** Resposta.).

## Palavras-chave
Os termos centrais da aula, separados por vírgula."""

ARQUIVO_PROMPT = PASTA / "prompt_resumo.txt"


def ler_orientacoes():
    """Orientações do aluno para o resumo (vazio = usar o modelo rico padrão)."""
    return ARQUIVO_PROMPT.read_text(encoding="utf-8").strip() if ARQUIVO_PROMPT.exists() else ""


def salvar_orientacoes(texto):
    texto = (texto or "").strip()
    if texto:
        ARQUIVO_PROMPT.write_text(texto + "\n", encoding="utf-8")
    elif ARQUIVO_PROMPT.exists():
        ARQUIVO_PROMPT.unlink()


# ---------------------------------------------------------------- configuração

CHAVES_ENV = {
    "GROQ_API_KEY": "Chave do Groq: https://console.groq.com/keys",
    "FOCA_DESTINO": "Onde salvar as aulas: notion, gdocs ou computador",
    "FOCA_RESUMO": "sim = transcrição e resumo; nao = só a transcrição",
    "NOTION_TOKEN": "Token da integração do Notion: https://www.notion.so/my-integrations",
    "NOTION_PAGINA_PAI": "Link da página do Notion onde as aulas vão ficar (conectada à integração)",
    "GDOCS_URL": "URL do app da Web do Apps Script (Google Docs)",
    "GDOCS_SEGREDO": "Senha que o script do Google Docs exige (gerada pelo Foca.AI)",
    "GDOCS_PASTA": "Link da pasta do Google Drive onde os documentos vão ficar (opcional)",
    "FOCA_DISPOSITIVO": "Fone/caixa de som que o Foca.AI escuta (vazio = o padrão do Windows)",
    "GROQ_MODELO_RESUMO": "Modelo usado no resumo (troque se o Groq descontinuar este)",
}


def carregar_env(sobrescrever=False):
    """Lê o .env para as variáveis de ambiente e devolve o que encontrou."""
    valores = {}
    if ARQUIVO_ENV.exists():
        for linha in ARQUIVO_ENV.read_text(encoding="utf-8-sig").splitlines():
            if "=" in linha and not linha.strip().startswith("#"):
                chave, valor = linha.split("=", 1)
                valores[chave.strip()] = valor.strip().strip('"').strip("'")
    for chave, valor in valores.items():
        if sobrescrever:
            os.environ[chave] = valor
        else:
            os.environ.setdefault(chave, valor)
    return valores


def salvar_env(novos):
    """Grava as configurações no .env, mantendo valores que não vieram em `novos`."""
    valores = carregar_env()
    valores.update({k: (v or "").strip() for k, v in novos.items()})
    linhas = ["# Configurações do Foca.AI (editável pela tela de Configurações)", ""]
    for chave in list(CHAVES_ENV) + [k for k in valores if k not in CHAVES_ENV]:
        if chave in CHAVES_ENV:
            linhas.append(f"# {CHAVES_ENV[chave]}")
        linhas += [f"{chave}={valores.get(chave, '')}", ""]
    ARQUIVO_ENV.write_text("\n".join(linhas), encoding="utf-8")
    for chave, valor in valores.items():
        os.environ[chave] = valor


# ---------------------------------------------------------------- áudio

def rms(audio):
    return float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0


def reamostrar(audio, taxa_origem):
    """Áudio mono na taxa do dispositivo → 16 kHz, que é o que vai para o Whisper."""
    n = int(len(audio) * TAXA / taxa_origem)
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


# Captura(nome) escuta o que sai num fone/caixa de som e entrega blocos de ~100 ms, mono e em 16 kHz,
# para a função passada em iniciar(). Cada sistema tem a sua implementação.
if WINDOWS:
    def listar_dispositivos():
        """Devolve (nomes dos fones/caixas de som, nome do padrão do sistema)."""
        p = pyaudio.PyAudio()
        try:
            nomes = [d["name"].replace(" [Loopback]", "") for d in p.get_loopback_device_info_generator()]
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            padrao = p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
            return nomes, padrao
        finally:
            p.terminate()

    def achar_loopback(p, nome=None):
        """Acha o 'espelho' de gravação (loopback WASAPI) do fone/caixa de som."""
        if not nome:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            nome = p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        for dispositivo in p.get_loopback_device_info_generator():
            if nome.lower() in dispositivo["name"].lower():
                return dispositivo
        raise RuntimeError(f"Não achei o dispositivo de som '{nome}'. Escolha outro nas configurações.")

    class Captura:
        def __init__(self, nome=None):
            self._p = pyaudio.PyAudio()
            try:
                self._disp = achar_loopback(self._p, nome)
            except Exception:
                self._p.terminate()
                raise
            self.nome = self._disp["name"].replace(" [Loopback]", "")
            self._stream = None

        def iniciar(self, ao_receber):
            canais, taxa = self._disp["maxInputChannels"], int(self._disp["defaultSampleRate"])

            def callback(dados, *_):
                mono = np.frombuffer(dados, dtype=np.int16).reshape(-1, canais).mean(axis=1) / 32768.0
                ao_receber(reamostrar(mono, taxa))
                return (None, pyaudio.paContinue)

            self._stream = self._p.open(format=pyaudio.paInt16, channels=canais, rate=taxa, input=True,
                                        input_device_index=self._disp["index"], frames_per_buffer=taxa // 10,
                                        stream_callback=callback)

        def parar(self):
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
            self._p.terminate()
else:
    def listar_dispositivos():
        """Devolve (nomes dos fones/caixas de som, nome do padrão do sistema)."""
        return [s.name for s in soundcard.all_speakers()], soundcard.default_speaker().name

    class Captura:
        TAXA_LINUX = 48000

        def __init__(self, nome=None):
            self.nome = nome or soundcard.default_speaker().name
            try:  # o "monitor" de uma saída de som aparece como microfone de loopback
                self._mic = soundcard.get_microphone(id=self.nome, include_loopback=True)
            except Exception:
                raise RuntimeError(f"Não achei o dispositivo de som '{self.nome}'. Escolha outro nas configurações.")
            self._parado = threading.Event()
            self._thread = None

        def iniciar(self, ao_receber):
            def gravar():
                with self._mic.recorder(samplerate=self.TAXA_LINUX) as gravador:
                    while not self._parado.is_set():
                        dados = gravador.record(numframes=self.TAXA_LINUX // 10)
                        mono = dados.mean(axis=1) if dados.ndim > 1 else dados
                        ao_receber(reamostrar(mono, self.TAXA_LINUX))

            self._thread = threading.Thread(target=gravar, daemon=True)
            self._thread.start()

        def parar(self):
            self._parado.set()
            if self._thread:
                self._thread.join(timeout=2)


def medir_volume(nome=None, segundos=2.0):
    """Escuta o dispositivo por alguns segundos e devolve o volume (0 = silêncio)."""
    blocos = []
    captura = Captura(nome)
    try:
        captura.iniciar(blocos.append)
        time.sleep(segundos)
    finally:
        captura.parar()
    return rms(np.concatenate(blocos)) if blocos else 0.0


def segundos_de_fala(audio):
    """Quanto do trecho tem som (janelas de 100 ms acima do limiar de silêncio)."""
    janela = TAXA // 10
    return sum(rms(audio[i:i + janela]) >= LIMIAR_SILENCIO for i in range(0, len(audio), janela)) / 10


def cortar_no_silencio(audio):
    """Divide no trecho de 100 ms mais silencioso dos últimos 10 s, para não cortar palavras."""
    janela = TAXA // 10
    melhor, menor = len(audio), float("inf")
    for i in range(max(0, len(audio) - 10 * TAXA), len(audio) - janela, janela):
        r = rms(audio[i:i + janela])
        if r < menor:
            melhor, menor = i + janela // 2, r
    return audio[:melhor], audio[melhor:]


def para_wav(audio):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- Notion

def texto_rico(texto):
    """Converte `código` e **negrito** do Markdown em formatação do Notion (máx. 2000 caracteres por trecho)."""
    partes = []
    for trecho in re.split(r"(`[^`]+`|\*\*[^*]+\*\*)", texto):
        anotacao = None
        if len(trecho) > 2 and trecho[0] == trecho[-1] == "`":
            trecho, anotacao = trecho[1:-1], {"code": True}
        elif len(trecho) > 4 and trecho.startswith("**") and trecho.endswith("**"):
            trecho, anotacao = trecho[2:-2].strip("`"), {"bold": True}
        for i in range(0, len(trecho), 2000):
            item = {"type": "text", "text": {"content": trecho[i:i + 2000]}}
            if anotacao:
                item["annotations"] = anotacao
            partes.append(item)
    return partes or [{"type": "text", "text": {"content": ""}}]


def bloco(tipo, texto):
    return {"object": "block", "type": tipo, tipo: {"rich_text": texto_rico(texto)}}


def markdown_para_blocos(md):
    blocos = []
    for linha in md.splitlines():
        l = linha.strip()
        if not l:
            continue
        if l.startswith("#"):
            blocos.append(bloco("heading_3", l.lstrip("#").strip()))
        elif l[:2] in ("- ", "* ", "• "):
            blocos.append(bloco("bulleted_list_item", l[2:].strip()))
        elif re.match(r"\d+[.)]\s", l):
            blocos.append(bloco("numbered_list_item", re.sub(r"^\d+[.)]\s+", "", l)))
        else:
            blocos.append(bloco("paragraph", l))
    return blocos


MARCADOR_RESUMO = "(o resumo aparece aqui quando a aula terminar)"


class Notion:
    API = "https://api.notion.com/v1"
    nome = "Notion"

    def __init__(self, token, pagina_pai):
        ids = re.findall(r"[0-9a-f]{32}", (pagina_pai or "").replace("-", ""))
        if not ids:
            raise ValueError("Link da página do Notion inválido: cole o link completo da página.")
        self.pai = ids[-1]
        self.sessao = requests.Session()
        self.sessao.headers.update({"Authorization": f"Bearer {token}",
                                    "Notion-Version": "2022-06-28"})

    def _pedir(self, metodo, caminho, **kw):
        for tentativa in range(5):
            r = self.sessao.request(metodo, self.API + caminho, timeout=30, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 * (tentativa + 1))))
                continue
            if not r.ok:
                raise RuntimeError(f"Notion {r.status_code}: {r.json().get('message', r.text[:200])}")
            return r.json()
        raise RuntimeError(f"Notion indisponível ({r.status_code})")

    def verificar(self):
        """Confere token e acesso à página; devolve o título dela."""
        pagina = self._pedir("GET", f"/pages/{self.pai}")
        for prop in pagina.get("properties", {}).values():
            if prop.get("type") == "title":
                return "".join(t["plain_text"] for t in prop["title"]) or "(sem título)"
        return "(sem título)"

    def criar_pagina(self, titulo, com_resumo=True):
        topo = [bloco("heading_2", "Resumo"), bloco("paragraph", MARCADOR_RESUMO)] if com_resumo else []
        pagina = self._pedir("POST", "/pages", json={
            "parent": {"page_id": self.pai},
            "properties": {"title": {"title": [{"text": {"content": titulo}}]}},
            "children": topo + [bloco("heading_2", "Transcrição")],
        })
        self.pagina = pagina["id"]
        if com_resumo:
            self.marcador = self._pedir("GET", f"/blocks/{self.pagina}/children")["results"][1]["id"]
        return pagina["url"]

    def anexar_texto(self, texto):
        self.anexar([bloco("paragraph", texto)])

    def anexar(self, blocos, depois=None):
        """Anexa os blocos (de 100 em 100, limite do Notion) no fim da página ou logo após `depois`."""
        for i in range(0, len(blocos), 100):
            corpo = {"children": blocos[i:i + 100]}
            if depois:
                corpo["after"] = depois
            criados = self._pedir("PATCH", f"/blocks/{self.pagina}/children", json=corpo)["results"]
            if depois:  # o próximo lote entra depois do último bloco deste
                depois = criados[-1]["id"]

    def colocar_resumo(self, markdown):
        self.anexar(markdown_para_blocos(markdown), depois=self.marcador)
        self._pedir("DELETE", f"/blocks/{self.marcador}")


class GoogleDocs:
    """Escreve num Google Docs através de um script (Apps Script) publicado na conta Google do aluno."""
    nome = "Google Docs"
    TIPOS = {"heading_3": "titulo", "bulleted_list_item": "lista",
             "numbered_list_item": "numerada", "paragraph": "paragrafo"}

    def __init__(self, url, segredo, pasta=""):
        if not re.match(r"https://script\.google\.com/macros/s/[\w-]+/exec", (url or "").strip()):
            raise ValueError("URL do script inválido: copie o “URL do app da Web”, que termina em /exec.")
        self.url, self.segredo = url.strip(), segredo
        ids = re.findall(r"folders/([\w-]{20,})", pasta or "")
        self.pasta = ids[0] if ids else ""
        self.doc = None

    def _pedir(self, acao, **dados):
        for tentativa in range(4):
            try:
                r = requests.post(self.url, json={"segredo": self.segredo, "acao": acao, "doc": self.doc, **dados},
                                  timeout=60)
            except requests.RequestException:
                time.sleep(3 * (tentativa + 1))
                continue
            if r.status_code >= 500 or r.status_code == 429:
                time.sleep(3 * (tentativa + 1))
                continue
            try:
                resposta = r.json()
            except ValueError:
                raise RuntimeError("O script respondeu algo inesperado. Confira se ele foi implantado como "
                                   "“App da Web” com acesso para “Qualquer pessoa”.")
            if not resposta.get("ok"):
                raise RuntimeError(f"Google Docs: {resposta.get('erro', 'erro desconhecido')}")
            return resposta
        raise RuntimeError("Google Docs indisponível no momento.")

    @staticmethod
    def _converter(blocos_notion):
        """Reaproveita a conversão de Markdown do Notion: tipo do bloco + trechos com negrito/código."""
        convertidos = []
        for b in blocos_notion:
            partes = [{"t": p["text"]["content"], "n": bool(p.get("annotations", {}).get("bold")),
                       "c": bool(p.get("annotations", {}).get("code"))} for p in b[b["type"]]["rich_text"]]
            convertidos.append({"tipo": GoogleDocs.TIPOS.get(b["type"], "paragrafo"), "partes": partes})
        return convertidos

    def verificar(self):
        return self._pedir("testar", pasta=self.pasta).get("descricao", "conta Google")

    def criar_pagina(self, titulo, com_resumo=True):
        r = self._pedir("criar", titulo=titulo, com_resumo=com_resumo, marcador=MARCADOR_RESUMO, pasta=self.pasta)
        self.doc = r["id"]
        return r["url"]

    def anexar_texto(self, texto):
        self._pedir("anexar", textos=[texto])

    def colocar_resumo(self, markdown):
        self._pedir("resumo", marcador=MARCADOR_RESUMO, blocos=self._converter(markdown_para_blocos(markdown)))


def criar_destino():
    """Monta o destino escolhido nas configurações (Notion, Google Docs) ou None para só o computador."""
    escolha = os.environ.get("FOCA_DESTINO")
    if not escolha:  # configurações antigas, de antes de existir a escolha
        escolha = "notion" if os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_PAGINA_PAI") else "computador"
    if escolha == "notion":
        if not (os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_PAGINA_PAI")):
            raise RuntimeError("Notion escolhido como destino, mas falta o token ou o link da página.")
        return Notion(os.environ["NOTION_TOKEN"], os.environ["NOTION_PAGINA_PAI"])
    if escolha == "gdocs":
        if not os.environ.get("GDOCS_URL"):
            raise RuntimeError("Google Docs escolhido como destino, mas falta o URL do script.")
        return GoogleDocs(os.environ["GDOCS_URL"], os.environ.get("GDOCS_SEGREDO", ""), os.environ.get("GDOCS_PASTA", ""))
    return None


# ---------------------------------------------------------------- Groq

def com_espera(funcao, evento):
    """Repete a chamada quando bate no limite gratuito ou o servidor falha."""
    erros = 0
    while True:
        try:
            return funcao()
        except RateLimitError as e:
            espera = int(float(e.response.headers.get("retry-after", 30))) + 1
            evento("status", f"Limite gratuito do Groq atingido, aguardando {espera}s")
            time.sleep(espera)
        except APIStatusError as e:
            erros += 1
            if e.status_code < 500 or erros >= 3:
                raise
            time.sleep(5 * erros)


def conversar(cliente, modelo, sistema, texto, evento):
    resposta = com_espera(lambda: cliente.chat.completions.create(
        model=modelo, temperature=0.2,
        messages=[{"role": "system", "content": sistema}, {"role": "user", "content": texto}]), evento)
    return resposta.choices[0].message.content.strip()


def eh_alucinacao(texto):
    t = texto.lower()
    return len(t) < 80 and any(a in t for a in ALUCINACOES)


# ---------------------------------------------------------------- sessão

class Sessao:
    def __init__(self, cliente, destino, arquivo, contexto, idioma, modelo, modelo_resumo, evento, com_resumo=True):
        self.cliente, self.destino, self.arquivo, self.evento = cliente, destino, arquivo, evento
        self.contexto, self.idioma, self.modelo, self.modelo_resumo = contexto, idioma, modelo, modelo_resumo
        self.com_resumo = com_resumo
        self.pedacos = queue.Queue()
        self.trechos = 0
        self.ultimo_texto = ""
        self.pendente = ""          # texto ainda não resumido
        self.resumos_parciais = []
        self.orientacoes = ler_orientacoes()

    def trabalhar(self):
        """Thread que transcreve os pedaços de áudio na ordem em que chegam."""
        while (audio := self.pedacos.get()) is not None:
            try:
                self.transcrever(audio)
            except Exception as e:  # um pedaço com erro não pode derrubar a aula inteira
                self.evento("status", f"Erro em um trecho: {e}")

    def _whisper(self, audio, prompt):
        return com_espera(lambda: self.cliente.audio.transcriptions.create(
            file=("trecho.wav", para_wav(audio)), model=self.modelo,
            language=self.idioma, prompt=prompt or None, temperature=0, response_format="json"), self.evento).text.strip()

    def transcrever(self, audio):
        partes = []
        if self.contexto:
            partes.append(f"Esta é a transcrição de uma aula. Assunto e termos: {self.contexto}.")
        if self.ultimo_texto:
            partes.append(self.ultimo_texto[-300:])
        prompt = " ".join(partes)
        texto = self._whisper(audio, prompt)
        if prompt and len(texto) < MIN_CARACTERES_POR_SEGUNDO * segundos_de_fala(audio):
            # Com prompt, o Whisper às vezes devolve só um pedaço da fala; tenta de novo sem ele
            sem_prompt = self._whisper(audio, None)
            if len(sem_prompt) > len(texto):
                texto = sem_prompt
        if not texto or eh_alucinacao(texto):
            return

        self.evento("texto", texto)
        self.trechos += 1
        self.ultimo_texto = texto
        with open(self.arquivo, "a", encoding="utf-8") as f:
            f.write(texto + "\n\n")
        if self.destino:
            try:
                self.destino.anexar_texto(texto)
            except Exception as e:
                self.evento("status", f"Falha ao escrever no {self.destino.nome} (o texto ficou salvo no computador): {e}")

        if self.com_resumo:
            self.pendente += " " + texto
            if len(self.pendente) >= CARACTERES_POR_RESUMO_PARCIAL:
                self.resumir_pendente()

    def resumir_pendente(self):
        if self.pendente.strip():
            sistema = PROMPT_PARCIAL
            if self.orientacoes:
                sistema += PROMPT_PARCIAL_ORIENTACOES.format(orientacoes=self.orientacoes)
            self.resumos_parciais.append(
                conversar(self.cliente, self.modelo_resumo, sistema, self.pendente, self.evento))
            self.pendente = ""

    def resumo_final(self):
        self.resumir_pendente()
        if not self.resumos_parciais:
            return None
        anotacoes = "\n\n".join(f"Parte {i}:\n{r}" for i, r in enumerate(self.resumos_parciais, 1))
        sistema = PROMPT_FINAL.format(orientacoes=self.orientacoes or MODELO_RESUMO_RICO)
        return conversar(self.cliente, self.modelo_resumo, sistema, anotacoes, self.evento)


def escutar(captura, sessao, parar):
    """Captura o áudio do fone e entrega pedaços de 15-35 s, cortados nas pausas, até `parar` ser acionado."""
    fila_audio = queue.Queue()
    captura.iniciar(fila_audio.put)
    buffer = np.zeros(0, dtype=np.float32)

    def emitir(audio):
        if rms(audio) >= LIMIAR_SILENCIO:  # pedaço todo em silêncio não vale a requisição
            sessao.pedacos.put(audio)

    try:
        ultimo_audio = time.time()
        while not parar.is_set():
            try:
                novo = fila_audio.get(timeout=0.5)
            except queue.Empty:
                # O Windows não entrega nada quando nenhum som está tocando (aula pausada)
                if len(buffer) and time.time() - ultimo_audio > 2:
                    emitir(buffer)
                    buffer = np.zeros(0, dtype=np.float32)
                continue
            ultimo_audio = time.time()
            buffer = np.concatenate([buffer, novo])
            duracao = len(buffer) / TAXA
            if duracao >= MIN_PEDACO and rms(novo) < LIMIAR_SILENCIO:
                emitir(buffer)
                buffer = np.zeros(0, dtype=np.float32)
            elif duracao >= MAX_PEDACO:
                pedaco, buffer = cortar_no_silencio(buffer)
                emitir(pedaco)
    except KeyboardInterrupt:
        pass
    finally:
        captura.parar()
        if len(buffer):
            emitir(buffer)


def executar(titulo, contexto="", dispositivo=None, idioma="pt", modelo="whisper-large-v3",
             parar=None, evento=None, com_resumo=None):
    """
    Roda uma aula inteira: escuta até `parar` ser acionado (ou Ctrl+C) e, se `com_resumo`, gera o resumo.
    `evento(tipo, valor)` recebe: "status", "pagina" ({"url", "nome"} do documento), "texto", "resumo".
    Devolve {"arquivo", "resumo", "pagina", "transcrito"}.
    """
    evento = evento or (lambda tipo, valor: print(valor))
    parar = parar or threading.Event()
    carregar_env()
    if not os.environ.get("GROQ_API_KEY"):
        raise RuntimeError("Configure a chave do Groq antes de começar.")
    if com_resumo is None:
        com_resumo = os.environ.get("FOCA_RESUMO", "sim") != "nao"
    cliente = Groq()
    modelo_resumo = os.environ.get("GROQ_MODELO_RESUMO") or MODELO_RESUMO_PADRAO
    dispositivo = dispositivo or os.environ.get("FOCA_DISPOSITIVO") or None

    destino, url = criar_destino(), None
    if destino:
        url = destino.criar_pagina(titulo, com_resumo)
        evento("pagina", {"url": url, "nome": destino.nome})
    else:
        evento("status", "Salvando só no computador.")

    PASTA_TRANSCRICOES.mkdir(exist_ok=True)
    nome = re.sub(r'[\\/:*?"<>|]', "-", titulo)
    arquivo = PASTA_TRANSCRICOES / f"{datetime.now():%Y-%m-%d_%H%M} {nome}.md"

    sessao = Sessao(cliente, destino, arquivo, contexto, idioma, modelo, modelo_resumo, evento, com_resumo)
    trabalhador = threading.Thread(target=sessao.trabalhar, daemon=True)
    trabalhador.start()

    try:
        captura = Captura(dispositivo)
        evento("status", f"Ouvindo: {captura.nome}")
        escutar(captura, sessao, parar)
    finally:
        sessao.pedacos.put(None)

    evento("status", "Terminando os últimos trechos...")
    trabalhador.join()
    resumo = None
    if not sessao.trechos:
        evento("status", "Nada foi transcrito.")
    elif com_resumo:
        evento("status", "Gerando o resumo...")
        resumo = sessao.resumo_final()
        arquivo.with_name(arquivo.stem + " - resumo.md").write_text(resumo + "\n", encoding="utf-8")
        if destino:
            destino.colocar_resumo(resumo)
        evento("resumo", resumo)
    return {"arquivo": str(arquivo), "resumo": resumo, "pagina": url, "transcrito": sessao.trechos > 0}


def main():
    parser = argparse.ArgumentParser(description="Foca.AI: transcreve ao vivo o áudio do fone para o Notion ou Google Docs.")
    parser.add_argument("--titulo", default=f"Aula {datetime.now():%d/%m/%Y %H:%M}")
    parser.add_argument("--contexto", default="", help="assunto e termos técnicos do curso")
    parser.add_argument("--idioma", default="pt")
    parser.add_argument("--modelo", default="whisper-large-v3",
                        help="whisper-large-v3 (mais preciso) ou whisper-large-v3-turbo")
    parser.add_argument("--dispositivo", help="parte do nome do fone/caixa a escutar (padrão: o configurado)")
    parser.add_argument("--minutos", type=float, help="encerra sozinho depois desse tempo (padrão: só com Ctrl+C)")
    parser.add_argument("--sem-resumo", action="store_true", help="só transcreve, sem gerar o resumo")
    parser.add_argument("--listar", action="store_true", help="lista os dispositivos de saída e sai")
    args = parser.parse_args()

    if args.listar:
        nomes, padrao = listar_dispositivos()
        for nome in nomes:
            print("-", nome, "(padrão)" if nome in padrao else "")
        return

    parar = threading.Event()
    if args.minutos:
        cronometro = threading.Timer(args.minutos * 60, parar.set)
        cronometro.daemon = True
        cronometro.start()
    print("Pode dar play na aula. Ctrl+C para encerrar.\n")
    try:
        r = executar(args.titulo, args.contexto, args.dispositivo, args.idioma, args.modelo, parar,
                     com_resumo=False if args.sem_resumo else None)
    except (RuntimeError, ValueError) as e:
        sys.exit(str(e))
    print(f"\nArquivos salvos em: {PASTA_TRANSCRICOES}")
    if r["pagina"]:
        print(f"Documento: {r['pagina']}")


if __name__ == "__main__":
    main()

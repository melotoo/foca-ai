# Foca.AI

Escuta a aula que está tocando no seu fone, transcreve em tempo real no **Notion** ou no **Google Docs**
e, no final, monta um resumo com os pontos principais.

**Baixar:** https://melotoo.github.io/foca-ai/

## Rodar pelo código

```bash
pip install -r requirements.txt
python foca.py
```

No Linux, o som é capturado pelo PulseAudio/PipeWire e a janela usa Qt.

## Publicar uma nova versão

Crie uma tag `v*`. O GitHub Actions monta o `.exe` do Windows e o pacote do Linux e publica tudo em Releases;
o site já aponta para a versão mais recente.

```bash
git tag v1.0.1
git push origin v1.0.1
```

## Arquivos

| Arquivo | O que é |
| --- | --- |
| `foca.py` | Janela do programa (pywebview) |
| `foca_transcritor.py` | Captura de áudio, transcrição, Notion/Google Docs e resumo (também roda no terminal) |
| `interface/` | Telas do programa |
| `google_docs_script.js` | Script que cada pessoa cola no Apps Script para usar o Google Docs |
| `foca.spec` | Receita do PyInstaller |
| `site/` | Página de download (GitHub Pages) |

As chaves e as aulas de cada pessoa ficam só no computador dela (`.env` e `transcricoes/`) e nunca vão para o repositório.

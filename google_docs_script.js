// Foca.AI — ponte para o Google Docs.
// Cole este código em script.google.com e implante como "App da Web" (veja Configurações no Foca.AI).
// Só aceita pedidos que tragam a senha abaixo, gerada pelo seu Foca.AI.

const SEGREDO = "{{SEGREDO}}";

function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.segredo !== SEGREDO) return responder({ ok: false, erro: "senha do script não confere: copie o código de novo pelo Foca.AI" });
    const acoes = { testar: testar, criar: criar, anexar: anexar, resumo: resumo };
    if (!acoes[d.acao]) return responder({ ok: false, erro: "ação desconhecida" });
    return responder(Object.assign({ ok: true }, acoes[d.acao](d)));
  } catch (err) {
    return responder({ ok: false, erro: String(err && err.message || err) });
  }
}

function responder(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function testar(d) {
  if (d.pasta) return { descricao: "pasta “" + DriveApp.getFolderById(d.pasta).getName() + "”" };
  return { descricao: "Meu Drive" };
}

function criar(d) {
  const doc = DocumentApp.create(d.titulo);
  const corpo = doc.getBody();
  if (d.com_resumo) {
    corpo.appendParagraph("Resumo").setHeading(DocumentApp.ParagraphHeading.HEADING2);
    corpo.appendParagraph(d.marcador).setHeading(DocumentApp.ParagraphHeading.NORMAL).editAsText().setItalic(true);
  }
  corpo.appendParagraph("Transcrição").setHeading(DocumentApp.ParagraphHeading.HEADING2);
  const vazio = corpo.getChild(0);  // todo documento novo começa com um parágrafo vazio
  if (vazio.getType() === DocumentApp.ElementType.PARAGRAPH && vazio.asParagraph().getText() === "") {
    vazio.removeFromParent();
  }
  doc.saveAndClose();
  if (d.pasta) DriveApp.getFileById(doc.getId()).moveTo(DriveApp.getFolderById(d.pasta));
  return { id: doc.getId(), url: doc.getUrl() };
}

function anexar(d) {
  const corpo = DocumentApp.openById(d.doc).getBody();
  d.textos.forEach(function (t) {
    const p = corpo.appendParagraph(t).setHeading(DocumentApp.ParagraphHeading.NORMAL);
    p.editAsText().setItalic(false).setBold(false);
  });
  return {};
}

function resumo(d) {
  const corpo = DocumentApp.openById(d.doc).getBody();
  const achado = corpo.findText(d.marcador.replace(/[()]/g, "\\$&"));
  if (!achado) throw new Error("não achei o lugar do resumo no documento");
  const marcador = achado.getElement().getParent();
  let indice = corpo.getChildIndex(marcador) + 1;
  let anterior = null, tipoAnterior = null;
  d.blocos.forEach(function (b) {
    // itens seguidos do mesmo tipo ficam na mesma lista (a numeração continua)
    anterior = inserir(corpo, indice++, b, tipoAnterior === b.tipo ? anterior : null);
    tipoAnterior = b.tipo;
  });
  marcador.removeFromParent();
  return {};
}

function inserir(corpo, indice, b, mesmaLista) {
  const texto = b.partes.map(function (p) { return p.t; }).join("") || " ";
  let el;
  if (b.tipo === "lista" || b.tipo === "numerada") {
    el = corpo.insertListItem(indice, texto);
    if (mesmaLista) el.setListId(mesmaLista);
    el.setGlyphType(b.tipo === "lista" ? DocumentApp.GlyphType.BULLET : DocumentApp.GlyphType.NUMBER);
    el.setHeading(DocumentApp.ParagraphHeading.NORMAL);
  } else {
    el = corpo.insertParagraph(indice, texto);
    el.setHeading(b.tipo === "titulo" ? DocumentApp.ParagraphHeading.HEADING3 : DocumentApp.ParagraphHeading.NORMAL);
  }
  const txt = el.editAsText();
  txt.setItalic(false).setBold(false);
  let pos = 0;
  b.partes.forEach(function (p) {
    if (p.t.length) {
      if (p.n) txt.setBold(pos, pos + p.t.length - 1, true);
      if (p.c) txt.setFontFamily(pos, pos + p.t.length - 1, "Roboto Mono");
    }
    pos += p.t.length;
  });
  return el;
}

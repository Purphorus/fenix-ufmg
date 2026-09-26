"""Companion de estudo: revisão assistida de questionário.

O ciclo que este módulo implementa:

    preparar_revisao()   lê a tentativa, separa as questões, acha no SEU
                         material o trecho que fala do assunto
          ↓
    (o modelo propõe resposta + explicação — quem faz isso é o Claude do
     outro lado do MCP, com o enunciado e o trecho na mão)
          ↓
    salvar_revisao()     você edita e aprova; grava em `questoes`/`respostas`
          ↓
    enviar_aprovado()    envia ao Moodle — só quando o quiz NÃO vale nota
          ↓
    roteiro_estudo()     o material de estudo que sobra disso

Sobre o envio. A API do Moodle informa a nota máxima do questionário, mas não
informa o que aquela nota vale na disciplina. Num curso em que o Moodle serve
para participação e a avaliação é presencial, `grade = 10` não diz nada sobre
risco. Quem sabe isso é você, então `pode_enviar()` resolve nesta ordem:

1. política declarada para o **quiz**;
2. política declarada para o **curso**;
3. política **global**;
4. sem nenhuma declaração: heurística `grade == 0` (só prática envia).

Declarar exige um motivo, e a declaração fica em `politicas_envio` com data.
O default é conservador para quem nunca configurou; quem configurou está
dizendo algo que o código não tinha como descobrir sozinho.

Nada aqui gera texto sozinho. O módulo prepara o contexto e persiste o que foi
aprovado; a redação da resposta vem de quem chama.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import busca
import db
import escrita
from moodle_client import MoodleClient, MoodleError

# As sessões de revisão ficam junto do banco, não no diretório corrente —
# senão `estudo preparar` suja a pasta de onde você chamou.
SESSOES_DIR = db.BASE_DIR / "sessoes"


def caminho_sessao(attemptid: int) -> "Path":
    from pathlib import Path as _P
    SESSOES_DIR.mkdir(parents=True, exist_ok=True)
    return SESSOES_DIR / f"sessao_{attemptid}.json"


def gravar_sessao(sessao: "Sessao") -> "Path":
    destino = caminho_sessao(sessao.attemptid)
    destino.write_text(sessao.para_json(), encoding="utf-8")
    return destino


def carregar_sessao(attemptid: int) -> "Sessao":
    return Sessao.de_json(caminho_sessao(attemptid).read_text(encoding="utf-8"))


# Numa lista de verdadeiro/falso, todas as questões repetem o mesmo enunciado
# longo e só mudam na afirmação do fim. Truncar pelo começo faz 20 linhas
# idênticas; o que distingue vem depois deste marcador.
_AFIRMACAO = re.compile(r"afirma[cç][aã]o\s*:?\s*", re.I)


def parte_distintiva(enunciado: str, limite: int = 130) -> str:
    """O pedaço do enunciado que diferencia esta questão das outras.

    Em V/F com preâmbulo compartilhado, é o texto depois de "Afirmação:".
    Sem esse marcador, cai no começo do enunciado.
    """
    m = _AFIRMACAO.search(enunciado or "")
    if m:
        cauda = enunciado[m.end():].strip()
        if cauda:
            return cauda[:limite]
    return (enunciado or "")[:limite]


def resumo_sessao(sessao: "Sessao", liberado: bool, motivo: str) -> str:
    """Listagem compacta: uma linha por questão, sem trecho de fonte.

    O detalhe (alternativas, fonte, correção) sai por `questao_detalhe`, só nos
    slots pedidos. Despejar as 23 questões inteiras custava 26k tokens, dos
    quais quase tudo era ignorado.
    """
    # Questões de uma mesma lista costumam repetir o enunciado inteiro ("Seja
    # uma economia fechada cuja oferta..."). Truncar em N chars faz todas as
    # linhas saírem iguais e não informarem nada. Elidir o prefixo comum
    # mostra justamente o que distingue uma da outra — e ainda sai mais curto.
    linhas = [
        f"Tentativa {sessao.attemptid} — {len(sessao.itens)} questões — modo {sessao.modo}",
        f"Envio: {'liberado' if liberado else 'BLOQUEADO'} — {motivo}",
        "",
        "slot | tipo        | estado      | fonte | o que a questão pergunta",
    ]
    for i in sessao.itens:
        estado = i.estado or "-"
        if i.acertou is True:
            estado = "certa"
        elif i.acertou is False:
            estado = "ERRADA"
        linhas.append(
            f"{i.slot:>4} | {(i.tipo or '-'):<11} | {estado:<11} | "
            f"{'sim' if i.fonte_trecho else 'não':<5} | "
            f"{parte_distintiva(i.enunciado)}"
        )
    errados = [i.slot for i in sessao.itens if i.acertou is False]
    linhas.append("")
    if errados:
        linhas.append(f"Erradas: slots {errados}")
    linhas.append(
        "Detalhe de questões específicas: questao_detalhe(attemptid, slots=[...])"
    )
    return "\n".join(linhas)


def detalhe_questoes(sessao: "Sessao", slots: list[int]) -> str:
    """Detalhe completo só dos slots pedidos."""
    pedidos = set(slots)
    saida = []
    for i in sessao.itens:
        if i.slot not in pedidos:
            continue
        saida.append(f"## slot {i.slot} [{i.tipo}] campo={i.campo or '-'}")
        saida.append(i.enunciado)
        for a in i.alternativas:
            marca = ""
            if a["valor"] == i.sua_resposta:
                marca += "  <- sua resposta"
            if a["valor"] == i.resposta_correta:
                marca += "  (correta)"
            saida.append(f"   ({a['valor']}) {a['texto']}{marca}")
        if not i.alternativas and i.sua_resposta:
            saida.append(f"   sua resposta: {i.sua_resposta}")
        if i.correta_texto:
            saida.append(f"   Moodle: {i.correta_texto}")
        if i.nota_questao:
            saida.append(f"   nota: {i.nota_questao}")
        saida.append(
            f"   fonte: {'…' + i.fonte_trecho[:600] + '…' if i.fonte_trecho else 'NÃO ENCONTRADA no material baixado'}"
        )
        saida.append("")
    return "\n".join(saida) or f"Nenhum dos slots {slots} existe nesta tentativa."


# --------------------------------------------------------------------------
# Leitura do HTML da tentativa
# --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
# O bloco de resposta vem como <fieldset class="ablock"> em umas versões e
# <div class="ablock"> em outras — daí o (?:div|fieldset).
_ENUNCIADO = re.compile(
    r'<div class="qtext">(.*?)</div>\s*<(?:div|fieldset) class="ablock', re.S
)
_QTEXT_SOLTO = re.compile(r'<div class="qtext">(.*?)</div>\s*<', re.S)

# Só inputs de resposta: o checkbox "Marcar questão" (…_:flagged) e o
# sequencecheck também são <input> dentro da questão e não são alternativas.
_ALTERNATIVA = re.compile(
    r'<input[^>]*name="[^"]*_answer"[^>]*value="(\d+)"[^>]*>\s*<label[^>]*>(.*?)</label>',
    re.S,
)
_ALTERNATIVA_ALT = re.compile(
    r'<input[^>]*value="(\d+)"[^>]*name="[^"]*_answer"[^>]*>\s*<label[^>]*>(.*?)</label>',
    re.S,
)
# Ordem dos inputs de resposta — é o que mapeia o índice do <div class="rN">
# para o `value` que o Moodle espera. Os dois não coincidem: r0 pode ser
# value=1 (Verdadeiro) e r1 value=0 (Falso).
_VALORES = re.compile(r'<input[^>]*name="[^"]*_answer"[^>]*value="(\d+)"', re.S)
# Questão numérica/dissertativa: a sua resposta é o value do input de texto.
_RESPOSTA_TEXTO = re.compile(
    r'<input[^>]*type="text"[^>]*name="[^"]*_answer"[^>]*value="([^"]*)"', re.S
)
_RESPOSTA_TEXTO_ALT = re.compile(
    r'<input[^>]*name="[^"]*_answer"[^>]*type="text"[^>]*value="([^"]*)"', re.S
)
# Numa tentativa já corrigida o Moodle entrega tudo pronto no HTML: qual opção
# você marcou (checked), qual era a certa (class r<N> correct) e a resposta em
# texto — que é o único caminho para tipos sem alternativa, como 'calculated'.
_MARCADA = re.compile(r'<input[^>]*value="(\d+)"[^>]*checked="checked"', re.S)
_MARCADA_ALT = re.compile(r'<input[^>]*checked="checked"[^>]*value="(\d+)"', re.S)
_OPCAO_CERTA = re.compile(r'<div class="r(\d+)[^"]*\bcorrect\b[^"]*">', re.S)
_RIGHTANSWER = re.compile(r'class="rightanswer">(.*?)</div>', re.S)
_NOTA = re.compile(r'class="grade">(.*?)</div>', re.S)


def _limpo(fragmento: str) -> str:
    """HTML → texto legível, preservando a ordem das palavras."""
    texto = _TAG.sub(" ", fragmento or "")
    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


def parse_questao(bruto: str) -> tuple[str, list[dict]]:
    """Extrai (enunciado, alternativas) do HTML renderizado de uma questão.

    Devolve alternativas como [{"valor": "0", "texto": "..."}]; `valor` é o que
    o Moodle espera no campo de resposta, não a letra que aparece na tela.
    """
    m = _ENUNCIADO.search(bruto or "") or _QTEXT_SOLTO.search(bruto or "")
    # Sem qtext, o fallback é o HTML inteiro — que carrega cabeçalho, nota e
    # "Marcar questão". Fica truncado e explicitamente sinalizado, para não se
    # passar por enunciado limpo.
    enunciado = _limpo(m.group(1)) if m else _limpo(bruto)[:500]

    pares = _ALTERNATIVA.findall(bruto or "") or _ALTERNATIVA_ALT.findall(bruto or "")
    alternativas = []
    for valor, rotulo in pares:
        texto = _limpo(rotulo)
        # o Moodle prefixa "a. ", "b. " no rótulo; guardamos como veio
        if texto:
            alternativas.append({"valor": valor, "texto": texto})
    return enunciado, alternativas


def _correta_por_texto(correta_texto: str, alternativas: list[dict]) -> str:
    """Descobre o `value` da alternativa certa a partir da frase do Moodle.

    Quando você erra a questão, o Moodle marca só a SUA opção (`r0 incorrect`)
    e não marca a certa — ela aparece apenas como texto ("A resposta correta é
    'Falso'."). Sem este caminho, justamente as questões erradas ficariam sem
    gabarito, que são as que mais importam no roteiro de estudo.
    """
    if not correta_texto or not alternativas:
        return ""
    alvo = correta_texto.lower()
    # do rótulo mais longo para o mais curto: evita "Falso" casar dentro de
    # outra alternativa que o contenha
    for alt in sorted(alternativas, key=lambda a: -len(a["texto"])):
        rotulo = alt["texto"].lower().strip()
        # o Moodle prefixa "a. ", "b. " em múltipla escolha
        rotulo = re.sub(r"^[a-z]\.\s*", "", rotulo)
        if rotulo and rotulo in alvo:
            return alt["valor"]
    return ""


def parse_revisao(bruto: str, alternativas: list[dict] | None = None) -> dict:
    """Extrai a correção de uma questão já avaliada.

    Devolve {marcada, correta, correta_texto, nota} — tudo vindo do Moodle,
    não do modelo. É o que torna a revisão de tentativa finalizada mais
    confiável que qualquer proposta: a resposta certa é a oficial.
    """
    m = _MARCADA.search(bruto or "") or _MARCADA_ALT.search(bruto or "")
    if not m:
        # sem radio marcado: questão de digitar (calculated, numerical, shortanswer)
        m = _RESPOSTA_TEXTO.search(bruto or "") or _RESPOSTA_TEXTO_ALT.search(bruto or "")
    texto = _RIGHTANSWER.search(bruto or "")
    nota = _NOTA.search(bruto or "")

    # `class="r1 correct"` dá a POSIÇÃO da opção certa, não o value dela. A
    # tradução é pela ordem dos inputs de resposta: a n-ésima opção na tela é o
    # n-ésimo input. Confundir os dois inverte o gabarito de todo verdadeiro/
    # falso, porque lá r0 costuma ser value=1.
    correta = ""
    pos = _OPCAO_CERTA.search(bruto or "")
    if pos:
        valores = _VALORES.findall(bruto or "")
        indice = int(pos.group(1))
        if 0 <= indice < len(valores):
            correta = valores[indice]

    correta_texto = _limpo(texto.group(1)) if texto else ""
    if not correta:
        correta = _correta_por_texto(correta_texto, alternativas or [])

    return {
        "marcada": m.group(1) if m else "",
        "correta": correta,
        "correta_texto": correta_texto,
        "nota": _limpo(nota.group(1)) if nota else "",
    }


# --------------------------------------------------------------------------
# Contexto: o trecho do SEU material que fala do assunto
# --------------------------------------------------------------------------

# Palavras que aparecem em qualquer enunciado e não ajudam a localizar nada.
_VAZIAS = {
    "para", "como", "qual", "quais", "sobre", "entre", "esse", "essa", "isso",
    "pela", "pelo", "ainda", "sendo", "seja", "pode", "deve", "mais", "menos",
    "assinale", "alternativa", "correta", "incorreta", "considere", "seguinte",
    "questao", "questão", "afirmativa", "afirmacoes", "afirmações", "opcao",
    "opção", "verdadeiro", "falso", "abaixo", "acima", "cada", "todos", "todas",
}


def _termos(enunciado: str, maximo: int = 6) -> list[str]:
    """Palavras do enunciado com maior chance de localizar o assunto."""
    palavras = re.findall(r"\w{5,}", (enunciado or "").lower(), flags=re.UNICODE)
    vistas: dict[str, int] = {}
    for p in palavras:
        if p not in _VAZIAS and not p.isdigit():
            vistas[p] = vistas.get(p, 0) + 1
    # mais longas primeiro: termo técnico costuma ser a palavra comprida
    return sorted(vistas, key=lambda p: (-len(p), -vistas[p]))[:maximo]


def achar_fonte(
    con: sqlite3.Connection,
    enunciado: str,
    site: str | None = None,
    courseid: int | None = None,
) -> tuple[str, int | None]:
    """Acha no material extraído o trecho que melhor cobre o enunciado.

    Devolve (trecho, arquivo_id). Trecho vazio significa que o assunto não
    está no material baixado — informação útil por si só: ou falta sincronizar,
    ou a questão saiu de algo que você não tem.

    Passa o enunciado inteiro para `busca.py`: o lado vetorial trabalha melhor
    com a frase do que com as seis palavras soltas, e o lado léxico continua
    recebendo as palavras porque é assim que a consulta FTS é montada.
    """
    if not _termos(enunciado):
        return "", None

    # OR, não AND: exigir que os seis termos do enunciado coocorram no mesmo
    # trecho quase nunca casa. Com OR o BM25 ranqueia por quantos termos
    # bateram e quão raros eles são, que é exatamente o que se quer aqui.
    achados = busca.buscar(
        con, enunciado, courseid=courseid, site=site, limite=1,
        exigir_todos=False,
    )
    if not achados:
        return "", None
    a = achados[0]
    # A citação precisa dizer de onde veio; o trecho já vem no tamanho certo,
    # então não há mais o passo de reler o arquivo inteiro para ampliar janela.
    onde = f"[{a['nome']}" + (f", {a['rotulo']}" if a["rotulo"] else "") + "] "
    return onde + a["trecho"], a["arquivo_id"]


# --------------------------------------------------------------------------
# Sessão de revisão
# --------------------------------------------------------------------------


@dataclass
class Item:
    """Uma questão da tentativa, pronta para o modelo propor resposta."""

    slot: int
    campo: str                      # q42:1_answer — o que o Moodle espera
    tipo: str                       # multichoice, truefalse, essay...
    enunciado: str
    alternativas: list[dict] = field(default_factory=list)
    fonte_trecho: str = ""
    arquivo_id: int | None = None
    estado: str = ""

    # vindos do Moodle quando a tentativa já foi corrigida
    sua_resposta: str = ""      # o valor que você marcou
    resposta_correta: str = ""  # o valor correto, segundo o Moodle
    correta_texto: str = ""     # a resposta certa em texto (serve p/ calculated)
    nota_questao: str = ""
    acertou: bool | None = None

    # preenchidos na revisão
    proposta_ia: str = ""
    explicacao: str = ""
    resposta_final: str = ""

    @property
    def divergiu(self) -> bool:
        """Você mudou a resposta do modelo? É o dado de estudo mais útil."""
        return bool(
            self.proposta_ia
            and self.resposta_final
            and self.proposta_ia != self.resposta_final
        )


@dataclass
class Sessao:
    attemptid: int
    quizid: int | None
    site: str
    courseid: int | None
    vale_nota: bool | None
    nota_maxima: float | None
    modo: str = "tentativa"   # tentativa (em andamento) | revisao (finalizada)
    itens: list[Item] = field(default_factory=list)

    def para_json(self) -> str:
        d = asdict(self)
        d["itens"] = [asdict(i) if not isinstance(i, dict) else i for i in self.itens]
        return json.dumps(d, ensure_ascii=False, indent=2)

    @staticmethod
    def de_json(bruto: str) -> "Sessao":
        d = json.loads(bruto)
        itens = [Item(**i) for i in d.pop("itens", [])]
        return Sessao(itens=itens, **d)


def _info_quiz(cli: MoodleClient, courseid: int, quizid: int | None) -> dict:
    """Dados do questionário no curso, incluindo se vale nota."""
    try:
        r = cli.call("mod_quiz_get_quizzes_by_courses", courseids=[courseid])
    except MoodleError:
        return {}
    for q in r.get("quizzes", []):
        if quizid is None or q.get("id") == quizid:
            return q
    return {}


def politica_vigente(
    con: sqlite3.Connection, site: str, courseid: int | None, quizid: int | None
) -> sqlite3.Row | None:
    """Política declarada que se aplica, do mais específico para o mais geral."""
    for escopo, alvo in (("quiz", quizid), ("curso", courseid), ("global", None)):
        if escopo != "global" and alvo is None:
            continue
        r = con.execute(
            """SELECT * FROM politicas_envio
               WHERE site = ? AND escopo = ? AND alvo IS ?""",
            (site, escopo, alvo),
        ).fetchone()
        if r:
            return r
    return None


def declarar_politica(
    con: sqlite3.Connection,
    site: str,
    escopo: str,
    alvo: int | None,
    permitir: bool,
    motivo: str,
) -> None:
    """Registra sua declaração sobre o peso real das notas de questionário.

    `motivo` é obrigatório: daqui a três meses, "por que este curso está
    liberado?" precisa ter resposta no próprio banco.
    """
    if escopo not in ("global", "curso", "quiz"):
        raise ValueError("escopo deve ser global, curso ou quiz")
    if not (motivo or "").strip():
        raise ValueError("informe o motivo da declaração")
    con.execute(
        """INSERT INTO politicas_envio (site, escopo, alvo, permitir, motivo, declarada_em)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(site, escopo, alvo) DO UPDATE SET
             permitir = excluded.permitir,
             motivo = excluded.motivo,
             declarada_em = excluded.declarada_em""",
        (site, escopo, alvo, 1 if permitir else 0, motivo.strip(),
         time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    con.commit()


def pode_enviar(
    sessao: Sessao, con: sqlite3.Connection | None = None
) -> tuple[bool, str]:
    """Única regra de envio do módulo.

    Com `con`, consulta primeiro a política que você declarou para o quiz, o
    curso ou global. Sem política declarada (ou sem `con`), cai na heurística:
    só questionário de prática é enviado.
    """
    if sessao.modo == "revisao":
        return False, "tentativa já finalizada — não há o que enviar, só estudar"

    if con is not None:
        pol = politica_vigente(con, sessao.site, sessao.courseid, sessao.quizid)
        if pol is not None:
            alvo = "" if pol["escopo"] == "global" else f" {pol['alvo']}"
            onde = f"{pol['escopo']}{alvo}"
            if pol["permitir"]:
                return True, f"liberado por política ({onde}): {pol['motivo']}"
            return False, f"bloqueado por política ({onde}): {pol['motivo']}"

    if sessao.vale_nota is False:
        return True, "questionário de prática (não vale nota)"
    if sessao.vale_nota is None:
        return False, (
            "não consegui confirmar se este questionário vale nota — tratando "
            "como avaliativo. Se a nota do Moodle não é a nota real da "
            "disciplina, declare com `estudo politica`"
        )
    return False, (
        f"vale até {sessao.nota_maxima} no Moodle e não há política declarada. "
        "Se essa nota é participação e a avaliação é presencial, declare com "
        "`estudo politica --curso N --permitir --motivo \"...\"`"
    )


def preparar_revisao(
    con: sqlite3.Connection,
    cli: MoodleClient,
    attemptid: int,
    paginas: int = 10,
) -> Sessao:
    """Lê a tentativa e monta a sessão: questões, alternativas e fonte.

    Não propõe resposta nenhuma — devolve o material para que quem chama
    (o Claude, via MCP) proponha.
    """
    # Tentativa em andamento responde a get_attempt_data; finalizada, não —
    # para essa só existe get_attempt_review, que em compensação traz a
    # correção oficial. Descobrir qual é o caso pela falha da primeira é o
    # caminho: não há campo que diga isso antes de perguntar.
    modo = "tentativa"
    try:
        primeira = escrita.dados_tentativa(cli, attemptid, 0)
    except MoodleError:
        modo = "revisao"
        primeira = cli.call("mod_quiz_get_attempt_review", attemptid=attemptid)
        for q in primeira.get("questions", []):
            q["campos"] = sorted(set(escrita._CAMPO.findall(q.get("html", "") or "")))
    attempt = primeira.get("attempt", {}) or {}
    quizid = attempt.get("quiz")
    courseid = None

    # o courseid não vem na tentativa; sai do quiz, quando dá
    info: dict = {}
    for c in con.execute("SELECT DISTINCT courseid FROM cursos WHERE site = ?", (cli.alias,)):
        cand = _info_quiz(cli, c["courseid"], quizid)
        if cand:
            info, courseid = cand, c["courseid"]
            break

    nota_maxima = float(info["grade"]) if info.get("grade") is not None else None
    vale_nota = None if nota_maxima is None else nota_maxima > 0

    itens: list[Item] = []
    vistos: set[int] = set()
    # A revisão devolve todas as questões de uma vez; a tentativa é paginada.
    for pagina in range(1 if modo == "revisao" else paginas):
        dados = primeira if pagina == 0 else escrita.dados_tentativa(cli, attemptid, pagina)
        questoes = dados.get("questions", [])
        if not questoes:
            break
        novas = 0
        for q in questoes:
            slot = int(q.get("slot", 0))
            if slot in vistos:
                continue
            vistos.add(slot)
            novas += 1
            bruto = q.get("html", "") or ""
            enunciado, alternativas = parse_questao(bruto)
            campos = [c for c in (q.get("campos") or []) if c.endswith("_answer")]
            trecho, arq = achar_fonte(con, enunciado, cli.alias, courseid)
            item = Item(
                slot=slot,
                campo=campos[0] if campos else "",
                tipo=q.get("type", ""),
                enunciado=enunciado,
                alternativas=alternativas,
                fonte_trecho=trecho,
                arquivo_id=arq,
                estado=q.get("state", ""),
            )
            if modo == "revisao":
                corr = parse_revisao(bruto, alternativas)
                estado = (q.get("state") or "").lower()
                item.sua_resposta = corr["marcada"]
                item.resposta_correta = corr["correta"]
                item.correta_texto = corr["correta_texto"]
                item.nota_questao = corr["nota"]
                item.acertou = estado.endswith("right") if estado.startswith(
                    ("graded", "mangr")
                ) else None
                # A resposta certa vem do Moodle: o modelo não precisa propô-la,
                # só explicar por quê. Deixar isto preenchido evita que uma
                # explicação alucinada passe por gabarito.
                item.resposta_final = corr["correta"] or corr["correta_texto"]
                item.proposta_ia = ""
            itens.append(item)
        if novas == 0:
            break

    return Sessao(
        attemptid=attemptid,
        quizid=quizid,
        site=cli.alias,
        courseid=courseid,
        vale_nota=vale_nota,
        nota_maxima=nota_maxima,
        modo=modo,
        itens=itens,
    )


# --------------------------------------------------------------------------
# Persistência: o que sobra para estudar
# --------------------------------------------------------------------------


def salvar_revisao(con: sqlite3.Connection, sessao: Sessao) -> list[int]:
    """Grava a sessão revisada em `questoes` + `respostas`.

    Guarda as duas versões: `proposta_ia` (o que o modelo sugeriu) e `correta`
    (o que você aprovou). Onde as duas diferem está a questão que merece
    revisão — é o que `roteiro_estudo` usa.
    """
    agora = time.strftime("%Y-%m-%dT%H:%M:%S")
    ids = []
    for item in sessao.itens:
        if not item.resposta_final and not item.proposta_ia:
            continue  # questão que ninguém respondeu não vira material

        rotulo = next(
            (a["texto"] for a in item.alternativas if a["valor"] == item.resposta_final),
            item.resposta_final,
        )
        ja = con.execute(
            "SELECT id FROM questoes WHERE attemptid = ? AND slot = ?",
            (sessao.attemptid, item.slot),
        ).fetchone()

        campos = (
            sessao.site, sessao.courseid, item.arquivo_id, item.enunciado,
            json.dumps(item.alternativas, ensure_ascii=False),
            item.resposta_final, item.explicacao, item.fonte_trecho,
            sessao.attemptid, item.slot, item.campo, item.proposta_ia,
            1 if item.resposta_final else 0,
            None if sessao.vale_nota is None else int(sessao.vale_nota),
        )
        if ja:
            con.execute(
                """UPDATE questoes SET site=?, courseid=?, arquivo_id=?, enunciado=?,
                       alternativas=?, correta=?, explicacao=?, fonte_trecho=?,
                       attemptid=?, slot=?, campo=?, proposta_ia=?, revisada=?,
                       vale_nota=? WHERE id=?""",
                campos + (ja["id"],),
            )
            qid = int(ja["id"])
        else:
            cur = con.execute(
                """INSERT INTO questoes
                   (site, courseid, arquivo_id, enunciado, alternativas, correta,
                    explicacao, fonte_trecho, attemptid, slot, campo, proposta_ia,
                    revisada, vale_nota, criada_em)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                campos + (agora,),
            )
            qid = int(cur.lastrowid)

        if item.resposta_final:
            # Em modo revisão o Moodle já corrigiu e `acertou` veio de lá; numa
            # tentativa em andamento fica NULL até `registrar_correcao`.
            # E o que se registra é a SUA resposta, não o gabarito: senão o
            # roteiro de estudo acharia que você acertou tudo.
            # Sua resposta, quando conhecida. Sem alternativa correspondente,
            # `sua_resposta` já é o valor digitado. Nunca cai no gabarito: dizer
            # que a resposta certa foi a sua falsificaria o roteiro de estudo.
            dada = next(
                (a["texto"] for a in item.alternativas if a["valor"] == item.sua_resposta),
                item.sua_resposta,
            ) if item.sua_resposta else None
            con.execute(
                """INSERT INTO respostas (questao_id, resposta_dada, acertou, respondida_em)
                   VALUES (?,?,?,?)""",
                (qid, dada, None if item.acertou is None else int(item.acertou), agora),
            )
        ids.append(qid)
    con.commit()
    return ids


def registrar_correcao(con: sqlite3.Connection, cli: MoodleClient, attemptid: int) -> int:
    """Depois que o Moodle corrigir, marca em `respostas` o que você acertou.

    É isto que fecha o ciclo: sem a correção real, a repetição espaçada não
    tem em que se apoiar.
    """
    try:
        r = cli.call("mod_quiz_get_attempt_review", attemptid=attemptid)
    except MoodleError:
        return 0
    atualizadas = 0
    for q in r.get("questions", []):
        slot = q.get("slot")
        estado = (q.get("state") or "").lower()
        # Estados do Moodle: gradedright / gradedpartial / gradedwrong e as
        # variantes mangr* (correção manual). Só conta o que já foi corrigido.
        if not (estado.startswith("graded") or estado.startswith("mangr")):
            continue
        acertou = 1 if estado.endswith("right") else 0
        linha = con.execute(
            "SELECT id FROM questoes WHERE attemptid = ? AND slot = ?", (attemptid, slot)
        ).fetchone()
        if not linha:
            continue
        con.execute(
            """UPDATE respostas SET acertou = ?
               WHERE id = (SELECT MAX(id) FROM respostas WHERE questao_id = ?)""",
            (acertou, linha["id"]),
        )
        atualizadas += 1
    con.commit()
    return atualizadas


# --------------------------------------------------------------------------
# Envio (só prática) e roteiro de estudo
# --------------------------------------------------------------------------


def enviar_aprovado(
    con: sqlite3.Connection,
    cli: MoodleClient,
    sessao: Sessao,
    *,
    finalizar: bool = True,
    confirmar: bool = False,
) -> escrita.Resultado:
    """Envia ao Moodle as respostas aprovadas — apenas se o quiz não valer nota.

    Num questionário avaliativo devolve um Resultado não-ok explicando, e o
    gabarito continua salvo e disponível em `gabarito()`.
    """
    liberado, motivo = pode_enviar(sessao, con)
    if not liberado:
        return escrita.Resultado(
            acao="quiz_enviar",
            alvo=f"attemptid={sessao.attemptid}",
            ok=False,
            detalhe=f"não enviado — {motivo}",
        )

    respostas = {i.campo: i.resposta_final for i in sessao.itens if i.campo and i.resposta_final}
    if not respostas:
        return escrita.Resultado(
            acao="quiz_enviar", alvo=f"attemptid={sessao.attemptid}",
            detalhe="nenhuma resposta aprovada para enviar",
        )
    return escrita.enviar_questionario(
        con, cli, sessao.attemptid, respostas, finalizar=finalizar, confirmar=confirmar
    )


def gabarito(sessao: Sessao, con: sqlite3.Connection | None = None) -> str:
    """O gabarito revisado, em texto, para conferir ou marcar à mão."""
    linhas = [f"Gabarito revisado — tentativa {sessao.attemptid}"]
    liberado, motivo = pode_enviar(sessao, con)
    linhas.append(f"({motivo})\n")
    for i in sessao.itens:
        rotulo = next(
            (a["texto"] for a in i.alternativas if a["valor"] == i.resposta_final), ""
        )
        marca = "≠" if i.divergiu else " "
        linhas.append(f"{marca} {i.slot}. {i.enunciado[:110]}")
        linhas.append(f"     resposta: {rotulo or i.resposta_final or '(em branco)'}")
        if i.divergiu:
            proposto = next(
                (a["texto"] for a in i.alternativas if a["valor"] == i.proposta_ia), i.proposta_ia
            )
            linhas.append(f"     (o modelo propunha: {proposto})")
        if i.explicacao:
            linhas.append(f"     porquê: {i.explicacao[:300]}")
        if i.fonte_trecho:
            linhas.append(f"     fonte: …{i.fonte_trecho[:200]}…")
        linhas.append("")
    return "\n".join(linhas)


def roteiro_estudo(
    con: sqlite3.Connection,
    site: str | None = None,
    courseid: int | None = None,
    limite: int = 20,
) -> str:
    """Monta o roteiro a partir do que foi revisado.

    Ordem de prioridade — o que menos se sabe vem primeiro:
      1. questões erradas na correção do Moodle
      2. questões onde você discordou do modelo (você mexeu: havia dúvida)
      3. o resto, para revisão
    """
    sql = """SELECT q.*, r.acertou, r.resposta_dada,
                    (SELECT COUNT(*) FROM respostas WHERE questao_id = q.id) AS tentativas
             FROM questoes q
             LEFT JOIN respostas r
               ON r.id = (SELECT MAX(id) FROM respostas WHERE questao_id = q.id)
             WHERE q.revisada = 1"""
    params: list = []
    if site:
        sql += " AND q.site = ?"
        params.append(site)
    if courseid:
        sql += " AND q.courseid = ?"
        params.append(courseid)
    sql += """ ORDER BY
                 CASE WHEN r.acertou = 0 THEN 0
                      WHEN q.proposta_ia IS NOT NULL AND q.proposta_ia <> q.correta THEN 1
                      ELSE 2 END,
                 q.id DESC
               LIMIT ?"""
    params.append(limite)

    linhas = con.execute(sql, params).fetchall()
    if not linhas:
        return "Nada revisado ainda. Use `cli.py estudo preparar --tentativa N`."

    saida = []
    for r in linhas:
        if r["acertou"] == 0:
            selo = "ERROU"
        elif r["proposta_ia"] and r["proposta_ia"] != r["correta"]:
            selo = "DIVERGIU"
        else:
            selo = "revisar"
        saida.append(f"\n[{selo}] #{r['id']} {r['enunciado'][:120]}")
        alts = json.loads(r["alternativas"] or "[]")
        gabarito_txt = next(
            (a["texto"] for a in alts if a["valor"] == r["correta"]), r["correta"]
        )
        if r["resposta_dada"]:
            saida.append(f"   sua resposta: {r['resposta_dada']}")
            if r["acertou"] == 0:
                saida.append(f"   correta:      {gabarito_txt}")
        else:
            saida.append(f"   resposta correta: {gabarito_txt}")
        if r["explicacao"]:
            saida.append(f"   {r['explicacao'][:400]}")
        if r["fonte_trecho"]:
            saida.append(f"   fonte: …{r['fonte_trecho'][:250]}…")
    return "\n".join(saida)

"""Extrai o cronograma do plano de ensino e propõe eventos de avaliação.

O calendário do Moodle só tem o que o professor cadastrou como atividade —
prazo de lista, tarefa. A data da prova quase sempre está só no PDF do
programa, numa tabela. É isso que este módulo lê.

**Nada aqui confirma sozinho.** Todo evento entra com `origem='programa_pdf'`
e `confirmado=0`, carregando em `trecho_origem` a linha literal de onde a data
saiu. "Avaliação 1" numa tabela de cronograma vira a data da aula com
facilidade assustadora, e um evento errado confirmado é pior que evento
nenhum: você para de conferir. O fluxo é `cli.py evento pendentes`, você olha o
trecho e confirma ou corrige.

Por padrão só propõe **avaliações**. Um cronograma tem 30 linhas de aula
comum; despejar todas no calendário afoga o que importa. `--tudo` inclui as
aulas, para quem quiser.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

import extract
from db import query
from sync import upsert_evento

# Nome de arquivo/seção que sugere plano de ensino.
PISTAS_NOME = ("programa", "plano de ensino", "plano_de_ensino", "syllabus", "ementa")

# Onde a tabela costuma começar.
_CRONOGRAMA = re.compile(
    r"cronograma|calend[aá]rio\s+de\s+aulas|programa[cç][aã]o\s+das\s+aulas", re.I
)

# 03/08, 26/08, 09/09 — dia/mês, ano quase nunca aparece na tabela.
_DATA = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?\b")

# "Avaliação" sozinha não serve: casa com título de capítulo ("Avaliação por
# Fluxos de Caixa Descontados - Cap. 6"), que é leitura, não prova. O que
# distingue a prova de verdade é o ordinal e a CAIXA ALTA — é assim que o
# professor escreve na tabela.
_ORDINAL = re.compile(
    r"\b(primeir|segund|terceir|quart|quint|sext|[1-6]\s*[ªao°]?)\w*\s+"
    r"(avalia[cç][aã]o|prova|exame)", re.I
)
_PALAVRA_PROVA = re.compile(
    r"avalia[cç][aã]o|prova|exame|\bteste\b|trabalho\s+final|semin[aá]rio", re.I
)
# "Avaliação DE Projetos", "Avaliação POR Fluxos de Caixa" são título de
# capítulo; a prova não leva preposição depois. Sem isto, uma linha curta de
# leitura ("Ross (Avaliação por Fluxos)") passaria por prova.
_PREPOSICAO = re.compile(
    r"(avalia[cç][aã]o|prova|exame)\s+(de|do|da|dos|das|por|pelo|pela|em)\b", re.I
)
# Onde a tabela acaba e começa a bibliografia.
_FIM_TABELA = re.compile(
    r"bibliografia|refer[eê]ncias|leitura\s+complementar", re.I
)


def _e_avaliacao(conteudo: str) -> bool:
    """Distingue prova de capítulo com 'avaliação' no título.

    Ordinal ("PRIMEIRA AVALIAÇÃO") ou CAIXA ALTA bastam. Linha curta falando
    de prova também conta — mas não quando vem preposição depois
    ("Avaliação de Projetos"), que é título de capítulo.
    """
    if _ORDINAL.search(conteudo):
        return True
    m = _PALAVRA_PROVA.search(conteudo)
    if not m:
        return False
    if _PREPOSICAO.search(conteudo) and not m.group(0).isupper():
        return False          # "Avaliação de Projetos": é leitura
    if len(conteudo.strip()) <= 60:
        return True
    return m.group(0).isupper()


def _sem_acento(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


@dataclass
class Entrada:
    """Uma linha do cronograma."""

    data: datetime
    texto: str
    avaliacao: bool
    trecho: str

    @property
    def titulo(self) -> str:
        """Título curto e legível, a partir do texto da linha."""
        t = re.sub(r"\s+", " ", self.texto).strip(" -–—:;")
        return t[:90] or "Atividade"


@dataclass
class Resultado:
    arquivo_id: int | None = None
    nome_arquivo: str = ""
    entradas: list[Entrada] = field(default_factory=list)
    criados: int = 0
    ignorados: int = 0
    erro: str = ""
    fora_de_ordem: list[str] = field(default_factory=list)


def ano_do_site(site: str) -> tuple[int, int]:
    """'20262' -> (2026, 2). Fallback (0, 0) quando o alias não é do padrão.

    O alias do semestre já carrega a informação que falta na tabela, que
    escreve só dia/mês.
    """
    m = re.fullmatch(r"(\d{4})(\d)", site or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def resolver_ano(dia: int, mes: int, ano_base: int, semestre: int) -> int:
    """Escolhe o ano de uma data escrita só como dia/mês.

    Num segundo semestre (agosto→dezembro), mês 1 ou 2 pertence ao ano
    seguinte — é a prova que caiu depois do recesso.
    """
    if not ano_base:
        return datetime.now().year
    if semestre == 2 and mes <= 2:
        return ano_base + 1
    if semestre == 1 and mes >= 11:
        return ano_base - 1
    return ano_base


def achar_programa(con: sqlite3.Connection, site: str, courseid: int) -> tuple[int, str] | None:
    """Acha o arquivo de plano de ensino da turma, por nome ou seção.

    Heurística deliberadamente simples: o professor nomeia esse arquivo de
    forma reconhecível quase sempre. Quando falha, `--arquivo` resolve na mão.
    """
    linhas = query(
        con,
        """SELECT id, nome, secao FROM arquivos
           WHERE site = ? AND courseid = ? AND texto_path IS NOT NULL""",
        (site, courseid),
    )
    for r in linhas:
        alvo = _sem_acento(f"{r['nome']} {r['secao'] or ''}")
        if any(p in alvo for p in PISTAS_NOME):
            return r["id"], r["nome"]
    return None


def parse_cronograma(texto: str, ano_base: int, semestre: int) -> list[Entrada]:
    """Lê a tabela de cronograma e devolve uma entrada por data encontrada.

    O texto extraído de PDF perde a estrutura da tabela: sobra uma sequência
    de "número da aula, data, matéria". Por isso o corte é feito *entre datas*
    — o conteúdo de uma linha é tudo que vem até a próxima data.
    """
    corte = _CRONOGRAMA.search(texto or "")
    corpo = texto[corte.start():] if corte else (texto or "")
    fim_tab = _FIM_TABELA.search(corpo)
    if fim_tab:
        corpo = corpo[: fim_tab.start()]

    achados = list(_DATA.finditer(corpo))
    entradas: list[Entrada] = []
    for i, m in enumerate(achados):
        dia, mes = int(m.group(1)), int(m.group(2))
        if not (1 <= dia <= 31 and 1 <= mes <= 12):
            continue  # 09:20-11:00 e afins viram par improvável
        ano = int(m.group(3) or 0) or resolver_ano(dia, mes, ano_base, semestre)
        if ano < 100:
            ano += 2000
        try:
            data = datetime(ano, mes, dia)
        except ValueError:
            continue  # 31/02 e companhia

        fim = achados[i + 1].start() if i + 1 < len(achados) else len(corpo)
        conteudo = corpo[m.end():fim]
        # o número da próxima aula gruda no fim do conteúdo desta
        conteudo = re.sub(r"\s*\d{1,2}\s*$", "", conteudo).strip()
        trecho = re.sub(r"\s+", " ", corpo[max(0, m.start() - 40):fim])[:300]

        entradas.append(
            Entrada(
                data=data,
                texto=conteudo,
                avaliacao=_e_avaliacao(conteudo),
                trecho=trecho,
            )
        )
    return entradas


def extrair_curso(
    con: sqlite3.Connection,
    site: str,
    courseid: int,
    arquivo_id: int | None = None,
    so_avaliacoes: bool = True,
    gravar: bool = True,
) -> Resultado:
    """Lê o programa da turma e propõe os eventos como pendentes."""
    res = Resultado()

    if arquivo_id is None:
        achado = achar_programa(con, site, courseid)
        if not achado:
            res.erro = "não achei plano de ensino nesta turma (use --arquivo)"
            return res
        arquivo_id, nome = achado
    else:
        r = query(con, "SELECT nome FROM arquivos WHERE id = ?", (arquivo_id,))
        nome = r[0]["nome"] if r else str(arquivo_id)

    res.arquivo_id, res.nome_arquivo = arquivo_id, nome

    texto = extract.texto_de(con, arquivo_id)
    if not texto:
        res.erro = f"{nome} não tem texto extraído (rode `cli.py extrair`)"
        return res

    ano_base, semestre = ano_do_site(site)
    todas = parse_cronograma(texto, ano_base, semestre)

    # Data que anda para trás na tabela é sinal de que o parse errou o ano ou
    # de que o programa tem erro de digitação. Nos dois casos você precisa ver
    # — esconder seria pior, porque a data entra no calendário do mesmo jeito.
    for anterior, seguinte in zip(todas, todas[1:]):
        if seguinte.data < anterior.data:
            res.fora_de_ordem.append(
                f"{anterior.data.strftime('%d/%m')} → "
                f"{seguinte.data.strftime('%d/%m')}: {seguinte.titulo[:60]}"
            )

    res.entradas = [e for e in todas if e.avaliacao] if so_avaliacoes else todas

    if not gravar:
        return res

    for e in res.entradas:
        _id, acao = upsert_evento(
            con,
            site,
            titulo=e.titulo,
            data_inicio=int(e.data.timestamp()),
            origem="programa_pdf",
            courseid=courseid,
            tipo="prova" if e.avaliacao else "aula",
            origem_ref=nome,
            trecho=e.trecho,
            confirmado=0,          # nunca confirma sozinho: a data pode estar errada
        )
        if acao == "criado":
            res.criados += 1
        else:
            res.ignorados += 1
    return res


def extrair_todos(
    con: sqlite3.Connection, site: str, so_avaliacoes: bool = True
) -> list[Resultado]:
    """Percorre as turmas acompanhadas do site."""
    saida = []
    for r in query(
        con,
        "SELECT courseid, shortname FROM cursos WHERE site = ? AND acompanhar = 1",
        (site,),
    ):
        res = extrair_curso(con, site, r["courseid"], so_avaliacoes=so_avaliacoes)
        res.nome_arquivo = res.nome_arquivo or (r["shortname"] or str(r["courseid"]))
        saida.append(res)
    return saida

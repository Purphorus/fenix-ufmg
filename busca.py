"""Busca nos materiais: o léxico acha ONDE, o grafo acha ATÉ ONDE, o vetor QUAL.

Cada camada responde uma pergunta diferente, e é daí que vem a precisão. A
versão anterior fundia duas listas por posto (RRF) e a fusão tinha que
arbitrar entre elas — três sinais foram testados para essa arbitragem (IDF dos
termos, concentração do BM25, densidade semântica) e nenhum separava bem.

O fluxo:

1. **Ancorar** (`grafo.ancorar`) — FTS5 com AND; OR só se AND não sustentar.
   Precisão, não recall: ancorar com OR e muitos candidatos dissolve a
   localidade, e "estado" sozinho traz de volta um capítulo em espanhol.
2. **Decidir** (`grafo.decidir`) — conta, não deliberação. Se a âncora mais o
   grafo alcançam o que o vetor alcançaria, fica no modo âncora; senão cai
   para o vetorial global. Nenhum token de raciocínio é gasto nisso.
3. **Expandir** — um salto na adjacência (`trechos.ordinal`): o termo aparece
   na página onde é definido, e a explicação começa na anterior. Com `ponte`,
   também atravessa para os documentos vizinhos no grafo.
4. **Ordenar** — cosseno dentro do conjunto, com corte RELATIVO ao melhor da
   rodada. Piso absoluto não sobrevive aqui: dentro de uma vizinhança já
   relevante os valores desabam, e o melhor de uma consulta certa pode ficar
   em 0,34.
5. **Montar** — descarta sha repetido, limita por arquivo, corta por orçamento,
   e rebaixa trecho que casa com tudo e não responde nada.

O resultado sempre carrega arquivo, nome e página: é a invariante de que
informação derivada anda com a fonte. Sem isso a resposta vira "acho que os
slides dizem", que é exatamente o que este projeto não quer.
"""

from __future__ import annotations

import re
import sqlite3

import vetor

# Teto de candidatos do modo vetorial global.
CANDIDATOS = 40

# Piso de cosseno do modo VETORIAL global, onde a comparação é contra o corpus
# inteiro. Medido: a mediana de uma consulta qualquer fica em 0,20-0,29 e o que
# responde passa de 0,60. Não vale para o modo âncora — lá o corte é relativo.
PISO_COSSENO = 0.45     # o do MiniLM; o limiar em uso é `vetor.modelo(con).piso_cosseno`

# Modo âncora.
ANCORAS = 20            # quantas âncoras expandir
SALTOS = 1              # vizinhos por ordinal; 2 quando há pouca âncora
POUCA_ANCORA = 5
BONUS_ANCORA = 0.05     # o trecho que o BM25 achou vale mais que o vizinho dele
MARGEM = 0.10           # corte relativo ao melhor da rodada
PONTE_POR_DOC = 5       # trechos trazidos de cada documento vizinho no grafo

# Quantos candidatos o reranker reordena. Medido: 10 e 20 dão o mesmo
# resultado, e 20 custa o dobro do tempo. Dez basta.
RERANK_CANDIDATOS = 10

# No máximo N trechos do mesmo arquivo. Sem isto, uma aula que trata do
# assunto inteiro ocupa os oito resultados e esconde a outra que também trata.
MAX_POR_ARQUIVO = 2

# Orçamento de saída, em caracteres (~1k tokens). É o número que mantém
# `buscar_material` barata; passar dele desfaz o motivo de a busca existir.
ORCAMENTO_CHARS = 4000
MAX_CHARS_TRECHO = 900


def _consulta_fts(termo: str, exigir_todos: bool = True) -> str:
    """Transforma texto livre em consulta FTS segura.

    Aspas, parênteses e operadores (AND/OR/NEAR/*) têm significado no FTS e
    quebram a query quando vêm de texto do usuário; cada palavra vira um termo
    citado.

    `exigir_todos=True` (AND) é o certo para busca do usuário: quem procura
    "estado estacionário" quer as duas palavras. `False` (OR) é para achar o
    material mais próximo de um enunciado inteiro, onde exigir que todos os
    seis termos coocorram quase nunca casa — aí o BM25 ranqueia por quantos
    termos bateram e quão raros eles são.
    """
    palavras = [p for p in re.findall(r"\w+", termo or "", flags=re.UNICODE) if len(p) > 1]
    if not palavras:
        return ""
    citadas = [f'"{p}"' for p in palavras]
    return " ".join(citadas) if exigir_todos else " OR ".join(citadas)


def _filtros(courseid: int | None, site: str | None) -> tuple[str, list]:
    """Filtro duro, aplicado ANTES de ranquear.

    `ignorado = 0` é regra do usuário, não heurística: ele já disse que aqueles
    arquivos não importam. Nenhum score os traz de volta.
    """
    sql = " AND a.ignorado = 0"
    params: list = []
    if courseid:
        sql += " AND a.courseid = ?"
        params.append(courseid)
    if site:
        sql += " AND a.site = ?"
        params.append(site)
    return sql, params


_COLUNAS = """t.id AS trecho_id, t.arquivo_id, t.pagina, t.rotulo, t.titulo,
              t.texto, t.n_chars, t.sha256, t.ruido, a.nome, a.secao, a.courseid,
              a.site, a.cmid, a.modname"""


def _lexico(
    con: sqlite3.Connection, termo: str, courseid, site, exigir_todos: bool
) -> list[sqlite3.Row]:
    consulta = _consulta_fts(termo, exigir_todos)
    if not consulta:
        return []
    onde, params = _filtros(courseid, site)
    sql = f"""SELECT {_COLUNAS}
              FROM trechos_fts f
              JOIN trechos t ON t.id = f.trecho_id
              JOIN arquivos a ON a.id = t.arquivo_id
              WHERE trechos_fts MATCH ?{onde}
              ORDER BY bm25(trechos_fts) LIMIT ?"""
    try:
        return con.execute(sql, [consulta, *params, CANDIDATOS]).fetchall()
    except sqlite3.OperationalError:
        return []  # sintaxe recusada pelo FTS


def _vetorial(con, termo, courseid, site) -> list[tuple[float, sqlite3.Row]]:
    """Modo global: cosseno contra todo o corpus filtrado. Vazio sem modelo."""
    if not vetor.disponivel(con):
        return []
    q = vetor.embutir_um(termo, con)
    if not q:
        return []
    onde, params = _filtros(courseid, site)
    linhas = con.execute(
        f"""SELECT {_COLUNAS}, v.vetor
            FROM trechos t
            JOIN arquivos a ON a.id = t.arquivo_id
            JOIN vetores v ON v.sha256 = t.sha256 AND v.modelo = ?
            WHERE 1=1{onde}""",
        [vetor.chave(con), *params],
    ).fetchall()
    if not linhas:
        return []
    scores = vetor.similaridades(q, [r["vetor"] for r in linhas])
    if not scores:
        return []
    return sorted(
        ((s, linha) for s, linha in zip(scores, linhas)
         if s >= vetor.modelo(con).piso_cosseno),
        key=lambda x: -x[0],
    )[:CANDIDATOS]


def _ancora(con, termo, courseid, site, docs, ponte, exigir_todos=True):
    """Modo âncora: núcleo por adjacência, ponte opcional pelo grafo.

    A ponte é para quando se quer COBRIR um assunto, não para responder uma
    pergunta: medido, ela sobe a cobertura de 5 para 9 arquivos num resumo de
    matéria, e derruba a precisão de 76% para 65% numa consulta pontual, onde
    o documento vizinho só dilui.
    """
    import grafo

    if exigir_todos:
        anc, _ = grafo.ancorar(con, termo, courseid, site, ANCORAS)
    else:
        anc = _lexico(con, termo, courseid, site, False)[:ANCORAS]
    if not anc:
        return []
    if not vetor.disponivel(con):
        return [(1.0 / (i + 1), r) for i, r in enumerate(anc)]

    ids_anc = {r["trecho_id"] for r in anc}
    d_anc = {r["arquivo_id"] for r in anc}
    saltos = 2 if len(anc) < POUCA_ANCORA else SALTOS

    ordinais = con.execute(
        f"""SELECT id, arquivo_id, ordinal FROM trechos
            WHERE id IN ({','.join('?' * len(ids_anc))})""",
        list(ids_anc),
    ).fetchall()
    cond, p = [], []
    for o in ordinais:
        cond.append("(t.arquivo_id = ? AND t.ordinal BETWEEN ? AND ?)")
        p += [o["arquivo_id"], o["ordinal"] - saltos, o["ordinal"] + saltos]
    cands = con.execute(
        f"""SELECT {_COLUNAS}, v.vetor FROM trechos t
            JOIN arquivos a ON a.id = t.arquivo_id
            JOIN vetores v ON v.sha256 = t.sha256 AND v.modelo = ?
            WHERE a.ignorado = 0 AND ({' OR '.join(cond)})""",
        [vetor.chave(con), *p],
    ).fetchall()

    if ponte and docs:
        import numpy as np
        q = np.frombuffer(vetor.embutir_um(termo, con), dtype=np.float32)
        for d in sorted(docs - d_anc):
            linhas = con.execute(
                f"""SELECT {_COLUNAS}, v.vetor FROM trechos t
                    JOIN arquivos a ON a.id = t.arquivo_id
                    JOIN vetores v ON v.sha256 = t.sha256 AND v.modelo = ?
                    WHERE t.arquivo_id = ? AND a.ignorado = 0""",
                [vetor.chave(con), d],
            ).fetchall()
            if not linhas:
                continue
            m = np.frombuffer(b"".join(r["vetor"] for r in linhas), dtype=np.float32)
            m = m.reshape(len(linhas), -1)
            for i in np.argsort(-(m @ q))[:PONTE_POR_DOC]:
                cands.append(linhas[int(i)])

    if not cands:
        return [(1.0 / (i + 1), r) for i, r in enumerate(anc)]

    scores = vetor.similaridades(vetor.embutir_um(termo, con), [r["vetor"] for r in cands])
    # Bônus e margem são DISTÂNCIAS de cosseno: valem na escala do modelo.
    escala = vetor.modelo(con).escala
    pontuados = sorted(
        ((s + (BONUS_ANCORA * escala if r["trecho_id"] in ids_anc else 0.0), r)
         for s, r in zip(scores, cands)),
        key=lambda x: -x[0],
    )
    # O teto sai do score JÁ rebaixado por ruído. Com o score cru, um título de
    # slide que só repete a consulta ("Motivação: a limitação do Modelo de
    # Solow", 41 caracteres) tinha o maior cosseno da rodada, virava o teto, e
    # a margem cortava os 74 trechos de conteúdo que vinham abaixo dele: a busca
    # devolvia UM resultado, e era o título. A avaliação não pegava porque mede
    # precisão entre os devolvidos, e um título certo conta como acerto.
    ajustados = [_ajustar(s, r) for s, r in pontuados]
    teto = max(ajustados)
    return [(s, r) for (s, r), a in zip(pontuados, ajustados)
            if a >= teto - MARGEM * escala]


# O modo vetorial é escolhido quando o vetor acha documentos que a âncora não
# alcança pelo grafo — e aí descartava a âncora inteira. "risco de
# inadimplência" aparece literalmente em 12 trechos de Investimento, mas as
# âncoras eram o mesmo PDF duas vezes (Cap 07 e Cap 07 rev), a cobertura deu
# 0,62 e nenhum dos 12 voltava. Unir, em vez de escolher, é o que a busca
# híbrida faz. Só âncora AND: com OR, uma palavra comum sozinha ancora ruído.
# Medido nas 85 consultas e 58 tópicos: muda 2 listas, uma para melhor (0/4 →
# 2/6 úteis) e nenhuma para pior — na maioria o vetor já trazia as âncoras.
UNIR_ANCORA = True


def _unir_ancora(con, termo, courseid, site, pontuados):
    """Soma ao resultado vetorial as âncoras AND, pontuadas como no modo âncora."""
    import grafo

    anc, modo_fts = grafo.ancorar(con, termo, courseid, site, ANCORAS)
    if modo_fts != "AND":
        return pontuados
    ja = {r["trecho_id"] for _, r in pontuados}
    novas = [r for r in anc if r["trecho_id"] not in ja]
    if not novas:
        return pontuados
    linhas = con.execute(
        f"""SELECT t.id, v.vetor FROM trechos t
            JOIN vetores v ON v.sha256 = t.sha256 AND v.modelo = ?
            WHERE t.id IN ({','.join('?' * len(novas))})""",
        [vetor.chave(con), *[r["trecho_id"] for r in novas]],
    ).fetchall()
    vet = {r["id"]: r["vetor"] for r in linhas}
    novas = [r for r in novas if r["trecho_id"] in vet]
    if not novas:
        return pontuados
    scores = vetor.similaridades(vetor.embutir_um(termo, con),
                                 [vet[r["trecho_id"]] for r in novas])
    bonus = BONUS_ANCORA * vetor.modelo(con).escala
    return sorted(pontuados + [(s + bonus, r) for s, r in zip(scores, novas)],
                  key=lambda x: -x[0])


# Peso máximo do rebaixamento por ruído. Rebaixa, nunca exclui: às vezes o
# sumário É a única menção ao assunto. Medido contra rótulo manual, as regras
# de `extract.pontuar_ruido` pegam 48% do boilerplate com 72% de acerto, então
# um trecho bom cai aqui de vez em quando e precisa poder voltar.
PESO_RUIDO_MAX = 0.55


def _ajustar(score: float, linha: sqlite3.Row) -> float:
    """Rebaixa pelo ruído já calculado na indexação.

    O critério vive em `extract.pontuar_ruido` e em `grafo.construir`, que
    calculam uma vez por trecho. Antes isto rodava regex a cada consulta e não
    enxergava repetição entre arquivos, que é o sinal mais forte.
    """
    r = linha["ruido"] or 0.0
    return score * (1.0 - PESO_RUIDO_MAX * r)


def configuracao(con: sqlite3.Connection) -> dict:
    """Como a busca deste banco está montada. Vai em cada rodada da avaliação,
    para que o relatório diga O QUE mudou entre duas rodadas do mesmo corpus."""
    return {"embedding": vetor.modelo(con).apelido,
            "contexto": vetor.contexto_ligado(con)}


def configurar(con: sqlite3.Connection, embedding: str = "",
               contexto: str = "") -> list[str]:
    """Troca o modelo e/ou o contexto do índice DESTE banco e o refaz.

    Refaz na hora porque índice e consulta desalinhados não erram: respondem
    menos, em silêncio. Os vetores do modelo anterior ficam no banco (são
    chaveados pelo modelo), então voltar atrás não paga o embedding de novo.
    """
    import extract
    import grafo

    msgs = []
    if embedding:
        if embedding not in vetor.MODELOS:
            return [f"modelo desconhecido: {embedding!r}. Existem: "
                    + ", ".join(sorted(vetor.MODELOS))]
        con.execute("""INSERT INTO config_busca (chave, valor) VALUES ('embedding', ?)
                       ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor""",
                    (embedding,))
        msgs.append(f"embedding: {embedding}")
    if contexto:
        v = "1" if contexto in ("1", "sim", "on", "ligado") else "0"
        con.execute("""INSERT INTO config_busca (chave, valor) VALUES ('contexto', ?)
                       ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor""", (v,))
        msgs.append(f"contexto: {'ligado' if v == '1' else 'desligado'}")
    con.commit()
    if not vetor.disponivel(con):
        return msgs + [f"modelo indisponível: {vetor.motivo_indisponivel(con)}"]
    # O FTS carrega o contexto na coluna `nome`, então mudar o contexto exige
    # reindexar; trocar só o modelo basta completar os vetores que faltam.
    if contexto:
        arquivos, trechos = extract.reindexar_tudo(con)
        msgs.append(f"reindexado: {trechos} trechos de {arquivos} arquivos")
    else:
        n = extract.completar_vetores(con)
        msgs.append(f"{n} vetores novos")
        msgs.append(f"grafo: {grafo.construir_tudo(con)} arestas")
    return msgs


def indexado(con: sqlite3.Connection) -> bool:
    return bool(con.execute("SELECT 1 FROM trechos LIMIT 1").fetchone())


def buscar(
    con: sqlite3.Connection,
    termo: str,
    courseid: int | None = None,
    site: str | None = None,
    limite: int = 8,
    exigir_todos: bool = True,
    orcamento_chars: int = ORCAMENTO_CHARS,
    ponte: bool = False,
    rerank: bool = False,
    max_por_arquivo: int = MAX_POR_ARQUIVO,
    podar: bool = False,
) -> list[dict]:
    """Busca com decisão de modo calculada. Devolve trechos com a fonte.

    `max_por_arquivo` existe para o perfilador, onde uma conversa inteira é
    UM arquivo: com o teto de 2, a busca dentro de uma pessoa nunca trazia
    mais que duas janelas. No material, 2 por PDF continua o certo.

    `ponte=True` atravessa para os documentos vizinhos no grafo. Use quando o
    objetivo for COBRIR um assunto (montar resumo, percorrer a matéria), não
    para responder uma pergunta: a ponte sobe a cobertura de 5 para 9 arquivos
    num resumo e derruba a precisão de 76% para 65% numa consulta pontual.

    `podar=True` devolve só as frases do trecho que casam com a consulta (e a
    vizinha anterior), quando alguma casa. É para `buscar_material`, onde o
    degrau seguinte (`trecho_material`) existe para quem quiser a página toda.

    Cada item: trecho_id, arquivo_id, nome, secao, courseid, pagina, rotulo,
    titulo, trecho, score, via.
    """
    import grafo

    modo, docs, _diag = grafo.decidir(con, termo, courseid, site)
    if modo == "ancora":
        pontuados = _ancora(con, termo, courseid, site, docs, ponte, exigir_todos)
        if not pontuados:  # âncora vazia apesar da decisão: não deixa sem resposta
            modo = "vetorial"
    if modo == "vetorial":
        pontuados = _vetorial(con, termo, courseid, site)
        if pontuados and UNIR_ANCORA:
            pontuados = _unir_ancora(con, termo, courseid, site, pontuados)
        if not pontuados:  # sem modelo de embedding: sobra o léxico puro
            linhas = _lexico(con, termo, courseid, site, exigir_todos) or \
                     _lexico(con, termo, courseid, site, False)
            pontuados = [(1.0 / (i + 1), r) for i, r in enumerate(linhas)]
    if not pontuados:
        return []

    ordenados = sorted(
        ((_ajustar(s, linha), linha) for s, linha in pontuados), key=lambda x: -x[0]
    )

    # Reordenação por cross-encoder. Antes do corte por orçamento, e só sobre
    # os candidatos que teriam chance de entrar: reranquear 200 trechos custa
    # segundos e muda só a ordem dos primeiros.
    if rerank and len(ordenados) > 1:
        topo = ordenados[:RERANK_CANDIDATOS]
        nova = vetor.reordenar(termo, [l["texto"][:MAX_CHARS_TRECHO] for _, l in topo])
        if nova:
            # Mantém o score original para não confundir quem lê o campo; o
            # que muda é a ordem.
            ordenados = [topo[i] for i in nova] + ordenados[RERANK_CANDIDATOS:]

    saida: list[dict] = []
    por_arquivo: dict[int, int] = {}
    vistos: set[str] = set()
    gasto = 0
    for score, linha in ordenados:
        if len(saida) >= limite or gasto >= orcamento_chars:
            break
        aid = linha["arquivo_id"]
        # Mesmo texto em dois arquivos: material republicado ("Cap 07.pdf" e
        # "Cap 07 rev.pdf"), ou slide repetido entre duas aulas. Devolver os
        # dois gasta o dobro de token e não acrescenta nada — o sha do trecho
        # detecta isso exatamente, sem heurística de semelhança.
        if linha["sha256"] in vistos:
            continue
        if por_arquivo.get(aid, 0) >= max_por_arquivo:
            continue
        vistos.add(linha["sha256"])
        texto = podar_trecho(termo, linha["texto"]) if podar else ""
        if not texto:
            texto = linha["texto"][:MAX_CHARS_TRECHO]
            if len(linha["texto"]) > MAX_CHARS_TRECHO:
                texto += " …"
        por_arquivo[aid] = por_arquivo.get(aid, 0) + 1
        # O cabeçalho da citação (nome do arquivo, página, seção) entra no
        # orçamento: ele é o que torna o resultado citável, mas custa token
        # igual. Ignorá-lo fazia a saída passar 30% do teto.
        gasto += len(texto) + len(linha["nome"] or "") + 40
        saida.append(
            {
                "trecho_id": linha["trecho_id"],
                # O sha é a identidade ESTÁVEL do trecho: o id é reciclado a
                # cada reindexação. Quem registra cobertura usa este campo.
                "sha256": linha["sha256"],
                "arquivo_id": aid,
                "nome": linha["nome"],
                "secao": linha["secao"],
                "courseid": linha["courseid"],
                # Para abrir a atividade no Moodle: /mod/<modname>/view.php?id=<cmid>.
                # Vai o par, não a URL pronta — a base do site sai uma vez no
                # cabeçalho da resposta em vez de oito vezes.
                "site": linha["site"],
                "cmid": linha["cmid"],
                "modname": linha["modname"],
                "pagina": linha["pagina"],
                "rotulo": linha["rotulo"] or "",
                "titulo": linha["titulo"] or "",
                "trecho": texto,
                "score": round(float(score), 5),
                "via": modo,
            }
        )
    return saida


# --------------------------------------------------------------------------
# Poda: devolver a frase que responde, não o trecho inteiro
# --------------------------------------------------------------------------
#
# O trecho tem ~1200 caracteres porque é a unidade de sentido de um slide, mas
# quem pergunta "taxa interna de retorno" precisa da frase que a define e da
# anterior, não da página. O Provence (ICLR 2025) mostra poda por frase com
# perda desprezível. Aqui ela é léxica e CONSERVADORA: só poda quando alguma
# frase casa com a consulta; sem casamento (pergunta em linguagem natural que
# chegou pelo vetor) não há sinal de onde está a resposta, e cortar seria
# chutar — o trecho segue como antes.
#
# **MEDIDO EM 24/09/2026 E PERDEU — fica desligada.** Economiza ~50% dos
# caracteres, mas julgando às cegas 30 pares (consulta, trecho útil) em três
# versões misturadas, o trecho inteiro servia em 21, e o podado em 11: a poda
# estragou 10 dos 21 e não consertou nenhum. A variante generosa (alvo 650,
# frase anterior E seguinte) deu exatamente o mesmo 11/30. O que se perde é o
# que casa pouco com a consulta e explica muito: a solução do exercício depois
# do enunciado, a frase "por outro lado, as economias internas..." que não
# repete o termo. Juntar as linhas quebradas do PDF (`_frases`) consertou os
# fragmentos, não isso. Não ligue `podar` sem refazer esse julgamento.

PODA_MIN = 500          # trecho menor que isto já é barato: não se mexe
PODA_ALVO = 450         # teto do trecho podado, em caracteres
VIZINHAS = (-1, 0)      # além da frase que casa, quais levar (anterior = -1)
_PARADAS = set(
    "a o as os um uma uns umas de da do das dos e em no na nos nas por para com "
    "como que qual quais se ao aos à às entre sua seu suas seus mais menos sobre "
    "onde quando porque isso esta este essa esse ser são foi forma ele ela eles "
    "elas não sim tem têm faz fazer the of and to in is".split()
)
_FIM_DE_FRASE = re.compile(r"(?<=[.!?;])\s+(?=[\"“(\[]?[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9])")
_MARCADOR_ITEM = re.compile(r"^\s*([•▪●◦\-–—*]|\d+[.)]|[a-z][.)])\s")
LINHA_CURTA = 50        # linha menor que isto é título ou item de slide, não texto quebrado


def _frases(texto: str) -> list[str]:
    """Frases de verdade, com a quebra de linha do PDF desfeita.

    Na primeira versão cada quebra de linha virava frase, e o livro de
    Regional — que o PDF quebra a cada 70 caracteres, no meio da frase — saía
    em fragmentos: "emprega-se o termo economias de economias". Julgados às
    cegas, 10 de 37 trechos podados ficaram fracos, quase todos por isso.
    Agora a linha só termina a frase quando parece item de slide ou título
    (curta, com marcador, ou fechada por pontuação); senão, junta com a
    próxima, desfazendo a hifenização.
    """
    frases: list[str] = []
    for par in re.split(r"\n\s*\n", texto):
        linhas = [l.strip() for l in par.split("\n") if l.strip()]
        blocos, atual = [], ""
        for l in linhas:
            if not atual:
                atual = l
            elif (_MARCADOR_ITEM.match(l) or len(atual) < LINHA_CURTA
                  or atual.endswith((".", "!", "?", ":", ";"))):
                blocos.append(atual)
                atual = l
            elif atual.endswith("-") and l[:1].islower():
                atual = atual[:-1] + l
            else:
                atual += " " + l
        if atual:
            blocos.append(atual)
        for b in blocos:
            frases += [f.strip() for f in _FIM_DE_FRASE.split(b) if f.strip()]
    return frases


def _sem_acento(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower())
                   if unicodedata.category(c) != "Mn")


def _radicais(termo: str) -> set[str]:
    """Radical grosseiro (5 letras) de cada palavra de conteúdo da consulta.

    Grosseiro de propósito: "estacionário" e "estacionária" têm de casar, e um
    stemmer de verdade (RSLP) piorou o BM25 em português nos estudos da
    STIL 2021. Aqui o radical só decide QUAL frase mostrar, nunca o que achar.
    """
    return {_sem_acento(p)[:5] for p in re.findall(r"\w{3,}", termo or "")
            if _sem_acento(p) not in _PARADAS}


def podar_trecho(termo: str, texto: str, alvo: int = PODA_ALVO) -> str:
    """As frases do trecho que casam com a consulta, na ordem, com "…" nas
    lacunas. Vazio quando não há o que podar com segurança — quem chama
    devolve o trecho como sempre devolveu."""
    if len(texto) <= max(PODA_MIN, alvo):
        return ""
    rad = _radicais(termo)
    frases = _frases(texto)
    if len(frases) < 2:
        return ""
    palavras = [{_sem_acento(w)[:5] for w in re.findall(r"\w{3,}", f)} for f in frases]
    nota = [len(rad & p) for p in palavras]
    if not max(nota):
        return ""
    # Cada frase que casa traz a anterior: a definição costuma vir depois da
    # frase que introduz o assunto. Entra primeiro quem casa com mais termos.
    escolhidas: set[int] = set()
    gasto = 0
    for i in sorted(range(len(frases)), key=lambda i: (-nota[i], i)):
        if not nota[i]:
            break
        for j in (i + d for d in VIZINHAS):
            if j < 0 or j >= len(frases) or j in escolhidas:
                continue
            if gasto + len(frases[j]) > alvo and escolhidas:
                break
            escolhidas.add(j)
            gasto += len(frases[j]) + 1
        if gasto >= alvo:
            break
    partes, ultimo = [], -1
    for i in sorted(escolhidas):
        if ultimo >= 0 and i != ultimo + 1:
            partes.append("…")
        partes.append(frases[i][:alvo])
        ultimo = i
    saida = " ".join(partes)
    if min(escolhidas) > 0:
        saida = "… " + saida
    if max(escolhidas) < len(frases) - 1:
        saida += " …"
    # Podar e não economizar nada é só perder contexto.
    return saida if len(saida) < len(texto[:MAX_CHARS_TRECHO]) else ""


# Abstenção: quando o material MENCIONA o assunto mas não o ENSINA.
#
# É o defeito mais perigoso que a avaliação encontrou. Para assuntos que só
# existem nos PDFs digitalizados, todo método devolvia de 9 a 12 trechos com
# confiança — bibliografia, ementa, vizinhança temática — e montar questionário
# em cima disso produz questão sobre conteúdo que o aluno não tem.
#
# Dois sinais medidos separam, e só juntos:
#   - frequência do termo mais raro da consulta: mediana 0 nas ausentes contra
#     19 nas de termo e 4 nas de assunto;
#   - score do primeiro colocado: média 0,484 nas ausentes contra 0,69 e 0,72.
# Combinados, pegam as 6 consultas ausentes do conjunto e marcam 1 das 28
# presentes — que é "externalidades marshallianas", e o material realmente
# nunca usa a palavra. Ou seja, o falso positivo estava certo.
#
# AVISA, não recusa. São 6 consultas ausentes de base: pouco para negar
# resposta, suficiente para pôr a dúvida na mesa. E esconder resultado seria
# trocar um erro barulhento por um silencioso.
DF_MENCAO = 2           # termo que aparece em até tantos trechos é menção
SCORE_FRACO = 0.62      # o do MiniLM; o limiar em uso é `vetor.modelo(con).score_fraco`


def _df_minimo(con: sqlite3.Connection, termo: str) -> int:
    """Em quantos trechos aparece o termo MAIS RARO da consulta."""
    palavras = [p for p in re.findall(r"\w{4,}", termo or "", flags=re.UNICODE)]
    if not palavras:
        return 999
    menor = 999
    for p in palavras:
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM trechos_fts WHERE trechos_fts MATCH ?", (f'"{p}"',)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        menor = min(menor, n)
    return menor


def aviso_cobertura(
    con: sqlite3.Connection, termo: str, achados: list[dict],
    courseid: int | None = None,
) -> str:
    """Aviso de que o assunto parece só mencionado, não coberto. Vazio se não."""
    if not achados:
        return ""
    # O score do topo é cosseno quando a busca passou pelo vetor, e cada modelo
    # tem sua escala; o limiar vem dele. Sem vetor (1/posição) o topo é 1,0 e
    # o aviso nunca dispara — sem o sinal semântico, não há base para avisar.
    if achados[0]["score"] >= vetor.modelo(con).score_fraco:
        return ""
    df = _df_minimo(con, termo)
    if df > DF_MENCAO:
        return ""
    aviso = (
        f"⚠ O material parece MENCIONAR “{termo}” sem ensinar: o termo mais "
        f"específico da pergunta aparece em {df} trecho(s) e nenhum resultado "
        "ficou forte. Confira a fonte antes de montar questão sobre isso."
    )
    sql = "SELECT COUNT(*) FROM arquivos WHERE ignorado = 1"
    params: list = []
    if courseid:
        sql += " AND courseid = ?"
        params.append(courseid)
    n = con.execute(sql, params).fetchone()[0]
    if n:
        aviso += f" ({n} PDF(s) digitalizado(s) deste curso estão fora da busca.)"
    return aviso


def trechos_do_arquivo(
    con: sqlite3.Connection,
    arquivo_id: int,
    pagina: int | None = None,
    vizinhos: int = 1,
) -> list[dict]:
    """Trechos de um arquivo, opcionalmente em volta de uma página.

    É a escada entre a busca e `texto_material`: quando o trecho achado quase
    responde, ler as duas páginas em volta custa ~1k tokens, e ler o PDF
    inteiro custa 20k.
    """
    sql = "SELECT * FROM trechos WHERE arquivo_id = ?"
    params: list = [arquivo_id]
    if pagina:
        sql += " AND pagina BETWEEN ? AND ?"
        params += [pagina - vizinhos, pagina + vizinhos]
    sql += " ORDER BY ordinal"
    return [dict(r) for r in con.execute(sql, params).fetchall()]

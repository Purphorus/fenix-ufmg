"""Grafo entre documentos e a decisão de modo da busca.

Existe para resolver um defeito medido. A busca por âncora (o léxico acha
ONDE, o vetor acha QUAL) é a mais precisa que temos, mas o léxico vira
porteiro: em Macro ela alcançava 5 dos 10 arquivos, e afrouxar o corte de 0,10
até 0,60 não mudava isso — três arquivos simplesmente não compartilham
vocabulário com as consultas. Porta que nunca abriu não abre por margem.

Com o grafo, a âncora atravessa para o documento vizinho e alcança 9 dos 10.

**As duas arestas saem de medição, não de teoria.** Os três arquivos órfãos de
Macro eram justamente os que tinham conteúdo repetido em outro arquivo (a
mesma tabela do IBGE, a mesma figura de Maddison) ou centroide muito próximo
de um deles (Romer a 0,919 do deck de Economia das Ideias, que o cita).

E a decisão de qual modo usar é **conta, não deliberação**: quem decide é
`decidir()`, em Python, antes de recuperar qualquer coisa. Gastar token de
raciocínio numa escolha que uma razão entre conjuntos resolve é desperdício.
"""

from __future__ import annotations

import sqlite3

import busca
import vetor

# Vizinhos por documento no grafo de centroide. Dois. Testado com três: a
# cobertura CAI (de 9 para 8 arquivos) e a precisão também, porque aresta
# demais dilui a vizinhança até ela não significar mais nada.
K_VIZ = 2

# Cosseno entre trechos de arquivos diferentes para dizer "mesmo conteúdo".
# 0,90 pega a tabela republicada e a figura reaproveitada sem pegar dois
# parágrafos que só falam do mesmo assunto.
PISO_DUP = 0.90

# Trecho com quase-gêmeo em pelo menos tantos OUTROS arquivos é boilerplate.
# Três é conservador: em Macro, os 5 trechos que passam disso têm mediana de
# 1231 caracteres e são citação real entre artigos, e o filtro não muda o
# grafo de lá (8 arestas antes e depois). Em ECN300 são 53 trechos de mediana
# 49 caracteres, e o grafo cai de 78 arestas para 1 — que era o número certo.
REPETIDO_BOILERPLATE = 3

# Quantos trechos do topo vetorial definem "o alcance que a consulta pede".
TOP_VET = 30

# Abaixo desta fração, a âncora mais o grafo não alcançam o que o vetor
# alcançaria, e a busca cai para o modo vetorial. 0,70 deixa Regional sempre no
# modo âncora (12 tópicos de 12) e faz Macro cair para vetorial em 4 de 12 —
# que são exatamente os tópicos onde a âncora falhava.
COBERTURA_MINIMA = 0.70

_cache: dict[tuple, dict] = {}


# --------------------------------------------------------------------------
# Construção — roda na indexação, não na consulta
# --------------------------------------------------------------------------


def construir(con: sqlite3.Connection, courseid: int, site: str | None = None) -> int:
    """(Re)constrói o grafo de um curso. Devolve o número de arestas.

    Chamado pela indexação. Sem vetor disponível não há grafo, e a busca
    continua funcionando sem ele — só perde a travessia entre documentos.
    """
    if not vetor.disponivel(con):
        return 0
    import numpy as np

    sql = """SELECT t.id, t.arquivo_id, v.vetor FROM trechos t
             JOIN arquivos a ON a.id = t.arquivo_id
             JOIN vetores v ON v.sha256 = t.sha256 AND v.modelo = ?
             WHERE a.courseid = ? AND a.ignorado = 0"""
    params: list = [vetor.chave_conteudo(con), courseid]
    if site:
        sql += " AND a.site = ?"
        params.append(site)
    linhas = con.execute(sql + " ORDER BY t.id", params).fetchall()

    con.execute("DELETE FROM grafo_documentos WHERE courseid = ?", (courseid,))
    if len(linhas) < 2:
        con.commit()
        return 0

    M = np.frombuffer(b"".join(r["vetor"] for r in linhas), dtype=np.float32)
    M = M.reshape(len(linhas), -1)
    dono = np.array([r["arquivo_id"] for r in linhas])
    docs = sorted(set(dono.tolist()))
    if len(docs) < 2:
        con.commit()
        return 0

    # `repetido`: em quantos OUTROS arquivos o trecho tem quase-gêmeo. Calculado
    # aqui porque a matriz de similaridade já está na mão. É o detector de capa
    # e cabeçalho institucional, e é ele que salva o grafo: sem isto, em ECN300
    # os trechos de 42 caracteres com só o nome do departamento geravam 77 das
    # 78 arestas de conteúdo do curso.
    S = M @ M.T
    espalha = []
    for i in range(len(linhas)):
        viz = np.where(S[i] >= PISO_DUP)[0]
        espalha.append(len({int(dono[j]) for j in viz if dono[j] != dono[i]}))

    con.executemany(
        "UPDATE trechos SET repetido = ?, ruido = MIN(1.0, ruido + ?) WHERE id = ?",
        [(e, 0.4 if e >= REPETIDO_BOILERPLATE else 0.0, linhas[i]["id"])
         for i, e in enumerate(espalha)],
    )
    boilerplate = [e >= REPETIDO_BOILERPLATE for e in espalha]

    arestas: dict[tuple[int, int, str], float] = {}

    # Aresta "vizinho": k mais próximos por centroide.
    cent = {}
    for d in docs:
        c = M[dono == d].mean(0)
        n = float(np.linalg.norm(c))
        cent[d] = c / n if n else c
    for d in docs:
        prox = sorted(
            ((float(cent[d] @ cent[o]), o) for o in docs if o != d), reverse=True
        )[:K_VIZ]
        for peso, o in prox:
            arestas[(min(d, o), max(d, o), "vizinho")] = peso

    # Aresta "conteudo": trecho praticamente igual em dois arquivos — mas só
    # conteúdo de verdade. Capa e cabeçalho ficam de fora, senão todo arquivo
    # do curso vira vizinho de todo arquivo pelo timbre do departamento.
    for i, j in zip(*np.where(np.triu(S, 1) >= PISO_DUP)):
        if boilerplate[i] or boilerplate[j]:
            continue
        a, b = int(dono[i]), int(dono[j])
        if a != b:
            chave = (min(a, b), max(a, b), "conteudo")
            arestas[chave] = max(arestas.get(chave, 0.0), float(S[i, j]))

    con.executemany(
        """INSERT OR REPLACE INTO grafo_documentos (site, courseid, a, b, tipo, peso)
           VALUES (?,?,?,?,?,?)""",
        [(site, courseid, a, b, t, p) for (a, b, t), p in arestas.items()],
    )
    con.commit()
    _cache.pop((courseid, site), None)
    return len(arestas)


def construir_tudo(con: sqlite3.Connection) -> int:
    """Reconstrói o grafo de todos os cursos que têm material indexado."""
    total = 0
    cursos = con.execute(
        """SELECT DISTINCT a.courseid FROM arquivos a
           JOIN trechos t ON t.arquivo_id = a.id WHERE a.ignorado = 0"""
    ).fetchall()
    for r in cursos:
        total += construir(con, r["courseid"])
    return total


# --------------------------------------------------------------------------
# Consulta
# --------------------------------------------------------------------------


def vizinhos(con: sqlite3.Connection, courseid: int) -> dict[int, set[int]]:
    """Lista de adjacência do curso, em memória. Barata e pequena."""
    chave = (courseid, None)
    if chave in _cache:
        return _cache[chave]
    g: dict[int, set[int]] = {}
    for r in con.execute(
        "SELECT a, b FROM grafo_documentos WHERE courseid = ?", (courseid,)
    ):
        g.setdefault(r["a"], set()).add(r["b"])
        g.setdefault(r["b"], set()).add(r["a"])
    _cache[chave] = g
    return g


def ancorar(con: sqlite3.Connection, termo, courseid, site, limite: int = 20):
    """Âncora léxica: AND primeiro, OR só se AND não sustentar nada.

    Precisão, não recall. Ancorar com OR e muitos candidatos dissolve a
    localidade que a âncora deveria criar: "estado" sozinho casa com um
    capítulo de geografia em espanhol, e ele volta a ganhar.
    """
    for todos in (True, False):
        linhas = busca._lexico(con, termo, courseid, site, todos)[:limite]
        if linhas:
            return linhas, ("AND" if todos else "OR")
    return [], "-"


def decidir(con: sqlite3.Connection, termo: str, courseid: int | None,
            site: str | None = None) -> tuple[str, set[int] | None, dict]:
    """Escolhe o modo por conta, antes de recuperar. Nada disto custa token.

    Devolve (modo, documentos_permitidos, diagnóstico).
    `ancora` restringe a busca aos documentos alcançáveis pelo grafo;
    `vetorial` libera o corpus, porque a âncora não daria conta.
    """
    if not vetor.disponivel(con):
        return "vetorial", None, {"motivo": "sem vetor"}

    # Sem curso não há grafo (ele é por curso), mas o núcleo da âncora — léxico
    # acha onde, adjacência amplia, vetor ordena — funciona igual e é o que
    # segura a precisão. Cair direto para vetorial aqui trazia de volta o
    # capítulo de geografia em espanhol para "estado estacionário", que é
    # exatamente o defeito que a âncora existe para evitar.
    if not courseid:
        anc, modo_fts = ancorar(con, termo, None, site)
        if not anc:
            return "vetorial", None, {"motivo": "sem curso e sem âncora"}
        return "ancora", None, {"fts": modo_fts, "docs_ancora": len(
            {r["arquivo_id"] for r in anc}), "motivo": "sem curso: âncora sem grafo"}

    anc, modo_fts = ancorar(con, termo, courseid, site)
    if not anc:
        return "vetorial", None, {"motivo": "sem âncora léxica"}

    import numpy as np

    d_anc = {r["arquivo_id"] for r in anc}
    g = vizinhos(con, courseid)
    d_gra = set(d_anc) | {x for d in d_anc for x in g.get(d, ())}

    linhas = con.execute(
        f"""SELECT t.arquivo_id, v.vetor FROM trechos t
            JOIN arquivos a ON a.id = t.arquivo_id
            JOIN vetores v ON v.sha256 = t.sha256 AND v.modelo = ?
            WHERE a.courseid = ? AND a.ignorado = 0""",
        (vetor.chave(con), courseid),
    ).fetchall()
    if not linhas:
        return "ancora", d_gra, {"motivo": "sem vetor no curso"}

    q = np.frombuffer(vetor.embutir_um(termo, con), dtype=np.float32)
    M = np.frombuffer(b"".join(r["vetor"] for r in linhas), dtype=np.float32)
    M = M.reshape(len(linhas), -1)
    dono = [r["arquivo_id"] for r in linhas]
    topo = np.argsort(-(M @ q))[:TOP_VET]
    d_vet = {dono[int(i)] for i in topo}

    cobertura = len(d_gra & d_vet) / len(d_vet) if d_vet else 1.0
    diag = {
        "fts": modo_fts, "docs_ancora": len(d_anc), "docs_grafo": len(d_gra),
        "docs_vetor": len(d_vet), "cobertura": round(cobertura, 2),
    }
    if cobertura >= COBERTURA_MINIMA:
        return "ancora", d_gra, diag
    return "vetorial", None, diag

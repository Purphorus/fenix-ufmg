"""Memória: o que não precisa ser redescoberto a cada conversa.

Duas coisas, e só duas:

**Apelidos** — "macro" → curso 6095. Toda conversa hoje gasta uma chamada a
`listar_turmas` só para traduzir o nome que você usa no id que a API quer.
O apelido é estável dentro do semestre e a chave inclui o site, então quando
você troca de semestre os apelidos antigos param de valer sozinhos.

**Resumos** — chaveados pelo `sha256` do arquivo, não pelo id. Se o professor
republicar com alteração real de conteúdo, o hash muda, o resumo antigo deixa
de ser encontrado, e ninguém precisa lembrar de invalidar nada. É a mesma
chave que o `sync` já usa para não avisar de republicação falsa.

O que este módulo se recusa a memorizar está em `VOLATIL`, e a recusa é
código, não comentário — mesma disciplina de `companion.pode_enviar()`.
"""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from typing import Iterable

# O Moodle é autoridade sobre isto, e isto muda. Guardar não economiza token:
# faz a ferramenta mentir com confiança, que é pior que resposta lenta. Nota
# velha apresentada como atual é o pior resultado possível deste projeto.
VOLATIL = {
    "nota", "prazo", "estado_tentativa", "status_entrega", "novidade",
    "progresso", "resposta",
}

TIPOS_VALIDOS = {"curso", "quiz", "arquivo", "forum", "tarefa", "secao"}


def normalizar(termo: str) -> str:
    """'Macro III ' -> 'macro iii'. Sem acento, sem caixa, sem espaço sobrando.

    Assim "Macro", "macro" e "MACRO" resolvem para a mesma entrada, e
    "Econometria" casa com "econometria" digitado sem acento.
    """
    t = unicodedata.normalize("NFKD", (termo or "").strip().lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t)


def agora_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------
# Apelidos
# --------------------------------------------------------------------------


def memorizar(
    con: sqlite3.Connection,
    site: str,
    termo: str,
    alvo_tipo: str,
    alvo_id: int,
    rotulo: str | None = None,
) -> str:
    """Grava um apelido. Recusa o que não pode ser memorizado.

    Devolve a mensagem para mostrar ao usuário — inclusive a da recusa, que
    explica o motivo em vez de só negar.
    """
    tipo = (alvo_tipo or "").strip().lower()

    if tipo in VOLATIL:
        return (
            f"Não memorizo '{tipo}': o Moodle é a autoridade sobre isso e muda. "
            "Guardar aqui faria a resposta ficar velha sem avisar — nota e prazo "
            "são sempre consultados ao vivo. Memorize o caminho (a turma, o "
            "questionário), não o valor."
        )
    if tipo not in TIPOS_VALIDOS:
        return (
            f"Tipo '{alvo_tipo}' desconhecido. "
            f"Use um de: {', '.join(sorted(TIPOS_VALIDOS))}."
        )

    chave = normalizar(termo)
    if not chave:
        return "Informe o termo a memorizar."

    con.execute(
        """INSERT INTO memoria_alias
           (site, termo, alvo_tipo, alvo_id, rotulo, criado_em, usos)
           VALUES (?,?,?,?,?,?,0)
           ON CONFLICT(site, termo) DO UPDATE SET
             alvo_tipo = excluded.alvo_tipo,
             alvo_id = excluded.alvo_id,
             rotulo = excluded.rotulo,
             criado_em = excluded.criado_em""",
        (site, chave, tipo, int(alvo_id), rotulo, agora_iso()),
    )
    con.commit()
    return f"Memorizado: '{chave}' = {tipo} {alvo_id}" + (f" ({rotulo})" if rotulo else "")


def resolver(
    con: sqlite3.Connection, site: str, termo: str, alvo_tipo: str | None = None
) -> sqlite3.Row | None:
    """Traduz um apelido no alvo. Conta o uso, para saber o que vale manter."""
    chave = normalizar(termo)
    sql = "SELECT * FROM memoria_alias WHERE site = ? AND termo = ?"
    params: list = [site, chave]
    if alvo_tipo:
        sql += " AND alvo_tipo = ?"
        params.append(alvo_tipo)
    r = con.execute(sql, params).fetchone()
    if r:
        con.execute(
            "UPDATE memoria_alias SET usos = usos + 1, ultimo_uso = ? WHERE id = ?",
            (agora_iso(), r["id"]),
        )
        con.commit()
    return r


def esquecer(con: sqlite3.Connection, site: str, termo: str) -> str:
    cur = con.execute(
        "DELETE FROM memoria_alias WHERE site = ? AND termo = ?",
        (site, normalizar(termo)),
    )
    con.commit()
    return (
        f"Esqueci '{normalizar(termo)}'." if cur.rowcount
        else f"'{termo}' não estava memorizado."
    )


def listar(con: sqlite3.Connection, site: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM memoria_alias"
    params: list = []
    if site:
        sql += " WHERE site = ?"
        params.append(site)
    return con.execute(sql + " ORDER BY usos DESC, termo", params).fetchall()


def contexto(con: sqlite3.Connection, site: str | None = None, limite: int = 40) -> str:
    """A tabela de apelidos, compacta, para carregar no início da conversa.

    Custa ~200 tokens e poupa uma chamada de resolução em quase toda pergunta.
    É a única parte da memória que entra sem ser pedida.
    """
    linhas = listar(con, site)[:limite]
    if not linhas:
        return (
            "Nenhum apelido memorizado. Quando resolver um pela primeira vez "
            "(ex.: 'Macro' = curso 6095), ofereça salvá-lo com memorizar()."
        )
    partes = [
        f"{r['termo']}={r['alvo_tipo']}:{r['alvo_id']}" for r in linhas
    ]
    return "Apelidos conhecidos: " + "; ".join(partes)


# --------------------------------------------------------------------------
# Resumos (chave = sha256 do conteúdo)
# --------------------------------------------------------------------------


def guardar_resumo(
    con: sqlite3.Connection,
    sha256: str,
    resumo: str,
    arquivo_id: int | None = None,
    escopo: str = "arquivo",
) -> str:
    if not sha256:
        return "Sem sha256 não dá para guardar resumo: é ele que diz quando expirar."
    con.execute(
        """INSERT INTO memoria_resumo (sha256, arquivo_id, escopo, resumo, criado_em)
           VALUES (?,?,?,?,?)
           ON CONFLICT(sha256) DO UPDATE SET
             resumo = excluded.resumo, criado_em = excluded.criado_em,
             arquivo_id = COALESCE(excluded.arquivo_id, memoria_resumo.arquivo_id)""",
        (sha256, arquivo_id, escopo, resumo, agora_iso()),
    )
    con.commit()
    return f"Resumo guardado para {sha256[:12]}… ({len(resumo)} chars)"


def buscar_resumo(con: sqlite3.Connection, sha256: str) -> str | None:
    """Resumo válido para este conteúdo, ou None se o arquivo mudou."""
    r = con.execute(
        "SELECT resumo FROM memoria_resumo WHERE sha256 = ?", (sha256,)
    ).fetchone()
    return r["resumo"] if r else None


def resumo_de_arquivo(con: sqlite3.Connection, arquivo_id: int) -> str | None:
    """Resumo pelo id do arquivo, respeitando o hash atual.

    Passa pelo `sha256` de propósito: se o arquivo foi rebaixado com conteúdo
    novo, o hash mudou e o resumo antigo não é servido.
    """
    r = con.execute(
        "SELECT sha256 FROM arquivos WHERE id = ?", (arquivo_id,)
    ).fetchone()
    return buscar_resumo(con, r["sha256"]) if r and r["sha256"] else None


def estado_do_resumo(con: sqlite3.Connection, arquivo_id: int) -> tuple[str, str | None]:
    """(estado, resumo). Estado: 'valido' | 'desatualizado' | 'nunca'.

    Separa dois casos que a mensagem antiga juntava. "Nunca resumi" e "resumi
    mas o professor republicou" pedem ações diferentes: a segunda diz que vale
    a pena refazer, porque o trabalho já foi feito uma vez sobre este material.
    """
    r = con.execute(
        "SELECT sha256 FROM arquivos WHERE id = ?", (arquivo_id,)
    ).fetchone()
    if not r or not r["sha256"]:
        return "nunca", None
    atual = buscar_resumo(con, r["sha256"])
    if atual:
        return "valido", atual
    velho = con.execute(
        "SELECT resumo FROM memoria_resumo WHERE arquivo_id = ? LIMIT 1", (arquivo_id,)
    ).fetchone()
    return ("desatualizado", velho["resumo"]) if velho else ("nunca", None)


# --------------------------------------------------------------------------
# Cobertura: o que já foi produzido, e com que material
# --------------------------------------------------------------------------
#
# É o maior ganho medido do projeto. Saber que um documento já existe economiza
# de 60% a 92% de token; escolher o melhor método de busca economiza 21%.
#
# Chaveado pelo sha do TEXTO do trecho, nunca pelo id: o id é reciclado a cada
# `extrair --reindexar`, e isso já invalidou 172 referências de uma vez. Mesma
# escolha que `vetores` faz, pelo mesmo motivo.


def _adotar_orfao(con: sqlite3.Connection, cid: int, escopo: str, rotulo: str) -> None:
    """Puxa para o curso `cid` o documento de mesmo rótulo que ficou sem curso.

    Um documento nasce sem curso quando nada no conteúdo dele o identificava
    ainda. Quando o curso aparece depois — a cobertura passou a sair das
    citações, e elas dizem de que matéria é —, o registro velho tem de virar o
    mesmo documento, e não um segundo com o mesmo nome.
    """
    orfao = con.execute(
        "SELECT id FROM produzido WHERE courseid = 0 AND escopo = ? AND rotulo = ?",
        (escopo, rotulo),
    ).fetchone()
    if not orfao:
        return
    novo = con.execute(
        "SELECT id FROM produzido WHERE courseid = ? AND escopo = ? AND rotulo = ?",
        (cid, escopo, rotulo),
    ).fetchone()
    if novo is None:
        # Ninguém ocupa o lugar ainda: basta promover a linha que existe, e
        # assim a data de criação e o caminho originais são preservados.
        con.execute("UPDATE produzido SET courseid = ? WHERE id = ?", (cid, orfao["id"]))
    else:
        con.execute(
            "INSERT OR IGNORE INTO cobertura (produzido_id, trecho_sha, em) "
            "SELECT ?, trecho_sha, em FROM cobertura WHERE produzido_id = ?",
            (novo["id"], orfao["id"]),
        )
        con.execute(
            "UPDATE produzido SET caminho = COALESCE(caminho, "
            "(SELECT caminho FROM produzido WHERE id = ?)) WHERE id = ?",
            (orfao["id"], novo["id"]),
        )
        con.execute("DELETE FROM produzido WHERE id = ?", (orfao["id"],))
    con.commit()


def registrar_cobertura(
    con: sqlite3.Connection,
    courseid: int | None,
    escopo: str,
    rotulo: str,
    shas: Iterable[str],
    site: str | None = None,
    caminho: str | None = None,
) -> int:
    """Registra que estes trechos entraram num documento. Devolve o id.

    Chamado pela própria ferramenta que devolveu o material, não pelo modelo:
    pedir contabilidade ao modelo custa token e ele esquece.
    """
    # Sentinela 0, nunca NULL: com NULL o UNIQUE não valia e o ON CONFLICT
    # abaixo nunca disparava, criando uma linha nova a cada remontagem.
    cid = int(courseid or 0)
    if cid:
        _adotar_orfao(con, cid, escopo, rotulo)
    con.execute(
        """INSERT INTO produzido (site, courseid, escopo, rotulo, caminho, criado_em)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(courseid, escopo, rotulo) DO UPDATE SET
             caminho = COALESCE(excluded.caminho, produzido.caminho),
             site    = COALESCE(excluded.site, produzido.site)""",
        (site, cid, escopo, rotulo, caminho, agora_iso()),
    )
    # `lastrowid` mente no caminho do DO UPDATE (devolve o rowid da tentativa,
    # não o da linha que ficou), então a leitura é sempre explícita.
    r = con.execute(
        "SELECT id FROM produzido WHERE courseid = ? AND escopo = ? AND rotulo = ?",
        (cid, escopo, rotulo),
    ).fetchone()
    pid = r["id"] if r else None
    if pid:
        con.executemany(
            "INSERT OR IGNORE INTO cobertura (produzido_id, trecho_sha, em) VALUES (?,?,?)",
            [(pid, s, agora_iso()) for s in shas],
        )
    # Fora do `if shas`: registrar que o documento existe, mesmo sem nenhum
    # trecho identificado, já é metade do ganho — é o que responde "isso eu
    # já montei".
    con.commit()
    return int(pid or 0)


def shas_cobertos(
    con: sqlite3.Connection, courseid: int | None, escopo: str, rotulo: str
) -> set[str]:
    """Trechos que já entraram neste documento. Vazio se ele não existe."""
    return {
        r["trecho_sha"]
        for r in con.execute(
            """SELECT c.trecho_sha FROM cobertura c
               JOIN produzido p ON p.id = c.produzido_id
               WHERE p.courseid = ? AND p.escopo = ? AND p.rotulo = ?""",
            (int(courseid or 0), escopo, rotulo),
        )
    }


def listar_produzido(con: sqlite3.Connection, courseid: int | None = None) -> list[dict]:
    """O que já foi montado, com quantos trechos cada um cobre."""
    sql = """SELECT p.*, (SELECT COUNT(*) FROM cobertura c WHERE c.produzido_id = p.id) n
             FROM produzido p"""
    params: list = []
    if courseid:
        sql += " WHERE p.courseid = ?"
        params.append(courseid)
    return [dict(r) for r in con.execute(sql + " ORDER BY p.criado_em DESC", params)]


# --------------------------------------------------------------------------
# Citação → trecho: a apostila diz o que usou, o banco confirma
# --------------------------------------------------------------------------
#
# `apostila.citacoes` devolve ("Cap 07 rev", 26) lendo o HTML montado. Aqui
# esse rótulo vira um arquivo do banco e a página vira os shas dos trechos
# daquela página. É assim que a cobertura de uma apostila é REGISTRADA pela
# ferramenta, e não pedida ao modelo — que esquece e custa token.
#
# O rótulo citado é sempre mais curto que o nome do arquivo ("Cap 06" contra
# "Aula 08 09 ECN300 2026-02 Cap 06.pdf"), então o casamento é por conteúdo.


def _achatar(texto: str) -> str:
    """Para casar rótulo citado com nome de arquivo: sem acento, sem caixa.

    O que está entre parênteses sai fora: a citação às vezes explica qual
    versão é ("Cap 07 (versão sem \"rev\")") e essa explicação não está no
    nome do arquivo.
    """
    sem_parenteses = re.sub(r"\([^)]*\)", " ", texto)
    return re.sub(r"\s+", " ", normalizar(sem_parenteses)).strip()


def resolver_arquivo(
    con: sqlite3.Connection, rotulo: str, courseid: int | None = None
) -> int | None:
    """Arquivo cujo nome contém o rótulo citado. `None` se não der para decidir.

    Ambiguidade é resolvida pelo nome mais curto, e só por ele: "Cap 07" cabe
    tanto em "... Cap 07.pdf" quanto em "... Cap 07 rev.pdf", e quem citou
    "Cap 07" seco queria o primeiro — quem queria o outro teria escrito "rev".
    Empate no comprimento devolve `None`: errar a fonte é pior que não achar.
    """
    alvo = _achatar(rotulo)
    if len(alvo) < 3:  # "p", "6" — casaria com meio corpus
        return None
    sql = "SELECT id, nome, courseid FROM arquivos WHERE ignorado = 0"
    params: list = []
    if courseid:
        sql += " AND courseid = ?"
        params.append(courseid)
    candidatos = [
        r for r in con.execute(sql, params) if alvo in _achatar(r["nome"] or "")
    ]
    if not candidatos:
        return None
    if len(candidatos) == 1:
        return int(candidatos[0]["id"])
    candidatos.sort(key=lambda r: len(r["nome"] or ""))
    if len(candidatos[0]["nome"] or "") == len(candidatos[1]["nome"] or ""):
        return None
    return int(candidatos[0]["id"])


def resolver_citacoes(
    con: sqlite3.Connection,
    citacoes: Iterable[tuple[str, int]],
    courseid: int | None = None,
) -> tuple[set[str], int | None, list[str]]:
    """(shas cobertos, curso inferido, rótulos que não resolveram).

    O curso sai por maioria entre os arquivos citados: uma apostila de prova
    fala de uma matéria só, e assim `montar_apostila` não precisa que o modelo
    informe o curso — informação que ele erraria de vez em quando.
    """
    shas: set[str] = set()
    cursos: dict[int, int] = {}
    perdidos: list[str] = []
    cache: dict[str, int | None] = {}
    for rotulo, pagina in citacoes:
        if rotulo not in cache:
            cache[rotulo] = resolver_arquivo(con, rotulo, courseid)
        aid = cache[rotulo]
        if aid is None:
            if rotulo not in perdidos:
                perdidos.append(rotulo)
            continue
        linhas = con.execute(
            "SELECT sha256 FROM trechos WHERE arquivo_id = ? AND pagina = ?",
            (aid, pagina),
        ).fetchall()
        shas.update(r["sha256"] for r in linhas)
        c = con.execute(
            "SELECT courseid FROM arquivos WHERE id = ?", (aid,)
        ).fetchone()
        if c and c["courseid"]:
            cursos[c["courseid"]] = cursos.get(c["courseid"], 0) + 1
    curso = max(cursos, key=lambda k: cursos[k]) if cursos else None
    return shas, curso, perdidos


def registrar_apostila(
    con: sqlite3.Connection,
    titulo: str,
    citacoes: Iterable[tuple[str, int]],
    caminho: str | None = None,
    courseid: int | None = None,
) -> tuple[int, int, list[str]]:
    """Registra a apostila e a cobertura que as citações dela revelam.

    Devolve (id, trechos cobertos, rótulos não resolvidos). Um só lugar porque
    o servidor MCP e o `cli.py apostila` montam a mesma coisa — e antes só o
    servidor registrava, então toda apostila feita pelo CLI sumia do índice
    do que já foi produzido.
    """
    shas, curso, perdidos = resolver_citacoes(con, citacoes, courseid)
    pid = registrar_cobertura(
        con, courseid or curso, "apostila", titulo, shas, caminho=caminho
    )
    return pid, len(shas), perdidos


def listar_resumos(con: sqlite3.Connection, courseid: int | None = None) -> list[dict]:
    """Resumos guardados que ainda valem (o arquivo não mudou desde então).

    Não existia jeito de perguntar "o que eu já resumi" sem SQL cru.
    """
    sql = """SELECT m.sha256, m.criado_em, LENGTH(m.resumo) tamanho,
                    a.id AS arquivo_id, a.nome, a.courseid
             FROM memoria_resumo m JOIN arquivos a ON a.sha256 = m.sha256"""
    params: list = []
    if courseid:
        sql += " WHERE a.courseid = ?"
        params.append(courseid)
    return [dict(r) for r in con.execute(sql + " ORDER BY a.courseid, a.nome", params)]


def herdeiros_de_resumo(con: sqlite3.Connection, courseid: int) -> list[dict]:
    """Arquivos sem resumo que compartilham conteúdo com um que tem.

    Usa a aresta `conteudo` do grafo, que só liga conteúdo de verdade desde que
    capa e cabeçalho passaram a ser excluídos da construção. Antes disso, em
    ECN300 todo arquivo era vizinho de todo arquivo pelo timbre do departamento,
    e herdar aqui teria espalhado resumo errado pelo curso inteiro.
    """
    com = {
        r["id"]
        for r in con.execute(
            """SELECT a.id FROM arquivos a JOIN memoria_resumo m ON m.sha256 = a.sha256
               WHERE a.courseid = ?""",
            (courseid,),
        )
    }
    if not com:
        return []
    saida = []
    for r in con.execute(
        """SELECT g.a, g.b, g.peso, na.nome AS nome_a, nb.nome AS nome_b
           FROM grafo_documentos g
           JOIN arquivos na ON na.id = g.a JOIN arquivos nb ON nb.id = g.b
           WHERE g.courseid = ? AND g.tipo = 'conteudo'""",
        (courseid,),
    ):
        for fonte, alvo, nome in ((r["a"], r["b"], r["nome_b"]), (r["b"], r["a"], r["nome_a"])):
            if fonte in com and alvo not in com:
                saida.append({"fonte": fonte, "arquivo_id": alvo, "nome": nome,
                              "peso": round(r["peso"], 3)})
    return saida


def fonte_de_resumo_irmao(con: sqlite3.Connection, arquivo_id: int) -> list[dict]:
    """Arquivos com resumo que compartilham conteúdo com este. Inverso de
    `herdeiros_de_resumo`, para responder "alguém já resumiu isto por mim?".
    """
    return [
        {"fonte": r["outro"], "nome": r["nome"], "peso": round(r["peso"], 3)}
        for r in con.execute(
            """SELECT CASE WHEN g.a = ? THEN g.b ELSE g.a END AS outro,
                      g.peso, a.nome
               FROM grafo_documentos g
               JOIN arquivos a ON a.id = CASE WHEN g.a = ? THEN g.b ELSE g.a END
               JOIN memoria_resumo m ON m.sha256 = a.sha256
               WHERE (g.a = ? OR g.b = ?) AND g.tipo = 'conteudo'
               ORDER BY g.peso DESC""",
            (arquivo_id, arquivo_id, arquivo_id, arquivo_id),
        )
    ]

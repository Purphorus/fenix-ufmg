"""Sincroniza turmas, materiais e eventos do Moodle para o banco local.

Regra central: nada é sobrescrito em silêncio. Toda mudança de data vira uma
linha em historico_eventos, e evento confirmado por você nunca é alterado por
uma fonte automática.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import db
from db import MATERIAIS_DIR, marcar_notificado  # noqa: F401  (uso futuro)
from moodle_client import MoodleClient, MoodleError, get_client

# Confiança das fontes de data. Maior vence.
PESO_ORIGEM = {"manual": 5, "calendario": 4, "forum": 3, "programa_pdf": 2}

EXT_MATERIAL = {".pdf", ".pptx", ".ppt", ".docx", ".doc", ".txt", ".md", ".ipynb"}

# O endpoint de calendário do Moodle aceita no máximo 50 por chamada; o resto
# vem por paginação (aftereventid). O teto de páginas evita laço infinito se a
# API devolver sempre cheio.
LIMITE_CALENDARIO = 50
PAGINAS_CALENDARIO = 20


def agora_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def slug(texto: str, limite: int = 60) -> str:
    texto = re.sub(r"[^\w\s.-]", "", texto or "", flags=re.UNICODE).strip()
    texto = re.sub(r"\s+", "_", texto)
    return texto[:limite] or "sem_nome"


@dataclass
class Novidade:
    tipo: str  # arquivo_novo | arquivo_alterado | evento_novo | evento_mudou
    descricao: str
    curso: str = ""
    ref: str = ""


@dataclass
class ResultadoSync:
    site: str
    cursos: int = 0
    arquivos_novos: int = 0
    arquivos_alterados: int = 0
    eventos_novos: int = 0
    eventos_mudados: int = 0
    cursos_sem_mudanca: int = 0   # pulados porque updates_since disse "nada"
    avisos_novos: int = 0         # só os de pessoa; automático não conta
    erros: list[str] = field(default_factory=list)
    novidades: list[Novidade] = field(default_factory=list)


# --------------------------------------------------------------------------
# Cursos
# --------------------------------------------------------------------------


def texto_limpo(bruto: str | None, limite: int = 600) -> str:
    """HTML de mensagem do Moodle -> texto de uma linha, cortado."""
    t = re.sub(r"<br\s*/?>|</p>", " ", bruto or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limite else t[:limite].rstrip() + " …"


def sync_professores(
    con: sqlite3.Connection, cli: MoodleClient, ids: list[int], res: ResultadoSync
) -> None:
    """Quem é professor de cada turma. Uma chamada para todas.

    É o que separa aviso de conversa: mensagem direta do professor da turma é
    onde, medido, os avisos reais chegam ("aulas agora no Laboratório 1102").
    """
    if not ids:
        return
    try:
        r = cli.call(
            "core_course_get_courses_by_field",
            field="ids", value=",".join(str(i) for i in ids),
        )
    except MoodleError as e:
        res.erros.append(f"professores: {e}")
        return
    for c in r.get("courses", []) or []:
        profs = sorted(x["id"] for x in c.get("contacts", []) or [] if x.get("id"))
        con.execute(
            "UPDATE cursos SET professores = ? WHERE site = ? AND courseid = ?",
            (json.dumps(profs), cli.alias, c["id"]),
        )
    con.commit()


def sync_cursos(con: sqlite3.Connection, cli: MoodleClient) -> list[dict]:
    cursos = cli.call("core_enrol_get_users_courses", userid=cli.user_id())
    for c in cursos:
        con.execute(
            """INSERT INTO cursos (site, courseid, fullname, shortname, visto_em)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(site, courseid) DO UPDATE SET
                 fullname = excluded.fullname,
                 shortname = excluded.shortname,
                 visto_em = excluded.visto_em""",
            (cli.alias, c["id"], c.get("fullname"), c.get("shortname"), agora_iso()),
        )
    con.commit()
    ativos = {
        r["courseid"]
        for r in con.execute(
            "SELECT courseid FROM cursos WHERE site = ? AND acompanhar = 1",
            (cli.alias,),
        )
    }
    return [c for c in cursos if c["id"] in ativos]


# --------------------------------------------------------------------------
# Materiais
# --------------------------------------------------------------------------


def _registrar_arquivo(
    con: sqlite3.Connection,
    cli: MoodleClient,
    res: ResultadoSync,
    conteudo: dict,
    *,
    courseid: int,
    nome_curso: str,
    cmid: int | None,
    secao: str | None,
    modname: str | None,
    baixar: bool,
) -> None:
    """Registra/baixa um arquivo do Moodle (`fileurl`, `filename`, `filesize`,
    `timemodified`) — seja conteúdo de seção, seja anexo de aviso no fórum."""
    fileurl = conteudo.get("fileurl")
    if not fileurl:
        return
    nome = conteudo.get("filename") or "arquivo"
    if Path(nome).suffix.lower() not in EXT_MATERIAL:
        return

    anterior = con.execute(
        "SELECT * FROM arquivos WHERE site = ? AND fileurl = ?",
        (cli.alias, fileurl),
    ).fetchone()

    tm = conteudo.get("timemodified")
    tam = conteudo.get("filesize")
    mudou = anterior is not None and (
        anterior["timemodified"] != tm or anterior["filesize"] != tam
    )
    # Registrado numa passada com --sem-download (ou com o arquivo
    # local apagado) fica sem caminho_local. Sem esta checagem ele
    # nunca mais seria baixado: os metadados não mudaram, então o
    # sync o pularia para sempre — e você ficaria sem o material,
    # em silêncio.
    falta_local = baixar and anterior is not None and (
        not anterior["caminho_local"]
        or not Path(anterior["caminho_local"]).is_file()
    )
    if anterior is not None and not mudou and not falta_local:
        con.execute(
            "UPDATE arquivos SET visto_em = ? WHERE id = ?",
            (agora_iso(), anterior["id"]),
        )
        return

    destino = (
        MATERIAIS_DIR
        / slug(cli.alias)
        / slug(nome_curso)
        / slug(secao or "geral")
        / nome
    )
    sha = None
    caminho = None
    if baixar:
        try:
            cli.download(fileurl, destino)
            sha = hashlib.sha256(destino.read_bytes()).hexdigest()
            caminho = str(destino)
        except Exception as e:  # rede, permissão, arquivo removido
            res.erros.append(f"download {nome}: {e}")

    if anterior is None:
        con.execute(
            """INSERT INTO arquivos
               (site, courseid, cmid, secao, modname, nome, fileurl,
                filesize, timemodified, sha256, caminho_local,
                baixado_em, visto_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cli.alias, courseid, cmid, secao, modname, nome, fileurl,
                tam, tm, sha, caminho, agora_iso(), agora_iso(),
            ),
        )
        res.arquivos_novos += 1
        res.novidades.append(
            Novidade("arquivo_novo", nome, nome_curso, caminho or fileurl)
        )
        return

    if sha and sha == anterior["sha256"] and not falta_local:
        # republicado sem alteração real de conteúdo
        con.execute(
            "UPDATE arquivos SET timemodified=?, filesize=?, visto_em=? WHERE id=?",
            (tm, tam, agora_iso(), anterior["id"]),
        )
        return
    con.execute(
        """UPDATE arquivos SET filesize=?, timemodified=?, sha256=?,
              caminho_local=?, baixado_em=?, visto_em=?, texto_path=NULL
           WHERE id=?""",
        (tam, tm, sha, caminho, agora_iso(), agora_iso(), anterior["id"]),
    )
    # Baixar pela primeira vez algo que só estava registrado não
    # é "alterado": nada mudou no Moodle, faltava o download.
    if falta_local and not mudou:
        res.arquivos_novos += 1
        res.novidades.append(
            Novidade("arquivo_baixado", nome, nome_curso, caminho or fileurl)
        )
    else:
        res.arquivos_alterados += 1
        res.novidades.append(
            Novidade("arquivo_alterado", nome, nome_curso, caminho or fileurl)
        )


def sync_materiais(
    con: sqlite3.Connection,
    cli: MoodleClient,
    curso: dict,
    res: ResultadoSync,
    baixar: bool = True,
) -> None:
    """Percorre as seções do curso e registra/baixa arquivos novos ou alterados.

    Detecção de mudança: (timemodified, filesize). O `contenthash` nem sempre
    vem em core_course_get_contents, então o hash real é calculado localmente
    após o download, e serve para deduplicar o mesmo PDF republicado.
    """
    try:
        secoes = cli.call("core_course_get_contents", courseid=curso["id"])
    except MoodleError as e:
        res.erros.append(f"{curso.get('shortname')}: {e}")
        return

    nome_curso = curso.get("shortname") or str(curso["id"])
    # Vazio na primeira passada: aí toda atividade seria "nova" e o briefing
    # viraria o catálogo inteiro do semestre.
    conhecidos = {
        r["cmid"] for r in con.execute(
            "SELECT cmid FROM modulos WHERE site = ? AND courseid = ?",
            (cli.alias, curso["id"]),
        )
    }
    for secao in secoes:
        for mod in secao.get("modules", []):
            cmid = mod.get("id")
            if cmid is not None:
                con.execute(
                    """INSERT INTO modulos (site, courseid, cmid, modname, nome, secao, visto_em)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(site, cmid) DO UPDATE SET
                         modname = excluded.modname, nome = excluded.nome,
                         secao = excluded.secao, visto_em = excluded.visto_em""",
                    (cli.alias, curso["id"], cmid, mod.get("modname"),
                     mod.get("name"), secao.get("name"), agora_iso()),
                )
                # Arquivo novo já é contado abaixo, como arquivo; aqui entra o
                # que não tem arquivo — questionário, tarefa, página.
                if (conhecidos and cmid not in conhecidos
                        and mod.get("modname") not in ("resource", "folder", "label")):
                    res.novidades.append(Novidade(
                        "atividade_nova", f"[{mod.get('modname')}] {mod.get('name')}",
                        nome_curso, str(cmid),
                    ))
            for conteudo in mod.get("contents", []) or []:
                if conteudo.get("type") != "file":
                    continue
                _registrar_arquivo(
                    con, cli, res, conteudo,
                    courseid=curso["id"], nome_curso=nome_curso, cmid=mod.get("id"),
                    secao=secao.get("name"), modname=mod.get("modname"), baixar=baixar,
                )
    con.commit()


# --------------------------------------------------------------------------
# Eventos
# --------------------------------------------------------------------------


def classificar(nome: str, modulo: str | None, eventtype: str | None = None) -> str:
    """prova | entrega | aula | outro.

    `eventtype` vem do calendário do Moodle: 'due'/'close' é prazo de verdade,
    'open' é só a data em que a atividade abriu — tratar os dois como entrega
    encheria a agenda de datas que não exigem nada de você.
    """
    n = (nome or "").lower()
    if any(p in n for p in ("prova", "avaliaç", "exame", "teste")):
        return "prova"
    if eventtype in ("due", "close"):
        return "entrega"
    if eventtype == "open":
        return "outro"
    if modulo in ("assign", "quiz") or "entrega" in n:
        return "entrega"
    return "outro"


def upsert_evento(
    con: sqlite3.Connection,
    site: str,
    *,
    titulo: str,
    data_inicio: int | None,
    origem: str,
    courseid: int | None = None,
    chave_externa: str | None = None,
    tipo: str | None = None,
    origem_ref: str | None = None,
    trecho: str | None = None,
    confirmado: int = 0,
) -> tuple[int, str]:
    """Insere ou atualiza um evento. Devolve (id, acao) com acao em
    {criado, atualizado, ignorado}."""
    atual = None
    if chave_externa:
        atual = con.execute(
            "SELECT * FROM eventos WHERE site = ? AND chave_externa = ?",
            (site, chave_externa),
        ).fetchone()
    else:
        # Sem chave do Moodle (evento manual ou extraído do programa), a
        # identidade é (site, curso, título). Sem isso, rodar `evento add` ou
        # o extrator duas vezes criaria duplicatas silenciosas.
        atual = con.execute(
            """SELECT * FROM eventos
               WHERE site = ? AND titulo = ? AND cancelado = 0
                 AND courseid IS ? AND chave_externa IS NULL""",
            (site, titulo, courseid),
        ).fetchone()

    if atual is None:
        cur = con.execute(
            """INSERT INTO eventos
               (site, courseid, chave_externa, titulo, tipo, data_inicio, origem,
                origem_ref, trecho_origem, confirmado, criado_em, atualizado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                site, courseid, chave_externa, titulo,
                tipo or classificar(titulo, None), data_inicio, origem,
                origem_ref, trecho, confirmado, agora_iso(), agora_iso(),
            ),
        )
        con.commit()
        return int(cur.lastrowid), "criado"

    # Não deixa fonte automática sobrescrever o que você confirmou na mão.
    if atual["confirmado"] and origem != "manual":
        return int(atual["id"]), "ignorado"
    if PESO_ORIGEM.get(origem, 0) < PESO_ORIGEM.get(atual["origem"], 0):
        return int(atual["id"]), "ignorado"

    if atual["data_inicio"] != data_inicio and data_inicio is not None:
        con.execute(
            """INSERT INTO historico_eventos
               (evento_id, campo, valor_antigo, valor_novo, origem, em)
               VALUES (?,?,?,?,?,?)""",
            (atual["id"], "data_inicio", atual["data_inicio"], data_inicio,
             origem, agora_iso()),
        )
        con.execute(
            "UPDATE eventos SET data_inicio=?, origem=?, origem_ref=?, atualizado_em=? WHERE id=?",
            (data_inicio, origem, origem_ref, agora_iso(), atual["id"]),
        )
        con.commit()
        return int(atual["id"]), "atualizado"

    con.execute(
        "UPDATE eventos SET titulo=?, atualizado_em=? WHERE id=?",
        (titulo, agora_iso(), atual["id"]),
    )
    con.commit()
    return int(atual["id"]), "ignorado"


def sync_eventos(
    con: sqlite3.Connection, cli: MoodleClient, res: ResultadoSync, dias: int = 120
) -> None:
    agora = int(time.time())
    inicio, fim = agora - 30 * 86400, agora + dias * 86400

    # Duas fontes, porque nenhuma sozinha dá o calendário inteiro:
    #
    # get_calendar_events   — tudo que está no calendário do curso, inclusive
    #                         prazo já vencido e evento informativo. É a fonte
    #                         completa, e a que o aluno vê na tela.
    # action_events_by_timesort — só o que ainda exige ação sua. Perde o
    #                         restante, mas pega eventos de usuário/site que não
    #                         estão presos a um curso.
    #
    # As duas devolvem o mesmo id de evento, então `cal:<id>` deduplica sozinho.
    eventos: list[dict] = []

    cursos_ids = [
        r["courseid"]
        for r in con.execute(
            "SELECT courseid FROM cursos WHERE site = ? AND acompanhar = 1",
            (cli.alias,),
        )
    ]
    if cursos_ids:
        try:
            dados = cli.call(
                "core_calendar_get_calendar_events",
                events={"courseids": cursos_ids},
                options={
                    "timestart": inicio, "timeend": fim,
                    "userevents": 1, "siteevents": 1,
                },
            )
            eventos.extend(dados.get("events", []) or [])
        except MoodleError as e:
            res.erros.append(f"calendário (completo): {e}")

    # limitnum > 50 é recusado por este Moodle; daí a paginação por aftereventid
    #
    # Esta fonte só lista o que ainda exige ação sua, e é exatamente isso que
    # marca `pendente_acao`: entrega vencida que você já fez não é atraso.
    acoes: set[str] = set()
    acoes_inteiras = True
    depois_de: int | None = None
    for _ in range(PAGINAS_CALENDARIO):
        params = {
            "timesortfrom": inicio,
            "timesortto": fim,
            "limitnum": LIMITE_CALENDARIO,
        }
        if depois_de is not None:
            params["aftereventid"] = depois_de
        try:
            dados = cli.call("core_calendar_get_action_events_by_timesort", **params)
        except MoodleError as e:
            res.erros.append(f"calendário (ações): {e}")
            acoes_inteiras = False
            break
        pagina = dados.get("events", []) or []
        eventos.extend(pagina)
        acoes.update(f"cal:{ev.get('id')}" for ev in pagina)
        if len(pagina) < LIMITE_CALENDARIO:
            break
        depois_de = pagina[-1].get("id")
        if depois_de is None:
            break

    # Com a lista de ações incompleta, zerar a marca faria entregas pendentes
    # sumirem do briefing em silêncio. Melhor ficar com a marca da última vez.
    if acoes_inteiras:
        con.execute(
            "UPDATE eventos SET pendente_acao = 0 WHERE site = ? AND origem = 'calendario'",
            (cli.alias,),
        )

    for ev in eventos:
        curso = ev.get("course") or {}
        # get_calendar_events usa timestart; action_events usa timesort
        quando = ev.get("timestart") or ev.get("timesort")
        _id, acao = upsert_evento(
            con,
            cli.alias,
            titulo=ev.get("name") or "(sem título)",
            data_inicio=quando,
            origem="calendario",
            courseid=curso.get("id") or ev.get("courseid"),
            chave_externa=f"cal:{ev.get('id')}",
            tipo=classificar(
                ev.get("name"), ev.get("modulename"), ev.get("eventtype")
            ),
            origem_ref=ev.get("url"),
        )
        if acao == "criado":
            res.eventos_novos += 1
            res.novidades.append(
                Novidade("evento_novo", ev.get("name", ""), curso.get("shortname", ""))
            )
        elif acao == "atualizado":
            res.eventos_mudados += 1
            res.novidades.append(
                Novidade("evento_mudou", ev.get("name", ""), curso.get("shortname", ""))
            )
        if acoes_inteiras and f"cal:{ev.get('id')}" in acoes:
            con.execute("UPDATE eventos SET pendente_acao = 1 WHERE id = ?", (_id,))
    con.commit()


# --------------------------------------------------------------------------
# Mudanças, conclusão e avisos
# --------------------------------------------------------------------------

# Mesmo sem mudança anunciada, a estrutura do curso é baixada inteira uma vez
# por dia. `updates_since` é o Moodle falando dele mesmo; se ele deixar escapar
# algo (visibilidade trocada, atividade restaurada), o erro dura um dia e não o
# semestre.
COMPLETO_A_CADA = 24 * 3600

# Mudança que é você agindo, não o professor: marcar atividade como concluída
# aparece em updates_since e não é novidade para ninguém.
_MUDANCA_PROPRIA = {"completion"}


def mudancas_desde(
    con: sqlite3.Connection, cli: MoodleClient, curso: dict, res: ResultadoSync
) -> list[dict] | None:
    """O que mudou na turma desde o último sync completo dela.

    None = não dá para saber (primeira vez, passou um dia, erro): faça completo.
    Lista vazia = o Moodle diz que nada mudou, e a turma pode ser pulada.
    """
    r = con.execute(
        "SELECT sincronizado_em FROM cursos WHERE site = ? AND courseid = ?",
        (cli.alias, curso["id"]),
    ).fetchone()
    desde = r["sincronizado_em"] if r else None
    if not desde or time.time() - desde > COMPLETO_A_CADA:
        return None
    try:
        dados = cli.call(
            "core_course_get_updates_since", courseid=curso["id"], since=int(desde)
        )
    except MoodleError as e:
        res.erros.append(f"{curso.get('shortname')}: mudanças: {e}")
        return None
    return [
        i for i in dados.get("instances", []) or []
        if {u.get("name") for u in i.get("updates", []) or []} - _MUDANCA_PROPRIA
    ]


def registrar_mudancas(
    con: sqlite3.Connection, cli: MoodleClient, instancias: list[dict]
) -> None:
    for inst in instancias:
        if inst.get("contextlevel") != "module":
            continue
        ups = inst.get("updates", []) or []
        nomes = sorted({u["name"] for u in ups if u.get("name")} - _MUDANCA_PROPRIA)
        # `contentfiles` vem sem timeupdated; aí vale a hora em que soubemos.
        quando = max((u.get("timeupdated") or 0 for u in ups), default=0) or int(time.time())
        con.execute(
            "UPDATE modulos SET mudou_em = ?, mudou_o_que = ? WHERE site = ? AND cmid = ?",
            (quando, ", ".join(nomes), cli.alias, inst.get("id")),
        )
    con.commit()


def sync_conclusao(
    con: sqlite3.Connection, cli: MoodleClient, curso: dict, res: ResultadoSync
) -> None:
    """Rastreio de conclusão do Moodle -> modulos.concluido.

    Para arquivo o valor não significa leitura (o download pela API não marca
    visualização); quem lê a coluna filtra por tipo, veja `_EXIGE_ACAO` no
    servidor.
    """
    try:
        dados = cli.call(
            "core_completion_get_activities_completion_status",
            courseid=curso["id"], userid=cli.user_id(),
        )
    except MoodleError as e:
        res.erros.append(f"{curso.get('shortname')}: conclusão: {e}")
        return
    for s in dados.get("statuses", []) or []:
        # state: 0 incompleto, 1 completo, 2 completo e aprovado, 3 completo e
        # reprovado. Reprovado ainda é feito — não é pendência.
        valor = (1 if s.get("state") else 0) if s.get("tracking") else None
        con.execute(
            "UPDATE modulos SET concluido = ? WHERE site = ? AND cmid = ?",
            (valor, cli.alias, s.get("cmid")),
        )
    con.commit()


# Quantas mensagens por chamada. Cinquenta cobre semanas de uso normal; o que
# passar disso já foi lido no Moodle e continua lá.
AVISOS_POR_CHAMADA = 50


def _curso_do_link(con: sqlite3.Connection, site: str, url: str | None) -> int | None:
    """Notificação traz o link da atividade; o catálogo diz de que turma ela é."""
    if not url:
        return None
    m = re.search(r"/mod/\w+/view\.php\?id=(\d+)", url)
    if m:
        r = con.execute(
            "SELECT courseid FROM modulos WHERE site = ? AND cmid = ?", (site, int(m.group(1)))
        ).fetchone()
        return r["courseid"] if r else None
    m = re.search(r"/course/view\.php\?id=(\d+)", url)
    return int(m.group(1)) if m else None


def _guardar_aviso(
    con: sqlite3.Connection, site: str, res: ResultadoSync, nomes: dict, **campos
) -> None:
    novo = con.execute(
        "SELECT 1 FROM avisos WHERE site = ? AND chave = ?", (site, campos["chave"])
    ).fetchone() is None
    con.execute(
        """INSERT INTO avisos (site, chave, origem, courseid, autor, assunto, texto,
                               url, prioridade, criado_em, lido_moodle)
           VALUES (:site, :chave, :origem, :courseid, :autor, :assunto, :texto,
                   :url, :prioridade, :criado_em, :lido_moodle)
           ON CONFLICT(site, chave) DO UPDATE SET
             lido_moodle = excluded.lido_moodle,
             courseid = COALESCE(avisos.courseid, excluded.courseid)""",
        {"site": site, **campos},
    )
    if novo and campos["prioridade"] >= 1:
        res.avisos_novos += 1
        res.novidades.append(Novidade(
            "aviso_novo", f"{campos['autor']}: {(campos['texto'] or '')[:80]}",
            nomes.get(campos["courseid"], ""),
        ))


def sync_avisos(con: sqlite3.Connection, cli: MoodleClient, res: ResultadoSync) -> None:
    """Mensagens e notificações -> avisos.

    Medido: nas três matérias o fórum de avisos estava vazio e os avisos reais
    tinham chegado como mensagem direta do professor. Sem isto, "aula mudou de
    sala" nunca aparecia no briefing.
    """
    profs: dict[int, int] = {}
    nomes: dict[int | None, str] = {}
    for r in con.execute(
        "SELECT courseid, shortname, professores FROM cursos WHERE site = ? AND acompanhar = 1",
        (cli.alias,),
    ):
        nomes[r["courseid"]] = r["shortname"] or ""
        for p in json.loads(r["professores"] or "[]"):
            profs.setdefault(p, r["courseid"])
    uid = cli.user_id()

    # `read` só aceita 0 ou 1 neste Moodle, então são duas chamadas: sem a de
    # lidas, um aviso que você abriu no celular antes do sync nunca entraria.
    for lidas in (0, 1):
        try:
            dados = cli.call(
                "core_message_get_messages", useridto=uid, type="both", read=lidas,
                newestfirst=1, limitfrom=0, limitnum=AVISOS_POR_CHAMADA,
            )
        except MoodleError as e:
            res.erros.append(f"avisos: {e}")
            continue
        for m in dados.get("messages", []) or []:
            autor = m.get("useridfrom") or 0
            notif = bool(m.get("notification"))
            # Professor da turma = 2. Pessoa = 1. Remetente negativo é o
            # próprio Moodle (resumo de fórum, recibo de envio) = 0. A regra não
            # lista tipos de evento: o Moodle tem dezenas, e lista envelhece.
            prioridade = 2 if autor in profs else (1 if autor > 0 and autor != uid else 0)
            texto = texto_limpo(
                m.get("smallmessage") or m.get("text") or m.get("fullmessage")
            )
            _guardar_aviso(
                con, cli.alias, res, nomes,
                chave=f"{'notif' if notif else 'msg'}:{m.get('id')}",
                origem="notificacao" if notif else "mensagem",
                courseid=profs.get(autor) or _curso_do_link(con, cli.alias, m.get("contexturl")),
                autor=m.get("userfromfullname"),
                assunto=texto_limpo(m.get("subject"), 200) if notif else None,
                texto=texto,
                url=m.get("contexturl") or None,
                prioridade=prioridade,
                criado_em=m.get("timecreated"),
                lido_moodle=1 if m.get("timeread") else 0,
            )
    con.commit()


def sync_forum_avisos(
    con: sqlite3.Connection,
    cli: MoodleClient,
    ids: list[int],
    res: ResultadoSync,
    reler: set[int] | frozenset[int] = frozenset(),
    baixar: bool = True,
) -> None:
    """Fórum "Avisos" de cada turma, e os anexos das discussões -> arquivos.

    Só busca as discussões quando há mais do que o banco já tem, ou quando a
    turma está em `reler` (as que tiveram sync completo, ~1x por dia). Sem o
    reler, anexo trocado numa discussão antiga nunca seria visto.

    Medido em ECN299: o professor publica slides e o programa como ANEXO de
    aviso, com todas as seções da turma vazias. `core_course_get_contents` não
    enxerga anexo de fórum, então sem isto a matéria inteira ficava fora da busca.
    """
    if not ids:
        return
    try:
        foruns = cli.call("mod_forum_get_forums_by_courses", courseids=ids)
    except MoodleError as e:
        res.erros.append(f"fórum de avisos: {e}")
        return
    nomes = {
        r["courseid"]: r["shortname"] or ""
        for r in con.execute("SELECT courseid, shortname FROM cursos WHERE site = ?", (cli.alias,))
    }
    for f in foruns:
        if f.get("type") != "news" or not f.get("numdiscussions"):
            continue
        ja = con.execute(
            """SELECT COUNT(*) FROM avisos
               WHERE site = ? AND origem = 'forum_avisos' AND courseid = ?""",
            (cli.alias, f["course"]),
        ).fetchone()[0]
        if ja >= f["numdiscussions"] and f["course"] not in reler:
            continue
        try:
            d = cli.call(
                "mod_forum_get_forum_discussions", forumid=f["id"],
                sortorder=-1, page=0, perpage=10,
            )
        except MoodleError as e:
            res.erros.append(f"fórum de avisos {f['id']}: {e}")
            continue
        for disc in d.get("discussions", []) or []:
            _guardar_aviso(
                con, cli.alias, res, nomes,
                chave=f"forum:{disc.get('discussion')}",
                origem="forum_avisos",
                courseid=f["course"],
                autor=disc.get("userfullname"),
                assunto=texto_limpo(disc.get("name"), 200),
                texto=texto_limpo(disc.get("message")),
                url=f"{cli.url}/mod/forum/discuss.php?d={disc.get('discussion')}",
                prioridade=2,
                criado_em=disc.get("created") or disc.get("timemodified"),
                lido_moodle=0,
            )
            # Seção = "Avisos — <assunto>": separa no disco anexos homônimos de
            # discussões diferentes e diz na citação de onde o arquivo veio.
            for anexo in disc.get("attachments") or []:
                _registrar_arquivo(
                    con, cli, res, anexo,
                    courseid=f["course"], nome_curso=nomes.get(f["course"]) or str(f["course"]),
                    cmid=f.get("cmid"), modname="forum", baixar=baixar,
                    secao=f"Avisos — {texto_limpo(disc.get('name'), 80)}",
                )
    con.commit()


def _falta_baixar(con: sqlite3.Connection, site: str, courseid: int) -> bool:
    return con.execute(
        """SELECT 1 FROM arquivos WHERE site = ? AND courseid = ? AND ignorado = 0
             AND caminho_local IS NULL LIMIT 1""",
        (site, courseid),
    ).fetchone() is not None


# --------------------------------------------------------------------------


def sync_site(
    con: sqlite3.Connection, alias: str | None = None, baixar: bool = True
) -> ResultadoSync:
    cli = get_client(alias)
    res = ResultadoSync(site=cli.alias)
    inicio = agora_iso()
    arq = con.execute(
        "SELECT arquivado FROM semestres WHERE site = ?", (cli.alias,)
    ).fetchone()
    if arq and arq["arquivado"]:
        # Semestre arquivado tem token expirado; rodar todo dia só produz erro
        # que ninguém lê. O material continua na busca.
        res.erros.append(f"semestre {cli.alias} está arquivado — sync pulado")
        return res
    db.registrar_semestre(con, cli.alias)
    try:
        cursos = sync_cursos(con, cli)
        res.cursos = len(cursos)
        completos: list[int] = []
        for curso in cursos:
            comeco = int(time.time())
            instancias = mudancas_desde(con, cli, curso, res)
            if instancias == [] and not (baixar and _falta_baixar(con, cli.alias, curso["id"])):
                res.cursos_sem_mudanca += 1
                continue
            erros_antes = len(res.erros)
            sync_materiais(con, cli, curso, res, baixar=baixar)
            if instancias:
                registrar_mudancas(con, cli, instancias)
            sync_conclusao(con, cli, curso, res)
            completos.append(curso["id"])
            # Com erro no meio, a turma não é marcada: a próxima rodada refaz
            # completo em vez de perguntar "o que mudou desde" um sync que não
            # terminou.
            if len(res.erros) == erros_antes:
                con.execute(
                    "UPDATE cursos SET sincronizado_em = ? WHERE site = ? AND courseid = ?",
                    (comeco, cli.alias, curso["id"]),
                )
                con.commit()
        # Professor muda pouco: só relê para turma que teve sync completo, ou
        # que ainda não tem a lista.
        sem_lista = [
            r["courseid"] for r in con.execute(
                "SELECT courseid FROM cursos WHERE site = ? AND acompanhar = 1 AND professores IS NULL",
                (cli.alias,),
            )
        ]
        sync_professores(con, cli, sorted(set(completos) | set(sem_lista)), res)
        sync_eventos(con, cli, res)
        sync_avisos(con, cli, res)
        sync_forum_avisos(
            con, cli, [c["id"] for c in cursos], res, reler=set(completos), baixar=baixar
        )
    finally:
        con.execute(
            """INSERT INTO sync_log (site, iniciado_em, terminado_em, novidades, erro)
               VALUES (?,?,?,?,?)""",
            (
                res.site, inicio, agora_iso(), len(res.novidades),
                "; ".join(res.erros)[:2000] or None,
            ),
        )
        con.commit()
        cli.close()
    return res

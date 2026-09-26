"""CLI do UFMG Moodle Companion.

    python cli.py init
    python cli.py sync [--site 20262] [--sem-download]
    python cli.py turmas
    python cli.py agenda [--dias 30] [--todos]
    python cli.py evento add "Prova 1" --data 2026-04-15T14:00 --curso 12345
    python cli.py evento set 7 --data 2026-04-22T14:00
    python cli.py evento confirmar 7
    python cli.py evento pendentes
    python cli.py materiais [--curso 12345] [--novos]
    python cli.py extrair [--site 20262] [--refazer] [--reindexar]
    python cli.py buscar "estado estacionário" [--curso 6095]
    python cli.py programa [--curso 8025] [--tudo]
    python cli.py estudo preparar --tentativa 88 [--saida sessao.json]
    python cli.py estudo salvar --arquivo sessao.json
    python cli.py estudo enviar --arquivo sessao.json --confirmar
    python cli.py estudo corrigir --tentativa 88
    python cli.py estudo roteiro [--curso 12345]
    python cli.py estudo politica --curso 12345 --permitir --motivo "..."
    python cli.py estudo politicas
    python cli.py memoria lembrar "macro" --curso 6095
    python cli.py memoria listar | esquecer "macro"
    python cli.py apostila --partes DIR [--saida arq.pdf] [--novo]
    python cli.py email enviar --para x@y.com --assunto "..." --texto "..."
    python cli.py email remetentes | contatos
    python cli.py agendar instalar [--minutos 60] [--email voce@gmail.com]
    python cli.py agendar status | remover

Escrita no Moodle (ensaia por padrão; --confirmar envia de verdade):

    python cli.py forum postar --forum 3 --assunto "Dúvida" --texto "..."
    python cli.py forum responder --post 42 --assunto "Re" --texto "..."
    python cli.py tarefa anexar --assign 12 --arquivo trabalho.pdf
    python cli.py tarefa salvar --assign 12 --itemid 99
    python cli.py tarefa entregar --assign 12 --aceitar-declaracao --confirmar
    python cli.py quiz tentativas --quiz 5
    python cli.py quiz ler --tentativa 88
    python cli.py escritas
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import agendar
import apostila
import busca
import calendario
import companion
import correio
import db
import memoria
import programa
import simulado
import escrita
import extract
import vetor
from moodle_client import MoodleError, get_client
from sync import agora_iso, sync_site, upsert_evento


def fmt(epoch) -> str:
    if not epoch:
        return "sem data"
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(int(epoch)))


def parse_data(texto: str) -> int:
    for formato in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(texto, formato).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"Data não reconhecida: {texto} (use 2026-04-15T14:00)")


# --------------------------------------------------------------------------


def cmd_init(args, con):
    db.init_db(con)
    db.MATERIAIS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Banco em {db.DB_PATH}\nMateriais em {db.MATERIAIS_DIR}")


def cmd_sync(args, con):
    db.init_db(con)
    res = sync_site(con, args.site, baixar=not args.sem_download)
    print(
        f"[{res.site}] {res.cursos} turmas"
        + (f" ({res.cursos_sem_mudanca} sem mudança no Moodle, puladas)"
           if res.cursos_sem_mudanca else "")
    )
    print(
        f"  arquivos: {res.arquivos_novos} novos, {res.arquivos_alterados} alterados\n"
        f"  eventos: {res.eventos_novos} novos, {res.eventos_mudados} com data mudada\n"
        f"  avisos: {res.avisos_novos} novos de pessoas"
    )
    for n in res.novidades:
        print(f"  · [{n.tipo}] {n.curso}: {n.descricao}")
    for e in res.erros:
        print(f"  ! {e}")


def cmd_avisos(args, con):
    import server as _srv
    print(_srv.avisos(dias=args.dias, curso=args.curso or 0, automaticos=args.automaticos))


def cmd_token(args, con):
    """Onde o token está guardado, e move para o chaveiro do sistema."""
    import moodle_client as mc

    sites = mc.load_sites()
    if not sites:
        raise SystemExit(f"Nenhum site em {mc.CONFIG_PATH}.")
    if args.acao == "chaveiro":
        movidos = mc.mover_para_chaveiro()
        print(f"Movidos para o chaveiro: {', '.join(movidos)}" if movidos
              else "Nada em texto puro; já estava tudo no chaveiro.")
        sites = mc.load_sites()
    for alias, s in sorted(sites.items()):
        onde = "chaveiro do sistema" if s.get("token_em") == "chaveiro" else (
            "TEXTO PURO no sites.json" if s.get("token") else "ausente")
        print(f"  {alias}: {s.get('url')} — token em {onde}")
    # Confere lendo de verdade: um token que o chaveiro não devolve só seria
    # descoberto na próxima hora, pelo agente do sync, num log que ninguém lê.
    try:
        print(f"  leitura ok: {get_client(None).alias}")
    except MoodleError as e:
        raise SystemExit(f"  ! {e}")


def cmd_turmas(args, con):
    linhas = db.query(
        con, "SELECT * FROM cursos ORDER BY site, fullname"
    )
    for r in linhas:
        marca = "✓" if r["acompanhar"] else " "
        print(f"[{marca}] {r['site']}/{r['courseid']}  {r['fullname']}")
    if linhas:
        print("\n[✓] = o sync acompanha esta turma")
    if not linhas:
        print("Rode `python cli.py sync` primeiro.")


def cmd_agenda(args, con):
    agora = int(time.time())
    limite = agora + args.dias * 86400
    sql = "SELECT * FROM eventos WHERE cancelado = 0"
    params: list = []
    if not args.todos:
        sql += " AND data_inicio BETWEEN ? AND ?"
        params += [agora, limite]
    sql += " ORDER BY data_inicio"
    for r in db.query(con, sql, params):
        selo = "✓" if r["confirmado"] else "?"
        print(
            f"#{r['id']:<4} {selo} {fmt(r['data_inicio']):<17} "
            f"{r['tipo']:<8} {r['titulo']}  ({r['origem']})"
        )


def cmd_evento(args, con):
    if args.acao == "add":
        eid, _ = upsert_evento(
            con,
            args.site or "manual",
            titulo=args.titulo,
            data_inicio=parse_data(args.data),
            origem="manual",
            courseid=args.curso,
            tipo=args.tipo,
            confirmado=1,
        )
        print(f"Evento #{eid} criado e confirmado.")

    elif args.acao == "set":
        atual = con.execute(
            "SELECT * FROM eventos WHERE id = ?", (args.id,)
        ).fetchone()
        if not atual:
            raise SystemExit(f"Evento #{args.id} não existe.")
        nova = parse_data(args.data)
        con.execute(
            """INSERT INTO historico_eventos
               (evento_id, campo, valor_antigo, valor_novo, origem, em)
               VALUES (?,?,?,?,?,?)""",
            (args.id, "data_inicio", atual["data_inicio"], nova, "manual", agora_iso()),
        )
        con.execute(
            """UPDATE eventos SET data_inicio=?, origem='manual', confirmado=1,
                   atualizado_em=? WHERE id=?""",
            (nova, agora_iso(), args.id),
        )
        con.commit()
        print(f"Evento #{args.id}: {fmt(atual['data_inicio'])} → {fmt(nova)}")

    elif args.acao == "confirmar":
        con.execute(
            "UPDATE eventos SET confirmado=1, atualizado_em=? WHERE id=?",
            (agora_iso(), args.id),
        )
        con.commit()
        print(f"Evento #{args.id} confirmado (fontes automáticas não sobrescrevem mais).")

    elif args.acao == "cancelar":
        con.execute("UPDATE eventos SET cancelado=1 WHERE id=?", (args.id,))
        con.commit()
        print(f"Evento #{args.id} cancelado.")

    elif args.acao == "pendentes":
        linhas = db.query(
            con,
            """SELECT * FROM eventos
               WHERE confirmado = 0 AND origem IN ('programa_pdf','forum')
               ORDER BY data_inicio""",
        )
        for r in linhas:
            print(f"#{r['id']} {fmt(r['data_inicio'])} {r['titulo']}  [{r['origem']}]")
            if r["trecho_origem"]:
                print(f"     origem: …{r['trecho_origem'][:160]}…")
        if not linhas:
            print("Nada pendente de confirmação.")


def cmd_materiais(args, con):
    sql = "SELECT a.*, c.shortname FROM arquivos a LEFT JOIN cursos c" \
          " ON c.site = a.site AND c.courseid = a.courseid WHERE 1=1"
    params: list = []
    if args.ocr:
        sql += " AND a.precisa_ocr = 1"
    if args.curso:
        sql += " AND a.courseid = ?"
        params.append(args.curso)
    if args.novos:
        sql += " AND a.baixado_em >= date('now','-7 day')"
    sql += " ORDER BY a.baixado_em DESC LIMIT ?"
    params.append(args.limite)
    linhas = db.query(con, sql, params)

    if args.ignorar or args.restaurar:
        valor = 1 if args.ignorar else 0
        for r in linhas:
            con.execute("UPDATE arquivos SET ignorado = ? WHERE id = ?", (valor, r["id"]))
        con.commit()
        verbo = "ignorado(s)" if valor else "restaurado(s)"
        print(f"{len(linhas)} arquivo(s) marcado(s) como {verbo}:")
        for r in linhas:
            print(f"  {r['nome']}")
        return

    for r in linhas:
        selo = " [precisa OCR]" if r["precisa_ocr"] else ""
        selo += " [ignorado]" if r["ignorado"] else ""
        print(f"{r['shortname'] or r['courseid']:<12} {r['nome']}{selo}")
        print(f"    {r['caminho_local'] or '(não baixado)'}")


def cmd_skill(args, con):
    """Confere (ou refaz) o cardápio de assinaturas embutido na skill.

    O cardápio existe para o modelo não precisar chamar `indice`, que custa
    167 tokens e um turno. Mas assinatura copiada envelhece: mudar um
    parâmetro sem refazer o bloco faz a skill mentir com confiança.
    """
    import re
    import server

    INI, FIM = "<!-- cardapio:inicio -->", "<!-- cardapio:fim -->"
    base = Path(__file__).parent / ".claude/skills/ufmg-companion"
    arquivos = [base / "SKILL.md"] + sorted((base / "referencias").glob("*.md"))
    problemas = 0
    for f in arquivos:
        txt = f.read_text(encoding="utf-8")
        corpo = re.sub(re.escape(INI) + r".*?" + re.escape(FIM) + r"\n?", "", txt, flags=re.S)
        citadas = sorted(
            n for n in server._FRIAS
            if re.search(rf"`{n}\b|\"{n}\"", corpo)
        )
        esperado = "\n".join(server._assinatura(server._FRIAS[n]) for n in citadas)
        atual = ""
        m = re.search(re.escape(INI) + r".*?```\n(.*?)```", txt, re.S)
        if m:
            atual = m.group(1).strip()
        if not citadas:
            continue
        if atual == esperado:
            print(f"  ✓ {f.name} ({len(citadas)} assinaturas)")
            continue
        problemas += 1
        print(f"  ✗ {f.name}: cardápio desatualizado")
        velhas = {l.split("(")[0] for l in atual.split("\n") if l.strip()}
        novas = {l.split("(")[0] for l in esperado.split("\n") if l.strip()}
        if novas - velhas:
            print(f"      faltam: {', '.join(sorted(novas - velhas))}")
        if velhas - novas:
            print(f"      sobram: {', '.join(sorted(velhas - novas))}")
        if args.refazer:
            cab = re.search(re.escape(INI) + r"(.*?)```\n", txt, re.S).group(0)
            novo_bloco = cab + esperado + "\n```\n\n" + FIM + "\n"
            f.write_text(
                re.sub(re.escape(INI) + r".*?" + re.escape(FIM) + r"\n?",
                       novo_bloco, txt, flags=re.S),
                encoding="utf-8",
            )
            print("      refeito.")
    if problemas and not args.refazer:
        print(f"\n{problemas} arquivo(s) fora de dia. `cli.py skill --refazer` conserta.")
    elif not problemas:
        print("\nCardápio em dia com o código.")


def cmd_avaliar(args, con):
    db.init_db(con)
    from avaliacao import rodar as av
    cursos = [args.curso] if args.curso else None
    if args.sem_rotulo:
        print(av.listar_sem_rotulo(con, cursos, rerank=not args.sem_rerank))
        return
    r = av.rodar(con, rerank=not args.sem_rerank, cursos=[args.curso] if args.curso else None)
    ant = av.anterior()
    texto = av.relatorio(r, ant)
    print(texto)
    if not args.nao_gravar:
        print(f"\ngravado em {av.gravar(r)}")
    if args.email:
        # Invariante do projeto: escrita ENSAIA por padrão. Sem --confirmar,
        # isto abre um rascunho no Mail.app e para ali.
        res = correio.compor(
            con, args.email, f"Avaliação da busca — {r.em[:10]}", texto,
            confirmar=args.confirmar, aceitar_novos=args.aceitar_novos,
        )
        print(f"\n{res}")


# Raiz dos projetos de apostila. Fica ao lado do banco, como todo o resto.
APOSTILAS = db.BASE_DIR / "apostilas"


# Títulos que o modelo deixou como veio do modelo: não identificam nada, e
# duas apostilas diferentes com esse título colidiriam numa linha só.
_TITULO_GENERICO = {"apostila", "apostila de estudo", "apostila macro iii — prova 1"}


def _titulo_do_projeto(d: Path) -> str:
    """Título da apostila: o `<title>` do cabeçalho, ou o nome do diretório."""
    import re as _re

    cabecalho = d / "00_head.html"
    if cabecalho.is_file():
        m = _re.search(
            r"<title>(.*?)</title>", cabecalho.read_text(encoding="utf-8"), _re.S
        )
        if m:
            t = _re.sub(r"\s+", " ", m.group(1)).strip()
            if t and t.lower() not in _TITULO_GENERICO:
                return t
    return d.name.replace("-", " ")


def _reconstruir_cobertura(con, raiz: Path) -> None:
    """Relê as apostilas já montadas e registra a cobertura que elas declaram.

    Existe porque a cobertura passou a sair das citações só agora: tudo que
    foi montado antes está gravado sem curso e sem nenhum trecho. Reler é
    barato e não depende de lembrar nada — o documento diz o que usou.
    """
    if not raiz.is_dir():
        raise SystemExit(f"não achei {raiz}")
    for d in sorted(x for x in raiz.iterdir() if x.is_dir()):
        partes = apostila.listar_partes(d)
        if not partes:
            continue
        html = "\n".join(x.read_text(encoding="utf-8") for x in partes)
        cit = apostila.citacoes(html)
        titulo = _titulo_do_projeto(d)
        pdfs = sorted(d.glob("*.pdf"))
        _, n, perdidos = memoria.registrar_apostila(
            con, titulo, cit, caminho=str(pdfs[0]) if pdfs else None
        )
        print(f"  {titulo[:44]:<44} {len(cit):>4} citação(ões) → {n:>3} trecho(s)")
        for x in perdidos[:4]:
            print(f"      ? sem arquivo correspondente: “{x}”")


def cmd_cobertura(args, con):
    db.init_db(con)
    if getattr(args, "reconstruir", False):
        raiz = Path(args.apostilas).expanduser() if args.apostilas else APOSTILAS
        print(f"Relendo apostilas em {raiz}:")
        _reconstruir_cobertura(con, raiz)
        print()
    db.init_db(con)
    res = memoria.listar_resumos(con, args.curso)
    print(f"{len(res)} resumo(s) guardado(s) e válido(s):")
    for r in res:
        print(f"  [{r['arquivo_id']:>3}] {r['nome'][:50]:<52} {r['tamanho']:>5} chars")
    if args.curso:
        herd = memoria.herdeiros_de_resumo(con, args.curso)
        if herd:
            print(f"\n{len(herd)} arquivo(s) herdariam resumo por conteúdo compartilhado:")
            for h in herd:
                print(f"  [{h['arquivo_id']:>3}] {h['nome'][:44]:<46} <- arquivo {h['fonte']} (peso {h['peso']})")
    prod = memoria.listar_produzido(con, args.curso)
    if prod:
        print(f"\n{len(prod)} documento(s) registrado(s):")
        for p in prod:
            print(f"  {p['escopo']}:{p['rotulo']:<28} {p['n']:>4} trecho(s)  {p['criado_em']}")
    sql = """SELECT a.courseid, COUNT(*) tot,
             SUM(CASE WHEN EXISTS(SELECT 1 FROM cobertura c WHERE c.trecho_sha=t.sha256)
                 THEN 1 ELSE 0 END) cob
             FROM trechos t JOIN arquivos a ON a.id=t.arquivo_id WHERE a.ignorado=0"""
    params = []
    if args.curso:
        sql += " AND a.courseid = ?"; params.append(args.curso)
    print("\ntrechos já usados em algum documento:")
    for r in db.query(con, sql + " GROUP BY a.courseid", params):
        print(f"  curso {r['courseid']}: {r['cob']}/{r['tot']}")


def cmd_buscar(args, con):
    db.init_db(con)
    if not busca.indexado(con):
        print("Índice vazio. Rode `cli.py extrair --reindexar`.")
        return
    achados = busca.buscar(
        con, args.termo, courseid=args.curso, limite=args.limite
    )
    if not achados:
        print(f"Nada para “{args.termo}”.")
        return
    for a in achados:
        onde = f"[{a['arquivo_id']}] {a['nome']}"
        if a["rotulo"]:
            onde += f", {a['rotulo']}"
        print(f"\n{onde}  (curso {a['courseid']}, achado por {a['via']})")
        print(f"    {a['trecho'][:300].replace(chr(10), ' ')}")


def cmd_indice(args, con):
    db.init_db(con)
    if not args.embedding and not args.contexto:
        c = busca.configuracao(con)
        print(f"embedding: {c['embedding']}  ({vetor.chave(con)})")
        print(f"contexto:  {'ligado' if c['contexto'] else 'desligado'}")
        print("modelos: " + ", ".join(sorted(vetor.MODELOS)))
        return
    for m in busca.configurar(con, args.embedding, args.contexto):
        print(m)


def cmd_extrair(args, con):
    db.init_db(con)
    if args.reindexar:
        if not vetor.disponivel(con):
            print(f"Sem lado vetorial: {vetor.motivo_indisponivel(con)}")
            print("O índice sai só com busca por termo. Continuando…")
        arquivos, trechos = extract.reindexar_tudo(con)
        print(f"Índice reconstruído: {trechos} trecho(s) de {arquivos} arquivo(s).")
        n = con.execute("SELECT COUNT(*) FROM vetores WHERE modelo = ?",
                        (vetor.chave(con),)).fetchone()[0]
        print(f"{n} vetor(es) para {vetor.chave(con)}.")
        a = con.execute("SELECT COUNT(*) FROM grafo_documentos").fetchone()[0]
        print(f"{a} aresta(s) no grafo entre documentos.")
        return
    res = extract.extrair_pendentes(
        con, site=args.site, refazer=args.refazer, limite=args.limite
    )
    for d in res.detalhes:
        print(f"  {d}")
    print(
        f"\n{res.extraidos} extraído(s), {res.ocr} precisa(m) de OCR, "
        f"{res.erros} com erro"
    )
    if res.vetores:
        print(f"{res.vetores} trecho(s) ganharam vetor.")
    if res.arestas:
        print(f"grafo entre documentos refeito: {res.arestas} aresta(s).")
    faltam = extract.vetores_pendentes(con)
    if faltam:
        print(f"{faltam} trecho(s) sem vetor: {vetor.motivo_indisponivel(con)}")
    if res.ocr:
        print("Os que precisam de OCR aparecem com selo em `cli.py materiais`.")
    if not res.detalhes:
        print("Nada pendente. Rode `cli.py sync` para baixar material novo.")


def cmd_programa(args, con):
    db.init_db(con)
    site = args.site or get_client(args.site).alias
    if args.curso:
        resultados = [programa.extrair_curso(
            con, site, args.curso, arquivo_id=args.arquivo_id,
            so_avaliacoes=not args.tudo)]
    else:
        resultados = programa.extrair_todos(con, site, so_avaliacoes=not args.tudo)

    for r in resultados:
        if r.erro:
            print(f"  · {r.nome_arquivo}: {r.erro}")
            continue
        print(f"\n{r.nome_arquivo} — {len(r.entradas)} entrada(s), "
              f"{r.criados} novas, {r.ignorados} já existentes")
        for e in r.entradas:
            print(f"    {e.data.strftime('%d/%m/%Y')}  {e.titulo[:70]}")
        for aviso in r.fora_de_ordem:
            print(f"    ! data fora de ordem: {aviso}")
    print("\nTudo entra como PENDENTE. Confira com `cli.py evento pendentes` "
          "e confirme com `cli.py evento confirmar <id>`.")


def cmd_agendar(args, con):
    if sys.platform != "darwin":
        # O agendador é o launchd. Fora do Mac o que ele executa continua
        # valendo; só o agendamento é outro.
        py, script = sys.executable, Path(__file__).resolve().parent / "agendar.py"
        print("`agendar` usa o launchd e só funciona no macOS. O comando que ele\n"
              "roda funciona em qualquer sistema — agende-o por conta própria:\n\n"
              f"  Linux (crontab -e):   0 * * * * {py} {script} --rodar-sync\n"
              f"  Windows:              schtasks /create /sc hourly /tn fenix-sync "
              f"/tr \"{py} {script} --rodar-sync\"")
        return
    if args.acao == "status":
        linhas = agendar.status()
        for a in linhas:
            print(f"  {a}")
            print(f"    log: {agendar.LOGS / (a.rotulo + '.log')}")
        if not linhas:
            print("Nada agendado. `cli.py agendar instalar` liga o sync horário.")
        return
    if args.acao == "remover":
        for l in agendar.remover():
            print(f"  {l}")
        return
    for l in agendar.instalar(args.minutos or 60, args.email or ""):
        print(f"  {l}")
    if args.avaliar_email:
        print(f"  {agendar.instalar_avaliacao(args.avaliar_email, args.avaliar_hora)}")
    print(f"\nLogs em {agendar.LOGS}")
    print("Confira com `cli.py agendar status` daqui a alguns minutos.")


def cmd_apostila(args, con):
    dir_partes = Path(args.partes).expanduser()
    if args.novo:
        d = apostila.novo_projeto(dir_partes, args.titulo or "Apostila")
        print(f"Projeto criado em {d}")
        print(f"  00_head.html copiado do modelo. Acrescente 01_*.html, 02_*.html…")
        return
    titulo = args.titulo or "Apostila de estudo"
    r = apostila.construir(dir_partes, args.saida, titulo)
    for nome in r.partes:
        print(f"  {nome}")
    print(r)
    # O CLI registrava NADA: apostila montada por aqui não entrava em
    # `produzido` e o "isso eu já montei" não a enxergava. Mesmo caminho do
    # servidor MCP agora.
    if r.pdf and not r.erro:
        _, n, perdidos = memoria.registrar_apostila(
            con, titulo, r.citacoes, caminho=str(r.pdf),
            courseid=getattr(args, "curso", None),
        )
        print(f"  cobertura: {n} trecho(s) registrados")
        for x in perdidos:
            print(f"    ? não casou com arquivo do banco: “{x}”")


def cmd_semestre(args, con):
    db.init_db(con)
    if args.acao in ("arquivar", "desarquivar"):
        if not args.site:
            raise SystemExit("informe --site")
        print(db.arquivar_semestre(con, args.site, args.acao == "arquivar"))
        return
    linhas = db.semestres(con)
    if not linhas:
        print("Nenhum semestre registrado ainda. Rode `cli.py sync` uma vez.")
        return
    atual = db.site_atual(con)
    print(f"{'':2} {'site':8} {'rótulo':9} {'cursos':>7} {'arquivos':>9} {'trechos':>8}")
    for r in linhas:
        marca = "→" if r["site"] == atual else (" " if not r["arquivado"] else "·")
        print(
            f"{marca:2} {r['site']:8} {r['rotulo'] or '':9} {r['cursos']:>7} "
            f"{r['arquivos']:>9} {r['trechos']:>8}"
            + ("   (arquivado)" if r["arquivado"] else "")
        )
    print("\n→ é o semestre corrente: é nele que a busca procura por padrão.")
    print("  Arquivado sai do sync e continua na busca, quando você pedir.")


def cmd_simulado(args, con):
    db.init_db(con)
    if args.acao == "ver":
        print(simulado.listar(con, args.curso, args.topico or "", args.gabarito))
        return
    if args.acao == "responder":
        if not args.questao or not args.resposta:
            raise SystemExit("informe --questao e --resposta")
        print(simulado.responder(con, args.questao, args.resposta))
        return
    if args.acao == "desempenho":
        linhas = simulado.desempenho(con, args.curso)
        if not linhas:
            print("Nada respondido ainda.")
            return
        print("acertos por tópico (o pior primeiro):")
        for r in linhas:
            n, a = r["respondidas"], r["acertos"] or 0
            print(f"  {r['topico'][:44]:<46} {a}/{n}")
        return
    # material: o mesmo que a ferramenta MCP entrega, para conferir na mão
    if not args.topico:
        raise SystemExit("informe --topico")
    achados, aviso = simulado.material(
        con, args.curso, args.topico, args.limite, args.novidade
    )
    if aviso:
        print(aviso + "\n")
    if not achados:
        print("Nada sobre esse tópico no material extraído.")
        return
    for a in achados:
        print(f"\n### [{a['arquivo_id']}] {a['nome']}, {a['rotulo']}")
        print(a["trecho"])


def cmd_calendario(args, con):
    db.init_db(con)
    destino = Path(args.saida).expanduser() if args.saida else Path.home() / "Downloads" / "ufmg.ics"
    r = calendario.exportar(
        con, destino, site=args.site, courseid=args.curso, todos=args.todos
    )
    print(r)
    if not args.todos and r.total == 0:
        # A recusa tem de ensinar o caminho, senão parece defeito.
        print(
            "  Só evento confirmado é exportado. Confirme com "
            "`cli.py evento confirmar --id N`, ou use --todos para levar também "
            "os automáticos (vão marcados como não confirmados)."
        )


def cmd_calendario_mac(args, con):
    import agenda_mac
    db.init_db(con)
    if args.acao == "agendas":
        for a in agenda_mac.agendas():
            print(f"  {a['nome']:<34} {'escrita' if a['gravavel'] else 'só leitura'}")
        cfg = agenda_mac.config()
        print(f"Escolhida: {cfg.get('agenda') or '(nenhuma)'}")
        return
    if args.acao == "definir":
        if not args.nome:
            raise SystemExit("Informe o nome da agenda, como aparece em `agendas`.")
        print(agenda_mac.definir(args.nome, args.ocupado or None, args.nao_ocupa or None))
        return
    if args.acao == "livres":
        janelas = agenda_mac.livres(
            dia=args.dia or "", dias=args.dias, das=args.das, ate=args.ate,
            minimo_min=args.minimo,
        )
        for a, b in janelas:
            print(f"  {agenda_mac._fmt(a)}–{time.strftime('%H:%M', time.localtime(b))}"
                  f"  ({(b - a) // 60} min)")
        if not janelas:
            print("Nenhuma janela livre nesse intervalo.")
        return
    if args.acao == "prazos":
        print(agenda_mac.sincronizar_prazos(con, dias=args.dias, confirmar=args.confirmar))
        return
    if args.acao == "marcar":
        import json as _json
        blocos = _json.loads(Path(args.arquivo).read_text()) if args.arquivo else []
        if not blocos:
            raise SystemExit("Informe --arquivo com uma lista JSON de blocos.")
        itens = agenda_mac.itens_de_estudo(blocos)
        print(agenda_mac.aplicar(con, itens, confirmar=args.confirmar,
                                 sobrepor=args.sobrepor))
        return
    if args.acao == "marcados":
        for r in agenda_mac.marcados(con, futuros=not args.todos):
            print(f"  {agenda_mac._fmt(r['inicio'])}  {r['titulo']}  [{r['chave']}]")
        return
    if args.acao == "desmarcar":
        if not args.chave:
            raise SystemExit("Informe --chave (veja `marcados`).")
        print(agenda_mac.aplicar(con, [], apagar=args.chave, confirmar=args.confirmar))
        return


def cmd_email(args, con):
    if args.acao == "remetentes":
        try:
            for c in correio.remetentes():
                print(f"  {c['conta']:<16} {c['endereco']}")
        except RuntimeError as e:
            raise SystemExit(f"Erro: {e}")
        return

    if args.acao == "contatos":
        linhas = correio.contatos(con)
        for r in linhas:
            print(f"  {r['endereco']:<34} {r['nome'] or '':<18} usos: {r['usos']}")
        if not linhas:
            print("Nenhum contato confirmado ainda.")
        return

    # --para é repetível (vira lista); estas duas ações agem num endereço só
    if args.acao in ("lembrar", "esquecer"):
        if not args.para:
            raise SystemExit("Informe --para com o endereço.")
        for endereco in args.para:
            print(
                correio.lembrar_contato(con, endereco, args.nome)
                if args.acao == "lembrar"
                else correio.esquecer_contato(con, endereco)
            )
        return

    if args.acao == "briefing":
        # Resumo da semana por e-mail, para rodar no cron. Sem --confirmar
        # abre rascunho, o que é inútil num job agendado: o cron deve usar
        # --confirmar e --aceitar-novo já resolvido antes, na mão.
        if not args.para:
            raise SystemExit("Informe --para.")
        import server as _srv
        corpo = _srv.briefing(dias=args.dias or 7)
        hoje = time.strftime("%d/%m")
        r = correio.compor(
            con, args.para, args.assunto or f"UFMG — situação em {hoje}", corpo,
            de=args.de, confirmar=args.confirmar, aceitar_novos=args.aceitar_novo,
        )
        print(r)
        # Código de saída importa no cron: sem ele, falha de envio passa
        # despercebida para sempre.
        if not (r.ok or r.ensaio):
            raise SystemExit(1)
        return

    # enviar
    if not args.para:
        raise SystemExit("Informe --para (pode repetir para vários destinatários).")
    corpo = _texto(args) if (args.texto or args.arquivo_texto) else ""
    r = correio.compor(
        con, args.para, args.assunto or "", corpo,
        de=args.de, anexos=args.anexo or [],
        confirmar=args.confirmar, aceitar_novos=args.aceitar_novo,
    )
    print(r)
    if r.ensaio:
        print("  (revise no Mail.app; --confirmar envia direto, sem abrir janela)")
    if not (r.ok or r.ensaio):
        raise SystemExit(1)


def cmd_memoria(args, con):
    db.init_db(con)
    alias = args.site or get_client(args.site).alias

    if args.acao == "listar":
        linhas = memoria.listar(con, alias)
        for r in linhas:
            print(f"  {r['termo']:<20} {r['alvo_tipo']}:{r['alvo_id']:<8} "
                  f"{r['rotulo'] or ''}  (usos: {r['usos']})")
        if not linhas:
            print("Nada memorizado ainda.")
        return

    if not args.termo:
        raise SystemExit('Informe o termo. Ex.: memoria lembrar "macro" --curso 6095')

    if args.acao == "esquecer":
        print(memoria.esquecer(con, alias, args.termo))
        return

    # lembrar
    tipos = [("curso", args.curso), ("quiz", args.quiz), ("forum", args.forum),
             ("tarefa", args.tarefa), ("arquivo", args.arquivo_id)]
    escolhidos = [(t, v) for t, v in tipos if v]
    if len(escolhidos) != 1:
        raise SystemExit(
            "Informe exatamente um alvo: --curso, --quiz, --forum, --tarefa "
            "ou --arquivo-id"
        )
    tipo, alvo = escolhidos[0]
    print(memoria.memorizar(con, alias, args.termo, tipo, alvo, args.rotulo))


def cmd_estudo(args, con):
    db.init_db(con)

    if args.acao == "roteiro":
        print(companion.roteiro_estudo(con, site=args.site, courseid=args.curso))
        return

    if args.acao == "politicas":
        sql = "SELECT * FROM politicas_envio"
        params: list = []
        if args.site:
            sql += " WHERE site = ?"
            params.append(args.site)
        linhas = db.query(con, sql + " ORDER BY site, escopo", params)
        for r in linhas:
            onde = r["escopo"] if r["escopo"] == "global" else f"{r['escopo']} {r['alvo']}"
            estado = "LIBERADO" if r["permitir"] else "bloqueado"
            print(f"[{r['site']}] {onde}: {estado} ({r['declarada_em']})")
            print(f"    motivo: {r['motivo']}")
        if not linhas:
            print("Nenhuma política declarada. Vale a heurística: só prática envia.")
        return

    if args.acao == "politica":
        escopo = "quiz" if args.quiz else "curso" if args.curso else "global"
        alvo = args.quiz or args.curso
        if not args.motivo:
            raise SystemExit(
                "Informe --motivo. Ex.: --motivo \"participação; provas são presenciais\""
            )
        alias = args.site or get_client(args.site).alias
        companion.declarar_politica(
            con, alias, escopo, alvo, not args.bloquear, args.motivo
        )
        onde = escopo if escopo == "global" else f"{escopo} {alvo}"
        print(f"Política registrada: envio "
              f"{'bloqueado' if args.bloquear else 'liberado'} para {onde} em {alias}.")
        print(f"Motivo: {args.motivo}")
        return

    cli = get_client(args.site)

    if args.acao == "preparar":
        sessao = companion.preparar_revisao(con, cli, args.tentativa)
        liberado, motivo = companion.pode_enviar(sessao, con)
        print(f"Tentativa {args.tentativa}: {len(sessao.itens)} questão(ões)")
        print(f"Envio pelo companion: {'liberado' if liberado else 'BLOQUEADO'} — {motivo}\n")
        for i in sessao.itens:
            print(f"## slot {i.slot} [{i.campo}]\n{i.enunciado}")
            for a in i.alternativas:
                print(f"   ({a['valor']}) {a['texto']}")
            print(f"   fonte: {'…' + i.fonte_trecho[:200] + '…' if i.fonte_trecho else 'não encontrada no material'}\n")
        destino = (Path(args.saida) if args.saida
                   else companion.caminho_sessao(args.tentativa))
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(sessao.para_json(), encoding="utf-8")
        print(f"Sessão salva em {destino}")
        print("Preencha proposta_ia / explicacao / resposta_final e rode `estudo salvar`.")
        return

    if args.acao == "corrigir":
        n = companion.registrar_correcao(con, cli, args.tentativa)
        print(f"{n} questão(ões) atualizadas com a correção do Moodle.")
        return

    # salvar / enviar precisam da sessão preenchida
    if not args.arquivo:
        raise SystemExit("Informe --arquivo com o JSON da sessão.")
    sessao = companion.Sessao.de_json(Path(args.arquivo).read_text(encoding="utf-8"))

    if args.acao == "salvar":
        ids = companion.salvar_revisao(con, sessao)
        print(f"{len(ids)} questão(ões) salvas para estudo.\n")
        print(companion.gabarito(sessao, con))
    else:  # enviar
        r = companion.enviar_aprovado(
            con, cli, sessao, finalizar=not args.sem_finalizar, confirmar=args.confirmar
        )
        print(r)
        _dica(r)
        if not r.ok:
            print("\n" + companion.gabarito(sessao, con))


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------


def _dica(r) -> None:
    if r.ensaio:
        print("  (ensaio — repita com --confirmar para enviar de verdade)")


def _texto(args) -> str:
    """Texto vindo de --texto ou de --arquivo-texto (para textos longos)."""
    if args.arquivo_texto:
        return Path(args.arquivo_texto).read_text(encoding="utf-8")
    if args.texto:
        return args.texto
    raise SystemExit("Informe --texto ou --arquivo-texto.")


def cmd_forum(args, con):
    db.init_db(con)
    cli = get_client(args.site)
    if args.acao == "postar":
        r = escrita.nova_discussao(
            con, cli, args.forum, args.assunto, _texto(args), confirmar=args.confirmar
        )
    else:
        r = escrita.responder_post(
            con, cli, args.post, args.assunto, _texto(args), confirmar=args.confirmar
        )
    print(r)
    _dica(r)


def cmd_tarefa(args, con):
    db.init_db(con)
    cli = get_client(args.site)
    if args.acao == "anexar":
        r = escrita.anexar_arquivo(
            con, cli, args.assign, args.arquivo,
            itemid=args.itemid or 0, confirmar=args.confirmar,
        )
    elif args.acao == "salvar":
        r = escrita.salvar_tarefa(
            con, cli, args.assign,
            itemid=args.itemid,
            texto=_texto(args) if (args.texto or args.arquivo_texto) else None,
            confirmar=args.confirmar,
        )
    else:  # entregar
        r = escrita.enviar_tarefa(
            con, cli, args.assign,
            aceitar_declaracao=args.aceitar_declaracao, confirmar=args.confirmar,
        )
    print(r)
    _dica(r)


def cmd_quiz(args, con):
    db.init_db(con)
    cli = get_client(args.site)

    if args.acao == "tentativas":
        for t in escrita.tentativas(cli, args.quiz):
            print(
                f"[{t.get('id')}] tentativa {t.get('attempt')} — {t.get('state')} "
                f"— iniciada {fmt(t.get('timestart'))} — nota {t.get('sumgrades')}"
            )
        return

    if args.acao == "iniciar":
        print(escrita.iniciar_questionario(con, cli, args.quiz, confirmar=args.confirmar))
        return

    if args.acao == "ler":
        import re as _re
        d = escrita.dados_tentativa(cli, args.tentativa, args.pagina)
        for q in d.get("questions", []):
            print(f"\n## slot {q.get('slot')} [{q.get('type')}] {q.get('state')}")
            print(f"   campos: {', '.join(q.get('campos') or []) or '(nenhum)'}")
            print("   " + _re.sub(r"<[^>]+>", " ", q.get("html") or "")[:800])
        return

    if args.acao == "revisar":
        d = escrita.revisar_tentativa(cli, args.tentativa)
        for q in d.get("questions", []):
            print(f"slot {q.get('slot')}: {q.get('state')} — {q.get('status')}")
        return

    # salvar / enviar: respostas em JSON
    respostas = json.loads(_texto(args)) if (args.texto or args.arquivo_texto) else {}
    if args.acao == "salvar":
        r = escrita.salvar_questionario(
            con, cli, args.tentativa, respostas, confirmar=args.confirmar
        )
    else:  # enviar
        r = escrita.enviar_questionario(
            con, cli, args.tentativa, respostas,
            finalizar=not args.sem_finalizar, confirmar=args.confirmar,
        )
    print(r)
    _dica(r)


def cmd_escritas(args, con):
    db.init_db(con)
    linhas = escrita.historico(con, args.limite)
    for r in linhas:
        marca = "!" if r["erro"] else "·"
        print(f"{marca} {r['em']} [{r['site']}] {r['acao']} {r['alvo']}")
        print(f"    {r['resumo']}")
        if r["erro"]:
            print(f"    ERRO: {r['erro'][:200]}")
    if not linhas:
        print("Nada foi escrito no Moodle ainda.")


# --------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(prog="ufmg", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    s = sub.add_parser("sync")
    s.add_argument("--site")
    s.add_argument("--sem-download", action="store_true")
    s.set_defaults(func=cmd_sync)

    av_ = sub.add_parser("avisos", help="mensagens e avisos que o sync trouxe")
    av_.add_argument("--dias", type=int, default=14)
    av_.add_argument("--curso", type=int)
    av_.add_argument("--automaticos", action="store_true",
                     help="inclui resumo de fórum e recibo de envio")
    av_.set_defaults(func=cmd_avisos)

    tk = sub.add_parser("token", help="onde o token está guardado")
    tk.add_argument("acao", choices=["status", "chaveiro"], nargs="?", default="status")
    tk.set_defaults(func=cmd_token)

    sub.add_parser("turmas").set_defaults(func=cmd_turmas)

    a = sub.add_parser("agenda")
    a.add_argument("--dias", type=int, default=30)
    a.add_argument("--todos", action="store_true")
    a.set_defaults(func=cmd_agenda)

    e = sub.add_parser("evento")
    e.add_argument("acao", choices=["add", "set", "confirmar", "cancelar", "pendentes"])
    e.add_argument("titulo", nargs="?")
    e.add_argument("--id", type=int)
    e.add_argument("--data")
    e.add_argument("--curso", type=int)
    e.add_argument("--site")
    e.add_argument("--tipo", default="prova")
    e.set_defaults(func=cmd_evento)

    m = sub.add_parser("materiais")
    m.add_argument("--curso", type=int)
    m.add_argument("--novos", action="store_true")
    m.add_argument("--limite", type=int, default=50)
    m.add_argument("--ignorar", action="store_true",
                   help="marca os arquivos listados como irrelevantes")
    m.add_argument("--restaurar", action="store_true")
    m.add_argument("--ocr", action="store_true",
                   help="filtra só os que precisam de OCR")
    m.set_defaults(func=cmd_materiais)

    def add_escrita(sp):
        sp.add_argument("--site")
        sp.add_argument("--texto")
        sp.add_argument("--arquivo-texto", dest="arquivo_texto")
        sp.add_argument(
            "--confirmar", action="store_true",
            help="envia de verdade (sem isso, só mostra o que seria enviado)",
        )

    sk = sub.add_parser("skill")
    sk.add_argument("--refazer", action="store_true",
                    help="reescreve o cardápio a partir do código")
    sk.set_defaults(func=cmd_skill)

    av = sub.add_parser("avaliar")
    av.add_argument("--curso", type=int, help="só uma matéria")
    av.add_argument("--sem-rerank", action="store_true")
    av.add_argument("--sem-rotulo", action="store_true",
                    help="lista os trechos devolvidos que ainda não têm rótulo")
    av.add_argument("--nao-gravar", action="store_true", help="não guarda a rodada")
    av.add_argument("--email", help="manda o relatório para este endereço")
    av.add_argument("--confirmar", action="store_true",
                    help="envia de verdade; sem isso abre rascunho no Mail.app")
    av.add_argument("--aceitar-novos", action="store_true",
                    help="libera endereço ainda não confirmado")
    av.set_defaults(func=cmd_avaliar)

    cb = sub.add_parser("cobertura")
    cb.add_argument(
        "--reconstruir", action="store_true",
        help="relê as apostilas montadas e registra a cobertura que elas citam",
    )
    cb.add_argument("--apostilas", help=f"raiz dos projetos (padrão: {APOSTILAS})")
    cb.add_argument("--curso", type=int)
    cb.set_defaults(func=cmd_cobertura)

    b = sub.add_parser("buscar")
    b.add_argument("termo")
    b.add_argument("--curso", type=int)
    b.add_argument("--limite", type=int, default=8)
    b.set_defaults(func=cmd_buscar)

    x = sub.add_parser("extrair")
    x.add_argument("--site")
    x.add_argument("--refazer", action="store_true", help="reprocessa tudo")
    x.add_argument("--limite", type=int)
    x.add_argument("--reindexar", action="store_true",
                   help="reconstrói o índice de busca a partir dos textos já extraídos")
    x.set_defaults(func=cmd_extrair)

    ib = sub.add_parser(
        "indice", help="mostra ou troca o modelo de embedding e o contexto do índice")
    ib.add_argument("--embedding", default="",
                    help="apelido em vetor.MODELOS (ex.: e5-large, minilm)")
    ib.add_argument("--contexto", default="", choices=["", "sim", "nao"],
                    help="arquivo/seção/título na frente do trecho ao indexar")
    ib.set_defaults(func=cmd_indice)

    est = sub.add_parser("estudo")
    est.add_argument(
        "acao",
        choices=["preparar", "salvar", "enviar", "corrigir", "roteiro",
                 "politica", "politicas"],
    )
    est.add_argument("--tentativa", type=int)
    est.add_argument("--arquivo")
    est.add_argument("--saida")
    est.add_argument("--curso", type=int)
    est.add_argument("--site")
    est.add_argument("--quiz", type=int)
    est.add_argument("--motivo")
    est.add_argument("--permitir", action="store_true", help="(padrão da ação politica)")
    est.add_argument("--bloquear", action="store_true")
    est.add_argument("--sem-finalizar", dest="sem_finalizar", action="store_true")
    est.add_argument("--confirmar", action="store_true")
    est.set_defaults(func=cmd_estudo)

    prog = sub.add_parser("programa")
    prog.add_argument("--curso", type=int)
    prog.add_argument("--arquivo-id", dest="arquivo_id", type=int)
    prog.add_argument("--tudo", action="store_true",
                      help="inclui aulas comuns, não só avaliações")
    prog.add_argument("--site")
    prog.set_defaults(func=cmd_programa)

    ag = sub.add_parser("agendar")
    ag.add_argument("acao", choices=["instalar", "status", "remover"])
    ag.add_argument("--minutos", type=int, help="intervalo do sync (padrão 60)")
    ag.add_argument("--email", help="também manda o resumo semanal para este endereço")
    ag.add_argument("--avaliar-email",
                    help="avaliação diária da busca, com relatório para este endereço")
    ag.add_argument("--avaliar-hora", type=int, default=20,
                    help="hora da avaliação diária (padrão 20)")
    ag.set_defaults(func=cmd_agendar)

    ap_ = sub.add_parser("apostila")
    ap_.add_argument("--partes", required=True, help="diretório com NN_*.html")
    ap_.add_argument("--saida", help="caminho do PDF (padrão: apostila.pdf ao lado)")
    ap_.add_argument("--titulo")
    ap_.add_argument("--novo", action="store_true", help="cria o projeto com o modelo")
    ap_.add_argument("--curso", type=int, help="só quando as citações não bastam")
    ap_.set_defaults(func=cmd_apostila)

    se = sub.add_parser("semestre")
    se.add_argument("acao", nargs="?", default="listar",
                    choices=["listar", "arquivar", "desarquivar"])
    se.add_argument("--site")
    se.set_defaults(func=cmd_semestre)

    sm = sub.add_parser("simulado")
    sm.add_argument("acao", choices=["material", "ver", "responder", "desempenho"])
    sm.add_argument("--curso", type=int, required=True)
    sm.add_argument("--topico")
    sm.add_argument("--limite", type=int, default=simulado.TRECHOS_PADRAO)
    sm.add_argument("--novidade", action="store_true",
                    help="só material que ainda não virou questão neste tópico")
    sm.add_argument("--gabarito", action="store_true", help="mostra as respostas")
    sm.add_argument("--questao", type=int)
    sm.add_argument("--resposta")
    sm.set_defaults(func=cmd_simulado)

    cal = sub.add_parser("calendario")
    cal.add_argument("--saida", help="caminho do .ics (padrão: ~/Downloads/ufmg.ics)")
    cal.add_argument("--curso", type=int)
    cal.add_argument("--site")
    cal.add_argument(
        "--todos", action="store_true",
        help="inclui os não confirmados, marcados no título",
    )
    cal.set_defaults(func=cmd_calendario)

    cm = sub.add_parser(
        "calendario-mac",
        help="agenda do Calendário do Mac: prazos, estudo, horário livre",
    )
    cm.add_argument(
        "acao",
        choices=["agendas", "definir", "livres", "prazos", "marcar", "marcados", "desmarcar"],
    )
    cm.add_argument("nome", nargs="?", help="agenda, em `definir`")
    cm.add_argument("--ocupado", action="append",
                    help="conta cujas agendas contam como ocupado (repetível)")
    cm.add_argument("--nao-ocupa", action="append",
                    help="trecho de título que não conta como ocupado (repetível)")
    cm.add_argument("--dia", help="início, AAAA-MM-DD (padrão: hoje)")
    cm.add_argument("--dias", type=int, default=7)
    cm.add_argument("--das", default="08:00")
    cm.add_argument("--ate", default="22:00")
    cm.add_argument("--minimo", type=int, default=60, help="minutos")
    cm.add_argument("--arquivo", help="JSON com [{titulo, inicio, fim|minutos, notas}]")
    cm.add_argument("--chave", action="append")
    cm.add_argument("--sobrepor", action="store_true")
    cm.add_argument("--todos", action="store_true")
    cm.add_argument("--confirmar", action="store_true")
    cm.set_defaults(func=cmd_calendario_mac)

    em = sub.add_parser("email")
    em.add_argument("acao", choices=["enviar", "briefing", "remetentes",
                                     "contatos", "lembrar", "esquecer"])
    em.add_argument("--dias", type=int, help="janela do briefing (padrão 7)")
    em.add_argument("--para", action="append", help="destinatário (repetível)")
    em.add_argument("--de", help="remetente; veja `email remetentes`")
    em.add_argument("--assunto")
    em.add_argument("--anexo", action="append", help="arquivo a anexar (repetível)")
    em.add_argument("--nome", help="nome do contato, em `lembrar`")
    em.add_argument("--aceitar-novo", dest="aceitar_novo", action="store_true",
                    help="permite destinatário ainda não confirmado")
    em.add_argument("--texto")
    em.add_argument("--arquivo-texto", dest="arquivo_texto")
    em.add_argument("--confirmar", action="store_true",
                    help="envia de verdade, sem abrir janela (sem isso, "
                         "abre rascunho no Mail.app para revisão)")
    em.set_defaults(func=cmd_email)

    mem = sub.add_parser("memoria")
    mem.add_argument("acao", choices=["lembrar", "listar", "esquecer"])
    mem.add_argument("termo", nargs="?")
    mem.add_argument("--curso", type=int)
    mem.add_argument("--quiz", type=int)
    mem.add_argument("--forum", type=int)
    mem.add_argument("--tarefa", type=int)
    mem.add_argument("--arquivo-id", dest="arquivo_id", type=int)
    mem.add_argument("--rotulo")
    mem.add_argument("--site")
    mem.set_defaults(func=cmd_memoria)

    f = sub.add_parser("forum")
    f.add_argument("acao", choices=["postar", "responder"])
    f.add_argument("--forum", type=int)
    f.add_argument("--post", type=int)
    f.add_argument("--assunto", default="")
    add_escrita(f)
    f.set_defaults(func=cmd_forum)

    t = sub.add_parser("tarefa")
    t.add_argument("acao", choices=["anexar", "salvar", "entregar"])
    t.add_argument("--assign", type=int, required=True)
    t.add_argument("--arquivo")
    t.add_argument("--itemid", type=int)
    t.add_argument("--aceitar-declaracao", dest="aceitar_declaracao", action="store_true")
    add_escrita(t)
    t.set_defaults(func=cmd_tarefa)

    q = sub.add_parser("quiz")
    q.add_argument("acao", choices=["tentativas", "iniciar", "ler", "revisar", "salvar", "enviar"])
    q.add_argument("--quiz", type=int)
    q.add_argument("--tentativa", type=int)
    q.add_argument("--pagina", type=int, default=0)
    q.add_argument("--sem-finalizar", dest="sem_finalizar", action="store_true")
    add_escrita(q)
    q.set_defaults(func=cmd_quiz)

    esc = sub.add_parser("escritas")
    esc.add_argument("--limite", type=int, default=30)
    esc.set_defaults(func=cmd_escritas)

    args = p.parse_args()
    if args.cmd == "evento" and args.acao in ("set", "confirmar", "cancelar") and not args.id:
        # permite `evento set 7 --data ...`
        if args.titulo and args.titulo.isdigit():
            args.id = int(args.titulo)
        else:
            raise SystemExit("Informe o id do evento, ex.: evento set 7 --data ...")

    con = db.conectar()
    # Migração num lugar só. Antes cada comando chamava init_db por conta
    # própria — e os que esqueciam quebravam ao encontrar coluna nova.
    db.init_db(con)
    try:
        args.func(args, con)
    except MoodleError as e:
        # Token faltando/expirado é o caso comum; traceback não ajuda ninguém.
        raise SystemExit(f"Erro: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

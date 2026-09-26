"""Banco local (SQLite): estado do sync, materiais, eventos e questões."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(os.environ.get("UFMG_DATA_DIR", Path.home() / ".ufmg-moodle-mcp"))
DB_PATH = BASE_DIR / "dados.db"
MATERIAIS_DIR = Path(
    os.environ.get("UFMG_MATERIAIS_DIR", BASE_DIR / "materiais")
)

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS cursos (
    site        TEXT NOT NULL,
    courseid    INTEGER NOT NULL,
    fullname    TEXT,
    shortname   TEXT,
    acompanhar  INTEGER NOT NULL DEFAULT 1,
    visto_em    TEXT,
    PRIMARY KEY (site, courseid)
);

CREATE TABLE IF NOT EXISTS arquivos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    courseid      INTEGER NOT NULL,
    cmid          INTEGER,
    secao         TEXT,
    modname       TEXT,
    nome          TEXT NOT NULL,
    fileurl       TEXT NOT NULL,
    filesize      INTEGER,
    timemodified  INTEGER,
    sha256        TEXT,
    caminho_local TEXT,
    baixado_em    TEXT,
    texto_path    TEXT,          -- txt extraído, quando houver
    precisa_ocr   INTEGER NOT NULL DEFAULT 0,
    ignorado      INTEGER NOT NULL DEFAULT 0,  -- fora da busca e dos avisos
    visto_em      TEXT,
    UNIQUE (site, fileurl)
);

CREATE INDEX IF NOT EXISTS idx_arquivos_curso ON arquivos (site, courseid);

-- origem: calendario | forum | programa_pdf | manual
-- Precedência de confiança: manual > calendario > forum > programa_pdf
-- (PESO_ORIGEM em sync.py), e confirmado=1 blinda contra fonte automática.
CREATE TABLE IF NOT EXISTS eventos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    site           TEXT NOT NULL,
    courseid       INTEGER,
    chave_externa  TEXT,          -- id do evento no Moodle, quando existir
    titulo         TEXT NOT NULL,
    tipo           TEXT,          -- prova | entrega | aula | outro
    data_inicio    INTEGER,       -- epoch
    origem         TEXT NOT NULL,
    origem_ref     TEXT,          -- arquivo/URL de onde a data saiu
    trecho_origem  TEXT,          -- texto literal p/ você conferir
    confirmado     INTEGER NOT NULL DEFAULT 0,
    cancelado      INTEGER NOT NULL DEFAULT 0,
    observacao     TEXT,
    criado_em      TEXT,
    atualizado_em  TEXT,
    UNIQUE (site, chave_externa)
);

CREATE TABLE IF NOT EXISTS historico_eventos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id     INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
    campo         TEXT,
    valor_antigo  TEXT,
    valor_novo    TEXT,
    origem        TEXT,
    em            TEXT
);

CREATE TABLE IF NOT EXISTS notificacoes (
    chave      TEXT PRIMARY KEY,   -- ex.: "evento:12:prazo_48h"
    canal      TEXT,
    enviada_em TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site        TEXT,
    iniciado_em TEXT,
    terminado_em TEXT,
    novidades   INTEGER DEFAULT 0,
    erro        TEXT
);

-- Companion de estudo: questões geradas a partir do SEU material.
CREATE TABLE IF NOT EXISTS questoes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT,
    courseid      INTEGER,
    arquivo_id    INTEGER REFERENCES arquivos(id) ON DELETE SET NULL,
    topico        TEXT,
    enunciado     TEXT NOT NULL,
    alternativas  TEXT,           -- JSON: [{"id":"a","texto":"..."}]
    correta       TEXT,           -- a resposta que VOCÊ aprovou
    explicacao    TEXT,
    fonte_trecho  TEXT,           -- de onde veio, p/ auditar alucinação
    criada_em     TEXT,
    -- Questão capturada de um questionário real (não gerada do material):
    attemptid     INTEGER,        -- tentativa de onde veio
    slot          INTEGER,        -- posição na tentativa
    campo         TEXT,           -- nome do input no Moodle (q42:1_answer)
    proposta_ia   TEXT,           -- o que o modelo propôs, antes da sua revisão
    revisada      INTEGER NOT NULL DEFAULT 0,  -- você passou os olhos?
    vale_nota     INTEGER         -- 1 = quiz avaliativo, 0 = prática, NULL = ?
);

-- Política de envio de questionário, declarada por você.
--
-- A API do Moodle diz a nota máxima do quiz, mas não diz se aquela nota vale
-- algo no esquema real da disciplina. Num curso em que o Moodle serve para
-- participação e a avaliação é presencial, `grade = 10` não significa nada.
-- Só o aluno sabe disso, então é ele quem declara — e a declaração fica
-- registrada com o motivo e a data.
--
-- Resolução: quiz > curso > global > heurística (grade == 0).
CREATE TABLE IF NOT EXISTS politicas_envio (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site         TEXT,
    escopo       TEXT NOT NULL,      -- global | curso | quiz
    alvo         INTEGER,            -- courseid ou quizid (NULL se global)
    permitir     INTEGER NOT NULL,   -- 1 = envia mesmo valendo nota
    motivo       TEXT NOT NULL,      -- por que essa nota não é a nota real
    declarada_em TEXT,
    UNIQUE (site, escopo, alvo)
);

-- Endereços para os quais você já mandou e-mail, confirmados por você.
--
-- E-mail para destino errado não tem desfazer: diferente de post em fórum, que
-- dá para apagar, ele já está na caixa de outra pessoa. Endereço novo exige
-- confirmação explícita; depois disso fica conhecido e não pergunta mais.
CREATE TABLE IF NOT EXISTS memoria_contato (
    endereco   TEXT PRIMARY KEY,   -- normalizado: minúsculo, sem espaço
    nome       TEXT,
    confirmado INTEGER NOT NULL DEFAULT 0,
    criado_em  TEXT,
    usos       INTEGER NOT NULL DEFAULT 0,
    ultimo_uso TEXT
);

-- O material cortado no tamanho em que ele é CITADO: uma página, um slide,
-- um bloco de parágrafos. É a unidade de recuperação e de citação.
--
-- Antes existia um índice por arquivo inteiro, e ele ranqueava o arquivo
-- errado pelo motivo certo: um PDF de 80 páginas ganhava de um slide exato só
-- por conter o termo em algum lugar. E o trecho devolvido era uma janela de 28
-- tokens sem número de página, o que empurrava a conversa para ler o PDF
-- inteiro (20k chars) quando aquela janela não bastava.
--
-- `pagina` vem dos marcadores que extract.py já escreve no texto
-- (`--- página 12 ---`, `--- slide 4 ---`). Formato sem marcador (docx, txt,
-- notebook) fica com pagina NULL e só o ordinal.
CREATE TABLE IF NOT EXISTS trechos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    arquivo_id INTEGER NOT NULL REFERENCES arquivos(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL,     -- posição dentro do arquivo, a partir de 1
    pagina     INTEGER,              -- número da página/slide, quando há
    rotulo     TEXT,                 -- "página 12" | "slide 4" — para citar
    titulo     TEXT,                 -- primeira linha curta, quando parece título
    texto      TEXT NOT NULL,
    n_chars    INTEGER NOT NULL,
    sha256     TEXT NOT NULL,        -- do texto do trecho; é a chave do vetor
    -- Quanto o trecho parece boilerplate (0 a 1). Calculado na indexação e
    -- usado para REBAIXAR, nunca excluir: às vezes o sumário é a única menção
    -- a um assunto. Medido: ruído tem mediana de 377 caracteres contra 1071
    -- do que serve, e o comprimento sozinho pega 33% dele com 82% de acerto.
    ruido      REAL NOT NULL DEFAULT 0,
    -- Em quantos OUTROS arquivos do curso este trecho tem quase-gêmeo. É o
    -- detector de capa e cabeçalho institucional: em ECN300, trechos de 42
    -- caracteres com só o nome do departamento apareciam em 12 arquivos e
    -- geravam 77 das 78 arestas de conteúdo do grafo.
    repetido   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (arquivo_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_trechos_arquivo ON trechos (arquivo_id);
CREATE INDEX IF NOT EXISTS idx_trechos_sha ON trechos (sha256);

-- Lado léxico da busca. `unicode61 remove_diacritics 2` faz "economico" achar
-- "econômico". `nome` entra junto porque o nome do arquivo costuma ser o único
-- lugar onde o assunto aparece escrito por extenso ("Aula 7 - Solow").
CREATE VIRTUAL TABLE IF NOT EXISTS trechos_fts USING fts5(
    nome,
    texto,
    trecho_id UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);

-- Lado vetorial. Chaveado pelo sha256 do TEXTO do trecho, não pelo id: um
-- slide repetido entre duas aulas embute uma vez só, e reextrair um arquivo
-- que não mudou não paga o modelo de novo.
--
-- `modelo` fica na linha porque vetor de modelo diferente não é comparável;
-- trocar de modelo invalida sozinho, sem ninguém precisar lembrar de limpar.
CREATE TABLE IF NOT EXISTS vetores (
    sha256    TEXT NOT NULL,
    modelo    TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vetor     BLOB NOT NULL,        -- float32 já normalizado: cosseno vira produto interno
    criado_em TEXT,
    PRIMARY KEY (sha256, modelo)
);

-- Como o índice deste banco foi construído. `embedding` é o apelido em
-- `vetor.MODELOS`; `contexto` = '1' põe arquivo, seção e título na frente do
-- trecho antes de embutir e de indexar no FTS. Mora no banco, e não no código,
-- porque índice e consulta têm de usar a MESMA configuração, e o Fênix e o
-- perfilador (`perfil.db`) compartilham o motor. Sem linha, vale o padrão
-- histórico — que é o que o perfilador calibrou.
CREATE TABLE IF NOT EXISTS config_busca (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

-- Grafo entre DOCUMENTOS. A adjacência dentro de um arquivo já está em
-- `trechos.ordinal`; isto liga arquivo a arquivo, e existe por um motivo
-- medido: a âncora léxica sozinha alcançava 5 dos 10 arquivos de Macro, e
-- afrouxar o corte não resolvia — porta que nunca abriu não abre por margem.
-- Com o grafo, 9 dos 10.
--
-- `vizinho`  — centroide dos vetores do arquivo; guarda os k mais próximos de
--              cada um, e não um limiar de similaridade, porque limiar
--              absoluto não transfere entre cursos (o par mais parecido de
--              Regional fica abaixo do limiar que funciona em Macro).
-- `conteudo` — os dois arquivos têm trecho praticamente igual. É o que liga
--              material republicado e o artigo que reaparece como fonte de
--              um slide.
CREATE TABLE IF NOT EXISTS grafo_documentos (
    site      TEXT,
    courseid  INTEGER,
    a         INTEGER NOT NULL REFERENCES arquivos(id) ON DELETE CASCADE,
    b         INTEGER NOT NULL REFERENCES arquivos(id) ON DELETE CASCADE,
    tipo      TEXT NOT NULL,     -- vizinho | conteudo
    peso      REAL,
    PRIMARY KEY (a, b, tipo)
);

CREATE INDEX IF NOT EXISTS idx_grafo_a ON grafo_documentos (a);
CREATE INDEX IF NOT EXISTS idx_grafo_b ON grafo_documentos (b);

-- Memória: o que NÃO precisa ser redescoberto a cada conversa.
--
-- "Macro" -> curso 6095. Estável dentro do semestre; quando o site troca, os
-- apelidos do site antigo deixam de valer sozinhos, porque a chave inclui site.
CREATE TABLE IF NOT EXISTS memoria_alias (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    site       TEXT NOT NULL,
    termo      TEXT NOT NULL,     -- normalizado: minúsculo, sem acento
    alvo_tipo  TEXT NOT NULL,     -- curso | quiz | arquivo | forum | tarefa
    alvo_id    INTEGER NOT NULL,
    rotulo     TEXT,              -- nome legível, para exibir
    criado_em  TEXT,
    usos       INTEGER NOT NULL DEFAULT 0,
    ultimo_uso TEXT,
    UNIQUE (site, termo)
);

-- Resumo derivado, chaveado pelo HASH DO CONTEÚDO, não pelo id do arquivo.
-- Professor republica com alteração real -> sha256 muda -> o resumo antigo
-- deixa de ser encontrado, sem ninguém precisar lembrar de limpar. E o mesmo
-- PDF publicado em dois cursos aproveita um resumo só.
CREATE TABLE IF NOT EXISTS memoria_resumo (
    sha256     TEXT PRIMARY KEY,
    arquivo_id INTEGER REFERENCES arquivos(id) ON DELETE SET NULL,
    escopo     TEXT,              -- arquivo | secao
    resumo     TEXT NOT NULL,
    criado_em  TEXT
);

-- O que já foi PRODUZIDO a partir do material, e com que trechos.
--
-- É a peça que faltava para o maior ganho medido do projeto: saber que um
-- documento já existe economiza de 60% a 92% de token, contra 21% da melhor
-- escolha de método de busca. Sem isto, pedir "uma versão mais completa do
-- resumo" refazia a recuperação inteira.
--
-- `cobertura` é chaveada pelo sha do TEXTO DO TRECHO, nunca pelo id: o id é
-- reciclado a cada `extrair --reindexar` (a tabela é apagada e reinserida), e
-- isso já invalidou 172 referências de uma vez. É a mesma escolha que
-- `vetores` faz, pelo mesmo motivo.
-- `courseid` usa a sentinela 0 para "sem curso", e NUNCA NULL. No SQLite
-- NULL não colide em UNIQUE, então com NULL ali o ON CONFLICT nunca disparava
-- e cada remontagem da mesma apostila criava uma linha nova: "Microeconomia II
-- — Lista 1 resolvida" chegou a existir três vezes, cada uma com a cobertura
-- vazia. Além disso `listar_produzido(curso)` filtra por igualdade e não
-- enxergava nenhuma linha NULL — 8 das 9 que existiam.
-- Um semestre do UFMG Virtual. O `site` já era a chave de tudo (cursos,
-- arquivos, eventos, memória), mas nada dizia QUAL semestre é o de agora, e
-- por isso a busca varria todos — invisível enquanto só existe um, e uma
-- resposta com o slide do semestre passado no dia em que existirem dois.
--
-- `arquivado` é o semestre que acabou: fica na busca, sai do sync. O token
-- dele expira e uma rodada por dia falhando não avisa nada a ninguém.
CREATE TABLE IF NOT EXISTS semestres (
    site      TEXT PRIMARY KEY,
    rotulo    TEXT,                          -- "2026/2"
    arquivado INTEGER NOT NULL DEFAULT 0,
    visto_em  TEXT
);

CREATE TABLE IF NOT EXISTS produzido (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    site      TEXT,
    courseid  INTEGER NOT NULL DEFAULT 0,
    escopo    TEXT NOT NULL,        -- resumo | apostila | topico
    rotulo    TEXT NOT NULL,        -- "Prova 2", "crescimento endógeno"
    caminho   TEXT,                 -- arquivo no disco, quando houver
    criado_em TEXT,
    UNIQUE (courseid, escopo, rotulo)
);

CREATE TABLE IF NOT EXISTS cobertura (
    produzido_id INTEGER NOT NULL REFERENCES produzido(id) ON DELETE CASCADE,
    trecho_sha   TEXT NOT NULL,
    em           TEXT,
    PRIMARY KEY (produzido_id, trecho_sha)
);

CREATE INDEX IF NOT EXISTS idx_cobertura_sha ON cobertura (trecho_sha);

-- O que o professor AVISA fora do calendário: mensagem, notificação, fórum de
-- avisos. Existe por medição: nas três matérias acompanhadas o fórum "Avisos"
-- estava vazio, e os avisos reais ("aulas agora no Laboratório 1102", "o curso
-- começa amanhã") tinham chegado como mensagem direta — invisíveis ao briefing.
--
-- Não vira evento sozinho. Data lida de texto livre é pior que a do plano de
-- ensino, e `programa.py` já não confirma nem aquela; aqui o aviso é mostrado,
-- e quem decide é o usuário.
--
-- `prioridade`: 2 = professor da turma, 1 = pessoa, 0 = automático do sistema
-- (resumo de fórum, recibo de envio de tarefa). Separar é o que impede o
-- briefing de virar lista de recibo e o usuário parar de ler.
CREATE TABLE IF NOT EXISTS avisos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site        TEXT NOT NULL,
    chave       TEXT NOT NULL,       -- msg:<id> | notif:<id> | forum:<discussão>
    origem      TEXT NOT NULL,       -- mensagem | notificacao | forum_avisos
    courseid    INTEGER,
    autor       TEXT,
    assunto     TEXT,
    texto       TEXT,                -- sem HTML, cortado
    url         TEXT,
    prioridade  INTEGER NOT NULL DEFAULT 1,
    criado_em   INTEGER,             -- epoch, do Moodle
    lido_moodle INTEGER NOT NULL DEFAULT 0,
    visto_em    TEXT,
    UNIQUE (site, chave)
);

CREATE INDEX IF NOT EXISTS idx_avisos_data ON avisos (site, criado_em);

-- Toda atividade da turma, não só as que têm arquivo. É o catálogo que faltava:
-- questionário, página, livro, link, tarefa — o que `arquivos` não enxerga.
--
-- `concluido` vem do rastreio de conclusão do Moodle e é NULL quando a
-- atividade não é rastreada. Para `resource` ele NÃO significa "você leu": o
-- sync baixa pela API e isso não marca visualização, então todo arquivo aparece
-- como não visto. Medido nas três matérias: 50 de 50. Quem lê este campo
-- precisa ignorar arquivo.
--
-- `mudou_em` vem de core_course_get_updates_since: o momento em que o Moodle
-- diz que a atividade mudou, e `mudou_o_que` o tipo da mudança.
CREATE TABLE IF NOT EXISTS modulos (
    site        TEXT NOT NULL,
    courseid    INTEGER NOT NULL,
    cmid        INTEGER NOT NULL,
    modname     TEXT,
    nome        TEXT,
    secao       TEXT,
    concluido   INTEGER,
    mudou_em    INTEGER,
    mudou_o_que TEXT,
    visto_em    TEXT,
    PRIMARY KEY (site, cmid)
);

CREATE INDEX IF NOT EXISTS idx_modulos_curso ON modulos (site, courseid);

-- Toda escrita no Moodle (fórum, tarefa, questionário) vira uma linha aqui.
-- É a ação menos reversível do sistema: se algo saiu no seu nome, saiu daqui,
-- e o payload guardado é o que permite reconstruir exatamente o que foi enviado.
CREATE TABLE IF NOT EXISTS log_escrita (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site        TEXT NOT NULL,
    acao        TEXT NOT NULL,   -- forum_discussao | forum_resposta |
                                 -- tarefa_rascunho | tarefa_enviar |
                                 -- quiz_iniciar | quiz_salvar | quiz_enviar
                                 -- email_rascunho | email_enviar
                                 -- agenda_criado | agenda_atualizado |
                                 -- agenda_apagar | agenda_gravar (erro)
    alvo        TEXT,            -- forumid=3 | assignid=12 | attemptid=88
    resumo      TEXT,            -- o que foi enviado, legível
    payload     TEXT,            -- JSON exato mandado ao Moodle
    resposta    TEXT,            -- JSON devolvido pelo Moodle
    erro        TEXT,
    em          TEXT
);

-- O que o Fênix pôs no Calendário do Mac. `chave` é nossa e estável
-- (evento:<site>:<id> ou estudo:<sha>); `uid` é do Calendar.app e é o que
-- acha o evento para mover ou apagar. `assinatura` evita regravar o igual.
CREATE TABLE IF NOT EXISTS agenda_itens (
    chave         TEXT PRIMARY KEY,
    agenda        TEXT NOT NULL,
    uid           TEXT,
    titulo        TEXT,
    inicio        INTEGER,
    fim           INTEGER,
    dia_inteiro   INTEGER DEFAULT 0,
    assinatura    TEXT,
    origem        TEXT,          -- prazo | estudo
    criado_em     TEXT,
    atualizado_em TEXT
);

CREATE TABLE IF NOT EXISTS respostas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    questao_id    INTEGER NOT NULL REFERENCES questoes(id) ON DELETE CASCADE,
    resposta_dada TEXT,
    acertou       INTEGER,
    respondida_em TEXT
);
"""


def conectar(path: Path | None = None) -> sqlite3.Connection:
    caminho = path or DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


# Colunas acrescentadas depois da v1. SQLite não tem ADD COLUMN IF NOT EXISTS,
# então init_db compara com o PRAGMA e aplica o que falta.
COLUNAS_NOVAS = {
    "cursos": [
        # epoch do último sync completo da turma: é o `since` de
        # core_course_get_updates_since. Sem mudança desde ali, o sync não
        # baixa a estrutura inteira do curso de novo.
        ("sincronizado_em", "INTEGER"),
        # ids dos professores (JSON), para saber que uma mensagem é aviso da
        # turma e não conversa de colega.
        ("professores", "TEXT"),
    ],
    "eventos": [
        # 1 = ainda exige ação sua, segundo o próprio Moodle (aparece em
        # core_calendar_get_action_events_by_timesort). É o que distingue
        # "entrega vencida" de "entrega vencida que você não fez".
        ("pendente_acao", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "trechos": [
        ("ruido", "REAL NOT NULL DEFAULT 0"),
        ("repetido", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "arquivos": [
        ("ignorado", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "questoes": [
        ("attemptid", "INTEGER"),
        ("slot", "INTEGER"),
        ("campo", "TEXT"),
        ("proposta_ia", "TEXT"),
        ("revisada", "INTEGER NOT NULL DEFAULT 0"),
        ("vale_nota", "INTEGER"),
    ],
}


# Índices virtuais que saíram de uso. Deixar um FTS órfão no banco é pior que
# apagá-lo: ele continua respondendo a consulta antiga com dado congelado.
# `agenda_propostas`: escrita na agenda por token pelo WhatsApp, desenhada em
# 21/09/2026 e adiada no dia seguinte — pelo WhatsApp a agenda é só leitura.
TABELAS_APOSENTADAS = ("arquivos_fts", "agenda_propostas")


# Reparos de dado, não de coluna: o que `COLUNAS_NOVAS` não resolve porque
# muda o formato do que já está gravado. Cada um confere sozinho se já foi
# aplicado — `init_db` roda em toda conexão.


def _reparar_produzido(con: sqlite3.Connection) -> bool:
    """`produzido.courseid` NULL vira 0, e as linhas duplicadas se fundem.

    Enquanto `courseid` aceitou NULL, o UNIQUE não valia (NULL não colide com
    NULL no SQLite) e a mesma apostila entrava de novo a cada remontagem. A
    coluna não dá para alterar no lugar: a tabela é reconstruída e a cobertura
    é remapeada para a linha que sobrou.

    Devolve True se reparou alguma coisa.
    """
    info = con.execute("PRAGMA table_info(produzido)").fetchall()
    if not info:
        return False
    for coluna in info:
        # (cid, name, type, notnull, default, pk)
        if coluna[1] == "courseid" and coluna[3] == 1:
            return False  # já reparado
    con.commit()
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.executescript(
            """
            BEGIN;
            CREATE TABLE produzido_novo (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                site      TEXT,
                courseid  INTEGER NOT NULL DEFAULT 0,
                escopo    TEXT NOT NULL,
                rotulo    TEXT NOT NULL,
                caminho   TEXT,
                criado_em TEXT,
                UNIQUE (courseid, escopo, rotulo)
            );
            -- A linha que sobrevive é a mais antiga (MIN(id)); o caminho e o
            -- site vêm de qualquer uma que os tenha (MAX ignora NULL).
            INSERT INTO produzido_novo (id, site, courseid, escopo, rotulo, caminho, criado_em)
            SELECT MIN(id), MAX(site), COALESCE(courseid, 0), escopo, rotulo,
                   MAX(caminho), MIN(criado_em)
              FROM produzido
             GROUP BY COALESCE(courseid, 0), escopo, rotulo;

            CREATE TABLE cobertura_nova (
                produzido_id INTEGER NOT NULL REFERENCES produzido(id) ON DELETE CASCADE,
                trecho_sha   TEXT NOT NULL,
                em           TEXT,
                PRIMARY KEY (produzido_id, trecho_sha)
            );
            -- OR IGNORE porque duas linhas fundidas podem ter coberto o mesmo
            -- trecho: a chave primária é (produzido_id, trecho_sha).
            INSERT OR IGNORE INTO cobertura_nova (produzido_id, trecho_sha, em)
            SELECT n.id, c.trecho_sha, MIN(c.em)
              FROM cobertura c
              JOIN produzido o ON o.id = c.produzido_id
              JOIN produzido_novo n
                ON n.courseid = COALESCE(o.courseid, 0)
               AND n.escopo = o.escopo
               AND n.rotulo = o.rotulo
             GROUP BY n.id, c.trecho_sha;

            DROP TABLE cobertura;
            DROP TABLE produzido;
            ALTER TABLE produzido_novo RENAME TO produzido;
            ALTER TABLE cobertura_nova RENAME TO cobertura;
            CREATE INDEX IF NOT EXISTS idx_cobertura_sha ON cobertura (trecho_sha);
            COMMIT;
            """
        )
    finally:
        con.execute("PRAGMA foreign_keys = ON")
    return True


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    for tabela in TABELAS_APOSENTADAS:
        con.execute(f"DROP TABLE IF EXISTS {tabela}")
    for tabela, colunas in COLUNAS_NOVAS.items():
        existentes = {
            r[1] for r in con.execute(f"PRAGMA table_info({tabela})").fetchall()
        }
        for nome, tipo in colunas:
            if nome not in existentes:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
    con.commit()
    _reparar_produzido(con)


def registrar_semestre(con: sqlite3.Connection, site: str) -> None:
    """Marca que este semestre existe. Chamado pelo sync, não pelo usuário."""
    import time as _time

    rotulo = f"{site[:4]}/{site[4:]}" if len(site) == 5 and site.isdigit() else site
    con.execute(
        """INSERT INTO semestres (site, rotulo, visto_em) VALUES (?,?,?)
           ON CONFLICT(site) DO UPDATE SET visto_em = excluded.visto_em""",
        (site, rotulo, _time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    con.commit()


def site_atual(con: sqlite3.Connection) -> str | None:
    """O semestre corrente: o maior alias que não foi arquivado.

    Mesma regra de `moodle_client.get_client` sem alias — os aliases da UFMG
    são "20262", "20271", e ordem alfabética é ordem cronológica. Sem nenhum
    semestre registrado, devolve o maior que apareça em `cursos`: banco velho
    não fica sem escopo por causa de tabela nova.
    """
    r = con.execute(
        "SELECT site FROM semestres WHERE arquivado = 0 ORDER BY site DESC LIMIT 1"
    ).fetchone()
    if r:
        return r["site"]
    r = con.execute("SELECT MAX(site) s FROM cursos").fetchone()
    return r["s"] if r and r["s"] else None


def tem_outro_semestre(con: sqlite3.Connection, alem_de: str) -> bool:
    """Há material indexado fora deste semestre?"""
    return bool(
        con.execute(
            """SELECT 1 FROM arquivos a
                WHERE a.site <> ? AND a.ignorado = 0
                  AND EXISTS (SELECT 1 FROM trechos t WHERE t.arquivo_id = a.id)
                LIMIT 1""",
            (alem_de,),
        ).fetchone()
    )


def semestres(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Semestres com o que cada um tem, do mais novo para o mais velho."""
    return con.execute(
        """SELECT s.site, s.rotulo, s.arquivado,
                  (SELECT COUNT(*) FROM cursos c WHERE c.site = s.site) cursos,
                  (SELECT COUNT(*) FROM arquivos a WHERE a.site = s.site) arquivos,
                  (SELECT COUNT(*) FROM trechos t JOIN arquivos a2 ON a2.id = t.arquivo_id
                    WHERE a2.site = s.site) trechos
             FROM semestres s
         ORDER BY s.site DESC"""
    ).fetchall()


def arquivar_semestre(con: sqlite3.Connection, site: str, arquivar: bool = True) -> str:
    n = con.execute(
        "UPDATE semestres SET arquivado = ? WHERE site = ?", (int(arquivar), site)
    ).rowcount
    con.commit()
    if not n:
        return f"Semestre '{site}' não está registrado. Rode o sync uma vez."
    if arquivar:
        return (
            f"Semestre {site} arquivado: continua na busca, sai do sync. "
            "O material dele só aparece quando você pedir."
        )
    return f"Semestre {site} voltou a ser acompanhado."


def query(con: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return con.execute(sql, tuple(params)).fetchall()


def ja_notificado(con: sqlite3.Connection, chave: str) -> bool:
    return con.execute(
        "SELECT 1 FROM notificacoes WHERE chave = ?", (chave,)
    ).fetchone() is not None


def marcar_notificado(con: sqlite3.Connection, chave: str, canal: str, agora: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO notificacoes (chave, canal, enviada_em) VALUES (?, ?, ?)",
        (chave, canal, agora),
    )
    con.commit()


def registrar_escrita(
    con: sqlite3.Connection,
    site: str,
    acao: str,
    *,
    alvo: str = "",
    resumo: str = "",
    payload: Any = None,
    resposta: Any = None,
    erro: str | None = None,
) -> int:
    """Grava uma escrita feita no Moodle. Chamado sempre, inclusive em erro."""
    import json as _json
    import time as _time

    cur = con.execute(
        """INSERT INTO log_escrita
           (site, acao, alvo, resumo, payload, resposta, erro, em)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            site, acao, alvo, resumo,
            _json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None,
            _json.dumps(resposta, ensure_ascii=False, default=str)[:8000] if resposta is not None else None,
            erro,
            _time.strftime("%Y-%m-%dT%H:%M:%S"),
        ),
    )
    con.commit()
    return int(cur.lastrowid)

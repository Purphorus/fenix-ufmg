"""As sete invariantes do CLAUDE.md, uma classe cada.

Elas existem porque cada uma já custou alguma coisa: uma escrita indevida no
Moodle, uma data errada apresentada com confiança, 172 referências perdidas
numa reindexação. O comentário de cada teste diz o que aconteceria sem ele.
"""

from __future__ import annotations

import inspect

import pytest

import companion
import escrita
import memoria
import programa
import sync


# --------------------------------------------------------------------------
# 1. Escrita ensaia por padrão
# --------------------------------------------------------------------------
#
# "Pedir para redigir não é pedir para publicar." Uma assinatura nova que
# esqueça o padrão publica no fórum da turma sem que ninguém tenha mandado.


def _funcoes_de_escrita():
    for nome, f in inspect.getmembers(escrita, inspect.isfunction):
        if nome.startswith("_") or f.__module__ != "escrita":
            continue
        if "confirmar" in inspect.signature(f).parameters:
            yield nome, f


class TestEnsaioPorPadrao:
    def test_toda_escrita_tem_confirmar(self):
        # Quem escreve precisa de `con` para registrar em log_escrita e de
        # `cli` para falar com o Moodle. Tomar os dois é o que distingue uma
        # escrita de uma leitura como `dados_tentativa`, que só lê.
        nomes = {n for n, _ in _funcoes_de_escrita()}
        assert nomes, "nenhuma função de escrita encontrada — o teste cegou"
        for nome, f in inspect.getmembers(escrita, inspect.isfunction):
            if nome.startswith("_") or f.__module__ != "escrita":
                continue
            params = inspect.signature(f).parameters
            if "cli" in params and "con" in params:
                assert "confirmar" in params, f"{nome} escreve sem gate"

    @pytest.mark.parametrize("nome,f", list(_funcoes_de_escrita()))
    def test_padrao_e_falso(self, nome, f):
        assert f.__defaults__ is not None or True
        p = inspect.signature(f).parameters["confirmar"]
        assert p.default is False, f"{nome} publica por padrão"

    def test_ensaio_nao_chama_o_moodle(self, con, cli):
        r = escrita.nova_discussao(
            con, cli, forumid=3, assunto="teste", mensagem="oi", confirmar=False
        )
        assert r.ensaio is True
        assert cli.chamadas == [], "o ensaio chamou o Moodle"
        assert con.execute("SELECT COUNT(*) FROM log_escrita").fetchone()[0] == 0

    def test_envio_confirmado_vai_para_o_log(self, con, cli):
        escrita.nova_discussao(
            con, cli, forumid=3, assunto="teste", mensagem="oi", confirmar=True
        )
        assert len(cli.chamadas) == 1
        assert con.execute("SELECT COUNT(*) FROM log_escrita").fetchone()[0] == 1

    def test_falha_tambem_vai_para_o_log(self, con):
        # import relativo: com `testes/zap/` existindo, `conftest` solto
        # resolvia para o conftest do zap e o nome não estava lá
        from .conftest import ClienteFalso

        ruim = ClienteFalso(erro=RuntimeError("token expirado"))
        r = escrita.nova_discussao(
            con, ruim, forumid=3, assunto="t", mensagem="m", confirmar=True
        )
        assert r.ok is False
        linha = con.execute("SELECT erro, payload FROM log_escrita").fetchone()
        # Sem isto, uma escrita que falhou some: não dá para saber se chegou
        # ao Moodle ou não, que é justamente a dúvida que importa.
        assert linha is not None and "token expirado" in linha["erro"]
        assert linha["payload"], "a invariante 7 pede o payload junto"


# --------------------------------------------------------------------------
# 2. Evento confirmado nunca é alterado por fonte automática
# --------------------------------------------------------------------------


class TestPrecedenciaDeEvento:
    def _criar(self, con, **kw):
        return sync.upsert_evento(con, "20262", **kw)

    def test_confirmado_resiste_a_fonte_automatica(self, con):
        eid, acao = self._criar(
            con, titulo="Prova 1", data_inicio=1000, origem="manual",
            courseid=1, confirmado=1,
        )
        assert acao == "criado"
        _, acao = self._criar(
            con, titulo="Prova 1", data_inicio=2000, origem="calendario",
            courseid=1,
        )
        assert acao == "ignorado"
        assert con.execute(
            "SELECT data_inicio FROM eventos WHERE id=?", (eid,)
        ).fetchone()[0] == 1000

    def test_confirmado_resiste_a_fonte_de_peso_MAIOR(self, con):
        # O caso que importa de verdade. `cli.py evento confirmar` grava
        # confirmado=1 SEM trocar a origem, então um evento lido do plano de
        # ensino (peso 2) e conferido na mão continua com origem
        # programa_pdf. Quando o calendário do Moodle (peso 4) traz outra
        # data, só a checagem de `confirmado` impede que a data conferida
        # seja trocada em silêncio — a comparação de peso deixaria passar.
        eid, _ = self._criar(
            con, titulo="Prova 5", data_inicio=1000, origem="programa_pdf",
            courseid=1,
        )
        con.execute("UPDATE eventos SET confirmado=1 WHERE id=?", (eid,))
        con.commit()
        _, acao = self._criar(
            con, titulo="Prova 5", data_inicio=9999, origem="calendario", courseid=1
        )
        assert acao == "ignorado"
        assert con.execute(
            "SELECT data_inicio FROM eventos WHERE id=?", (eid,)
        ).fetchone()[0] == 1000

    def test_fonte_fraca_nao_sobrescreve_forte(self, con):
        # O plano de ensino (peso 2) não corrige o calendário do Moodle
        # (peso 4): o PDF é lido por regex de tabela e erra mais.
        eid, _ = self._criar(
            con, titulo="Prova 2", data_inicio=1000, origem="calendario", courseid=1
        )
        _, acao = self._criar(
            con, titulo="Prova 2", data_inicio=2000, origem="programa_pdf", courseid=1
        )
        assert acao == "ignorado"
        assert con.execute(
            "SELECT data_inicio FROM eventos WHERE id=?", (eid,)
        ).fetchone()[0] == 1000

    def test_fonte_forte_sobrescreve_fraca_e_guarda_historico(self, con):
        eid, _ = self._criar(
            con, titulo="Prova 3", data_inicio=1000, origem="programa_pdf", courseid=1
        )
        _, acao = self._criar(
            con, titulo="Prova 3", data_inicio=2000, origem="calendario", courseid=1
        )
        assert acao == "atualizado"
        h = con.execute(
            "SELECT valor_antigo, valor_novo FROM historico_eventos WHERE evento_id=?",
            (eid,),
        ).fetchone()
        assert (h["valor_antigo"], h["valor_novo"]) == ("1000", "2000") or (
            int(h["valor_antigo"]), int(h["valor_novo"])
        ) == (1000, 2000)

    def test_manual_pode_corrigir_o_que_voce_confirmou(self, con):
        eid, _ = self._criar(
            con, titulo="Prova 4", data_inicio=1000, origem="manual",
            courseid=1, confirmado=1,
        )
        _, acao = self._criar(
            con, titulo="Prova 4", data_inicio=3000, origem="manual", courseid=1
        )
        assert acao == "atualizado"


# --------------------------------------------------------------------------
# 3. programa.py nunca confirma sozinho
# --------------------------------------------------------------------------


CRONOGRAMA = """
Cronograma da disciplina

12/08  Apresentação do curso
26/08  Primeira prova
09/09  Crescimento econômico
21/10  Segunda prova
"""


class TestProgramaNaoConfirma:
    def test_datas_saem_sem_confirmacao_e_com_o_trecho(self):
        entradas = programa.parse_cronograma(CRONOGRAMA, 2026, 2)
        assert entradas, "o parser não achou nada — o teste cegou"
        provas = [e for e in entradas if e.avaliacao]
        assert provas, "não achou as provas no cronograma"
        for e in provas:
            # O trecho literal é o que permite conferir contra o PDF. Sem ele,
            # a data vira afirmação sem fonte.
            assert e.trecho.strip(), "entrada sem trecho de origem"
            assert str(e.data.year) == "2026"

    def test_extrator_grava_confirmado_zero(self, con):
        # A garantia real é no upsert: mesmo mandando, a origem programa_pdf
        # entra com confirmado=0.
        eid, _ = sync.upsert_evento(
            con, "20262", titulo="Primeira prova", data_inicio=1000,
            origem="programa_pdf", courseid=1, trecho="26/08 Primeira prova",
        )
        linha = con.execute(
            "SELECT confirmado, trecho_origem FROM eventos WHERE id=?", (eid,)
        ).fetchone()
        assert linha["confirmado"] == 0
        assert linha["trecho_origem"] == "26/08 Primeira prova"

    def test_codigo_do_extrator_nao_passa_confirmado_um(self):
        # Barreira contra alguém "melhorar" o extrator confirmando as datas.
        fonte = inspect.getsource(programa)
        assert "confirmado=1" not in fonte.replace(" ", "")


# --------------------------------------------------------------------------
# 4. companion.pode_enviar() decide envio de questionário
# --------------------------------------------------------------------------


def _sessao(**kw):
    campos = {
        "site": "20262", "courseid": 1, "quizid": 7, "attemptid": 9,
        "modo": "tentativa", "vale_nota": None, "nota_maxima": 10.0,
        "itens": [],
    }
    campos.update(kw)
    s = companion.Sessao.__new__(companion.Sessao)
    for k, v in campos.items():
        object.__setattr__(s, k, v)
    return s


class TestPodeEnviar:
    def test_revisao_nunca_envia(self, con):
        ok, motivo = companion.pode_enviar(_sessao(modo="revisao"), con)
        assert ok is False
        assert "finalizada" in motivo

    def test_sem_saber_se_vale_nota_recusa(self, con):
        # A dúvida conta contra enviar: enviar por engano não tem desfazer.
        ok, _ = companion.pode_enviar(_sessao(vale_nota=None), con)
        assert ok is False

    def test_pratica_pode(self, con):
        ok, _ = companion.pode_enviar(_sessao(vale_nota=False), con)
        assert ok is True

    def test_vale_nota_recusa_sem_politica(self, con):
        ok, _ = companion.pode_enviar(_sessao(vale_nota=True), con)
        assert ok is False


# --------------------------------------------------------------------------
# 5. memoria.VOLATIL recusa memorizar nota, prazo e status
# --------------------------------------------------------------------------


class TestMemoriaVolatil:
    @pytest.mark.parametrize("tipo", sorted(memoria.VOLATIL))
    def test_recusa_o_que_o_moodle_manda(self, con, tipo):
        msg = memoria.memorizar(con, "20262", "minha nota", tipo, 1)
        assert "Não memorizo" in msg
        assert con.execute("SELECT COUNT(*) FROM memoria_alias").fetchone()[0] == 0

    def test_recusa_explica_o_motivo(self, con):
        # A recusa tem de ensinar o caminho certo, senão o modelo tenta de novo.
        msg = memoria.memorizar(con, "20262", "minha nota", "nota", 1)
        assert "Moodle" in msg and "caminho" in msg

    def test_o_que_e_estavel_memoriza(self, con):
        memoria.memorizar(con, "20262", "macro", "curso", 6095)
        r = memoria.resolver(con, "20262", "macro")
        assert r is not None and r["alvo_id"] == 6095

    def test_volatil_e_disjunto_de_tipos_validos(self):
        # Um tipo nos dois conjuntos seria aceito ou recusado conforme a ordem
        # dos ifs — decisão por acaso.
        assert not (memoria.VOLATIL & memoria.TIPOS_VALIDOS)


# --------------------------------------------------------------------------
# 6. Cobertura é chaveada por sha de trecho, nunca por id
# --------------------------------------------------------------------------
#
# `trechos.id` é reciclado a cada `extrair --reindexar`: a tabela é apagada e
# reinserida. Isso já invalidou 172 referências de uma vez.


def _indexar(con, arquivo_id, textos, courseid=6095, nome="Aula 1.pdf"):
    """Insere um arquivo e seus trechos como o extrator faria."""
    import hashlib

    con.execute(
        """INSERT OR REPLACE INTO arquivos
           (id, site, courseid, nome, fileurl, ignorado)
           VALUES (?,?,?,?,?,0)""",
        (arquivo_id, "20262", courseid, nome, f"http://x/{arquivo_id}"),
    )
    shas = []
    for i, t in enumerate(textos, start=1):
        sha = hashlib.sha256(t.encode()).hexdigest()
        shas.append(sha)
        cur = con.execute(
            """INSERT INTO trechos
               (arquivo_id, ordinal, pagina, rotulo, titulo, texto, n_chars, sha256)
               VALUES (?,?,?,?,?,?,?,?)""",
            (arquivo_id, i, i, f"página {i}", None, t, len(t), sha),
        )
        # O FTS é tabela separada: sem alimentá-lo, a âncora léxica não acha
        # nada e a busca inteira fica muda.
        con.execute(
            "INSERT INTO trechos_fts (nome, texto, trecho_id) VALUES (?,?,?)",
            (nome, t, cur.lastrowid),
        )
    con.commit()
    return shas


class TestCoberturaPorSha:
    def test_sobrevive_a_reindexacao(self, con):
        textos = ["o modelo de Solow supõe retornos decrescentes", "a regra de ouro"]
        shas = _indexar(con, 1, textos)
        ids_antes = [r["id"] for r in con.execute("SELECT id FROM trechos")]
        memoria.registrar_cobertura(con, 6095, "topico", "Prova 1", shas)

        # `extrair --reindexar`: apaga tudo e reinsere. Os ids mudam.
        con.execute("DELETE FROM trechos")
        con.commit()
        _indexar(con, 1, textos)
        ids_depois = [r["id"] for r in con.execute("SELECT id FROM trechos")]
        assert ids_antes != ids_depois, "o teste não reciclou id nenhum"

        cobertos = memoria.shas_cobertos(con, 6095, "topico", "Prova 1")
        assert cobertos == set(shas), "a cobertura não sobreviveu à reindexação"

    def test_vetor_tambem_e_chaveado_por_sha(self, con):
        colunas = {r[1] for r in con.execute("PRAGMA table_info(vetores)")}
        assert "sha256" in colunas
        assert "trecho_id" not in colunas, "vetor voltou a depender de id"

    def test_cobertura_guarda_sha_e_nao_id(self, con):
        colunas = {r[1] for r in con.execute("PRAGMA table_info(cobertura)")}
        assert "trecho_sha" in colunas
        assert "trecho_id" not in colunas

    def test_produzido_nao_duplica(self, con):
        # NULL em courseid fazia o UNIQUE não valer e cada remontagem criava
        # uma linha nova, com a cobertura sempre vazia.
        a = memoria.registrar_cobertura(con, None, "apostila", "X", ["s1"])
        b = memoria.registrar_cobertura(con, None, "apostila", "X", ["s2"])
        assert a == b
        assert con.execute("SELECT COUNT(*) FROM produzido").fetchone()[0] == 1
        assert memoria.shas_cobertos(con, None, "apostila", "X") == {"s1", "s2"}

    def test_documento_sem_curso_e_adotado_quando_o_curso_aparece(self, con):
        antes = memoria.registrar_cobertura(con, None, "apostila", "Y", ["s1"])
        depois = memoria.registrar_cobertura(con, 6095, "apostila", "Y", ["s2"])
        assert con.execute("SELECT COUNT(*) FROM produzido").fetchone()[0] == 1
        assert memoria.shas_cobertos(con, 6095, "apostila", "Y") == {"s1", "s2"}
        assert antes == depois


# --------------------------------------------------------------------------
# 7. Informação derivada carrega a fonte
# --------------------------------------------------------------------------


class TestFonteJunto:
    def test_busca_devolve_arquivo_e_pagina(self, con):
        import busca

        _indexar(
            con, 1,
            [
                "o modelo de Solow supõe retornos decrescentes do capital",
                "a regra de ouro do estoque de capital maximiza o consumo",
                "crescimento endógeno e a economia das ideias",
            ],
        )
        achados = busca.buscar(con, "modelo de Solow", courseid=6095)
        assert achados, "a busca não achou o que acabou de ser indexado"
        for a in achados:
            # Sem arquivo e página a resposta vira "acho que os slides dizem",
            # que é exatamente o que este projeto não quer.
            assert a["nome"], "resultado sem nome de arquivo"
            assert a["rotulo"], "resultado sem página"
            assert a["sha256"], "resultado sem a chave do trecho"

    def test_questao_guarda_a_fonte(self, con):
        colunas = {r[1] for r in con.execute("PRAGMA table_info(questoes)")}
        assert "fonte_trecho" in colunas

    def test_evento_guarda_o_trecho_de_origem(self, con):
        colunas = {r[1] for r in con.execute("PRAGMA table_info(eventos)")}
        assert "trecho_origem" in colunas and "origem_ref" in colunas

    def test_escrita_guarda_o_payload(self, con):
        colunas = {r[1] for r in con.execute("PRAGMA table_info(log_escrita)")}
        assert "payload" in colunas


class TestSomenteLeituraParaAgente:
    """Invariante 1, estendida ao agente automático.

    O ouvinte do WhatsApp roda um Claude headless que alcança as ferramentas
    de escrita por `executar`. Uma instrução plantada no material lido poderia
    mandar publicar com confirmar=True, e o desenho é o agente PROPOR e o dono
    confirmar por fora. `FENIX_SOMENTE_LEITURA` torna a publicação impossível,
    não improvável.
    """

    def test_recusa_mesmo_com_confirmar(self, con, monkeypatch):
        import escrita
        monkeypatch.setenv("FENIX_SOMENTE_LEITURA", "1")
        r = escrita._executar(con, None, "forum_post", "alvo", "resumo",
                              "mod_forum_add_discussion_post", {}, confirmar=True)
        assert not r.ok and "somente leitura" in r.detalhe

    def test_sem_a_trava_o_caminho_normal_segue(self, con, monkeypatch):
        """A trava não pode virar um bloqueio permanente: sem a variável, o
        ensaio normal continua acontecendo."""
        import escrita
        monkeypatch.delenv("FENIX_SOMENTE_LEITURA", raising=False)
        r = escrita._executar(con, None, "forum_post", "alvo", "resumo",
                              "mod_forum_add_discussion_post", {}, confirmar=False)
        assert r.ensaio


class TestParedeDoSSO:
    """A UFMG pôs a API atrás do login do minhaUFMG (entre 18 e 21/09/2026).

    O erro antigo era "resposta não-JSON", e o agente gastou 15 turnos tentando
    contornar uma parede do servidor. E no download era pior: a página de
    login vinha com status 200 e seria gravada no lugar do PDF.
    """

    def _resposta(self, url_final, location=""):
        import httpx
        req = httpx.Request("POST", "https://virtual.ufmg.br/20262/x")
        historico = []
        if location:
            historico = [httpx.Response(302, headers={"location": location},
                                        request=req)]
        r = httpx.Response(200, text="<html>SSO - minhaUFMG</html>",
                           request=httpx.Request("GET", url_final))
        r.history = historico
        return r

    def test_login_do_sso_vira_erro_nomeado(self):
        import moodle_client
        r = self._resposta("https://sistemas.ufmg.br/idp/login.jsp",
                           "https://sistemas.ufmg.br/idp/profile/SAML2/Redirect/SSO")
        with pytest.raises(moodle_client.MoodleBloqueadoSSO, match="NÃO é o token"):
            moodle_client._conferir_sso(r, "teste")

    def test_resposta_normal_passa(self):
        import moodle_client
        r = self._resposta("https://virtual.ufmg.br/20262/webservice/rest/server.php")
        moodle_client._conferir_sso(r, "teste")     # não levanta

    def test_e_um_MoodleError(self):
        """Quem já trata MoodleError continua tratando — erro em lote não
        derruba a rodada do sync."""
        import moodle_client
        assert issubclass(moodle_client.MoodleBloqueadoSSO, moodle_client.MoodleError)

    def test_o_cliente_manda_o_cookie_do_sso(self):
        """Dos nove cookies do login, só `ufmg_saml_session` abre a API —
        medido um a um. Sem ele, toda chamada volta 302 para o SSO."""
        import moodle_client
        c = moodle_client.MoodleClient("https://virtual.ufmg.br/20262", "t",
                                       saml="valor-da-sessao")
        assert c._http.cookies.get(moodle_client.COOKIE_SAML,
                                   domain="virtual.ufmg.br") == "valor-da-sessao"

    def test_sem_cookie_nao_inventa(self):
        import moodle_client
        c = moodle_client.MoodleClient("https://virtual.ufmg.br/20262", "t")
        assert moodle_client.COOKIE_SAML not in c._http.cookies


class TestLoginAutomatico:
    """Autorizado pelo usuário em 21/09/2026: guardar as credenciais do
    minhaUFMG e refazer o login sozinho quando a sessão expira.

    A senha abre TODOS os sistemas da UFMG. As guardas abaixo existem para que
    a automação não a exponha nem bloqueie a conta insistindo.
    """

    def _resposta_sso(self):
        import httpx
        r = httpx.Response(200, text="login",
                           request=httpx.Request("GET", "https://sistemas.ufmg.br/idp/login.jsp"))
        return r

    def _resposta_ok(self):
        import httpx
        return httpx.Response(200, json={"userid": 1},
                              request=httpx.Request("POST", "https://virtual.ufmg.br/20262/x"))

    def test_renova_uma_vez_e_repete_a_chamada(self, monkeypatch):
        import login_navegador
        import moodle_client
        c = moodle_client.MoodleClient("https://virtual.ufmg.br/20262", "t", alias="20262")
        respostas = iter([self._resposta_sso(), self._resposta_ok()])
        monkeypatch.setattr(c._http, "post", lambda *a, **k: next(respostas))
        chamadas = []
        monkeypatch.setattr(login_navegador, "renovar_automatico",
                            lambda alias, forcar=False: chamadas.append(alias) or (True, "ok"))
        monkeypatch.setattr(moodle_client, "_segredo", lambda a, s, campo: "cookie-novo")
        monkeypatch.setattr(moodle_client, "load_sites", lambda: {"20262": {}})
        assert c.call("core_webservice_get_site_info") == {"userid": 1}
        assert chamadas == ["20262"], "renova UMA vez"

    def test_renovacao_falhou_da_o_erro_nomeado(self, monkeypatch):
        import login_navegador
        import moodle_client
        c = moodle_client.MoodleClient("https://virtual.ufmg.br/20262", "t", alias="20262")
        monkeypatch.setattr(c._http, "post", lambda *a, **k: self._resposta_sso())
        monkeypatch.setattr(login_navegador, "renovar_automatico",
                            lambda alias, forcar=False: (False, "senha recusada"))
        with pytest.raises(moodle_client.MoodleBloqueadoSSO):
            c.call("core_webservice_get_site_info")

    def test_da_para_desligar(self, monkeypatch):
        import login_navegador
        import moodle_client
        monkeypatch.setenv("MOODLE_SEM_LOGIN_AUTOMATICO", "1")
        chamou = []
        monkeypatch.setattr(login_navegador, "renovar_automatico",
                            lambda *a, **k: chamou.append(1) or (True, ""))
        c = moodle_client.MoodleClient("https://virtual.ufmg.br/20262", "t", alias="20262")
        assert c._precisa_renovar(self._resposta_sso()) is False and chamou == []

    def test_no_maximo_uma_tentativa_a_cada_15_min(self, monkeypatch, tmp_path):
        """Insistir com senha trocada aciona o captcha do SSO e bloqueia a
        conta. A segunda tentativa dentro da janela nem abre navegador."""
        import login_navegador
        marca = tmp_path / "marca"
        marca.touch()
        monkeypatch.setattr(login_navegador, "_MARCA_TENTATIVA", marca)
        monkeypatch.setattr(login_navegador, "tem_credenciais", lambda: True)
        ok, motivo = login_navegador.renovar_automatico("20262")
        assert not ok and "15 min" in motivo

    def test_sem_credenciais_nao_tenta(self, monkeypatch):
        import login_navegador
        monkeypatch.setattr(login_navegador, "tem_credenciais", lambda: False)
        assert login_navegador.renovar_automatico("20262", forcar=True) == (
            False, "sem credenciais no chaveiro")

    def test_senha_nao_vem_por_argumento_nem_ambiente(self):
        """Argumento fica no histórico do shell e na lista de processos;
        variável de ambiente vaza para todo processo filho."""
        import inspect
        import login_navegador
        fonte = inspect.getsource(login_navegador.main)
        assert "--senha" not in fonte and "--password" not in fonte
        assert "environ" not in inspect.getsource(login_navegador.guardar_credenciais)


class TestCookieRecusado:
    """24/09/2026: a UFMG tirou a sessão da frente da API, e o cookie vencido
    passou a ser recusado com 200 VAZIO + ordem de apagar o cookie — sem
    redirecionamento, então parecia "resposta não-JSON" sem causa."""

    def _vazia(self):
        import httpx
        return httpx.Response(
            200, content=b"",
            headers={"content-type": "text/plain", "set-cookie":
                     "ufmg_saml_session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT"},
            request=httpx.Request("POST", "https://virtual.ufmg.br/20262/x"))

    def _ok(self):
        import httpx
        return httpx.Response(200, json={"userid": 7},
                              request=httpx.Request("POST", "https://virtual.ufmg.br/20262/x"))

    def test_descarta_o_cookie_e_repete(self, monkeypatch):
        import moodle_client
        c = moodle_client.MoodleClient("https://virtual.ufmg.br/20262", "t", saml="vencido")
        respostas = iter([self._vazia(), self._ok()])
        monkeypatch.setattr(c._http, "post", lambda *a, **k: next(respostas))
        assert c.call("core_webservice_get_site_info") == {"userid": 7}
        assert moodle_client.COOKIE_SAML not in c._http.cookies

    def test_resposta_vazia_sem_ordem_de_apagar_nao_e_isso(self):
        import httpx
        import moodle_client
        r = httpx.Response(200, content=b"",
                           request=httpx.Request("POST", "https://virtual.ufmg.br/x"))
        assert moodle_client._cookie_recusado(r) is False

    def test_resposta_com_corpo_nao_e_isso(self):
        import moodle_client
        assert moodle_client._cookie_recusado(self._ok()) is False

    def test_resposta_com_corpo_e_ordem_de_apagar_nao_repete(self):
        """Se o gateway apaga o cookie mas responde com conteúdo, a resposta
        vale: repetir a chamada duplicaria uma escrita."""
        import httpx
        import moodle_client
        r = httpx.Response(200, json={"ok": 1},
                           headers={"set-cookie": "ufmg_saml_session=; Path=/"},
                           request=httpx.Request("POST", "https://virtual.ufmg.br/x"))
        assert moodle_client._cookie_recusado(r) is False

"""Agenda do Mac: o que a escrita não pode fazer, sem tocar no Calendário.

Nenhum teste aqui fala com o Calendar.app. `_osascript` é trocado por um
gravador, e o ocupado vem de uma lista. O que se testa é a fronteira: ensaio
não grava, só o confirmado vira compromisso, o igual não é regravado, estudo
não cai em cima de reunião, e só se apaga o que tem a marca do Fênix.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import agenda_mac


@pytest.fixture()
def agenda(tmp_path, monkeypatch):
    """Agenda escolhida, Calendário falso. Devolve a lista de chamadas."""
    cfg = tmp_path / "agenda_mac.json"
    cfg.write_text('{"agenda": "Teste", "ocupado": ["Teste"]}')
    monkeypatch.setattr(agenda_mac, "CONFIG", cfg)
    monkeypatch.delenv("FENIX_SOMENTE_LEITURA", raising=False)
    chamadas: list[tuple] = []

    def falso(script, *args, timeout=60):
        chamadas.append((script, args))
        if script is agenda_mac._AS_APAGAR:
            return "apagado"
        return f"criado|UID-{len(chamadas)}"

    monkeypatch.setattr(agenda_mac, "_osascript", falso)
    monkeypatch.setattr(agenda_mac, "ocupados", lambda a, b, contas=None: [])
    return chamadas


def _evento(con, **kw):
    campos = {
        "site": "20262", "courseid": 9065, "titulo": "Prova I", "tipo": "prova",
        "data_inicio": int(time.time()) + 5 * 86400, "origem": "manual",
        "confirmado": 1, "cancelado": 0,
    }
    campos.update(kw)
    cur = con.execute(
        f"INSERT INTO eventos ({', '.join(campos)}) VALUES ({', '.join('?' * len(campos))})",
        tuple(campos.values()),
    )
    con.commit()
    return cur.lastrowid


def _bloco(inicio="2030-03-04T14:00", minutos=90, titulo="ERU — seção 8"):
    return agenda_mac.itens_de_estudo([{"titulo": titulo, "inicio": inicio, "minutos": minutos}])


class TestEnsaio:
    # Invariante 1 vale para a agenda: pedir para planejar não é pedir para
    # gravar. Um padrão `confirmar=True` encheria a agenda de trabalho, que
    # colegas veem, sem ninguém ter mandado.
    def test_padrao_e_ensaio(self):
        import inspect

        for f in (agenda_mac.aplicar, agenda_mac.sincronizar_prazos):
            assert inspect.signature(f).parameters["confirmar"].default is False

    def test_ensaio_nao_toca_no_calendario(self, con, agenda):
        _evento(con)
        r = agenda_mac.sincronizar_prazos(con)
        assert r.ensaio and len(r.criar) == 1
        assert agenda == []
        assert con.execute("SELECT count(*) FROM agenda_itens").fetchone()[0] == 0

    def test_somente_leitura_recusa_mesmo_confirmando(self, con, agenda, monkeypatch):
        monkeypatch.setenv("FENIX_SOMENTE_LEITURA", "1")
        _evento(con)
        r = agenda_mac.sincronizar_prazos(con, confirmar=True)
        assert r.recusa and agenda == []


class TestSoConfirmadoViraCompromisso:
    # Invariante 2/3: data lida do plano de ensino entra com confirmado=0.
    # Na agenda do celular ela seria uma prova que talvez não exista.
    def test_nao_confirmado_fica_fora(self, con, agenda):
        _evento(con, confirmado=0, origem="programa_pdf", titulo="TERCEIRA AVALIAÇÃO")
        _evento(con, titulo="Prova I")
        titulos = [it.titulo for it in agenda_mac.itens_de_prazos(con)]
        assert titulos == ["Prova I"]

    def test_cancelado_sai_da_agenda(self, con, agenda):
        eid = _evento(con)
        agenda_mac.sincronizar_prazos(con, confirmar=True)
        con.execute("UPDATE eventos SET cancelado = 1 WHERE id = ?", (eid,))
        con.commit()
        r = agenda_mac.sincronizar_prazos(con, confirmar=True)
        assert [a["chave"] for a in r.apagar] == [f"evento:20262:{eid}"]
        assert agenda[-1][0] is agenda_mac._AS_APAGAR

    def test_hora_zero_vira_dia_inteiro(self, con, agenda):
        # 00:00 é "hora não informada"; um bloco de madrugada seria inventar.
        from datetime import datetime, timedelta

        d = (datetime.now() + timedelta(days=10)).replace(hour=0, minute=0, second=0, microsecond=0)
        _evento(con, data_inicio=int(d.timestamp()))
        (it,) = agenda_mac.itens_de_prazos(con)
        assert it.dia_inteiro


class TestIdempotencia:
    def test_igual_nao_regrava(self, con, agenda):
        _evento(con)
        agenda_mac.sincronizar_prazos(con, confirmar=True)
        n = len(agenda)
        r = agenda_mac.sincronizar_prazos(con, confirmar=True)
        assert r.iguais == 1 and len(agenda) == n

    def test_mudou_a_data_move_o_mesmo_evento(self, con, agenda):
        # Mover tem de reusar o uid gravado; criar outro deixaria a prova
        # duas vezes na agenda, uma delas na data velha.
        eid = _evento(con)
        agenda_mac.sincronizar_prazos(con, confirmar=True)
        uid = con.execute("SELECT uid FROM agenda_itens").fetchone()[0]
        con.execute("UPDATE eventos SET data_inicio = data_inicio + 86400 WHERE id = ?", (eid,))
        con.commit()
        r = agenda_mac.sincronizar_prazos(con, confirmar=True)
        assert len(r.atualizar) == 1 and not r.criar
        assert agenda[-1][1][1] == uid


class TestConflito:
    def test_estudo_em_cima_de_compromisso_e_pulado(self, con, agenda, monkeypatch):
        (it,) = _bloco()
        reuniao = [{"inicio": it.inicio, "fim": it.fim, "titulo": "Reunião",
                    "marca": "", "agenda": "Teste"}]
        monkeypatch.setattr(agenda_mac, "ocupados", lambda a, b, contas=None: reuniao)
        r = agenda_mac.aplicar(con, [it], confirmar=True)
        assert r.conflitos and not r.criar and agenda == []

    def test_sobrepor_libera(self, con, agenda, monkeypatch):
        (it,) = _bloco()
        reuniao = [{"inicio": it.inicio, "fim": it.fim, "titulo": "Reunião",
                    "marca": "", "agenda": "Teste"}]
        monkeypatch.setattr(agenda_mac, "ocupados", lambda a, b, contas=None: reuniao)
        r = agenda_mac.aplicar(con, [it], confirmar=True, sobrepor=True)
        assert len(r.criar) == 1

    def test_o_proprio_bloco_nao_conflita_consigo(self, con, agenda, monkeypatch):
        # Depois de gravado, o bloco aparece no ocupado. Remarcar o mesmo
        # bloco com outra nota não pode ser barrado por ele mesmo.
        (it,) = _bloco()
        eu = [{"inicio": it.inicio, "fim": it.fim, "titulo": it.titulo,
               "marca": it.marca, "agenda": "Teste"}]
        monkeypatch.setattr(agenda_mac, "ocupados", lambda a, b, contas=None: eu)
        assert len(agenda_mac.aplicar(con, [it]).criar) == 1

    def test_prova_do_fenix_conflita(self, con, agenda, monkeypatch):
        (it,) = _bloco()
        prova = [{"inicio": it.inicio, "fim": it.fim, "titulo": "[ECN231] Prova I",
                  "marca": "fenix://evento:20262:1", "agenda": "Teste"}]
        monkeypatch.setattr(agenda_mac, "ocupados", lambda a, b, contas=None: prova)
        assert agenda_mac.aplicar(con, [it]).conflitos

    def test_ocupado_ilegivel_nao_vira_livre(self, con, agenda, monkeypatch):
        def quebra(a, b, contas=None):
            raise RuntimeError("banco trancado")

        monkeypatch.setattr(agenda_mac, "ocupados", quebra)
        r = agenda_mac.aplicar(con, _bloco(), confirmar=True)
        assert r.erros and agenda == []


class TestSoMexeNoQueEDoFenix:
    # A agenda é a de trabalho, com 342 eventos de outras pessoas. Apagar ou
    # alterar por uid sem conferir a marca apagaria reunião alheia se um uid
    # fosse parar na tabela errada. A conferência é no AppleScript, no
    # instante do toque, e não só na tabela.
    def test_apagar_confere_a_marca_antes_do_delete(self):
        s = agenda_mac._AS_APAGAR
        confere = s.index("does not contain (item 3 of argv)")
        assert confere < s.index("delete ev")

    def test_alterar_confere_a_marca_antes_de_mexer(self):
        s = agenda_mac._AS_GRAVAR
        confere = s.index("does not contain marca")
        assert confere < s.index("set start date of ev")
        assert confere < s.index("set summary of ev")

    def test_marca_vai_nas_notas_nao_no_url(self, con, agenda):
        # A conta Google descarta o `url` em silêncio. Marca no url = evento
        # que o próprio Fênix não consegue mais mover nem apagar.
        assert "url:" not in agenda_mac._AS_GRAVAR
        (it,) = _bloco()
        agenda_mac.aplicar(con, [it], confirmar=True)
        # argv: agenda, uid, título, início, fim, dia inteiro, notas, marca, regra, local
        args = agenda[0][1]
        assert args[7] == it.marca and it.marca in args[6]

    def test_apagar_manda_a_chave_exata(self, con, agenda):
        (it,) = _bloco()
        agenda_mac.aplicar(con, [it], confirmar=True)
        agenda_mac.aplicar(con, [], apagar=[it.chave], confirmar=True)
        assert agenda[-1][1][-1] == it.marca

    def test_marca_e_lida_das_notas(self):
        assert agenda_mac._marca_em("estudar\nfenix://estudo:abc123") == "fenix://estudo:abc123"
        assert agenda_mac._marca_em("reunião de equipe") == ""
        assert agenda_mac._marca_em(None) == ""

    def test_desmarcar_so_o_que_esta_na_tabela(self, con, agenda):
        r = agenda_mac.aplicar(con, [], apagar=["qualquer-uid-de-fora"], confirmar=True)
        assert not r.apagar and agenda == []


class TestLog:
    def test_falha_tambem_vai_para_o_log(self, con, agenda, monkeypatch):
        def falha(script, *args, timeout=60):
            raise RuntimeError("Calendar não respondeu")

        monkeypatch.setattr(agenda_mac, "_osascript", falha)
        r = agenda_mac.aplicar(con, _bloco(), confirmar=True)
        assert r.erros
        linha = con.execute("SELECT acao, erro FROM log_escrita").fetchone()
        assert linha["acao"] == "agenda_gravar" and "não respondeu" in linha["erro"]

    def test_sucesso_vai_para_o_log(self, con, agenda):
        agenda_mac.aplicar(con, _bloco(), confirmar=True)
        assert con.execute(
            "SELECT acao FROM log_escrita"
        ).fetchone()["acao"] == "agenda_criado"


class TestLivres:
    def test_janelas_entre_compromissos(self, monkeypatch):
        from datetime import datetime

        base = datetime(2030, 3, 4)
        h = lambda hh, mm=0: int(base.replace(hour=hh, minute=mm).timestamp())
        ocup = [
            {"inicio": h(8), "fim": h(12), "titulo": "a", "marca": ""},
            {"inicio": h(12, 30), "fim": h(13, 45), "titulo": "b", "marca": ""},
            {"inicio": h(18), "fim": h(23), "titulo": "c", "marca": ""},
        ]
        monkeypatch.setattr(agenda_mac, "ocupados", lambda a, b, contas=None: ocup)
        janelas = agenda_mac.livres("2030-03-04", dias=1, das="08:00", ate="22:00")
        # 12:00–12:30 tem 30 min, abaixo do mínimo de 60.
        assert janelas == [(h(13, 45), h(18))]


class TestNaoOcupa:
    # "Último ônibus (não estarei presencialmente)" é aviso para os colegas,
    # das 18h à meia-noite todo dia. Contado como compromisso, nenhuma noite
    # teria horário livre e todo bloco de estudo seria pulado.
    def test_aviso_configurado_nao_ocupa(self, tmp_path, monkeypatch):
        import sqlite3

        banco = tmp_path / "Calendar.sqlitedb"
        c = sqlite3.connect(banco)
        c.executescript("""
            CREATE TABLE Store (ROWID INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, store_id INT, title TEXT);
            CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY, summary TEXT,
                description TEXT, all_day INT, hidden INT, status INT, availability INT);
            CREATE TABLE OccurrenceCache (event_id INT, calendar_id INT,
                occurrence_date REAL, occurrence_end_date REAL);
            INSERT INTO Store VALUES (1, 'Teste');
            INSERT INTO Calendar VALUES (1, 1, 'Teste');
            INSERT INTO CalendarItem VALUES (1, 'Último ônibus (não estarei presencialmente)', '', 0, 0, 0, 0);
            INSERT INTO CalendarItem VALUES (2, 'Reunião', '', 0, 0, 0, 0);
            INSERT INTO OccurrenceCache VALUES (1, 1, 1000, 2000);
            INSERT INTO OccurrenceCache VALUES (2, 1, 1000, 2000);
        """)
        c.commit()
        c.close()
        cfg = tmp_path / "agenda_mac.json"
        cfg.write_text('{"agenda": "Teste", "ocupado": ["Teste"], "nao_ocupa": ["último ônibus"]}')
        monkeypatch.setattr(agenda_mac, "CONFIG", cfg)
        monkeypatch.setattr(agenda_mac, "CALENDARIO_DB", banco)
        base = agenda_mac._EPOCA_APPLE
        titulos = [o["titulo"] for o in agenda_mac.ocupados(base, base + 5000)]
        assert titulos == ["Reunião"]


class TestRecorrencia:
    def test_regra_e_local_chegam_ao_applescript(self, con, agenda):
        it = agenda_mac.Item("aula:ECN054:ter", "Aula · Macro III", 1893996000, 1894002000,
                             origem="rotina", recorrencia="FREQ=WEEKLY;UNTIL=20261205T025959Z",
                             local="Sala 4109")
        agenda_mac.aplicar(con, [it], confirmar=True)
        args = agenda[0][1]
        assert args[-2:] == ("FREQ=WEEKLY;UNTIL=20261205T025959Z", "Sala 4109")

    def test_mudar_a_regra_regrava(self, con, agenda):
        # A assinatura precisa ver a regra: mudar o fim do semestre sem
        # regravar deixaria a aula repetindo além do que devia.
        a = agenda_mac.Item("k", "Aula", 1, 2, recorrencia="FREQ=WEEKLY;UNTIL=20261205T025959Z")
        b = agenda_mac.Item("k", "Aula", 1, 2, recorrencia="FREQ=WEEKLY;UNTIL=20261212T025959Z")
        assert a.assinatura() != b.assinatura()


class TestSoLeituraPeloWhatsApp:
    # Decisão do usuário: pelo WhatsApp a agenda é consultada, não escrita.
    # O ouvinte sobe o servidor do Fênix inteiro, então sem esta trava o
    # agente gravaria na agenda de trabalho com confirmar=True.
    @pytest.fixture
    def leitura(self, monkeypatch):
        monkeypatch.setenv("FENIX_AGENDA_SO_LEITURA", "1")

    def test_confirmar_e_recusado_sem_tocar_no_calendario(self, con, agenda, leitura):
        _evento(con)
        for r in (agenda_mac.aplicar(con, _bloco(), confirmar=True),
                  agenda_mac.sincronizar_prazos(con, confirmar=True),
                  agenda_mac.aplicar(con, [], apagar=["x"], confirmar=True)):
            assert r.recusa
        assert agenda == []
        assert con.execute("SELECT count(*) FROM agenda_itens").fetchone()[0] == 0

    def test_ensaio_continua_mostrando(self, con, agenda, leitura):
        r = agenda_mac.aplicar(con, _bloco())
        assert r.criar and not r.recusa and agenda == []

    def test_ajustar_evento_e_recusado(self, con, leitura):
        import server

        eid = _evento(con, titulo="Prova 2 - Micro II")
        assert "Recusado" in server.ajustar_evento("Micro II", data="2030-10-23T09:20")
        antes = con.execute("SELECT data_inicio FROM eventos WHERE id = ?", (eid,)).fetchone()[0]
        assert antes != agenda_mac._instante("2030-10-23T09:20")

    def test_ponte_liga_a_trava(self):
        fenix_zap = pytest.importorskip("fenix_zap")  # ponte do WhatsApp: só na versão completa

        assert fenix_zap.ambiente() == {"FENIX_AGENDA_SO_LEITURA": "1"}


class TestAtalhosDeAgenda:
    def test_dia_por_extenso(self):
        from datetime import date, timedelta

        fenix_zap = pytest.importorskip("fenix_zap")  # ponte do WhatsApp: só na versão completa

        hoje = date.today()
        assert fenix_zap._dia("amanhã") == hoje + timedelta(days=1)
        assert fenix_zap._dia("no sábado") == hoje + timedelta(days=(5 - hoje.weekday()) % 7)
        assert fenix_zap._dia("26/09/2030") == date(2030, 9, 26)
        assert fenix_zap._dia("sei lá") is None

    def test_pergunta_livre_vai_para_o_agente(self):
        # "Atalho não adivinha": só dispara com o comando no começo.
        fenix_zap = pytest.importorskip("fenix_zap")  # ponte do WhatsApp: só na versão completa

        assert fenix_zap.atalho("o que tenho livre no sábado?") is None



class TestAjustarEvento:
    def test_muda_data_confirma_e_guarda_historico(self, con, monkeypatch):
        import server

        import db

        caminho = con.execute("PRAGMA database_list").fetchone()[2]
        monkeypatch.setattr(server, "_con", lambda: db.conectar(Path(caminho)))
        eid = _evento(con, titulo="Prova 2 - Micro II", confirmado=0, origem="programa_pdf")
        r = server.ajustar_evento("Micro II", data="2030-10-23T09:20")
        e = con.execute("SELECT * FROM eventos WHERE id = ?", (eid,)).fetchone()
        assert "Confirmado" in r and e["confirmado"] == 1 and e["origem"] == "manual"
        assert e["data_inicio"] == agenda_mac._instante("2030-10-23T09:20")
        assert con.execute("SELECT count(*) FROM historico_eventos WHERE evento_id = ?",
                           (eid,)).fetchone()[0] == 1

    def test_ambiguo_e_recusado(self, con, monkeypatch):
        import server

        import db

        caminho = con.execute("PRAGMA database_list").fetchone()[2]
        monkeypatch.setattr(server, "_con", lambda: db.conectar(Path(caminho)))
        _evento(con, titulo="Prova 2 - Micro II")
        _evento(con, titulo="Prova 3 - Micro II")
        r = server.ajustar_evento("Micro II", data="2030-10-23T09:20")
        assert "Mais de um" in r

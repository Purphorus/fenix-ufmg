"""O .ics é conferido por um parser independente, não por inspeção visual.

Formato que outro programa vai ler tem de ser validado por outro programa: um
arquivo que "parece certo" e o calendário recusa é pior que não exportar.
"""

from __future__ import annotations

import pytest

import calendario

icalendar = pytest.importorskip("icalendar", reason="dependência só de teste")


def _evento(con, **kw):
    campos = {
        "site": "20262", "courseid": 6095, "titulo": "Prova 1", "tipo": "prova",
        "data_inicio": 1789036200, "origem": "manual", "confirmado": 1,
        "trecho_origem": None, "origem_ref": None,
    }
    campos.update(kw)
    con.execute(
        """INSERT INTO eventos
           (site, courseid, titulo, tipo, data_inicio, origem, confirmado,
            trecho_origem, origem_ref)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        tuple(campos[k] for k in (
            "site", "courseid", "titulo", "tipo", "data_inicio", "origem",
            "confirmado", "trecho_origem", "origem_ref",
        )),
    )
    con.commit()


def _ler(texto):
    return list(icalendar.Calendar.from_ical(texto).walk("VEVENT"))


class TestSoOConfirmadoSai:
    def test_automatico_fica_de_fora_por_padrao(self, con):
        _evento(con, titulo="Confirmada", confirmado=1)
        _evento(con, titulo="Chutada", confirmado=0, origem="programa_pdf")
        texto, n, _ = calendario.montar(calendario.eventos_para_exportar(con))
        assert n == 1
        eventos = _ler(texto)
        assert [str(e.get("SUMMARY")) for e in eventos] == ["Confirmada"]

    def test_com_todos_o_nao_confirmado_vai_marcado(self, con):
        _evento(con, titulo="Chutada", confirmado=0, origem="programa_pdf")
        texto, n, _ = calendario.montar(
            calendario.eventos_para_exportar(con, todos=True)
        )
        assert n == 1
        e = _ler(texto)[0]
        # No celular só o título aparece: a ressalva tem de estar nele.
        assert str(e.get("SUMMARY")).startswith("[não confirmado]")
        assert str(e.get("STATUS")) == "TENTATIVE"

    def test_confirmado_sai_como_compromisso(self, con):
        _evento(con)
        e = _ler(calendario.montar(calendario.eventos_para_exportar(con))[0])[0]
        assert str(e.get("STATUS")) == "CONFIRMED"

    def test_cancelado_nunca_sai(self, con):
        _evento(con, titulo="Cancelada")
        con.execute("UPDATE eventos SET cancelado = 1")
        con.commit()
        _, n, _ = calendario.montar(calendario.eventos_para_exportar(con, todos=True))
        assert n == 0

    def test_sem_data_fica_de_fora(self, con):
        _evento(con, data_inicio=None)
        _, n, pulados = calendario.montar(
            calendario.eventos_para_exportar(con, todos=True)
        )
        # Exportar sem data significaria inventar uma.
        assert (n, pulados) == (0, 1)


class TestFormato:
    def test_titulo_longo_com_acento_sobrevive(self, con):
        titulo = (
            "1a Atividade Prática: criando Mapas Temáticos no QGIS está "
            "marcado(a) para esta data — e ainda continua"
        )
        _evento(con, titulo=titulo)
        texto = calendario.montar(calendario.eventos_para_exportar(con))[0]
        # Nenhuma linha pode passar de 75 octetos, e o título tem de voltar
        # inteiro: o dobramento por byte comeria o "á".
        for linha in texto.split("\r\n"):
            assert len(linha.encode("utf-8")) <= 75, linha
        assert str(_ler(texto)[0].get("SUMMARY")) == titulo

    def test_virgula_e_ponto_e_virgula_nao_quebram(self, con):
        titulo = "Prova 1, unidade 3; leve calculadora"
        _evento(con, titulo=titulo)
        texto = calendario.montar(calendario.eventos_para_exportar(con))[0]
        assert str(_ler(texto)[0].get("SUMMARY")) == titulo

    def test_descricao_leva_a_fonte(self, con):
        _evento(
            con, confirmado=0, origem="programa_pdf",
            trecho_origem="26/08 Primeira prova", origem_ref="plano.pdf",
        )
        texto = calendario.montar(
            calendario.eventos_para_exportar(con, todos=True)
        )[0]
        d = str(_ler(texto)[0].get("DESCRIPTION"))
        # Invariante 7: informação derivada anda com a fonte.
        assert "26/08 Primeira prova" in d and "plano.pdf" in d

    def test_uid_estavel_entre_exportacoes(self, con):
        _evento(con)
        a = calendario.montar(calendario.eventos_para_exportar(con))[0]
        b = calendario.montar(calendario.eventos_para_exportar(con))[0]
        # UID instável faria o calendário duplicar o evento a cada importação.
        assert str(_ler(a)[0].get("UID")) == str(_ler(b)[0].get("UID"))

    def test_calendario_vazio_ainda_e_valido(self, con):
        texto, n, _ = calendario.montar([])
        assert n == 0
        assert icalendar.Calendar.from_ical(texto) is not None

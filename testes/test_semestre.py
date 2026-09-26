"""Escopo de semestre: o bug que só aparece quando existir o segundo.

Hoje há um semestre só, e por isso "buscar em todos" e "buscar no corrente"
dão o mesmo resultado. No dia em que houver dois, a resposta para "o que o
material diz sobre X" traria o slide do ano passado sem avisar.
"""

from __future__ import annotations

import db
import busca
from testes.test_invariantes import _indexar


def _dois_semestres(con):
    """Mesmo assunto nos dois semestres, textos diferentes."""
    _indexar(con, 1, ["o modelo de Solow e o estado estacionário, versão de 2026"],
             courseid=6095, nome="Solow 2026.pdf")
    con.execute("UPDATE arquivos SET site = '20262' WHERE id = 1")
    _indexar(con, 2, ["o modelo de Solow e o estado estacionário, versão de 2025"],
             courseid=5000, nome="Solow 2025.pdf")
    con.execute("UPDATE arquivos SET site = '20251' WHERE id = 2")
    con.execute("INSERT INTO cursos (site, courseid, fullname) VALUES ('20262',6095,'Macro 2026')")
    con.execute("INSERT INTO cursos (site, courseid, fullname) VALUES ('20251',5000,'Macro 2025')")
    con.commit()
    db.registrar_semestre(con, "20262")
    db.registrar_semestre(con, "20251")


class TestSemestreCorrente:
    def test_o_corrente_e_o_maior_alias(self, con):
        _dois_semestres(con)
        assert db.site_atual(con) == "20262"

    def test_arquivar_o_corrente_promove_o_anterior(self, con):
        _dois_semestres(con)
        db.arquivar_semestre(con, "20262")
        assert db.site_atual(con) == "20251"

    def test_banco_sem_a_tabela_ainda_tem_escopo(self, con):
        # Banco de antes desta tabela não pode ficar sem semestre corrente.
        _indexar(con, 1, ["texto"], courseid=6095)
        con.execute("INSERT INTO cursos (site, courseid, fullname) VALUES ('20262',6095,'x')")
        con.commit()
        assert db.site_atual(con) == "20262"

    def test_sem_nada_devolve_none(self, con):
        assert db.site_atual(con) is None


class TestEscopoDaBusca:
    def test_sem_filtro_a_busca_mistura_os_dois(self, con):
        _dois_semestres(con)
        achados = busca.buscar(con, "estado estacionário", limite=8)
        sites = {a["site"] for a in achados}
        # É o comportamento cru de `busca`: por isso o ESCOPO é decidido em
        # cima, por `_escopo_semestre`.
        assert len(sites) == 2

    def test_com_o_semestre_corrente_so_vem_ele(self, con):
        _dois_semestres(con)
        achados = busca.buscar(
            con, "estado estacionário", site=db.site_atual(con), limite=8
        )
        assert achados
        assert {a["site"] for a in achados} == {"20262"}
        assert all("2026" in a["nome"] for a in achados)

    def test_semestre_antigo_continua_alcancavel(self, con):
        _dois_semestres(con)
        achados = busca.buscar(con, "estado estacionário", site="20251", limite=8)
        assert achados and {a["site"] for a in achados} == {"20251"}

    def test_arquivado_sai_do_padrao_mas_nao_da_busca(self, con):
        _dois_semestres(con)
        db.arquivar_semestre(con, "20251")
        # Arquivar não apaga nada: o material continua achável quando pedido.
        assert busca.buscar(con, "estado estacionário", site="20251", limite=8)


class TestEscopoDoServidor:
    def test_curso_informado_dispensa_o_semestre(self, con):
        import server

        _dois_semestres(con)
        # O curso já implica o semestre; filtrar de novo só criaria a chance
        # de os dois discordarem.
        assert server._escopo_semestre(con, 6095, "") is None

    def test_padrao_e_o_corrente(self, con):
        import server

        _dois_semestres(con)
        assert server._escopo_semestre(con, 0, "") == "20262"

    def test_asterisco_libera_todos(self, con):
        import server

        _dois_semestres(con)
        assert server._escopo_semestre(con, 0, "*") is None

    def test_alias_explicito_manda(self, con):
        import server

        _dois_semestres(con)
        assert server._escopo_semestre(con, 0, "20251") == "20251"

    def test_avisa_que_existe_material_em_outro_semestre(self, con):
        _dois_semestres(con)
        assert db.tem_outro_semestre(con, "20262") is True
        con.execute("DELETE FROM arquivos WHERE site = '20251'")
        con.commit()
        assert db.tem_outro_semestre(con, "20262") is False

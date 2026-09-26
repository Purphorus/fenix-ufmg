"""`questionarios` e `atividade`: as duas ferramentas que faltavam.

No histórico real, "listar questionários da turma" e "ver o enunciado da
atividade" foram ao `indice` seis vezes e terminaram em `chamar_ws` com JSON
cru. Nenhum teste chama a rede: o cliente é falso e responde por função.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class Moodle:
    def __init__(self, respostas):
        self.respostas, self.chamadas = respostas, []

    def call(self, funcao, **k):
        self.chamadas.append(funcao)
        return self.respostas[funcao]

    def user_id(self):
        return 1


@pytest.fixture()
def srv(con, monkeypatch):
    import db
    import server

    caminho = con.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(server, "_con", lambda: db.conectar(Path(caminho)))
    return server


def _moodle(monkeypatch, srv, respostas):
    m = Moodle(respostas)
    monkeypatch.setattr(srv, "get_client", lambda *a: m)
    return m


QUIZ = {"quizzes": [{"id": 3711, "coursemodule": 127211, "course": 6095, "name": "Lista 1",
                     "timeopen": 0, "timeclose": 0, "attempts": 1, "intro": "<p>Resolva <b>tudo</b></p>"},
                    # outro questionário da mesma turma: tem de ser ignorado
                    {"id": 3800, "coursemodule": 132271, "course": 6095, "name": "Lista 2",
                     "timeopen": 0, "timeclose": 0, "attempts": 2, "intro": "OUTRO enunciado"}]}
TAREFA = {"courses": [{"assignments": [
    {"id": 3406, "intro": "<p>" + "palavra " * 1000 + "</p>", "duedate": 0},
    {"id": 9999, "intro": "OUTRA tarefa", "duedate": 0}]}]}


def test_questionarios_da_o_quizid_por_turma(srv, monkeypatch):
    _moodle(monkeypatch, srv, {"core_enrol_get_users_courses": [{"id": 6095, "fullname": "Macro III"}],
                               "mod_quiz_get_quizzes_by_courses": QUIZ})
    r = srv.questionarios()
    assert "## Macro III" in r and "quizid 3711" in r and "tentativas: 1" in r


class TestAtividade:
    def _modulos(self, con, *nomes):
        for i, (nome, tipo) in enumerate(nomes):
            con.execute("INSERT INTO modulos (site, courseid, cmid, modname, nome) VALUES (?,?,?,?,?)",
                        ("20262", 9065, 100 + i, tipo, nome))
        con.commit()

    def test_nome_ambiguo_lista_sem_ir_a_rede(self, con, srv, monkeypatch):
        self._modulos(con, ("1a Atividade Prática", "label"), ("1a Atividade Prática: QGIS", "assign"))
        m = _moodle(monkeypatch, srv, {})
        r = srv.atividade(busca="Atividade Prática")
        assert "cmid 100" in r and "cmid 101" in r and m.chamadas == []

    def test_nome_unico_vira_cmid_e_traz_enunciado_cortado(self, con, srv, monkeypatch):
        self._modulos(con, ("1a Atividade Prática: QGIS", "assign"))
        _moodle(monkeypatch, srv, {
            "core_course_get_course_module": {"cm": {"name": "QGIS", "modname": "assign",
                                                     "instance": 3406, "course": 9065}},
            "mod_assign_get_assignments": TAREFA})
        r = srv.atividade(busca="QGIS")
        assert "assignid 3406" in r and "<p>" not in r and "OUTRA" not in r
        # o enunciado não vira um texto_material: sai cortado
        assert len(r) < srv.ENUNCIADO_CHARS + 300

    def test_questionario_pelo_cmid(self, srv, monkeypatch):
        _moodle(monkeypatch, srv, {
            "core_course_get_course_module": {"cm": {"name": "Lista 1", "modname": "quiz",
                                                     "instance": 3711, "course": 6095}},
            "mod_quiz_get_quizzes_by_courses": QUIZ})
        r = srv.atividade(cmid=127211)
        assert "quizid 3711" in r and "Resolva tudo" in r and "OUTRO" not in r


class TestResolverCurso:
    """'macro' → 6095 sem chamada a mais. E o atalho do WhatsApp, que chamava
    memoria.resolver sem o site, dava TypeError em "notas macro"."""

    def _base(self, con):
        import memoria
        for cid, nome, curto in ((6095, "2026_2 - MACROECONOMIA III - TC", "ECN140"),
                                 (6738, "2026_2 - MICROECONOMIA II - TC", "ECN299"),
                                 (9065, "2026_2 - ECONOMIA REGIONAL E URBANA - TC", "ECN231")):
            con.execute("INSERT INTO cursos (site, courseid, fullname, shortname) VALUES ('20262',?,?,?)",
                        (cid, nome, curto))
        con.commit()
        memoria.memorizar(con, "20262", "macro", "curso", 6095)
        # apelido que não está em nome nenhum: só o que foi memorizado resolve
        memoria.memorizar(con, "20262", "mac3", "curso", 6095)

    def test_numero_apelido_nome_e_ambiguo(self, con, srv, monkeypatch):
        import db
        monkeypatch.setattr(db, "site_atual", lambda con: "20262")
        self._base(con)
        assert srv._resolver_curso(con, "6738") == (6738, "")
        assert srv._resolver_curso(con, "mac3") == (6095, "")
        assert srv._resolver_curso(con, "regional") == (9065, "")      # sem apelido
        cid, erro = srv._resolver_curso(con, "economia")
        assert cid == 0 and "Mais de uma" in erro                      # não chuta

    def test_notas_pelo_nome_numa_chamada(self, con, srv, monkeypatch):
        import db
        monkeypatch.setattr(db, "site_atual", lambda con: "20262")
        self._base(con)
        m = _moodle(monkeypatch, srv, {"gradereport_user_get_grade_items": {"usergrades": []}})
        srv.notas(curso="macro")
        assert m.chamadas == ["gradereport_user_get_grade_items"]
        m2 = _moodle(monkeypatch, srv, {})
        assert "Mais de uma" in srv.notas(curso="economia") and m2.chamadas == []

    def test_atalho_do_whatsapp_resolve(self, con, srv, monkeypatch):
        import db
        fenix_zap = pytest.importorskip("fenix_zap")  # ponte do WhatsApp: só na versão completa
        monkeypatch.setattr(db, "site_atual", lambda con: "20262")
        self._base(con)
        assert fenix_zap._curso(con, "macro") == 6095

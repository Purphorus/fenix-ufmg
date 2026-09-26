"""O simulado só vale se a questão puder ser conferida contra a fonte.

Questão inventada sobre matéria que o material não cobre é o pior resultado
possível deste projeto, e é justamente o que ninguém percebe ao estudar por
ela: o erro só aparece na prova.
"""

from __future__ import annotations

import json

import simulado


def _q(**kw):
    base = {
        "enunciado": "O que é o estado estacionário?",
        "correta": "b",
        "explicacao": "onde o investimento iguala a depreciação",
        "fonte_trecho": "Topico 5.pdf, página 30",
    }
    base.update(kw)
    return base


class TestFonteObrigatoria:
    def test_sem_fonte_e_recusada(self, con):
        r = simulado.guardar(con, 6095, "Solow", [_q(fonte_trecho="")])
        assert r.salvas == 0
        assert r.recusadas and "sem fonte_trecho" in r.recusadas[0]
        assert con.execute("SELECT COUNT(*) FROM questoes").fetchone()[0] == 0

    def test_fonte_so_de_espaco_tambem_e_recusada(self, con):
        r = simulado.guardar(con, 6095, "Solow", [_q(fonte_trecho="   ")])
        assert r.salvas == 0

    def test_sem_enunciado_e_recusada(self, con):
        r = simulado.guardar(con, 6095, "Solow", [_q(enunciado="")])
        assert r.salvas == 0

    def test_a_boa_passa_mesmo_com_ruim_no_lote(self, con):
        # Uma questão ruim não pode derrubar o lote: é a regra do projeto para
        # erro em lote.
        r = simulado.guardar(con, 6095, "Solow", [_q(), _q(fonte_trecho=""), _q(enunciado="outra")])
        assert r.salvas == 2 and len(r.recusadas) == 1

    def test_o_que_foi_salvo_guarda_a_fonte(self, con):
        simulado.guardar(con, 6095, "Solow", [_q()])
        linha = con.execute("SELECT fonte_trecho, topico FROM questoes").fetchone()
        assert linha["fonte_trecho"] == "Topico 5.pdf, página 30"
        assert linha["topico"] == "Solow"


class TestGabaritoSeparado:
    def test_sem_gabarito_por_padrao(self, con):
        simulado.guardar(con, 6095, "Solow", [_q()])
        texto = simulado.listar(con, 6095, "Solow")
        # O usuário resolve antes de ler a resposta; gabarito ao lado do
        # enunciado estraga isso.
        assert "onde o investimento iguala" not in texto
        assert "O que é o estado estacionário?" in texto

    def test_com_gabarito_sai_no_fim_e_com_a_fonte(self, con):
        simulado.guardar(con, 6095, "Solow", [_q()])
        texto = simulado.listar(con, 6095, "Solow", com_gabarito=True)
        assert texto.index("Gabarito") > texto.index("O que é o estado")
        assert "Topico 5.pdf, página 30" in texto

    def test_alternativas_quebradas_nao_derrubam(self, con):
        simulado.guardar(con, 6095, "Solow", [_q()])
        con.execute("UPDATE questoes SET alternativas = 'isto não é json'")
        con.commit()
        assert "O que é o estado" in simulado.listar(con, 6095, "Solow")


class TestResponder:
    def _uma(self, con):
        return simulado.guardar(con, 6095, "Solow", [_q()]).ids[0]

    def test_certo_e_errado(self, con):
        qid = self._uma(con)
        assert "✓" in simulado.responder(con, qid, "b")
        assert "✗" in simulado.responder(con, qid, "a")

    def test_caixa_nao_conta(self, con):
        qid = self._uma(con)
        assert "✓" in simulado.responder(con, qid, "B")

    def test_a_correcao_vem_com_a_fonte(self, con):
        qid = self._uma(con)
        assert "Topico 5.pdf, página 30" in simulado.responder(con, qid, "a")

    def test_sem_gabarito_nao_diz_que_errou(self, con):
        qid = simulado.guardar(con, 6095, "S", [_q(correta=None)]).ids[0]
        texto = simulado.responder(con, qid, "a")
        assert "✗" not in texto
        # acertou=NULL, e não 0: 0 significaria que a resposta estava errada.
        assert con.execute("SELECT acertou FROM respostas").fetchone()[0] is None

    def test_questao_inexistente_nao_explode(self, con):
        assert "não existe" in simulado.responder(con, 9999, "a")

    def test_questao_de_quiz_do_moodle_nao_entra_aqui(self, con):
        qid = self._uma(con)
        con.execute("UPDATE questoes SET attemptid = 88 WHERE id = ?", (qid,))
        con.commit()
        # `attemptid IS NULL` é o que separa gerada do material de capturada
        # do Moodle; misturar as duas confundiria o desempenho.
        assert "não existe" in simulado.responder(con, qid, "b")
        assert simulado.ja_perguntado(con, 6095, "Solow") == []


class TestNaoRepetir:
    def test_ja_perguntado_lista_o_que_existe(self, con):
        simulado.guardar(con, 6095, "Solow", [_q(), _q(enunciado="outra coisa")])
        feitas = simulado.ja_perguntado(con, 6095, "Solow")
        assert len(feitas) == 2

    def test_outro_topico_nao_contamina(self, con):
        simulado.guardar(con, 6095, "Solow", [_q()])
        assert simulado.ja_perguntado(con, 6095, "Romer") == []

    def test_material_registra_cobertura(self, con):
        import memoria
        from testes.test_invariantes import _indexar

        _indexar(con, 1, [
            "o estado estacionário ocorre quando o investimento iguala a depreciação",
            "a regra de ouro maximiza o consumo per capita",
        ])
        achados, _ = simulado.material(con, 6095, "estado estacionário", limite=2)
        assert achados
        # Registrado pela ferramenta, não pelo modelo: ele esquece.
        cobertos = memoria.shas_cobertos(con, 6095, simulado.ESCOPO, "estado estacionário")
        assert cobertos == {a["sha256"] for a in achados}

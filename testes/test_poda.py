"""A poda devolve a frase que responde — e só quando sabe qual é.

O risco da poda é cortar a explicação e devolver um fragmento com cara de
resposta. Por isso ela é conservadora: sem frase que case com a consulta, o
trecho segue inteiro como antes; e ela só liga onde há degrau seguinte
(`buscar_material` → `trecho_material`).
"""

from __future__ import annotations

import busca
from testes.test_embedding import falso  # noqa: F401 — fixture

TIR = (
    "Taxa Interna de Retorno\n"
    "O fluxo de caixa do projeto é descontado a uma taxa.\n"
    "A taxa interna de retorno (TIR) é a taxa que zera o valor presente líquido.\n"
    "Exemplo numérico com cinco períodos e investimento inicial de 100 mil reais, "
    "seguido de entradas anuais constantes de 30 mil reais ao longo de cinco anos.\n"
    "Comparação com o payback simples, que ignora o valor do dinheiro no tempo e por "
    "isso pode aceitar projetos ruins que destroem valor para o acionista.\n"
    "Outras métricas: índice de lucratividade, VPL anualizado e payback descontado."
)


class TestPodarTrecho:
    def test_fica_a_frase_que_casa_e_a_anterior(self):
        p = busca.podar_trecho("taxa interna de retorno", TIR)
        assert "zera o valor presente líquido" in p
        assert "descontado a uma taxa" in p          # a anterior, que introduz
        assert "Exemplo numérico" not in p
        assert p.endswith("…")
        assert len(p) < len(TIR)

    def test_a_anterior_entra_mesmo_sem_casar(self):
        # "TIR" só aparece na frase da definição; a que a introduz não tem o
        # termo e entra por ser a anterior
        p = busca.podar_trecho("TIR", TIR)
        assert "zera o valor presente líquido" in p
        assert "descontado a uma taxa" in p

    def test_ordem_original_e_lacuna_marcada(self):
        p = busca.podar_trecho("payback", TIR)
        # duas frases com payback, separadas por nada: a ordem é a do texto
        assert p.index("payback simples") < p.index("payback descontado")
        assert p.startswith("…")

    def test_sem_frase_que_case_nao_poda(self):
        # pergunta que chegou pelo vetor: sem sinal de onde está a resposta
        assert busca.podar_trecho("por que as empresas se concentram", TIR) == ""

    def test_trecho_curto_nao_poda(self):
        # entre o alvo (450) e o mínimo (500): podado ficaria menor, e ainda
        # assim não se mexe — o ganho não paga o contexto perdido
        texto = "Taxa de juros real é a nominal menos a inflação." + \
            " Enchimento sem relação nenhuma." * 13
        assert busca.PODA_ALVO < len(texto) < busca.PODA_MIN
        assert busca.podar_trecho("taxa de juros", texto) == ""

    def test_flexao_e_acento_casam(self):
        texto = ("Introdução ao modelo.\n" + "Texto de enchimento sem relação. " * 20
                 + "\nNo estado estacionario o capital por trabalhador fica constante.")
        p = busca.podar_trecho("estado estacionário", texto)
        assert "capital por trabalhador fica constante" in p

    def test_so_paradas_na_consulta_nao_poda(self):
        assert busca.podar_trecho("o que é a", TIR) == ""


class TestBuscarComPoda:
    def _indexar(self, con):
        import extract

        con.execute("""INSERT INTO arquivos (id, site, courseid, nome, fileurl, ignorado)
                       VALUES (1, '20262', 6095, 'Aula TIR.pdf', 'http://x/1', 0)""")
        extract.indexar(con, 1, "Aula TIR.pdf", "--- página 1 ---\n" + TIR)

    def test_padrao_devolve_o_trecho_como_antes(self, con, falso):  # noqa: F811
        self._indexar(con)
        a = busca.buscar(con, "taxa interna de retorno", courseid=6095)
        assert a and "Exemplo numérico" in a[0]["trecho"]

    def test_com_poda_devolve_menos_e_mantem_a_fonte(self, con, falso):  # noqa: F811
        self._indexar(con)
        cheio = busca.buscar(con, "taxa interna de retorno", courseid=6095)[0]
        podado = busca.buscar(con, "taxa interna de retorno", courseid=6095, podar=True)[0]
        assert len(podado["trecho"]) < len(cheio["trecho"])
        assert "zera o valor presente" in podado["trecho"]
        # invariante 7: a citação não depende do texto devolvido
        assert (podado["nome"], podado["pagina"], podado["sha256"]) == \
               (cheio["nome"], cheio["pagina"], cheio["sha256"])

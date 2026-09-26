"""A separação entre o que roda em qualquer sistema e o que é só do Mac.

A versão para colegas roda em Linux e Windows. Ali, oferecer uma ferramenta de
agenda do Mac é pior que não ter agenda: o modelo a escolhe, ela falha com
`osascript` inexistente, e a resposta vira um erro técnico. O servidor tem de
escondê-las do índice e recusá-las no `executar` com o que usar no lugar.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def server(monkeypatch):
    import server as s
    return s


def _fora_do_mac(monkeypatch, s):
    monkeypatch.setattr(s, "_no_mac", lambda: False)
    monkeypatch.setattr(s, "_ranquear", lambda filtro: sorted(s._FRIAS))


def test_so_mac_sao_ferramentas_que_existem(server):
    """Nome errado em SO_MAC deixaria a ferramenta de verdade exposta."""
    assert server.SO_MAC <= set(server._FRIAS)


def test_toda_ferramenta_que_fala_com_o_calendario_do_mac_esta_marcada(server):
    """Quem chama o AppleScript da agenda precisa estar em SO_MAC.

    `ajustar_evento` usa só as funções puras de `agenda_mac` (mexe no banco
    local) e por isso fica de fora de propósito.
    """
    import inspect
    usa_applescript = ("livres", "aplicar", "sincronizar_prazos", "marcados",
                       "desmarcar", "itens_de_estudo")
    for nome, f in server._FRIAS.items():
        fonte = inspect.getsource(f)
        if any(f"agenda_mac.{x}(" in fonte for x in usa_applescript):
            assert nome in server.SO_MAC, nome


def test_fora_do_mac_o_indice_nao_oferece(server, monkeypatch):
    _fora_do_mac(monkeypatch, server)
    for saida in (server.indice(tudo=True), server.indice("marcar estudo na agenda")):
        for nome in server.SO_MAC:
            assert nome + "(" not in saida


def test_fora_do_mac_executar_recusa_e_diz_o_que_usar(server, monkeypatch):
    _fora_do_mac(monkeypatch, server)
    import agenda_mac

    def nao_pode(*a, **k):
        raise AssertionError("chegou ao Calendário do Mac")
    monkeypatch.setattr(agenda_mac, "livres", nao_pode)
    r = server.executar("horarios_livres", "{}")
    assert "só funciona no macOS" in r and "exportar_calendario" in r


def test_no_mac_continua_tudo(server, monkeypatch):
    monkeypatch.setattr(server, "_no_mac", lambda: True)
    saida = server.indice(tudo=True)
    for nome in server.SO_MAC:
        assert nome + "(" in saida


def test_portavel_continua_fora_do_mac(server, monkeypatch):
    """Esconder demais também é defeito: o .ics é justamente a alternativa."""
    _fora_do_mac(monkeypatch, server)
    saida = server.indice(tudo=True)
    assert "exportar_calendario(" in saida and "ajustar_evento(" in saida


@pytest.mark.parametrize("modulo", ["correio", "agenda_mac"])
def test_applescript_fora_do_mac_falha_explicando(modulo, monkeypatch):
    import importlib
    import subprocess
    m = importlib.import_module(modulo)
    monkeypatch.setattr(m.sys, "platform", "linux")

    def nao_pode(*a, **k):
        raise AssertionError("tentou rodar osascript")
    monkeypatch.setattr(subprocess, "run", nao_pode)
    with pytest.raises(RuntimeError, match="macOS"):
        m._osascript("return 1")

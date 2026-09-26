"""Cliente mínimo para a API REST de Web Services do Moodle."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

CONFIG_PATH = Path(
    os.environ.get(
        "MOODLE_SITES_FILE", Path.home() / ".ufmg-moodle-mcp" / "sites.json"
    )
)


class MoodleError(RuntimeError):
    """Erro devolvido pelo próprio Moodle (exception/errorcode)."""


class MoodleBloqueadoSSO(MoodleError):
    """A API foi posta atrás do login do minhaUFMG — não é o token.

    Aconteceu entre 18/09/2026 17:44 (último sync bom) e 21/09: o
    `webservice/rest/server.php` passou a responder 302 para
    `sistemas.ufmg.br/idp`, COM OU SEM token. O erro antigo dizia só "resposta
    não-JSON", e numa pergunta pelo WhatsApp o agente gastou 15 turnos tentando
    contornar o que era uma parede do servidor. Nomear a causa encerra isso no
    primeiro turno.
    """


# --------------------------------------------------------------------------
# Configuração (vários sites: um por semestre, ex. 20261, 20262, plataforma)
# --------------------------------------------------------------------------


# O token equivale à sua identidade no Moodle: com ele alguém lê suas notas e
# posta no fórum em seu nome. Em texto puro no sites.json, qualquer backup,
# sincronização de pasta ou `cat` descuidado o expõe. No chaveiro do sistema
# (Keychain no macOS) ele fica cifrado e preso à sua sessão de usuário.
#
# O sites.json continua existindo, mas só com a URL e a marca `token_em`.
# Sem o pacote `keyring`, ou com MOODLE_SEM_CHAVEIRO=1 (servidor sem sessão
# gráfica), cai para o arquivo com permissão 600, como sempre foi.
SERVICO_CHAVEIRO = "ufmg-moodle-mcp"
# `saml` é o cookie `ufmg_saml_session`. Desde 21/09/2026 a UFMG exige ele na
# frente da API: sem ele toda chamada volta 302 para o SSO, com ou sem token.
# Medido, dos nove cookies do login ele é o ÚNICO necessário. É de sessão —
# o cookie não diz quando expira; quem decide é o servidor.
_SEGREDOS = ("token", "privatetoken", "saml")
COOKIE_SAML = "ufmg_saml_session"


def _chaveiro():
    if os.environ.get("MOODLE_SEM_CHAVEIRO"):
        return None
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def load_sites() -> dict[str, dict[str, str]]:
    """URL e marcas de cada site. NÃO traz o token — ele sai por `_segredo`."""
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _gravar(sites: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(sites, indent=2), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)  # mesmo sem token dentro: diz quais sites você usa


def _segredo(alias: str, site: dict, campo: str) -> str | None:
    if site.get(campo):  # legado, ou sem chaveiro disponível
        return site[campo]
    if site.get(f"{campo}_em") != "chaveiro":
        return None
    kr = _chaveiro()
    if kr is None:
        raise MoodleError(
            f"O {campo} do site '{alias}' está no chaveiro do sistema, mas o "
            "pacote keyring não está instalado (ou MOODLE_SEM_CHAVEIRO está "
            "ligado). Rode: pip install keyring"
        )
    valor = kr.get_password(SERVICO_CHAVEIRO, f"{alias}:{campo}")
    if not valor:
        raise MoodleError(
            f"O {campo} do site '{alias}' sumiu do chaveiro. Faça login de novo: "
            "python login_navegador.py"
        )
    return valor


def save_site(alias: str, url: str, token: str, privatetoken: str | None = None,
              saml: str | None = None) -> None:
    sites = load_sites()
    entrada: dict[str, str] = {"url": url.rstrip("/")}
    kr = _chaveiro()
    for campo, valor in (("token", token), ("privatetoken", privatetoken),
                         ("saml", saml)):
        if not valor:
            continue
        if kr is not None:
            kr.set_password(SERVICO_CHAVEIRO, f"{alias}:{campo}", valor)
            entrada[f"{campo}_em"] = "chaveiro"
        else:
            entrada[campo] = valor
    sites[alias] = entrada
    _gravar(sites)


def salvar_saml(alias: str, valor: str) -> None:
    """Grava só o cookie do SSO, mantendo o token que já existe.

    A sessão do SSO expira muito antes do token. Renovar tudo a cada vez
    obrigaria o usuário a refazer o fluxo do token sem necessidade.
    """
    sites = load_sites()
    if alias not in sites:
        raise MoodleError(f"Site '{alias}' não configurado.")
    kr = _chaveiro()
    if kr is None:
        sites[alias]["saml"] = valor
    else:
        kr.set_password(SERVICO_CHAVEIRO, f"{alias}:saml", valor)
        sites[alias]["saml_em"] = "chaveiro"
        sites[alias].pop("saml", None)
    _gravar(sites)


def mover_para_chaveiro() -> list[str]:
    """Tira do sites.json os tokens em texto puro. Devolve o que moveu.

    Só apaga do arquivo depois de ler de volta do chaveiro e conferir que é
    igual: perder o token obriga a refazer o login pelo SSO, e o SSO da UFMG é
    o passo mais chato do projeto inteiro.
    """
    kr = _chaveiro()
    if kr is None:
        raise MoodleError("Chaveiro indisponível: pip install keyring")
    sites = load_sites()
    movidos = []
    for alias, site in sites.items():
        for campo in _SEGREDOS:
            valor = site.get(campo)
            if not valor:
                continue
            kr.set_password(SERVICO_CHAVEIRO, f"{alias}:{campo}", valor)
            if kr.get_password(SERVICO_CHAVEIRO, f"{alias}:{campo}") != valor:
                raise MoodleError(f"O chaveiro não devolveu o {campo} de '{alias}'; nada apagado.")
            del site[campo]
            site[f"{campo}_em"] = "chaveiro"
            movidos.append(f"{alias}:{campo}")
    if movidos:
        _gravar(sites)
    return movidos


def get_client(alias: str | None = None) -> "MoodleClient":
    sites = load_sites()
    if not sites:
        raise MoodleError(
            f"Nenhum site configurado em {CONFIG_PATH}. Rode: python get_token.py"
        )
    if alias is None:
        alias = os.environ.get("MOODLE_DEFAULT_SITE") or sorted(sites)[-1]
    if alias not in sites:
        raise MoodleError(
            f"Site '{alias}' não configurado. Disponíveis: {', '.join(sorted(sites))}"
        )
    site = sites[alias]
    token = _segredo(alias, site, "token")
    if not token:
        raise MoodleError(f"Site '{alias}' sem token. Rode: python login_navegador.py")
    try:
        saml = _segredo(alias, site, "saml")
    except MoodleError:
        saml = None     # sumiu do chaveiro: a chamada vai bater no SSO e dizer
    return MoodleClient(site["url"], token, alias=alias, saml=saml or "")


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """Converte estruturas aninhadas no formato de query do PHP/Moodle.

    {"courseids": [2, 3]} -> {"courseids[0]": "2", "courseids[1]": "3"}
    """
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}[{k}]" if prefix else str(k)
            out.update(_flatten(v, key))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    elif isinstance(value, bool):
        out[prefix] = "1" if value else "0"
    elif value is not None:
        out[prefix] = str(value)
    return out


def _conferir_sso(r, onde: str) -> None:
    """Levanta `MoodleBloqueadoSSO` se a resposta for a página de login.

    O sinal é o destino final do redirecionamento, não o corpo: token inválido
    vem em JSON (`invalidtoken`), e só a parede do SSO manda para outro host.
    """
    final = str(r.url)
    if "sistemas.ufmg.br/idp" in final or (
        r.history and "/idp/" in str(r.history[-1].headers.get("location", ""))):
        raise MoodleBloqueadoSSO(
            f"{onde}: a sessão do minhaUFMG expirou e não foi possível renovar "
            "sozinho (a API redireciona para sistemas.ufmg.br/idp). NÃO é o "
            "token. Renove com uma janela de login: "
            "`.venv/bin/python login_navegador.py 20262 --so-sessao`. "
            "Enquanto isso, o banco local continua respondendo."
        )


def _cookie_recusado(r) -> bool:
    """O gateway da UFMG recusou o cookie de sessão e respondeu vazio?

    Em 24/09/2026 a UFMG tirou a exigência da sessão na frente da API — o token
    sozinho voltou a bastar. Mas quem ainda mandava o `ufmg_saml_session`
    vencido recebia 200, `text/plain`, corpo VAZIO e um Set-Cookie apagando o
    cookie: sem redirecionamento, então `_conferir_sso` não reconhecia, e o
    erro virava "resposta não-JSON" sem causa. O sinal é o par corpo vazio +
    ordem de apagar o cookie.
    """
    if r.status_code != 200 or r.content:
        return False
    apagar = f"{COOKIE_SAML}=;"
    return any(apagar in v.replace(" ", "") for v in r.headers.get_list("set-cookie"))


class MoodleClient:
    def __init__(self, url: str, token: str, alias: str = "", timeout: float = 30.0,
                 saml: str = ""):
        self.url = url.rstrip("/")
        self.token = token
        self.alias = alias
        self._userid: int | None = None
        # IPv4 forçado. virtual.ufmg.br publica endereço IPv6 que não conecta:
        # medido com curl, IPv4 responde em 0,1 s e IPv6 estoura em 40 s. O
        # httpx tenta o IPv6 primeiro e só cai para o IPv4 depois de ~30 s, então
        # TODA primeira chamada de um processo levava 30 s — o sync inteiro (31 s)
        # e cada ferramenta do servidor que fala com o Moodle. Parecia lentidão
        # do `site_info`, que só era a primeira chamada da fila.
        # MOODLE_IPV6=1 devolve o comportamento padrão, para rede só IPv6.
        transporte = None if os.environ.get("MOODLE_IPV6") else httpx.HTTPTransport(
            local_address="0.0.0.0"
        )
        self._http = httpx.Client(
            timeout=timeout, follow_redirects=True, transport=transporte
        )
        if saml:
            host = httpx.URL(self.url).host
            self._http.cookies.set(COOKIE_SAML, saml, domain=host, path="/")

    def call(self, wsfunction: str, **params: Any) -> Any:
        data = {
            "wstoken": self.token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
        }
        data.update(_flatten(params))
        r = self._http.post(f"{self.url}/webservice/rest/server.php", data=data)
        if self._descartar_cookie_recusado(r):
            r = self._http.post(f"{self.url}/webservice/rest/server.php", data=data)
        if self._precisa_renovar(r):
            r = self._http.post(f"{self.url}/webservice/rest/server.php", data=data)
        _conferir_sso(r, wsfunction)
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            raise MoodleError(f"Resposta não-JSON de {wsfunction}: {r.text[:300]}")
        if isinstance(payload, dict) and "exception" in payload:
            raise MoodleError(
                f"{payload.get('errorcode')}: {payload.get('message')} "
                f"(função {wsfunction})"
            )
        return payload

    def _descartar_cookie_recusado(self, r) -> bool:
        """Tira o cookie de sessão recusado e diz se vale repetir a chamada.

        Não apaga nada do chaveiro: se a parede do SSO voltar, o login
        automático grava um cookie novo pelo caminho de sempre.
        """
        if not _cookie_recusado(r):
            return False
        for c in list(self._http.cookies.jar):
            if c.name == COOKIE_SAML:
                self._http.cookies.jar.clear(c.domain, c.path, c.name)
        return True

    def _precisa_renovar(self, r) -> bool:
        """Se a resposta bateu no SSO, tenta o login automático UMA vez.

        Devolve True quando renovou e vale repetir a chamada. O login mora em
        `login_navegador` e é importado só aqui: carregar o Playwright em todo
        processo que fala com o Moodle seria pagar por algo que quase nunca
        roda. Sem credenciais guardadas, ou dentro do intervalo de 15 min
        entre tentativas, devolve False e o erro nomeado segue normalmente.
        """
        try:
            _conferir_sso(r, "renovar")
            return False
        except MoodleBloqueadoSSO:
            pass
        if not self.alias or os.environ.get("MOODLE_SEM_LOGIN_AUTOMATICO"):
            return False
        try:
            import login_navegador
        except ImportError:
            return False
        ok, motivo = login_navegador.renovar_automatico(self.alias)
        self.ultimo_login_automatico = motivo
        if not ok:
            return False
        sites = load_sites()
        novo = _segredo(self.alias, sites.get(self.alias, {}), "saml")
        if not novo:
            return False
        host = httpx.URL(self.url).host
        self._http.cookies.set(COOKIE_SAML, novo, domain=host, path="/")
        return True

    # -- atalhos ----------------------------------------------------------

    def site_info(self) -> dict:
        return self.call("core_webservice_get_site_info")

    def user_id(self) -> int:
        # Cacheado: o sync pede o id para turmas, mensagens e conclusão, e cada
        # pedido era uma ida ao servidor para uma resposta que não muda.
        if self._userid is None:
            self._userid = int(self.site_info()["userid"])
        return self._userid

    def download(self, fileurl: str, dest: Path) -> Path:
        """Baixa um arquivo do Moodle (pluginfile.php) usando o token."""
        sep = "&" if "?" in fileurl else "?"
        r = self._http.get(f"{fileurl}{sep}token={self.token}")
        if self._descartar_cookie_recusado(r):
            r = self._http.get(f"{fileurl}{sep}token={self.token}")
        if self._precisa_renovar(r):
            r = self._http.get(f"{fileurl}{sep}token={self.token}")
        # Sem esta conferência, a página de login do SSO seria gravada no
        # lugar do PDF — com status 200 — e a extração indexaria HTML de login
        # como se fosse material de aula.
        _conferir_sso(r, "download")
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest

    def upload(self, arquivo: Path, itemid: int = 0) -> dict:
        """Envia um arquivo para a área de rascunho (draft) do seu usuário.

        Não usa o endpoint REST: o Moodle recebe upload em /webservice/upload.php
        via multipart. Devolve o descritor do arquivo, cujo `itemid` é o que se
        passa depois para mod_assign_save_submission.

        Enviar para a área de rascunho não entrega nada a ninguém — é só o
        equivalente a arrastar o arquivo para a caixa antes de clicar em enviar.
        """
        arquivo = Path(arquivo)
        if not arquivo.is_file():
            raise MoodleError(f"Arquivo não encontrado: {arquivo}")
        with arquivo.open("rb") as fh:
            r = self._http.post(
                f"{self.url}/webservice/upload.php",
                data={"token": self.token, "filearea": "draft", "itemid": str(itemid)},
                files={"file": (arquivo.name, fh)},
            )
        r.raise_for_status()
        payload = r.json()
        # upload.php devolve lista em sucesso, dict com "error" em falha
        if isinstance(payload, dict) and payload.get("error"):
            raise MoodleError(f"upload: {payload['error']}")
        if not payload:
            raise MoodleError(f"upload de {arquivo.name} não devolveu descritor")
        return payload[0]

    def close(self) -> None:
        self._http.close()

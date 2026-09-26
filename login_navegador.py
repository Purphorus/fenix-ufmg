"""Pega o token abrindo um navegador de verdade — um login, zero copia-e-cola.

Mesmo resultado do `get_token.py`, com a parte chata automatizada: o script
abre uma janela do Chromium, VOCÊ faz o login da minhaUFMG nela, e o script
descobre os semestres e captura o `moodlemobile://token=...` sozinho.

No login interativo a senha é digitada por você, na janela do navegador, e o
script não a lê.

**Login automático é opcional e foi autorizado explicitamente pelo usuário**
(21/09/2026), porque desde então a UFMG exige a sessão do minhaUFMG na frente
da API e ela expira. Com `--guardar-credenciais`, usuário e senha vão para o
chaveiro por um prompt que não ecoa — NUNCA por argumento ou variável de
ambiente, que ficam no histórico do shell e na lista de processos. Quando a
sessão expira, o cliente refaz o login sozinho, sem janela, no máximo uma vez a
cada 15 minutos: o formulário do SSO tem captcha que aparece após tentativas
erradas, e insistir com uma senha trocada bloquearia a conta. A senha do
minhaUFMG abre TODOS os sistemas da UFMG, não só o Moodle — é a credencial mais
valiosa que o projeto guarda. `--esquecer-credenciais` a remove.

O login do SSO fica gravado como sessão de navegador, então da segunda vez em
diante o token sai sem digitar nada — inclusive quando o token expirar no meio
do semestre.

    python login_navegador.py                    # descobre os semestres
    python login_navegador.py 20262              # vai direto num site
    python login_navegador.py 20262 --apelido moodle
    python login_navegador.py --novo-login       # ignora a sessão salva
    python login_navegador.py --esquecer         # apaga a sessão salva
    python login_navegador.py 20262 --so-sessao  # renova só o cookie do SSO
    python login_navegador.py --guardar-credenciais   # login automático
    python login_navegador.py --esquecer-credenciais
    python login_navegador.py 20262 --testar-automatico

Desde 21/09/2026 a UFMG exige o cookie `ufmg_saml_session` na frente da API.
O token continua valendo; quem expira é essa sessão. `--so-sessao` abre a
janela, espera você entrar e grava só o cookie, sem refazer o token.

Requer:  pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import urllib.parse

from get_token import decode_token, normalize
from moodle_client import (COOKIE_SAML, CONFIG_PATH, MoodleClient,
                           load_sites, salvar_saml, save_site)

PORTAL = "https://virtual.ufmg.br/minhasturmas/"

# Sessão do navegador (cookies do SSO), para não repetir o login a cada token.
# Fica ao lado do sites.json, com a mesma permissão restrita: são cookies de
# sessão, não são a senha, e expiram sozinhos — mas ainda dão acesso enquanto
# valem, então recebem o mesmo cuidado do token.
SESSAO_PATH = CONFIG_PATH.parent / "sessao_navegador.json"

# virtual.ufmg.br/minhasturmas é só um índice de atalhos; as instalações Moodle
# de verdade são /20261, /20262, /plataforma...
_INSTANCIA = re.compile(r"virtual\.ufmg\.br/(\d{5}|plataforma)\b", re.I)
_ESQUEMA = re.compile(r"moodlemobile://[^\s\"'<>]+")

# O launch.php termina redirecionando para um esquema que o navegador não abre.
# Em vez de esperar a navegação falhar, interceptamos as saídas em JS antes que
# ela aconteça — mais confiável entre versões de Moodle e temas.
_GANCHO = """
(() => {
  window.__tokenMoodle = null;
  const guardar = (u) => {
    if (typeof u === 'string' && u.indexOf('moodlemobile://') === 0) {
      window.__tokenMoodle = u;
      return true;
    }
    return false;
  };
  const assign = window.location.assign.bind(window.location);
  const replace = window.location.replace.bind(window.location);
  window.location.assign = (u) => guardar(u) || assign(u);
  window.location.replace = (u) => guardar(u) || replace(u);
  try {
    const desc = Object.getOwnPropertyDescriptor(Location.prototype, 'href');
    Object.defineProperty(window.location, 'href', {
      set(u) { if (!guardar(u)) desc.set.call(window.location, u); },
      get() { return desc.get.call(window.location); },
    });
  } catch (e) {}
  document.addEventListener('click', (ev) => {
    const a = ev.target && ev.target.closest
      && ev.target.closest('a[href^="moodlemobile://"]');
    if (a) { guardar(a.href); ev.preventDefault(); }
  }, true);
})();
"""


def instancias_no_html(html_bruto: str) -> list[str]:
    """Códigos de instalação (20262, plataforma…) achados num HTML.

    Só funciona com a sessão aberta: sem login, o portal redireciona para o SSO
    da UFMG e não mostra turma nenhuma. Por isso a descoberta acontece dentro
    do navegador, depois do login, e não por httpx antes dele.
    """
    return sorted({m.lower() for m in _INSTANCIA.findall(html_bruto or "")}, reverse=True)


def _esperar_login(pagina, timeout_s: int) -> bool:
    """Espera você sair do SSO e voltar para virtual.ufmg.br."""
    for _ in range(timeout_s):
        try:
            if "virtual.ufmg.br" in (pagina.url or ""):
                return True
        except Exception:
            pass
        pagina.wait_for_timeout(1000)
    return False


def _via_redirect(contexto, launch: str) -> str | None:
    """Pede o launch.php sem seguir redirect e lê o `Location`.

    É o caminho mais confiável: o Moodle responde 302 apontando para
    moodlemobile://, e o cabeçalho carrega o token. Navegar até lá é o que
    falha, porque o Chromium aborta esquemas que não conhece.

    Usa `contexto.request`, que compartilha os cookies da sessão já logada.
    """
    try:
        resposta = contexto.request.get(launch, max_redirects=0)
    except Exception as e:
        # max_redirects=0 faz o Playwright levantar quando há redirect; a
        # mensagem carrega a URL de destino, que é justamente o que queremos.
        achado = _ESQUEMA.search(str(e))
        return achado.group(0) if achado else None

    local = resposta.headers.get("location", "") or resposta.headers.get("Location", "")
    if local.startswith("moodlemobile://"):
        return local
    # alguns temas devolvem 200 com o link no corpo
    try:
        achado = _ESQUEMA.search(resposta.text())
        if achado:
            return achado.group(0)
    except Exception:
        pass
    return None


def _capturar(pagina, capturado: list[str], timeout_s: int) -> str | None:
    """Espera o moodlemobile:// por qualquer um dos caminhos possíveis."""
    for _ in range(timeout_s):
        if capturado:
            return capturado[0]
        try:
            valor = pagina.evaluate("window.__tokenMoodle")
            if valor:
                return valor
            achado = _ESQUEMA.search(pagina.content())
            if achado:
                return achado.group(0)
        except Exception:
            pass  # navegação em curso; tenta de novo
        pagina.wait_for_timeout(1000)
    return capturado[0] if capturado else None


def _cookie_saml(contexto) -> str:
    """O valor de `ufmg_saml_session`, ou vazio. Nunca é impresso."""
    for c in contexto.cookies():
        if c["name"] == COOKIE_SAML:
            return c["value"]
    return ""


def so_sessao(alias: str, timeout_s: int = 300) -> int:
    """Renova só o cookie do SSO: abre a janela, espera o login, grava.

    O token continua o mesmo. É o caminho curto para quando o Fênix disser
    que a sessão do minhaUFMG expirou.
    """
    from playwright.sync_api import sync_playwright
    import time

    sites = load_sites()
    if alias not in sites:
        print(f"Site '{alias}' não configurado. Rode sem --so-sessao primeiro.")
        return 1
    url = sites[alias]["url"]
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=False)
        contexto = navegador.new_context(
            storage_state=str(SESSAO_PATH) if SESSAO_PATH.is_file() else None)
        pagina = contexto.new_page()
        pagina.goto(url + "/my/")
        print("Faça o login do minhaUFMG na janela que abriu.")
        fim = time.time() + timeout_s
        saml = ""
        while time.time() < fim:
            if pagina.url.startswith(url) and "idp" not in pagina.url:
                saml = _cookie_saml(contexto)
                if saml:
                    break
            time.sleep(1.5)
        if saml:
            contexto.storage_state(path=str(SESSAO_PATH))
            SESSAO_PATH.chmod(0o600)
        navegador.close()
    if not saml:
        print("Não vi o login concluir a tempo.")
        return 1
    salvar_saml(alias, saml)
    from moodle_client import get_client
    try:
        info = get_client(alias).site_info()
    except Exception as e:
        print(f"Gravei a sessão, mas a API ainda recusou: {e}")
        return 1
    print(f"✓ sessão renovada — {info.get('fullname')}, {info.get('sitename')}")
    return 0


# --------------------------------------------------------------------------
# Login automático (opcional, autorizado pelo usuário)
# --------------------------------------------------------------------------

_CRED_USUARIO = "minhaufmg:usuario"
_CRED_SENHA = "minhaufmg:senha"
_MARCA_TENTATIVA = CONFIG_PATH.parent / "ultimo_login_automatico"
INTERVALO_MIN_S = 15 * 60


def tem_credenciais() -> bool:
    try:
        import keyring
        from moodle_client import SERVICO_CHAVEIRO
        return bool(keyring.get_password(SERVICO_CHAVEIRO, _CRED_USUARIO)
                    and keyring.get_password(SERVICO_CHAVEIRO, _CRED_SENHA))
    except Exception:
        return False


def guardar_credenciais() -> int:
    """Pede usuário e senha num prompt que não ecoa e grava no chaveiro."""
    import getpass
    import keyring
    from moodle_client import SERVICO_CHAVEIRO
    usuario = input("Usuário do minhaUFMG: ").strip()
    senha = getpass.getpass("Senha do minhaUFMG (não aparece enquanto digita): ")
    if not usuario or not senha:
        print("Nada gravado.")
        return 1
    keyring.set_password(SERVICO_CHAVEIRO, _CRED_USUARIO, usuario)
    keyring.set_password(SERVICO_CHAVEIRO, _CRED_SENHA, senha)
    del senha
    print("✓ credenciais no chaveiro. Testando um login automático agora...")
    ok, motivo = renovar_automatico(sorted(load_sites())[-1], forcar=True)
    print(("✓ " if ok else "✗ ") + motivo)
    return 0 if ok else 1


def esquecer_credenciais() -> int:
    import keyring
    from moodle_client import SERVICO_CHAVEIRO
    for chave in (_CRED_USUARIO, _CRED_SENHA):
        try:
            keyring.delete_password(SERVICO_CHAVEIRO, chave)
        except Exception:
            pass
    _MARCA_TENTATIVA.unlink(missing_ok=True)
    print("Credenciais do minhaUFMG removidas do chaveiro.")
    return 0


def renovar_automatico(alias: str, forcar: bool = False) -> tuple[bool, str]:
    """Refaz o login do SSO sem janela e grava o cookie novo.

    Devolve (ok, motivo). A senha nunca é impressa nem entra em mensagem de
    erro. Respeita o intervalo mínimo entre tentativas, a não ser com
    `forcar` — insistir com senha errada aciona captcha e bloqueia a conta.
    """
    import time
    if not tem_credenciais():
        return False, "sem credenciais no chaveiro"
    if not forcar and _MARCA_TENTATIVA.exists():
        passou = time.time() - _MARCA_TENTATIVA.stat().st_mtime
        if passou < INTERVALO_MIN_S:
            return False, (f"login automático já tentado há {passou/60:.0f} min; "
                           "espero 15 min entre tentativas para não bloquear a conta")
    _MARCA_TENTATIVA.parent.mkdir(parents=True, exist_ok=True)
    _MARCA_TENTATIVA.touch()

    import keyring
    from moodle_client import SERVICO_CHAVEIRO
    sites = load_sites()
    if alias not in sites:
        return False, f"site '{alias}' não configurado"
    url = sites[alias]["url"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright não instalado"

    usuario = keyring.get_password(SERVICO_CHAVEIRO, _CRED_USUARIO)
    senha = keyring.get_password(SERVICO_CHAVEIRO, _CRED_SENHA)
    saml, motivo = "", ""
    try:
        with sync_playwright() as p:
            nav = p.chromium.launch(headless=True)
            ctx = nav.new_context()
            pg = ctx.new_page()
            pg.goto(url + "/my/", wait_until="domcontentloaded", timeout=45000)
            if "idp/login" in pg.url:
                pg.fill("#j_username", usuario)
                pg.fill("#j_password", senha)
                pg.click("#submit")
                try:
                    pg.wait_for_url(
                        lambda u: u.startswith(url) and "idp" not in u,
                        timeout=45000)
                except Exception:
                    texto = pg.inner_text("body")[:3000].lower()
                    if "captcha" in texto and ("digite" in texto or "caracteres" in texto):
                        motivo = "o SSO pediu captcha — faça um login manual (--so-sessao)"
                    elif any(x in texto for x in ("inválid", "incorret", "não confere")):
                        motivo = "o SSO recusou usuário ou senha (a senha mudou?)"
                    else:
                        motivo = "o login não voltou ao Moodle a tempo"
            if not motivo:
                saml = _cookie_saml(ctx)
                if saml:
                    ctx.storage_state(path=str(SESSAO_PATH))
                    SESSAO_PATH.chmod(0o600)
            nav.close()
    except Exception as e:
        motivo = f"falha no navegador: {type(e).__name__}"
    finally:
        senha = None

    if motivo:
        return False, motivo
    if not saml:
        return False, "voltou ao Moodle mas sem o cookie da sessão"
    salvar_saml(alias, saml)
    return True, "sessão do minhaUFMG renovada automaticamente"


def sessao_navegador(
    site: str | None = None,
    apelido: str | None = None,
    timeout_s: int = 300,
    novo_login: bool = False,
) -> tuple[str, str, str, str, str] | None:
    """Um login só: você entra, o script descobre o semestre e pega o token.

    A sessão do SSO é gravada em `SESSAO_PATH`, então da segunda vez em diante
    o token sai sem você digitar nada. `novo_login=True` ignora a sessão salva.

    Devolve (url_site, apelido, url_moodlemobile, passport, saml) ou None.
    """
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "O playwright não está instalado. Duas saídas:\n\n"
            "    pip install playwright && python -m playwright install chromium\n"
            "  ou pegue o token na mão:\n"
            "    python get_token.py\n"
        )
        return None

    with sync_playwright() as p:
        try:
            navegador = p.chromium.launch(headless=False)
        except Exception as e:
            print(f"Não consegui abrir o Chromium: {e}")
            print("Rode:  .venv/bin/python -m playwright install chromium")
            return None

        reusando = SESSAO_PATH.is_file() and not novo_login
        contexto = navegador.new_context(
            storage_state=str(SESSAO_PATH) if reusando else None
        )
        contexto.add_init_script(_GANCHO)
        pagina = contexto.new_page()
        if reusando:
            print(f"\nReusando a sessão salva em {SESSAO_PATH}.")

        capturado: list[str] = []
        pagina.on("request", lambda r: (
            capturado.append(r.url) if r.url.startswith("moodlemobile://") else None
        ))
        pagina.on("framenavigated", lambda f: (
            capturado.append(f.url) if f.url.startswith("moodlemobile://") else None
        ))

        print("\n" + "=" * 68)
        if reusando:
            print("Abrindo o Chromium com a sessão salva — não deve pedir senha.")
        else:
            print("Abrindo o Chromium. Faça o login da minhaUFMG na janela que abrir.")
            print("A senha é digitada só ali: este script não lê o campo de senha,")
            print("não a recebe e não a grava em lugar nenhum. O que fica salvo")
            print("é o cookie de sessão, para as próximas vezes.")
        print("=" * 68)

        try:
            pagina.goto(PORTAL, wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            pass

        if not reusando:
            print("\nEsperando você concluir o login…")
        if not _esperar_login(pagina, 20 if reusando else timeout_s):
            print("⚠ o login não concluiu no tempo previsto.")
            navegador.close()
            return None
        print("✓ login detectado.")
        try:
            SESSAO_PATH.parent.mkdir(parents=True, exist_ok=True)
            contexto.storage_state(path=str(SESSAO_PATH))
            SESSAO_PATH.chmod(0o600)
            if not reusando:
                print(f"  sessão salva em {SESSAO_PATH} — da próxima vez não pede senha")
        except Exception as e:
            print(f"  (não consegui salvar a sessão: {e})")

        alvo = site
        if not alvo:
            try:
                pagina.goto(PORTAL, wait_until="networkidle", timeout=45000)
            except PWTimeout:
                pass
            try:
                achados = instancias_no_html(pagina.content())
            except Exception:
                achados = []
            if achados:
                print("\nInstalações encontradas nas suas turmas:")
                for i, inst in enumerate(achados, 1):
                    print(f"  {i}. https://virtual.ufmg.br/{inst}")
                escolha = input("\nQual? (número ou código) ").strip()
                alvo = (
                    achados[int(escolha) - 1]
                    if escolha.isdigit() and 1 <= int(escolha) <= len(achados)
                    else escolha
                )
            else:
                print("\nNão identifiquei os semestres na página.")
                alvo = input("Código do semestre (ex.: 20262): ").strip()

        if not alvo:
            navegador.close()
            return None

        url = normalize(
            alvo if alvo.startswith("http") else f"https://virtual.ufmg.br/{alvo}"
        )
        nome = apelido or url.rsplit("/", 1)[-1]

        passport = str(random.random() * 1000)
        launch = f"{url}/admin/tool/mobile/launch.php?" + urllib.parse.urlencode(
            {
                "service": "moodle_mobile_app",
                "passport": passport,
                "urlscheme": "moodlemobile",
            }
        )
        print(f"\nPedindo o token em {url} …")

        # Caminho principal: pedir sem seguir redirecionamento e ler o Location.
        # O launch.php responde 302 para moodlemobile://, que o navegador aborta
        # (ERR_ABORTED) — então não adianta navegar. Aqui usamos o contexto de
        # request do Playwright, que compartilha os cookies da sessão logada.
        bruto = _via_redirect(contexto, launch)

        # Alguns sites redirecionam por JavaScript em vez de 302. Aí o gancho
        # injetado pega, e a navegação abortada é esperada, não erro.
        if not bruto:
            try:
                pagina.goto(launch, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                if "ERR_ABORTED" not in str(e):
                    print(f"  (navegação: {e})")
            bruto = _capturar(pagina, capturado, 20)

        if not bruto:
            print("\n⚠ não capturei o redirecionamento automaticamente.")
            print("  Se a URL moodlemobile:// apareceu na janela, copie agora.")
            print("  (a janela fecha quando você responder)")
            bruto = input("  Cole aqui (ou Enter para desistir): ").strip()
        saml = _cookie_saml(contexto)
        navegador.close()

    if not bruto:
        return None
    return url, nome, bruto, passport, saml


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("site", nargs="?", help="ex.: 20262, plataforma ou a URL inteira")
    ap.add_argument("--apelido", help="como chamar este site (padrão: o código)")
    ap.add_argument(
        "--novo-login", dest="novo_login", action="store_true",
        help="ignora a sessão salva e loga de novo",
    )
    ap.add_argument(
        "--esquecer", action="store_true",
        help="apaga a sessão salva do navegador e sai",
    )
    ap.add_argument(
        "--so-sessao", dest="so_sessao", action="store_true",
        help="renova só o cookie do SSO, mantendo o token",
    )
    ap.add_argument("--guardar-credenciais", dest="guardar", action="store_true",
                    help="grava usuário e senha no chaveiro para login automático")
    ap.add_argument("--esquecer-credenciais", dest="esquecer_cred",
                    action="store_true", help="remove usuário e senha do chaveiro")
    ap.add_argument("--testar-automatico", dest="testar", action="store_true",
                    help="faz um login automático agora, sem janela")
    args = ap.parse_args()

    if args.guardar:
        return guardar_credenciais()
    if args.esquecer_cred:
        return esquecer_credenciais()
    if args.testar:
        ok, motivo = renovar_automatico(args.site or sorted(load_sites())[-1],
                                        forcar=True)
        print(("✓ " if ok else "✗ ") + motivo)
        return 0 if ok else 1

    if args.so_sessao:
        if not args.site:
            print("Diga o site: python login_navegador.py 20262 --so-sessao")
            return 1
        return so_sessao(args.site)

    if args.esquecer:
        if SESSAO_PATH.is_file():
            SESSAO_PATH.unlink()
            print(f"Sessão apagada: {SESSAO_PATH}")
        else:
            print("Não havia sessão salva.")
        return 0

    if args.site and args.site.rstrip("/").endswith("minhasturmas"):
        print(
            "'minhasturmas' é a página de atalhos, não uma instalação Moodle —\n"
            "não tem /webservice/rest/server.php. Rode sem argumento que eu\n"
            "descubro os semestres depois do login, ou passe o código (ex.: 20262)."
        )
        return 1

    resultado = sessao_navegador(args.site, args.apelido, novo_login=args.novo_login)
    if not resultado:
        return 1
    url, apelido, bruto, passport, saml = resultado

    try:
        token, private = decode_token(bruto, url, passport)
    except Exception as e:
        print(f"Não consegui decodificar o token: {e}")
        return 1

    cliente = MoodleClient(url, token, saml=saml)
    try:
        info = cliente.site_info()
    except Exception as e:
        print(f"O token não funcionou: {e}")
        return 1

    print(f"\n✓ Autenticado como {info.get('fullname')} ({info.get('username')})")
    print(f"  site: {info.get('sitename')}  |  Moodle {info.get('release')}")
    save_site(apelido, url, token, private, saml=saml or None)
    print(f"✓ salvo em {CONFIG_PATH} sob '{apelido}' (permissão 600)")
    print("\nPróximo passo:  .venv/bin/python diagnostico.py")
    cliente.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

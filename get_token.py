"""Obtém um token de Web Service do Moodle usando o mesmo fluxo do app oficial.

Como o login da UFMG passa pelo SSO da minhaUFMG, o token não pode ser pedido
com usuário/senha em /login/token.php. O caminho é o mesmo do app Moodle:

  1. o script gera um "passport" aleatório e monta uma URL de launch;
  2. VOCÊ abre essa URL no seu navegador e faz login normalmente na minhaUFMG;
  3. o Moodle redireciona para moodlemobile://token=<base64>, que o navegador
     não sabe abrir — você copia essa URL e cola aqui;
  4. o script confere a assinatura e salva o token.

O script nunca vê sua senha: o login acontece no seu navegador.
"""

from __future__ import annotations

import base64
import hashlib
import random
import sys
import urllib.parse

import httpx

from moodle_client import CONFIG_PATH, MoodleClient, save_site


def normalize(url: str) -> str:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    return url.rstrip("/")


def check_webservices(url: str) -> None:
    """Verifica se o site tem web services / app móvel habilitados."""
    try:
        r = httpx.get(f"{url}/webservice/rest/server.php", timeout=20)
        if r.status_code == 200 and "servicesnotavailable" not in r.text:
            print("✓ endpoint /webservice/rest/server.php respondeu")
        else:
            print(f"⚠ endpoint respondeu {r.status_code}: {r.text[:200]}")
    except httpx.HTTPError as e:
        print(f"⚠ não consegui acessar o endpoint REST: {e}")

    try:
        r = httpx.get(
            f"{url}/lib/ajax/service.php",
            params={"info": "tool_mobile_get_public_config"},
            timeout=20,
        )
        print(f"  (tool_mobile respondeu {r.status_code})")
    except httpx.HTTPError:
        pass


def decode_token(raw: str, site_url: str, passport: str) -> tuple[str, str | None]:
    raw = raw.strip()
    if "token=" in raw:
        raw = raw.split("token=", 1)[1]
        raw = urllib.parse.unquote(raw).split("&")[0]
    # padding do base64
    raw += "=" * (-len(raw) % 4)
    decoded = base64.b64decode(raw).decode("utf-8")
    parts = decoded.split(":::")
    if len(parts) < 2:
        raise ValueError(f"formato inesperado: {decoded[:80]}")
    signature, token = parts[0], parts[1]
    private = parts[2] if len(parts) > 2 and parts[2] not in ("", "null") else None

    expected = hashlib.md5((site_url + passport).encode()).hexdigest()
    if signature != expected:
        print("⚠ assinatura não confere — o site pode ter redirecionado para outro")
        print(f"  esperado={expected} recebido={signature}")
    return token, private


def main() -> int:
    print("== Token do Moodle UFMG ==\n")
    print("Exemplos de site: https://virtual.ufmg.br/20262")
    print("                  https://virtual.ufmg.br/plataforma\n")
    url = normalize(input("URL do site Moodle: "))
    alias = input(f"Apelido para esse site [{url.rsplit('/', 1)[-1]}]: ").strip()
    alias = alias or url.rsplit("/", 1)[-1]

    check_webservices(url)

    passport = str(random.random() * 1000)
    launch = (
        f"{url}/admin/tool/mobile/launch.php?"
        + urllib.parse.urlencode(
            {
                "service": "moodle_mobile_app",
                "passport": passport,
                "urlscheme": "moodlemobile",
            }
        )
    )

    print("\n1) Abra esta URL no navegador (já logado na minhaUFMG):\n")
    print(f"   {launch}\n")
    print("2) Após o login, o navegador tentará abrir 'moodlemobile://token=...'.")
    print("   No Firefox aparece um diálogo com a URL; no Chrome, veja a aba")
    print("   Network do DevTools ou o histórico. Copie a URL inteira.\n")

    raw = input("3) Cole aqui a URL (ou só o trecho depois de token=): ")
    token, private = decode_token(raw, url, passport)

    client = MoodleClient(url, token)
    info = client.site_info()
    print(f"\n✓ Autenticado como {info.get('fullname')} ({info.get('username')})")
    print(f"  site: {info.get('sitename')}")

    save_site(alias, url, token, private)
    print(f"✓ salvo em {CONFIG_PATH} sob o apelido '{alias}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Confere o que o seu token realmente libera antes de testar de verdade.

O Moodle expõe funções de web service por perfil e por configuração do site.
Nem toda instalação libera para aluno o que o app oficial usa — em especial
escrita em fórum e questionário. Este script pergunta ao servidor quais
funções o SEU token vê e cruza com as que o projeto usa, para você saber o que
vai funcionar antes de tentar.

    python diagnostico.py            # site padrão
    python diagnostico.py 20262      # site específico
"""

from __future__ import annotations

import sys

from moodle_client import MoodleError, get_client, load_sites

# O que cada parte do projeto precisa. Grupo -> {função: para que serve}
NECESSARIAS: dict[str, dict[str, str]] = {
    "Leitura — o mínimo para o sync funcionar": {
        "core_webservice_get_site_info": "identidade e versão do site",
        "core_enrol_get_users_courses": "listar suas turmas",
        "core_course_get_contents": "seções, atividades e arquivos",
        "core_calendar_get_action_events_by_timesort": "prazos do calendário",
    },
    "Leitura — tarefas e notas": {
        "mod_assign_get_assignments": "tarefas com prazo",
        "mod_assign_get_submission_status": "status da sua entrega",
        "gradereport_user_get_grade_items": "notas lançadas",
    },
    "Leitura — fórum": {
        "mod_forum_get_forums_by_courses": "fóruns da turma",
        "mod_forum_get_forum_discussions": "discussões",
        "mod_forum_get_discussion_posts": "posts (dá o postid para responder)",
    },
    "Escrita — fórum": {
        "mod_forum_add_discussion": "abrir discussão",
        "mod_forum_add_discussion_post": "responder post",
    },
    "Escrita — tarefa": {
        "mod_assign_save_submission": "salvar rascunho",
        "mod_assign_submit_for_grading": "entregar",
    },
    "Questionário — leitura": {
        "mod_quiz_get_quizzes_by_courses": "nota máxima (decide a política de envio)",
        "mod_quiz_get_user_attempts": "suas tentativas",
        "mod_quiz_get_attempt_data": "questões e nomes de campo",
        "mod_quiz_get_attempt_summary": "o que está respondido",
        "mod_quiz_get_attempt_review": "correção, para o roteiro de estudo",
    },
    "Questionário — escrita": {
        "mod_quiz_start_attempt": "iniciar tentativa",
        "mod_quiz_save_attempt": "salvar sem finalizar",
        "mod_quiz_process_attempt": "enviar/finalizar",
    },
}

# Funções cuja ausência quebra o projeto inteiro, não só um recurso.
CRITICAS = {
    "core_webservice_get_site_info",
    "core_enrol_get_users_courses",
    "core_course_get_contents",
}


def main() -> int:
    sites = load_sites()
    if not sites:
        print(
            "Nenhum site configurado. Rode primeiro:\n\n"
            "    python login_navegador.py    (abre o navegador, mais fácil)\n"
            "    python get_token.py          (manual, sem playwright)\n"
        )
        return 1

    alias = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        cli = get_client(alias)
        info = cli.site_info()
    except MoodleError as e:
        print(f"Erro: {e}")
        return 1

    print(f"Site:    {info.get('sitename')} ({cli.url})")
    print(f"Usuário: {info.get('fullname')} ({info.get('username')})")
    print(f"Moodle:  {info.get('release')}")

    disponiveis = {f.get("name") for f in info.get("functions", [])}
    print(f"Funções liberadas para este token: {len(disponiveis)}\n")

    faltando_critico: list[str] = []
    faltando_recurso: list[str] = []

    for grupo, funcoes in NECESSARIAS.items():
        print(grupo)
        for nome, para_que in funcoes.items():
            tem = nome in disponiveis
            print(f"  {'✓' if tem else '✗'} {nome:<48} {para_que}")
            if not tem:
                (faltando_critico if nome in CRITICAS else faltando_recurso).append(nome)
        print()

    if faltando_critico:
        print("BLOQUEIO: faltam funções básicas — nem o sync vai rodar:")
        for f in faltando_critico:
            print(f"  - {f}")
        print("\nProvavelmente o serviço moodle_mobile_app está restrito neste site.")
        return 2

    if faltando_recurso:
        print("Recursos indisponíveis neste site (o resto funciona):")
        for f in faltando_recurso:
            print(f"  - {f}")
        print(
            "\nAusência aqui costuma ser política do site, não erro do projeto:\n"
            "muitas instalações não liberam escrita de fórum/questionário por\n"
            "web service para aluno. As funções de leitura seguem valendo."
        )
    else:
        print("Tudo que o projeto usa está liberado neste token.")

    cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

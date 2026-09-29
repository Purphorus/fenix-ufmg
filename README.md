# Fênix — companion do UFMG Virtual

Assistente de estudo para o Moodle da UFMG. Ele sincroniza as suas turmas,
baixa o material, monta o calendário real das provas e responde perguntas **a
partir do seu próprio material, citando arquivo e página**. Funciona como
servidor MCP: você conversa em português no Claude (Code ou Desktop) e ele
usa as ferramentas do Fênix.

```
"o que tem essa semana?"            → prazos, avisos e semana pesada, numa chamada
"o que o material diz sobre Solow?" → trechos dos slides, com arquivo e página
"revisa o questionário 2 de micro"  → o que você errou, com o trecho que explica
"me dá exercícios da prova 1"       → questões montadas do material, com fonte
```

## Versões: o que roda onde

| Parte | macOS | Linux | Windows |
|---|:-:|:-:|:-:|
| Sync do Moodle, extração, busca, calendário `.ics` | ✓ | ✓ | ✓ |
| Servidor MCP, CLI, skill e subagente | ✓ | ✓ | ✓ |
| Revisão de questionário, simulado, apostila em PDF | ✓ | ✓ | ✓ |
| Postar em fórum, entregar tarefa (ensaia por padrão) | ✓ | ✓ | ✓ |
| Provas e blocos de estudo no **Calendário do Mac** | ✓ | — | — |
| E-mail pelo **Mail.app** | ✓ | — | — |
| Sync agendado automático (`cli.py agendar`) | ✓ | cron | Agendador |

Fora do Mac, as ferramentas de agenda não aparecem para o modelo, e quem
pedir uma delas recebe a alternativa: `exportar_calendario` gera um `.ics`
com as provas e prazos confirmados, que o Google Agenda e o Outlook importam.

## Instalação

Python 3.11 ou mais novo.

```bash
git clone https://github.com/Purphorus/fenix-ufmg && cd fenix-ufmg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt            # Windows: .venv\Scripts\pip
.venv/bin/python -m playwright install chromium      # login pelo navegador e apostila em PDF
```

No Windows, troque `.venv/bin/python` por `.venv\Scripts\python` em todos os
comandos deste arquivo.

A busca semântica usa um modelo local (~470 MB, baixado no primeiro uso e
depois offline, em CPU). Ele é opcional: sem o `fastembed`, a busca responde
só pelo termo.

## 1. Login e token

```bash
.venv/bin/python login_navegador.py
```

Abre um navegador, você faz o login da minhaUFMG **na janela**, e o script
descobre seus semestres e guarda o token. A senha é digitada no navegador; o
script não a lê.

O token é a sua identidade no Moodle. Ele fica no chaveiro do sistema
(Keychain, Credential Manager ou Secret Service); sem chaveiro disponível, em
`~/.ufmg-moodle-mcp/sites.json` com permissão 600. Cada pessoa usa o próprio
token: não existe credencial compartilhada.

Alternativa sem navegador automatizado: `.venv/bin/python get_token.py`.

```bash
.venv/bin/python cli.py token      # onde está o token, e se ele lê
.venv/bin/python diagnostico.py    # o que o seu token libera
```

## 2. Primeiro sync

```bash
.venv/bin/python cli.py sync && .venv/bin/python cli.py extrair
```

Tudo fica em `~/.ufmg-moodle-mcp/` (banco, materiais, token), nunca na pasta
do projeto. Rode de novo quando quiser novidade, ou agende
(`cli.py agendar` diz como no seu sistema).

## 3. Registrar no Claude

**Claude Code**, com caminho absoluto:

```bash
claude mcp add --scope user ufmg-moodle -- /caminho/fenix-ufmg/.venv/bin/python /caminho/fenix-ufmg/server.py
```

A **skill** (`.claude/skills/ufmg-companion`) e o **subagente**
(`.claude/agents/pesquisador-material.md`) são carregados quando você abre o
Claude Code dentro da pasta do projeto. Para tê-los em qualquer pasta, copie
os dois para `~/.claude/skills/` e `~/.claude/agents/`. A skill é o
roteador: diz qual ferramenta usar e quando parar, e é o que mantém as
conversas baratas.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ufmg-moodle": {
      "command": "/caminho/fenix-ufmg/.venv/bin/python",
      "args": ["/caminho/fenix-ufmg/server.py"]
    }
  }
}
```

No Desktop a skill entra por upload; o [GUIA.md](GUIA.md) explica como.

**E-mail (só macOS):** o servidor de e-mail é separado e só custa contexto
se registrado:

```bash
claude mcp add --scope user ufmg-correio -- /caminho/fenix-ufmg/.venv/bin/python /caminho/fenix-ufmg/server_correio.py
```

## Como usar

O [GUIA.md](GUIA.md) é o passo a passo: o ritmo de sync, estudar com o
material, revisar questionário, escrever no Moodle, conferir se a busca
continua boa.

## Como a busca funciona

Cada resposta sobre a matéria vem de uma busca em três etapas: **a palavra
diz onde procurar** (BM25 no SQLite), **um grafo entre documentos diz até
onde ir** (arquivos com conteúdo parecido ou com trecho repetido), e **o
vetor escolhe qual trecho responde** (cosseno, com corte relativo ao melhor
da rodada). Quem decide entre esse modo e a busca vetorial pura é uma conta
em Python, não o modelo. Todo resultado traz arquivo e página, e quando o
material só **menciona** o assunto sem ensinar, a busca avisa.

O desenho, as medições e o que foi testado e descartado estão em
[PROJETO.md](PROJETO.md) e no [CLAUDE.md](CLAUDE.md).

## Escrita no Moodle

- **Ensaia por padrão.** Sem `confirmar=True` (ou `--confirmar` no CLI), nada
  é enviado: você vê o que sairia. Pedir para redigir não é pedir para postar.
- **Fica registrada.** Toda tentativa, inclusive as que falham, vai para
  `log_escrita` com o conteúdo exato. `cli.py escritas` lista tudo.
- **Questionário segue a política que você declara** por curso
  (`cli.py estudo politica`). Sem declaração, só questionário de prática
  (nota zero) é enviado; nos outros você recebe o gabarito revisado para
  marcar você mesmo.

## Avisos

- O que sai por estas ferramentas sai **como você**. Não comite o
  `sites.json` nem cole o token em lugar nenhum.
- Automatizar participação avaliada pode esbarrar no Termo de Compromisso de
  Uso dos Recursos de TI da UFMG. Confira o que a sua disciplina considera
  aceitável antes de usar a escrita em atividade que vale nota.
- Sync de hora em hora é o bastante; intervalo menor pesa num servidor
  institucional sem ganho nenhum.
- Os PDFs escaneados, sem camada de texto, ficam fora da busca.

## Desenvolvimento

```bash
.venv/bin/python -m pytest testes/ -q     # a suíte, incluindo as invariantes
.venv/bin/python cli.py skill             # o cardápio da skill bate com o código?
```

As regras que não podem quebrar estão no [CLAUDE.md](CLAUDE.md), cada uma com
teste conferido por mutação.

## Licença

[PolyForm Noncommercial 1.0.0](LICENSE). Em resumo (o que vale é o texto da
licença):

- **Uso não comercial é livre**: estudar, usar nas suas matérias, modificar,
  repassar a colegas.
- **Crédito é obrigatório**: quem repassar qualquer parte do código, alterada
  ou não, leva junto a licença e a linha `Required Notice:` do topo dela.
- **Uso comercial de qualquer parte precisa de autorização** de
  [Purphorus](https://github.com/Purphorus), dada caso a caso e nos termos
  combinados.

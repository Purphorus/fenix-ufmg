# Fluxo: agenda do Mac (provas, prazos, blocos de estudo)

**Só no macOS.** A agenda usada é a escolhida no Calendário do Mac e gravada
em `~/.ufmg-moodle-mcp/agenda_mac.json`. Se ela for compartilhada (trabalho,
família), outras pessoas veem esses horários: título curto e objetivo, nada
de nota ou comentário pessoal no título. Fora do macOS, `exportar_calendario`
gera um .ics com as provas e prazos confirmados.

O Fênix só mexe no que ele mesmo criou (linha `fenix://…` nas notas). As
ferramentas recusam evento de outra pessoa ou criado à mão, e isso não se
contorna por conta própria. Se o usuário pedir explicitamente para tirar um
evento dele, confira antes que é dele (sem convidados) e prefira **encerrar a
repetição** a apagar: apagar uma série no Google leva o histórico inteiro. O
que foi feito vai para `log_escrita` como `agenda_encerrar_serie`.

<!-- cardapio:inicio -->

### Assinaturas das ferramentas deste fluxo

Chame com `executar("nome", '{"arg": valor}')`. Estão aqui para você
**não precisar de `indice`**: ele custa 167 tokens e um turno, e este
bloco já veio junto com o documento. `?` = opcional.

```
agenda_marcados(todos?) — O que o Fênix pôs na agenda do Mac, com a chave para desmarcar.
ajustar_evento(busca, data?, titulo?) — Muda data ou título de prova/prazo do banco local e o marca confirmado.
desmarcar_agenda(chaves, confirmar?) [ESCREVE] — Apaga da agenda do Mac eventos que o Fênix criou, pela chave. Irreversível.
exportar_calendario(saida?, curso?, todos?) — Grava os eventos confirmados num .ics para importar no calendário.
horarios_livres(dia?, dias?, das?, ate?, minimo_min?) — Janelas livres na agenda do Mac, dia a dia. Só lê; recorrência já expandida.
marcar_estudos(blocos, confirmar?, sobrepor?) [ESCREVE] — Marca blocos de estudo na agenda do Mac: [{titulo, inicio, fim|minutos, notas}].
sincronizar_agenda(dias?, confirmar?) [ESCREVE] — Põe provas e prazos confirmados na agenda do Mac; tira os cancelados.
```

<!-- cardapio:fim -->

## "Põe as provas na agenda"

1. `sincronizar_agenda()` — ensaio: lista o que criaria, moveria ou tiraria.
2. Mostre a lista. Só com o "pode gravar" nessa mensagem:
   `sincronizar_agenda(confirmar=True)`.

Só evento **confirmado** entra (invariante 2). Se a prova que ele espera não
aparece, ela está pendente: mostre `evento pendentes` e ofereça confirmar.
Prova com hora 00:00 entra como dia inteiro — é "hora não informada".

## "Marca meus estudos"

1. `horarios_livres(dia, dias, das, ate)` — as janelas livres, com as
   reuniões recorrentes já contadas. As aulas de 2026/2 estão na agenda
   (chave `aula:…`, repetem até 04/12) e o almoço também (`rotina:almoco`).
   A rotina de horário do usuário está na memória: respeite-a ao propor.
2. Monte os blocos a partir do plano (a apostila tem a tabela dia a dia):
   um bloco = uma tarefa concreta, com a seção e os exercícios no campo *notas*.
   Título no formato `Estudo · ERU — seção 8`.
3. `marcar_estudos(blocos=[{titulo, inicio: "2026-09-29T14:00", minutos: 90, notas}])`
   — ensaio. Mostre a lista e os conflitos.
4. Só com o "pode marcar" nessa mensagem: repita com `confirmar=True`.

Bloco em cima de compromisso é **pulado**, não gravado. `sobrepor=True` só
se o usuário pedir explicitamente aquele horário.

## Desmarcar e remarcar

`agenda_marcados()` lista o que o Fênix pôs, com a chave.
`desmarcar_agenda(chaves=[...])` apaga — ensaio primeiro, e é irreversível.
Remarcar um bloco = desmarcar o antigo e marcar o novo: a chave vem de
título + início, então outro horário é outro bloco.

## Mudar a data de uma prova

`ajustar_evento(busca, data)` — `busca` é trecho do título ou `#id`; ambíguo
é recusado com a lista. Muda o banco local e marca confirmado. A agenda do
Mac só muda depois, com `sincronizar_agenda`.

## Se falhar

- "nenhuma agenda escolhida": `cli.py calendario-mac definir "<nome da agenda>"`.
- "não é do Fênix": o evento perdeu a marca nas notas (alguém editou). Não
  contorne; peça para ajustar no Calendário.
- Apagar demora ~20 s (o Calendário espera o Google). Não é travamento.

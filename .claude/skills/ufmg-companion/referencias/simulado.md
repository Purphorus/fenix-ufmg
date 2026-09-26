# Montar simulado a partir do material

Para quando não há questionário no Moodle e a prova é presencial — que é o
caso da maior parte da matéria dele.

## Fluxo

1. `preparar_simulado(curso, topico, limite=6, novidade=False)` — devolve o
   material com arquivo e página, o que **já foi perguntado** nesse tópico, e
   o aviso de cobertura quando o material é fraco.
2. Escreva as questões. Cada uma precisa de `fonte_trecho` com arquivo e
   página — sem isso `guardar_simulado` recusa, uma a uma.
3. `guardar_simulado(curso, topico, questoes_json)`.
4. `simulado_ver(curso, topico)` para o usuário resolver; só depois
   `com_gabarito=True`.
5. `responder_simulado(questao, resposta)` corrige e registra.

## O que não fazer

**Não invente questão sobre o que o material não cobre.** Se
`preparar_simulado` vier com o aviso de cobertura, diga ao usuário que o
material é fraco naquele ponto antes de montar qualquer coisa. Montar assim
mesmo, em silêncio, é o pior resultado possível aqui: o erro só aparece na
prova.

**Não repita o que já foi perguntado.** A ferramenta lista isso sem você
pedir. Para um segundo simulado do mesmo tópico, chame com `novidade=True`:
traz só o material que ainda não virou questão.

**Não misture com questionário do Moodle.** Questão gerada do material vive
na mesma tabela, mas separada por `attemptid` nulo. Para revisar tentativa
real, é `preparar_questionario` e `referencias/revisar.md`.

## Formato das questões

Cinco ou seis por tópico, em dificuldade crescente, e o gabarito no fim — é o
mesmo formato das apostilas, e existe porque ele resolve antes de ler a
resposta. A explicação de cada uma cita arquivo e página, para ele conferir
sem reler a aula inteira.

<!-- cardapio:inicio -->

### Assinaturas das ferramentas deste fluxo

Chame com `executar("nome", '{"arg": valor}')`. Estão aqui para você
**não precisar de `indice`**: ele custa 167 tokens e um turno, e este
bloco já veio junto com o documento. `?` = opcional.

```
guardar_simulado(curso, topico, questoes_json) — Grava as questões de treino do simulado. Recusa questão sem fonte.
preparar_questionario(attemptid, site?) — Monta a sessão de revisão: uma linha por questão, sessão salva em disco.
preparar_simulado(curso, topico, limite?, novidade?) — Monta simulado: material com fonte para criar exercícios de treino.
responder_simulado(questao, resposta) — Corrige sua resposta a um exercício do simulado e mostra a fonte.
simulado_ver(curso, topico?, com_gabarito?) — Mostra o simulado/lista de exercícios para resolver, sem o gabarito.
```

<!-- cardapio:fim -->

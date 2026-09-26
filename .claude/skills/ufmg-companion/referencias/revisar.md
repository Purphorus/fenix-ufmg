# Fluxo: revisar questionário

## Dois modos, e a diferença importa

**Tentativa finalizada** (o caso comum): o Moodle entrega a correção oficial —
sua resposta, a certa, a nota. Você **não propõe resposta**, só explica o
porquê. Uma explicação errada não vira gabarito, porque o gabarito é do Moodle.

**Tentativa aberta**: você propõe (`proposta_ia`) e explica, o usuário edita e
aprova, e só então `enviar_revisao`. O envio depende da política declarada —
`pode_enviar` decide, não você.

## Passos

1. `tentativas_questionario(quizid)` se não souber o attemptid.
2. `preparar_questionario(attemptid)` — listagem compacta, ~900 tokens. Já
   mostra quais slots estão errados.
3. `questao_detalhe(attemptid, slots=[...])` **só nos slots relevantes**. Se o
   usuário disse "as que eu errei", são só essas.
4. Explique cada uma citando o `fonte:` que veio no detalhe.
5. `salvar_revisao(sessao_json)` quando o usuário aprovar, para virar roteiro.
6. Depois da correção do professor: `corrigir_tentativa(attemptid)`.

## Erros a evitar

- Chamar `questao_detalhe` com todos os slots. Derrota o motivo de a listagem
  ser compacta.
- Tratar a resposta do modelo como gabarito em tentativa finalizada. O campo
  `Moodle:` no detalhe é a autoridade.
- Enviar sem o usuário ter pedido nessa mensagem.

<!-- cardapio:inicio -->

### Assinaturas das ferramentas deste fluxo

Chame com `executar("nome", '{"arg": valor}')`. Estão aqui para você
**não precisar de `indice`**: ele custa 167 tokens e um turno, e este
bloco já veio junto com o documento. `?` = opcional.

```
corrigir_tentativa(attemptid, site?) — Puxa a correção do Moodle para `respostas`. Rode após a correção.
enviar_revisao(sessao_json, finalizar?, confirmar?, site?) [ESCREVE] — Envia as respostas aprovadas, conforme a política do curso.
preparar_questionario(attemptid, site?) — Monta a sessão de revisão: uma linha por questão, sessão salva em disco.
questao_detalhe(attemptid, slots, site?) — Detalhe das questões pedidas: alternativas, correção e fonte.
salvar_revisao(sessao_json) — Grava a sessão revisada em questoes/respostas (proposta do modelo e a
tentativas_questionario(quizid, site?) — Lista suas tentativas num questionário, com estado e nota.
```

<!-- cardapio:fim -->

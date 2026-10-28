# DM Followers (emulador)

Bot do **Quase Nada Bots** que manda DM de boas-vindas pros **novos seguidores** de cada conta,
tocando no app real do Instagram dentro de um emulador Android (MuMu Player) via `uiautomator2`.

## Como funciona

1. Abre as **notificações** da conta, expande os "Ver mais" e lê quem "começou a seguir".
2. Na primeira vez numa conta, pega só os N mais recentes (padrão 20); depois, só os novos. Quem
   seguiu há mais de X dias fica sem DM; a ordem é dos mais recentes (ou dos mais antigos).
3. Pra cada um: busca o perfil → "Mensagem" → confere se já não mandamos antes (marca do brechó na
   conversa; balão "Falha ao enviar" não conta) → envia uma das mensagens, sorteada: as 5 variações
   padrão (com o desconto de 10%) ou as do modo (toda mensagem tem que citar o @ do brechó).
4. Conta **restrita** pelo IG (não pode mandar DM) é detectada e pulada. Perfil sem botão de
   mensagem conta como sinal de bloqueio da nossa conta e a pessoa não é marcada (recebe depois).
5. **Limites do modo**: máximo por run (só DM que saiu conta), por hora e por dia (pelos horários
   do `dms_feitos.json`). Depois de falha, soft-block ou bloqueio a conta descansa N horas.
   Conta que cai na **verificação humana** ("confirma que és uma pessoa") é parada na hora — a
   troca pra próxima conta tira ela do aparelho e avisa.
6. Pausas humanas entre envios; o histórico fica em `dms_feitos.json` (por conta).

## Onde roda

Pasta `bots/dm_follow/` da base `quase-nada-bots-device` (agente do PC). Depende do
[Auto Follow (emulador)](https://github.com/ViniciusMateos/auto-follow-emulador), que fica em
`bots/auto_follow/` (navegação, troca de conta, leitura de tela):

```python
from bots.dm_follow import dm_follow as dmf
```

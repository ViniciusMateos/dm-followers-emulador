# DM Followers (emulador)

Bot do **Quase Nada Bots** que manda DM de boas-vindas pros **novos seguidores** de cada conta,
tocando no app real do Instagram dentro de um emulador Android (MuMu Player) via `uiautomator2`.

## Como funciona

1. Abre as **notificações** da conta, expande os "Ver mais" e lê quem "começou a seguir".
2. Na primeira vez numa conta, pega só os N mais recentes (padrão 20); depois, só os novos.
3. Pra cada um: busca o perfil → "Mensagem" → confere se já não mandamos antes (marca do brechó na
   conversa; balão "Falha ao enviar" não conta) → envia uma das 5 variações da mensagem (com o
   desconto de 10%), sorteada.
4. Conta **restrita** pelo IG (não pode mandar DM) é detectada e pulada; perfil sem botão de
   mensagem é pulado.
5. Pausas humanas entre envios; o histórico fica em `dms_feitos.json` (por conta).

## Onde roda

Pasta `bots/dm_follow/` da base `quase-nada-bots-device` (agente do PC). Depende do
[Auto Follow (emulador)](https://github.com/ViniciusMateos/auto-follow-emulador), que fica em
`bots/auto_follow/` (navegação, troca de conta, leitura de tela):

```python
from bots.dm_follow import dm_follow as dmf
```

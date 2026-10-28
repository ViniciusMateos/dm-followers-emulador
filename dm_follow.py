"""DM follow no EMULADOR — mesma lógica do worker `dm-followers-instagram` (servidor/Playwright),
feita em TOQUES no app real (uiautomator2).

O que faz (igual ao antigo): manda UMA DM de "obrigado pelo follow" pra quem COMEÇOU A SEGUIR a
conta, lendo as notificações ("X começou a seguir-te. 2 d"), do MAIS ANTIGO pro mais novo. Cada
pessoa recebe uma vez por conta. Antes de enviar, VERIFICAÇÃO DUPLA dentro da conversa (se já tem a
marca @brechoquasenadaa, não remanda). Bloqueio do IG → para a conta. 6 falhas seguidas abrindo
conversa → trata como bloqueio (soft-block de DM).

Diferenças forçadas pela UI (o antigo usava API):
  - não há pk nem timestamp exato → a chave é o USERNAME e a ordem vem do tempo relativo da notif;
  - estado em dms_feitos.json {conta: {"enviados": {user: ts}, "vistos": {user: ts}}} (o antigo
    guardava pks por ds_user_id no servidor);
  - 1ª vez da conta: manda só pros N mais recentes e marca o resto como "visto" (o antigo mandava pra
    todos os ~100 do inbox — ele mesmo apontava isso como risco de bloqueio);
  - o envio é CONFIRMADO na tela (o antigo marcava enviado com qualquer JSON de resposta).
Caminho de cada DM: busca → perfil (dwell, igual ao "visitar perfil" antigo) → Mensagem → conversa.
"""
import json
import os
import random
import re
import time

from bots.auto_follow import auto_follow as af
from bots.auto_follow.auto_follow import _attr, _bounds, _dump, _log, _sleep, rid

# ─────────────────────── mensagens ───────────────────────
# 19/10/2026: volta o desconto de 10% na 1ª compra (da mensagem original do worker), em 5 variações
# rotativas (anti-flag de mensagem idêntica). Tudo minúsculo, a pedido do Vinicius. Todas citam
# @brechoquasenadaa (CTA + âncora da verificação dupla).
MENSAGENS = [
    "{username}, valeu pelo follow! segue o @brechoquasenadaa pra acompanhar os próximos drops. "
    "primeira compra no brechó tem 10% de desconto em qualquer peça!",
    "fala {username}! obrigado por seguir. os drops do brechó saem no @brechoquasenadaa, e na sua "
    "primeira compra tem 10% off em qualquer item.",
    "opa {username}, tamo junto! cola no @brechoquasenadaa pra não perder os próximos garimpos. "
    "primeira compra lá tem 10% de desconto em qualquer peça.",
    "e aí {username}, valeu por acompanhar! segue o @brechoquasenadaa pra ver os drops primeiro. "
    "na primeira compra no brechó você ganha 10% de desconto em qualquer item.",
    "oii {username}! obrigado pelo follow. as peças do brechó vão saindo no @brechoquasenadaa, e a "
    "primeira compra tem 10% off em qualquer item!",
]
MARCA = "brechoquasenadaa"     # âncora da verificação dupla (está em TODAS as variações)

# MODO (20/10/2026 — reformulado pro emulador). O backend manda os campos do modo escolhido em
# params; o que não vier usa estes. 0 desliga limite/pausa.
PADRAO = {
    "max_dms_por_conta": 10,        # por run, contando só DM que SAIU
    "max_dms_hora": 10,             # janela rolante, pelos horários do dms_feitos.json
    "max_dms_dia": 30,              # (a segue14 tomou "Falha ao enviar" com 16 DMs em ~20 min)
    "primeira_vez_n": 20,           # 1ª vez da conta: só os N mais recentes
    "idade_max_dias": 7,            # follow mais velho que isso não recebe (vira "visto")
    "ordem": "recentes",            # 'recentes' (quem acabou de seguir primeiro) | 'antigos'
    "delay_dm": (20, 60),           # entre uma pessoa e outra
    "delay_acao_ui": (1.5, 4.0),    # dwell ao abrir perfil / conversa
    "pausa_longa_cada": 5,          # a cada N DMs ENVIADAS
    "pausa_longa": (180, 420),
    "pausa_humana_cada": 6,         # a cada ~N pessoas, navega no feed e volta
    "resfriar_conta_h": 24,         # depois de falha/soft-block/bloqueio, a conta descansa N horas
    "mensagens": [],                # vazio = as 5 do código; toda mensagem TEM que citar o @ da marca
}
MAX_FALHAS_SEGUIDAS = 6             # igual ao antigo: 6 conversas que não abrem = soft-block de DM

_ESTADO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "dms_feitos.json")
_CONTAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "contas.json")

RE_FOLLOW = re.compile(r"^@?([A-Za-z0-9._]{1,30})\s+(?:começou a seguir-te|começou a seguir você|"
                       r"started following you)\.?\s*(.*)$", re.I)
RE_AGRUPADO = re.compile(r"(começaram a seguir|started following)", re.I)
FALHA_ENVIO_KW = ("não foi possível enviar", "nao foi possivel enviar", "falha ao enviar",
                  "toca para tentar", "toque para tentar", "não enviada", "nao enviada",
                  "couldn't send", "not delivered")


# ─────────────────────────────────── estado ───────────────────────────────────
def _estado_carregar():
    try:
        with open(_ESTADO_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _estado_salvar(est):
    try:
        tmp = _ESTADO_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(est, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _ESTADO_PATH)
    except Exception:
        pass


def _marcar(est, conta, user, chave="enviados"):
    c = est.setdefault(conta, {"enviados": {}, "vistos": {}})
    c.setdefault(chave, {})[user.lower()] = time.time()
    _estado_salvar(est)


def _ja_tratado(est, conta, user):
    c = est.get(conta) or {}
    u = user.lower()
    return u in (c.get("enviados") or {}) or u in (c.get("vistos") or {})


def _frota(conta):
    """Contas da própria operação — nunca recebem DM (aparecem nas notificações seguindo a vitrine)."""
    nomes = {conta.lower(), MARCA}
    try:
        with open(_CONTAS_PATH, encoding="utf-8") as f:
            nomes |= {k.lower() for k in json.load(f)}
    except Exception:
        pass
    return nomes


def _eh_frota(user, frota):
    u = user.lower()
    return u in frota or u.startswith("quasenadasegue")


# ─────────────────────────────────── utilidades ───────────────────────────────────
def _idade_min(txt):
    """'14 h' / '2 d' / '1 sem' / '5 min' / '30 s' → minutos (aprox). Desconhecido = 0 (recente)."""
    t = (txt or "").strip().lower()
    m = re.match(r"(\d+)\s*(s|seg|min|m|h|d|sem|w)\b", t)
    if not m:
        return 0
    n, u = int(m.group(1)), m.group(2)
    return n * {"s": 1 / 60, "seg": 1 / 60, "min": 1, "m": 1, "h": 60, "d": 1440,
                "sem": 10080, "w": 10080}[u]


def _nos(xml):
    for m in re.finditer(r"<node[^>]*/?>", xml):
        t = m.group(0)
        bb = _bounds(t)
        if bb:
            yield t, bb


def _dormir(seg, motivo, log, parar=None):
    """Sleep que emite [espera] (o app mostra a contagem) e respeita o parar. Igual ao antigo:
    só anuncia esperas > 15s, a cada ~12s."""
    fim = time.time() + seg
    ultimo = 0
    while True:
        falta = fim - time.time()
        if falta <= 0:
            return True
        if parar and parar():
            return False
        if seg > 15 and time.time() - ultimo >= 12:
            _log(log, f"[espera] {int(falta)} {motivo}")
            ultimo = time.time()
        time.sleep(min(1.0, falta))


def _mensagens_validas(lista):
    """Mensagens do modo que citam a marca (sem ela a verificação dupla `ja_tem_marca` não funciona
    e a pessoa receberia de novo). Nenhuma válida → as 5 do código."""
    boas = [str(m) for m in (lista or []) if MARCA in str(m).lower()]
    return boas or MENSAGENS


def _montar_mensagem(user, mensagens=None):
    txt = random.choice(mensagens or MENSAGENS).replace("{username}", user)
    while True:   # spintax {a|b|c}
        m = re.search(r"\{([^{}]*\|[^{}]*)\}", txt)
        if not m:
            return txt
        txt = txt[:m.start()] + random.choice(m.group(1).split("|")) + txt[m.end():]


def _bloqueio_tela(d):
    """Diálogo de bloqueio/limite do IG na tela (ação bloqueada, tente mais tarde, limitamos…).
    O aviso fixo de conta RESTRITA não conta como bloqueio de ação (só o forte vale nesse caso)."""
    xml = _dump(d)
    blk = af._bloqueio(xml)
    if blk and blk not in af.BLOQUEIO_FORTE and any(k in xml.lower() for k in af.RESTRITA_KW):
        return None
    return blk


# ─────────────────────────────────── navegação ───────────────────────────────────
def conta_ativa(d, log=None):
    """Username da conta ATIVA, lido no PRÓPRIO perfil: SEMPRE toca a aba Perfil antes de ler.
    Não usar conta_atual() solto: em outras telas o action_bar_title é o título da tela
    ('Notificações') ou o perfil de OUTRA pessoa (o seletor também aparece lá — leu '_anaaires')."""
    af.garantir_app(d, log)
    for _ in range(4):
        if d(resourceId=rid(af.PROFILE_TAB)).exists:
            d(resourceId=rid(af.PROFILE_TAB)).click()
            _sleep(1.8, 2.5)
            if d(resourceId=rid(af.ACC_SWITCHER)).exists:
                return (af.conta_atual(d) or "").strip().lstrip("@")
        d.press("back")
        _sleep(0.9, 1.3)
    return ""


def _notif_carregou(d):
    """A lista de notificações tem conteúdo? (o layout varia por conta/versão: no novo não há o id da
    linha nem o título 'Notificações' como texto — vale qualquer um destes sinais)."""
    return (d(textContains="começou a seguir").exists or d(resourceId=rid("activity_feed_header_row")).exists
            or d(resourceId=rid("activity_feed_newsfeed_story_row")).exists or d(text="Notificações").exists)


def abrir_notificacoes(d, log):
    """Aba Atividade (news_tab) → lista de notificações COM conteúdo. Depois do reboot do PC (19/10) a
    página abriu EM BRANCO e ficou assim: troca de aba / puxa pra atualizar e, se seguir vazia,
    REINICIA o IG e tenta de novo (foi o que destravou na mão)."""
    for tentativa in range(3):
        af.garantir_app(d, log)
        for _ in range(5):
            if d(resourceId=rid("news_tab")).exists:
                break
            d.press("back")
            _sleep(0.9, 1.4)
        if d(resourceId=rid("news_tab")).exists:
            d(resourceId=rid("news_tab")).click()
            for _ in range(8):
                _sleep(1.0, 1.5)
                if _notif_carregou(d):
                    _ao_topo(d)   # a lista GUARDA a rolagem: se ficou no fim, coletava zero
                    return True
        if tentativa == 0:
            _log(log, "notificações não carregaram (tela em branco) — trocando de aba e atualizando…")
            try:
                d(resourceId=rid("feed_tab")).click()
                _sleep(2.0, 3.0)
                W, H = d.window_size()
                d(resourceId=rid("news_tab")).click()
                _sleep(1.5, 2.0)
                d.swipe(int(W * 0.6), int(H * 0.3), int(W * 0.6), int(H * 0.8), 0.3)   # puxa p/ atualizar
                _sleep(3.0, 4.0)
                if _notif_carregou(d):
                    _ao_topo(d)
                    return True
            except Exception:
                pass
        elif tentativa == 1:
            _log(log, "notificações seguem em branco — reiniciando o Instagram…")
            try:
                d.app_stop(af.IG_PKG)
                _sleep(2.0, 3.0)
                d.app_start(af.IG_PKG, use_monkey=True)
                _sleep(7.0, 9.0)
            except Exception:
                pass
    _log(log, "não consegui abrir as notificações")
    return False


def _ao_topo(d, max_swipes=20):
    """Rola pra CIMA até a tela parar de mudar (topo da lista)."""
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    prev = None
    for _ in range(max_swipes):
        sig = len(_dump(d)) // 200, tuple(sorted(re.findall(r'text="([^"]{6,40})"', _dump(d))))[:12]
        if sig == prev:
            return
        prev = sig
        d.swipe(int(W * 0.6), int(H * 0.3), int(W * 0.6), int(H * 0.8), random.uniform(0.15, 0.22))
        _sleep(0.8, 1.2)


def coletar_novos_seguidores(d, log, max_scrolls=30, parar=None):
    """Lê as notificações e devolve {username: idade_min} de quem 'começou a seguir'. Expande o
    'Mostrar mais' e rola até a tela parar de mudar (fim) ou max_scrolls. Mesmo usuário em 2 seções
    ('Em destaque' + dia) → fica a MAIOR idade (a notificação original)."""
    achados, agrupados = {}, 0
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    prev, parado, expandidos = None, 0, 0
    for _i in range(max_scrolls):
        if parar and parar():
            break
        # expande "Ver mais"/"Mostrar mais" que aparecer NA ROLAGEM (antes só o do topo era tocado e
        # quem ficava escondido atrás dele não recebia — Vinicius 19/10). Nunca o "Ver tudo" das
        # sugestões, e só acima do bloco "Sugerido para ti".
        if expandidos < 8 and _expandir_ver_mais(d):
            expandidos += 1
            _sleep(1.5, 2.2)
        xml = _dump(d)
        for t, _bb in _nos(xml):
            txt = _attr(t, "text").replace("\xa0", " ").strip()
            if not txt:
                continue
            m = RE_FOLLOW.match(txt)
            if m:
                u, idade = m.group(1), _idade_min(m.group(2))
                achados[u] = max(achados.get(u, 0), idade)
            elif RE_AGRUPADO.search(txt):
                agrupados += 1
        sig = tuple(sorted(achados)) + (len(xml) // 500,)
        if sig == prev:
            parado += 1
            if parado >= 2:
                break
        else:
            parado = 0
        prev = sig
        d.swipe(int(W * 0.6), int(H * 0.78), int(W * 0.6), int(H * 0.32), random.uniform(0.2, 0.3))
        _sleep(1.1, 1.8)
    if agrupados:
        _log(log, f"aviso: {agrupados} notificação(ões) AGRUPADA(s) de follow ('X, Y e N começaram a "
                  f"seguir') — essas pessoas não entram nesta versão")
    return achados


def _expandir_ver_mais(d):
    """Toca um 'Ver mais'/'Mostrar mais' visível que esteja ACIMA do bloco 'Sugerido para ti'.
    True se tocou."""
    xml = _dump(d)
    sug_y = min((bb[1] for t, bb in _nos(xml)
                 if _attr(t, "text").strip().lower().startswith("sugerido para ti")), default=10 ** 6)
    for t, bb in _nos(xml):
        rot = (_attr(t, "text").strip() or _attr(t, "content-desc").strip()).lower()
        if rot in ("ver mais", "mostrar mais") and bb[1] < sug_y and bb[0] >= 100:
            try:
                d.click((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)
                return True
            except Exception:
                return False
    return False


def abrir_perfil(d, user, log):
    """Busca → toca o resultado com o username EXATO (no nome, não na foto — a foto abre story) →
    confirma o perfil pelo título. True se abriu."""
    if not d(resourceId=rid("search_tab")).exists:
        for _ in range(4):
            d.press("back")
            _sleep(0.8, 1.2)
            if d(resourceId=rid("search_tab")).exists:
                break
    try:
        d(resourceId=rid("search_tab")).click()
        _sleep(1.5, 2.3)
        # a aba Pesquisar GUARDA a navegação (volta no último perfil aberto por ela) → re-toca a
        # aba / back até a barra de busca reaparecer
        barra = d(resourceId=rid("action_bar_search_edit_text"))
        for k in range(6):
            if barra.exists:
                break
            if k % 2 == 0 and d(resourceId=rid("search_tab")).exists:
                d(resourceId=rid("search_tab")).click()
            else:
                d.press("back")
            _sleep(1.0, 1.6)
        barra.click()
        _sleep(1.0, 1.6)
        ed = d(className="android.widget.EditText")
        ed.set_text(user)
    except Exception as e:
        _log(log, f"  busca falhou ({e})")
        return False
    _sleep(2.5, 3.5)
    alvo = None
    for _ in range(3):
        for t, bb in _nos(_dump(d)):
            if _attr(t, "resource-id").endswith("/row_search_user_username") and \
                    _attr(t, "text").strip().lower() == user.lower():
                alvo = bb
                break
        if alvo:
            break
        _sleep(1.2, 2.0)
    if not alvo:
        _log(log, f"  @{user} não apareceu na busca")
        return False
    d.click((alvo[0] + alvo[2]) // 2, (alvo[1] + alvo[3]) // 2)
    _sleep(2.2, 3.2)
    tit = d(resourceId=rid("action_bar_title"))
    try:
        ok = tit.exists and tit.get_text().strip().lower() == user.lower()
    except Exception:
        ok = False
    if not ok:
        _log(log, f"  perfil de @{user} não confirmou")
    return ok


def _no_por_rotulo(xml, rotulos, x_min=100):
    """Nó cujo text/desc é exatamente um dos rótulos, fora da barra lateral de abas (x_min — a aba
    Direct também se chama 'Mensagem'). Devolve bounds ou None (o mais acima)."""
    cands = []
    for t, bb in _nos(xml):
        al = (_attr(t, "text").strip() or _attr(t, "content-desc").strip()).lower()
        if al in rotulos and bb[0] >= x_min:
            cands.append(bb)
    return min(cands, key=lambda b: b[1]) if cands else None


def em_conversa(d):
    return d(resourceId=rid("row_thread_composer_edittext")).exists


def abrir_conversa(d, user, log):
    """No perfil: toca 'Mensagem' (ou ⋯ Opções → 'Enviar mensagem'). Devolve 'ok' | 'restrita' |
    'falha'. 'restrita' = a NOSSA conta não pode mandar DM (aviso de Padrões da Comunidade)."""
    xml = _dump(d)
    bb = _no_por_rotulo(xml, {"mensagem"})
    if not bb:
        op = _no_por_rotulo(xml, {"opções", "opcoes"})
        if op:
            d.click((op[0] + op[2]) // 2, (op[1] + op[3]) // 2)
            _sleep(1.2, 1.8)
            bb = _no_por_rotulo(_dump(d), {"enviar mensagem"})
    if not bb:
        _log(log, f"  @{user}: perfil sem botão Mensagem")
        return "sem_botao"
    d.click((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)
    for _ in range(8):
        _sleep(0.9, 1.3)
        if em_conversa(d):
            return "ok"
        if af.conta_restrita(d):
            return "restrita"
    return "falha"


def ja_tem_marca(d):
    """Verificação dupla: a conversa já tem uma mensagem com a marca (@brechoquasenadaa)?
    Balão com 'Falha ao enviar' NÃO conta: a mensagem fica na conversa com a marca mas nunca chegou
    (limite de DM do IG — segue14 → matheus.delboni, 19/10); senão a pessoa nunca mais recebia."""
    xml = _dump(d)
    if any(k in xml.lower() for k in FALHA_ENVIO_KW):
        return False
    for t, _bb in _nos(xml):
        if _attr(t, "resource-id").endswith("/row_thread_composer_edittext"):
            continue
        if MARCA in (_attr(t, "text") + " " + _attr(t, "content-desc")).lower():
            return True
    return False


def enviar(d, texto, log):
    """Digita (humano) e envia. CONFIRMA na tela: 'ok' | 'falha' | 'bloqueio:<x>' | 'incerto'."""
    comp = d(resourceId=rid("row_thread_composer_edittext"))
    try:
        b = comp.info["bounds"]
        xy = ((b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2)
    except Exception:
        return "falha"
    af._digitar_humano(d, xy, texto, log, "mensagem")
    try:
        if comp.get_text() != texto:          # o IME pode comer/duplicar pedaço → garante exato
            comp.set_text(texto)
            _sleep(0.5, 0.9)
    except Exception:
        pass
    btn = d(resourceId=rid("row_thread_composer_send_button_container"))
    if not btn.wait(timeout=4):
        _log(log, "  botão Enviar não apareceu")
        return "falha"
    btn.click()
    trecho = texto[:28].lower()
    for _ in range(4):
        _sleep(1.6, 2.4)
        xml = _dump(d)
        low = xml.lower()
        blk = _bloqueio_tela(d)
        if blk:
            return f"bloqueio:{blk}"
        if any(k in low for k in FALHA_ENVIO_KW):
            return "falha"
        vazio = False
        try:
            vazio = comp.get_text() in ("", "Enviar mensagem…", "Mensagem…", None)
        except Exception:
            pass
        tem_balao = any(trecho in _attr(t, "text").lower() for t, _bb in _nos(xml)
                        if not _attr(t, "resource-id").endswith("/row_thread_composer_edittext"))
        if vazio and tem_balao:
            return "ok"
    return "incerto"


def voltar_inicio(d):
    """Sai da conversa/perfil/busca até a barra de abas aparecer."""
    for _ in range(5):
        if d(resourceId=rid("feed_tab")).exists and not em_conversa(d):
            return
        d.press("back")
        _sleep(0.8, 1.3)


def pausa_humana(d, log, parar=None):
    """Igual ao antigo (leve de propósito): vai pro feed e rola 3-6 vezes. Sem curtir."""
    _log(log, "pausa humana: navegando no feed…")
    try:
        voltar_inicio(d)
        d(resourceId=rid("feed_tab")).click()
        _sleep(2.0, 3.0)
        W, H = d.window_size()
        for _ in range(random.randint(3, 6)):
            if parar and parar():
                return
            d.swipe(int(W * 0.6), int(H * 0.75), int(W * 0.6), int(H * 0.3), random.uniform(0.25, 0.4))
            _sleep(2.5, 6.0)
    except Exception:
        pass


# ─────────────────────────────────── orquestração ───────────────────────────────────
def escolher_alvos(achados, est, conta, primeira_vez_n, log, idade_max_dias=0, ordem="recentes"):
    """Pendentes = não-frota, ainda não enviados/vistos. Devolve (alvos, vistos):
    - follow mais velho que `idade_max_dias` → visto (agradecer 3 semanas depois fica estranho);
    - 1ª vez da conta (sem NINGUÉM gravado — não basta a conta existir no estado: a segue4 ficou com
      listas vazias e mandaria pra todo mundo): só os N mais recentes, o resto vira visto;
    - ordem: 'recentes' (quem acabou de seguir primeiro) ou 'antigos'."""
    frota = _frota(conta)
    novos = {u: a for u, a in achados.items() if not _eh_frota(u, frota)}
    c = est.get(conta) or {}
    primeira = not (c.get("enviados") or c.get("vistos"))
    pend = [u for u in novos if not _ja_tratado(est, conta, u)]
    velhos = []
    if idade_max_dias:
        lim = idade_max_dias * 1440
        velhos = [u for u in pend if novos[u] > lim]
        pend = [u for u in pend if novos[u] <= lim]
        if velhos:
            _log(log, f"{len(velhos)} seguiram há mais de {idade_max_dias} dia(s) — ficam sem DM")
    pend.sort(key=lambda u: novos[u])            # menor idade = mais recente primeiro
    if primeira and primeira_vez_n and len(pend) > primeira_vez_n:
        resto = pend[primeira_vez_n:]
        pend = pend[:primeira_vez_n]
        _log(log, f"1ª vez desta conta: DM só pros {primeira_vez_n} mais recentes; "
                  f"{len(resto)} mais antigos ficam marcados como vistos (sem DM)")
        velhos += resto
    if ordem == "antigos":
        pend.reverse()
    return pend, velhos


def processar_dm(d, log, conta, params=None, parar_flag=None, rotulo=""):
    """Roda o DM follow na conta JÁ ATIVA. Devolve {ok, motivo, enviadas, puladas}. Se cair no meio
    (ex.: device offline 19/10), conta as DMs que JÁ saíram pelo estado (senão o saldo dava 0)."""
    t0 = time.time()
    try:
        return _processar_dm(d, log, conta, params, parar_flag, rotulo)
    except Exception as e:
        c = (_estado_carregar().get(conta) or {}).get("enviados") or {}
        env = sum(1 for ts in c.values() if ts >= t0)
        _log(log, f"  erro no meio do DM ({e}) — {env} DM(s) já tinham saído")
        return {"ok": False, "motivo": f"erro:{e}", "enviadas": env, "puladas": 0}


def _modo(params):
    """PADRAO + o que veio do modo. Aceita os nomes antigos (modo ainda não migrado)."""
    p = dict(PADRAO)
    params = params or {}
    for k, v in params.items():
        if k in p and v is not None:
            p[k] = tuple(v) if isinstance(v, list) and k != "mensagens" else v
    if "max_dms_por_conta" not in params and params.get("max_dms_por_run") is not None:
        p["max_dms_por_conta"] = params["max_dms_por_run"]
    if "pausa_humana_cada" not in params and "humanizar" in params:
        p["pausa_humana_cada"] = int(params.get("pausa_cada") or 0) if params.get("humanizar") else 0
    return p


def _enviadas_desde(est, conta, seg):
    c = (est.get(conta) or {}).get("enviados") or {}
    lim = time.time() - seg
    return sum(1 for ts in c.values() if isinstance(ts, (int, float)) and ts >= lim)


def _resfriar(est, conta, horas, log, motivo):
    """Depois de falha/soft-block/bloqueio: a conta descansa `horas` (as próximas runs pulam ela
    sem abrir nada — antes ela tentava de novo e falhava na hora)."""
    if not horas:
        return
    c = est.setdefault(conta, {"enviados": {}, "vistos": {}})
    c["pausada_ate"] = time.time() + horas * 3600
    _estado_salvar(est)
    _log(log, f"@{conta} vai descansar {horas:g}h ({motivo}) — as próximas runs pulam ela até lá")


def _processar_dm(d, log, conta, params=None, parar_flag=None, rotulo=""):
    p = _modo(params)
    dry = bool((params or {}).get("dry_run"))
    parar = parar_flag or (lambda: False)
    enviadas = puladas = 0
    res = lambda ok, motivo: {"ok": ok, "motivo": motivo, "enviadas": enviadas, "puladas": puladas}
    mensagens = _mensagens_validas(p["mensagens"])
    if p["mensagens"] and len(mensagens) < len(p["mensagens"]):
        _log(log, f"aviso: {len(p['mensagens']) - len(mensagens)} mensagem(ns) do modo sem o "
                  f"@{MARCA} foram ignoradas (sem ele o bot não reconhece quem já recebeu)")
    resfriar = float(p["resfriar_conta_h"] or 0)

    _log(log, f"Conta: @{conta}")
    est = _estado_carregar()
    ate = (est.get(conta) or {}).get("pausada_ate") or 0
    if ate > time.time() and not dry:
        _log(log, f"@{conta} está descansando até {time.strftime('%d/%m %H:%M', time.localtime(ate))} "
                  f"(depois de falha/bloqueio) — pulada")
        return res(True, "resfriando")
    # limites de hora/dia pelos horários das DMs que já saíram desta conta
    max_h, max_d = int(p["max_dms_hora"] or 0), int(p["max_dms_dia"] or 0)
    na_hora, no_dia = _enviadas_desde(est, conta, 3600), _enviadas_desde(est, conta, 86400)
    if (max_h and na_hora >= max_h) or (max_d and no_dia >= max_d):
        _log(log, f"@{conta} já mandou {na_hora} DM(s) na última hora e {no_dia} hoje — no limite "
                  f"do modo, pulada")
        return res(True, "limite_dia" if (max_d and no_dia >= max_d) else "limite_hora")
    # PRÉ-CHECAGEM de restrição: conta restrita de DM nem mostra o botão "Mensagem" no perfil dos
    # outros → o bot visitava 6 perfis à toa e parava como "soft-block" (segue13, run-380 19/10).
    # O chat do grupo da frota (toda conta tem) mostra o aviso "Não podes enviar mensagens" na hora.
    try:
        if af.abrir_thread(d, (params or {}).get("chat") or "vaitomanoquasenada", None) and af.conta_restrita(d):
            _log(log, "⛔ conta RESTRITA de DM pelo IG (Padrões da Comunidade) — esta conta não consegue "
                      "mandar DM. Pulando a conta.")
            voltar_inicio(d)
            return res(False, "conta_restrita")
    except Exception:
        pass
    voltar_inicio(d)
    if not abrir_notificacoes(d, log):
        return res(False, "notificacoes_nao_abriram")
    achados = coletar_novos_seguidores(d, log, parar=parar)
    if parar():
        return res(True, "parado")
    alvos, vistos = escolher_alvos(achados, est, conta, int(p["primeira_vez_n"] or 0), log,
                                   int(p["idade_max_dias"] or 0), str(p["ordem"] or "recentes"))
    if not dry:
        # a conta só entra no estado quando alguém é gravado (visto/enviado)
        for u in vistos:
            _marcar(est, conta, u, "vistos")
    _log(log, f"{len(achados)} seguidor(es) nas notificações; {len(alvos)} novo(s) pra DM")
    if not alvos:
        _log(log, "Ainda não tem novos seguidores pra mandar DM.")
        return res(True, "fim")
    cap = int(p["max_dms_por_conta"] or 0)
    nomes = ", ".join("@" + u for u in alvos[:10]) + (" …" if len(alvos) > 10 else "")
    _log(log, f"Na fila ({len(alvos)}{f', até {cap} DMs nesta run' if cap else ''}): {nomes}")

    total = min(len(alvos), cap) if cap else len(alvos)
    rot = (rotulo + " ") if rotulo else ""
    _log(log, f"[progress] 0 {total} {rot}iniciando")
    falhas = 0
    desde_humana, desde_longa = 0, 0
    alvo_humana = int(p["pausa_humana_cada"] or 0)
    plc = int(p["pausa_longa_cada"] or 0)
    for i, user in enumerate(alvos, start=1):
        if parar():
            return res(True, "parado")
        # teto da run e limites de hora/dia contam só DM que SAIU (pulado/sem botão não gasta)
        if cap and enviadas >= cap:
            _log(log, f"chegou no limite de {cap} DM(s) desta run — o resto fica pra próxima")
            break
        if (max_h and na_hora >= max_h) or (max_d and no_dia >= max_d):
            _log(log, f"chegou no limite do modo ({na_hora}/h, {no_dia}/dia) — o resto fica pra próxima")
            break
        _log(log, f"[progress] {min(enviadas, total)} {total} {rot}@{user}")
        enviou = False
        if not abrir_perfil(d, user, log):
            falhas += 1                                  # busca falhando em série = sinal de bloqueio
        else:
            _sleep(*p["delay_acao_ui"])                 # dwell no perfil (o "visitar perfil" antigo)
            st = abrir_conversa(d, user, log)
            if st == "restrita":
                _log(log, "⛔ conta RESTRITA de DM pelo IG (Padrões da Comunidade) — esta conta não "
                          "consegue mandar DM. Pulando a conta.")
                voltar_inicio(d)
                return res(False, "conta_restrita")
            if st != "ok":
                # inclui "sem_botao": perfil sem "Mensagem" é sinal de a NOSSA conta estar bloqueada
                # de DM — conta pra parada e NÃO marca a pessoa (quando a conta voltar, ela recebe)
                falhas += 1
            else:
                falhas = 0
                if ja_tem_marca(d):
                    _log(log, f"• @{user} já tem a nossa DM na conversa — pulando (verificação dupla)")
                    if not dry:
                        _marcar(est, conta, user)
                    puladas += 1
                elif dry:
                    _log(log, f"│ [dry] DM → @{user}")
                    _log(log, f"│       {_montar_mensagem(user, mensagens)}")
                    enviadas += 1
                    enviou = True
                else:
                    _sleep(*p["delay_acao_ui"])
                    texto = _montar_mensagem(user, mensagens)
                    _log(log, f"│ DM → @{user}")
                    r = enviar(d, texto, log)
                    if r.startswith("bloqueio"):
                        _log(log, f"⛔ BLOQUEIO do Instagram — parando o run: {r.split(':', 1)[1]}")
                        voltar_inicio(d)
                        _resfriar(est, conta, resfriar, log, "bloqueio")
                        return res(False, r)
                    if r == "falha":
                        _log(log, f"! DM pra @{user} NÃO saiu (falha ao enviar) — parando a conta "
                                  f"pra não insistir")
                        voltar_inicio(d)
                        _resfriar(est, conta, resfriar, log, "falha ao enviar")
                        return res(False, "falha_envio")
                    _marcar(est, conta, user)        # ok ou incerto: marca (não remanda; a marca
                    enviadas += 1                    # na conversa segura duplicata depois)
                    enviou = True
                    na_hora += 1
                    no_dia += 1
                    if r == "incerto":
                        _log(log, f"✓ DM enviada → @{user} (não confirmei o balão na tela)")
                    else:
                        _log(log, f"✓ DM enviada → @{user}")
        if falhas:
            _log(log, f"! não consegui abrir conversa com @{user} — pulando ({falhas} seguidas)")
            # a conta caiu em "Confirma que és uma pessoa"? (segue17 27/10: o captcha tirava a barra de
            # abas e o bot gastava 6 conversas achando que era soft-block de DM) → para a conta JÁ; a
            # troca pra próxima conta resolve o desafio (tira a conta do aparelho e avisa)
            if af.desafio_humano(d):
                _log(log, f"⛔ @{conta} caiu na verificação humana (\"confirma que és uma pessoa\") — "
                          f"paro esta conta")
                return res(False, "desafio_humano")
            if falhas >= MAX_FALHAS_SEGUIDAS:
                _log(log, f"⛔ BLOQUEIO provável: {falhas} conversas seguidas não abriram (soft-block de "
                          f"DM) — deixa a conta descansar")
                voltar_inicio(d)
                _resfriar(est, conta, resfriar, log, "soft-block de DM")
                return res(False, "softblock_dm")
        _log(log, f"[progress] {min(enviadas, total)} {total} {rot}@{user}")
        voltar_inicio(d)
        if i == len(alvos) or (cap and enviadas >= cap):
            break
        # ritmo: pausa longa a cada N DMs ENVIADAS (pulado não conta), senão o delay entre perfis
        if enviou:
            desde_longa += 1
        if plc and enviou and desde_longa >= plc:
            seg = random.uniform(*p["pausa_longa"]) if not dry else 1.0
            motivo = "pausa longa"
            desde_longa = 0
        else:
            seg = random.uniform(*p["delay_dm"]) if not dry else 1.0
            motivo = "entre perfis"
        _log(log, f"dormindo {int(seg)}s ({motivo})")
        if not _dormir(seg, motivo, log, parar):
            return res(True, "parado")
        desde_humana += 1
        if alvo_humana and desde_humana >= alvo_humana and not dry:
            pausa_humana(d, log, parar)
            desde_humana = 0
    _log(log, f"[progress] {total} {total} {rot}concluído")
    return res(True, "fim")



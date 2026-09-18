"""Nenhuma rota pode renderizar um template sem as variáveis que ele exige.

Regressão de um erro real de produção: as três rotas POST do Copiloto
(perguntar aos autos, analisar, Hard Truth) chamavam a OpenAI, pagavam os
tokens e então estouravam HTTP 500 em

    jinja2.exceptions.UndefinedError: 'ocr_status' is undefined

O usuário perdia o resultado depois do trabalho feito. Só a rota GET passava
a variável; as três POST, não.
"""

import re
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tools"))

import pytest

import check_templates as CT

APP = RAIZ / "app"
TDIR = APP / "templates"


# ------------------------------------------------------- o teste que importa

def test_nenhuma_rota_renderiza_template_sem_variavel_obrigatoria():
    achados = CT.varrer()
    if achados:
        detalhe = "\n".join(
            f"  {a['arquivo']}:{a['linha']} → {a['template']} sem "
            f"{', '.join(a['faltando'])}" for a in achados)
        pytest.fail("HTTP 500 garantido nestas rotas:\n" + detalhe)


def test_todas_as_rotas_do_copiloto_passam_ocr_status():
    """Específico do bug relatado: 4 rotas renderizam copilot.html."""
    src = (APP / "main.py").read_text(encoding="utf-8")
    renders = []
    for m in re.finditer(r'safe_template_response\(\s*\n?\s*["\']copilot\.html["\']', src):
        i, prof = m.end(), 1
        while i < len(src) and prof:
            prof += (src[i] == "(") - (src[i] == ")")
            i += 1
        renders.append((src[:m.start()].count("\n") + 1, src[m.start():i]))

    assert len(renders) >= 4, (
        f"esperava ao menos 4 renderizações de copilot.html, achei {len(renders)}")
    sem = [linha for linha, chamada in renders if "ocr_status" not in chamada]
    assert not sem, f"linhas de main.py sem ocr_status: {sem}"


# ------------------------------------------- sanidade da própria ferramenta

def test_ferramenta_detecta_acesso_a_atributo_no_teste_do_if():
    """`{% if x.a %}` quebra: o teste é sempre avaliado.

    O find_all() do Jinja não inclui o próprio nó; sem tratar isso a
    ferramenta passava batido justamente no caso que ocorreu em produção.
    """
    from jinja2 import Environment
    ast = Environment().parse("{% if faltante.campo %}oi{% endif %}")
    assert "faltante" in CT._usos_perigosos(ast)


def test_ferramenta_ignora_uso_protegido_por_condicional():
    """`{% if guarda %}{{ x.a }}{% endif %}` pode nunca executar."""
    from jinja2 import Environment
    ast = Environment().parse("{% if guarda %}{{ x.campo }}{% endif %}")
    assert "x" not in CT._usos_perigosos(ast)


def test_ferramenta_detecta_iteracao_desprotegida():
    from jinja2 import Environment
    ast = Environment().parse("{% for i in lista %}{{ i }}{% endfor %}")
    assert "lista" in CT._usos_perigosos(ast)


def test_ferramenta_aceita_variavel_passada_como_chave_de_dicionario():
    """Rotas antigas usam {"plans": plans}; contar só kwargs dá falso positivo."""
    src = (APP / "main.py").read_text(encoding="utf-8")
    assert '"plans": plans' in src, "fixture mudou; reveja este teste"
    assert not [a for a in CT.varrer() if a["template"] in ("signup.html", "product.html")]


# ------------------------------------------------- consistência de versão

def test_scripts_auxiliares_nao_ficaram_na_versao_antiga():
    """DIAGNOSTICO/INICIAR etc. gravam e leem logs com a versão no nome.

    Se ficarem em 8.3.1 enquanto o instalador é 8.5.0, o diagnóstico mostra
    log de instalação velho e faz parecer que a instalação falhou.
    """
    versao = (RAIZ / "VERSION.txt").read_text(encoding="utf-8").strip()
    pasta = RAIZ / "scripts" if (RAIZ / "scripts").is_dir() else _RAIZ / "scripts"
    if not pasta.is_dir():
        pytest.skip("sem scripts/ neste layout")
    defasados = []
    for arq in pasta.glob("*.ps1"):
        texto = arq.read_text(encoding="utf-8-sig", errors="replace")
        for achado in re.findall(r"instalacao-(\d+\.\d+\.\d+)\.log", texto):
            if achado != versao:
                defasados.append(f"{arq.name}: instalacao-{achado}.log")
    assert not defasados, (
        f"scripts referenciam log de versão antiga (atual {versao}): {defasados}")

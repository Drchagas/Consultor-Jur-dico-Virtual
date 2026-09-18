"""Detecta variáveis de template ausentes no contexto das rotas.

Motivação real: `UndefinedError: 'ocr_status' is undefined` derrubou as três
rotas POST do Copiloto em produção — DEPOIS de a IA ter sido chamada e paga.
O usuário via HTTP 500 e perdia o resultado.

Nem toda ausência quebra. No Jinja padrão:
  {{ x }}            -> renderiza vazio            (inofensivo)
  {% if x %}         -> False                      (inofensivo)
  {{ x.attr }}       -> UndefinedError             CRÍTICO
  {% for i in x %}   -> UndefinedError             CRÍTICO
  {{ x|length }}     -> UndefinedError             CRÍTICO

Só o segundo grupo é reportado como CRÍTICO. É essa a diferença entre um
alerta útil e uma lista de ruído que ninguém lê.
"""

from __future__ import annotations

import sys
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, meta
from jinja2 import nodes

RAIZ = Path(__file__).resolve().parent.parent
APP = RAIZ / "payload" / "app" if (RAIZ / "payload" / "app").is_dir() else RAIZ / "app"
TDIR = APP / "templates"

SEMPRE_DISPONIVEIS = {
    "request", "csrf_token", "user", "organization", "workspaces",
    "subscription", "usage", "credits_left",
    "range", "dict", "len", "enumerate", "lipsum", "cycler", "joiner", "namespace",
}


def _env() -> Environment:
    """Ambiente com os MESMOS filtros e globais que a aplicação registra.

    Um ambiente Jinja próprio não conhece `csrf_token`, `moeda`, `data` — e o
    Jinja recusa compilar um template que use filtro desconhecido, com
    TemplateAssertionError. O verificador então falhava por um motivo que não
    tem nada a ver com o que ele verifica (variável de contexto ausente), e
    quem lesse o traceback concluiria que os templates estavam quebrados.

    Importar da aplicação mantém os dois lados em sincronia sozinhos: filtro
    novo registrado em main.py passa a ser conhecido aqui sem editar nada.
    """
    env = Environment(loader=FileSystemLoader(TDIR))
    try:
        sys.path.insert(0, str(RAIZ))
        from app.main import templates as app_templates
        env.filters.update(app_templates.env.filters)
        env.globals.update(app_templates.env.globals)
    except Exception as exc:  # pragma: no cover - diagnóstico, não bloqueio
        print(f"AVISO: não consegui carregar os filtros da aplicação ({exc}).")
        print("       Filtros próprios do JARBAS serão reportados como ausentes.")
    return env


def _usos_perigosos(ast) -> set[str]:
    """Nomes cujo uso quebra se indefinidos, SEM proteção condicional.

    Distinção que evita ruído:
      {% if x.a %}...{% endif %}          -> x está no TESTE: sempre avaliado, QUEBRA
      {% if y %}{{ x.a }}{% endif %}      -> x está no CORPO: pode não executar, ok
    Sem ela a ferramenta acusa dezenas de casos legítimos e ninguém a usa.
    """
    def raiz_de(no):
        while isinstance(no, (nodes.Getattr, nodes.Getitem)):
            no = no.node
        return no.name if isinstance(no, nodes.Name) else None

    def nomes_arriscados(no):
        """Usos perigosos dentro de uma subárvore, sem descer em corpos de if."""
        achados = set()
        # find_all() do Jinja NÃO inclui o próprio nó. Em `{% if x.a %}` o teste
        # É o Getattr, então sem o `[no] +` abaixo o caso não é detectado.
        for filho in [no] + list(no.find_all((nodes.Getattr, nodes.Getitem))):
            if not isinstance(filho, (nodes.Getattr, nodes.Getitem)):
                continue
            n = raiz_de(filho)
            if n:
                achados.add(n)
        for filho in no.find_all(nodes.For):
            n = raiz_de(filho.iter)
            if n:
                achados.add(n)
        for filho in [no] + list(no.find_all(nodes.Filter)):
            if not isinstance(filho, nodes.Filter):
                continue
            if filho.node is not None:
                n = raiz_de(filho.node)
                if n:
                    achados.add(n)
        return achados

    perigosos: set[str] = set()

    def caminhar(no, protegido: bool):
        if isinstance(no, nodes.If):
            # o teste é sempre avaliado neste nível
            if not protegido:
                perigosos.update(nomes_arriscados(no.test))
            for ramo in (no.body, no.elif_, no.else_):
                for filho in ramo:
                    caminhar(filho, True)
            return
        if isinstance(no, nodes.For):
            if not protegido:
                perigosos.update(nomes_arriscados(no.iter))
                simples = raiz_de(no.iter)   # {% for i in lista %} — Name puro
                if simples:
                    perigosos.add(simples)
            for ramo in (no.body, no.else_):
                for filho in ramo:
                    caminhar(filho, True)
            return
        if not protegido and not isinstance(no, (nodes.Template, nodes.Output,
                                                 nodes.Block, nodes.Extends)):
            perigosos.update(nomes_arriscados(no))
        for filho in no.iter_child_nodes():
            caminhar(filho, protegido)

    caminhar(ast, False)
    return perigosos


def analisar_template(nome: str, visto: set[str] | None = None) -> tuple[set[str], set[str]]:
    """(todas as variáveis, as que quebram se ausentes) — seguindo herança."""
    visto = visto or set()
    if nome in visto or not (TDIR / nome).is_file():
        return set(), set()
    visto.add(nome)
    ast = _env().parse((TDIR / nome).read_text(encoding="utf-8"))
    todas = meta.find_undeclared_variables(ast)
    criticas = _usos_perigosos(ast) & todas
    for pai in meta.find_referenced_templates(ast):
        if pai:
            t, c = analisar_template(pai, visto)
            todas |= t
            criticas |= c
    return todas, criticas


def _defaults_do_main() -> set[str]:
    src = (APP / "main.py").read_text(encoding="utf-8")
    d = set(re.findall(r'context\.setdefault\("(\w+)"', src))
    bloco = re.search(r"def common_context\(.*?(?=\ndef |\n@app)", src, re.S)
    if bloco:
        d |= set(re.findall(r'"(\w+)":', bloco.group(0)))
    return d | SEMPRE_DISPONIVEIS


def varrer() -> list[dict]:
    defaults = _defaults_do_main()
    achados = []
    for arq in sorted(APP.glob("*.py")):
        txt = arq.read_text(encoding="utf-8")
        for m in re.finditer(
                r'safe_template_response\(\s*\n?\s*["\']([\w./-]+\.html)["\']', txt):
            tpl = m.group(1)
            if not (TDIR / tpl).is_file():
                continue
            i, prof = m.end(), 1
            while i < len(txt) and prof:
                prof += (txt[i] == "(") - (txt[i] == ")")
                i += 1
            chamada = txt[m.start():i]
            # kwargs (nome=valor) E chaves de dicionário ("nome": valor)
            passadas = set(re.findall(r"(\w+)\s*=", chamada))
            passadas |= set(re.findall(r"[\"'](\w+)[\"']\s*:", chamada))
            passadas |= defaults
            _, criticas = analisar_template(tpl)
            faltando = sorted(criticas - passadas)
            if faltando:
                achados.append({
                    "arquivo": arq.name,
                    "linha": txt[:m.start()].count("\n") + 1,
                    "template": tpl,
                    "faltando": faltando,
                })
    return achados


if __name__ == "__main__":
    import sys
    achados = varrer()
    if not achados:
        print("OK — nenhuma variável crítica ausente.")
        sys.exit(0)
    print("VARIÁVEIS AUSENTES QUE CAUSAM HTTP 500:\n")
    for a in achados:
        print(f"  {a['arquivo']}:{a['linha']}  {a['template']}")
        print(f"      faltando: {', '.join(a['faltando'])}")
    sys.exit(1)

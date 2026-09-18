"""Executor mínimo compatível com a parte do pytest usada pela suíte do JARBAS.

Existe só para permitir rodar os testes em ambiente sem rede/sem pytest
instalado. Em máquina normal use `pytest -q`, que é o alvo real.

Suporta: coleta de test_*, fixtures autouse e NOMEADAS (inclusive com yield
e encadeadas), tmp_path, monkeypatch (setenv/delenv/setattr),
pytest.skip(allow_module_level=...), pytest.raises(match=...) e pytest.approx.

Fixture nomeada foi acrescentada porque a suíte passou a ter testes que sobem
a aplicação inteira contra um banco descartável. Sem isso este executor
reprovava 30 testes que o pytest aprova — e um executor que acusa falha onde
não há é pior do que executor nenhum: ensina a ignorar o resultado.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path


class _Approx:
    def __init__(self, valor, rel=1e-6, abs_=1e-12):
        self.valor, self.rel, self.abs = valor, rel, abs_

    def __eq__(self, outro):
        try:
            return abs(outro - self.valor) <= max(self.abs, self.rel * abs(self.valor))
        except TypeError:
            return NotImplemented

    def __repr__(self):
        return f"approx({self.valor})"


class _Raises:
    def __init__(self, exc, match=None):
        self.exc, self.match = exc, match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, tipo, valor, tb):
        if tipo is None:
            raise AssertionError(f"esperava {self.exc.__name__}, nada foi levantado")
        if not issubclass(tipo, self.exc):
            return False
        if self.match and not re.search(self.match, str(valor)):
            raise AssertionError(
                f"{self.exc.__name__} levantada, mas mensagem {str(valor)!r} "
                f"não casa com {self.match!r}"
            )
        self.value = valor
        return True


class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setenv(self, nome, valor):
        self._undo.append(("env", nome, os.environ.get(nome)))
        os.environ[nome] = str(valor)

    def delenv(self, nome, raising=True):
        if nome in os.environ:
            self._undo.append(("env", nome, os.environ[nome]))
            del os.environ[nome]
        elif raising:
            raise KeyError(nome)

    def setattr(self, alvo, nome, valor=None):
        if valor is None and isinstance(alvo, str):
            mod, _, attr = alvo.rpartition(".")
            alvo, nome, valor = importlib.import_module(mod), attr, nome
        self._undo.append(("attr", (alvo, nome), getattr(alvo, nome, _AUSENTE)))
        setattr(alvo, nome, valor)

    def desfazer(self):
        for tipo, chave, antigo in reversed(self._undo):
            if tipo == "env":
                if antigo is None:
                    os.environ.pop(chave, None)
                else:
                    os.environ[chave] = antigo
            else:
                obj, nome = chave
                if antigo is _AUSENTE:
                    delattr(obj, nome)
                else:
                    setattr(obj, nome, antigo)
        self._undo.clear()


_AUSENTE = object()


class _Pulado(Exception):
    pass


class _PytestShim:
    @staticmethod
    def skip(motivo="", allow_module_level=False):
        # allow_module_level existe no pytest de verdade e é a única forma
        # correta de pular um arquivo inteiro durante a coleta. O shim aceita
        # o argumento para que o mesmo arquivo de teste sirva aos dois
        # executores — o pytest e este, usado quando não há pytest instalado.
        raise _Pulado(motivo)

    @staticmethod
    def fail(motivo=""):
        raise AssertionError(motivo)

    approx = staticmethod(lambda v, **kw: _Approx(v, **{
        ("rel" if k == "rel" else "abs_"): kw[k] for k in kw}))
    raises = staticmethod(lambda exc, match=None: _Raises(exc, match))

    class _Fixture:
        def __init__(self, autouse=False):
            self.autouse = autouse

        def __call__(self, fn):
            fn._e_fixture = True
            fn._autouse = self.autouse
            return fn

    @staticmethod
    def fixture(fn=None, *, autouse=False):
        if fn is not None:
            fn._e_fixture = True
            fn._autouse = False
            return fn
        return _PytestShim._Fixture(autouse=autouse)


def _pedidos(fn):
    """Parâmetros que são pedido de fixture.

    O pytest ignora parâmetro que tem valor padrão — ele não é pedido de
    fixture, é argumento opcional. `def test_x(tmp=None)` existe na suíte e
    quebraria se tratássemos 'tmp' como fixture inexistente.
    """
    return [nome for nome, parametro in inspect.signature(fn).parameters.items()
            if parametro.default is inspect.Parameter.empty
            and parametro.kind not in (inspect.Parameter.VAR_POSITIONAL,
                                       inspect.Parameter.VAR_KEYWORD)]


def _resolver(nome, registro, cache, mp, tmp, pilha, geradores):
    """Entrega o valor de uma fixture, montando as de que ela depende.

    `pilha` guarda a cadeia em resolução para acusar dependência circular em
    vez de estourar a recursão com uma mensagem incompreensível.
    """
    if nome in cache:
        return cache[nome]
    if nome == "monkeypatch":
        return mp
    if nome == "tmp_path":
        return tmp
    if nome not in registro:
        raise LookupError(
            f"fixture '{nome}' não encontrada. Este executor resolve fixtures "
            f"definidas no próprio arquivo, além de monkeypatch e tmp_path.")
    if nome in pilha:
        raise LookupError(f"dependência circular entre fixtures: {' -> '.join(pilha)} -> {nome}")

    fixture = registro[nome]
    kw = {}
    for parametro in _pedidos(fixture):
        kw[parametro] = _resolver(parametro, registro, cache, mp, tmp,
                                  pilha + [nome], geradores)
    valor = fixture(**kw)
    if inspect.isgenerator(valor):
        gerador = valor
        valor = next(gerador)
        # Teardown na ordem inversa da construção, como o pytest faz.
        geradores.insert(0, gerador)
    cache[nome] = valor
    return valor


def _carregar(caminho: Path):
    sys.modules["pytest"] = _PytestShim
    spec = importlib.util.spec_from_file_location(caminho.stem, caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rodar(arquivos: list[Path]) -> int:
    passou = falhou = 0
    falhas = []
    for arq in arquivos:
        try:
            mod = _carregar(arq)
        except (SystemExit, _Pulado) as motivo:
            detalhe = f": {motivo}" if str(motivo) else ""
            print(f"\n── {arq.name}: ignorado neste layout{detalhe}")
            continue
        autouse = [f for _, f in inspect.getmembers(mod, inspect.isfunction)
                   if getattr(f, "_autouse", False)]
        testes = [(n, f) for n, f in inspect.getmembers(mod, inspect.isfunction)
                  if n.startswith("test_")]
        testes.sort(key=lambda t: inspect.getsourcelines(t[1])[1])
        print(f"\n── {arq.name} ({len(testes)} testes)")
        registro = {n: f for n, f in inspect.getmembers(mod, inspect.isfunction)
                    if getattr(f, "_e_fixture", False)}
        for nome, fn in testes:
            mp = _MonkeyPatch()
            geradores = []
            cache = {}
            tmpdir = tempfile.TemporaryDirectory(prefix="minipytest-")
            tmp = Path(tmpdir.name)
            try:
                for fx in autouse:
                    kw = {p: _resolver(p, registro, cache, mp, tmp, [], geradores)
                          for p in _pedidos(fx)}
                    r = fx(**kw)
                    if inspect.isgenerator(r):
                        next(r)
                        geradores.insert(0, r)
                kw = {p: _resolver(p, registro, cache, mp, tmp, [], geradores)
                      for p in _pedidos(fn)}
                fn(**kw)
                print(f"   PASS  {nome}")
                passou += 1
            except _Pulado as exc:
                print(f"   SKIP  {nome} ({exc})")
            except Exception:
                print(f"   FAIL  {nome}")
                falhas.append((arq.name, nome, traceback.format_exc()))
                falhou += 1
            finally:
                for g in geradores:
                    try:
                        next(g)
                    except StopIteration:
                        pass
                    except Exception:
                        pass
                mp.desfazer()
                tmpdir.cleanup()

    print(f"\n{'='*62}\nRESULTADO: {passou} passaram, {falhou} falharam")
    for arquivo, nome, tb in falhas:
        print(f"\n--- {arquivo}::{nome} ---\n{tb}")
    return 1 if falhou else 0


if __name__ == "__main__":
    raiz = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(raiz))
    alvos = [Path(a) for a in sys.argv[1:]] or sorted((raiz / "tests").glob("test_*.py"))
    sys.exit(rodar(alvos))

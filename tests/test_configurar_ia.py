"""Configurar a chave da IA DEPOIS de instalado.

Origem: o advogado instalou o sistema sem a chave (ela nem existia ainda) e
não havia caminho de volta visível. A tela de Configurações tinha o campo,
mas escondido no segundo painel, só para Super Admin e só com uma variável
de ambiente que ninguém descobre sozinha. Sem IA, sem explicação e sem
próximo passo.

O que estes testes protegem, em ordem de gravidade:

1. Uma chave que não funciona nunca substitui uma que funciona. O console da
   Anthropic mostra a chave uma única vez: perder a que estava valendo por
   causa de um erro de digitação deixaria o escritório sem IA sem volta.
2. O que vai para o .env.local é a chave PURA. Gravar o que foi colado — com
   aspas, invisível ou a linha inteira — conserta na sessão e volta a quebrar
   no próximo reinício, que foi exatamente o 401 sem explicação da 9.0.
3. Variável de ambiente do Windows vence o arquivo. Se a tela não disser
   isso, o operador corrige o arquivo, reinicia e nada muda.
"""

import os
import re
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

from app import ai_gateway as G  # noqa: E402

CHAVE = "sk-ant-api03-EXEMPLOdeCHAVEparaTESTE"
OUTRA = "sk-ant-api03-OUTRAchaveDIFERENTE"


@pytest.fixture()
def env_local(tmp_path, monkeypatch):
    arquivo = tmp_path / ".env.local"
    arquivo.write_text("JARBAS_PORT=8010\n", encoding="utf-8")
    monkeypatch.setattr(G, "ENV_FILE", arquivo)
    monkeypatch.setenv("JARBAS_ALLOW_SECRET_CONFIG", "1")
    monkeypatch.delenv(G.ENV_CHAVE, raising=False)
    return arquivo


# ================================================ o que vai para o arquivo

def test_a_chave_gravada_e_a_chave_pura(env_local):
    """Colar a linha inteira do .env.local é o engano mais comum."""
    for sujo in (f'"{CHAVE}"', f"{G.ENV_CHAVE}={CHAVE}", f"  {CHAVE}  ", f"﻿{CHAVE}"):
        G.salvar_chave(sujo)
        linhas = env_local.read_text(encoding="utf-8").splitlines()
        gravada = [l for l in linhas if l.startswith(G.ENV_CHAVE + "=")]
        assert gravada == [f"{G.ENV_CHAVE}={CHAVE}"], f"gravou sujo: {gravada}"


def test_gravar_a_chave_nao_apaga_o_resto_do_arquivo(env_local):
    """O .env.local guarda porta, segredo de sessão e limites. Reescrevê-lo
    inteiro deixaria a instalação sem conseguir subir."""
    G.salvar_chave(CHAVE)
    conteudo = env_local.read_text(encoding="utf-8")
    assert "JARBAS_PORT=8010" in conteudo


def test_chave_de_outro_fornecedor_e_recusada_antes_de_gravar(env_local):
    G.salvar_chave(CHAVE)
    with pytest.raises(G.ChaveInvalida) as exc:
        G.salvar_chave("sk-proj-umaCHAVEdaOPENAI")
    assert "OpenAI" in str(exc.value)
    assert G.chave_do_arquivo() == CHAVE, "a chave boa foi substituída pela recusada"


def test_campo_vazio_nao_apaga_a_chave_existente(env_local):
    G.salvar_chave(CHAVE)
    with pytest.raises(G.ChaveInvalida):
        G.salvar_chave("   ")
    assert G.chave_do_arquivo() == CHAVE


def test_save_local_config_tambem_saneia(env_local, monkeypatch):
    """O caminho antigo (/settings) precisa sanear igual, senão o defeito
    volta pela outra porta."""
    G.save_local_config(api_key=f'"{CHAVE}"', legal_model="claude-opus-5",
                        intake_model="claude-sonnet-5",
                        routine_model="claude-haiku-4-5-20251001")
    assert G.chave_do_arquivo() == CHAVE
    assert os.environ[G.ENV_CHAVE] == CHAVE


# ========================================= de onde a chave em uso está vindo

def test_origem_ausente_quando_nao_ha_chave(env_local):
    assert G.origem_da_chave() == "ausente"


def test_origem_arquivo(env_local, monkeypatch):
    G.salvar_chave(CHAVE)
    monkeypatch.setenv(G.ENV_CHAVE, CHAVE)
    assert G.origem_da_chave() == "arquivo"


def test_origem_ambiente_quando_so_o_windows_tem(env_local, monkeypatch):
    monkeypatch.setenv(G.ENV_CHAVE, CHAVE)
    assert G.origem_da_chave() == "ambiente"


def test_conflito_entre_arquivo_e_ambiente_e_detectado(env_local, monkeypatch):
    """O caso que fazia o operador corrigir o arquivo e nada mudar.

    load_dotenv roda com override=False: o que o PowerShell exportou vence.
    Sem esta detecção, a tela mostra "configurada", o arquivo está certo, e
    quem responde à Anthropic é a chave velha.
    """
    G.salvar_chave(CHAVE)
    monkeypatch.setenv(G.ENV_CHAVE, OUTRA)
    assert G.origem_da_chave() == "conflito"


def test_ler_o_arquivo_nao_polui_o_relatorio_de_saneamento(env_local, monkeypatch):
    """chave_saneada() fala da chave EM USO. Se a leitura do arquivo
    registrasse limpeza, a tela acusaria sujeira que não existe."""
    env_local.write_text(f'{G.ENV_CHAVE}="{CHAVE}"\n', encoding="utf-8")
    monkeypatch.setenv(G.ENV_CHAVE, CHAVE)
    assert G.chave_do_arquivo() == CHAVE
    assert G.chave_saneada() == []


# =================================================== a tela e o caminho dela

def _fonte(nome: str) -> str:
    caminho = RAIZ / "app" / nome
    if not caminho.is_file():
        pytest.skip(f"{nome} ausente nesta árvore")
    return caminho.read_text(encoding="utf-8")


def test_a_tela_e_alcancavel_pelo_menu():
    assert "/configurar-ia" in _fonte("templates/base.html"), (
        "sem link no menu, a tela existe e ninguém acha — que era o problema")


def test_quem_esta_sem_ia_recebe_o_caminho_em_qualquer_tela():
    base = _fonte("templates/base.html")
    assert "office_ai_connected" in base and "ai-callout" in base, (
        "o aviso de IA desligada precisa apontar para onde configurá-la")


def test_a_chave_e_testada_antes_de_ser_gravada():
    """Ordem importa: test_credentials ANTES de salvar_chave.

    Invertido, uma chave inválida apagaria a que funcionava e o escritório
    ficaria sem IA até gerar outra no console.
    """
    fonte = _fonte("ia_routes.py")
    assert fonte.index("test_credentials") < fonte.index("G.salvar_chave("), (
        "a gravação passou a acontecer antes do teste")


def test_a_tela_avisa_sobre_a_variavel_do_windows():
    tela = _fonte("templates/configurar_ia.html")
    assert "ambiente do Windows" in tela
    assert "DIAGNOSTICO_JARBAS" in tela


def test_a_tela_nao_promete_que_o_sistema_depende_da_chave():
    """Quem lê 'IA desligada' precisa saber que o resto continua funcionando,
    senão deixa de usar o sistema esperando a chave."""
    tela = _fonte("templates/configurar_ia.html")
    assert "Funciona sem chave" in tela

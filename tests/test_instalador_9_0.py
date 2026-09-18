"""Instalador do Windows: o que não pode voltar a acontecer.

O instalador não roda nesta suíte — é PowerShell, e a máquina de teste é
Linux. O que dá para garantir aqui é que o arquivo não contenha de novo os
defeitos que já custaram uma instalação ou uma orientação errada ao operador.

Não substitui rodar o instalador no Windows. Substitui o esquecimento.
"""

import re
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

# Na árvore de fontes o .ps1 vive em installer/; no pacote montado, na raiz.
VERSAO = (RAIZ / "VERSION.txt").read_text(encoding="utf-8").strip()
NOME_PS1 = f"INSTALAR_JARBAS_{VERSAO.replace('.', '_')}.ps1"
_CANDIDATOS = [_RAIZ / "installer" / NOME_PS1, _RAIZ / NOME_PS1]
PS1 = next((c for c in _CANDIDATOS if c.is_file()), None)

# Pulo no nível do módulo, e não pytest.mark: tools/minipytest.py é o
# executor usado quando não há pytest instalado — o caso da máquina do
# escritório — e o shim dele não implementa marcadores. Um teste que só roda
# num dos dois executores é um teste que não roda onde mais importa.
if PS1 is None:
    pytest.skip("instalador não presente nesta árvore", allow_module_level=True)

FONTE = PS1.read_text(encoding="utf-8-sig") if PS1 else ""
SCRIPTS = _RAIZ / "scripts"


def _codigo() -> str:
    """Só o que o PowerShell executa.

    Os comentários do próprio instalador citam os defeitos antigos para que
    ninguém os reintroduza por desconhecimento. Um guarda que varresse o
    arquivo inteiro acusaria justamente a explicação — e a forma mais fácil
    de calar o teste seria apagar a explicação.
    """
    return re.sub(r"(?m)^\s*#.*$", "", FONTE)


def test_nenhuma_variavel_de_provedor_extinto_e_consultada():
    """$OpenAIKey e $GeminiKey não existem na 9.0.

    O bloco que as lia sobrou do Conselho tri-IA da 8.5. Como PowerShell
    trata variável inexistente como $null sem reclamar, o instalador não
    quebrava: ele imprimia, em TODA instalação, 'Critica adversarial
    DESLIGADA — configure uma segunda chave em /conselho'. Uma instrução sem
    destino, porque a 9.0 só aceita chave da Anthropic, contradizendo a linha
    anterior, que anuncia provedor único.
    """
    for morta in ("$OpenAIKey", "$GeminiKey"):
        assert morta not in _codigo(), (
            f"{morta} não existe na 9.0: o bloco que a lê imprime orientação falsa"
        )


def test_nao_manda_configurar_uma_segunda_chave_de_ia():
    assert "segunda chave" not in _codigo().lower(), (
        "a 9.0 tem provedor único; pedir uma segunda chave não leva a lugar nenhum"
    )


def test_avisa_que_a_critica_e_entre_modelos_da_mesma_linhagem():
    """O operador precisa saber o que ainda tem de conferir à mão."""
    assert re.search(r"mesma linhagem", FONTE), (
        "sem esse aviso, o advogado confia numa crítica que compartilha os "
        "pontos cegos de quem redigiu"
    )


def test_a_interface_do_instalador_nao_promete_openai():
    """A 9.0 não aceita chave da OpenAI; mandar configurá-la é beco sem saída."""
    linhas_say = [l for l in FONTE.splitlines()
                  if l.strip().startswith("Say ") and "OpenAI" in l]
    assert not linhas_say, "mensagens ao operador ainda citam OpenAI:\n" + "\n".join(linhas_say)


def test_manifesto_de_integridade_enumera_os_scripts():
    """Lista fixa deixa de fora justamente o script recém-chegado."""
    assert "Get-ChildItem $InstallDir -Filter '*.ps1'" in FONTE, (
        "Write-InstalledManifest precisa enumerar os .ps1, não listá-los à mão"
    )


def test_env_local_declara_as_chaves_de_seguranca():
    """Variável que não aparece no arquivo é variável que ninguém descobre.

    Foi assim que JARBAS_2FA_OBRIGATORIO passou uma versão inteira existindo
    na documentação e não tendo efeito nenhum.
    """
    for chave in ("JARBAS_2FA_OBRIGATORIO", "JARBAS_SESSION_MAX_AGE",
                  "JARBAS_TRUSTED_PROXY_HOPS"):
        assert chave in FONTE, f"{chave} não é escrita no .env.local gerado"


def test_backup_diario_e_agendado_pela_instalacao():
    assert "BACKUP_AUTOMATICO.ps1" in FONTE, (
        "backup que depende de alguém lembrar não acontece no dia em que a "
        "máquina falha"
    )


def test_todo_wrapper_cmd_aponta_para_um_ps1_que_existe():
    """Wrapper órfão vira 'o botão não faz nada' na mesa do advogado."""
    wrappers = re.findall(r"'([A-Z_]+\.cmd)'='([A-Z_]+\.ps1)'", FONTE)
    assert wrappers, "nenhum wrapper encontrado — a extração do par mudou?"
    faltando = [(cmd, ps1) for cmd, ps1 in wrappers if not (SCRIPTS / ps1).is_file()]
    assert not faltando, f"wrapper sem script correspondente em scripts/: {faltando}"


def test_o_backup_do_windows_nao_copia_o_banco_com_o_servidor_de_pe():
    """Copy-Item de um SQLite em uso gera arquivo que o SQLite recusa a abrir.

    A falha é silenciosa: o arquivo aparece no disco, o operador relaxa, e a
    corrupção só se revela no dia da restauração.
    """
    script = SCRIPTS / "BACKUP_JARBAS.ps1"
    if not script.is_file():
        pytest.skip("BACKUP_JARBAS.ps1 ausente nesta árvore")
    texto = script.read_text(encoding="utf-8-sig")
    assert "tools\\backup.py" in texto or "tools/backup.py" in texto, (
        "o backup precisa passar por tools/backup.py, que usa a API de backup "
        "online do SQLite e confere a cópia"
    )
    assert not re.search(r"Copy-Item.*'data'", texto), (
        "voltou a copiar a pasta data/ diretamente"
    )


# ============================================ a suíte roda dentro do pacote

def test_nenhum_teste_derruba_a_coleta_com_systemexit():
    """SystemExit durante a coleta aborta o pytest INTEIRO.

    Aconteceu duas vezes neste projeto, das duas com o mesmo efeito: o
    comando roda, imprime "no tests ran" e devolve INTERNALERROR — e quem
    olha de relance conclui que está tudo certo.

    Primeiro com tools/test_claude.py, script operacional que o pytest
    coletava pelo nome (resolvido por testpaths no pytest.ini). Depois com
    tests/test_pacote_instalador.py, que usava `raise SystemExit(0)` para se
    autopular dentro do pacote montado. Resultado: na máquina do escritório,
    onde a suíte viaja junto justamente para revalidar a instalação, ela não
    rodava nunca.

    A forma correta de pular um arquivo inteiro é
    `pytest.skip(..., allow_module_level=True)`.
    """
    import re as _re
    pasta = Path(__file__).resolve().parent
    problemas = []
    for arq in sorted(pasta.glob("test_*.py")):
        fonte = arq.read_text(encoding="utf-8")
        sem_docstring = _re.sub(r'"""(?:.|\n)*?"""', "", fonte)
        # Só interessa o nível do módulo: dentro de função, o pytest trata.
        for linha in sem_docstring.splitlines():
            if _re.match(r"^\s{0,4}(raise\s+SystemExit|sys\.exit)\b", linha):
                problemas.append(f"{arq.name}: {linha.strip()}")
    assert not problemas, (
        "SystemExit no nível do módulo aborta a coleta inteira:\n"
        + "\n".join(problemas)
        + "\nUse pytest.skip(..., allow_module_level=True)."
    )


# ================================================ identidade do build

def test_o_pacote_carrega_um_carimbo_de_build():
    """Dois pacotes diziam "9.0.2" e não havia como distingui-los.

    Isso custou uma rodada inteira de correção: o operador extraiu o pacote
    antigo, a instalação falhou com um defeito JÁ corrigido, e nem ele nem eu
    tínhamos como perceber, olhando o diagnóstico, que o pacote aplicado não
    era o enviado.
    """
    montador = _RAIZ / "tools" / "build_installer.py"
    if not montador.is_file():
        pytest.skip("sem tools/ neste layout")
    fonte = montador.read_text(encoding="utf-8")
    assert "_gravar_carimbo" in fonte, "o pacote precisa carregar identidade própria"
    assert "BUILD.txt" in fonte


def test_o_carimbo_entra_no_manifesto():
    """Arquivo fora do manifesto faz o passo [1/19] abortar a instalação."""
    montador = _RAIZ / "tools" / "build_installer.py"
    if not montador.is_file():
        pytest.skip("sem tools/ neste layout")
    fonte = montador.read_text(encoding="utf-8")
    pos_carimbo = fonte.index("_gravar_carimbo(destino)")
    pos_manifesto = fonte.index("_gravar_manifesto(destino)")
    assert pos_carimbo < pos_manifesto, (
        "o carimbo tem de ser escrito ANTES do manifesto, senão fica fora dele"
    )


def test_o_instalador_leva_o_carimbo_para_a_instalacao():
    assert "'BUILD.txt'" in FONTE, (
        "sem copiar o BUILD.txt, a instalação não sabe de qual pacote veio"
    )


def test_o_diagnostico_mostra_o_build_instalado():
    fonte = (SCRIPTS / "DIAGNOSTICO_JARBAS.ps1").read_text(encoding="utf-8-sig")
    assert "BUILD.txt" in fonte
    assert "build instalado" in fonte
    assert "AUSENTE" in fonte, (
        "instalação sem carimbo precisa ser reportada, não passar em branco"
    )


# ======================================== 9.2: instalação visual e amigável

def test_existe_um_unico_arquivo_para_abrir():
    """Antes havia INSTALAR_AGORA.cmd, ATUALIZAR_OU_REPARAR.cmd e o .ps1 solto.

    Quem extrai o ZIP e vê três executáveis não sabe qual clicar, e clicar no
    .ps1 no Windows abre o Bloco de Notas.
    """
    alvo = _RAIZ / "installer" / "INSTALAR.cmd"
    if not alvo.is_file():
        alvo = _RAIZ / "INSTALAR.cmd"
    assert alvo.is_file(), "INSTALAR.cmd não está no pacote"


def test_o_instalador_avisa_quando_rodam_de_dentro_do_zip():
    """A falha nº 1 relatada: clicar no .cmd sem extrair o ZIP.

    O Windows abre uma cópia temporária, o instalador não acha payload/ e o
    erro que aparece fala de manifesto SHA-256 — que não diz nada a quem
    instala.
    """
    for candidato in (_RAIZ / "installer" / "INSTALAR.cmd", _RAIZ / "INSTALAR.cmd"):
        if candidato.is_file():
            texto = candidato.read_text(encoding="utf-8", errors="replace")
            break
    else:
        pytest.skip("INSTALAR.cmd ausente nesta árvore")
    assert "payload" in texto, "não confere se o ZIP foi extraído"
    assert "Extrair Tudo" in texto, "não diz ao operador o que fazer"


def test_os_cmd_usam_quebra_de_linha_do_windows():
    """.cmd com LF sozinho falha em Windows antigo, sem mensagem útil."""
    pasta = _RAIZ / "installer"
    if not pasta.is_dir():
        pytest.skip("sem installer/ neste layout")
    ruins = [c.name for c in sorted(pasta.glob("*.cmd"))
             if b"\r\n" not in c.read_bytes()]
    assert not ruins, f"arquivos .cmd sem CRLF: {ruins}"


def test_a_instalacao_mostra_progresso():
    """19 linhas cinzas iguais não dizem onde a instalação está."""
    assert "Write-Progress" in FONTE, "sem barra de progresso"
    assert "function Passo" in FONTE, "sem cabeçalho de passo"


def test_a_chave_da_ia_e_apresentada_como_opcional():
    """Era o que fazia o operador travar: pular o passo e ficar sem caminho
    de volta, achando que o sistema tinha sido instalado errado."""
    assert "OPCIONAL" in FONTE
    assert "CONFIGURAR IA" in FONTE, (
        "o instalador precisa dizer ONDE configurar a chave depois")


def test_a_instalacao_libera_a_importacao_de_pastas():
    """Sem esta variável, a tela de importação aparece desativada na máquina
    do escritório — que é justamente onde ela deve funcionar."""
    assert "JARBAS_PERMITE_MAPEAR_PASTA=1" in FONTE


def test_o_encerramento_aponta_os_proximos_passos():
    assert "IMPORTAR PASTAS" in FONTE
    assert "duas etapas" in FONTE.lower()

/* Widget do chatbot público de atendimento — JARBAS / CHAGAS – ADVOGADOS.
 *
 * Vanilla JS, sem dependência externa. Duas regras não negociáveis:
 *
 * 1. Todo texto vindo do visitante ou da IA entra no DOM por `textContent`,
 *    nunca por `innerHTML`. O visitante controla o que digita; se algum dia
 *    isso virar innerHTML, uma mensagem com <script> vira execução no
 *    navegador de quem estiver com o chat aberto.
 * 2. O token da conversa fica em sessionStorage (por aba), nunca na URL —
 *    não pode aparecer em log de acesso nem ser copiado sem querer ao
 *    compartilhar o link da página.
 */
(function () {
  "use strict";

  var root = document.getElementById("jarbas-chat-root");
  if (!root) return;
  var inline = root.getAttribute("data-inline") === "1";

  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.getAttribute("content") : "";

  var CHAVE_TOKEN = "jarbas_chat_token";
  var CHAVE_MSGS = "jarbas_chat_msgs";

  function armazenamento() {
    try {
      window.sessionStorage.setItem("__t", "1");
      window.sessionStorage.removeItem("__t");
      return window.sessionStorage;
    } catch (e) {
      return null; // aba privada / storage bloqueado: segue sem persistir
    }
  }
  var storage = armazenamento();

  function lerMsgs() {
    if (!storage) return [];
    try { return JSON.parse(storage.getItem(CHAVE_MSGS) || "[]"); } catch (e) { return []; }
  }
  function salvarMsgs(lista) {
    if (!storage) return;
    try { storage.setItem(CHAVE_MSGS, JSON.stringify(lista.slice(-40))); } catch (e) {}
  }

  // ------------------------------------------------------------- montagem

  var painel = document.createElement("div");
  painel.className = "jarbas-chat-painel" + (inline ? " inline" : "");
  painel.innerHTML =
    '<div class="jarbas-chat-cab">' +
      '<strong>Atendimento</strong><span class="jarbas-chat-sub">Assistente virtual</span>' +
      (inline ? "" : '<button type="button" class="jarbas-chat-fechar" aria-label="Fechar">✕</button>') +
    "</div>" +
    '<div class="jarbas-chat-corpo" role="log" aria-live="polite"></div>' +
    '<div class="jarbas-chat-rapidas"></div>' +
    '<form class="jarbas-chat-contato" hidden>' +
      '<label>Nome<input type="text" name="nome" maxlength="180" required></label>' +
      '<label>Telefone ou e-mail<input type="text" name="contato" maxlength="180" required></label>' +
      '<div class="jarbas-chat-contato-acoes">' +
        '<button type="submit">Enviar</button>' +
        '<button type="button" class="jarbas-chat-contato-cancelar">Cancelar</button>' +
      "</div>" +
    "</form>" +
    '<form class="jarbas-chat-form">' +
      '<textarea name="mensagem" maxlength="1200" rows="1" placeholder="Escreva sua mensagem…" required></textarea>' +
      '<button type="submit" aria-label="Enviar">➤</button>' +
    "</form>" +
    '<div class="jarbas-chat-rodape">' +
      '<a class="jarbas-chat-whats" href="#" hidden target="_blank" rel="noopener">Falar no WhatsApp</a>' +
      '<button type="button" class="jarbas-chat-contatar">Quero que me liguem</button>' +
    "</div>";

  var bolha = null;
  if (inline) {
    root.appendChild(painel);
  } else {
    bolha = document.createElement("button");
    bolha.type = "button";
    bolha.className = "jarbas-chat-bolha";
    bolha.setAttribute("aria-label", "Abrir atendimento");
    bolha.textContent = "💬";
    root.appendChild(bolha);
    root.appendChild(painel);
    painel.hidden = true;
  }

  var corpo = painel.querySelector(".jarbas-chat-corpo");
  var rapidas = painel.querySelector(".jarbas-chat-rapidas");
  var form = painel.querySelector(".jarbas-chat-form");
  var textarea = form.querySelector("textarea");
  var whatsBtn = painel.querySelector(".jarbas-chat-whats");
  var contatarBtn = painel.querySelector(".jarbas-chat-contatar");
  var contatoForm = painel.querySelector(".jarbas-chat-contato");
  var contatoCancelar = painel.querySelector(".jarbas-chat-contato-cancelar");
  var fecharBtn = painel.querySelector(".jarbas-chat-fechar");

  // -------------------------------------------------------------- estado

  var token = storage ? storage.getItem(CHAVE_TOKEN) : null;
  var iniciando = false;

  function bolhaMsg(papel, texto) {
    var div = document.createElement("div");
    div.className = "jarbas-chat-msg " + (papel === "visitor" ? "visitante" : "assistente");
    div.textContent = texto; // nunca innerHTML — texto de visitante/IA é sempre inseguro
    corpo.appendChild(div);
    corpo.scrollTop = corpo.scrollHeight;
  }

  function restaurarHistorico() {
    var msgs = lerMsgs();
    msgs.forEach(function (m) { bolhaMsg(m.role, m.text); });
    return msgs.length > 0;
  }

  function guardarMsg(papel, texto) {
    var msgs = lerMsgs();
    msgs.push({ role: papel, text: texto });
    salvarMsgs(msgs);
  }

  function mostrarWhats(url) {
    if (!url) return;
    whatsBtn.href = url;
    whatsBtn.hidden = false;
  }

  function enviarFormulario(url, dados, aoTerminar) {
    var corpo = new URLSearchParams(dados);
    corpo.set("_csrf", csrf);
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: corpo.toString(),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, corpo: j }; }); })
      .then(function (res) { aoTerminar(null, res); })
      .catch(function (err) { aoTerminar(err, null); });
  }

  function iniciarSessao(cb) {
    if (token) { cb(); return; }
    if (iniciando) return;
    iniciando = true;
    enviarFormulario("/api/chatbot/iniciar", {}, function (err, res) {
      iniciando = false;
      if (err) {
        bolhaMsg("assistant", "Não consegui abrir o atendimento agora. Tente novamente em instantes.");
        return;
      }
      if (!res.ok) {
        // Lê a mensagem específica do servidor (ex.: chat desativado, limite
        // de novas conversas atingido) em vez de um genérico "tente de novo"
        // que não explica nada e nunca mostra o WhatsApp já calculado.
        bolhaMsg("assistant", (res.corpo && res.corpo.erro) || "Não consegui abrir o atendimento agora.");
        if (res.corpo && res.corpo.whatsapp) mostrarWhats(res.corpo.whatsapp);
        return;
      }
      token = res.corpo.token;
      if (storage) storage.setItem(CHAVE_TOKEN, token);
      bolhaMsg("assistant", res.corpo.saudacao);
      guardarMsg("assistant", res.corpo.saudacao);
      mostrarWhats(res.corpo.whatsapp);
      montarRapidas();
      cb();
    });
  }

  function montarRapidas() {
    if (!rapidas || rapidas.childElementCount || lerMsgs().length > 1) return;
    var op1 = document.createElement("button");
    op1.type = "button"; op1.textContent = "Já sou cliente";
    var op2 = document.createElement("button");
    op2.type = "button"; op2.textContent = "Quero saber sobre um caso novo";
    [op1, op2].forEach(function (b) {
      b.addEventListener("click", function () {
        textarea.value = b.textContent;
        rapidas.innerHTML = "";
        form.dispatchEvent(new Event("submit", { cancelable: true }));
      });
      rapidas.appendChild(b);
    });
  }

  function enviarMensagem(texto) {
    bolhaMsg("visitor", texto);
    guardarMsg("visitor", texto);
    var pensando = document.createElement("div");
    pensando.className = "jarbas-chat-msg assistente pensando";
    pensando.textContent = "digitando…";
    corpo.appendChild(pensando);
    corpo.scrollTop = corpo.scrollHeight;

    enviarFormulario("/api/chatbot/mensagem", { token: token, mensagem: texto }, function (err, res) {
      pensando.remove();
      if (err) {
        bolhaMsg("assistant", "Sem conexão no momento. Tente de novo em instantes.");
        return;
      }
      if (!res.ok) {
        bolhaMsg("assistant", res.corpo.erro || "Não consegui responder agora.");
        if (res.corpo.whatsapp) mostrarWhats(res.corpo.whatsapp);
        return;
      }
      bolhaMsg("assistant", res.corpo.resposta);
      guardarMsg("assistant", res.corpo.resposta);
    });
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var texto = (textarea.value || "").trim();
    if (!texto) return;
    textarea.value = "";
    iniciarSessao(function () { enviarMensagem(texto); });
  });

  textarea.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      form.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  contatarBtn.addEventListener("click", function () {
    contatoForm.hidden = !contatoForm.hidden;
  });
  contatoCancelar.addEventListener("click", function () {
    contatoForm.hidden = true;
  });
  contatoForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var nome = contatoForm.nome.value.trim();
    var contato = contatoForm.contato.value.trim();
    if (!nome || !contato) return;
    iniciarSessao(function () {
      enviarFormulario("/api/chatbot/contato", { token: token, nome: nome, contato: contato }, function (err, res) {
        if (err || !res.ok) {
          bolhaMsg("assistant", (res && res.corpo && res.corpo.erro) || "Não consegui registrar seus dados agora. Tente pelo WhatsApp.");
          return;
        }
        contatoForm.hidden = true;
        contatoForm.reset();
        bolhaMsg("assistant", res.corpo.resposta);
        guardarMsg("assistant", res.corpo.resposta);
      });
    });
  });

  if (bolha) {
    bolha.addEventListener("click", function () {
      painel.hidden = !painel.hidden;
      if (!painel.hidden) {
        var tinhaHistorico = restaurarHistorico();
        if (!tinhaHistorico) { iniciarSessao(function () {}); }
        else { montarRapidas(); }
        textarea.focus();
      }
    });
  }
  if (fecharBtn) {
    fecharBtn.addEventListener("click", function () { painel.hidden = true; });
  }

  if (inline) {
    var tinhaHistorico = restaurarHistorico();
    if (!tinhaHistorico) { iniciarSessao(function () {}); }
    else { montarRapidas(); }
  }
})();

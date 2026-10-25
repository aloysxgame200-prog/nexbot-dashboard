// Nexbot — interface d'administration d'un serveur.
// Tout le contenu dynamique est inséré via textContent / createElement (pas d'injection HTML).
(function () {
  "use strict";

  const BOOT = JSON.parse(document.getElementById("boot").textContent);
  const G = BOOT.guild;
  const BASE = `/api/guilds/${G.id}`;
  const app = document.getElementById("app");
  let DISCORD = null;
  let leaveGuard = null; // fonction renvoyant true si des modifications ne sont pas enregistrées

  // ------------------------------------------------------------------ utilitaires DOM
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k in el && typeof v !== "string") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  const fmtDate = (iso) => iso ? new Date(iso).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "—";
  const clone = (o) => JSON.parse(JSON.stringify(o));

  async function api(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (method !== "GET") opts.headers["X-CSRF-Token"] = BOOT.csrf;
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, opts); } catch (e) { throw { status: 0, message: "Connexion impossible au Dashboard.", errors: [] }; }
    if (res.status === 401) { window.location.href = "/login"; throw { status: 401, message: "Session expirée", errors: [] }; }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw { status: res.status, message: (data && data.message) || `Erreur ${res.status}`, errors: (data && data.errors) || [] };
    return data;
  }

  async function loadDiscord() {
    if (DISCORD) return DISCORD;
    try { DISCORD = await api("GET", `${BASE}/discord`); }
    catch (e) { toast(e.message, "error"); DISCORD = { channels: [], roles: [] }; }
    return DISCORD;
  }

  const premiumBadge = () => h("span", { class: "badge premium", text: "✦ Premium" });
  const upgradeNotice = (text) => h("div", { class: "notice" },
    h("b", { text: "Fonctionnalité Premium. " }), text || "Vous pouvez configurer et prévisualiser, l'enregistrement nécessite le Premium. ",
    h("a", { href: `/g/${G.id}/premium`, "data-nav": "premium", text: "Voir le Premium →" }));

  // ------------------------------------------------------------------ navigation
  const ICONS = { overview: "◎", general: "⚙", moderation: "⚖", antiraid: "🛡", logs: "📜", welcome: "👋",
    autoroles: "🏷", tickets: "🎫", suggestions: "💡", reports: "🚩", theme: "🎨", stats: "📊",
    embeds: "✉", automations: "⚡", sanctions: "📋", incidents: "🚨", premium: "✦", help: "?" };
  const NAV = [
    ["Serveur", [["overview", "Vue d'ensemble"], ["general", "Général"]]],
    ["Sécurité", [["antiraid", "Anti-raid"], ["moderation", "Modération"], ["incidents", "Incidents"], ["sanctions", "Sanctions"]]],
    ["Communauté", [["welcome", "Bienvenue & départ"], ["autoroles", "Rôles automatiques"], ["tickets", "Tickets"], ["suggestions", "Suggestions"], ["reports", "Signalements"]]],
    ["Suivi", [["logs", "Logs"], ["stats", "Statistiques"]]],
    ["Personnalisation", [["embeds", "Éditeur d'embeds"], ["automations", "Automatisations"], ["theme", "Thème du Dashboard"]]],
    ["Compte", [["premium", "Premium"], ["help", "Aide"]]],
  ];
  const PREMIUM_SECTIONS = new Set(["theme", "stats", "embeds"]);
  const MODULE_KEYS = new Set(BOOT.modules.map((m) => m.key));

  function renderNav(active) {
    const nav = clear(document.getElementById("nav"));
    for (const [group, items] of NAV) {
      nav.append(h("div", { class: "group", text: group }));
      for (const [key, label] of items) {
        nav.append(h("a", { class: "nav" + (key === active ? " active" : ""), href: `/g/${G.id}/${key}`, "data-nav": key },
          h("span", { text: ICONS[key] || "•" }), label,
          PREMIUM_SECTIONS.has(key) && !G.premium ? h("span", { class: "lock", text: "✦" }) : null));
      }
    }
    nav.append(h("div", { class: "group", text: "Liens" }),
      h("a", { class: "nav", href: BOOT.support_url, target: "_blank", rel: "noopener" }, h("span", { text: "↗" }), "Serveur de support"),
      h("a", { class: "nav", href: "/servers" }, h("span", { text: "←" }), "Mes serveurs"));
  }

  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a[data-nav]");
    if (!a || ev.metaKey || ev.ctrlKey || ev.button !== 0) return;
    ev.preventDefault();
    go(a.dataset.nav);
  });
  window.addEventListener("popstate", () => route(location.pathname.split("/")[3] || "overview", false));
  window.addEventListener("beforeunload", (ev) => { if (leaveGuard && leaveGuard()) { ev.preventDefault(); ev.returnValue = ""; } });
  const toggleBtn = document.getElementById("menu-toggle");
  if (toggleBtn) toggleBtn.addEventListener("click", () => document.getElementById("sidebar").classList.toggle("open"));

  function go(section) {
    if (leaveGuard && leaveGuard() && !confirm("Des modifications ne sont pas enregistrées. Quitter quand même ?")) return;
    history.pushState({}, "", `/g/${G.id}/${section}`);
    route(section, true);
  }

  async function route(section, scroll) {
    leaveGuard = null;
    renderNav(section);
    document.getElementById("sidebar").classList.remove("open");
    clear(app).append(h("div", { class: "skeleton" }));
    if (scroll) window.scrollTo(0, 0);
    try {
      if (section === "overview") await pageOverview();
      else if (MODULE_KEYS.has(section)) await pageModule(section);
      else if (section === "embeds") await pageEmbeds();
      else if (section === "automations") await pageAutomations();
      else if (section === "sanctions") await pageSanctions();
      else if (section === "incidents") await pageIncidents();
      else if (section === "premium") await pagePremium();
      else pageHelp();
    } catch (e) {
      clear(app).append(h("div", { class: "notice danger" }, h("b", { text: "Erreur : " }), e.message || String(e)));
    }
  }

  function head(title, subtitle, ...actions) {
    return h("div", { class: "page-head" }, h("div", {}, h("h1", { text: title }), subtitle ? h("p", { class: "muted", text: subtitle }) : null),
      actions.length ? h("div", { class: "row" }, ...actions) : null);
  }

  // ------------------------------------------------------------------ aperçu Discord
  function sampleVars() {
    const now = new Date();
    return {
      "user": BOOT.user.name, "user.name": BOOT.user.name.toLowerCase().replace(/\s+/g, ""), "user.mention": "\u0000@" + BOOT.user.name + "\u0001",
      "user.id": BOOT.user.id, "user.avatar": BOOT.user.avatar, "user.created": "12/03/2021",
      "server": G.name, "server.id": G.id, "server.icon": G.icon ? `https://cdn.discordapp.com/icons/${G.id}/${G.icon}.png` : "",
      "member_count": "1 234", "date": now.toLocaleDateString("fr-FR"), "time": now.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }),
      "moderator": "\u0000@Modérateur\u0001", "reason": "Spam", "duration": "1 heure", "case_id": "42",
      "ticket.id": "17", "ticket.category": "Support",
    };
  }
  const sub = (text, vars) => (text || "").replace(/\{([a-zA-Z_.]+)\}/g, (m, k) => (k in vars ? vars[k] : m));
  function esc(s) { return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function md(text, vars) {
    // Échappement d'abord, puis mise en forme minimale façon Discord : sûr par construction.
    let s = esc(sub(text, vars));
    s = s.replace(/\u0000(.*?)\u0001/g, '<span class="dc-mention">$1</span>')
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/__(.+?)__/g, "<u>$1</u>")
      .replace(/\*(.+?)\*/g, "<i>$1</i>").replace(/`([^`]+)`/g, "<code>$1</code>");
    return s;
  }
  const mdEl = (tag, cls, text, vars) => { const el = h(tag, { class: cls }); el.innerHTML = md(text, vars); return el; };
  const imgOk = (u) => /^https?:\/\//.test(u || "");

  function preview(msg, extra) {
    const vars = sampleVars();
    const body = h("div", { style: { minWidth: 0, flex: 1 } },
      h("div", { class: "who" }, "Nexbot", h("span", { class: "tag", text: "BOT" }), h("span", { class: "when", text: "Aujourd'hui à " + vars.time })));
    if (msg.content) body.append(mdEl("div", "dc-content", msg.content, vars));
    const e = msg.embed;
    if (e) {
      const em = h("div", { class: "dc-embed", style: { borderLeftColor: e.color || "#7C5CFF" } });
      const left = h("div", { style: { minWidth: 0 } });
      if (e.author_name) {
        const icon = sub(e.author_icon_url, vars);
        left.append(h("div", { class: "e-author" }, imgOk(icon) ? h("img", { src: icon, alt: "" }) : null, sub(e.author_name, vars).replace(/[\u0000\u0001]/g, "")));
      }
      if (e.title) {
        const t = h("div", { class: "e-title" });
        const url = sub(e.url, vars);
        const inner = mdEl("span", "", e.title, vars);
        if (imgOk(url)) t.append(h("a", { href: url, target: "_blank", rel: "noopener" }, inner)); else t.append(inner);
        left.append(t);
      }
      if (e.description) left.append(mdEl("div", "e-desc", e.description, vars));
      if (e.fields && e.fields.length) {
        const fs = h("div", { class: "e-fields" });
        for (const f of e.fields) fs.append(h("div", { class: "e-field" + (f.inline ? " inline" : "") }, mdEl("b", "", f.name, vars), mdEl("span", "", f.value, vars)));
        left.append(fs);
      }
      em.append(left);
      const th = sub(e.thumbnail_url, vars);
      em.append(imgOk(th) ? h("div", { class: "e-thumb" }, h("img", { src: th, alt: "" })) : h("div"));
      const im = sub(e.image_url, vars);
      if (imgOk(im)) em.append(h("div", { class: "e-image" }, h("img", { src: im, alt: "" })));
      if (e.footer_text || e.timestamp) {
        const fi = sub(e.footer_icon_url, vars);
        em.append(h("div", { class: "e-footer" }, imgOk(fi) ? h("img", { src: fi, alt: "" }) : null,
          [sub(e.footer_text, vars).replace(/[\u0000\u0001]/g, ""), e.timestamp ? "Aujourd'hui à " + vars.time : ""].filter(Boolean).join(" • ")));
      }
      body.append(em);
    }
    if (extra && extra.button) body.append(h("span", { class: "dc-button " + (extra.button.style || "primary"), text: extra.button.label }));
    if (!msg.content && !e && !(extra && extra.button)) body.append(h("div", { class: "muted small", text: "(message vide)" }));
    return h("div", { class: "dc-preview" }, h("div", { class: "dc-msg" }, h("div", { class: "av", text: "N" }), body));
  }

  // ------------------------------------------------------------------ éditeur d'embed (réutilisable)
  const LIMITS = { title: 256, description: 4096, author_name: 256, footer_text: 2048, fname: 256, fvalue: 1024, total: 6000, fields: 25 };
  function newEmbed() { return { title: "", description: "", url: "", color: "#7C5CFF", author_name: "", author_icon_url: "", author_url: "", thumbnail_url: "", image_url: "", footer_text: "", footer_icon_url: "", timestamp: false, fields: [] }; }
  function embedTotal(e) { return (e.title || "").length + (e.description || "").length + (e.author_name || "").length + (e.footer_text || "").length + (e.fields || []).reduce((n, f) => n + f.name.length + f.value.length, 0); }

  function varButtons(context, target, onInsert) {
    const allowed = VARS_BY_CONTEXT[context] || Object.keys(VARS_BY_CONTEXT.generic);
    return h("div", { class: "vars" }, allowed.map((v) => h("button", { type: "button", title: VAR_LABELS[v] || v, text: "{" + v + "}",
      onclick: () => { const el = target(); if (!el) return; const p = el.selectionStart ?? el.value.length; el.value = el.value.slice(0, p) + "{" + v + "}" + el.value.slice(el.selectionEnd ?? p); el.dispatchEvent(new Event("input")); el.focus(); if (onInsert) onInsert(); } })));
  }
  const VAR_LABELS = {};
  const VARS_BY_CONTEXT = {
    welcome: ["user", "user.name", "user.mention", "user.id", "user.avatar", "user.created", "server", "server.id", "server.icon", "member_count", "date", "time"],
    moderation: ["user", "user.name", "user.mention", "user.id", "user.avatar", "server", "server.id", "server.icon", "moderator", "reason", "duration", "case_id", "date", "time"],
    ticket: ["user", "user.name", "user.mention", "user.id", "user.avatar", "server", "server.id", "server.icon", "ticket.id", "ticket.category", "date", "time"],
    announcement: ["server", "server.id", "server.icon", "member_count", "date", "time"],
  };
  VARS_BY_CONTEXT.generic = [...new Set(Object.values(VARS_BY_CONTEXT).flat())];

  function counter(input, max) {
    const c = h("div", { class: "counter" });
    const upd = () => { c.textContent = `${input.value.length} / ${max}`; c.classList.toggle("over", input.value.length > max); };
    input.addEventListener("input", upd); upd();
    return c;
  }

  function embedEditor(embed, onChange, context, disabled) {
    const root = h("div", { class: "embed-editor stack" });
    const total = h("div", { class: "counter" });
    const changed = () => { total.textContent = `Total : ${embedTotal(embed)} / ${LIMITS.total} caractères`; total.classList.toggle("over", embedTotal(embed) > LIMITS.total); onChange(embed); };
    const text = (key, label, max, area) => {
      const input = h(area ? "textarea" : "input", { type: "text", value: embed[key] || "", disabled, maxlength: max || 2000 });
      if (area) input.value = embed[key] || "";
      input.addEventListener("input", () => { embed[key] = input.value; changed(); });
      return h("div", {}, h("label", { class: "small muted", text: label }), input, max ? counter(input, max) : null,
        area ? varButtons(context, () => input) : null);
    };
    const color = h("input", { type: "color", value: embed.color || "#7C5CFF", disabled });
    color.addEventListener("input", () => { embed.color = color.value.toUpperCase(); changed(); });
    const ts = h("input", { type: "checkbox", checked: !!embed.timestamp, disabled });
    ts.addEventListener("change", () => { embed.timestamp = ts.checked; changed(); });
    const fieldsBox = h("div", { class: "efields" });
    const drawFields = () => {
      clear(fieldsBox);
      embed.fields.forEach((f, i) => {
        const n = h("input", { type: "text", value: f.name, placeholder: "Nom du champ", maxlength: LIMITS.fname, disabled });
        const v = h("textarea", { placeholder: "Valeur", maxlength: LIMITS.fvalue, disabled }); v.value = f.value; v.style.minHeight = "56px";
        const inl = h("input", { type: "checkbox", checked: !!f.inline, disabled });
        n.addEventListener("input", () => { f.name = n.value; changed(); });
        v.addEventListener("input", () => { f.value = v.value; changed(); });
        inl.addEventListener("change", () => { f.inline = inl.checked; changed(); });
        fieldsBox.append(h("div", { class: "efield stack" }, n, v,
          h("div", { class: "row" }, h("label", { class: "row small" }, inl, "En ligne"), h("span", { class: "grow" }),
            h("button", { class: "btn sm danger", type: "button", disabled, text: "Retirer", onclick: () => { embed.fields.splice(i, 1); drawFields(); changed(); } }))));
      });
    };
    drawFields();
    root.append(
      h("div", { class: "row" }, h("div", { class: "grow" }, text("title", "Titre", LIMITS.title)), h("div", {}, h("label", { class: "small muted", text: "Couleur" }), h("div", {}, color))),
      text("description", "Description", LIMITS.description, true),
      h("div", { class: "grid cols-2" }, text("author_name", "Auteur", LIMITS.author_name), text("author_icon_url", "Icône de l'auteur (URL)")),
      h("div", { class: "grid cols-2" }, text("url", "Lien du titre (URL)"), text("author_url", "Lien de l'auteur (URL)")),
      h("div", { class: "grid cols-2" }, text("thumbnail_url", "Miniature (URL ou {user.avatar})"), text("image_url", "Image (URL)")),
      h("div", { class: "grid cols-2" }, text("footer_text", "Pied de page", LIMITS.footer_text), text("footer_icon_url", "Icône du pied de page (URL)")),
      h("label", { class: "row small" }, ts, "Afficher l'heure d'envoi"),
      h("div", {}, h("div", { class: "row" }, h("b", { text: "Champs" }), h("span", { class: "grow" }),
        h("button", { class: "btn sm", type: "button", disabled, text: "+ Ajouter un champ", onclick: () => {
          if (embed.fields.length >= LIMITS.fields) return toast("25 champs maximum", "error");
          embed.fields.push({ name: "Nouveau champ", value: "Valeur", inline: false }); drawFields(); changed(); } })), fieldsBox),
      total);
    changed();
    return root;
  }

  // ------------------------------------------------------------------ contrôles de formulaire
  function channelOptions(types) { return (DISCORD ? DISCORD.channels : []).filter((c) => types.includes(c.type)); }
  function idSelect(items, value, disabled, prefix, emptyLabel) {
    const sel = h("select", { disabled }, h("option", { value: "", text: emptyLabel || "— Aucun —" }));
    let found = !value;
    for (const it of items) { const o = h("option", { value: it.id, text: (prefix || "") + it.name }); if (it.id === value) { o.selected = true; found = true; } sel.append(o); }
    if (!found) sel.append(h("option", { value, selected: true, text: `Introuvable (${value})` }));
    return sel;
  }
  const textChannels = () => channelOptions([0, 5]);
  const categories = () => channelOptions([4]);
  const roles = () => (DISCORD ? DISCORD.roles : []).filter((r) => !r.managed);
  const roleColor = (r) => r && r.color ? "#" + r.color.toString(16).padStart(6, "0") : "#99AAB5";

  function multiIds(kind, values, onChange, disabled, max) {
    const items = kind === "roles" ? roles() : textChannels();
    const byId = Object.fromEntries(items.map((x) => [x.id, x]));
    const box = h("div");
    const draw = () => {
      clear(box);
      const sel = idSelect(items.filter((x) => !values.includes(x.id)), "", disabled, kind === "roles" ? "@" : "#", kind === "roles" ? "Ajouter un rôle…" : "Ajouter un salon…");
      sel.addEventListener("change", () => { if (!sel.value) return; if (max && values.length >= max) { toast(`Maximum : ${max}`, "error"); sel.value = ""; return; } values.push(sel.value); onChange(values); draw(); });
      box.append(sel, h("div", { class: "chips" }, values.map((id, i) => h("span", { class: "chip" },
        kind === "roles" ? h("span", { class: "dot", style: { background: roleColor(byId[id]) } }) : "#",
        byId[id] ? byId[id].name : `Introuvable (${id})`,
        h("button", { type: "button", disabled, title: "Retirer", text: "×", onclick: () => { values.splice(i, 1); onChange(values); draw(); } })))));
    };
    draw();
    return box;
  }

  function control(f, state, onChange, locked) {
    const v = state[f.name];
    const set = (val) => { state[f.name] = val; onChange(f.name); };
    switch (f.ui) {
      case "toggle": {
        const input = h("input", { type: "checkbox", checked: !!v, disabled: locked });
        input.addEventListener("change", () => set(input.checked));
        return h("label", { class: "switch" }, input, h("span"));
      }
      case "number": {
        const input = h("input", { type: "number", value: v, min: f.min, max: f.max, disabled: locked, step: 1 });
        input.addEventListener("input", () => set(input.value === "" ? null : Number(input.value)));
        return input;
      }
      case "color": {
        const input = h("input", { type: "color", value: v, disabled: locked });
        const code = h("code", { text: v });
        input.addEventListener("input", () => { code.textContent = input.value.toUpperCase(); set(input.value.toUpperCase()); });
        return h("div", { class: "row" }, input, code);
      }
      case "select": {
        const sel = h("select", { disabled: locked });
        for (const [val, label] of f.options) sel.append(h("option", { value: val, text: label, selected: val === v }));
        sel.addEventListener("change", () => set(sel.value));
        return sel;
      }
      case "multiselect": {
        const cur = new Set(v || []);
        return h("div", { class: "checks" }, f.options.map(([val, label]) => {
          const cb = h("input", { type: "checkbox", checked: cur.has(val), disabled: locked });
          cb.addEventListener("change", () => { cb.checked ? cur.add(val) : cur.delete(val); set(f.options.map((o) => o[0]).filter((x) => cur.has(x))); });
          return h("label", {}, cb, label);
        }));
      }
      case "channel": case "category": {
        const sel = idSelect(f.ui === "category" ? categories() : textChannels(), v, locked, f.ui === "category" ? "📁 " : "#");
        sel.addEventListener("change", () => set(sel.value));
        return sel;
      }
      case "role": {
        const sel = idSelect(roles(), v, locked, "@");
        sel.addEventListener("change", () => set(sel.value));
        return sel;
      }
      case "roles": case "channels":
        return multiIds(f.ui, [...(v || [])], (vals) => set([...vals]), locked, G.premium ? f.maxlength : (f.free_max || f.maxlength));
      case "users": case "words": {
        const ta = h("textarea", { disabled: locked, placeholder: f.ui === "users" ? "Un identifiant Discord par ligne" : "Un élément par ligne" });
        ta.value = (v || []).join("\n");
        ta.addEventListener("input", () => set(ta.value.split("\n").map((x) => x.trim()).filter(Boolean)));
        const extra = f.free_max && !G.premium ? h("div", { class: "help", text: `Limite gratuite : ${f.free_max}` }) : null;
        return h("div", {}, ta, extra);
      }
      case "template": case "textarea": {
        const ta = h("textarea", { disabled: locked, maxlength: f.maxlength || 4000 });
        ta.value = v || "";
        ta.addEventListener("input", () => set(ta.value));
        return h("div", {}, ta, f.maxlength ? counter(ta, f.maxlength) : null, f.ui === "template" && !locked ? varButtons(f.context || "generic", () => ta) : null);
      }
      case "embed": {
        const wrap = h("div", { class: "stack" });
        const cb = h("input", { type: "checkbox", checked: !!v, disabled: locked });
        const editorBox = h("div");
        const draw = () => {
          clear(editorBox);
          if (state[f.name]) editorBox.append(embedEditor(state[f.name], () => onChange(f.name), f.context || "generic", locked));
        };
        cb.addEventListener("change", () => {
          state[f.name] = cb.checked ? Object.assign(newEmbed(), { description: f.context === "welcome" ? "Bienvenue {user.mention} sur **{server}** !" : "Votre texte ici", thumbnail_url: f.context === "welcome" ? "{user.avatar}" : "" }) : null;
          onChange(f.name); draw();
        });
        wrap.append(h("label", { class: "row" }, h("span", { class: "switch" }, cb, h("span")), "Utiliser un embed"), editorBox);
        draw();
        return wrap;
      }
      default: {
        const input = h("input", { type: "text", value: v ?? "", disabled: locked, maxlength: f.maxlength || 500 });
        input.addEventListener("input", () => set(input.value));
        return input;
      }
    }
  }

  // ------------------------------------------------------------------ page module
  const MODULE_NEEDS_DISCORD = new Set(["general", "moderation", "antiraid", "logs", "welcome", "autoroles", "tickets", "suggestions", "reports"]);

  async function pageModule(key) {
    const [data] = await Promise.all([api("GET", `${BASE}/modules/${key}`), MODULE_NEEDS_DISCORD.has(key) ? loadDiscord() : null]);
    for (const [k, label] of Object.entries(data.variables || {})) VAR_LABELS[k] = label;
    const original = JSON.stringify(data.values);
    const state = clone(data.values);
    const moduleLocked = data.premium_only && !G.premium;
    const fieldEls = {};
    const savebar = h("div", { class: "savebar" });
    const status = h("span", { class: "muted", text: "Aucune modification" });
    const saveBtn = h("button", { class: "btn primary", type: "button", text: "Enregistrer", disabled: true });
    const resetBtn = h("button", { class: "btn ghost", type: "button", text: "Annuler", disabled: true });
    const isDirty = () => JSON.stringify(state) !== original;
    leaveGuard = isDirty;
    const extras = [];
    const onChange = (name) => {
      const d = isDirty();
      savebar.classList.toggle("dirty", d);
      saveBtn.disabled = !d || moduleLocked; resetBtn.disabled = !d;
      status.textContent = d ? "Modifications non enregistrées" : "Aucune modification";
      if (fieldEls[name]) { fieldEls[name].classList.remove("has-error"); const e = fieldEls[name].querySelector(".err"); if (e) e.remove(); }
      for (const fn of extras) fn(state, name);
    };

    const sections = new Map();
    for (const f of data.schema) {
      const sec = f.section || "";
      if (!sections.has(sec)) sections.set(sec, []);
      sections.get(sec).push(f);
    }
    const page = h("div");
    page.append(head(data.title, data.description));
    if (moduleLocked) page.append(upgradeNotice("Ce module est réservé au Premium. "));
    else if (!G.premium && data.schema.some((f) => f.premium)) page.append(h("div", { class: "notice info" }, "Les options marquées ", premiumBadge(), " sont réservées au Premium. Elles restent visibles (et conservées si vous les aviez configurées), mais ne sont pas appliquées sans abonnement."));

    for (const [sec, fields] of sections) {
      const card = h("div", { class: "card" });
      if (sec) card.append(h("h3", {}, sec.replace(" (Premium)", ""), sec.includes("Premium") ? premiumBadge() : null));
      for (const f of fields) {
        const locked = moduleLocked || (f.premium && !G.premium);
        const ctl = control(f, state, onChange, locked);
        const inline = f.ui === "toggle";
        const row = h("div", { class: "field" + (inline ? " inline" : "") + (f.ui === "embed" ? " full" : "") + (locked ? " locked" : "") },
          h("div", {}, h("div", { class: "lbl" }, f.label, f.premium && !data.premium_only ? premiumBadge() : null), f.help ? h("div", { class: "help", text: f.help }) : null),
          h("div", { class: "ctl" }, ctl));
        fieldEls[f.name] = row;
        card.append(row);
      }
      page.append(card);
    }

    // Aperçus et outils propres à certains modules
    if (key === "welcome") page.append(welcomePreview(state, extras));
    if (key === "theme") themeLive(extras);
    if (key === "tickets") page.append(ticketsTools(state, extras));

    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true; status.textContent = "Enregistrement…";
      try {
        const res = await api("PUT", `${BASE}/modules/${key}`, state);
        toast("Paramètres enregistrés — Nexbot les applique sous quelques secondes.");
        route(key, false);
        void res;
      } catch (e) {
        status.textContent = e.message; saveBtn.disabled = false;
        toast(e.message, "error");
        for (const err of e.errors || []) {
          const name = (err.field || "").split(".")[0];
          const el = fieldEls[name];
          if (el) { el.classList.add("has-error"); el.querySelector(".ctl").append(h("div", { class: "err", text: err.message })); }
        }
      }
    });
    resetBtn.addEventListener("click", () => route(key, false));
    savebar.append(status, h("div", { class: "row" }, resetBtn, saveBtn));
    page.append(savebar);

    if (key === "suggestions" || key === "reports" || key === "tickets") page.append(await recordsCard(key));
    clear(app).append(page);
  }

  function welcomePreview(state, extras) {
    const box = h("div");
    const draw = () => {
      clear(box);
      const join = { content: (state.mention_member && state.embed && !(state.text || "").includes("{user.mention}") ? "{user.mention} " : "") + (state.text || ""), embed: G.premium ? state.embed : null };
      const leave = { content: state.leave_text || "", embed: G.premium ? state.leave_embed : null };
      box.append(h("div", { class: "grid cols-2" },
        h("div", {}, h("h3", { text: "Aperçu — arrivée" }), preview(join)),
        h("div", {}, h("h3", { text: "Aperçu — départ" }), preview(leave))));
    };
    extras.push(draw); draw();
    return h("div", { class: "card" }, box);
  }

  function themeLive(extras) {
    // Aperçu en direct : appliqué à cette page uniquement tant qu'il n'est pas enregistré.
    const map = { primary: "--primary", secondary: "--secondary", accent: "--accent", background: "--bg", surface: "--surface" };
    extras.push((state) => {
      for (const [k, cssVar] of Object.entries(map)) document.body.style.setProperty(cssVar, state[k]);
      document.body.style.setProperty("--radius", state.radius + "px");
      document.body.classList.toggle("compact", state.density === "compact");
    });
  }

  function ticketsTools(state, extras) {
    const pv = h("div");
    const draw = () => {
      clear(pv);
      const embed = G.premium && state.panel_embed ? state.panel_embed : { ...newEmbed(), title: state.panel_title, description: state.panel_description };
      pv.append(preview({ content: "", embed }, { button: { label: state.button_label || "Ouvrir un ticket", style: G.premium ? state.button_style : "primary" } }));
    };
    extras.push(draw); draw();
    const sel = idSelect(textChannels(), "", false, "#", "Choisir le salon du panneau…");
    const btn = h("button", { class: "btn primary", type: "button", text: "Poster le panneau" });
    const out = h("span", { class: "muted small" });
    btn.addEventListener("click", async () => {
      if (!sel.value) return toast("Choisissez un salon", "error");
      btn.disabled = true;
      try { const r = await api("POST", `${BASE}/tickets/panel`, { channel_id: sel.value }); await followJob(r.job_id, out); }
      catch (e) { toast(e.message, "error"); }
      btn.disabled = false;
    });
    return h("div", { class: "card" }, h("h3", { text: "Panneau des tickets" }),
      h("p", { class: "muted small", text: "Enregistrez vos paramètres, puis postez le panneau. Les membres ouvriront un ticket privé en un clic." }),
      pv, h("hr", { class: "sep" }), h("div", { class: "row" }, h("div", { class: "grow" }, sel), btn), out);
  }

  async function followJob(jobId, out) {
    out.textContent = "Action transmise à Nexbot…";
    for (let i = 0; i < 15; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const j = await api("GET", `${BASE}/jobs/${jobId}`);
      if (j.status === "done") { out.textContent = "✓ " + (j.result || "Terminé"); toast(j.result || "Action effectuée"); return j; }
      if (j.status === "failed") { out.textContent = "✗ " + (j.result || "Échec"); toast(j.result || "L'action a échoué", "error"); return j; }
    }
    out.textContent = "Nexbot n'a pas encore traité l'action. Est-il en ligne ? Elle sera exécutée dès sa reconnexion.";
    return null;
  }

  async function recordsCard(kind) {
    const titles = { tickets: "Tickets", suggestions: "Suggestions", reports: "Signalements" };
    const data = await api("GET", `${BASE}/records/${kind}`);
    const card = h("div", { class: "card" }, h("h3", { text: `${titles[kind]} récents` }));
    if (!data.items.length) { card.append(h("div", { class: "empty", text: "Rien pour le moment." })); return card; }
    const t = h("table");
    if (kind === "tickets") {
      t.append(h("thead", {}, h("tr", {}, ["#", "Membre", "Catégorie", "Statut", "Ouvert le"].map((x) => h("th", { text: x })))));
      t.append(h("tbody", {}, data.items.map((r) => h("tr", {}, h("td", { text: r.id }), h("td", { class: "mono", text: r.opener_id }), h("td", { text: r.category }),
        h("td", {}, h("span", { class: "badge " + (r.status === "open" ? "ok" : "off"), text: r.status === "open" ? "Ouvert" : "Fermé" })), h("td", { text: fmtDate(r.created_at) })))));
    } else if (kind === "suggestions") {
      const lab = { pending: ["En attente", "warn"], accepted: ["Acceptée", "ok"], refused: ["Refusée", "danger"] };
      t.append(h("thead", {}, h("tr", {}, ["#", "Suggestion", "Auteur", "Statut", "Date"].map((x) => h("th", { text: x })))));
      t.append(h("tbody", {}, data.items.map((r) => h("tr", {}, h("td", { text: r.id }), h("td", { text: r.content }), h("td", { class: "mono", text: r.author_id }),
        h("td", {}, h("span", { class: "badge " + (lab[r.status] || ["", "off"])[1], text: (lab[r.status] || [r.status])[0] })), h("td", { text: fmtDate(r.created_at) })))));
    } else {
      t.append(h("thead", {}, h("tr", {}, ["#", "Signalé", "Par", "Raison", "Statut", ""].map((x) => h("th", { text: x })))));
      const lab = { open: ["Ouvert", "warn"], handled: ["Traité", "ok"], dismissed: ["Classé", "off"] };
      t.append(h("tbody", {}, data.items.map((r) => {
        const actions = h("td", { class: "row" });
        for (const [st, label] of [["handled", "Traité"], ["dismissed", "Classer"]]) {
          if (r.status !== st) actions.append(h("button", { class: "btn sm", type: "button", text: label, onclick: async () => {
            try { await api("POST", `${BASE}/records/reports/${r.id}`, { status: st }); toast("Signalement mis à jour"); route("reports", false); } catch (e) { toast(e.message, "error"); } } }));
        }
        return h("tr", {}, h("td", { text: r.id }), h("td", { class: "mono", text: r.target_id }), h("td", { class: "mono", text: r.reporter_id }),
          h("td", {}, r.reason, r.message_link ? h("div", {}, h("a", { href: r.message_link, target: "_blank", rel: "noopener", text: "Voir le message" })) : null),
          h("td", {}, h("span", { class: "badge " + lab[r.status][1], text: lab[r.status][0] })), actions);
      })));
    }
    card.append(h("div", { class: "table-wrap" }, t));
    return card;
  }

  // ------------------------------------------------------------------ vue d'ensemble
  const PALETTE = ["var(--primary)", "var(--secondary)", "var(--accent)", "#34D399", "#FBBF24", "#F2545B", "#A3A3C2"];

  function chart(stats, labels, style) {
    const W = 760, H = 240, P = { l: 36, r: 8, t: 10, b: 26 };
    const keys = Object.keys(stats.series);
    const n = stats.labels.length;
    const max = Math.max(1, ...keys.flatMap((k) => stats.series[k]));
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    const mk = (tag, attrs, text) => { const e = document.createElementNS(ns, tag); for (const [a, b] of Object.entries(attrs)) e.setAttribute(a, b); if (text !== undefined) e.textContent = text; svg.append(e); return e; };
    const iw = W - P.l - P.r, ih = H - P.t - P.b;
    for (let i = 0; i <= 4; i++) {
      const y = P.t + ih - (ih * i) / 4;
      mk("line", { x1: P.l, x2: W - P.r, y1: y, y2: y, stroke: "var(--border)", "stroke-width": 1 });
      mk("text", { x: P.l - 6, y: y + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": 11 }, Math.round((max * i) / 4));
    }
    const step = iw / n;
    const every = Math.ceil(n / 10);
    stats.labels.forEach((d, i) => { if (i % every === 0) mk("text", { x: P.l + step * i + step / 2, y: H - 8, "text-anchor": "middle", fill: "var(--muted)", "font-size": 11 }, d.slice(5).split("-").reverse().join("/")); });
    keys.forEach((k, ki) => {
      const color = PALETTE[ki % PALETTE.length];
      if (style === "line") {
        const pts = stats.series[k].map((v, i) => `${P.l + step * i + step / 2},${P.t + ih - (ih * v) / max}`).join(" ");
        mk("polyline", { points: pts, fill: "none", stroke: color, "stroke-width": 2.5, "stroke-linejoin": "round" });
      } else {
        const bw = Math.max(2, (step * 0.8) / keys.length);
        stats.series[k].forEach((v, i) => {
          const bh = (ih * v) / max;
          const r = mk("rect", { x: P.l + step * i + step * 0.1 + bw * ki, y: P.t + ih - bh, width: bw, height: Math.max(0, bh), rx: 2, fill: color });
          const t = document.createElementNS(ns, "title"); t.textContent = `${labels[k] || k} — ${stats.labels[i]} : ${v}`; r.append(t);
        });
      }
    });
    return h("div", { class: "chart" }, svg, h("div", { class: "legend" }, keys.map((k, ki) => h("span", {}, h("i", { style: { background: PALETTE[ki % PALETTE.length] } }), labels[k] || k))));
  }

  async function pageOverview(days) {
    const d = await api("GET", `${BASE}/overview?days=${days || 7}`);
    const page = h("div");
    const sel = h("select", { style: { width: "auto" } });
    for (const n of [7, 30, 90]) sel.append(h("option", { value: n, text: `${n} jours${n > d.max_days ? " ✦" : ""}`, disabled: n > d.max_days, selected: n === d.days }));
    sel.addEventListener("change", () => pageOverview(Number(sel.value)));
    page.append(head("Vue d'ensemble", G.name, sel));
    page.append(h("div", { class: "card row" },
      h("span", { class: "badge " + (d.bot.online ? "ok" : "danger"), text: d.bot.online ? "● Nexbot en ligne" : "● Nexbot hors ligne" }),
      h("span", { class: "muted small", text: d.bot.last_seen ? `Dernier signal : ${fmtDate(d.bot.last_seen)}` : "Le bot ne s'est encore jamais connecté à cette base de données." })));
    const c = d.counts;
    page.append(h("div", { class: "grid stats", style: { marginBottom: "18px" } },
      [[c.sanctions, `Sanctions (${d.days} j)`], [c.incidents_open, "Incidents non résolus"], [c.tickets_open, "Tickets ouverts"], [c.reports_open, "Signalements ouverts"], [c.automations, "Automatisations actives"]]
        .map(([v, l]) => h("div", { class: "stat" }, h("div", { class: "v", text: v }), h("div", { class: "l", text: l })))));
    const total = Object.values(d.stats.series).flat().reduce((a, b) => a + b, 0);
    page.append(h("div", { class: "card" }, h("h3", { text: "Activité" }),
      total ? chart(d.stats, d.metric_labels, d.chart_style) : h("div", { class: "empty", text: "Aucune donnée enregistrée sur cette période. Les statistiques apparaissent dès que Nexbot observe de l'activité." })));
    const names = { moderation: "Modération", antiraid: "Anti-raid", logs: "Logs", welcome: "Bienvenue", autoroles: "Rôles auto", tickets: "Tickets", suggestions: "Suggestions", reports: "Signalements" };
    page.append(h("div", { class: "card" }, h("h3", { text: "Modules" }), h("div", { class: "grid stats" },
      Object.entries(names).map(([k, label]) => h("a", { class: "stat", href: `/g/${G.id}/${k}`, "data-nav": k, style: { color: "inherit", textDecoration: "none" } },
        h("div", { style: { fontWeight: 700 }, text: label }), h("span", { class: "badge " + (d.modules[k] ? "ok" : "off"), text: d.modules[k] ? "Actif" : "Inactif" }))))));
    clear(app).append(page);
  }

  // ------------------------------------------------------------------ éditeur d'embeds & annonces
  async function pageEmbeds() {
    await loadDiscord();
    const msg = { content: "", embed: Object.assign(newEmbed(), { title: "Annonce", description: "Bienvenue sur **{server}** ! Nous sommes {member_count}." }) };
    const pv = h("div");
    const redraw = () => { clear(pv).append(preview(msg)); };
    const content = h("textarea", { maxlength: 2000, placeholder: "Texte au-dessus de l'embed (facultatif)" });
    content.addEventListener("input", () => { msg.content = content.value; redraw(); });
    const useEmbed = h("input", { type: "checkbox", checked: true });
    const edBox = h("div");
    const drawEditor = () => { clear(edBox); if (msg.embed) edBox.append(embedEditor(msg.embed, redraw, "announcement", false)); };
    useEmbed.addEventListener("change", () => { msg.embed = useEmbed.checked ? newEmbed() : null; if (msg.embed) msg.embed.title = "Titre"; drawEditor(); redraw(); });
    drawEditor(); redraw();

    const chan = idSelect(textChannels(), "", false, "#", "Choisir un salon…");
    const out = h("span", { class: "muted small" });
    const sendBtn = h("button", { class: "btn primary", type: "button", text: "Envoyer maintenant", disabled: !G.premium });
    sendBtn.addEventListener("click", async () => {
      if (!chan.value) return toast("Choisissez un salon", "error");
      sendBtn.disabled = true;
      try { const r = await api("POST", `${BASE}/messages/send`, { channel_id: chan.value, message: msg }); await followJob(r.job_id, out); }
      catch (e) { toast(e.message, "error"); }
      sendBtn.disabled = false;
    });

    // Annonces programmées
    const schedName = h("input", { type: "text", placeholder: "Nom de l'annonce", maxlength: 80 });
    const interval = h("select", {}, [[0, "Une seule fois"], [60, "Toutes les heures"], [360, "Toutes les 6 heures"], [1440, "Tous les jours"], [10080, "Toutes les semaines"]].map(([v, l]) => h("option", { value: v, text: l })));
    const startIn = h("input", { type: "number", min: 1, value: 5 });
    const schedBtn = h("button", { class: "btn", type: "button", text: "Programmer", disabled: !G.premium });
    const schedList = h("div");
    const loadSched = async () => {
      const d = await api("GET", `${BASE}/scheduled`);
      clear(schedList);
      if (!d.items.length) return schedList.append(h("div", { class: "empty", text: "Aucune annonce programmée." }));
      const byId = Object.fromEntries(textChannels().map((c) => [c.id, c.name]));
      for (const it of d.items) {
        schedList.append(h("div", { class: "auto-item" },
          h("div", { class: "desc" }, h("b", { text: it.name }), h("div", { class: "muted small", text: `#${byId[it.channel_id] || it.channel_id} · ${it.interval_minutes ? "toutes les " + it.interval_minutes + " min" : "une fois"} · prochain envoi : ${it.enabled ? fmtDate(it.next_run_at) : "en pause"}` })),
          h("button", { class: "btn sm", type: "button", text: it.enabled ? "Pause" : "Reprendre", onclick: async () => { try { await api("POST", `${BASE}/scheduled/${it.id}/toggle`); loadSched(); } catch (e) { toast(e.message, "error"); } } }),
          h("button", { class: "btn sm danger", type: "button", text: "Supprimer", onclick: async () => { if (!confirm("Supprimer cette annonce ?")) return; try { await api("DELETE", `${BASE}/scheduled/${it.id}`); loadSched(); } catch (e) { toast(e.message, "error"); } } })));
      }
    };
    schedBtn.addEventListener("click", async () => {
      if (!chan.value) return toast("Choisissez un salon", "error");
      try {
        await api("POST", `${BASE}/scheduled`, { name: schedName.value || "Annonce", channel_id: chan.value, message: msg, interval_minutes: Number(interval.value), start_in_minutes: Number(startIn.value) || 1 });
        toast("Annonce programmée"); loadSched();
      } catch (e) { toast(e.message, "error"); }
    });

    const page = h("div", {}, head("Éditeur d'embeds", "Composez un message, prévisualisez-le tel qu'il apparaîtra sur Discord, puis envoyez-le ou programmez-le."));
    if (!G.premium) page.append(upgradeNotice("Essayez l'éditeur librement : l'envoi et la programmation sont réservés au Premium. "));
    page.append(h("div", { class: "grid cols-2", style: { alignItems: "start" } },
      h("div", { class: "card stack" }, h("h3", { text: "Message" }), content, varButtons("announcement", () => content), h("label", { class: "row" }, h("span", { class: "switch" }, useEmbed, h("span")), "Embed"), edBox),
      h("div", { style: { position: "sticky", top: "80px" } }, h("div", { class: "card" }, h("h3", { text: "Aperçu Discord" }), pv),
        h("div", { class: "card stack" }, h("h3", {}, "Envoyer", premiumBadge()), chan, h("div", { class: "row" }, sendBtn), out,
          h("hr", { class: "sep" }), h("h3", {}, "Programmer", premiumBadge()), schedName,
          h("div", { class: "row" }, h("div", { class: "grow" }, h("label", { class: "small muted", text: "Fréquence" }), interval), h("div", { style: { width: "130px" } }, h("label", { class: "small muted", text: "Départ dans (min)" }), startIn)),
          schedBtn))));
    page.append(h("div", { class: "card" }, h("h3", { text: "Annonces programmées" }), schedList));
    clear(app).append(page);
    loadSched().catch((e) => toast(e.message, "error"));
  }

  // ------------------------------------------------------------------ automatisations
  async function pageAutomations() {
    await loadDiscord();
    const data = await api("GET", `${BASE}/automations`);
    const cat = data.catalog, lim = data.limits;
    const page = h("div", {}, head("Automatisations", "Déclencheur → conditions → actions. Aucun code n'est exécuté : seules des actions prévues par Nexbot."));
    page.append(h("div", { class: "notice info" }, `Votre offre : ${lim.automations} automatisations, ${lim.automation_actions} action(s) chacune. Délai entre deux exécutions : 5 secondes minimum.`));
    const listCard = h("div", { class: "card" }, h("h3", { text: `Vos automatisations (${data.items.length}/${lim.automations})` }));
    const chanName = Object.fromEntries(textChannels().map((c) => [c.id, "#" + c.name]));
    const roleName = Object.fromEntries(roles().map((r) => [r.id, "@" + r.name]));
    const describe = (def) => {
      let t = cat.triggers[def.trigger.type];
      if (def.trigger.type === "message_contains") t += ` (${def.trigger.keywords.join(", ")})`;
      if (def.trigger.type === "warn_count") t += ` (${def.trigger.count})`;
      const acts = def.actions.map((a) => cat.actions[a.type] + (a.role_id ? " " + (roleName[a.role_id] || a.role_id) : "") + (a.channel_id ? " " + (chanName[a.channel_id] || a.channel_id) : "") + (a.delay_seconds ? ` après ${a.delay_seconds}s` : ""));
      return `${t} → ${acts.join(", ")}`;
    };
    if (!data.items.length) listCard.append(h("div", { class: "empty", text: "Aucune automatisation. Créez-en une ci-dessous." }));
    for (const a of data.items) {
      const tog = h("input", { type: "checkbox", checked: a.enabled });
      tog.addEventListener("change", async () => {
        try { await api("PUT", `${BASE}/automations/${a.id}`, { name: a.name, enabled: tog.checked, definition: a.definition }); toast(tog.checked ? "Activée" : "Désactivée"); }
        catch (e) { tog.checked = !tog.checked; toast(e.message, "error"); }
      });
      const out = h("div", { class: "muted small" });
      listCard.append(h("div", { class: "auto-item" }, h("label", { class: "switch" }, tog, h("span")),
        h("div", { class: "desc" }, h("b", { text: a.name }), h("div", { class: "muted small", text: describe(a.definition) }),
          h("div", { class: "muted small", text: `Exécutions : ${a.run_count}${a.last_run_at ? " · dernière : " + fmtDate(a.last_run_at) : ""}` }), out),
        h("button", { class: "btn sm", type: "button", text: "Tester", title: "Simulation : Nexbot indique ce qu'il ferait, sans agir", onclick: async () => {
          try { const r = await api("POST", `${BASE}/automations/${a.id}/test`); await followJob(r.job_id, out); } catch (e) { toast(e.message, "error"); } } }),
        h("button", { class: "btn sm", type: "button", text: "Modifier", onclick: () => { builderHost.replaceChildren(builder(a)); builderHost.scrollIntoView({ behavior: "smooth" }); } }),
        h("button", { class: "btn sm danger", type: "button", text: "Supprimer", onclick: async () => {
          if (!confirm(`Supprimer « ${a.name} » ?`)) return;
          try { await api("DELETE", `${BASE}/automations/${a.id}`); toast("Supprimée"); route("automations", false); } catch (e) { toast(e.message, "error"); } } })));
    }
    page.append(listCard);
    const builderHost = h("div");
    page.append(builderHost);
    builderHost.append(builder(null));
    clear(app).append(page);

    function builder(existing) {
      const st = existing ? clone(existing) : { name: "", enabled: true, definition: { trigger: { type: "member_join", keywords: [], count: 3 }, conditions: [], actions: [{ type: "send_message", channel_id: "", role_id: "", text: "Bienvenue {user.mention} !", minutes: 10, delay_seconds: 0 }], cooldown_seconds: 10 } };
      const def = st.definition;
      const card = h("div", { class: "card stack" });
      const draw = () => {
        clear(card);
        card.append(h("h3", { text: existing ? `Modifier « ${existing.name} »` : "Nouvelle automatisation" }));
        const name = h("input", { type: "text", value: st.name, placeholder: "Nom (ex. Accueil des nouveaux)", maxlength: 80 });
        name.addEventListener("input", () => { st.name = name.value; });
        card.append(name);

        // Déclencheur
        const tsel = h("select", {}, Object.entries(cat.triggers).map(([k, l]) => h("option", { value: k, text: l, selected: k === def.trigger.type })));
        tsel.addEventListener("change", () => { def.trigger.type = tsel.value; draw(); });
        const tb = h("div", { class: "builder-block" }, h("b", { text: "Quand…" }), h("div", { class: "row" }, h("div", { class: "grow" }, tsel)));
        if (def.trigger.type === "message_contains") {
          const kw = h("input", { type: "text", value: def.trigger.keywords.join(", "), placeholder: "Mots-clés séparés par des virgules" });
          kw.addEventListener("input", () => { def.trigger.keywords = kw.value.split(",").map((x) => x.trim()).filter(Boolean); });
          tb.append(kw);
        }
        if (def.trigger.type === "warn_count") {
          const cnt = h("input", { type: "number", min: 1, max: 50, value: def.trigger.count || 3 });
          cnt.addEventListener("input", () => { def.trigger.count = Number(cnt.value); });
          tb.append(h("div", { class: "row" }, "Nombre d'avertissements :", cnt));
        }
        card.append(tb);

        // Conditions
        const cb = h("div", { class: "builder-block" }, h("b", { text: "Si… (facultatif)" }));
        def.conditions.forEach((c, i) => {
          const sel = h("select", {}, Object.entries(cat.conditions).map(([k, l]) => h("option", { value: k, text: l, selected: k === c.type })));
          sel.addEventListener("change", () => { c.type = sel.value; draw(); });
          const row = h("div", { class: "row" }, h("div", { class: "grow" }, sel));
          if (c.type === "account_age_below") { const n = h("input", { type: "number", min: 1, value: c.days || 7, style: { width: "100px" } }); c.days = c.days || 7; n.addEventListener("input", () => { c.days = Number(n.value); }); row.append(n, "jours"); }
          if (c.type === "has_role" || c.type === "lacks_role") { const r = idSelect(roles(), c.role_id, false, "@"); r.addEventListener("change", () => { c.role_id = r.value; }); row.append(h("div", { class: "grow" }, r)); }
          if (c.type === "in_channel") { const r = idSelect(textChannels(), c.channel_id, false, "#"); r.addEventListener("change", () => { c.channel_id = r.value; }); row.append(h("div", { class: "grow" }, r)); }
          row.append(h("button", { class: "btn sm danger", type: "button", text: "×", onclick: () => { def.conditions.splice(i, 1); draw(); } }));
          cb.append(row);
        });
        cb.append(h("button", { class: "btn sm", type: "button", text: "+ Condition", onclick: () => { if (def.conditions.length >= 5) return toast("5 conditions maximum", "error"); def.conditions.push({ type: "is_not_bot", days: 0, role_id: "", channel_id: "" }); draw(); } }));
        card.append(cb);

        // Actions
        const ab = h("div", { class: "builder-block" }, h("b", { text: "Alors…" }));
        def.actions.forEach((a, i) => {
          const sel = h("select", {}, Object.entries(cat.actions).map(([k, l]) => h("option", { value: k, text: l, selected: k === a.type })));
          sel.addEventListener("change", () => { a.type = sel.value; draw(); });
          const box = h("div", { class: "stack", style: { borderTop: i ? "1px solid var(--border)" : "0", paddingTop: i ? "10px" : "0" } }, h("div", { class: "row" }, h("div", { class: "grow" }, sel),
            h("button", { class: "btn sm danger", type: "button", text: "×", onclick: () => { if (def.actions.length === 1) return toast("Au moins une action", "error"); def.actions.splice(i, 1); draw(); } })));
          if (a.type === "send_message") { const r = idSelect(textChannels(), a.channel_id, false, "#", "Salon…"); r.addEventListener("change", () => { a.channel_id = r.value; }); box.append(r); }
          if (a.type === "add_role" || a.type === "remove_role") { const r = idSelect(roles(), a.role_id, false, "@", "Rôle…"); r.addEventListener("change", () => { a.role_id = r.value; }); box.append(r); }
          if (["send_message", "dm_member", "alert"].includes(a.type)) {
            const ta = h("textarea", { maxlength: 2000 }); ta.value = a.text || ""; ta.addEventListener("input", () => { a.text = ta.value; });
            box.append(ta, varButtons(a.type === "alert" ? "generic" : "welcome", () => ta));
          }
          if (a.type === "timeout") { const n = h("input", { type: "number", min: 1, max: 40320, value: a.minutes || 10, style: { width: "120px" } }); a.minutes = a.minutes || 10; n.addEventListener("input", () => { a.minutes = Number(n.value); }); box.append(h("div", { class: "row" }, "Durée :", n, "minutes")); }
          const delay = h("input", { type: "number", min: 0, max: 86400, value: a.delay_seconds || 0, style: { width: "120px" } });
          delay.addEventListener("input", () => { a.delay_seconds = Number(delay.value); });
          box.append(h("div", { class: "row small muted" }, "Délai avant l'action :", delay, "secondes"));
          ab.append(box);
        });
        ab.append(h("button", { class: "btn sm", type: "button", text: `+ Action (${def.actions.length}/${lim.automation_actions})`, onclick: () => {
          if (def.actions.length >= lim.automation_actions) return toast(`Votre offre autorise ${lim.automation_actions} action(s)`, "error");
          def.actions.push({ type: "alert", channel_id: "", role_id: "", text: "", minutes: 10, delay_seconds: 0 }); draw(); } }));
        card.append(ab);

        const cd = h("input", { type: "number", min: 5, max: 86400, value: def.cooldown_seconds, style: { width: "120px" } });
        cd.addEventListener("input", () => { def.cooldown_seconds = Number(cd.value); });
        card.append(h("div", { class: "row" }, "Délai minimum entre deux exécutions :", cd, "secondes"));
        const save = h("button", { class: "btn primary", type: "button", text: existing ? "Enregistrer" : "Créer l'automatisation" });
        save.addEventListener("click", async () => {
          save.disabled = true;
          try {
            if (existing) await api("PUT", `${BASE}/automations/${existing.id}`, st); else await api("POST", `${BASE}/automations`, st);
            toast("Automatisation enregistrée"); route("automations", false);
          } catch (e) { toast(e.message, "error"); save.disabled = false; }
        });
        card.append(h("div", { class: "row" }, save, existing ? h("button", { class: "btn ghost", type: "button", text: "Annuler", onclick: () => builderHost.replaceChildren(builder(null)) }) : null));
      };
      draw();
      return card;
    }
  }

  // ------------------------------------------------------------------ sanctions & notes
  async function pageSanctions(page = 1) {
    const userInput = h("input", { type: "text", placeholder: "ID du membre", value: pageSanctions.user || "" });
    const typeSel = h("select", {}, [["", "Tous les types"], ["warn", "Avertissement"], ["timeout", "Timeout"], ["kick", "Expulsion"], ["ban", "Bannissement"], ["tempban", "Ban temporaire"], ["unban", "Débannissement"], ["untimeout", "Fin de timeout"]]
      .map(([v, l]) => h("option", { value: v, text: l, selected: v === (pageSanctions.type || "") })));
    const q = new URLSearchParams({ page, user_id: pageSanctions.user || "", type: pageSanctions.type || "" });
    const d = await api("GET", `${BASE}/sanctions?${q}`);
    const apply = () => { pageSanctions.user = userInput.value.trim(); pageSanctions.type = typeSel.value; pageSanctions(1); };
    userInput.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
    typeSel.addEventListener("change", apply);
    const LBL = { warn: ["Avertissement", "warn"], timeout: ["Timeout", "warn"], kick: ["Expulsion", "danger"], ban: ["Ban", "danger"], tempban: ["Ban temporaire", "danger"], unban: ["Débannissement", "ok"], untimeout: ["Fin de timeout", "ok"] };
    const pageEl = h("div", {}, head("Sanctions", `Historique affiché : ${d.history_days >= 3650 ? "illimité" : d.history_days + " derniers jours"}.`));
    pageEl.append(h("div", { class: "card row" }, h("div", { class: "grow" }, userInput), h("div", { class: "grow" }, typeSel), h("button", { class: "btn", type: "button", text: "Filtrer", onclick: apply })));
    const card = h("div", { class: "card" });
    if (!d.items.length) card.append(h("div", { class: "empty", text: "Aucune sanction." }));
    else {
      const t = h("table", {}, h("thead", {}, h("tr", {}, ["#", "Type", "Membre", "Modérateur", "Raison", "Date", ""].map((x) => h("th", { text: x })))));
      t.append(h("tbody", {}, d.items.map((s) => h("tr", {},
        h("td", { text: s.id }), h("td", {}, h("span", { class: "badge " + (LBL[s.type] || ["", "off"])[1], text: (LBL[s.type] || [s.type])[0] }), s.automatic ? h("div", { class: "small muted", text: "automatique" }) : null),
        h("td", { class: "mono" }, h("a", { href: "#", text: s.user_id, onclick: (e) => { e.preventDefault(); notesUser.value = s.user_id; loadNotes(); } })),
        h("td", { class: "mono", text: s.moderator_id }), h("td", { text: s.reason || "—" }),
        h("td", {}, fmtDate(s.created_at), s.expires_at ? h("div", { class: "small muted", text: "jusqu'au " + fmtDate(s.expires_at) }) : null),
        h("td", {}, s.type === "warn" && s.active ? h("button", { class: "btn sm", type: "button", text: "Retirer", onclick: async () => {
          if (!confirm("Retirer cet avertissement ?")) return;
          try { await api("POST", `${BASE}/sanctions/${s.id}/revoke`); toast("Avertissement retiré"); pageSanctions(page); } catch (e) { toast(e.message, "error"); } } })
          : (!s.active ? h("span", { class: "small muted", text: "retiré" }) : null))))));
      card.append(h("div", { class: "table-wrap" }, t));
      const pages = Math.max(1, Math.ceil(d.total / d.per_page));
      card.append(h("div", { class: "pager" }, h("span", { class: "muted small", text: `${d.total} sanction(s) · page ${page}/${pages}` }),
        h("button", { class: "btn sm", type: "button", text: "←", disabled: page <= 1, onclick: () => pageSanctions(page - 1) }),
        h("button", { class: "btn sm", type: "button", text: "→", disabled: page >= pages, onclick: () => pageSanctions(page + 1) })));
    }
    pageEl.append(card);

    // Notes internes
    const notesUser = h("input", { type: "text", placeholder: "ID du membre" });
    const notesBox = h("div");
    const noteText = h("textarea", { placeholder: "Note visible uniquement par le staff", maxlength: 1500 });
    const loadNotes = async () => {
      clear(notesBox);
      if (!/^\d{15,21}$/.test(notesUser.value.trim())) return notesBox.append(h("div", { class: "muted small", text: "Saisissez un identifiant de membre valide." }));
      const r = await api("GET", `${BASE}/notes?user_id=${notesUser.value.trim()}`);
      if (!r.items.length) notesBox.append(h("div", { class: "muted small", text: "Aucune note pour ce membre." }));
      for (const n of r.items) notesBox.append(h("div", { class: "auto-item" }, h("div", { class: "desc" }, n.content, h("div", { class: "small muted", text: `par ${n.author_id} · ${fmtDate(n.created_at)}` }))));
    };
    notesUser.addEventListener("change", () => loadNotes().catch((e) => toast(e.message, "error")));
    pageEl.append(h("div", { class: "card stack" }, h("h3", { text: "Notes internes" }), h("div", { class: "row" }, h("div", { class: "grow" }, notesUser), h("button", { class: "btn", type: "button", text: "Afficher", onclick: () => loadNotes().catch((e) => toast(e.message, "error")) })),
      notesBox, noteText, h("button", { class: "btn primary", type: "button", text: "Ajouter la note", onclick: async () => {
        try { await api("POST", `${BASE}/notes`, { user_id: notesUser.value.trim(), content: noteText.value }); noteText.value = ""; toast("Note ajoutée"); loadNotes(); } catch (e) { toast(e.message, "error"); } } })));
    clear(app).append(pageEl);
  }

  // ------------------------------------------------------------------ incidents
  async function pageIncidents() {
    const d = await api("GET", `${BASE}/incidents`);
    const KIND = { join_raid: "Arrivée massive", young_account: "Compte récent", bot_added: "Bot non autorisé", channel_delete: "Suppressions de salons", channel_create: "Créations de salons",
      role_delete: "Suppressions de rôles", role_create: "Créations de rôles", mass_ban: "Bannissements en masse", webhook_create: "Webhooks", dangerous_permission: "Permission dangereuse", mention_raid: "Spam de mentions", spam: "Spam" };
    const SEV = { low: "off", medium: "warn", high: "danger", critical: "danger" };
    const page = h("div", {}, head("Incidents de sécurité", "Détections de l'anti-raid et de l'anti-nuke. Marquez-les comme résolus après vérification (faux positifs compris)."));
    const card = h("div", { class: "card" });
    if (!d.items.length) card.append(h("div", { class: "empty", text: "Aucun incident. 🎉" }));
    for (const i of d.items) {
      const det = Object.entries(i.details || {}).slice(0, 6).map(([k, v]) => `${k} : ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
      card.append(h("div", { class: "auto-item" },
        h("span", { class: "badge " + (SEV[i.severity] || "off"), text: i.severity.toUpperCase() }),
        h("div", { class: "desc" }, h("b", { text: KIND[i.kind] || i.kind }), h("div", { class: "small muted", text: `${fmtDate(i.created_at)} · auteur : ${i.actor_id || "—"} · réaction : ${i.action_taken}` }), det ? h("div", { class: "small mono muted", text: det }) : null),
        i.resolved ? h("span", { class: "badge ok", text: "Résolu" }) : h("button", { class: "btn sm", type: "button", text: "Marquer résolu", onclick: async () => {
          try { await api("POST", `${BASE}/incidents/${i.id}/resolve`); route("incidents", false); } catch (e) { toast(e.message, "error"); } } })));
    }
    page.append(card);
    clear(app).append(page);
  }

  // ------------------------------------------------------------------ Premium
  async function pagePremium() {
    const d = await api("GET", `${BASE}/premium`);
    const params = new URLSearchParams(location.search);
    const page = h("div", {}, head("Premium", "Le Premium s'applique à ce serveur uniquement."));
    if (params.get("checkout") === "success") page.append(h("div", { class: "notice info", text: "Paiement reçu. L'activation est confirmée par Stripe en quelques secondes : rechargez la page si nécessaire." }));
    const sub = d.subscription;
    const STAT = { active: "Actif", trialing: "Essai", past_due: "Paiement en retard (période de grâce)", canceled: "Annulé", expired: "Expiré" };
    const status = h("div", { class: "card" }, h("h3", {}, "Statut ", d.premium ? premiumBadge() : h("span", { class: "badge off", text: "Gratuit" })));
    if (sub) {
      status.append(h("p", {}, `Abonnement : ${STAT[sub.status] || sub.status}${sub.source === "dev" ? " (mode développement)" : ""}`,
        sub.current_period_end ? h("br") : null, sub.current_period_end ? `${sub.cancel_at_period_end || !d.premium ? "Fin" : "Renouvellement"} : ${fmtDate(sub.current_period_end)}` : null));
      if (!d.premium) status.append(h("div", { class: "notice", text: "L'abonnement n'est plus actif. Vos personnalisations Premium sont conservées et redeviendront actives dès le réabonnement ; en attendant, les valeurs par défaut s'appliquent." }));
    } else status.append(h("p", { class: "muted", text: "Ce serveur utilise l'offre gratuite." }));
    const actions = h("div", { class: "row" });
    if (d.stripe_enabled) {
      if (!d.premium) actions.append(h("button", { class: "btn primary", type: "button", text: "Passer au Premium", onclick: async (ev) => {
        ev.target.disabled = true; try { const r = await api("POST", `${BASE}/premium/checkout`); window.location.href = r.url; } catch (e) { toast(e.message, "error"); ev.target.disabled = false; } } }));
      if (sub && sub.has_portal) actions.append(h("button", { class: "btn", type: "button", text: "Gérer l'abonnement", onclick: async () => {
        try { const r = await api("POST", `${BASE}/premium/portal`); window.location.href = r.url; } catch (e) { toast(e.message, "error"); } } }));
    } else if (!d.premium) {
      status.append(h("div", { class: "notice info" }, "Les paiements ouvrent bientôt. Suivez les annonces sur le ", h("a", { href: BOOT.support_url, target: "_blank", rel: "noopener", text: "serveur de support" }), "."));
    }
    status.append(actions);
    page.append(status);
    if (d.dev_premium) {
      page.append(h("div", { class: "card" }, h("h3", { text: "Mode développement (propriétaire de Nexbot)" }),
        h("p", { class: "muted small", text: "Visible uniquement par le propriétaire, et désactivé automatiquement en production (NEXBOT_ENV=production)." }),
        h("div", { class: "row" },
          h("button", { class: "btn", type: "button", text: "Activer 30 jours de Premium de test", onclick: async () => { try { await api("POST", `${BASE}/premium/dev`, { action: "grant", days: 30 }); location.reload(); } catch (e) { toast(e.message, "error"); } } }),
          h("button", { class: "btn danger", type: "button", text: "Simuler l'expiration", onclick: async () => { try { await api("POST", `${BASE}/premium/dev`, { action: "revoke" }); location.reload(); } catch (e) { toast(e.message, "error"); } } }))));
    }
    const t = h("table", {}, h("thead", {}, h("tr", {}, ["Fonctionnalité", "Gratuit", "Premium"].map((x) => h("th", { text: x })))),
      h("tbody", {}, d.features.map(([n, f, p]) => h("tr", {}, h("td", { text: n }), [f, p].map((v) => h("td", {}, v === true ? h("span", { class: "yes", text: "✓" }) : v === false ? h("span", { class: "no", text: "—" }) : v))))));
    page.append(h("div", { class: "card table-wrap" }, h("h3", { text: "Comparatif" }), t));
    clear(app).append(page);
  }

  function pageHelp() {
    clear(app).append(h("div", {}, head("Aide", "Tout ce qu'il faut pour bien démarrer."),
      h("div", { class: "grid cols-2" },
        h("div", { class: "card stack" }, h("h3", { text: "Bien démarrer" }),
          h("p", {}, "1. Placez le rôle de Nexbot ", h("b", { text: "au-dessus" }), " des rôles qu'il doit gérer."),
          h("p", {}, "2. Configurez les ", h("a", { href: `/g/${G.id}/logs`, "data-nav": "logs", text: "logs" }), " et l'", h("a", { href: `/g/${G.id}/antiraid`, "data-nav": "antiraid", text: "anti-raid" }), " (salon d'alertes, rôle de quarantaine)."),
          h("p", {}, "3. Ajustez la ", h("a", { href: `/g/${G.id}/moderation`, "data-nav": "moderation", text: "modération automatique" }), " puis les modules communautaires."),
          h("p", { class: "muted small", text: "Les modifications sont appliquées par Nexbot sans redémarrage, en quelques secondes." })),
        h("div", { class: "card stack" }, h("h3", { text: "Ressources" }),
          h("a", { class: "btn", href: "/help", text: "Liste des commandes et FAQ" }),
          h("a", { class: "btn primary", href: BOOT.support_url, target: "_blank", rel: "noopener", text: "Serveur de support" })))));
  }

  route(BOOT.section, false);
})();

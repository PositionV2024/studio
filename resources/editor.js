(() => {
  if (window.__hoyoProjectStudioInstalled) return;
  window.__hoyoProjectStudioInstalled = true;

  const SELECTED_CLASS = "hps-editor-selected";
  const HOVER_CLASS = "hps-editor-hover";
  const MESSAGE_PREFIX = "hps-studio";
  const documentToken = window.__hpsPreviewDocumentToken || "";
  const ignoredTags = new Set([
    "html", "body", "head", "script", "style", "link", "meta", "noscript"
  ]);

  let editMode = false;
  let selected = null;
  let hovered = null;
  let bridge = null;
  let bridgeConnecting = false;
  const bridgeOutbox = [];
  const knownChildren = new Set();
  const childFlushes = new Map();
  const selectionRoutes = new Map();
  let pendingLocalSelection = null;
  let selectionGeneration = 0;
  let serial = 0;
  let inlineEditor = null;
  let currentSettings = {};
  let mutationTimer = null;
  // Entries exist only for actual DOM fields Studio has changed. Keeping the
  // baseline per field lets Undo remove an override without resetting Streamlit
  // or replacing an element that owns live widget state/listeners.
  const editedElements = new Map();
  const styleProperties = {
    font_size: ["font-size"], width: ["width"], height: ["height"],
    padding: ["padding-top", "padding-right", "padding-bottom", "padding-left"],
    margin: ["margin-top", "margin-right", "margin-bottom", "margin-left"],
    color: ["color"], background_color: ["background-color"],
    border_color: ["border-top-color", "border-right-color", "border-bottom-color", "border-left-color"],
    border_width: ["border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
      "border-top-style", "border-right-style", "border-bottom-style", "border-left-style"],
    border_radius: ["border-top-left-radius", "border-top-right-radius", "border-bottom-right-radius", "border-bottom-left-radius"],
    opacity: ["opacity"], visible: ["display"]
  };

  const style = document.createElement("style");
  style.id = "hps-editor-style";
  style.textContent = `
    .${HOVER_CLASS}{
      outline:2px dashed #72b7ff!important;
      outline-offset:3px!important;
      cursor:crosshair!important;
    }
    .${SELECTED_CLASS}{
      outline:3px solid #ff6bb5!important;
      outline-offset:4px!important;
      box-shadow:0 0 0 5px rgba(255,107,181,.16),0 0 34px rgba(255,107,181,.30)!important;
    }
    html.hps-editing *{cursor:crosshair!important}
    html.hps-editing input,html.hps-editing textarea{caret-color:transparent!important}
    html.hps-editing [data-hps-ignore='true']{cursor:auto!important}
  `;
  document.documentElement.appendChild(style);

  function frameKey() {
    if (window === window.top) return "top";
    try {
      const frame = window.frameElement;
      if (!frame) return `frame:${location.pathname}`;
      const title = frame.getAttribute("title") || frame.id || frame.name || "iframe";
      const siblings = frame.parentElement
        ? Array.from(frame.parentElement.querySelectorAll(":scope > iframe"))
        : [];
      const index = Math.max(0, siblings.indexOf(frame));
      return `${title}:${index}`;
    } catch (_error) {
      return `frame:${location.pathname}:${location.hash}`;
    }
  }

  function attrValue(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function selectorFor(element) {
    if (!element || element.nodeType !== Node.ELEMENT_NODE) return "";
    if (element.id && document.querySelectorAll(`#${CSS.escape(element.id)}`).length === 1) {
      return `#${CSS.escape(element.id)}`;
    }

    const parts = [];
    let current = element;
    let depth = 0;
    while (current && current !== document.body && depth < 9) {
      let part = current.tagName.toLowerCase();
      const preferred = ["data-testid", "aria-label", "name", "role"];
      for (const attribute of preferred) {
        const value = current.getAttribute && current.getAttribute(attribute);
        if (value) {
          part += `[${attribute}="${attrValue(value)}"]`;
          break;
        }
      }

      const parent = current.parentElement;
      if (parent) {
        const sameTag = Array.from(parent.children).filter(
          sibling => sibling.tagName === current.tagName
        );
        if (sameTag.length > 1) {
          part += `:nth-of-type(${sameTag.indexOf(current) + 1})`;
        }
      }

      parts.unshift(part);
      const candidate = parts.join(" > ");
      try {
        if (document.querySelectorAll(candidate).length === 1) return candidate;
      } catch (_error) {
        // Continue building a longer selector.
      }
      current = parent;
      depth += 1;
    }
    return parts.join(" > ");
  }

  function generatedId(element) {
    const declared = element.dataset && element.dataset.editorId;
    if (declared) return declared;
    const selector = selectorFor(element);
    return `css|${encodeURIComponent(frameKey())}|${encodeURIComponent(selector)}`;
  }

  function isSelectable(element) {
    if (!(element instanceof Element)) return false;
    const tag = element.tagName.toLowerCase();
    if (ignoredTags.has(tag)) return false;
    if (element.id === "hps-editor-style") return false;
    if (element.closest && element.closest("[data-hps-ignore='true']")) return false;
    const rect = element.getBoundingClientRect();
    return rect.width >= 4 && rect.height >= 4;
  }

  function editableTarget(event) {
    const path = typeof event.composedPath === "function" ? event.composedPath() : [];
    for (const item of path) {
      if (item instanceof Element && isSelectable(item)) return item;
    }
    let raw = event.target;
    if (raw && raw.nodeType === Node.TEXT_NODE) raw = raw.parentElement;
    while (raw && !isSelectable(raw)) raw = raw.parentElement;
    return raw || null;
  }

  function inferType(element) {
    const explicit = element.dataset && element.dataset.editorType;
    if (explicit) return explicit;
    const tag = element.tagName.toLowerCase();
    if (tag === "img" || tag === "picture") return "image";
    if (["input", "textarea", "select"].includes(tag)) return "input";
    if (["button", "a"].includes(tag)) return "button";
    const textTags = new Set([
      "h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "label", "strong", "small", "li", "code"
    ]);
    if (textTags.has(tag) || element.children.length === 0) return "text";
    return "container";
  }

  function labelFor(element) {
    return (
      element.getAttribute("aria-label") ||
      element.getAttribute("title") ||
      element.getAttribute("placeholder") ||
      element.getAttribute("data-testid") ||
      element.dataset?.editorId ||
      element.textContent?.trim().replace(/\s+/g, " ").slice(0, 70) ||
      element.tagName.toLowerCase()
    );
  }

  // A property is offered only when applying it can preserve the live widget.
  // In particular, textContent on a button/container would remove its child
  // icons and controls, and changing input values can trigger Streamlit actions.
  function textTargetFor(element) {
    const tag = element.tagName.toLowerCase();
    if (["input", "textarea", "select", "option", "img", "picture", "svg", "canvas",
         "video", "audio", "iframe", "object", "embed", "br", "hr", "progress", "meter"].includes(tag)) return null;
    if (element.namespaceURI !== "http://www.w3.org/1999/xhtml") return null;
    if (!["text", "button"].includes(inferType(element))) return null;
    const directText = Array.from(element.childNodes).filter(node => node.nodeType === Node.TEXT_NODE);
    const meaningfulText = directText.filter(node => node.nodeValue.trim());
    const previousTarget = fieldState(element, "text")?.target;
    const emptyTarget = previousTarget?.parentNode === element ? previousTarget : directText[0];
    if (element.children.length === 0) {
      if (meaningfulText.length > 1) return null;
      return meaningfulText[0] || emptyTarget || element;
    }
    if (!directText.length || meaningfulText.length > 1) return null;
    const meaningfulChildren = Array.from(element.children).some(child => {
      if (child.matches("svg, [aria-hidden='true']")) return false;
      return Boolean(child.textContent.trim()) || child.matches(
        "input, textarea, select, img, picture, video, audio, canvas, iframe, object, embed"
      ) || Boolean(child.querySelector("input, textarea, select"));
    });
    // Keep an emptied label editable, including a heading with an anchor icon.
    return meaningfulChildren ? null : (meaningfulText[0] || emptyTarget);
  }

  function capabilitiesFor(element, computed = getComputedStyle(element)) {
    const tag = element.tagName.toLowerCase();
    const fields = new Set([
      "text", "source", "font_size", "width", "height", "padding", "margin",
      "color", "background_color", "border_color", "border_width", "border_radius",
      "opacity", "visible"
    ]);
    const reasons = {};
    const unavailable = (key, reason) => {
      fields.delete(key);
      reasons[key] = reason;
    };
    if (!textTargetFor(element)) {
      unavailable("text", ["input", "textarea", "select", "option"].includes(tag)
        ? "This is a live input value. Use the preview with Edit Mode off to change it."
        : "This element has no directly editable text. Select its text label instead.");
    }
    if (tag !== "img") {
      unavailable("source", "Image source is available only for an image element. Select the image itself.");
    } else if (element.srcset || element.closest("picture")?.querySelector("source[srcset]")) {
      unavailable("source", "This image uses responsive sources. Change its sources in the project code.");
    }
    const nonText = ["img", "picture", "svg", "canvas", "video", "audio", "iframe", "object", "embed"].includes(tag)
      || element.namespaceURI !== "http://www.w3.org/1999/xhtml";
    if (nonText) {
      unavailable("font_size", "This element does not display editable text.");
      unavailable("color", "This element does not display editable text.");
    }
    if (computed.display === "inline" && !nonText && !["input", "textarea", "select"].includes(tag)) {
      for (const key of ["width", "height"]) {
        unavailable(key, "Width and height do not apply to inline text. Select the surrounding block.");
      }
    }
    return {supported_fields: Array.from(fields), field_reasons: reasons};
  }

  function describe(element) {
    const computed = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    const type = inferType(element);
    const tag = element.tagName.toLowerCase();
    const textTarget = textTargetFor(element);
    let text = textTarget ? textTarget.textContent : (element.textContent || "").trim().slice(0, 1000);
    if (type === "input") text = element.value || element.getAttribute("placeholder") || "";

    return {
      id: generatedId(element),
      label: labelFor(element),
      type,
      tag,
      text,
      source: tag === "img" ? (element.currentSrc || element.src || "") : "",
      ...capabilitiesFor(element, computed),
      computed: {
        font_size: parseFloat(computed.fontSize) || 16,
        color: computed.color,
        background_color: computed.backgroundColor,
        border_color: computed.borderTopColor,
        border_width: parseFloat(computed.borderTopWidth) || 0,
        border_radius: parseFloat(computed.borderRadius) || 0,
        padding: parseFloat(computed.paddingTop) || 0,
        margin: parseFloat(computed.marginTop) || 0,
        width: Math.round(rect.width) || 0,
        height: Math.round(rect.height) || 0,
        opacity: Number.isFinite(parseFloat(computed.opacity)) ? parseFloat(computed.opacity) : 1,
        visible: computed.display !== "none" && computed.visibility !== "hidden"
      }
    };
  }

  function connectBridge() {
    if (window !== window.top || bridge || bridgeConnecting) return;
    if (!window.qt || !qt.webChannelTransport || typeof QWebChannel !== "function") return;
    bridgeConnecting = true;
    try {
      // PreviewView injects Qt's bundled client before this editor script.
      new QWebChannel(qt.webChannelTransport, channel => {
        bridgeConnecting = false;
        bridge = channel.objects.studioBridge;
        if (bridge) {
          bridge.log(`Visual editor bridge connected (${frameKey()})\n`);
          for (const [method, payload] of bridgeOutbox.splice(0)) {
            bridge[method](JSON.stringify(payload));
          }
        }
      });
    } catch (_error) {
      bridgeConnecting = false;
    }
  }

  function sendBridge(method, payload) {
    const data = {...payload, document_token: documentToken};
    if (bridge) bridge[method](JSON.stringify(data));
    else bridgeOutbox.push([method, data]);
  }

  function post(target, type, data = {}) {
    try {
      target.postMessage({source: MESSAGE_PREFIX, document_token: documentToken, type, ...data}, "*");
    } catch (_error) { /* A frame may close between selection and delivery. */ }
  }

  function emitSelection(payload) {
    if (window === window.top) sendBridge("selectElement", payload);
    else post(window.parent, "selection", {payload});
  }

  function emitInline(payload) {
    if (payload.phase === "update") {
      currentSettings = {
        ...currentSettings,
        [payload.id]: {...(currentSettings[payload.id] || {}), text: payload.text},
      };
    }
    if (window === window.top) sendBridge("inlineEdit", payload);
    else post(window.parent, "inline", {payload});
  }

  function emitLog(message) {
    if (window === window.top) {
      if (bridge) bridge.log(String(message));
    } else {
      post(window.parent, "log", {payload: String(message)});
    }
  }

  function clearHover() {
    if (hovered) hovered.classList.remove(HOVER_CLASS);
    hovered = null;
  }

  function clearSelection() {
    finishInlineEdit(false);
    if (selected) selected.classList.remove(SELECTED_CLASS);
    selected = null;
    pendingLocalSelection = null;
  }

  function setLocalEditMode(enabled) {
    editMode = Boolean(enabled);
    document.documentElement.classList.toggle("hps-editing", editMode);
    if (!editMode) {
      clearHover();
      clearSelection();
    }
  }

  function activateSelection(request) {
    const pending = pendingLocalSelection;
    if (!pending || pending.request !== request || !editMode || !pending.target.isConnected) return;
    const target = pending.target;
    clearHover();
    clearSelection();
    selected = target;
    selected.classList.add(SELECTED_CLASS);
    selected.scrollIntoView({block: "nearest", inline: "nearest"});
    emitSelection(describe(selected));
    beginInlineEdit(selected);
  }

  function grantSelection(request) {
    const child = selectionRoutes.get(request);
    selectionRoutes.delete(request);
    if (child) post(child, "select-granted", {request});
    else activateSelection(request);
  }

  function coordinateSelection(request) {
    const generation = ++selectionGeneration;
    finishTree(`select:${request}`).then(success => {
      if (!success || generation !== selectionGeneration || !editMode) return;
      // Every old text session has reported its final patch before granting
      // another selection, including sessions inside nested component frames.
      if (selected) selected.classList.remove(SELECTED_CLASS);
      selected = null;
      broadcast({type: "clear-selected-outline"});
      grantSelection(request);
    });
  }

  function chooseElement(target) {
    if (!target) return;
    const request = `${frameKey()}:${Date.now()}:${++serial}`;
    pendingLocalSelection = {request, target};
    if (window === window.top) coordinateSelection(request);
    else post(window.parent, "select-request", {request});
  }

  function fromInlineEditor(event) {
    return Boolean(inlineEditor && (event.composedPath?.() || []).includes(inlineEditor.host));
  }

  function positionInlineEditor() {
    const state = inlineEditor;
    if (!state) return;
    if (!state.element.isConnected) {
      finishInlineEdit(false);
      return;
    }
    const rect = state.element.getBoundingClientRect();
    const availableWidth = Math.max(80, innerWidth - 16);
    const width = Math.min(availableWidth, Math.max(100, rect.width + 12));
    state.host.style.setProperty("left", `${Math.max(4, Math.min(rect.left - 6, innerWidth - width - 4))}px`, "important");
    state.host.style.setProperty("top", `${Math.max(4, rect.top - 4)}px`, "important");
    state.host.style.setProperty("width", `${width}px`, "important");
    state.input.style.height = "0px";
    state.input.style.height = `${Math.max(rect.height + 8, state.input.scrollHeight + 4, 32)}px`;
  }

  function publishInlineText() {
    const state = inlineEditor;
    if (!state) return;
    const text = state.input.value;
    if (text === state.lastText) return;
    state.lastText = text;
    applyLocalPatch(state.id, {text});
    emitInline({id: state.id, session: state.session, phase: "update", text});
    positionInlineEditor();
  }

  function finishInlineEdit(cancel = false) {
    const state = inlineEditor;
    if (!state) return;
    if (cancel) {
      const patch = {...(currentSettings[state.id] || {})};
      if (state.hadOverride) patch.text = state.previousOverride;
      else delete patch.text;
      const settings = {...currentSettings};
      if (Object.keys(patch).length) settings[state.id] = patch;
      else delete settings[state.id];
      applyLocalAll(settings);
    } else publishInlineText();
    // Removing the editor can trigger blur. Clear its state before doing so.
    inlineEditor = null;
    state.host.remove();
    emitInline({
      id: state.id, session: state.session,
      phase: cancel ? "cancel" : "finish",
      text: cancel ? state.original : state.lastText,
    });
  }

  function beginInlineEdit(element) {
    const target = textTargetFor(element);
    if (!editMode || !target || !document.body) return;
    finishInlineEdit(false);
    const computed = getComputedStyle(element);
    const id = generatedId(element);
    const original = target.textContent || "";
    const host = document.createElement("div");
    host.id = "hps-inline-editor";
    host.dataset.hpsIgnore = "true";
    for (const [key, value] of Object.entries({
      all: "initial", position: "fixed", "z-index": "2147483647",
      display: "block", "box-sizing": "border-box", margin: "0", padding: "0",
      border: "0", outline: "none", overflow: "visible", "pointer-events": "auto",
      transform: "none", visibility: "visible", opacity: "1",
    })) host.style.setProperty(key, value, "important");
    const shadow = host.attachShadow({mode: "open"});
    const css = document.createElement("style");
    css.textContent = `
      :host{color-scheme:normal}
      textarea{display:block;width:100%;min-height:32px;box-sizing:border-box;
        padding:2px 4px;margin:0;border:2px solid #ff6bb5;border-radius:4px;
        outline:none;resize:none;overflow:hidden;white-space:pre-wrap;overflow-wrap:break-word;
        cursor:text;caret-color:currentColor;box-shadow:0 5px 25px rgba(0,0,0,.3)}
      .hint{display:inline-block;max-width:100%;box-sizing:border-box;padding:5px 8px;
        border-radius:0 0 5px 5px;background:#19253d;color:#e7edff;
        font:11px/1.4 system-ui,Segoe UI,sans-serif;user-select:none;pointer-events:none}
    `;
    const input = document.createElement("textarea");
    input.id = "hps-inline-text";
    input.spellcheck = false;
    input.setAttribute("aria-label", `Edit ${labelFor(element)}`);
    input.setAttribute("aria-describedby", "hps-inline-hint");
    input.setAttribute("autocomplete", "off");
    input.value = original;
    for (const property of ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight",
      "letterSpacing", "textAlign", "textTransform", "color", "direction"]) {
      input.style[property] = computed[property];
    }
    const components = computed.color.match(/[\d.]+/g)?.slice(0, 3).map(Number) || [255, 255, 255];
    const luminance = (components[0] * .2126 + components[1] * .7152 + components[2] * .0722) / 255;
    input.style.background = luminance > .55 ? "#111b2f" : "#fff";
    const hint = document.createElement("div");
    hint.id = "hps-inline-hint";
    hint.className = "hint";
    hint.textContent = "Type to edit · Enter to apply · Esc to cancel";
    shadow.append(css, input, hint);
    const existing = currentSettings[id];
    inlineEditor = {
      element, id, host, input, original, lastText: original, composing: false,
      session: `${frameKey()}:${Date.now()}:${++serial}`,
      hadOverride: Boolean(existing && Object.prototype.hasOwnProperty.call(existing, "text")),
      previousOverride: existing?.text,
    };
    const state = inlineEditor;
    input.addEventListener("input", publishInlineText);
    input.addEventListener("compositionstart", () => { state.composing = true; });
    input.addEventListener("compositionend", () => {
      state.composing = false;
      if (inlineEditor === state) publishInlineText();
    });
    input.addEventListener("keydown", event => {
      event.stopPropagation();
      if (event.isComposing || state.composing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.preventDefault();
        finishInlineEdit(true);
      } else if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        finishInlineEdit(false);
      }
    });
    input.addEventListener("paste", event => {
      event.preventDefault();
      event.stopPropagation();
      const text = event.clipboardData?.getData("text/plain") || "";
      input.setRangeText(text, input.selectionStart, input.selectionEnd, "end");
      publishInlineText();
    });
    input.addEventListener("blur", () => {
      if (inlineEditor === state) finishInlineEdit(false);
    });
    for (const name of ["pointerdown", "pointerup", "mousedown", "mouseup", "click", "dblclick",
      "keydown", "keyup", "input", "change", "paste", "compositionstart", "compositionend"]) {
      shadow.addEventListener(name, event => event.stopPropagation());
    }
    document.body.appendChild(host);
    positionInlineEditor();
    emitInline({id, session: state.session, phase: "start", text: original});
    input.focus({preventScroll: true});
    input.select();
  }

  function directChildren() {
    return Array.from(document.querySelectorAll("iframe"), frame => frame.contentWindow).filter(Boolean);
  }

  function finishTree(request) {
    finishInlineEdit(false);
    const children = new Set(directChildren().filter(child => knownChildren.has(child)));
    if (!children.size) return Promise.resolve(true);
    return new Promise(resolve => {
      const state = {children, success: true, resolve, timer: null};
      state.timer = setTimeout(() => {
        childFlushes.delete(request);
        resolve(false);
      }, 2200);
      childFlushes.set(request, state);
      for (const child of children) post(child, "flush", {request});
    });
  }

  function receiveChildFlush(source, message) {
    const state = childFlushes.get(message.request);
    if (!state || !state.children.delete(source)) return;
    state.success = state.success && message.success === true;
    if (!state.children.size) {
      clearTimeout(state.timer);
      childFlushes.delete(message.request);
      state.resolve(state.success);
    }
  }

  window.addEventListener("resize", positionInlineEditor);
  document.addEventListener("scroll", positionInlineEditor, true);
  window.addEventListener("blur", () => finishInlineEdit(false));
  window.addEventListener("pagehide", () => finishInlineEdit(false));

  document.addEventListener("pointerover", event => {
    if (!editMode || fromInlineEditor(event)) return;
    const target = editableTarget(event);
    if (!target || target === selected || target === hovered) return;
    clearHover();
    hovered = target;
    hovered.classList.add(HOVER_CLASS);
  }, true);

  document.addEventListener("pointerout", event => {
    if (!editMode || fromInlineEditor(event)) return;
    const target = editableTarget(event);
    if (target && target === hovered) clearHover();
  }, true);

  document.addEventListener("pointerdown", event => {
    if (!editMode || fromInlineEditor(event)) return;
    const target = editableTarget(event);
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    chooseElement(target);
  }, true);

  // Stop the website action that normally follows pointerdown. This prevents
  // Streamlit buttons and links from firing while the user is selecting them.
  for (const eventName of ["mousedown", "mouseup", "click", "dblclick"]) {
    document.addEventListener(eventName, event => {
      if (!editMode || fromInlineEditor(event)) return;
      const target = editableTarget(event);
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
    }, true);
  }

  function parseTargetId(id) {
    if (!String(id).startsWith("css|")) return null;
    const parts = String(id).split("|");
    if (parts.length < 3) return null;
    return {
      frame: decodeURIComponent(parts[1]),
      selector: decodeURIComponent(parts.slice(2).join("|"))
    };
  }

  function targetFor(id) {
    const parsed = parseTargetId(id);
    if (parsed) {
      if (parsed.frame !== frameKey()) return null;
      try {
        return document.querySelector(parsed.selector);
      } catch (_error) {
        return null;
      }
    }
    try {
      return Array.from(document.querySelectorAll("[data-editor-id]")).find(
        element => element.dataset.editorId === String(id)
      ) || null;
    } catch (_error) {
      return null;
    }
  }

  function setPixelStyle(element, property, value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) {
      element.style.removeProperty(property);
    } else {
      element.style.setProperty(property, `${number}px`, "important");
    }
  }

  function fieldState(element, key) {
    return editedElements.get(element)?.get(key);
  }

  function rememberField(element, key, state) {
    if (!editedElements.has(element)) editedElements.set(element, new Map());
    editedElements.get(element).set(key, state);
  }

  function inlineValue(element, property) {
    return {value: element.style.getPropertyValue(property), priority: element.style.getPropertyPriority(property)};
  }

  function sameStyle(left, right) {
    return left.value === right.value && left.priority === right.priority;
  }

  function changeStyle(element, key, apply) {
    const previous = fieldState(element, key);
    const originals = new Map();
    for (const property of styleProperties[key]) {
      const current = inlineValue(element, property);
      const remembered = previous?.properties.get(property);
      // A live app may update this field between renders. Its newer value
      // becomes the baseline instead of restoring an obsolete value later.
      originals.set(property, remembered && sameStyle(current, remembered.applied)
        ? remembered.original : current);
    }
    apply();
    const properties = new Map();
    for (const [property, original] of originals) {
      const applied = inlineValue(element, property);
      if (!sameStyle(original, applied)) properties.set(property, {original, applied});
    }
    if (properties.size) rememberField(element, key, {kind: "style", properties});
    else forgetField(element, key);
  }

  function changeText(element, value) {
    let target = textTargetFor(element);
    if (!target) return;
    let created = false;
    if (target === element) {
      target = document.createTextNode("");
      element.appendChild(target);
      created = true;
    }
    const previous = fieldState(element, "text");
    const keepOriginal = previous?.target === target && target.nodeValue === previous.applied;
    const original = keepOriginal ? previous.original : target.nodeValue;
    created = keepOriginal ? previous.created : created;
    if (target.nodeValue !== value) target.nodeValue = value;
    if (created || original !== value) {
      rememberField(element, "text", {kind: "text", target, original, applied: value, created});
    } else forgetField(element, "text");
  }

  function changeSource(element, value) {
    const previous = fieldState(element, "source");
    const current = element.getAttribute("src");
    const original = previous && current === previous.applied ? previous.original : current;
    if (current !== value) element.setAttribute("src", value);
    if (original !== value) rememberField(element, "source", {kind: "source", original, applied: value});
    else forgetField(element, "source");
  }

  function forgetField(element, key) {
    const fields = editedElements.get(element);
    if (!fields) return;
    fields.delete(key);
    if (!fields.size) editedElements.delete(element);
  }

  function restoreField(element, key, state) {
    if (state.kind === "style") {
      for (const [property, saved] of state.properties) {
        if (!sameStyle(inlineValue(element, property), saved.applied)) continue;
        if (saved.original.value) element.style.setProperty(property, saved.original.value, saved.original.priority);
        else element.style.removeProperty(property);
      }
    } else if (state.kind === "text") {
      if (state.target.parentNode === element && state.target.nodeValue === state.applied) {
        if (state.created) state.target.remove();
        else state.target.nodeValue = state.original;
      }
    } else if (state.kind === "source" && element.getAttribute("src") === state.applied) {
      if (state.original === null) element.removeAttribute("src");
      else element.setAttribute("src", state.original);
    }
    forgetField(element, key);
  }

  function applyPatch(element, patch) {
    if (!element || !patch) return;
    const allowed = new Set(capabilitiesFor(element).supported_fields);
    const supports = key => allowed.has(key) && patch[key] !== undefined;
    for (const [key, state] of editedElements.get(element) || []) {
      if (!allowed.has(key)) restoreField(element, key, state);
    }

    if (supports("text")) {
      changeText(element, String(patch.text ?? ""));
    }

    if (supports("source")) {
      changeSource(element, String(patch.source || ""));
    }
    if (supports("font_size")) changeStyle(element, "font_size", () => setPixelStyle(element, "font-size", patch.font_size));
    if (supports("width")) changeStyle(element, "width", () => setPixelStyle(element, "width", patch.width));
    if (supports("height")) changeStyle(element, "height", () => setPixelStyle(element, "height", patch.height));
    if (supports("padding")) changeStyle(element, "padding", () => element.style.setProperty("padding", `${Number(patch.padding) || 0}px`, "important"));
    if (supports("margin")) changeStyle(element, "margin", () => element.style.setProperty("margin", `${Number(patch.margin) || 0}px`, "important"));
    for (const [key, property] of [["color", "color"], ["background_color", "background-color"], ["border_color", "border-color"]]) {
      if (supports(key)) changeStyle(element, key, () => {
        if (patch[key]) element.style.setProperty(property, patch[key], "important");
        else element.style.removeProperty(property);
      });
    }
    if (supports("border_width")) {
      changeStyle(element, "border_width", () => {
        element.style.setProperty("border-width", `${Number(patch.border_width) || 0}px`, "important");
        if (Number(patch.border_width) > 0 && computedStyleSafe(element, "border-style") === "none") {
          element.style.setProperty("border-style", "solid", "important");
        }
      });
    }
    if (supports("border_radius")) changeStyle(element, "border_radius", () => element.style.setProperty("border-radius", `${Number(patch.border_radius) || 0}px`, "important"));
    if (supports("opacity")) changeStyle(element, "opacity", () => element.style.setProperty("opacity", String(patch.opacity), "important"));
    if (supports("visible")) changeStyle(element, "visible", () => {
      if (patch.visible) element.style.removeProperty("display");
      else element.style.setProperty("display", "none", "important");
    });
  }

  function computedStyleSafe(element, property) {
    try { return getComputedStyle(element).getPropertyValue(property); }
    catch (_error) { return ""; }
  }

  function applyLocalPatch(id, patch) {
    currentSettings = {...currentSettings, [id]: {...(currentSettings[id] || {}), ...patch}};
    applyLocalAll(currentSettings);
  }

  function applyLocalAll(settings) {
    if (!settings || typeof settings !== "object" || Array.isArray(settings)) return;
    currentSettings = settings;
    const desired = new Map();
    for (const [id, patch] of Object.entries(settings)) {
      if (!patch || typeof patch !== "object" || Array.isArray(patch)) continue;
      const element = targetFor(id);
      if (element) desired.set(element, {...(desired.get(element) || {}), ...patch});
    }
    for (const [element, fields] of editedElements) {
      if (!element.isConnected) {
        editedElements.delete(element);
        continue;
      }
      const patch = desired.get(element) || {};
      for (const [key, state] of fields) {
        if (patch[key] === undefined) restoreField(element, key, state);
      }
    }
    for (const [element, patch] of desired) applyPatch(element, patch);
  }

  function broadcast(payload) {
    for (const child of directChildren()) post(child, payload.type, payload);
  }

  window.hpsSetEditMode = enabled => {
    setLocalEditMode(enabled);
    if (!enabled) selectionGeneration += 1;
    broadcast({type: "edit-mode", payload: Boolean(enabled)});
  };

  window.hpsApplyPatch = (id, patch) => {
    applyLocalPatch(id, patch);
    broadcast({type: "patch", id, patch});
  };

  window.hpsApplyAll = settings => {
    applyLocalAll(settings || {});
    broadcast({type: "settings", payload: settings || {}});
  };

  window.hpsClearSelection = () => {
    selectionGeneration += 1;
    clearSelection();
    broadcast({type: "clear-selection"});
  };

  window.hpsCommitInlineEdit = request => {
    selectionGeneration += 1;
    finishTree(String(request)).then(success => {
      if (window === window.top) sendBridge("inlineFlushed", {request, success});
      else post(window.parent, "flushed", {request, success});
    });
  };

  window.addEventListener("message", event => {
    const message = event.data;
    if (!message || message.source !== MESSAGE_PREFIX || message.document_token !== documentToken) return;
    const childMessage = directChildren().includes(event.source);
    const parentMessage = window !== window.top && event.source === window.parent;
    if (!childMessage && !parentMessage) return;

    if (childMessage) {
      if (message.type === "ready") {
        knownChildren.add(event.source);
        post(event.source, "edit-mode", {payload: editMode});
        post(event.source, "settings", {payload: currentSettings});
        return;
      }
      if (!knownChildren.has(event.source)) return;
      if (message.type === "selection") emitSelection(message.payload);
      if (message.type === "inline") emitInline(message.payload);
      if (message.type === "log") emitLog(message.payload);
      if (message.type === "flushed") receiveChildFlush(event.source, message);
      if (message.type === "select-request") {
        selectionRoutes.set(message.request, event.source);
        if (window === window.top) coordinateSelection(message.request);
        else post(window.parent, "select-request", {request: message.request});
      }
      return;
    }
    if (message.type === "select-granted") {
      grantSelection(message.request);
      return;
    }
    if (message.type === "flush") {
      finishTree(message.request).then(success => {
        post(window.parent, "flushed", {request: message.request, success});
      });
      return;
    }
    if (message.type === "edit-mode") setLocalEditMode(message.payload);
    if (message.type === "settings") applyLocalAll(message.payload || {});
    if (message.type === "patch") applyLocalPatch(message.id, message.patch || {});
    if (message.type === "clear-selection") clearSelection();
    if (message.type === "clear-selected-outline") {
      if (selected) selected.classList.remove(SELECTED_CLASS);
      selected = null;
    }
    broadcast(message);
  });

  const observer = new MutationObserver(() => {
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(() => {
      applyLocalAll(currentSettings);
      positionInlineEditor();
    }, 90);
  });
  if (document.body) observer.observe(document.body, {childList: true, characterData: true, subtree: true});

  if (window === window.top) {
    connectBridge();
    const bridgeTimer = setInterval(() => {
      connectBridge();
      if (bridge) clearInterval(bridgeTimer);
    }, 300);
  }

  if (window !== window.top) {
    post(window.parent, "ready");
  }
  emitLog(`Visual editor script ready (${frameKey()})`);
})();

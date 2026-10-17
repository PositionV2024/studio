(() => {
  if (window.__hoyoProjectStudioInstalled) return;
  window.__hoyoProjectStudioInstalled = true;

  const SELECTED_CLASS = "hps-editor-selected";
  const HOVER_CLASS = "hps-editor-hover";
  const MESSAGE_PREFIX = "hps-studio";
  const ignoredTags = new Set([
    "html", "body", "head", "script", "style", "link", "meta", "noscript"
  ]);

  let editMode = false;
  let selected = null;
  let hovered = null;
  let bridge = null;
  let pendingSelection = null;
  let currentSettings = {};
  let mutationTimer = null;

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

  function describe(element) {
    const computed = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    const type = inferType(element);
    const tag = element.tagName.toLowerCase();
    let text = element.dataset?.editorText || element.textContent?.trim().slice(0, 1000) || "";
    if (type === "input") text = element.value || element.getAttribute("placeholder") || "";

    return {
      id: generatedId(element),
      label: labelFor(element),
      type,
      tag,
      text,
      source: tag === "img" ? (element.currentSrc || element.src || "") : "",
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
        opacity: parseFloat(computed.opacity) || 1,
        visible: computed.display !== "none" && computed.visibility !== "hidden"
      }
    };
  }

  function connectBridge() {
    if (bridge || !window.qt || !qt.webChannelTransport) return;
    if (document.querySelector("script[data-hps-webchannel]")) return;
    const script = document.createElement("script");
    script.dataset.hpsWebchannel = "true";
    script.src = "qrc:///qtwebchannel/qwebchannel.js";
    script.onload = () => {
      new QWebChannel(qt.webChannelTransport, channel => {
        bridge = channel.objects.studioBridge;
        if (bridge) {
          bridge.log(`Visual editor bridge connected (${frameKey()})`);
          if (pendingSelection) {
            bridge.selectElement(JSON.stringify(pendingSelection));
            pendingSelection = null;
          }
        }
      });
    };
    document.documentElement.appendChild(script);
  }

  function emitSelection(payload) {
    if (window === window.top) {
      if (bridge) bridge.selectElement(JSON.stringify(payload));
      else pendingSelection = payload;
    } else {
      window.top.postMessage(
        {source: MESSAGE_PREFIX, type: "selection", payload},
        "*"
      );
    }
  }

  function emitLog(message) {
    if (window === window.top) {
      if (bridge) bridge.log(String(message));
    } else {
      window.top.postMessage(
        {source: MESSAGE_PREFIX, type: "log", payload: String(message)},
        "*"
      );
    }
  }

  function clearHover() {
    if (hovered) hovered.classList.remove(HOVER_CLASS);
    hovered = null;
  }

  function clearSelection() {
    if (selected) selected.classList.remove(SELECTED_CLASS);
    selected = null;
  }

  function setLocalEditMode(enabled) {
    editMode = Boolean(enabled);
    document.documentElement.classList.toggle("hps-editing", editMode);
    if (!editMode) {
      clearHover();
      clearSelection();
    }
  }

  function chooseElement(target) {
    if (!target) return;
    clearHover();
    clearSelection();
    selected = target;
    selected.classList.add(SELECTED_CLASS);
    selected.scrollIntoView({block: "nearest", inline: "nearest"});
    emitSelection(describe(selected));
  }

  document.addEventListener("pointerover", event => {
    if (!editMode) return;
    const target = editableTarget(event);
    if (!target || target === selected || target === hovered) return;
    clearHover();
    hovered = target;
    hovered.classList.add(HOVER_CLASS);
  }, true);

  document.addEventListener("pointerout", event => {
    if (!editMode) return;
    const target = editableTarget(event);
    if (target && target === hovered) clearHover();
  }, true);

  document.addEventListener("pointerdown", event => {
    if (!editMode) return;
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
      if (!editMode) return;
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

  function applyPatch(element, patch) {
    if (!element || !patch) return;
    const type = inferType(element);
    const tag = element.tagName.toLowerCase();

    if (Object.prototype.hasOwnProperty.call(patch, "text")) {
      const value = String(patch.text ?? "");
      if (["input", "textarea", "select"].includes(tag)) {
        element.value = value;
        element.dispatchEvent(new Event("input", {bubbles: true}));
        element.dispatchEvent(new Event("change", {bubbles: true}));
      } else if (["text", "button"].includes(type)) {
        element.textContent = value;
      }
      element.dataset.editorText = value;
    }

    if (patch.source !== undefined && tag === "img") {
      element.src = String(patch.source || "");
    }
    if (patch.font_size !== undefined) setPixelStyle(element, "font-size", patch.font_size);
    if (patch.width !== undefined) setPixelStyle(element, "width", patch.width);
    if (patch.height !== undefined) setPixelStyle(element, "height", patch.height);
    if (patch.padding !== undefined) element.style.setProperty("padding", `${Number(patch.padding) || 0}px`, "important");
    if (patch.margin !== undefined) element.style.setProperty("margin", `${Number(patch.margin) || 0}px`, "important");
    if (patch.color) element.style.setProperty("color", patch.color, "important");
    if (patch.background_color) element.style.setProperty("background-color", patch.background_color, "important");
    if (patch.border_color) element.style.setProperty("border-color", patch.border_color, "important");
    if (patch.border_width !== undefined) {
      element.style.setProperty("border-width", `${Number(patch.border_width) || 0}px`, "important");
      if (Number(patch.border_width) > 0 && computedStyleSafe(element, "border-style") === "none") {
        element.style.setProperty("border-style", "solid", "important");
      }
    }
    if (patch.border_radius !== undefined) element.style.setProperty("border-radius", `${Number(patch.border_radius) || 0}px`, "important");
    if (patch.opacity !== undefined) element.style.setProperty("opacity", String(patch.opacity), "important");
    if (patch.visible !== undefined) element.style.setProperty("display", patch.visible ? "" : "none", "important");
  }

  function computedStyleSafe(element, property) {
    try { return getComputedStyle(element).getPropertyValue(property); }
    catch (_error) { return ""; }
  }

  function applyLocalPatch(id, patch) {
    applyPatch(targetFor(id), patch);
  }

  function applyLocalAll(settings) {
    if (!settings || typeof settings !== "object") return;
    currentSettings = settings;
    Object.entries(settings).forEach(([id, patch]) => applyLocalPatch(id, patch));
  }

  function broadcast(payload) {
    document.querySelectorAll("iframe").forEach(frame => {
      try { frame.contentWindow.postMessage(payload, "*"); }
      catch (_error) { /* A frame may disappear during a Streamlit rerun. */ }
    });
  }

  window.hpsSetEditMode = enabled => {
    setLocalEditMode(enabled);
    broadcast({source: MESSAGE_PREFIX, type: "edit-mode", payload: Boolean(enabled)});
  };

  window.hpsApplyPatch = (id, patch) => {
    if (!currentSettings[id]) currentSettings[id] = {};
    currentSettings[id] = {...currentSettings[id], ...patch};
    applyLocalPatch(id, patch);
    broadcast({source: MESSAGE_PREFIX, type: "patch", id, patch});
  };

  window.hpsApplyAll = settings => {
    applyLocalAll(settings || {});
    broadcast({source: MESSAGE_PREFIX, type: "settings", payload: settings || {}});
  };

  window.hpsClearSelection = () => {
    clearSelection();
    broadcast({source: MESSAGE_PREFIX, type: "clear-selection"});
  };

  window.addEventListener("message", event => {
    const message = event.data;
    if (!message || message.source !== MESSAGE_PREFIX) return;

    if (message.type === "selection" && window === window.top) {
      if (bridge) bridge.selectElement(JSON.stringify(message.payload));
      else pendingSelection = message.payload;
      return;
    }
    if (message.type === "log" && window === window.top) {
      if (bridge) bridge.log(String(message.payload));
      return;
    }
    if (message.type === "ready" && window === window.top) {
      try {
        event.source.postMessage(
          {source: MESSAGE_PREFIX, type: "edit-mode", payload: editMode},
          "*"
        );
        event.source.postMessage(
          {source: MESSAGE_PREFIX, type: "settings", payload: currentSettings},
          "*"
        );
      } catch (_error) {}
      return;
    }
    if (message.type === "edit-mode") setLocalEditMode(message.payload);
    if (message.type === "settings") applyLocalAll(message.payload || {});
    if (message.type === "patch") applyLocalPatch(message.id, message.patch || {});
    if (message.type === "clear-selection") clearSelection();
  });

  const observer = new MutationObserver(() => {
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(() => applyLocalAll(currentSettings), 90);
  });
  if (document.body) observer.observe(document.body, {childList: true, subtree: true});

  connectBridge();
  const bridgeTimer = setInterval(() => {
    connectBridge();
    if (bridge) clearInterval(bridgeTimer);
  }, 300);

  if (window !== window.top) {
    window.top.postMessage({source: MESSAGE_PREFIX, type: "ready"}, "*");
  }
  emitLog(`Visual editor script ready (${frameKey()})`);
})();

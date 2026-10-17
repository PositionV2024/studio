# Adapting a Streamlit project for HoYo Project Studio

HoYo Project Studio can select and visually modify ordinary browser elements at runtime. For the most stable saved edits, add explicit editor IDs to important custom HTML elements.

## Stable editor IDs

```html
<h1 data-editor-id="header.title" data-editor-type="text">
    HoYo Profile Viewer
</h1>
```

```html
<section data-editor-id="uid-search.panel" data-editor-type="container">
    ...
</section>
```

When an element has no `data-editor-id`, the editor creates a CSS-based selector automatically. This works well for testing, but explicit IDs are more reliable across Streamlit reruns and future layout changes.

## Publishing visual settings

The editor saves changes in:

```text
.studio/draft.json
```

Selecting **Publish** copies the draft into:

```text
.studio/published.json
```

The embedded preview applies both explicit-ID and generated-selector changes automatically. A public deployment must load `published.json` itself if those changes should remain outside Project Studio.

The included `studio_runtime.py` contains helper functions for reading values and constructing inline styles from published settings.

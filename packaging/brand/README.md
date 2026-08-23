# Brand assets

`dme-innovation-logo.ai` is the **master** DME Innovation wordmark (Adobe
Illustrator, PDF-1.6 container, 968 × 432 pt) supplied by DME Innovation GmbH.
Everything else is derived from it:

| Derived file | What it is | How to regenerate |
|---|---|---|
| `webui/public/assets/dme-logo.svg` | the wordmark in the app header (vector, `currentColor`) | export from the master as SVG |
| `packaging/icon.png` / `icon.ico` | the app icon: the **DME** mark in white on the papaya brand square | `python packaging/brand/make_icon.py` |

The app icon deliberately uses only the large **DME** row of the wordmark — the
"INNOVATION" line is too fine to stay legible at 16 px, where a taskbar icon
lives. The icon script renders each ICO size separately with size-appropriate
padding so the strokes never fall below one pixel.

Brand colour: papaya **#FF7A00** (the same accent the UI uses).
